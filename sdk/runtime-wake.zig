//! Shared POSIX wait: notification pipes work before or during the wait.
const builtin = @import("builtin");
const c = @cImport({
    @cInclude("poll.h");
    @cInclude("errno.h");
});

pub const Ready = struct {
    display: bool = false,
    failed: bool = false,
    worker: bool = false,
};

pub fn wait(display_fd: i32, write_pending: bool, wake_fd: ?i32, timeout_ms: i32) !Ready {
    var fds = [_]c.struct_pollfd{
        .{
            .fd = display_fd,
            .events = @intCast(c.POLLIN | (if (write_pending) c.POLLOUT else @as(c_int, 0))),
            .revents = 0,
        },
        .{ .fd = wake_fd orelse -1, .events = c.POLLIN, .revents = 0 },
    };
    if (c.poll(&fds, fds.len, timeout_ms) < 0) {
        const errno = if (builtin.os.tag == .macos) c.__error().* else c.__errno_location().*;
        if (errno == c.EINTR) return .{};
        return error.Poll;
    }
    return .{
        .display = fds[0].revents & c.POLLIN != 0,
        .failed = fds[0].revents & (c.POLLERR | c.POLLHUP | c.POLLNVAL) != 0,
        .worker = fds[1].revents != 0,
    };
}
