//! Producer-owned CPU buffers. Scene/renderer locks determine when a slot is reusable.
const std = @import("std");
const wlr = @import("wlroots");
const wl = @import("wayland").server.wl;

pub const Buffer = struct {
    base: wlr.Buffer = undefined,
    pixels: []u32,
    slot: u1,
    serial: u64 = 0,
    context: *anyopaque,
    released: *const fn (*anyopaque, u1, u64) void,
    release_listener: wl.Listener(void) = .init(onRelease),

    pub fn create(width: u32, height: u32, slot: u1, context: *anyopaque, released: *const fn (*anyopaque, u1, u64) void) !*Buffer {
        const self = try std.heap.c_allocator.create(Buffer);
        errdefer std.heap.c_allocator.destroy(self);
        self.* = .{
            .pixels = try std.heap.c_allocator.alloc(u32, @as(usize, width) * height),
            .slot = slot,
            .context = context,
            .released = released,
        };
        self.base.init(&implementation, @intCast(width), @intCast(height));
        self.base.events.release.add(&self.release_listener);
        return self;
    }

    fn onRelease(listener: *wl.Listener(void)) void {
        const self: *Buffer = @alignCast(@fieldParentPtr("release_listener", listener));
        self.released(self.context, self.slot, self.serial);
    }

    fn destroy(base: *wlr.Buffer) callconv(.c) void {
        const self: *Buffer = @alignCast(@fieldParentPtr("base", base));
        self.release_listener.link.remove();
        std.heap.c_allocator.free(self.pixels);
        std.heap.c_allocator.destroy(self);
    }

    fn begin(base: *wlr.Buffer, flags: u32, data: **anyopaque, format: *u32, stride: *usize) callconv(.c) bool {
        if (flags & wlr.Buffer.data_ptr_access_flag.write != 0) return false;
        const self: *Buffer = @alignCast(@fieldParentPtr("base", base));
        data.* = self.pixels.ptr;
        format.* = 0x34325258; // DRM_FORMAT_XRGB8888
        stride.* = @as(usize, @intCast(base.width)) * 4;
        return true;
    }

    fn end(_: *wlr.Buffer) callconv(.c) void {}
    const implementation = wlr.Buffer.Impl{ .destroy = destroy, .get_dmabuf = null, .get_shm = null, .begin_data_ptr_access = begin, .end_data_ptr_access = end };
};
