const std = @import("std");
const fascia = @import("fascia");
const ui = fascia.ui;
const t = fascia.types;
const model = @import("model.zig");
const worker = @import("worker.zig");
const fs = @import("io.zig");
const c = @cImport({
    @cInclude("stdlib.h");
});
const Page = enum { photos, settings, people, albums, display, connection };

pub const App = struct {
    arena: std.heap.ArenaAllocator,
    background: *worker.Worker,
    settings: model.Settings,
    snapshot: ?*worker.Snapshot = null,
    page: Page = .photos,
    page_index: usize = 0,
    paused: bool = false,
    pair_later: bool = false,
    pair_switch: f64 = 0,
    controls_until: f64 = 0,
    next_photo: f64 = 0,
    start_x: f32 = 0,
    start_y: f32 = 0,
    interaction: ui.Interaction = .{},
    editor: ?fascia.keyboard.Model = null,
    editing_key: bool = false,
    scratch: [2048]u8 = undefined,
    notice: []const u8 = "",
    dirty: bool = false,
    excluding: bool = false,

    pub fn wakeFd(self: *const App) ?i32 {
        return self.background.pipes[0];
    }

    pub fn nextWake(self: *const App, ctx: fascia.Context) ?f64 {
        if (self.editor) |editor| if (editor.erase_id != null) return ctx.time_seconds + 0.04;
        if (self.page != .photos) return null;
        var next: ?f64 = if (!self.paused) @max(self.next_photo, ctx.time_seconds + 0.1) else null;
        if (!self.paused and !self.pair_later and self.pair_switch > ctx.time_seconds) next = @min(next orelse self.pair_switch, self.pair_switch);
        if (self.controls_until > ctx.time_seconds) next = @min(next orelse self.controls_until, self.controls_until);
        return next;
    }

    fn apply(self: *App) bool {
        if (!self.settings.valid()) {
            self.notice = "Check the address and API key.";
            return false;
        }
        self.background.configure(self.settings) catch {
            self.notice = "Could not apply settings.";
            return false;
        };
        if (self.snapshot) |snapshot| snapshot.destroy();
        self.snapshot = null;
        self.notice = "";
        self.dirty = false;
        return true;
    }

    fn schedule(self: *App, now: f64) void {
        const interval: f64 = @floatFromInt(self.settings.interval);
        const paired = if (self.snapshot) |snapshot| snapshot.images[1] != null else false;
        self.pair_switch = if (paired and !self.pair_later) now + interval else 0;
        self.next_photo = now + interval * @as(f64, if (paired and !self.pair_later) 2 else 1);
    }

    fn toggle(self: *App, list: *[]const []const u8, id: []const u8) void {
        const a = self.arena.allocator();
        var values: std.ArrayList([]const u8) = .empty;
        const exists = model.contains(list.*, id);
        for (list.*) |value| if (!std.mem.eql(u8, value, id)) {
            values.append(a, value) catch return;
        };
        if (!exists and values.items.len < 32) values.append(a, a.dupe(u8, id) catch return) catch return;
        list.* = values.toOwnedSlice(a) catch return;
        self.dirty = true;
    }

    pub fn update(self: *App, input: t.Input, hits: []const ui.Hit, ctx: fascia.Context) void {
        if (self.background.take()) |snapshot| {
            if (self.snapshot) |old| old.destroy();
            self.snapshot = snapshot;
            self.pair_later = false;
            self.schedule(ctx.time_seconds);
        }
        if (self.next_photo == 0) self.next_photo = ctx.time_seconds + @as(f64, @floatFromInt(self.settings.interval));
        if (self.page == .photos and !self.paused and self.pair_switch > 0 and ctx.time_seconds >= self.pair_switch) self.pair_later = true;
        if (self.page == .photos and !self.paused and ctx.time_seconds >= self.next_photo) {
            self.background.next(1);
            self.next_photo = ctx.time_seconds + @as(f64, @floatFromInt(self.settings.interval));
        }
        if (self.editor) |*editor| {
            if (editor.update(input, hits, ctx.time_seconds)) |result| {
                if (result == .submit) {
                    const value = self.arena.allocator().dupe(u8, std.mem.trim(u8, editor.text(), " \r\n")) catch return;
                    if (self.editing_key) self.settings.api_key = value else self.settings.server = value;
                    self.dirty = true;
                }
                self.editor = null;
            }
            return;
        }
        if (self.interaction.update(hits, input)) |event| {
            switch (event.action) {
                1 => {
                    self.page = .settings;
                    self.page_index = 0;
                },
                2 => {
                    if (self.dirty and !self.apply()) return;
                    self.page = .photos;
                    self.paused = false;
                    self.controls_until = 0;
                    self.schedule(ctx.time_seconds);
                },
                3 => {
                    self.paused = false;
                    self.controls_until = 0;
                    self.schedule(ctx.time_seconds);
                },
                4 => {
                    self.background.next(1);
                    self.next_photo = ctx.time_seconds + @as(f64, @floatFromInt(self.settings.interval));
                },
                5 => self.background.next(-1),
                6, 7 => {
                    self.editing_key = event.action == 7;
                    self.editor = fascia.keyboard.Model.init(&self.scratch, if (self.editing_key) self.settings.api_key else self.settings.server, if (self.editing_key) .secure else .url) catch null;
                },
                8 => {
                    self.page = .people;
                    self.page_index = 0;
                },
                9 => {
                    self.page = .albums;
                    self.page_index = 0;
                },
                10 => {
                    self.settings.mode = if (self.settings.mode == .slideshow) .then_now else .slideshow;
                    self.dirty = true;
                },
                11 => {
                    self.settings.match = if (self.settings.match == .any) .all else .any;
                    self.dirty = true;
                },
                12 => {
                    self.settings.allow_others = !self.settings.allow_others;
                    self.dirty = true;
                },
                13 => {
                    self.settings.favourites = !self.settings.favourites;
                    self.dirty = true;
                },
                14 => {
                    self.settings.interval = switch (self.settings.interval) {
                        30 => 60,
                        60 => 300,
                        else => 30,
                    };
                    self.dirty = true;
                },
                15 => {
                    _ = self.apply();
                },
                18 => {
                    self.settings.framing = switch (self.settings.framing) {
                        .smart => .fit,
                        .fit => .fill,
                        .fill => .smart,
                    };
                    self.dirty = true;
                },
                16 => self.page_index -|= 1,
                17 => self.page_index += 1,
                20 => self.page = .display,
                21 => self.page = .connection,
                22 => {
                    self.excluding = !self.excluding;
                    self.page_index = 0;
                },
                23 => {
                    self.settings.albums = &.{};
                    self.dirty = true;
                },
                24 => {
                    if (self.excluding) self.settings.exclude = &.{} else self.settings.include = &.{};
                    self.dirty = true;
                },
                30, 31, 32 => {
                    self.settings.framing = switch (event.action) {
                        30 => .fit,
                        31 => .fill,
                        else => .smart,
                    };
                    self.dirty = true;
                },
                33, 34, 35 => {
                    self.settings.interval = switch (event.action) {
                        33 => 30,
                        34 => 60,
                        else => 300,
                    };
                    self.dirty = true;
                },
                40...45 => {
                    self.settings.interval = model.intervals[event.action - 40];
                    self.dirty = true;
                },
                36, 37 => {
                    self.settings.match = if (event.action == 36) .any else .all;
                    self.dirty = true;
                },
                100...199 => if (self.snapshot) |snapshot| {
                    const index = self.personIndex(snapshot.people, (event.action - 100) / 2) orelse return;
                    const id = snapshot.people[index].id;
                    if (!self.excluding) {
                        self.toggle(&self.settings.include, id);
                        if (model.contains(self.settings.exclude, id)) self.toggle(&self.settings.exclude, id);
                    } else {
                        self.toggle(&self.settings.exclude, id);
                        if (model.contains(self.settings.include, id)) self.toggle(&self.settings.include, id);
                    }
                },
                200...299 => if (self.snapshot) |snapshot| {
                    const index = self.page_index * 6 + event.action - 200;
                    if (index < snapshot.albums.len) self.toggle(&self.settings.albums, snapshot.albums[index].id);
                },
                else => {},
            }
            self.background.portraits(if (self.page == .people) self.page_index else null);
            return;
        }
        if (self.page != .photos) return;
        if (input.pressed) {
            self.start_x = input.x;
            self.start_y = input.y;
        }
        if (input.released) {
            const dx = input.x - self.start_x;
            if (@abs(input.y - self.start_y) > 100) return;
            self.paused = true;
            if (@abs(dx) > 90 or input.x < ctx.width * 0.25 or input.x > ctx.width * 0.75) {
                const direction: i32 = if (@abs(dx) > 90) (if (dx < 0) @as(i32, 1) else -1) else (if (input.x > ctx.width / 2) @as(i32, 1) else -1);
                self.background.next(direction);
            } else self.controls_until = if (self.controls_until > ctx.time_seconds) 0 else ctx.time_seconds + 30;
        }
    }

    fn personIndex(self: *const App, people: []const model.Person, row: usize) ?usize {
        var visible: usize = 0;
        for (people, 0..) |person, i| {
            if (person.name.len == 0 and !model.contains(self.settings.include, person.id) and !model.contains(self.settings.exclude, person.id)) continue;
            if (visible == self.page_index * 6 + row) return i;
            visible += 1;
        }
        return null;
    }

    pub fn view(self: *App, a: std.mem.Allocator, ctx: fascia.Context) !ui.Node {
        if (self.page != .photos) return self.settingsView(a, ctx);
        var commands: std.ArrayList(t.Command) = .empty;
        var nodes: std.ArrayList(ui.Node) = .empty;
        var has_photo = false;
        if (self.snapshot) |snapshot| {
            const paired = snapshot.images[1] != null;
            const slot: usize = if (paired and self.pair_later) 1 else 0;
            if (snapshot.images[slot]) |im| {
                has_photo = true;
                const area = t.Rect{ .width = ctx.width, .height = ctx.height };
                const placement = model.place(@floatFromInt(im.width), @floatFromInt(im.height), ctx.width, ctx.height, self.settings.framing, snapshot.faces[slot]);
                try commands.append(a, .{ .bounds = .{ .x = placement.x, .y = placement.y, .width = placement.width, .height = placement.height }, .clip = area, .kind = .{ .image = .{ .pixels = im.pixels, .width = im.width, .height = im.height, .content_hash = im.hash } } });
                if (paired) {
                    try commands.append(a, .{ .bounds = .{ .width = ctx.width, .height = 140 }, .kind = .{ .vertical_gradient = .{ .top = 0x101719c0, .bottom = 0x10171900 } } });
                    try nodes.append(a, label(try std.fmt.allocPrint(a, "{s}  ·  {s}  ·  {s}", .{ if (slot == 0) "Then" else "Now", snapshot.dates[slot], snapshot.person }), 36, 28, ctx.width - 72, 24, 0xfaf8f0ff));
                }
            }
            if (!has_photo) try nodes.append(a, label(snapshot.message, 80, 320, ctx.width - 160, 27, 0xf0eadcff));
        } else try nodes.append(a, label("Finding photographs…", 80, 320, ctx.width - 160, 28, 0xf0eadcff));
        try nodes.insert(a, 0, ui.canvas(.{ .height = .grow }, ctx.width, ctx.height, try commands.toOwnedSlice(a)));
        if (self.paused and has_photo) try nodes.append(a, label("Paused", 32, 32, 180, 20, 0xffffffff));
        if (!has_photo or self.controls_until > ctx.time_seconds) {
            try nodes.append(a, try button(a, "Resume", 3, .{ .x = 360, .y = 696, .width = 176, .height = 64 }, false));
            try nodes.append(a, try button(a, "Photos", 1, .{ .x = 552, .y = 696, .width = 176, .height = 64 }, false));
            try nodes.append(a, try button(a, "Display", 20, .{ .x = 744, .y = 696, .width = 176, .height = 64 }, false));
        }
        return ui.stack(a, .{ .height = .grow, .background = 0x171b1bff }, try nodes.toOwnedSlice(a));
    }

    fn settingsView(self: *App, a: std.mem.Allocator, ctx: fascia.Context) !ui.Node {
        var nodes: std.ArrayList(ui.Node) = .empty;
        const title = switch (self.page) {
            .people => if (self.excluding) "Leave out" else "People",
            .albums => "Albums",
            .display => "Display",
            .connection => "Connection",
            else => "Choose photos",
        };
        try nodes.append(a, label(title, 280, 32, 760, 30, ink));
        try nodes.append(a, try button(a, "Done", 2, .{ .x = 1100, .y = 24, .width = 132, .height = 60 }, true));
        try nodes.append(a, rule(0, 104, 1280, 1));
        try nodes.append(a, rule(240, 104, 1, 696));
        try nodes.append(a, try navigation(a, "Photos", 1, .{ .x = 24, .y = 136, .width = 192, .height = 64 }, self.page == .settings or self.page == .albums));
        try nodes.append(a, try navigation(a, "People", 8, .{ .x = 24, .y = 216, .width = 192, .height = 64 }, self.page == .people));
        try nodes.append(a, try navigation(a, "Display", 20, .{ .x = 24, .y = 296, .width = 192, .height = 64 }, self.page == .display));
        try nodes.append(a, try navigation(a, "Connection", 21, .{ .x = 24, .y = 704, .width = 192, .height = 56 }, self.page == .connection));

        switch (self.page) {
            .settings => {
                try nodes.append(a, label("Show photos from", 280, 148, 900, 22, muted));
                try nodes.append(a, try button(a, "All photos", 23, .{ .x = 280, .y = 196, .width = 460, .height = 76 }, self.settings.albums.len == 0));
                try nodes.append(a, try button(a, "Albums", 9, .{ .x = 756, .y = 196, .width = 476, .height = 76 }, self.settings.albums.len > 0));
                try nodes.append(a, rule(280, 316, 952, 1));
                try nodes.append(a, label("People", 280, 360, 280, 26, ink));
                try nodes.append(a, try button(a, if (self.settings.include.len == 0) "Anyone  ›" else try std.fmt.allocPrint(a, "{d} selected  ›", .{self.settings.include.len}), 8, .{ .x = 780, .y = 340, .width = 452, .height = 76 }, false));
                try nodes.append(a, rule(280, 444, 952, 1));
                try nodes.append(a, label("Favourites only", 280, 486, 620, 26, ink));
                try nodes.append(a, try button(a, if (self.settings.favourites) "On" else "Off", 13, .{ .x = 1080, .y = 472, .width = 152, .height = 64 }, self.settings.favourites));
                if (self.settings.exclude.len > 0) try nodes.append(a, label(try std.fmt.allocPrint(a, "{d} people excluded", .{self.settings.exclude.len}), 280, 580, 900, 20, muted));
            },
            .connection => {
                try nodes.append(a, label("Immich address", 280, 136, 900, 22, muted));
                try nodes.append(a, try button(a, if (self.settings.server.len == 0) "Enter address" else self.settings.server, 6, .{ .x = 280, .y = 180, .width = 952, .height = 72 }, false));
                try nodes.append(a, label("API key", 280, 280, 900, 22, muted));
                try nodes.append(a, try button(a, if (self.settings.api_key.len == 0) "Enter API key" else "••••••••  ·  Change", 7, .{ .x = 280, .y = 324, .width = 952, .height = 72 }, false));
            },
            .display => {
                try nodes.append(a, label("Photo framing", 280, 136, 900, 22, muted));
                for (0..2) |i| {
                    const x: f32 = 280 + @as(f32, @floatFromInt(i)) * 484;
                    const mode: model.Framing = if (i == 0) .fit else .fill;
                    if (self.snapshot) |snapshot| if (snapshot.images[0]) |im| {
                        const area = t.Rect{ .width = 400, .height = 250 };
                        const placement = model.place(@floatFromInt(im.width), @floatFromInt(im.height), 400, 250, mode, &.{});
                        try nodes.append(a, ui.canvas(.{ .position = .{ .x = x + 34, .y = 184, .width = 400, .height = 250 }, .background = 0x161616ff }, 400, 250, try a.dupe(t.Command, &.{.{ .bounds = .{ .x = placement.x, .y = placement.y, .width = placement.width, .height = placement.height }, .clip = area, .kind = .{ .image = .{ .pixels = im.pixels, .width = im.width, .height = im.height, .content_hash = im.hash } } }})));
                    };
                    try nodes.append(a, try button(a, if (i == 0) "Whole photo" else "Fill screen", @intCast(30 + i), .{ .x = x, .y = 450, .width = 468, .height = 60 }, self.settings.framing == mode));
                }
                try nodes.append(a, try button(a, "Face-aware fill", 32, .{ .x = 280, .y = 526, .width = 280, .height = 52 }, self.settings.framing == .smart));
                try nodes.append(a, label("Change photo every", 280, 602, 900, 22, muted));
                const durations = [_][]const u8{ "30 seconds", "1 minute", "5 minutes", "15 minutes", "30 minutes", "1 hour" };
                for (durations, 0..) |text, i| try nodes.append(a, try button(a, text, @intCast(40 + i), .{
                    .x = 280 + @as(f32, @floatFromInt(i % 3)) * 320,
                    .y = 642 + @as(f32, @floatFromInt(i / 3)) * 72,
                    .width = 304,
                    .height = 60,
                }, self.settings.interval == model.intervals[i]));
                try nodes.append(a, try button(a, if (self.settings.mode == .then_now) "Then & now: On" else "Then & now: Off", 10, .{ .x = 896, .y = 526, .width = 336, .height = 52 }, self.settings.mode == .then_now));
            },
            .people, .albums => {
                if (self.page == .people) {
                    try nodes.append(a, label(if (self.excluding) "Skip photos containing these people." else "Choose faces. No selection means anyone.", 280, 132, 900, 20, muted));
                    if (!self.excluding) {
                        try nodes.append(a, label("Show photos with", 928, 202, 304, 22, muted));
                        try nodes.append(a, try button(a, "Any", 36, .{ .x = 928, .y = 250, .width = 140, .height = 60 }, self.settings.match == .any));
                        try nodes.append(a, try button(a, "Together", 37, .{ .x = 1080, .y = 250, .width = 152, .height = 60 }, self.settings.match == .all));
                        try nodes.append(a, label("Other people allowed", 928, 370, 304, 22, ink));
                        try nodes.append(a, try button(a, if (self.settings.allow_others) "On" else "Off", 12, .{ .x = 928, .y = 420, .width = 140, .height = 60 }, self.settings.allow_others));
                    }
                    try nodes.append(a, try button(a, if (self.excluding) "Choose who to show" else "Leave someone out…", 22, .{ .x = 928, .y = 540, .width = 304, .height = 60 }, false));
                    try nodes.append(a, try button(a, "Clear selection", 24, .{ .x = 928, .y = 616, .width = 304, .height = 60 }, false));
                }
                if (self.snapshot) |snapshot| {
                    for (0..6) |row| {
                        if (self.page == .people) {
                            const index = self.personIndex(snapshot.people, row) orelse break;
                            const person = snapshot.people[index];
                            const x: f32 = 280 + @as(f32, @floatFromInt(row % 3)) * 208;
                            const y: f32 = 188 + @as(f32, @floatFromInt(row / 3)) * 244;
                            const chosen = model.contains(if (self.excluding) self.settings.exclude else self.settings.include, person.id);
                            var commands: std.ArrayList(t.Command) = .empty;
                            try commands.append(a, .{ .bounds = .{ .width = 192, .height = 184 }, .kind = .{ .rectangle = .{ .colour = 0xe9ecefff } } });
                            try commands.append(a, .{ .bounds = .{ .x = 20, .y = 76, .width = 160, .height = 28 }, .kind = .{ .text = .{ .content = if (snapshot.portrait_page == self.page_index) "No portrait" else "Loading…", .size = 18, .colour = muted } } });
                            if (snapshot.portrait_page == self.page_index) if (snapshot.portraits[row]) |im| {
                                const placement = model.place(@floatFromInt(im.width), @floatFromInt(im.height), 192, 184, .fill, &.{});
                                try commands.append(a, .{ .bounds = .{ .x = placement.x, .y = placement.y, .width = placement.width, .height = placement.height }, .clip = .{ .width = 192, .height = 184 }, .kind = .{ .image = .{ .pixels = im.pixels, .width = im.width, .height = im.height, .content_hash = im.hash } } });
                            };
                            try nodes.append(a, ui.canvas(.{ .position = .{ .x = x, .y = y, .width = 192, .height = 184 } }, 192, 184, try commands.toOwnedSlice(a)));
                            try nodes.append(a, try button(a, try std.fmt.allocPrint(a, "{s}{s}", .{ if (chosen) "✓ " else "", if (person.name.len > 0) person.name else "Unnamed" }), @intCast(100 + row * 2), .{ .x = x, .y = y + 184, .width = 192, .height = 48 }, chosen));
                            try nodes.append(a, try ui.column(a, .{ .position = .{ .x = x, .y = y, .width = 192, .height = 184 }, .id = @intCast(3000 + row), .action = @intCast(100 + row * 2) }, &.{}));
                        } else {
                            const index = self.page_index * 6 + row;
                            if (index >= snapshot.albums.len) break;
                            const album = snapshot.albums[index];
                            try nodes.append(a, try button(a, album.albumName, @intCast(200 + row), .{ .x = 280, .y = 148 + @as(f32, @floatFromInt(row)) * 84, .width = 952, .height = 68 }, model.contains(self.settings.albums, album.id)));
                        }
                    }
                    const more = if (self.page == .people) self.personIndex(snapshot.people, 6) != null else (self.page_index + 1) * 6 < snapshot.albums.len;
                    if (more) try nodes.append(a, try button(a, "Next page  ›", 17, .{ .x = 700, .y = 714, .width = 200, .height = 56 }, false));
                    if (self.page == .albums and snapshot.albums.len == 0) try nodes.append(a, label("No albums in this account.", 280, 180, 900, 24, muted));
                } else try nodes.append(a, label("Loading…", 280, 220, 600, 24, muted));
                if (self.page_index > 0) try nodes.append(a, try button(a, "‹  Previous page", 16, .{ .x = 280, .y = 714, .width = 220, .height = 56 }, false));
            },
            else => {},
        }
        if (self.notice.len > 0) try nodes.append(a, label(self.notice, 280, 756, 900, 18, 0xb53030ff));
        if (self.editor) |*editor| try nodes.append(a, try editor.view(a, .{ .x = 0, .y = 420, .width = ctx.width, .height = ctx.height - 420 }, ctx.measure));
        return ui.stack(a, .{ .height = .grow, .background = 0xfafafaff }, try nodes.toOwnedSlice(a));
    }
};

const ink: u32 = 0x24272cff;
const muted: u32 = 0x686e76ff;

fn rule(x: f32, y: f32, width: f32, height: f32) ui.Node {
    return .{ .style = .{ .position = .{ .x = x, .y = y, .width = width, .height = height }, .background = 0xe2e5e8ff } };
}

fn label(content: []const u8, x: f32, y: f32, width: f32, size: f32, colour: u32) ui.Node {
    var node = ui.text(content, size, colour);
    node.style.position = .{ .x = x, .y = y, .width = width, .height = size * 1.6 };
    return node;
}

fn button(a: std.mem.Allocator, title: []const u8, action: u32, bounds: t.Rect, selected: bool) !ui.Node {
    return ui.column(a, .{ .position = bounds, .id = 1000 + action, .action = action, .alignment = .center, .justify = .center, .background = if (selected) 0x087beaff else 0xf0f2f4ff, .radius = 5 }, &.{ui.text(title, 22, if (selected) 0xffffffff else ink)});
}

fn navigation(a: std.mem.Allocator, title: []const u8, action: u32, bounds: t.Rect, selected: bool) !ui.Node {
    return ui.column(a, .{ .position = bounds, .id = 1000 + action, .action = action, .alignment = .center, .justify = .center, .background = if (selected) 0xe3effcff else 0xfafafaff, .radius = 5 }, &.{ui.text(title, 22, if (selected) 0x076acbff else ink)});
}

pub fn main() !void {
    var arena = std.heap.ArenaAllocator.init(std.heap.c_allocator);
    const a = arena.allocator();
    const home = if (c.getenv("HOME")) |value| std.mem.span(value) else "/tmp";
    const config = if (c.getenv("XDG_CONFIG_HOME")) |value| std.mem.span(value) else try std.fmt.allocPrint(a, "{s}/.config/fascia-immich", .{home});
    const cache = if (c.getenv("XDG_CACHE_HOME")) |value| std.mem.span(value) else try std.fmt.allocPrint(a, "{s}/.cache/fascia-immich", .{home});
    const path = try std.fmt.allocPrint(a, "{s}/settings.json", .{config});
    const bytes = fs.read(a, path, 32 * 1024) catch "{}";
    const parsed = std.json.parseFromSlice(model.Settings, a, bytes, .{ .allocate = .alloc_always, .ignore_unknown_fields = true }) catch null;
    const settings = if (parsed) |p| (if (p.value.valid()) p.value else model.Settings{}) else model.Settings{};
    const background = try worker.Worker.init(config, cache, settings);
    defer background.deinit();
    var app = App{ .arena = arena, .background = background, .settings = settings, .page = if (settings.server.len == 0 or settings.api_key.len == 0) .connection else .photos };
    defer app.arena.deinit();
    defer if (app.snapshot) |snapshot| snapshot.destroy();
    try fascia.run(&app);
}
