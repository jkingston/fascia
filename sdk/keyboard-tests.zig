const std = @import("std");
const keyboard = @import("keyboard.zig");
const ui = keyboard.ui;
const t = keyboard.t;

fn measure(_: ?*anyopaque, text: []const u8, size: f32) t.Metrics {
    return .{
        .width = @as(f32, @floatFromInt(text.len)) * size * 0.5,
        .height = size,
    };
}

fn scene(a: std.mem.Allocator, model: *keyboard.Model) !ui.Scene {
    return ui.build(
        a,
        try model.view(a, .{ .x = 0, .y = 420, .width = 1280, .height = 380 }, .{ .function = measure }),
        .{ .width = 1280, .height = 800 },
        .{ .function = measure },
    );
}

fn hit(hits: []const ui.Hit, action: u32) ui.Hit {
    for (hits) |h| if (h.action == action) return h;
    unreachable;
}

fn tap(model: *keyboard.Model, hits: []const ui.Hit, action: u32, now: f64) ?keyboard.Result {
    const h = hit(hits, action);
    const x = h.bounds.x + h.bounds.width / 2;
    const y = h.bounds.y + h.bounds.height / 2;
    _ = model.update(.{ .x = x, .y = y, .pressed = true, .down = true }, hits, now);
    return model.update(.{ .x = x, .y = y, .released = true }, hits, now + 0.01);
}

test "UTF8 editor preserves cursor boundaries and rejects overflow atomically" {
    var buffer: [16]u8 = undefined;
    var model = try keyboard.Model.init(&buffer, "Aé🦋", .text);
    model.backspace();
    try std.testing.expectEqualStrings("Aé", model.text());
    model.moveLeft();
    try model.insert("日");
    try std.testing.expectEqualStrings("A日é", model.text());
    model.moveRight();
    model.backspace();
    try std.testing.expectEqualStrings("A日", model.text());
    const cursor = model.cursor;
    try std.testing.expectError(error.NoSpace, model.insert("01234567890123456789"));
    try std.testing.expectEqual(cursor, model.cursor);
    try std.testing.expectEqualStrings("A日", model.text());
    try std.testing.expectError(error.InvalidUtf8, model.insert(&.{0xff}));
    model.cursor = 2;
    try std.testing.expectError(error.InvalidUtf8, model.insert("x"));
}

test "secure mode paints only masks and cancel wipes scratch once" {
    var buffer: [64]u8 = undefined;
    var model = try keyboard.Model.init(&buffer, "FAKE-é-secret", .secure);
    var arena = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena.deinit();
    const built = try scene(arena.allocator(), &model);
    var mask = false;
    for (built.commands) |command| if (command.kind == .text) {
        const text = command.kind.text.content;
        try std.testing.expect(std.mem.indexOf(u8, text, "FAKE") == null);
        try std.testing.expect(std.mem.indexOf(u8, text, "secret") == null);
        if (std.mem.indexOf(u8, text, "•") != null) mask = true;
    };
    try std.testing.expect(mask);
    try std.testing.expectEqual(keyboard.Result.cancel, tap(&model, built.hits, @intFromEnum(keyboard.Action.cancel), 1).?);
    try std.testing.expectEqual(@as(usize, 0), model.len);
    for (buffer) |byte| try std.testing.expectEqual(@as(u8, 0), byte);
    try std.testing.expect(model.update(.{ .pressed = true, .down = true }, built.hits, 2) == null);
}

test "backspace repeat timing stops when leaving key and does not double on release" {
    var buffer: [64]u8 = undefined;
    var model = try keyboard.Model.init(&buffer, "abcdefghij", .text);
    var arena = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena.deinit();
    const built = try scene(arena.allocator(), &model);
    const h = hit(built.hits, @intFromEnum(keyboard.Action.backspace));
    const x = h.bounds.x + 20;
    const y = h.bounds.y + 20;
    _ = model.update(.{ .x = x, .y = y, .down = true, .pressed = true }, built.hits, 0);
    try std.testing.expectEqual(@as(usize, 9), model.len);
    _ = model.update(.{ .x = x, .y = y, .down = true }, built.hits, 0.44);
    try std.testing.expectEqual(@as(usize, 9), model.len);
    _ = model.update(.{ .x = x, .y = y, .down = true }, built.hits, 0.45);
    try std.testing.expectEqual(@as(usize, 8), model.len);
    _ = model.update(.{ .x = 0, .y = 0, .down = true }, built.hits, 1);
    try std.testing.expectEqual(@as(usize, 8), model.len);
    _ = model.update(.{ .x = x, .y = y, .released = true }, built.hits, 1.1);
    try std.testing.expectEqual(@as(usize, 8), model.len);
}

test "URL layout has unique large targets and submit happens only once" {
    var buffer: [128]u8 = undefined;
    var model = try keyboard.Model.init(&buffer, "", .url);
    var arena = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena.deinit();
    const built = try scene(arena.allocator(), &model);
    for (built.hits, 0..) |h, i| {
        try std.testing.expect(h.bounds.width >= 60 and h.bounds.height >= 60);
        for (built.hits[i + 1 ..]) |other| try std.testing.expect(h.id != other.id);
    }
    _ = tap(&model, built.hits, @intFromEnum(keyboard.Action.shift), 0);
    _ = tap(&model, built.hits, 'a', 1);
    try std.testing.expectEqualStrings("A", model.text());
    try std.testing.expect(!model.shift);
    _ = tap(&model, built.hits, '.', 2);
    _ = tap(&model, built.hits, '/', 3);
    _ = tap(&model, built.hits, ':', 4);
    try std.testing.expectEqualStrings("A./:", model.text());
    try std.testing.expectEqual(keyboard.Result.submit, tap(&model, built.hits, @intFromEnum(keyboard.Action.submit), 5).?);
    try std.testing.expect(model.update(.{}, built.hits, 6) == null);
    try std.testing.expectEqualStrings("A./:", model.text());
}

test "numbers symbols and caps pages remain editable across rebuilt hit regions" {
    var buffer: [64]u8 = undefined;
    var model = try keyboard.Model.init(&buffer, "", .text);
    var arena = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena.deinit();
    var built = try scene(arena.allocator(), &model);
    _ = tap(&model, built.hits, @intFromEnum(keyboard.Action.caps), 0);
    _ = tap(&model, built.hits, 'a', 1);
    try std.testing.expectEqualStrings("A", model.text());
    _ = tap(&model, built.hits, @intFromEnum(keyboard.Action.page), 2);
    built = try scene(arena.allocator(), &model);
    _ = tap(&model, built.hits, '1', 3);
    _ = tap(&model, built.hits, @intFromEnum(keyboard.Action.page), 4);
    built = try scene(arena.allocator(), &model);
    _ = tap(&model, built.hits, '[', 5);
    try std.testing.expectEqualStrings("A1[", model.text());
}
