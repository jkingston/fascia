//! Visual white balance with reversible live preview and explicit persistence.
const std = @import("std");
const ui = @import("fascia").ui;
const t = @import("fascia").types;
const display = @import("fascia").display;

pub const Model = struct {
    gains: [3]f32 = .{ 1, 1, 1 },
    saved: [3]f32 = .{ 1, 1, 1 },
    original: bool = false,
    request_close: bool = false,
    save_failed: bool = false,
    interaction: ui.Interaction = .{},

    pub fn init(saved: [3]f32) Model {
        return .{ .gains = saved, .saved = saved };
    }

    pub fn previewGains(self: *const Model) [3]f32 {
        return if (self.original) .{ 1, 1, 1 } else self.gains;
    }

    pub fn update(self: *Model, input: t.Input, hits: []const ui.Hit) void {
        const event = self.interaction.update(hits, input) orelse return;
        if (event.action == 10) {
            self.original = false;
            self.save_failed = false;
            if (event.id == 0) {
                self.gains = .{ 1, 1 - event.value * 0.08, 1 - event.value * 0.24 };
            } else if (event.id <= 3) self.gains[@intCast(event.id - 1)] = 0.5 + event.value * 0.5;
            return;
        }
        if (event.action != 1) return;
        self.save_failed = false;
        switch (event.id) {
            1 => self.original = !self.original,
            2 => {
                self.gains = .{ 1, 0.96, 0.88 };
                self.original = false;
            },
            3 => {
                self.gains = .{ 1, 0.92, 0.76 };
                self.original = false;
            },
            4 => {
                self.gains = .{ 1, 1, 1 };
                self.original = false;
            },
            5 => {
                const chosen = self.previewGains();
                const colour = display.Colour.init(chosen) catch {
                    self.save_failed = true;
                    return;
                };
                display.saveConfigured(colour) catch {
                    self.save_failed = true;
                    return;
                };
                self.saved = chosen;
                self.gains = chosen;
                self.request_close = true;
            },
            6 => {
                self.gains = self.saved;
                self.original = false;
                self.request_close = true;
            },
            else => {},
        }
    }

    pub fn view(self: *const Model, a: std.mem.Allocator, width: f32, height: f32) !ui.Node {
        var nodes: std.ArrayList(ui.Node) = .empty;
        const ink: u32 = 0x242520ff;
        const muted: u32 = 0x696b62ff;
        try nodes.append(a, label("Settings / Display", 76, 40, 15, muted));
        try nodes.append(a, label("Display colour", 76, 90, 42, ink));
        try nodes.append(a, label("Adjust until the white and grey patches look neutral.", 78, 151, 18, muted));
        const titles = [_][]const u8{ "Warmth", "Red", "Green", "Blue" };
        const colours = [_]u32{ 0x9f4b32ff, 0x9f4b45ff, 0x52745aff, 0x4b6991ff };
        for (titles, 0..) |title, i| {
            const y = 213 + @as(f32, @floatFromInt(i)) * 88;
            const value = if (i == 0) std.math.clamp((1 - self.gains[2]) / 0.24, 0, 1) else (self.gains[i - 1] - 0.5) * 2;
            try nodes.append(a, label(title, 76, y, 19, ink));
            const custom = i == 0 and
                (@abs(self.gains[0] - 1) > 0.0001 or
                    @abs(self.gains[1] - (1 - value * 0.08)) > 0.0001 or
                    @abs(self.gains[2] - (1 - value * 0.24)) > 0.0001);
            try nodes.append(
                a,
                label(
                    if (custom) "Custom" else try std.fmt.allocPrint(a, "{d:.0}%", .{if (i == 0) value * 100 else self.gains[i - 1] * 100}),
                    if (custom) 395 else 410,
                    y,
                    18,
                    muted,
                ),
            );
            try nodes.append(a, try slider(a, i, 76, y + 26, 404, value, colours[i]));
        }
        try nodes.append(a, try button(a, 1, if (self.original) "Adjusted" else "Original", 76, 600, 126, self.original));
        try nodes.append(a, try button(a, 2, "Warm", 215, 600, 126, false));
        try nodes.append(a, try button(a, 3, "Warmer", 354, 600, 126, false));
        try nodes.append(a, try button(a, 4, "Reset", 76, 674, 112, false));
        try nodes.append(a, try button(a, 6, "Cancel", 201, 674, 112, false));
        try nodes.append(a, try button(a, 5, "Save", 326, 674, 154, true));
        try nodes.append(
            a,
            label(
                if (self.original) "Original colours" else "Changes are not saved until you press Save.",
                78,
                752,
                15,
                muted,
            ),
        );

        var patches: std.ArrayList(t.Command) = .empty;
        try patches.append(a, rect(559, 225, 318, 152, 0xbfc0b5ff));
        try patches.append(a, rect(560, 226, 316, 150, 0xffffffff));
        try patches.append(a, rect(892, 226, 316, 150, 0xc0c0c0ff));
        for (0..12) |i| {
            const level: u32 = @intCast(i * 255 / 11);
            try patches.append(a, rect(560 + @as(f32, @floatFromInt(i)) * 54, 414, 54, 88, (level * 0x010101 << 8) | 255));
        }
        const swatches = [_]u32{
            0xff0000ff,
            0x00ff00ff,
            0x0000ffff,
            0xffff00ff,
            0x00ffffff,
            0xff00ffff,
        };
        for (swatches, 0..) |colour, i| try patches.append(a, rect(560 + @as(f32, @floatFromInt(i)) * 108, 558, 108, 58, colour));
        try nodes.append(
            a,
            ui.canvas(
                .{ .position = .{ .width = width, .height = height }, .group = 970 },
                width,
                height,
                try patches.toOwnedSlice(a),
            ),
        );
        try nodes.append(a, label("White", 560, 390, 13, muted));
        try nodes.append(a, label("Mid-grey", 892, 390, 13, muted));
        try nodes.append(a, label("Black", 560, 517, 13, muted));
        try nodes.append(a, label("White", 1156, 517, 13, muted));
        try nodes.append(a, label("Check for a colour tint in the grey steps.", 560, 660, 21, ink));
        try nodes.append(a, label("Use the room lighting you normally view the frame in.", 560, 701, 16, muted));
        if (self.save_failed) try nodes.append(a, label("Could not confirm save. Please try again.", 560, 751, 15, 0x9f4b32ff));
        return ui.stack(a, .{ .height = .grow, .background = 0xf3f1eaff }, try nodes.toOwnedSlice(a));
    }
};

fn label(content: []const u8, x: f32, y: f32, size: f32, colour: u32) ui.Node {
    var n = ui.text(content, size, colour);
    n.style.position = .{ .x = x, .y = y, .width = 1240 - x, .height = size * 1.5 };
    return n;
}

fn rect(x: f32, y: f32, width: f32, height: f32, colour: u32) t.Command {
    return .{
        .bounds = .{ .x = x, .y = y, .width = width, .height = height },
        .kind = .{ .rectangle = .{ .colour = colour } },
    };
}

fn button(a: std.mem.Allocator, id: u64, title: []const u8, x: f32, y: f32, width: f32, emphasis: bool) !ui.Node {
    return ui.column(a, .{
        .id = id,
        .action = 1,
        .position = .{ .x = x, .y = y, .width = width, .height = 58 },
        .radius = 4,
        .background = if (emphasis) 0x242520ff else 0xe0dfd7ff,
        .alignment = .center,
        .justify = .center,
    }, &.{ui.text(title, 18, if (emphasis) 0xf3f1eaff else 0x242520ff)});
}

fn slider(a: std.mem.Allocator, index: usize, x: f32, y: f32, width: f32, value: f32, colour: u32) !ui.Node {
    const commands = try a.dupe(t.Command, &.{ rect(0, 20, width, 4, 0xd0d0c6ff), rect(0, 20, width * value, 4, colour), .{
        .bounds = .{ .x = width * value - 9, .y = 13, .width = 18, .height = 18 },
        .kind = .{
            .circle = .{ .cx = width * value, .cy = 22, .radius = 9, .colour = colour },
        },
    } });
    return ui.interactive(ui.canvas(.{
        .position = .{ .x = x, .y = y, .width = width, .height = 48 },
        .group = 971 + index,
    }, width, 48, commands), index, 10, true);
}

test "display preview original cancel and captured slider remain reversible" {
    var model = Model.init(.{ 1, 0.96, 0.88 });
    const slider_hit = [_]ui.Hit{.{
        .bounds = .{ .x = 76, .y = 239, .width = 404, .height = 48 },
        .id = 0,
        .action = 10,
        .drag = true,
    }};
    model.update(.{ .x = 76, .y = 260, .pressed = true, .down = true }, &slider_hit);
    model.update(.{ .x = 600, .y = 260, .released = true }, &slider_hit);
    try std.testing.expectApproxEqAbs(@as(f32, 0.76), model.gains[2], 0.0001);
    const before = model.gains;
    model.original = true;
    try std.testing.expectEqual([3]f32{ 1, 1, 1 }, model.previewGains());
    model.original = false;
    try std.testing.expectEqual(before, model.previewGains());
    const cancel = [_]ui.Hit{.{
        .bounds = .{ .width = 100, .height = 60 },
        .id = 6,
        .action = 1,
        .drag = false,
    }};
    model.update(.{ .x = 30, .y = 30, .pressed = true, .down = true }, &cancel);
    model.update(.{ .x = 30, .y = 30, .released = true }, &cancel);
    try std.testing.expect(model.request_close);
    try std.testing.expectEqual([3]f32{ 1, 0.96, 0.88 }, model.previewGains());
}
