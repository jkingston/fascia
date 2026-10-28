const std = @import("std");
extern fn stbi_info_from_memory([*]const u8, c_int, *c_int, *c_int, *c_int) c_int;
extern fn stbi_load_from_memory([*]const u8, c_int, *c_int, *c_int, *c_int, c_int) ?[*]u8;
extern fn stbi_image_free(?*anyopaque) void;

pub const Image = struct {
    pixels: []u32,
    width: u32,
    height: u32,
    hash: u64,
};
pub const limit = 5 * 1024 * 1024;

pub fn decode(a: std.mem.Allocator, bytes: []const u8) !Image {
    if (bytes.len < 8 or bytes.len > limit) return error.InvalidImage;
    const png = std.mem.startsWith(u8, bytes, "\x89PNG\r\n\x1a\n");
    const jpeg = std.mem.startsWith(u8, bytes, "\xff\xd8\xff");
    if (!png and !jpeg) return error.UnsupportedImage;
    var w: c_int = 0;
    var h: c_int = 0;
    var channels: c_int = 0;
    if (stbi_info_from_memory(bytes.ptr, @intCast(bytes.len), &w, &h, &channels) == 0 or
        w < 1 or
        h < 1 or
        w > 4096 or
        h > 4096 or
        @as(i64, w) * h > 8 * 1024 * 1024) return error.Dimensions;
    const rgba = stbi_load_from_memory(bytes.ptr, @intCast(bytes.len), &w, &h, &channels, 4) orelse return error.InvalidImage;
    defer stbi_image_free(rgba);
    const cover = @max(1280 / @as(f32, @floatFromInt(w)), 800 / @as(f32, @floatFromInt(h)));
    const budget = @sqrt(@as(f32, 2 * 1024 * 1024) / (@as(f32, @floatFromInt(w)) * @as(f32, @floatFromInt(h))));
    const scale = @min(@as(f32, 1), @min(cover, budget));
    const width: u32 = @intFromFloat(@max(1, @round(@as(f32, @floatFromInt(w)) * scale)));
    const height: u32 = @intFromFloat(@max(1, @round(@as(f32, @floatFromInt(h)) * scale)));
    const pixels = try a.alloc(u32, @as(usize, width) * height);
    for (pixels, 0..) |*pixel, i| {
        const x = std.math.clamp(
            (@as(f32, @floatFromInt(i % width)) + 0.5) * @as(f32, @floatFromInt(w)) / @as(f32, @floatFromInt(width)) - 0.5,
            0,
            @as(f32, @floatFromInt(w - 1)),
        );
        const y = std.math.clamp(
            (@as(f32, @floatFromInt(i / width)) + 0.5) * @as(f32, @floatFromInt(h)) / @as(f32, @floatFromInt(height)) - 0.5,
            0,
            @as(f32, @floatFromInt(h - 1)),
        );
        const x0: usize = @intFromFloat(x);
        const y0: usize = @intFromFloat(y);
        const x1 = @min(x0 + 1, @as(usize, @intCast(w - 1)));
        const y1 = @min(y0 + 1, @as(usize, @intCast(h - 1)));
        const fx = x - @as(f32, @floatFromInt(x0));
        const fy = y - @as(f32, @floatFromInt(y0));
        var colour: u32 = 0xff000000;
        for (0..3) |channel| {
            const p00 = sample(rgba, @intCast(w), x0, y0, channel);
            const p10 = sample(rgba, @intCast(w), x1, y0, channel);
            const p01 = sample(rgba, @intCast(w), x0, y1, channel);
            const p11 = sample(rgba, @intCast(w), x1, y1, channel);
            const value: u32 = @intFromFloat(std.math.clamp(@round((p00 + (p10 - p00) * fx) * (1 - fy) + (p01 + (p11 - p01) * fx) * fy), 0, 255));
            colour |= value << @as(u5, @intCast(16 - channel * 8));
        }
        pixel.* = colour;
    }
    return .{
        .pixels = pixels,
        .width = width,
        .height = height,
        .hash = std.hash.Wyhash.hash(0, bytes) | 1,
    };
}

fn sample(rgba: [*]u8, width: usize, x: usize, y: usize, channel: usize) f32 {
    const at = (y * width + x) * 4;
    return @as(f32, @floatFromInt(rgba[at + channel])) * @as(f32, @floatFromInt(rgba[at + 3])) / 255;
}

test "actual JPEG signatures decode while fake WebP HTML and broken PNG do not" {
    const im = try decode(std.testing.allocator, @embedFile("fixtures/tiny.jpg"));
    defer std.testing.allocator.free(im.pixels);
    try std.testing.expect(im.width <= 1280 and im.height <= 800 and im.hash != 0);
    try std.testing.expectError(error.UnsupportedImage, decode(std.testing.allocator, "RIFF0000WEBP"));
    try std.testing.expectError(error.UnsupportedImage, decode(std.testing.allocator, "<html>not an image</html>"));
    try std.testing.expectError(error.Dimensions, decode(std.testing.allocator, "\x89PNG\r\n\x1a\n0000"));
}

test "PNG payload is decoded by signature even when its URL ends in webp" {
    const image = try decode(std.testing.allocator, @embedFile("fixtures/png-as-webp.webp"));
    defer std.testing.allocator.free(image.pixels);
    try std.testing.expectEqual(@as(u32, 2), image.width);
    try std.testing.expectEqual(@as(u32, 1), image.height);
    try std.testing.expectEqual(@as(u32, 0xffff0000), image.pixels[0]);
    try std.testing.expectEqual(@as(u32, 0xff00ff00), image.pixels[1]);
}
