test {
    _ = @import("feed.zig");
    _ = @import("codec.zig");
    _ = @import("cache.zig");
    _ = @import("logic.zig");
    _ = @import("worker.zig");
}

const std = @import("std");
const fs = @import("io.zig");
const cachemod = @import("cache.zig");
const feed = @import("feed.zig");
const worker = @import("worker.zig");
const c = @cImport({
    @cInclude("stdlib.h");
    @cInclude("unistd.h");
    @cInclude("poll.h");
});

test "atomic cache roundtrip evicts orphaned images and stays bounded" {
    var path: [64:0]u8 = @splat(0);
    const template = "/tmp/fascia-clock-cache-XXXXXX";
    @memcpy(path[0..template.len], template);
    const dir = c.mkdtemp(&path) orelse return error.Temp;
    const root = std.mem.span(dir);
    var arena = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena.deinit();
    const a = arena.allocator();
    var cache = cachemod.Cache.init(std.testing.allocator, root);
    defer cache.deinit();
    cache.feed_url = feed.default_url;
    const original = @embedFile("fixtures/png-as-webp.webp");
    const bytes = try a.alloc(u8, original.len + 4);
    @memcpy(bytes[0..original.len], original);
    for (0..14) |i| {
        std.mem.writeInt(u32, bytes[original.len..][0..4], @intCast(i), .little);
        const url = try std.fmt.allocPrint(a, "https://example.org/{d}.jpg", .{i});
        try cache.add(.{
            .url = url,
            .caption = "Actual archive photograph",
            .source = "Test archive",
            .source_url = "",
        }, bytes, url);
    }
    try std.testing.expectEqual(@as(usize, 12), cache.photos.items.len);
    var restored = cachemod.Cache.init(std.testing.allocator, root);
    defer restored.deinit();
    try restored.load();
    try std.testing.expectEqual(@as(usize, 12), restored.photos.items.len);
    for (restored.photos.items) |photo| fs.remove(a, try std.fmt.allocPrint(a, "{s}/{s}", .{ root, photo.file }));
    fs.remove(a, try std.fmt.allocPrint(a, "{s}/index.json", .{root}));
    try std.testing.expectEqual(@as(c_int, 0), c.rmdir(dir));
}

test "fresh offline worker publishes status without inventing a photograph" {
    var path: [64:0]u8 = @splat(0);
    const template = "/tmp/fascia-clock-worker-XXXXXX";
    @memcpy(path[0..template.len], template);
    const dir = c.mkdtemp(&path) orelse return error.Temp;
    const root = std.mem.span(dir);
    const background = try worker.Worker.initWithNetwork(std.testing.allocator, root, .{}, false);
    var descriptor = c.struct_pollfd{ .fd = background.pipes[0], .events = c.POLLIN, .revents = 0 };
    try std.testing.expect(c.poll(&descriptor, 1, 3000) > 0);
    try std.testing.expect(background.take() == null);
    try std.testing.expectEqual(worker.Status.offline, background.state());
    background.deinit();
    var child: [128]u8 = undefined;
    const cache_dir = try std.fmt.bufPrintSentinel(&child, "{s}/cache", .{root}, 0);
    _ = c.rmdir(cache_dir);
    try std.testing.expectEqual(@as(c_int, 0), c.rmdir(dir));
}

test "offline worker displays a previously downloaded RSS image" {
    var path: [64:0]u8 = @splat(0);
    const template = "/tmp/fascia-clock-saved-XXXXXX";
    @memcpy(path[0..template.len], template);
    const dir = c.mkdtemp(&path) orelse return error.Temp;
    const root = std.mem.span(dir);
    var arena = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena.deinit();
    const a = arena.allocator();
    const cache_root = try std.fmt.allocPrint(a, "{s}/cache", .{root});
    try fs.ensureDir(cache_root);
    var cache = cachemod.Cache.init(a, cache_root);
    defer cache.deinit();
    cache.feed_url = feed.default_url;
    const url = "https://example.org/rss-image.png";
    try cache.add(.{ .url = url, .caption = "Saved RSS image", .source = "Test feed", .source_url = "" }, @embedFile("fixtures/png-as-webp.webp"), url);
    const background = try worker.Worker.initWithNetwork(std.testing.allocator, root, .{}, false);
    defer background.deinit();
    var descriptor = c.struct_pollfd{ .fd = background.pipes[0], .events = c.POLLIN, .revents = 0 };
    try std.testing.expect(c.poll(&descriptor, 1, 3000) > 0);
    const snapshot = background.take() orelse return error.NoSnapshot;
    defer snapshot.destroy(std.testing.allocator);
    try std.testing.expectEqual(@as(usize, 1), snapshot.count);
    try std.testing.expectEqualStrings("Saved RSS image", snapshot.caption);
    try std.testing.expectEqual(@as(u32, 2), snapshot.image.width);
    for (cache.photos.items) |photo| fs.remove(a, try std.fmt.allocPrint(a, "{s}/{s}", .{ cache_root, photo.file }));
    fs.remove(a, try std.fmt.allocPrint(a, "{s}/index.json", .{cache_root}));
    const zcache = try a.dupeZ(u8, cache_root);
    try std.testing.expectEqual(@as(c_int, 0), c.rmdir(zcache));
    try std.testing.expectEqual(@as(c_int, 0), c.rmdir(dir));
}
