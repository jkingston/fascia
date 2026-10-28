const std = @import("std");
pub const c = @cImport({
    @cInclude("stdio.h");
    @cInclude("stdlib.h");
    @cInclude("unistd.h");
    @cInclude("fcntl.h");
    @cInclude("sys/stat.h");
    @cInclude("dirent.h");
});

pub fn read(a: std.mem.Allocator, path: []const u8, maximum: usize) ![]u8 {
    const z = try a.dupeZ(u8, path);
    defer a.free(z);
    const f = c.fopen(z, "rb") orelse return error.Unavailable;
    defer _ = c.fclose(f);
    if (c.fseek(f, 0, c.SEEK_END) != 0) return error.Read;
    const size = c.ftell(f);
    if (size <= 0 or size > maximum) return error.Size;
    c.rewind(f);
    const buffer = try a.alloc(u8, @intCast(size));
    errdefer a.free(buffer);
    const n = c.fread(buffer.ptr, 1, buffer.len, f);
    if (n == 0 or n > maximum or c.ferror(f) != 0) return error.Size;
    return a.realloc(buffer, n);
}

pub fn ensureDir(path: []const u8) !void {
    if (path.len == 0 or path.len > 3000 or path[0] != '/') return error.InvalidPath;
    var buf: [4096]u8 = undefined;
    @memcpy(buf[0..path.len], path);
    buf[path.len] = 0;
    for (1..path.len + 1) |i| if (i == path.len or buf[i] == '/') {
        const original = buf[i];
        buf[i] = 0;
        _ = c.mkdir(@ptrCast(&buf), 0o700);
        buf[i] = original;
    };
    var info: c.struct_stat = undefined;
    if (c.lstat(@ptrCast(&buf), &info) != 0 or (info.st_mode & c.S_IFMT) != c.S_IFDIR) return error.Directory;
}

pub fn atomic(a: std.mem.Allocator, path: []const u8, bytes: []const u8) !void {
    const name = try a.dupeZ(u8, path);
    defer a.free(name);
    const temp = try std.fmt.allocPrintSentinel(a, "{s}.pending-XXXXXX", .{path}, 0);
    defer a.free(temp);
    const fd = c.mkstemp(temp);
    if (fd < 0) return error.Create;
    var opened = true;
    defer {
        if (opened) _ = c.close(fd);
        _ = c.unlink(temp);
    }
    var written: usize = 0;
    while (written < bytes.len) {
        const count = c.write(fd, bytes[written..].ptr, bytes.len - written);
        if (count <= 0) return error.Write;
        written += @intCast(count);
    }
    if (c.fsync(fd) != 0) return error.Sync;
    const closed = c.close(fd);
    opened = false;
    if (closed != 0) return error.Close;
    if (c.rename(temp, name) != 0) return error.Rename;
    const slash = std.mem.lastIndexOfScalar(u8, path, '/') orelse return;
    const parent = try a.dupeZ(u8, if (slash == 0) "/" else path[0..slash]);
    defer a.free(parent);
    const directory = c.open(parent, c.O_RDONLY | c.O_DIRECTORY | c.O_CLOEXEC);
    if (directory < 0) return error.CommitUncertain;
    defer _ = c.close(directory);
    if (c.fsync(directory) != 0) return error.CommitUncertain;
}

pub fn remove(a: std.mem.Allocator, path: []const u8) void {
    const z = a.dupeZ(u8, path) catch return;
    defer a.free(z);
    _ = c.unlink(z);
}
