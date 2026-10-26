//! Bounded RSS 2.0 image enclosures. No DTD, external entities or HTML rendering.
const std = @import("std");
pub const default_url = "https://www.nasa.gov/feeds/iotd-feed/";

pub const Entry = struct {
    url: []const u8,
    caption: []const u8,
    source: []const u8,
    source_url: []const u8,
    credit: []const u8 = "",
};

pub fn validUrl(url: []const u8) bool {
    if (url.len < 9 or url.len > 2048 or std.mem.indexOfAny(u8, url, "\x00\r\n\t ") != null) return false;
    const uri = std.Uri.parse(url) catch return false;
    return (std.mem.eql(u8, uri.scheme, "https") or
        std.mem.eql(u8, uri.scheme, "http")) and
        uri.host != null and
        uri.user == null and
        uri.password == null and
        uri.fragment == null;
}

fn tag(xml: []const u8, name: []const u8) ?[]const u8 {
    var begin_buf: [80]u8 = undefined;
    var end_buf: [80]u8 = undefined;
    const begin = std.fmt.bufPrint(&begin_buf, "<{s}>", .{name}) catch return null;
    const end = std.fmt.bufPrint(&end_buf, "</{s}>", .{name}) catch return null;
    const start = (std.mem.indexOf(u8, xml, begin) orelse return null) + begin.len;
    return xml[start .. start + (std.mem.indexOf(u8, xml[start..], end) orelse return null)];
}

fn attribute(xml: []const u8, name: []const u8) ?[]const u8 {
    var at: usize = 0;
    while (std.mem.indexOfPos(u8, xml, at, name)) |found| {
        at = found + name.len;
        if (found > 0 and !std.ascii.isWhitespace(xml[found - 1])) continue;
        while (at < xml.len and std.ascii.isWhitespace(xml[at])) : (at += 1) {}
        if (at == xml.len or xml[at] != '=') continue;
        at += 1;
        while (at < xml.len and std.ascii.isWhitespace(xml[at])) : (at += 1) {}
        if (at == xml.len or (xml[at] != '"' and xml[at] != '\'')) continue;
        const quote = xml[at];
        at += 1;
        const end = std.mem.indexOfScalarPos(u8, xml, at, quote) orelse return null;
        return xml[at..end];
    }
    return null;
}

pub fn plain(a: std.mem.Allocator, raw: []const u8, limit: usize) ![]const u8 {
    var text = std.mem.trim(u8, raw, " \t\r\n");
    if (std.mem.startsWith(u8, text, "<![CDATA[") and std.mem.endsWith(u8, text, "]]>")) text = text[9 .. text.len - 3];
    var out: std.ArrayList(u8) = .empty;
    var at: usize = 0;
    while (at < text.len and out.items.len < limit) {
        if (text[at] == '<') {
            at = (std.mem.indexOfScalarPos(u8, text, at, '>') orelse break) + 1;
            continue;
        }
        if (text[at] == '&') {
            if (std.mem.indexOfScalarPos(u8, text, at, ';')) |end| {
                if (end - at < 16) {
                    const entity = text[at + 1 .. end];
                    const replacement: ?u21 = if (std.mem.eql(u8, entity, "amp")) '&' else if (std.mem.eql(u8, entity, "lt")) '<' else if (std.mem.eql(u8, entity, "gt")) '>' else if (std.mem.eql(u8, entity, "quot")) '"' else if (std.mem.eql(u8, entity, "apos")) '\'' else if (std.mem.startsWith(u8, entity, "#x")) std.fmt.parseInt(u21, entity[2..], 16) catch null else if (std.mem.startsWith(u8, entity, "#")) std.fmt.parseInt(u21, entity[1..], 10) catch null else null;
                    if (replacement) |cp| {
                        var encoded: [4]u8 = undefined;
                        const n = std.unicode.utf8Encode(cp, &encoded) catch 0;
                        if (n > 0 and out.items.len + n <= limit) try out.appendSlice(a, encoded[0..n]);
                        at = end + 1;
                        continue;
                    }
                }
            }
        }
        const n = std.unicode.utf8ByteSequenceLength(text[at]) catch {
            at += 1;
            continue;
        };
        if (at + n > text.len or out.items.len + n > limit) break;
        if (!std.unicode.utf8ValidateSlice(text[at..][0..n])) {
            at += 1;
            continue;
        }
        if (text[at] >= 32) try out.appendSlice(a, text[at..][0..n]);
        at += n;
    }
    return out.toOwnedSlice(a);
}

pub fn parse(a: std.mem.Allocator, xml: []const u8) ![]const Entry {
    if (xml.len > 2 * 1024 * 1024 or std.mem.indexOf(u8, xml, "<!DOCTYPE") != null or std.mem.indexOf(u8, xml, "<!ENTITY") != null) return error.InvalidFeed;
    if (std.mem.indexOf(u8, xml, "<rss") == null) return error.UnsupportedFeed;
    const first_item = std.mem.indexOf(u8, xml, "<item") orelse return error.NoImages;
    const source = try plain(a, tag(xml[0..first_item], "title") orelse "Photo feed", 120);
    var entries: std.ArrayList(Entry) = .empty;
    var at = first_item;
    var scanned: usize = 0;
    while (scanned < 120 and entries.items.len < 60) : (scanned += 1) {
        const start = std.mem.indexOfPos(u8, xml, at, "<item") orelse break;
        const content = (std.mem.indexOfScalarPos(u8, xml, start, '>') orelse break) + 1;
        const end = std.mem.indexOfPos(u8, xml, content, "</item>") orelse break;
        const item = xml[content..end];
        at = end + 7;
        const enclosure_start = std.mem.indexOf(u8, item, "<enclosure") orelse continue;
        const enclosure_end = std.mem.indexOfScalarPos(u8, item, enclosure_start, '>') orelse continue;
        const url = try plain(a, attribute(item[enclosure_start..enclosure_end], "url") orelse continue, 2048);
        if (!validUrl(url)) continue;
        const kind = attribute(item[enclosure_start..enclosure_end], "type") orelse "";
        if (kind.len > 0 and !std.mem.startsWith(u8, kind, "image/")) continue;
        const link = try plain(a, tag(item, "link") orelse "", 2048);
        try entries.append(a, .{
            .url = url,
            .caption = try plain(a, tag(item, "title") orelse "Photograph", 400),
            .source = source,
            .source_url = if (validUrl(link)) link else "",
            .credit = try plain(a, tag(item, "media:credit") orelse tag(item, "dc:creator") orelse "", 240),
        });
    }
    if (entries.items.len == 0) return error.NoImages;
    return entries.toOwnedSlice(a);
}

pub fn imageUrl(a: std.mem.Allocator, url: []const u8) ![]const u8 {
    // NASA advertises originals that can exceed 200 MB. Its WordPress image
    // endpoint supplies bounded display variants; other hosts are unchanged.
    if (std.mem.startsWith(u8, url, "https://www.nasa.gov/wp-content/uploads/") and std.mem.indexOfScalar(u8, url, '?') == null)
        return std.fmt.allocPrint(a, "{s}?resize=1536,1152", .{url});
    return a.dupe(u8, url);
}

test "RSS entities enclosures and missing optional author are honest" {
    var arena = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena.deinit();
    const entries = try parse(
        arena.allocator(),
        "<rss><channel><title>NASA &amp; science</title><item><title><![CDATA[Moon – &#x263d;]]></title><link>https://example.org/story</link><enclosure type='image/png' length='208563652' url='https://www.nasa.gov/wp-content/uploads/giant.webp'/></item></channel></rss>",
    );
    try std.testing.expectEqual(@as(usize, 1), entries.len);
    try std.testing.expectEqualStrings("NASA & science", entries[0].source);
    try std.testing.expectEqualStrings("", entries[0].credit);
    try std.testing.expect(std.unicode.utf8ValidateSlice(entries[0].caption));
    const variant = try imageUrl(arena.allocator(), entries[0].url);
    try std.testing.expect(std.mem.endsWith(u8, variant, "?resize=1536,1152"));
    try std.testing.expect(!validUrl("file:///etc/passwd"));
    try std.testing.expect(!validUrl("https://user:password@example.org/feed"));
    try std.testing.expectError(error.InvalidFeed, parse(arena.allocator(), "<!DOCTYPE rss><rss/>"));
}
