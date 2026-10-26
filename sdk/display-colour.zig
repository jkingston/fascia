//! Display-space channel attenuation, applied once to each source before blending.
//! This is a user-adjustable white-point approximation, not colour calibration.
const std = @import("std");
const builtin = @import("builtin");
const c = @cImport({
    @cInclude("stdlib.h");
    @cInclude("stdio.h");
    @cInclude("unistd.h");
    @cInclude("fcntl.h");
    @cInclude("errno.h");
    @cInclude("sys/stat.h");
});

pub const Colour = struct {
    gains: [3]f32 = .{ 1, 1, 1 },
    tables: [3][256]u8 = identityTables(),
    identity: bool = true,

    pub fn init(gains: [3]f32) !Colour {
        var result = Colour{ .gains = gains };
        for (gains, 0..) |gain, channel| {
            if (!std.math.isFinite(gain) or gain < 0.5 or gain > 1) return error.InvalidDisplayGain;
            result.identity = result.identity and gain == 1;
            for (0..256) |value| result.tables[channel][value] = @intFromFloat(@round(@as(f32, @floatFromInt(value)) * gain));
        }
        return result;
    }

    pub fn parse(text: []const u8) !Colour {
        var fields = std.mem.splitScalar(u8, text, ',');
        var gains: [3]f32 = undefined;
        for (&gains) |*gain| gain.* = std.fmt.parseFloat(f32, std.mem.trim(u8, fields.next() orelse return error.InvalidDisplayGain, " \t\r\n")) catch return error.InvalidDisplayGain;
        if (fields.next() != null) return error.InvalidDisplayGain;
        return init(gains);
    }

    pub fn environment() Colour {
        return loadFromEnvironment();
    }

    pub inline fn xrgb(self: *const Colour, pixel: u32) u32 {
        if (self.identity) return pixel;
        return (pixel & 0xff000000) | (@as(u32, self.tables[0][(pixel >> 16) & 255]) << 16) | (@as(u32, self.tables[1][(pixel >> 8) & 255]) << 8) | self.tables[2][pixel & 255];
    }

    pub inline fn rgba(self: *const Colour, colour: u32) u32 {
        if (self.identity) return colour;
        return (self.xrgb(colour >> 8) << 8) | (colour & 255);
    }
};
pub const default_config_path: [:0]const u8 = "/home/fascia/.config/fascia/display-colour.conf";

pub fn configPath() [:0]const u8 {
    return if (c.getenv("FASCIA_DISPLAY_CONFIG")) |raw| std.mem.span(raw) else default_config_path;
}

// An explicit env value, including unity or an invalid value, always overrides disk.
pub fn loadFromEnvironment() Colour {
    if (c.getenv("FASCIA_DISPLAY_GAINS")) |raw| return Colour.parse(std.mem.span(raw)) catch {
        std.log.warn("Invalid FASCIA_DISPLAY_GAINS: expected three finite values in 0.5..1.0; using unity", .{});
        return .{};
    };
    return loadSaved();
}

pub fn loadConfigured() Colour {
    return loadFromEnvironment();
}

pub fn loadSaved() Colour {
    return loadPath(configPath()) catch |err| {
        if (err != error.FileNotFound) std.log.warn("Cannot load display-colour setting ({s}); using unity", .{@errorName(err)});
        return .{};
    };
}

pub fn saveConfigured(colour: Colour) !void {
    try savePath(configPath(), colour);
}

fn errno() c_int {
    return if (builtin.os.tag == .macos) c.__error().* else c.__errno_location().*;
}

fn loadPath(path: [:0]const u8) !Colour {
    const file = c.fopen(path, "rb") orelse return if (errno() == c.ENOENT) error.FileNotFound else error.ConfigRead;
    defer _ = c.fclose(file);
    var bytes: [129]u8 = undefined;
    const len = c.fread(&bytes, 1, bytes.len, file);
    if (c.ferror(file) != 0) return error.ConfigRead;
    if (len > 128) return error.ConfigTooLarge;
    return Colour.parse(bytes[0..len]);
}

fn savePath(path: [:0]const u8, colour: Colour) !void {
    _ = try Colour.init(colour.gains);
    var bytes: [128]u8 = undefined;
    const text = try std.fmt.bufPrint(&bytes, "{d},{d},{d}\n", .{ colour.gains[0], colour.gains[1], colour.gains[2] });
    try @import("persist.zig").replace(path, text);
}

fn identityTables() [3][256]u8 {
    var result: [3][256]u8 = undefined;
    for (&result) |*channel| for (channel, 0..) |*value, i| {
        value.* = @intCast(i);
    };
    return result;
}

test "channel packing alpha preservation and unity are exact" {
    const identity = Colour{};
    for (0..256) |i| {
        const pixel = @as(u32, @intCast(i)) * 0x01010101;
        try std.testing.expectEqual(pixel, identity.rgba(pixel));
        try std.testing.expectEqual(pixel, identity.xrgb(pixel));
    }
    const warm = try Colour.parse("1,0.96,0.88");
    try std.testing.expectEqual(@as(u32, 0xfffff5e0), warm.xrgb(0xffffffff));
    try std.testing.expectEqual(@as(u32, 0xfff5e080), warm.rgba(0xffffff80));
    try std.testing.expectEqual(@as(u32, 0x00000025), warm.rgba(0x00000025));
    for ([_][]const u8{
        "nan,1,1",
        "inf,1,1",
        "1,1",
        "1,1,1,1",
        "1.1,1,1",
        "0.49,1,1",
        "x,1,1",
    }) |invalid| try std.testing.expectError(error.InvalidDisplayGain, Colour.parse(invalid));
}

test "saved gains round trip atomically with private mode and bounded reads" {
    var directory = "/tmp/fascia-display-colour-XXXXXX\x00".*;
    const root = c.mkdtemp(&directory) orelse return error.TestDirectory;
    defer _ = c.rmdir(root);
    var path_buffer: [512]u8 = undefined;
    const path = try std.fmt.bufPrintSentinel(&path_buffer, "{s}/settings/display.conf", .{root}, 0);
    var parent_buffer: [512]u8 = undefined;
    const parent = try std.fmt.bufPrintSentinel(&parent_buffer, "{s}/settings", .{root}, 0);
    defer _ = c.rmdir(parent);
    defer _ = c.unlink(path);
    try std.testing.expectError(error.FileNotFound, loadPath(path));
    const warm = try Colour.parse("1,0.96,0.88\n");
    try savePath(path, warm);
    try std.testing.expectEqualSlices(f32, &warm.gains, &(try loadPath(path)).gains);
    const old_path = if (c.getenv("FASCIA_DISPLAY_CONFIG")) |raw| try std.testing.allocator.dupeZ(u8, std.mem.span(raw)) else null;
    defer if (old_path) |saved| {
        _ = c.setenv("FASCIA_DISPLAY_CONFIG", saved, 1);
        std.testing.allocator.free(saved);
    } else {
        _ = c.unsetenv("FASCIA_DISPLAY_CONFIG");
    };
    const old_gains = if (c.getenv("FASCIA_DISPLAY_GAINS")) |raw| try std.testing.allocator.dupeZ(u8, std.mem.span(raw)) else null;
    defer if (old_gains) |saved| {
        _ = c.setenv("FASCIA_DISPLAY_GAINS", saved, 1);
        std.testing.allocator.free(saved);
    } else {
        _ = c.unsetenv("FASCIA_DISPLAY_GAINS");
    };
    try std.testing.expectEqual(@as(c_int, 0), c.setenv("FASCIA_DISPLAY_CONFIG", path, 1));
    _ = c.unsetenv("FASCIA_DISPLAY_GAINS");
    try std.testing.expectEqualSlices(f32, &warm.gains, &loadConfigured().gains);
    try std.testing.expectEqual(@as(c_int, 0), c.setenv("FASCIA_DISPLAY_GAINS", "1,1,1", 1));
    try std.testing.expect(loadConfigured().identity);
    try std.testing.expectEqualSlices(f32, &warm.gains, &loadSaved().gains);
    _ = c.setenv("FASCIA_DISPLAY_GAINS", "bad", 1);
    try std.testing.expect(loadConfigured().identity);

    var status: c.struct_stat = undefined;
    try std.testing.expectEqual(@as(c_int, 0), c.stat(path, &status));
    try std.testing.expectEqual(@as(@TypeOf(status.st_mode), 0o600), status.st_mode & 0o777);
    try savePath(path, .{});
    try std.testing.expect((try loadPath(path)).identity);
    const file = c.fopen(path, "wb") orelse return error.TestFile;
    const too_large = [_]u8{'1'} ** 129;
    _ = c.fwrite(&too_large, 1, too_large.len, file);
    _ = c.fclose(file);
    try std.testing.expectError(error.ConfigTooLarge, loadPath(path));
}
