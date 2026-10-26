//! Wayland signalfd masks belong to the host, never to launched applications.
const std = @import("std");
const c = @cImport({
    @cDefine("_GNU_SOURCE", "1");
    @cDefine("_POSIX_C_SOURCE", "200809L");
    @cInclude("spawn.h");
    @cInclude("signal.h");
    @cInclude("unistd.h");
    @cInclude("sys/wait.h");
    @cInclude("time.h");
});

extern "c" fn posix_spawn_file_actions_addchdir_np(*c.posix_spawn_file_actions_t, [*:0]const u8) c_int;

pub fn spawn(a: std.mem.Allocator, argv: []const []const u8, environment: *const std.process.Environ.Map, cwd: ?[]const u8) !std.process.Child {
    if (argv.len == 0 or !std.fs.path.isAbsolute(argv[0])) return error.InvalidExecutable;
    var arena = std.heap.ArenaAllocator.init(a);
    defer arena.deinit();
    const temp = arena.allocator();
    const arguments = try temp.allocSentinel(?[*:0]const u8, argv.len, null);
    for (argv, 0..) |arg, i| {
        if (std.mem.indexOfScalar(u8, arg, 0) != null) return error.InvalidArgument;
        arguments[i] = (try temp.dupeZ(u8, arg)).ptr;
    }
    const env = try environment.createPosixBlock(temp, .{});
    var attributes: c.posix_spawnattr_t = undefined;
    if (c.posix_spawnattr_init(&attributes) != 0) return error.SpawnAttributes;
    defer _ = c.posix_spawnattr_destroy(&attributes);
    var empty: c.sigset_t = undefined;
    if (c.sigemptyset(&empty) != 0 or c.posix_spawnattr_setsigmask(&attributes, &empty) != 0) return error.SpawnSignalMask;
    if (c.posix_spawnattr_setpgroup(&attributes, 0) != 0 or
        c.posix_spawnattr_setflags(&attributes, c.POSIX_SPAWN_SETSIGMASK | c.POSIX_SPAWN_SETPGROUP) != 0) return error.SpawnAttributes;
    var actions: c.posix_spawn_file_actions_t = undefined;
    if (c.posix_spawn_file_actions_init(&actions) != 0) return error.SpawnActions;
    defer _ = c.posix_spawn_file_actions_destroy(&actions);
    if (cwd) |directory| {
        if (!std.fs.path.isAbsolute(directory) or std.mem.indexOfScalar(u8, directory, 0) != null) return error.InvalidDirectory;
        if (posix_spawn_file_actions_addchdir_np(&actions, (try temp.dupeZ(u8, directory)).ptr) != 0) return error.SpawnDirectory;
    }
    var pid: c.pid_t = undefined;
    if (c.posix_spawn(&pid, arguments[0].?, &actions, &attributes, @ptrCast(arguments.ptr), @ptrCast(env.slice.ptr)) != 0) return error.ChildSpawn;
    return .{
        .id = pid,
        .thread_handle = {},
        .stdin = null,
        .stdout = null,
        .stderr = null,
        .request_resource_usage_statistics = false,
    };
}

test "spawn clears child mask while parent remains blocked and TERM exits normally" {
    var blocked: c.sigset_t = undefined;
    var previous: c.sigset_t = undefined;
    try std.testing.expectEqual(@as(c_int, 0), c.sigemptyset(&blocked));
    for ([_]c_int{ c.SIGTERM, c.SIGINT, c.SIGCHLD }) |signal| _ = c.sigaddset(&blocked, signal);
    try std.testing.expectEqual(@as(c_int, 0), c.sigprocmask(c.SIG_BLOCK, &blocked, &previous));
    defer _ = c.sigprocmask(c.SIG_SETMASK, &previous, null);
    var environment = std.process.Environ.Map.init(std.testing.allocator);
    defer environment.deinit();
    var child = try spawn(std.testing.allocator, &.{ "/bin/sleep", "30" }, &environment, "/");
    const pid = child.id.?;
    defer if (child.id != null) {
        _ = c.kill(pid, c.SIGKILL);
        _ = c.waitpid(pid, null, 0);
    };
    var current: c.sigset_t = undefined;
    try std.testing.expectEqual(@as(c_int, 0), c.sigprocmask(c.SIG_BLOCK, null, &current));
    for ([_]c_int{ c.SIGTERM, c.SIGINT, c.SIGCHLD }) |signal| try std.testing.expectEqual(@as(c_int, 1), c.sigismember(&current, signal));
    try std.testing.expectEqual(pid, c.getpgid(pid));
    try std.testing.expectEqual(@as(c_int, 0), c.kill(pid, c.SIGTERM));
    var status: c_int = 0;
    for (0..200) |_| {
        const result = c.waitpid(pid, &status, c.WNOHANG);
        if (result == pid) {
            child.id = null;
            try std.testing.expectEqual(@as(c_int, c.SIGTERM), status & 0x7f);
            return;
        }
        var pause: c.struct_timespec = .{ .tv_sec = 0, .tv_nsec = 5_000_000 };
        _ = c.nanosleep(&pause, null);
    }
    return error.TerminationTimedOut;
}

test "spawn preserves explicit environment and child cwd and reports failed exec" {
    var environment = std.process.Environ.Map.init(std.testing.allocator);
    defer environment.deinit();
    try environment.put("FASCIA_TEST", "child-env");
    const child = try spawn(std.testing.allocator, &.{ "/bin/sh", "-c", "test \"$FASCIA_TEST\" = child-env && test \"$(pwd)\" = /" }, &environment, "/");
    var status: c_int = 0;
    try std.testing.expectEqual(child.id.?, c.waitpid(child.id.?, &status, 0));
    try std.testing.expectEqual(@as(c_int, 0), status);
    try std.testing.expectError(error.ChildSpawn, spawn(std.testing.allocator, &.{"/fascia-missing-executable"}, &environment, null));
    try std.testing.expectError(error.ChildSpawn, spawn(std.testing.allocator, &.{"/bin/sleep"}, &environment, "/fascia-missing-directory"));
}
