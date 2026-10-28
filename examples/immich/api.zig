const std = @import("std");
const fs = @import("io.zig");
const model = @import("model.zig");
const c = @cImport({
    @cInclude("unistd.h");
    @cInclude("sys/wait.h");
    @cInclude("signal.h");
    @cInclude("errno.h");
});
extern fn posix_spawn(*c_int, [*:0]const u8, ?*anyopaque, ?*anyopaque, [*:null]const ?[*:0]const u8, [*:null]const ?[*:0]u8) c_int;
extern var environ: [*:null]?[*:0]u8;

pub const Client = struct {
    a: std.mem.Allocator,
    settings: model.Settings,
    root: []const u8,
    generation: ?*std.atomic.Value(u32) = null,
    expected: u32 = 0,

    pub fn cancelled(self: Client) bool {
        return if (self.generation) |g| g.load(.acquire) != self.expected else false;
    }

    pub fn request(self: Client, endpoint: []const u8, body: ?[]const u8, limit: usize) ![]u8 {
        if (self.cancelled()) return error.Cancelled;
        if (!self.settings.valid() or self.settings.server.len == 0 or self.settings.api_key.len == 0) return error.NotConfigured;
        const a = self.a;
        try fs.ensureDir(self.root);
        const header_path = try std.fmt.allocPrint(a, "{s}/headers", .{self.root});
        const output_path = try std.fmt.allocPrint(a, "{s}/response", .{self.root});
        const input_path = try std.fmt.allocPrint(a, "{s}/request", .{self.root});
        defer fs.remove(a, header_path);
        defer fs.remove(a, output_path);
        defer fs.remove(a, input_path);
        try fs.atomic(a, header_path, try std.fmt.allocPrint(a, "x-api-key: {s}\nContent-Type: application/json\n", .{self.settings.api_key}));
        if (body) |data| try fs.atomic(a, input_path, data);
        const base = std.mem.trimEnd(u8, self.settings.server, "/");
        const url = try std.fmt.allocPrintSentinel(a, "{s}{s}{s}", .{ base, if (std.mem.endsWith(u8, base, "/api")) "" else "/api", endpoint }, 0);
        var args: std.ArrayList(?[*:0]const u8) = .empty;
        for ([_][]const u8{ "/usr/bin/curl", "--disable", "--silent", "--fail", "--proto", "=http,https", "--connect-timeout", "5", "--max-time", "20", "--max-filesize", try std.fmt.allocPrint(a, "{d}", .{limit}), "--header", try std.fmt.allocPrint(a, "@{s}", .{header_path}), "--output", output_path }) |arg| try args.append(a, (try a.dupeZ(u8, arg)).ptr);
        if (body != null) {
            try args.append(a, "--data-binary");
            try args.append(a, (try std.fmt.allocPrintSentinel(a, "@{s}", .{input_path}, 0)).ptr);
        }
        try args.append(a, "--");
        try args.append(a, url);
        try args.append(a, null);
        var pid: c.pid_t = undefined;
        if (posix_spawn(&pid, "/usr/bin/curl", null, null, @ptrCast(args.items.ptr), @ptrCast(environ)) != 0) return error.Spawn;
        var status: c_int = 0;
        while (true) {
            const waited = c.waitpid(pid, &status, c.WNOHANG);
            if (waited == pid) break;
            if (waited < 0) {
                const number = if (@import("builtin").os.tag == .macos) c.__error().* else c.__errno_location().*;
                if (number == c.EINTR) continue;
                return error.Wait;
            }
            if (self.cancelled()) {
                _ = c.kill(pid, c.SIGKILL);
                _ = c.waitpid(pid, &status, 0);
                return error.Cancelled;
            }
            _ = c.usleep(20_000);
        }
        if (status != 0) return error.ServerUnavailable;
        return fs.read(a, output_path, limit);
    }

    pub fn json(self: Client, comptime T: type, endpoint: []const u8, body: ?[]const u8) !T {
        const bytes = try self.request(endpoint, body, 8 * 1024 * 1024);
        return (try std.json.parseFromSlice(T, self.a, bytes, .{ .allocate = .alloc_always, .ignore_unknown_fields = true })).value;
    }

    pub fn version(self: Client) !void {
        const Version = struct { major: u32, minor: u32 };
        const v = try self.json(Version, "/server/version", null);
        if (v.major != 3 or v.minor < 2) return error.UnsupportedServer;
    }

    pub fn people(self: Client) ![]const model.Person {
        const Response = struct { people: []const model.Person, hasNextPage: bool = false };
        var result: std.ArrayList(model.Person) = .empty;
        for (1..11) |page| {
            const response = try self.json(Response, try std.fmt.allocPrint(self.a, "/people?size=1000&page={d}&withHidden=true", .{page}), null);
            try result.appendSlice(self.a, response.people);
            if (!response.hasNextPage) return result.toOwnedSlice(self.a);
        }
        return error.TooManyPeople;
    }

    pub fn albums(self: Client) ![]const model.Album {
        return self.json([]const model.Album, "/albums", null);
    }
};

pub fn query(a: std.mem.Allocator, settings: model.Settings) ![]const u8 {
    const Ids = struct { any: ?[]const []const u8 = null, all: ?[]const []const u8 = null, none: ?[]const []const u8 = null };
    const ids: ?Ids = if (settings.include.len == 0 and settings.exclude.len == 0) null else .{
        .any = if (settings.include.len > 0 and settings.match == .any) settings.include else null,
        .all = if (settings.include.len > 0 and settings.match == .all) settings.include else null,
        .none = if (settings.exclude.len > 0) settings.exclude else null,
    };
    return std.json.Stringify.valueAlloc(a, .{
        .size = 500,
        .withPeople = true,
        .withExif = false,
        .filter = .{
            .type = .{ .eq = "IMAGE" },
            .visibility = .{ .eq = "timeline" },
            .isOffline = .{ .eq = false },
            .personIds = ids,
            .albumIds = if (settings.albums.len == 0) @as(?Ids, null) else @as(?Ids, .{ .any = settings.albums }),
            .isFavorite = if (settings.favourites) @as(?struct { eq: bool }, .{ .eq = true }) else null,
        },
    }, .{ .emit_null_optional_fields = false });
}
