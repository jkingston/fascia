//! App event loop. Views use a per-frame arena.
const std = @import("std");
const p = if (@import("fascia_options").desktop) @import("desktop.zig") else @import("platform.zig");
const ui = @import("layout.zig");
const t = @import("types.zig");
const schedule = @import("runtime-schedule.zig");
const Renderer = @import("renderer.zig").Renderer;
const c = @cImport({
    @cInclude("stdlib.h");
});

pub const Context = struct {
    time_seconds: f64,
    dt: f32,
    width: f32,
    height: f32,
    measure: t.Measure,
};

/// Model implements update(Input, []const ui.Hit, Context), view(Allocator,
/// Context)!ui.Node and optionally desiredFps()u32. Caller owns init/deinit.
/// Optional nextWake(Context)?f64 overrides periodic FPS: absolute monotonic
/// seconds (Context.time_seconds), or null to wait only for input/worker/resize.
/// Optional wakeFd()?i32 supplies an app-owned nonblocking notification pipe;
/// publish worker results safely, write a byte, then drain/take results in update.
/// Never let a worker mutate the scene/model while view/render reads it.
pub fn run(app: anytype) !void {
    const App = @typeInfo(@TypeOf(app)).pointer.child;
    const a = std.heap.c_allocator;
    const font: [:0]const u8 = if (c.getenv("FASCIA_FONT")) |value| std.mem.span(value) else @import("fascia_options").font_path ++ "";
    var renderer = try Renderer.init(a, font);
    defer renderer.deinit();
    try p.init();
    defer if (@hasDecl(p, "deinit")) p.deinit();
    var arena = std.heap.ArenaAllocator.init(a);
    defer arena.deinit();
    var hits: []const ui.Hit = &.{};
    var previous = p.now();
    var deadline: ?i64 = previous;
    var rendered_width: u32 = 0;
    var rendered_height: u32 = 0;
    var last_x: f32 = 0;
    var last_y: f32 = 0;
    while (p.running) {
        const wake_fd: ?i32 = if (@hasDecl(App, "wakeFd")) app.wakeFd() else null;
        if (!p.pumpWithWake(0, wake_fd)) break;
        const now = p.now();
        const moved = p.input.down and (p.input.x != last_x or p.input.y != last_y);
        if (!schedule.due(
            now,
            deadline,
            p.input.pressed or p.input.released or moved,
            p.wake_ready,
            p.width != rendered_width or p.height != rendered_height,
        )) {
            if (!p.pumpWithWake(schedule.timeout(now, deadline), wake_fd)) break;
            continue;
        }
        const slot = (try p.acquire()) orelse {
            if (!p.pumpWithWake(2, wake_fd)) break;
            continue;
        };
        const ctx = Context{
            .time_seconds = @as(f64, @floatFromInt(now)) / 1000000,
            .dt = @min(0.1, @as(f32, @floatFromInt(now - previous)) / 1000000),
            .width = @floatFromInt(p.width),
            .height = @floatFromInt(p.height),
            .measure = renderer.measure(),
        };
        previous = now;
        last_x = p.input.x;
        last_y = p.input.y;
        p.wake_ready = false;
        app.update(p.consumeInput(), hits, ctx);
        _ = arena.reset(.retain_capacity);
        const view = try app.view(arena.allocator(), ctx);
        const scene = try ui.build(arena.allocator(), view, .{ .width = ctx.width, .height = ctx.height }, renderer.measure());
        hits = scene.hits;
        _ = try renderer.render(p.buffers[slot].pixels, p.width, p.height, slot, scene.commands);
        const damage = try renderer.visible(slot, p.width, p.height);
        // Skip unchanged scenes. Buffer history retains values, not arena pointers.
        if (damage.len != 0) p.present(slot, damage);
        rendered_width = p.width;
        rendered_height = p.height;
        const fps: u32 = if (@hasDecl(App, "desiredFps")) std.math.clamp(app.desiredFps(), 1, 60) else 1;
        deadline = if (@hasDecl(App, "nextWake")) schedule.deadline(app.nextWake(ctx), now) else now + @divTrunc(@as(i64, 1000000), @as(i64, fps));
    }
}
