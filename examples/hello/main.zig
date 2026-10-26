const std = @import("std");
const fascia = @import("fascia");
const ui = fascia.ui;

const App = struct {
    count: u32 = 0,
    interaction: ui.Interaction = .{},

    pub fn update(self: *App, input: fascia.types.Input, hits: []const ui.Hit, _: fascia.Context) void {
        if (self.interaction.update(hits, input)) |event| {
            switch (event.action) {
                1 => self.count +|= 1,
                2 => self.count = 0,
                else => {},
            }
        }
    }

    pub fn view(self: *const App, a: std.mem.Allocator, _: fascia.Context) !ui.Node {
        return ui.column(a, .{ .height = .grow, .background = 0xf3f1eaff }, &.{
            try ui.column(a, .{
                .position = .{ .x = 490, .y = 190, .width = 300, .height = 64 },
                .justify = .center,
            }, &.{
                ui.text("Counter", 36, 0x242520ff),
            }),
            try ui.column(a, .{
                .position = .{ .x = 490, .y = 270, .width = 300, .height = 150 },
                .justify = .center,
            }, &.{
                ui.text(try std.fmt.allocPrint(a, "{d}", .{self.count}), 104, 0x242520ff),
            }),
            try ui.column(a, .{
                .id = 1,
                .action = 1,
                .position = .{ .x = 490, .y = 470, .width = 300, .height = 88 },
                .background = 0x9f4b32ff,
                .radius = 5,
                .alignment = .center,
                .justify = .center,
            }, &.{
                ui.text("+1", 32, 0xf3f1eaff),
            }),
            try ui.column(a, .{
                .id = 2,
                .action = 2,
                .position = .{ .x = 490, .y = 582, .width = 300, .height = 72 },
                .background = 0xe5e3daff,
                .radius = 5,
                .alignment = .center,
                .justify = .center,
            }, &.{
                ui.text("Reset", 26, 0x242520ff),
            }),
        });
    }
};

pub fn main() !void {
    var app = App{};
    try fascia.run(&app);
}
