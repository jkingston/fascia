const std = @import("std");
const schedule = @import("runtime-schedule.zig");

test "minute clock sleeps without periodic wake and every external cause wakes it" {
    const next = schedule.deadline(160, 100_000_000);
    try std.testing.expectEqual(@as(i32, 60_000), schedule.timeout(100_000_000, next));
    try std.testing.expect(!schedule.due(159_999_999, next, false, false, false));
    try std.testing.expect(schedule.due(160_000_000, next, false, false, false));
    try std.testing.expectEqual(@as(i32, -1), schedule.timeout(100, null));
    try std.testing.expect(!schedule.due(100, null, false, false, false));
    try std.testing.expect(schedule.due(100, null, true, false, false));
    try std.testing.expect(schedule.due(100, null, false, true, false));
    try std.testing.expect(schedule.due(100, null, false, false, true));
}

test "deadline handles overdue, fractional and invalid app wake times" {
    try std.testing.expectEqual(@as(?i64, 10_000_000), schedule.deadline(1, 10_000_000));
    try std.testing.expectEqual(@as(i32, 1), schedule.timeout(999_999, 1_000_000));
    try std.testing.expectEqual(@as(i32, 0), schedule.timeout(1_000_000, 1_000_000));
    try std.testing.expectEqual(@as(?i64, null), schedule.deadline(std.math.nan(f64), 0));
    try std.testing.expectEqual(@as(?i64, null), schedule.deadline(std.math.inf(f64), 0));
}

const wake = @import("runtime-wake.zig");
const c = @cImport({
    @cInclude("unistd.h");
    @cInclude("sys/wait.h");
});

test "worker notification wakes an indefinite wait without requiring display traffic" {
    var pipe: [2]c_int = undefined;
    try std.testing.expectEqual(@as(c_int, 0), c.pipe(&pipe));
    defer _ = c.close(pipe[0]);
    defer _ = c.close(pipe[1]);
    const child = c.fork();
    try std.testing.expect(child >= 0);
    if (child == 0) {
        _ = c.usleep(20_000);
        _ = c.write(pipe[1], "x", 1);
        c._exit(0);
    }
    defer {
        var status: c_int = 0;
        _ = c.waitpid(child, &status, 0);
    }
    const ready = try wake.wait(-1, false, pipe[0], 1000);
    try std.testing.expect(ready.worker and !ready.display and !ready.failed);
    // Published bytes stay readable if they arrive before the next sleep.
    try std.testing.expect((try wake.wait(-1, false, pipe[0], 0)).worker);
    var byte: [1]u8 = undefined;
    try std.testing.expectEqual(@as(isize, 1), c.read(pipe[0], &byte, 1));
    try std.testing.expect(!(try wake.wait(-1, false, pipe[0], 0)).worker);
}
