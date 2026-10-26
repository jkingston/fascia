//! Native preview transport; app layout/rendering stays identical to the frame.
const std = @import("std");
const t = @import("types.zig");
const Viewport = @import("desktop-geometry.zig").Viewport;
const c = @cImport({
    @cInclude("MiniFB.h");
    @cInclude("poll.h");
    @cInclude("time.h");
    @cInclude("stdlib.h");
});
const a = std.heap.c_allocator;

pub const Buffer = struct {
    pixels: []u32 = &.{},
};
pub var buffers: [2]Buffer = .{ .{}, .{} };
pub var width: u32 = 1280;
pub var height: u32 = 800;
pub var running = true;
pub var input = t.Input{};
pub var wake_ready = false;
var window: ?*c.struct_mfb_window = null;
var viewport = Viewport.fit(1280, 800);
var last_slot: ?usize = null;
var capture_requested = false;
var capture_path: ?[:0]const u8 = null;
var capture_deadline: i64 = 0;

pub fn init() !void {
    running = true;
    errdefer deinit();
    for (&buffers) |*buffer| buffer.pixels = try a.alloc(u32, 1280 * 800);
    // Offscreen preview uses the same update/view/render loop for CI and docs.
    capture_path = if (c.getenv("FASCIA_PREVIEW_CAPTURE")) |raw| std.mem.span(raw) else null;
    if (capture_path != null) {
        capture_deadline = now() + 250_000;
        return;
    }
    var sx: f32 = 1;
    var sy: f32 = 1;
    c.mfb_get_monitor_scale(null, &sx, &sy);
    window = c.mfb_open_ex("Fascia preview - F12 saves PNG", @intFromFloat(1280 / @max(1, sx)), @intFromFloat(800 / @max(1, sy)), c.MFB_WF_RESIZABLE);
    if (window == null) return error.DesktopWindow;
    c.mfb_set_mouse_move_callback(window, motion);
    c.mfb_set_mouse_button_callback(window, button);
    c.mfb_set_keyboard_callback(window, key);
    c.mfb_set_active_callback(window, active);
    viewport = .{ .x = 0, .y = 0, .width = 0, .height = 0 };
    updateViewport();
}

pub fn deinit() void {
    if (window) |w| {
        c.mfb_close(w);
        _ = c.mfb_update_events(w);
        window = null;
    }
    for (&buffers) |*buffer| {
        a.free(buffer.pixels);
        buffer.* = .{};
    }
    last_slot = null;
    running = false;
}

fn alive(state: c.mfb_update_state) bool {
    if (state == c.MFB_STATE_OK) return true;
    // MiniFB destroys the window before returning EXIT.
    if (state == c.MFB_STATE_EXIT) window = null;
    running = false;
    return false;
}

fn updateViewport() void {
    const next = Viewport.fit(c.mfb_get_window_width(window), c.mfb_get_window_height(window));
    if (std.meta.eql(next, viewport)) return;
    viewport = next;
    if (next.width == 0 or next.height == 0) return;
    _ = c.mfb_set_viewport(window, next.x, next.y, next.width, next.height);
    if (last_slot) |slot| _ = alive(c.mfb_update_ex(window, buffers[slot].pixels.ptr, width, height));
}

fn motion(_: ?*c.struct_mfb_window, x: c_int, y: c_int) callconv(.c) void {
    if (viewport.point(x, y)) |point| {
        input.x = point.x;
        input.y = point.y;
    } else cancelTouch();
}

fn button(w: ?*c.struct_mfb_window, which: c.mfb_mouse_button, _: c.mfb_key_mod, down: bool) callconv(.c) void {
    if (which != c.MFB_MOUSE_BTN_1) return;
    if (viewport.point(c.mfb_get_mouse_x(w), c.mfb_get_mouse_y(w))) |point| {
        input.x = point.x;
        input.y = point.y;
        input.pressed = input.pressed or (down and !input.down);
        input.released = input.released or (!down and input.down);
        input.down = down;
    } else cancelTouch();
}

fn cancelTouch() void {
    // An abandoned drag must not activate a control at the last in-bounds point.
    if (input.down) {
        input.x = -1;
        input.y = -1;
        input.down = false;
        input.released = true;
    }
}

fn active(_: ?*c.struct_mfb_window, focused: bool) callconv(.c) void {
    if (!focused) cancelTouch();
}

fn key(_: ?*c.struct_mfb_window, code: c.mfb_key, modifiers: c.mfb_key_mod, down: bool) callconv(.c) void {
    if (!down) return;
    if (code == c.MFB_KB_KEY_F12 or (code == c.MFB_KB_KEY_S and modifiers & (c.MFB_KB_MOD_CONTROL | c.MFB_KB_MOD_SUPER) != 0)) capture_requested = true;
    if (code == c.MFB_KB_KEY_ESCAPE) running = false;
}

fn capture() void {
    capture_requested = false;
    const slot = last_slot orelse return;
    const root = if (c.getenv("XDG_STATE_HOME")) |raw| std.mem.span(raw) else ".local/preview";
    const path = if (capture_path) |value| a.dupeZ(u8, value) catch return else std.fmt.allocPrintSentinel(a, "{s}/screenshot.png", .{root}, 0) catch return;
    defer a.free(path);
    const bytes = @import("screenshot.zig").png(a, buffers[slot].pixels, width, height) catch |err| {
        std.log.err("Screenshot: {s}", .{@errorName(err)});
        return;
    };
    defer a.free(bytes);
    @import("persist.zig").replace(path, bytes) catch |err| {
        std.log.err("Screenshot: {s}", .{@errorName(err)});
        return;
    };
    std.log.info("Screenshot saved: {s}", .{path});
}

pub fn acquire() !?usize {
    return if (last_slot) |slot| 1 - slot else 0;
}

pub fn present(slot: usize, _: []const t.Rect) void {
    last_slot = slot;
    if (window != null) _ = alive(c.mfb_update_ex(window, buffers[slot].pixels.ptr, width, height));
}

pub fn pump(timeout_ms: i32) bool {
    return pumpWithWake(timeout_ms, null);
}

pub fn pumpWithWake(timeout_ms: i32, wake_fd: ?i32) bool {
    if (!running) return false;
    if (window != null) {
        if (!alive(c.mfb_update_events(window))) return false;
        updateViewport();
    }
    if (capture_path != null and last_slot != null and now() >= capture_deadline) {
        capture();
        running = false;
    } else if (capture_requested) capture();
    if (!running) return false;
    // Native events and worker pipes are checked at least every 16ms, without
    // repainting an idle app or adding a worker thread to its model.
    const timeout = if (input.pressed or input.released) 0 else if (timeout_ms < 0) 16 else @min(16, timeout_ms);
    var fd = c.struct_pollfd{ .fd = wake_fd orelse -1, .events = c.POLLIN, .revents = 0 };
    _ = c.poll(&fd, 1, timeout);
    if (fd.revents != 0) wake_ready = true;
    return running;
}

pub fn now() i64 {
    var value: c.timespec = undefined;
    _ = c.clock_gettime(c.CLOCK_MONOTONIC, &value);
    return @as(i64, value.tv_sec) * 1000000 + @divTrunc(value.tv_nsec, 1000);
}

pub fn consumeInput() t.Input {
    const result = input;
    input.pressed = false;
    input.released = false;
    return result;
}
