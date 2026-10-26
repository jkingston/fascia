"""Offline tests; never format/mount or open a real target."""

import importlib.util
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("fascia_guest", HERE / "guest.py")
guest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guest)


class GuestTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def inputs(self):
        for name in ("project", "release"):
            (self.root / name).mkdir()
            if name == "project":
                for part in ("image", "tools", "scripts"):
                    (self.root / name / part).mkdir()
                (self.root / name / "image/file").write_bytes(name.encode())
            else:
                (self.root / name / "file").write_bytes(name.encode())
        return self.root

    def test_fingerprint_ignores_runtime_state_but_detects_source_and_modes(self):
        self.inputs()
        before = guest.source_key(self.root)
        (self.root / "project/README.md").write_text("documentation")
        (self.root / "project/image/test-example.py").write_text("test only")
        self.assertEqual(before, guest.source_key(self.root))
        (self.root / "request.json").write_text("changed")
        (self.root / "cache").mkdir()
        (self.root / "cache/item").write_text("changed")
        self.assertEqual(before, guest.source_key(self.root))
        (self.root / "project/image/new").write_text("new")
        changed = guest.source_key(self.root)
        self.assertNotEqual(before, changed)
        (self.root / "project/image/new").chmod(0o755)
        self.assertNotEqual(changed, guest.source_key(self.root))

    def test_changed_source_request_rejected(self):
        self.inputs()
        request = dict(
            schema_version=1,
            board_id=guest.BOARD,
            phase="prepare",
            source_key=guest.source_key(self.root),
        )
        (self.root / "request.json").write_text(json.dumps(request))
        self.assertEqual("prepare", guest.request(self.root)["phase"])
        (self.root / "release/file").write_text("different")
        with self.assertRaisesRegex(ValueError, "changed"):
            guest.request(self.root)

    def test_prepared_archive_hash_and_completeness(self):
        key = "a" * 64
        folder = guest.prepared_directory(self.root / "prepared", key)
        folder.mkdir(parents=True)
        files = {}
        for name in guest.ARTIFACTS:
            path = folder / name
            path.write_bytes(name.encode())
            files[name] = dict(bytes=path.stat().st_size, sha256=guest.digest(path))
        manifest = dict(
            schema_version=1, board_id=guest.BOARD, source_key=key, files=files
        )
        (folder / "manifest.json").write_text(json.dumps(manifest))
        self.assertEqual(manifest, guest.prepared_manifest(self.root / "prepared", key))
        (folder / "runtime.tar.zst").write_bytes(b"corrupt")
        with self.assertRaisesRegex(ValueError, "changed"):
            guest.prepared_manifest(self.root / "prepared", key)
        with self.assertRaises(ValueError):
            guest.prepared_directory(self.root, "../escape")

    def test_api_mounts_exposes_private_root_and_cleans_up_reverse_order(self):
        runner = mock.Mock()
        unmounted = []

        def mknod(path, _mode, _device):
            Path(path).touch()

        with (
            mock.patch.object(guest.os, "mknod", side_effect=mknod),
            mock.patch.object(guest, "stop_chroot_agents"),
            mock.patch.object(
                guest,
                "unmount_checked",
                side_effect=lambda path, _runner: unmounted.append(path),
            ),
        ):
            with guest.api_mounts(self.root, runner):
                self.assertEqual(
                    ["mount", "--bind", self.root, self.root],
                    runner.run.call_args_list[0].args[0],
                )
                self.assertTrue((self.root / "dev/fd").is_symlink())
        self.assertEqual(
            [self.root / "dev", self.root / "sys", self.root / "proc", self.root],
            unmounted,
        )
        self.assertFalse(
            any(
                "/dev" in str(arg) and str(arg) == "/dev"
                for call in runner.run.call_args_list
                for arg in call.args[0]
            )
        )

    def test_api_mounts_partial_failure_unmounts_only_successful_owned_mounts(self):
        runner = mock.Mock()
        runner.run.side_effect = [
            None,
            None,
            guest.subprocess.CalledProcessError(1, "mount sys"),
        ]
        with (
            mock.patch.object(guest, "stop_chroot_agents"),
            mock.patch.object(guest, "unmount_checked") as cleanup,
        ):
            with self.assertRaises(guest.subprocess.CalledProcessError):
                with guest.api_mounts(self.root, runner):
                    self.fail("Failed mount must not enter transaction body")
        self.assertEqual(
            [mock.call(self.root / "proc", runner), mock.call(self.root, runner)],
            cleanup.call_args_list,
        )
        with mock.patch.object(Path, "is_mount", return_value=True):
            with self.assertRaisesRegex(ValueError, "unmounted isolated"):
                with guest.api_mounts(self.root, runner):
                    self.fail("Existing caller mount must not be taken over")

    def test_official_base_requires_matching_digest_and_signer(self):
        archive = self.root / "base.tar.gz"
        signature = self.root / "base.sig"
        keyring = self.root / "trust.gpg"
        archive.write_bytes(b"official fixture")
        signature.write_bytes(b"signature fixture")
        keyring.write_bytes(b"binary fixture")
        fingerprint = "A" * 40
        resolved = dict(
            rootfs=str(archive),
            rootfs_signature=str(signature),
            rootfs_sha256=guest.digest(archive),
            trust_keyring=str(keyring),
            fingerprint=fingerprint,
        )

        class SignatureRunner:
            def run(self, args, **kwargs):
                descriptor = int(args[args.index("--status-fd") + 1])
                assert descriptor in kwargs["pass_fds"]
                os.write(
                    descriptor,
                    (
                        "[GNUPG:] VALIDSIG "
                        + fingerprint
                        + " 2026 1 0 4 0 1 10 00 "
                        + fingerprint
                        + "\n"
                    ).encode(),
                )

        runner = SignatureRunner()
        runner.output = self.root / "output"
        runner.output.mkdir()
        self.assertEqual(
            guest.digest(archive),
            guest.verify_rootfs(resolved, self.root / "derived", runner),
        )
        resolved["fingerprint"] = "B" * 40
        with self.assertRaisesRegex(ValueError, "signer"):
            guest.verify_rootfs(resolved, self.root / "derived", runner)
        self.assertTrue((runner.output / "rejected-rootfs.json").is_file())
        resolved["rootfs_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "digest changed"):
            guest.verify_rootfs(resolved, self.root / "derived", mock.Mock())

    def test_only_signature_rejection_marks_exact_cached_pair(self):
        archive, signature, keyring = [
            self.root / name for name in ("base.tar.gz", "base.sig", "trust.gpg")
        ]
        for path in (archive, signature, keyring):
            path.write_bytes(path.name.encode())
        resolved = dict(
            rootfs=archive,
            rootfs_signature=signature,
            rootfs_sha256=guest.digest(archive),
            trust_keyring=keyring,
            trust_fingerprint="A" * 40,
        )
        output = self.root / "output"
        output.mkdir()
        runner = mock.Mock(output=output)
        marker = output / "rejected-rootfs.json"
        for error in (
            FileNotFoundError("gpgv missing"),
            PermissionError("execution forbidden"),
        ):
            runner.run.side_effect = error
            with self.assertRaises(type(error)):
                guest.verify_rootfs(resolved, self.root / "derived", runner)
            self.assertFalse(marker.exists())
        runner.run.side_effect = guest.subprocess.CalledProcessError(1, ["gpgv"])
        with self.assertRaisesRegex(
            ValueError, "signature verification failed"
        ) as caught:
            guest.verify_rootfs(resolved, self.root / "derived", runner)
        self.assertIsInstance(
            caught.exception.__cause__, guest.subprocess.CalledProcessError
        )
        self.assertEqual(
            {
                "schema_version": 1,
                "rootfs_sha256": guest.digest(archive),
                "signature_sha256": guest.digest(signature),
            },
            json.loads(marker.read_text()),
        )
        self.assertFalse((output / "rejected-rootfs.json.tmp").exists())

    def test_initial_app_payload_changes_image_identity_independently_of_os_release(
        self,
    ):
        self.inputs()
        before = guest.source_key(self.root)
        package = self.root / "initial-apps"
        package.mkdir()
        (package / "manifest.json").write_text('{"apps":["clock"]}')
        changed = guest.source_key(self.root)
        self.assertNotEqual(before, changed)
        (package / "manifest.json").write_text('{"apps":[]}')
        self.assertNotEqual(changed, guest.source_key(self.root))

    def test_official_base_record_changes_source_key(self):
        self.inputs()
        path = self.root / "rootfs-input.json"
        path.write_text('{"rootfs_sha256":"first"}')
        before = guest.source_key(self.root)
        path.write_text('{"rootfs_sha256":"second"}')
        self.assertNotEqual(before, guest.source_key(self.root))

    def test_board_cache_changes_with_actual_toolchain_base_and_kernel(self):
        project = self.root / "project"
        for name in (
            "image/boards/pf1007l",
            "image/arch/fascia-board-support",
            "image/host",
        ):
            (project / name).mkdir(parents=True)
        for name in (
            "image/sources.lock.json",
            "image/host/sources.lock.json",
            "image/arch/build-package.py",
        ):
            (project / name).write_text("fixture")
        versions = {"gcc": "1", "linux-armv7": "1"}
        first = guest.board_build_key(project, "a" * 64, versions, {"kernel": "a"})
        self.assertEqual(
            first,
            guest.board_build_key(
                project,
                "a" * 64,
                dict(reversed(list(versions.items()))),
                {"kernel": "a"},
            ),
        )
        self.assertNotEqual(
            first, guest.board_build_key(project, "b" * 64, versions, {"kernel": "a"})
        )
        self.assertNotEqual(
            first,
            guest.board_build_key(
                project, "a" * 64, {**versions, "gcc": "2"}, {"kernel": "a"}
            ),
        )
        self.assertNotEqual(
            first, guest.board_build_key(project, "a" * 64, versions, {"kernel": "b"})
        )

    def test_refresh_bypasses_completed_cache_without_erasing_it(self):
        output = self.root / "prepared"
        output.mkdir()
        files = {}
        for name in guest.ARTIFACTS:
            path = output / name
            path.write_bytes(("old:" + name).encode())
            files[name] = {"bytes": path.stat().st_size, "sha256": guest.digest(path)}
        manifest = {
            "schema_version": 1,
            "board_id": guest.BOARD,
            "source_key": "a" * 64,
            "files": files,
        }
        (output / "manifest.json").write_text(json.dumps(manifest))
        before = {p.name: p.read_bytes() for p in output.iterdir()}
        runner = guest.Runner(self.root / "logs")
        inputs = mock.Mock()
        inputs.prepare.side_effect = RuntimeError("fresh preparation reached")
        req = {"source_key": "a" * 64}
        with (
            mock.patch.object(guest, "preflight_filesystems"),
            mock.patch.object(guest, "load_module", return_value=inputs),
        ):
            self.assertEqual(
                output, guest.prepare(self.root, output, req, runner, self.root)
            )
            inputs.prepare.assert_not_called()
            with self.assertRaisesRegex(RuntimeError, "fresh preparation reached"):
                guest.prepare(
                    self.root, output, {**req, "refresh": True}, runner, self.root
                )
        self.assertEqual(before, {p.name: p.read_bytes() for p in output.iterdir()})
        guest.verify_prepared(output, req["source_key"])
        # Refresh is also the recovery path for an interrupted/corrupt export:
        # do not parse the existing manifest before starting the replacement.
        (output / "manifest.json").write_text("{interrupted manifest")
        with (
            mock.patch.object(guest, "preflight_filesystems"),
            mock.patch.object(guest, "load_module", return_value=inputs),
        ):
            with self.assertRaisesRegex(RuntimeError, "fresh preparation reached"):
                guest.prepare(
                    self.root, output, {**req, "refresh": True}, runner, self.root
                )
            with self.assertRaises(json.JSONDecodeError):
                guest.prepare(self.root, output, req, runner, self.root)
        with mock.patch.object(guest, "preflight_filesystems") as preflight:
            with self.assertRaisesRegex(ValueError, "offline cannot refresh"):
                guest.prepare(
                    self.root,
                    output,
                    {**req, "refresh": True, "offline": True},
                    runner,
                    self.root,
                )
            preflight.assert_not_called()

    def test_refresh_checkpoint_cannot_clobber_completed_board_archive(self):
        output = self.root / "prepared"
        output.mkdir()
        (output / "manifest.json").write_text("completed fixture")
        archive = output / "board-cache.tar.gz"
        archive.write_bytes(b"old verified board archive")
        with mock.patch.object(guest, "export_board_cache") as export:
            self.assertIsNone(
                guest.publish_board_checkpoint(
                    self.root / "work", output, "a" * 64, mock.Mock()
                )
            )
            export.assert_not_called()
        self.assertEqual(b"old verified board archive", archive.read_bytes())
        self.assertFalse((output / "board-checkpoint.json").exists())

    def test_fresh_offline_prepare_refuses_live_transactions(self):
        runner = guest.Runner(self.root / "output")
        module = mock.Mock()
        with (
            mock.patch.object(guest, "preflight_filesystems"),
            mock.patch.object(guest, "load_module", return_value=module),
        ):
            with self.assertRaisesRegex(ValueError, "Offline preparation"):
                guest.prepare(
                    self.root,
                    self.root / "prepared",
                    {"source_key": "a" * 64, "offline": True},
                    runner,
                    self.root,
                )
        module.prepare.assert_not_called()

    def test_rolling_upgrade_uses_full_upgrade_and_normal_firmware_removal(self):
        runner = mock.Mock()
        with (
            mock.patch.object(
                guest,
                "installed_packages",
                return_value={
                    "linux-firmware": "1",
                    "linux-firmware-realtek": "1",
                    "wireless-regdb": "1",
                },
            ),
            mock.patch.object(guest, "run_package_transaction") as transaction,
        ):
            guest.rolling_upgrade(self.root, ["python", "linux-armv7"], runner)
        calls = [call.args[0] for call in transaction.call_args_list]
        self.assertEqual(
            [
                "chroot",
                self.root,
                "/usr/bin/pacman",
                "--disable-sandbox",
                "-Rdd",
                "--noconfirm",
                "linux-firmware",
                "linux-firmware-realtek",
            ],
            calls[0],
        )
        self.assertTrue(all("--disable-sandbox" in call for call in calls))
        self.assertIn("-Syu", calls[1])
        self.assertIn("--needed", calls[1])
        self.assertNotIn("-U", calls[1])
        self.assertEqual("wireless-regdb", calls[2][-1])
        self.assertNotIn("--needed", calls[2])
        with self.assertRaisesRegex(ValueError, "profile"):
            guest.rolling_upgrade(self.root, ["--bad-option"], runner)

    def test_official_base_setup_keeps_firmware_out_and_uses_arm_https(self):
        destination = self.root / "runtime"

        class ExtractRunner:
            def run(self, args):
                self.args = args
                (destination / "etc/pacman.d").mkdir(parents=True)
                (destination / "etc/pacman.conf").write_text(
                    "[options]\n#Architecture = auto\n[core]\nInclude = /etc/pacman.d/mirrorlist\n"
                )
                (destination / "etc/pacman.d/mirrorlist").write_text(
                    "Server = http://mirror.example/$arch/$repo\n"
                )
                (destination / "etc/resolv.conf").symlink_to(
                    "/run/systemd/resolve/stub-resolv.conf"
                )

        runner = ExtractRunner()
        guest.initialize_rolling_root(destination, "official.tar.gz", runner)
        self.assertIn("--exclude=usr/lib/firmware", runner.args)
        config = (destination / "etc/pacman.conf").read_text()
        self.assertIn("Architecture = armv7h", config)
        self.assertIn("!usr/lib/firmware/regulatory.db", config)
        self.assertIn(
            "https://fl.us.mirror.archlinuxarm.org/$arch/$repo",
            (destination / "etc/pacman.d/mirrorlist").read_text(),
        )
        self.assertFalse((destination / "etc/resolv.conf").is_symlink())
        self.assertEqual(0o1777, stat.S_IMODE((destination / "tmp").stat().st_mode))
        self.assertTrue(
            (destination / "etc/pacman.d/hooks/70-dkms-install.hook").is_symlink()
        )
        guest.remove_initial_hooks(destination)
        self.assertFalse(
            (destination / "etc/pacman.d/hooks/70-dkms-install.hook").is_symlink()
        )

    def test_kernel_selection_and_inventory_are_unambiguous(self):
        modules = self.root / "usr/lib/modules"
        (modules / "1-one").mkdir(parents=True)
        self.assertEqual(
            "1-one",
            guest.installed_kernel(self.root, ["usr/lib/modules/1-one/kernel/a.ko"]),
        )
        # A stale directory has no authority, but two package-owned releases fail.
        (modules / "2-two").mkdir()
        self.assertEqual(
            "1-one",
            guest.installed_kernel(self.root, ["usr/lib/modules/1-one/kernel/a.ko"]),
        )
        with self.assertRaisesRegex(ValueError, "one installed"):
            guest.installed_kernel(
                self.root, ["usr/lib/modules/1-one/", "usr/lib/modules/2-two/"]
            )
        with mock.patch.object(
            guest.subprocess, "check_output", return_value="gcc 1\nlinux-armv7 2\n"
        ):
            self.assertEqual(
                {"gcc": "1", "linux-armv7": "2"}, guest.installed_packages(self.root)
            )
        with mock.patch.object(
            guest.subprocess, "check_output", return_value="gcc 1\ngcc 2\n"
        ):
            with self.assertRaisesRegex(ValueError, "inventory"):
                guest.installed_packages(self.root)

    def fake_target(self):
        sys = self.root / "sys"
        (sys / "vda/device").mkdir(parents=True)
        (sys / "vda/serial").write_text("FASCIA-INSTALL-FILE\n")
        (sys / "vda/size").write_text(str(8 * 1024**3 // 512))
        req = dict(
            phase="install",
            erase_authorized=True,
            serial="FASCIA-INSTALL-FILE",
            capacity=8 * 1024**3,
        )
        original = Path.stat

        def info(path, *args, **kwargs):
            if str(path) == "/dev/vda":
                return os.stat_result((stat.S_IFBLK | 0o600, 1, 1, 1, 0, 0, 0, 0, 0, 0))
            return original(path, *args, **kwargs)

        return sys, req, info

    def test_target_guard_checks_authorization_size_serial_and_extra_disks(self):
        sys, req, info = self.fake_target()
        with mock.patch.object(Path, "stat", info):
            self.assertEqual(
                Path("/dev/vda"),
                guest.validate_target(
                    "/dev/vda", req, req["capacity"], req["serial"], sys
                ),
            )
            bad = {**req, "erase_authorized": False}
            with self.assertRaisesRegex(ValueError, "authorized"):
                guest.validate_target(
                    "/dev/vda", bad, req["capacity"], req["serial"], sys
                )
            with self.assertRaisesRegex(ValueError, "arguments"):
                guest.validate_target(
                    "/dev/vda", req, req["capacity"] + 512, req["serial"], sys
                )
            (sys / "vdb/device").mkdir(parents=True)
            with self.assertRaisesRegex(ValueError, "additional"):
                guest.validate_target(
                    "/dev/vda", req, req["capacity"], req["serial"], sys
                )

    def test_regular_file_cannot_masquerade_as_block_target(self):
        path = self.root / "file"
        path.touch()
        with self.assertRaisesRegex(ValueError, "block device"):
            guest.validate_target(path, dict(phase="install", erase_authorized=True))

    def test_partition_paths(self):
        self.assertEqual("/dev/vda1", guest.partition_path("/dev/vda", 1))
        self.assertEqual("/dev/loop4p2", guest.partition_path("/dev/loop4", 2))
        self.assertEqual("/dev/mmcblk0p2", guest.partition_path("/dev/mmcblk0", 2))

    def test_inventory_detects_content_and_retains_hardlink_relation(self):
        (self.root / "a").write_text("before")
        os.link(self.root / "a", self.root / "b")
        before = guest.inventory(self.root)
        self.assertEqual("a", before["b"]["hardlink"])
        (self.root / "a").write_text("after")
        self.assertNotEqual(before, guest.inventory(self.root))

    def test_failed_stage_persists_failure(self):
        runner = guest.Runner(self.root)
        with self.assertRaises(RuntimeError):
            with runner.phase("fixture"):
                raise RuntimeError("actual failure")
        self.assertEqual(
            "failed",
            json.loads((self.root / "status.json").read_text())["stages"][0]["state"],
        )

    def test_agent_cleanup_selects_only_exact_root_and_names(self):
        proc = self.root / "proc"
        proc.mkdir()
        jail = self.root / "jail"
        jail.mkdir()
        other = self.root / "other"
        other.mkdir()
        for pid, name, root in (
            (12, "gpg-agent", jail),
            (13, "dirmngr", other),
            (14, "unrelated", jail),
        ):
            p = proc / str(pid)
            p.mkdir()
            (p / "comm").write_text(name + "\n")
            (p / "root").symlink_to(root)
        self.assertEqual([12], guest.chroot_agents(jail, proc))

    def test_board_cache_promotion_and_checked_stream_restore(self):
        import io
        import tarfile

        key = "b" * 64
        output = self.root / "output"
        output.mkdir()
        members = {
            "board/boot/u-boot.bin": b"boot",
            "board/manifest.json": b"{}",
            "dkms-package/fascia.pkg.tar.xz": b"package",
        }
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode="w:gz") as archive:
            for name, data in members.items():
                item = tarfile.TarInfo(name)
                item.size = len(data)
                archive.addfile(item, io.BytesIO(data))
        path = output / "board-cache.tar.gz"
        path.write_bytes(raw.getvalue())
        record = dict(
            schema_version=1,
            key=key,
            file=path.name,
            bytes=path.stat().st_size,
            sha256=guest.digest(path),
            files={
                name: dict(
                    bytes=len(data),
                    sha256=guest.hashlib.sha256(data).hexdigest(),
                    mode=0o644,
                )
                for name, data in members.items()
            },
        )
        (output / "board-checkpoint.json").write_text(
            json.dumps({"board_cache": record})
        )
        cache = self.root / "cache"
        guest.promote_board_cache(output, cache)
        # Same files in a different gzip/tar envelope reuse the existing cache.
        import gzip

        original = (cache / "boards" / key / path.name).read_bytes()
        path.write_bytes(gzip.compress(gzip.decompress(raw.getvalue()), mtime=1))
        record.update(bytes=path.stat().st_size, sha256=guest.digest(path))
        (output / "board-checkpoint.json").write_text(
            json.dumps({"board_cache": record})
        )
        guest.promote_board_cache(output, cache)
        self.assertEqual(original, (cache / "boards" / key / path.name).read_bytes())
        # A self-consistent archive SHA cannot hide changed member contents.
        path.write_bytes(
            gzip.compress(
                gzip.decompress(raw.getvalue()).replace(
                    b"boot" + bytes(508), b"evil" + bytes(508), 1
                )
            )
        )
        record.update(bytes=path.stat().st_size, sha256=guest.digest(path))
        (output / "board-checkpoint.json").write_text(
            json.dumps({"board_cache": record})
        )
        with self.assertRaises(ValueError):
            guest.promote_board_cache(output, cache)
        work = self.root / "work"
        work.mkdir()
        self.assertTrue(guest.restore_board_cache(cache, work, key))
        self.assertEqual(b"boot", (work / "board/boot/u-boot.bin").read_bytes())
        (cache / "boards" / key / "board-cache.tar.gz").write_bytes(b"corrupt")
        with self.assertRaisesRegex(ValueError, "changed"):
            guest.restore_board_cache(cache, self.root / "unused", key)

    def test_board_cache_rejects_traversal(self):
        record = dict(
            schema_version=1,
            key="a" * 64,
            file="board-cache.tar.gz",
            files={
                "board/boot/../../escape": dict(bytes=0, sha256="a" * 64, mode=0o644)
            },
        )
        with self.assertRaisesRegex(ValueError, "Unsafe"):
            guest.valid_board_record(record)

    def test_busy_unmount_retries_normally_and_stops_on_success(self):
        runner = mock.Mock()
        runner.run.side_effect = [
            guest.subprocess.CalledProcessError(32, "umount"),
            None,
        ]
        with (
            mock.patch.object(Path, "is_mount", return_value=True),
            mock.patch.object(Path, "iterdir", return_value=iter([])),
            mock.patch.object(guest.time, "sleep") as sleep,
        ):
            guest.unmount_checked(self.root / "dev", runner)
        self.assertEqual(2, runner.run.call_count)
        self.assertEqual(
            ["umount", "-R", self.root / "dev"], runner.run.call_args.args[0]
        )
        sleep.assert_called_once_with(0.2)

    def test_completed_cache_seed_hardlinks_only_archives_and_matching_signatures(self):
        bootstrap, runtime = self.root / "build", self.root / "runtime"
        source = bootstrap / "var/cache/pacman/pkg"
        source.mkdir(parents=True)
        for name in (
            "python.pkg.tar.xz",
            "python.pkg.tar.xz.sig",
            "linux.pkg.tar.zst",
            "partial.pkg.tar.xz.part",
            "partial.pkg.tar.xz.part.sig",
            "orphan.pkg.tar.zst.sig",
        ):
            (source / name).write_bytes(name.encode())
        (source / "symlink.pkg.tar.xz").symlink_to(source / "python.pkg.tar.xz")
        (source / "directory.pkg.tar.xz").mkdir()
        seeded = guest.seed_package_cache(bootstrap, runtime)
        target = runtime / "var/cache/pacman/pkg"
        expected = {"python.pkg.tar.xz", "python.pkg.tar.xz.sig", "linux.pkg.tar.zst"}
        self.assertEqual(expected, {p.name for p in target.iterdir()})
        self.assertEqual(3, seeded["files"])
        for name in expected:
            self.assertEqual(
                (source / name).stat().st_ino, (target / name).stat().st_ino
            )
        self.assertEqual(
            {"files": 0, "bytes": 0}, guest.seed_package_cache(bootstrap, runtime)
        )
        # Finalization unlinks the runtime cache, not the shared source inode.
        import shutil

        shutil.rmtree(runtime / "var/cache")
        self.assertEqual(
            b"python.pkg.tar.xz", (source / "python.pkg.tar.xz").read_bytes()
        )
        target.mkdir(parents=True)
        (target / "python.pkg.tar.xz").write_bytes(b"conflicting content")
        with self.assertRaisesRegex(ValueError, "Conflicting completed"):
            guest.seed_package_cache(bootstrap, runtime)

    def test_cache_seed_exdev_copies_and_failed_copy_leaves_no_partial(self):
        bootstrap, runtime = self.root / "build", self.root / "runtime"
        source = bootstrap / "var/cache/pacman/pkg"
        source.mkdir(parents=True)
        archive = source / "package.pkg.tar.xz"
        content = b"archive" * 200000
        archive.write_bytes(content)
        signature = source / "package.pkg.tar.xz.sig"
        signature.write_bytes(b"detached signature")
        cross_mount = OSError(guest.errno.EXDEV, "cross-device link")
        with mock.patch.object(guest.os, "link", side_effect=cross_mount):
            result = guest.seed_package_cache(bootstrap, runtime)
        target = runtime / "var/cache/pacman/pkg"
        self.assertEqual(
            {"files": 2, "bytes": len(content) + len(b"detached signature")}, result
        )
        self.assertEqual(content, (target / archive.name).read_bytes())
        self.assertNotEqual(
            archive.stat().st_ino, (target / archive.name).stat().st_ino
        )
        self.assertEqual(b"detached signature", (target / signature.name).read_bytes())
        self.assertFalse(list(target.glob(".fascia-cache-*")))
        for path in target.iterdir():
            path.unlink()

        def failed_copy(_source, destination, **_kwargs):
            destination.write(b"partial")
            raise OSError(guest.errno.ENOSPC, "fixture storage full")

        with (
            mock.patch.object(guest.os, "link", side_effect=cross_mount),
            mock.patch.object(guest.shutil, "copyfileobj", side_effect=failed_copy),
        ):
            with self.assertRaises(OSError) as error:
                guest.seed_package_cache(bootstrap, runtime)
        self.assertEqual(guest.errno.ENOSPC, error.exception.errno)
        self.assertEqual([], list(target.iterdir()))
        self.assertEqual(content, archive.read_bytes())
        with (
            mock.patch.object(
                guest.os,
                "link",
                side_effect=PermissionError(guest.errno.EACCES, "fixture denied"),
            ),
            mock.patch.object(guest.shutil, "copyfileobj") as copied,
        ):
            with self.assertRaises(PermissionError):
                guest.seed_package_cache(bootstrap, runtime)
            copied.assert_not_called()
        self.assertEqual([], list(target.iterdir()))

    def test_finalization_builds_initramfs_for_actual_installed_kernel(self):
        project, runtime, work = (
            self.root / "project",
            self.root / "runtime",
            self.root / "work",
        )
        (project / "image/host").mkdir(parents=True)
        for name in ("sources.lock.json", "profiles.json", "rootfs.json"):
            (project / "image/host" / name).write_text("{}")
        (self.root / "rootfs-input.json").write_text("{}")
        (work / "board/boot/dtbs").mkdir(parents=True)
        (work / "board/boot/dtbs/pf1007l-provision.dtb").write_bytes(b"dtb")
        (work / "board/manifest.json").write_text("{}")
        runtime.mkdir()
        (runtime / ".fascia-image-build-root").write_text("isolated\n")
        kernel = "7.2.11-1-armv7-ARCH"
        runner, installer = mock.Mock(), mock.Mock()

        def info(args, **_kwargs):
            if "-n" in args:
                return "/usr/lib/modules/" + kernel + "/updates/dkms/module.ko\n"
            return kernel + " SMP mod_unload ARMv7\n"

        with (
            mock.patch.object(guest, "installed_kernel", return_value=kernel),
            mock.patch.object(guest, "load_module", return_value=installer),
            mock.patch.object(
                guest, "build_mounts", return_value=guest.contextlib.nullcontext()
            ),
            mock.patch.object(guest.subprocess, "check_output", side_effect=info),
            mock.patch.object(guest, "validate_runtime_elfs"),
        ):
            guest.finalize_runtime(
                project, runtime, self.root / "release", work, runner
            )
        calls = [call.args[0] for call in runner.run.call_args_list]
        initramfs = [
            args for args in calls if project / "image/tools/build-initramfs.py" in args
        ]
        self.assertEqual(1, len(initramfs))
        self.assertEqual(kernel, initramfs[0][initramfs[0].index("--kernel") + 1])
        self.assertEqual(runtime, initramfs[0][initramfs[0].index("--root") + 1])
        self.assertNotIn("7.2.10-1-armv7-ARCH", initramfs[0])

    def test_transport_retry_keeps_same_root_and_cache_and_stops_at_three(self):
        output = self.root / "output"
        output.mkdir()
        cache = self.root / "runtime/var/cache/pacman/pkg"
        cache.mkdir(parents=True)
        (cache / "completed.pkg.tar.xz").write_bytes(b"completed download")
        failure = b"error: failed retrieving file 'python.pkg.tar.xz' from mirror : Operation too slow. Less than 1 bytes/sec transferred the last 10 seconds\nerror: failed to commit transaction (unexpected error)\nErrors occurred, no packages were upgraded.\n"

        class DownloadRunner:
            calls = []

            def __init__(self, failures):
                self.output = output
                self.failures = failures
                self.calls = []

            def run(self, args):
                self.calls.append(list(args))
                with (output / "guest.log").open("ab") as stream:
                    stream.write(
                        failure
                        if len(self.calls) <= self.failures
                        else b"successful transaction\n"
                    )
                if len(self.calls) <= self.failures:
                    raise guest.subprocess.CalledProcessError(1, args)

        args = [
            "chroot",
            self.root / "runtime",
            "/usr/bin/pacman",
            "--disable-sandbox",
            "-Syu",
            "python",
        ]
        with mock.patch.object(guest.time, "sleep"):
            recovered = DownloadRunner(2)
            guest.run_package_transaction(args, recovered)
            self.assertEqual([args, args, args], recovered.calls)
            failed = DownloadRunner(3)
            with self.assertRaises(guest.subprocess.CalledProcessError):
                guest.run_package_transaction(args, failed)
            self.assertEqual(3, len(failed.calls))
        self.assertEqual(
            b"completed download", (cache / "completed.pkg.tar.xz").read_bytes()
        )

    def test_download_retry_refuses_unsafe_and_unknown_failures(self):
        transport = b"error: failed retrieving file 'pkg' from mirror : Connection timed out after 10000 milliseconds\n"
        self.assertTrue(
            guest.retryable_package_download(
                transport
                + b"error: failed to synchronize all databases (download library error)\n"
            )
        )
        for extra in (
            b"error: signature from signer is invalid",
            b"error: package is invalid or corrupted",
            b"error: no space left on device",
            b":: Processing package changes...",
            b":: Running pre-transaction hooks...",
            b"( 1/12) upgrading package",
            b"error: command failed to execute correctly",
            b"error: failed retrieving file 'other' from mirror : The requested URL returned error: 404",
            b"error: unknown failure",
            b"error: checksum mismatch",
        ):
            with self.subTest(extra=extra):
                self.assertFalse(
                    guest.retryable_package_download(transport + extra + b"\n")
                )
        output = self.root / "failure-output"
        output.mkdir()
        runner = mock.Mock(output=output)

        def failure(args):
            with (output / "guest.log").open("ab") as stream:
                stream.write(transport)
            raise guest.subprocess.CalledProcessError(1, args)

        runner.run.side_effect = failure
        with mock.patch.object(guest.time, "sleep") as sleep:
            with self.assertRaises(guest.subprocess.CalledProcessError):
                guest.run_package_transaction(
                    ["pacman", "-U", "local.pkg.tar.zst"], runner
                )
            self.assertEqual(1, runner.run.call_count)
            sleep.assert_not_called()

    def test_successful_pacman_with_failed_hook_is_rejected(self):
        runner = mock.Mock(output=self.root)
        log = self.root / "guest.log"
        log.write_text("old error: command failed to execute correctly\n")
        runner.run.side_effect = lambda args: log.write_text(
            log.read_text() + "success\n"
        )
        guest.run_package_transaction(["pacman"], runner)
        runner.run.side_effect = lambda args: log.write_text(
            log.read_text() + "error: command failed to execute correctly\n"
        )
        with self.assertRaisesRegex(RuntimeError, "Package hook failed"):
            guest.run_package_transaction(["pacman"], runner)

    def test_target_loader_checks_dependency_failure_without_app_execution(self):
        import struct

        header = bytearray(84)
        header[:7] = b"\x7fELF\x01\x01\x01"
        struct.pack_into("<H", header, 18, 40)
        struct.pack_into("<I", header, 28, 52)
        struct.pack_into("<HH", header, 42, 32, 1)
        interpreter = b"/lib/ld-linux-armhf.so.3\0"
        struct.pack_into("<8I", header, 52, 3, 84, 0, 0, len(interpreter), 0, 0, 0)
        executable = self.root / "app"
        executable.write_bytes(header + interpreter)
        self.assertEqual("/lib/ld-linux-armhf.so.3", guest.elf_interpreter(executable))
        runner = mock.Mock()
        runner.run.side_effect = guest.subprocess.CalledProcessError(
            127, "missing libfontconfig.so"
        )
        with mock.patch.object(
            guest, "elf_interpreter", return_value="/lib/ld-linux-armhf.so.3"
        ):
            with self.assertRaises(guest.subprocess.CalledProcessError):
                guest.validate_runtime_elfs(self.root, runner)
        args = runner.run.call_args.args[0]
        self.assertEqual(
            ["chroot", self.root, "/lib/ld-linux-armhf.so.3", "--list"], args[:4]
        )
        self.assertEqual("/usr/lib/fascia/bin/fascia-host", args[4])
        struct.pack_into("<I", header, 52, 1)  # No PT_INTERP: static helper.
        executable.write_bytes(header + interpreter)
        self.assertIsNone(guest.elf_interpreter(executable))

    def test_filesystem_preflight_owns_only_new_file_loops_and_cleans_failure(self):
        import contextlib

        sizes, closed = [], []

        @contextlib.contextmanager
        def attach(fd, capacity):
            self.assertTrue(stat.S_ISREG(os.fstat(fd).st_mode))
            self.assertEqual(capacity, os.fstat(fd).st_size)
            sizes.append(capacity)
            try:
                yield {"path": "/dev/loop-fixture"}
            finally:
                closed.append(capacity)

        runner = mock.Mock()
        with mock.patch.object(
            guest, "load_module", return_value=mock.Mock(attach=attach)
        ):
            guest.preflight_filesystems(self.root, runner)
        self.assertEqual([64 * 1024**2, 256 * 1024**2], sizes)
        self.assertEqual(sizes, closed)
        self.assertEqual([], list(self.root.iterdir()))
        self.assertTrue(
            any(
                call.args[0][:3] == ["mkfs.btrfs", "-f", "-K"]
                for call in runner.run.call_args_list
            )
        )
        runner.run.side_effect = guest.subprocess.CalledProcessError(32, "mount")
        with mock.patch.object(
            guest, "load_module", return_value=mock.Mock(attach=attach)
        ):
            with self.assertRaises(guest.subprocess.CalledProcessError):
                guest.preflight_filesystems(self.root, runner)
        self.assertEqual(sizes, closed)
        self.assertEqual([], list(self.root.iterdir()))

    def test_runtime_alias_is_recursive_and_removed_before_return(self):
        runner = mock.Mock()
        bootstrap = self.root / "bootstrap"
        runtime = self.root / "runtime"
        with mock.patch.object(guest, "stop_chroot_agents"):
            with guest.runtime_alias(bootstrap, runtime, runner) as relative:
                self.assertEqual("/run/fascia-runtime-target", relative)
                self.assertEqual(
                    [
                        "mount",
                        "--rbind",
                        runtime,
                        bootstrap / "run/fascia-runtime-target",
                    ],
                    runner.run.call_args.args[0],
                )
        self.assertEqual(
            ["umount", "-R", bootstrap / "run/fascia-runtime-target"],
            runner.run.call_args.args[0],
        )

    def test_kernel_io_units_include_discards_and_flushes(self):
        path = self.root / "stat"
        path.write_text("1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17\n")
        result = guest.block_counters(path)
        self.assertEqual(3 * 512, result["read_bytes"])
        self.assertEqual(7 * 512, result["write_bytes"])
        self.assertEqual(14 * 512, result["discard_bytes"])
        self.assertEqual(16, result["flushes"])

    def test_boot_script_header_crc_and_payload(self):
        import struct
        import zlib

        path = self.root / "boot.scr"
        guest.create_boot_script(path)
        data = path.read_bytes()
        header = data[:64]
        words = struct.unpack(">7I4B32s", header)
        self.assertEqual(0x27051956, words[0])
        self.assertEqual(words[1], zlib.crc32(header[:4] + bytes(4) + header[8:]))
        self.assertEqual(words[6], zlib.crc32(data[64:]))
        self.assertIn(b"sysboot mmc 0:1", data)


if __name__ == "__main__":
    unittest.main()
