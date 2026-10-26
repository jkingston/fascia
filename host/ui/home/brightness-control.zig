//! Eight-step brightness control. One touch region includes the gaps.
const std = @import("std");
const ui = @import("fascia").ui;
const t = @import("fascia").types;
pub const bounds = t.Rect{ .x = 76, .y = 390, .width = 736, .height = 104 };
pub const action: u32 = 18;
const gap: f32 = 10;
const middle: f32 = 48;

fn segmentCount(maximum: u32) u32 {
    return @min(maximum, 16);
}

fn segmentWidth(maximum: u32) f32 {
    const count: f32 = @floatFromInt(segmentCount(maximum));
    return (bounds.width - gap * (count - 1)) / count;
}

fn xForLevel(level: u32, maximum: u32) f32 {
    if (maximum < 2) return bounds.width / 2;
    const inset = segmentWidth(maximum) / 2;
    const fraction = @as(f64, @floatFromInt(std.math.clamp(level, 1, maximum) - 1)) / @as(f64, @floatFromInt(maximum - 1));
    return inset + @as(f32, @floatCast(fraction)) * (bounds.width - inset * 2);
}

pub fn levelForValue(value: f32, maximum: u32) ?u32 {
    if (maximum == 0 or !std.math.isFinite(value)) return null;
    if (maximum == 1) return 1;
    const inset = segmentWidth(maximum) / 2;
    const fraction: f64 = @floatCast(std.math.clamp((value * bounds.width - inset) / (bounds.width - inset * 2), 0, 1));
    return 1 + @as(u32, @intFromFloat(@round(fraction * @as(f64, @floatFromInt(maximum - 1)))));
}

pub fn view(a: std.mem.Allocator, level: u32, maximum: u32) !ui.Node {
    if (maximum == 0) return ui.stack(a, .{}, &.{});
    var paint: std.ArrayList(t.Command) = .empty;
    const count = segmentCount(maximum);
    const width = segmentWidth(maximum);
    const selected_level = std.math.clamp(level, 1, maximum);
    const fraction: f64 = if (maximum < 2) 0 else @as(f64, @floatFromInt(selected_level - 1)) / @as(f64, @floatFromInt(maximum - 1));
    const selected: u32 = @intFromFloat(@round(fraction * @as(f64, @floatFromInt(count - 1))));
    for (0..count) |i| {
        const x = @as(f32, @floatFromInt(i)) * (width + gap);
        const current = i == selected;
        const lit = i < selected;
        try rect(a, &paint, x, 13, width, 70, if (current) 0x252b24ff else if (lit) 0x707666ff else 0xcfd1c5ff, 8);
        try rect(
            a,
            &paint,
            x + 1,
            14,
            width - 2,
            67,
            if (current) 0x30372eff else if (lit) 0x7b816fff else 0xe4e5dcff,
            7,
        );
        if (current) {
            const intensity: f32 = @floatCast(fraction);
            try sun(a, &paint, x + width / 2, middle, 7 + intensity * 2, 0xf8e2b4ff);
            try rect(a, &paint, x + width / 2 - 10, 94, 20, 3, 0x9f4b32ff, 1.5);
        } else {
            try circle(a, &paint, x + width / 2, middle, 2.5, if (lit) 0xdfe3d2ff else 0xa2a798ff);
        }
    }
    return ui.interactive(
        ui.canvas(.{ .position = bounds }, bounds.width, bounds.height, try paint.toOwnedSlice(a)),
        1800,
        action,
        true,
    );
}

fn rect(
    a: std.mem.Allocator,
    paint: *std.ArrayList(t.Command),
    x: f32,
    y: f32,
    width: f32,
    height: f32,
    colour: u32,
    radius: f32,
) !void {
    if (width <= 0) return;
    try paint.append(a, .{
        .bounds = .{ .x = x, .y = y, .width = width, .height = height },
        .kind = .{ .rectangle = .{ .colour = colour, .radius = radius } },
    });
}

fn circle(a: std.mem.Allocator, paint: *std.ArrayList(t.Command), x: f32, y: f32, radius: f32, colour: u32) !void {
    try paint.append(a, .{
        .bounds = .{
            .x = x - radius,
            .y = y - radius,
            .width = radius * 2,
            .height = radius * 2,
        },
        .kind = .{ .circle = .{ .cx = x, .cy = y, .radius = radius, .colour = colour } },
    });
}

fn sun(a: std.mem.Allocator, paint: *std.ArrayList(t.Command), x: f32, y: f32, radius: f32, colour: u32) !void {
    try circle(a, paint, x, y, radius, colour);
    for (0..8) |i| {
        const angle = @as(f32, @floatFromInt(i)) * std.math.tau / 8;
        const inner = radius + 4;
        const outer = radius + 8;
        const x1 = x + @cos(angle) * inner;
        const y1 = y + @sin(angle) * inner;
        const x2 = x + @cos(angle) * outer;
        const y2 = y + @sin(angle) * outer;
        try paint.append(a, .{
            .bounds = .{
                .x = @min(x1, x2) - 1,
                .y = @min(y1, y2) - 1,
                .width = @abs(x2 - x1) + 2,
                .height = @abs(y2 - y1) + 2,
            },
            .kind = .{
                .line = .{ .x1 = x1, .y1 = y1, .x2 = x2, .y2 = y2, .width = 2, .colour = colour },
            },
        });
    }
}

test "separate brightness segments retain one touch capture" {
    var arena = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena.deinit();
    const dim = try view(arena.allocator(), 1, 8);
    const bright = try view(arena.allocator(), 8, 8);
    try std.testing.expectEqual(action, dim.style.action);
    try std.testing.expect(dim.style.drag and bright.style.drag);
    try std.testing.expectEqual(bounds, dim.style.position.?);
}

test "all eight detents map exactly and clamp outside the capture" {
    for (1..9) |level| {
        const value = xForLevel(@intCast(level), 8) / bounds.width;
        try std.testing.expectEqual(@as(u32, @intCast(level)), levelForValue(value, 8).?);
    }
    try std.testing.expectEqual(@as(u32, 1), levelForValue(-1, 8).?);
    try std.testing.expectEqual(@as(u32, 8), levelForValue(2, 8).?);
    try std.testing.expectEqual(@as(u32, 1), levelForValue(0.5, 1).?);
    try std.testing.expectEqual(std.math.maxInt(u32), levelForValue(1, std.math.maxInt(u32)).?);
    try std.testing.expect(levelForValue(0.5, 0) == null);
    try std.testing.expect(levelForValue(std.math.nan(f32), 8) == null);
    try std.testing.expect(segmentWidth(8) >= 70);
}
