#!/usr/bin/env python3
"""Pinned PF1007L bootloader, stock-kernel modules and DTBs; no device access."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import datetime
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request

BOARD = Path(__file__).resolve().parent
IMAGE = BOARD.parent.parent
MODULE = "panel-jadard-jd9365da-h3"
OFFLINE_SOURCES = None


def offline_file(record, boundary):
    name = record.get("file", "")
    relative = Path(name)
    if ".." in relative.parts or not name:
        raise ValueError("Unsafe offline source path")
    path = relative if relative.is_absolute() else boundary / relative
    if (
        path.is_symlink()
        or not path.is_file()
        or not path.resolve().is_relative_to(boundary.resolve())
    ):
        raise ValueError("Missing regular offline source: " + name)
    if digest(path) != record.get("sha256"):
        raise ValueError("Offline source checksum mismatch: " + name)
    return path


def digest(path):
    sha = hashlib.sha256()
    with Path(path).open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            sha.update(block)
    return sha.hexdigest()


def run(args, cwd=None, env=None, log=None):
    if log:
        with Path(log).open("a") as file:
            file.write("$ " + " ".join(map(str, args)) + "\n")
            file.flush()
            result = subprocess.run(
                list(map(str, args)),
                stdin=subprocess.DEVNULL,
                cwd=cwd,
                env=env,
                stdout=file,
                stderr=subprocess.STDOUT,
            )
        if result.returncode:
            print("\n".join(Path(log).read_text(errors="replace").splitlines()[-40:]))
            raise subprocess.CalledProcessError(result.returncode, args)
        return ""
    return subprocess.run(
        list(map(str, args)),
        stdin=subprocess.DEVNULL,
        cwd=cwd,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def fetch(urls, path, sha):
    if path.exists():
        if digest(path) != sha:
            raise ValueError("Cached input checksum mismatch: " + str(path))
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if OFFLINE_SOURCES is not None:
        manifest, boundary = OFFLINE_SOURCES
        records = [
            r
            for r in [
                *manifest.get("files", []),
                *manifest.get("archives", {}).values(),
            ]
            if r.get("sha256") == sha
        ]
        if not records:
            raise ValueError(
                "Required source absent from offline manifest: " + str(path)
            )
        source = offline_file(records[0], boundary)
        shutil.copy2(source, path)
        return
    failure = None
    for url in urls:
        temporary = path.with_name(path.name + ".download-" + os.urandom(6).hex())
        try:
            with (
                urllib.request.urlopen(url, timeout=180) as source,
                temporary.open("wb") as output,
            ):
                shutil.copyfileobj(source, output, 1024 * 1024)
            if digest(temporary) != sha:
                raise ValueError("Downloaded input checksum mismatch")
            os.replace(temporary, path)
            return
        except (OSError, ValueError) as error:
            failure = error
        finally:
            temporary.unlink(missing_ok=True)
    raise ValueError(f"Unable to fetch verified source {path.name}: {failure}")


def git_archive(item, sources, output, name):
    commit = item["commit"]
    if len(commit) != 40 or any(c not in "0123456789abcdef" for c in commit):
        raise ValueError("Unpinned Git source")
    if OFFLINE_SOURCES is not None:
        manifest, boundary = OFFLINE_SOURCES
        record = manifest.get("archives", {}).get(name, {})
        if record.get("commit") != commit:
            raise ValueError("Offline source commit mismatch: " + name)
        source = offline_file(record, boundary)
        archive = output / (name + "-" + commit + ".tar")
        shutil.copy2(source, archive)
        return archive
    repository = sources / (name + ".git")
    if not repository.exists():
        run(["git", "init", "--bare", repository])
    try:
        resolved = run(
            ["git", "--git-dir", repository, "rev-parse", commit + "^{commit}"]
        )
    except subprocess.CalledProcessError:
        run(
            [
                "git",
                "--git-dir",
                repository,
                "fetch",
                "--depth",
                "1",
                item["git"],
                commit,
            ]
        )
        resolved = run(["git", "--git-dir", repository, "rev-parse", "FETCH_HEAD"])
    if resolved != commit:
        raise ValueError("Git commit mismatch")
    archive = output / (name + "-" + commit + ".tar")
    run(
        [
            "git",
            "--git-dir",
            repository,
            "archive",
            "--format=tar",
            "--output",
            archive,
            commit,
        ]
    )
    return archive


def unpack(archive, destination):
    destination.mkdir(parents=True, exist_ok=True)
    # Inputs are pinned public archives, never arbitrary update bundles.
    run(["tar", "-xf", archive, "-C", destination])


def module_check(path, stock, kernel_symbols, extra_symbols, nm, log):
    ours = run(["modinfo", "-F", "vermagic", path])
    theirs = run(["modinfo", "-F", "vermagic", stock])
    if ours != theirs:
        raise ValueError("Module vermagic mismatch: " + path.name)
    exported = {}
    for file in [kernel_symbols] + extra_symbols:
        for line in file.read_text().splitlines():
            fields = line.split()
            if len(fields) >= 2:
                exported[fields[1]] = int(fields[0], 16)
    undefined = {
        line.split()[-1] for line in run([nm, "-u", path]).splitlines() if line.split()
    }
    missing = undefined - exported.keys()
    if missing:
        raise ValueError("Unexported module symbols: " + ",".join(sorted(missing)))
    versions = {}
    for line in run(["modprobe", "--dump-modversions", path]).splitlines():
        crc, symbol = line.split()[:2]
        versions[symbol] = int(crc, 16)
    if not versions or any(
        exported.get(symbol) != crc for symbol, crc in versions.items()
    ):
        raise ValueError("Missing or incompatible module symbol CRCs")
    info = {
        "file": path.name,
        "vermagic": ours,
        "license": run(["modinfo", "-F", "license", path]),
        "symbol_crc_entries": len(versions),
        "undefined_symbols": len(undefined),
    }
    log.write(json.dumps(info) + "\n")
    log.flush()
    return info


def native_helpers(headers, host_tools):
    for relative in (
        "scripts/basic/fixdep",
        "scripts/mod/modpost",
        "scripts/dtc/dtc",
        "scripts/genksyms/genksyms",
        "scripts/recordmcount",
        "scripts/sign-file",
    ):
        if not (headers / relative).is_file():
            continue
        tool = headers / relative
        if host_tools:
            replacement = Path(host_tools) / relative
            if not replacement.is_file():
                raise ValueError(
                    "Missing prepared native kernel helper: " + str(replacement)
                )
            shutil.copy2(replacement, tool)
        # All accept an invalid invocation with a normal exit, but must execute natively.
        try:
            subprocess.run(
                [str(tool)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as error:
            raise ValueError(
                "Kernel helper cannot execute on builder; use ARMv7 buildroot or --host-tools prepared from matching 7.2.10 source"
            ) from error


def installed_kernel(root, reviewed_release):
    """Use only the kernel/header pair already installed by signature-checked pacman."""
    root = Path(root).resolve(strict=True)
    versions = {}
    for line in run(
        ["pacman", "--root", root, "-Q", "linux-armv7", "linux-armv7-headers"]
    ).splitlines():
        name, version = line.split()
        versions[name] = version
    if (
        set(versions) != {"linux-armv7", "linux-armv7-headers"}
        or len(set(versions.values())) != 1
    ):
        raise ValueError(
            "Installed linux-armv7 and linux-armv7-headers versions must match"
        )
    candidates = []
    for path in (root / "usr/lib/modules").glob(
        "*/build/include/config/kernel.release"
    ):
        headers = path.parents[2]
        if not headers.resolve().is_relative_to(root):
            raise ValueError("Installed headers escape kernel root")
        release = path.read_text().strip()
        if release != headers.parent.name:
            raise ValueError("Installed header release disagrees with module directory")
        candidates.append((release, headers))
    if len(candidates) != 1:
        raise ValueError("Expected exactly one installed kernel/header release")
    release, headers = candidates[0]
    # Reviewed source is an input version, not the installed package version.
    # Permit maintenance releases, while compilation, symbol CRCs, bindings and
    # stock-DTB byte identity still gate publication against the installed kernel.
    family = re.match(r"^(\d+\.\d+)\.", reviewed_release)
    actual_family = re.match(r"^(\d+\.\d+)\.", release)
    if (
        family is None
        or actual_family is None
        or family.group(1) != actual_family.group(1)
    ):
        raise ValueError(
            f"Installed kernel {release} is outside the reviewed maintenance family "
            f"of board source {reviewed_release}; review newer-family support before installation. "
            "No older kernel will be downloaded."
        )
    relatives = [
        f"usr/lib/modules/{release}/build/.config",
        f"usr/lib/modules/{release}/build/Module.symvers",
        f"usr/lib/modules/{release}/build/include/config/kernel.release",
        f"usr/lib/modules/{release}/build/include/generated/utsrelease.h",
        f"usr/lib/modules/{release}/kernel/drivers/gpu/drm/panel/panel-lvds.ko",
        "boot/dtbs/sun8i-a33-ga10h-v1.1.dtb",
    ]
    hashes = {}
    for relative in relatives:
        path = root / relative
        if not path.is_file() or not path.resolve().is_relative_to(root):
            raise ValueError("Missing installed kernel reference: " + relative)
        hashes[relative] = digest(path)
    return {
        "root": root,
        "headers": headers,
        "release": release,
        "packages": versions,
        "reference_hashes": hashes,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 1)
    parser.add_argument("--cross-compile", default=os.environ.get("CROSS_COMPILE", ""))
    parser.add_argument(
        "--host-tools",
        type=Path,
        help="Native scripts/{basic/fixdep,mod/modpost,dtc/dtc}, built from matching kernel source; needed for non-ARM hosts",
    )
    parser.add_argument(
        "--only", choices=("all", "uboot", "kernel", "wifi", "dtb"), default="all"
    )
    parser.add_argument(
        "--offline-source-manifest",
        type=Path,
        help="Verified retained source manifest; refuse all network fetches",
    )
    parser.add_argument(
        "--kernel-root",
        "--installed-root",
        type=Path,
        help="Root with matching kernel and headers installed by normal signed pacman",
    )
    parser.add_argument(
        "--locked-kernel-packages",
        action="store_true",
        help="Explicit legacy mode: fetch the archived reviewed kernel/header package pair",
    )
    args = parser.parse_args()
    global OFFLINE_SOURCES
    if args.offline_source_manifest is not None:
        manifest_path = args.offline_source_manifest.resolve(strict=True)
        manifest = json.loads(manifest_path.read_text())
        if manifest.get("schema_version") != 1:
            parser.error("Unsupported offline source manifest")
        # Manifest may live in RAM; every source must remain below the explicit
        # read-only cache boundary recorded by the caller.
        boundary = Path(manifest.get("cache_root", manifest_path.parent))
        OFFLINE_SOURCES = (manifest, boundary)
    if platform.system() != "Linux":
        parser.error(
            "Run inside a native Linux buildroot; macOS is not a Linux kernel build host"
        )
    if platform.machine() not in ("armv7l", "armv7") and not args.cross_compile:
        parser.error("Non-ARMv7 Linux requires an explicit --cross-compile prefix")
    if args.jobs < 1:
        parser.error("--jobs must be positive")
    sources = args.source_dir.resolve()
    cache = args.cache_dir.resolve()
    out = args.output_dir.resolve()
    for directory in (sources, cache, out.parent):
        directory.mkdir(parents=True, exist_ok=True)
    if out.exists():
        parser.error(
            "Output must not exist; choose a new output directory to avoid mixing old artifacts"
        )
    lock = json.loads((IMAGE / "sources.lock.json").read_text())
    if (
        lock["board_id"] != "pf1007l-pl5"
        or lock["kernel_release"] != "7.2.10-1-armv7-ARCH"
    ):
        parser.error("Unsupported board/kernel source lock")
    if args.kernel_root is not None and args.locked_kernel_packages:
        parser.error(
            "Choose installed kernel root or explicit legacy packages, not both"
        )
    if (
        args.only != "uboot"
        and args.kernel_root is None
        and not args.locked_kernel_packages
    ):
        parser.error(
            "--kernel-root is required; archived package fetching needs explicit --locked-kernel-packages"
        )
    installed = (
        installed_kernel(args.kernel_root, lock["kernel_release"])
        if args.kernel_root is not None
        else None
    )
    krel = installed["release"] if installed is not None else lock["kernel_release"]
    cross = args.cross_compile
    env = dict(
        os.environ,
        CROSS_COMPILE=cross,
        ARCH="arm",
        SOURCE_DATE_EPOCH=str(lock["uboot"]["source_date_epoch"]),
        KBUILD_BUILD_USER="fascia-builder",
        KBUILD_BUILD_HOST="fascia-builder",
        KBUILD_BUILD_VERSION="1",
        KBUILD_BUILD_TIMESTAMP=datetime.datetime.fromtimestamp(
            lock["uboot"]["source_date_epoch"], datetime.timezone.utc
        ).strftime("%a %b %d %H:%M:%S UTC %Y"),
    )
    tools = [
        "git",
        "tar",
        "make",
        "patch",
        cross + "gcc",
        cross + "nm",
        "modinfo",
        "modprobe",
    ]
    for tool in tools:
        if not shutil.which(tool):
            parser.error("Missing builder tool: " + tool)
    work = Path(tempfile.mkdtemp(prefix="board-build-", dir=out.parent))
    result = work / "result"
    result.mkdir()
    for directory in ("boot/dtbs", "modules/" + krel + "/updates", "sources", "logs"):
        (result / directory).mkdir(parents=True)
    records = []
    try:
        env["KCFLAGS"] = (
            f"-ffile-prefix-map={work}=/usr/src/fascia-board-build -fdebug-prefix-map={work}=/usr/src/fascia-board-build"
        )
        source_output = result / "sources"
        for name in ("uboot", "wifi"):
            if args.only not in ("all", name):
                continue
            archive = git_archive(lock[name], sources, source_output, name)
            target = work / name
            unpack(archive, target)
            if name == "uboot":
                run(["python3", "-c", "import elftools, libfdt"])
                for patch in ("uboot-board.patch", "uboot-linux.patch"):
                    run(
                        [
                            "patch",
                            "-p1",
                            "--batch",
                            "--forward",
                            "-i",
                            BOARD / "patches" / patch,
                        ],
                        cwd=target,
                    )
                build = work / "uboot-output"
                run(
                    [
                        "make",
                        f"O={build}",
                        "PYTHON_ENABLE=n",
                        "pf1007l_linux_defconfig",
                    ],
                    cwd=target,
                    env=env,
                    log=result / "logs/uboot.log",
                )
                run(
                    ["make", f"O={build}", "PYTHON_ENABLE=n", f"-j{args.jobs}"],
                    cwd=target,
                    env=env,
                    log=result / "logs/uboot.log",
                )
                shutil.copy2(
                    build / "u-boot-sunxi-with-spl.bin",
                    result / "boot/u-boot-sunxi-with-spl.bin",
                )
                shutil.copy2(build / ".config", result / "sources/uboot.config")
        if args.only != "uboot":
            if installed is not None:
                headers_root = work / "headers"
                headers = headers_root / "usr/lib/modules" / krel / "build"
                shutil.copytree(installed["headers"], headers, symlinks=True)
                stock_root = installed["root"]
                (source_output / "installed-kernel.json").write_text(
                    json.dumps(
                        {
                            "kernel_release": krel,
                            "packages": installed["packages"],
                            "reference_hashes": installed["reference_hashes"],
                        },
                        indent=2,
                    )
                    + "\n"
                )
            else:
                for key in ("kernel_headers", "kernel_package"):
                    item = lock[key]
                    fetch([item["url"]], cache / item["file"], item["sha256"])
                headers_root = work / "headers"
                unpack(cache / lock["kernel_headers"]["file"], headers_root)
                headers = headers_root / "usr/lib/modules" / krel / "build"
                shutil.copy2(
                    cache / lock["kernel_headers"]["file"],
                    source_output / lock["kernel_headers"]["file"],
                )
                stock_root = work / "stock"
                unpack(cache / lock["kernel_package"]["file"], stock_root)
            full = lock["kernel_source"]
            full_archive = cache / (
                "linux-" + full["tag"].removeprefix("v") + ".tar.xz"
            )
            fetch([full["archive_url"]], full_archive, full["archive_sha256"])
            shutil.copy2(full_archive, source_output / full_archive.name)
            if platform.machine() not in ("armv7l", "armv7") and not args.host_tools:
                full_root = work / "kernel-full"
                unpack(full_archive, full_root)
                full_source = full_root / ("linux-" + full["tag"].removeprefix("v"))
                helpers = work / "host-helper-build"
                helpers.mkdir()
                shutil.copy2(headers / ".config", helpers / ".config")
                for target in ("olddefconfig", "prepare", "scripts"):
                    run(
                        [
                            "make",
                            "-C",
                            full_source,
                            f"O={helpers}",
                            f"-j{args.jobs}",
                            target,
                        ],
                        env=env,
                        log=result / "logs/host-helpers.log",
                    )
                args.host_tools = helpers
            native_helpers(headers, args.host_tools)
            stock = (
                stock_root
                / "usr/lib/modules"
                / krel
                / "kernel/drivers/gpu/drm/panel/panel-lvds.ko"
            )
            kernel = sources / "linux-v7.2.10"
            kernel.mkdir(exist_ok=True)
            manifest = BOARD / "kernel-sources.sha256"
            listed = []
            for line in manifest.read_text().splitlines():
                sha, name = line.split()
                listed.append(name)
                fetch(
                    [
                        f"https://raw.githubusercontent.com/gregkh/linux/v7.2.10/{name}",
                        f"https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git/plain/{name}?h=v7.2.10",
                    ],
                    kernel / name,
                    sha,
                )
            (kernel / "scripts/dtc/include-prefixes").mkdir(parents=True, exist_ok=True)
            for destination, target in (
                (
                    kernel / "scripts/dtc/include-prefixes/dt-bindings",
                    "../../../include/dt-bindings",
                ),
                (
                    kernel / "include/dt-bindings/input/linux-event-codes.h",
                    "../../uapi/linux/input-event-codes.h",
                ),
            ):
                if destination.is_symlink():
                    if os.readlink(destination) != target:
                        raise ValueError("Unexpected cached source symlink")
                else:
                    destination.symlink_to(target)
            for name in listed:
                if (
                    name.startswith("include/dt-bindings/")
                    and (headers / name).read_bytes() != (kernel / name).read_bytes()
                ):
                    raise ValueError("Pinned DT binding differs from installed headers")
            with tarfile.open(
                source_output / "kernel-v7.2.10-selected.tar", "w"
            ) as archive:
                for name in listed:
                    info = archive.gettarinfo(kernel / name, arcname=name)
                    info.uid = info.gid = 0
                    info.uname = info.gname = ""
                    info.mtime = lock["uboot"]["source_date_epoch"]
                    with (kernel / name).open("rb") as source:
                        archive.addfile(info, source)
            shutil.copy2(manifest, source_output / "kernel-sources.sha256")
            shutil.copytree(BOARD / "patches", source_output / "patches")
            shutil.copytree(BOARD / "dts", source_output / "dts")
            symbol_log = (result / "logs/module-checks.jsonl").open("w")
            try:
                if args.only in ("all", "kernel"):
                    panel = work / "panel"
                    panel.mkdir()
                    relative = f"drivers/gpu/drm/panel/{MODULE}.c"
                    apply = work / "panel-apply"
                    (apply / Path(relative).parent).mkdir(parents=True)
                    shutil.copy2(kernel / relative, apply / relative)
                    run(
                        [
                            "patch",
                            "-p1",
                            "--batch",
                            "--forward",
                            "-i",
                            BOARD / "patches/panel.patch",
                        ],
                        cwd=apply,
                    )
                    shutil.copy2(apply / relative, panel / (MODULE + ".c"))
                    (panel / "Kbuild").write_text("obj-m := " + MODULE + ".o\n")
                    run(
                        [
                            "make",
                            f"-j{args.jobs}",
                            "-C",
                            headers,
                            f"M={panel}",
                            "modules",
                        ],
                        env=env,
                        log=result / "logs/panel.log",
                    )
                    dest = result / "modules" / krel / "updates" / (MODULE + ".ko")
                    shutil.copy2(panel / (MODULE + ".ko"), dest)
                    records.append(
                        module_check(
                            dest,
                            stock,
                            headers / "Module.symvers",
                            [],
                            cross + "nm",
                            symbol_log,
                        )
                    )
                    if "yhk,zr789-sq9365-101" not in run(
                        ["modinfo", "-F", "alias", dest]
                    ):
                        raise ValueError("Panel compatible alias missing")
                if args.only in ("all", "wifi"):
                    wifi = work / "wifi"
                    run(["git", "init", "-q", wifi])
                    driver = lock["wifi"]["driver"]
                    for line in (
                        (wifi / "debian/patches/series").read_text().splitlines()
                    ):
                        patch_name = line.strip()
                        if not patch_name or patch_name.startswith("#"):
                            continue
                        patch = wifi / "debian/patches" / patch_name
                        if any(
                            line.startswith("+++ ") and driver + "/" in line
                            for line in patch.read_text(errors="replace").splitlines()
                        ):
                            run(
                                [
                                    "git",
                                    "apply",
                                    "--whitespace=nowarn",
                                    f"--include={driver}/*",
                                    patch,
                                ],
                                cwd=wifi,
                            )
                    module_dir = wifi / driver / "aic8800"
                    run(
                        [
                            "make",
                            f"-j{args.jobs}",
                            "-C",
                            headers,
                            f"M={module_dir}",
                            "modules",
                        ],
                        env=env,
                        log=result / "logs/wifi.log",
                    )
                    for name in ("aic8800_bsp", "aic8800_fdrv", "aic8800_btlpm"):
                        dest = result / "modules" / krel / "updates" / (name + ".ko")
                        shutil.copy2(module_dir / name / (name + ".ko"), dest)
                        records.append(
                            module_check(
                                dest,
                                stock,
                                headers / "Module.symvers",
                                [module_dir / "Module.symvers"],
                                cross + "nm",
                                symbol_log,
                            )
                        )
                if args.only in ("all", "kernel", "dtb"):
                    from check_board_dtb import check

                    inc = kernel / "scripts/dtc/include-prefixes"
                    allwinner = kernel / "arch/arm/boot/dts/allwinner"
                    dtc = headers / "scripts/dtc/dtc"

                    def dtb(source, destination):
                        preprocessed = destination.with_suffix(".pp")
                        run(
                            [
                                cross + "gcc",
                                "-E",
                                "-nostdinc",
                                "-I",
                                inc,
                                "-I",
                                allwinner,
                                "-I",
                                BOARD / "dts",
                                "-undef",
                                "-D__DTS__",
                                "-x",
                                "assembler-with-cpp",
                                "-o",
                                preprocessed,
                                source,
                            ]
                        )
                        run(
                            [
                                dtc,
                                "-@",
                                "-o",
                                destination,
                                "-b",
                                "0",
                                "-i",
                                source.parent,
                                "-i",
                                inc,
                                preprocessed,
                            ],
                            log=result / "logs/dtb.log",
                        )
                        preprocessed.unlink()

                    control = work / "control.dtb"
                    dtb(allwinner / "sun8i-a33-ga10h-v1.1.dts", control)
                    if (
                        control.read_bytes()
                        != (
                            stock_root / "boot/dtbs/sun8i-a33-ga10h-v1.1.dtb"
                        ).read_bytes()
                    ):
                        raise ValueError("Stock DTB byte-for-byte control failed")
                    for source_name, artifact, provision in (
                        ("sun8i-a33-pf1007l-kiosk-audio.dts", "pf1007l.dtb", False),
                        (
                            "sun8i-a33-pf1007l-provision.dts",
                            "pf1007l-provision.dtb",
                            True,
                        ),
                    ):
                        destination = result / "boot/dtbs" / artifact
                        dtb(BOARD / "dts" / source_name, destination)
                        check(destination, provision)
            finally:
                symbol_log.close()
        shutil.copy2(IMAGE / "sources.lock.json", source_output / "sources.lock.json")
        licenses = {
            "uboot": "GPL-2.0-or-later; full pinned source archive retained",
            "kernel": (
                "GPL-2.0 and individual-file SPDX terms; pinned source retained; matching installed header package and reference hashes recorded"
                if installed is not None
                else "GPL-2.0 and individual-file SPDX terms; patched source and headers archive retained"
            ),
            "wifi": "See full pinned upstream archive COPYING/LICENSE files and module license metadata",
        }
        report = {
            "schema_version": 1,
            "board_id": lock["board_id"],
            "kernel_release": krel,
            "kernel_source_release": lock["kernel_release"],
            "kernel_input_mode": "installed"
            if installed is not None
            else "locked-legacy",
            "kernel_packages": installed["packages"] if installed is not None else {},
            "kernel_reference_hashes": installed["reference_hashes"]
            if installed is not None
            else {},
            "builder_architecture": platform.machine(),
            "compiler": run([cross + "gcc", "--version"]).splitlines()[0],
            "jobs": args.jobs,
            "cross_compile": cross,
            "source_date_epoch": env["SOURCE_DATE_EPOCH"],
            "modules": records,
            "licenses": licenses,
            "files": {
                str(p.relative_to(result)): digest(p)
                for p in sorted(result.rglob("*"))
                if p.is_file()
            },
        }
        (result / "manifest.json").write_text(json.dumps(report, indent=2) + "\n")
        os.rename(result, out)
        print("Board artifacts built and checked: " + str(out))
    finally:
        if result.exists() and any((result / "logs").iterdir()):
            failed_logs = out.with_name(
                out.name + ".failed-" + work.name.removeprefix("board-build-")
            )
            shutil.move(result / "logs", failed_logs)
            print(
                "Unpublished build failed; logs retained at " + str(failed_logs),
                flush=True,
            )
        shutil.rmtree(work)


if __name__ == "__main__":
    main()
