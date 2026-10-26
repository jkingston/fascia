test {
    _ = @import("home/countdown.zig");
    _ = @import("home/model.zig");
    _ = @import("home/system.zig");
    _ = @import("home-apps.zig");
}

test {
    _ = @import("home/display-check.zig");
    _ = @import("fascia").persist;
}

test {
    _ = @import("home/controller.zig");
}
