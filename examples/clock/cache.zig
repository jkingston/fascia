const std = @import("std");
const fs = @import("io.zig");
const codec = @import("codec.zig");
const feed = @import("feed.zig");
pub const max_count = 12;
pub const max_bytes = 56 * 1024 * 1024; // Leaves space for feed + one bounded download below 64 MiB.

pub const Photo = struct {
    file: []const u8,
    bytes: usize,
    url: []const u8,
    caption: []const u8,
    source: []const u8,
    source_url: []const u8,
    credit: []const u8 = "",
};

const Wire = struct {
    schema: u8 = 1,
    feed_url: []const u8,
    photos: []const Photo,
};

pub const Cache = struct {
    arena: std.heap.ArenaAllocator,
    root: []const u8,
    photos: std.ArrayList(Photo) = .empty,
    feed_url: []const u8 = "",

    pub fn init(a: std.mem.Allocator, root: []const u8) Cache {
        return .{ .arena = std.heap.ArenaAllocator.init(a), .root = root };
    }

    pub fn deinit(self: *Cache) void {
        self.arena.deinit();
    }

    pub fn load(self: *Cache) !void {
        const a = self.arena.allocator();
        const path = try std.fmt.allocPrint(a, "{s}/index.json", .{self.root});
        const bytes = try fs.read(a, path, 128 * 1024);
        const parsed = try std.json.parseFromSlice(Wire, a, bytes, .{ .allocate = .alloc_always, .ignore_unknown_fields = true });
        try validate(parsed.value.photos);
        if (parsed.value.schema != 1 or !feed.validUrl(parsed.value.feed_url)) return error.InvalidCache;
        // A partial/corrupt cache never replaces its last-good in-memory list.
        for (parsed.value.photos) |photo| {
            const image_path = try std.fmt.allocPrint(a, "{s}/{s}", .{ self.root, photo.file });
            const z = try a.dupeZ(u8, image_path);
            var info: fs.c.struct_stat = undefined;
            if (fs.c.lstat(z, &info) != 0 or info.st_mode & fs.c.S_IFMT != fs.c.S_IFREG or info.st_size != photo.bytes) return error.InvalidCache;
        }
        self.feed_url = parsed.value.feed_url;
        try self.photos.appendSlice(a, parsed.value.photos);
    }

    pub fn add(self: *Cache, entry: feed.Entry, bytes: []const u8, url: []const u8) !void {
        const a = self.arena.allocator();
        const hash = std.hash.Wyhash.hash(0, bytes);
        const name = try std.fmt.allocPrint(a, "image-{x:0>16}.img", .{hash});
        for (self.photos.items) |photo| if (std.mem.eql(u8, photo.file, name)) return;
        const photo = Photo{
            .file = name,
            .bytes = bytes.len,
            .url = try a.dupe(u8, url),
            .caption = try a.dupe(u8, entry.caption),
            .source = try a.dupe(u8, entry.source),
            .source_url = try a.dupe(u8, entry.source_url),
            .credit = try a.dupe(u8, entry.credit),
        };
        var total = bytes.len;
        for (self.photos.items) |old| total += old.bytes;
        // Eviction changes only the next index until its atomic commit succeeds.
        var keep = self.photos.items.len;
        while (keep > 0 and (keep >= max_count or total > max_bytes)) {
            keep -= 1;
            total -= self.photos.items[keep].bytes;
        }
        if (total > max_bytes or bytes.len > codec.limit) return error.CacheFull;
        const path = try std.fmt.allocPrint(a, "{s}/{s}", .{ self.root, name });
        fs.atomic(a, path, bytes) catch |err| {
            if (err != error.CommitUncertain) return err;
        };
        const previous = self.photos.items;
        const next = try a.alloc(Photo, keep + 1);
        next[0] = photo;
        @memcpy(next[1..], previous[0..keep]);
        const encoded = try std.json.Stringify.valueAlloc(a, Wire{ .feed_url = self.feed_url, .photos = next }, .{});
        fs.atomic(a, try std.fmt.allocPrint(a, "{s}/index.json", .{self.root}), encoded) catch |err| {
            if (err != error.CommitUncertain) return err;
        };
        self.photos = .{ .items = next, .capacity = next.len };
        self.prune();
    }

    pub fn prune(self: *Cache) void {
        const a = self.arena.allocator();
        const path = a.dupeZ(u8, self.root) catch return;
        const directory = fs.c.opendir(path) orelse return;
        defer _ = fs.c.closedir(directory);
        while (fs.c.readdir(directory)) |ent| {
            const name = std.mem.sliceTo(ent.*.d_name[0..], 0);
            if (!std.mem.startsWith(u8, name, "image-") and !std.mem.eql(u8, name, ".download") and std.mem.indexOf(u8, name, ".pending-") == null) continue;
            var keep = false;
            for (self.photos.items) |photo| if (std.mem.eql(u8, photo.file, name)) {
                keep = true;
                break;
            };
            if (!keep) fs.remove(a, std.fmt.allocPrint(a, "{s}/{s}", .{ self.root, name }) catch continue);
        }
    }
};

pub fn validate(photos: []const Photo) !void {
    if (photos.len > max_count) return error.InvalidCache;
    var total: usize = 0;
    for (photos) |photo| {
        if (!std.mem.startsWith(u8, photo.file, "image-") or
            !std.mem.endsWith(u8, photo.file, ".img") or
            photo.file.len > 80 or
            std.mem.indexOfAny(u8, photo.file, "/\\\x00") != null or
            std.mem.indexOf(u8, photo.file, "..") != null or
            photo.bytes > codec.limit or
            photo.bytes == 0 or
            photo.caption.len > 400 or
            photo.credit.len > 240 or
            photo.source.len > 120 or
            photo.source_url.len > 2048 or
            !feed.validUrl(photo.url)) return error.InvalidCache;
        total += photo.bytes;
    }
    if (total > max_bytes) return error.InvalidCache;
}

test "offline index enforces count byte and path bounds" {
    const p = Photo{
        .file = "image-0123.img",
        .bytes = 1024,
        .url = "https://example.org/a",
        .caption = "Source photo",
        .source = "Archive",
        .source_url = "",
    };
    try validate(&.{p});
    var bad = p;
    bad.file = "image-../secret.img";
    try std.testing.expectError(error.InvalidCache, validate(&.{bad}));
    const many = [_]Photo{p} ** 13;
    try std.testing.expectError(error.InvalidCache, validate(&many));
}
