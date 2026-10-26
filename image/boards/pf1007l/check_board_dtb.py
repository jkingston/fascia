"""Semantic checks for the PL5 unit's compiled DTBs; never accesses hardware."""

import struct
from pathlib import Path


def parse(blob):
    if len(blob) < 40 or len(blob) > 2 * 1024 * 1024:
        raise ValueError("Invalid DTB size")
    magic, total, structure, strings = struct.unpack_from(">4I", blob)
    if magic != 0xD00DFEED or total > len(blob):
        raise ValueError("Invalid DTB header")
    pos = structure
    stack = []
    nodes = {}
    while pos + 4 <= total:
        token = struct.unpack_from(">I", blob, pos)[0]
        pos += 4
        if token == 1:
            end = blob.index(b"\0", pos, total)
            name = blob[pos:end].decode()
            pos = (end + 4) & ~3
            stack.append(name)
            nodes["/" + "/".join(stack[1:])] = {}
        elif token == 2:
            stack.pop()
        elif token == 3:
            length, offset = struct.unpack_from(">2I", blob, pos)
            pos += 8
            end = blob.index(b"\0", strings + offset, total)
            name = blob[strings + offset : end].decode()
            if pos + length > total or not stack:
                raise ValueError("Invalid DTB property")
            nodes["/" + "/".join(stack[1:])][name] = blob[pos : pos + length]
            pos = (pos + length + 3) & ~3
        elif token == 4:
            pass
        elif token == 9:
            return nodes
        else:
            raise ValueError("Invalid DTB token")
    raise ValueError("Truncated DTB")


def cells(value):
    if len(value) % 4:
        raise ValueError("Invalid DTB cells")
    return struct.unpack(">" + str(len(value) // 4) + "I", value)


def check(path, provision=False):
    return check_nodes(parse(Path(path).read_bytes()), provision)


def check_nodes(nodes, provision=False):
    def find(suffix):
        matches = [
            (name, value)
            for name, value in nodes.items()
            if name.endswith("/" + suffix)
        ]
        if len(matches) != 1:
            raise ValueError("Expected one DTB node " + suffix)
        return matches[0][1]

    def value(node, field):
        if field not in node:
            raise ValueError("Missing DTB property " + field)
        return node[field]

    def enabled(node):
        return node.get("status", b"okay\0") in (b"okay\0", b"ok\0")

    def require(test, label):
        if not test:
            raise ValueError("DTB invariant failed: " + label)

    pio = find("pinctrl@1c20800")
    rpio = find("pinctrl@1f02c00")
    pio_handle = cells(value(pio, "phandle"))[0]
    rpio_handle = cells(value(rpio, "phandle"))[0]
    wifi = find("regulator-wifi-chip-en")
    sequence = find("wifi-pwrseq")
    require(
        cells(value(wifi, "gpio")) == (rpio_handle, 0, 5, 0),
        "Wi-Fi chip enable must be PL5 active-high, never PL2",
    )
    require(
        cells(value(sequence, "reset-gpios")) == (rpio_handle, 0, 6, 1),
        "Wi-Fi reset must be PL6 active-low",
    )
    mmc1 = find("mmc@1c10000")
    mmc2 = find("mmc@1c11000")
    require(
        enabled(mmc1)
        and cells(value(mmc1, "bus-width")) == (4,)
        and "keep-power-in-suspend" in mmc1,
        "SDIO configuration",
    )
    require(not enabled(find("serial@1f02800")), "PL2/PL3 R_UART must stay disabled")
    require(enabled(find("mmc@1c0f000")), "SD boot controller")
    require(enabled(mmc2) == provision, "eMMC only enabled in provisioning DTB")
    if provision:
        require(
            cells(value(mmc2, "bus-width")) == (4,)
            and cells(value(mmc2, "max-frequency")) == (26000000,),
            "eMMC 4-bit / 26 MHz",
        )
        require(
            all(
                key in mmc2 for key in ("non-removable", "no-1-8-v", "no-sd", "no-sdio")
            ),
            "eMMC conservative probe flags",
        )
        pins = find("pf-mmc2-4bit-pins")
        require(
            value(pins, "pins").split(b"\0")[:-1]
            == [b"PC5", b"PC6", b"PC8", b"PC9", b"PC10", b"PC11"],
            "eMMC exact proven four-bit pins",
        )
        require(
            value(pins, "function") == b"mmc2\0"
            and cells(value(pins, "drive-strength")) == (30,)
            and "bias-pull-up" in pins
            and cells(value(mmc2, "pinctrl-0")) == cells(value(pins, "phandle")),
            "eMMC proven pinctrl configuration",
        )
    touch = find("touchscreen@40")
    require(
        cells(value(touch, "power-gpios")) == (pio_handle, 7, 1, 0), "touch power PH1"
    )
    require(
        cells(value(touch, "interrupts")) == (1, 5, 2), "touch interrupt PB5 falling"
    )
    require(
        value(touch, "firmware-name") == b"gsl_m86_hd.fw\0", "touch firmware filename"
    )
    panel = find("panel@0")
    backlight = find("backlight")
    require(
        value(panel, "compatible").split(b"\0")[:2]
        == [b"yhk,zr789-sq9365-101", b"jadard,jd9365da-h3"],
        "panel compatibles",
    )
    require(
        cells(value(panel, "reset-gpios")) == (pio_handle, 7, 7, 1), "panel reset PH7"
    )
    require(
        cells(value(backlight, "enable-gpios")) == (pio_handle, 7, 6, 0),
        "backlight enable PH6",
    )
    require(
        cells(value(backlight, "brightness-levels"))
        == (0, 8, 16, 32, 64, 96, 128, 192, 255),
        "eight nonzero backlight levels",
    )
    require(
        enabled(find("codec@1c22e00"))
        and enabled(find("dai@1c22c00"))
        and enabled(find("sound")),
        "working audio nodes",
    )
    require(
        find("usb@1c19000").get("dr_mode") == b"peripheral\0",
        "USB stays device/peripheral",
    )
    require("/__symbols__" in nodes, "stock symbol-enabled DTB format")
    # PL2 is the verified physical Home input: no enabled GPIO consumer may claim it.
    for name, node in nodes.items():
        if not enabled(node):
            continue
        for prop, raw in node.items():
            if prop.endswith("-gpios") or prop in ("gpio", "gpios"):
                values = cells(raw)
                for offset in range(0, len(values) - 3):
                    require(
                        values[offset : offset + 3] != (rpio_handle, 0, 2),
                        "PL2 conflict in " + name,
                    )
    return True
