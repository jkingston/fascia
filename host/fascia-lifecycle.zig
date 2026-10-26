//! Fascia application lifecycle, control protocol and embedded Home policy.
const std = @import("std");
const posix = std.posix;
const wl = @import("wayland").server.wl;
const wlr = @import("wlroots");
const pixman = @import("pixman");
const gpa = std.heap.c_allocator;
const spawn_child = @import("spawn-child.zig");
const protocol = @import("host-protocol.zig");
const builtin_ui = @import("home-interface.zig");
const HomeBuffer = @import("home-buffer.zig").Buffer;
const core = @import("server.zig");
const Server = core.Server;
const Toplevel = core.Toplevel;
const nowMicros = core.nowMicros;
const timestamp = core.timestamp;
extern "c" fn waitpid(pid: c_int, status: *c_int, flags: c_int) c_int;
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

// The host owns presentation policy; systemd owns app processes and descendants.
pub const Host = struct {
    server: *Server,
    io: std.Io,
    environment: *std.process.Environ.Map,
    home_child: ?std.process.Child = null,
    app_child: ?std.process.Child = null,
    active: bool = false,
    stopping: bool = false,
    stop_child: ?std.process.Child = null,
    launch_timer: ?*wl.EventSource = null,
    generation: u64 = 0,
    app_id: [64]u8 = undefined,
    app_id_len: usize = 0,
    socket_fd: c_int = -1,
    socket_source: ?*wl.EventSource = null,
    socket_path: [108:0]u8 = undefined,
    socket_path_len: usize = 0,
    socket_bound: bool = false,
    connections: [8]Connection = [_]Connection{.{}} ** 8,
    overlay: ?*wlr.SceneTree = null,
    overlay_enabled: bool = true,
    overlay_auto: bool = false,
    presentation_owner: bool = false,
    presentation_written: bool = false,
    presentation_warned: bool = false,
    presentation_attempt_ms: i64 = -1000,
    built_in: ?builtin_ui.UI = null,
    home_scene: ?*wlr.SceneBuffer = null,
    home_buffers: [2]?*HomeBuffer = .{ null, null },
    home_has_frame: bool = false,
    ui_timer: ?*wl.EventSource = null,
    ui_input: builtin_ui.Input = .{},
    ui_touch: ?i32 = null,

    fn launchUI(context: *anyopaque, id: []const u8) bool {
        const self: *Host = @ptrCast(@alignCast(context));
        self.launch(id) catch |err| {
            std.log.warn("FASCIA_APP=launch-failed {s}", .{@errorName(err)});
            return false;
        };
        return true;
    }

    fn scheduleUI(self: *Host) void {
        const ui = self.built_in orelse return;
        const timer = self.ui_timer orelse return;
        const now = nowMicros();
        const deadline = ui.wake(ui.context, now) orelse {
            timer.timerUpdate(0) catch {};
            return;
        };
        const delay = std.math.clamp(@divTrunc(@max(0, deadline - now) + 999, 1000), 1, std.math.maxInt(c_int));
        timer.timerUpdate(@intCast(delay)) catch self.server.wl_server.terminate();
    }

    pub fn inputUI(self: *Host, input: builtin_ui.Input) void {
        const ui = self.built_in orelse return;
        self.ui_input = input;
        ui.input(ui.context, input, nowMicros());
        self.ui_input.pressed = false;
        self.ui_input.released = false;
        self.scheduleUI();
    }

    fn releaseUI(context: *anyopaque, slot: u1, serial: u64) void {
        const self: *Host = @ptrCast(@alignCast(context));
        if (self.built_in) |ui| ui.release(ui.context, slot, serial);
        if (self.ui_timer != null) self.scheduleUI();
    }

    fn tickUI(self: *Host) c_int {
        self.paintUI() catch |err| {
            std.log.err("FASCIA_HOME=render-failed {s}", .{@errorName(err)});
            self.server.child_exit_code = 1;
            self.server.wl_server.terminate();
        };
        return 0;
    }

    fn paintUI(self: *Host) !void {
        const ui = self.built_in orelse return;
        var chosen: ?u1 = null;
        for (0..2) |index| if (ui.available(ui.context, @intCast(index))) {
            chosen = @intCast(index);
            break;
        };
        const slot = chosen orelse {
            try self.ui_timer.?.timerUpdate(4);
            return;
        };
        if (self.home_buffers[slot] == null) self.home_buffers[slot] = try HomeBuffer.create(self.server.width, self.server.height, slot, self, releaseUI);
        const buffer = self.home_buffers[slot].?;
        // The supported panel has a fixed mode; do not overwrite buffers on a mode change.
        if (buffer.base.width != self.server.width or buffer.base.height != self.server.height) return error.HomeOutputModeChanged;
        if (try ui.paint(ui.context, slot, buffer.pixels, self.server.width, self.server.height, nowMicros())) |frame| {
            var damage: pixman.Region32 = undefined;
            damage.init();
            defer damage.deinit();
            for (frame.damage) |rect| {
                if (!damage.unionRect(&damage, @intFromFloat(@floor(rect.x)), @intFromFloat(@floor(rect.y)), @intFromFloat(@ceil(rect.x + rect.width) - @floor(rect.x)), @intFromFloat(@ceil(rect.y + rect.height) - @floor(rect.y)))) {
                    try ui.cancel(ui.context, frame);
                    return error.HomeDamageAllocation;
                }
            }
            try ui.accept(ui.context, frame);
            buffer.serial = frame.serial;
            self.home_scene.?.setBufferWithDamage(&buffer.base, &damage);
            self.home_scene.?.setDestSize(@intCast(self.server.width), @intCast(self.server.height));
            self.home_scene.?.node.setEnabled(!self.active);
            self.home_has_frame = true;
        }
        self.scheduleUI();
    }

    pub fn clearPresentation(self: *Host) void {
        self.presentation_written = false;
        self.presentation_attempt_ms = -1000;
        if (self.presentation_owner) _ = c.unlink("/run/fascia-home/presented");
    }

    pub fn visibleHomePid(self: *Host) ?i32 {
        if (self.built_in != null) {
            return if (!self.active and self.home_scene != null and self.home_has_frame) c.getpid() else null;
        }
        const child = self.home_child orelse return null;
        if (self.active) return null;
        var iterator = self.server.toplevels.iterator(.forward);
        while (iterator.next()) |view| {
            if (view.home_surface and view.scene_tree.node.enabled and view.xdg_toplevel.base.surface.buffer != null) return child.id;
        }
        return null;
    }

    pub fn recordPresentation(self: *Host, home_pid: i32) void {
        if (!self.presentation_owner) return;
        if (self.presentation_written) return;
        const now = timestamp();
        const milliseconds: i64 = @as(i64, @intCast(now.sec)) * 1000 + @divTrunc(@as(i64, @intCast(now.nsec)), 1_000_000);
        if (milliseconds - self.presentation_attempt_ms < 1000) return;
        self.presentation_attempt_ms = milliseconds;
        self.writePresentation(home_pid, milliseconds) catch |err| {
            if (!self.presentation_warned) std.log.warn("FASCIA_HOME=presentation-marker-unavailable {s}", .{@errorName(err)});
            self.presentation_warned = true;
            return;
        };
        self.presentation_written = true;
        if (self.built_in) |ui| ui.presented(ui.context, nowMicros());
        self.scheduleUI();
        std.log.info("FASCIA_HOME=presented pid={d}", .{home_pid});
    }

    fn writePresentation(_: *Host, home_pid: i32, milliseconds: i64) !void {
        const boot = c.open("/proc/sys/kernel/random/boot_id", c.O_RDONLY | c.O_CLOEXEC);
        if (boot < 0) return error.BootId;
        defer _ = c.close(boot);
        var boot_buffer: [64]u8 = undefined;
        const count = c.read(boot, &boot_buffer, boot_buffer.len);
        if (count < 36) return error.BootId;
        const boot_id = std.mem.trim(u8, boot_buffer[0..@intCast(count)], "\r\n");
        if (boot_id.len != 36) return error.BootId;
        var buffer: [160]u8 = undefined;
        const record = try std.fmt.bufPrint(&buffer, "{s} {d} {d} {d}\n", .{ boot_id, c.getpid(), home_pid, milliseconds });
        var temporary = "/run/fascia-home/.presented-XXXXXX".*;
        const fd = c.mkstemp(&temporary);
        if (fd < 0) return error.PresentationCreate;
        defer _ = c.close(fd);
        defer _ = c.unlink(&temporary);
        if (c.fchmod(fd, 0o644) != 0) return error.PresentationPermissions;
        var offset: usize = 0;
        while (offset < record.len) {
            const written = c.write(fd, record[offset..].ptr, record.len - offset);
            if (written < 0 and c.__errno_location().* == c.EINTR) continue;
            if (written <= 0) return error.PresentationWrite;
            offset += @intCast(written);
        }
        if (c.rename(&temporary, "/run/fascia-home/presented") != 0) return error.PresentationRename;
    }

    pub fn start(self: *Host, home_path: ?[]const u8) !void {
        if (home_path) |path| if (!std.fs.path.isAbsolute(path)) {
            return error.HomePathMustBeAbsolute;
        };
        errdefer self.stop();
        const mode = self.environment.get("FASCIA_HOME_OVERLAY") orelse "1";
        self.overlay_enabled = !std.mem.eql(u8, mode, "0");
        self.overlay_auto = std.mem.eql(u8, mode, "auto");
        if (self.overlay_enabled) {
            self.overlay = try self.server.scene.tree.createSceneTree();
            const background = try self.overlay.?.createSceneRect(64, 64, &.{ 0.055, 0.075, 0.095, 1 });
            background.node.setPosition(12, 12);
            for (0..9) |row| {
                const rect = try self.overlay.?.createSceneRect(@intCast(4 + row * 4), 2, &.{ 0.95, 0.93, 0.85, 1 });
                rect.node.setPosition(@intCast(42 - row * 2), @intCast(25 + row * 2));
            }
            const house = try self.overlay.?.createSceneRect(28, 21, &.{ 0.95, 0.93, 0.85, 1 });
            house.node.setPosition(30, 43);
            const door = try self.overlay.?.createSceneRect(8, 14, &.{ 0.055, 0.075, 0.095, 1 });
            door.node.setPosition(40, 50);
            self.overlay.?.node.setEnabled(false);
        }
        const runtime = self.environment.get("XDG_RUNTIME_DIR") orelse return error.MissingRuntimeDirectory;
        const path = try std.fmt.bufPrintSentinel(&self.socket_path, "{s}/fascia-home.sock", .{runtime}, 0);
        self.socket_path_len = path.len;
        self.socket_fd = c.socket(c.AF_UNIX, c.SOCK_STREAM | c.SOCK_NONBLOCK | c.SOCK_CLOEXEC, 0);
        if (self.socket_fd < 0) return error.ControlSocket;
        var address = std.mem.zeroes(c.struct_sockaddr_un);
        address.sun_family = c.AF_UNIX;
        @memcpy(address.sun_path[0..path.len], path);
        var status: c.struct_stat = undefined;
        if (c.lstat(path, &status) == 0) {
            if ((status.st_mode & c.S_IFMT) != c.S_IFSOCK or status.st_uid != c.getuid()) return error.UnsafeControlPath;
            if (c.connect(self.socket_fd, .{ .__sockaddr_un__ = &address }, @sizeOf(@TypeOf(address))) == 0) return error.HostAlreadyRunning;
            if (c.__errno_location().* != c.ECONNREFUSED) return error.ControlPathBusy;
            if (c.unlink(path) != 0) return error.ControlUnlink;
        }
        if (c.bind(self.socket_fd, .{ .__sockaddr_un__ = &address }, @sizeOf(@TypeOf(address))) != 0) return error.ControlBind;
        self.socket_bound = true;
        if (c.chmod(path, 0o600) != 0 or c.listen(self.socket_fd, 8) != 0) return error.ControlListen;
        self.socket_source = try self.server.wl_server.getEventLoop().addFd(*Host, self.socket_fd, .{ .readable = true }, acceptControl, self);
        self.launch_timer = try self.server.wl_server.getEventLoop().addTimer(*Host, launchTimeout, self);
        self.presentation_owner = std.mem.eql(u8, runtime, "/run/pf-kiosk");
        self.clearPresentation();
        if (home_path) |executable| {
            self.home_child = try spawn_child.spawn(gpa, &.{executable}, self.environment, null);
        } else {
            const ui = self.built_in.?;
            self.home_scene = try self.server.scene.tree.createSceneBuffer(null);
            self.ui_timer = try self.server.wl_server.getEventLoop().addTimer(*Host, tickUI, self);
            ui.bind(ui.context, .{ .context = self, .launch = launchUI });
            ui.visibility(ui.context, true, self.generation, nowMicros());
            self.scheduleUI();
        }
    }

    pub fn stop(self: *Host) void {
        self.clearPresentation();
        if (self.launch_timer) |timer| timer.remove();
        self.launch_timer = null;
        if (self.ui_timer) |timer| timer.remove();
        self.ui_timer = null;
        if (self.home_scene) |scene| scene.node.destroy();
        self.home_scene = null;
        for (&self.home_buffers) |*buffer| {
            if (buffer.*) |value| value.base.drop();
            buffer.* = null;
        }
        // Input and surface destroy callbacks run later during Server.deinit.
        self.server.host = null;
        for (&self.connections) |*connection| connection.close();
        if (self.socket_source) |source| source.remove();
        self.socket_source = null;
        if (self.socket_fd >= 0) _ = c.close(self.socket_fd);
        self.socket_fd = -1;
        if (self.socket_bound) _ = c.unlink(self.socket_path[0..self.socket_path_len :0]);
        self.socket_bound = false;
        self.stopApp();
        // The service target also stops apps if the host crashes before cleanup.
        if (self.app_child) |*child| child.kill(self.io);
        self.app_child = null;
        if (self.home_child) |*child| {
            posix.kill(-child.id.?, .KILL) catch {};
            child.kill(self.io);
        }
        self.home_child = null;
    }

    pub fn homeAt(self: *const Host, x: f64, y: f64) bool {
        return self.overlayVisible() and x >= 12 and x < 76 and y >= 12 and y < 76;
    }

    fn overlayVisible(self: *const Host) bool {
        return self.overlay_enabled and (!self.overlay_auto or self.server.home_keyboards == 0);
    }

    pub fn updateOverlay(self: *Host) void {
        if (self.overlay) |overlay| {
            overlay.node.setEnabled(self.active and self.overlayVisible());
            overlay.node.raiseToTop();
        }
    }

    pub fn goHome(self: *Host) void {
        if (self.built_in) |ui| ui.home(ui.context);
        self.ui_input = .{};
        self.ui_touch = null;
        if (self.active) self.generation +%= 1;
        self.active = false;
        self.server.cursor_mode = .passthrough;
        self.server.grabbed_view = null;
        self.stopApp();
        self.refresh();
    }

    pub fn refresh(self: *Host) void {
        if (self.built_in) |ui| {
            if (self.home_scene) |scene| scene.node.setEnabled(!self.active);
            ui.visibility(ui.context, !self.active, self.generation, nowMicros());
            self.scheduleUI();
        }
        self.server.seat.keyboardClearFocus();
        self.server.seat.pointerClearFocus();
        var focus: ?*Toplevel = null;
        var it = self.server.toplevels.iterator(.forward);
        while (it.next()) |view| {
            const visible = if (view.home_surface) !self.active else self.active and self.app_child != null and view.app_member;
            view.scene_tree.node.setEnabled(visible);
            if (visible and focus == null) focus = view;
        }
        if (focus) |view| self.server.focusView(view, view.xdg_toplevel.base.surface);
        self.updateOverlay();
    }

    fn launch(self: *Host, id: []const u8) !void {
        if (!protocol.validId(id)) return error.InvalidAppId;
        if (self.app_child != null or self.stop_child != null) return error.AppBusy;
        const apps = self.environment.get("FASCIA_APPS_ROOT") orelse "/var/lib/fascia/apps";
        const states = self.environment.get("FASCIA_STATE_ROOT") orelse "/var/lib/fascia/state";
        const active_path = try std.fmt.allocPrintSentinel(gpa, "{s}/{s}/active", .{ apps, id }, 0);
        defer gpa.free(active_path);
        const release_ptr = c.realpath(active_path, null) orelse return error.AppNotInstalled;
        defer c.free(release_ptr);
        const release = std.mem.span(release_ptr);
        const prefix = try std.fmt.allocPrint(gpa, "{s}/{s}/releases/", .{ apps, id });
        defer gpa.free(prefix);
        if (!std.mem.startsWith(u8, release, prefix)) return error.InvalidReleasePath;
        const executable = try std.fmt.allocPrintSentinel(gpa, "{s}/AppRun", .{release}, 0);
        defer gpa.free(executable);
        if (c.access(executable, c.X_OK) != 0) return error.AppNotExecutable;
        const state = try std.fmt.allocPrint(gpa, "{s}/{s}", .{ states, id });
        defer gpa.free(state);
        var arguments: std.ArrayList([]const u8) = .empty;
        defer arguments.deinit(gpa);
        var arena = std.heap.ArenaAllocator.init(gpa);
        defer arena.deinit();
        const a = arena.allocator();
        const unit = try std.fmt.allocPrint(a, "--unit=fascia-app-{s}.service", .{id});
        try arguments.appendSlice(gpa, &.{
            "/usr/bin/env",                             try std.fmt.allocPrint(a, "XDG_RUNTIME_DIR=/run/user/{d}", .{c.getuid()}), "/usr/bin/systemd-run",              "--user",                       "--quiet",                    "--wait",                "--collect",             "--service-type=exec",            unit,
            "--property=BindsTo=fascia-session.target", "--property=After=fascia-session.target",                                  "--property=KillMode=control-group", "--property=TimeoutStopSec=2s", "--property=SendSIGKILL=yes", "--property=Restart=no", "--property=UMask=0077", "--property=NoNewPrivileges=yes", try std.fmt.allocPrint(a, "--working-directory={s}", .{release}),
        });
        for ([_][]const u8{ "XDG_RUNTIME_DIR", "WAYLAND_DISPLAY", "FASCIA_FONT" }) |key| {
            if (self.environment.get(key)) |value| try arguments.append(gpa, try std.fmt.allocPrint(a, "--setenv={s}={s}", .{ key, value }));
        }
        for ([_][]const u8{ "CONFIG", "DATA", "STATE", "CACHE" }, [_][]const u8{ "config", "data", "state", "cache" }) |kind, directory| {
            try arguments.append(gpa, try std.fmt.allocPrint(a, "--setenv=XDG_{s}_HOME={s}/{s}", .{ kind, state, directory }));
        }
        try arguments.appendSlice(gpa, &.{ "--", executable });
        self.app_child = try spawn_child.spawn(gpa, arguments.items, self.environment, release);
        if (self.built_in) |ui| ui.message(ui.context, "Opening app…");
        self.stopping = false;
        if (self.launch_timer) |timer| try timer.timerUpdate(8000);
        @memcpy(self.app_id[0..id.len], id);
        self.app_id_len = id.len;
        self.active = false;
        self.generation +%= 1;
        self.refresh();
        std.log.info("FASCIA_APP=launched id={s} pid={d}", .{ id, self.app_child.?.id.? });
    }

    fn stopApp(self: *Host) void {
        if (self.launch_timer) |timer| timer.timerUpdate(0) catch {};
        if (self.app_child == null or self.stopping) return;
        var buffer: [96]u8 = undefined;
        const unit = std.fmt.bufPrint(&buffer, "fascia-app-{s}.service", .{self.app_id[0..self.app_id_len]}) catch unreachable;
        var runtime_buffer: [64]u8 = undefined;
        const runtime = std.fmt.bufPrint(&runtime_buffer, "XDG_RUNTIME_DIR=/run/user/{d}", .{c.getuid()}) catch unreachable;
        self.stop_child = spawn_child.spawn(gpa, &.{ "/usr/bin/env", runtime, "/usr/bin/systemctl", "--user", "stop", unit }, self.environment, null) catch {
            std.log.err("FASCIA_APP=stop-request-failed", .{});
            return;
        };
        self.stopping = true;
    }

    pub fn ownsClient(self: *Host, pid: c_int) bool {
        if (self.app_child == null) return false;
        var path_buffer: [64]u8 = undefined;
        const path = std.fmt.bufPrintSentinel(&path_buffer, "/proc/{d}/cgroup", .{pid}, 0) catch return false;
        const fd = c.open(path, c.O_RDONLY | c.O_CLOEXEC);
        if (fd < 0) return false;
        defer _ = c.close(fd);
        var buffer: [4096]u8 = undefined;
        const count = c.read(fd, &buffer, buffer.len);
        if (count <= 0 or count == buffer.len) return false;
        var name_buffer: [96]u8 = undefined;
        const name = std.fmt.bufPrint(&name_buffer, "fascia-app-{s}.service", .{self.app_id[0..self.app_id_len]}) catch return false;
        var components = std.mem.tokenizeAny(u8, buffer[0..@intCast(count)], "/\n");
        while (components.next()) |part| if (std.mem.eql(u8, part, name)) return true;
        return false;
    }

    fn launchTimeout(self: *Host) c_int {
        std.log.warn("FASCIA_APP=first-fascia-timeout", .{});
        self.goHome();
        if (self.built_in) |ui| ui.message(ui.context, "This app did not open. Try again or check its logs.");
        self.scheduleUI();
        return 0;
    }

    pub fn appMapped(self: *Host) void {
        if (self.stopping or self.app_child == null) return;
        if (self.launch_timer) |timer| timer.timerUpdate(0) catch {};
        if (self.built_in) |ui| ui.message(ui.context, "");
        self.active = true;
        self.refresh();
    }

    pub fn reap(self: *Host) void {
        if (self.stop_child) |child| {
            var status: c_int = 0;
            if (waitpid(child.id.?, &status, 1) > 0) self.stop_child = null;
        }
        if (self.app_child) |child| {
            var status: c_int = 0;
            if (waitpid(child.id.?, &status, 1) > 0) {
                if (self.launch_timer) |timer| timer.timerUpdate(0) catch {};
                self.app_child = null;
                if (self.active) self.generation +%= 1;
                self.active = false;
                if (!self.stopping) {
                    if (self.built_in) |ui| ui.message(ui.context, if (status == 0) "" else "The app stopped unexpectedly. You can open it again.");
                }
                self.refresh();
                std.log.info("FASCIA_APP=ended id={s} wait_status={d}", .{ self.app_id[0..self.app_id_len], status });
            }
        }
        if (self.home_child) |child| {
            var status: c_int = 0;
            if (waitpid(child.id.?, &status, 1) > 0) {
                posix.kill(-child.id.?, .KILL) catch {};
                self.clearPresentation();
                self.home_child = null;
                self.server.child_exit_code = if (status & 0x7f == 0) @intCast((status >> 8) & 255) else 128 + @as(u8, @intCast(status & 0x7f));
                std.log.err("FASCIA_HOME=ended wait_status={d}; host stops without restarting it", .{status});
                self.server.wl_server.terminate();
            }
        }
    }

    fn acceptControl(fd: c_int, _: wl.EventMask, self: *Host) c_int {
        for (0..8) |_| {
            const client = c.accept4(fd, .{ .__sockaddr__ = null }, null, c.SOCK_NONBLOCK | c.SOCK_CLOEXEC);
            if (client < 0) break;
            var available: ?*Connection = null;
            for (&self.connections) |*connection| if (connection.fd < 0) {
                available = connection;
                break;
            };
            const connection = available orelse {
                _ = c.close(client);
                continue;
            };
            connection.* = .{ .host = self, .fd = client };
            const loop = self.server.wl_server.getEventLoop();
            connection.source = loop.addFd(*Connection, client, .{ .readable = true }, Connection.read, connection) catch {
                connection.close();
                continue;
            };
            connection.timer = loop.addTimer(*Connection, Connection.timeout, connection) catch {
                connection.close();
                continue;
            };
            connection.timer.?.timerUpdate(1000) catch {
                connection.close();
            };
        }
        return 0;
    }

    const Connection = struct {
        host: *Host = undefined,
        fd: c_int = -1,
        source: ?*wl.EventSource = null,
        timer: ?*wl.EventSource = null,
        bytes: [128]u8 = undefined,
        len: usize = 0,

        fn close(self: *Connection) void {
            if (self.source) |source| source.remove();
            if (self.timer) |timer| timer.remove();
            self.source = null;
            self.timer = null;
            if (self.fd >= 0) _ = c.close(self.fd);
            self.fd = -1;
        }

        fn timeout(self: *Connection) c_int {
            self.close();
            return 0;
        }

        fn reply(self: *Connection, text: []const u8) void {
            _ = c.send(self.fd, text.ptr, text.len, c.MSG_DONTWAIT | c.MSG_NOSIGNAL);
            self.close();
        }

        fn read(fd: c_int, mask: wl.EventMask, self: *Connection) c_int {
            if (mask.@"error") {
                self.close();
                return 0;
            }
            while (self.len < self.bytes.len) {
                const received = c.recv(fd, self.bytes[self.len..].ptr, self.bytes.len - self.len, c.MSG_DONTWAIT);
                if (received < 0) {
                    if (c.__errno_location().* == c.EINTR) continue;
                    break;
                }
                if (received == 0) {
                    self.close();
                    return 0;
                }
                self.len += @intCast(received);
                if (std.mem.indexOfScalar(u8, self.bytes[0..self.len], '\n')) |newline| {
                    if (newline + 1 != self.len) {
                        self.reply("error InvalidCommand\n");
                        return 0;
                    }
                    const command = protocol.parse(self.bytes[0..newline]) catch {
                        self.reply("error InvalidCommand\n");
                        return 0;
                    };
                    switch (command) {
                        .home => self.host.goHome(),
                        .stop => |id| {
                            if (std.mem.eql(u8, id, self.host.app_id[0..self.host.app_id_len])) self.host.goHome();
                        },
                        .state => {
                            var buffer: [112]u8 = undefined;
                            const message = if (!self.host.active)
                                std.fmt.bufPrint(&buffer, "home {d}\n", .{self.host.generation}) catch unreachable
                            else
                                std.fmt.bufPrint(&buffer, "app {s} {d}\n", .{ self.host.app_id[0..self.host.app_id_len], self.host.generation }) catch unreachable;
                            self.reply(message);
                            return 0;
                        },
                        .launch => |id| self.host.launch(id) catch |err| {
                            var buffer: [96]u8 = undefined;
                            const message = std.fmt.bufPrint(&buffer, "error {s}\n", .{@errorName(err)}) catch unreachable;
                            self.reply(message);
                            return 0;
                        },
                        .status => {
                            if (!self.host.active) self.reply("home\n") else {
                                var buffer: [80]u8 = undefined;
                                const message = std.fmt.bufPrint(&buffer, "app {s}\n", .{self.host.app_id[0..self.host.app_id_len]}) catch unreachable;
                                self.reply(message);
                            }
                            return 0;
                        },
                    }
                    self.reply("ok\n");
                    return 0;
                }
            }
            if (self.len == self.bytes.len) self.reply("error InvalidCommand\n");
            return 0;
        }
    };
};
