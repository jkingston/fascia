const std = @import("std");

pub const Command = union(enum) { home, status, state, stop: []const u8, launch: []const u8 };

pub fn validId(id: []const u8) bool {
    if (id.len == 0 or id.len > 64) return false;
    for (id, 0..) |ch, index| {
        if (!((ch >= 'a' and ch <= 'z') or (ch >= '0' and ch <= '9') or (index > 0 and ch == '-'))) return false;
    }
    return true;
}

pub fn parse(line: []const u8) !Command {
    if (std.mem.eql(u8, line, "home")) return .home;
    if (std.mem.eql(u8, line, "status")) return .status;
    if (std.mem.eql(u8, line, "state")) return .state;
    if (std.mem.startsWith(u8, line, "stop ") and validId(line[5..])) return .{ .stop = line[5..] };
    if (std.mem.startsWith(u8, line, "launch ")) {
        const id = line[7..];
        if (validId(id)) return .{ .launch = id };
    }
    return error.InvalidCommand;
}

test "IPC accepts bounded registered ids and rejects paths shell arguments and trailing commands" {
    try std.testing.expectEqualStrings("photo-2", (try parse("launch photo-2")).launch);
    try std.testing.expect((try parse("home")) == .home);
    for ([_][]const u8{
        "launch ../x",
        "launch /bin/sh",
        "launch a;id",
        "launch a b",
        "launch A",
        "launch -a",
        "launch a\nhome",
        "launch ",
        "home ",
        "stop ../x",
        "stop a b",
        "state extra",
    }) |bad| try std.testing.expectError(error.InvalidCommand, parse(bad));
    try std.testing.expect(!validId(&([_]u8{'a'} ** 65)));
}
