//! UI and renderer types. Colours are straight RGBA (0xRRGGBBAA).
pub const Rect = struct {
    x: f32 = 0,
    y: f32 = 0,
    width: f32 = 0,
    height: f32 = 0,
};

pub const Metrics = struct {
    width: f32,
    height: f32,
    ascender: f32 = 0,
};

pub const Measure = struct {
    context: ?*anyopaque = null,
    function: *const fn (?*anyopaque, []const u8, f32) Metrics,

    pub fn text(self: Measure, content: []const u8, size: f32) Metrics {
        return self.function(self.context, content, size);
    }
};

pub const Command = struct {
    id: u64 = 0,
    group: u64 = 0,
    bounds: Rect,
    clip: ?Rect = null,
    kind: union(enum) {
        rectangle: struct { colour: u32, radius: f32 = 0 },
        // Straight-RGBA interpolation; stable dither on the composited colour.
        vertical_gradient: struct { top: u32, bottom: u32, dither: bool = true },
        circle: struct { cx: f32, cy: f32, radius: f32, colour: u32, opacity: f32 = 1 },
        line: struct { x1: f32, y1: f32, x2: f32, y2: f32, width: f32, colour: u32 },
        // Convex polygon. Vertex storage lives until the current render ends.
        polygon: struct { points: []const Point, colour: u32 },
        text: struct { content: []const u8, size: f32, colour: u32 },
        image: struct {
            // Native XRGB8888 words (0xffRRGGBB), tightly packed.
            pixels: []const u32,
            width: u32,
            height: u32,
            opacity: f32 = 1,
            // Immutable asset fingerprint; zero asks the renderer to hash pixels.
            content_hash: u64 = 0,
        },
    },
};

pub const Input = struct {
    x: f32 = 0,
    y: f32 = 0,
    down: bool = false,
    pressed: bool = false,
    released: bool = false,
};

pub const Event = struct {
    action: u32,
    id: u64,
    value: f32 = 0,
};

pub const Point = struct {
    x: f32,
    y: f32,
};
