const std = @import("std");
pub const ui = @import("layout.zig");

pub fn card(a: std.mem.Allocator, s: ui.Style, children: []const ui.Node) !ui.Node {
    var style = s;
    style.background = 0x14212dff;
    style.radius = 24;
    style.padding = 24;
    style.gap = 8;
    return ui.column(a, style, children);
}

pub fn button(a: std.mem.Allocator, id: u64, title: []const u8, action: u32, selected: bool) !ui.Node {
    return ui.column(a, .{
        .id = id,
        .action = action,
        .height = .grow,
        .background = if (selected) 0x8be4c4ff else 0x1b2a36ff,
        .radius = 18,
        .alignment = .center,
        .justify = .center,
    }, &.{ui.text(title, 21, if (selected) 0x10211dff else 0xe8f0f5ff)});
}
const t = @import("types.zig");

fn circle(x: f32, y: f32, r: f32, c: u32, o: f32) t.Command {
    return .{
        .bounds = .{ .x = x - r, .y = y - r, .width = r * 2, .height = r * 2 },
        .kind = .{
            .circle = .{ .cx = x, .cy = y, .radius = r, .colour = c, .opacity = o },
        },
    };
}

fn rect(x: f32, y: f32, width: f32, height: f32, c: u32, r: f32) t.Command {
    return .{
        .bounds = .{ .x = x, .y = y, .width = width, .height = height },
        .kind = .{ .rectangle = .{ .colour = c, .radius = r } },
    };
}

pub fn accent(warm: bool) u32 {
    return if (warm) 0xefb880ff else 0x8be4c4ff;
}

pub fn orbit(a: std.mem.Allocator, phase: f32, warm: bool) !ui.Node {
    var shapes: std.ArrayList(t.Command) = .empty;
    for (0..80) |i| {
        const angle = @as(f32, @floatFromInt(i)) * std.math.tau / 80;
        try shapes.append(a, circle(120 + 80 * @cos(angle), 108 + 80 * @sin(angle), 0.7, 0x405360ff, 0.8));
    }
    try shapes.appendSlice(
        a,
        &.{
            circle(120, 108, 40, accent(warm), 0.08),
            circle(120, 108, 29, accent(warm), 0.18),
            circle(120, 108, 18, accent(warm), 1),
        },
    );
    for (0..12) |i| {
        const index: f32 = @floatFromInt(i);
        const angle = index * std.math.tau / 12 + phase * 0.2;
        try shapes.append(
            a,
            circle(
                120 + 80 * @cos(angle),
                108 + 80 * @sin(angle),
                @floatFromInt(3 + i % 3),
                accent(warm),
                0.25 + 0.75 * index / 12,
            ),
        );
    }
    try shapes.append(a, circle(120 + 80 * @cos(phase), 108 + 80 * @sin(phase), 7, 0xe8f0f5ff, 1));
    return ui.canvas(
        .{ .width = .{ .fixed = 240 }, .height = .{ .fixed = 216 }, .group = 11 },
        240,
        216,
        try shapes.toOwnedSlice(a),
    );
}

pub fn bars(a: std.mem.Allocator, phase: f32, music: bool, warm: bool, group: u64) !ui.Node {
    var shapes: std.ArrayList(t.Command) = .empty;
    const height: f32 = if (music) 102 else 90;
    const width: f32 = 346.6667;
    for (0..32) |i| {
        const index: f32 = @floatFromInt(i);
        const amplitude = @abs(@sin(index * 0.47 + phase * 1.8));
        const h = 10 + amplitude * (height - 14) * (if (music) @as(f32, 0.4) + 0.6 * @abs(@sin(index * 0.21 + phase)) else 1);
        try shapes.append(a, rect(index * width / 32, height - h, width / 32 - 4, h, accent(warm), 3));
    }
    return ui.canvas(.{ .height = .{ .fixed = height }, .group = group }, width, height, try shapes.toOwnedSlice(a));
}

pub fn gauge(a: std.mem.Allocator, value: f32, warm: bool) !ui.Node {
    var shapes: std.ArrayList(t.Command) = .empty;
    for (0..40) |i| {
        const index: f32 = @floatFromInt(i);
        const angle = -std.math.pi * 0.75 + index * std.math.pi * 1.5 / 39;
        try shapes.append(
            a,
            circle(
                100 + 65 * @cos(angle),
                90 + 65 * @sin(angle),
                4,
                if (index < value * 0.4) accent(warm) else 0x2c3d49ff,
                1,
            ),
        );
    }
    return ui.stack(a, .{
        .width = .{ .fixed = 200 },
        .height = .{ .fixed = 180 },
        .alignment = .center,
        .justify = .center,
    }, &.{
        ui.canvas(.{ .height = .grow, .group = 12 }, 200, 180, try shapes.toOwnedSlice(a)),
        try ui.column(
            a,
            .{ .width = .fit, .gap = 10, .alignment = .center },
            &.{
                ui.text(try std.fmt.allocPrint(a, "{d}", .{@as(u32, @intFromFloat(value))}), 40, 0xe8f0f5ff),
                ui.text("clear", 16, 0x8b9daaff),
            },
        ),
    });
}

pub fn slider(a: std.mem.Allocator, value: f32, warm: bool) !ui.Node {
    const width: f32 = 346.6667;
    return ui.interactive(
        ui.canvas(
            .{ .height = .{ .fixed = 70 }, .group = 40 },
            width,
            70,
            try a.dupe(
                t.Command,
                &.{
                    rect(0, 29, width, 12, 0x2c3d49ff, 6),
                    rect(0, 29, width * value, 12, accent(warm), 6),
                    circle(width * value, 35, 12, 0xe8f0f5ff, 1),
                },
            ),
        ),
        500,
        5,
        true,
    );
}
