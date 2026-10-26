const std = @import("std");
const embedded = @import("embedded.zig");
const ui = @import("fascia").ui;
const t = @import("fascia").types;
const raster = @import("fascia").Renderer;
const font = std.fmt.comptimePrint("{s}", .{@import("test_assets").font_path});

fn scene(paint: []const t.Command, action: u32) ui.Node {
    return ui.canvas(.{ .width = .grow, .height = .grow, .id = 19, .action = action }, 64, 48, paint);
}

fn commands(x: f32) [3]t.Command {
    return .{
        .{
            .bounds = .{ .width = 64, .height = 48 },
            .kind = .{ .rectangle = .{ .colour = 0x192b35ff } },
        },
        .{
            .bounds = .{ .x = x, .y = 7.25, .width = 17, .height = 17 },
            .kind = .{ .circle = .{ .cx = x + 8.5, .cy = 15.75, .radius = 8.5, .colour = 0xf3bb6490 } },
        },
        .{
            .bounds = .{ .x = 3, .y = 30, .width = 52, .height = 16 },
            .kind = .{ .text = .{ .content = "Home", .size = 13, .colour = 0xffffffff } },
        },
    };
}

test "embedded slots match full layout replay and cannot overwrite in-flight pixels" {
    var surface = try embedded.Surface.init(std.testing.allocator, font);
    defer surface.deinit();
    var oracle = try raster.init(std.testing.allocator, font);
    defer oracle.deinit();
    var arena = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena.deinit();
    var pixels: [2][64 * 48]u32 = undefined;
    var expected: [64 * 48]u32 = undefined;
    var active: ?embedded.Frame = null;
    defer if (active) |frame| {
        _ = surface.release(frame.slot, frame.serial);
    };
    for (0..12) |index| {
        const slot = index % 2;
        const paint = commands(@as(f32, @floatFromInt(index)) + 0.25);
        const node = scene(&paint, 7);
        const frame = (try surface.prepare(slot, &pixels[slot], 64, 48, node)).?;
        try std.testing.expect(frame.damage.len > 0);
        try std.testing.expectError(error.FramePending, surface.prepare(1 - slot, &pixels[1 - slot], 64, 48, node));
        try surface.accept(frame);
        if (active) |previous| try std.testing.expect(surface.release(previous.slot, previous.serial));
        active = frame;
        const saved = pixels[slot];
        try std.testing.expectError(error.BufferInFlight, surface.prepare(slot, &pixels[slot], 64, 48, node));
        try std.testing.expectEqualSlices(u32, &saved, &pixels[slot]);
        _ = arena.reset(.retain_capacity);
        const built = try ui.build(arena.allocator(), node, .{ .width = 64, .height = 48 }, oracle.measure());
        oracle.dimensions[0] = .{ 0, 0 };
        _ = try oracle.render(&expected, 64, 48, 0, built.commands);
        try std.testing.expectEqualSlices(u32, &expected, &pixels[slot]);
        try std.testing.expectEqual(@as(u32, 7), surface.hits()[0].action);
    }
}

test "unchanged visual scenes still refresh input and cancellation restores damage" {
    var surface = try embedded.Surface.init(std.testing.allocator, font);
    defer surface.deinit();
    var pixels: [2][64 * 48]u32 = undefined;
    const paint = commands(4);
    const first = (try surface.prepare(0, &pixels[0], 64, 48, scene(&paint, 7))).?;
    try surface.accept(first);
    try std.testing.expectEqual(@as(?embedded.Frame, null), try surface.prepare(1, &pixels[1], 64, 48, scene(&paint, 8)));
    try std.testing.expectEqual(@as(u32, 8), surface.hits()[0].action);
    const moved = commands(20);
    const cancelled = (try surface.prepare(1, &pixels[1], 64, 48, scene(&moved, 9))).?;
    try std.testing.expectEqual(@as(u32, 8), surface.hits()[0].action);
    try surface.cancel(cancelled);
    try std.testing.expectEqual(@as(u32, 8), surface.hits()[0].action);
    // Back to the submitted scene must repaint visibly after a failed attach.
    const retry = (try surface.prepare(1, &pixels[1], 64, 48, scene(&paint, 7))).?;
    try std.testing.expectEqual(@as(f32, 64), retry.damage[0].width);
    try surface.accept(retry);
    try std.testing.expect(!surface.release(1, cancelled.serial));
    try std.testing.expect(surface.release(0, first.serial));
    try std.testing.expect(surface.release(1, retry.serial));
    try std.testing.expect(!surface.release(1, retry.serial));
    var replacement: [64 * 48]u32 = @splat(0xdeadbeef);
    try std.testing.expectEqual(@as(?embedded.Frame, null), try surface.prepare(1, &replacement, 64, 48, scene(&paint, 7)));
    try std.testing.expectEqualSlices(u32, &pixels[1], &replacement);
    try std.testing.expectError(error.InvalidBuffer, surface.prepare(0, pixels[0][0..20], 64, 48, scene(&paint, 7)));
    try std.testing.expectError(error.InvalidSlot, surface.prepare(2, &pixels[0], 64, 48, scene(&paint, 7)));
}

test "touch setup screens render with secure entry and correctly bounded controls" {
    const setup = @import("home/setup.zig");
    const keyboard = @import("fascia").keyboard;
    const screenshot = @import("fascia").screenshot;
    const file = @cImport({
        @cInclude("stdio.h");
        @cInclude("stdlib.h");
    });
    const a = std.testing.allocator;
    var renderer = try raster.init(a, font);
    defer renderer.deinit();
    var model = setup.Model{ .active = true, .first_boot = true };
    defer model.deinit();
    model.status = try std.json.parseFromSlice(setup.Reply, a,
        \\{"ok":true,"hostname":"lounge","timezone":"Europe/London","stages":{"hostname":true},"mdns_service_active":true,"ssh_active":true,"key_count":1,"addresses":[{"interface":"wlan0","address":"192.168.1.42"}]}
    , .{ .allocate = .alloc_always });
    model.scan = try std.json.parseFromSlice(setup.Reply, a,
        \\{"ok":true,"networks":[{"name":"Studio","dbm":-42,"security":"psk","connected":true},{"name":"Guest network","dbm":-65,"security":"open"}]}
    , .{ .allocate = .alloc_always });
    @memcpy(model.ssid[0..6], "Studio");
    model.ssid_len = 6;
    const pixels = try a.alloc(u32, 1280 * 800);
    defer a.free(pixels);
    inline for (.{ .welcome, .name, .timezone, .wifi, .password, .ssh, .summary }) |page| {
        var arena = std.heap.ArenaAllocator.init(a);
        defer arena.deinit();
        model.page = page;
        const text = if (page == .name) "lounge" else if (page == .timezone) "Europe/London" else "";
        @memcpy(model.field[0..text.len], text);
        model.field_len = text.len;
        if (page == .password) {
            model.editing = .password;
            model.editor = try keyboard.Model.init(&model.scratch, "FAKE-password", .secure);
        } else model.editor = null;
        const node = try model.view(arena.allocator(), 1280, 800, renderer.measure());
        const built = try ui.build(arena.allocator(), node, .{ .width = 1280, .height = 800 }, renderer.measure());
        for (built.hits) |hit| {
            try std.testing.expect(hit.bounds.x >= 0 and hit.bounds.y >= 0);
            try std.testing.expect(hit.bounds.x + hit.bounds.width <= 1280 and hit.bounds.y + hit.bounds.height <= 800);
        }
        for (built.commands) |command| if (command.kind == .text) {
            try std.testing.expect(std.mem.indexOf(u8, command.kind.text.content, "FAKE-password") == null);
        };
        renderer.dimensions[0] = .{ 0, 0 };
        _ = try renderer.render(pixels, 1280, 800, 0, built.commands);
        if (file.getenv("FASCIA_SETUP_CAPTURE")) |directory| {
            const path = try std.fmt.allocPrintSentinel(a, "{s}/{s}.png", .{ std.mem.span(directory), @tagName(page) }, 0);
            defer a.free(path);
            const png = try screenshot.png(a, pixels, 1280, 800);
            defer a.free(png);
            const output = file.fopen(path, "wb") orelse return error.Capture;
            defer _ = file.fclose(output);
            try std.testing.expectEqual(png.len, file.fwrite(png.ptr, 1, png.len, output));
        }
    }
}
