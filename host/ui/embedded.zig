//! Built-in UI surface. The host owns two XRGB buffers.
//! Retain accepted buffers until release(slot, serial). Cancel failed attachments;
//! call forgetBuffer before replacing storage.
const std = @import("std");
const ui = @import("fascia").ui;
const t = @import("fascia").types;
const raster = @import("fascia").Renderer;
const display = @import("fascia").display;

pub const Frame = struct {
    slot: usize,
    serial: u64,
    /// Borrowed until the next prepare; copy/consume when updating the scene.
    damage: []const t.Rect,
};

const Slot = struct {
    pixels: ?[*]u32 = null,
    width: u32 = 0,
    height: u32 = 0,
    serial: u64 = 0,
    in_flight: bool = false,
};

pub const Surface = struct {
    allocator: std.mem.Allocator,
    renderer: raster,
    scratch: std.heap.ArenaAllocator,
    hit_storage: std.ArrayList(ui.Hit) = .empty,
    pending_hits: std.ArrayList(ui.Hit) = .empty,
    slots: [2]Slot = .{ .{}, .{} },
    pending: ?Frame = null,
    serial: u64 = 0,

    pub fn init(a: std.mem.Allocator, font: [:0]const u8) !Surface {
        return .{
            .allocator = a,
            .renderer = try raster.init(a, font),
            .scratch = std.heap.ArenaAllocator.init(a),
        };
    }
    /// The host must destroy/unlock its scene buffers before destroying Surface.
    pub fn deinit(self: *Surface) void {
        std.debug.assert(self.pending == null);
        for (self.slots) |slot| std.debug.assert(!slot.in_flight);
        self.hit_storage.deinit(self.allocator);
        self.pending_hits.deinit(self.allocator);
        self.scratch.deinit();
        self.renderer.deinit();
    }

    pub fn measure(self: *Surface) t.Measure {
        return self.renderer.measure();
    }
    /// Accepted scalar hit rectangles, valid until accept or an unchanged prepare.
    pub fn hits(self: *const Surface) []const ui.Hit {
        return self.hit_storage.items;
    }

    pub fn available(self: *const Surface, slot: usize) bool {
        return slot < 2 and self.pending == null and !self.slots[slot].in_flight;
    }

    pub fn setDisplayColour(self: *Surface, colour: display.Colour) !void {
        if (self.pending != null) return error.FramePending;
        self.renderer.setDisplayColour(colour);
    }

    pub fn forgetBuffer(self: *Surface, slot: usize) !void {
        if (slot >= 2) return error.InvalidSlot;
        if (!self.available(slot)) return error.BufferInFlight;
        self.slots[slot] = .{};
        self.renderer.dimensions[slot] = .{ 0, 0 };
    }

    /// Render synchronously; view memory may be freed on return.
    /// Null means unchanged pixels; hits and buffer history still update.
    pub fn prepare(self: *Surface, slot: usize, pixels: []u32, width: u32, height: u32, node: ui.Node) !?Frame {
        if (slot >= 2) return error.InvalidSlot;
        if (self.pending != null) return error.FramePending;
        if (self.slots[slot].in_flight) return error.BufferInFlight;
        if (width == 0 or height == 0 or pixels.len != @as(u64, width) * height) return error.InvalidBuffer;
        const old = &self.slots[slot];
        if (old.pixels != pixels.ptr or old.width != width or old.height != height) {
            self.renderer.dimensions[slot] = .{ 0, 0 };
            old.pixels = pixels.ptr;
            old.width = width;
            old.height = height;
        }
        _ = self.scratch.reset(.retain_capacity);
        const scene = try ui.build(self.scratch.allocator(), node, .{ .width = @floatFromInt(width), .height = @floatFromInt(height) }, self.measure());
        // Reserve first so an allocation failure cannot expose half-updated hits.
        try self.pending_hits.ensureTotalCapacity(self.allocator, scene.hits.len);
        _ = self.renderer.render(pixels, width, height, slot, scene.commands) catch |err| {
            self.renderer.dimensions[slot] = .{ 0, 0 };
            return err;
        };
        const damage = self.renderer.visible(slot, width, height) catch |err| {
            self.renderer.dimensions[2] = .{ 0, 0 };
            return err;
        };
        self.pending_hits.clearRetainingCapacity();
        self.pending_hits.appendSliceAssumeCapacity(scene.hits);
        if (damage.len == 0) {
            std.mem.swap(std.ArrayList(ui.Hit), &self.hit_storage, &self.pending_hits);
            return null;
        }
        self.serial +%= 1;
        if (self.serial == 0) self.serial = 1;
        const frame = Frame{ .slot = slot, .serial = self.serial, .damage = damage };
        self.pending = frame;
        return frame;
    }

    fn checkPending(self: *const Surface, frame: Frame) !void {
        const pending = self.pending orelse return error.NoPendingFrame;
        if (pending.slot != frame.slot or pending.serial != frame.serial) return error.StaleFrame;
    }
    /// Accept the scene buffer. Release must include its slot and serial to reject stale callbacks.
    pub fn accept(self: *Surface, frame: Frame) !void {
        try self.checkPending(frame);
        self.slots[frame.slot].serial = frame.serial;
        self.slots[frame.slot].in_flight = true;
        std.mem.swap(std.ArrayList(ui.Hit), &self.hit_storage, &self.pending_hits);
        self.pending = null;
    }

    pub fn cancel(self: *Surface, frame: Frame) !void {
        try self.checkPending(frame);
        self.pending = null;
        // visible() tentatively advanced submitted stamps. Force conservative
        // full visible damage next time instead of claiming a failed attachment.
        self.renderer.dimensions[2] = .{ 0, 0 };
    }
    /// Returns false for a stale/duplicate callback, without changing ownership.
    pub fn release(self: *Surface, slot: usize, serial: u64) bool {
        if (slot >= 2 or !self.slots[slot].in_flight or self.slots[slot].serial != serial) return false;
        self.slots[slot].in_flight = false;
        return true;
    }
};
