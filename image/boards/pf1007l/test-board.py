"""Host checks for source integrity and compiled-board wiring invariants."""

import hashlib
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent


def load(name):
    spec = importlib.util.spec_from_file_location(name, HERE / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build = load("build_board")
dtb = load("check_board_dtb")


def cells(*items):
    import struct

    return struct.pack(">" + str(len(items)) + "I", *items)


def nodes():
    return {
        "/soc/pinctrl@1c20800": {"phandle": cells(1)},
        "/soc/pinctrl@1f02c00": {"phandle": cells(2)},
        "/regulator-wifi-chip-en": {"gpio": cells(2, 0, 5, 0)},
        "/wifi-pwrseq": {"reset-gpios": cells(2, 0, 6, 1)},
        "/soc/mmc@1c10000": {"bus-width": cells(4), "keep-power-in-suspend": b""},
        "/soc/mmc@1c11000": {"status": b"disabled\0"},
        "/soc/mmc@1c0f000": {},
        "/soc/serial@1f02800": {"status": b"disabled\0"},
        "/soc/i2c/touchscreen@40": {
            "power-gpios": cells(1, 7, 1, 0),
            "interrupts": cells(1, 5, 2),
            "firmware-name": b"gsl_m86_hd.fw\0",
        },
        "/soc/dsi/panel@0": {
            "compatible": b"yhk,zr789-sq9365-101\0jadard,jd9365da-h3\0",
            "reset-gpios": cells(1, 7, 7, 1),
        },
        "/backlight": {
            "enable-gpios": cells(1, 7, 6, 0),
            "brightness-levels": cells(0, 8, 16, 32, 64, 96, 128, 192, 255),
        },
        "/soc/codec@1c22e00": {},
        "/soc/dai@1c22c00": {},
        "/sound": {},
        "/soc/usb@1c19000": {"dr_mode": b"peripheral\0"},
        "/__symbols__": {},
    }


class Board(unittest.TestCase):
    def test_normal_working_gpio_contract(self):
        self.assertTrue(dtb.check_nodes(nodes()))

    def test_rejects_old_pl2_wifi_and_touch_changes(self):
        for node, field, value in [
            ("/regulator-wifi-chip-en", "gpio", cells(2, 0, 2, 0)),
            ("/soc/i2c/touchscreen@40", "power-gpios", cells(1, 7, 2, 0)),
            ("/soc/dsi/panel@0", "reset-gpios", cells(1, 7, 6, 1)),
        ]:
            value_nodes = nodes()
            value_nodes[node][field] = value
            with self.assertRaises(ValueError):
                dtb.check_nodes(value_nodes)

    def test_emmc_probe_only_conservative(self):
        value_nodes = nodes()
        mmc = value_nodes["/soc/mmc@1c11000"]
        mmc.update(
            status=b"okay\0",
            **{
                "bus-width": cells(4),
                "max-frequency": cells(26000000),
                "non-removable": b"",
                "no-1-8-v": b"",
                "no-sd": b"",
                "no-sdio": b"",
            },
        )
        value_nodes["/soc/pinctrl/pf-mmc2-4bit-pins"] = {
            "pins": b"PC5\0PC6\0PC8\0PC9\0PC10\0PC11\0",
            "function": b"mmc2\0",
            "drive-strength": cells(30),
            "bias-pull-up": b"",
            "phandle": cells(77),
        }
        mmc["pinctrl-0"] = cells(77)
        self.assertTrue(dtb.check_nodes(value_nodes, True))
        value_nodes["/soc/pinctrl/pf-mmc2-4bit-pins"]["pins"] += b"PC12\0"
        with self.assertRaises(ValueError):
            dtb.check_nodes(value_nodes, True)
        value_nodes["/soc/pinctrl/pf-mmc2-4bit-pins"]["pins"] = (
            b"PC5\0PC6\0PC8\0PC9\0PC10\0PC11\0"
        )
        with self.assertRaises(ValueError):
            dtb.check_nodes(value_nodes, False)
        mmc["bus-width"] = cells(8)
        with self.assertRaises(ValueError):
            dtb.check_nodes(value_nodes, True)

    def test_cached_source_tampering_rejected_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "input"
            file.write_bytes(b"bad")
            with mock.patch.object(build.urllib.request, "urlopen") as network:
                with self.assertRaises(ValueError):
                    build.fetch(
                        ["https://invalid.test"],
                        file,
                        hashlib.sha256(b"good").hexdigest(),
                    )
                network.assert_not_called()

    def test_19_kernel_sources_have_valid_pins(self):
        lines = (HERE / "kernel-sources.sha256").read_text().splitlines()
        self.assertEqual(len(lines), 19)
        for line in lines:
            sha, path = line.split()
            self.assertEqual(len(sha), 64)
            int(sha, 16)
            self.assertNotIn("..", Path(path).parts)

    def kernel_fixture(self, root, release="7.2.10-1-armv7-ARCH"):
        files = {
            f"usr/lib/modules/{release}/build/.config": b"CONFIG_ARM=y\n",
            f"usr/lib/modules/{release}/build/Module.symvers": b"symbols",
            f"usr/lib/modules/{release}/build/include/config/kernel.release": release.encode(),
            f"usr/lib/modules/{release}/build/include/generated/utsrelease.h": release.encode(),
            f"usr/lib/modules/{release}/kernel/drivers/gpu/drm/panel/panel-lvds.ko": b"stock module",
            "boot/dtbs/sun8i-a33-ga10h-v1.1.dtb": b"stock tree",
        }
        for name, data in files.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        return files

    def test_installed_signed_package_pair_uses_local_references_without_fetch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = self.kernel_fixture(root)
            packages = "linux-armv7 7.2.10-1\nlinux-armv7-headers 7.2.10-1\n"
            with (
                mock.patch.object(build, "run", return_value=packages) as run,
                mock.patch.object(build, "fetch") as fetch,
            ):
                result = build.installed_kernel(root, "7.2.10-1-armv7-ARCH")
            self.assertEqual(result["release"], "7.2.10-1-armv7-ARCH")
            self.assertEqual(result["packages"]["linux-armv7"], "7.2.10-1")
            self.assertEqual(
                result["reference_hashes"],
                {
                    name: hashlib.sha256(data).hexdigest()
                    for name, data in files.items()
                },
            )
            self.assertEqual(
                run.call_args.args[0],
                [
                    "pacman",
                    "--root",
                    root.resolve(),
                    "-Q",
                    "linux-armv7",
                    "linux-armv7-headers",
                ],
            )
            fetch.assert_not_called()

    def test_newer_patch_kernel_uses_actual_installed_release(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.kernel_fixture(root, "7.2.11-1-armv7-ARCH")
            with (
                mock.patch.object(
                    build,
                    "run",
                    return_value="linux-armv7 7.2.11-1\nlinux-armv7-headers 7.2.11-1\n",
                ),
                mock.patch.object(build, "fetch") as fetch,
            ):
                result = build.installed_kernel(root, "7.2.10-1-armv7-ARCH")
            self.assertEqual(result["release"], "7.2.11-1-armv7-ARCH")
            self.assertEqual(
                result["headers"],
                root.resolve() / "usr/lib/modules/7.2.11-1-armv7-ARCH/build",
            )
            fetch.assert_not_called()

    def test_future_kernel_refuses_before_any_archive_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.kernel_fixture(root, "7.3.0-1-armv7-ARCH")
            with (
                mock.patch.object(
                    build,
                    "run",
                    return_value="linux-armv7 7.3.0-1\nlinux-armv7-headers 7.3.0-1\n",
                ),
                mock.patch.object(build, "fetch") as fetch,
            ):
                with self.assertRaisesRegex(
                    ValueError, "No older kernel will be downloaded"
                ):
                    build.installed_kernel(root, "7.2.10-1-armv7-ARCH")
                fetch.assert_not_called()

    def test_mismatched_installed_kernel_headers_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(
                build,
                "run",
                return_value="linux-armv7 7.2.10-2\nlinux-armv7-headers 7.2.10-1\n",
            ):
                with self.assertRaisesRegex(ValueError, "versions must match"):
                    build.installed_kernel(Path(directory), "7.2.10-1-armv7-ARCH")

    def test_installed_headers_cannot_escape_the_selected_root(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root, outside = base / "root", base / "outside"
            self.kernel_fixture(outside)
            modules = root / "usr/lib/modules/7.2.10-1-armv7-ARCH"
            modules.mkdir(parents=True)
            (modules / "build").symlink_to(
                outside / "usr/lib/modules/7.2.10-1-armv7-ARCH/build"
            )
            with mock.patch.object(
                build,
                "run",
                return_value="linux-armv7 7.2.10-1\nlinux-armv7-headers 7.2.10-1\n",
            ):
                with self.assertRaisesRegex(ValueError, "escape"):
                    build.installed_kernel(root, "7.2.10-1-armv7-ARCH")

    def test_missing_reference_and_ambiguous_headers_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.kernel_fixture(root)
            with mock.patch.object(
                build,
                "run",
                return_value="linux-armv7 7.2.10-1\nlinux-armv7-headers 7.2.10-1\n",
            ):
                symbols = (
                    root / "usr/lib/modules/7.2.10-1-armv7-ARCH/build/Module.symvers"
                )
                symbols.unlink()
                with self.assertRaisesRegex(
                    ValueError, "Missing installed kernel reference"
                ):
                    build.installed_kernel(root, "7.2.10-1-armv7-ARCH")
                symbols.write_bytes(b"symbols")
                self.kernel_fixture(root, "7.2.10-2-armv7-ARCH")
                with self.assertRaisesRegex(ValueError, "exactly one"):
                    build.installed_kernel(root, "7.2.10-1-armv7-ARCH")

    def test_invalid_fdt_rejected(self):
        for raw in (b"", b"bad" * 20):
            with self.assertRaises(ValueError):
                dtb.parse(raw)


if __name__ == "__main__":
    unittest.main()
