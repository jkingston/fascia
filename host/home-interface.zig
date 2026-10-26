//! Optional built-in UI contract; the compositor does not import the app SDK.
pub const Input = struct {
    x: f32 = 0,
    y: f32 = 0,
    down: bool = false,
    pressed: bool = false,
    released: bool = false,
};

pub const Rect = struct {
    x: f32,
    y: f32,
    width: f32,
    height: f32,
};

pub const Frame = struct {
    slot: u1,
    serial: u64,
    damage: []const Rect,
};

pub const Actions = struct {
    context: *anyopaque,
    launch: *const fn (*anyopaque, []const u8) bool,
};

pub const UI = struct {
    context: *anyopaque,
    bind: *const fn (*anyopaque, Actions) void,
    paint: *const fn (*anyopaque, u1, []u32, u32, u32, i64) anyerror!?Frame,
    available: *const fn (*anyopaque, u1) bool,
    accept: *const fn (*anyopaque, Frame) anyerror!void,
    cancel: *const fn (*anyopaque, Frame) anyerror!void,
    release: *const fn (*anyopaque, u1, u64) void,
    input: *const fn (*anyopaque, Input, i64) void,
    message: *const fn (*anyopaque, []const u8) void,
    home: *const fn (*anyopaque) void,
    visibility: *const fn (*anyopaque, bool, u64, i64) void,
    wake: *const fn (*anyopaque, i64) ?i64,
    presented: *const fn (*anyopaque, i64) void,
};
