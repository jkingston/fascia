const std = @import("std");
const worker = @import("worker.zig");
const c = @cImport({
    @cInclude("stdlib.h");
    @cInclude("poll.h");
    @cInclude("time.h");
});

fn seconds() f64 {
    var ts: c.struct_timespec = undefined;
    _ = c.clock_gettime(c.CLOCK_MONOTONIC, &ts);
    return @as(f64, @floatFromInt(ts.tv_sec)) + @as(f64, @floatFromInt(ts.tv_nsec)) / 1e9;
}

pub fn main() !void {
    const a = std.heap.c_allocator;
    const directory = c.getenv("FASCIA_CLOCK_CHECK_STATE") orelse return error.ExplicitCheckDirectoryRequired;
    const offline = c.getenv("FASCIA_CLOCK_CHECK_OFFLINE") != null;
    const background = try worker.Worker.initWithNetwork(a, std.mem.span(directory), .{}, !offline);
    defer background.deinit();
    const started = seconds();
    var seen_network = false;
    while (seconds() - started < 120) {
        var fd = c.struct_pollfd{ .fd = background.pipes[0], .events = c.POLLIN, .revents = 0 };
        _ = c.poll(&fd, 1, 1000);
        if (background.take()) |snapshot| {
            defer snapshot.destroy(a);
            std.debug.print(
                "snapshot elapsed_s={d:.3} cached={d} size={d}x{d}\n",
                .{
                    seconds() - started,
                    snapshot.count,
                    snapshot.image.width,
                    snapshot.image.height,
                },
            );
            if (snapshot.count > 0) seen_network = true;
            if (offline) return;
        }
        if (offline and background.state() == .offline) return error.NoCachedPhotos;
        if (background.state() == .ready and seen_network) {
            std.debug.print("feed_complete elapsed_s={d:.3}\n", .{seconds() - started});
            return;
        }
    }
    return error.FeedCheckTimeout;
}
