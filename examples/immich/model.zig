const std = @import("std");

pub const Mode = enum { slideshow, then_now };
pub const Match = enum { any, all };
pub const intervals = [_]u32{ 30, 60, 300, 900, 1800, 3600 };

pub const Settings = struct {
    server: []const u8 = "",
    api_key: []const u8 = "",
    include: []const []const u8 = &.{},
    exclude: []const []const u8 = &.{},
    albums: []const []const u8 = &.{},
    match: Match = .any,
    allow_others: bool = true,
    favourites: bool = false,
    mode: Mode = .slideshow,
    framing: Framing = .smart,
    interval: u32 = 60,

    pub fn valid(self: Settings) bool {
        if (self.server.len > 2048 or self.api_key.len > 512 or self.interval < 10 or self.interval > 86400) return false;
        if (std.mem.indexOfAny(u8, self.api_key, "\r\n\x00") != null) return false;
        if (self.server.len != 0) {
            const uri = std.Uri.parse(self.server) catch return false;
            if ((!std.mem.eql(u8, uri.scheme, "http") and !std.mem.eql(u8, uri.scheme, "https")) or uri.host == null or uri.user != null or uri.password != null or uri.query != null or uri.fragment != null) return false;
            if (std.mem.indexOfAny(u8, self.server, "\r\n\x00\t ") != null) return false;
        }
        for ([_][]const []const u8{ self.include, self.exclude, self.albums }) |list| {
            if (list.len > 32) return false;
            for (list) |id| if (!validId(id)) return false;
        }
        return true;
    }
};

pub const Person = struct { id: []const u8, name: []const u8 = "", isHidden: bool = false };
pub const Album = struct { id: []const u8, albumName: []const u8 };
pub const Asset = struct {
    id: []const u8,
    type: []const u8 = "IMAGE",
    visibility: []const u8 = "",
    isArchived: bool = false,
    isTrashed: bool = false,
    isOffline: bool = false,
    isFavorite: bool = false,
    localDateTime: []const u8 = "",
    fileCreatedAt: []const u8 = "",
    people: ?[]const Person = null,
    duplicateId: ?[]const u8 = null,

    pub fn date(self: Asset) []const u8 {
        const value = if (self.localDateTime.len >= 10) self.localDateTime else self.fileCreatedAt;
        return value[0..@min(value.len, 10)];
    }
};

pub fn validId(id: []const u8) bool {
    if (id.len != 36) return false;
    for (id, 0..) |ch, i| {
        if (i == 8 or i == 13 or i == 18 or i == 23) {
            if (ch != '-') return false;
        } else if (!std.ascii.isHex(ch)) return false;
    }
    return true;
}

pub fn contains(ids: []const []const u8, id: []const u8) bool {
    for (ids) |other| if (std.mem.eql(u8, id, other)) return true;
    return false;
}

pub fn hasPerson(asset: Asset, id: []const u8) bool {
    for (asset.people orelse return false) |person| if (std.mem.eql(u8, person.id, id)) return true;
    return false;
}

pub fn accepts(settings: Settings, asset: Asset) bool {
    if (!validId(asset.id) or !std.mem.eql(u8, asset.type, "IMAGE") or !std.mem.eql(u8, asset.visibility, "timeline") or asset.isArchived or asset.isTrashed or asset.isOffline) return false;
    if (settings.favourites and !asset.isFavorite) return false;
    const filtered = settings.include.len > 0 or settings.exclude.len > 0;
    const people = asset.people orelse return !filtered and settings.allow_others;
    for (people) |person| {
        if (contains(settings.exclude, person.id)) return false;
        if (!settings.allow_others and !contains(settings.include, person.id)) return false;
    }
    if (settings.include.len == 0) return settings.allow_others;
    var matches: usize = 0;
    for (settings.include) |id| if (hasPerson(asset, id)) {
        matches += 1;
    };
    return if (settings.match == .all) matches == settings.include.len else matches > 0;
}

pub const Pair = struct { earlier: usize, later: usize, person: []const u8 };

/// Same identified person, different days, at least a year apart.
pub fn pair(assets: []const Asset, settings: Settings, start: usize) ?Pair {
    if (assets.len < 2) return null;
    for (0..assets.len) |offset| {
        const i = (start + offset) % assets.len;
        const first = assets[i];
        if (!accepts(settings, first)) continue;
        const day = dateNumber(first.date()) orelse continue;
        for (first.people orelse continue) |person| {
            if (settings.include.len > 0 and !contains(settings.include, person.id)) continue;
            var best: ?usize = null;
            var distance: i64 = 0;
            for (assets, 0..) |second, j| {
                if (i == j or !accepts(settings, second) or !hasPerson(second, person.id)) continue;
                if (first.duplicateId != null and second.duplicateId != null and std.mem.eql(u8, first.duplicateId.?, second.duplicateId.?)) continue;
                const other_day = dateNumber(second.date()) orelse continue;
                const gap: i64 = @intCast(@abs(day - other_day));
                if (gap >= 366 and gap > distance) {
                    best = j;
                    distance = gap;
                }
            }
            if (best) |j| return if (std.mem.order(u8, first.date(), assets[j].date()) == .lt)
                .{ .earlier = i, .later = j, .person = person.id }
            else
                .{ .earlier = j, .later = i, .person = person.id };
        }
    }
    return null;
}

fn dateNumber(date: []const u8) ?i64 {
    if (date.len != 10 or date[4] != '-' or date[7] != '-') return null;
    const year = std.fmt.parseInt(i64, date[0..4], 10) catch return null;
    const month = std.fmt.parseInt(i64, date[5..7], 10) catch return null;
    const day = std.fmt.parseInt(i64, date[8..10], 10) catch return null;
    if (month < 1 or month > 12 or day < 1 or day > 31) return null;
    const y = year - @as(i64, if (month <= 2) 1 else 0);
    const era = @divFloor(y, 400);
    const yoe = y - era * 400;
    const m = month + @as(i64, if (month > 2) -3 else 9);
    return era * 146097 + yoe * 365 + @divFloor(yoe, 4) - @divFloor(yoe, 100) + @divFloor(153 * m + 2, 5) + day - 1;
}

pub const Framing = enum { smart, fit, fill };
pub const Face = struct {
    imageWidth: f32,
    imageHeight: f32,
    boundingBoxX1: f32,
    boundingBoxY1: f32,
    boundingBoxX2: f32,
    boundingBoxY2: f32,
    person: ?Person = null,
};

pub const Placement = struct { x: f32, y: f32, width: f32, height: f32 };

pub fn place(width: f32, height: f32, screen_w: f32, screen_h: f32, framing: Framing, faces: ?[]const Face) Placement {
    const fit = @min(screen_w / width, screen_h / height);
    const fill = @max(screen_w / width, screen_h / height);
    var scale = if (framing == .fit) fit else fill;
    var centre_x: f32 = 0.5;
    var centre_y: f32 = 0.5;
    if (framing == .smart) {
        if (faces) |known| {
            var left: f32 = 1;
            var top: f32 = 1;
            var right: f32 = 0;
            var bottom: f32 = 0;
            for (known) |face| {
                if (face.imageWidth <= 0 or face.imageHeight <= 0 or face.boundingBoxX2 <= face.boundingBoxX1 or face.boundingBoxY2 <= face.boundingBoxY1 or face.boundingBoxX1 < 0 or face.boundingBoxY1 < 0 or face.boundingBoxX2 > face.imageWidth or face.boundingBoxY2 > face.imageHeight) {
                    scale = fit;
                    continue;
                }
                if (@abs(face.imageWidth / face.imageHeight - width / height) > 0.02) {
                    scale = fit;
                    continue;
                }
                left = @min(left, @max(0, face.boundingBoxX1 / face.imageWidth - 0.04));
                right = @max(right, @min(1, face.boundingBoxX2 / face.imageWidth + 0.04));
                top = @min(top, @max(0, face.boundingBoxY1 / face.imageHeight - 0.06));
                bottom = @max(bottom, @min(1, face.boundingBoxY2 / face.imageHeight + 0.04));
            }
            if (known.len > 0) {
                if ((right - left) * width * fill > screen_w or (bottom - top) * height * fill > screen_h) scale = fit;
                centre_x = (left + right) / 2;
                centre_y = (top + bottom) / 2;
            }
            // Extreme crops discard most of a portrait. Preserve the photograph.
            if (fill / fit > 1.8) scale = fit;
        } else scale = fit;
    }
    const w = width * scale;
    const h = height * scale;
    return .{
        .x = if (w <= screen_w) (screen_w - w) / 2 else std.math.clamp(screen_w / 2 - centre_x * w, screen_w - w, 0),
        .y = if (h <= screen_h) (screen_h - h) / 2 else std.math.clamp(screen_h / 2 - centre_y * h, screen_h - h, 0),
        .width = w,
        .height = h,
    };
}
