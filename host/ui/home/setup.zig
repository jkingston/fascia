//! Touchscreen setup and settings; shares durable stages with the serial wizard.
const std = @import("std");
const ui = @import("fascia").ui;
const t = @import("fascia").types;
const keyboard = @import("fascia").keyboard;
const transport = @import("setup-transport.zig");
const c = @cImport({
    @cInclude("unistd.h");
});

const Page = enum { loading, welcome, name, timezone, wifi, password, ssh, summary };

const Stage = struct {
    hostname: bool = false,
    timezone: bool = false,
    time: bool = false,
    wifi: bool = false,
    ssh: bool = false,
};

pub const Network = struct {
    name: []const u8,
    dbm: i32,
    security: []const u8,
    connected: bool = false,
};

const Address = struct {
    interface: []const u8,
    address: []const u8,
};

pub const Reply = struct {
    ok: bool = false,
    @"error": []const u8 = "Could not confirm that change.",
    hostname: []const u8 = "fascia",
    timezone: []const u8 = "UTC",
    completed: bool = false,
    stages: Stage = .{},
    key_count: usize = 0,
    ssh_active: bool = false,
    ssh_auth: []const u8 = "unconfigured",
    mdns_service_active: bool = false,
    addresses: []const Address = &.{},
    ssh_host_fingerprint: ?[]const u8 = null,
    networks: []const Network = &.{},
};

const Request = struct {
    action: []const u8,
    value: []const u8 = "",
    password: []const u8 = "",
    enable: bool = false,
};

const Edit = enum { name, timezone, ssid, password };

pub const Model = struct {
    active: bool = false,
    initial: bool = false,
    first_boot: bool = false,
    page: Page = .loading,
    next_page: ?Page = null,
    job: ?transport.Job = null,
    status: ?std.json.Parsed(Reply) = null,
    scan: ?std.json.Parsed(Reply) = null,
    scanning: bool = false,
    offset: usize = 0,
    editor: ?keyboard.Model = null,
    editing: Edit = .name,
    scratch: [2048]u8 = @splat(0),
    field: [2048]u8 = @splat(0),
    field_len: usize = 0,
    ssid: [128]u8 = @splat(0),
    ssid_len: usize = 0,
    security: enum { psk, open, enterprise } = .psk,
    message: [256]u8 = @splat(0),
    message_len: usize = 0,
    interaction: ui.Interaction = .{},

    pub fn deinit(self: *Model) void {
        if (self.job) |*job| job.deinit();
        if (self.status) |*value| value.deinit();
        if (self.scan) |*value| value.deinit();
        self.clearSecret();
    }

    pub fn begin(self: *Model, a: std.mem.Allocator, now: i64, initial: bool) void {
        if (self.job != null) return;
        self.active = true;
        self.initial = initial;
        self.page = .loading;
        self.request(a, .{ .action = "status" }, now, null);
    }

    pub fn available() bool {
        return c.access("/usr/local/bin/fascia-setup", c.X_OK) == 0;
    }

    fn info(self: *const Model) Reply {
        return if (self.status) |status| status.value else .{};
    }

    fn note(self: *Model, text: []const u8) void {
        self.message_len = @min(text.len, self.message.len);
        @memcpy(self.message[0..self.message_len], text[0..self.message_len]);
    }

    fn clearSecret(self: *Model) void {
        self.editor = null;
        std.crypto.secureZero(u8, &self.scratch);
        std.crypto.secureZero(u8, &self.field);
        self.field_len = 0;
    }

    pub fn close(self: *Model) void {
        self.clearSecret();
        self.active = false;
        self.interaction = .{};
    }

    fn nextStage(self: *const Model) Page {
        const state = self.info().stages;
        if (!state.hostname) return .name;
        if (!state.timezone or !state.time) return .timezone;
        if (!state.wifi) return .wifi;
        return .summary;
    }

    fn request(self: *Model, a: std.mem.Allocator, value: Request, now: i64, next: ?Page) void {
        if (self.job != null) return;
        const payload = std.json.Stringify.valueAlloc(a, value, .{}) catch {
            self.note("Not enough memory. Please try again.");
            return;
        };
        defer {
            std.crypto.secureZero(u8, payload);
            a.free(payload);
        }
        self.message_len = 0;
        self.next_page = next;
        self.scanning = std.mem.eql(u8, value.action, "scan");
        self.job = transport.Job.start(payload, now) catch {
            self.note("Setup helper is unavailable. Connect USB and run fascia setup.");
            self.page = .summary;
            return;
        };
    }

    fn enter(self: *Model, a: std.mem.Allocator, page: Page, now: i64) void {
        self.clearSecret();
        self.page = page;
        self.interaction = .{};
        self.message_len = 0;
        const initial = switch (page) {
            .name => self.info().hostname,
            .timezone => self.info().timezone,
            else => "",
        };
        self.field_len = @min(initial.len, self.field.len);
        @memcpy(self.field[0..self.field_len], initial[0..self.field_len]);
        if (page == .wifi) self.request(a, .{ .action = "scan" }, now, null);
    }

    fn edit(self: *Model, kind: Edit) void {
        self.editing = kind;
        self.editor = keyboard.Model.init(&self.scratch, if (kind == .ssid) "" else self.field[0..self.field_len], if (kind == .password) .secure else .text) catch null;
    }

    fn accept(self: *Model, parsed: std.json.Parsed(Reply)) void {
        if (self.scanning) {
            if (self.scan) |*old| old.deinit();
            self.scan = parsed;
            self.offset = 0;
        } else {
            if (self.status) |*old| old.deinit();
            self.status = parsed;
        }
    }

    pub fn update(self: *Model, a: std.mem.Allocator, now: i64, input: t.Input, hits: []const ui.Hit) bool {
        if (self.job) |*job| {
            const response = job.poll(now) catch {
                job.deinit();
                self.job = null;
                self.note("Setup did not respond. Retry here or use USB serial.");
                if (self.page == .loading) self.page = .summary;
                return true;
            };
            if (response) |bytes| {
                const parsed = std.json.parseFromSlice(Reply, a, bytes, .{ .allocate = .alloc_always, .ignore_unknown_fields = true }) catch {
                    job.deinit();
                    self.job = null;
                    self.note("Could not read setup status.");
                    if (self.page == .loading) self.page = .summary;
                    return true;
                };
                job.deinit();
                self.job = null;
                if (!parsed.value.ok) {
                    self.note(parsed.value.@"error");
                    parsed.deinit();
                    if (self.page == .loading) self.page = .summary;
                    return true;
                }
                self.accept(parsed);
                if (!self.initial and self.info().completed) self.first_boot = false;
                if (self.initial) {
                    self.initial = false;
                    self.first_boot = !self.info().completed;
                    if (!self.first_boot) {
                        self.close();
                        return true;
                    }
                    self.page = .welcome;
                } else if (self.next_page) |next| {
                    if (next == .loading) {
                        self.enter(a, if (self.first_boot) self.nextStage() else .summary, now);
                    } else self.enter(a, next, now);
                } else if (self.page == .loading) {
                    self.first_boot = !self.info().completed;
                    self.page = .summary;
                }
                self.next_page = null;
                return true;
            }
        }
        if (!self.active or self.job != null) return false;
        if (self.editor) |*editor| {
            if (editor.update(input, hits, @as(f64, @floatFromInt(now)) / 1_000_000)) |result| {
                if (result == .submit) {
                    const text = editor.text();
                    if (self.editing == .ssid) {
                        self.ssid_len = @min(text.len, self.ssid.len);
                        @memcpy(self.ssid[0..self.ssid_len], text[0..self.ssid_len]);
                        self.security = .psk;
                        self.clearSecret();
                        self.page = .password;
                    } else {
                        std.crypto.secureZero(u8, &self.field);
                        self.field_len = text.len;
                        @memcpy(self.field[0..self.field_len], text);
                        self.editor = null;
                        std.crypto.secureZero(u8, &self.scratch);
                    }
                } else {
                    self.editor = null;
                    std.crypto.secureZero(u8, &self.scratch);
                }
                self.interaction = .{};
                return true;
            }
            return false;
        }
        const event = self.interaction.update(hits, input) orelse return false;
        switch (event.action) {
            1 => self.close(),
            2 => self.request(a, .{ .action = "status" }, now, .loading),
            3 => self.edit(if (self.page == .name) .name else if (self.page == .timezone) .timezone else .password),
            4 => {
                self.request(a, .{
                    .action = if (self.page == .name) "hostname" else "timezone",
                    .value = self.field[0..self.field_len],
                }, now, .loading);
            },
            5 => self.request(a, .{ .action = "scan" }, now, null),
            6 => self.edit(.ssid),
            7 => self.request(a, .{ .action = "wifi" }, now, .loading),
            8 => {
                self.request(a, .{
                    .action = "wifi",
                    .value = self.ssid[0..self.ssid_len],
                    .password = self.field[0..self.field_len],
                }, now, .loading);
                self.clearSecret();
            },
            9 => self.enter(a, .wifi, now),
            10 => {
                self.request(a, .{ .action = "ssh", .enable = event.id == 1 }, now, .loading);
                self.clearSecret();
            },
            11 => {
                if (!self.info().completed and self.first_boot) self.request(a, .{ .action = "finish" }, now, .summary) else self.close();
            },
            12 => self.enter(a, switch (event.id) {
                0 => .name,
                1 => .timezone,
                2 => .wifi,
                else => .ssh,
            }, now),
            13 => self.request(a, .{ .action = "status" }, now, null),
            14 => {
                const count = if (self.scan) |scan| scan.value.networks.len else 0;
                self.offset = if (self.offset + 5 < count) self.offset + 5 else 0;
            },
            20 => if (self.scan) |scan| {
                if (event.id < scan.value.networks.len) {
                    const network = scan.value.networks[@intCast(event.id)];
                    if (network.connected) {
                        self.request(a, .{ .action = "wifi" }, now, .loading);
                        return true;
                    }
                    if (std.mem.eql(u8, network.security, "8021x") or std.mem.eql(u8, network.security, "wep")) {
                        self.note("This network needs specialist configuration over USB serial.");
                        return true;
                    }
                    self.ssid_len = @min(network.name.len, self.ssid.len);
                    @memcpy(self.ssid[0..self.ssid_len], network.name[0..self.ssid_len]);
                    self.clearSecret();
                    self.page = .password;
                    if (std.mem.eql(u8, network.security, "open")) self.security = .open else self.security = .psk;
                }
            },
            else => {},
        }
        return true;
    }

    pub fn view(self: *const Model, a: std.mem.Allocator, width: f32, height: f32, measure: t.Measure) !ui.Node {
        var nodes: std.ArrayList(ui.Node) = .empty;
        try nodes.append(a, label("Fascia", 64, 42, 24, 0x696b62ff));
        if (self.job == null) try nodes.append(a, try button(a, if (self.first_boot) "Later" else "Close", 1, 0, 1036, 30, 180, 64));
        const title = switch (self.page) {
            .loading => "Checking your frame",
            .welcome => "Make yourself at home",
            .name => "Name your frame",
            .timezone => "Your local time",
            .wifi => "Connect to Wi-Fi",
            .password => "Join your network",
            .ssh => "Access from your computer",
            .summary => "Connections & setup",
        };
        try nodes.append(a, label(title, 64, 130, 42, 0x242520ff));
        const subtitle = switch (self.page) {
            .loading => "Reading saved setup. Your existing settings stay in place.",
            .welcome => "A few quick choices, saved as you go. USB serial is always available.",
            .name => "Use a short name, such as lounge. This becomes lounge.local.",
            .timezone => "Use a timezone such as Europe/London. The clock syncs over Wi-Fi.",
            .wifi => "Choose a nearby network, enter a name, or continue offline.",
            .password => if (self.security == .open) "This is an open network. No password is required." else "Your password stays private and is saved only by the Wi-Fi service.",
            .ssh => "Access is chosen when preparing the card. Add keys later over USB with fascia setup.",
            .summary => if (self.info().completed) "Setup is saved. You can change these settings at any time." else "Completed steps are saved. Finish setup, or return to it later.",
        };
        try nodes.append(a, label(subtitle, 64, 202, 21, 0x696b62ff));
        if (self.message_len > 0) try nodes.append(a, try clippedLabel(a, self.message[0..self.message_len], 64, 254, 1152, 30, 20, 0x9c442aff));
        if (self.editor) |editor| {
            const field_title = switch (self.editing) {
                .ssid => "Wi-Fi network name",
                .name => "Device name",
                .timezone => "Timezone",
                .password => self.ssid[0..self.ssid_len],
            };
            try nodes.append(a, try clippedLabel(a, field_title, 64, 304, 1152, 46, 30, 0x242520ff));
            try nodes.append(a, try editor.view(a, .{ .x = 0, .y = height - 370, .width = width, .height = 370 }, measure));
            return ui.stack(a, .{ .height = .grow, .background = 0xf2f0e7ff }, try nodes.toOwnedSlice(a));
        }
        switch (self.page) {
            .welcome => {
                const state = self.info().stages;
                const count = @as(u8, @intFromBool(state.hostname)) + @intFromBool(state.timezone and state.time) + @intFromBool(state.wifi);
                try nodes.append(a, label(try std.fmt.allocPrint(a, "{d} of 3 steps already saved", .{count}), 64, 320, 30, 0x242520ff));
                try nodes.append(a, label("Name   ·   Local time   ·   Wi-Fi", 64, 374, 22, 0x696b62ff));
                if (self.job == null) try nodes.append(a, try button(a, if (count == 0) "Get started" else "Continue setup", 2, 0, 856, 664, 360, 72));
            },
            .name, .timezone => {
                try nodes.append(a, label(if (self.page == .name) "Device name" else "Timezone", 64, 290, 18, 0x696b62ff));
                try nodes.append(a, try fieldButton(a, self.field[0..self.field_len], 3, 64, 328, 1152, 96));
                if (self.page == .name) try nodes.append(a, label("This name also appears on your network with .local added.", 64, 446, 19, 0x696b62ff));
                if (self.job == null) try nodes.append(a, try button(a, "Save & continue", 4, 0, 856, 664, 360, 72));
            },
            .wifi => {
                if (self.scan) |scan| {
                    const end = @min(self.offset + 5, scan.value.networks.len);
                    for (scan.value.networks[self.offset..end], self.offset..) |network, index| {
                        try nodes.append(a, try networkRow(a, network, index, 300 + @as(f32, @floatFromInt(index - self.offset)) * 66));
                    }
                    if (scan.value.networks.len == 0) try nodes.append(a, label("No networks found. Try scanning again or enter a name.", 64, 330, 24, 0x696b62ff));
                }
                if (self.job == null) {
                    try nodes.append(a, try button(a, "Rescan", 5, 0, 64, 664, 180, 72));
                    try nodes.append(a, try button(a, "Enter name", 6, 0, 264, 664, 220, 72));
                    if (self.scan) |scan| if (scan.value.networks.len > 5) {
                        try nodes.append(a, try button(a, "More networks", 14, 0, 504, 664, 220, 72));
                    };
                    var connected = false;
                    if (self.scan) |scan| for (scan.value.networks) |network| {
                        connected = connected or network.connected;
                    };
                    try nodes.append(a, try button(a, if (connected) "Keep connection" else "Continue offline", 7, 0, 936, 664, 280, 72));
                }
            },
            .password => {
                try nodes.append(a, label("Network", 64, 290, 18, 0x696b62ff));
                try nodes.append(a, try clippedLabel(a, self.ssid[0..self.ssid_len], 64, 326, 1152, 48, 30, 0x242520ff));
                if (self.security != .open) {
                    try nodes.append(a, label("Password", 64, 394, 18, 0x696b62ff));
                    try nodes.append(a, try fieldButton(a, if (self.field_len == 0) "Enter password" else "••••••••", 3, 64, 430, 1152, 96));
                }
                if (self.job == null) {
                    try nodes.append(a, try button(a, "Back", 9, 0, 64, 664, 180, 72));
                    try nodes.append(a, try button(a, "Connect", 8, 0, 856, 664, 360, 72));
                }
            },
            .ssh => {
                try nodes.append(a, label(if (self.info().ssh_active) "SSH is on" else "SSH is off", 64, 320, 30, 0x242520ff));
                try nodes.append(a, try clippedLabel(a, try std.fmt.allocPrint(a, "Authentication: {s}", .{self.info().ssh_auth}), 64, 374, 1152, 40, 22, 0x696b62ff));
                if (self.job == null) try nodes.append(a, try button(a, if (self.info().ssh_active) "Disable SSH" else "Enable SSH", 10, @intFromBool(!self.info().ssh_active), 856, 664, 360, 72));
            },
            .summary => {
                const state = self.info();
                try nodes.append(a, label("Network name", 64, 286, 18, 0x696b62ff));
                try nodes.append(a, try clippedLabel(a, try std.fmt.allocPrint(a, "{s}.local", .{state.hostname}), 64, 312, 790, 43, 29, 0x242520ff));
                try nodes.append(a, label(if (state.mdns_service_active) "mDNS active" else "mDNS unavailable", 890, 318, 18, 0x696b62ff));
                try nodes.append(a, label("Network address", 64, 370, 18, 0x696b62ff));
                if (state.addresses.len > 0) {
                    var address = state.addresses[0];
                    for (state.addresses) |candidate| {
                        if (std.mem.startsWith(u8, candidate.interface, "wl")) {
                            address = candidate;
                            break;
                        }
                    }
                    try nodes.append(a, try clippedLabel(a, try std.fmt.allocPrint(a, "{s}  ·  {s}", .{ address.interface, address.address }), 64, 399, 560, 38, 24, 0x242520ff));
                    if (state.ssh_active) try nodes.append(a, try clippedLabel(a, try std.fmt.allocPrint(a, "ssh fascia@{s}", .{address.address}), 648, 399, 568, 38, 24, 0x242520ff));
                } else try nodes.append(a, label("No address yet. Connect Wi-Fi or use USB.", 64, 399, 24, 0x696b62ff));
                try nodes.append(a, label(if (state.ssh_active) "SSH on" else "SSH off", 64, 460, 18, 0x696b62ff));
                if (state.ssh_host_fingerprint) |fingerprint| try nodes.append(a, try clippedLabel(a, fingerprint, 180, 460, 1036, 28, 17, 0x696b62ff));
                for ([_][]const u8{ "Name", "Time", "Wi-Fi", "SSH" }, 0..) |text_label, index| try nodes.append(a, try button(a, text_label, 12, index, 64 + @as(f32, @floatFromInt(index)) * 292, 540, 276, 76));
                if (self.job == null) {
                    try nodes.append(a, try button(a, "Refresh", 13, 0, 64, 664, 220, 72));
                    try nodes.append(a, try button(a, if (!state.completed and self.first_boot) "Finish setup" else "Done", 11, 0, 856, 664, 360, 72));
                }
            },
            else => {},
        }
        if (self.job != null) try nodes.append(a, label(if (self.scanning) "Scanning nearby networks…" else "Saving and checking…", 64, 674, 22, 0x696b62ff));
        return ui.stack(a, .{ .height = .grow, .background = 0xf2f0e7ff }, try nodes.toOwnedSlice(a));
    }
};

fn label(text: []const u8, x: f32, y: f32, size: f32, colour: u32) ui.Node {
    var node = ui.text(text, size, colour);
    node.style.position = .{ .x = x, .y = y, .width = 1200 - x, .height = size * 1.5 };
    return node;
}

fn clippedLabel(a: std.mem.Allocator, content: []const u8, x: f32, y: f32, w: f32, h: f32, size: f32, colour: u32) !ui.Node {
    return ui.canvas(.{ .position = .{ .x = x, .y = y, .width = w, .height = h } }, w, h, try a.dupe(t.Command, &.{.{
        .bounds = .{ .width = w, .height = h },
        .clip = .{ .width = w, .height = h },
        .kind = .{ .text = .{ .content = content, .size = size, .colour = colour } },
    }}));
}

fn fieldButton(a: std.mem.Allocator, value: []const u8, action: u32, x: f32, y: f32, w: f32, h: f32) !ui.Node {
    return ui.stack(a, .{
        .id = 0,
        .action = action,
        .position = .{ .x = x, .y = y, .width = w, .height = h },
        .background = 0xe4e2d9ff,
        .radius = 6,
    }, &.{
        try clippedLabel(a, value, 24, 30, w - 150, 46, 28, 0x242520ff),
        label("Edit", w - 88, 35, 20, 0x696b62ff),
    });
}

fn button(a: std.mem.Allocator, text: []const u8, action: u32, id: u64, x: f32, y: f32, w: f32, h: f32) !ui.Node {
    const primary = action == 2 or action == 4 or action == 8 or action == 11 or (action == 10 and id == 1);
    return ui.column(a, .{
        .id = id,
        .action = action,
        .position = .{ .x = x, .y = y, .width = w, .height = h },
        .background = if (primary) 0x242520ff else 0xe4e2d9ff,
        .radius = 6,
        .alignment = .center,
        .justify = .center,
    }, &.{ui.text(text, 24, if (primary) 0xfaf9f2ff else 0x242520ff)});
}

fn networkRow(a: std.mem.Allocator, network: Network, index: usize, y: f32) !ui.Node {
    const security = if (std.mem.eql(u8, network.security, "psk")) "Secured" else if (std.mem.eql(u8, network.security, "open")) "Open" else "Advanced";
    const detail = if (network.connected) "Connected" else security;
    var bars: [4]t.Command = undefined;
    var strength: usize = 0;
    for ([_]i32{ -90, -75, -65, -55 }) |threshold| if (network.dbm >= threshold) {
        strength += 1;
    };
    for (&bars, 0..) |*bar, i| {
        const h: f32 = @floatFromInt(6 + i * 6);
        bar.* = .{
            .bounds = .{ .x = @as(f32, @floatFromInt(i)) * 12, .y = 26 - h, .width = 7, .height = h },
            .kind = .{ .rectangle = .{ .colour = if (i < strength) 0x4e6652ff else 0xc4c5baff, .radius = 2 } },
        };
    }
    return ui.column(a, .{
        .id = index,
        .action = 20,
        .position = .{ .x = 64, .y = y, .width = 1152, .height = 58 },
        .background = if (network.connected) 0xe0e6dcff else 0xe4e2d9ff,
        .radius = 6,
    }, &.{
        try clippedLabel(a, network.name, 22, 12, 830, 38, 24, 0x242520ff),
        label(detail, 888, 17, 18, 0x696b62ff),
        ui.canvas(.{ .position = .{ .x = 1072, .y = 16, .width = 48, .height = 26 } }, 48, 26, try a.dupe(t.Command, &bars)),
    });
}

fn testMeasure(_: ?*anyopaque, content: []const u8, size: f32) t.Metrics {
    return .{ .width = @as(f32, @floatFromInt(content.len)) * size * 0.5, .height = size };
}

test "touch setup resumes durable stages and closes without losing status" {
    var model = Model{ .active = true, .first_boot = true };
    defer model.deinit();
    model.status = try std.json.parseFromSlice(Reply, std.testing.allocator,
        \\{"ok":true,"hostname":"study","stages":{"hostname":true,"timezone":true,"time":true,"wifi":false,"ssh":false}}
    , .{ .allocate = .alloc_always });
    try std.testing.expectEqual(Page.wifi, model.nextStage());
    model.status.?.value.stages.wifi = true;
    try std.testing.expectEqual(Page.summary, model.nextStage());
    model.clearSecret();
    model.field_len = 6;
    @memcpy(model.field[0..6], "secret");
    model.close();
    try std.testing.expect(!model.active);
    try std.testing.expectEqual(@as(usize, 0), model.field_len);
    try std.testing.expectEqual(@as(u8, 0), model.field[0]);
    try std.testing.expect(model.info().stages.hostname);
}

test "Wi-Fi selection preserves SSID and password keyboard never renders its value" {
    var model = Model{ .active = true, .page = .wifi };
    defer model.deinit();
    model.scan = try std.json.parseFromSlice(Reply, std.testing.allocator,
        \\{"ok":true,"networks":[{"name":"Room with spaces","dbm":-45,"security":"psk","connected":false}]}
    , .{ .allocate = .alloc_always });
    const hit = [_]ui.Hit{.{
        .id = 0,
        .action = 20,
        .drag = false,
        .bounds = .{ .x = 0, .y = 0, .width = 100, .height = 100 },
    }};
    _ = model.update(std.testing.allocator, 0, .{ .x = 10, .y = 10, .pressed = true, .down = true }, &hit);
    _ = model.update(std.testing.allocator, 1, .{ .x = 10, .y = 10, .released = true }, &hit);
    try std.testing.expectEqual(Page.password, model.page);
    try std.testing.expectEqualStrings("Room with spaces", model.ssid[0..model.ssid_len]);
    model.edit(.password);
    try std.testing.expectEqual(keyboard.Mode.secure, model.editor.?.mode);
    try model.editor.?.insert("FAKE-password");
    var arena = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena.deinit();
    const node = try model.view(arena.allocator(), 1280, 800, .{ .function = testMeasure });
    _ = node;
    model.close();
    try std.testing.expectEqual(@as(u8, 0), model.scratch[0]);
}
