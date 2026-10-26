const std = @import("std");
const c = @cImport({
    @cDefine("_GNU_SOURCE", "1");
    @cInclude("unistd.h");
    @cInclude("sys/wait.h");
    @cInclude("stdio.h");
    @cInclude("ifaddrs.h");
    @cInclude("netinet/in.h");
    @cInclude("arpa/inet.h");
    @cInclude("sys/socket.h");
    @cInclude("sys/un.h");
    @cInclude("fcntl.h");
    @cInclude("poll.h");
    @cInclude("errno.h");
    @cInclude("time.h");
    @cInclude("signal.h");
});

pub fn spawn(a: std.mem.Allocator, tool: enum { app, system }, verb: [:0]const u8, value: ?[]const u8) !c.pid_t {
    const path: [:0]const u8 = if (tool == .app) "/usr/local/bin/fascia-app" else "/usr/local/bin/fascia-system";
    const arg = if (value) |v| try a.dupeZ(u8, v) else null;
    defer if (arg) |v| a.free(v);
    const pid = c.fork();
    if (pid < 0) return error.ForkFailed;
    if (pid == 0) {
        // signalfd masks belong to the embedding host, not helper programs.
        var empty: c.sigset_t = undefined;
        if (c.sigemptyset(&empty) != 0 or c.sigprocmask(c.SIG_SETMASK, &empty, null) != 0) c._exit(127);
        var args = [_:null]?[*:0]const u8{ path.ptr, verb.ptr, if (arg) |v| v.ptr else null };
        _ = c.execv(path.ptr, @ptrCast(&args));
        c._exit(127);
    }
    return pid;
}

pub fn poll(pid: c.pid_t) ?bool {
    var status: c_int = 0;
    const result = c.waitpid(pid, &status, c.WNOHANG);
    if (result == 0) return null;
    return result == pid and status == 0;
}

pub fn connections(a: std.mem.Allocator) ![]const u8 {
    var host: [256]u8 = @splat(0);
    const valid = c.gethostname(&host, host.len - 1) == 0;
    const hostname = if (valid) std.mem.sliceTo(&host, 0) else "Unavailable";
    var wifi: [64]u8 = @splat(0);
    var usb: [64]u8 = @splat(0);
    var addresses: ?*c.struct_ifaddrs = null;
    if (c.getifaddrs(&addresses) == 0) {
        defer c.freeifaddrs(addresses);
        var item = addresses;
        while (item) |entry| : (item = entry.ifa_next) {
            const addr = entry.ifa_addr orelse continue;
            if (addr.*.sa_family != c.AF_INET) continue;
            const name = std.mem.span(entry.ifa_name);
            const destination: *[64]u8 = if (std.mem.startsWith(u8, name, "wl")) &wifi else if (std.mem.startsWith(u8, name, "usb")) &usb else continue;
            if (destination[0] != 0) continue;
            const ipv4: *const c.struct_sockaddr_in = @ptrCast(@alignCast(addr));
            _ = c.inet_ntop(c.AF_INET, &ipv4.sin_addr, destination, destination.len);
        }
    }
    return connectionText(
        a,
        if (valid) hostname else null,
        std.mem.sliceTo(&wifi, 0),
        std.mem.sliceTo(&usb, 0),
        sshListening(),
    );
}

fn connectionText(a: std.mem.Allocator, hostname: ?[]const u8, wifi: []const u8, usb: []const u8, listening: ?bool) ![]const u8 {
    var text: std.ArrayList(u8) = .empty;
    errdefer text.deinit(a);
    var part: [768]u8 = undefined;
    if (hostname) |name| {
        try text.appendSlice(a, try std.fmt.bufPrint(&part, "Name    ssh fascia@{s}.local\n", .{name}));
    } else try text.appendSlice(a, "Name    Unavailable\n");
    for ([_][]const u8{ wifi, usb }, [_][]const u8{ "Wi-Fi", "USB" }) |address, label| {
        if (address.len != 0) {
            try text.appendSlice(a, try std.fmt.bufPrint(&part, "{s}    ssh fascia@{s}\n", .{ label, address }));
        } else try text.appendSlice(a, try std.fmt.bufPrint(&part, "{s}    No IPv4 address\n", .{label}));
    }
    try text.appendSlice(
        a,
        if (listening) |active| if (active) "SSH    Listening on port 22\n" else "SSH    No listener on port 22\n" else "SSH    Listener status unavailable\n",
    );
    try text.appendSlice(
        a,
        ".local names need local discovery on the same network.\nConnect USB and run fascia setup to change network settings.",
    );
    return text.toOwnedSlice(a);
}

test "connection details are six lines with actionable addresses" {
    const text = try connectionText(std.testing.allocator, "fascia", "192.168.1.2", "10.55.0.1", true);
    defer std.testing.allocator.free(text);
    try std.testing.expectEqual(@as(usize, 5), std.mem.count(u8, text, "\n"));
    try std.testing.expect(std.mem.indexOf(u8, text, "Wi-Fi    ssh fascia@192.168.1.2") != null);
    try std.testing.expect(std.mem.indexOf(u8, text, "USB    ssh fascia@10.55.0.1") != null);
    try std.testing.expect(std.mem.indexOf(u8, text, "same network") != null);
}

test "unavailable connections do not imply discovery or SSH is running" {
    const text = try connectionText(std.testing.allocator, null, "", "", null);
    defer std.testing.allocator.free(text);
    try std.testing.expectEqual(@as(usize, 5), std.mem.count(u8, text, "\n"));
    try std.testing.expect(std.mem.indexOf(u8, text, "Name    Unavailable") != null);
    try std.testing.expect(std.mem.indexOf(u8, text, "Listener status unavailable") != null);
    try std.testing.expect(std.mem.indexOf(u8, text, "ssh fascia@") == null);
}

pub const Brightness = struct {
    level: u32,
    maximum: u32,
};

pub fn brightnessLevels(level: u64, maximum: u64) ?Brightness {
    if (maximum == 0 or maximum > std.math.maxInt(u32) or level < 1 or level > maximum) return null;
    return .{ .level = @intCast(level), .maximum = @intCast(maximum) };
}

pub fn brightness() ?Brightness {
    const file = c.fopen("/sys/class/backlight/backlight/brightness", "r") orelse return null;
    defer _ = c.fclose(file);
    var value: c_ulonglong = 0;
    if (c.fscanf(file, "%llu", &value) != 1) return null;
    const maximum = c.fopen("/sys/class/backlight/backlight/max_brightness", "r") orelse return null;
    defer _ = c.fclose(maximum);
    var max: c_ulonglong = 0;
    if (c.fscanf(maximum, "%llu", &max) != 1) return null;
    return brightnessLevels(value, max);
}

test "backlight reads preserve coarse levels and reject unavailable values" {
    try std.testing.expectEqual(Brightness{ .level = 5, .maximum = 8 }, brightnessLevels(5, 8).?);
    try std.testing.expect(brightnessLevels(9, 8) == null);
    try std.testing.expect(brightnessLevels(0, 8) == null);
    try std.testing.expect(brightnessLevels(1, 0) == null);
    try std.testing.expect(brightnessLevels(1, @as(u64, std.math.maxInt(u32)) + 1) == null);
}

fn sshListening() ?bool {
    var readable = false;
    for ([_][:0]const u8{ "/proc/net/tcp", "/proc/net/tcp6" }) |path| {
        const file = c.fopen(path, "r") orelse continue;
        defer _ = c.fclose(file);
        readable = true;
        var line: [1024]u8 = undefined;
        while (c.fgets(&line, line.len, file) != null) {
            var fields = std.mem.tokenizeAny(u8, std.mem.sliceTo(&line, 0), " \t\r\n");
            _ = fields.next();
            const local = fields.next() orelse continue;
            _ = fields.next();
            const state = fields.next() orelse continue;
            if (std.mem.endsWith(u8, local, ":0016") and std.mem.eql(u8, state, "0A")) return true;
        }
    }
    return if (readable) false else null;
}

pub const Visibility = enum { home, app, unknown };

pub const State = struct {
    visibility: Visibility = .unknown,
    generation: ?u64 = null,
};

pub fn visibility() State {
    const ipc = @cImport({
        @cDefine("_GNU_SOURCE", "1");
        @cInclude("sys/socket.h");
        @cInclude("sys/un.h");
        @cInclude("sys/time.h");
        @cInclude("unistd.h");
        @cInclude("stdlib.h");
    });
    const runtime = ipc.getenv("XDG_RUNTIME_DIR") orelse return .{};
    var address: ipc.struct_sockaddr_un = std.mem.zeroes(ipc.struct_sockaddr_un);
    address.sun_family = ipc.AF_UNIX;
    _ = std.fmt.bufPrintZ(&address.sun_path, "{s}/fascia-home.sock", .{std.mem.span(runtime)}) catch return .{};
    const fd = ipc.socket(ipc.AF_UNIX, ipc.SOCK_STREAM | ipc.SOCK_CLOEXEC, 0);
    if (fd < 0) return .{};
    defer _ = ipc.close(fd);
    const timeout = ipc.struct_timeval{ .tv_sec = 0, .tv_usec = 100_000 };
    _ = ipc.setsockopt(fd, ipc.SOL_SOCKET, ipc.SO_RCVTIMEO, &timeout, @sizeOf(@TypeOf(timeout)));
    _ = ipc.setsockopt(fd, ipc.SOL_SOCKET, ipc.SO_SNDTIMEO, &timeout, @sizeOf(@TypeOf(timeout)));
    if (ipc.connect(
        fd,
        if (@import("builtin").os.tag == .linux) .{ .__sockaddr_un__ = &address } else @ptrCast(&address),
        @sizeOf(@TypeOf(address)),
    ) != 0) return .{};
    if (ipc.send(fd, "state\n", 6, ipc.MSG_NOSIGNAL) != 6) return .{};
    var reply: [128]u8 = undefined;
    const count = ipc.recv(fd, &reply, reply.len, 0);
    if (count <= 0) return .{};
    const text = reply[0..@intCast(count)];
    var words = std.mem.tokenizeAny(u8, text, " \r\n");
    const kind = words.next() orelse return .{};
    var result = State{};
    if (std.mem.eql(u8, kind, "home")) result.visibility = .home else if (std.mem.eql(u8, kind, "app")) {
        result.visibility = .app;
        _ = words.next();
    } else return .{};
    if (words.next()) |value| result.generation = std.fmt.parseInt(u64, value, 10) catch null;
    return result;
}

// One bounded local roundtrip: persistence happens independently in the daemon.
pub fn setBrightness(level: u32) !void {
    if (level == 0) return error.InvalidBrightness;
    const builtin = @import("builtin");
    const fd = c.socket(c.AF_UNIX, c.SOCK_STREAM, 0);
    if (fd < 0) return error.BrightnessUnavailable;
    defer _ = c.close(fd);
    _ = c.fcntl(fd, c.F_SETFD, c.FD_CLOEXEC);
    _ = c.fcntl(fd, c.F_SETFL, c.O_NONBLOCK);
    if (comptime builtin.os.tag == .macos) {
        var enabled: c_int = 1;
        _ = c.setsockopt(fd, c.SOL_SOCKET, c.SO_NOSIGPIPE, &enabled, @sizeOf(c_int));
    }
    var address: c.struct_sockaddr_un = std.mem.zeroes(c.struct_sockaddr_un);
    address.sun_family = c.AF_UNIX;
    const path = "/run/fascia-device/brightness.sock";
    @memcpy(address.sun_path[0..path.len], path);
    var clock: c.struct_timespec = undefined;
    _ = c.clock_gettime(c.CLOCK_MONOTONIC, &clock);
    const deadline = @as(i64, clock.tv_sec) * 1000000000 + clock.tv_nsec + 10000000;
    const result = c.connect(
        fd,
        if (builtin.os.tag == .linux) .{ .__sockaddr_un__ = &address } else @ptrCast(&address),
        @sizeOf(@TypeOf(address)),
    );
    if (result != 0) {
        const err = if (builtin.os.tag == .macos) c.__error().* else c.__errno_location().*;
        if (err != c.EINPROGRESS and err != c.EAGAIN) return error.BrightnessUnavailable;
    }
    var request_buffer: [32]u8 = undefined;
    const request = try std.fmt.bufPrint(&request_buffer, "set {d}\n", .{level});
    var sent: usize = 0;
    var response: [64]u8 = undefined;
    var received: usize = 0;
    while (true) {
        _ = c.clock_gettime(c.CLOCK_MONOTONIC, &clock);
        const remaining = deadline - (@as(i64, clock.tv_sec) * 1000000000 + clock.tv_nsec);
        if (remaining <= 0) return error.BrightnessTimeout;
        var descriptor = c.struct_pollfd{
            .fd = fd,
            .events = if (sent < request.len) c.POLLOUT else c.POLLIN,
            .revents = 0,
        };
        const ready = c.poll(&descriptor, 1, @intCast(@divTrunc(remaining, 1000000)));
        if (ready < 0) continue;
        if (ready == 0) return error.BrightnessTimeout;
        if (sent < request.len) {
            const n = c.send(fd, request[sent..].ptr, request.len - sent, if (builtin.os.tag == .linux) c.MSG_NOSIGNAL else 0);
            if (n <= 0) return error.BrightnessUnavailable;
            sent += @intCast(n);
        } else {
            const n = c.recv(fd, response[received..].ptr, response.len - received, 0);
            if (n <= 0) return error.BrightnessUnavailable;
            received += @intCast(n);
            if (std.mem.indexOfScalar(u8, response[0..received], '\n') != null) return brightnessReply(response[0..received], level);
            if (received == response.len) return error.InvalidBrightnessReply;
        }
    }
}

fn brightnessReply(bytes: []const u8, requested: u32) !void {
    if (!std.mem.endsWith(u8, bytes, "\n")) return error.InvalidBrightnessReply;
    var words = std.mem.splitScalar(u8, bytes[0 .. bytes.len - 1], ' ');
    if (!std.mem.eql(u8, words.next() orelse "", "ok")) return error.BrightnessRejected;
    const actual = std.fmt.parseInt(u32, words.next() orelse "", 10) catch return error.InvalidBrightnessReply;
    const maximum = std.fmt.parseInt(u32, words.next() orelse "", 10) catch return error.InvalidBrightnessReply;
    if (words.next() != null or actual != requested or actual == 0 or actual > maximum) return error.InvalidBrightnessReply;
}

test "brightness acknowledgement requires the requested nonzero device level" {
    try brightnessReply("ok 4 8\n", 4);
    try std.testing.expectError(error.InvalidBrightnessReply, brightnessReply("ok 0 8\n", 0));
    try std.testing.expectError(error.InvalidBrightnessReply, brightnessReply("ok 3 8\n", 4));
    try std.testing.expectError(error.BrightnessRejected, brightnessReply("error device\n", 4));
    try std.testing.expectError(error.InvalidBrightnessReply, brightnessReply("ok 4 8\nok 4 8\n", 4));
}
