const std = @import("std");

pub fn build(b: *std.Build) void {
    const target = b.resolveTargetQuery(.{
        .cpu_arch = .arm,
        .cpu_model = .{ .explicit = &std.Target.arm.cpu.cortex_a7 },
        .os_tag = .linux,
        .abi = .gnueabihf,
    });
    const protocol = b.dependency("protocol", .{});
    const wayland = protocol.builder.dependency("wayland_linux", .{});
    const module = b.createModule(.{ .target = target, .optimize = .ReleaseSmall, .link_libc = true });
    module.addCSourceFile(.{ .file = b.path("main.c"), .flags = &.{ "-Wall", "-Wextra", "-Werror" } });
    module.addCSourceFile(.{ .file = protocol.path("protocol/xdg-shell-protocol.c") });
    module.addIncludePath(protocol.path("protocol"));
    module.addIncludePath(wayland.path("usr/include"));
    module.addLibraryPath(wayland.path("usr/lib"));
    module.linkSystemLibrary("wayland-client", .{ .use_pkg_config = .no });
    const executable = b.addExecutable(.{ .name = "AppRun", .root_module = module });
    b.getInstallStep().dependOn(&b.addInstallArtifact(executable, .{ .dest_dir = .{ .override = .{ .custom = "compat-c.AppDir" } } }).step);
    for ([_][]const u8{ "compat-c.desktop", "compat-c.png", ".DirIcon" }) |name|
        b.installFile(name, b.fmt("compat-c.AppDir/{s}", .{name}));
}
