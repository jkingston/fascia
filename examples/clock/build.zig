const std = @import("std");

fn codec(b: *std.Build, module: *std.Build.Module, stb: *std.Build.Dependency) void {
    module.addIncludePath(stb.path(""));
    const source = b.addWriteFiles().add(
        "jpeg.c",
        "#define STB_IMAGE_IMPLEMENTATION\n#define STBI_ONLY_JPEG\n#define STBI_ONLY_PNG\n#define STBI_MAX_DIMENSIONS 4096\n#define STBI_NO_STDIO\n#include \"stb_image.h\"\n",
    );
    module.addCSourceFile(.{ .file = source, .flags = &.{"-O2"} });
    const clock_source = b.addWriteFiles().add(
        "clock-time.c",
        "#ifdef __linux__\n#include <sys/timex.h>\nint fascia_clock_synced(void) { struct timex t = {0}; return adjtimex(&t) >= 0 && !(t.status & STA_UNSYNC); }\n#else\nint fascia_clock_synced(void) { return 0; }\n#endif\n",
    );
    module.addCSourceFile(.{ .file = clock_source, .flags = &.{"-O2"} });
}

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
    const stb = b.dependency("stb", .{});
    const module = b.createModule(.{
        .root_source_file = b.path("main.zig"),
        .target = target,
        .optimize = optimize,
        .link_libc = true,
        .strip = optimize != .Debug,
    });
    module.addImport("fascia", fascia.module("fascia"));
    codec(b, module, stb);
    const preview = @import("fascia").addPreview(b, "clock", b.path("main.zig"), optimize);
    codec(b, preview, stb);
    @import("fascia").installApp(b, "clock", module);
    b.getInstallStep().dependOn(&b.addInstallFileWithDir(stb.path("LICENSE"), .prefix, "clock.AppDir/licenses/stb.txt").step);
    const test_module = b.createModule(.{
        .root_source_file = b.path("tests.zig"),
        .target = b.graph.host,
        .optimize = optimize,
        .link_libc = true,
    });
    codec(b, test_module, stb);
    b.step("test", "Run bounded feed, codec, cache and clock tests").dependOn(&b.addRunArtifact(b.addTest(.{ .root_module = test_module })).step);
    const check_module = b.createModule(.{
        .root_source_file = b.path("check.zig"),
        .target = b.graph.host,
        .optimize = optimize,
        .link_libc = true,
    });
    codec(b, check_module, stb);
    b.step("check-feed", "Explicit online smoke check; set FASCIA_CLOCK_CHECK_STATE to a scratch directory").dependOn(
        &b.addRunArtifact(b.addExecutable(.{ .name = "clock-feed-check", .root_module = check_module })).step,
    );
}
