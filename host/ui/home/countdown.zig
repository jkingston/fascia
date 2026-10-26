const std = @import("std");
/// A one-shot monotonic countdown. Input is processed before deadline evaluation.
pub const Countdown = struct {
    target: ?usize = null,
    duration_us: i64 = 10_000_000,
    started: ?i64 = null,
    cancelled: bool = false,
    launched: bool = false,

    pub fn configure(target: ?usize, seconds: u8) Countdown {
        return .{
            .target = target,
            .duration_us = @as(i64, std.math.clamp(seconds, 5, 60)) * 1_000_000,
        };
    }

    pub fn presented(self: *Countdown, now: i64) void {
        if (self.target != null and self.started == null and !self.cancelled and !self.launched) self.started = now;
    }

    pub fn cancel(self: *Countdown) void {
        self.cancelled = true;
    }

    pub fn remaining(self: Countdown, now: i64) u32 {
        const elapsed = if (self.started) |start| @max(0, now - start) else 0;
        return @intCast(@divTrunc(@max(0, self.duration_us - elapsed) + 999_999, 1_000_000));
    }

    pub fn tick(self: *Countdown, now: i64, interaction: bool) ?usize {
        if (interaction) self.cancel();
        if (self.cancelled or self.launched or self.started == null or self.target == null) return null;
        if (self.remaining(now) != 0) return null;
        self.launched = true;
        return self.target;
    }

    pub fn pending(self: Countdown) bool {
        return self.target != null and !self.cancelled and !self.launched;
    }
};

test "visible start absolute elapsed cancellation wins deadline and launch once" {
    var timer = Countdown.configure(2, 10);
    try std.testing.expectEqual(@as(?usize, null), timer.tick(90_000_000, false));
    timer.presented(100_000_000);
    try std.testing.expectEqual(@as(u32, 10), timer.remaining(100_000_000));
    try std.testing.expectEqual(@as(u32, 1), timer.remaining(109_500_000));
    try std.testing.expectEqual(@as(?usize, null), timer.tick(110_000_000, true));
    timer.presented(120_000_000);
    try std.testing.expectEqual(@as(?usize, null), timer.tick(140_000_000, false));
    var launch = Countdown.configure(3, 10);
    launch.presented(0);
    try std.testing.expectEqual(@as(?usize, 3), launch.tick(40_000_000, false));
    try std.testing.expectEqual(@as(?usize, null), launch.tick(41_000_000, false));
    var invalid = Countdown.configure(null, 10);
    invalid.presented(0);
    try std.testing.expectEqual(@as(?usize, null), invalid.tick(100_000_000, false));
}
