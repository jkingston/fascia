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
    const desktop = b.option(bool, "desktop", "Native desktop app preview") orelse false;
    const ui = b.dependency("native_ui", .{ .target = target, .optimize = optimize, .desktop = desktop });
    if (desktop) return;
    const brightness = b.createModule(.{
        .root_source_file = b.path("tools/fascia-brightness.zig"),
        .target = target,
        .optimize = optimize,
        .link_libc = true,
        .strip = optimize != .Debug,
    });
    b.installArtifact(b.addExecutable(.{ .name = "fascia-brightness", .root_module = brightness }));
    b.getInstallStep().dependOn(
        &b.addInstallFileWithDir(ui.namedLazyPath("freetype-license"), .prefix, "share/licenses/freetype/FTL.TXT").step,
    );
    b.getInstallStep().dependOn(
        &b.addInstallFileWithDir(ui.namedLazyPath("zlib-license"), .prefix, "share/licenses/zlib/LICENSE").step,
    );
    const fonts = b.dependency("fonts", .{});
    b.getInstallStep().dependOn(
        &b.addInstallFileWithDir(fonts.path("usr/share/fonts/TTF/DejaVuSans.ttf"), .prefix, "share/fonts/DejaVuSans.ttf").step,
    );
    b.getInstallStep().dependOn(
        &b.addInstallFileWithDir(fonts.path("usr/share/licenses/ttf-dejavu/LICENSE"), .prefix, "share/licenses/ttf-dejavu/LICENSE").step,
    );
}
