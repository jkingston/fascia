const std = @import("std");
const c = @cImport({
    @cInclude("stdlib.h");
});

pub fn main(init: std.process.Init) !u8 {
    const args = init.minimal.args.vector;
    if (args.len > 2 or (args.len == 2 and !std.mem.eql(u8, std.mem.span(args[1]), "--display"))) return error.InvalidArguments;
    var home = try @import("fascia-home.zig").Home.init(std.heap.c_allocator, args.len == 2);
    defer home.deinit();
    return @import("server.zig").run(init, home.interface());
}
