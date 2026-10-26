//! Persistent, narrowly privileged backlight control. The socket loop never fsyncs.
const std = @import("std");
const builtin = @import("builtin");
const c = @cImport({
    @cDefine("_GNU_SOURCE", "1");
    @cInclude("sys/socket.h");
    @cInclude("sys/un.h");
    @cInclude("sys/stat.h");
    @cInclude("sys/types.h");
    @cInclude("unistd.h");
    @cInclude("fcntl.h");
    @cInclude("poll.h");
    @cInclude("pthread.h");
    @cInclude("pwd.h");
    @cInclude("errno.h");
    @cInclude("stdio.h");
    @cInclude("stdlib.h");
    @cInclude("signal.h");
    @cInclude("time.h");
});
var stopping: c.sig_atomic_t = 0;

fn ignore(_: c_int) callconv(.c) void {}

fn stop(_: c_int) callconv(.c) void {
    stopping = 1;
}

fn errnoValue() c_int {
    return if (builtin.os.tag == .macos) c.__error().* else c.__errno_location().*;
}

fn nowMs() i64 {
    var ts: c.struct_timespec = undefined;
    _ = c.clock_gettime(c.CLOCK_MONOTONIC, &ts);
    return @as(i64, ts.tv_sec) * 1000 + @divTrunc(ts.tv_nsec, 1000000);
}

fn readLevel(fd: c_int) !u32 {
    if (c.lseek(fd, 0, c.SEEK_SET) < 0) return error.Seek;
    var bytes: [32]u8 = undefined;
    const n = c.read(fd, &bytes, bytes.len);
    if (n <= 0 or n == bytes.len) return error.Read;
    return std.fmt.parseInt(u32, std.mem.trim(u8, bytes[0..@intCast(n)], " \r\n\t"), 10) catch error.InvalidDeviceValue;
}

fn readPath(path: [:0]const u8) !u32 {
    const fd = c.open(path, c.O_RDONLY | c.O_CLOEXEC);
    if (fd < 0) return error.Open;
    defer _ = c.close(fd);
    return readLevel(fd);
}

fn writeAll(fd: c_int, bytes: []const u8) !void {
    var offset: usize = 0;
    while (offset < bytes.len) {
        const n = c.write(fd, bytes[offset..].ptr, bytes.len - offset);
        if (n < 0 and errnoValue() == c.EINTR) continue;
        if (n <= 0) return error.Write;
        offset += @intCast(n);
    }
}

fn applyLevel(fd: c_int, level: u32, test_mode: bool) !u32 {
    var bytes: [32]u8 = undefined;
    const text = try std.fmt.bufPrint(&bytes, "{d}\n", .{level});
    if (c.lseek(fd, 0, c.SEEK_SET) < 0) return error.Seek;
    if (test_mode and c.ftruncate(fd, 0) != 0) return error.Truncate;
    try writeAll(fd, text);
    const actual = try readLevel(fd);
    if (actual != level) return error.DeviceMismatch;
    return actual;
}

fn parseRequest(bytes: []const u8, maximum: u32) !u32 {
    if (bytes.len < 6 or
        bytes.len > 32 or
        bytes[bytes.len - 1] != '\n' or
        !(std.mem.startsWith(u8, bytes, "set ") or
            std.mem.startsWith(u8, bytes, "save "))) return error.Request;
    const digits = bytes[(if (std.mem.startsWith(u8, bytes, "save ")) @as(usize, 5) else 4) .. bytes.len - 1];
    if (digits.len == 0 or digits.len > 10) return error.Request;
    for (digits) |ch| if (ch < '0' or ch > '9') return error.Request;
    const level = std.fmt.parseInt(u32, digits, 10) catch return error.Request;
    if (level == 0 or level > maximum) return error.Range;
    return level;
}

fn atomicSave(path: [:0]const u8, level: u32, maximum: u32) !void {
    var temporary: [2048]u8 = undefined;
    const name = try std.fmt.bufPrintSentinel(&temporary, "{s}.tmp.XXXXXX", .{path}, 0);
    const fd = c.mkstemp(name);
    if (fd < 0) return error.Create;
    defer _ = c.unlink(name);
    var open = true;
    defer if (open) {
        _ = c.close(fd);
    };
    if (c.fchmod(fd, 0o600) != 0) return error.Permissions;
    var buffer: [256]u8 = undefined;
    const percent = @max(5, @min(100, (@as(u64, level) * 100 + maximum / 2) / maximum));
    const text = try std.fmt.bufPrint(
        &buffer,
        "{{\"schema_version\":1,\"brightness\":{d},\"brightness_level\":{d},\"brightness_maximum\":{d}}}\n",
        .{ percent, level, maximum },
    );
    try writeAll(fd, text);
    if (c.fsync(fd) != 0) return error.FileSync;
    const result = c.close(fd);
    open = false;
    if (result != 0) return error.Close;
    if (c.rename(name, path) != 0) return error.Rename;
    const slash = std.mem.lastIndexOfScalar(u8, path, '/') orelse return error.Parent;
    var parent_buffer: [2048]u8 = undefined;
    const parent = try std.fmt.bufPrintSentinel(&parent_buffer, "{s}", .{if (slash == 0) "/" else path[0..slash]}, 0);
    const directory = c.open(parent, c.O_RDONLY | c.O_DIRECTORY | c.O_CLOEXEC);
    if (directory < 0) return error.Parent;
    defer _ = c.close(directory);
    if (c.fsync(directory) != 0) return error.ParentSync;
}

const Saver = struct {
    mutex: c.pthread_mutex_t = undefined,
    condition: c.pthread_cond_t = undefined,
    thread: c.pthread_t = undefined,
    generation: u64 = 0,
    completed: u64 = 0,
    successful: bool = false,
    saved_level: u32 = 0,
    pending: bool = false,
    done: bool = false,
    level: u32 = 1,
    maximum: u32,
    path: [:0]const u8,
    delay_ms: u32 = 0,

    fn init(self: *Saver) !void {
        if (c.pthread_mutex_init(&self.mutex, null) != 0) return error.Mutex;
        if (c.pthread_cond_init(&self.condition, null) != 0) return error.Condition;
        if (c.pthread_create(&self.thread, null, worker, self) != 0) return error.Thread;
    }

    fn queue(self: *Saver, level: u32) u64 {
        _ = c.pthread_mutex_lock(&self.mutex);
        self.generation += 1;
        const generation = self.generation;
        self.level = level;
        self.pending = true;
        _ = c.pthread_cond_signal(&self.condition);
        _ = c.pthread_mutex_unlock(&self.mutex);
        return generation;
    }

    fn finish(self: *Saver) void {
        _ = c.pthread_mutex_lock(&self.mutex);
        self.done = true;
        _ = c.pthread_cond_signal(&self.condition);
        _ = c.pthread_mutex_unlock(&self.mutex);
        _ = c.pthread_join(self.thread, null);
        _ = c.pthread_cond_destroy(&self.condition);
        _ = c.pthread_mutex_destroy(&self.mutex);
    }

    fn worker(arg: ?*anyopaque) callconv(.c) ?*anyopaque {
        const self: *Saver = @ptrCast(@alignCast(arg.?));
        var failed = false;
        while (true) {
            _ = c.pthread_mutex_lock(&self.mutex);
            while (!self.pending and !self.done) _ = c.pthread_cond_wait(&self.condition, &self.mutex);
            if (!self.pending and self.done) {
                _ = c.pthread_mutex_unlock(&self.mutex);
                break;
            }
            const level = self.level;
            const generation = self.generation;
            self.pending = false;
            _ = c.pthread_mutex_unlock(&self.mutex);
            if (self.delay_ms > 0) {
                var delay = c.struct_timespec{
                    .tv_sec = @intCast(self.delay_ms / 1000),
                    .tv_nsec = @intCast((self.delay_ms % 1000) * 1000000),
                };
                _ = c.nanosleep(&delay, null);
            }
            var successful = true;
            atomicSave(self.path, level, self.maximum) catch |err| {
                successful = false;
                if (!failed) std.debug.print("BRIGHTNESS_SAVE=unconfirmed error={s}\n", .{@errorName(err)});
                failed = true;
            };
            _ = c.pthread_mutex_lock(&self.mutex);
            self.completed = generation;
            self.successful = successful;
            self.saved_level = level;
            _ = c.pthread_mutex_unlock(&self.mutex);
            if (!successful) continue;
            if (self.delay_ms > 0) std.debug.print("BRIGHTNESS_TEST_SAVE level={d}\n", .{level});
            if (failed) std.debug.print("BRIGHTNESS_SAVE=recovered\n", .{});
            failed = false;
        }
        return null;
    }
};

const Connection = struct {
    fd: c_int = -1,
    bytes: [32]u8 = undefined,
    len: usize = 0,
    deadline: i64 = 0,
    waiting: u64 = 0,

    fn close(self: *Connection) void {
        if (self.fd >= 0) _ = c.close(self.fd);
        self.fd = -1;
        self.len = 0;
    }
};

fn allowed(fd: c_int, uid: c.uid_t) bool {
    if (comptime builtin.os.tag == .linux) {
        var peer: c.struct_ucred = undefined;
        var len: c.socklen_t = @sizeOf(@TypeOf(peer));
        return c.getsockopt(fd, c.SOL_SOCKET, c.SO_PEERCRED, &peer, &len) == 0 and (peer.uid == 0 or peer.uid == uid);
    }
    var peer_uid: c.uid_t = undefined;
    var peer_gid: c.gid_t = undefined;
    return c.getpeereid(fd, &peer_uid, &peer_gid) == 0 and (peer_uid == 0 or peer_uid == uid);
}

fn reply(fd: c_int, text: []const u8) void {
    _ = c.send(fd, text.ptr, text.len, 0);
}

pub fn main(init: std.process.Init) !void {
    var socket_buffer: [108]u8 = undefined;
    var brightness_buffer: [2048]u8 = undefined;
    var maximum_buffer: [2048]u8 = undefined;
    var settings_buffer: [2048]u8 = undefined;
    var socket_path: [:0]const u8 = "/run/fascia-device/brightness.sock";
    var brightness_path: [:0]const u8 = "/sys/class/backlight/backlight/brightness";
    var maximum_path: [:0]const u8 = "/sys/class/backlight/backlight/max_brightness";
    var settings_path: [:0]const u8 = "/var/lib/fascia/device.json";
    var test_mode = false;
    var save_delay: u32 = 0;
    const args = init.minimal.args.vector;
    if (args.len > 1) {
        if (args.len != 3 and args.len != 5) return error.Arguments;
        if (!std.mem.eql(u8, std.mem.span(args[1]), "--test-root") or c.geteuid() == 0) return error.UnprivilegedTestModeOnly;
        test_mode = true;
        const root = std.mem.span(args[2]);
        if (!std.fs.path.isAbsolute(root)) return error.AbsoluteTestRoot;
        socket_path = try std.fmt.bufPrintSentinel(&socket_buffer, "{s}/brightness.sock", .{root}, 0);
        brightness_path = try std.fmt.bufPrintSentinel(&brightness_buffer, "{s}/brightness", .{root}, 0);
        maximum_path = try std.fmt.bufPrintSentinel(&maximum_buffer, "{s}/max_brightness", .{root}, 0);
        settings_path = try std.fmt.bufPrintSentinel(&settings_buffer, "{s}/device.json", .{root}, 0);
        if (args.len == 5) {
            if (!std.mem.eql(u8, std.mem.span(args[3]), "--test-save-delay-ms")) return error.Arguments;
            save_delay = try std.fmt.parseInt(u32, std.mem.span(args[4]), 10);
            if (save_delay > 5000) return error.Delay;
        }
    } else if (c.geteuid() != 0) return error.RootRequired;
    const user = if (test_mode) null else c.getpwnam("fascia") orelse return error.FasciaUser;
    const uid = if (test_mode) c.geteuid() else user.?.*.pw_uid;
    const gid = if (test_mode) c.getegid() else user.?.*.pw_gid;
    const maximum = try readPath(maximum_path);
    if (maximum == 0) return error.InvalidMaximum;
    const brightness_fd = c.open(brightness_path, c.O_RDWR | c.O_CLOEXEC);
    if (brightness_fd < 0) return error.BacklightOpen;
    defer _ = c.close(brightness_fd);
    var latest = try readLevel(brightness_fd);
    const listener = c.socket(c.AF_UNIX, c.SOCK_STREAM, 0);
    if (listener < 0) return error.Socket;
    defer _ = c.close(listener);
    _ = c.fcntl(listener, c.F_SETFL, c.O_NONBLOCK);
    _ = c.fcntl(listener, c.F_SETFD, c.FD_CLOEXEC);
    var address = std.mem.zeroes(c.struct_sockaddr_un);
    address.sun_family = c.AF_UNIX;
    if (socket_path.len >= address.sun_path.len) return error.SocketPathTooLong;
    @memcpy(address.sun_path[0..socket_path.len], socket_path);
    var existing: c.struct_stat = undefined;
    if (c.lstat(socket_path, &existing) == 0) {
        if ((existing.st_mode & c.S_IFMT) != c.S_IFSOCK or existing.st_uid != c.geteuid()) return error.UnsafeSocket;
        if (c.connect(
            listener,
            if (builtin.os.tag == .linux) .{ .__sockaddr_un__ = &address } else @ptrCast(&address),
            @sizeOf(@TypeOf(address)),
        ) == 0) return error.AlreadyRunning;
        if (errnoValue() != c.ECONNREFUSED) return error.ExistingSocket;
        _ = c.unlink(socket_path);
    }
    if (c.bind(
        listener,
        if (builtin.os.tag == .linux) .{ .__sockaddr_un__ = &address } else @ptrCast(&address),
        @sizeOf(@TypeOf(address)),
    ) != 0) return error.Bind;
    defer _ = c.unlink(socket_path);
    if (c.chmod(socket_path, 0o660) != 0 or (!test_mode and c.chown(socket_path, 0, gid) != 0) or c.listen(listener, 16) != 0) return error.Listen;
    _ = c.signal(c.SIGTERM, stop);
    _ = c.signal(c.SIGINT, stop);
    _ = c.signal(c.SIGPIPE, ignore);
    var saver = Saver{ .maximum = maximum, .path = settings_path, .delay_ms = save_delay };
    try saver.init();
    defer saver.finish();
    var clients: [16]Connection = @splat(.{});
    defer for (&clients) |*connection| connection.close();
    var pending = false;
    var changed: i64 = 0;
    std.debug.print("BRIGHTNESS_READY max={d} socket={s}\n", .{ maximum, socket_path });
    while (stopping == 0) {
        const now = nowMs();
        if (pending and now - changed >= 750) {
            _ = saver.queue(latest);
            pending = false;
        }
        var descriptors: [17]c.struct_pollfd = undefined;
        descriptors[0] = .{ .fd = listener, .events = c.POLLIN, .revents = 0 };
        var timeout: i64 = 250;
        if (pending) timeout = @min(timeout, @max(0, 750 - (now - changed)));
        for (&clients, 0..) |*connection, i| {
            if (connection.fd >= 0 and connection.waiting != 0) {
                _ = c.pthread_mutex_lock(&saver.mutex);
                const complete = saver.completed >= connection.waiting;
                const successful = saver.successful;
                const saved_level = saver.saved_level;
                _ = c.pthread_mutex_unlock(&saver.mutex);
                if (complete) {
                    var response: [80]u8 = undefined;
                    reply(
                        connection.fd,
                        if (successful) try std.fmt.bufPrint(&response, "ok {d} {d}\n", .{ saved_level, maximum }) else "error persistence-unconfirmed\n",
                    );
                    connection.close();
                } else timeout = @min(timeout, 10);
            }
            if (connection.fd >= 0) {
                if (now >= connection.deadline) connection.close() else timeout = @min(timeout, connection.deadline - now);
            }
            descriptors[i + 1] = .{ .fd = connection.fd, .events = c.POLLIN, .revents = 0 };
        }
        const polled = c.poll(&descriptors, descriptors.len, @intCast(timeout));
        if (polled < 0) {
            if (errnoValue() == c.EINTR) continue;
            return error.Poll;
        }
        if (descriptors[0].revents & c.POLLIN != 0) {
            for (0..32) |_| {
                const fd = c.accept(listener, if (builtin.os.tag == .linux) .{ .__sockaddr__ = null } else null, null);
                if (fd < 0) break;
                _ = c.fcntl(fd, c.F_SETFL, c.O_NONBLOCK);
                _ = c.fcntl(fd, c.F_SETFD, c.FD_CLOEXEC);
                if (!allowed(fd, uid)) {
                    reply(fd, "error denied\n");
                    _ = c.close(fd);
                    continue;
                }
                var assigned = false;
                for (&clients) |*connection| {
                    if (connection.fd < 0) {
                        connection.* = .{ .fd = fd, .deadline = nowMs() + 150 };
                        assigned = true;
                        break;
                    }
                }
                if (!assigned) {
                    reply(fd, "error busy\n");
                    _ = c.close(fd);
                }
            }
        }
        for (&clients, 0..) |*connection, i| {
            if (connection.fd < 0 or connection.waiting != 0 or descriptors[i + 1].fd != connection.fd or descriptors[i + 1].revents == 0) continue;
            const n = c.recv(connection.fd, connection.bytes[connection.len..].ptr, connection.bytes.len - connection.len, 0);
            if (n <= 0) {
                if (n < 0 and (errnoValue() == c.EAGAIN or errnoValue() == c.EINTR)) continue;
                connection.close();
                continue;
            }
            connection.len += @intCast(n);
            const message = connection.bytes[0..connection.len];
            if (std.mem.indexOfScalar(u8, message, '\n') == null) {
                if (connection.len == connection.bytes.len) {
                    reply(connection.fd, "error request\n");
                    connection.close();
                }
                continue;
            }
            if (std.mem.eql(u8, message, "get\n")) {
                var response: [80]u8 = undefined;
                const actual = readLevel(brightness_fd) catch {
                    reply(connection.fd, "error device\n");
                    connection.close();
                    continue;
                };
                reply(connection.fd, try std.fmt.bufPrint(&response, "ok {d} {d}\n", .{ actual, maximum }));
                connection.close();
                continue;
            }
            const level = parseRequest(message, maximum) catch {
                reply(connection.fd, "error request\n");
                connection.close();
                continue;
            };
            const actual = applyLevel(brightness_fd, level, test_mode) catch {
                reply(connection.fd, "error device\n");
                connection.close();
                continue;
            };
            if (std.mem.startsWith(u8, message, "save ")) {
                latest = actual;
                pending = false;
                connection.waiting = saver.queue(actual);
                connection.deadline = nowMs() + 5000;
                continue;
            }
            var response: [80]u8 = undefined;
            reply(connection.fd, try std.fmt.bufPrint(&response, "ok {d} {d}\n", .{ actual, maximum }));
            latest = actual;
            changed = nowMs();
            pending = true;
            connection.close();
        }
    }
    if (pending) _ = saver.queue(latest);
}

test "bounded brightness protocol never permits off, extras, or overflow" {
    try std.testing.expectEqual(@as(u32, 8), try parseRequest("set 8\n", 8));
    for ([_][]const u8{
        "set 0\n",
        "set 9\n",
        "set -1\n",
        "set 1\nset 2\n",
        "set 4294967296\n",
        "set 1",
        "set 1 \n",
        " set 1\n",
    }) |raw| try std.testing.expectError(
        if (std.mem.eql(u8, raw, "set 0\n") or std.mem.eql(u8, raw, "set 9\n")) error.Range else error.Request,
        parseRequest(raw, 8),
    );
}
