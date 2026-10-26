//! Atomic replacement with file and directory fsync.
//! Failure after rename leaves durability uncertain; report an unconfirmed save.
const std = @import("std");
const builtin = @import("builtin");
const c = @cImport({
    @cInclude("stdlib.h");
    @cInclude("stdio.h");
    @cInclude("unistd.h");
    @cInclude("fcntl.h");
    @cInclude("errno.h");
    @cInclude("sys/stat.h");
    @cInclude("dirent.h");
});

fn errno() c_int {
    return if (builtin.os.tag == .macos) c.__error().* else c.__errno_location().*;
}

fn syncParent(path: [:0]const u8) !void {
    const separator = std.mem.lastIndexOfScalar(u8, path, '/');
    const parent = if (separator) |i| (if (i == 0) "/" else path[0..i]) else ".";
    var buffer: [4096]u8 = undefined;
    const name = try std.fmt.bufPrintSentinel(&buffer, "{s}", .{parent}, 0);
    const fd = c.open(name, c.O_RDONLY | c.O_DIRECTORY | c.O_CLOEXEC);
    if (fd < 0) return error.ParentOpen;
    defer _ = c.close(fd);
    if (c.fsync(fd) != 0) return error.ParentSync;
}

fn makeParents(path: [:0]const u8) !void {
    var buffer: [4096]u8 = undefined;
    if (path.len >= buffer.len) return error.PathTooLong;
    @memcpy(buffer[0..path.len], path);
    buffer[path.len] = 0;
    for (path, 0..) |ch, i| {
        if (ch != '/' or i == 0) continue;
        buffer[i] = 0;
        const prefix: [:0]const u8 = buffer[0..i :0];
        if (c.mkdir(prefix, 0o700) == 0) {
            try syncParent(prefix);
        } else {
            var status: c.struct_stat = undefined;
            if (c.stat(prefix, &status) != 0 or (status.st_mode & c.S_IFMT) != c.S_IFDIR) return error.ParentDirectory;
        }
        buffer[i] = '/';
    }
}

pub fn replace(path: [:0]const u8, bytes: []const u8) !void {
    return replaceParts(path, &.{bytes});
}

pub fn replaceParts(path: [:0]const u8, parts: []const []const u8) !void {
    if (std.mem.indexOfScalar(u8, path, 0) != null) return error.InvalidPath;
    try makeParents(path);
    var buffer: [4096]u8 = undefined;
    const temporary = try std.fmt.bufPrintSentinel(&buffer, "{s}.tmp.XXXXXX", .{path}, 0);
    const fd = c.mkstemp(temporary);
    if (fd < 0) return error.Create;
    defer _ = c.unlink(temporary);
    var closed = false;
    defer if (!closed) {
        _ = c.close(fd);
    };
    if (c.fchmod(fd, 0o600) != 0) return error.Permissions;
    for (parts) |bytes| {
        var offset: usize = 0;
        while (offset < bytes.len) {
            const count = c.write(fd, bytes.ptr + offset, bytes.len - offset);
            if (count < 0 and errno() == c.EINTR) continue;
            if (count <= 0) return error.Write;
            offset += @intCast(count);
        }
    }
    if (c.fsync(fd) != 0) return error.FileSync;
    const close_result = c.close(fd);
    closed = true;
    if (close_result != 0) return error.Close;
    if (c.rename(temporary, path) != 0) return error.Rename;
    try syncParent(path);
}

test "durable replacement preserves old until rename and removes temporary files" {
    var directory = "/tmp/fascia-persist-XXXXXX\x00".*;
    const root = c.mkdtemp(&directory) orelse return error.TestDirectory;
    defer _ = c.rmdir(root);
    var path_buffer: [512]u8 = undefined;
    const path = try std.fmt.bufPrintSentinel(&path_buffer, "{s}/state", .{root}, 0);
    defer _ = c.unlink(path);
    try replace(path, "old");
    try replaceParts(path, &.{ "new", " value" });
    const file = c.fopen(path, "rb") orelse return error.TestRead;
    defer _ = c.fclose(file);
    var bytes: [20]u8 = undefined;
    const size = c.fread(&bytes, 1, bytes.len, file);
    try std.testing.expectEqualStrings("new value", bytes[0..size]);
    var info: c.struct_stat = undefined;
    try std.testing.expectEqual(@as(c_int, 0), c.stat(path, &info));
    try std.testing.expectEqual(@as(@TypeOf(info.st_mode), 0o600), info.st_mode & 0o777);
    var blocked_buffer: [512]u8 = undefined;
    const blocked = try std.fmt.bufPrintSentinel(&blocked_buffer, "{s}/directory", .{root}, 0);
    try std.testing.expectEqual(@as(c_int, 0), c.mkdir(blocked, 0o700));
    defer _ = c.rmdir(blocked);
    try std.testing.expectError(error.Rename, replace(blocked, "cannot replace a directory"));
    const dir = c.opendir(root) orelse return error.TestReadDirectory;
    defer _ = c.closedir(dir);
    while (c.readdir(dir)) |entry| {
        const name = std.mem.span(@as([*:0]const u8, @ptrCast(&entry.*.d_name)));
        try std.testing.expect(std.mem.indexOf(u8, name, ".tmp.") == null);
    }
}
