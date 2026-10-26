const std = @import("std");
const t = @import("types.zig");
pub const c = @cImport({
    @cDefine("_GNU_SOURCE", "1");
    @cInclude("wayland-client.h");
    @cInclude("xdg-shell-client-protocol.h");
    @cInclude("sys/mman.h");
    @cInclude("unistd.h");
    @cInclude("poll.h");
    @cInclude("time.h");
    @cInclude("errno.h");
});

pub const Buffer = struct {
    wl: ?*c.wl_buffer = null,
    pixels: []u32 = &.{},
    busy: bool = false,
    width: u32 = 0,
    height: u32 = 0,
};
var display: ?*c.wl_display = null;
var compositor: ?*c.wl_compositor = null;
var shm: ?*c.wl_shm = null;
var wm: ?*c.xdg_wm_base = null;
var surface: ?*c.wl_surface = null;
var seat: ?*c.wl_seat = null;
pub var width: u32 = 1280;
pub var height: u32 = 800;
pub var running = true;
var configured = false;
pub var input = t.Input{};
var touch_id: i32 = -1;
pub var buffers: [2]Buffer = .{ .{}, .{} };

fn ping(_: ?*anyopaque, w: ?*c.xdg_wm_base, s: u32) callconv(.c) void {
    c.xdg_wm_base_pong(w, s);
}
const wm_listener = c.xdg_wm_base_listener{ .ping = ping };

fn configure(_: ?*anyopaque, s: ?*c.xdg_surface, serial: u32) callconv(.c) void {
    c.xdg_surface_ack_configure(s, serial);
    configured = true;
}
const surface_listener = c.xdg_surface_listener{ .configure = configure };

fn topConfigure(_: ?*anyopaque, _: ?*c.xdg_toplevel, w: i32, h: i32, _: ?*c.wl_array) callconv(.c) void {
    if (w > 0 and h > 0 and w <= 4096 and h <= 4096) {
        width = @intCast(w);
        height = @intCast(h);
    }
}

fn close(_: ?*anyopaque, _: ?*c.xdg_toplevel) callconv(.c) void {
    running = false;
}
const top_listener = c.xdg_toplevel_listener{ .configure = topConfigure, .close = close };

fn down(_: ?*anyopaque, _: ?*c.wl_touch, _: u32, _: u32, s: ?*c.wl_surface, id: i32, x: i32, y: i32) callconv(.c) void {
    if (s != surface or touch_id != -1) return;
    touch_id = id;
    input.x = @as(f32, @floatFromInt(x)) / 256;
    input.y = @as(f32, @floatFromInt(y)) / 256;
    input.down = true;
    input.pressed = true;
}

fn up(_: ?*anyopaque, _: ?*c.wl_touch, _: u32, _: u32, id: i32) callconv(.c) void {
    if (id == touch_id) {
        touch_id = -1;
        input.down = false;
        input.released = true;
    }
}

fn motion(_: ?*anyopaque, _: ?*c.wl_touch, _: u32, id: i32, x: i32, y: i32) callconv(.c) void {
    if (id == touch_id) {
        input.x = @as(f32, @floatFromInt(x)) / 256;
        input.y = @as(f32, @floatFromInt(y)) / 256;
    }
}

fn touchFrame(_: ?*anyopaque, _: ?*c.wl_touch) callconv(.c) void {}

fn cancel(_: ?*anyopaque, _: ?*c.wl_touch) callconv(.c) void {
    touch_id = -1;
    input.down = false;
    input.released = true;
}
const touch_listener = c.wl_touch_listener{
    .down = down,
    .up = up,
    .motion = motion,
    .frame = touchFrame,
    .cancel = cancel,
};

fn enter(_: ?*anyopaque, p: ?*c.wl_pointer, s: u32, _: ?*c.wl_surface, x: i32, y: i32) callconv(.c) void {
    c.wl_pointer_set_cursor(p, s, null, 0, 0);
    input.x = @as(f32, @floatFromInt(x)) / 256;
    input.y = @as(f32, @floatFromInt(y)) / 256;
}

fn leave(_: ?*anyopaque, _: ?*c.wl_pointer, _: u32, _: ?*c.wl_surface) callconv(.c) void {
    input.down = false;
    input.released = true;
}

fn pointerMotion(_: ?*anyopaque, _: ?*c.wl_pointer, _: u32, x: i32, y: i32) callconv(.c) void {
    input.x = @as(f32, @floatFromInt(x)) / 256;
    input.y = @as(f32, @floatFromInt(y)) / 256;
}

fn button(_: ?*anyopaque, _: ?*c.wl_pointer, _: u32, _: u32, b: u32, state: u32) callconv(.c) void {
    if (b == 0x110) {
        input.down = state == 1;
        input.pressed = input.pressed or input.down;
        input.released = input.released or !input.down;
    }
}

fn axis(_: ?*anyopaque, _: ?*c.wl_pointer, _: u32, _: u32, _: i32) callconv(.c) void {}
const pointer_listener = c.wl_pointer_listener{
    .enter = enter,
    .leave = leave,
    .motion = pointerMotion,
    .button = button,
    .axis = axis,
};

fn capabilities(_: ?*anyopaque, s: ?*c.wl_seat, caps: u32) callconv(.c) void {
    if (caps & c.WL_SEAT_CAPABILITY_TOUCH != 0) {
        const touch = c.wl_seat_get_touch(s);
        _ = c.wl_touch_add_listener(touch, &touch_listener, null);
    }
    if (caps & c.WL_SEAT_CAPABILITY_POINTER != 0) {
        const pointer = c.wl_seat_get_pointer(s);
        _ = c.wl_pointer_add_listener(pointer, &pointer_listener, null);
    }
}
const seat_listener = c.wl_seat_listener{ .capabilities = capabilities };

fn global(_: ?*anyopaque, r: ?*c.wl_registry, name: u32, iface: [*c]const u8, version: u32) callconv(.c) void {
    const s = std.mem.span(iface);
    if (std.mem.eql(u8, s, "wl_compositor")) compositor = @ptrCast(c.wl_registry_bind(r, name, &c.wl_compositor_interface, @min(4, version))) else if (std.mem.eql(u8, s, "wl_shm")) shm = @ptrCast(c.wl_registry_bind(r, name, &c.wl_shm_interface, 1)) else if (std.mem.eql(u8, s, "xdg_wm_base")) {
        wm = @ptrCast(c.wl_registry_bind(r, name, &c.xdg_wm_base_interface, 1));
        _ = c.xdg_wm_base_add_listener(wm, &wm_listener, null);
    } else if (std.mem.eql(u8, s, "wl_seat")) {
        seat = @ptrCast(c.wl_registry_bind(r, name, &c.wl_seat_interface, 1));
        _ = c.wl_seat_add_listener(seat, &seat_listener, null);
    }
}

fn remove(_: ?*anyopaque, _: ?*c.wl_registry, _: u32) callconv(.c) void {}
const registry_listener = c.wl_registry_listener{ .global = global, .global_remove = remove };

fn release(data: ?*anyopaque, _: ?*c.wl_buffer) callconv(.c) void {
    const b: *Buffer = @ptrCast(@alignCast(data.?));
    b.busy = false;
}
const buffer_listener = c.wl_buffer_listener{ .release = release };

pub fn init() !void {
    display = c.wl_display_connect(null);
    if (display == null) return error.WaylandConnect;
    const registry = c.wl_display_get_registry(display);
    _ = c.wl_registry_add_listener(registry, &registry_listener, null);
    if (c.wl_display_roundtrip(display) < 0 or compositor == null or shm == null or wm == null) return error.WaylandGlobals;
    surface = c.wl_compositor_create_surface(compositor);
    const xdg = c.xdg_wm_base_get_xdg_surface(wm, surface);
    _ = c.xdg_surface_add_listener(xdg, &surface_listener, null);
    const top = c.xdg_surface_get_toplevel(xdg);
    _ = c.xdg_toplevel_add_listener(top, &top_listener, null);
    c.xdg_toplevel_set_title(top, "Fascia / native Zig");
    c.xdg_toplevel_set_app_id(top, "fascia-native");
    c.xdg_toplevel_set_fullscreen(top, null);
    c.wl_surface_commit(surface);
    while (!configured) if (c.wl_display_dispatch(display) < 0) return error.WaylandConfigure;
}

pub fn acquire() !?usize {
    for (&buffers, 0..) |*b, i| {
        if (b.busy) continue;
        if (b.width != width or b.height != height) {
            if (b.wl != null) {
                c.wl_buffer_destroy(b.wl);
                _ = c.munmap(b.pixels.ptr, b.pixels.len * 4);
            }
            const bytes: usize = @as(usize, width) * height * 4;
            const fd = c.memfd_create("fascia-native", c.MFD_CLOEXEC);
            if (fd < 0) return error.SharedMemory;
            defer _ = c.close(fd);
            if (c.ftruncate(fd, @intCast(bytes)) != 0) return error.SharedMemory;
            const mapped = c.mmap(null, bytes, c.PROT_READ | c.PROT_WRITE, c.MAP_SHARED, fd, 0);
            if (mapped == c.MAP_FAILED) return error.SharedMemory;
            b.pixels = @as([*]u32, @ptrCast(@alignCast(mapped.?)))[0 .. bytes / 4];
            const pool = c.wl_shm_create_pool(shm, fd, @intCast(bytes));
            b.wl = c.wl_shm_pool_create_buffer(pool, 0, @intCast(width), @intCast(height), @intCast(width * 4), c.WL_SHM_FORMAT_XRGB8888);
            c.wl_shm_pool_destroy(pool);
            b.width = width;
            b.height = height;
            _ = c.wl_buffer_add_listener(b.wl, &buffer_listener, b);
        }
        return i;
    }
    return null;
}

pub fn present(index: usize, damage: []const t.Rect) void {
    const b = &buffers[index];
    b.busy = true;
    c.wl_surface_attach(surface, b.wl, 0, 0);
    for (damage) |r| c.wl_surface_damage(
        surface,
        @intFromFloat(@floor(r.x)),
        @intFromFloat(@floor(r.y)),
        @intFromFloat(@ceil(r.width) + 1),
        @intFromFloat(@ceil(r.height) + 1),
    );
    c.wl_surface_commit(surface);
    _ = c.wl_display_flush(display);
}

/// App-owned nonblocking notification descriptor, polled with Wayland. The app
/// drains it in update; it must remain valid until run returns.
pub var wake_ready = false;

pub fn pump(timeout_ms: i32) bool {
    return pumpWithWake(timeout_ms, null);
}

pub fn pumpWithWake(timeout_ms: i32, wake_fd: ?i32) bool {
    const old_input = input;
    const old_width = width;
    const old_height = height;
    while (c.wl_display_prepare_read(display) != 0) if (c.wl_display_dispatch_pending(display) < 0) return false;
    const flushed = c.wl_display_flush(display);
    const blocked = flushed < 0 and c.__errno_location().* == c.EAGAIN;
    if (flushed < 0 and !blocked) {
        c.wl_display_cancel_read(display);
        return false;
    }
    // Dispatching queued events above may already have delivered input/configure.
    const pending = !running or
        input.pressed != old_input.pressed or
        input.released != old_input.released or
        (input.down and
            (input.x != old_input.x or
                input.y != old_input.y)) or
        width != old_width or
        height != old_height;
    const ready = @import("runtime-wake.zig").wait(c.wl_display_get_fd(display), blocked, wake_fd, if (pending) 0 else timeout_ms) catch {
        c.wl_display_cancel_read(display);
        return false;
    };
    if (ready.display) {
        if (c.wl_display_read_events(display) < 0) return false;
    } else c.wl_display_cancel_read(display);
    if (ready.failed) return false;
    if (ready.worker) wake_ready = true;
    if (c.wl_display_dispatch_pending(display) < 0) return false;
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
