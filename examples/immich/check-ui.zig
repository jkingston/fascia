const std = @import("std");
const fascia = @import("fascia");
const App = @import("main.zig").App;
const worker = @import("worker.zig");
const model = @import("model.zig");
const fs = @import("io.zig");
const c = @cImport({
    @cInclude("stdlib.h");
    @cInclude("unistd.h");
    @cInclude("stdio.h");
});

pub fn main() !void {
    const root = if (c.getenv("XDG_CONFIG_HOME")) |value| std.mem.span(value) else return error.SetXdgConfigHome;
    var arena = std.heap.ArenaAllocator.init(std.heap.c_allocator);
    const a = arena.allocator();
    const settings = (try std.json.parseFromSlice(model.Settings, a, try fs.read(a, try std.fmt.allocPrint(a, "{s}/settings.json", .{root}), 32768), .{ .allocate = .alloc_always, .ignore_unknown_fields = true })).value;
    const background = try worker.Worker.initWithNetwork(root, try std.fmt.allocPrint(a, "{s}/cache", .{root}), settings, c.getenv("IMMICH_CHECK_OFFLINE") == null);
    defer background.deinit();
    var app = App{ .arena = arena, .background = background, .settings = settings, .paused = true };
    defer app.arena.deinit();
    defer if (app.snapshot) |snapshot| snapshot.destroy();
    for (0..3000) |_| {
        if (background.take()) |snapshot| {
            app.snapshot = snapshot;
            break;
        }
        _ = c.usleep(20_000);
    }
    var snapshot = app.snapshot orelse return error.WorkerTimeout;
    if (c.getenv("IMMICH_CHECK_OFFLINE") != null and !snapshot.offline) return error.OfflineTest;
    if (snapshot.images[0] == null) {
        std.debug.print("Worker: {s}\n", .{snapshot.message});
        return error.NoPhoto;
    }
    if (settings.mode == .then_now and snapshot.images[1] == null) return error.NoPair;
    var renderer = try fascia.Renderer.init(a, try a.dupeZ(u8, @import("check_options").font_path));
    defer renderer.deinit();
    var scene_arena = std.heap.ArenaAllocator.init(std.heap.c_allocator);
    defer scene_arena.deinit();
    const pixels = try a.alloc(u32, 1280 * 800);
    const ctx = fascia.Context{ .time_seconds = 1, .dt = 0, .width = 1280, .height = 800, .measure = renderer.measure() };
    inline for (.{ .photos, .settings, .people, .albums, .display, .connection }) |page| {
        _ = scene_arena.reset(.retain_capacity);
        const sa = scene_arena.allocator();
        app.page = page;
        if (page == .people) {
            background.portraits(0);
            for (0..3000) |_| {
                if (background.take()) |loaded| {
                    snapshot.destroy();
                    snapshot = loaded;
                    app.snapshot = loaded;
                    break;
                }
                _ = c.usleep(20_000);
            }
            if (c.getenv("IMMICH_CHECK_OFFLINE") == null and snapshot.portraits[0] == null) return error.NoPortrait;
        }
        const scene = try fascia.ui.build(sa, try app.view(sa, ctx), .{ .width = 1280, .height = 800 }, renderer.measure());
        for (scene.hits) |hit| {
            if (hit.bounds.x < 0 or hit.bounds.y < 0 or hit.bounds.x + hit.bounds.width > 1280 or hit.bounds.y + hit.bounds.height > 800) return error.ControlOutsideScreen;
        }
        _ = try renderer.render(pixels, 1280, 800, 0, scene.commands);
        try fs.atomic(a, try std.fmt.allocPrint(a, "{s}/{s}.png", .{ root, @tagName(page) }), try fascia.screenshot.png(a, pixels, 1280, 800));
        if (page == .photos and settings.mode == .then_now) {
            app.pair_later = true;
            const now_scene = try fascia.ui.build(sa, try app.view(sa, ctx), .{ .width = 1280, .height = 800 }, renderer.measure());
            _ = try renderer.render(pixels, 1280, 800, 0, now_scene.commands);
            try fs.atomic(a, try std.fmt.allocPrint(a, "{s}/now.png", .{root}), try fascia.screenshot.png(a, pixels, 1280, 800));
            app.pair_later = false;
        }
        if (page == .display) {
            try tap(&app, scene.hits, 44, ctx);
            if (app.settings.interval != 1800) return error.IntervalButton;
            app.settings.interval = settings.interval;
        }
        if (page == .people) {
            const before = app.settings.match;
            try tap(&app, scene.hits, if (before == .any) 37 else 36, ctx);
            if (app.settings.match == before) return error.MatchButton;
            try tap(&app, scene.hits, 12, ctx);
            if (app.settings.allow_others == settings.allow_others) return error.OnlyButton;
            if (snapshot.people.len > 0) {
                const initial = app.settings.include.len;
                try tap(&app, scene.hits, 100, ctx);
                if (app.settings.include.len == initial) return error.IncludeButton;
                app.excluding = true;
                try tap(&app, scene.hits, 100, ctx);
                app.excluding = false;
                if (app.settings.exclude.len == settings.exclude.len) return error.ExcludeButton;
            }
        }
    }
    app.page = .photos;
    app.paused = false;
    app.controls_until = 0;
    app.update(.{ .x = 640, .y = 400, .down = true, .pressed = true }, &.{}, ctx);
    app.update(.{ .x = 640, .y = 400, .released = true }, &.{}, ctx);
    if (!app.paused or app.controls_until <= ctx.time_seconds) return error.CentreTap;
    const controls = try fascia.ui.build(scene_arena.allocator(), try app.view(scene_arena.allocator(), ctx), .{ .width = 1280, .height = 800 }, renderer.measure());
    try tap(&app, controls.hits, 3, ctx);
    if (app.paused or app.controls_until != 0) return error.Resume;
    if (app.snapshot) |current| {
        const saved = current.images[1];
        current.images[1] = current.images[0];
        app.pair_later = false;
        try tap(&app, controls.hits, 3, ctx);
        const duration: f64 = @floatFromInt(app.settings.interval);
        if (app.pair_switch != ctx.time_seconds + duration or app.next_photo != ctx.time_seconds + 2 * duration) return error.PairInterval;
        current.images[1] = saved;
    }
    app.update(.{ .x = 1200, .y = 400, .down = true, .pressed = true }, &.{}, ctx);
    app.update(.{ .x = 1200, .y = 400, .released = true }, &.{}, ctx);
    if (!app.paused) return error.EdgeTap;
    _ = c.printf("Rendered all six screens. People controls, edge taps and resume passed.\n");
}

fn tap(app: *App, hits: []const fascia.ui.Hit, action: u32, ctx: fascia.Context) !void {
    for (hits) |hit| if (hit.action == action) {
        const x = hit.bounds.x + hit.bounds.width / 2;
        const y = hit.bounds.y + hit.bounds.height / 2;
        app.update(.{ .x = x, .y = y, .down = true, .pressed = true }, hits, ctx);
        app.update(.{ .x = x, .y = y, .released = true }, hits, ctx);
        return;
    };
    return error.MissingControl;
}
