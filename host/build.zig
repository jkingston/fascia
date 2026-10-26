const std = @import("std");

const Scanner = @import("wayland").Scanner;

pub fn build(b: *std.Build) void {
    const target = b.standardTargetOptions(.{
        .default_target = .{
            .cpu_arch = .arm,
            .os_tag = .linux,
            .abi = .gnueabihf,
            .glibc_version = .{ .major = 2, .minor = 43, .patch = 0 },
            .cpu_model = .{ .explicit = &std.Target.arm.cpu.cortex_a7 },
        },
    });
    const optimize = b.standardOptimizeOption(.{});
    const build_kiosk = b.option(bool, "kiosk", "Build standalone kiosk for debugging") orelse false;
    const build_host = b.option(bool, "host", "Build the integrated Fascia host") orelse true;

    const use_llvm = b.option(bool, "llvm", "force usage of LLVM and LLD") orelse true;

    const arm_wayland = b.dependency("arm_wayland", .{});
    const arm_protocols = b.dependency("arm_protocols", .{});
    const arm_wlroots = b.dependency("arm_wlroots", .{});
    const arm_xkbcommon = b.dependency("arm_xkbcommon", .{});
    const arm_pixman = b.dependency("arm_pixman", .{});
    const scanner = Scanner.create(b, .{
        .wayland_xml = arm_wayland.path("usr/share/wayland/wayland.xml"),
        .wayland_protocols = arm_protocols.path("usr/share/wayland-protocols"),
    });
    scanner.addSystemProtocol("stable/xdg-shell/xdg-shell.xml");
    scanner.addSystemProtocol("stable/tablet/tablet-v2.xml");
    scanner.addSystemProtocol("staging/color-management/color-management-v1.xml");

    // Protocol versions select generated bindings; wlroots implements the protocols.
    scanner.generate("wl_compositor", 4);
    scanner.generate("wl_subcompositor", 1);
    scanner.generate("wl_shm", 1);
    scanner.generate("wl_output", 4);
    scanner.generate("wl_seat", 7);
    scanner.generate("wl_data_device_manager", 3);
    scanner.generate("xdg_wm_base", 2);
    scanner.generate("zwp_tablet_manager_v2", 1);
    scanner.generate("wp_color_manager_v1", 2);

    const wayland = b.createModule(.{ .root_source_file = scanner.result });
    const xkbcommon = b.dependency("xkbcommon", .{}).module("xkbcommon");
    const pixman = b.dependency("pixman", .{}).module("pixman");
    const wlroots = b.dependency("wlroots", .{}).module("wlroots");

    wlroots.addImport("wayland", wayland);
    wlroots.addImport("xkbcommon", xkbcommon);
    wlroots.addImport("pixman", pixman);

    // Link pinned ARM libraries; bindings need only wlroots version/config headers.
    wlroots.resolved_target = target;
    wlroots.linkSystemLibrary("wlroots-0.20", .{ .use_pkg_config = .no });
    wlroots.addSystemIncludePath(arm_wlroots.path("usr/include/wlroots-0.20"));
    wlroots.addLibraryPath(arm_wlroots.path("usr/lib"));

    const tinywl: ?*std.Build.Step.Compile = if (build_kiosk) b.addExecutable(.{
        .name = "fascia-compositor",
        .root_module = b.createModule(.{
            .root_source_file = b.path("main.zig"),
            .target = target,
            .optimize = optimize,
            .link_libc = true,
            .strip = optimize != .Debug,
        }),
        .use_llvm = use_llvm,
        .use_lld = use_llvm,
    }) else null;

    const host: ?*std.Build.Step.Compile = if (build_host) block: {
        const executable = b.addExecutable(.{
            .name = "fascia-host",
            .root_module = b.createModule(.{
                .root_source_file = b.path("fascia-host.zig"),
                .target = target,
                .optimize = optimize,
                .link_libc = true,
                .strip = optimize != .Debug,
            }),
            .use_llvm = use_llvm,
            .use_lld = use_llvm,
        });
        const home = b.createModule(.{
            .root_source_file = b.path("ui/host-sdk.zig"),
            .target = target,
            .optimize = optimize,
            .link_libc = true,
        });
        home.addImport("fascia", b.dependency("native_ui", .{ .target = target, .optimize = optimize, .embedded = true }).module("fascia"));
        executable.root_module.addImport("native_ui", home);
        break :block executable;
    } else null;
    for ([_]?*std.Build.Step.Compile{ tinywl, host }) |optional| {
        const executable = optional orelse continue;
        executable.root_module.addImport("wayland", wayland);
        executable.root_module.addImport("xkbcommon", xkbcommon);
        executable.root_module.addImport("wlroots", wlroots);
        executable.root_module.addImport("pixman", pixman);
        executable.root_module.linkSystemLibrary("wayland-server", .{ .use_pkg_config = .no });
        executable.root_module.linkSystemLibrary("xkbcommon", .{ .use_pkg_config = .no });
        executable.root_module.linkSystemLibrary("pixman-1", .{ .use_pkg_config = .no });
        for ([_]*std.Build.Dependency{ arm_wlroots, arm_wayland, arm_xkbcommon, arm_pixman }) |dependency| {
            executable.root_module.addLibraryPath(dependency.path("usr/lib"));
        }
        b.installArtifact(executable);
    }
    const home_button = b.addExecutable(.{
        .name = "fascia-home-button",
        .root_module = b.createModule(.{
            .root_source_file = b.path("home-button.zig"),
            .target = target,
            .optimize = optimize,
            .link_libc = true,
            .strip = optimize != .Debug,
        }),
    });
    b.installArtifact(home_button);
    const button_tests = b.addTest(.{
        .root_module = b.createModule(.{
            .root_source_file = b.path("home-button.zig"),
            .target = b.graph.host,
            .optimize = optimize,
            .link_libc = true,
        }),
    });
    const protocol_tests = b.addTest(.{ .root_module = b.createModule(.{
        .root_source_file = b.path("host-protocol.zig"),
        .target = b.graph.host,
        .optimize = optimize,
    }) });
    const tests = b.step("test", "Validate Home control commands and physical button debounce");
    tests.dependOn(&b.addRunArtifact(protocol_tests).step);
    const presentation_tests = b.addTest(.{
        .root_module = b.createModule(.{
            .root_source_file = b.path("presentation.zig"),
            .target = b.graph.host,
            .optimize = optimize,
        }),
    });
    tests.dependOn(&b.addRunArtifact(presentation_tests).step);
    tests.dependOn(&b.addRunArtifact(button_tests).step);
    const spawn_tests = b.addTest(.{ .root_module = b.createModule(.{
        .root_source_file = b.path("spawn-child.zig"),
        .target = b.graph.host,
        .optimize = optimize,
        .link_libc = true,
    }) });
    tests.dependOn(&b.addRunArtifact(spawn_tests).step);
    const test_sdk = b.dependency("native_ui", .{ .target = b.graph.host, .optimize = optimize, .embedded = true });
    const home_tests = b.step("test-home", "Test integrated Home and its rendering backend");
    for ([_][]const u8{ "ui/home-tests.zig", "ui/embedded-tests.zig" }) |source| {
        const module = b.createModule(.{
            .root_source_file = b.path(source),
            .target = b.graph.host,
            .optimize = optimize,
            .link_libc = true,
        });
        module.addImport("fascia", test_sdk.module("fascia"));
        const assets = b.addOptions();
        assets.addOption([]const u8, "font_path", test_sdk.builder.dependency("test_fonts", .{}).path("usr/share/fonts/TTF/DejaVuSans.ttf").getPath(b));
        module.addOptions("test_assets", assets);
        home_tests.dependOn(&b.addRunArtifact(b.addTest(.{ .root_module = module })).step);
    }
}
