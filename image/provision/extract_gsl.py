"""Bounded ELF32/ARM GSL extractor. No vendor firmware is distributed here."""

import struct

MAX_MODULE = 8 * 1024 * 1024


class Elf:
    def __init__(self, data):
        self.data = data
        if not 52 <= len(data) <= MAX_MODULE or data[:7] != b"\x7fELF\x01\x01\x01":
            raise ValueError("expected bounded ELF32 little-endian module")
        kind, machine = struct.unpack_from("<HH", data, 16)
        offset = struct.unpack_from("<I", data, 32)[0]
        size, count, names = struct.unpack_from("<HHH", data, 46)
        if (
            (kind, machine, size) != (1, 40, 40)
            or not 1 <= count <= 4096
            or names >= count
        ):
            raise ValueError("unsupported ARM ELF section header")
        if offset + count * size > len(data):
            raise ValueError("section headers outside module")
        self.sections = [
            struct.unpack_from("<10I", data, offset + i * size) for i in range(count)
        ]
        for section in self.sections:
            if section[1] != 8 and section[4] + section[5] > len(data):
                raise ValueError("section outside module")
        self.names = [self.string(names, s[0]) for s in self.sections]
        sym = self.names.index(".symtab")
        section = self.sections[sym]
        if (
            section[9] != 16
            or section[5] % 16
            or section[5] // 16 > 100000
            or section[6] >= count
        ):
            raise ValueError("invalid symbol table")
        self.sym_index = sym
        self.symbols = []
        for i in range(section[5] // 16):
            name, value, length, info, _, target = struct.unpack(
                "<IIIBBH", self.part(sym, i * 16, 16)
            )
            self.symbols.append(
                (self.string(section[6], name), value, length, info, target)
            )

    def part(self, section, offset, size):
        if not 0 <= section < len(self.sections):
            raise ValueError("invalid section index")
        entry = self.sections[section]
        if entry[1] == 8 or offset < 0 or size < 0 or offset + size > entry[5]:
            raise ValueError("data outside section")
        return self.data[entry[4] + offset : entry[4] + offset + size]

    def string(self, section, offset):
        remaining = self.sections[section][5] - offset
        raw = self.part(section, offset, min(4096, max(0, remaining)))
        if b"\0" not in raw:
            raise ValueError("unterminated ELF string")
        return raw.split(b"\0", 1)[0].decode("ascii")

    def symbol(self, name):
        hits = [s for s in self.symbols if s[0] == name and s[3] & 15 == 1]
        if len(hits) != 1:
            raise ValueError("missing or ambiguous symbol " + name)
        return hits[0]

    def pointer(self, target, offset):
        addend = struct.unpack("<I", self.part(target, offset, 4))[0]
        matches = []
        for index, section in enumerate(self.sections):
            if section[1] != 9 or section[7] != target:
                continue
            if (
                section[6] != self.sym_index
                or section[5] % 8
                or section[5] // 8 > 100000
            ):
                raise ValueError("invalid relocation section")
            for i in range(section[5] // 8):
                address, info = struct.unpack("<II", self.part(index, i * 8, 8))
                if address == offset:
                    matches.append(info)
        if (
            len(matches) != 1
            or matches[0] & 255 != 2
            or matches[0] >> 8 >= len(self.symbols)
        ):
            raise ValueError("invalid ARM_ABS32 pointer")
        symbol = self.symbols[matches[0] >> 8]
        return symbol[4], symbol[1] + addend


def extract(data, name="gsl_m86_hd"):
    elf = Elf(data)
    _, start, size, _, section = elf.symbol("gslx680_fw_grp")
    if size % 12 or not 1 <= size // 12 <= 128:
        raise ValueError("invalid firmware group count")
    matches = []
    for i in range(size // 12):
        offset = start + i * 12
        name_section, name_offset = elf.pointer(section, offset)
        if elf.string(name_section, name_offset) != name:
            continue
        records = struct.unpack("<I", elf.part(section, offset + 4, 4))[0]
        if not 33 <= records <= 32768 or records % 33:
            raise ValueError("invalid page record count")
        fw_section, fw_offset = elf.pointer(section, offset + 8)
        matches.append(elf.part(fw_section, fw_offset, records * 8))
    if len(matches) != 1:
        raise ValueError("firmware entry absent or ambiguous")
    raw = matches[0]
    pages = set()
    for offset in range(0, len(raw), 33 * 8):
        records = [struct.unpack_from("<II", raw, offset + i * 8) for i in range(33)]
        if records[0][0] != 0xF0 or records[0][1] in pages:
            raise ValueError("invalid or duplicate firmware page")
        pages.add(records[0][1])
        if any(register != i * 4 for i, (register, _) in enumerate(records[1:])):
            raise ValueError("invalid firmware register sequence")
    return raw
