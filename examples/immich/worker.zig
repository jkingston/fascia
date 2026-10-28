const std = @import("std");
const model = @import("model.zig");
const api = @import("api.zig");
const fs = @import("io.zig");
const codec = @import("codec.zig");
const cachemod = @import("cache.zig");
const c = @cImport({
    @cInclude("pthread.h");
    @cInclude("unistd.h");
    @cInclude("fcntl.h");
});
const allocator = std.heap.c_allocator;

pub const Snapshot = struct {
    arena: std.heap.ArenaAllocator,
    images: [2]?codec.Image = .{ null, null },
    faces: [2]?[]const model.Face = .{ null, null },
    portraits: [6]?codec.Image = @splat(null),
    portrait_page: ?usize = null,
    dates: [2][]const u8 = .{ "", "" },
    person: []const u8 = "",
    people: []const model.Person = &.{},
    albums: []const model.Album = &.{},
    count: usize = 0,
    message: []const u8 = "",
    offline: bool = false,

    pub fn destroy(self: *Snapshot) void {
        self.arena.deinit();
        allocator.destroy(self);
    }
};

const Catalog = struct { people: []const model.Person = &.{}, albums: []const model.Album = &.{}, assets: []const model.Asset = &.{} };

pub const Worker = struct {
    root: []const u8,
    cache: []const u8,
    mutex: c.pthread_mutex_t = undefined,
    condition: c.pthread_cond_t = undefined,
    pipes: [2]c_int = .{ -1, -1 },
    thread: ?std.Thread = null,
    generation: std.atomic.Value(u32) = .init(1),
    stop: bool = false,
    network: bool = true,
    pending: ?[]u8 = null,
    delta: i32 = 0,
    portrait_page: ?usize = null,
    portrait_dirty: bool = false,
    latest: ?*Snapshot = null,

    pub fn init(root: []const u8, cache: []const u8, settings: model.Settings) !*Worker {
        return initWithNetwork(root, cache, settings, true);
    }

    pub fn initWithNetwork(root: []const u8, cache: []const u8, settings: model.Settings, network: bool) !*Worker {
        const self = try allocator.create(Worker);
        self.* = .{ .root = try allocator.dupe(u8, root), .cache = try allocator.dupe(u8, cache), .network = network };
        try fs.ensureDir(root);
        try fs.ensureDir(cache);
        if (c.pthread_mutex_init(&self.mutex, null) != 0 or c.pthread_cond_init(&self.condition, null) != 0 or c.pipe(&self.pipes) != 0) return error.WorkerInit;
        for (self.pipes) |fd| {
            _ = c.fcntl(fd, c.F_SETFD, c.FD_CLOEXEC);
            _ = c.fcntl(fd, c.F_SETFL, c.O_NONBLOCK);
        }
        self.pending = try std.json.Stringify.valueAlloc(allocator, settings, .{});
        self.thread = try std.Thread.spawn(.{}, run, .{self});
        return self;
    }

    pub fn deinit(self: *Worker) void {
        self.lock();
        self.stop = true;
        _ = self.generation.fetchAdd(1, .release);
        _ = c.pthread_cond_signal(&self.condition);
        self.unlock();
        if (self.thread) |thread| thread.join();
        if (self.pending) |pending| allocator.free(pending);
        if (self.latest) |latest| latest.destroy();
        for (self.pipes) |fd| _ = c.close(fd);
        _ = c.pthread_mutex_destroy(&self.mutex);
        _ = c.pthread_cond_destroy(&self.condition);
        allocator.free(self.root);
        allocator.free(self.cache);
        allocator.destroy(self);
    }

    fn lock(self: *Worker) void {
        _ = c.pthread_mutex_lock(&self.mutex);
    }
    fn unlock(self: *Worker) void {
        _ = c.pthread_mutex_unlock(&self.mutex);
    }

    pub fn configure(self: *Worker, settings: model.Settings) !void {
        if (!settings.valid()) return error.InvalidSettings;
        const bytes = try std.json.Stringify.valueAlloc(allocator, settings, .{});
        self.lock();
        defer self.unlock();
        if (self.pending) |pending| allocator.free(pending);
        self.pending = bytes;
        self.delta = 0;
        if (self.latest) |latest| latest.destroy();
        self.latest = null;
        _ = self.generation.fetchAdd(1, .release);
        _ = c.pthread_cond_signal(&self.condition);
    }

    pub fn next(self: *Worker, delta: i32) void {
        self.lock();
        defer self.unlock();
        self.delta = std.math.clamp(self.delta + delta, -10, 10);
        _ = c.pthread_cond_signal(&self.condition);
    }

    pub fn portraits(self: *Worker, page: ?usize) void {
        self.lock();
        defer self.unlock();
        if (self.portrait_page == page) return;
        self.portrait_page = page;
        self.portrait_dirty = true;
        _ = c.pthread_cond_signal(&self.condition);
    }

    pub fn take(self: *Worker) ?*Snapshot {
        var buffer: [32]u8 = undefined;
        while (c.read(self.pipes[0], &buffer, buffer.len) > 0) {}
        self.lock();
        defer self.unlock();
        const result = self.latest;
        self.latest = null;
        return result;
    }

    fn publish(self: *Worker, snapshot: *Snapshot, generation: u32) void {
        self.lock();
        defer self.unlock();
        if (self.stop or self.generation.load(.acquire) != generation) {
            snapshot.destroy();
            return;
        }
        if (self.latest) |old| old.destroy();
        self.latest = snapshot;
        _ = c.write(self.pipes[1], "x", 1);
    }

    fn run(self: *Worker) void {
        var arena = std.heap.ArenaAllocator.init(allocator);
        defer arena.deinit();
        var settings = model.Settings{};
        var catalog = Catalog{};
        var generation: u32 = 0;
        var index: usize = 0;
        var offline = false;
        var message: []const u8 = "";
        var scope: []const u8 = "";
        while (true) {
            self.lock();
            while (!self.stop and self.pending == null and self.delta == 0 and !self.portrait_dirty) _ = c.pthread_cond_wait(&self.condition, &self.mutex);
            if (self.stop) {
                self.unlock();
                return;
            }
            const pending = self.pending;
            self.pending = null;
            const portrait_page = self.portrait_page;
            self.portrait_dirty = false;
            const delta = self.delta;
            self.delta = 0;
            generation = self.generation.load(.acquire);
            self.unlock();
            if (pending) |bytes| {
                defer allocator.free(bytes);
                _ = arena.reset(.free_all);
                const a = arena.allocator();
                settings = (std.json.parseFromSlice(model.Settings, a, bytes, .{ .allocate = .alloc_always }) catch continue).value;
                catalog = .{};
                offline = false;
                message = "";
                index = 0;
                scope = std.fmt.allocPrint(a, "{s}/{x}", .{ self.cache, std.hash.Wyhash.hash(0, bytes) }) catch continue;
                fs.ensureDir(scope) catch {
                    message = "Cannot create photo cache.";
                };
                const config_path = std.fmt.allocPrint(a, "{s}/settings.json", .{self.root}) catch continue;
                fs.atomic(a, config_path, bytes) catch {
                    message = "Settings could not be saved.";
                };
                if (settings.server.len == 0 or settings.api_key.len == 0) {
                    message = "Connect your Immich server in Settings.";
                } else {
                    catalog = self.fetchCatalog(a, settings, scope, generation) catch |err| fallback: {
                        if (err == error.Cancelled) continue;
                        offline = true;
                        const cache_path = std.fmt.allocPrint(a, "{s}/catalog.json", .{scope}) catch continue;
                        const cached = fs.read(a, cache_path, 12 * 1024 * 1024) catch {
                            message = switch (err) {
                                error.UnsupportedServer => "Immich 3.2 or newer is required.",
                                else => "Cannot connect. Check the address, key and read permissions.",
                            };
                            break :fallback Catalog{};
                        };
                        break :fallback (std.json.parseFromSlice(Catalog, a, cached, .{ .allocate = .alloc_always }) catch {
                            message = "Saved collection unavailable.";
                            break :fallback Catalog{};
                        }).value;
                    };
                }
            } else if (catalog.assets.len > 0) {
                index = @intCast(@mod(@as(i64, @intCast(index)) + delta, @as(i64, @intCast(catalog.assets.len))));
            }
            if (offline and pending != null) {
                const a = arena.allocator();
                var cached: std.ArrayList(model.Asset) = .empty;
                for (catalog.assets) |asset| {
                    if (!model.accepts(settings, asset)) continue;
                    var path_buffer: [4096]u8 = undefined;
                    const path = std.fmt.bufPrintSentinel(&path_buffer, "{s}/{s}.jpg", .{ scope, asset.id }, 0) catch continue;
                    if (c.access(path, c.R_OK) == 0) cached.append(a, asset) catch {};
                }
                catalog.assets = cached.toOwnedSlice(a) catch &.{};
                if (catalog.assets.len > 0) index %= catalog.assets.len;
            }
            const snapshot = self.makeSnapshot(settings, catalog, index, scope, generation, offline, message, portrait_page) catch |err| {
                if (err == error.Cancelled) continue;
                const failed = newSnapshot() catch continue;
                failed.message = "Photograph unavailable. Try Next or check the connection.";
                self.publish(failed, generation);
                continue;
            };
            self.publish(snapshot, generation);
            if (pending != null and !offline and portrait_page == null) self.warmPortraits(settings, catalog.people, scope, generation);
        }
    }

    fn fetchCatalog(self: *Worker, a: std.mem.Allocator, settings: model.Settings, scope: []const u8, generation: u32) !Catalog {
        if (!self.network) return error.Offline;
        const client = api.Client{ .a = a, .settings = settings, .root = scope, .generation = &self.generation, .expected = generation };
        try client.version();
        const people = try client.people();
        const albums = try client.albums();
        // A bounded random sample spans the library without indexing it on the frame.
        const body = try api.query(a, settings);
        const batch = try client.json([]const model.Asset, "/search/random", body);
        var assets: std.ArrayList(model.Asset) = .empty;
        for (batch) |asset| if (model.accepts(settings, asset)) {
            try assets.append(a, asset);
        };
        const catalog = Catalog{ .people = people, .albums = albums, .assets = try assets.toOwnedSlice(a) };
        const path = try std.fmt.allocPrint(a, "{s}/catalog.json", .{scope});
        const data = try std.json.Stringify.valueAlloc(a, catalog, .{});
        try cachemod.reserve(a, self.cache, data.len);
        try fs.atomic(a, path, data);
        return catalog;
    }

    fn portraitBytes(self: *Worker, a: std.mem.Allocator, client: api.Client, id: []const u8, offline: bool) ![]u8 {
        if (!model.validId(id)) return error.InvalidPerson;
        const scope = try std.fmt.allocPrint(a, "{s}/{x}", .{ self.cache, std.hash.Wyhash.hash(std.hash.Wyhash.hash(1, client.settings.server), client.settings.api_key) });
        try fs.ensureDir(scope);
        const path = try std.fmt.allocPrint(a, "{s}/{s}.portrait.jpg", .{ scope, id });
        return fs.read(a, path, codec.limit) catch {
            if (offline) return error.NotCached;
            const bytes = try client.request(try std.fmt.allocPrint(a, "/people/{s}/thumbnail", .{id}), null, codec.limit);
            try cachemod.reserve(a, self.cache, bytes.len);
            try fs.atomic(a, path, bytes);
            return bytes;
        };
    }

    fn warmPortraits(self: *Worker, settings: model.Settings, people: []const model.Person, scope: []const u8, generation: u32) void {
        var arena = std.heap.ArenaAllocator.init(allocator);
        defer arena.deinit();
        const a = arena.allocator();
        const client = api.Client{ .a = a, .settings = settings, .root = scope, .generation = &self.generation, .expected = generation };
        var count: usize = 0;
        for (people) |person| {
            if (person.name.len == 0 and !model.contains(settings.include, person.id) and !model.contains(settings.exclude, person.id)) continue;
            if (count == 6 or client.cancelled()) return;
            _ = self.portraitBytes(a, client, person.id, false) catch {};
            count += 1;
        }
    }

    fn makeSnapshot(self: *Worker, settings: model.Settings, catalog: Catalog, index: usize, scope: []const u8, generation: u32, offline: bool, message: []const u8, portrait_page: ?usize) !*Snapshot {
        const snapshot = try newSnapshot();
        errdefer snapshot.destroy();
        const a = snapshot.arena.allocator();
        snapshot.offline = offline;
        snapshot.count = catalog.assets.len;
        snapshot.message = try a.dupe(u8, message);
        const people_json = try std.json.Stringify.valueAlloc(a, catalog.people, .{});
        snapshot.people = (try std.json.parseFromSlice([]const model.Person, a, people_json, .{ .allocate = .alloc_always })).value;
        const albums_json = try std.json.Stringify.valueAlloc(a, catalog.albums, .{});
        snapshot.albums = (try std.json.parseFromSlice([]const model.Album, a, albums_json, .{ .allocate = .alloc_always })).value;
        const client = api.Client{ .a = a, .settings = settings, .root = scope, .generation = &self.generation, .expected = generation };
        if (portrait_page) |page| {
            snapshot.portrait_page = page;
            var visible: usize = 0;
            for (catalog.people) |person| {
                if (person.name.len == 0 and !model.contains(settings.include, person.id) and !model.contains(settings.exclude, person.id)) continue;
                defer visible += 1;
                if (visible < page * 6 or visible >= (page + 1) * 6) continue;
                if (!model.validId(person.id)) continue;
                const bytes = self.portraitBytes(a, client, person.id, offline) catch continue;
                snapshot.portraits[visible % 6] = codec.decode(a, bytes) catch null;
            }
        }
        if (catalog.assets.len == 0) {
            if (snapshot.message.len == 0) snapshot.message = "No matches in this sample. Edit filters or refresh the selection.";
            return snapshot;
        }
        var indices: [2]?usize = .{ index, null };
        if (settings.mode == .then_now) {
            const pair = model.pair(catalog.assets, settings, index) orelse {
                snapshot.message = "No pair a year apart in this sample. Refresh or change the selection.";
                return snapshot;
            };
            indices = .{ pair.earlier, pair.later };
            for (catalog.people) |person| if (std.mem.eql(u8, person.id, pair.person)) {
                snapshot.person = try a.dupe(u8, if (person.name.len == 0) "Then & now" else person.name);
                break;
            };
        }
        for (indices, 0..) |selected, slot| if (selected) |i| {
            const asset = catalog.assets[i];
            if (!model.accepts(settings, asset)) return error.FilterMismatch;
            const path = try std.fmt.allocPrint(a, "{s}/{s}.jpg", .{ scope, asset.id });
            const bytes = fs.read(a, path, codec.limit) catch download: {
                if (offline) return error.NotCached;
                const downloaded = try client.request(try std.fmt.allocPrint(a, "/assets/{s}/thumbnail?size=preview", .{asset.id}), null, codec.limit);
                try cachemod.reserve(a, self.cache, downloaded.len);
                try fs.atomic(a, path, downloaded);
                break :download downloaded;
            };
            snapshot.images[slot] = try codec.decode(a, bytes);
            snapshot.dates[slot] = try a.dupe(u8, asset.date());
            if (settings.framing == .smart) {
                const faces_path = try std.fmt.allocPrint(a, "{s}/{s}.faces.json", .{ scope, asset.id });
                const faces_bytes = fs.read(a, faces_path, 1024 * 1024) catch download_faces: {
                    if (offline) break :download_faces null;
                    const data = client.request(try std.fmt.allocPrint(a, "/faces?id={s}", .{asset.id}), null, 1024 * 1024) catch break :download_faces null;
                    try cachemod.reserve(a, self.cache, data.len);
                    fs.atomic(a, faces_path, data) catch {};
                    break :download_faces data;
                };
                if (faces_bytes) |data| {
                    const parsed = std.json.parseFromSlice([]const model.Face, a, data, .{ .allocate = .alloc_always, .ignore_unknown_fields = true }) catch null;
                    if (parsed) |value| snapshot.faces[slot] = value.value;
                }
            }
        };
        return snapshot;
    }
};

fn newSnapshot() !*Snapshot {
    const snapshot = try allocator.create(Snapshot);
    snapshot.* = .{ .arena = std.heap.ArenaAllocator.init(allocator) };
    return snapshot;
}
