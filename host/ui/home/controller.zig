//! Home state, actions and scheduling independent of a Wayland client.
const std = @import("std");
const ui = @import("fascia").ui;
const t = @import("fascia").types;
const home_model = @import("model.zig");
const system = @import("system.zig");
const registrymod = @import("../home-apps.zig");
const display = @import("fascia").display;
const DisplayCheck = @import("display-check.zig").Model;
const c = @cImport({
    @cInclude("time.h");
});
const Setup = @import("setup.zig").Model;

const Pending = struct {
    pid: i32,
    action: home_model.Action,
};

pub const Host = struct {
    context: ?*anyopaque = null,
    launch: *const fn (?*anyopaque, []const u8) bool,
};

pub const Options = struct {
    apps_root: []const u8 = "/var/lib/fascia/apps",
    autostart: bool = true,
    display_maintenance: bool = false,
    host: ?Host = null,
};

pub const Controller = struct {
    allocator: std.mem.Allocator,
    options: Options,
    registry: registrymod.Registry,
    apps: []home_model.App,
    model: home_model.Model,
    check: DisplayCheck,
    show_display: bool,
    colour: display.Colour,
    pending: ?Pending = null,
    setup: Setup = .{},
    check_setup: bool = true,
    clock: [32]u8 = @splat(0),
    refresh_at: i64 = 0,
    visible: bool = true,
    generation: ?u64 = null,

    pub fn init(allocator: std.mem.Allocator, options: Options) !Controller {
        const apps_root = options.apps_root;
        var registry = try registrymod.Registry.load(allocator, apps_root);
        errdefer registry.deinit();
        var apps = try allocator.alloc(home_model.App, registry.entries.len);
        errdefer allocator.free(apps);
        var startup: ?usize = null;
        var saved_startup: ?usize = null;
        const configured = registry.default_id;
        for (registry.entries, 0..) |entry, i| {
            apps[i] = .{
                .id = entry.id,
                .name = entry.name,
                .description = entry.description,
                .version = entry.version,
                .builtin = entry.builtin,
            };
            if (registry.default_id) |id| {
                if (std.mem.eql(u8, id, entry.id)) saved_startup = i;
            }
            if (configured) |id| {
                if (std.mem.eql(u8, id, entry.id)) startup = i;
            }
        }
        var model = home_model.Model.init(apps, saved_startup, false);
        model.countdown.target = if (options.autostart and !options.display_maintenance and registry.settings_valid) startup else null;
        model.banner = model.countdown.target != null;
        model.countdown.duration_us = @as(i64, std.math.clamp(registry.startup_delay_seconds, 5, 60)) * 1_000_000;
        model.connections = try system.connections(allocator);
        errdefer allocator.free(model.connections);
        if (system.brightness()) |value| {
            model.brightness = value.level;
            model.brightness_max = value.maximum;
            model.brightness_known = true;
        } else model.brightness_known = false;
        if (!registry.settings_valid) model.message = "Startup settings need attention. Staying in Home.";
        return .{
            .allocator = allocator,
            .options = options,
            .registry = registry,
            .apps = apps,
            .model = model,
            .check = DisplayCheck.init(display.loadSaved().gains),
            .show_display = options.display_maintenance,
            .colour = display.loadConfigured(),
        };
    }

    pub fn deinit(self: *Controller) void {
        self.setup.deinit();
        self.allocator.free(self.model.connections);
        self.allocator.free(self.apps);
        self.registry.deinit();
    }

    pub fn setVisibility(self: *Controller, visible: bool, generation: ?u64) void {
        if (generation) |value| {
            if (self.generation) |previous| {
                if (value != previous) {
                    self.model.countdown.cancel();
                    self.model.banner = false;
                }
            }
            self.generation = value;
        }
        if (!visible) {
            self.model.countdown.cancel();
            self.model.banner = false;
        }
        if (visible and !self.visible) self.refresh_at = 0;
        self.visible = visible;
    }
    /// Physical Home while already in Home closes temporary routes/previews.
    pub fn home(self: *Controller) void {
        self.setup.close();
        self.model.countdown.cancel();
        self.model.banner = false;
        self.model.page = .apps;
        self.model.interaction = .{};
        if (self.show_display) self.colour = display.loadConfigured();
        self.show_display = false;
        self.refresh_at = 0;
    }

    pub fn nextWake(self: *const Controller, now: i64) ?i64 {
        if (self.setup.job != null) return now + 50_000;
        if (self.setup.editor) |editor| if (editor.erase_id != null) {
            return now + 40_000;
        };
        if (!self.visible) return if (self.pending != null) now + 50_000 else null;
        var deadline = if (self.refresh_at > now) @min(now + 1_000_000, self.refresh_at) else now + 1_000_000;
        if (self.pending != null) deadline = @min(deadline, now + 50_000);
        return deadline;
    }

    pub fn presented(self: *Controller, now: i64) void {
        if (!self.setup.active) self.model.countdown.presented(now);
    }

    pub fn displayColour(self: *const Controller) display.Colour {
        return self.colour;
    }

    pub fn view(self: *const Controller, arena: std.mem.Allocator, width: f32, height: f32, now: i64, measure: t.Measure) !ui.Node {
        if (self.setup.active) return self.setup.view(arena, width, height, measure);
        return if (self.show_display) self.check.view(arena, width, height) else self.model.view(arena, now, measure);
    }
    /// Process input before the countdown deadline. True invalidates old hit regions.
    pub fn update(self: *Controller, now: i64, input: t.Input, hits: []const ui.Hit) !bool {
        const allocator = self.allocator;
        const apps_root = self.options.apps_root;
        var invalidated = false;
        if (self.check_setup) {
            self.check_setup = false;
            if (Setup.available()) self.setup.begin(allocator, now, true);
        }
        if (!self.setup.active and self.setup.job != null) _ = self.setup.update(allocator, now, .{}, &.{});
        if (self.setup.active) {
            const changed = self.setup.update(allocator, now, input, hits);
            if (self.setup.first_boot) {
                self.model.countdown.cancel();
                self.model.banner = false;
            }
            return changed;
        }
        if (self.pending) |request| {
            if (system.poll(request.pid)) |success| {
                if (success) {
                    switch (request.action) {
                        .startup => |chosen| self.model.confirmStartup(chosen, true),
                        .launch => self.model.page = .apps,
                        .remove => {
                            self.model.page = .apps;
                            self.model.message = "App removed. Its saved data was kept.";
                            self.refresh_at = 0;
                        },
                        .brightness => unreachable,
                        .sound => self.model.message = "Quiet sound test requested.",
                        else => self.model.message = "",
                    }
                } else self.model.message = "Could not confirm that change. Check the local Fascia tools.";
                self.pending = null;
            }
        }
        if (!self.visible) return false;
        if (now >= self.refresh_at and !input.down and !input.pressed and !input.released and self.pending == null) {
            var fresh = registrymod.Registry.load(allocator, apps_root) catch null;
            if (fresh) |*next| {
                var changed = self.apps.len != next.entries.len;
                if (!changed) for (self.apps, next.entries) |old, current| {
                    if (!std.mem.eql(u8, old.id, current.id) or
                        !std.mem.eql(u8, old.name, current.name) or
                        !std.mem.eql(u8, old.description, current.description) or
                        !std.mem.eql(u8, old.version, current.version) or
                        old.builtin != current.builtin)
                    {
                        changed = true;
                        break;
                    }
                };
                const saved_id: ?[]const u8 = if (self.model.startup) |i| self.apps[i].id else null;
                if (!optionalStringEqual(saved_id, next.default_id)) changed = true;
                if (next.startup_delay_seconds != self.registry.startup_delay_seconds or next.settings_valid != self.registry.settings_valid) changed = true;
                if (changed) {
                    const replacement = try allocator.alloc(home_model.App, next.entries.len);
                    var chosen: ?usize = null;
                    for (next.entries, 0..) |entry, i| {
                        replacement[i] = .{
                            .id = entry.id,
                            .name = entry.name,
                            .description = entry.description,
                            .version = entry.version,
                            .builtin = entry.builtin,
                        };
                        if (next.default_id) |id| {
                            if (std.mem.eql(u8, id, entry.id)) chosen = i;
                        }
                    }
                    self.model.countdown.cancel();
                    self.model.banner = false;
                    self.model.apps = replacement;
                    self.model.startup = chosen;
                    self.model.page = .apps;
                    self.model.offset = 0;
                    self.model.interaction = .{};
                    invalidated = true;
                    allocator.free(self.apps);
                    self.apps = replacement;
                    self.registry.deinit();
                    self.registry = next.*;
                } else next.deinit();
            }
            const connection_text = system.connections(allocator) catch null;
            if (connection_text) |text| {
                allocator.free(self.model.connections);
                self.model.connections = text;
            }
            if (system.brightness()) |value| {
                self.model.brightness = value.level;
                self.model.brightness_max = value.maximum;
                self.model.brightness_known = true;
            } else self.model.brightness_known = false;
            self.refresh_at = now + 2_000_000;
        }

        if (self.show_display) {
            self.check.update(input, hits);
            self.colour = try display.Colour.init(self.check.previewGains());
            if (self.check.request_close) {
                self.show_display = false;
                self.check.request_close = false;
                self.colour = display.loadConfigured();
                invalidated = true;
            }
        } else if (self.model.update(now, input, hits)) |action| {
            if (action == .brightness) {
                system.setBrightness(action.brightness) catch {
                    self.model.message = "Brightness control is unavailable.";
                    readBrightness(&self.model);
                };
            } else if (self.pending == null) switch (action) {
                .setup => {
                    self.model.countdown.cancel();
                    self.model.banner = false;
                    self.setup.begin(allocator, now, false);
                    invalidated = true;
                },
                .display => {
                    self.show_display = true;
                    self.check = DisplayCheck.init(display.loadSaved().gains);
                    invalidated = true;
                },
                .launch => |index| if (index < self.apps.len) {
                    if (self.options.host) |host| {
                        if (host.launch(host.context, self.apps[index].id)) self.model.page = .apps else self.model.message = "Could not open app.";
                        return true;
                    }
                    self.pending = .{ .pid = system.spawn(allocator, .app, "launch", self.apps[index].id) catch {
                        self.model.message = "Could not open app.";
                        return invalidated;
                    }, .action = action };
                },
                .remove => |index| if (index < self.apps.len and !self.apps[index].builtin) {
                    self.pending = .{ .pid = system.spawn(allocator, .app, "uninstall", self.apps[index].id) catch {
                        self.model.message = "Could not confirm app removal.";
                        return invalidated;
                    }, .action = action };
                },
                .startup => |chosen| {
                    self.pending = .{ .pid = system.spawn(allocator, .app, "default", if (chosen) |i| self.apps[i].id else "home") catch {
                        self.model.message = "Could not confirm startup save.";
                        return invalidated;
                    }, .action = action };
                },
                .sound => {
                    self.pending = .{ .pid = system.spawn(allocator, .system, "sound-test", null) catch {
                        self.model.message = "Sound test is unavailable.";
                        return invalidated;
                    }, .action = action };
                },
                .brightness => unreachable,
            };
        }
        var timestamp = c.time(null);
        var tm: c.struct_tm = undefined;
        if (c.localtime_r(&timestamp, &tm) != null) {
            const len = c.strftime(&self.clock, self.clock.len, "%H:%M", &tm);
            self.model.clock = self.clock[0..len];
        }
        return invalidated;
    }
};

fn optionalStringEqual(a: ?[]const u8, b: ?[]const u8) bool {
    if (a) |first| {
        if (b) |second| return std.mem.eql(u8, first, second);
        return false;
    }
    return b == null;
}

fn readBrightness(model: *home_model.Model) void {
    if (system.brightness()) |value| {
        model.brightness = value.level;
        model.brightness_max = value.maximum;
        model.brightness_known = true;
    } else model.brightness_known = false;
}

fn testController(apps: []home_model.App) Controller {
    return .{
        .allocator = std.testing.allocator,
        .options = .{},
        .registry = undefined,
        .apps = apps,
        .model = home_model.Model.init(apps, null, false),
        .check = DisplayCheck.init(.{ 1, 1, 1 }),
        .show_display = false,
        .colour = display.Colour.init(.{ 1, 1, 1 }) catch unreachable,
        .refresh_at = std.math.maxInt(i64),
        .check_setup = false,
    };
}

test "embedded lifecycle cancels startup once and hidden controller sleeps" {
    var apps = [_]home_model.App{.{ .id = "clock", .name = "Clock" }};
    var controller = testController(&apps);
    controller.model.countdown = .configure(0, 5);
    controller.presented(10);
    controller.setVisibility(true, 0);
    controller.setVisibility(false, 1);
    try std.testing.expect(controller.model.countdown.cancelled);
    try std.testing.expect(controller.nextWake(20) == null);
    controller.setVisibility(true, 2);
    controller.model.page = .settings;
    controller.home();
    try std.testing.expectEqual(home_model.Page.apps, controller.model.page);
    controller.presented(30);
    try std.testing.expect(controller.model.countdown.cancelled);
    try std.testing.expectEqual(@as(?i64, 10), controller.model.countdown.started);
    // An overdue registry refresh while a helper is pending must not spin.
    controller.pending = .{ .pid = 1, .action = .sound };
    try std.testing.expectEqual(@as(?i64, 50_030), controller.nextWake(30));
}

test "embedded launch uses callback once and cancellation wins exact deadline" {
    const Sink = struct {
        calls: usize = 0,

        fn launch(raw: ?*anyopaque, id: []const u8) bool {
            const self: *@This() = @ptrCast(@alignCast(raw.?));
            std.debug.assert(std.mem.eql(u8, id, "clock"));
            self.calls += 1;
            return true;
        }
    };
    var sink = Sink{};
    var apps = [_]home_model.App{.{ .id = "clock", .name = "Clock" }};
    var controller = testController(&apps);
    controller.options.host = .{ .context = &sink, .launch = Sink.launch };
    controller.model.countdown = .configure(0, 5);
    controller.presented(0);
    _ = try controller.update(5_000_000, .{ .pressed = true, .down = true }, &.{});
    try std.testing.expectEqual(@as(usize, 0), sink.calls);
    controller.model.countdown = .configure(0, 5);
    controller.presented(0);
    _ = try controller.update(5_000_000, .{}, &.{});
    _ = try controller.update(6_000_000, .{}, &.{});
    try std.testing.expectEqual(@as(usize, 1), sink.calls);
}

test "first boot setup holds and cancels application autostart" {
    var apps = [_]home_model.App{.{ .id = "clock", .name = "Clock" }};
    var controller = testController(&apps);
    controller.model.countdown = .configure(0, 5);
    controller.setup.active = true;
    controller.setup.first_boot = true;
    controller.setup.page = .welcome;
    controller.presented(100);
    try std.testing.expect(controller.model.countdown.started == null);
    _ = try controller.update(6_000_000, .{}, &.{});
    try std.testing.expect(controller.model.countdown.cancelled);
    controller.home();
    try std.testing.expect(!controller.setup.active);
    try std.testing.expectEqual(home_model.Page.apps, controller.model.page);
}
