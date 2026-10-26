//! Installed-app registry. Resolves validated IDs to trusted executables.
const std = @import("std");
const c = @cImport({
    @cInclude("stdio.h");
    @cInclude("dirent.h");
    @cInclude("unistd.h");
    @cInclude("stdlib.h");
});

pub const Entry = struct {
    id: []const u8,
    name: []const u8,
    version: []const u8,
    description: []const u8,
    entrypoint: []const u8,
    release_path: []const u8,
    display: []const u8,
    builtin: bool = false,
};

pub const Registry = struct {
    arena: std.heap.ArenaAllocator,
    entries: []const Entry = &.{},
    default_id: ?[]const u8 = null,
    settings_valid: bool = false,
    startup_delay_seconds: u8 = 10,

    pub fn load(allocator: std.mem.Allocator, root: []const u8) !Registry {
        var result = Registry{ .arena = std.heap.ArenaAllocator.init(allocator) };
        errdefer result.deinit();
        const a = result.arena.allocator();
        const root_z = try a.dupeZ(u8, root);
        var root_buf: [4096]u8 = undefined;
        const canonical_root = if (c.realpath(root_z, &root_buf)) |r| try a.dupe(u8, std.mem.span(r)) else root;
        var entries: std.ArrayList(Entry) = .empty;
        if (c.opendir(root_z)) |directory| {
            defer _ = c.closedir(directory);
            while (c.readdir(directory)) |item| {
                if (entries.items.len == 128) break;
                const id = std.mem.sliceTo(item.*.d_name[0..], 0);
                if (!validId(id)) continue;
                const active = try std.fmt.allocPrintSentinel(a, "{s}/{s}/active", .{ root, id }, 0);
                var link: [128]u8 = undefined;
                const count = c.readlink(active, &link, link.len);
                if (count < 0 or count >= link.len) continue;
                const pointer = link[0..@intCast(count)];
                if (!validRelease(pointer)) continue;
                // Reject an on-disk pointer escaping via another symlink.
                const wanted = try std.fmt.allocPrintSentinel(a, "{s}/{s}/{s}", .{ root, id, pointer }, 0);
                var actual_buf: [4096]u8 = undefined;
                var expected_buf: [4096]u8 = undefined;
                const actual = c.realpath(active, &actual_buf) orelse continue;
                const expected = c.realpath(wanted, &expected_buf) orelse continue;
                if (!std.mem.eql(u8, std.mem.span(actual), std.mem.span(expected))) continue;
                // Canonical release must remain inside the app's releases directory.
                const releases_z = try std.fmt.allocPrintSentinel(a, "{s}/{s}/releases", .{ root, id }, 0);
                var releases_buf: [4096]u8 = undefined;
                const releases = c.realpath(releases_z, &releases_buf) orelse continue;
                const expected_releases = try std.fmt.allocPrint(a, "{s}/{s}/releases", .{ canonical_root, id });
                if (!std.mem.eql(u8, std.mem.span(releases), expected_releases)) continue;
                const prefix = try std.fmt.allocPrint(a, "{s}/", .{std.mem.span(releases)});
                if (!std.mem.startsWith(u8, std.mem.span(actual), prefix)) continue;
                const release = try a.dupe(u8, std.mem.span(actual));
                const file = try std.fmt.allocPrintSentinel(a, "{s}/.fascia.json", .{release}, 0);
                const raw = (try read(a, file)) orelse continue;
                const entry = (try parseEntry(a, raw, id, release)) orelse continue;
                const run_z = try a.dupeZ(u8, entry.entrypoint);
                if (c.access(run_z, c.X_OK) != 0) continue;
                try entries.append(a, entry);
            }
        }
        std.mem.sort(Entry, entries.items, {}, struct {
            fn less(_: void, x: Entry, y: Entry) bool {
                return std.mem.order(u8, x.name, y.name) == .lt;
            }
        }.less);
        result.entries = try entries.toOwnedSlice(a);
        const parent = std.fs.path.dirname(std.mem.trimEnd(u8, root, "/")) orelse ".";
        const settings = try std.fmt.allocPrintSentinel(a, "{s}/settings.json", .{parent}, 0);
        if (try read(a, settings)) |raw| {
            const parsed = std.json.parseFromSlice(std.json.Value, a, raw, .{ .allocate = .alloc_always }) catch return result;
            const value = parsed.value;
            const schema = field(value, "schema_version") orelse return result;
            if (schema != .integer or schema.integer != 1) return result;
            const delay = field(value, "startup_delay_seconds") orelse return result;
            if (delay != .integer or delay.integer < 5 or delay.integer > 60) return result;
            const default = field(value, "default_app") orelse return result;
            if (default != .null and default != .string) return result;
            result.startup_delay_seconds = @intCast(delay.integer);
            if (default == .string) {
                if (!validId(default.string)) return result;
                for (result.entries) |entry| {
                    if (std.mem.eql(u8, entry.id, default.string)) {
                        result.default_id = entry.id;
                        break;
                    }
                }
                if (result.default_id == null) return result;
            }
            result.settings_valid = true;
        }
        return result;
    }

    pub fn deinit(self: *Registry) void {
        self.arena.deinit();
        self.* = undefined;
    }
};

pub fn validId(id: []const u8) bool {
    if (id.len == 0 or id.len > 64 or std.mem.eql(u8, id, "home")) return false;
    for (id, 0..) |ch, i| if (!(ch >= 'a' and ch <= 'z') and !(ch >= '0' and ch <= '9') and !(i > 0 and ch == '-')) return false;
    return true;
}

fn validRelease(pointer: []const u8) bool {
    if (pointer.len != 33 or !std.mem.startsWith(u8, pointer, "releases/")) return false;
    for (pointer[9..]) |ch| if (!(ch >= '0' and ch <= '9') and !(ch >= 'a' and ch <= 'f')) return false;
    return true;
}

fn read(a: std.mem.Allocator, path: [:0]const u8) !?[]const u8 {
    const file = c.fopen(path, "rb") orelse return null;
    defer _ = c.fclose(file);
    const buf = try a.alloc(u8, 8193);
    const n = c.fread(buf.ptr, 1, buf.len, file);
    if (n > 8192 or c.ferror(file) != 0) return null;
    return buf[0..n];
}

fn field(value: std.json.Value, key: []const u8) ?std.json.Value {
    return if (value == .object) value.object.get(key) else null;
}

fn text(value: std.json.Value, key: []const u8, limit: usize) ?[]const u8 {
    const v = field(value, key) orelse return null;
    if (v != .string or v.string.len == 0 or v.string.len > limit) return null;
    for (v.string) |ch| if (ch < 32) return null;
    return v.string;
}

fn parseEntry(a: std.mem.Allocator, raw: []const u8, id: []const u8, release: []const u8) !?Entry {
    const parsed = std.json.parseFromSlice(std.json.Value, a, raw, .{ .allocate = .alloc_always }) catch return null;
    const v = parsed.value;
    const schema = field(v, "schema_version") orelse return null;
    if (schema != .integer or schema.integer != 1) return null;
    const mid = text(v, "id", 64) orelse return null;
    if (!validId(mid) or !std.mem.eql(u8, id, mid)) return null;
    const entry = text(v, "entrypoint", 64) orelse return null;
    const arch = text(v, "architecture", 64) orelse return null;
    const display = text(v, "display", 8) orelse return null;
    if (!std.mem.eql(u8, entry, "AppRun") or
        !std.mem.eql(u8, arch, "arm-linux-gnueabihf") or
        !(std.mem.eql(u8, display, "cpu") or
            std.mem.eql(u8, display, "gpu"))) return null;
    if (field(v, "min_glibc")) |floor| {
        if (floor != .string or !std.mem.eql(u8, floor.string, "2.43")) return null;
    }
    const builtin: std.json.Value = field(v, "builtin") orelse .{ .bool = false };
    if (builtin != .bool) return null;
    return .{
        .id = mid,
        .name = text(v, "name", 80) orelse return null,
        .version = text(v, "version", 80) orelse return null,
        .description = text(v, "description", 240) orelse return null,
        .entrypoint = try std.fmt.allocPrint(a, "{s}/AppRun", .{release}),
        .release_path = release,
        .display = display,
        .builtin = builtin.bool,
    };
}

test "registry rejects traversal and invalid release pointers" {
    try std.testing.expect(validId("counter"));
    for ([_][]const u8{ "", "../clock", "Clock", "home", "-clock", "clock/x" }) |id| try std.testing.expect(!validId(id));
    try std.testing.expect(validRelease("releases/0123456789abcdef01234567"));
    try std.testing.expect(!validRelease("releases/../../elsewhere"));
}

test "registry tolerates a missing store" {
    var registry = try Registry.load(std.testing.allocator, "/this-fascia-registry-does-not-exist/apps");
    defer registry.deinit();
    try std.testing.expectEqual(@as(usize, 0), registry.entries.len);
    try std.testing.expect(registry.default_id == null);
}

test "manifest parsing matches installer schema and rejects mismatched ids" {
    var arena = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena.deinit();
    const raw = "{\"schema_version\":1,\"id\":\"hello\",\"name\":\"Hello Fascia\",\"version\":\"1.0\",\"description\":\"A test\",\"entrypoint\":\"AppRun\",\"architecture\":\"arm-linux-gnueabihf\",\"display\":\"cpu\",\"builtin\":true}";
    const entry = (try parseEntry(arena.allocator(), raw, "hello", "/apps/hello/releases/123")) orelse return error.InvalidFixture;
    try std.testing.expectEqualStrings("Hello Fascia", entry.name);
    try std.testing.expectEqualStrings("/apps/hello/releases/123/AppRun", entry.entrypoint);
    try std.testing.expect(entry.builtin);
    try std.testing.expect((try parseEntry(arena.allocator(), raw, "other", "/apps/other")) == null);
    try std.testing.expect((try parseEntry(arena.allocator(), "{}", "hello", "/apps")) == null);
}
