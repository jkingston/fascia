const std = @import("std");
const ui = @import("fascia").ui;
const t = @import("fascia").types;
const brightness_control = @import("brightness-control.zig");
const Countdown = @import("countdown.zig").Countdown;

pub const App = struct {
    id: []const u8,
    name: []const u8,
    description: []const u8 = "",
    version: []const u8 = "",
    builtin: bool = false,
};

pub const Action = union(enum) { launch: usize, startup: ?usize, remove: usize, display, setup, sound, brightness: u32 };

pub const Page = enum { apps, details, settings, connections, remove };

pub const Model = struct {
    apps: []const App,
    page: Page = .apps,
    offset: usize = 0,
    detail: usize = 0,
    startup: ?usize = null,
    countdown: Countdown = .{},
    banner: bool = false,
    interaction: ui.Interaction = .{},
    message: []const u8 = "",
    connections: []const u8 = "Checking local connections…",
    clock: []const u8 = "",
    brightness: u32 = 1,
    brightness_max: u32 = 0,
    brightness_known: bool = false,

    pub fn init(apps: []const App, startup: ?usize, enabled: bool) Model {
        const valid = if (startup) |i| if (i < apps.len) startup else null else null;
        return .{
            .apps = apps,
            .startup = valid,
            .countdown = Countdown.configure(if (enabled) valid else null, 10),
            .banner = enabled and valid != null,
        };
    }

    pub fn confirmStartup(self: *Model, chosen: ?usize, success: bool) void {
        if (!success) {
            self.message = "Could not confirm startup save.";
            return;
        }
        if (chosen) |i| {
            if (i >= self.apps.len) {
                self.message = "That app is no longer installed.";
                return;
            }
        }
        self.startup = chosen;
        self.message = if (chosen == null) "Home will open at startup." else "Startup app saved.";
    }

    pub fn update(self: *Model, now: i64, input: t.Input, hits: []const ui.Hit) ?Action {
        // A press at the deadline always cancels before a timer may launch.
        if (input.pressed or input.released or input.down) self.countdown.cancel();
        const event = self.interaction.update(hits, input);
        if (input.released) self.banner = false;
        if (event) |e| {
            if (e.action == 1) {
                self.detail = @intCast(e.id - 100);
                if (self.detail < self.apps.len) self.page = .details;
            } else switch (e.action) {
                2 => {
                    self.page = .apps;
                },
                3 => {
                    self.page = .settings;
                },
                4 => {
                    self.offset = if (self.offset + 6 < self.apps.len) self.offset + 6 else 0;
                },
                5 => {
                    self.banner = false;
                },
                6 => {
                    self.banner = false;
                    if (self.countdown.target) |target| return .{ .launch = target };
                },
                7 => {
                    if (self.detail < self.apps.len) return .{ .launch = self.detail };
                },
                8 => return .{ .startup = self.detail },
                9 => return .{ .startup = null },
                10 => return .display,
                11 => return .sound,
                13 => return .setup,
                12 => {
                    self.page = .connections;
                },
                18 => {
                    if (self.brightness_known) if (brightness_control.levelForValue(e.value, self.brightness_max)) |level| {
                        if (level != self.brightness) {
                            self.brightness = level;
                            return .{ .brightness = level };
                        }
                    };
                },
                14 => {
                    return self.stepBrightness(true);
                },
                16 => {
                    self.page = .remove;
                },
                17 => {
                    if (self.detail < self.apps.len and !self.apps[self.detail].builtin) return .{ .remove = self.detail };
                },
                15 => {
                    return self.stepBrightness(false);
                },
                else => {},
            }
        }
        if (self.countdown.tick(now, false)) |target| {
            self.banner = false;
            return .{ .launch = target };
        }
        return null;
    }

    pub fn stepBrightness(self: *Model, increase: bool) ?Action {
        if (!self.brightness_known or self.brightness_max == 0) return null;
        const level = std.math.clamp(self.brightness, 1, self.brightness_max);
        const next = if (increase) @min(self.brightness_max, level +| 1) else @max(1, level -| 1);
        if (next == self.brightness) return null;
        self.brightness = next;
        return .{ .brightness = next };
    }

    pub fn view(self: *const Model, a: std.mem.Allocator, now: i64, measure: t.Measure) !ui.Node {
        var n: std.ArrayList(ui.Node) = .empty;
        try n.append(a, label("Fascia", 48, 32, 18, muted));
        try n.append(a, label(self.clock, 1030, 32, 20, ink));
        if (self.page == .apps) {
            try n.append(a, label("Apps", 48, 78, 39, ink));
            try n.append(a, try button(a, "Settings", 3, 0, 1040, 82, 190, 60, false));
            if (self.apps.len == 0) {
                try n.append(a, label("No apps installed", 80, 280, 42, ink));
                try n.append(a, label("Install an app using the local Fascia tools.", 82, 350, 22, muted));
            }
            try n.append(a, rule(48, 158, 1184));
            const end = @min(self.offset + 6, self.apps.len);
            for (self.offset..end) |i| {
                const k = i - self.offset;
                const x = 48 + @as(f32, @floatFromInt(k % 3)) * 404;
                const y = 180 + @as(f32, @floatFromInt(k / 3)) * 250;
                var title = label(try ellipsis(a, self.apps[i].name, 32, 336, measure), 20, 60, 32, ink);
                title.style.position.?.width = 336;
                var metadata = label(if (self.startup == i) "Startup" else "", 20, 118, 15, accent);
                metadata.style.position.?.width = 336;
                try n.append(a, try ui.stack(a, .{
                    .id = 100 + i,
                    .action = 1,
                    .position = .{ .x = x, .y = y, .width = 376, .height = 222 },
                }, &.{ title, metadata, rule(20, 200, 336) }));
            }
            try n.append(
                a,
                label(
                    try std.fmt.allocPrint(a, "{d} installed {s}", .{ self.apps.len, if (self.apps.len == 1) "app" else "apps" }),
                    48,
                    728,
                    17,
                    muted,
                ),
            );
            if (self.apps.len > 6) try n.append(a, try button(a, "More apps →", 4, 0, 1030, 706, 202, 64, false));
        } else {
            try n.append(a, try button(a, "← Home", 2, 0, 48, 82, 170, 64, false));
            switch (self.page) {
                .details => if (self.detail < self.apps.len) {
                    const app = self.apps[self.detail];
                    try n.append(a, label(try ellipsis(a, app.name, 52, 1160, measure), 72, 220, 52, ink));
                    try n.append(a, label(try ellipsis(a, app.description, 23, 1156, measure), 76, 306, 23, muted));
                    try n.append(
                        a,
                        label(
                            try ellipsis(a, try std.fmt.allocPrint(a, "Version {s}", .{app.version}), 16, 1156, measure),
                            76,
                            366,
                            16,
                            muted,
                        ),
                    );
                    try n.append(a, try button(a, "Open app", 7, 0, 76, 472, 300, 88, true));
                    try n.append(
                        a,
                        try button(
                            a,
                            if (self.startup == self.detail) "Startup app" else "Use at startup",
                            8,
                            0,
                            400,
                            472,
                            300,
                            88,
                            false,
                        ),
                    );
                    if (!app.builtin) try n.append(a, try button(a, "Remove app…", 16, 0, 724, 472, 300, 88, false));
                    try n.append(
                        a,
                        label(
                            if (app.builtin) "Included with the frame system." else "Update this app using the local Fascia tools.",
                            76,
                            610,
                            18,
                            muted,
                        ),
                    );
                },
                .settings => {
                    try n.append(a, label("Settings", 270, 88, 38, ink));
                    try n.append(a, try button(a, "Display colour", 10, 0, 76, 218, 350, 100, false));
                    try n.append(a, try button(a, "Connections", 12, 0, 466, 218, 350, 100, false));
                    try n.append(a, try button(a, "Setup", 13, 0, 856, 218, 350, 100, false));
                    try n.append(a, label(if (self.brightness_known) "Brightness" else "Brightness unavailable", 76, 340, 27, ink));
                    if (self.brightness_known) {
                        try n.append(
                            a,
                            label(
                                try std.fmt.allocPrint(a, "{d} of {d}", .{ self.brightness, self.brightness_max }),
                                260,
                                347,
                                18,
                                muted,
                            ),
                        );
                        try n.append(a, label("Tap or drag", 700, 347, 17, muted));
                    }
                    if (self.brightness_known) try n.append(a, try brightness_control.view(a, self.brightness, self.brightness_max));
                    try n.append(a, try button(a, "Quiet sound test", 11, 0, 76, 548, 350, 80, false));
                    try n.append(a, try button(a, "Start in Home", 9, 0, 462, 548, 350, 80, false));
                    try n.append(a, label("Set a startup app from its details.", 76, 736, 18, muted));
                },
                .connections => {
                    try n.append(a, label("Connections", 72, 216, 42, ink));
                    var lines = std.mem.splitScalar(u8, self.connections, '\n');
                    var y: f32 = 306;
                    while (lines.next()) |line| {
                        try n.append(a, label(line, 76, y, 20, muted));
                        y += 52;
                    }
                },
                .remove => if (self.detail < self.apps.len) {
                    try n.append(
                        a,
                        label(try framed(a, "Remove ", self.apps[self.detail].name, "?", 42, 1160, measure), 72, 216, 42, ink),
                    );
                    try n.append(a, label("Remove the app and keep its saved data.", 76, 306, 24, muted));
                    try n.append(a, label("If this is your startup app, Home will open instead.", 76, 366, 21, muted));
                    if (!self.apps[self.detail].builtin) try n.append(a, try button(a, "Remove app", 17, 0, 76, 480, 300, 88, true));
                    try n.append(a, try button(a, "Cancel", 2, 0, 400, 480, 260, 88, false));
                },
                else => {},
            }
        }
        if (self.message.len > 0) try n.append(a, label(self.message, 76, 680, 18, accent));
        if (self.banner) {
            try n.append(a, try ui.column(a, .{
                .position = .{ .x = 24, .y = 16, .width = 1232, .height = 136 },
                .background = 0xe7e4d9ff,
                .radius = 5,
            }, &.{}));
            const target = self.countdown.target.?;
            var opening = label(
                try framed(
                    a,
                    "Opening ",
                    self.apps[target].name,
                    try std.fmt.allocPrint(a, " in {d}s", .{self.countdown.remaining(now)}),
                    26,
                    672,
                    measure,
                ),
                48,
                43,
                26,
                ink,
            );
            opening.style.position.?.width = 672;
            try n.append(a, opening);
            try n.append(a, label("Touch anywhere to cancel startup.", 48, 88, 17, muted));
            try n.append(a, try button(a, "Stay in Home", 5, 0, 748, 44, 244, 80, false));
            try n.append(a, try button(a, "Open now", 6, 0, 1010, 44, 220, 80, true));
        }
        return ui.stack(a, .{ .height = .grow, .background = 0xf3f1eaff }, try n.toOwnedSlice(a));
    }
};
const ink: u32 = 0x242520ff;
const muted: u32 = 0x696b62ff;
const accent: u32 = 0x9f4b32ff;

fn label(text: []const u8, x: f32, y: f32, size: f32, colour: u32) ui.Node {
    var node = ui.text(text, size, colour);
    node.style.position = .{ .x = x, .y = y, .width = 1232 - x, .height = size * 1.8 };
    return node;
}

fn button(
    a: std.mem.Allocator,
    text: []const u8,
    action: u32,
    id: usize,
    x: f32,
    y: f32,
    w: f32,
    h: f32,
    primary: bool,
) !ui.Node {
    return ui.column(a, .{
        .id = id,
        .action = action,
        .position = .{ .x = x, .y = y, .width = w, .height = h },
        .background = if (primary) ink else 0xe0dfd7ff,
        .radius = 5,
        .alignment = .center,
        .justify = .center,
    }, &.{ui.text(text, 21, if (primary) 0xffffffff else ink)});
}

test "invalid startup and any press cancel automatic launch" {
    const invalid = Model.init(&.{}, 4, true);
    try std.testing.expect(!invalid.banner);
    var model = Model.init(&.{.{ .id = "test", .name = "Test" }}, 0, true);
    model.countdown.presented(0);
    try std.testing.expectEqual(@as(?Action, null), model.update(10_000_000, .{ .pressed = true }, &.{}));
    try std.testing.expectEqual(@as(?Action, null), model.update(20_000_000, .{}, &.{}));
}

test "startup changes are acknowledged and open-now remains release-inside" {
    var model = Model.init(&.{.{ .id = "test", .name = "Test" }}, null, false);
    model.confirmStartup(0, false);
    try std.testing.expectEqual(@as(?usize, null), model.startup);
    model.confirmStartup(0, true);
    try std.testing.expectEqual(@as(?usize, 0), model.startup);
    model.confirmStartup(9, true);
    try std.testing.expectEqual(@as(?usize, 0), model.startup);
    model = Model.init(model.apps, 0, true);
    const hits = [_]ui.Hit{.{
        .id = 0,
        .action = 6,
        .bounds = .{ .x = 20, .y = 20, .width = 100, .height = 80 },
        .drag = false,
    }};
    model.countdown.presented(0);
    try std.testing.expect(model.update(10_000_000, .{ .x = 30, .y = 30, .pressed = true, .down = true }, &hits) == null);
    const action = model.update(10_100_000, .{ .x = 30, .y = 30, .released = true }, &hits).?;
    try std.testing.expectEqual(@as(usize, 0), action.launch);
    try std.testing.expect(!model.banner);
}

test "all Home pages compose with installed registry data" {
    var arena = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena.deinit();
    var model = Model.init(&.{.{
        .id = "clock",
        .name = "Clock",
        .description = "The time, quietly.",
        .version = "1.0.0",
    }}, 0, true);
    for (std.enums.values(Page)) |page| {
        model.page = page;
        _ = try model.view(arena.allocator(), 0, testMeasure());
    }
}

fn rule(x: f32, y: f32, width: f32) ui.Node {
    return .{
        .style = .{
            .position = .{ .x = x, .y = y, .width = width, .height = 1 },
            .background = 0xd1d1c6ff,
        },
    };
}

/// Measures complete candidates including the ellipsis; never splits a UTF-8 scalar.
fn ellipsis(a: std.mem.Allocator, text: []const u8, size: f32, width: f32, measure: t.Measure) ![]const u8 {
    if (!std.math.isFinite(width) or width <= 0) return "";
    const view = std.unicode.Utf8View.init(text) catch return "";
    if (measure.text(text, size).width <= width) return text;
    const mark = "…";
    if (measure.text(mark, size).width > width) return "";
    const buffer = try a.alloc(u8, text.len + mark.len);
    var iterator = view.iterator();
    var end: usize = 0;
    var best: usize = 0;
    while (iterator.nextCodepointSlice()) |part| {
        end += part.len;
        @memcpy(buffer[0..end], text[0..end]);
        @memcpy(buffer[end..][0..mark.len], mark);
        if (measure.text(buffer[0 .. end + mark.len], size).width <= width) best = end else break;
    }
    @memcpy(buffer[0..best], text[0..best]);
    @memcpy(buffer[best..][0..mark.len], mark);
    return buffer[0 .. best + mark.len];
}

/// Reserve the measured prefix and suffix so actions/countdown text never disappears.
fn framed(
    a: std.mem.Allocator,
    prefix: []const u8,
    text: []const u8,
    suffix: []const u8,
    size: f32,
    width: f32,
    measure: t.Measure,
) ![]const u8 {
    const fixed_width = measure.text(prefix, size).width + measure.text(suffix, size).width;
    const fitted = try ellipsis(a, text, size, @max(0, width - fixed_width), measure);
    const combined = try std.fmt.allocPrint(a, "{s}{s}{s}", .{ prefix, fitted, suffix });
    // Recheck the full string in case shaping changes the summed width.
    if (measure.text(combined, size).width <= width) return combined;
    return ellipsis(a, combined, size, width, measure);
}

fn testMeasure() t.Measure {
    return .{ .function = struct {
        fn width(_: ?*anyopaque, text: []const u8, size: f32) t.Metrics {
            var total: f32 = 0;
            const view = std.unicode.Utf8View.init(text) catch return .{ .width = 0, .height = size };
            var iterator = view.iterator();
            while (iterator.nextCodepoint()) |cp| total += if (cp >= 0x3000) size * 2 else if (cp == 'i') size * 0.2 else size * 0.6;
            return .{ .width = total, .height = size };
        }
    }.width };
}

test "measured ellipsis fits wide UTF8 glyphs and preserves registry strings" {
    var arena = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena.deinit();
    const a = arena.allocator();
    const measure = testMeasure();
    const original = "世界世界世界世界世界世界";
    const narrow = "iiiiiiiiiiiiiiiiiiiiiiii";
    const fitted = try ellipsis(a, original, 20, 100, measure);
    try std.testing.expect(std.unicode.utf8ValidateSlice(fitted));
    try std.testing.expect(std.mem.endsWith(u8, fitted, "…"));
    try std.testing.expect(measure.text(fitted, 20).width <= 100);
    try std.testing.expectEqualStrings(narrow, try ellipsis(a, narrow, 20, 100, measure));
    try std.testing.expectEqualStrings("", try ellipsis(a, original, 20, 1, measure));
    try std.testing.expectEqualStrings("世界世界世界世界世界世界", original);
    const banner = try framed(a, "Opening ", original, " in 10s", 26, 672, measure);
    try std.testing.expect(measure.text(banner, 26).width <= 672);
    try std.testing.expect(std.mem.endsWith(u8, banner, " in 10s"));
    const title = try framed(a, "Remove ", original, "?", 42, 1160, measure);
    try std.testing.expect(measure.text(title, 42).width <= 1160);
    try std.testing.expect(std.mem.endsWith(u8, title, "?"));
}

test "wide registry metadata fits card details removal and countdown bounds" {
    var arena = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena.deinit();
    const measure = testMeasure();
    const wide = "世界世界世界世界世界世界世界世界世界世界世界世界世界世界世界世界世界世界世界世界";
    var model = Model.init(&.{.{ .id = "wide", .name = wide, .description = wide, .version = "1" }}, 0, true);
    for ([_]Page{ .apps, .details, .remove }) |page| {
        model.page = page;
        const node = try model.view(arena.allocator(), 0, measure);
        const scene = try ui.build(arena.allocator(), node, .{ .width = 1280, .height = 800 }, measure);
        for (scene.commands) |command| switch (command.kind) {
            .text => |text| {
                try std.testing.expect(std.unicode.utf8ValidateSlice(text.content));
                try std.testing.expect(measure.text(text.content, text.size).width <= command.bounds.width + 0.01);
                if (std.mem.startsWith(u8, text.content, "Opening ")) {
                    try std.testing.expectEqual(@as(f32, 672), command.bounds.width);
                    try std.testing.expect(std.mem.endsWith(u8, text.content, " in 10s"));
                }
            },
            else => {},
        };
    }
    try std.testing.expectEqualStrings(wide, model.apps[0].name);
    try std.testing.expectEqualStrings(wide, model.apps[0].description);
}

test "plain launcher preserves six app touch targets and omits descriptions" {
    var arena = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena.deinit();
    const apps = [_]App{.{
        .id = "test",
        .name = "Test",
        .description = "This description belongs only in details.",
    }} ** 6;
    const model = Model.init(&apps, 2, false);
    const measure = testMeasure();
    const node = try model.view(arena.allocator(), 0, measure);
    const scene = try ui.build(arena.allocator(), node, .{ .width = 1280, .height = 800 }, measure);
    var app_hits: usize = 0;
    for (scene.hits) |hit| {
        if (hit.action != 1) continue;
        const i = hit.id - 100;
        try std.testing.expectEqual(@as(f32, 48) + @as(f32, @floatFromInt(i % 3)) * 404, hit.bounds.x);
        try std.testing.expectEqual(@as(f32, 180) + @as(f32, @floatFromInt(i / 3)) * 250, hit.bounds.y);
        try std.testing.expectEqual(@as(f32, 376), hit.bounds.width);
        try std.testing.expectEqual(@as(f32, 222), hit.bounds.height);
        app_hits += 1;
    }
    try std.testing.expectEqual(@as(usize, 6), app_hits);
    var startup_labels: usize = 0;
    for (scene.commands) |command| switch (command.kind) {
        .text => |text| {
            try std.testing.expect(!std.mem.eql(u8, text.content, apps[0].description));
            try std.testing.expect(!std.mem.eql(u8, text.content, "INSTALLED APP"));
            if (std.mem.eql(u8, text.content, "Startup")) startup_labels += 1;
        },
        else => {},
    };
    try std.testing.expectEqual(@as(usize, 1), startup_labels);
}

test "coarse brightness steps actual levels without percentage drift" {
    var model = Model.init(&.{}, null, false);
    model.brightness = 5;
    model.brightness_max = 8;
    model.brightness_known = true;
    for (0..32) |_| {
        try std.testing.expectEqual(@as(u32, 6), model.stepBrightness(true).?.brightness);
        try std.testing.expectEqual(@as(u32, 5), model.stepBrightness(false).?.brightness);
    }
    model.brightness = 8;
    try std.testing.expect(model.stepBrightness(true) == null);
    model.brightness = 1;
    try std.testing.expect(model.stepBrightness(false) == null);
    model.brightness_known = false;
    try std.testing.expect(model.stepBrightness(true) == null);
    model.brightness_known = true;
    model.brightness_max = 17;
    model.brightness = 16;
    try std.testing.expectEqual(@as(u32, 17), model.stepBrightness(true).?.brightness);
    try std.testing.expect(model.stepBrightness(true) == null);
}

test "brightness rail taps drag clamps and suppresses repeated level commands" {
    var arena = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena.deinit();
    var model = Model.init(&.{}, null, false);
    model.page = .settings;
    model.brightness_known = true;
    model.brightness = 5;
    model.brightness_max = 8;
    const node = try model.view(arena.allocator(), 0, testMeasure());
    const scene = try ui.build(arena.allocator(), node, .{ .width = 1280, .height = 800 }, testMeasure());
    var rail: ?ui.Hit = null;
    for (scene.hits) |hit| if (hit.action == 18) {
        rail = hit;
    };
    try std.testing.expectEqual(brightness_control.bounds, rail.?.bounds);
    const r = rail.?.bounds;
    try std.testing.expectEqual(
        @as(u32, 1),
        model.update(0, .{ .x = r.x + 10, .y = r.y + 50, .down = true, .pressed = true }, scene.hits).?.brightness,
    );
    try std.testing.expect(model.update(0, .{ .x = r.x + 10, .y = r.y + 50, .down = true }, scene.hits) == null);
    try std.testing.expectEqual(@as(u32, 8), model.update(0, .{ .x = 1400, .y = r.y + 50, .down = true }, scene.hits).?.brightness);
    try std.testing.expect(model.update(0, .{ .x = 1400, .y = r.y + 50, .released = true }, scene.hits) == null);
}
