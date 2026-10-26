//! Fixed panel coordinates inside a resizable desktop window.
const std = @import("std");
const t = @import("types.zig");

pub const Viewport = struct {
    x: u32,
    y: u32,
    width: u32,
    height: u32,

    pub fn fit(width: u32, height: u32) Viewport {
        const scale = @min(@as(f64, @floatFromInt(width)) / 1280, @as(f64, @floatFromInt(height)) / 800);
        const w: u32 = @intFromFloat(@floor(1280 * scale));
        const h: u32 = @intFromFloat(@floor(800 * scale));
        return .{ .x = (width - w) / 2, .y = (height - h) / 2, .width = w, .height = h };
    }

    pub fn point(self: Viewport, x: i32, y: i32) ?t.Point {
        const px = @as(f32, @floatFromInt(x)) - @as(f32, @floatFromInt(self.x));
        const py = @as(f32, @floatFromInt(y)) - @as(f32, @floatFromInt(self.y));
        if (self.width == 0 or self.height == 0 or px < 0 or py < 0 or
            px >= @as(f32, @floatFromInt(self.width)) or py >= @as(f32, @floatFromInt(self.height))) return null;
        return .{
            .x = px * 1280 / @as(f32, @floatFromInt(self.width)),
            .y = py * 800 / @as(f32, @floatFromInt(self.height)),
        };
    }
};

test "letterboxing, Retina scaling and outside clicks preserve panel coordinates" {
    for ([_][2]u32{ .{ 1280, 800 }, .{ 640, 400 }, .{ 2560, 1600 }, .{ 1000, 1000 }, .{ 1400, 600 } }) |size| {
        const v = Viewport.fit(size[0], size[1]);
        const centre = v.point(@intCast(v.x + v.width / 2), @intCast(v.y + v.height / 2)).?;
        try std.testing.expectApproxEqAbs(@as(f32, 640), centre.x, 2);
        try std.testing.expectApproxEqAbs(@as(f32, 400), centre.y, 2);
        try std.testing.expect(v.point(@intCast(v.x + v.width), @intCast(v.y)) == null);
        try std.testing.expect(v.point(-1, 0) == null);
    }
    try std.testing.expect(Viewport.fit(0, 0).point(0, 0) == null);
}
