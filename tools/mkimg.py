#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Rootless helpers: newc cpio writer (no root, no mknod) and a minimal Android boot image v0 packer.
  mkimg.py cpio  --tree DIR --out FILE [--node PATH TYPE MAJOR MINOR MODE]...   (uid/gid 0, mtime 0, sorted)
  mkimg.py boot  --kernel F --ramdisk F --cmdline STR --out F [addresses...]
"""
import argparse, hashlib, os, stat, struct, sys


def cpio_newc(tree, nodes):
    out = bytearray()
    ino = [0]

    def add(name, mode, data=b"", rdev=(0, 0)):
        ino[0] += 1
        nm = name.encode() + b"\0"
        hdr = "070701" + "".join("%08X" % v for v in (
            ino[0], mode, 0, 0, 1, 0, len(data), 0, 0, rdev[0], rdev[1], len(nm), 0))
        out.extend(hdr.encode()); out.extend(nm)
        out.extend(b"\0" * (-len(out) % 4))
        out.extend(data); out.extend(b"\0" * (-len(out) % 4))

    entries = []
    for root, dirs, files in os.walk(tree):
        dirs.sort()
        rel = os.path.relpath(root, tree)
        if rel != ".":
            entries.append(rel)
        for f in sorted(files):
            entries.append(os.path.join(rel, f) if rel != "." else f)
        for d in dirs:  # symlinks to dirs are listed in dirs by os.walk
            p = os.path.join(root, d)
            if os.path.islink(p):
                entries.append(os.path.relpath(p, tree))
    entries = sorted(set(entries))
    done = set()
    for e in entries:
        p = os.path.join(tree, e)
        st = os.lstat(p)
        m = stat.S_IMODE(st.st_mode)
        if stat.S_ISLNK(st.st_mode):
            add(e, stat.S_IFLNK | 0o777, os.readlink(p).encode())
        elif stat.S_ISDIR(st.st_mode):
            add(e, stat.S_IFDIR | (m & 0o755 | 0o755))
        else:
            add(e, stat.S_IFREG | (0o755 if m & 0o111 else 0o644), open(p, "rb").read())
        done.add(e)
    for path, typ, maj, mino, mode in nodes:
        path = path.lstrip("/")
        parent = os.path.dirname(path)
        assert parent in done or parent == "", "node parent %s must exist in the tree" % parent
        kind = {"c": stat.S_IFCHR, "b": stat.S_IFBLK}[typ]
        add(path, kind | int(mode, 8), b"", (int(maj), int(mino)))
    add("TRAILER!!!", 0)
    return bytes(out)


def pad(b, n):
    return b + b"\0" * (-len(b) % n)


def bootimg(a):
    k = open(a.kernel, "rb").read()
    r = open(a.ramdisk, "rb").read() if a.ramdisk else b""
    page = a.pagesize
    assert len(a.cmdline.encode()) < 512, "cmdline must fit in 512 bytes"
    base = a.base
    h = hashlib.sha1(); h.update(k); h.update(struct.pack("<I", len(k)))
    h.update(r); h.update(struct.pack("<I", len(r))); h.update(struct.pack("<I", 0))
    hdr = struct.pack("<8s10I16s512s32s1024s", b"ANDROID!", len(k), base + a.kernel_offset, len(r),
                      base + a.ramdisk_offset, 0, base + a.second_offset, base + a.tags_offset, page, 0, 0,
                      a.name.encode(), a.cmdline.encode(), h.digest() + b"\0" * 12, b"")
    img = pad(hdr, page) + pad(k, page) + pad(r, page)
    open(a.out, "wb").write(img)


def main():
    ap = argparse.ArgumentParser(); sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("cpio"); c.add_argument("--tree", required=True); c.add_argument("--out", required=True)
    c.add_argument("--node", nargs=5, action="append", default=[], metavar=("PATH", "TYPE", "MAJOR", "MINOR", "MODE"))
    b = sub.add_parser("boot")
    for n in ("kernel", "out"): b.add_argument("--" + n, required=True)
    b.add_argument("--ramdisk"); b.add_argument("--cmdline", default=""); b.add_argument("--name", default="")
    b.add_argument("--pagesize", type=int, default=2048)
    for n, d in (("base", 0x80000000), ("kernel_offset", 0x80000), ("ramdisk_offset", 0x2000000),
                 ("second_offset", 0xf00000), ("tags_offset", 0x1e00000)):
        b.add_argument("--" + n, type=lambda x: int(x, 0), default=d)
    a = ap.parse_args()
    if a.cmd == "cpio":
        open(a.out, "wb").write(cpio_newc(a.tree, a.node))
    else:
        bootimg(a)


if __name__ == "__main__":
    main()
