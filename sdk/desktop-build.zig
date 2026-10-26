//! MiniFB desktop build.
const std = @import("std");

pub fn configure(b: *std.Build, module: *std.Build.Module, target: std.Build.ResolvedTarget) void {
    const source = b.lazyDependency("minifb", .{}) orelse return;
    module.addIncludePath(source.path("include"));
    module.addIncludePath(source.path("src"));
    // The C API needs the disposal hook but creates no C++ callback objects.
    const hook = b.addWriteFiles().add("minifb-c-only.c", "struct mfb_window; void release_cpp_stub(struct mfb_window *window) { (void)window; }\n");
    module.addCSourceFile(.{ .file = hook });
    module.addCSourceFiles(.{ .root = source.path(""), .files = &.{
        "src/MiniFB_common.c",       "src/MiniFB_internal.c",     "src/MiniFB_timer.c",
        "src/MiniFB_utf8.c",         "src/MiniFB_cpu_x86.c",      "src/MiniFB_stretch.c",
        "src/MiniFB_stretch_neon.c", "src/MiniFB_stretch_sse2.c", "src/MiniFB_stretch_avx2.c",
    }, .flags = &.{"-O2"} });
    switch (target.result.os.tag) {
        .macos => {
            module.addCMacro("USE_METAL_API", "1");
            module.addCSourceFiles(.{ .root = source.path(""), .files = &.{
                "src/macos/MacMiniFB.m", "src/macos/OSXWindow.m",
                "src/macos/OSXView.m",   "src/macos/OSXViewDelegate.m",
            }, .flags = &.{"-O2"} });
            for ([_][]const u8{ "Cocoa", "Carbon", "Metal", "MetalKit", "QuartzCore" }) |framework|
                module.linkFramework(framework, .{});
        },
        .linux => {
            module.addCSourceFiles(.{ .root = source.path(""), .files = &.{
                "src/x11/X11MiniFB.c", "src/x11/X11MiniFB_scale_funcs.c", "src/x11/X11MiniFB_text.c",
                "src/MiniFB_linux.c",  "src/MiniFB_evdev_keys.c",
            }, .flags = &.{"-O2"} });
            module.linkSystemLibrary("X11", .{ .use_pkg_config = .no });
        },
        else => @panic("Desktop preview supports macOS and Linux"),
    }
}
