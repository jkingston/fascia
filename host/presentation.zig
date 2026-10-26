//! Match output presentation to the scene commit which included visible Home.
const std = @import("std");

pub const Tracker = struct {
    sequence: ?u32 = null,
    home_pid: i32 = 0,

    pub fn begin(self: *Tracker, sequence: u32, home_pid: i32) void {
        self.* = .{ .sequence = sequence, .home_pid = home_pid };
    }

    pub fn clear(self: *Tracker) void {
        self.* = .{};
    }

    pub fn presented(self: *Tracker, sequence: u32, home_pid: i32, successful: bool, visible: bool) bool {
        if (self.sequence == null or self.sequence.? != sequence) return false;
        const valid = successful and visible and home_pid > 0 and home_pid == self.home_pid;
        self.clear();
        return valid;
    }
};

test "only successful matching Home scene commits prove presentation" {
    var tracker = Tracker{};
    try std.testing.expect(!tracker.presented(1, 42, true, true));
    tracker.begin(7, 42);
    try std.testing.expect(!tracker.presented(6, 42, true, true));
    try std.testing.expect(tracker.presented(7, 42, true, true));
    try std.testing.expect(!tracker.presented(7, 42, true, true));
    tracker.begin(8, 42);
    try std.testing.expect(!tracker.presented(8, 43, true, true));
    tracker.begin(9, 42);
    try std.testing.expect(!tracker.presented(9, 42, false, true));
    tracker.begin(10, 42);
    try std.testing.expect(!tracker.presented(10, 42, true, false));
    tracker.begin(11, 42);
    tracker.clear();
    try std.testing.expect(!tracker.presented(11, 42, true, true));
    tracker.begin(0, 42);
    try std.testing.expect(tracker.presented(0, 42, true, true));
}
