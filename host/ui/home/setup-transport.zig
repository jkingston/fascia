//! One bounded privileged request over an anonymous socket. Secrets never enter argv.
const std = @import("std");
const builtin = @import("builtin");
const c = @cImport({
    @cDefine("_GNU_SOURCE", "1");
    @cInclude("unistd.h");
    @cInclude("sys/socket.h");
    @cInclude("sys/wait.h");
    @cInclude("fcntl.h");
    @cInclude("signal.h");
    @cInclude("errno.h");
});

pub const Job = struct {
    fd: c_int,
    pid: c_int,
    deadline: i64,
    bytes: [65536]u8 = undefined,
    len: usize = 0,
    eof: bool = false,

    pub fn start(payload: []const u8, now: i64) !Job {
        if (payload.len > 20000) return error.RequestTooLarge;
        var pair: [2]c_int = undefined;
        if (c.socketpair(c.AF_UNIX, c.SOCK_STREAM, 0, &pair) != 0) return error.Socket;
        errdefer {
            _ = c.close(pair[0]);
            _ = c.close(pair[1]);
        }
        for (pair) |fd| _ = c.fcntl(fd, c.F_SETFD, c.FD_CLOEXEC);
        if (comptime builtin.os.tag == .macos) {
            var one: c_int = 1;
            _ = c.setsockopt(pair[0], c.SOL_SOCKET, c.SO_NOSIGPIPE, &one, @sizeOf(c_int));
        }
        const pid = c.fork();
        if (pid < 0) return error.Fork;
        if (pid == 0) {
            _ = c.close(pair[0]);
            var empty: c.sigset_t = undefined;
            _ = c.sigemptyset(&empty);
            _ = c.sigprocmask(c.SIG_SETMASK, &empty, null);
            _ = c.setsid();
            _ = c.dup2(pair[1], 0);
            _ = c.dup2(pair[1], 1);
            _ = c.close(pair[1]);
            const null_fd = c.open("/dev/null", c.O_WRONLY);
            if (null_fd >= 0) {
                _ = c.dup2(null_fd, 2);
                _ = c.close(null_fd);
            }
            const path = "/usr/local/bin/fascia-setup";
            var args = [_:null]?[*:0]const u8{ path, "--request" };
            _ = c.execv(path, @ptrCast(&args));
            c._exit(127);
        }
        var sent: usize = 0;
        while (sent < payload.len) {
            const n = c.send(pair[0], payload[sent..].ptr, payload.len - sent, if (comptime builtin.os.tag == .linux) c.MSG_NOSIGNAL else 0);
            if (n <= 0) {
                _ = c.kill(pid, c.SIGKILL);
                _ = c.waitpid(pid, null, 0);
                return error.Send;
            }
            sent += @intCast(n);
        }
        _ = c.shutdown(pair[0], c.SHUT_WR);
        _ = c.close(pair[1]);
        _ = c.fcntl(pair[0], c.F_SETFL, c.O_NONBLOCK);
        return .{ .fd = pair[0], .pid = pid, .deadline = now + 65_000_000 };
    }

    pub fn deinit(self: *Job) void {
        _ = c.close(self.fd);
        if (self.pid > 0) {
            _ = c.kill(self.pid, c.SIGKILL);
            _ = c.waitpid(self.pid, null, 0);
        }
        std.crypto.secureZero(u8, &self.bytes);
    }

    pub fn poll(self: *Job, now: i64) !?[]const u8 {
        if (now >= self.deadline) return error.Timeout;
        while (!self.eof) {
            if (self.len == self.bytes.len) return error.ResponseTooLarge;
            const n = c.recv(self.fd, self.bytes[self.len..].ptr, self.bytes.len - self.len, 0);
            if (n == 0) {
                self.eof = true;
                break;
            }
            if (n < 0) {
                const code = if (comptime builtin.os.tag == .macos) c.__error().* else c.__errno_location().*;
                if (code == c.EINTR) continue;
                if (code == c.EAGAIN or code == c.EWOULDBLOCK) break;
                return error.Read;
            }
            self.len += @intCast(n);
        }
        if (self.eof) {
            var status: c_int = 0;
            const waited = c.waitpid(self.pid, &status, c.WNOHANG);
            if (waited == 0) return null;
            self.pid = 0;
            if (waited < 0 or status != 0) return error.Helper;
            return self.bytes[0..self.len];
        }
        return null;
    }
};
