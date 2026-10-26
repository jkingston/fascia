const std = @import("std");
const posix = std.posix;

const wl = @import("wayland").server.wl;

const wlr = @import("wlroots");
const xkb = @import("xkbcommon");

const gpa = std.heap.c_allocator;
const Host = @import("fascia-lifecycle.zig").Host;
const presentation = @import("presentation.zig");
const builtin_ui = @import("home-interface.zig");
const c = @cImport({
    @cDefine("_GNU_SOURCE", "1");
    @cInclude("sys/socket.h");
    @cInclude("sys/un.h");
    @cInclude("sys/stat.h");
    @cInclude("unistd.h");
    @cInclude("fcntl.h");
    @cInclude("errno.h");
    @cInclude("stdlib.h");
    @cInclude("stdio.h");
});

// Kiosk policy and input routing are Zig; wlroots provides DRM/libinput/Pixman.
pub fn run(init: std.process.Init, home_ui: ?builtin_ui.UI) anyerror!u8 {
    wlr.log.init(.info, null);
    const args = init.minimal.args.vector;
    const hosting = home_ui != null or (args.len == 3 and std.mem.eql(u8, std.mem.span(args[1]), "--home"));
    if (hosting and c.setenv("WLR_RENDERER", "gles2", 1) != 0) return error.RendererEnvironment;
    var server: Server = undefined;
    try server.init();
    defer server.deinit();
    var socket_buffer: [11]u8 = undefined;
    const socket = try server.wl_server.addSocketAuto(&socket_buffer);
    var environment = try init.minimal.environ.createMap(gpa);
    defer environment.deinit();
    try environment.put("WAYLAND_DISPLAY", socket);
    const first: usize = if (args.len > 1 and std.mem.eql(u8, std.mem.span(args[1]), "--")) 2 else 1;
    if (!hosting and args.len <= first) return error.MissingApplication;
    try server.backend.start();
    if (hosting) {
        var host = Host{ .server = &server, .io = init.io, .environment = &environment, .built_in = home_ui };
        server.host = &host;
        const loop = server.wl_server.getEventLoop();
        const sigchild = try loop.addSignal(*Server, @intFromEnum(posix.SIG.CHLD), handleSignal, &server);
        defer sigchild.remove();
        const sigterm = try loop.addSignal(*Server, @intFromEnum(posix.SIG.TERM), handleSignal, &server);
        defer sigterm.remove();
        const sigint = try loop.addSignal(*Server, @intFromEnum(posix.SIG.INT), handleSignal, &server);
        defer sigint.remove();
        defer host.stop();
        try host.start(if (home_ui == null) std.mem.span(args[2]) else null);
        std.log.info("FASCIA_HOME_HOST=ready renderer=gles2 integrated={any}", .{home_ui != null});
        server.wl_server.run();
        return server.child_exit_code;
    }
    const argv = try gpa.alloc([]const u8, args.len - first);
    defer gpa.free(argv);
    for (args[first..], argv) |argument, *destination| destination.* = std.mem.span(argument);
    var child = try std.process.spawn(init.io, .{ .argv = argv, .environ_map = &environment, .pgid = 0 });
    defer {
        // Kill the whole child group before reopening DRM, including hung descendants.
        if (server.home_requested or !server.child_exited) posix.kill(-server.child_pid, .KILL) catch {};
        if (!server.child_exited) child.kill(init.io);
    }
    server.child_pid = child.id.?;
    const loop = server.wl_server.getEventLoop();
    const sigchild = try loop.addSignal(*Server, @intFromEnum(posix.SIG.CHLD), handleSignal, &server);
    defer sigchild.remove();
    const sigterm = try loop.addSignal(*Server, @intFromEnum(posix.SIG.TERM), handleSignal, &server);
    defer sigterm.remove();
    const sigint = try loop.addSignal(*Server, @intFromEnum(posix.SIG.INT), handleSignal, &server);
    defer sigint.remove();
    std.log.info(
        "COMPOSITOR=zig-wlroots RENDERER={s} WAYLAND_DISPLAY={s}",
        .{ environment.get("WLR_RENDERER") orelse "auto", socket },
    );
    server.wl_server.run();
    return if (server.home_requested) 42 else server.child_exit_code;
}
extern "c" fn waitpid(pid: c_int, status: *c_int, flags: c_int) c_int;

fn handleSignal(signal: c_int, server: *Server) c_int {
    if (signal == @intFromEnum(posix.SIG.CHLD)) {
        if (server.host) |host| {
            host.reap();
            return 0;
        }
        var status: c_int = 0;
        if (waitpid(server.child_pid, &status, 1) <= 0) return 0;
        server.child_exited = true;
        const signal_number: u8 = @intCast(status & 0x7f);
        server.child_exit_code = if (signal_number == 0) @intCast((status >> 8) & 0xff) else 128 + signal_number;
    }
    server.wl_server.terminate();
    return 0;
}

pub const Server = struct {
    host: ?*Host = null,
    home_touch: ?i32 = null,
    home_pointer: bool = false,
    child_pid: posix.pid_t = 0,
    child_exited: bool = false,
    child_exit_code: u8 = 0,
    home_requested: bool = false,
    home_keyboards: usize = 0,
    width: u32 = 1280,
    height: u32 = 800,
    wl_server: *wl.Server,
    backend: *wlr.Backend,
    renderer: *wlr.Renderer,
    allocator: *wlr.Allocator,
    scene: *wlr.Scene,

    output_layout: *wlr.OutputLayout,
    scene_output_layout: *wlr.SceneOutputLayout,
    new_output: wl.Listener(*wlr.Output) = .init(newOutput),

    xdg_shell: *wlr.XdgShell,
    new_xdg_toplevel: wl.Listener(*wlr.XdgToplevel) = .init(newXdgToplevel),
    new_xdg_popup: wl.Listener(*wlr.XdgPopup) = .init(newXdgPopup),
    toplevels: wl.list.Head(Toplevel, .link) = undefined,

    seat: *wlr.Seat,
    new_input: wl.Listener(*wlr.InputDevice) = .init(newInput),
    request_set_cursor: wl.Listener(*wlr.Seat.event.RequestSetCursor) = .init(requestSetCursor),
    request_set_selection: wl.Listener(*wlr.Seat.event.RequestSetSelection) = .init(requestSetSelection),
    keyboards: wl.list.Head(Keyboard, .link) = undefined,

    cursor: *wlr.Cursor,
    cursor_mgr: *wlr.XcursorManager,
    cursor_motion: wl.Listener(*wlr.Pointer.event.Motion) = .init(cursorMotion),
    cursor_motion_absolute: wl.Listener(*wlr.Pointer.event.MotionAbsolute) = .init(cursorMotionAbsolute),
    cursor_button: wl.Listener(*wlr.Pointer.event.Button) = .init(cursorButton),
    cursor_axis: wl.Listener(*wlr.Pointer.event.Axis) = .init(cursorAxis),
    cursor_frame: wl.Listener(*wlr.Cursor) = .init(cursorFrame),

    touch_down: wl.Listener(*wlr.Touch.event.Down) = .init(touchDown),
    touch_motion: wl.Listener(*wlr.Touch.event.Motion) = .init(touchMotion),
    touch_up: wl.Listener(*wlr.Touch.event.Up) = .init(touchUp),
    touch_cancel: wl.Listener(*wlr.Touch.event.Cancel) = .init(touchCancel),
    touch_frame: wl.Listener(void) = .init(touchFrame),

    cursor_mode: enum { passthrough, move, resize } = .passthrough,
    grabbed_view: ?*Toplevel = null,
    grab_x: f64 = 0,
    grab_y: f64 = 0,
    grab_box: wlr.Box = undefined,
    resize_edges: wlr.Edges = .{},

    fn init(server: *Server) !void {
        const wl_server = try wl.Server.create();
        const loop = wl_server.getEventLoop();
        const backend = try wlr.Backend.autocreate(loop, null);
        const renderer = try wlr.Renderer.autocreate(backend);
        const output_layout = try wlr.OutputLayout.create(wl_server);
        const scene = try wlr.Scene.create();
        server.* = .{
            .wl_server = wl_server,
            .backend = backend,
            .renderer = renderer,
            .allocator = try wlr.Allocator.autocreate(backend, renderer),
            .scene = scene,
            .output_layout = output_layout,
            .scene_output_layout = try scene.attachOutputLayout(output_layout),
            .xdg_shell = try wlr.XdgShell.create(wl_server, 2),
            .seat = try wlr.Seat.create(wl_server, "default"),
            .cursor = try wlr.Cursor.create(),
            .cursor_mgr = try wlr.XcursorManager.create(null, 24),
        };

        try server.renderer.initServer(wl_server);
        _ = try wlr.ScreencopyManagerV1.create(wl_server);
        _ = try wlr.XdgOutputManagerV1.create(wl_server, output_layout);
        _ = try wlr.Viewporter.create(wl_server);

        _ = try wlr.Compositor.create(server.wl_server, 6, server.renderer);
        _ = try wlr.Subcompositor.create(server.wl_server);
        _ = try wlr.DataDeviceManager.create(server.wl_server);

        server.backend.events.new_output.add(&server.new_output);

        server.xdg_shell.events.new_toplevel.add(&server.new_xdg_toplevel);
        server.xdg_shell.events.new_popup.add(&server.new_xdg_popup);
        server.toplevels.init();

        server.backend.events.new_input.add(&server.new_input);
        server.seat.events.request_set_cursor.add(&server.request_set_cursor);
        server.seat.events.request_set_selection.add(&server.request_set_selection);
        server.keyboards.init();

        server.cursor.attachOutputLayout(server.output_layout);
        try server.cursor_mgr.load(1);
        server.cursor.events.motion.add(&server.cursor_motion);
        server.cursor.events.motion_absolute.add(&server.cursor_motion_absolute);
        server.cursor.events.button.add(&server.cursor_button);
        server.cursor.events.axis.add(&server.cursor_axis);
        server.cursor.events.frame.add(&server.cursor_frame);
        server.cursor.events.touch_down.add(&server.touch_down);
        server.cursor.events.touch_motion.add(&server.touch_motion);
        server.cursor.events.touch_up.add(&server.touch_up);
        server.cursor.events.touch_cancel.add(&server.touch_cancel);
        server.cursor.events.touch_frame.add(&server.touch_frame);
    }

    fn deinit(server: *Server) void {
        server.wl_server.destroyClients();

        server.new_input.link.remove();
        server.new_output.link.remove();

        server.new_xdg_toplevel.link.remove();
        server.new_xdg_popup.link.remove();
        server.request_set_cursor.link.remove();
        server.request_set_selection.link.remove();
        server.cursor_motion.link.remove();
        server.cursor_motion_absolute.link.remove();
        server.cursor_button.link.remove();
        server.cursor_axis.link.remove();
        server.cursor_frame.link.remove();
        server.touch_down.link.remove();
        server.touch_motion.link.remove();
        server.touch_up.link.remove();
        server.touch_cancel.link.remove();
        server.touch_frame.link.remove();

        server.backend.destroy();
        server.wl_server.destroy();
    }

    fn newOutput(listener: *wl.Listener(*wlr.Output), wlr_output: *wlr.Output) void {
        const server: *Server = @alignCast(@fieldParentPtr("new_output", listener));

        if (!wlr_output.initRender(server.allocator, server.renderer)) return;

        var state = wlr.Output.State.init();
        defer state.finish();

        state.setEnabled(true);
        state.setTransform(.@"270");
        if (wlr_output.preferredMode()) |mode| {
            state.setMode(mode);
        }
        if (!wlr_output.commitState(&state)) return;
        var width: c_int = 0;
        var height: c_int = 0;
        wlr_output.effectiveResolution(&width, &height);
        if (width > 0 and height > 0) {
            server.width = @intCast(width);
            server.height = @intCast(height);
        }

        Output.create(server, wlr_output) catch {
            std.log.err("failed to allocate new output", .{});
            wlr_output.destroy();
            return;
        };
    }

    fn newXdgToplevel(listener: *wl.Listener(*wlr.XdgToplevel), xdg_toplevel: *wlr.XdgToplevel) void {
        const server: *Server = @alignCast(@fieldParentPtr("new_xdg_toplevel", listener));
        const xdg_surface = xdg_toplevel.base;

        // Don't add the toplevel to server.toplevels until it is mapped
        const toplevel = gpa.create(Toplevel) catch {
            std.log.err("failed to allocate new toplevel", .{});
            return;
        };

        toplevel.* = .{
            .server = server,
            .xdg_toplevel = xdg_toplevel,
            .scene_tree = server.scene.tree.createSceneXdgSurface(xdg_surface) catch {
                gpa.destroy(toplevel);
                std.log.err("failed to allocate new toplevel", .{});
                return;
            },
        };
        if (server.host) |host| {
            const resource: *wl.Resource = @ptrCast(xdg_surface.surface.resource);
            const pid = resource.getClient().getCredentials().pid;
            toplevel.home_surface = if (host.home_child) |child| pid == child.id else false;
            toplevel.app_member = host.ownsClient(pid);
        }
        toplevel.scene_tree.node.data = toplevel;
        xdg_surface.data = toplevel.scene_tree;

        xdg_surface.surface.events.commit.add(&toplevel.commit);
        xdg_surface.surface.events.map.add(&toplevel.map);
        xdg_surface.surface.events.unmap.add(&toplevel.unmap);
        xdg_toplevel.events.destroy.add(&toplevel.destroy);
        xdg_toplevel.events.request_move.add(&toplevel.request_move);
        xdg_toplevel.events.request_resize.add(&toplevel.request_resize);
    }

    fn newXdgPopup(_: *wl.Listener(*wlr.XdgPopup), xdg_popup: *wlr.XdgPopup) void {
        const xdg_surface = xdg_popup.base;

        // Only xdg-shell creates popups here.
        const parent = wlr.XdgSurface.tryFromWlrSurface(xdg_popup.parent.?) orelse return;
        const parent_tree = @as(?*wlr.SceneTree, @ptrCast(@alignCast(parent.data))) orelse {
            // The xdg surface user data could be left null due to allocation failure.
            return;
        };
        const scene_tree = parent_tree.createSceneXdgSurface(xdg_surface) catch {
            std.log.err("failed to allocate xdg popup node", .{});
            return;
        };
        xdg_surface.data = scene_tree;

        const popup = gpa.create(Popup) catch {
            std.log.err("failed to allocate new popup", .{});
            return;
        };
        popup.* = .{
            .xdg_popup = xdg_popup,
        };

        xdg_surface.surface.events.commit.add(&popup.commit);
        xdg_popup.events.destroy.add(&popup.destroy);
    }

    const ViewAtResult = struct {
        toplevel: *Toplevel,
        surface: *wlr.Surface,
        sx: f64,
        sy: f64,
    };

    fn viewAt(server: *Server, lx: f64, ly: f64) ?ViewAtResult {
        var sx: f64 = undefined;
        var sy: f64 = undefined;
        if (server.scene.tree.node.at(lx, ly, &sx, &sy)) |node| {
            if (node.type != .buffer) return null;
            const scene_buffer = wlr.SceneBuffer.fromNode(node);
            const scene_surface = wlr.SceneSurface.tryFromBuffer(scene_buffer) orelse return null;

            var it: ?*wlr.SceneTree = node.parent;
            while (it) |n| : (it = n.node.parent) {
                if (@as(?*Toplevel, @ptrCast(@alignCast(n.node.data)))) |toplevel| {
                    return ViewAtResult{
                        .toplevel = toplevel,
                        .surface = scene_surface.surface,
                        .sx = sx,
                        .sy = sy,
                    };
                }
            }
        }
        return null;
    }

    pub fn focusView(server: *Server, toplevel: *Toplevel, surface: *wlr.Surface) void {
        if (server.seat.keyboard_state.focused_surface) |previous_surface| {
            if (previous_surface == surface) return;
            if (wlr.XdgSurface.tryFromWlrSurface(previous_surface)) |xdg_surface| {
                _ = xdg_surface.role_data.toplevel.?.setActivated(false);
            }
        }

        toplevel.scene_tree.node.raiseToTop();
        if (server.host) |host| if (host.overlay) |overlay| overlay.node.raiseToTop();
        toplevel.link.remove();
        if (toplevel.home_surface) {
            if (toplevel.server.host) |host| host.clearPresentation();
        }
        server.toplevels.prepend(toplevel);

        _ = toplevel.xdg_toplevel.setActivated(true);

        const wlr_keyboard = server.seat.getKeyboard() orelse return;
        server.seat.keyboardNotifyEnter(
            surface,
            wlr_keyboard.keycodes[0..wlr_keyboard.num_keycodes],
            &wlr_keyboard.modifiers,
        );
    }

    fn newInput(listener: *wl.Listener(*wlr.InputDevice), device: *wlr.InputDevice) void {
        const server: *Server = @alignCast(@fieldParentPtr("new_input", listener));
        switch (device.type) {
            .keyboard => Keyboard.create(server, device) catch |err| {
                std.log.err("failed to create keyboard: {}", .{err});
                return;
            },
            .pointer, .touch => server.cursor.attachInputDevice(device),
            else => {},
        }

        server.seat.setCapabilities(.{
            .pointer = true,
            .touch = true,
            .keyboard = server.keyboards.length() > 0,
        });
    }

    fn touchDown(listener: *wl.Listener(*wlr.Touch.event.Down), event: *wlr.Touch.event.Down) void {
        const server: *Server = @alignCast(@fieldParentPtr("touch_down", listener));
        var lx: f64 = undefined;
        var ly: f64 = undefined;
        server.cursor.absoluteToLayoutCoords(event.device, event.x, event.y, &lx, &ly);
        if (server.host) |host| if (host.built_in != null and !host.active) {
            if (host.ui_touch != null) return;
            host.ui_touch = event.touch_id;
            host.inputUI(.{ .x = @floatCast(lx), .y = @floatCast(ly), .down = true, .pressed = true });
            return;
        };
        if (server.host) |host| if (host.active and host.homeAt(lx, ly)) {
            server.home_touch = event.touch_id;
            host.goHome();
            return;
        };
        if (server.viewAt(lx, ly)) |hit| {
            server.focusView(hit.toplevel, hit.surface);
            _ = server.seat.touchNotifyDown(hit.surface, event.time_msec, event.touch_id, hit.sx, hit.sy);
        }
    }

    fn touchMotion(listener: *wl.Listener(*wlr.Touch.event.Motion), event: *wlr.Touch.event.Motion) void {
        const server: *Server = @alignCast(@fieldParentPtr("touch_motion", listener));
        if (server.home_touch == event.touch_id) return;
        var lx: f64 = undefined;
        var ly: f64 = undefined;
        server.cursor.absoluteToLayoutCoords(event.device, event.x, event.y, &lx, &ly);
        if (server.host) |host| if (host.built_in != null and !host.active) {
            if (host.ui_touch == event.touch_id) host.inputUI(.{ .x = @floatCast(lx), .y = @floatCast(ly), .down = true });
            return;
        };
        if (server.viewAt(lx, ly)) |hit| {
            server.seat.touchNotifyMotion(event.time_msec, event.touch_id, hit.sx, hit.sy);
        }
    }

    fn touchUp(listener: *wl.Listener(*wlr.Touch.event.Up), event: *wlr.Touch.event.Up) void {
        const server: *Server = @alignCast(@fieldParentPtr("touch_up", listener));
        if (server.host) |host| if (host.ui_touch == event.touch_id) {
            host.ui_touch = null;
            host.inputUI(.{ .x = host.ui_input.x, .y = host.ui_input.y, .released = true });
            return;
        };
        if (server.home_touch == event.touch_id) {
            server.home_touch = null;
            return;
        }
        _ = server.seat.touchNotifyUp(event.time_msec, event.touch_id);
    }

    fn touchCancel(listener: *wl.Listener(*wlr.Touch.event.Cancel), event: *wlr.Touch.event.Cancel) void {
        const server: *Server = @alignCast(@fieldParentPtr("touch_cancel", listener));
        if (server.host) |host| if (host.ui_touch == event.touch_id) {
            host.ui_touch = null;
            host.inputUI(.{ .x = -1000, .y = -1000, .released = true });
            return;
        };
        if (server.home_touch == event.touch_id) {
            server.home_touch = null;
            return;
        }
        if (server.seat.touchGetPoint(event.touch_id)) |point| {
            server.seat.touchNotifyCancel(point.client);
        }
    }

    fn touchFrame(listener: *wl.Listener(void)) void {
        const server: *Server = @alignCast(@fieldParentPtr("touch_frame", listener));
        server.seat.touchNotifyFrame();
    }

    fn requestSetCursor(
        listener: *wl.Listener(*wlr.Seat.event.RequestSetCursor),
        event: *wlr.Seat.event.RequestSetCursor,
    ) void {
        const server: *Server = @alignCast(@fieldParentPtr("request_set_cursor", listener));
        if (event.seat_client == server.seat.pointer_state.focused_client)
            server.cursor.setSurface(event.surface, event.hotspot_x, event.hotspot_y);
    }

    fn requestSetSelection(
        listener: *wl.Listener(*wlr.Seat.event.RequestSetSelection),
        event: *wlr.Seat.event.RequestSetSelection,
    ) void {
        const server: *Server = @alignCast(@fieldParentPtr("request_set_selection", listener));
        server.seat.setSelection(event.source, event.serial);
    }

    fn cursorMotion(
        listener: *wl.Listener(*wlr.Pointer.event.Motion),
        event: *wlr.Pointer.event.Motion,
    ) void {
        const server: *Server = @alignCast(@fieldParentPtr("cursor_motion", listener));
        server.cursor.move(event.device, event.delta_x, event.delta_y);
        server.processCursorMotion(event.time_msec);
    }

    fn cursorMotionAbsolute(
        listener: *wl.Listener(*wlr.Pointer.event.MotionAbsolute),
        event: *wlr.Pointer.event.MotionAbsolute,
    ) void {
        const server: *Server = @alignCast(@fieldParentPtr("cursor_motion_absolute", listener));
        server.cursor.warpAbsolute(event.device, event.x, event.y);
        server.processCursorMotion(event.time_msec);
    }

    fn processCursorMotion(server: *Server, time_msec: u32) void {
        if (server.host) |host| if (host.built_in != null and !host.active) {
            host.inputUI(.{
                .x = @floatCast(server.cursor.x),
                .y = @floatCast(server.cursor.y),
                .down = host.ui_input.down,
            });
            server.seat.pointerClearFocus();
            return;
        };
        switch (server.cursor_mode) {
            .passthrough => if (server.viewAt(server.cursor.x, server.cursor.y)) |res| {
                server.seat.pointerNotifyEnter(res.surface, res.sx, res.sy);
                server.seat.pointerNotifyMotion(time_msec, res.sx, res.sy);
            } else {
                server.cursor.setXcursor(server.cursor_mgr, "default");
                server.seat.pointerClearFocus();
            },
            .move => {
                const toplevel = server.grabbed_view.?;
                toplevel.x = @as(i32, @intFromFloat(server.cursor.x - server.grab_x));
                toplevel.y = @as(i32, @intFromFloat(server.cursor.y - server.grab_y));
                toplevel.scene_tree.node.setPosition(toplevel.x, toplevel.y);
            },
            .resize => {
                const toplevel = server.grabbed_view.?;
                const border_x = @as(i32, @intFromFloat(server.cursor.x - server.grab_x));
                const border_y = @as(i32, @intFromFloat(server.cursor.y - server.grab_y));

                var new_left = server.grab_box.x;
                var new_right = server.grab_box.x + server.grab_box.width;
                var new_top = server.grab_box.y;
                var new_bottom = server.grab_box.y + server.grab_box.height;

                if (server.resize_edges.top) {
                    new_top = border_y;
                    if (new_top >= new_bottom)
                        new_top = new_bottom - 1;
                } else if (server.resize_edges.bottom) {
                    new_bottom = border_y;
                    if (new_bottom <= new_top)
                        new_bottom = new_top + 1;
                }

                if (server.resize_edges.left) {
                    new_left = border_x;
                    if (new_left >= new_right)
                        new_left = new_right - 1;
                } else if (server.resize_edges.right) {
                    new_right = border_x;
                    if (new_right <= new_left)
                        new_right = new_left + 1;
                }

                toplevel.x = new_left - toplevel.xdg_toplevel.base.geometry.x;
                toplevel.y = new_top - toplevel.xdg_toplevel.base.geometry.y;
                toplevel.scene_tree.node.setPosition(toplevel.x, toplevel.y);

                const new_width = new_right - new_left;
                const new_height = new_bottom - new_top;
                _ = toplevel.xdg_toplevel.setSize(new_width, new_height);
            },
        }
    }

    fn cursorButton(
        listener: *wl.Listener(*wlr.Pointer.event.Button),
        event: *wlr.Pointer.event.Button,
    ) void {
        const server: *Server = @alignCast(@fieldParentPtr("cursor_button", listener));
        if (server.host) |host| if (host.built_in != null and !host.active) {
            if (event.button == 272) host.inputUI(.{
                .x = @floatCast(server.cursor.x),
                .y = @floatCast(server.cursor.y),
                .down = event.state == .pressed,
                .pressed = event.state == .pressed,
                .released = event.state == .released,
            });
            return;
        };
        if (server.home_pointer) {
            if (event.state == .released) server.home_pointer = false;
            return;
        }
        if (server.host) |host| if (host.active and host.homeAt(server.cursor.x, server.cursor.y)) {
            if (event.state == .pressed) {
                server.home_pointer = true;
                host.goHome();
            }
            return;
        };
        _ = server.seat.pointerNotifyButton(event.time_msec, event.button, event.state);
        if (event.state == .released) {
            server.cursor_mode = .passthrough;
        } else if (server.viewAt(server.cursor.x, server.cursor.y)) |res| {
            server.focusView(res.toplevel, res.surface);
        }
    }

    fn cursorAxis(
        listener: *wl.Listener(*wlr.Pointer.event.Axis),
        event: *wlr.Pointer.event.Axis,
    ) void {
        const server: *Server = @alignCast(@fieldParentPtr("cursor_axis", listener));
        server.seat.pointerNotifyAxis(
            event.time_msec,
            event.orientation,
            event.delta,
            event.delta_discrete,
            event.source,
            event.relative_direction,
        );
    }

    fn cursorFrame(listener: *wl.Listener(*wlr.Cursor), _: *wlr.Cursor) void {
        const server: *Server = @alignCast(@fieldParentPtr("cursor_frame", listener));
        server.seat.pointerNotifyFrame();
    }

    /// Assumes the modifier used for compositor keybinds is pressed
    /// Returns true if the key was handled
    fn handleKeybind(server: *Server, key: xkb.Keysym) bool {
        switch (@intFromEnum(key)) {
            xkb.Keysym.Escape => if (server.host) |host| host.goHome() else server.wl_server.terminate(),
            xkb.Keysym.F1 => {
                if (server.toplevels.length() < 2) return true;
                const toplevel: *Toplevel = @alignCast(@fieldParentPtr("link", server.toplevels.link.prev.?));
                server.focusView(toplevel, toplevel.xdg_toplevel.base.surface);
            },
            else => return false,
        }
        return true;
    }
};

const Output = struct {
    server: *Server,
    wlr_output: *wlr.Output,

    frame: wl.Listener(*wlr.Output) = .init(handleFrame),
    present: wl.Listener(*wlr.Output.event.Present) = .init(handlePresent),
    presentation_tracker: presentation.Tracker = .{},
    request_state: wl.Listener(*wlr.Output.event.RequestState) = .init(handleRequestState),
    destroy: wl.Listener(*wlr.Output) = .init(handleDestroy),

    // The wlr.Output should be destroyed by the caller on failure to trigger cleanup.
    fn create(server: *Server, wlr_output: *wlr.Output) !void {
        const output = try gpa.create(Output);

        output.* = .{
            .server = server,
            .wlr_output = wlr_output,
        };
        wlr_output.events.frame.add(&output.frame);
        wlr_output.events.present.add(&output.present);
        wlr_output.events.request_state.add(&output.request_state);
        wlr_output.events.destroy.add(&output.destroy);

        const layout_output = try server.output_layout.addAuto(wlr_output);

        const scene_output = try server.scene.createSceneOutput(wlr_output);
        server.scene_output_layout.addOutput(layout_output, scene_output);
    }

    fn handleFrame(listener: *wl.Listener(*wlr.Output), _: *wlr.Output) void {
        const output: *Output = @alignCast(@fieldParentPtr("frame", listener));

        const scene_output = output.server.scene.getSceneOutput(output.wlr_output).?;
        output.presentation_tracker.clear();
        if (output.server.host) |host| {
            if (!host.presentation_written) {
                if (host.visibleHomePid()) |pid| output.presentation_tracker.begin(output.wlr_output.commit_seq +% 1, pid);
            }
        }
        if (!scene_output.commit(null)) output.presentation_tracker.clear();

        var now = timestamp();
        scene_output.sendFrameDone(&now);
    }

    fn handlePresent(listener: *wl.Listener(*wlr.Output.event.Present), event: *wlr.Output.event.Present) void {
        const output: *Output = @alignCast(@fieldParentPtr("present", listener));
        const host = output.server.host orelse return;
        const pid = host.visibleHomePid() orelse 0;
        if (output.presentation_tracker.presented(event.commit_seq, pid, event.presented, pid > 0)) host.recordPresentation(pid);
    }

    fn handleRequestState(
        listener: *wl.Listener(*wlr.Output.event.RequestState),
        event: *wlr.Output.event.RequestState,
    ) void {
        const output: *Output = @alignCast(@fieldParentPtr("request_state", listener));

        _ = output.wlr_output.commitState(event.state);
    }

    fn handleDestroy(listener: *wl.Listener(*wlr.Output), _: *wlr.Output) void {
        const output: *Output = @alignCast(@fieldParentPtr("destroy", listener));

        output.frame.link.remove();
        output.present.link.remove();
        output.request_state.link.remove();
        output.destroy.link.remove();

        gpa.destroy(output);
    }
};

pub const Toplevel = struct {
    server: *Server,
    link: wl.list.Link = undefined,
    xdg_toplevel: *wlr.XdgToplevel,
    scene_tree: *wlr.SceneTree,

    x: i32 = 0,
    y: i32 = 0,
    home_surface: bool = false,
    app_member: bool = false,

    commit: wl.Listener(*wlr.Surface) = .init(handleCommit),
    map: wl.Listener(void) = .init(handleMap),
    unmap: wl.Listener(void) = .init(handleUnmap),
    destroy: wl.Listener(void) = .init(handleDestroy),
    request_move: wl.Listener(*wlr.XdgToplevel.event.Move) = .init(handleRequestMove),
    request_resize: wl.Listener(*wlr.XdgToplevel.event.Resize) = .init(handleRequestResize),

    fn handleCommit(listener: *wl.Listener(*wlr.Surface), _: *wlr.Surface) void {
        const toplevel: *Toplevel = @alignCast(@fieldParentPtr("commit", listener));
        if (toplevel.xdg_toplevel.base.initial_commit) {
            _ = toplevel.xdg_toplevel.setSize(1280, 800);
            _ = toplevel.xdg_toplevel.setFullscreen(true);
        }
    }

    fn handleMap(listener: *wl.Listener(void)) void {
        const toplevel: *Toplevel = @alignCast(@fieldParentPtr("map", listener));
        toplevel.server.toplevels.prepend(toplevel);
        if (toplevel.server.host) |host| {
            if (toplevel.app_member) host.appMapped();
            host.refresh();
            std.log.info(
                "FASCIA_VIEW=mapped kind={s} member={any} visible={any}",
                .{
                    if (toplevel.home_surface) "home" else "app",
                    toplevel.app_member,
                    toplevel.scene_tree.node.enabled,
                },
            );
        } else toplevel.server.focusView(toplevel, toplevel.xdg_toplevel.base.surface);
    }

    fn handleUnmap(listener: *wl.Listener(void)) void {
        const toplevel: *Toplevel = @alignCast(@fieldParentPtr("unmap", listener));
        toplevel.link.remove();
    }

    fn handleDestroy(listener: *wl.Listener(void)) void {
        const toplevel: *Toplevel = @alignCast(@fieldParentPtr("destroy", listener));

        toplevel.commit.link.remove();
        toplevel.map.link.remove();
        toplevel.unmap.link.remove();
        toplevel.destroy.link.remove();
        toplevel.request_move.link.remove();
        toplevel.request_resize.link.remove();

        gpa.destroy(toplevel);
    }

    fn handleRequestMove(
        listener: *wl.Listener(*wlr.XdgToplevel.event.Move),
        _: *wlr.XdgToplevel.event.Move,
    ) void {
        const toplevel: *Toplevel = @alignCast(@fieldParentPtr("request_move", listener));
        const server = toplevel.server;
        server.grabbed_view = toplevel;
        server.cursor_mode = .move;
        server.grab_x = server.cursor.x - @as(f64, @floatFromInt(toplevel.x));
        server.grab_y = server.cursor.y - @as(f64, @floatFromInt(toplevel.y));
    }

    fn handleRequestResize(
        listener: *wl.Listener(*wlr.XdgToplevel.event.Resize),
        event: *wlr.XdgToplevel.event.Resize,
    ) void {
        const toplevel: *Toplevel = @alignCast(@fieldParentPtr("request_resize", listener));
        const server = toplevel.server;

        server.grabbed_view = toplevel;
        server.cursor_mode = .resize;
        server.resize_edges = event.edges;

        const box = toplevel.xdg_toplevel.base.geometry;

        const border_x = toplevel.x + box.x + if (event.edges.right) box.width else 0;
        const border_y = toplevel.y + box.y + if (event.edges.bottom) box.height else 0;
        server.grab_x = server.cursor.x - @as(f64, @floatFromInt(border_x));
        server.grab_y = server.cursor.y - @as(f64, @floatFromInt(border_y));

        server.grab_box = box;
        server.grab_box.x += toplevel.x;
        server.grab_box.y += toplevel.y;
    }
};

const Popup = struct {
    xdg_popup: *wlr.XdgPopup,

    commit: wl.Listener(*wlr.Surface) = .init(handleCommit),
    destroy: wl.Listener(void) = .init(handleDestroy),

    fn handleCommit(listener: *wl.Listener(*wlr.Surface), _: *wlr.Surface) void {
        const popup: *Popup = @alignCast(@fieldParentPtr("commit", listener));
        if (popup.xdg_popup.base.initial_commit) {
            _ = popup.xdg_popup.base.scheduleConfigure();
        }
    }

    fn handleDestroy(listener: *wl.Listener(void)) void {
        const popup: *Popup = @alignCast(@fieldParentPtr("destroy", listener));

        popup.commit.link.remove();
        popup.destroy.link.remove();

        gpa.destroy(popup);
    }
};

const Keyboard = struct {
    server: *Server,
    link: wl.list.Link = undefined,
    device: *wlr.InputDevice,
    physical_home: bool = false,

    modifiers: wl.Listener(*wlr.Keyboard) = .init(handleModifiers),
    key: wl.Listener(*wlr.Keyboard.event.Key) = .init(handleKey),
    destroy: wl.Listener(*wlr.InputDevice) = .init(handleDestroy),

    fn create(server: *Server, device: *wlr.InputDevice) !void {
        const keyboard = try gpa.create(Keyboard);
        errdefer gpa.destroy(keyboard);

        keyboard.* = .{
            .server = server,
            .device = device,
        };

        const context = xkb.Context.new(.no_flags) orelse return error.ContextFailed;
        defer context.unref();
        const keymap = xkb.Keymap.newFromNames(context, null, .no_flags) orelse return error.KeymapFailed;
        defer keymap.unref();

        const wlr_keyboard = device.toKeyboard();
        if (!wlr_keyboard.setKeymap(keymap)) return error.SetKeymapFailed;
        wlr_keyboard.setRepeatInfo(25, 600);

        wlr_keyboard.events.modifiers.add(&keyboard.modifiers);
        wlr_keyboard.events.key.add(&keyboard.key);
        device.events.destroy.add(&keyboard.destroy);

        server.seat.setKeyboard(wlr_keyboard);
        server.keyboards.append(keyboard);
        keyboard.physical_home = if (device.name) |name| std.mem.eql(u8, std.mem.span(name), "PF1007L Home") else false;
        if (keyboard.physical_home) {
            server.home_keyboards += 1;
            if (server.host) |host| host.updateOverlay();
        }
    }

    fn handleModifiers(listener: *wl.Listener(*wlr.Keyboard), wlr_keyboard: *wlr.Keyboard) void {
        const keyboard: *Keyboard = @alignCast(@fieldParentPtr("modifiers", listener));
        keyboard.server.seat.setKeyboard(wlr_keyboard);
        keyboard.server.seat.keyboardNotifyModifiers(&wlr_keyboard.modifiers);
    }

    fn handleKey(listener: *wl.Listener(*wlr.Keyboard.event.Key), event: *wlr.Keyboard.event.Key) void {
        const keyboard: *Keyboard = @alignCast(@fieldParentPtr("key", listener));
        const wlr_keyboard = keyboard.device.toKeyboard();

        // Handle KEY_HOMEPAGE before xkb. Consume both edges; act on release.
        if (event.keycode == 172) {
            if (event.state == .released) {
                if (keyboard.server.host) |host| host.goHome() else {
                    keyboard.server.home_requested = true;
                    keyboard.server.wl_server.terminate();
                }
            }
            return;
        }

        // Translate libinput keycode -> xkbcommon
        const keycode = event.keycode + 8;

        var handled = false;
        if (wlr_keyboard.getModifiers().alt and event.state == .pressed) {
            for (wlr_keyboard.xkb_state.?.keyGetSyms(keycode)) |sym| {
                if (keyboard.server.handleKeybind(sym)) {
                    handled = true;
                    break;
                }
            }
        }

        if (!handled) {
            keyboard.server.seat.setKeyboard(wlr_keyboard);
            keyboard.server.seat.keyboardNotifyKey(event.time_msec, event.keycode, event.state);
        }
    }

    fn handleDestroy(listener: *wl.Listener(*wlr.InputDevice), _: *wlr.InputDevice) void {
        const keyboard: *Keyboard = @alignCast(@fieldParentPtr("destroy", listener));

        if (keyboard.physical_home) {
            keyboard.server.home_keyboards -= 1;
            if (keyboard.server.host) |host| host.updateOverlay();
        }
        keyboard.link.remove();

        keyboard.modifiers.link.remove();
        keyboard.key.link.remove();
        keyboard.destroy.link.remove();

        gpa.destroy(keyboard);
    }
};

pub fn timestamp() posix.timespec {
    var timespec: posix.timespec = undefined;
    switch (posix.errno(posix.system.clock_gettime(posix.CLOCK.MONOTONIC, &timespec))) {
        .SUCCESS => return timespec,
        else => @panic("CLOCK_MONOTONIC not supported"),
    }
}

pub fn nowMicros() i64 {
    const value = timestamp();
    return @as(i64, @intCast(value.sec)) * 1_000_000 + @divTrunc(@as(i64, @intCast(value.nsec)), 1000);
}
