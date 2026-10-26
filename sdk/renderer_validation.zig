//! Independent pixel oracle: incremental retained rendering must equal a full replay.
const std = @import("std");
const r = @import("renderer.zig");
const t = @import("types.zig");
const font = std.fmt.comptimePrint("{s}", .{@import("test_assets").font_path});

test "convex polygon coverage has crisp fractional edges and no retained vertex pointers" {
    var renderer = try r.Renderer.init(std.testing.allocator, font);
    defer renderer.deinit();
    var oracle = try r.Renderer.init(std.testing.allocator, font);
    defer oracle.deinit();
    var buffers: [2][48 * 48]u32 = undefined;
    var expected: [48 * 48]u32 = undefined;
    for (0..10) |n| {
        const x: f32 = @floatFromInt(n);
        var points = [_]t.Point{
            .{ .x = x + 3.25, .y = 4.25 },
            .{ .x = x + 33.25, .y = 4.25 },
            .{ .x = x + 3.25, .y = 34.25 },
        };
        const scene = [_]t.Command{
            .{
                .bounds = .{ .width = 48, .height = 48 },
                .kind = .{ .rectangle = .{ .colour = 0x000000ff } },
            },
            .{
                .bounds = .{ .x = x + 3.25, .y = 4.25, .width = 30, .height = 30 },
                .kind = .{ .polygon = .{ .points = &points, .colour = 0xffffffff } },
            },
        };
        _ = try renderer.render(&buffers[n % 2], 48, 48, n % 2, &scene);
        oracle.dimensions[0] = .{ 0, 0 };
        _ = try oracle.render(&expected, 48, 48, 0, &scene);
        try std.testing.expectEqualSlices(u32, &expected, &buffers[n % 2]);
        try std.testing.expectEqual(@as(u32, 0xffffffff), buffers[n % 2][10 * 48 + n + 8]);
        const edge = buffers[n % 2][4 * 48 + n + 8] & 255;
        try std.testing.expect(edge > 170 and edge < 210);
        @memset(&points, .{ .x = -999, .y = -999 });
    }
}

test "native round lines match independent distance oracle at every orientation" {
    var renderer = try r.Renderer.init(std.testing.allocator, font);
    defer renderer.deinit();
    var pixels: [96 * 64]u32 = undefined;
    for (0..17) |angle| {
        const a: f32 = @as(f32, @floatFromInt(angle)) * 0.37;
        const ex: f32 = if (angle == 16) 9.7 else 40 + 46 * @cos(a);
        const ey: f32 = if (angle == 16) 11.3 else 30 + 34 * @sin(a);
        const width: f32 = if (angle % 2 == 0) 0.8 else 4.7;
        const dx = ex - 9.7;
        const dy = ey - 11.3;
        const clip = t.Rect{ .x = 3, .y = 4, .width = 85, .height = 55 };
        const commands = [_]t.Command{
            .{
                .bounds = .{ .width = 96, .height = 64 },
                .kind = .{ .rectangle = .{ .colour = 0x000000ff } },
            },
            .{
                .bounds = .{
                    .x = @min(ex, 9.7) - width / 2,
                    .y = @min(ey, 11.3) - width / 2,
                    .width = @abs(dx) + width,
                    .height = @abs(dy) + width,
                },
                .clip = clip,
                .kind = .{
                    .line = .{
                        .x1 = 9.7,
                        .y1 = 11.3,
                        .x2 = ex,
                        .y2 = ey,
                        .width = width,
                        .colour = 0xffffffbf,
                    },
                },
            },
        };
        _ = try renderer.render(&pixels, 96, 64, 0, &commands);
        for (0..64) |y| for (0..96) |x| {
            const px = @as(f32, @floatFromInt(x)) + 0.5 - 9.7;
            const py = @as(f32, @floatFromInt(y)) + 0.5 - 11.3;
            const ratio = if (dx * dx + dy * dy > 0.0001) std.math.clamp((px * dx + py * dy) / (dx * dx + dy * dy), 0, 1) else 0;
            const distance = @sqrt((px - ratio * dx) * (px - ratio * dx) + (py - ratio * dy) * (py - ratio * dy));
            const covered = x >= 3 and x < 88 and y >= 4 and y < 59;
            const alpha = if (covered) std.math.clamp(width / 2 + 0.5 - distance, 0, 1) * 191 else 0;
            const actual: f32 = @floatFromInt(pixels[y * 96 + x] & 255);
            try std.testing.expect(@abs(actual - alpha) <= 2);
        };
    }
}

test "line movement deletion and changing colour retain exact swap buffer parity" {
    var renderer = try r.Renderer.init(std.testing.allocator, font);
    defer renderer.deinit();
    var oracle = try r.Renderer.init(std.testing.allocator, font);
    defer oracle.deinit();
    var pixels: [2][80 * 64]u32 = undefined;
    var expected: [80 * 64]u32 = undefined;
    for (0..20) |n| {
        const x: f32 = @floatFromInt(n * 2);
        const scene = [_]t.Command{
            .{
                .bounds = .{ .width = 80, .height = 64 },
                .kind = .{ .rectangle = .{ .colour = 0x202830ff } },
            },
            .{
                .bounds = .{ .x = x - 3, .y = 7, .width = 35, .height = 49 },
                .kind = .{
                    .line = .{
                        .x1 = x,
                        .y1 = 10,
                        .x2 = x + 29,
                        .y2 = 53,
                        .width = 6,
                        .colour = if (n % 2 == 0) 0xddaa44aa else 0x4488ccff,
                    },
                },
            },
        };
        const commands = scene[0..if (n % 3 == 0) 1 else 2];
        _ = try renderer.render(&pixels[n % 2], 80, 64, n % 2, commands);
        oracle.dimensions[0] = .{ 0, 0 };
        _ = try oracle.render(&expected, 80, 64, 0, commands);
        try std.testing.expectEqualSlices(u32, &expected, &pixels[n % 2]);
    }
}

test "alternating buffers equal full replay across move delete resize alpha clip text image" {
    var retained = try r.Renderer.init(std.testing.allocator, font);
    defer retained.deinit();
    var oracle = try r.Renderer.init(std.testing.allocator, font);
    defer oracle.deinit();
    var buffers: [2][96 * 64]u32 = undefined;
    var expected: [96 * 64]u32 = undefined;
    var image = [_]u32{ 0xffff0000, 0xff00ff00, 0xff0000ff, 0xffffffff };
    for (0..32) |frame| {
        const step: f32 = @floatFromInt(frame);
        const width: u32 = if (frame < 24) 96 else 90;
        const height: u32 = if (frame < 24) 64 else 60;
        const len = @as(usize, width) * height;
        const slot = frame % 2;
        image[0] = if (frame % 3 == 0) 0xffaa5522 else 0xff2255aa;
        var commands = [_]t.Command{
            .{
                .id = 1,
                .bounds = .{ .width = @floatFromInt(width), .height = @floatFromInt(height) },
                .kind = .{ .rectangle = .{ .colour = 0x304050ff } },
            },
            .{
                .id = 2,
                .bounds = .{ .x = step * 0.7 - 3, .y = 5.3, .width = 24.4, .height = 22.8 },
                .clip = .{ .x = 3.2, .y = 8.7, .width = 31.3, .height = 33.3 },
                .kind = .{ .rectangle = .{ .colour = 0xaa44dd83, .radius = 5.8 } },
            },
            .{
                .id = 3,
                .bounds = .{ .x = 10 + step * 0.6, .y = 15.4, .width = 25, .height = 25 },
                .kind = .{
                    .circle = .{
                        .cx = 22.5 + step * 0.6,
                        .cy = 27.9,
                        .radius = 12,
                        .colour = 0x22bbddc0,
                        .opacity = 0.7,
                    },
                },
            },
            .{
                .id = 4,
                .bounds = .{
                    .x = 33.2 - step * 0.4,
                    .y = 18.3,
                    .width = 19.8 + @mod(step, 4),
                    .height = 17.6,
                },
                .clip = .{ .x = 20.4, .y = 15.7, .width = 50.1, .height = 28.8 },
                .kind = .{
                    .image = .{ .pixels = &image, .width = 2, .height = 2, .opacity = 0.51 },
                },
            },
            .{
                .id = 5,
                .bounds = .{ .x = 8.7 + step * 0.2, .y = 34.2, .width = 70, .height = 24 },
                .kind = .{
                    .text = .{
                        .content = if (frame % 3 == 0) "jÁgy" else "Voilà",
                        .size = 19,
                        .colour = 0xeeddccbb,
                    },
                },
            },
        };
        const scene = commands[0..if (frame % 5 == 0) 4 else 5];
        _ = try retained.render(buffers[slot][0..len], width, height, slot, scene);
        oracle.dimensions[0] = .{ 0, 0 };
        _ = try oracle.render(expected[0..len], width, height, 0, scene);
        std.testing.expectEqualSlices(u32, expected[0..len], buffers[slot][0..len]) catch |err| {
            std.debug.print("pixel oracle failed at frame {d}, slot {d}\n", .{ frame, slot });
            return err;
        };
        try std.testing.expectEqual(@as(usize, 0), (try retained.render(buffers[slot][0..len], width, height, slot, scene)).len);
    }
}

test "no opaque background insertion reordering immutable image and clipped offscreen paint" {
    var retained = try r.Renderer.init(std.testing.allocator, font);
    defer retained.deinit();
    var oracle = try r.Renderer.init(std.testing.allocator, font);
    defer oracle.deinit();
    var buffers: [2][80 * 50]u32 = undefined;
    var expected: [80 * 50]u32 = undefined;
    const image = [_]u32{ 0xffeeddcc, 0xff332211, 0xff88cc44, 0xff4466bb };
    for (0..24) |frame| {
        const step: f32 = @floatFromInt(frame);
        var commands = [_]t.Command{
            .{
                .id = 10,
                .bounds = .{ .width = 80, .height = 50 },
                .kind = .{
                    .rectangle = .{ .colour = if (frame % 3 == 0) 0x22446600 else 0x22446660 },
                },
            },
            .{
                .id = 11,
                .bounds = .{
                    .x = step * 0.3 - 9,
                    .y = step * 0.2 - 3,
                    .width = 27.4,
                    .height = 29.1,
                },
                .clip = .{ .x = 2.3, .y = 1.7, .width = 50.4, .height = 44.4 },
                .kind = .{
                    .image = .{
                        .pixels = &image,
                        .width = 2,
                        .height = 2,
                        .opacity = 0.42,
                        .content_hash = 12345,
                    },
                },
            },
            .{
                .id = 12,
                .bounds = .{ .x = 31.6 - step * 0.5, .y = 11.3, .width = 25, .height = 25 },
                .kind = .{
                    .circle = .{
                        .cx = 44.1 - step * 0.5,
                        .cy = 23.8,
                        .radius = 12,
                        .colour = 0x22bbddc0,
                        .opacity = 0.7,
                    },
                },
            },
            .{
                .id = 13,
                .bounds = .{ .x = 12.7 + step * 0.2, .y = 22.2, .width = 58, .height = 24 },
                .kind = .{ .text = .{ .content = "jÁg", .size = 20, .colour = 0xeeddcc77 } },
            },
        };
        if (frame % 4 == 0) std.mem.swap(t.Command, &commands[1], &commands[2]);
        const scene = commands[if (frame % 6 == 0) 1 else 0..if (frame % 5 == 0) 3 else 4];
        const slot = frame % 2;
        _ = try retained.render(&buffers[slot], 80, 50, slot, scene);
        oracle.dimensions[0] = .{ 0, 0 };
        _ = try oracle.render(&expected, 80, 50, 0, scene);
        try std.testing.expectEqualSlices(u32, &expected, &buffers[slot]);
    }
}

test "skipping unchanged submissions preserves alternating buffer repaint history" {
    var renderer = try r.Renderer.init(std.testing.allocator, font);
    defer renderer.deinit();
    var buffers: [2][16 * 16]u32 = undefined;
    var commands = [_]t.Command{
        .{
            .bounds = .{ .width = 16, .height = 16 },
            .kind = .{ .rectangle = .{ .colour = 0x304050ff } },
        },
        .{
            .bounds = .{ .x = 2, .y = 2, .width = 4, .height = 4 },
            .kind = .{ .rectangle = .{ .colour = 0xff000080 } },
        },
    };
    _ = try renderer.render(&buffers[0], 16, 16, 0, &commands);
    try std.testing.expect((try renderer.visible(0, 16, 16)).len != 0);
    _ = try renderer.render(&buffers[1], 16, 16, 1, &commands);
    try std.testing.expectEqual(@as(usize, 0), (try renderer.visible(1, 16, 16)).len);
    commands[1].bounds.x = 8;
    _ = try renderer.render(&buffers[1], 16, 16, 1, &commands);
    try std.testing.expect((try renderer.visible(1, 16, 16)).len != 0);
    // Buffer zero still contains the older scene, despite the skipped commit.
    _ = try renderer.render(&buffers[0], 16, 16, 0, &commands);
    try std.testing.expectEqualSlices(u32, &buffers[1], &buffers[0]);
    try std.testing.expectEqual(@as(usize, 0), (try renderer.visible(0, 16, 16)).len);
}
