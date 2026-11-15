"""Offline, hash-bound adapter from verified M4 images to the documented GPT-T layout.

This describes partition intent only. It does not write a device and does not
establish that a particular device is in the required bootloader mode.
"""
import hashlib
import ctypes
import errno
import json
import os
import shutil
import stat
import tempfile
import types
from pathlib import Path

from . import store

LAYOUT_ID = "ufi103s-gpt-t-board6-v1"
EMMC_SECTORS = 7634944
EXPECTED_PARTITIONS = {
    "boot": (396384, 429151),
    "userdata": (2428000, 7634910),
}


_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
_FILE_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)


def _signature(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            getattr(info, "st_mtime_ns", int(info.st_mtime * 1e9)),
            getattr(info, "st_ctime_ns", int(info.st_ctime * 1e9)))


def _open_dir(path):
    """Open a directory by walking from / without following any symlink."""
    absolute = Path(os.path.abspath(os.fspath(path)))
    try:
        fd = os.open(absolute.anchor, _DIR_FLAGS)
    except OSError as exc:
        raise store.RefusedError("directory path is unsafe or inaccessible") from exc
    try:
        for part in absolute.parts[1:]:
            try:
                child = os.open(part, _DIR_FLAGS, dir_fd=fd)
            except OSError as exc:
                raise store.RefusedError("directory path contains a symlink or changed during access") from exc
            os.close(fd)
            fd = child
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode):
            raise store.RefusedError("expected a directory")
        return fd, _signature(info)
    except BaseException:
        os.close(fd)
        raise


def _open_relative(root_fd, relative, expected=None):
    parts = Path(relative).parts
    if not parts or Path(relative).is_absolute() or any(p in ("", ".", "..") for p in parts):
        raise store.RefusedError("unsafe relative image-set path")
    parent = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            try:
                child = os.open(part, _DIR_FLAGS, dir_fd=parent)
            except OSError as exc:
                raise store.RefusedError("image-set path changed or contains a symlink") from exc
            os.close(parent)
            parent = child
        try:
            fd = os.open(parts[-1], _FILE_FLAGS, dir_fd=parent)
        except OSError as exc:
            raise store.RefusedError("image-set member changed or contains a symlink") from exc
    finally:
        os.close(parent)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise store.RefusedError("image-set member is not a regular file")
        if expected is not None and _signature(info) != expected:
            raise store.RefusedError("image-set member changed during validation")
        return fd, _signature(info)
    except BaseException:
        os.close(fd)
        raise


def _open_path(path):
    path = Path(os.path.abspath(os.fspath(path)))
    parent_fd, _ = _open_dir(path.parent)
    try:
        try:
            fd = os.open(path.name, _FILE_FLAGS, dir_fd=parent_fd)
        except OSError as exc:
            raise store.RefusedError("input changed or contains a symlink") from exc
    finally:
        os.close(parent_fd)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise store.RefusedError("input is not a regular file")
        return fd, _signature(info)
    except BaseException:
        os.close(fd)
        raise


def _assert_unchanged(fd, before):
    after = _signature(os.fstat(fd))
    if after != before:
        raise store.RefusedError("input changed while it was being read")


def _hash_fd(fd, before=None):
    if before is None:
        before = _signature(os.fstat(fd))
    digest = hashlib.sha256()
    os.lseek(fd, 0, os.SEEK_SET)
    while True:
        block = os.read(fd, 1024 * 1024)
        if not block:
            break
        digest.update(block)
    _assert_unchanged(fd, before)
    os.lseek(fd, 0, os.SEEK_SET)
    return digest.hexdigest()


def _hash(path):
    fd, before = _open_path(path)
    try:
        return _hash_fd(fd, before)
    finally:
        os.close(fd)


def _read_fd(fd, before=None):
    if before is None:
        before = _signature(os.fstat(fd))
    chunks = []
    os.lseek(fd, 0, os.SEEK_SET)
    while True:
        block = os.read(fd, 1024 * 1024)
        if not block:
            break
        chunks.append(block)
    _assert_unchanged(fd, before)
    return b"".join(chunks)


def _read_regular(path):
    fd, before = _open_path(path)
    try:
        return _read_fd(fd, before)
    finally:
        os.close(fd)


def _read_relative(root_fd, relative, expected=None):
    fd, before = _open_relative(root_fd, relative, expected)
    try:
        return _read_fd(fd, before)
    finally:
        os.close(fd)


def _hash_relative(root_fd, relative, expected=None):
    fd, before = _open_relative(root_fd, relative, expected)
    try:
        return _hash_fd(fd, before)
    finally:
        os.close(fd)


def _hash_info(path):
    fd, before = _open_path(path)
    try:
        return _hash_fd(fd, before), before[3]
    finally:
        os.close(fd)


def _sha(path):
    return _hash_info(path)[0]


def _copy_verified(source, destination, expected_hash, expected_size):
    fd, before = _open_path(source)
    digest = hashlib.sha256()
    copied = 0
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        with Path(destination).open("xb") as output:
            while True:
                block = os.read(fd, 1024 * 1024)
                if not block:
                    break
                output.write(block)
                digest.update(block)
                copied += len(block)
            output.flush()
            os.fsync(output.fileno())
        _assert_unchanged(fd, before)
    finally:
        os.close(fd)
    if copied != expected_size or digest.hexdigest() != expected_hash:
        raise store.RefusedError("build artifact changed while preparing image set")


def _scan_tree_fd(root_fd):
    """Return path identities while rejecting links and special members."""
    files, entries = {}, set()

    def walk(directory_fd, prefix):
        for name in os.listdir(directory_fd):
            if name in (".", "..") or "/" in name:
                raise store.RefusedError("invalid image-set directory entry")
            rel = (prefix + "/" if prefix else "") + name
            try:
                before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except OSError as exc:
                raise store.RefusedError("image-set member changed during traversal") from exc
            if stat.S_ISLNK(before.st_mode):
                raise store.RefusedError("GPT-T image set contains a symlink")
            if stat.S_ISDIR(before.st_mode):
                try:
                    child = os.open(name, _DIR_FLAGS, dir_fd=directory_fd)
                except OSError as exc:
                    raise store.RefusedError("image-set directory changed during traversal") from exc
                try:
                    opened = os.fstat(child)
                    if _signature(opened) != _signature(before):
                        raise store.RefusedError("image-set directory changed during traversal")
                    entries.add(rel + "/")
                    walk(child, rel)
                finally:
                    os.close(child)
            elif stat.S_ISREG(before.st_mode):
                try:
                    child = os.open(name, _FILE_FLAGS, dir_fd=directory_fd)
                except OSError as exc:
                    raise store.RefusedError("image-set member changed during traversal") from exc
                try:
                    opened = os.fstat(child)
                    if _signature(opened) != _signature(before):
                        raise store.RefusedError("image-set member changed during traversal")
                    files[rel] = _signature(opened)
                    entries.add(rel)
                finally:
                    os.close(child)
            else:
                raise store.RefusedError("GPT-T image set contains a special file")

    walk(root_fd, "")
    return files, entries


def _chmod_private_tree(path):
    root_fd, _ = _open_dir(path)

    def walk(directory_fd):
        for name in os.listdir(directory_fd):
            before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if stat.S_ISREG(before.st_mode):
                fd, opened = _open_relative(directory_fd, name, _signature(before))
                try:
                    os.fchmod(fd, 0o400)
                finally:
                    os.close(fd)
            elif stat.S_ISDIR(before.st_mode):
                try:
                    child = os.open(name, _DIR_FLAGS, dir_fd=directory_fd)
                except OSError as exc:
                    raise store.RefusedError("image-set output directory changed during permission setup") from exc
                try:
                    if _signature(os.fstat(child)) != _signature(before):
                        raise store.RefusedError("image-set output directory changed during permission setup")
                    walk(child)
                    os.fchmod(child, 0o500)
                finally:
                    os.close(child)
            else:
                raise store.RefusedError("image-set output contains a link or special file")

    try:
        walk(root_fd)
        os.fchmod(root_fd, 0o500)
    finally:
        os.close(root_fd)


def _cleanup_staging(path):
    try:
        root_fd, _ = _open_dir(path)

        def make_writable(directory_fd):
            for name in os.listdir(directory_fd):
                info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    child = os.open(name, _DIR_FLAGS, dir_fd=directory_fd)
                    try:
                        if _signature(os.fstat(child)) != _signature(info):
                            raise store.RefusedError("staging directory changed during cleanup")
                        make_writable(child)
                        os.fchmod(child, 0o700)
                    finally:
                        os.close(child)

        try:
            make_writable(root_fd)
            os.fchmod(root_fd, 0o700)
        finally:
            os.close(root_fd)
    except (OSError, store.RefusedError):
        pass
    shutil.rmtree(path, ignore_errors=True)


def _rename_noreplace(source, destination):
    """Atomically publish without replacing a path created after preflight."""
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise store.StoreError("atomic no-replace publication is unavailable on this Linux host")
    renameat2.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    renameat2.restype = ctypes.c_int
    rc = renameat2(-100, os.fsencode(source), -100, os.fsencode(destination), 1)  # AT_FDCWD, RENAME_NOREPLACE
    if rc == 0:
        return
    error = ctypes.get_errno()
    if error == errno.EEXIST:
        raise store.RefusedError("image-set output appeared during preparation; existing destination was preserved")
    raise store.StoreError("atomic no-replace image-set publication failed (errno %d)" % error)


def _gpt_layout(layout_dir=None, root_fd=None, prefix="layout"):
    if root_fd is not None:
        primary = _read_relative(root_fd, prefix + "/gpt-T-primary.bin")
        secondary = _read_relative(root_fd, prefix + "/gpt-T-secondary.bin")
    else:
        layout_dir = Path(os.path.abspath(layout_dir))
        if store._has_symlink_component(layout_dir) or not layout_dir.is_dir():
            raise store.RefusedError("GPT-T layout directory is missing or unsafe")
        primary_path = Path(layout_dir) / "gpt-T-primary.bin"
        secondary_path = Path(layout_dir) / "gpt-T-secondary.bin"
        primary = _read_regular(primary_path)
        secondary = _read_regular(secondary_path)
    if len(primary) != 34 * 512 or len(secondary) != 33 * 512:
        raise store.RefusedError("GPT-T files have unsupported sizes")
    protective = primary[446:462]
    if (primary[510:512] != b"\x55\xaa" or protective[4] != 0xee or
            int.from_bytes(protective[8:12], "little") != 1 or
            int.from_bytes(protective[12:16], "little") != EMMC_SECTORS - 1 or
            primary[462:510] != b"\0" * 48):
        raise store.RefusedError("GPT-T primary file lacks a protective MBR")
    gptool_path = Path(__file__).resolve().parents[2] / "scripts" / "gptool.py"
    module = types.ModuleType("openstick_imageset_gptool")
    exec(compile(_read_regular(gptool_path), str(gptool_path), "exec"), module.__dict__)
    p = module.load_gpt(primary, "primary", EMMC_SECTORS)
    s = module.load_gpt(secondary, "secondary", EMMC_SECTORS)
    for parsed, expected_my, expected_alt, expected_elba in ((p, 1, EMMC_SECTORS - 1, 2),
                                                               (s, EMMC_SECTORS - 1, 1, EMMC_SECTORS - 33)):
        h = parsed["hdr"]
        if not module.hdr_crc_ok(parsed["hsec"]):
            raise store.RefusedError("GPT-T header CRC is invalid")
        if (h["my"], h["alt"], h["first"], h["last"], h["elba"], h["n"], h["esz"]) != (expected_my, expected_alt, 34, EMMC_SECTORS - 34, expected_elba, 28, 128):
            raise store.RefusedError("GPT-T header geometry does not match the supported UFI103S target")
        if h["guid"] == b"\0" * 16:
            raise store.RefusedError("GPT-T disk GUID is invalid")
        if __import__("zlib").crc32(parsed["tab"]) & 0xffffffff != h["ecrc"]:
            raise store.RefusedError("GPT-T entry-array CRC is invalid")
    if (p["tab"] != s["tab"] or p["hdr"]["guid"] != s["hdr"]["guid"] or
            (p["hdr"]["first"], p["hdr"]["last"], p["hdr"]["n"], p["hdr"]["esz"]) !=
            (s["hdr"]["first"], s["hdr"]["last"], s["hdr"]["n"], s["hdr"]["esz"])):
        raise store.RefusedError("GPT-T primary and secondary partition tables disagree")
    rows = {}
    unique_guids = set()
    for raw in p["entries"]:
        if raw is None:
            continue
        name = raw["name"]
        if name in rows:
            raise store.RefusedError("GPT-T has duplicate partition names")
        if raw["type"] == b"\0" * 16 or raw["uniq"] == b"\0" * 16 or raw["uniq"] in unique_guids:
            raise store.RefusedError("GPT-T partition type or unique GUID is invalid/duplicated")
        unique_guids.add(raw["uniq"])
        rows[name] = (raw["start"], raw["end"])
        if raw["start"] < 34 or raw["end"] > EMMC_SECTORS - 34 or raw["end"] < raw["start"]:
            raise store.RefusedError("GPT-T partition lies outside the supported usable range")
    ordered = sorted(rows.values())
    for left, right in zip(ordered, ordered[1:]):
        if right[0] <= left[1]:
            raise store.RefusedError("GPT-T partitions overlap")
    if rows.get("boot") != EXPECTED_PARTITIONS["boot"] or rows.get("userdata") != EXPECTED_PARTITIONS["userdata"]:
        raise store.RefusedError("GPT is not the supported GPT-T layout; expected boot and userdata at documented UFI103S LBAs")
    if "rootfs" in rows or "system" not in rows or "userdata" not in rows:
        # GPT-B has a `rootfs` entry and must never be mistaken for GPT-T.
        raise store.RefusedError("GPT is not GPT-T; legacy GPT-B and unknown layouts are unsupported")
    return {name: {"start_lba": start, "end_lba": end, "sectors": end - start + 1}
            for name, (start, end) in EXPECTED_PARTITIONS.items()}, primary, secondary


def _open_for_validation(path=None, root_fd=None, relative=None, expected=None):
    if root_fd is not None:
        return _open_relative(root_fd, relative, expected)
    return _open_path(path)


def _validate_boot(path=None, root_fd=None, relative=None, expected=None):
    fd, before = _open_for_validation(path, root_fd, relative, expected)
    try:
        head = os.read(fd, 2048)
        if len(head) < 1632 or head[:8] != b"ANDROID!":
            raise store.RefusedError("boot.img is not an Android boot image")
        import struct
        fields = struct.unpack_from("<10I", head, 8)
        kernel_size, _, ramdisk_size, _, second_size, _, _, page_size, header_version, _ = fields
        if page_size < 1632 or page_size > 65536 or page_size & (page_size - 1) or header_version != 0:
            raise store.RefusedError("boot.img is not a supported Android boot image v0")
        if not kernel_size or not ramdisk_size or second_size:
            raise store.RefusedError("boot.img kernel/ramdisk structure is unsupported")
        expected_size = page_size + ((kernel_size + page_size - 1) // page_size) * page_size + ((ramdisk_size + page_size - 1) // page_size) * page_size
        if before[3] != expected_size:
            raise store.RefusedError("boot.img size does not match its Android boot v0 header")
        _assert_unchanged(fd, before)
        return {"format": "android-boot-v0", "page_size": page_size,
                "kernel_size": kernel_size, "ramdisk_size": ramdisk_size}
    finally:
        os.close(fd)


def _validate_ext4(path=None, capacity=None, root_fd=None, relative=None, expected=None):
    fd, before = _open_for_validation(path, root_fd, relative, expected)
    try:
        size = before[3]
        if size < 2048 or size % 512:
            raise store.RefusedError("rootfs.img is not a sector-aligned ext4 image")
        os.lseek(fd, 1024, os.SEEK_SET)
        superblock = os.read(fd, 1024)
        if len(superblock) != 1024 or superblock[56:58] != b"\x53\xef":
            raise store.RefusedError("rootfs.img does not contain an ext4 superblock")
        import struct
        blocks = struct.unpack_from("<I", superblock, 4)[0]
        log_block_size = struct.unpack_from("<I", superblock, 24)[0]
        if not blocks or log_block_size > 6:
            raise store.RefusedError("rootfs.img ext4 geometry is invalid")
        block_size = 1024 << log_block_size
        if blocks * block_size > size or size > capacity:
            raise store.RefusedError("rootfs.img exceeds its ext4 size or target userdata capacity")
        _assert_unchanged(fd, before)
        return {"format": "ext4", "image_size": size, "filesystem_size": blocks * block_size,
                "target_capacity": capacity}
    finally:
        os.close(fd)


def _registered_build(home, build_id):
    registry = store.load_registry(home)
    if not isinstance(build_id, str) or not build_id or len(build_id) > 128 or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for c in build_id):
        raise store.RefusedError("build ID has unsafe characters")
    matches = [item for item in registry.get("builds", []) if item.get("build_id") == build_id]
    if len(matches) != 1:
        raise store.RefusedError("build ID is missing or ambiguous in the private registry")
    root = Path(matches[0].get("path", ""))
    expected_root = Path(os.path.abspath(Path(home) / "builds" / build_id))
    if Path(os.path.abspath(root)) != expected_root:
        raise store.RefusedError("registered build path is missing or unsafe")
    root_fd, _ = _open_dir(root)
    manifest_path = root / "build.json"
    try:
        manifest_bytes = _read_relative(root_fd, "build.json")
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        raise store.RefusedError("registered build manifest is invalid")
    finally:
        os.close(root_fd)
    if (manifest.get("schema_version") != 1 or manifest.get("build_id") != build_id or manifest.get("board") != "ufi103s" or
            matches[0].get("board") != manifest.get("board") or matches[0].get("profile") != manifest.get("profile")):
        raise store.RefusedError("build ID or board does not match the supported GPT-T adapter")
    artifacts = manifest.get("artifacts", {})
    for name in ("boot.img", "rootfs.img"):
        path = root / name
        record = artifacts.get(name, {})
        digest, size = _hash_info(path)
        if record.get("sha256") != digest or record.get("size") != size:
            raise store.RefusedError("registered build artifact hash or size is invalid: " + name)
    return root, manifest, hashlib.sha256(manifest_bytes).hexdigest()


def _verify_set(root):
    root_fd, root_signature = _open_dir(root)
    try:
        files, entries = _scan_tree_fd(root_fd)
        expected = {"payload/boot.img", "payload/rootfs.img", "layout/gpt-T-primary.bin", "layout/gpt-T-secondary.bin"}
        expected_entries = expected | {"manifest.json", "SHA256SUMS", "README.txt", "payload/", "layout/"}
        if entries != expected_entries:
            raise store.RefusedError("GPT-T image set has missing or unlisted files")
        manifest = json.loads(_read_relative(root_fd, "manifest.json", files["manifest.json"]).decode("utf-8"))
        if manifest.get("schema_version") != 1 or manifest.get("format") != "openstick-gpt-t-image-set" or manifest.get("layout_id") != LAYOUT_ID:
            raise store.RefusedError("unsupported image-set format or GPT layout")
        if manifest.get("board") != "ufi103s" or not manifest.get("build_id"):
            raise store.RefusedError("image-set board/build provenance is invalid")
        try:
            sums_data = _read_relative(root_fd, "SHA256SUMS", files["SHA256SUMS"]).decode("ascii")
        except UnicodeError as exc:
            raise store.RefusedError("GPT-T image-set checksum list is not ASCII") from exc
        rows = {}
        for line in sums_data.splitlines():
            pieces = line.split("  ", 1)
            if len(pieces) != 2 or len(pieces[0]) != 64 or pieces[1] in rows:
                raise store.RefusedError("GPT-T image-set checksum list is malformed")
            rel = pieces[1]
            if Path(rel).is_absolute() or ".." in Path(rel).parts or rel not in expected:
                raise store.RefusedError("GPT-T image-set checksum list contains an unsafe member")
            rows[rel] = pieces[0]
        if set(rows) != expected:
            raise store.RefusedError("GPT-T image set has missing or unlisted checksums")
        if any(not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest)
               for digest in rows.values()):
            raise store.RefusedError("GPT-T image-set checksum list contains an invalid digest")
        for rel, digest in rows.items():
            if _hash_relative(root_fd, rel, files[rel]) != digest:
                raise store.RefusedError("GPT-T image-set checksum mismatch: " + rel)
        gpt_primary = _hash_relative(root_fd, "layout/gpt-T-primary.bin", files["layout/gpt-T-primary.bin"])
        gpt_secondary = _hash_relative(root_fd, "layout/gpt-T-secondary.bin", files["layout/gpt-T-secondary.bin"])
        if manifest.get("gpt", {}).get("primary_sha256") != gpt_primary or manifest.get("gpt", {}).get("secondary_sha256") != gpt_secondary:
            raise store.RefusedError("GPT-T manifest GPT hashes do not match the layout files")
        partitions, _, _ = _gpt_layout(root_fd=root_fd)
        if partitions != manifest.get("partitions"):
            raise store.RefusedError("GPT-T layout geometry does not match the manifest")
        boot_format = _validate_boot(root_fd=root_fd, relative="payload/boot.img", expected=files["payload/boot.img"])
        rootfs_format = _validate_ext4(root_fd=root_fd, relative="payload/rootfs.img",
                                       expected=files["payload/rootfs.img"],
                                       capacity=partitions["userdata"]["sectors"] * 512)
        if boot_format != manifest.get("boot_format") or rootfs_format != manifest.get("rootfs_format"):
            raise store.RefusedError("GPT-T image metadata does not match the manifest")
        if manifest.get("artifacts", {}).get("boot.img", {}).get("partition") != "boot" or manifest.get("artifacts", {}).get("rootfs.img", {}).get("partition") != "userdata":
            raise store.RefusedError("GPT-T artifact-to-partition mapping is invalid")
        for name, rel in (("boot.img", "payload/boot.img"), ("rootfs.img", "payload/rootfs.img")):
            record = manifest.get("artifacts", {}).get(name, {})
            if rows[rel] != record.get("sha256") or files[rel][3] != record.get("size"):
                raise store.RefusedError("GPT-T manifest artifact does not match the payload")
        later_files, later_entries = _scan_tree_fd(root_fd)
        if later_entries != entries or later_files != files or _signature(os.fstat(root_fd)) != root_signature:
            raise store.RefusedError("GPT-T image set changed during validation")
        return manifest
    finally:
        os.close(root_fd)


def verify_command(path):
    manifest = _verify_set(path)
    print("PASS GPT-T image-set integrity: build=%s board=%s layout=%s" %
          (manifest["build_id"], manifest["board"], manifest["layout_id"]))
    print("HARDWARE_REQUIRED: this offline preparation does not prove device mode, identity transition, or flash/readback behavior")
    return 0


def prepare_command(args, home, repo):
    output = Path(os.path.abspath(args.output))
    store.validate_location(output)
    if output.exists():
        raise store.RefusedError("image-set output already exists; choose a new private destination")
    build_root, build, build_manifest = _registered_build(home, args.build)
    if build.get("profile") not in ("dev", "prod"):
        raise store.RefusedError("build profile is invalid")
    layout, primary, secondary = _gpt_layout(args.layout)
    boot = build_root / "boot.img"
    rootfs = build_root / "rootfs.img"
    boot_format = _validate_boot(boot)
    userdata_capacity = layout["userdata"]["sectors"] * 512
    rootfs_format = _validate_ext4(rootfs, userdata_capacity)
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if store._has_symlink_component(output.parent) or store._inside_git(output.parent):
        raise store.RefusedError("image-set parent is unsafe or inside a Git worktree")
    staging = Path(tempfile.mkdtemp(prefix=".openstick-image-set-", dir=str(output.parent)))
    os.chmod(staging, 0o700)
    try:
        (staging / "payload").mkdir(mode=0o700)
        (staging / "layout").mkdir(mode=0o700)
        _copy_verified(boot, staging / "payload" / "boot.img", build["artifacts"]["boot.img"]["sha256"], build["artifacts"]["boot.img"]["size"])
        _copy_verified(rootfs, staging / "payload" / "rootfs.img", build["artifacts"]["rootfs.img"]["sha256"], build["artifacts"]["rootfs.img"]["size"])
        (staging / "layout" / "gpt-T-primary.bin").write_bytes(primary)
        (staging / "layout" / "gpt-T-secondary.bin").write_bytes(secondary)
        artifacts = {}
        for name in ("boot.img", "rootfs.img"):
            path = staging / "payload" / name
            expected_artifact = build["artifacts"][name]
            digest, size = _hash_info(path)
            if digest != expected_artifact["sha256"] or size != expected_artifact["size"]:
                raise store.RefusedError("build artifact changed while preparing image set: " + name)
            artifacts[name] = {"sha256": digest, "size": size,
                               "partition": "boot" if name == "boot.img" else "userdata"}
        manifest = {"schema_version": 1, "format": "openstick-gpt-t-image-set", "layout_id": LAYOUT_ID,
                    "board": "ufi103s", "profile": build["profile"], "build_id": args.build,
                    "sensitive": True, "legacy_user_env": bool(build.get("legacy_user_env")),
                    "source_build_manifest_sha256": build_manifest, "artifacts": artifacts,
                    "partitions": layout, "boot_format": boot_format, "rootfs_format": rootfs_format,
                    "gpt": {"primary_sha256": _sha(staging / "layout" / "gpt-T-primary.bin"),
                            "secondary_sha256": _sha(staging / "layout" / "gpt-T-secondary.bin")},
                    "verified_by": ["registered-build-artifact-hashes", "android-boot-v0-header", "ext4-superblock-and-size",
                                    "paired-gpt-crc-and-geometry"],
                    "hardware_flash_verified": False}
        (staging / "manifest.json").write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        rows = []
        for rel in ("payload/boot.img", "payload/rootfs.img", "layout/gpt-T-primary.bin", "layout/gpt-T-secondary.bin"):
            rows.append("%s  %s" % (_sha(staging / rel), rel))
        (staging / "SHA256SUMS").write_text("\n".join(rows) + "\n", encoding="ascii")
        (staging / "README.txt").write_text(
            "Sensitive device image set. boot.img targets GPT-T partition boot; rootfs.img targets userdata.\n"
            "Prepared offline from a registered build. This package has no verified device write path.\n",
            encoding="utf-8")
        os.chmod(staging / "manifest.json", 0o600)
        os.chmod(staging / "SHA256SUMS", 0o600)
        os.chmod(staging / "README.txt", 0o600)
        _chmod_private_tree(staging)
        _verify_set(staging)
        _rename_noreplace(staging, output)
        print("GPT-T image set prepared: %s" % output)
        print("boot.img -> boot; rootfs.img -> userdata; byte copies and hashes verified.")
        print("HARDWARE_REQUIRED: no write, mode transition, or target readback was performed or validated.")
        return 0
    except BaseException:
        _cleanup_staging(staging)
        raise

