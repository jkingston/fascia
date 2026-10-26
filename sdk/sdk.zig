//! Public app surface. Zig dependencies are source packages; the frame supplies
//! Linux/Wayland and its graphics drivers. No compiler runs on the device.
pub const ui = @import("layout.zig");
pub const types = @import("types.zig");
pub const widgets = @import("widgets.zig");
pub const keyboard = @import("keyboard.zig");
pub const Renderer = @import("renderer.zig").Renderer;
pub const platform = if (@import("fascia_options").desktop) @import("desktop.zig") else @import("platform.zig");
pub const persist = @import("persist.zig");
pub const display = @import("display-colour.zig");
pub const Context = @import("runtime.zig").Context;
pub const run = @import("runtime.zig").run;
pub const screenshot = @import("screenshot.zig");
