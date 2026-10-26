#!/usr/bin/env python3
"""Finalize a marked, offline ARM root from an explicit prebuilt public release."""

import argparse
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import tempfile

PROJECT = Path(__file__).resolve().parent.parent
MARKER = ".fascia-image-build-root"


class Root:
    def __init__(self, path, ownership=True):
        original = Path(path)
        if not original.is_absolute() or original.is_symlink():
            raise ValueError("root must be an absolute real directory")
        self.path = original.resolve(strict=True)
        if len(self.path.parts) < 3 or self.path in (
            Path("/home/fascia"),
            Path("/var/lib/fascia"),
        ):
            raise ValueError("refusing a live or broad filesystem root")
        marker = self.path / MARKER
        if (
            marker.is_symlink()
            or not stat.S_ISREG(marker.stat().st_mode)
            or marker.stat().st_size > 64
            or marker.read_bytes() != b"isolated\n"
        ):
            raise ValueError("prepared isolated-root marker missing or invalid")
        self.ownership = ownership
        # Do not operate across bind mounts into live/persistent storage.
        mountinfo = Path("/proc/self/mountinfo")
        if mountinfo.exists():
            for line in mountinfo.read_text().splitlines():
                mount = Path(line.split()[4].replace("\\040", " "))
                if mount != self.path and mount.is_relative_to(self.path):
                    raise ValueError(
                        "unmount nested filesystems before finalization: " + str(mount)
                    )

    def at(self, relative):
        relative = PurePosixPath(relative)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("unsafe root path")
        path = self.path
        for component in relative.parts[:-1]:
            path /= component
            if path.is_symlink():
                raise ValueError("symlink ancestor in destination: " + str(path))
        return self.path.joinpath(*relative.parts)

    def directory(self, relative, mode=0o755, owner=(0, 0)):
        path = self.at(relative)
        if path.is_symlink():
            raise ValueError("destination directory cannot be a symlink")
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(mode)
        if self.ownership:
            os.chown(path, *owner)
        return path

    def write(self, relative, data, mode=0o644):
        path = self.at(relative)
        self.directory(str(PurePosixPath(relative).parent))
        fd, temporary = tempfile.mkstemp(prefix=".fascia-install-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                os.fchmod(stream.fileno(), mode)
                if self.ownership:
                    os.fchown(stream.fileno(), 0, 0)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def copy(self, source, relative, mode=0o644):
        source = Path(source)
        if source.is_symlink() or not source.is_file():
            raise ValueError("release input must be a regular file: " + str(source))
        self.write(relative, source.read_bytes(), mode)

    def link(self, relative, target):
        path = self.at(relative)
        self.directory(str(PurePosixPath(relative).parent))
        path.unlink(missing_ok=True)
        path.symlink_to(target)
        if self.ownership:
            os.lchown(path, 0, 0)

    def remove(self, relative):
        path = self.at(relative)
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.exists():
            shutil.rmtree(path)


def arm_executable(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError("missing regular release executable: " + str(path))
    with path.open("rb") as stream:
        header = stream.read(52)
    if (
        len(header) < 52
        or header[:7] != b"\x7fELF\x01\x01\x01"
        or header[18:20] != b"\x28\x00"
    ):
        raise ValueError("expected ARM32 little-endian executable: " + str(path))


def validate_release(release):
    if (release / "apps").exists() or (release / "apps").is_symlink():
        raise ValueError("OS releases must not contain apps; build examples separately")
    for name in (
        "fascia-host",
        "fascia-brightness",
        "fascia-home-button",
    ):
        arm_executable(release / "bin" / name)
    for name in ("fascia-home", "fascia-compositor"):
        path = release / "bin" / name
        if path.exists() or path.is_symlink():
            raise ValueError(
                "Standalone debug executables must not ship in OS releases: " + name
            )


def validate_initial_apps(directory):
    directory = Path(directory)
    spec = importlib.util.spec_from_file_location(
        "fascia_initial_plan", PROJECT / "image/fascia-state-init.py"
    )
    state = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(state)
    plan = state.read_plan(directory)
    if plan is None:
        raise ValueError("missing initial app package")
    bundles = directory / "apps"
    if (
        bundles.is_symlink()
        or not bundles.is_dir()
        or {path.name for path in directory.iterdir()} != {"manifest.json", "apps"}
        or {path.name for path in bundles.iterdir()} != set(plan["apps"])
    ):
        raise ValueError("initial app package does not match its manifest")
    for app in plan["apps"]:
        bundle = bundles / app
        if bundle.is_symlink() or not bundle.is_dir():
            raise ValueError("initial app bundle must be a regular directory")
        spec = importlib.util.spec_from_file_location(
            "fascia_app", PROJECT / "tools/fascia-app.py"
        )
        app_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(app_module)
        # Use the same validation as installation, including contained links.
        with tempfile.TemporaryDirectory(prefix="fascia-app-check-") as temporary:
            info, _ = app_module.copy_bundle(bundle, Path(temporary))
            if info["id"] != app:
                raise ValueError("factory app id does not match directory")
    return plan


def accounts(root):
    passwd = root.at("etc/passwd").read_text().splitlines()
    retained = []
    for line in passwd:
        fields = line.split(":")
        if len(fields) != 7:
            raise ValueError("invalid passwd entry")
        name, uid = fields[0], int(fields[2])
        if name in ("alarm", "fascia"):
            continue
        if uid >= 1000 and name != "nobody":
            raise ValueError("unexpected interactive account in image: " + name)
        retained.append(line)
    retained.append("fascia:x:1000:1000:Fascia:/home/fascia:/bin/bash")
    root.write("etc/passwd", ("\n".join(retained) + "\n").encode())
    names = {line.split(":")[0] for line in retained}
    shadow = {}
    for line in root.at("etc/shadow").read_text().splitlines():
        fields = line.split(":")
        if len(fields) != 9:
            raise ValueError("invalid shadow entry")
        if fields[0] in names:
            fields[1] = "!"
            shadow[fields[0]] = ":".join(fields)
    shadow["fascia"] = "fascia:!::0:99999:7:::"
    if "root" not in shadow:
        raise ValueError("missing root account")
    root.write("etc/shadow", ("\n".join(shadow.values()) + "\n").encode(), 0o600)
    groups = []
    occupied = set()
    for line in root.at("etc/group").read_text().splitlines():
        fields = line.split(":")
        if len(fields) != 4:
            raise ValueError("invalid group entry")
        if fields[0] in ("alarm", "fascia"):
            continue
        if int(fields[2]) == 1000:
            raise ValueError("GID1000 already belongs to another group")
        fields[1] = "x"
        fields[3] = ",".join(
            name for name in fields[3].split(",") if name in names and name != "fascia"
        )
        groups.append(fields)
        occupied.add(int(fields[2]))
    groups.append(["fascia", "x", "1000", ""])
    occupied.add(1000)
    for name in (
        "audio",
        "input",
        "render",
        "uucp",
        "video",
        "seat",
        "systemd-journal",
    ):
        selected = next((g for g in groups if g[0] == name), None)
        if selected is None:
            gid = next(i for i in range(2000, 65000) if i not in occupied)
            occupied.add(gid)
            selected = [name, "x", str(gid), ""]
            groups.append(selected)
        selected[3] = ",".join(filter(None, [selected[3], "fascia"]))
    root.write("etc/group", ("\n".join(":".join(g) for g in groups) + "\n").encode())
    root.write(
        "etc/gshadow",
        ("\n".join(g[0] + ":!::" + g[3] for g in groups) + "\n").encode(),
        0o600,
    )


def finalize(root, release, initial_apps=None):
    validate_release(release)
    plan = validate_initial_apps(initial_apps) if initial_apps is not None else None
    accounts(root)
    root.copy(release / "bin/fascia-host", "usr/lib/fascia/bin/fascia-host", 0o755)
    for name in ("fascia-brightness", "fascia-home-button"):
        root.copy(release / "bin" / name, "usr/local/libexec/" + name, 0o755)
    for name in ("fascia", "fascia-serial-session"):
        root.copy(release / "tools" / (name + ".py"), "usr/local/bin/" + name, 0o755)
    root.copy(release / "tools/fascia-app.py", "usr/local/bin/fascia-app", 0o755)
    for name in ("system", "setup"):
        root.copy(
            release / "tools" / ("fascia-" + name + ".py"),
            "usr/local/libexec/fascia-" + name + "-root",
            0o755,
        )
        root.write(
            "usr/bin/fascia-" + name,
            (
                "#!/bin/sh\nexec sudo -n /usr/local/libexec/fascia-"
                + name
                + '-root "$@"\n'
            ).encode(),
            0o755,
        )
        root.link("usr/local/bin/fascia-" + name, "/usr/bin/fascia-" + name)
    root.copy(release / "scripts/fascia-display", "usr/local/bin/fascia-display", 0o755)
    root.copy(
        release / "scripts/profile.d/90-fascia-serial.sh",
        "etc/profile.d/90-fascia-serial.sh",
    )
    root.copy(
        release / "share/fonts/DejaVuSans.ttf", "usr/local/share/fascia/DejaVuSans.ttf"
    )
    for path in (release / "share/licenses").rglob("*"):
        if path.is_file():
            root.copy(
                path,
                "usr/local/share/licenses/fascia/"
                + str(path.relative_to(release / "share/licenses")),
            )
    if (release / "LICENSE").is_file():
        root.copy(release / "LICENSE", "usr/local/share/licenses/fascia/LICENSE")
    # Initial apps are a separate image input, never an OS release requirement.
    root.remove("usr/share/fascia/factory-apps")
    root.remove("usr/share/fascia/image-defaults.json")
    if plan is not None:
        for path in initial_apps.rglob("*"):
            if path.is_symlink():
                root.link(
                    "usr/share/fascia/factory-apps/"
                    + str(path.relative_to(initial_apps)),
                    os.readlink(path),
                )
            elif path.is_file():
                mode = 0o755 if path.stat().st_mode & 0o111 else 0o644
                root.copy(
                    path,
                    "usr/share/fascia/factory-apps/"
                    + str(path.relative_to(initial_apps)),
                    mode,
                )
    for name in (
        "fascia_identity.py",
        "fascia_ssh.py",
        "extract_gsl.py",
        "fascia-provision.py",
        "vendor-files.json",
    ):
        root.copy(
            PROJECT / "image/provision" / name, "usr/lib/fascia/provision/" + name
        )
    root.copy(
        PROJECT / "image/provision/fascia-identity-generator.py",
        "usr/lib/systemd/system-generators/fascia-identity-generator",
        0o755,
    )
    defaults = PROJECT / "image/boards/pf1007l/defaults"
    for path in defaults.rglob("*"):
        if path.is_file():
            root.copy(path, "etc/" + str(path.relative_to(defaults)))
    pacman = root.at("etc/pacman.conf")
    if pacman.exists():
        root.write(
            "etc/pacman.conf",
            pacman.read_bytes()
            .replace(b"/build/mirrorlist", b"/etc/pacman.d/mirrorlist")
            .replace(
                b"IgnorePkg = linux-armv7 linux-armv7-headers linux-firmware*\n", b""
            )
            .replace(
                b"NoExtract = usr/lib/firmware/* !usr/lib/firmware/regulatory.db*\n",
                b"",
            ),
        )
    root.copy(
        PROJECT / "image/fascia-state-init.py", "usr/lib/fascia/fascia-state-init.py"
    )
    root.copy(
        PROJECT / "image/fascia-grow-storage.py",
        "usr/lib/fascia/fascia-grow-storage.py",
    )
    root.copy(
        PROJECT / "image/tools/build-initramfs.py",
        "usr/lib/fascia/build-initramfs",
        0o755,
    )
    root.copy(
        PROJECT / "image/arch/fascia-board-refresh.py",
        "usr/lib/fascia/fascia-board-refresh",
        0o755,
    )
    root.copy(
        PROJECT / "image/arch/00-fascia-board-protect.hook",
        "usr/share/libalpm/hooks/00-fascia-board-protect.hook",
    )
    root.copy(
        PROJECT / "image/arch/99-fascia-board-refresh.hook",
        "usr/share/libalpm/hooks/99-fascia-board-refresh.hook",
    )
    for name in ("init", "fascia-gadget", "fascia-emmc-readonly"):
        root.copy(
            PROJECT / "image/initramfs" / name,
            "usr/share/fascia/initramfs/" + name,
            0o755,
        )
    root.copy(
        PROJECT / "image/system/fascia-boot-select.sh",
        "usr/share/fascia/fascia-boot-select.sh",
        0o755,
    )
    root.copy(
        PROJECT / "image/system/00-fascia-recovery.hook",
        "etc/pacman.d/hooks/00-fascia-recovery.hook",
    )
    for name in ("fascia-system-update.py", "fascia-system-health.sh"):
        installed = (
            "fascia-system-update" if name.endswith(".py") else "fascia-system-health"
        )
        root.copy(
            PROJECT / "image/system" / name, "usr/local/libexec/" + installed, 0o755
        )
    for name in ("fascia-system-health.service", "fascia-system-health.timer"):
        root.copy(PROJECT / "image/system" / name, "usr/lib/systemd/system/" + name)
    root.link(
        "etc/systemd/system/timers.target.wants/fascia-system-health.timer",
        "/usr/lib/systemd/system/fascia-system-health.timer",
    )
    # Keep the app manager alive after the last SSH/serial login ends.
    root.write("var/lib/systemd/linger/fascia", b"")
    root.copy(
        PROJECT / "image/fascia-session.target",
        "usr/lib/systemd/user/fascia-session.target",
    )
    root.copy(PROJECT / "image/run-home.sh", "usr/lib/fascia/run-home", 0o755)
    for name in ("fascia-provision",):
        root.copy(
            PROJECT / "image/provision" / (name + ".service"),
            "usr/lib/systemd/system/" + name + ".service",
        )
    for name in (
        "fascia-grow-storage",
        "fascia-pacman-init",
        "fascia-state-init",
        "pf-kiosk",
    ):
        root.copy(
            PROJECT / "image" / (name + ".service"),
            "usr/lib/systemd/system/" + name + ".service",
        )
    for name in ("fascia-brightness", "fascia-home-button", "fascia-device-state"):
        root.copy(
            release / "scripts" / (name + ".service"),
            "usr/lib/systemd/system/" + name + ".service",
        )
    for unit in ("fascia-provision.service",):
        root.link(
            "etc/systemd/system/sysinit.target.wants/" + unit,
            "/usr/lib/systemd/system/" + unit,
        )
    for unit in (
        "fascia-grow-storage.service",
        "fascia-pacman-init.service",
        "fascia-state-init.service",
        "fascia-device-state.service",
        "fascia-brightness.service",
        "pf-kiosk.service",
        "seatd.service",
        "iwd.service",
        "avahi-daemon.service",
        "systemd-networkd.service",
        "systemd-resolved.service",
        "systemd-timesyncd.service",
    ):
        root.link(
            "etc/systemd/system/multi-user.target.wants/" + unit,
            "/usr/lib/systemd/system/" + unit,
        )
    root.link(
        "etc/systemd/system/getty.target.wants/serial-getty@ttyGS0.service",
        "/usr/lib/systemd/system/serial-getty@.service",
    )
    root.write(
        "etc/systemd/system/serial-getty@ttyGS0.service.d/10-fascia-setup.conf",
        b"[Service]\nExecStart=\nExecStart=-/usr/bin/agetty --autologin fascia --noclear --keep-baud 115200,57600,38400,9600 - vt100\n",
    )
    root.write(
        "etc/tmpfiles.d/fascia.conf",
        b"d /run/fascia-home 0755 fascia fascia -\nd /run/fascia 0755 root root -\n",
    )
    root.copy(
        release / "system/40-fascia-volatile.conf",
        "etc/systemd/journald.conf.d/40-fascia-volatile.conf",
    )
    sudoers = root.at("etc/sudoers")
    base_sudoers = (
        sudoers.read_text() if sudoers.exists() else "root ALL=(ALL:ALL) ALL\n"
    )
    if not any(
        line.strip() in ("@includedir /etc/sudoers.d", "#includedir /etc/sudoers.d")
        for line in base_sudoers.splitlines()
    ):
        base_sudoers += "\n@includedir /etc/sudoers.d\n"
    root.write("etc/sudoers", base_sudoers.encode(), 0o440)
    root.write(
        "etc/sudoers.d/fascia-home",
        b"fascia ALL=(root) NOPASSWD: /usr/local/libexec/fascia-system-root *, /usr/local/libexec/fascia-setup-root *, /usr/bin/pacman *, /usr/bin/systemctl reboot, /usr/bin/systemctl poweroff\n",
        0o440,
    )
    ssh_config = root.at("etc/ssh/sshd_config")
    old_config = ssh_config.read_text() if ssh_config.exists() else ""
    # Put the factory policy first: OpenSSH takes the first obtained value.
    root.write(
        "etc/ssh/sshd_config",
        (
            "Include /etc/ssh/sshd_config.d/40-fascia.conf\n"
            + old_config.replace("Include /etc/ssh/sshd_config.d/40-fascia.conf\n", "")
        ).encode(),
    )
    root.write(
        "etc/ssh/sshd_config.d/40-fascia.conf",
        b"PasswordAuthentication no\nKbdInteractiveAuthentication no\nPermitRootLogin no\nPubkeyAuthentication yes\nAllowUsers fascia\nUsePAM yes\n",
    )
    root.write(
        "etc/systemd/network/20-fascia-wlan.network",
        b"[Match]\nName=wlan*\n\n[Network]\nDHCP=yes\n",
    )
    root.write(
        "etc/systemd/network/21-fascia-usb.network",
        b"[Match]\nName=usb0\n\n[Network]\nAddress=10.55.0.1/24\nLinkLocalAddressing=ipv6\nIPv6AcceptRA=no\n",
    )
    root.write(
        "etc/iwd/main.conf",
        b"[General]\nEnableNetworkConfiguration=false\n\n[Scan]\nDisableRoamingScan=true\n",
    )
    root.write(
        "etc/fstab",
        b"# Root, /boot, /data, persistent binds and tmpfs mounts are established by the Fascia initramfs.\n# Do not add overlapping mount definitions.\n",
    )
    root.link("etc/resolv.conf", "/run/systemd/resolve/stub-resolv.conf")
    root.write(
        "etc/udev/rules.d/55-fascia-emmc-readonly.rules",
        b'SUBSYSTEM=="block", KERNEL=="mmcblk*", ENV{DEVPATH}=="*/1c11000.mmc/*", ENV{UDISKS_IGNORE}="1", RUN+="/usr/bin/blockdev --setro /dev/%k"\n',
    )
    root.write(
        "etc/fascia-system.json",
        b'{"board_id":"pf1007l-pl5","shared_state_schema":1}\n',
    )
    for directory in (
        "data",
        "system",
        "run",
        "var/lib/systemd/timesync",
        "var/lib/lastlog",
    ):
        root.directory(directory)
    # The fascia account has a persistent app manager, including without SSH.
    root.directory("var/lib/systemd/linger", 0o755, owner=(0, 0))
    root.write("etc/hostname", b"fascia\n")
    root.write("etc/machine-id", b"")
    root.link("var/lib/dbus/machine-id", "/etc/machine-id")
    root.directory("home/fascia", owner=(1000, 1000))
    for path in (
        "build",
        "boot/initramfs-linux.img",
        "boot/initramfs-linux-fallback.img",
        "root/.cache",
        "var/cache/pacman/pkg",
        "root/.ssh",
        "root/.gnupg",
        "root/.bash_history",
        "home/alarm",
        "home/fascia/.ssh",
        "home/fascia/.bash_history",
        "etc/pacman.d/gnupg",
        "var/lib/systemd/random-seed",
        "etc/shadow-",
        "etc/gshadow-",
        "etc/passwd-",
        "etc/group-",
    ):
        root.remove(path)
    # Build-root HOME and scratch space are not part of the runtime payload.
    root.directory("root", 0o700)
    for path in list(root.at("root").iterdir()):
        root.remove(str(path.relative_to(root.path)))
    for directory, mode in (
        ("tmp", 0o1777),
        ("var/tmp", 0o1777),
        ("var/cache", 0o755),
        ("var/log", 0o755),
    ):
        root.remove(directory)
        root.directory(directory, mode)
    for path in root.at("etc/ssh").glob("ssh_host_*"):
        root.remove(str(path.relative_to(root.path)))
    for directory in ("var/lib/iwd", "var/lib/fascia"):
        path = root.at(directory)
        if path.exists() and any(path.iterdir()):
            raise ValueError("refusing to ship persistent device state: " + directory)
        root.directory(directory)
    for unit in ("sshd.service", "fascia-home-button.service"):
        root.remove("etc/systemd/system/multi-user.target.wants/" + unit)
    # Fascia manages device state; disable duplicate persistence services.
    for unit in (
        "systemd-random-seed.service",
        "archlinux-keyring-wkd-sync.timer",
        "archlinux-keyring-wkd-sync.service",
        "shadow.timer",
        "shadow.service",
    ):
        path = root.at("etc/systemd/system/" + unit)
        if path.is_symlink() and os.readlink(path) == "/dev/null":
            root.remove("etc/systemd/system/" + unit)
    for unit in (
        "pacman-init.service",
        "systemd-firstboot.service",
        "systemd-coredump@.service",
        "systemd-journal-catalog-update.service",
        "systemd-update-done.service",
        # Networking uses /run; these optional persistence helpers are unnecessary.
        "systemd-networkd-persistent-storage.service",
        # Fascia owns brightness persistence; avoid a competing boot/shutdown restore.
        "systemd-backlight@.service",
        "systemd-rfkill.service",
        "systemd-rfkill.socket",
        "systemd-pstore.service",
    ):
        root.link("etc/systemd/system/" + unit, "/dev/null")
    # Disable upstream socket activation so SSH follows the user's service setting.
    root.link("etc/systemd/system-generators/systemd-ssh-generator", "/dev/null")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument(
        "--initial-apps", type=Path, help="First-image apps; omitted for OS-only roots"
    )
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error(
            "finalization requires root ownership; use only a disposable marked root"
        )
    root = Root(args.root)
    finalize(
        root,
        args.release.resolve(strict=True),
        args.initial_apps.resolve(strict=True) if args.initial_apps else None,
    )
    print("Prepared Fascia root finalized; no image was mounted or device written.")


if __name__ == "__main__":
    main()
