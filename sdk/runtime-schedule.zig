//! Pure timing rules shared by the event loop and its host tests.
const std = @import("std");

pub fn deadline(seconds: ?f64, now: i64) ?i64 {
    const value = seconds orelse return null;
    if (!std.math.isFinite(value)) return null;
    // Never overflow a poll deadline supplied by an application.
    const micros = std.math.clamp(value * 1000000, -9e18, 9e18);
    return @max(now, @as(i64, @intFromFloat(micros)));
}

pub fn timeout(now: i64, next: ?i64) i32 {
    const target = next orelse return -1;
    if (target <= now) return 0;
    return @intCast(@min(@divTrunc(target - now + 999, 1000), std.math.maxInt(i32)));
}

pub fn due(now: i64, next: ?i64, input: bool, worker: bool, resized: bool) bool {
    return input or worker or resized or if (next) |target| now >= target else false;
}
