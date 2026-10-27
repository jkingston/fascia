//! One-shot early framebuffer screen; no compositor, GPU or background process.
const std = @import("std");
const c = @cImport({
    @cUndef("_FORTIFY_SOURCE");
    @cDefine("_FORTIFY_SOURCE", "0");
    @cInclude("fcntl.h");
    @cInclude("unistd.h");
    @cInclude("sys/ioctl.h");
    @cInclude("stdio.h");
});
const font = @embedFile("boot/font.bin");
const width = 1280;
const height = 800;
var pixels: [width * height]u8 = undefined;

const Fixed = extern struct {
    id: [16]u8,
    start: usize,
    length: u32,
    kind: u32,
    auxiliary: u32,
    visual: u32,
    xpan: u16,
    ypan: u16,
    ywrap: u16,
    stride: u32,
    mmio: usize,
    mmio_length: u32,
    accelerator: u32,
    capabilities: u16,
    reserved: [2]u16,
};
const Stage = struct { name: []const u8, message: []const u8, step: usize };
const stages = [_]Stage{
    .{ .name = "start", .message = "Starting up", .step = 0 },
    .{ .name = "firmware", .message = "Preparing this device", .step = 1 },
    .{ .name = "storage", .message = "Preparing storage", .step = 2 },
    .{ .name = "apps", .message = "Installing included apps", .step = 3 },
    .{ .name = "ready", .message = "Opening setup", .step = 4 },
    .{ .name = "error", .message = "Setup could not finish", .step = 0 },
};

fn stageFor(name: []const u8) Stage {
    for (stages) |stage| if (std.mem.eql(u8, stage.name, name)) return stage;
    return stages[0];
}

fn rectangle(x: usize, y: usize, w: usize, h: usize, value: u8) void {
    for (y..@min(y + h, height)) |row| @memset(pixels[row * width + x .. row * width + @min(x + w, width)], value);
}

fn text(value: []const u8, y: usize, scale: usize, colour: u8) void {
    var total: usize = 0;
    for (value) |ch| total += font[ch - 32] * scale;
    var x = (width -| total) / 2;
    for (value) |ch| {
        const index: usize = ch - 32;
        for (0..48) |row| for (0..40) |column| {
            const alpha: u32 = font[95 + index * 40 * 48 + row * 40 + column];
            if (alpha == 0) continue;
            const shade: u8 = @intCast(18 + (alpha * (colour - 18)) / 255);
            rectangle(x + column * scale, y + row * scale, scale, scale, shade);
        };
        x += font[index] * scale;
    }
}

fn render(stage: Stage, first: bool) void {
    @memset(&pixels, 18);
    text("Fascia", 232, 2, 242);
    text(if (first or std.mem.eql(u8, stage.name, "error")) stage.message else "Starting up", 384, 1, 190);
    if (first and !std.mem.eql(u8, stage.name, "error")) {
        for (0..4) |i| rectangle(476 + i * 84, 480, 76, 4, if (i < stage.step) 220 else 55);
        text("Keep the power connected", 558, 1, 125);
    } else if (std.mem.eql(u8, stage.name, "error")) {
        text("Connect USB to view the startup log", 480, 1, 155);
    }
}

fn field(bytes: *const [160]u8, offset: usize) u32 {
    return std.mem.readInt(u32, bytes[offset..][0..4], .little);
}

fn channel(value: u8, offset: u32, bits: u32) u32 {
    if (bits == 0 or bits > 8 or offset > 31 or bits + offset > 32) return 0;
    return (@as(u32, value) >> @as(u5, @intCast(8 - bits))) << @as(u5, @intCast(offset));
}

fn framebuffer() void {
    const fd = c.open("/dev/fb0", c.O_RDWR | c.O_CLOEXEC);
    if (fd < 0) return;
    defer _ = c.close(fd);
    var fixed: Fixed = undefined;
    var variable: [160]u8 = @splat(0);
    if (c.ioctl(fd, @as(c_ulong, 0x4602), &fixed) != 0 or c.ioctl(fd, @as(c_ulong, 0x4600), &variable) != 0) return;
    const w = field(&variable, 0);
    const h = field(&variable, 4);
    const bpp = field(&variable, 24);
    if (w != height or h != width or (bpp != 32 and bpp != 16)) return;
    const bytes = bpp / 8;
    const xoff = field(&variable, 16);
    const yoff = field(&variable, 20);
    if ((@as(u64, xoff) + w) * bytes > fixed.stride or (@as(u64, yoff) + h) * fixed.stride > fixed.length) return;
    var row: [height * 4]u8 = undefined;
    for (0..h) |py| {
        for (0..w) |px| {
            // Match Home's WL_OUTPUT_TRANSFORM_270 on the portrait panel.
            const shade = pixels[px * width + (width - 1 - py)];
            const value = channel(shade, field(&variable, 32), field(&variable, 36)) |
                channel(shade, field(&variable, 44), field(&variable, 48)) |
                channel(shade, field(&variable, 56), field(&variable, 60)) |
                channel(255, field(&variable, 68), field(&variable, 72));
            for (0..bytes) |byte| row[px * bytes + byte] = @truncate(value >> @as(u5, @intCast(byte * 8)));
        }
        const offset = (@as(u64, yoff) + py) * fixed.stride + @as(u64, xoff) * bytes;
        if (c.pwrite(fd, &row, w * bytes, @intCast(offset)) != w * bytes) return;
    }
}

pub fn main(init: std.process.Init.Minimal) void {
    const args = init.args.vector;
    const argc = args.len;
    const name = if (argc > 1) std.mem.span(args[1]) else "start";
    const preview = argc > 2 and std.mem.eql(u8, std.mem.span(args[2]), "--preview");
    const first = preview or c.access("/run/fascia-first-boot", c.F_OK) == 0;
    if (!first and !std.mem.eql(u8, name, "start") and !std.mem.eql(u8, name, "error")) return;
    render(stageFor(name), first);
    if (preview) {
        _ = c.printf("P5\n1280 800\n255\n");
        _ = c.fflush(null);
        _ = c.write(1, &pixels, pixels.len);
    } else framebuffer();
}

test "stages render visible text and distinct progress" {
    render(stageFor("firmware"), true);
    try std.testing.expectEqual(@as(u8, 220), pixels[480 * width + 476]);
    try std.testing.expectEqual(@as(u8, 55), pixels[480 * width + 560]);
    render(stageFor("ready"), true);
    try std.testing.expectEqual(@as(u8, 220), pixels[480 * width + 728]);
    try std.testing.expectEqual(@as(u32, 0xf800), channel(255, 11, 5));
}
