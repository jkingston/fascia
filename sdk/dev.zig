//! Rebuild and restart desktop previews on source changes.
const std = @import("std");
const builtin = @import("builtin");
const c = @cImport({
    @cInclude("dirent.h");
    @cInclude("errno.h");
    @cInclude("sys/stat.h");
    @cInclude("sys/wait.h");
    @cInclude("signal.h");
    @cInclude("unistd.h");
    @cInclude("poll.h");
    @cInclude("time.h");
});
const a = std.heap.c_allocator;
var interrupted = std.atomic.Value(bool).init(false);

fn interrupt(_: c_int) callconv(.c) void {
    interrupted.store(true, .monotonic);
}

fn now() i64 {
    var time: c.timespec = undefined;
    _ = c.clock_gettime(c.CLOCK_MONOTONIC, &time);
    return @as(i64, time.tv_sec) * 1000 + @divTrunc(time.tv_nsec, 1_000_000);
}

fn ignored(name: []const u8) bool {
    return name.len == 0 or name[0] == '.' or
        std.mem.eql(u8, name, "zig-out") or std.mem.eql(u8, name, "zig-pkg") or
        std.mem.eql(u8, name, "node_modules") or std.mem.eql(u8, name, "__pycache__") or
        std.mem.endsWith(u8, name, "~") or std.mem.endsWith(u8, name, ".swp") or
        std.mem.endsWith(u8, name, ".tmp");
}

fn stamp(path: [:0]const u8, depth: usize) !u64 {
    if (depth > 64) return error.WatchDepth;
    var info: c.struct_stat = undefined;
    // Do not follow links into caches or back into another watched directory.
    if (c.lstat(path, &info) != 0) return 0;
    const kind = info.st_mode & c.S_IFMT;
    if (kind == c.S_IFLNK) return 0;
    var hash = std.hash.Wyhash.init(0);
    hash.update(path);
    if (kind == c.S_IFREG) {
        const modified = if (builtin.os.tag == .macos) info.st_mtimespec else info.st_mtim;
        hash.update(std.mem.asBytes(&modified.tv_sec));
        hash.update(std.mem.asBytes(&modified.tv_nsec));
        hash.update(std.mem.asBytes(&info.st_size));
        return hash.final();
    }
    if (kind != c.S_IFDIR) return 0;
    const directory = c.opendir(path) orelse return error.WatchDirectory;
    defer _ = c.closedir(directory);
    var result = hash.final();
    while (c.readdir(directory)) |entry| {
        const name = std.mem.sliceTo(entry.*.d_name[0..], 0);
        if (ignored(name)) continue;
        const child = try std.fmt.allocPrintSentinel(a, "{s}/{s}", .{ path, name }, 0);
        defer a.free(child);
        // XOR makes directory enumeration order irrelevant.
        result ^= try stamp(child, depth + 1);
    }
    return result;
}

fn fingerprint(paths: []const [:0]const u8) !u64 {
    var result: u64 = 0;
    for (paths) |path| result ^= try stamp(path, 0);
    return result;
}

fn spawn(io: std.Io, argv: []const []const u8, cwd: []const u8, env: ?*const std.process.Environ.Map) !c.pid_t {
    const child = try std.process.spawn(io, .{
        .argv = argv,
        .cwd = .{ .path = cwd },
        .environ_map = env,
        .pgid = 0,
        .stdin = .ignore,
        .stdout = .inherit,
        .stderr = .inherit,
    });
    return child.id.?;
}

fn finished(pid: c.pid_t) ?bool {
    var status: c_int = 0;
    if (c.waitpid(pid, &status, c.WNOHANG) <= 0) return null;
    return c.WIFEXITED(status) and c.WEXITSTATUS(status) == 0;
}

fn stop(pid: c.pid_t) void {
    _ = c.kill(-pid, c.SIGTERM);
    // Reap the leader last to prevent process-group ID reuse during cleanup.
    _ = c.poll(null, 0, 150);
    _ = c.kill(-pid, c.SIGKILL);
    var status: c_int = 0;
    while (c.waitpid(pid, &status, 0) < 0) {
        const number = if (builtin.os.tag == .macos) c.__error().* else c.__errno_location().*;
        if (number != c.EINTR) break;
    }
}

fn launch(io: std.Io, app: []const u8, prefix: []const u8, env: *const std.process.Environ.Map, args: []const []const u8) !c.pid_t {
    const directory_path = try a.dupeZ(u8, app);
    defer a.free(directory_path);
    const directory = c.opendir(directory_path) orelse return error.AppDirectory;
    defer _ = c.closedir(directory);
    var found: ?[]const u8 = null;
    defer if (found) |value| a.free(value);
    while (c.readdir(directory)) |entry| {
        const name = std.mem.sliceTo(entry.*.d_name[0..], 0);
        if (std.mem.endsWith(u8, name, ".desktop")) {
            if (found != null) return error.MultipleDesktopEntries;
            found = try a.dupe(u8, name[0 .. name.len - 8]);
        }
    }
    const id = found orelse return error.MissingDesktopEntry;
    if (id.len == 0 or id.len > 64 or id[0] == '-' or std.mem.eql(u8, id, "home")) return error.InvalidAppId;
    for (id) |char| if (!std.ascii.isLower(char) and !std.ascii.isDigit(char) and char != '-') return error.InvalidAppId;
    const binary = if (builtin.os.tag == .macos)
        try std.fmt.allocPrint(a, "{s}/preview/{s}.app/Contents/MacOS/{s}", .{ prefix, id, id })
    else
        try std.fmt.allocPrint(a, "{s}/preview/{s}", .{ prefix, id });
    defer a.free(binary);
    var argv: std.ArrayList([]const u8) = .empty;
    defer argv.deinit(a);
    try argv.append(a, binary);
    try argv.appendSlice(a, args);
    const pid = try spawn(io, argv.items, app, env);
    std.log.info("Preview started: {s} (pid {d})", .{ id, pid });
    return pid;
}

pub fn main(init: std.process.Init) !void {
    const args = try init.minimal.args.toSlice(a);
    defer a.free(args);
    if (args.len < 7) return error.MissingArguments;
    const compiler = args[1];
    const app = args[2];
    const prefix = args[3];
    const mode = args[4];
    const sdk = args[5];
    const sdk_build = args[6];
    const sdk_zon = try std.fmt.allocPrintSentinel(a, "{s}.zon", .{sdk_build}, 0);
    defer a.free(sdk_zon);
    const watch = [_][:0]const u8{ app, sdk, sdk_build, sdk_zon };
    var env = try init.minimal.environ.createMap(a);
    defer env.deinit();
    for ([_][]const u8{ "CONFIG", "DATA", "STATE", "CACHE" }, [_][]const u8{ "config", "data", "state", "cache" }) |kind, directory| {
        const key = try std.fmt.allocPrint(a, "XDG_{s}_HOME", .{kind});
        defer a.free(key);
        const value = try std.fs.path.join(a, &.{ app, ".local/preview", directory });
        defer a.free(value);
        try env.put(key, value);
        try std.Io.Dir.cwd().createDirPath(init.io, value);
    }
    try env.put("FASCIA_DISPLAY_GAINS", "1,1,1");
    _ = c.signal(c.SIGINT, interrupt);
    _ = c.signal(c.SIGTERM, interrupt);
    var build: ?c.pid_t = null;
    defer if (build) |pid| stop(pid);
    var preview: ?c.pid_t = null;
    defer if (preview) |pid| stop(pid);
    var observed = try fingerprint(&watch);
    var queued = true;
    var deadline = now();
    std.log.info("Watching app and SDK sources. Ctrl-C to stop.", .{});
    while (!interrupted.load(.monotonic)) {
        const current = try fingerprint(&watch);
        if (current != observed) {
            observed = current;
            queued = true;
            deadline = now() + 150;
        }
        if (preview) |pid| if (finished(pid) != null) {
            preview = null;
            std.log.info("Preview closed; watching for edits.", .{});
        };
        if (build) |pid| if (finished(pid)) |success| {
            build = null;
            if (success and !queued) {
                if (preview) |previous| stop(previous);
                preview = null;
                preview = launch(init.io, app, prefix, &env, args[7..]) catch |err| failed: {
                    std.log.err("Cannot start preview: {s}", .{@errorName(err)});
                    break :failed null;
                };
            } else if (!success) std.log.warn("Build failed; keeping the previous preview.", .{});
        };
        if (queued and build == null and now() >= deadline) {
            std.log.info("Building preview…", .{});
            build = try spawn(init.io, &.{ compiler, "build", "preview", mode, "--prefix", prefix }, app, null);
            queued = false;
        }
        _ = c.poll(null, 0, 100);
    }
}
