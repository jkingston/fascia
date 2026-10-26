//! Touch keyboard. The caller owns the text buffer and handles submission.
const std = @import("std");
pub const ui = @import("layout.zig");
pub const t = @import("types.zig");

pub const Mode = enum { text, url, secure };

pub const Result = enum { submit, cancel };

pub const Page = enum { letters, numbers, symbols };

pub const Action = enum(u32) { backspace = 0x10000, shift, caps, page, letters, left, right, space, submit, cancel };
const action_base: u64 = 0x4b420000;

pub const Model = struct {
    buffer: []u8,
    len: usize = 0,
    cursor: usize = 0,
    mode: Mode,
    page: Page = .letters,
    shift: bool = false,
    caps: bool = false,
    closed: bool = false,
    full: bool = false,
    interaction: ui.Interaction = .{},
    erase_id: ?u64 = null,
    next_repeat: f64 = 0,

    pub fn init(buffer: []u8, initial: []const u8, mode: Mode) !Model {
        if (!std.unicode.utf8ValidateSlice(initial)) return error.InvalidUtf8;
        if (initial.len > buffer.len) return error.NoSpace;
        std.mem.copyForwards(u8, buffer[0..initial.len], initial);
        return .{
            .buffer = buffer,
            .len = initial.len,
            .cursor = initial.len,
            .mode = mode,
        };
    }

    pub fn text(self: *const Model) []const u8 {
        return self.buffer[0..self.len];
    }

    fn validCursor(self: *const Model) bool {
        return self.cursor <= self.len and (self.cursor == self.len or self.cursor == 0 or self.buffer[self.cursor] & 0xc0 != 0x80);
    }

    pub fn insert(self: *Model, bytes: []const u8) !void {
        if (self.closed) return error.Closed;
        if (!self.validCursor() or !std.unicode.utf8ValidateSlice(bytes)) return error.InvalidUtf8;
        if (bytes.len > self.buffer.len - self.len) {
            self.full = true;
            return error.NoSpace;
        }
        std.mem.copyBackwards(u8, self.buffer[self.cursor + bytes.len .. self.len + bytes.len], self.buffer[self.cursor..self.len]);
        @memcpy(self.buffer[self.cursor..][0..bytes.len], bytes);
        self.cursor += bytes.len;
        self.len += bytes.len;
        self.full = false;
    }

    pub fn backspace(self: *Model) void {
        if (self.closed or self.cursor == 0 or !self.validCursor()) return;
        const start = previous(self.buffer[0..self.len], self.cursor);
        std.mem.copyForwards(u8, self.buffer[start..], self.buffer[self.cursor..self.len]);
        const removed = self.cursor - start;
        self.len -= removed;
        self.cursor = start;
        std.crypto.secureZero(u8, self.buffer[self.len..][0..removed]);
        self.full = false;
    }

    pub fn moveLeft(self: *Model) void {
        if (!self.closed and self.validCursor()) self.cursor = previous(self.text(), self.cursor);
    }

    pub fn moveRight(self: *Model) void {
        if (!self.closed and self.validCursor() and self.cursor < self.len) self.cursor += std.unicode.utf8ByteSequenceLength(self.buffer[self.cursor]) catch 1;
    }

    fn finish(self: *Model, result: Result) Result {
        self.closed = true;
        self.erase_id = null;
        self.interaction = .{};
        if (result == .cancel and self.mode == .secure) {
            std.crypto.secureZero(u8, self.buffer);
            self.len = 0;
            self.cursor = 0;
        }
        return result;
    }

    pub fn update(self: *Model, input: t.Input, hits: []const ui.Hit, now_seconds: f64) ?Result {
        if (self.closed) return null;
        if (input.pressed) {
            self.erase_id = null;
            for (hits) |hit| if (hit.action == @intFromEnum(Action.backspace) and contains(hit.bounds, input.x, input.y)) {
                self.backspace();
                self.erase_id = hit.id;
                self.next_repeat = now_seconds + 0.45;
                break;
            };
        }
        if (self.erase_id) |id| {
            var active = false;
            for (hits) |hit| if (hit.id == id and hit.action == @intFromEnum(Action.backspace) and contains(hit.bounds, input.x, input.y)) {
                active = true;
                break;
            };
            if (!input.down or !active or !std.math.isFinite(now_seconds)) {
                self.erase_id = null;
            } else if (now_seconds >= self.next_repeat) {
                // Cap repeat catch-up after delayed frames.
                const count: usize = @intFromFloat(@min(16, @floor((now_seconds - self.next_repeat) / 0.07) + 1));
                for (0..count) |_| self.backspace();
                self.next_repeat = now_seconds + 0.07;
            }
        }
        const event = self.interaction.update(hits, input) orelse return null;
        if (event.id < action_base or event.id > action_base + 2000000) return null;
        if (event.action < 128) {
            var byte: [1]u8 = .{@intCast(event.action)};
            if (byte[0] >= 'a' and byte[0] <= 'z' and (self.caps != self.shift)) byte[0] = std.ascii.toUpper(byte[0]);
            self.insert(&byte) catch {};
            self.shift = false;
            return null;
        }
        if (event.action < @intFromEnum(Action.backspace) or event.action > @intFromEnum(Action.cancel)) return null;
        const action: Action = @enumFromInt(event.action);
        switch (action) {
            .backspace => {}, // Erases on press, never a second time on release.
            .shift => self.shift = !self.shift,
            .caps => self.caps = !self.caps,
            .page => self.page = switch (self.page) {
                .letters => .numbers,
                .numbers => .symbols,
                .symbols => .letters,
            },
            .letters => self.page = .letters,
            .left => self.moveLeft(),
            .right => self.moveRight(),
            .space => self.insert(" ") catch {},
            .submit => return self.finish(.submit),
            .cancel => return self.finish(.cancel),
        }
        return null;
    }

    pub fn view(self: *const Model, a: std.mem.Allocator, area: t.Rect, measure: t.Measure) !ui.Node {
        if (self.closed) return ui.stack(a, .{}, &.{});
        var nodes: std.ArrayList(ui.Node) = .empty;
        const row_h: f32 = @min(66, (area.height - 101) / 4);
        const gap: f32 = 7;
        const background = try a.alloc(t.Command, 1);
        background[0] = .{
            .bounds = .{ .width = area.width, .height = area.height },
            .kind = .{ .rectangle = .{ .colour = 0xe4e2d9ff } },
        };
        try nodes.append(a, ui.canvas(.{ .position = area }, area.width, area.height, background));
        const display = if (self.mode == .secure) try mask(a, self.text()) else try a.dupe(u8, self.text());
        const prefix = if (self.mode == .secure) try mask(a, self.text()[0..self.cursor]) else self.text()[0..self.cursor];
        const prefix_width = measure.text(prefix, 26).width;
        const offset = @max(0, prefix_width - (area.width - 72));
        const clip = t.Rect{
            .x = area.x + 18,
            .y = area.y + 8,
            .width = area.width - 36,
            .height = 58,
        };
        const editor = try a.alloc(t.Command, if (self.full) 4 else 3);
        editor[0] = .{
            .bounds = clip,
            .kind = .{ .rectangle = .{ .colour = 0xfaf9f2ff, .radius = 5 } },
        };
        editor[1] = .{
            .bounds = .{
                .x = clip.x + 12 - offset,
                .y = clip.y + 13,
                .width = @max(area.width, @as(f32, @floatFromInt(display.len)) * 26),
                .height = 40,
            },
            .clip = clip,
            .kind = .{ .text = .{ .content = display, .size = 26, .colour = 0x252820ff } },
        };
        editor[2] = .{
            .bounds = .{
                .x = clip.x + 12 + prefix_width - offset,
                .y = clip.y + 12,
                .width = 2,
                .height = 34,
            },
            .clip = clip,
            .kind = .{ .rectangle = .{ .colour = 0x9f4b32ff } },
        };
        if (self.full) editor[3] = .{
            .bounds = .{
                .x = area.x + 22,
                .y = area.y + 65,
                .width = area.width - 44,
                .height = 15,
            },
            .kind = .{
                .text = .{ .content = "Text limit reached", .size = 12, .colour = 0x9f4b32ff },
            },
        };
        try nodes.append(a, ui.canvas(.{}, 1280, 800, editor));
        const rows = switch (self.page) {
            .letters => [_][]const u8{ "qwertyuiop", "asdfghjkl", "zxcvbnm" },
            .numbers => [_][]const u8{ "1234567890", "@#$%&*()-", ".,!?_+=;" },
            .symbols => [_][]const u8{ "[]{}<>^~|", "\\`\"'@#%&", ".,!?_+=;" },
        };
        for (rows, 0..) |letters, row| {
            const extra: usize = if (row == 2) 2 else 0;
            const unit = (area.width - 32 - gap * @as(f32, @floatFromInt(letters.len + extra - 1))) / @as(f32, @floatFromInt(letters.len + extra));
            const y = area.y + 80 + @as(f32, @floatFromInt(row)) * (row_h + gap);
            var x = area.x + 16;
            if (row == 2) {
                try nodes.append(
                    a,
                    try key(
                        a,
                        .{ .x = x, .y = y, .width = unit, .height = row_h },
                        if (self.page == .letters) (if (self.shift) "SHIFT" else "Shift") else "ABC",
                        if (self.page == .letters) @intFromEnum(Action.shift) else @intFromEnum(Action.letters),
                        false,
                    ),
                );
                x += unit + gap;
            }
            for (letters) |letter| {
                const shown = if (self.page == .letters and self.caps != self.shift) std.ascii.toUpper(letter) else letter;
                const label = try a.dupe(u8, &.{shown});
                try nodes.append(a, try key(a, .{ .x = x, .y = y, .width = unit, .height = row_h }, label, letter, false));
                x += unit + gap;
            }
            if (row == 2) try nodes.append(
                a,
                try key(
                    a,
                    .{ .x = x, .y = y, .width = unit, .height = row_h },
                    "Erase",
                    @intFromEnum(Action.backspace),
                    false,
                ),
            );
        }
        const bottom = area.y + 80 + 3 * (row_h + gap);
        const labels = if (self.mode == .url) &[_][]const u8{ "123", "Caps", "←", "→", ".", "/", ":", "Space", "Cancel", "Done" } else &[_][]const u8{ "123", "Caps", "←", "→", "Space", "Cancel", "Done" };
        const actions = if (self.mode == .url) &[_]u32{
            @intFromEnum(Action.page),
            @intFromEnum(Action.caps),
            @intFromEnum(Action.left),
            @intFromEnum(Action.right),
            '.',
            '/',
            ':',
            @intFromEnum(Action.space),
            @intFromEnum(Action.cancel),
            @intFromEnum(Action.submit),
        } else &[_]u32{
            @intFromEnum(Action.page),
            @intFromEnum(Action.caps),
            @intFromEnum(Action.left),
            @intFromEnum(Action.right),
            @intFromEnum(Action.space),
            @intFromEnum(Action.cancel),
            @intFromEnum(Action.submit),
        };
        const weights = if (self.mode == .url) &[_]f32{ 1, 1, 0.8, 0.8, 0.8, 0.8, 0.8, 2, 1.4, 1.4 } else &[_]f32{ 1, 1, 0.8, 0.8, 4, 1.4, 1.4 };
        var total: f32 = 0;
        for (weights) |weight| total += weight;
        const base = (area.width - 32 - gap * @as(f32, @floatFromInt(labels.len - 1))) / total;
        var x = area.x + 16;
        for (labels, actions, weights) |label, action, weight| {
            const shown = if (action == @intFromEnum(Action.page)) switch (self.page) {
                .letters => "123",
                .numbers => "#+=",
                .symbols => "ABC",
            } else if (action == @intFromEnum(Action.caps) and self.caps) "CAPS" else label;
            try nodes.append(
                a,
                try key(
                    a,
                    .{ .x = x, .y = bottom, .width = base * weight, .height = row_h },
                    shown,
                    action,
                    action == @intFromEnum(Action.submit),
                ),
            );
            x += base * weight + gap;
        }
        return ui.stack(a, .{ .height = .grow }, try nodes.toOwnedSlice(a));
    }
};

fn key(a: std.mem.Allocator, bounds: t.Rect, label: []const u8, action: u32, primary: bool) !ui.Node {
    return ui.column(a, .{
        .position = bounds,
        .id = action_base + @as(u64, @intFromFloat(@max(0, bounds.y))) * 1280 + @as(u64, @intFromFloat(@max(0, bounds.x))),
        .action = action,
        .alignment = .center,
        .justify = .center,
        .background = if (primary) 0x292e25ff else 0xf6f5edff,
        .radius = 5,
    }, &.{
        ui.text(label, if (label.len > 4) 19 else 23, if (primary) 0xffffffff else 0x30352bff),
    });
}

fn contains(r: t.Rect, x: f32, y: f32) bool {
    return x >= r.x and y >= r.y and x < r.x + r.width and y < r.y + r.height;
}

fn previous(bytes: []const u8, position: usize) usize {
    if (position == 0) return 0;
    var at = @min(position, bytes.len) - 1;
    while (at > 0 and bytes[at] & 0xc0 == 0x80) : (at -= 1) {}
    return at;
}

fn codepoints(bytes: []const u8) usize {
    var n: usize = 0;
    for (bytes) |byte| if (byte & 0xc0 != 0x80) {
        n += 1;
    };
    return n;
}

fn mask(a: std.mem.Allocator, bytes: []const u8) ![]const u8 {
    const count = codepoints(bytes);
    const out = try a.alloc(u8, count * 3);
    for (0..count) |i| @memcpy(out[i * 3 ..][0..3], "•");
    return out;
}
