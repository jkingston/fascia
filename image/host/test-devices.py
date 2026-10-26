"""External-card safety and hot-swap checks; never opens a real device."""

import importlib.util
import os
from pathlib import Path
import stat
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location(
    "devices", Path(__file__).with_name("devices.py")
)
devices = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(devices)
CAPACITY = 32 * 1024**3


def mac():
    return dict(
        DeviceIdentifier="disk9",
        DeviceNode="/dev/disk9",
        WholeDisk=True,
        Internal=False,
        VirtualOrPhysical="Physical",
        Writable=True,
        ReadOnlyMedia=False,
        TotalSize=CAPACITY,
        MediaName="Test SD",
        DeviceTreePath="test/usb/one",
    )


def linux():
    return {
        "path": "/dev/sdb",
        "type": "disk",
        "size": CAPACITY,
        "ro": False,
        "rm": True,
        "tran": "usb",
        "serial": "test",
        "maj:min": "8:16",
        "children": [
            {"path": "/dev/sdb1", "type": "part", "mountpoints": ["/media/test/card"]}
        ],
    }


class Safety(unittest.TestCase):
    def test_mac_external_whole_card(self):
        identity = devices.validate_mac(mac(), "disk9", CAPACITY)
        self.assertEqual(identity["DeviceTreePath"], "test/usb/one")

    def test_mac_refuses_internal_readonly_virtual_wrong_size_and_partition(self):
        cases = [
            dict(Internal=True),
            dict(OSInternalMedia=True),
            dict(Writable=False),
            dict(ReadOnlyMedia=True),
            dict(VirtualOrPhysical="Virtual"),
            dict(WholeDisk=False),
            dict(TotalSize=CAPACITY + 512),
            dict(DeviceNode="/dev/disk8"),
        ]
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                devices.validate_mac({**mac(), **changes}, "disk9", CAPACITY)
        for name in ("disk9s1", "../disk9", "/dev/disk9", "disk9; true"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                devices.validate_mac(mac(), name, CAPACITY)

    def test_exact_capacity_rejects_bool_float_and_unaligned(self):
        for value in (True, float(CAPACITY), CAPACITY + 1, 4096):
            with self.subTest(value=value), self.assertRaises(ValueError):
                devices.validate_capacity(value)

    def test_linux_external_card_and_system_mount_refusal(self):
        devices.validate_linux(linux(), "/dev/sdb", CAPACITY)
        for mount in ("/", "/boot", "/home", "/var/lib", "[SWAP]"):
            value = linux()
            value["children"][0]["mountpoints"] = [mount]
            with self.subTest(mount=mount), self.assertRaises(ValueError):
                devices.validate_linux(value, "/dev/sdb", CAPACITY)

    def test_linux_refuses_internal_mapping_swap_and_wrong_capacity(self):
        for change in (
            {"tran": "nvme", "rm": False},
            {"ro": True},
            {"size": CAPACITY + 512},
            {"path": "/dev/sdc"},
            {"type": "part"},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                devices.validate_linux({**linux(), **change}, "/dev/sdb", CAPACITY)
        value = linux()
        value["children"][0]["type"] = "crypt"
        with self.assertRaises(ValueError):
            devices.validate_linux(value, "/dev/sdb", CAPACITY)
        with self.assertRaises(ValueError):
            devices.validate_linux(linux(), "/dev/sdb", CAPACITY, ["/dev/sdb1"])

    def test_hot_swap_rejected_even_for_same_capacity(self):
        before = devices.validate_mac(mac(), "disk9", CAPACITY)
        after = devices.validate_mac(
            {**mac(), "DeviceTreePath": "different/socket"}, "disk9", CAPACITY
        )
        with self.assertRaises(ValueError):
            devices.assert_identity(before, after)

    def test_changed_identity_never_opens_target(self):
        with (
            mock.patch.object(devices, "inspect", return_value={"new": True}),
            mock.patch.object(devices.os, "open") as opened,
        ):
            with self.assertRaises(ValueError):
                devices.open_target("disk9", CAPACITY, {"old": True})
            opened.assert_not_called()

    def test_node_replaced_during_open_closes_fd(self):
        before = os.stat_result((stat.S_IFBLK, 1, 2, 1, 0, 0, 0, 0, 0, 0))
        after = os.stat_result((stat.S_IFBLK, 2, 2, 1, 0, 0, 0, 0, 0, 0))
        with (
            mock.patch.object(devices, "inspect", return_value={}),
            mock.patch.object(devices, "unmount"),
            mock.patch.object(devices.os, "stat", return_value=before),
            mock.patch.object(devices.os, "open", return_value=71),
            mock.patch.object(devices.os, "fstat", return_value=after),
            mock.patch.object(devices.os, "close") as close,
        ):
            with self.assertRaises(ValueError):
                devices.open_target("disk9", CAPACITY, {})
            close.assert_called_once_with(71)

    def test_linux_claim_is_exclusive_without_changing_mac_flags(self):
        node = os.stat_result((stat.S_IFBLK, 1, 2, 1, 0, 0, 0, 0, 0, 0))
        for platform, name in (("linux", "/dev/sdb"), ("darwin", "disk9")):
            with (
                self.subTest(platform=platform),
                mock.patch.object(devices.sys, "platform", platform),
                mock.patch.object(devices, "inspect", return_value={}),
                mock.patch.object(devices, "unmount"),
                mock.patch.object(devices.os, "stat", return_value=node),
                mock.patch.object(devices.os, "fstat", return_value=node),
                mock.patch.object(devices.os, "open", return_value=71) as opened,
            ):
                self.assertEqual(71, devices.open_target(name, CAPACITY, {}))
                flags = opened.call_args.args[1]
                self.assertEqual(platform == "linux", bool(flags & os.O_EXCL))


if __name__ == "__main__":
    unittest.main()
