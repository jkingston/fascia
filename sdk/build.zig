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
    const embedded = b.option(bool, "embedded", "Renderer without a client backend") orelse false;
    const wayland = b.dependency("wayland_linux", .{});
    const freetype = b.dependency("freetype", .{ .target = target, .optimize = optimize });
    b.addNamedLazyPath("freetype-license", freetype.builder.dependency("freetype", .{}).path("docs/FTL.TXT"));
    b.addNamedLazyPath(
        "zlib-license",
        freetype.builder.dependency("zlib", .{ .target = target, .optimize = optimize }).builder.dependency("zlib", .{}).path("LICENSE"),
    );
    const sdk = b.addModule("fascia", .{
        .root_source_file = b.path("sdk.zig"),
        .target = target,
        .optimize = optimize,
        .link_libc = true,
    });
    const options = b.addOptions();
    options.addOption(bool, "desktop", desktop);
    options.addOption([]const u8, "font_path", if (desktop)
        b.dependency("test_fonts", .{}).path("usr/share/fonts/TTF/DejaVuSans.ttf").getPath(b)
    else
        "/usr/share/fonts/TTF/DejaVuSans.ttf");
    sdk.addOptions("fascia_options", options);
    if (desktop) {
        sdk.linkLibrary(freetype.artifact("freetype"));
        @import("desktop-build.zig").configure(b, sdk, target);
        return;
    }
    if (embedded) sdk.linkLibrary(freetype.artifact("freetype")) else configure(sdk, b, wayland, freetype);
    const host_ft = b.dependency("freetype", .{ .target = b.graph.host, .optimize = optimize });
    const fonts = b.dependency("test_fonts", .{});
    const assets = b.addOptions();
    assets.addOption([]const u8, "font_path", fonts.path("usr/share/fonts/TTF/DejaVuSans.ttf").getPath(b));
    const test_step = b.step("test", "Run native renderer, layout, persistence and Home tests");
    inline for (.{
        "renderer_validation.zig",
        "runtime-tests.zig",
        "desktop-geometry.zig",
        "screenshot.zig",
        "keyboard-tests.zig",
    }) |source| {
        const module = b.createModule(.{
            .root_source_file = b.path(source),
            .target = b.graph.host,
            .optimize = optimize,
            .link_libc = true,
        });
        if (std.mem.eql(u8, source, "renderer_validation.zig")) {
            module.linkLibrary(host_ft.artifact("freetype"));
            module.addOptions("test_assets", assets);
        }
        test_step.dependOn(&b.addRunArtifact(b.addTest(.{ .root_module = module })).step);
    }
}

fn configure(module: *std.Build.Module, b: *std.Build, wayland: *std.Build.Dependency, ft: *std.Build.Dependency) void {
    module.addIncludePath(wayland.path("usr/include"));
    module.addLibraryPath(wayland.path("usr/lib"));
    module.linkSystemLibrary("wayland-client", .{ .use_pkg_config = .no });
    module.linkLibrary(ft.artifact("freetype"));
    module.addIncludePath(b.path("protocol"));
    module.addCSourceFile(.{ .file = b.path("protocol/xdg-shell-protocol.c"), .flags = &.{"-O2"} });
}

/// Add native preview/run steps alongside an app's ordinary ARM build.
pub fn addPreview(b: *std.Build, id: []const u8, source: std.Build.LazyPath, optimize: std.builtin.OptimizeMode) *std.Build.Module {
    const mode = if (optimize == .Debug) .ReleaseFast else optimize;
    const sdk = b.dependency("fascia", .{ .target = b.graph.host, .optimize = mode, .desktop = true });
    const module = b.createModule(.{ .root_source_file = source, .target = b.graph.host, .optimize = mode, .link_libc = true });
    module.addImport("fascia", sdk.module("fascia"));
    const executable = b.addExecutable(.{ .name = id, .root_module = module });
    const macos = b.graph.host.result.os.tag == .macos;
    const destination = if (macos) b.fmt("preview/{s}.app/Contents/MacOS", .{id}) else "preview";
    const install = b.addInstallArtifact(executable, .{ .dest_dir = .{ .override = .{ .custom = destination } } });
    const preview = b.step("preview", "Build desktop preview without opening a window");
    preview.dependOn(&install.step);
    const run = if (macos) b.addSystemCommand(&.{b.getInstallPath(.prefix, b.fmt("{s}/{s}", .{ destination, id }))}) else b.addRunArtifact(executable);
    if (macos) {
        const info = b.addWriteFiles().add("Info.plist", b.fmt(
            \\<?xml version="1.0" encoding="UTF-8"?>
            \\<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
            \\<plist version="1.0"><dict>
            \\<key>CFBundleIdentifier</key><string>dev.fascia.preview.{s}</string>
            \\<key>CFBundleName</key><string>{s}</string>
            \\<key>CFBundleExecutable</key><string>{s}</string>
            \\<key>CFBundlePackageType</key><string>APPL</string>
            \\<key>NSHighResolutionCapable</key><true/>
            \\</dict></plist>
        , .{ id, id, id }));
        const plist = b.addInstallFileWithDir(info, .prefix, b.fmt("preview/{s}.app/Contents/Info.plist", .{id}));
        preview.dependOn(&plist.step);
        run.step.dependOn(&plist.step);
        run.step.dependOn(&install.step);
    }
    for ([_][]const u8{ "CONFIG", "DATA", "STATE", "CACHE" }, [_][]const u8{ "config", "data", "state", "cache" }) |kind, directory|
        run.setEnvironmentVariable(b.fmt("XDG_{s}_HOME", .{kind}), b.pathFromRoot(b.fmt(".local/preview/{s}", .{directory})));
    run.setEnvironmentVariable("FASCIA_DISPLAY_GAINS", "1,1,1");
    if (b.args) |args| run.addArgs(args);
    b.step("run", "Run this app in a desktop window").dependOn(&run.step);
    const supervisor = b.addExecutable(.{ .name = "fascia-dev", .root_module = b.createModule(.{
        .root_source_file = sdk.path("dev.zig"),
        .target = b.graph.host,
        .optimize = .ReleaseFast,
        .link_libc = true,
    }) });
    const dev = b.addRunArtifact(supervisor);
    dev.addArgs(&.{
        b.graph.zig_exe,
        b.pathFromRoot("."),
        b.getInstallPath(.prefix, ""),
        b.fmt("-Doptimize={s}", .{@tagName(mode)}),
        sdk.path("").getPath(b),
        sdk.path("build.zig").getPath(b),
    });
    if (b.args) |args| dev.addArgs(args);
    b.step("dev", "Watch edits, rebuild and restart the desktop preview").dependOn(&dev.step);
    return module;
}

/// Build a complete portable directory; AppRun needs no Fascia-specific client API.
pub fn installApp(b: *std.Build, id: []const u8, module: *std.Build.Module) void {
    const executable = b.addExecutable(.{ .name = "AppRun", .root_module = module });
    b.getInstallStep().dependOn(&b.addInstallArtifact(executable, .{
        .dest_dir = .{ .override = .{ .custom = b.fmt("{s}.AppDir", .{id}) } },
    }).step);
    for ([_][]const u8{ ".desktop", ".png" }) |suffix|
        b.installFile(b.fmt("{s}{s}", .{ id, suffix }), b.fmt("{s}.AppDir/{s}{s}", .{ id, id, suffix }));
    b.installFile(b.fmt("{s}.png", .{id}), b.fmt("{s}.AppDir/.DirIcon", .{id}));
}
