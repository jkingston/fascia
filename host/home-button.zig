//! PF1007L Home button on PL2, configured as input.
const std = @import("std");
const builtin = @import("builtin");
const c = @cImport({
    @cDefine("_GNU_SOURCE", "1");
    @cInclude("unistd.h");
    @cInclude("fcntl.h");
    @cInclude("poll.h");
    @cInclude("signal.h");
    @cInclude("errno.h");
    @cInclude("sys/ioctl.h");
    @cInclude("sys/socket.h");
    @cInclude("sys/un.h");
    @cInclude("time.h");
    @cInclude("stdlib.h");
});
const chip_path = "/dev/gpiochip0";
const chip_label = "1f02c00.pinctrl";
const key_home: u16 = 172;
const ChipInfo = extern struct {
    name: [32]u8 = @splat(0),
    label: [32]u8 = @splat(0),
    lines: u32 = 0,
};
const Attribute = extern struct {
    id: u32 = 0,
    padding: u32 = 0,
    value: u64 align(8) = 0,
};
const ConfigAttribute = extern struct {
    attr: Attribute = .{},
    mask: u64 align(8) = 0,
};
const LineConfig = extern struct {
    flags: u64 align(8) = 0,
    num_attrs: u32 = 0,
    padding: [5]u32 = @splat(0),
    attrs: [10]ConfigAttribute = @splat(.{}),
};
const Request = extern struct {
    offsets: [64]u32 = @splat(0),
    consumer: [32]u8 = @splat(0),
    config: LineConfig = .{},
    num_lines: u32 = 1,
    event_buffer_size: u32 = 16,
    padding: [5]u32 = @splat(0),
    fd: i32 = -1,
};
const Values = extern struct {
    bits: u64 align(8) = 0,
    mask: u64 align(8) = 1,
};
const Edge = extern struct {
    timestamp_ns: u64 align(8),
    id: u32,
    offset: u32,
    seqno: u32,
    line_seqno: u32,
    padding: [6]u32,
};
// input_event uses kernel word width, NOT libc's possibly time64 timeval.
const KernelLong = if (@sizeOf(usize) == 4) i32 else i64;
const InputEvent = extern struct {
    sec: KernelLong = 0,
    usec: KernelLong = 0,
    type: u16,
    code: u16,
    value: i32,
};
const InputId = extern struct {
    bus: u16 = 3,
    vendor: u16 = 1,
    product: u16 = 1,
    version: u16 = 1,
};
const UserDevice = extern struct {
    name: [80]u8 = @splat(0),
    id: InputId = .{},
    ff_effects_max: u32 = 0,
    absmax: [64]i32 = @splat(0),
    absmin: [64]i32 = @splat(0),
    absfuzz: [64]i32 = @splat(0),
    absflat: [64]i32 = @splat(0),
};
comptime {
    if (@sizeOf(ChipInfo) != 68 or
        @sizeOf(LineConfig) != 272 or
        @sizeOf(Request) != 592 or
        @offsetOf(Request, "config") != 288 or
        @offsetOf(Request, "num_lines") != 560 or
        @offsetOf(Request, "fd") != 588 or
        @sizeOf(Values) != 16 or
        @sizeOf(Edge) != 48 or
        @sizeOf(UserDevice) != 1116) @compileError("Linux GPIO/uinput ABI mismatch");
    if (@sizeOf(InputEvent) != (if (@sizeOf(usize) == 4) @as(usize, 16) else 24)) @compileError("Linux input_event ABI mismatch");
}
const get_chip: c_ulong = 0x8044b401;
const request_line: c_ulong = 0xc250b407;
const get_values: c_ulong = 0xc010b40e;
const ui_set_ev: c_ulong = 0x40045564;
const ui_set_key: c_ulong = 0x40045565;
const ui_create: c_ulong = 0x5501;
const ui_destroy: c_ulong = 0x5502;
var signal_write: c_int = -1;
var stopping: c.sig_atomic_t = 0;

fn stop(_: c_int) callconv(.c) void {
    const saved_errno = errno();
    defer {
        if (builtin.os.tag == .macos) c.__error().* = saved_errno else c.__errno_location().* = saved_errno;
    }
    @as(*volatile c.sig_atomic_t, &stopping).* = 1;
    if (signal_write >= 0) {
        const byte: u8 = 1;
        _ = c.write(signal_write, &byte, 1);
    }
}

fn ignore(_: c_int) callconv(.c) void {}

fn errno() c_int {
    return if (builtin.os.tag == .macos) c.__error().* else c.__errno_location().*;
}

fn nowUs() !i64 {
    var time: c.struct_timespec = undefined;
    if (c.clock_gettime(c.CLOCK_MONOTONIC, &time) != 0) return error.Clock;
    return @as(i64, @intCast(time.tv_sec)) * 1_000_000 + @divTrunc(@as(i64, @intCast(time.tv_nsec)), 1000);
}

fn high(fd: c_int) !bool {
    var values = Values{};
    if (c.ioctl(fd, get_values, &values) < 0) return error.GpioRead;
    return values.bits & 1 != 0;
}

fn writeExact(fd: c_int, bytes: []const u8) !void {
    while (true) {
        const count = c.write(fd, bytes.ptr, bytes.len);
        if (count < 0 and errno() == c.EINTR) continue;
        // EAGAIN/short writes fail rather than spinning or splitting input records.
        if (count < 0 or @as(usize, @intCast(count)) != bytes.len) return error.UinputWrite;
        return;
    }
}

fn key(fd: c_int, pressed: bool) !void {
    const events = [_]InputEvent{
        .{ .type = 1, .code = key_home, .value = @intFromBool(pressed) },
        .{ .type = 0, .code = 0, .value = 0 },
    };
    try writeExact(fd, std.mem.sliceAsBytes(&events));
}

fn notifyReady() !void {
    const path = c.getenv("NOTIFY_SOCKET") orelse return;
    const value = std.mem.span(path);
    var address = std.mem.zeroes(c.struct_sockaddr_un);
    if (value.len == 0 or value.len >= address.sun_path.len) return error.NotifyPath;
    address.sun_family = c.AF_UNIX;
    @memcpy(address.sun_path[0..value.len], value);
    if (value[0] == '@') address.sun_path[0] = 0;
    const length: c.socklen_t = @intCast(@offsetOf(c.struct_sockaddr_un, "sun_path") + value.len + @as(usize, if (value[0] == '@') 0 else 1));
    const fd = c.socket(c.AF_UNIX, c.SOCK_DGRAM | c.SOCK_CLOEXEC | c.SOCK_NONBLOCK, 0);
    if (fd < 0) return error.NotifySocket;
    defer _ = c.close(fd);
    const message = "READY=1\nSTATUS=PL2 Home input ready";
    if (c.sendto(fd, message.ptr, message.len, c.MSG_NOSIGNAL, .{ .__sockaddr_un__ = &address }, length) != message.len) return error.NotifySend;
}

pub const Debounce = struct {
    stable_high: bool,
    candidate_high: bool,
    since: i64,
    armed: bool,
    const delay: i64 = 30_000;

    pub fn init(level: bool, now: i64) Debounce {
        return .{
            .stable_high = level,
            .candidate_high = level,
            .since = now,
            .armed = level,
        };
    }

    pub fn observe(self: *Debounce, level: bool, now: i64) void {
        if (level != self.candidate_high) {
            self.candidate_high = level;
            self.since = now;
        }
    }

    pub fn edgeBatch(self: *Debounce, level: bool, now: i64) void {
        self.observe(level, now);
        // Restart debounce even if a burst ends at the previous level.
        if (self.candidate_high != self.stable_high) self.since = now;
    }

    pub fn waitMs(self: Debounce, now: i64) c_int {
        if (self.candidate_high == self.stable_high) return -1;
        return @intCast(@divTrunc(@max(0, delay - (now - self.since)) + 999, 1000));
    }

    pub fn settle(self: *Debounce, now: i64) ?bool {
        if (self.candidate_high == self.stable_high or now - self.since < delay) return null;
        self.stable_high = self.candidate_high;
        if (!self.armed) {
            if (self.stable_high) self.armed = true;
            return null;
        }
        return !self.stable_high;
    }
};

pub fn main() !void {
    const chip = c.open(chip_path, c.O_RDONLY | c.O_CLOEXEC);
    if (chip < 0) return error.GpioOpen;
    defer _ = c.close(chip);
    var info = ChipInfo{};
    if (c.ioctl(chip, get_chip, &info) < 0 or info.lines < 3 or !std.mem.eql(u8, std.mem.sliceTo(&info.label, 0), chip_label)) return error.WrongGpioChip;
    var request = Request{};
    request.offsets[0] = 2;
    const consumer = "fascia-home-button";
    @memcpy(request.consumer[0..consumer.len], consumer);
    // INPUT + RISING + FALLING + BIAS_PULL_UP; never OUTPUT or ACTIVE_LOW.
    request.config.flags = (1 << 2) | (1 << 4) | (1 << 5) | (1 << 8);
    if (c.ioctl(chip, request_line, &request) < 0 or request.fd < 0) return error.GpioRequest;
    const line = request.fd;
    defer _ = c.close(line);
    if (c.fcntl(line, c.F_SETFL, c.O_NONBLOCK) < 0 or c.fcntl(line, c.F_SETFD, c.FD_CLOEXEC) < 0) return error.GpioNonblock;
    var debounce = Debounce.init(try high(line), try nowUs());
    const keyboard = c.open("/dev/uinput", c.O_WRONLY | c.O_NONBLOCK | c.O_CLOEXEC);
    if (keyboard < 0) return error.UinputOpen;
    defer _ = c.close(keyboard);
    if (c.ioctl(keyboard, ui_set_ev, @as(c_ulong, 0)) < 0 or
        c.ioctl(keyboard, ui_set_ev, @as(c_ulong, 1)) < 0 or
        c.ioctl(keyboard, ui_set_key, @as(c_ulong, key_home)) < 0) return error.UinputCapability;
    var device = UserDevice{};
    const name = "PF1007L Home";
    @memcpy(device.name[0..name.len], name);
    try writeExact(keyboard, std.mem.asBytes(&device));
    if (c.ioctl(keyboard, ui_create, @as(c_ulong, 0)) < 0) return error.UinputCreate;
    defer {
        key(keyboard, false) catch {};
        _ = c.ioctl(keyboard, ui_destroy, @as(c_ulong, 0));
    }
    var pipe: [2]c_int = undefined;
    if (c.pipe2(&pipe, c.O_NONBLOCK | c.O_CLOEXEC) != 0) return error.SignalPipe;
    defer {
        signal_write = -1;
        _ = c.close(pipe[0]);
        _ = c.close(pipe[1]);
    }
    signal_write = pipe[1];
    if (@intFromPtr(c.signal(c.SIGTERM, stop)) == std.math.maxInt(usize) or
        @intFromPtr(c.signal(c.SIGINT, stop)) == std.math.maxInt(usize)) return error.SignalHandler;
    defer {
        _ = c.signal(c.SIGTERM, ignore);
        _ = c.signal(c.SIGINT, ignore);
    }
    try notifyReady();
    std.debug.print("HOME_BUTTON_READY chip={s} line=2 key=172 held={any}\n", .{ chip_label, !debounce.armed });
    var polls = [_]c.struct_pollfd{
        .{ .fd = line, .events = c.POLLIN, .revents = 0 },
        .{ .fd = pipe[0], .events = c.POLLIN, .revents = 0 },
    };
    while (@as(*volatile c.sig_atomic_t, &stopping).* == 0) {
        const result = c.poll(&polls, polls.len, debounce.waitMs(try nowUs()));
        if (result < 0) {
            if (errno() == c.EINTR) continue;
            return error.Poll;
        }
        if (polls[1].revents != 0 or @as(*volatile c.sig_atomic_t, &stopping).* != 0) break;
        if (polls[0].revents & (c.POLLERR | c.POLLHUP | c.POLLNVAL) != 0) return error.GpioPoll;
        if (polls[0].revents & c.POLLIN != 0) {
            var edges: [16]Edge = undefined;
            var batches: usize = 0;
            while (batches < 16 and @as(*volatile c.sig_atomic_t, &stopping).* == 0) : (batches += 1) {
                const bytes = c.read(line, &edges, @sizeOf(@TypeOf(edges)));
                if (bytes < 0) {
                    if (errno() == c.EINTR) continue;
                    if (errno() == c.EAGAIN) break;
                    return error.GpioEvents;
                }
                if (bytes == 0 or @mod(bytes, @sizeOf(Edge)) != 0) return error.GpioEvents;
            }
            if (@as(*volatile c.sig_atomic_t, &stopping).* != 0) break;
            debounce.edgeBatch(try high(line), try nowUs());
        }
        // At a transition deadline resample the actual pin, not a stale edge queue.
        if (debounce.waitMs(try nowUs()) == 0) {
            const now = try nowUs();
            debounce.observe(try high(line), now);
            if (debounce.settle(now)) |pressed| {
                if (@as(*volatile c.sig_atomic_t, &stopping).* == 0) try key(keyboard, pressed);
            }
        }
    }
    std.debug.print("HOME_BUTTON_STOPPED\n", .{});
}

test "idle blocks indefinitely and held startup needs release before Home" {
    var state = Debounce.init(false, 0);
    try std.testing.expectEqual(@as(c_int, -1), state.waitMs(100_000));
    state.observe(true, 100_000);
    try std.testing.expect(state.settle(130_000) == null);
    try std.testing.expect(state.armed);
    state.observe(false, 200_000);
    try std.testing.expectEqual(@as(?bool, true), state.settle(230_000));
    try std.testing.expect(state.settle(240_000) == null);
}

test "bounce produces one press and one release with 30ms stability" {
    var state = Debounce.init(true, 0);
    state.observe(false, 1000);
    state.observe(true, 5000);
    state.observe(false, 9000);
    try std.testing.expectEqual(@as(c_int, 30), state.waitMs(9000));
    try std.testing.expect(state.settle(38_999) == null);
    try std.testing.expectEqual(@as(?bool, true), state.settle(39_000));
    try std.testing.expectEqual(@as(c_int, -1), state.waitMs(50_000));
    state.observe(true, 60_000);
    state.observe(false, 65_000);
    state.observe(true, 70_000);
    try std.testing.expectEqual(@as(?bool, false), state.settle(100_000));
    try std.testing.expectEqual(@as(c_int, -1), state.waitMs(200_000));
}

test "bounce back to stable cancels the transition timeout" {
    var state = Debounce.init(true, 0);
    state.observe(false, 100_000);
    state.observe(true, 110_000);
    try std.testing.expectEqual(@as(c_int, -1), state.waitMs(500_000));
    try std.testing.expect(state.settle(500_000) == null);
}

test "queued bounce ending at same candidate restarts stability" {
    var state = Debounce.init(true, 0);
    state.edgeBatch(false, 100_000);
    state.edgeBatch(false, 120_000); // queued high then low ended low
    try std.testing.expect(state.settle(130_000) == null);
    try std.testing.expectEqual(@as(?bool, true), state.settle(150_000));
}
