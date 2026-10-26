#!/usr/bin/env python3
"""Filesystem integration tests; all mutations stay inside temporary directories."""

import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest import mock
import socket
import threading

spec = importlib.util.spec_from_file_location(
    "fascia_app", Path(__file__).with_name("fascia-app.py")
)
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = app.Store(self.root / "registry")

    def bundle(self, version="1.0", **fields):
        path = self.root / ("bundle-" + version)
        path.mkdir()
        app_id = fields.get("id", "hello")
        (path / (app_id + ".desktop")).write_text(
            f"[Desktop Entry]\nType=Application\nName=Hello\nExec=AppRun\nIcon={app_id}\nX-Fascia-Version={version}\nComment=Test app\n"
        )
        (path / (app_id + ".png")).write_bytes(b"\x89PNG\r\n\x1a\n")
        (path / ".DirIcon").symlink_to(app_id + ".png")
        (path / "AppRun").write_text("#!/bin/sh\nexec /bin/true\n# " + version)
        (path / "AppRun").chmod(0o755)
        return path

    def test_update_rollback_and_state_survival(self):
        self.store.install(self.bundle())
        state = self.store.state / "hello" / "preferences"
        state.write_text("keep me")
        first = self.store.active("hello")[0]
        self.store.install(self.bundle("2.0"))
        self.assertNotEqual(first, self.store.active("hello")[0])
        self.assertEqual("2.0", self.store.list()[0]["version"])
        self.store.rollback("hello")
        self.assertEqual(first, self.store.active("hello")[0])
        self.assertEqual("keep me", state.read_text())

    def test_release_directory_is_traversable_by_the_session(self):
        self.store.install(self.bundle())
        release = self.store.app_dir("hello") / self.store.active("hello")[0]
        self.assertEqual(0o755, release.stat().st_mode & 0o777)
        self.assertEqual(0o755, (release / "AppRun").stat().st_mode & 0o777)
        self.assertEqual(0o644, (release / ".fascia.json").stat().st_mode & 0o777)

    def test_reinstall_repairs_legacy_private_release_directory(self):
        bundle = self.bundle()
        self.store.install(bundle)
        release = self.store.app_dir("hello") / self.store.active("hello")[0]
        release.chmod(0o700)
        self.store.install(bundle)
        self.assertEqual(0o755, release.stat().st_mode & 0o777)

    def test_rollback_recovers_both_pointers_after_each_durable_write(self):
        self.store.install(self.bundle())
        self.store.install(self.bundle("2.0"))
        original = tuple(
            self.store.active("hello", n)[0] for n in ("active", "previous")
        )
        for interrupted_name in ("previous", "active"):
            with self.subTest(after=interrupted_name):
                point = self.store.point

                def interrupted(directory, name, target):
                    point(directory, name, target)
                    if name == interrupted_name:
                        raise OSError("injected power loss after durable pointer write")

                with mock.patch.object(self.store, "point", side_effect=interrupted):
                    with self.assertRaises(OSError):
                        self.store.rollback("hello")
                fresh = app.Store(self.store.root)
                with fresh.locked():
                    pass
                self.assertEqual(
                    original,
                    tuple(fresh.active("hello", n)[0] for n in ("active", "previous")),
                )
                self.assertFalse(
                    (fresh.app_dir("hello") / ".pointer-transaction.json").exists()
                )
        self.assertEqual("1.0", self.store.rollback("hello")["version"])

    def test_recovery_can_itself_be_interrupted_and_retried(self):
        self.store.install(self.bundle())
        self.store.install(self.bundle("2.0"))
        original = tuple(
            self.store.active("hello", n)[0] for n in ("active", "previous")
        )
        point = self.store.point

        def interrupted(directory, name, target):
            point(directory, name, target)
            raise OSError("injected interruption during recovery")

        with mock.patch.object(self.store, "point", side_effect=interrupted):
            with self.assertRaises(OSError):
                self.store.rollback("hello")
            with self.assertRaises(OSError):
                with self.store.locked():
                    pass
        self.assertTrue(
            (self.store.app_dir("hello") / ".pointer-transaction.json").exists()
        )
        fresh = app.Store(self.store.root)
        with fresh.locked():
            pass
        self.assertEqual(
            original, tuple(fresh.active("hello", n)[0] for n in ("active", "previous"))
        )

    def test_interrupted_install_restores_previous_rollback_target(self):
        self.store.install(self.bundle())
        self.store.install(self.bundle("2.0"))
        before = tuple(self.store.active("hello", n)[0] for n in ("active", "previous"))
        point = self.store.point

        def interrupted(directory, name, target):
            point(directory, name, target)
            raise OSError("injected interruption")

        with mock.patch.object(self.store, "point", side_effect=interrupted):
            with self.assertRaises(OSError):
                self.store.install(self.bundle("3.0"))
        with app.Store(self.store.root).locked():
            pass
        self.assertEqual(
            before,
            tuple(self.store.active("hello", n)[0] for n in ("active", "previous")),
        )

    def test_reinstall_rejects_corrupt_existing_release(self):
        bundle = self.bundle()
        self.store.install(bundle)
        target = self.store.active("hello")[0]
        self.store.install(bundle)  # Exact reinstall remains idempotent.
        release = self.store.app_dir("hello") / target
        (release / "AppRun").write_text("#!/bin/sh\nexit 99\n")
        with self.assertRaisesRegex(ValueError, "does not match its hash"):
            self.store.install(bundle)
        self.assertEqual(target, self.store.active("hello")[0])
        self.assertFalse(list(self.store.apps.glob(".stage-*")))

    def test_interrupted_update_keeps_old_active_and_state(self):
        self.store.install(self.bundle())
        before = self.store.active("hello")[0]

        def fail():
            raise RuntimeError("simulated interruption before atomic activation")

        with self.assertRaises(RuntimeError):
            self.store.install(self.bundle("2.0"), before_activate=fail)
        self.assertEqual(before, self.store.active("hello")[0])
        self.assertEqual("1.0", self.store.list()[0]["version"])
        self.assertFalse(list(self.store.apps.glob(".stage-*")))

    def test_uninstall_resets_default_and_keeps_state_unless_purged(self):
        self.store.install(self.bundle())
        state = self.store.state / "hello" / "settings"
        state.write_text("survive")
        self.store.default("hello", 10)
        self.store.uninstall("hello")
        self.assertEqual([], self.store.list())
        self.assertIsNone(self.store.read_settings()["default_app"])
        self.assertEqual("survive", state.read_text())
        self.store.install(self.bundle("2.0"))
        self.store.uninstall("hello", delete_state=True)
        self.assertFalse(state.parent.exists())

    def test_corrupt_settings_safely_fall_back_to_home(self):
        self.store.install(self.bundle())
        for raw in (
            b"{",
            b"[]",
            b'{"schema_version":1,"default_app":"../bad"}',
            b"x" * 9000,
        ):
            self.store.settings.write_bytes(raw)
            self.assertIsNone(self.store.read_settings()["default_app"])
        self.store.default("hello")
        self.assertEqual("hello", self.store.read_settings()["default_app"])
        with self.assertRaises(FileNotFoundError):
            self.store.default("missing")

    def test_reject_bad_architecture_and_non_executable(self):
        bad_arch = self.bundle()
        header = bytearray(52)
        header[:6] = b"\x7fELF\x02\x01"
        (bad_arch / "AppRun").write_bytes(header)
        with self.assertRaises(ValueError):
            self.store.install(bad_arch)
        bad = self.bundle("2.0")
        (bad / "AppRun").chmod(0o644)
        with self.assertRaises(ValueError):
            self.store.install(bad)
        self.assertEqual([], self.store.list())

    def test_reject_tar_traversal_and_links_without_activation(self):
        self.store.install(self.bundle())
        before = self.store.active("hello")[0]
        for name, kind in (
            ("../escape", tarfile.REGTYPE),
            ("run", tarfile.SYMTYPE),
            ("run", tarfile.LNKTYPE),
        ):
            path = self.root / "bad.tar"
            with tarfile.open(path, "w") as out:
                item = tarfile.TarInfo(name)
                item.type = kind
                item.linkname = "/tmp/escape"
                item.size = 0
                out.addfile(item, io.BytesIO())
            with self.assertRaises(ValueError):
                self.store.install(path)
            self.assertEqual(before, self.store.active("hello")[0])
        self.assertFalse((self.root / "escape").exists())

    def test_tar_link_ancestors_cannot_create_links_outside_staging(self):
        archive = self.root / "bad-links.tar"
        with tarfile.open(archive, "w") as out:
            for name, target in (("parent", ".."), ("parent/escaped", "missing")):
                item = tarfile.TarInfo(name)
                item.type = tarfile.SYMTYPE
                item.linkname = target
                out.addfile(item)
        destination = self.root / "staging"
        destination.mkdir()
        with self.assertRaisesRegex(ValueError, "below a symlink"):
            app.copy_bundle(archive, destination)
        self.assertFalse((self.root / "escaped").is_symlink())

    def test_relative_appdir_links_survive_install_and_rollback(self):
        first = self.bundle()
        (first / "lib").mkdir()
        (first / "lib/library.so.1").write_bytes(b"library")
        (first / "lib/library.so").symlink_to("library.so.1")
        self.store.install(first)
        release = self.store.app_dir("hello") / self.store.active("hello")[0]
        self.assertTrue((release / ".DirIcon").is_symlink())
        self.assertEqual(b"library", (release / "lib/library.so").read_bytes())
        self.store.install(self.bundle("2.0"))
        self.store.rollback("hello")
        self.assertEqual(b"library", (release / "lib/library.so").read_bytes())

    def test_reject_broken_cyclic_and_escaping_relative_links(self):
        for index, target in enumerate(("missing", "link", "../outside")):
            bundle = self.bundle(str(index))
            (bundle / "link").symlink_to(target)
            with self.assertRaises(ValueError):
                self.store.install(bundle)
        self.assertEqual([], self.store.list())

    def test_directory_symlink_and_private_cache_are_rejected(self):
        path = self.bundle()
        (path / "outside").symlink_to(self.root)
        with self.assertRaises(ValueError):
            self.store.install(path)
        (path / "outside").unlink()
        (path / ".local").mkdir()
        with self.assertRaises(ValueError):
            self.store.install(path)


class IpcDiscoveryTests(unittest.TestCase):
    def test_explicit_socket_and_xdg_take_priority_without_fallback(self):
        with mock.patch.dict(
            app.os.environ, {"XDG_RUNTIME_DIR": "/chosen/runtime"}, clear=True
        ):
            self.assertEqual(
                "/explicit/socket", app.ipc_socket_path("/explicit/socket")
            )
            with mock.patch.object(app.Path, "is_socket", return_value=True):
                self.assertEqual(
                    "/chosen/runtime/fascia-home.sock", app.ipc_socket_path()
                )

    def test_missing_ssh_runtime_socket_falls_back_to_service(self):
        with (
            mock.patch.dict(
                app.os.environ, {"XDG_RUNTIME_DIR": "/run/user/1000"}, clear=True
            ),
            mock.patch.object(
                app.Path,
                "is_socket",
                side_effect=lambda path: str(path) == app.SERVICE_SOCKET,
                autospec=True,
            ),
        ):
            self.assertEqual(app.SERVICE_SOCKET, app.ipc_socket_path())
        with mock.patch.dict(
            app.os.environ, {"FASCIA_HOME_SOCKET": "/explicit/env/socket"}, clear=True
        ):
            self.assertEqual("/explicit/env/socket", app.ipc_socket_path())

    def test_serial_session_discovers_service_socket_and_uid_fallback(self):
        with (
            mock.patch.dict(app.os.environ, {}, clear=True),
            mock.patch.object(app.os, "getuid", return_value=4321),
        ):
            with mock.patch.object(app.Path, "is_socket", return_value=True):
                self.assertEqual(
                    "/run/pf-kiosk/fascia-home.sock", app.ipc_socket_path()
                )
            with mock.patch.object(app.Path, "is_socket", return_value=False):
                self.assertEqual(
                    "/run/user/4321/fascia-home.sock", app.ipc_socket_path()
                )

    def test_ssh_style_ipc_reaches_discovered_service(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "home.sock")
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                server.bind(path)
                server.listen(1)
                server.settimeout(3)
                received = []

                def reply():
                    with server.accept()[0] as client:
                        received.append(client.recv(100))
                        client.sendall(b"ok\n")

                thread = threading.Thread(target=reply, daemon=True)
                thread.start()
                with (
                    mock.patch.dict(app.os.environ, {}, clear=True),
                    mock.patch.object(app, "SERVICE_SOCKET", path),
                ):
                    self.assertEqual("ok", app.ipc("home"))
                thread.join(3)
                self.assertFalse(thread.is_alive())
                self.assertEqual([b"home\n"], received)


if __name__ == "__main__":
    unittest.main()
