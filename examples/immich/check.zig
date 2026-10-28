const std = @import("std");
const fs = @import("io.zig");
const api = @import("api.zig");
const model = @import("model.zig");
const codec = @import("codec.zig");
const c = @cImport({
    @cInclude("stdlib.h");
    @cInclude("stdio.h");
});

pub fn main() !void {
    var arena = std.heap.ArenaAllocator.init(std.heap.c_allocator);
    defer arena.deinit();
    const a = arena.allocator();
    const root = if (c.getenv("XDG_CONFIG_HOME")) |value| std.mem.span(value) else return error.SetXdgConfigHome;
    const bytes = try fs.read(a, try std.fmt.allocPrint(a, "{s}/settings.json", .{root}), 32768);
    const settings = (try std.json.parseFromSlice(model.Settings, a, bytes, .{ .ignore_unknown_fields = true })).value;
    const client = api.Client{ .a = a, .settings = settings, .root = try std.fmt.allocPrint(a, "{s}/check", .{root}) };
    try client.version();
    const people = try client.people();
    const albums = try client.albums();
    const photos = try client.json([]const model.Asset, "/search/random", try api.query(a, settings));
    var matches: usize = 0;
    for (photos) |photo| if (model.accepts(settings, photo)) {
        matches += 1;
    };
    if (photos.len > 0) {
        const bytes_image = try client.request(try std.fmt.allocPrint(a, "/assets/{s}/thumbnail?size=preview", .{photos[0].id}), null, codec.limit);
        _ = try codec.decode(a, bytes_image);
    }
    const pair = model.pair(photos, settings, 0);
    _ = c.printf("People: %zu; albums: %zu; sample: %zu; matches: %zu; Then & now: %s\n", people.len, albums.len, photos.len, matches, @as([*:0]const u8, if (pair != null) "available" else "no pair"));
}
