//! Software UI support for a compositor-owned display; no Wayland client backend.
pub const Controller = @import("home/controller.zig").Controller;
pub const Surface = @import("embedded.zig").Surface;
pub const types = @import("fascia").types;
pub const Colour = @import("fascia").display.Colour;
pub const HomeOptions = @import("home/controller.zig").Options;
pub const HomeHost = @import("home/controller.zig").Host;
pub const Frame = @import("embedded.zig").Frame;
