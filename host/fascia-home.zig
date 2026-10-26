//! Native Home adapter. The compositor core only sees home-interface.zig.
const std = @import("std");
const native = @import("native_ui");
const api = @import("home-interface.zig");
const c = @cImport({
    @cInclude("stdlib.h");
});

pub const Home = struct {
    allocator: std.mem.Allocator,
    controller: native.Controller,
    surface: native.Surface,
    arena: std.heap.ArenaAllocator,
    actions: ?api.Actions = null,
    pending: ?native.Frame = null,
    damage: [128]api.Rect = undefined,
    dirty: bool = true,
    invalidate_hits: bool = false,
    visible: bool = true,
    colour: native.Colour,
    last_input: native.types.Input = .{},

    pub fn init(a: std.mem.Allocator, display_maintenance: bool) !Home {
        const font: [:0]const u8 = if (c.getenv("FASCIA_FONT")) |raw| std.mem.span(raw) else "/usr/share/fonts/TTF/DejaVuSans.ttf";
        const apps = if (c.getenv("FASCIA_APPS_ROOT")) |raw| std.mem.span(raw) else "/var/lib/fascia/apps";
        const autostart = if (c.getenv("FASCIA_AUTOSTART")) |raw| !std.mem.eql(u8, std.mem.span(raw), "0") else true;
        var controller = try native.Controller.init(a, .{ .apps_root = apps, .autostart = autostart, .display_maintenance = display_maintenance });
        errdefer controller.deinit();
        var surface = try native.Surface.init(a, font);
        errdefer surface.deinit();
        const colour = controller.displayColour();
        try surface.setDisplayColour(colour);
        return .{
            .allocator = a,
            .controller = controller,
            .surface = surface,
            .arena = std.heap.ArenaAllocator.init(a),
            .colour = colour,
        };
    }

    pub fn deinit(self: *Home) void {
        if (self.pending) |frame| self.surface.cancel(frame) catch {};
        self.surface.deinit();
        self.controller.deinit();
        self.arena.deinit();
    }

    fn from(context: *anyopaque) *Home {
        return @ptrCast(@alignCast(context));
    }

    fn launch(context: ?*anyopaque, id: []const u8) bool {
        const self = from(context orelse return false);
        const actions = self.actions orelse return false;
        return actions.launch(actions.context, id);
    }

    fn bind(context: *anyopaque, actions: api.Actions) void {
        const self = from(context);
        self.actions = actions;
        self.controller.options.host = .{ .context = self, .launch = launch };
    }

    fn available(context: *anyopaque, slot: u1) bool {
        return from(context).surface.available(slot);
    }

    fn paint(context: *anyopaque, slot: u1, pixels: []u32, width: u32, height: u32, now: i64) !?api.Frame {
        const self = from(context);
        self.invalidate_hits = (try self.controller.update(now, .{ .x = self.last_input.x, .y = self.last_input.y, .down = self.last_input.down }, if (self.invalidate_hits) &.{} else self.surface.hits())) or self.invalidate_hits;
        if (!self.visible) return null;
        const colour = self.controller.displayColour();
        if (!std.meta.eql(colour.gains, self.colour.gains)) {
            try self.surface.setDisplayColour(colour);
            self.colour = colour;
        }
        _ = self.arena.reset(.retain_capacity);
        const node = try self.controller.view(self.arena.allocator(), @floatFromInt(width), @floatFromInt(height), now, self.surface.measure());
        const prepared = try self.surface.prepare(slot, pixels, width, height, node);
        self.dirty = false;
        self.invalidate_hits = false;
        const frame = prepared orelse return null;
        self.pending = frame;
        std.debug.assert(frame.damage.len <= self.damage.len);
        for (frame.damage, self.damage[0..frame.damage.len]) |r, *out| out.* = .{ .x = r.x, .y = r.y, .width = r.width, .height = r.height };
        return .{
            .slot = @intCast(frame.slot),
            .serial = frame.serial,
            .damage = self.damage[0..frame.damage.len],
        };
    }

    fn accept(context: *anyopaque, frame: api.Frame) !void {
        const self = from(context);
        const prepared = self.pending orelse return error.NoPendingFrame;
        if (prepared.slot != frame.slot or prepared.serial != frame.serial) return error.StaleFrame;
        try self.surface.accept(prepared);
        self.pending = null;
    }

    fn cancel(context: *anyopaque, frame: api.Frame) !void {
        const self = from(context);
        const prepared = self.pending orelse return error.NoPendingFrame;
        if (prepared.slot != frame.slot or prepared.serial != frame.serial) return error.StaleFrame;
        try self.surface.cancel(prepared);
        self.pending = null;
        self.invalidate_hits = true;
        self.dirty = true;
    }

    fn release(context: *anyopaque, slot: u1, serial: u64) void {
        _ = from(context).surface.release(slot, serial);
    }

    fn input(context: *anyopaque, event: api.Input, now: i64) void {
        const self = from(context);
        self.last_input = .{
            .x = event.x,
            .y = event.y,
            .down = event.down,
            .pressed = event.pressed,
            .released = event.released,
        };
        self.invalidate_hits = (self.controller.update(now, self.last_input, if (self.invalidate_hits) &.{} else self.surface.hits()) catch |err| {
            std.log.err("Home input: {s}", .{@errorName(err)});
            return;
        }) or self.invalidate_hits;
        self.dirty = true;
    }

    fn visibility(context: *anyopaque, visible: bool, generation: u64, _: i64) void {
        const self = from(context);
        self.controller.setVisibility(visible, generation);
        self.visible = visible;
        self.last_input = .{};
        self.invalidate_hits = true;
        self.dirty = true;
    }

    fn wake(context: *anyopaque, now: i64) ?i64 {
        const self = from(context);
        return if (self.visible and self.dirty) now else self.controller.nextWake(now);
    }

    fn presented(context: *anyopaque, now: i64) void {
        from(context).controller.presented(now);
    }

    fn home(context: *anyopaque) void {
        const self = from(context);
        self.controller.home();
        self.last_input = .{};
        self.invalidate_hits = true;
        self.dirty = true;
    }

    fn message(context: *anyopaque, text: []const u8) void {
        const self = from(context);
        self.controller.model.message = text;
        self.dirty = true;
    }

    pub fn interface(self: *Home) api.UI {
        return .{
            .context = self,
            .message = message,
            .bind = bind,
            .paint = paint,
            .available = available,
            .accept = accept,
            .cancel = cancel,
            .release = release,
            .input = input,
            .visibility = visibility,
            .wake = wake,
            .presented = presented,
            .home = home,
        };
    }
};
