const std = @import("std");
const builtin = @import("builtin");
const fascia = @import("fascia");
const ui = fascia.ui;
const t = fascia.types;
const worker = @import("worker.zig");
const feed = @import("feed.zig");
const fs = @import("io.zig");
const logic = @import("logic.zig");
const c = @cImport({
    @cInclude("stdlib.h");
    @cInclude("time.h");
    @cInclude("sys/stat.h");
});
extern fn fascia_clock_synced() c_int;

pub const App = struct {
    a: std.mem.Allocator,
    background: *worker.Worker,
    snapshot: ?*worker.Snapshot = null,
    url: [2048]u8 = @splat(0),
    url_len: usize = 0,
    interval: u32 = 1800,
    twelve: bool = false,
    fill: bool = true,
    settings_open: bool = false,
    editor: ?fascia.keyboard.Model = null,
    scratch: [2048]u8 = undefined,
    interaction: ui.Interaction = .{},
    start_x: f32 = 0,
    start_y: f32 = 0,
    caption_until: f64 = 0,
    next_photo: f64 = 0,
    now: i64 = 0,
    trusted: bool = false,
    time_checked: bool = false,
    time_text: [48]u8 = @splat(0),
    time_len: usize = 0,
    date_text: [128]u8 = @splat(0),
    date_len: usize = 0,
    message: []const u8 = "",

    fn settings(self: *const App) worker.Settings {
        return .{
            .feed_url = self.url[0..self.url_len],
            .interval_seconds = self.interval,
            .twelve_hour = self.twelve,
            .fill = self.fill,
        };
    }

    pub fn wakeFd(self: *const App) ?i32 {
        return self.background.pipes[0];
    }

    pub fn nextWake(self: *const App, ctx: fascia.Context) ?f64 {
        var next = ctx.time_seconds + (if (self.trusted) logic.untilMinute(self.now) else @as(f64, 5));
        if (!self.settings_open and self.next_photo > 0) next = @min(next, self.next_photo);
        if (self.caption_until > ctx.time_seconds) next = @min(next, self.caption_until);
        if (self.editor) |editor| if (editor.erase_id != null) {
            next = @min(next, ctx.time_seconds + 0.04);
        };
        return next;
    }

    pub fn update(self: *App, input: t.Input, hits: []const ui.Hit, ctx: fascia.Context) void {
        if (self.background.take()) |snapshot| {
            if (self.snapshot) |old| old.destroy(self.a);
            self.snapshot = snapshot;
        }
        self.now = c.time(null);
        const previous_trust = self.trusted;
        self.trusted = timeTrusted(self.a);
        if (self.time_checked and !previous_trust and self.trusted) self.background.refreshFeed();
        self.time_checked = true;
        var tm: c.struct_tm = undefined;
        var timestamp: c.time_t = @intCast(self.now);
        if (self.trusted and c.localtime_r(&timestamp, &tm) != null) {
            self.time_len = c.strftime(&self.time_text, self.time_text.len, if (self.twelve) "%I:%M %p" else "%H:%M", &tm);
            self.date_len = c.strftime(&self.date_text, self.date_text.len, "%A, %e %B", &tm);
            if (self.twelve and self.time_len > 0 and self.time_text[0] == '0') {
                std.mem.copyForwards(u8, self.time_text[0 .. self.time_len - 1], self.time_text[1..self.time_len]);
                self.time_len -= 1;
            }
        } else {
            const waiting = "Setting time";
            @memcpy(self.time_text[0..waiting.len], waiting);
            self.time_len = waiting.len;
            self.date_len = 0;
        }
        if (self.next_photo == 0) self.next_photo = ctx.time_seconds + @as(f64, @floatFromInt(self.interval));
        if (!self.settings_open and ctx.time_seconds >= self.next_photo) {
            self.background.next(1);
            self.next_photo = ctx.time_seconds + @as(f64, @floatFromInt(self.interval));
        }
        if (self.editor) |*editor| {
            if (editor.update(input, hits, ctx.time_seconds)) |result| {
                if (result == .submit) {
                    const url = editor.text();
                    if (feed.validUrl(url)) {
                        self.url_len = url.len;
                        @memcpy(self.url[0..self.url_len], url);
                        self.background.configure(self.settings());
                        self.message = "Feed updated. Loading photographs in the background.";
                    } else self.message = "Enter a public HTTP or HTTPS RSS address without a password.";
                }
                self.editor = null;
            }
            return;
        }
        if (self.interaction.update(hits, input)) |event| {
            switch (event.action) {
                1 => {
                    self.settings_open = true;
                    self.caption_until = 0;
                },
                2 => {
                    self.settings_open = false;
                    self.next_photo = ctx.time_seconds + @as(f64, @floatFromInt(self.interval));
                },
                3 => self.editor = fascia.keyboard.Model.init(&self.scratch, self.url[0..self.url_len], .url) catch null,
                4 => {
                    self.twelve = !self.twelve;
                    self.background.configure(self.settings());
                },
                5 => {
                    self.fill = !self.fill;
                    self.background.configure(self.settings());
                },
                10...13 => {
                    const choices = [_]u32{ 600, 1800, 3600, 7200 };
                    self.interval = choices[event.action - 10];
                    self.background.configure(self.settings());
                    self.next_photo = ctx.time_seconds + @as(f64, @floatFromInt(self.interval));
                },
                else => {},
            }
            return;
        }
        if (self.settings_open) return;
        if (input.pressed) {
            self.start_x = input.x;
            self.start_y = input.y;
        }
        if (input.released) {
            if (logic.swipe(self.start_x, self.start_y, input.x, input.y)) |delta| {
                self.background.next(delta);
                self.next_photo = ctx.time_seconds + @as(f64, @floatFromInt(self.interval));
            } else if (@abs(input.x - self.start_x) < 25 and @abs(input.y - self.start_y) < 25) self.caption_until = ctx.time_seconds + 9;
        }
    }

    pub fn view(self: *App, a: std.mem.Allocator, ctx: fascia.Context) !ui.Node {
        if (self.settings_open) return self.settingsView(a, ctx);
        var p: std.ArrayList(t.Command) = .empty;
        try rect(a, &p, .{ .width = ctx.width, .height = ctx.height }, 0x111619ff);
        if (self.snapshot) |snapshot| {
            const im = snapshot.image;
            const sx = ctx.width / @as(f32, @floatFromInt(im.width));
            const sy = ctx.height / @as(f32, @floatFromInt(im.height));
            const scale = if (self.fill) @max(sx, sy) else @min(sx, sy);
            const w = @as(f32, @floatFromInt(im.width)) * scale;
            const h = @as(f32, @floatFromInt(im.height)) * scale;
            try p.append(a, .{
                .bounds = .{
                    .x = (ctx.width - w) / 2,
                    .y = (ctx.height - h) / 2,
                    .width = w,
                    .height = h,
                },
                .clip = .{ .width = ctx.width, .height = ctx.height },
                .kind = .{
                    .image = .{
                        .pixels = im.pixels,
                        .width = im.width,
                        .height = im.height,
                        .content_hash = im.hash,
                    },
                },
            });
        }
        if (self.snapshot == null) {
            const message = switch (self.background.state()) {
                .offline => "Feed unavailable. Check Wi-Fi or change the RSS address in settings.",
                .save_failed => "Could not save photographs. Check available storage.",
                else => "Loading photographs from RSS…",
            };
            try text(a, &p, .{ .x = 48, .y = 80, .width = ctx.width - 160, .height = 80 }, 24, 0xb5bfbeff, message);
        }
        try p.append(a, .{
            .bounds = .{ .y = ctx.height - 300, .width = ctx.width, .height = 300 },
            .kind = .{ .vertical_gradient = .{ .top = 0x090d1000, .bottom = 0x090d1080 } },
        });
        try text(a, &p, .{
            .x = 48,
            .y = ctx.height - 212,
            .width = ctx.width - 96,
            .height = 136,
        }, if (self.trusted) 112 else 58, 0xfaf9f1ff, self.time_text[0..self.time_len]);
        if (self.date_len > 0) try text(a, &p, .{
            .x = 55,
            .y = ctx.height - 78,
            .width = ctx.width - 100,
            .height = 44,
        }, 27, 0xe7e9e3ff, self.date_text[0..self.date_len]);
        if (self.caption_until > ctx.time_seconds) if (self.snapshot) |snapshot| {
            try rect(a, &p, .{ .height = 180, .width = ctx.width }, 0x10181beb);
            try text(a, &p, .{ .x = 40, .y = 28, .width = 1080, .height = 48 }, 27, 0xf9f4e9ff, snapshot.caption);
            const credit = if (snapshot.credit.len > 0) try std.fmt.allocPrint(a, "{s} · {s}", .{ snapshot.source, snapshot.credit }) else snapshot.source;
            try text(a, &p, .{ .x = 40, .y = 90, .width = 1100, .height = 32 }, 17, 0xd1d8d4ff, credit);
            try text(a, &p, .{ .x = 40, .y = 132, .width = 1100, .height = 28 }, 14, 0xa9b9b5ff, snapshot.source_url);
        };
        return ui.stack(
            a,
            .{ .height = .grow },
            &.{
                ui.canvas(.{ .height = .grow }, ctx.width, ctx.height, try p.toOwnedSlice(a)),
                try button(a, "···", 1, .{ .x = ctx.width - 104, .y = 24, .width = 80, .height = 64 }, true),
            },
        );
    }

    fn settingsView(self: *App, a: std.mem.Allocator, ctx: fascia.Context) !ui.Node {
        var nodes: std.ArrayList(ui.Node) = .empty;
        try nodes.append(a, try button(a, "Back", 2, .{ .x = 48, .y = 30, .width = 140, .height = 64 }, false));
        try nodes.append(a, label("Clock", 260, 40, 36, 0x242520ff));
        var p: std.ArrayList(t.Command) = .empty;
        try text(
            a,
            &p,
            .{ .x = 48, .y = 130, .width = 1184, .height = 28 },
            17,
            0x696b62ff,
            "Photo feed · RSS image enclosures",
        );
        try rect(a, &p, .{ .x = 48, .y = 172, .width = 1184, .height = 66 }, 0xe0dfd7ff);
        try text(
            a,
            &p,
            .{ .x = 64, .y = 188, .width = 1152, .height = 38 },
            21,
            0x242520ff,
            self.url[0..self.url_len],
        );
        try nodes.append(a, ui.canvas(.{ .height = .grow }, ctx.width, ctx.height, try p.toOwnedSlice(a)));
        try nodes.append(
            a,
            ui.interactive(
                ui.canvas(.{ .position = .{ .x = 48, .y = 172, .width = 1184, .height = 66 } }, 1184, 66, &.{}),
                30,
                3,
                false,
            ),
        );
        if (self.editor) |*editor| {
            try nodes.append(a, try editor.view(a, .{ .x = 0, .y = 420, .width = ctx.width, .height = ctx.height - 420 }, ctx.measure));
        } else {
            try nodes.append(a, label("Change photograph", 48, 274, 19, 0x696b62ff));
            const labels = [_][]const u8{ "10 minutes", "30 minutes", "1 hour", "2 hours" };
            const values = [_]u32{ 600, 1800, 3600, 7200 };
            for (labels, 0..) |name, i| try nodes.append(a, try button(a, name, @intCast(10 + i), .{
                .x = 48 + @as(f32, @floatFromInt(i)) * 302,
                .y = 318,
                .width = 278,
                .height = 70,
            }, self.interval == values[i]));
            try nodes.append(
                a,
                try button(
                    a,
                    if (self.twelve) "12-hour clock" else "24-hour clock",
                    4,
                    .{ .x = 48, .y = 432, .width = 560, .height = 84 },
                    false,
                ),
            );
            try nodes.append(
                a,
                try button(
                    a,
                    if (self.fill) "Fill the fascia" else "Fit whole photograph",
                    5,
                    .{ .x = 640, .y = 432, .width = 592, .height = 84 },
                    false,
                ),
            );
            const status = switch (self.background.state()) {
                .starting => "Opening the saved collection…",
                .cached => "Showing saved photographs.",
                .refreshing => "Checking the feed in the background…",
                .ready => "Photographs are available offline.",
                .offline => "Feed unavailable. Keeping the last good photographs.",
                .save_failed => "Could not confirm settings were saved.",
            };
            try nodes.append(a, label(status, 48, 575, 19, 0x696b62ff));
            try nodes.append(a, label("Tap a photograph for its caption. Swipe for another.", 48, 631, 19, 0x696b62ff));
            if (self.message.len > 0) try nodes.append(a, label(self.message, 48, 699, 18, 0x9f4b32ff));
        }
        return ui.stack(a, .{ .height = .grow, .background = 0xf3f1eaff }, try nodes.toOwnedSlice(a));
    }
};

fn rect(a: std.mem.Allocator, p: *std.ArrayList(t.Command), bounds: t.Rect, colour: u32) !void {
    try p.append(a, .{ .bounds = bounds, .kind = .{ .rectangle = .{ .colour = colour } } });
}

fn text(
    a: std.mem.Allocator,
    p: *std.ArrayList(t.Command),
    bounds: t.Rect,
    size: f32,
    colour: u32,
    content: []const u8,
) !void {
    var at: usize = 0;
    var width: f32 = 0;
    while (at < content.len) {
        const length = std.unicode.utf8ByteSequenceLength(content[at]) catch break;
        if (at + length > content.len) break;
        const advance = size * (if (length == 1) @as(f32, 0.65) else 1);
        if (width + advance > bounds.width - size) break;
        width += advance;
        at += length;
    }
    const shown = if (at < content.len) try std.fmt.allocPrint(a, "{s}…", .{content[0..at]}) else content;
    try p.append(a, .{
        .bounds = bounds,
        .clip = bounds,
        .kind = .{ .text = .{ .content = shown, .size = size, .colour = colour } },
    });
}

fn label(content: []const u8, x: f32, y: f32, size: f32, colour: u32) ui.Node {
    var node = ui.text(content, size, colour);
    node.style.position = .{ .x = x, .y = y, .width = 1232 - x, .height = size * 1.8 };
    return node;
}

fn button(a: std.mem.Allocator, title: []const u8, action: u32, bounds: t.Rect, selected: bool) !ui.Node {
    return ui.column(a, .{
        .position = bounds,
        .id = 100 + action,
        .action = action,
        .alignment = .center,
        .justify = .center,
        .background = if (selected) 0x242520e8 else 0xe0dfd7ff,
        .radius = 5,
    }, &.{ui.text(title, 22, if (selected) 0xffffffff else 0x242520ff)});
}

fn rootFile(path: [:0]const u8) bool {
    var info: c.struct_stat = undefined;
    return c.lstat(path, &info) == 0 and info.st_uid == 0 and info.st_mode & c.S_IFMT == c.S_IFREG and info.st_mode & 0o022 == 0;
}

fn timeTrusted(a: std.mem.Allocator) bool {
    if (builtin.os.tag != .linux) return true;
    if (rootFile("/run/systemd/timesync/synchronized")) return true;
    if (fascia_clock_synced() != 0) return true;
    if (!rootFile("/run/fascia/time-set")) return false;
    const bytes = fs.read(a, "/run/fascia/time-set", 2048) catch return false;
    defer a.free(bytes);
    const boot = fs.read(a, "/proc/sys/kernel/random/boot_id", 128) catch return false;
    defer a.free(boot);
    return logic.manualMarker(a, bytes, boot);
}

pub fn main() !void {
    const a = std.heap.c_allocator;
    const root = if (c.getenv("XDG_STATE_HOME")) |value| try a.dupe(u8, std.mem.span(value)) else if (c.getenv("HOME")) |home| try std.fmt.allocPrint(a, "{s}/.local/state/fascia-clock", .{std.mem.span(home)}) else return error.MissingStateDirectory;
    defer a.free(root);
    const settings = worker.loadSettings(a, root);
    defer if (settings.feed_url.ptr != feed.default_url.ptr) a.free(settings.feed_url);
    const background = try worker.Worker.init(a, root, settings);
    defer background.deinit();
    var app = App{
        .a = a,
        .background = background,
        .interval = settings.interval_seconds,
        .twelve = settings.twelve_hour,
        .fill = settings.fill,
    };
    app.url_len = settings.feed_url.len;
    @memcpy(app.url[0..app.url_len], settings.feed_url);
    defer if (app.snapshot) |snapshot| snapshot.destroy(a);
    try fascia.run(&app);
}
