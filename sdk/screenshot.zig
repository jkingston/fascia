//! Lossless RGB PNG from the same logical pixels presented on the frame.
const std = @import("std");

fn integer(a: std.mem.Allocator, out: *std.ArrayList(u8), value: u32) !void {
    var bytes: [4]u8 = undefined;
    std.mem.writeInt(u32, &bytes, value, .big);
    try out.appendSlice(a, &bytes);
}

fn chunk(a: std.mem.Allocator, out: *std.ArrayList(u8), kind: *const [4]u8, data: []const u8) !void {
    try integer(a, out, @intCast(data.len));
    const start = out.items.len;
    try out.appendSlice(a, kind);
    try out.appendSlice(a, data);
    try integer(a, out, std.hash.Crc32.hash(out.items[start..]));
}

pub fn png(a: std.mem.Allocator, pixels: []const u32, width: u32, height: u32) ![]u8 {
    if (width == 0 or height == 0 or width > 4096 or height > 4096 or pixels.len != @as(usize, width) * height) return error.InvalidDimensions;
    var raw: std.ArrayList(u8) = .empty;
    defer raw.deinit(a);
    for (0..height) |y| {
        try raw.append(a, 0);
        for (pixels[y * width ..][0..width]) |pixel| {
            try raw.appendSlice(a, &.{ @truncate(pixel >> 16), @truncate(pixel >> 8), @truncate(pixel) });
        }
    }
    var zlib: std.ArrayList(u8) = .empty;
    defer zlib.deinit(a);
    try zlib.appendSlice(a, &.{ 0x78, 0x01 });
    var at: usize = 0;
    while (at < raw.items.len) {
        const count: u16 = @intCast(@min(65535, raw.items.len - at));
        try zlib.append(a, if (at + count == raw.items.len) 1 else 0);
        const inverse = ~count;
        try zlib.appendSlice(a, &.{ @truncate(count), @truncate(count >> 8), @truncate(inverse), @truncate(inverse >> 8) });
        try zlib.appendSlice(a, raw.items[at..][0..count]);
        at += count;
    }
    try integer(a, &zlib, std.hash.Adler32.hash(raw.items));
    var out: std.ArrayList(u8) = .empty;
    errdefer out.deinit(a);
    try out.appendSlice(a, "\x89PNG\r\n\x1a\n");
    var header: [13]u8 = @splat(0);
    std.mem.writeInt(u32, header[0..4], width, .big);
    std.mem.writeInt(u32, header[4..8], height, .big);
    header[8] = 8;
    header[9] = 2;
    try chunk(a, &out, "IHDR", &header);
    try chunk(a, &out, "IDAT", zlib.items);
    try chunk(a, &out, "IEND", "");
    return out.toOwnedSlice(a);
}

test "PNG signature, dimensions and invalid input" {
    const a = std.testing.allocator;
    const bytes = try png(a, &.{ 0xffff0000, 0xff00ff00, 0xff0000ff, 0xffffffff }, 2, 2);
    defer a.free(bytes);
    try std.testing.expectEqualSlices(u8, "\x89PNG\r\n\x1a\n", bytes[0..8]);
    try std.testing.expectEqual(@as(u32, 2), std.mem.readInt(u32, bytes[16..20], .big));
    try std.testing.expectError(error.InvalidDimensions, png(a, &.{0}, 0, 0));
}
