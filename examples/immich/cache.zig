const std = @import("std");
const fs = @import("io.zig");
const model = @import("model.zig");
const c = fs.c;
const limit = 128 * 1024 * 1024;
const File = struct { path: []const u8, size: usize, modified: i64 };

/// Evict old app-owned files across selections before writing another preview.
pub fn reserve(a: std.mem.Allocator, root: []const u8, incoming: usize) !void {
    if (incoming > limit) return error.CacheFull;
    const zroot = try a.dupeZ(u8, root);
    const dir = c.opendir(zroot) orelse return error.CacheDirectory;
    defer _ = c.closedir(dir);
    var files: std.ArrayList(File) = .empty;
    var total: usize = 0;
    while (c.readdir(dir)) |entry| {
        const name = std.mem.span(@as([*:0]const u8, @ptrCast(&entry.*.d_name)));
        if (name.len == 0 or name.len > 16) continue;
        var valid = true;
        for (name) |ch| if (!std.ascii.isHex(ch)) {
            valid = false;
        };
        if (!valid) continue;
        const scope = try std.fmt.allocPrintSentinel(a, "{s}/{s}", .{ root, name }, 0);
        var info: c.struct_stat = undefined;
        if (c.lstat(scope, &info) != 0 or info.st_mode & c.S_IFMT != c.S_IFDIR) continue;
        const nested = c.opendir(scope) orelse continue;
        defer _ = c.closedir(nested);
        while (c.readdir(nested)) |item| {
            const leaf = std.mem.span(@as([*:0]const u8, @ptrCast(&item.*.d_name)));
            if (!std.mem.eql(u8, leaf, "catalog.json") and !(leaf.len >= 40 and model.validId(leaf[0..36]) and (std.mem.eql(u8, leaf[36..], ".jpg") or std.mem.eql(u8, leaf[36..], ".portrait.jpg") or std.mem.eql(u8, leaf[36..], ".faces.json")))) continue;
            const path = try std.fmt.allocPrintSentinel(a, "{s}/{s}", .{ scope, leaf }, 0);
            if (c.lstat(path, &info) != 0 or info.st_mode & c.S_IFMT != c.S_IFREG or info.st_size < 0) continue;
            const size: usize = @intCast(info.st_size);
            total += size;
            try files.append(a, .{ .path = path, .size = size, .modified = if (@import("builtin").os.tag == .macos) info.st_mtimespec.tv_sec else info.st_mtim.tv_sec });
        }
    }
    std.mem.sort(File, files.items, {}, struct {
        fn less(_: void, left: File, right: File) bool {
            return left.modified < right.modified;
        }
    }.less);
    for (files.items) |file| {
        if (total + incoming <= limit) return;
        const path = try a.dupeZ(u8, file.path);
        if (c.unlink(path) == 0) total -= file.size;
    }
    if (total + incoming > limit) return error.CacheFull;
}
