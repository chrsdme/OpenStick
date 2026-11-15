#!/usr/bin/env python3
"""Create a small deterministic test-only fake eMMC image."""
import argparse
import struct
import uuid
import zlib
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("--mib", type=int, default=8)
    args = parser.parse_args()
    if not 2 <= args.mib <= 64:
        parser.error("--mib must be between 2 and 64")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("wb") as image:
        remaining = args.mib * 1024 * 1024
        chunk = bytes((i * 37 + 11) & 255 for i in range(65536))
        while remaining:
            part = chunk[:min(len(chunk), remaining)]
            image.write(part)
            remaining -= len(part)
    # Tiny, valid GPT layout for test tools. The fake partitions are one-sector
    # placeholders; this is metadata only and contains no device data.
    sector = 512
    sectors = args.mib * 1024 * 1024 // sector
    names = ("fsg", "sec", "fsc", "pad", "boot", "userdata", "cache", "persist",
             "sbl1", "rpm", "tz", "tzbak", "hyp", "aboot", "modemst1", "modemst2")
    entries = bytearray(128 * 128)
    type_guid = uuid.UUID("0fc63daf-8483-4772-8e79-3d69d8477de4")
    for index, name in enumerate(names):
        first = 34 + index * 2
        unique = uuid.uuid5(uuid.NAMESPACE_DNS, "openstick-fake-" + name)
        entry = type_guid.bytes_le + unique.bytes_le + struct.pack("<QQQ", first, first + 1, 0)
        entry += name.encode("utf-16le").ljust(72, b"\0")
        entries[index * 128:(index + 1) * 128] = entry
    entry_crc = zlib.crc32(entries) & 0xffffffff
    disk_guid = uuid.UUID("12345678-1234-5678-9abc-def012345678")

    def header(current, backup, entries_lba):
        raw = bytearray(sector)
        struct.pack_into("<8sIIIIQQQQ16sQIII", raw, 0, b"EFI PART", 0x10000, 92, 0, 0,
                         current, backup, 34, sectors - 34, disk_guid.bytes_le,
                         entries_lba, 128, 128, entry_crc)
        struct.pack_into("<I", raw, 16, zlib.crc32(raw[:92]) & 0xffffffff)
        return raw

    with args.output.open("r+b") as image:
        mbr = bytearray(sector)
        mbr[446 + 4] = 0xEE
        struct.pack_into("<II", mbr, 446 + 8, 1, min(sectors - 1, 0xffffffff))
        mbr[510:512] = b"\x55\xaa"
        image.seek(0); image.write(mbr)
        image.seek(sector); image.write(header(1, sectors - 1, 2))
        image.seek(2 * sector); image.write(entries)
        image.seek((sectors - 33) * sector); image.write(entries)
        image.seek((sectors - 1) * sector); image.write(header(sectors - 1, 1, sectors - 33))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
