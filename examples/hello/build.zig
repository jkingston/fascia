const std = @import("std");

pub fn build(b: *std.Build) void {
    const target = b.standardTargetOptions(.{
        .default_target = .{
            .cpu_arch = .arm,
            .cpu_model = .{ .explicit = &std.Target.arm.cpu.cortex_a7 },
            .os_tag = .linux,
            .abi = .gnueabihf,
            .glibc_version = .{ .major = 2, .minor = 43, .patch = 0 },
        },
    });
    const optimize = b.standardOptimizeOption(.{});
    const fascia = b.dependency("fascia", .{ .target = target, .optimize = optimize });
    const module = b.createModule(.{
        .root_source_file = b.path("main.zig"),
        .target = target,
        .optimize = optimize,
        .strip = optimize != .Debug,
    });
    module.addImport("fascia", fascia.module("fascia"));
    @import("fascia").installApp(b, "counter", module);
    _ = @import("fascia").addPreview(b, "counter", b.path("main.zig"), optimize);
}
