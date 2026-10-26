//! Network, disk and decoding worker. Publishes snapshots under a mutex.
const std = @import("std");
const builtin = @import("builtin");
const fs = @import("io.zig");
const feed = @import("feed.zig");
const codec = @import("codec.zig");
const cachemod = @import("cache.zig");
const c = @cImport({
    @cInclude("pthread.h");
    @cInclude("unistd.h");
    @cInclude("fcntl.h");
    @cInclude("time.h");
    @cInclude("stdlib.h");
    @cInclude("sys/wait.h");
    @cInclude("sys/resource.h");
    @cInclude("signal.h");
    @cInclude("errno.h");
});

pub const Status = enum { starting, cached, refreshing, ready, offline, save_failed };

pub const Settings = struct {
    feed_url: []const u8 = feed.default_url,
    interval_seconds: u32 = 1800,
    twelve_hour: bool = false,
    fill: bool = true,
};

pub fn validSettings(settings: Settings) bool {
    return feed.validUrl(settings.feed_url) and settings.interval_seconds >= 60 and settings.interval_seconds <= 86400;
}

pub fn loadSettings(a: std.mem.Allocator, root: []const u8) Settings {
    const path = std.fmt.allocPrint(a, "{s}/settings.json", .{root}) catch return .{};
    defer a.free(path);
    const bytes = fs.read(a, path, 8192) catch return .{};
    defer a.free(bytes);
    const parsed = std.json.parseFromSlice(Settings, a, bytes, .{ .allocate = .alloc_always, .ignore_unknown_fields = true }) catch return .{};
    defer parsed.deinit();
    if (!validSettings(parsed.value)) return .{};
    var value = parsed.value;
    value.feed_url = a.dupe(u8, value.feed_url) catch return .{};
    return value;
}

pub const Snapshot = struct {
    image: codec.Image,
    caption: []const u8,
    source: []const u8,
    credit: []const u8,
    source_url: []const u8,
    count: usize,

    pub fn destroy(self: *Snapshot, a: std.mem.Allocator) void {
        a.free(self.image.pixels);
        a.free(self.caption);
        a.free(self.source);
        a.free(self.credit);
        a.free(self.source_url);
        a.destroy(self);
    }
};

pub const Worker = struct {
    a: std.mem.Allocator,
    root: []const u8,
    cache_root: []const u8,
    mutex: c.pthread_mutex_t = undefined,
    condition: c.pthread_cond_t = undefined,
    thread: ?std.Thread = null,
    pipes: [2]c_int = .{ -1, -1 },
    latest: ?*Snapshot = null,
    status: Status = .starting,
    save_error: bool = false,
    stop: bool = false,
    network_enabled: bool = true,
    delta: i32 = 0,
    refresh: bool = true,
    dirty: bool = false,
    url_buffer: [2048]u8 = @splat(0),
    url_len: usize = 0,
    interval: u32 = 1800,
    twelve: bool = false,
    fill: bool = true,

    pub fn init(a: std.mem.Allocator, root: []const u8, settings: Settings) !*Worker {
        return initWithNetwork(a, root, settings, true);
    }

    pub fn initWithNetwork(a: std.mem.Allocator, root: []const u8, settings: Settings, network: bool) !*Worker {
        if (!validSettings(settings)) return error.InvalidSettings;
        const self = try a.create(Worker);
        errdefer a.destroy(self);
        self.* = .{
            .a = a,
            .root = try a.dupe(u8, root),
            .cache_root = try std.fmt.allocPrint(a, "{s}/cache", .{root}),
        };
        if (c.pthread_mutex_init(&self.mutex, null) != 0) return error.Mutex;
        if (c.pthread_cond_init(&self.condition, null) != 0) return error.Condition;
        if (c.pipe(&self.pipes) != 0) return error.Pipe;
        for (self.pipes) |fd| {
            _ = c.fcntl(fd, c.F_SETFD, c.FD_CLOEXEC);
            _ = c.fcntl(fd, c.F_SETFL, c.O_NONBLOCK);
        }
        self.refresh = network;
        self.network_enabled = network;
        self.setValues(settings);
        self.dirty = false;
        self.thread = try std.Thread.spawn(.{}, run, .{self});
        return self;
    }

    pub fn deinit(self: *Worker) void {
        self.lock();
        self.stop = true;
        _ = c.pthread_cond_signal(&self.condition);
        self.unlock();
        if (self.thread) |thread| thread.join();
        if (self.latest) |snapshot| snapshot.destroy(self.a);
        for (self.pipes) |fd| _ = c.close(fd);
        _ = c.pthread_cond_destroy(&self.condition);
        _ = c.pthread_mutex_destroy(&self.mutex);
        self.a.free(self.root);
        self.a.free(self.cache_root);
        self.a.destroy(self);
    }

    fn lock(self: *Worker) void {
        _ = c.pthread_mutex_lock(&self.mutex);
    }

    fn unlock(self: *Worker) void {
        _ = c.pthread_mutex_unlock(&self.mutex);
    }

    pub fn take(self: *Worker) ?*Snapshot {
        var buf: [64]u8 = undefined;
        while (c.read(self.pipes[0], &buf, buf.len) > 0) {}
        self.lock();
        defer self.unlock();
        const result = self.latest;
        self.latest = null;
        return result;
    }

    pub fn state(self: *Worker) Status {
        self.lock();
        defer self.unlock();
        return if (self.save_error) .save_failed else self.status;
    }

    pub fn next(self: *Worker, delta: i32) void {
        self.lock();
        self.delta = std.math.clamp(self.delta + delta, -12, 12);
        _ = c.pthread_cond_signal(&self.condition);
        self.unlock();
    }

    pub fn refreshFeed(self: *Worker) void {
        self.lock();
        if (self.network_enabled) self.refresh = true;
        _ = c.pthread_cond_signal(&self.condition);
        self.unlock();
    }

    pub fn configure(self: *Worker, settings: Settings) void {
        if (!validSettings(settings)) return;
        self.lock();
        defer self.unlock();
        const changed = !std.mem.eql(u8, self.url_buffer[0..self.url_len], settings.feed_url);
        self.setValues(settings);
        self.dirty = true;
        self.refresh = self.network_enabled and (self.refresh or changed);
        _ = c.pthread_cond_signal(&self.condition);
    }

    fn setValues(self: *Worker, settings: Settings) void {
        self.url_len = settings.feed_url.len;
        @memcpy(self.url_buffer[0..self.url_len], settings.feed_url);
        self.interval = settings.interval_seconds;
        self.twelve = settings.twelve_hour;
        self.fill = settings.fill;
    }

    fn wake(self: *Worker) void {
        const byte = [_]u8{1};
        _ = c.write(self.pipes[1], &byte, 1);
    }

    fn setStatus(self: *Worker, status: Status) void {
        self.lock();
        self.status = status;
        self.unlock();
        self.wake();
    }

    fn publish(self: *Worker, image: codec.Image, entry: feed.Entry, count: usize) !void {
        const snapshot = try self.a.create(Snapshot);
        snapshot.* = .{
            .image = image,
            .caption = try self.a.dupe(u8, entry.caption),
            .source = try self.a.dupe(u8, entry.source),
            .source_url = try self.a.dupe(u8, entry.source_url),
            .credit = try self.a.dupe(u8, entry.credit),
            .count = count,
        };
        self.lock();
        const old = self.latest;
        self.latest = snapshot;
        self.unlock();
        if (old) |previous| previous.destroy(self.a);
        self.wake();
    }

    fn show(self: *Worker, cache: *cachemod.Cache, index: usize) !void {
        const photo = cache.photos.items[index];
        const path = try std.fmt.allocPrint(self.a, "{s}/{s}", .{ self.cache_root, photo.file });
        defer self.a.free(path);
        const bytes = try fs.read(self.a, path, codec.limit);
        defer self.a.free(bytes);
        const image = try codec.decode(self.a, bytes);
        try self.publish(image, .{
            .url = photo.url,
            .caption = photo.caption,
            .source = photo.source,
            .source_url = photo.source_url,
            .credit = photo.credit,
        }, cache.photos.items.len);
    }

    fn urgent(self: *Worker) bool {
        self.lock();
        defer self.unlock();
        return self.stop or self.delta != 0 or self.dirty;
    }

    fn download(self: *Worker, url: []const u8, path: []const u8, maximum: usize) ![]u8 {
        if (!feed.validUrl(url)) return error.Url;
        const zurl = try self.a.dupeZ(u8, url);
        defer self.a.free(zurl);
        const zpath = try self.a.dupeZ(u8, path);
        defer self.a.free(zpath);
        var limit_buf: [32]u8 = undefined;
        const limit_text = try std.fmt.bufPrintSentinel(&limit_buf, "{d}", .{maximum}, 0);
        var args = [_:null]?[*:0]const u8{
            "/usr/bin/curl",
            "--fail",
            "--silent",
            "--location",
            "--max-redirs",
            "3",
            "--proto",
            "=http,https",
            "--proto-redir",
            "=http,https",
            "--connect-timeout",
            "5",
            "--max-time",
            "15",
            "--max-filesize",
            limit_text,
            "--output",
            zpath,
            "--",
            zurl,
        };
        defer fs.remove(self.a, path);
        const pid = c.fork();
        if (pid < 0) return error.Spawn;
        if (pid == 0) {
            var limit = c.struct_rlimit{ .rlim_cur = @intCast(maximum), .rlim_max = @intCast(maximum) };
            if (c.setrlimit(c.RLIMIT_FSIZE, &limit) != 0) c._exit(126);
            const null_fd = c.open("/dev/null", c.O_RDWR);
            if (null_fd >= 0) {
                _ = c.dup2(null_fd, 1);
                _ = c.dup2(null_fd, 2);
                _ = c.close(null_fd);
            }
            _ = c.execv(args[0].?, @ptrCast(&args));
            c._exit(127);
        }
        var status: c_int = 0;
        while (true) {
            const waited = c.waitpid(pid, &status, c.WNOHANG);
            if (waited == pid) break;
            if (waited < 0) {
                const number = if (builtin.os.tag == .macos) c.__error().* else c.__errno_location().*;
                if (number == c.EINTR) continue;
                return error.Wait;
            }
            if (self.urgent()) {
                _ = c.kill(pid, c.SIGKILL);
                while (c.waitpid(pid, &status, 0) < 0) {
                    const number = if (builtin.os.tag == .macos) c.__error().* else c.__errno_location().*;
                    if (number != c.EINTR) break;
                }
                return error.Interrupted;
            }
            _ = c.usleep(20_000);
        }
        if (status != 0) return error.Download;
        return fs.read(self.a, path, maximum);
    }

    fn run(self: *Worker) void {
        fs.ensureDir(self.root) catch {};
        fs.ensureDir(self.cache_root) catch {};
        var cache = cachemod.Cache.init(self.a, self.cache_root);
        defer cache.deinit();
        cache.load() catch {};
        cache.prune();
        var selected: usize = 0;
        var shown_network = false;
        var changing_feed = false;
        for (cache.photos.items, 0..) |_, i| {
            if (self.show(&cache, i)) |_| {
                selected = i;
                shown_network = true;
                break;
            } else |_| {}
        }
        self.setStatus(if (shown_network) .cached else if (self.network_enabled) .starting else .offline);
        var feed_arena = std.heap.ArenaAllocator.init(self.a);
        defer feed_arena.deinit();
        var entries: []const feed.Entry = &.{};
        var at: usize = 0;
        var accepted: usize = 0;
        var refresh_deadline: i64 = 0;
        var next_refresh: i64 = 0;
        while (true) {
            self.lock();
            while (!self.stop and self.delta == 0 and !self.refresh and !self.dirty and at >= entries.len) {
                var until: c.struct_timespec = undefined;
                _ = c.clock_gettime(c.CLOCK_REALTIME, &until);
                until.tv_sec += 60;
                _ = c.pthread_cond_timedwait(&self.condition, &self.mutex, &until);
                if (self.network_enabled and monotonic() >= next_refresh) self.refresh = true;
            }
            const stop = self.stop;
            const delta = self.delta;
            self.delta = 0;
            const refresh = self.refresh;
            self.refresh = false;
            const dirty = self.dirty;
            self.dirty = false;
            const url = self.a.dupe(u8, self.url_buffer[0..self.url_len]) catch {
                self.unlock();
                return;
            };
            const settings = Settings{
                .feed_url = url,
                .interval_seconds = self.interval,
                .twelve_hour = self.twelve,
                .fill = self.fill,
            };
            self.unlock();
            defer self.a.free(url);
            if (stop) return;
            if (dirty) {
                const bytes = std.json.Stringify.valueAlloc(self.a, settings, .{}) catch continue;
                defer self.a.free(bytes);
                const path = std.fmt.allocPrint(self.a, "{s}/settings.json", .{self.root}) catch continue;
                defer self.a.free(path);
                if (fs.atomic(self.a, path, bytes)) |_| {
                    self.lock();
                    self.save_error = false;
                    self.unlock();
                    self.wake();
                } else |_| {
                    self.lock();
                    self.save_error = true;
                    self.unlock();
                    self.wake();
                }
            }
            if (delta != 0 and cache.photos.items.len > 0) {
                selected = @intCast(@mod(@as(i64, @intCast(selected)) + delta, @as(i64, @intCast(cache.photos.items.len))));
                self.show(&cache, selected) catch {};
                continue;
            }
            if (refresh) {
                // Release old metadata on each refresh.
                var compact = cachemod.Cache.init(self.a, self.cache_root);
                if (compact.load()) |_| {
                    cache.deinit();
                    cache = compact;
                } else |_| compact.deinit();
                next_refresh = monotonic() + 6 * 3600;
                self.setStatus(.refreshing);
                const path = std.fmt.allocPrint(self.a, "{s}/.feed", .{self.cache_root}) catch continue;
                defer self.a.free(path);
                const xml = self.download(url, path, 2 * 1024 * 1024) catch |err| {
                    if (err == error.Interrupted) {
                        self.lock();
                        self.refresh = true;
                        self.unlock();
                    } else {
                        self.setStatus(.offline);
                        next_refresh = monotonic() + 300;
                    }
                    continue;
                };
                defer self.a.free(xml);
                _ = feed_arena.reset(.retain_capacity);
                entries = feed.parse(feed_arena.allocator(), xml) catch {
                    entries = &.{};
                    self.setStatus(.offline);
                    continue;
                };
                entries = entries[0..@min(entries.len, 24)];
                at = 0;
                accepted = 0;
                refresh_deadline = monotonic() + 120;
                changing_feed = !std.mem.eql(u8, cache.feed_url, url);
            }
            if (at < entries.len) {
                if (monotonic() >= refresh_deadline) {
                    at = entries.len;
                    self.setStatus(.offline);
                    continue;
                }
                const entry = entries[at];
                var exists = false;
                for (cache.photos.items) |photo| if (std.mem.eql(u8, photo.url, entry.url)) {
                    exists = true;
                    break;
                };
                if (exists) {
                    at += 1;
                    accepted += 1;
                    if (accepted >= 12) at = entries.len;
                    if (at == entries.len) self.setStatus(.ready);
                    continue;
                }
                const image_url = feed.imageUrl(self.a, entry.url) catch {
                    at += 1;
                    continue;
                };
                defer self.a.free(image_url);
                const path = std.fmt.allocPrint(self.a, "{s}/.download", .{self.cache_root}) catch continue;
                defer self.a.free(path);
                const bytes = self.download(image_url, path, codec.limit) catch |err| {
                    if (err != error.Interrupted) at += 1;
                    self.setStatus(.offline);
                    continue;
                };
                defer self.a.free(bytes);
                const image = codec.decode(self.a, bytes) catch {
                    at += 1;
                    self.setStatus(.offline);
                    continue;
                };
                const previous_photos = cache.photos;
                const previous_feed = cache.feed_url;
                if (changing_feed) {
                    cache.photos = .empty;
                    cache.feed_url = cache.arena.allocator().dupe(u8, url) catch continue;
                }
                cache.add(entry, bytes, entry.url) catch {
                    cache.photos = previous_photos;
                    cache.feed_url = previous_feed;
                    self.a.free(image.pixels);
                    cache.prune();
                    at += 1;
                    continue;
                };
                if (!shown_network or changing_feed) {
                    selected = 0;
                    self.publish(image, entry, cache.photos.items.len) catch {};
                    shown_network = true;
                } else {
                    self.a.free(image.pixels);
                    selected = @min(selected + 1, cache.photos.items.len - 1);
                }
                changing_feed = false;
                at += 1;
                accepted += 1;
                if (accepted >= 12) at = entries.len;
                if (at == entries.len) self.setStatus(.ready);
            }
        }
    }
};

fn monotonic() i64 {
    var now: c.struct_timespec = undefined;
    _ = c.clock_gettime(c.CLOCK_MONOTONIC, &now);
    return now.tv_sec;
}
