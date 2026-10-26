const std = @import("std");

pub fn main(init: std.process.Init) !u8 {
    return @import("server.zig").run(init, null);
}
