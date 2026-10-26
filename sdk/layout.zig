//! Layout tree and commands share a per-frame arena.
const std = @import("std");
pub const t = @import("types.zig");

pub const Size = union(enum) { fit, grow, fixed: f32 };

pub const Align = enum { start, center, end };

pub const Style = struct {
    id: u64 = 0,
    group: u64 = 0,
    position: ?t.Rect = null,
    width: Size = .grow,
    height: Size = .fit,
    padding: f32 = 0,
    gap: f32 = 0,
    alignment: Align = .start,
    justify: Align = .start,
    background: u32 = 0,
    radius: f32 = 0,
    action: u32 = 0,
    drag: bool = false,
};

pub const Node = struct {
    kind: enum { row, column, stack, text, canvas } = .column,
    style: Style = .{},
    children: []const Node = &.{},
    content: []const u8 = "",
    font_size: f32 = 16,
    colour: u32 = 0xffffffff,
    paint: []const t.Command = &.{},
    view_width: f32 = 1,
    view_height: f32 = 1,
};

fn container(a: std.mem.Allocator, kind: @FieldType(Node, "kind"), style: Style, children: []const Node) !Node {
    return .{ .kind = kind, .style = style, .children = try a.dupe(Node, children) };
}

pub fn row(a: std.mem.Allocator, s: Style, children: []const Node) !Node {
    return container(a, .row, s, children);
}

pub fn column(a: std.mem.Allocator, s: Style, children: []const Node) !Node {
    return container(a, .column, s, children);
}

pub fn stack(a: std.mem.Allocator, s: Style, children: []const Node) !Node {
    return container(a, .stack, s, children);
}

pub fn text(content: []const u8, size: f32, colour: u32) Node {
    return .{
        .kind = .text,
        .style = .{ .width = .fit },
        .content = content,
        .font_size = size,
        .colour = colour,
    };
}

pub fn canvas(s: Style, width: f32, height: f32, paint: []const t.Command) Node {
    return .{
        .kind = .canvas,
        .style = s,
        .view_width = width,
        .view_height = height,
        .paint = paint,
    };
}

pub fn interactive(node: Node, id: u64, action: u32, drag: bool) Node {
    var n = node;
    n.style.id = id;
    n.style.action = action;
    n.style.drag = drag;
    return n;
}

pub const Hit = struct {
    bounds: t.Rect,
    id: u64,
    action: u32,
    drag: bool,
};

pub const Scene = struct {
    commands: []const t.Command,
    hits: []const Hit,
};

fn resolve(size: Size, intrinsic: f32, available: f32) f32 {
    return @max(0, switch (size) {
        .fit => intrinsic,
        .grow => available,
        .fixed => |v| v,
    });
}

fn natural(n: Node, m: t.Measure) t.Metrics {
    var w: f32 = 0;
    var h: f32 = 0;
    switch (n.kind) {
        .text => {
            const q = m.text(n.content, n.font_size);
            w = q.width;
            h = q.height;
        },
        .canvas => {
            w = n.view_width;
            h = n.view_height;
        },
        else => for (n.children, 0..) |c, i| {
            const q = natural(c, m);
            const cw = resolve(c.style.width, q.width, 0);
            const ch = resolve(c.style.height, q.height, 0);
            if (n.kind == .row) {
                w += cw + (if (i > 0) n.style.gap else 0);
                h = @max(h, ch);
            } else if (n.kind == .column) {
                h += ch + (if (i > 0) n.style.gap else 0);
                w = @max(w, cw);
            } else {
                w = @max(w, cw);
                h = @max(h, ch);
            }
        },
    }
    return .{ .width = w + n.style.padding * 2, .height = h + n.style.padding * 2 };
}

fn offset(a: Align, room: f32) f32 {
    return @max(0, room) * switch (a) {
        .start => @as(f32, 0),
        .center => 0.5,
        .end => 1,
    };
}

const Builder = struct {
    allocator: std.mem.Allocator,
    measure: t.Measure,
    commands: std.ArrayList(t.Command) = .empty,
    hits: std.ArrayList(Hit) = .empty,
    serial: u64 = 0,

    fn emit(b: *Builder, cmd: t.Command, group: u64) !void {
        var c = cmd;
        b.serial += 1;
        if (c.id == 0) c.id = b.serial;
        if (c.group == 0) c.group = group;
        try b.commands.append(b.allocator, c);
    }

    fn layout(b: *Builder, n: Node, r: t.Rect, inherited: u64) anyerror!void {
        const group = if (n.style.group != 0) n.style.group else inherited;
        if (n.style.background & 255 != 0) try b.emit(.{
            .bounds = r,
            .kind = .{
                .rectangle = .{ .colour = n.style.background, .radius = n.style.radius },
            },
        }, group);
        if (n.style.action != 0) try b.hits.append(b.allocator, .{
            .bounds = r,
            .id = n.style.id,
            .action = n.style.action,
            .drag = n.style.drag,
        });
        if (n.kind == .text) {
            try b.emit(.{
                .bounds = r,
                .kind = .{
                    .text = .{ .content = n.content, .size = n.font_size, .colour = n.colour },
                },
            }, group);
            return;
        }
        if (n.kind == .canvas) {
            const sx = r.width / @max(1, n.view_width);
            const sy = r.height / @max(1, n.view_height);
            for (n.paint) |p| {
                var c = p;
                c.bounds = .{
                    .x = r.x + p.bounds.x * sx,
                    .y = r.y + p.bounds.y * sy,
                    .width = p.bounds.width * sx,
                    .height = p.bounds.height * sy,
                };
                if (p.clip) |clip| c.clip = .{
                    .x = r.x + clip.x * sx,
                    .y = r.y + clip.y * sy,
                    .width = clip.width * sx,
                    .height = clip.height * sy,
                };
                switch (c.kind) {
                    .polygon => |*v| {
                        const points = try b.allocator.dupe(t.Point, v.points);
                        for (points) |*point| {
                            point.x = r.x + point.x * sx;
                            point.y = r.y + point.y * sy;
                        }
                        v.points = points;
                    },
                    .line => |*v| {
                        v.x1 = r.x + v.x1 * sx;
                        v.y1 = r.y + v.y1 * sy;
                        v.x2 = r.x + v.x2 * sx;
                        v.y2 = r.y + v.y2 * sy;
                        v.width *= @min(sx, sy);
                    },
                    .circle => |*v| {
                        v.cx = r.x + v.cx * sx;
                        v.cy = r.y + v.cy * sy;
                        v.radius *= @min(sx, sy);
                    },
                    .text => |*v| v.size *= @min(sx, sy),
                    .rectangle => |*v| v.radius *= @min(sx, sy),
                    else => {},
                }
                try b.emit(c, group);
            }
            return;
        }
        const p = n.style.padding;
        const inner = t.Rect{
            .x = r.x + p,
            .y = r.y + p,
            .width = @max(0, r.width - 2 * p),
            .height = @max(0, r.height - 2 * p),
        };
        const vertical = n.kind == .column;
        const main = if (vertical) inner.height else inner.width;
        var used: f32 = if (n.children.len > 0 and n.kind != .stack) n.style.gap * @as(f32, @floatFromInt(n.children.len - 1)) else 0;
        var growers: f32 = 0;
        for (n.children) |c| {
            const q = natural(c, b.measure);
            const size = if (vertical) c.style.height else c.style.width;
            if (size == .grow) growers += 1 else used += resolve(size, if (vertical) q.height else q.width, main);
        }
        const share = if (growers > 0) @max(0, main - used) / growers else 0;
        var cursor = offset(n.style.justify, main - used);
        if (growers > 0) cursor = 0;
        for (n.children) |c| {
            const q = natural(c, b.measure);
            const cw = resolve(c.style.width, q.width, if (vertical or n.kind == .stack) inner.width else share);
            const ch = resolve(c.style.height, q.height, if (!vertical or n.kind == .stack) inner.height else share);
            var cr = t.Rect{ .x = inner.x, .y = inner.y, .width = cw, .height = ch };
            if (n.kind == .stack) {
                cr.x += offset(n.style.alignment, inner.width - cw);
                cr.y += offset(n.style.justify, inner.height - ch);
            } else if (vertical) {
                cr.x += offset(n.style.alignment, inner.width - cw);
                cr.y += cursor;
                cursor += ch + n.style.gap;
            } else {
                cr.y += offset(n.style.alignment, inner.height - ch);
                cr.x += cursor;
                cursor += cw + n.style.gap;
            }
            if (c.style.position) |position| cr = .{
                .x = inner.x + position.x,
                .y = inner.y + position.y,
                .width = position.width,
                .height = position.height,
            };
            try b.layout(c, cr, group);
        }
    }
};

pub fn build(a: std.mem.Allocator, root: Node, viewport: t.Rect, measure: t.Measure) !Scene {
    var b = Builder{ .allocator = a, .measure = measure };
    try b.layout(root, viewport, 0);
    return .{
        .commands = try b.commands.toOwnedSlice(a),
        .hits = try b.hits.toOwnedSlice(a),
    };
}

fn contains(r: t.Rect, x: f32, y: f32) bool {
    return x >= r.x and y >= r.y and x < r.x + r.width and y < r.y + r.height;
}

pub const Interaction = struct {
    capture: ?Hit = null,

    pub fn update(self: *Interaction, hits: []const Hit, input: t.Input) ?t.Event {
        if (input.pressed) {
            self.capture = null;
            var i = hits.len;
            while (i > 0) {
                i -= 1;
                if (contains(hits[i].bounds, input.x, input.y)) {
                    self.capture = hits[i];
                    break;
                }
            }
        }
        if (self.capture) |captured| {
            if (!captured.drag) {
                self.capture = null;
                for (hits) |current| if (current.id == captured.id and current.action == captured.action) {
                    self.capture = current;
                    break;
                };
            }
        }
        if (self.capture) |hit| {
            if (input.released) {
                self.capture = null;
                if (hit.drag or contains(hit.bounds, input.x, input.y)) return .{
                    .id = hit.id,
                    .action = hit.action,
                    .value = std.math.clamp((input.x - hit.bounds.x) / @max(1, hit.bounds.width), 0, 1),
                };
            } else if (hit.drag and (input.down or input.pressed)) return .{
                .id = hit.id,
                .action = hit.action,
                .value = std.math.clamp((input.x - hit.bounds.x) / @max(1, hit.bounds.width), 0, 1),
            };
        }
        return null;
    }
};

fn testMeasure(_: ?*anyopaque, content: []const u8, size: f32) t.Metrics {
    return .{
        .width = @as(f32, @floatFromInt(content.len)) * size / 2,
        .height = size,
    };
}

test "native layout distributes grow and measures fit" {
    var arena = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena.deinit();
    const a = arena.allocator();
    const root = try row(
        a,
        .{ .gap = 10 },
        &.{
            text("abc", 20, 0xffffffff),
            try column(a, .{ .height = .grow, .background = 0x123456ff }, &.{}),
        },
    );
    const scene = try build(a, root, .{ .width = 200, .height = 100 }, .{ .function = testMeasure });
    try std.testing.expectEqual(@as(usize, 2), scene.commands.len);
    try std.testing.expectEqual(@as(f32, 40), scene.commands[1].bounds.x);
    try std.testing.expectEqual(@as(f32, 160), scene.commands[1].bounds.width);
}

test "buttons release inside and drag capture clamps outside" {
    var interaction = Interaction{};
    const hits = [_]Hit{.{
        .bounds = .{ .width = 100, .height = 40 },
        .id = 1,
        .action = 2,
        .drag = false,
    }};
    try std.testing.expect(interaction.update(&hits, .{ .x = 20, .y = 20, .pressed = true, .down = true }) == null);
    try std.testing.expect(interaction.update(&hits, .{ .x = 120, .y = 20, .released = true }) == null);
    var drag = hits;
    drag[0].drag = true;
    try std.testing.expectEqual(
        @as(f32, 0.2),
        interaction.update(&drag, .{ .x = 20, .y = 20, .pressed = true, .down = true }).?.value,
    );
    try std.testing.expectEqual(@as(f32, 1), interaction.update(&drag, .{ .x = 200, .y = 20, .released = true }).?.value);
}

test "captured button disappearing cancels and relocated button uses current geometry" {
    var interaction = Interaction{};
    var hits = [_]Hit{.{
        .bounds = .{ .width = 100, .height = 40 },
        .id = 42,
        .action = 1,
        .drag = false,
    }};
    _ = interaction.update(&hits, .{ .x = 10, .y = 10, .down = true, .pressed = true });
    try std.testing.expect(interaction.update(&.{}, .{ .x = 10, .y = 10, .released = true }) == null);
    _ = interaction.update(&hits, .{ .x = 10, .y = 10, .down = true, .pressed = true });
    hits[0].bounds.x = 200;
    try std.testing.expect(interaction.update(&hits, .{ .x = 10, .y = 10, .released = true }) == null);
}

test "canvas local clipping follows translated scaled paint" {
    var arena = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena.deinit();
    const a = arena.allocator();
    const paint = [_]t.Command{.{
        .bounds = .{ .width = 20, .height = 20 },
        .clip = .{ .x = 2, .y = 3, .width = 10, .height = 12 },
        .kind = .{ .rectangle = .{ .colour = 0xffffffff } },
    }};
    const scene = try build(
        a,
        canvas(.{}, 100, 100, &paint),
        .{ .x = 100, .y = 50, .width = 200, .height = 200 },
        .{ .function = testMeasure },
    );
    const clip = scene.commands[0].clip.?;
    try std.testing.expectEqual(@as(f32, 104), clip.x);
    try std.testing.expectEqual(@as(f32, 56), clip.y);
    try std.testing.expectEqual(@as(f32, 20), clip.width);
}
