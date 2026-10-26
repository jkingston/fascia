const std = @import("std");

pub fn hour12(hour: u8) u8 {
    return if (hour % 12 == 0) 12 else hour % 12;
}

pub fn untilMinute(epoch: i64) f64 {
    return @floatFromInt(60 - @mod(epoch, 60));
}

pub fn swipe(x: f32, y: f32, end_x: f32, end_y: f32) ?i32 {
    const dx = end_x - x;
    return if (@abs(dx) > 70 and @abs(dx) > @abs(end_y - y)) (if (dx < 0) @as(i32, 1) else -1) else null;
}

test "clock midnight noon boundaries and gesture direction" {
    try std.testing.expectEqual(@as(u8, 12), hour12(0));
    try std.testing.expectEqual(@as(u8, 12), hour12(12));
    try std.testing.expectEqual(@as(u8, 1), hour12(13));
    try std.testing.expectEqual(@as(f64, 60), untilMinute(120));
    try std.testing.expectEqual(@as(f64, 1), untilMinute(179));
    try std.testing.expectEqual(@as(?i32, 1), swipe(500, 400, 350, 405));
    try std.testing.expect(swipe(500, 400, 490, 600) == null);
}

pub fn manualMarker(a: std.mem.Allocator, bytes: []const u8, boot: []const u8) bool {
    if (bytes.len > 2048) return false;

    const Marker = struct {
        schema_version: u8,
        unix_seconds: i64,
        boot_id: []const u8,
    };
    const parsed = std.json.parseFromSlice(Marker, a, bytes, .{}) catch return false;
    defer parsed.deinit();
    return parsed.value.schema_version == 1 and parsed.value.unix_seconds > 0 and std.mem.eql(u8, parsed.value.boot_id, std.mem.trim(u8, boot, "\r\n "));
}

test "manual clock proof belongs to current boot and valid schema" {
    const good = "{\"schema_version\":1,\"unix_seconds\":1800000000,\"boot_id\":\"current\"}";
    try std.testing.expect(manualMarker(std.testing.allocator, good, "current\n"));
    try std.testing.expect(!manualMarker(std.testing.allocator, good, "previous"));
    try std.testing.expect(!manualMarker(std.testing.allocator, "{}", "current"));
}
