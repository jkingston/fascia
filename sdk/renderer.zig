//! Retained software rasterization for finite UI geometry. Widths, heights,
//! radii and text sizes are nonnegative; opacity/coordinates must be finite.
//! Hot paths use optimized float math to avoid ARMv7 NaN min/max helpers.
//! Nonzero image content_hash denotes immutable content; zero hashes bytes.
//! Histories retain value/content stamps, never frame-arena pointers.
const std = @import("std");
const t = @import("types.zig");
const display = @import("display-colour.zig");
const c = @cImport({
    @cInclude("ft2build.h");
    @cInclude("freetype/freetype.h");
});

const Stamp = struct {
    hash: u64,
    bounds: t.Rect,
    group: u64,
};

const Glyph = struct {
    pixels: []u8,
    w: i32,
    h: i32,
    left: i32,
    top: i32,
    advance: f32,
};

pub const Renderer = struct {
    allocator: std.mem.Allocator,
    display_colour: display.Colour = .{},
    ft: c.FT_Library = null,
    face: c.FT_Face = null,
    glyphs: std.AutoHashMap(u64, Glyph),
    history: [3]std.ArrayList(Stamp) = .{ .empty, .empty, .empty },
    dimensions: [3][2]u32 = .{ .{ 0, 0 }, .{ 0, 0 }, .{ 0, 0 } },
    damage: [128]t.Rect = undefined,
    count: usize = 0,
    groups: [128]struct { id: u64, bounds: t.Rect } = undefined,
    group_count: usize = 0,

    pub fn init(a: std.mem.Allocator, path: [:0]const u8) !Renderer {
        var self = Renderer{ .allocator = a, .glyphs = std.AutoHashMap(u64, Glyph).init(a) };
        self.display_colour = display.loadConfigured();
        if (c.FT_Init_FreeType(&self.ft) != 0) return error.FontInit;
        if (c.FT_New_Face(self.ft, path, 0, &self.face) != 0) return error.FontOpen;
        return self;
    }

    pub fn setDisplayColour(self: *Renderer, colour: display.Colour) void {
        self.display_colour = colour;
        // Every swapchain buffer and visible damage history must be repainted.
        for (&self.history) |*history| history.clearRetainingCapacity();
        self.dimensions = .{ .{ 0, 0 }, .{ 0, 0 }, .{ 0, 0 } };
    }

    pub fn deinit(self: *Renderer) void {
        var it = self.glyphs.valueIterator();
        while (it.next()) |g| self.allocator.free(g.pixels);
        self.glyphs.deinit();
        for (&self.history) |*h| h.deinit(self.allocator);
        _ = c.FT_Done_Face(self.face);
        _ = c.FT_Done_FreeType(self.ft);
    }

    fn glyph(self: *Renderer, code: u32, size: f32) !Glyph {
        const px: u32 = @intFromFloat(@max(1, @round(size)));
        const key = (@as(u64, px) << 32) | code;
        if (self.glyphs.get(key)) |g| return g;
        _ = c.FT_Set_Pixel_Sizes(self.face, 0, px);
        if (c.FT_Load_Char(self.face, code, c.FT_LOAD_RENDER) != 0) return error.Glyph;
        const s = self.face.*.glyph;
        const bm = s.*.bitmap;
        const pixels = try self.allocator.alloc(u8, @as(usize, bm.width) * bm.rows);
        for (0..bm.rows) |y| @memcpy(
            pixels[y * bm.width ..][0..bm.width],
            bm.buffer[y * @as(usize, @intCast(@abs(bm.pitch))) ..][0..bm.width],
        );
        const g = Glyph{
            .pixels = pixels,
            .w = @intCast(bm.width),
            .h = @intCast(bm.rows),
            .left = s.*.bitmap_left,
            .top = s.*.bitmap_top,
            .advance = @as(f32, @floatFromInt(s.*.advance.x)) / 64,
        };
        try self.glyphs.put(key, g);
        return g;
    }

    fn ascender(self: *Renderer, size: f32) f32 {
        return @ceil(
            size * @as(f32, @floatFromInt(self.face.*.ascender)) / @as(f32, @floatFromInt(self.face.*.units_per_EM)),
        );
    }

    fn paintBounds(cmd: t.Command) t.Rect {
        @setFloatMode(.optimized);
        var r = cmd.bounds;
        if (cmd.kind == .circle or cmd.kind == .line) {
            r.x -= 0.5;
            r.y -= 0.5;
            r.width += 1;
            r.height += 1;
        }
        if (cmd.kind == .text) {
            const margin = cmd.kind.text.size * 0.25;
            r.x -= margin;
            r.y -= margin;
            r.width += margin * 2;
            r.height += margin * 2;
        }
        return r;
    }

    pub fn measure(self: *Renderer) t.Measure {
        return .{ .context = self, .function = measureText };
    }

    fn measureText(ctx: ?*anyopaque, text: []const u8, size: f32) t.Metrics {
        const self: *Renderer = @ptrCast(@alignCast(ctx.?));
        var width: f32 = 0;
        var view = std.unicode.Utf8View.init(text) catch return .{ .width = 0, .height = size };
        var it = view.iterator();
        while (it.nextCodepoint()) |cp| {
            const g = self.glyph(cp, size) catch continue;
            width += g.advance;
        }
        return .{
            .width = width,
            .height = size * 1.2,
            .ascender = self.ascender(size),
        };
    }

    fn hash(cmd: t.Command) u64 {
        var h = std.hash.Wyhash.init(0);
        h.update(std.mem.asBytes(&cmd.id));
        h.update(std.mem.asBytes(&cmd.bounds));
        if (cmd.clip) |r| h.update(std.mem.asBytes(&r));
        const tag = @intFromEnum(cmd.kind);
        h.update(std.mem.asBytes(&tag));
        switch (cmd.kind) {
            .vertical_gradient => |v| {
                h.update(std.mem.asBytes(&v.top));
                h.update(std.mem.asBytes(&v.bottom));
                h.update(std.mem.asBytes(&v.dither));
            },
            .polygon => |v| {
                h.update(std.mem.sliceAsBytes(v.points));
                h.update(std.mem.asBytes(&v.colour));
            },
            .line => |v| {
                h.update(std.mem.asBytes(&v.x1));
                h.update(std.mem.asBytes(&v.y1));
                h.update(std.mem.asBytes(&v.x2));
                h.update(std.mem.asBytes(&v.y2));
                h.update(std.mem.asBytes(&v.width));
                h.update(std.mem.asBytes(&v.colour));
            },
            .rectangle => |v| {
                h.update(std.mem.asBytes(&v.colour));
                h.update(std.mem.asBytes(&v.radius));
            },
            .circle => |v| {
                h.update(std.mem.asBytes(&v.cx));
                h.update(std.mem.asBytes(&v.cy));
                h.update(std.mem.asBytes(&v.radius));
                h.update(std.mem.asBytes(&v.colour));
                h.update(std.mem.asBytes(&v.opacity));
            },
            .text => |v| {
                h.update(v.content);
                h.update(std.mem.asBytes(&v.size));
                h.update(std.mem.asBytes(&v.colour));
            },
            .image => |v| {
                if (v.content_hash != 0) h.update(std.mem.asBytes(&v.content_hash)) else h.update(std.mem.sliceAsBytes(v.pixels));
                h.update(std.mem.asBytes(&v.width));
                h.update(std.mem.asBytes(&v.height));
                h.update(std.mem.asBytes(&v.opacity));
            },
        }
        return h.final();
    }

    fn add(self: *Renderer, r: t.Rect) void {
        @setFloatMode(.optimized);
        if (r.width <= 0 or r.height <= 0) return;
        if (self.count == self.damage.len) {
            self.damage[0] = unite(self.damage[0], r);
            for (self.damage[1..self.count]) |v| self.damage[0] = unite(self.damage[0], v);
            self.count = 1;
            return;
        }
        var value = r;
        var i: usize = 0;
        while (i < self.count) {
            if (overlaps(value, self.damage[i])) {
                value = unite(value, self.damage[i]);
                self.count -= 1;
                self.damage[i] = self.damage[self.count];
                i = 0;
            } else i += 1;
        }
        self.damage[self.count] = value;
        self.count += 1;
    }

    fn collect(self: *Renderer, r: t.Rect, group: u64) void {
        if (group == 0) {
            self.add(r);
            return;
        }
        for (self.groups[0..self.group_count]) |*g| if (g.id == group) {
            g.bounds = unite(g.bounds, r);
            return;
        };
        if (self.group_count == self.groups.len) {
            self.add(r);
            return;
        }
        self.groups[self.group_count] = .{ .id = group, .bounds = r };
        self.group_count += 1;
    }

    fn flushGroups(self: *Renderer) void {
        for (self.groups[0..self.group_count]) |g| self.add(g.bounds);
        self.group_count = 0;
    }

    pub fn changes(self: *Renderer, slot: usize, w: u32, h: u32, commands: []const t.Command) ![]const t.Rect {
        self.count = 0;
        self.group_count = 0;
        const prev = &self.history[slot];
        const comparable = self.dimensions[slot][0] == w and self.dimensions[slot][1] == h and prev.items.len == commands.len;
        if (!comparable) self.add(.{ .width = @floatFromInt(w), .height = @floatFromInt(h) });
        try prev.resize(self.allocator, commands.len);
        for (commands, prev.items) |cmd, *old| {
            const current = Stamp{ .hash = hash(cmd), .bounds = paintBounds(cmd), .group = cmd.group };
            if (comparable and current.hash != old.hash) {
                self.collect(old.bounds, old.group);
                self.collect(current.bounds, current.group);
            }
            old.* = current;
        }
        self.flushGroups();
        self.dimensions[slot] = .{ w, h };
        return self.damage[0..self.count];
    }

    /// Reuse the retained scalar/content stamps of the buffer just rendered.
    /// This avoids hashing the complete scene again for compositor-visible damage.
    pub fn visible(self: *Renderer, source: usize, w: u32, h: u32) ![]const t.Rect {
        self.count = 0;
        self.group_count = 0;
        const current = self.history[source].items;
        const prev = &self.history[2];
        const comparable = self.dimensions[2][0] == w and self.dimensions[2][1] == h and prev.items.len == current.len;
        if (!comparable) self.add(.{ .width = @floatFromInt(w), .height = @floatFromInt(h) });
        try prev.resize(self.allocator, current.len);
        for (current, prev.items) |stamp, *old| {
            if (comparable and stamp.hash != old.hash) {
                self.collect(old.bounds, old.group);
                self.collect(stamp.bounds, stamp.group);
            }
            old.* = stamp;
        }
        self.flushGroups();
        self.dimensions[2] = .{ w, h };
        return self.damage[0..self.count];
    }

    pub fn render(self: *Renderer, pixels: []u32, w: u32, h: u32, slot: usize, commands: []const t.Command) ![]const t.Rect {
        @setFloatMode(.optimized);
        const dirty = try self.changes(slot, w, h, commands);
        for (dirty) |d| {
            const snapped = t.Rect{
                .x = @floor(d.x),
                .y = @floor(d.y),
                .width = @ceil(d.x + d.width) - @floor(d.x),
                .height = @ceil(d.y + d.height) - @floor(d.y),
            };
            const region = intersect(snapped, .{ .width = @floatFromInt(w), .height = @floatFromInt(h) });
            if (region.width <= 0 or region.height <= 0) continue;
            const x0: i32 = @intFromFloat(@floor(region.x));
            const y0: i32 = @intFromFloat(@floor(region.y));
            const x1: i32 = @intFromFloat(@ceil(region.x + region.width));
            const y1: i32 = @intFromFloat(@ceil(region.y + region.height));
            const root_opaque = commands.len > 0 and
                commands[0].kind == .rectangle and
                commands[0].kind.rectangle.radius == 0 and
                commands[0].kind.rectangle.colour & 255 == 255 and
                commands[0].clip == null and
                commands[0].bounds.x <= region.x and
                commands[0].bounds.y <= region.y and
                commands[0].bounds.x + commands[0].bounds.width >= region.x + region.width and
                commands[0].bounds.y + commands[0].bounds.height >= region.y + region.height;
            var y = y0;
            while (!root_opaque and
                y < y1) : (y += 1) @memset(pixels[@as(usize, @intCast(y)) * w + @as(usize, @intCast(x0)) ..][0..@intCast(x1 - x0)], 0xff000000);
            for (commands) |cmd| {
                var clip = intersect(region, paintBounds(cmd));
                if (cmd.clip) |r| clip = intersect(clip, r);
                if (clip.width > 0 and clip.height > 0) self.draw(pixels, w, cmd, clip);
            }
        }
        return dirty;
    }

    fn draw(self: *Renderer, p: []u32, w: u32, original: t.Command, clip: t.Rect) void {
        var cmd = original;
        switch (cmd.kind) {
            .vertical_gradient => |*v| {
                v.top = self.display_colour.rgba(v.top);
                v.bottom = self.display_colour.rgba(v.bottom);
            },
            .rectangle => |*v| v.colour = self.display_colour.rgba(v.colour),
            .polygon => |*v| v.colour = self.display_colour.rgba(v.colour),
            .line => |*v| v.colour = self.display_colour.rgba(v.colour),
            .circle => |*v| v.colour = self.display_colour.rgba(v.colour),
            .text => |*v| v.colour = self.display_colour.rgba(v.colour),
            .image => {},
        }
        @setFloatMode(.optimized);
        const x0: i32 = @intFromFloat(@floor(clip.x));
        const y0: i32 = @intFromFloat(@floor(clip.y));
        const x1: i32 = @intFromFloat(@ceil(clip.x + clip.width));
        const y1: i32 = @intFromFloat(@ceil(clip.y + clip.height));
        if (cmd.kind == .vertical_gradient) {
            const v = cmd.kind.vertical_gradient;
            if (cmd.bounds.height <= 0) return;
            const top = rgbaChannels(v.top);
            const delta = rgbaChannels(v.bottom) - top;
            var y = y0;
            while (y < y1) : (y += 1) {
                const fy = @as(f32, @floatFromInt(y));
                const fraction = clamp((fy + 0.5 - cmd.bounds.y) / cmd.bounds.height, 0, 1);
                const colour = top + delta * @as(@Vector(4, f32), @splat(fraction));
                const row_coverage = clamp(@min(fy + 1, clip.y + clip.height) - @max(fy, clip.y), 0, 1);
                var x = x0;
                while (x < x1) : (x += 1) {
                    const fx = @as(f32, @floatFromInt(x));
                    const coverage = row_coverage * clamp(@min(fx + 1, clip.x + clip.width) - @max(fx, clip.x), 0, 1);
                    gradientBlend(&p[@as(usize, @intCast(y)) * w + @as(usize, @intCast(x))], colour, coverage, @intCast(x), @intCast(y), v.dither);
                }
            }
            return;
        }
        if (cmd.kind == .polygon) {
            const v = cmd.kind.polygon;
            if (v.points.len < 3) return;
            var y = y0;
            while (y < y1) : (y += 1) {
                var left: [4]f32 = @splat(@as(f32, @floatFromInt(x1)));
                var right: [4]f32 = @splat(@as(f32, @floatFromInt(x0)));
                for (0..4) |sample| {
                    const fy = @as(f32, @floatFromInt(y)) + (@as(f32, @floatFromInt(sample)) + 0.5) / 4;
                    for (v.points, 0..) |p1, i| {
                        const p2 = v.points[(i + 1) % v.points.len];
                        if ((p1.y <= fy and p2.y > fy) or (p2.y <= fy and p1.y > fy)) {
                            const x = p1.x + (fy - p1.y) * (p2.x - p1.x) / (p2.y - p1.y);
                            left[sample] = @min(left[sample], x);
                            right[sample] = @max(right[sample], x);
                        }
                    }
                }
                const first = @max(x0, @as(i32, @intFromFloat(@floor(@reduce(.Min, @as(@Vector(4, f32), left))))));
                const last = @min(x1, @as(i32, @intFromFloat(@ceil(@reduce(.Max, @as(@Vector(4, f32), right))))));
                var x = first;
                while (x < last) : (x += 1) {
                    var coverage: f32 = 0;
                    const fx: f32 = @floatFromInt(x);
                    for (0..4) |sample| coverage += clamp(@min(fx + 1, right[sample]) - @max(fx, left[sample]), 0, 1) * 0.25;
                    blend(&p[@as(usize, @intCast(y)) * w + @as(usize, @intCast(x))], v.colour, coverage);
                }
            }
            return;
        }
        if (cmd.kind == .line) {
            const v = cmd.kind.line;
            if (v.width <= 0) return;
            const dx = v.x2 - v.x1;
            const dy = v.y2 - v.y1;
            const length2 = dx * dx + dy * dy;
            const steep = @abs(dy) > @abs(dx);
            const major_start = if (steep) v.y1 else v.x1;
            const major_end = if (steep) v.y2 else v.x2;
            const minor_start = if (steep) v.x1 else v.y1;
            const major_delta = if (steep) dy else dx;
            const minor_delta = if (steep) dx else dy;
            const slope = if (@abs(major_delta) > 0.0001) minor_delta / major_delta else 0;
            const radius = v.width * 0.5;
            // Iterate a narrow band along the dominant axis. A diagonal never
            // scans its full bounding rectangle; work scales with stroke length.
            const band = (radius + 0.5) * @sqrt(1 + slope * slope);
            var major = if (steep) y0 else x0;
            const major_limit = if (steep) y1 else x1;
            const minor_low = if (steep) x0 else y0;
            const minor_high = if (steep) x1 else y1;
            while (major < major_limit) : (major += 1) {
                const fm = @as(f32, @floatFromInt(major)) + 0.5;
                const along = clamp(fm, @min(major_start, major_end), @max(major_start, major_end));
                const centre = minor_start + (along - major_start) * slope;
                var minor = @max(minor_low, @as(i32, @intFromFloat(@floor(centre - band))));
                const limit = @min(minor_high, @as(i32, @intFromFloat(@ceil(centre + band))));
                while (minor < limit) : (minor += 1) {
                    const x = if (steep) minor else major;
                    const y = if (steep) major else minor;
                    const px = @as(f32, @floatFromInt(x)) + 0.5 - v.x1;
                    const py = @as(f32, @floatFromInt(y)) + 0.5 - v.y1;
                    const f = if (length2 > 0.0001) clamp((px * dx + py * dy) / length2, 0, 1) else 0;
                    const ex = px - f * dx;
                    const ey = py - f * dy;
                    const d2 = ex * ex + ey * ey;
                    const coverage = if (radius >= 0.5 and d2 <= (radius - 0.5) * (radius - 0.5)) 1 else clamp(radius + 0.5 - @sqrt(d2), 0, 1);
                    blend(&p[@as(usize, @intCast(y)) * w + @as(usize, @intCast(x))], v.colour, coverage);
                }
            }
            return;
        }
        if (cmd.kind == .rectangle) {
            const v = cmd.kind.rectangle;
            const b = cmd.bounds;
            const radius = @min(v.radius, @min(b.width, b.height) * 0.5);
            var row = y0;
            while (row < y1) : (row += 1) {
                const fy = @as(f32, @floatFromInt(row)) + 0.5;
                const dy = @max(@max(b.y + radius - fy, fy - (b.y + b.height - radius)), 0);
                const inset = if (radius > 0 and dy > 0) radius - @sqrt(@max(0, radius * radius - dy * dy)) else 0;
                const left = @max(clip.x, b.x + inset);
                const right = @min(clip.x + clip.width, b.x + b.width - inset);
                const first: i32 = @intFromFloat(@ceil(left));
                const last: i32 = @intFromFloat(@floor(right));
                const row_alpha = clamp(
                    @min(@as(f32, @floatFromInt(row)) + 1, b.y + b.height) - @max(@as(f32, @floatFromInt(row)), b.y),
                    0,
                    1,
                );
                const offset = @as(usize, @intCast(row)) * w;
                if (last > first) {
                    if (v.colour & 255 == 255 and
                        row_alpha >= 1) @memset(p[offset + @as(usize, @intCast(first)) ..][0..@intCast(last - first)], 0xff000000 | (v.colour >> 8)) else {
                        var x = first;
                        while (x < last) : (x += 1) blend(&p[offset + @as(usize, @intCast(x))], v.colour, row_alpha);
                    }
                }
                if (@as(f32, @floatFromInt(first)) > left and first > x0 and first <= x1) blend(
                    &p[offset + @as(usize, @intCast(first - 1))],
                    v.colour,
                    (@as(f32, @floatFromInt(first)) - left) * row_alpha,
                );
                if (@as(f32, @floatFromInt(last)) < right and last >= x0 and last < x1) blend(
                    &p[offset + @as(usize, @intCast(last))],
                    v.colour,
                    (right - @as(f32, @floatFromInt(last))) * row_alpha,
                );
            }
            return;
        }
        if (cmd.kind == .text) {
            const v = cmd.kind.text;
            var pen = cmd.bounds.x;
            var view = std.unicode.Utf8View.init(v.content) catch return;
            var it = view.iterator();
            while (it.nextCodepoint()) |cp| {
                const g = self.glyph(cp, v.size) catch continue;
                const gx: i32 = @intFromFloat(@round(pen + @as(f32, @floatFromInt(g.left))));
                const gy: i32 = @intFromFloat(@round(cmd.bounds.y + self.ascender(v.size) - @as(f32, @floatFromInt(g.top))));
                var y = @max(y0, gy);
                while (y < @min(y1, gy + g.h)) : (y += 1) {
                    var x = @max(x0, gx);
                    while (x < @min(x1, gx + g.w)) : (x += 1) {
                        const alpha = g.pixels[@intCast((y - gy) * g.w + x - gx)];
                        blend(
                            &p[@as(usize, @intCast(y)) * w + @as(usize, @intCast(x))],
                            v.colour,
                            @as(f32, @floatFromInt(alpha)) / 255,
                        );
                    }
                }
                pen += g.advance;
            }
            return;
        }
        var y = y0;
        while (y < y1) : (y += 1) {
            var x = x0;
            while (x < x1) : (x += 1) {
                const fx = @as(f32, @floatFromInt(x)) + 0.5;
                const fy = @as(f32, @floatFromInt(y)) + 0.5;
                var colour: u32 = 0;
                var alpha: f32 = 1;
                switch (cmd.kind) {
                    .rectangle => |v| {
                        colour = v.colour;
                        const r = @min(v.radius, @min(cmd.bounds.width, cmd.bounds.height) * 0.5);
                        if (r > 0) {
                            const dx = @max(@max(cmd.bounds.x + r - fx, fx - (cmd.bounds.x + cmd.bounds.width - r)), 0);
                            const dy = @max(@max(cmd.bounds.y + r - fy, fy - (cmd.bounds.y + cmd.bounds.height - r)), 0);
                            alpha = clamp(r + 0.5 - @sqrt(dx * dx + dy * dy), 0, 1);
                        }
                    },
                    .circle => |v| {
                        colour = v.colour;
                        const dx = fx - v.cx;
                        const dy = fy - v.cy;
                        const d2 = dx * dx + dy * dy;
                        alpha = if (d2 < (v.radius - 0.5) * (v.radius - 0.5)) v.opacity else v.opacity * clamp(v.radius + 0.5 - @sqrt(d2), 0, 1);
                    },
                    .image => |v| {
                        if (v.width == 0 or v.height == 0 or @as(usize, v.width) * v.height > v.pixels.len) continue;
                        const sx: u32 = @intFromFloat(clamp((fx - cmd.bounds.x) / cmd.bounds.width, 0, 0.99999) * @as(f32, @floatFromInt(v.width)));
                        const sy: u32 = @intFromFloat(clamp((fy - cmd.bounds.y) / cmd.bounds.height, 0, 0.99999) * @as(f32, @floatFromInt(v.height)));
                        const src = self.display_colour.xrgb(v.pixels[@as(usize, sy) * v.width + sx]);
                        colour = ((src & 0xffffff) << 8) | 255;
                        alpha = v.opacity;
                    },
                    .text, .line, .polygon, .vertical_gradient => unreachable,
                }
                blend(&p[@as(usize, @intCast(y)) * w + @as(usize, @intCast(x))], colour, alpha);
            }
        }
    }
};

fn rgbaChannels(colour: u32) @Vector(4, f32) {
    return .{
        @floatFromInt(colour >> 24),
        @floatFromInt((colour >> 16) & 255),
        @floatFromInt((colour >> 8) & 255),
        @floatFromInt(colour & 255),
    };
}

fn gradientBlend(dst: *u32, colour: @Vector(4, f32), coverage: f32, x: u32, y: u32, dither: bool) void {
    @setFloatMode(.optimized);
    const alpha = clamp(colour[3] * coverage / 255, 0, 1);
    if (alpha <= 0) return;
    // One final quantization rather than first rounding alpha into 8-bit steps.
    // The small ordered signal is below one output code value and never moves.
    const bayer = [64]u8{ 0, 32, 8, 40, 2, 34, 10, 42, 48, 16, 56, 24, 50, 18, 58, 26, 12, 44, 4, 36, 14, 46, 6, 38, 60, 28, 52, 20, 62, 30, 54, 22, 3, 35, 11, 43, 1, 33, 9, 41, 51, 19, 59, 27, 49, 17, 57, 25, 15, 47, 7, 39, 13, 45, 5, 37, 63, 31, 55, 23, 61, 29, 53, 21 };
    const noise: f32 = if (dither) (@as(f32, @floatFromInt(bayer[(y & 7) * 8 + (x & 7)])) + 0.5) / 64 else 0.5;
    const old: @Vector(4, f32) = .{
        @floatFromInt((dst.* >> 16) & 255),
        @floatFromInt((dst.* >> 8) & 255),
        @floatFromInt(dst.* & 255),
        0,
    };
    const out = old + (colour - old) * @as(@Vector(4, f32), @splat(alpha));
    const red: u32 = @intFromFloat(clamp(@floor(out[0] + noise), 0, 255));
    const green: u32 = @intFromFloat(clamp(@floor(out[1] + noise), 0, 255));
    const blue: u32 = @intFromFloat(clamp(@floor(out[2] + noise), 0, 255));
    dst.* = 0xff000000 | red << 16 | green << 8 | blue;
}

fn blend(dst: *u32, rgba: u32, coverage: f32) void {
    @setFloatMode(.optimized);
    const a: u32 = @intFromFloat(clamp(coverage, 0, 1) * @as(f32, @floatFromInt(rgba & 255)));
    const src = rgba >> 8;
    if (a == 255) {
        dst.* = 0xff000000 | src;
        return;
    }
    if (a == 0) return;
    const inv = 255 - a;
    const rb = (((src & 0xff00ff) * a + (dst.* & 0xff00ff) * inv + 0x800080) >> 8) & 0xff00ff;
    const g = (((src & 0xff00) * a + (dst.* & 0xff00) * inv + 0x8000) >> 8) & 0xff00;
    dst.* = 0xff000000 | rb | g;
}

fn overlaps(a: t.Rect, b: t.Rect) bool {
    return a.x <= b.x + b.width and b.x <= a.x + a.width and a.y <= b.y + b.height and b.y <= a.y + a.height;
}

fn unite(a: t.Rect, b: t.Rect) t.Rect {
    @setFloatMode(.optimized);
    const x = @min(a.x, b.x);
    const y = @min(a.y, b.y);
    return .{
        .x = x,
        .y = y,
        .width = @max(a.x + a.width, b.x + b.width) - x,
        .height = @max(a.y + a.height, b.y + b.height) - y,
    };
}

fn intersect(a: t.Rect, b: t.Rect) t.Rect {
    @setFloatMode(.optimized);
    const x = @max(a.x, b.x);
    const y = @max(a.y, b.y);
    return .{
        .x = x,
        .y = y,
        .width = @max(0, @min(a.x + a.width, b.x + b.width) - x),
        .height = @max(0, @min(a.y + a.height, b.y + b.height) - y),
    };
}

test "per buffer damage restores old bounds and stable scenes are idle" {
    var r = Renderer{
        .allocator = std.testing.allocator,
        .glyphs = std.AutoHashMap(u64, Glyph).init(std.testing.allocator),
    };
    defer {
        r.glyphs.deinit();
        for (&r.history) |*h| h.deinit(r.allocator);
    }
    var pixels: [64]u32 = undefined;
    var commands = [_]t.Command{ .{
        .bounds = .{ .width = 8, .height = 8 },
        .kind = .{ .rectangle = .{ .colour = 0x102030ff } },
    }, .{
        .bounds = .{ .x = 1, .y = 1, .width = 2, .height = 2 },
        .kind = .{ .rectangle = .{ .colour = 0xff0000ff } },
    } };
    _ = try r.render(&pixels, 8, 8, 0, &commands);
    try std.testing.expectEqual(@as(u32, 0xffff0000), pixels[9]);
    try std.testing.expectEqual(@as(usize, 0), (try r.render(&pixels, 8, 8, 0, &commands)).len);
    commands[1].bounds.x = 4;
    _ = try r.render(&pixels, 8, 8, 0, &commands);
    try std.testing.expectEqual(@as(u32, 0xff102030), pixels[9]);
    try std.testing.expectEqual(@as(u32, 0xffff0000), pixels[12]);
    try std.testing.expect((try r.changes(1, 8, 8, &commands)).len > 0);
}

inline fn clamp(value: f32, lower: f32, upper: f32) f32 {
    @setFloatMode(.optimized);
    return if (value < lower) lower else if (value > upper) upper else value;
}

test "group damage and submitted scene stay independent of buffer age" {
    var r = Renderer{
        .allocator = std.testing.allocator,
        .glyphs = std.AutoHashMap(u64, Glyph).init(std.testing.allocator),
    };
    defer {
        r.glyphs.deinit();
        for (&r.history) |*h| h.deinit(r.allocator);
    }
    var commands = [_]t.Command{
        .{
            .group = 7,
            .bounds = .{ .x = 2, .y = 2, .width = 3, .height = 3 },
            .kind = .{ .rectangle = .{ .colour = 0xff0000ff } },
        },
        .{
            .group = 7,
            .bounds = .{ .x = 20, .y = 2, .width = 3, .height = 3 },
            .kind = .{ .rectangle = .{ .colour = 0xff0000ff } },
        },
    };
    _ = try r.changes(0, 32, 16, &commands);
    _ = try r.visible(0, 32, 16);
    commands[0].kind.rectangle.colour = 0x00ff00ff;
    commands[1].kind.rectangle.colour = 0x00ff00ff;
    const grouped = try r.changes(1, 32, 16, &commands);
    try std.testing.expectEqual(@as(usize, 1), grouped.len);
    const submitted = try r.visible(1, 32, 16);
    try std.testing.expectEqual(@as(usize, 1), submitted.len);
    try std.testing.expectEqual(@as(f32, 21), submitted[0].width);
    commands[0].kind.rectangle.colour = 0xff0000ff;
    commands[1].kind.rectangle.colour = 0xff0000ff;
    try std.testing.expectEqual(@as(usize, 0), (try r.changes(0, 32, 16, &commands)).len);
    try std.testing.expectEqual(@as(usize, 1), (try r.visible(0, 32, 16)).len);
    try std.testing.expectEqual(@as(usize, 0), (try r.visible(0, 32, 16)).len);
}

test "display gains affect sources once and reset both retained buffers" {
    const a = std.testing.allocator;
    var warm = Renderer{ .allocator = a, .glyphs = std.AutoHashMap(u64, Glyph).init(a) };
    var reference = Renderer{ .allocator = a, .glyphs = std.AutoHashMap(u64, Glyph).init(a) };
    defer {
        warm.glyphs.deinit();
        reference.glyphs.deinit();
        for (&warm.history) |*h| h.deinit(a);
        for (&reference.history) |*h| h.deinit(a);
    }
    const gains = try display.Colour.parse("1,0.96,0.88");
    warm.setDisplayColour(gains);
    const source = [_]u32{ 0xffffffff, 0xff4455cc, 0xff00ff00, 0xffff0000 };
    var corrected: [4]u32 = undefined;
    for (source, 0..) |pixel, i| corrected[i] = gains.xrgb(pixel);
    var scene = [_]t.Command{
        .{
            .bounds = .{ .width = 16, .height = 12 },
            .kind = .{ .rectangle = .{ .colour = 0xffffffFF } },
        },
        .{
            .bounds = .{ .x = 2, .y = 1, .width = 11, .height = 9 },
            .kind = .{ .rectangle = .{ .colour = 0x3355ee80, .radius = 2 } },
        },
        .{
            .bounds = .{ .x = 4, .y = 3, .width = 6, .height = 6 },
            .kind = .{
                .circle = .{ .cx = 7, .cy = 6, .radius = 3, .colour = 0xee88ddc0, .opacity = 0.7 },
            },
        },
        .{
            .bounds = .{ .x = 5, .y = 5, .width = 7, .height = 5 },
            .kind = .{
                .image = .{ .pixels = &source, .width = 2, .height = 2, .opacity = 0.6 },
            },
        },
    };
    var tinted = scene;
    tinted[0].kind.rectangle.colour = gains.rgba(scene[0].kind.rectangle.colour);
    tinted[1].kind.rectangle.colour = gains.rgba(scene[1].kind.rectangle.colour);
    tinted[2].kind.circle.colour = gains.rgba(scene[2].kind.circle.colour);
    tinted[3].kind.image.pixels = &corrected;
    var buffers: [2][16 * 12]u32 = undefined;
    var expected: [16 * 12]u32 = undefined;
    for (0..2) |slot| {
        _ = try warm.render(&buffers[slot], 16, 12, slot, &scene);
        _ = try reference.render(&expected, 16, 12, 0, &tinted);
        try std.testing.expectEqualSlices(u32, &expected, &buffers[slot]);
        try std.testing.expectEqual(@as(u32, 0xfffff5e0), buffers[slot][0]);
        try std.testing.expectEqual(@as(usize, 0), (try warm.render(&buffers[slot], 16, 12, slot, &scene)).len);
    }
    warm.setDisplayColour(.{});
    reference.setDisplayColour(.{});
    for (0..2) |slot| {
        try std.testing.expect((try warm.render(&buffers[slot], 16, 12, slot, &scene)).len > 0);
        _ = try reference.render(&expected, 16, 12, 0, &scene);
        try std.testing.expectEqualSlices(u32, &expected, &buffers[slot]);
        try std.testing.expectEqual(@as(u32, 0xffffffff), buffers[slot][0]);
    }
}

test "continuous dithered gradient has no strip edges or quantized-alpha bands" {
    const a = std.testing.allocator;
    var r = Renderer{ .allocator = a, .glyphs = std.AutoHashMap(u64, Glyph).init(a) };
    defer {
        r.glyphs.deinit();
        for (&r.history) |*h| h.deinit(a);
    }
    var pixels: [32 * 300]u32 = undefined;
    const scene = [_]t.Command{
        .{
            .bounds = .{ .width = 32, .height = 300 },
            .kind = .{ .rectangle = .{ .colour = 0xffffffff } },
        },
        .{
            .bounds = .{ .width = 32, .height = 300 },
            .kind = .{ .vertical_gradient = .{ .top = 0x00000000, .bottom = 0x00000080 } },
        },
    };
    _ = try r.render(&pixels, 32, 300, 0, &scene);
    var previous: f32 = 255;
    var changed: usize = 0;
    var varied: usize = 0;
    for (0..300) |y| {
        var sum: u32 = 0;
        var low: u32 = 255;
        var high: u32 = 0;
        for (pixels[y * 32 ..][0..32]) |pixel| {
            const level = pixel & 255;
            sum += level;
            low = @min(low, level);
            high = @max(high, level);
            try std.testing.expectEqual(level, (pixel >> 16) & 255);
        }
        const mean = @as(f32, @floatFromInt(sum)) / 32;
        try std.testing.expect(@abs(mean - previous) <= 2);
        const ideal = 255 - 128 * (@as(f32, @floatFromInt(y)) + 0.5) / 300;
        try std.testing.expect(@abs(mean - ideal) <= 1);
        if (mean != previous) changed += 1;
        if (low != high) varied += 1;
        previous = mean;
    }
    try std.testing.expect(changed > 150 and varied > 100 and previous < 129);
}

test "gradient damage replay is pixel-identical across alternating buffers" {
    const a = std.testing.allocator;
    var retained = Renderer{ .allocator = a, .glyphs = std.AutoHashMap(u64, Glyph).init(a) };
    var fresh = Renderer{ .allocator = a, .glyphs = std.AutoHashMap(u64, Glyph).init(a) };
    defer {
        retained.glyphs.deinit();
        fresh.glyphs.deinit();
        for (&retained.history) |*h| h.deinit(a);
        for (&fresh.history) |*h| h.deinit(a);
    }
    var buffers: [2][32 * 24]u32 = undefined;
    var reference: [32 * 24]u32 = undefined;
    var scene = [_]t.Command{
        .{
            .bounds = .{ .width = 32, .height = 24 },
            .kind = .{ .rectangle = .{ .colour = 0xe0c090ff } },
        },
        .{
            .bounds = .{ .x = 1.25, .y = 2.5, .width = 28.5, .height = 20 },
            .kind = .{ .vertical_gradient = .{ .top = 0x11223300, .bottom = 0x334455a0 } },
        },
        .{
            .bounds = .{ .x = 2, .y = 3, .width = 5, .height = 6 },
            .kind = .{ .rectangle = .{ .colour = 0xeeaa00ff } },
        },
    };
    for (0..12) |frame| {
        const slot = frame % 2;
        scene[2].bounds.x = @floatFromInt(2 + frame);
        if (frame == 4) scene[1].kind.vertical_gradient.bottom = 0x556677d0;
        if (frame == 6) scene[1].clip = .{ .x = 4, .y = 5, .width = 20, .height = 15 };
        if (frame == 8) scene[1].bounds.y = 1.75;
        _ = try retained.render(&buffers[slot], 32, 24, slot, &scene);
        for (&fresh.history) |*h| h.clearRetainingCapacity();
        _ = try fresh.render(&reference, 32, 24, 0, &scene);
        try std.testing.expectEqualSlices(u32, &reference, &buffers[slot]);
    }
}
