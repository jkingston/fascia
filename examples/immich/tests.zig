const std = @import("std");
const m = @import("model.zig");
const api = @import("api.zig");
const a = "aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa";
const b = "bbbbbbbb-bbbb-4bbb-bbbb-bbbbbbbbbbbb";
const c = "cccccccc-cccc-4ccc-cccc-cccccccccccc";
const people = [_]m.Person{ .{ .id = a }, .{ .id = b }, .{ .id = c } };
fn asset(id: []const u8, persons: []const m.Person, date: []const u8) m.Asset {
    return .{ .id = id, .people = persons, .localDateTime = date, .visibility = "timeline" };
}

test "any all exact group and exclusions are independent and fail closed" {
    var settings = m.Settings{ .include = &.{ a, b } };
    const one = asset(a, people[0..1], "2020-01-01");
    const both = asset(b, people[0..2], "2022-01-01");
    const group = asset(c, &people, "2024-01-01");
    try std.testing.expect(m.accepts(settings, one));
    settings.match = .all;
    try std.testing.expect(!m.accepts(settings, one));
    try std.testing.expect(m.accepts(settings, both));
    try std.testing.expect(m.accepts(settings, group));
    settings.allow_others = false;
    try std.testing.expect(m.accepts(settings, both));
    try std.testing.expect(!m.accepts(settings, group));
    settings.match = .any;
    try std.testing.expect(m.accepts(settings, one));
    settings.exclude = &.{a};
    try std.testing.expect(!m.accepts(settings, one));
    try std.testing.expect(!m.accepts(settings, both));
    var unknown = one;
    unknown.people = null;
    try std.testing.expect(!m.accepts(settings, unknown));
    settings = .{};
    unknown.isTrashed = true;
    try std.testing.expect(!m.accepts(settings, unknown));
    unknown.isTrashed = false;
    unknown.visibility = "locked";
    try std.testing.expect(!m.accepts(settings, unknown));
}

test "Then and now shares a selected identity and never relaxes filters" {
    const photos = [_]m.Asset{ asset(a, people[0..1], "2020-01-01"), asset(b, people[0..2], "2024-01-01"), asset(c, people[1..2], "2025-01-01") };
    var settings = m.Settings{ .include = &.{a} };
    const pair = m.pair(&photos, settings, 0).?;
    try std.testing.expectEqual(@as(usize, 0), pair.earlier);
    try std.testing.expectEqual(@as(usize, 1), pair.later);
    try std.testing.expectEqualStrings(a, pair.person);
    settings.allow_others = false;
    try std.testing.expect(m.pair(&photos, settings, 0) == null);
    settings.allow_others = true;
    settings.exclude = &.{b};
    try std.testing.expect(m.pair(&photos, settings, 0) == null);
    const near = [_]m.Asset{ asset(a, people[0..1], "2024-01-01"), asset(b, people[0..1], "2024-11-01") };
    try std.testing.expect(m.pair(&near, .{}, 0) == null);
}

test "query combines source with people and explicit visibility without legacy fields" {
    const data = try api.query(std.testing.allocator, .{ .include = &.{a}, .exclude = &.{b}, .albums = &.{c}, .match = .all });
    defer std.testing.allocator.free(data);
    const parsed = try std.json.parseFromSlice(std.json.Value, std.testing.allocator, data, .{});
    defer parsed.deinit();
    const root = parsed.value.object;
    try std.testing.expect(root.get("cursor") == null);
    const filter = root.get("filter").?.object;
    try std.testing.expectEqualStrings("timeline", filter.get("visibility").?.object.get("eq").?.string);
    const ids = filter.get("personIds").?.object;
    try std.testing.expect(ids.get("any") == null);
    try std.testing.expectEqualStrings(a, ids.get("all").?.array.items[0].string);
    try std.testing.expectEqualStrings(b, ids.get("none").?.array.items[0].string);
}

test "configuration rejects header injection credentials in URLs and bad IDs" {
    try std.testing.expect(!m.Settings.valid(.{ .server = "https://user:pass@host" }));
    try std.testing.expect(!m.Settings.valid(.{ .api_key = "hello\r\nother: header" }));
    try std.testing.expect(!m.Settings.valid(.{ .include = &.{"../../file"} }));
    try std.testing.expect(m.Settings.valid(.{ .server = "http://nas:2283" }));
}

test {
    _ = @import("codec.zig");
}

test "smart crop retains all detected faces and fits when boxes cannot fit" {
    const faces = [_]m.Face{.{ .imageWidth = 1600, .imageHeight = 1200, .boundingBoxX1 = 600, .boundingBoxX2 = 900, .boundingBoxY1 = 40, .boundingBoxY2 = 300 }};
    const placement = m.place(1600, 1200, 1280, 800, .smart, &faces);
    try std.testing.expectApproxEqAbs(@as(f32, 1280), placement.width, 0.1);
    try std.testing.expect(placement.y + 40 * placement.height / 1200 >= 0);
    const distant = [_]m.Face{ faces[0], .{ .imageWidth = 1600, .imageHeight = 1200, .boundingBoxX1 = 400, .boundingBoxX2 = 700, .boundingBoxY1 = 950, .boundingBoxY2 = 1180 } };
    const fitted = m.place(1600, 1200, 1280, 800, .smart, &distant);
    try std.testing.expectApproxEqAbs(@as(f32, 800), fitted.height, 0.1);
    const unknown = m.place(1600, 1200, 1280, 800, .smart, null);
    try std.testing.expectApproxEqAbs(@as(f32, 800), unknown.height, 0.1);
    const portrait = m.place(600, 1200, 1280, 800, .smart, &.{});
    try std.testing.expectApproxEqAbs(@as(f32, 800), portrait.height, 0.1);
}

test {
    _ = @import("worker.zig");
}

test "long display intervals remain valid" {
    for (m.intervals) |seconds| try std.testing.expect((m.Settings{ .interval = seconds }).valid());
    try std.testing.expect((m.Settings{ .interval = 1800 }).valid());
}
