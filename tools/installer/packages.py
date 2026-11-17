"""Portable, integrity checked OpenStick package creation and verification.

Package verification is intentionally read-only: it never extracts or executes
package content.  Portable execution gates live in scripts carried by each
package, so packages do not depend on this checkout.
"""
import hashlib
import csv
import json
import os
import re
import shutil
import stat
import sys
import subprocess
import tarfile
import tempfile
import time
import uuid
from pathlib import Path, PurePosixPath

from . import store

SCHEMA = 1
MAX_MANIFEST = 1024 * 1024
MAX_PACKAGE = 32 * 1024 ** 3
_HEX = re.compile(r"[0-9a-f]{64}\Z")


def _sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _archive_sha(archive, member):
    digest = hashlib.sha256()
    stream = archive.extractfile(member)
    for block in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(block)
    return digest.hexdigest()


def _safe_member(name):
    if not isinstance(name, str) or not name or "\\" in name or "\x00" in name:
        return False
    path = PurePosixPath(name)
    return (not path.is_absolute() and path.as_posix() == name and
            all(part not in ("", ".", "..") for part in name.split("/")))


def _tar_members(path):
    path = Path(path)
    absolute = Path(os.path.abspath(path))
    # Walk/open each parent with O_NOFOLLOW and open the leaf by dir_fd. This
    # prevents a concurrent replacement with a symlink between validation/open.
    directory_fd = os.open(absolute.anchor, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        for component in absolute.parts[1:-1]:
            next_fd = os.open(component, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        fd = os.open(absolute.name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory_fd)
    except OSError as exc:
        os.close(directory_fd)
        raise store.RefusedError("package path contains a symlink or is not readable") from exc
    os.close(directory_fd)
    try:
        file_obj = os.fdopen(fd, "rb")
        info = os.fstat(file_obj.fileno())
        if not stat.S_ISREG(info.st_mode):
            file_obj.close()
            raise store.RefusedError("package must be a regular archive file")
        if info.st_size > MAX_PACKAGE:
            file_obj.close()
            raise store.RefusedError("package exceeds the supported maximum size")
        archive = tarfile.open(fileobj=file_obj, mode="r:")
    except (OSError, tarfile.TarError) as exc:
        try: file_obj.close()
        except (UnboundLocalError, OSError): pass
        raise store.RefusedError("package is not a readable uncompressed tar archive") from exc
    return archive, file_obj


def verify_package(path):
    """Verify a .ostpkg.tar without extracting or executing any member.

    Returns the validated manifest. Raises RefusedError for malformed, unsafe,
    incomplete, or tampered packages.
    """
    archive, archive_file = _tar_members(path)
    with archive_file, archive:
        members = archive.getmembers()
        if len(members) > 20000:
            raise store.RefusedError("package contains too many tar members")
        seen = set()
        regular = {}
        directories = set()
        total_size = 0
        for member in members:
            name = member.name.rstrip("/") if member.isdir() else member.name
            if not _safe_member(name) or name in seen:
                raise store.RefusedError("package has an unsafe or duplicate tar member")
            seen.add(name)
            if member.isdir():
                if member.size != 0:
                    raise store.RefusedError("package directory member has data")
                directories.add(name)
                continue
            if not member.isfile():
                raise store.RefusedError("package contains a link or special tar member")
            if member.size < 0 or member.size > MAX_PACKAGE:
                raise store.RefusedError("package member size is invalid")
            total_size += member.size
            if total_size > MAX_PACKAGE:
                raise store.RefusedError("package uncompressed data exceeds supported size")
            regular[name] = member
        for name in regular:
            parent = PurePosixPath(name).parent
            while str(parent) not in (".", ""):
                if str(parent) in regular:
                    raise store.RefusedError("package has a file/directory member conflict")
                parent = parent.parent
        expected_directories = set()
        for name in regular:
            parent = PurePosixPath(name).parent
            while str(parent) not in (".", ""):
                expected_directories.add(str(parent))
                parent = parent.parent
        if directories != expected_directories:
            raise store.RefusedError("package contains unlisted or missing directory members")
        for required in ("manifest.json", "SHA256SUMS", "README.txt"):
            if required not in regular:
                raise store.RefusedError("package is missing " + required)
        manifest_member = regular["manifest.json"]
        if manifest_member.size > MAX_MANIFEST:
            raise store.RefusedError("package manifest is too large")
        stream = archive.extractfile(manifest_member)
        try:
            manifest = json.loads(stream.read(MAX_MANIFEST + 1).decode("utf-8"))
        except (AttributeError, UnicodeError, json.JSONDecodeError) as exc:
            raise store.RefusedError("package manifest is invalid JSON") from exc
        if not isinstance(manifest, dict) or manifest.get("schema_version") != SCHEMA:
            raise store.RefusedError("unsupported package manifest schema")
        if manifest.get("format") != "ostpkg" or manifest.get("package_type") not in ("install", "restore"):
            raise store.RefusedError("unsupported package type")
        if not isinstance(manifest.get("sensitive"), bool) or not manifest["sensitive"]:
            raise store.RefusedError("package must be explicitly labeled SENSITIVE")
        files = manifest.get("files")
        if not isinstance(files, dict) or not files:
            raise store.RefusedError("package manifest has no file inventory")
        expected = set(files) | {"manifest.json", "SHA256SUMS"}
        if set(regular) != expected:
            raise store.RefusedError("package has unlisted files or listed files are missing")
        if manifest["package_type"] == "restore":
            required = {"payload/emmc-full.bin", "payload/manifest.json", "payload/gpt-primary.bin",
                        "payload/gpt-secondary.bin", "payload/SHA256SUMS", "payload/partitions.csv", "payload/rollback-plan.py", "payload/rollback-plan.txt",
                        "scripts/restore.py"}
        else:
            board = manifest.get("board")
            if not isinstance(board, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", board):
                raise store.RefusedError("install package has an invalid board ID")
            required = {"payload/backup/emmc-full.bin", "payload/backup/manifest.json",
                             "payload/image-set/manifest.json", "payload/image-set/files-" + board + "/boot.bin",
                             "payload/image-set/files-" + board + "/rootfs.bin",
                             "payload/image-set/files-" + board + "/SHA256SUMS",
                             "payload/image-set/fw/layout-" + board + ".env",
                             "payload/external/firehose-loader.sha256",
                             "payload/image-set/pathA/SHA256SUMS", "scripts/install.py",
                             "scripts/runtime/tools/installer/flash.py",
                             "scripts/runtime/tools/installer/cli.py", "scripts/runtime/tools/installer/store.py",
                             "scripts/runtime/tools/installer/seed.py", "scripts/runtime/tools/installer/configure.py",
                             "scripts/runtime/tools/installer/imageset.py",
                             "scripts/runtime/tools/installer/__init__.py",
                             "scripts/runtime/tools/flash/flash-board.sh", "scripts/runtime/tools/pathA/pathA-flash.sh",
                             "scripts/runtime/tools/pathA/fwlib.sh", "scripts/runtime/scripts/common.sh",
                             "scripts/runtime/boards/" + board + "/board.env", "scripts/runtime/config/defaults.env"}
        if not required.issubset(files) or "README.txt" not in files:
            raise store.RefusedError("package is missing required standalone payload or safety script")
        for name, record in files.items():
            if not _safe_member(name) or name in ("manifest.json", "SHA256SUMS"):
                raise store.RefusedError("package manifest lists an unsafe path")
            if name != "README.txt" and not name.startswith(("payload/", "scripts/")):
                raise store.RefusedError("package file is outside payload/ or scripts/")
            if not isinstance(record, dict) or type(record.get("size")) is not int or record["size"] < 0 or not _HEX.fullmatch(str(record.get("sha256", ""))):
                raise store.RefusedError("package manifest contains an invalid file record")
            member = regular.get(name)
            if member is None or member.size != record["size"]:
                raise store.RefusedError("package file size does not match its manifest")
            src = archive.extractfile(member)
            digest = hashlib.sha256()
            while True:
                block = src.read(1024 * 1024)
                if not block:
                    break
                digest.update(block)
            if digest.hexdigest() != record["sha256"]:
                raise store.RefusedError("package file SHA-256 mismatch: " + name)
        sums_file = archive.extractfile(regular["SHA256SUMS"])
        try:
            sums = sums_file.read(MAX_MANIFEST).decode("ascii", "strict").splitlines()
        except UnicodeError as exc:
            raise store.RefusedError("package SHA256SUMS is not ASCII") from exc
        expected_sums = ["%s  %s" % (files[name]["sha256"], name) for name in sorted(files)]
        if sums != expected_sums:
            raise store.RefusedError("package SHA256SUMS does not match the manifest")
        if manifest["package_type"] == "restore":
            try:
                if regular["payload/manifest.json"].size > MAX_MANIFEST:
                    raise store.RefusedError("restore package backup manifest is too large")
                backup = json.loads(archive.extractfile(regular["payload/manifest.json"]).read(MAX_MANIFEST + 1).decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise store.RefusedError("restore package backup manifest is invalid") from exc
            if (backup.get("device_id", "").lower() != manifest.get("device_id", "").lower() or
                    backup.get("sha256") != manifest.get("backup_sha256") or
                    backup.get("size") != manifest.get("full_disk_bytes") or
                    files.get("payload/emmc-full.bin", {}).get("sha256") != backup.get("sha256")):
                raise store.RefusedError("restore package metadata does not match its full backup")
            transition = manifest.get("transition_state")
            if transition is not None:
                transition_names = ("payload/transition/manifest.json", "payload/transition/source-manifest.json",
                                    "payload/transition/pathA-SHA256SUMS", "payload/transition/gpt-T-primary.bin",
                                    "payload/transition/gpt-T-secondary.bin")
                if not set(transition_names).issubset(regular):
                    raise store.RefusedError("restore transition evidence is incomplete")
                if (regular["payload/transition/manifest.json"].size > MAX_MANIFEST or
                        regular["payload/transition/source-manifest.json"].size > MAX_MANIFEST or
                        regular["payload/transition/pathA-SHA256SUMS"].size > MAX_MANIFEST):
                    raise store.RefusedError("restore transition metadata is too large")
                try:
                    transition_file = json.loads(archive.extractfile(regular["payload/transition/manifest.json"]).read(MAX_MANIFEST + 1).decode("utf-8"))
                    source_manifest = json.loads(archive.extractfile(regular["payload/transition/source-manifest.json"]).read(MAX_MANIFEST + 1).decode("utf-8"))
                    patha_lines = archive.extractfile(regular["payload/transition/pathA-SHA256SUMS"]).read(MAX_MANIFEST + 1).decode("ascii", "strict").splitlines()
                except (UnicodeError, json.JSONDecodeError) as exc:
                    raise store.RefusedError("restore transition provenance is invalid") from exc
                patha_sums = {}
                for line in patha_lines:
                    hit = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9_./+-]+)", line)
                    if not hit or hit.group(2) in patha_sums:
                        raise store.RefusedError("restore transition checksum list is malformed")
                    patha_sums[hit.group(2)] = hit.group(1)
                if (transition_file != transition or transition.get("schema_version") != 1 or
                        transition.get("format") != "openstick-gpt-b-transition" or
                        transition.get("device_id", "").lower() != manifest.get("device_id", "").lower() or
                        transition.get("backup_sha256") != backup.get("sha256") or
                        transition.get("board") != manifest.get("board") or
                        _archive_sha(archive, regular["payload/transition/manifest.json"]) != manifest.get("transition_manifest_sha256") or
                        _archive_sha(archive, regular["payload/transition/source-manifest.json"]) != transition.get("source_manifest_sha256") or
                        source_manifest.get("format") != "openstick-gpt-b-image-set" or
                        source_manifest.get("board") != transition.get("board") or
                        source_manifest.get("build_id") != transition.get("build_id") or
                        source_manifest.get("redistribution_authorized") is not True):
                    raise store.RefusedError("restore transition source manifest mismatch")
                gpt = transition.get("gpt", {})
                for member_name, sum_name, digest_key, backup_name, backup_key, length in (
                        ("payload/transition/gpt-T-primary.bin", "gpt/gpt-T-primary.bin", "target_primary_sha256", "payload/gpt-primary.bin", "backup_primary_sha256", 34*512),
                        ("payload/transition/gpt-T-secondary.bin", "gpt/gpt-T-secondary.bin", "target_secondary_sha256", "payload/gpt-secondary.bin", "backup_secondary_sha256", 33*512)):
                    digest = _archive_sha(archive, regular[member_name])
                    original_name = "rollback/gpt-orig-" + ("primary.bin" if "primary" in sum_name else "secondary.bin")
                    if (regular[member_name].size != length or gpt.get(digest_key) != digest or
                            patha_sums.get(sum_name) != digest or patha_sums.get(original_name) != gpt.get(backup_key) or
                            files.get(backup_name, {}).get("sha256") != gpt.get(backup_key)):
                        raise store.RefusedError("restore transition GPT differs from package backup or source")
                expected_fw = {"sbl1": ("fw/sbl1.img", "rollback/stock-sbl1.bin", "sbl1"),
                               "rpm": ("fw/rpm.img", "rollback/stock-rpm.bin", "rpm"),
                               "tz": ("fw/tz.img", "rollback/stock-tz-tzbak.bin", "tz"),
                               "hyp": ("fw/hyp.img", "rollback/stock-hyp.bin", "hyp"),
                               "aboot": ("fw/aboot.img", "rollback/stock-aboot.bin", "aboot")}
                firmware = transition.get("firmware")
                if not isinstance(firmware, dict) or set(firmware) != set(expected_fw):
                    raise store.RefusedError("restore transition firmware evidence is incomplete")
                for name, (new_name, stock_name, part_name) in expected_fw.items():
                    record = firmware[name]; part = backup.get("partitions", {}).get(part_name)
                    if (not isinstance(record, dict) or not isinstance(part, dict) or
                            record.get("start_lba") != part.get("start_lba") or record.get("end_lba") != part.get("end_lba") or
                            patha_sums.get(new_name) != record.get("new_sha256") or patha_sums.get(stock_name) != record.get("stock_sha256") or
                            not _HEX.fullmatch(str(record.get("new_sha256", ""))) or not _HEX.fullmatch(str(record.get("stock_sha256", "")))):
                        raise store.RefusedError("restore transition firmware evidence mismatch: " + name)
            transition = manifest.get("transition_state")
            if transition is not None:
                transition_required = {"payload/transition/manifest.json", "payload/transition/source-manifest.json",
                                      "payload/transition/pathA-SHA256SUMS", "payload/transition/gpt-T-primary.bin",
                                      "payload/transition/gpt-T-secondary.bin"}
                if not transition_required.issubset(regular):
                    raise store.RefusedError("restore package transition evidence is incomplete")
                if (transition.get("schema_version") != 1 or transition.get("format") != "openstick-gpt-b-transition" or
                        transition.get("device_id", "").lower() != manifest.get("device_id", "").lower() or
                        transition.get("backup_sha256") != backup.get("sha256") or
                        transition.get("board") != manifest.get("board") or
                        regular["payload/transition/manifest.json"].size > MAX_MANIFEST or
                        regular["payload/transition/source-manifest.json"].size > MAX_MANIFEST or
                        regular["payload/transition/pathA-SHA256SUMS"].size > MAX_MANIFEST or
                        _archive_sha(archive, regular["payload/transition/manifest.json"]) != manifest.get("transition_manifest_sha256")):
                    raise store.RefusedError("restore package transition metadata is invalid")
                try:
                    transition_file = json.loads(archive.extractfile(regular["payload/transition/manifest.json"]).read(MAX_MANIFEST + 1).decode("utf-8"))
                    source_manifest = json.loads(archive.extractfile(regular["payload/transition/source-manifest.json"]).read(MAX_MANIFEST + 1).decode("utf-8"))
                    patha_lines = archive.extractfile(regular["payload/transition/pathA-SHA256SUMS"]).read(MAX_MANIFEST + 1).decode("ascii", "strict").splitlines()
                    patha_sums = {}
                    for line in patha_lines:
                        hit = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9_./+-]+)", line)
                        if not hit or hit.group(2) in patha_sums:
                            raise store.RefusedError("restore transition checksum list is malformed")
                        patha_sums[hit.group(2)] = hit.group(1)
                except (UnicodeError, json.JSONDecodeError) as exc:
                    raise store.RefusedError("restore transition provenance is malformed") from exc
                if (transition_file != transition or _archive_sha(archive, regular["payload/transition/source-manifest.json"]) != transition.get("source_manifest_sha256") or
                        source_manifest.get("format") != "openstick-gpt-b-image-set" or
                        source_manifest.get("board") != transition.get("board") or source_manifest.get("build_id") != transition.get("build_id") or
                        source_manifest.get("redistribution_authorized") is not True):
                    raise store.RefusedError("restore transition source provenance does not match")
                gpt = transition.get("gpt")
                for package_name, source_name, key, backup_name, backup_key, size in (
                        ("payload/transition/gpt-T-primary.bin", "gpt/gpt-T-primary.bin", "target_primary_sha256", "payload/gpt-primary.bin", "backup_primary_sha256", 34*512),
                        ("payload/transition/gpt-T-secondary.bin", "gpt/gpt-T-secondary.bin", "target_secondary_sha256", "payload/gpt-secondary.bin", "backup_secondary_sha256", 33*512)):
                    member = regular[package_name]
                    digest = _archive_sha(archive, member)
                    if (member.size != size or digest != gpt.get(key) or patha_sums.get(source_name) != digest or
                            patha_sums.get("rollback/" + source_name.split("/")[-1].replace("gpt-T", "gpt-orig")) != gpt.get(backup_key) or
                            files.get(backup_name, {}).get("sha256") != gpt.get(backup_key)):
                        raise store.RefusedError("restore transition GPT evidence does not match the source or backup")
                expected_firmware = {"sbl1": ("fw/sbl1.img", "rollback/stock-sbl1.bin", "sbl1"),
                                     "rpm": ("fw/rpm.img", "rollback/stock-rpm.bin", "rpm"),
                                     "tz": ("fw/tz.img", "rollback/stock-tz-tzbak.bin", "tz"),
                                     "hyp": ("fw/hyp.img", "rollback/stock-hyp.bin", "hyp"),
                                     "aboot": ("fw/aboot.img", "rollback/stock-aboot.bin", "aboot")}
                firmware = transition.get("firmware")
                if not isinstance(firmware, dict) or set(firmware) != set(expected_firmware):
                    raise store.RefusedError("restore transition firmware evidence is incomplete")
                for name, (new_name, stock_name, part_name) in expected_firmware.items():
                    row = firmware[name]; part = backup.get("partitions", {}).get(part_name)
                    if (not isinstance(row, dict) or not isinstance(part, dict) or
                            row.get("start_lba") != part.get("start_lba") or row.get("end_lba") != part.get("end_lba") or
                            patha_sums.get(new_name) != row.get("new_sha256") or patha_sums.get(stock_name) != row.get("stock_sha256") or
                            _HEX.fullmatch(str(row.get("new_sha256", ""))) is None or _HEX.fullmatch(str(row.get("stock_sha256", ""))) is None):
                        raise store.RefusedError("restore transition firmware evidence does not match backup geometry: " + name)
            if regular["payload/SHA256SUMS"].size > MAX_MANIFEST:
                raise store.RefusedError("restore package checksum list is too large")
            sum_lines = archive.extractfile(regular["payload/SHA256SUMS"]).read(MAX_MANIFEST + 1).decode("ascii", "strict").splitlines()
            if sum_lines != [backup.get("sha256") + "  emmc-full.bin"]:
                raise store.RefusedError("restore payload SHA256SUMS does not match its full backup")
            dump_stream = archive.extractfile(regular["payload/emmc-full.bin"])
            for name, start, count in (("payload/gpt-primary.bin", 0, 34),
                                       ("payload/gpt-secondary.bin", backup["sectors"] - 33, 33)):
                source = archive.extractfile(regular[name])
                source_bytes = source.read(count * 512 + 1)
                if len(source_bytes) != count * 512:
                    raise store.RefusedError("restore package GPT evidence has an invalid size")
                dump_stream.seek(start * 512)
                if source_bytes != dump_stream.read(count * 512):
                    raise store.RefusedError("restore package GPT evidence does not match the full dump")
        else:
            try:
                if (regular["payload/backup/manifest.json"].size > MAX_MANIFEST or
                        regular["payload/image-set/manifest.json"].size > MAX_MANIFEST):
                    raise store.RefusedError("install package provenance manifest is too large")
                backup = json.loads(archive.extractfile(regular["payload/backup/manifest.json"]).read(MAX_MANIFEST + 1).decode("utf-8"))
                images = json.loads(archive.extractfile(regular["payload/image-set/manifest.json"]).read(MAX_MANIFEST + 1).decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise store.RefusedError("install package provenance manifest is invalid") from exc
            loader_record = manifest.get("external_dependencies", {}).get("firehose_loader_sha256")
            if (backup.get("device_id", "").lower() != manifest.get("device_id", "").lower() or
                    backup.get("sha256") != manifest.get("required_backup_sha256") or
                    files.get("payload/backup/emmc-full.bin", {}).get("sha256") != backup.get("sha256") or
                    images.get("format") != "openstick-gpt-b-image-set" or images.get("board") != manifest.get("board") or
                    images.get("build_id") != manifest.get("build_id") or
                    images.get("profile") != manifest.get("profile") or
                    images.get("redistribution_authorized") is not True or
                    images.get("loader_sha256") != loader_record or not _HEX.fullmatch(str(loader_record or ""))):
                raise store.RefusedError("install package device, build, board, image-set, or external loader provenance mismatch")
            if (files.get("payload/image-set/manifest.json", {}).get("sha256") != manifest.get("image_set_manifest_sha256") or
                    backup.get("size") != manifest.get("full_disk_bytes")):
                raise store.RefusedError("install package backup or image-set hash metadata mismatch")
            if regular["payload/backup/SHA256SUMS"].size > MAX_MANIFEST:
                raise store.RefusedError("install backup checksum list is too large")
            backup_sums = archive.extractfile(regular["payload/backup/SHA256SUMS"]).read(MAX_MANIFEST + 1).decode("ascii", "strict").splitlines()
            if backup_sums != [backup.get("sha256") + "  emmc-full.bin"]:
                raise store.RefusedError("install backup SHA256SUMS does not match its full image")

            def check_nested_sums(summary_name, prefix, external=None):
                if regular[summary_name].size > MAX_MANIFEST:
                    raise store.RefusedError("nested image-set checksum list is too large")
                lines = archive.extractfile(regular[summary_name]).read(MAX_MANIFEST + 1).decode("ascii", "strict").splitlines()
                listed = set()
                for line in lines:
                    match = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9_./+-]+)", line)
                    if not match:
                        raise store.RefusedError("nested image-set checksum list is malformed")
                    digest, relative = match.groups()
                    if relative.startswith("/") or ".." in Path(relative).parts or relative in listed:
                        raise store.RefusedError("nested image-set checksum path is unsafe or duplicated")
                    listed.add(relative)
                    full = prefix + relative
                    if external and relative == external[0]:
                        if digest != external[1]:
                            raise store.RefusedError("external loader checksum differs from its declared dependency")
                    elif files.get(full, {}).get("sha256") != digest:
                        raise store.RefusedError("nested image-set checksum mismatch: " + relative)
                actual = {name[len(prefix):] for name in files
                          if name.startswith(prefix) and name != summary_name}
                expected_actual = listed - ({external[0]} if external else set())
                if actual != expected_actual:
                    raise store.RefusedError("nested image-set has unlisted or missing members")

            loader_rel = "loader/prog_emmc_firehose_8916.mbn"
            check_nested_sums("payload/image-set/SHA256SUMS", "payload/image-set/",
                              (loader_rel, loader_record))
            board = manifest["board"]
            check_nested_sums("payload/image-set/files-" + board + "/SHA256SUMS",
                              "payload/image-set/files-" + board + "/")
            check_nested_sums("payload/image-set/pathA/SHA256SUMS", "payload/image-set/pathA/")
        return manifest


def _verified_backup(home, device, backup_id=None):
    candidates = [item for item in device.get("backups", []) if backup_id is None or item.get("id") == backup_id]
    for item in candidates:
        path = Path(item.get("path", ""))
        if path.is_symlink() or store._has_symlink_component(path):
            continue
        try:
            manifest_path = path / "manifest.json"
            sums_path = path / "SHA256SUMS"
            if (manifest_path.is_symlink() or sums_path.is_symlink() or
                    not stat.S_ISREG(os.lstat(manifest_path).st_mode) or
                    not stat.S_ISREG(os.lstat(sums_path).st_mode)):
                continue
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            dump = path / "emmc-full.bin"
            sums = sums_path.read_text(encoding="ascii").splitlines()
            if (manifest.get("schema_version") != 1 or
                    not {"full-read-size", "gpt-primary-and-backup-crc", "sha256"}.issubset(manifest.get("verified_by", [])) or
                    manifest.get("device_id", "").lower() != device.get("id", "").lower() or
                    dump.is_symlink() or not dump.is_file() or dump.stat().st_size != manifest.get("size") or
                    _sha(dump) != manifest.get("sha256") or item.get("sha256") != manifest.get("sha256") or
                    sums != [manifest.get("sha256") + "  emmc-full.bin"] or
                    manifest.get("identity") != device.get("identity")):
                continue
            partitions = manifest.get("partitions")
            if not isinstance(partitions, dict) or not all(p in partitions for p in ("fsg", "sec", "fsc")):
                continue
            # Re-parse the full image instead of trusting registry/manifest
            # claims; compare GPT geometry and all three protected identity
            # fingerprints to the registered device.
            from . import cli as common
            actual = common._parse_gpt(dump)
            expected = {name: (int(value["start_lba"]), int(value["end_lba"]))
                        for name, value in partitions.items()}
            if actual != expected or dump.stat().st_size % 512 or manifest.get("sectors") != dump.stat().st_size // 512:
                continue
            actual_identity = {name: common._hash_range(dump, *actual[name]) for name in ("fsg", "sec", "fsc")}
            if actual_identity != manifest["identity"]:
                continue
            return path, manifest
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
    raise store.RefusedError("no verified, byte-complete matching backup is available")


def _restore_transition(device, backup_manifest):
    """Return checked Path A state hashes for a portable restore, if available."""
    flashes = device.get("flashes", [])
    if not isinstance(flashes, list):
        return None
    from . import flash as flash_module
    needed = (
        "gpt/gpt-T-primary.bin", "gpt/gpt-T-secondary.bin",
        "rollback/gpt-orig-primary.bin", "rollback/gpt-orig-secondary.bin",
        "fw/sbl1.img", "fw/rpm.img", "fw/tz.img", "fw/hyp.img", "fw/aboot.img",
        "rollback/stock-sbl1.bin", "rollback/stock-rpm.bin", "rollback/stock-tz-tzbak.bin",
        "rollback/stock-hyp.bin", "rollback/stock-aboot.bin",
    )
    for flash_record in reversed(flashes):
        root = Path(flash_record.get("image_set", ""))
        if (not root.is_dir() or root.is_symlink() or store._has_symlink_component(root) or
                store._inside_git(root)):
            continue
        try:
            outer = flash_module._checked_sum_file(root, require_all=True)
            patha = root / "pathA"
            inner = flash_module._checked_sum_file(patha)
            required = {"pathA/" + name for name in needed}
            if not required.issubset(outer) or not set(needed).issubset(inner):
                continue
            source_manifest_path = root / "manifest.json"
            if source_manifest_path.is_symlink() or not source_manifest_path.is_file():
                continue
            source_manifest = json.loads(source_manifest_path.read_text(encoding="utf-8"))
            if (source_manifest.get("format") != "openstick-gpt-b-image-set" or
                    source_manifest.get("board") != device.get("board") or
                    source_manifest.get("redistribution_authorized") is not True):
                continue
            source_hashes = {}
            for rel in needed:
                item = patha / rel
                if item.is_symlink() or not item.is_file() or "pathA/" + rel not in outer or rel not in inner:
                    raise store.RefusedError("registered Path A transition asset changed")
                source_hashes[rel] = _sha(item)
            firmware = {}
            spans = {
                "sbl1": ("fw/sbl1.img", "rollback/stock-sbl1.bin", "sbl1"),
                "rpm": ("fw/rpm.img", "rollback/stock-rpm.bin", "rpm"),
                "tz": ("fw/tz.img", "rollback/stock-tz-tzbak.bin", "tz"),
                "hyp": ("fw/hyp.img", "rollback/stock-hyp.bin", "hyp"),
                "aboot": ("fw/aboot.img", "rollback/stock-aboot.bin", "aboot"),
            }
            for name, (new, stock, partition) in spans.items():
                row = backup_manifest["partitions"][partition]
                start, end = int(row["start_lba"]), int(row["end_lba"])
                expected_size = (end - start + 1) * 512
                if (Path(patha / new).stat().st_size != expected_size or
                        Path(patha / stock).stat().st_size != expected_size):
                    raise store.RefusedError("Path A transition firmware size differs from verified backup geometry")
                firmware[name] = {"start_lba": start, "end_lba": end,
                                  "new_sha256": source_hashes[new], "stock_sha256": source_hashes[stock]}
            transition = {"schema_version": 1, "format": "openstick-gpt-b-transition",
                          "board": source_manifest["board"], "build_id": source_manifest["build_id"],
                          "source_manifest_sha256": _sha(source_manifest_path),
                          "gpt": {
                              "target_primary_sha256": source_hashes["gpt/gpt-T-primary.bin"],
                              "target_secondary_sha256": source_hashes["gpt/gpt-T-secondary.bin"],
                              "backup_primary_sha256": source_hashes["rollback/gpt-orig-primary.bin"],
                              "backup_secondary_sha256": source_hashes["rollback/gpt-orig-secondary.bin"]},
                          "firmware": firmware}
            copied = {name: patha / name for name in needed}
            copied["_source_manifest"] = source_manifest_path
            copied["_patha_sums"] = patha / "SHA256SUMS"
            return transition, copied
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            continue
    return None


def _copy_file(source, destination):
    source, destination = Path(source), Path(destination)
    if source.is_symlink() or not source.is_file():
        raise store.RefusedError("package input is missing or unsafe: " + source.name)
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with source.open("rb") as inp, destination.open("xb") as out:
        shutil.copyfileobj(inp, out, 1024 * 1024)
        out.flush()
        os.fsync(out.fileno())
    os.chmod(destination, 0o600)


def _write_script(path, content):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    os.chmod(path, 0o700)


_RESTORE_RUNNER_INSPECT = r'''#!/usr/bin/env python3
"""Standalone guarded restore package entrypoint (Python 3 standard library)."""
import argparse, hashlib, json, os, re, shlex, shutil, signal, subprocess, sys, tempfile
from pathlib import Path
PINNED_LOADER_SHA256="959439aa5864685999b713c3ed12ad5fa408149648b670a9a9ef77bcc9dcab14"
IDENTITY_READ_TIMEOUT_SECONDS=60
INTERRUPT_GRACE_SECONDS=10
POWER_CYCLE_HINT='The board may be in a half-open EDL session. Unplug USB for 30 s and reconnect WITHOUT the button, then re-enter EDL.'
EDL_FAILURE_HINT='Hint: verify EDL mode and the USB connection, confirm the pinned loader, correct the reported issue, and re-enter EDL before retrying.'
EDL_SESSION_CALLS=0
EDL_SESSION_USED_NOTE='EDL session used. Before the next command that talks to the board: unplug USB for 30 s, reconnect WITHOUT the button, re-enter EDL. Never use "edl reset".'
def sha(path):
    h=hashlib.sha256()
    with open(path,'rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''): h.update(b)
    return h.hexdigest()
def resolve_client(requested):
    directory=os.environ.get('OPENSTICK_EDL_DIR')
    if directory:
        root=Path(directory); executable=root/'edl'; script=root/'edl.py'
        if executable.is_file() and os.access(executable,os.X_OK): return [str(executable.resolve())]
        if script.is_file(): return [sys.executable,str(script.resolve())]
        raise RuntimeError('OPENSTICK_EDL_DIR must contain edl or edl.py')
    candidate=Path(requested) if requested else None
    if candidate and candidate.is_file():
        if candidate.suffix=='.py': return [sys.executable,str(candidate.resolve())]
        if os.access(candidate,os.X_OK): return [str(candidate.resolve())]
    found=shutil.which(requested or 'edl')
    if found: return [str(Path(found).resolve())]
    raise RuntimeError('EDL client unavailable; set OPENSTICK_EDL_DIR or provide an executable path')
def edl_failure(message,out='',err='',verbose=False,debug=False):
    detail=message
    if 'Hint:' not in detail: detail+=' '+EDL_FAILURE_HINT
    if (verbose or debug) and 'EDL client output (last lines):' not in detail:
        tail='\n'.join((str(out or '')+'\n'+str(err or '')).splitlines()[-12:])
        if tail: detail+='\nEDL client output (last lines):\n'+tail
    return detail
def run_edl(client,operation,loader,timeout,verbose=False,debug=False):
    global EDL_SESSION_CALLS
    EDL_SESSION_CALLS+=1
    cmd=client+operation+['--loader='+str(loader),'--memory=emmc']
    print('EDL command: '+shlex.join(cmd))
    p=subprocess.Popen(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try: out,err=p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try: p.send_signal(signal.SIGINT)
        except ProcessLookupError: pass
        try: out,err=p.communicate(timeout=INTERRUPT_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            p.terminate()
            try: out,err=p.communicate(timeout=INTERRUPT_GRACE_SECONDS)
            except subprocess.TimeoutExpired as final:
                raise RuntimeError(edl_failure('EDL client ignored SIGINT and terminate. '+POWER_CYCLE_HINT,
                                               final.stdout,final.stderr,verbose,debug))
            raise RuntimeError(edl_failure('EDL client timed out after SIGINT. '+POWER_CYCLE_HINT,out,err,verbose,debug))
        raise RuntimeError(edl_failure('EDL client timed out. '+POWER_CYCLE_HINT,out,err,verbose,debug))
    if p.returncode:
        raise RuntimeError(edl_failure('EDL client exited unsuccessfully. '+POWER_CYCLE_HINT,out,err,verbose,debug))
    return out,err
def parse_sahara(text):
    serial=re.search(r'(?m)^Serial:\s*0x([0-9a-fA-F]{8,16})\s*$',text)
    hwid=re.search(r'(?m)^HWID:\s*0x([0-9a-fA-F]{8,16})(?:\s|$)',text)
    pk=re.search(r'(?m)^PK_HASH:\s*0x([0-9a-fA-F]{64})\s*$',text)
    if not (serial and hwid and pk): raise RuntimeError('EDL Sahara identity block is missing or malformed. Hint: verify EDL mode and client compatibility.')
    return {'serial':serial.group(1).lower(),'hwid':hwid.group(1).lower(),'pk_hash':pk.group(1).lower()}
def resolve_client(requested=None):
 directory=os.environ.get('OPENSTICK_EDL_DIR')
 if directory:
  root=Path(directory); executable=root/'edl'; script=root/'edl.py'
  if executable.is_file() and os.access(executable,os.X_OK): return [str(executable.resolve())]
  if script.is_file(): return [sys.executable,str(script.resolve())]
  raise RuntimeError('OPENSTICK_EDL_DIR must contain edl or edl.py')
 candidate=Path(requested) if requested else None
 if candidate and candidate.is_file():
  if candidate.suffix=='.py': return [sys.executable,str(candidate.resolve())]
  if os.access(candidate,os.X_OK): return [str(candidate.resolve())]
 found=shutil.which(requested or 'edl')
 if found: return [str(Path(found).resolve())]
 raise RuntimeError('EDL client unavailable; set OPENSTICK_EDL_DIR or provide an executable path')
def _run_main():
    p=argparse.ArgumentParser(description='Offline verified OpenStick restore package')
    p.add_argument('--edl', help='explicit EDL client executable')
    p.add_argument('--loader', required=True, help='pinned Firehose loader')
    p.add_argument('--device', required=True, help='expected live EDL serial')
    p.add_argument('--dry-run', action='store_true'); p.add_argument('--verbose',action='store_true'); p.add_argument('--debug',action='store_true')
    p.add_argument('--phase', choices=('inspect','restore'), default='inspect'); p.add_argument('--confirm', default='')
    a=p.parse_args(); root=Path(__file__).resolve().parents[1]
    package=json.loads((root/'manifest.json').read_text())
    if package.get('format')!='ostpkg' or package.get('package_type')!='restore' or package.get('sensitive') is not True:
        print('REFUSED: unsupported package manifest',file=sys.stderr); return 2
    files=package.get('files')
    if not isinstance(files,dict): print('REFUSED: package file inventory missing',file=sys.stderr); return 2
    for rel,record in files.items():
        candidate=root/rel
        if candidate.is_symlink() or not candidate.is_file() or candidate.stat().st_size!=record.get('size') or sha(candidate)!=record.get('sha256'):
            print('REFUSED: package member integrity mismatch',file=sys.stderr); return 2
    sums=['%s  %s'%(files[n]['sha256'],n) for n in sorted(files)]
    if (root/'SHA256SUMS').read_text().splitlines()!=sums:
        print('REFUSED: package checksum list mismatch',file=sys.stderr); return 2
    m=json.loads((root/'payload/manifest.json').read_text()); dump=root/'payload/emmc-full.bin'
    if m.get('device_id','').lower()!=package.get('device_id','').lower() or m.get('sha256')!=package.get('backup_sha256'):
        print('REFUSED: package and backup identity metadata disagree',file=sys.stderr); return 2
    if sha(dump)!=m['sha256'] or dump.stat().st_size!=m['size']:
        print('REFUSED: packaged full-disk backup hash or size mismatch',file=sys.stderr); return 2
    if not re.fullmatch('[0-9a-fA-F]{8,64}',a.device) or a.device.lower()!=m['device_id'].lower():
        print('REFUSED: expected package device identity does not match',file=sys.stderr); return 2
    loader=Path(a.loader).absolute()
    expected=package.get('external_dependencies',{}).get('firehose_loader_sha256',PINNED_LOADER_SHA256)
    if loader.is_symlink() or not loader.is_file() or sha(loader)!=expected:
        print('REFUSED: external Firehose loader is missing or hash mismatched',file=sys.stderr); return 2
    loader=loader.resolve(strict=True)
    try:
        client=resolve_client(a.edl)
        with tempfile.TemporaryDirectory(prefix='openstick-package-inspect-') as td:
            target=Path(td)/'primary-gpt.bin'
            out,err=run_edl(client,['rs','0','34',str(target)],loader,IDENTITY_READ_TIMEOUT_SECONDS,a.verbose,a.debug)
            if not target.is_file() or target.stat().st_size!=34*512: raise RuntimeError(edl_failure('primary GPT identity range was incomplete',out,err,a.verbose,a.debug))
            try: identity=parse_sahara(out+'\n'+err)
            except RuntimeError as exc: raise RuntimeError(edl_failure(str(exc),out,err,a.verbose,a.debug))
            if identity['serial']!=m['device_id'].lower(): raise RuntimeError('live EDL serial mismatch')
    except (OSError,RuntimeError) as exc:
        print('REFUSED: '+str(exc),file=sys.stderr); return 2
    if a.phase=='inspect':
        print('PASS: package hash, backup identity, and live EDL serial verified; read-only inspect only')
        print('Restore is not enabled by this package runner; use the reviewed OpenStick restore integration.')
        return 0
    print('REFUSED: no destructive restore phase is authorized by this standalone package version',file=sys.stderr)
    return 2

def finish_session():
    if EDL_SESSION_CALLS: print(EDL_SESSION_USED_NOTE)
def main():
    global EDL_SESSION_CALLS
    EDL_SESSION_CALLS=0
    try: return _run_main()
    finally: finish_session()

if __name__=='__main__': raise SystemExit(main())
'''

# Standalone packages are often unpacked into a directory writable by the
# invoking user.  Validate a descriptor-opened source tree once, copy its exact
# inventory into a private snapshot, and use only that snapshot afterwards.
# This closes the validate-then-use window for packages with shell helpers that
# may later run through sudo.  The tar verifier above remains read-only.
_STANDALONE_SNAPSHOT = r'''import atexit, stat

_SNAPSHOT_FLAGS=os.O_RDONLY|getattr(os,'O_CLOEXEC',0)
_SNAPSHOT_DIR_FLAGS=_SNAPSHOT_FLAGS|getattr(os,'O_DIRECTORY',0)|getattr(os,'O_NOFOLLOW',0)
_SNAPSHOT_FILE_FLAGS=_SNAPSHOT_FLAGS|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_NONBLOCK',0)
_PACKAGE_SNAPSHOT=None

def _snapshot_open_dir(path):
 path=os.path.abspath(os.fspath(path)); fd=os.open(os.path.sep,_SNAPSHOT_DIR_FLAGS)
 try:
  for part in Path(path).parts[1:]:
   child=os.open(part,_SNAPSHOT_DIR_FLAGS,dir_fd=fd); os.close(fd); fd=child
  if not stat.S_ISDIR(os.fstat(fd).st_mode): raise RuntimeError('package root is not a directory')
  return fd
 except BaseException:
  os.close(fd); raise

def _snapshot_open_file(root_fd,rel):
 if not isinstance(rel,str) or not rel or rel.startswith('/') or '\\' in rel or '\x00' in rel:
  raise RuntimeError('unsafe package member path')
 parts=rel.split('/')
 if any(part in ('','.','..') for part in parts): raise RuntimeError('unsafe package member path')
 parent=os.dup(root_fd)
 try:
  for part in parts[:-1]:
   child=os.open(part,_SNAPSHOT_DIR_FLAGS,dir_fd=parent); os.close(parent); parent=child
  fd=os.open(parts[-1],_SNAPSHOT_FILE_FLAGS,dir_fd=parent)
 finally: os.close(parent)
 info=os.fstat(fd)
 if not stat.S_ISREG(info.st_mode): os.close(fd); raise RuntimeError('package member is not a regular file')
 return fd, (info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns)

def _snapshot_scan(fd,prefix=''):
 files=set(); dirs=set()
 for name in os.listdir(fd):
  if name in ('','.','..') or '/' in name or '\\' in name: raise RuntimeError('unsafe package directory entry')
  rel=(prefix+'/'+name).lstrip('/'); info=os.stat(name,dir_fd=fd,follow_symlinks=False)
  if stat.S_ISDIR(info.st_mode):
   dirs.add(rel); child=os.open(name,_SNAPSHOT_DIR_FLAGS,dir_fd=fd)
   try:
    subfiles,subdirs=_snapshot_scan(child,rel); files.update(subfiles); dirs.update(subdirs)
   finally: os.close(child)
  elif stat.S_ISREG(info.st_mode): files.add(rel)
  else: raise RuntimeError('package contains a symlink or special file')
 return files,dirs

def _snapshot_package(source,expected_type):
 global _PACKAGE_SNAPSHOT
 root_fd=_snapshot_open_dir(source)
 try:
  fd,_=_snapshot_open_file(root_fd,'manifest.json')
  with os.fdopen(fd,'rb') as stream: manifest_raw=stream.read(1024*1024+1)
  manifest=json.loads(manifest_raw.decode('utf-8'))
  if manifest.get('format')!='ostpkg' or manifest.get('package_type')!=expected_type or manifest.get('sensitive') is not True:
   raise RuntimeError('unsupported package manifest')
  inventory=manifest.get('files')
  if not isinstance(inventory,dict) or not inventory: raise RuntimeError('package inventory missing')
  if len(inventory)>20000: raise RuntimeError('package contains too many files')
  total_size=0
  expected=set(inventory)|{'manifest.json','SHA256SUMS'}
  directories=set()
  for name,record in inventory.items():
   if not isinstance(name,str) or name.startswith('/') or '\\' in name or any(p in ('','.','..') for p in name.split('/')):
    raise RuntimeError('unsafe package inventory path')
   if not isinstance(record,dict) or type(record.get('size')) is not int or record['size']<0 or not isinstance(record.get('sha256'),str) or not re.fullmatch('[0-9a-f]{64}',record['sha256']):
    raise RuntimeError('invalid package file record')
   if record['size']>32*1024**3: raise RuntimeError('package member exceeds supported size')
   total_size+=record['size']
   if total_size>32*1024**3: raise RuntimeError('package exceeds supported total size')
   parent=Path(name).parent
   while str(parent) not in ('.',''):
    directories.add(str(parent)); parent=parent.parent
  if 'README.txt' not in inventory: raise RuntimeError('package README missing')
  actual,actual_dirs=_snapshot_scan(root_fd)
  if len(actual)>20002: raise RuntimeError('package contains too many filesystem members')
  if actual!=expected or actual_dirs!=directories: raise RuntimeError('package contains unlisted files or directories')
  fd,_=_snapshot_open_file(root_fd,'SHA256SUMS')
  with os.fdopen(fd,'rb') as stream: sums_raw=stream.read(1024*1024+1)
  sums=sums_raw.decode('ascii').splitlines()
  wanted=['%s  %s'%(inventory[n]['sha256'],n) for n in sorted(inventory)]
  if sums!=wanted: raise RuntimeError('package checksum list mismatch')
  _PACKAGE_SNAPSHOT=tempfile.mkdtemp(prefix='openstick-package-'); os.chmod(_PACKAGE_SNAPSHOT,0o700)
  snapshot=Path(_PACKAGE_SNAPSHOT)
  for directory in sorted(directories,key=lambda x:(x.count('/'),x)):
   target=snapshot/directory; target.mkdir(mode=0o700,parents=True,exist_ok=True)
  for name in sorted(expected):
   source_fd,before=_snapshot_open_file(root_fd,name); target=snapshot/name
   target.parent.mkdir(mode=0o700,parents=True,exist_ok=True); digest=hashlib.sha256(); size=0
   try:
    with os.fdopen(source_fd,'rb') as inp, target.open('xb') as out:
     while True:
      block=inp.read(1024*1024)
      if not block: break
      out.write(block); digest.update(block); size+=len(block)
     out.flush(); os.fsync(out.fileno())
     after=os.fstat(inp.fileno())
    if (after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns)!=before:
     raise RuntimeError('package member changed while snapshotting')
   except BaseException:
    try: target.unlink()
    except OSError: pass
    raise
   if name in inventory and (size!=inventory[name]['size'] or digest.hexdigest()!=inventory[name]['sha256']):
    raise RuntimeError('package member integrity mismatch: '+name)
   os.chmod(target,0o700 if name.startswith('scripts/') else 0o600)
  if (snapshot/'manifest.json').read_bytes()!=manifest_raw or (snapshot/'SHA256SUMS').read_bytes()!=sums_raw:
   raise RuntimeError('package metadata changed while snapshotting')
  atexit.register(shutil.rmtree,_PACKAGE_SNAPSHOT,True)
  return snapshot,manifest
 except BaseException:
  if _PACKAGE_SNAPSHOT:
   shutil.rmtree(_PACKAGE_SNAPSHOT,ignore_errors=True); _PACKAGE_SNAPSHOT=None
  raise
 finally: os.close(root_fd)
'''

_RESTORE_RUNNER = r'''#!/usr/bin/env python3
"""Standalone phased restore package. Python 3 stdlib plus pinned external tools."""
import argparse,fcntl,hashlib,json,os,re,shutil,signal,stat,subprocess,sys,tempfile,time,shlex
from pathlib import Path
# __STANDALONE_SNAPSHOT__
PINNED_LOADER_SHA256="959439aa5864685999b713c3ed12ad5fa408149648b670a9a9ef77bcc9dcab14"
CHUNK=1024*1024
IDENTITY_READ_TIMEOUT_SECONDS=60
RANGE_READ_TIMEOUT_SECONDS=60
FULL_READ_TIMEOUT_SECONDS=3600
WRITE_TIMEOUT_SECONDS=600
INTERRUPT_GRACE_SECONDS=10
POWER_CYCLE_HINT='The board may be in a half-open EDL session. Unplug USB for 30 s and reconnect WITHOUT the button, then re-enter EDL.'
EDL_FAILURE_HINT='Hint: verify EDL mode and the USB connection, confirm the pinned loader, correct the reported issue, and re-enter EDL before retrying.'
EDL_CLIENT=[]; EDL_LOADER=None; VERBOSE=False; DEBUG=False
EDL_LAST_OUT=''; EDL_LAST_ERR=''
EDL_SESSION_IDENTITY=None
EDL_SESSION_CALLS=0
EDL_SESSION_USED_NOTE='EDL session used. Before the next command that talks to the board: unplug USB for 30 s, reconnect WITHOUT the button, re-enter EDL. Never use "edl reset".'
def usb_sysfs_root_default():
 value=os.environ.get('OPENSTICK_USB_SYSFS_ROOT'); marker=os.environ.get('OPENSTICK_TEST_USB_ROOTS_DIR')
 real='/sys/bus/usb/devices'
 if not value or not marker: return real
 try:
  base=os.path.realpath(marker); resolved=os.path.realpath(value)
  if not os.path.isabs(marker) or not os.path.isabs(value) or base==os.sep or not os.path.isdir(base) or os.path.commonpath([base,resolved])!=base: return real
 except (OSError,ValueError): return real
 return value
EDL_SYSFS_ROOT=usb_sysfs_root_default()
EDL_SESSION_BUS_KEY=None
def sha(p):
 h=hashlib.sha256()
 with open(p,'rb') as f:
  for b in iter(lambda:f.read(CHUNK),b''): h.update(b)
 return h.hexdigest()
def call(cmd,timeout=300): return subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=timeout)
def edl_failure(message,out=None,err=None):
 detail=message
 if 'Hint:' not in detail: detail+=' '+EDL_FAILURE_HINT
 if out is None: out=EDL_LAST_OUT
 if err is None: err=EDL_LAST_ERR
 if (VERBOSE or DEBUG) and 'EDL client output (last lines):' not in detail:
  tail='\n'.join((str(out or '')+'\n'+str(err or '')).splitlines()[-12:])
  if tail: detail+='\nEDL client output (last lines):\n'+tail
 return detail
def usb_preflight():
 if EDL_SYSFS_ROOT is None: return None
 root=Path(EDL_SYSFS_ROOT)
 try: entries=sorted(root.iterdir(),key=lambda p:p.name)
 except OSError: return None
 normal=None
 try:
  for entry in entries:
   vendor=entry/'idVendor'; product=entry/'idProduct'
   if not vendor.is_file() or not product.is_file(): continue
   v=vendor.read_text(encoding='ascii').strip().lower(); p=product.read_text(encoding='ascii').strip().lower()
   if v!='05c6': continue
   if p=='9008':
    bus=entry/'busnum'; dev=entry/'devnum'
    key=(bus.read_text(encoding='ascii').strip(),dev.read_text(encoding='ascii').strip()) if bus.is_file() and dev.is_file() else None
    return ('EDL',p,key)
   if normal is None: normal=p
 except (OSError,UnicodeError): return None
 if normal is not None: return ('NORMAL',normal,None)
 return ('ABSENT','',None)
def check_usb_session():
 global EDL_SESSION_BUS_KEY
 observed=usb_preflight()
 if observed is None: return
 state,product,key=observed
 if EDL_SESSION_BUS_KEY is None:
  if state=='ABSENT': raise RuntimeError('No Qualcomm EDL device (05c6:9008) is connected. Put the board in EDL mode, then run this command again as the first EDL command of that session.')
  if state=='NORMAL': raise RuntimeError('The board is connected in normal mode (05c6:%s), not EDL. Enter EDL mode, then run this command again.'%product)
  EDL_SESSION_BUS_KEY=key
 elif state!='EDL' or (key is not None and key!=EDL_SESSION_BUS_KEY):
  raise RuntimeError('The 05c6:9008 USB bus/device number changed during this command; possible board swap. Stop and reconnect the original board before retrying.')
def edl_refusal(message,result=None):
 out=result.stdout if result is not None else None
 err=result.stderr if result is not None else None
 print('REFUSED: '+edl_failure(message,out,err),file=sys.stderr)
def resolve_client(requested=None):
 directory=os.environ.get('OPENSTICK_EDL_DIR')
 if directory:
  root=Path(directory); executable=root/'edl'; script=root/'edl.py'
  if executable.is_file() and os.access(executable,os.X_OK): return [str(executable.resolve())]
  if script.is_file(): return [sys.executable,str(script.resolve())]
  raise RuntimeError('OPENSTICK_EDL_DIR must contain edl or edl.py')
 candidate=Path(requested) if requested else None
 if candidate and candidate.is_file():
  if candidate.suffix=='.py': return [sys.executable,str(candidate.resolve())]
  if os.access(candidate,os.X_OK): return [str(candidate.resolve())]
 found=shutil.which(requested or 'edl')
 if found: return [str(Path(found).resolve())]
 raise RuntimeError('EDL client unavailable; set OPENSTICK_EDL_DIR or provide an executable path')
def edl_call(operation,timeout=300,allow_failure=False):
 global EDL_LAST_OUT, EDL_LAST_ERR, EDL_SESSION_IDENTITY, EDL_SESSION_CALLS
 check_usb_session()
 cmd=EDL_CLIENT+operation+['--loader='+str(EDL_LOADER),'--memory=emmc']
 print('EDL command: '+shlex.join(cmd))
 EDL_SESSION_CALLS+=1
 p=subprocess.Popen(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,start_new_session=True)
 try: out,err=p.communicate(timeout=timeout)
 except subprocess.TimeoutExpired:
  try: os.killpg(p.pid,signal.SIGINT)
  except ProcessLookupError: pass
  try: out,err=p.communicate(timeout=INTERRUPT_GRACE_SECONDS)
  except subprocess.TimeoutExpired:
   try: os.killpg(p.pid,signal.SIGTERM)
   except ProcessLookupError: pass
   try: out,err=p.communicate(timeout=INTERRUPT_GRACE_SECONDS)
   except subprocess.TimeoutExpired as final:
    EDL_LAST_OUT=final.stdout or ''; EDL_LAST_ERR=final.stderr or ''
    raise RuntimeError(edl_failure('EDL client ignored SIGINT and terminate. '+POWER_CYCLE_HINT,EDL_LAST_OUT,EDL_LAST_ERR))
   EDL_LAST_OUT=out; EDL_LAST_ERR=err
   raise RuntimeError(edl_failure('EDL client timed out after SIGINT. '+POWER_CYCLE_HINT,out,err))
  EDL_LAST_OUT=out; EDL_LAST_ERR=err
  raise RuntimeError(edl_failure('EDL client timed out. '+POWER_CYCLE_HINT,out,err))
 EDL_LAST_OUT=out; EDL_LAST_ERR=err
 transcript=out+'\n'+err
 if EDL_SESSION_IDENTITY is None:
  if 'Waiting for the device' in transcript and 'Device detected :)' not in transcript:
   raise RuntimeError('No Qualcomm EDL device (05c6:9008) is connected. Put the board in EDL mode, then run this command again as the first EDL command of that session.')
  if 'Mode detected: firehose' in transcript and 'Serial:' not in transcript:
   raise RuntimeError('The board is in EDL, but its Sahara session was already used by an earlier command, so its identity cannot be read again. Unplug USB for 30 s, reconnect WITHOUT the button, re-enter EDL, and run this command first.')
  if 'Mode detected: sahara' not in transcript:
   raise RuntimeError(edl_failure('EDL client did not report a fresh Sahara identity block',out,err))
  EDL_SESSION_IDENTITY=parse_sahara(transcript)
 elif 'Mode detected: firehose' not in transcript:
  if 'Mode detected: sahara' in transcript:
   current=parse_sahara(transcript)
   if current!=EDL_SESSION_IDENTITY: raise RuntimeError('EDL identity changed during this command; possible board swap.')
  else: raise RuntimeError(edl_failure('EDL client did not report firehose mode for a later invocation',out,err))
 if p.returncode and not allow_failure:
  raise RuntimeError(edl_failure('EDL client exited unsuccessfully. '+POWER_CYCLE_HINT,out,err))
 return subprocess.CompletedProcess(cmd,p.returncode,out,err)
def parse_sahara(text):
 serial=re.search(r'(?m)^Serial:\s*0x([0-9a-fA-F]{8,16})\s*$',text)
 hwid=re.search(r'(?m)^HWID:\s*0x([0-9a-fA-F]{8,16})(?:\s|$)',text)
 pk=re.search(r'(?m)^PK_HASH:\s*0x([0-9a-fA-F]{64})\s*$',text)
 if not (serial and hwid and pk): raise RuntimeError('EDL Sahara identity block is missing or malformed. Hint: verify EDL mode and client compatibility.')
 return {'serial':serial.group(1).lower(),'hwid':hwid.group(1).lower(),'pk_hash':pk.group(1).lower()}
def read_identity(directory,expected_id):
 target=Path(directory)/'identity-primary-gpt.bin'
 result=edl_call(['rs','0','34',str(target)],IDENTITY_READ_TIMEOUT_SECONDS)
 if not target.is_file() or target.stat().st_size!=34*512: raise RuntimeError(edl_failure('EDL primary GPT identity range was incomplete',result.stdout,result.stderr))
 identity=EDL_SESSION_IDENTITY
 if not identity: raise RuntimeError(edl_failure('EDL session identity was not captured',result.stdout,result.stderr))
 if expected_id and identity['serial']!=expected_id.lower(): raise RuntimeError('live EDL serial mismatch. Hint: reconnect the intended board in EDL mode.')
 return identity
def read_range(edl,loader,start,count,path):
 r=edl_call(['rs',str(start),str(count),str(path)],RANGE_READ_TIMEOUT_SECONDS)
 if r.returncode or not Path(path).is_file() or Path(path).stat().st_size!=count*512: raise RuntimeError(edl_failure('live EDL range read failed',r.stdout,r.stderr))
 return sha(path)
def live_state(edl,loader,m,p,payload,work,completed,origin=None):
 if origin not in (None,'target','backup'): raise RuntimeError('restore GPT origin is unknown')
 read_identity(work,m['device_id'])
 for name in ('fsg','sec','fsc'):
  part=m['partitions'][name]
  got=read_range(edl,loader,int(part['start_lba']),int(part['end_lba'])-int(part['start_lba'])+1,work/('gate-'+name+'.bin'))
  if got!=m['identity'].get(name): raise RuntimeError(edl_failure('live '+name+' fingerprint mismatch'))
 transition=p.get('transition_state'); backup=payload
 target_primary=transition['gpt']['target_primary_sha256'] if transition else sha(backup/'gpt-primary.bin')
 target_secondary=transition['gpt']['target_secondary_sha256'] if transition else sha(backup/'gpt-secondary.bin')
 stock_primary=sha(backup/'gpt-primary.bin'); stock_secondary=sha(backup/'gpt-secondary.bin')
 actual={}
 for name,start,count in (('primary',0,34),('secondary',m['sectors']-33,33)):
  actual[name]=read_range(edl,loader,start,count,work/('gate-gpt-'+name+'.bin'))
 if origin is None:
  if actual=={'primary':target_primary,'secondary':target_secondary}: origin='target'
  elif actual=={'primary':stock_primary,'secondary':stock_secondary}: origin='backup'
  else: raise RuntimeError('live GPT matches neither known target nor backup state')
 expected_primary=stock_primary if 'gpt-primary' in completed else (target_primary if origin=='target' else stock_primary)
 expected_secondary=stock_secondary if 'gpt-secondary' in completed else (target_secondary if origin=='target' else stock_secondary)
 if actual!={'primary':expected_primary,'secondary':expected_secondary}: raise RuntimeError('live GPT differs from recorded restore phase')
 # A completed data phase is trusted only while its entire live partition still
 # matches the corresponding bytes in the verified full backup. This detects
 # torn or later-corrupted data writes before any following phase can proceed.
 for name in ('userdata','cache','persist','boot','modemst1','modemst2'):
  if name not in completed: continue
  part=m['partitions'].get(name)
  if not isinstance(part,dict): raise RuntimeError('backup partition geometry is missing for '+name)
  start=int(part['start_lba']); count=int(part['end_lba'])-start+1
  got=read_range(edl,loader,start,count,work/('gate-data-'+name+'.bin'))
  with (payload/'emmc-full.bin').open('rb') as source:
   source.seek(start*512); h=hashlib.sha256(); left=count*512
   while left:
    block=source.read(min(CHUNK,left))
    if not block: raise RuntimeError('verified backup is truncated for '+name)
    h.update(block); left-=len(block)
  if got!=h.hexdigest(): raise RuntimeError('completed restore data partition differs from backup: '+name)
 if transition:
  for name,item in transition['firmware'].items():
   got=read_range(edl,loader,int(item['start_lba']),int(item['end_lba'])-int(item['start_lba'])+1,work/('gate-fw-'+name+'.bin'))
   if got not in (item['new_sha256'],item['stock_sha256']): raise RuntimeError('live firmware fingerprint is unknown for '+name)
   if name in completed and got!=item['stock_sha256']: raise RuntimeError('completed restore firmware differs from backup for '+name)
 return origin
def atomic(path,data):
 path=Path(os.path.abspath(path)); flags=os.O_RDONLY|getattr(os,'O_DIRECTORY',0)|getattr(os,'O_NOFOLLOW',0)
 parent_fd=os.open(path.anchor,flags)
 try:
  for part in path.parts[1:-1]:
   try: child=os.open(part,flags,dir_fd=parent_fd)
   except FileNotFoundError:
    os.mkdir(part,0o700,dir_fd=parent_fd); child=os.open(part,flags,dir_fd=parent_fd)
   os.close(parent_fd); parent_fd=child
  try:
   current=os.stat(path.name,dir_fd=parent_fd,follow_symlinks=False)
   if not stat.S_ISREG(current.st_mode): raise RuntimeError('state file is not regular')
  except FileNotFoundError: pass
  temporary='.'+path.name+'.'+os.urandom(12).hex()+'.tmp'
  fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),0o600,dir_fd=parent_fd)
  try:
   os.fchmod(fd,0o600)
   with os.fdopen(fd,'w') as f: json.dump(data,f,sort_keys=True); f.write('\n'); f.flush(); os.fsync(f.fileno())
   os.replace(temporary,path.name,src_dir_fd=parent_fd,dst_dir_fd=parent_fd)
   os.fsync(parent_fd)
  except Exception:
   try: os.unlink(temporary,dir_fd=parent_fd)
   except OSError: pass
   raise
 finally: os.close(parent_fd)
def validate_transition(root,p,m):
 t=p.get('transition_state')
 if t is None: return True
 try:
  base=root/'payload/transition'
  if (t.get('schema_version')!=1 or t.get('format')!='openstick-gpt-b-transition' or
      t.get('device_id','').lower()!=m.get('device_id','').lower() or t.get('backup_sha256')!=m.get('sha256') or
      t.get('board')!=p.get('board')): return False
  mt=base/'manifest.json'; sm=base/'source-manifest.json'; sums=base/'pathA-SHA256SUMS'
  if any(x.stat().st_size>1024*1024 for x in (mt,sm,sums)): return False
  if sha(mt)!=p.get('transition_manifest_sha256') or json.loads(mt.read_text())!=t: return False
  source=json.loads(sm.read_text())
  if sha(sm)!=t.get('source_manifest_sha256') or source.get('format')!='openstick-gpt-b-image-set' or source.get('board')!=t.get('board') or source.get('build_id')!=t.get('build_id') or source.get('redistribution_authorized') is not True: return False
  patha={}
  for line in sums.read_text(encoding='ascii').splitlines():
   hit=re.fullmatch(r'([0-9a-f]{64})  ([A-Za-z0-9_./+-]+)',line)
   if not hit or hit.group(2) in patha: return False
   patha[hit.group(2)]=hit.group(1)
  g=t['gpt']
  for filename,source_name,key in (('gpt-T-primary.bin','gpt/gpt-T-primary.bin','target_primary_sha256'),
                                   ('gpt-T-secondary.bin','gpt/gpt-T-secondary.bin','target_secondary_sha256')):
   item=base/filename
   if sha(item)!=g.get(key) or patha.get(source_name)!=g.get(key): return False
  for filename,key,source_name in (('gpt-primary.bin','backup_primary_sha256','rollback/gpt-orig-primary.bin'),
                                   ('gpt-secondary.bin','backup_secondary_sha256','rollback/gpt-orig-secondary.bin')):
   if sha(root/'payload'/filename)!=g.get(key) or patha.get(source_name)!=g.get(key): return False
  expected={'sbl1':('fw/sbl1.img','rollback/stock-sbl1.bin','sbl1'),
            'rpm':('fw/rpm.img','rollback/stock-rpm.bin','rpm'),
            'tz':('fw/tz.img','rollback/stock-tz-tzbak.bin','tz'),
            'hyp':('fw/hyp.img','rollback/stock-hyp.bin','hyp'),
            'aboot':('fw/aboot.img','rollback/stock-aboot.bin','aboot')}
  fw=t.get('firmware')
  if not isinstance(fw,dict) or set(fw)!=set(expected): return False
  for name,(new,stock,partname) in expected.items():
   row=fw[name]; part=m['partitions'][partname]
   if (row.get('start_lba')!=part['start_lba'] or row.get('end_lba')!=part['end_lba'] or
       patha.get(new)!=row.get('new_sha256') or patha.get(stock)!=row.get('stock_sha256') or
       not re.fullmatch('[0-9a-f]{64}',str(row.get('new_sha256',''))) or not re.fullmatch('[0-9a-f]{64}',str(row.get('stock_sha256','')))): return False
  return True
 except (OSError,ValueError,KeyError,TypeError,AttributeError): return False
def acquire_lock(path):
 path=Path(os.path.abspath(path)); flags=os.O_RDONLY|getattr(os,'O_DIRECTORY',0)|getattr(os,'O_NOFOLLOW',0)
 directory_fd=os.open(path.anchor,flags)
 try:
  for part in path.parts[1:-1]:
   try: child=os.open(part,flags,dir_fd=directory_fd)
   except FileNotFoundError:
    os.mkdir(part,0o700,dir_fd=directory_fd); child=os.open(part,flags,dir_fd=directory_fd)
   os.close(directory_fd); directory_fd=child
  fd=os.open(path.name,os.O_CREAT|os.O_RDWR|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_NONBLOCK',0),0o600,dir_fd=directory_fd)
 finally: os.close(directory_fd)
 info=os.fstat(fd)
 if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:
  os.close(fd); raise RuntimeError('operation lock is not a private regular file')
 os.fchmod(fd,0o600)
 try:
  fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
 except BlockingIOError:
  os.close(fd); raise RuntimeError('another package operation is active; no write was attempted')
 return fd
def package_ok(root):
 try:
  checked=_snapshot_package(root,'restore')
  return checked if checked and validate_transition(checked[0],checked[1],json.loads((checked[0]/'payload/manifest.json').read_text())) else None
 except (OSError,ValueError,KeyError,TypeError,RuntimeError): return None
def _run_main(sysfs_root=None):
 global EDL_SYSFS_ROOT, EDL_SESSION_BUS_KEY, EDL_SESSION_IDENTITY, EDL_SESSION_CALLS
 if sysfs_root is None: sysfs_root=usb_sysfs_root_default()
 EDL_SYSFS_ROOT=sysfs_root; EDL_SESSION_BUS_KEY=None
 EDL_SESSION_IDENTITY=None; EDL_SESSION_CALLS=0
 ap=argparse.ArgumentParser(description='Portable, fail-closed phased restore from a complete verified backup. UNVALIDATED on hardware; writes require the normal typed restore confirmation.')
 ap.add_argument('--edl'); ap.add_argument('--loader',required=True); ap.add_argument('--device',required=True)
 ap.add_argument('--phase',default='inspect'); ap.add_argument('--dry-run',action='store_true'); ap.add_argument('--state')
 ap.add_argument('--verbose',action='store_true'); ap.add_argument('--debug',action='store_true')
 a=ap.parse_args(); root=Path(__file__).resolve().parents[1]; checked=package_ok(root)
 if not checked: print('REFUSED: package integrity failed',file=sys.stderr); return 2
 root,p=checked
 payload=root/'payload'; m=json.loads((payload/'manifest.json').read_text()); disk=payload/'emmc-full.bin'
 if p.get('device_id','').lower()!=m.get('device_id','').lower() or p.get('backup_sha256')!=m.get('sha256') or p.get('full_disk_bytes')!=m.get('size') or sha(disk)!=m.get('sha256') or disk.stat().st_size!=m.get('size'):
  print('REFUSED: complete backup integrity/identity mismatch',file=sys.stderr); return 2
 if a.device.lower()!=m['device_id'].lower(): print('REFUSED: selected device mismatch',file=sys.stderr); return 2
 loader=Path(a.loader).absolute()
 if loader.is_symlink() or not loader.is_file() or sha(loader)!=PINNED_LOADER_SHA256:
  print('REFUSED: Firehose loader missing or not pinned SHA-256',file=sys.stderr); return 2
 loader=loader.resolve(strict=True)
 state=Path.home()/('.local/state/openstick/restore-'+m['device_id']+'-'+m['sha256']+'.json')
 if a.state and Path(a.state).expanduser().absolute()!=state.absolute():
  print('REFUSED: restore state path is pinned per device and backup; alternate state files cannot authorize a retry',file=sys.stderr); return 2
 lock_fd=None
 global EDL_CLIENT, EDL_LOADER, VERBOSE, DEBUG
 try: EDL_CLIENT=resolve_client(a.edl)
 except RuntimeError as exc: print('REFUSED: '+str(exc),file=sys.stderr); return 2
 EDL_LOADER=loader; VERBOSE=a.verbose; DEBUG=a.debug
 try:
  with tempfile.TemporaryDirectory(prefix='ostpkg-restore-') as td:
   work=Path(td); os.chmod(work,0o700); live=work/'live-emmc.bin'
   read_identity(work,m['device_id'])
   read=edl_call(['rf',str(live)],FULL_READ_TIMEOUT_SECONDS)
   if read.returncode or not live.is_file() or live.stat().st_size!=m['size']:
    edl_refusal('live full-disk identity read unavailable or wrong-sized',read); return 2
   for name in ('fsg','sec','fsc'):
    row=m['partitions'].get(name)
    if not row: print('REFUSED: backup lacks '+name,file=sys.stderr); return 2
    start=int(row['start_lba'])*512; left=(int(row['end_lba'])-int(row['start_lba'])+1)*512; h=hashlib.sha256()
    with live.open('rb') as f:
     f.seek(start)
     while left:
      b=f.read(min(CHUNK,left))
      if not b: edl_refusal('short identity read '+name,read); return 2
      h.update(b); left-=len(b)
    if h.hexdigest()!=m['identity'].get(name): edl_refusal('live '+name+' fingerprint mismatch',read); return 2
   planner=[sys.executable,'-I',str(payload/'rollback-plan.py'),'--dump',str(disk),'--csv',str(payload/'partitions.csv'),
    '--primary',str(payload/'gpt-primary.bin'),'--secondary',str(payload/'gpt-secondary.bin'),'--emmc',str(m['sectors']),
    '--dump-sha',m['sha256'],'--work',str(work),'--loader',str(loader),'--edl',EDL_CLIENT[-1]]
   plan=call(planner,300)
   if plan.returncode: print('REFUSED: bundled rollback planner rejected backup',file=sys.stderr); return 2
   rows=[]
   for line in plan.stdout.splitlines():
    hit=re.match(r'## (R\d+|G\d+) ([A-Za-z0-9_-]+): LBA (\d+)\.\.(\d+),',line)
    if hit:
     tag,name,start,end=hit.groups(); start,end=int(start),int(end)
     if end<start: print('REFUSED: rollback planner emitted invalid range geometry',file=sys.stderr); return 2
     rows.append({'tag':tag,'name':name,'start':start,'count':end-start+1})
   expected=['userdata','cache','persist','boot','modemst1','modemst2','sbl1','rpm','tz','tzbak','hyp','aboot','gpt-secondary','gpt-primary']
   if [row['name'] for row in rows]!=expected:
    print('REFUSED: rollback planner phases are incomplete, duplicated, or out of order',file=sys.stderr); return 2
   for row in rows:
    name=row['name']
    if name=='gpt-primary': wanted=(0,34,'G')
    elif name=='gpt-secondary': wanted=(m['sectors']-33,33,'G')
    else:
     part=m['partitions'].get(name)
     if not isinstance(part,dict): print('REFUSED: backup manifest lacks planner partition '+name,file=sys.stderr); return 2
     start=int(part['start_lba']); end=int(part['end_lba'])
     wanted=(start,end-start+1,'R')
    if (row['start'],row['count'],row['tag'][:1])!=wanted:
     print('REFUSED: rollback planner range differs from verified backup geometry for '+name,file=sys.stderr); return 2
   if a.phase=='inspect':
    print('PASS: package, full backup, bundled rollback plan, live serial and fsg/sec/fsc fingerprints verified; read-only')
    print('Ordered restore phases: '+', '.join(r['name'] for r in rows)); return 0
   lock_fd=acquire_lock(state.with_name(state.name+'.lock'))
   state_data={'schema':1,'device_id':m['device_id'],'backup_sha256':m['sha256'],'status':'ready','completed':[]}
   if state.exists():
    if state.is_symlink(): print('REFUSED: state file is a symlink',file=sys.stderr); return 2
    state_data=json.loads(state.read_text())
    if not isinstance(state_data,dict) or state_data.get('schema')!=1 or state_data.get('status') not in ('ready','write-started','interrupted','failed-write','failed-verify','verified'):
     print('REFUSED: restore state schema or status is unknown',file=sys.stderr); return 2
    if state_data.get('status') in ('write-started','interrupted','failed-write','failed-verify'):
     print('REFUSED: destructive phase unresolved; manual reconciliation required',file=sys.stderr); return 2
    if state_data.get('device_id')!=m['device_id'] or state_data.get('backup_sha256')!=m['sha256']:
     print('REFUSED: state belongs to a different device or backup',file=sys.stderr); return 2
   ordered=[r['name'] for r in rows]
   completed=state_data.get('completed',[])
   if not isinstance(completed,list) or completed!=ordered[:len(completed)]:
    print('REFUSED: restore state is not an exact prefix of the rollback plan',file=sys.stderr); return 2
   if state_data.get('status')=='ready' and completed and state_data.get('phase')!=completed[-1]:
    print('REFUSED: ready restore state phase does not match its completed prefix',file=sys.stderr); return 2
   if state_data.get('status')=='ready' and not completed and state_data.get('phase') not in (None,''):
    print('REFUSED: initial ready restore state has an unexpected phase',file=sys.stderr); return 2
   if state_data.get('status')=='verified' and (completed!=ordered or state_data.get('phase')!='final' or state_data.get('final_sha256')!=m['sha256']):
    print('REFUSED: verified restore state lacks complete final hash evidence',file=sys.stderr); return 2
   if state_data.get('status')=='verified' and a.phase!='final':
    print('REFUSED: restore is already verified and will not replay writes',file=sys.stderr); return 2
   try: gpt_origin=live_state(a.edl,loader,m,p,payload,work,completed,state_data.get('gpt_origin'))
   except (OSError,ValueError,KeyError,RuntimeError,subprocess.TimeoutExpired) as e:
    print('REFUSED: live restore state is not recognized: '+str(e),file=sys.stderr); return 2
   if state_data.get('gpt_origin') not in (None,gpt_origin): print('REFUSED: restore GPT origin changed',file=sys.stderr); return 2
   state_data['gpt_origin']=gpt_origin
   if a.phase=='final':
    if completed!=ordered:
     print('REFUSED: final exact-hash check requires every planned restore phase to be completed',file=sys.stderr); return 2
    final=work/'final-emmc.bin'; r=edl_call(['rf',str(final)],FULL_READ_TIMEOUT_SECONDS,allow_failure=True)
    if r.returncode or not final.is_file() or final.stat().st_size!=m['size'] or sha(final)!=m['sha256']:
     state_data.update({'status':'failed-verify','phase':'final','at':int(time.time())}); atomic(state,state_data)
     print('VERIFY FAILED: complete full-disk readback differs. Do not boot.',file=sys.stderr); return 3
    state_data.update({'status':'verified','phase':'final','final_sha256':m['sha256'],'at':int(time.time())}); atomic(state,state_data)
    print('PASS: complete full-disk readback exactly matches backup SHA-256. Boot is not verified.'); return 0
   row=next((x for x in rows if x['name']==a.phase),None)
   if not row: print('REFUSED: phase is absent from validated rollback plan',file=sys.stderr); return 2
   if len(completed)>=len(ordered) or a.phase!=ordered[len(completed)]:
    print('REFUSED: phase is out of order; expected '+(ordered[len(completed)] if len(completed)<len(ordered) else 'final'),file=sys.stderr); return 2
   slice_path=work/(a.phase+'.bin')
   with disk.open('rb') as src, slice_path.open('xb') as dst:
    src.seek(row['start']*512); left=row['count']*512
    while left:
     b=src.read(min(CHUNK,left))
     if not b: raise RuntimeError('backup slice is truncated')
     dst.write(b); left-=len(b)
    dst.flush(); os.fsync(dst.fileno())
   os.chmod(slice_path,0o600)
   cmd=['ws',str(row['start']),str(slice_path),'--sectorsize=512']
   dry=edl_call(cmd+['--skipwrite'],RANGE_READ_TIMEOUT_SECONDS)
   token='restore %s %s'%(m['device_id'][-8:],a.phase)
   print('Phase %s at LBA %d for %d sectors; source SHA-256 %s'%(a.phase,row['start'],row['count'],sha(slice_path)))
   print('WARNING: packaged restore scripts are UNVALIDATED on hardware.')
   if a.dry_run: print('DRY RUN: skipwrite passed; no write occurred'); return 0
   if os.environ.get('COMMIT')!='1' or os.environ.get('CONFIRM')!=token:
    print('REFUSED: requires COMMIT=1 CONFIRM="%s" and typed confirmation'%token,file=sys.stderr); return 2
   if not sys.stdin.isatty(): print('REFUSED: write confirmation requires a terminal',file=sys.stderr); return 2
   print('Type exactly: '+token)
   if input('> ')!=token: print('REFUSED: typed confirmation mismatch',file=sys.stderr); return 2
   # Re-establish physical identity, firmware, and GPT after confirmation.
   try: live_state(a.edl,loader,m,p,payload,work,completed,gpt_origin)
   except (OSError,ValueError,KeyError,RuntimeError,subprocess.TimeoutExpired) as e:
    edl_refusal('live identity/firmware/GPT changed before destructive phase: '+str(e)); return 2
   state_data.update({'status':'write-started','phase':a.phase,'at':int(time.time())}); atomic(state,state_data)
   try: written=edl_call(cmd,WRITE_TIMEOUT_SECONDS,allow_failure=True)
   except RuntimeError as e:
    state_data.update({'status':'interrupted','at':int(time.time())}); atomic(state,state_data)
    edl_refusal(str(e)+' The write state is unresolved and this phase will not be retried.'); return 2
   if written.returncode:
    state_data.update({'status':'interrupted','at':int(time.time())}); atomic(state,state_data)
    edl_refusal('write failed; state is unresolved and this phase will not be retried',written); return 2
   rb=work/'readback.bin'; rr=edl_call(['rs',str(row['start']),str(row['count']),str(rb)],RANGE_READ_TIMEOUT_SECONDS,allow_failure=True)
   if rr.returncode or not rb.is_file() or sha(rb)!=sha(slice_path):
    state_data.update({'status':'failed-write','at':int(time.time())}); atomic(state,state_data)
    edl_refusal('phase readback failed; state is unresolved',rr); return 2
   state_data.setdefault('completed',[]).append(a.phase); state_data.update({'status':'ready','phase':a.phase,'at':int(time.time())}); atomic(state,state_data)
   if all(r['name'] in state_data['completed'] for r in rows):
    final=work/'final-emmc.bin'; r=edl_call(['rf',str(final)],FULL_READ_TIMEOUT_SECONDS,allow_failure=True)
    if r.returncode or not final.is_file() or final.stat().st_size!=m['size'] or sha(final)!=m['sha256']:
     state_data.update({'status':'failed-verify','phase':'final','at':int(time.time())}); atomic(state,state_data)
     print('VERIFY FAILED: '+edl_failure('complete full-disk readback differs. Do not boot.',r.stdout,r.stderr),file=sys.stderr); return 3
    state_data.update({'status':'verified','phase':'final','final_sha256':m['sha256'],'at':int(time.time())}); atomic(state,state_data)
    print('PASS: all planned writes and complete full-disk readback match. Boot is not verified.')
   else: print('Phase readback verified; no reset or boot recommendation was made.')
   return 0
 except (OSError,ValueError,KeyError,TypeError,RuntimeError,subprocess.TimeoutExpired) as e:
  edl_refusal(str(e)); return 2
 finally:
  if lock_fd is not None: os.close(lock_fd)
def finish_session():
 if EDL_SESSION_CALLS: print(EDL_SESSION_USED_NOTE)
def main(sysfs_root=None):
 try: return _run_main(sysfs_root)
 finally: finish_session()
if __name__=='__main__': raise SystemExit(main())
'''

_INSTALL_RUNNER = r'''#!/usr/bin/env python3
"""Standalone runner reusing bundled legacy GPT-B flash gates."""
import argparse,fcntl,hashlib,json,os,re,shutil,stat,sys,tempfile,shlex
from pathlib import Path
# __STANDALONE_SNAPSHOT__
def sha(p):
 h=hashlib.sha256()
 with open(p,'rb') as f:
  for b in iter(lambda:f.read(1048576),b''): h.update(b)
 return h.hexdigest()
def checked(root):
 try: return _snapshot_package(root,'install')
 except (OSError,ValueError,KeyError,TypeError,RuntimeError): return None
def acquire_lock(path):
 path=Path(os.path.abspath(path)); flags=os.O_RDONLY|getattr(os,'O_DIRECTORY',0)|getattr(os,'O_NOFOLLOW',0)
 directory_fd=os.open(path.anchor,flags)
 try:
  for part in path.parts[1:-1]:
   try: child=os.open(part,flags,dir_fd=directory_fd)
   except FileNotFoundError:
    os.mkdir(part,0o700,dir_fd=directory_fd); child=os.open(part,flags,dir_fd=directory_fd)
   os.close(directory_fd); directory_fd=child
  fd=os.open(path.name,os.O_CREAT|os.O_RDWR|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_NONBLOCK',0),0o600,dir_fd=directory_fd)
 finally: os.close(directory_fd)
 info=os.fstat(fd)
 if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:
  os.close(fd); raise RuntimeError('operation lock is not a private regular file')
 os.fchmod(fd,0o600)
 try: fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
 except BlockingIOError:
  os.close(fd); raise RuntimeError('another package operation is active; no write was attempted')
 return fd
def main(sysfs_root=None):
 p=argparse.ArgumentParser(description='Portable legacy GPT-B install via the bundled OpenStick flash gates. UNVALIDATED on hardware; writes require the normal typed flash confirmation.')
 p.add_argument('--edl',required=True); p.add_argument('--loader',required=True); p.add_argument('--device',required=True)
 p.add_argument('--state-dir',required=True); p.add_argument('--dry-run',action='store_true'); p.add_argument('--reconcile-state',action='store_true')
 a=p.parse_args(); root=Path(__file__).resolve().parents[1]; checked_package=checked(root)
 if not checked_package: print('REFUSED: package checksum verification failed',file=sys.stderr); return 2
 root,m=checked_package
 if a.device.lower()!=m.get('device_id','').lower(): print('REFUSED: selected device does not match package',file=sys.stderr); return 2
 loader=Path(a.loader)
 if loader.is_symlink() or not loader.is_file() or sha(loader)!=m.get('external_dependencies',{}).get('firehose_loader_sha256'):
  print('REFUSED: external Firehose loader is missing or hash mismatched',file=sys.stderr); return 2
 if (root/'payload/external/firehose-loader.sha256').read_text().strip()!=m['external_dependencies']['firehose_loader_sha256']:
  print('REFUSED: package loader dependency metadata mismatch',file=sys.stderr); return 2
 print('WARNING: packaged install scripts are UNVALIDATED on hardware.')
 backup=root/'payload/backup'; bm=json.loads((backup/'manifest.json').read_text());
 if bm.get('sha256')!=m.get('required_backup_sha256') or bm.get('device_id','').lower()!=a.device.lower() or sha(backup/'emmc-full.bin')!=bm.get('sha256'):
  print('REFUSED: matching verified full backup is unavailable or invalid',file=sys.stderr); return 2
 image=root/'payload/image-set'; im=json.loads((image/'manifest.json').read_text())
 if im.get('format')!='openstick-gpt-b-image-set' or im.get('board')!=m.get('board') or sha(image/'manifest.json')!=m.get('image_set_manifest_sha256'):
  print('REFUSED: unsupported or mismatched legacy GPT-B image-set manifest',file=sys.stderr); return 2
 runtime=root/'scripts/runtime'; sys.dont_write_bytecode=True; os.environ['PYTHONDONTWRITEBYTECODE']='1'; sys.path.insert(0,str(runtime))
 try:
  lock_fd=acquire_lock(Path(a.state_dir).expanduser()/'.openstick-package-install.lock')
  try:
   from tools.installer import store,flash
   state=store.ensure_store(Path(a.state_dir).expanduser())
   backup_path=state/'packages'/m['device_id']/'backup'; backup_path.mkdir(mode=0o700,parents=True,exist_ok=True)
   for name in ('emmc-full.bin','manifest.json','SHA256SUMS'):
    shutil.copyfile(backup/name,backup_path/name); os.chmod(backup_path/name,0o600)
   store.save_registry(state,{'schema':1,'devices':[{'id':m['device_id'],'board':m['board'],'identity':bm['identity'],
     'backups':[{'id':m['backup_id'],'path':str(backup_path),'size':bm['size'],'sha256':bm['sha256'],'verified_by':bm['verified_by']}]}]})
   with tempfile.TemporaryDirectory(prefix='ostpkg-install-') as td:
    work=Path(td); os.chmod(work,0o700); staged=work/'image-set'; shutil.copytree(image,staged)
    # Provenance was independently checked above and is carried by the outer
    # package. The current flash wrapper distinguishes legacy sets by absence of
    # manifest.json; its checksum list must match the actual staged payload.
    (staged/'manifest.json').unlink()
    sums=staged/'SHA256SUMS'
    sums.write_text(''.join(line for line in sums.read_text().splitlines(True) if not line.endswith('  manifest.json\n')))
    (staged/'loader').mkdir(mode=0o700,exist_ok=True); shutil.copyfile(loader,staged/'loader/prog_emmc_firehose_8916.mbn')
    os.chmod(staged/'loader/prog_emmc_firehose_8916.mbn',0o600)
    # Adapt the existing wrapper's documented `sudo .venv/bin/python edl/edl.py`
    # call to the explicit external client. No source checkout is consulted.
    tool=Path(a.edl).absolute()
    if not tool.is_file() or (tool.suffix!='.py' and not os.access(tool,os.X_OK)):
     print('REFUSED: live EDL client unavailable; provide an executable or Python script path',file=sys.stderr); return 2
    vpython=work/'.venv/bin/python'; vpython.parent.mkdir(mode=0o700,parents=True)
    edl_prefix=([sys.executable,str(tool)] if tool.suffix=='.py' else [str(tool)])
    quoted=shlex.join(edl_prefix)
    vpython.write_text('#!/bin/sh\n[ "${1:-}" = edl/edl.py ] && shift\nexec '+quoted+' "$@"\n'); os.chmod(vpython,0o700)
    edl_dir=work/'edl'; edl_dir.mkdir(mode=0o700)
    shutil.copyfile(loader,edl_dir/'prog_emmc_firehose_8916.mbn'); os.chmod(edl_dir/'prog_emmc_firehose_8916.mbn',0o600)
    (edl_dir/'edl.py').write_text('# external EDL adapter is the adjacent .venv/bin/python shim\n')
    bindir=work/'bin'; bindir.mkdir(mode=0o700)
    edlshim=edl_dir/'edl'; edlshim.write_text('#!/bin/sh\nexec '+quoted+' "$@"\n'); os.chmod(edlshim,0o700)
    os.environ['PATH']=str(bindir)+os.pathsep+str(tool.parent)+os.pathsep+os.environ.get('PATH','/usr/bin:/bin')
    os.environ['OPENSTICK_ROOT']=str(runtime)
    os.environ['OPENSTICK_EDL_DIR']=str(edl_dir)
    os.chdir(work)
    args=type('Args',(),{'flash_device':m['device_id'],'image_set':str(staged),'board':m['board'],
      'i_accept_untested':False,'dry_run':a.dry_run,'reconcile_state':a.reconcile_state})()
    session=flash.edl_runner.Session(sysfs_root=sysfs_root)
    try: return flash.flash_command(args,state,runtime,usb_sysfs_root=sysfs_root,session=session)
    finally:
     if session.invocation_count: print(flash.edl_runner.EDL_SESSION_USED_NOTE)
  finally:
   os.close(lock_fd)
 except Exception as exc:
  print('REFUSED: '+str(exc),file=sys.stderr); return 2
if __name__=='__main__': raise SystemExit(main())
'''


def _build_archive(destination, package_type, metadata, staging):
    (staging / "README.txt").write_text("SENSITIVE OpenStick package. Keep private and retain an offline copy.\nPackage contents are data and scripts; verify before use. Verification never executes content.\nWARNING: Packaged install and restore scripts are UNVALIDATED on hardware. Destructive writes require the normal typed flash or restore confirmation.\n", encoding="utf-8")
    files = {}
    for path in sorted(staging.rglob("*")):
        rel = path.relative_to(staging).as_posix()
        if path.is_file() and rel not in ("manifest.json", "SHA256SUMS"):
            files[rel] = {"sha256": _sha(path), "size": path.stat().st_size}
    manifest = {"schema_version": SCHEMA, "format": "ostpkg", "package_type": package_type,
                "sensitive": True, "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "files": files, **metadata}
    (staging / "manifest.json").write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    (staging / "SHA256SUMS").write_text("".join("%s  %s\n" % (files[name]["sha256"], name) for name in sorted(files)), encoding="ascii")
    os.chmod(staging / "manifest.json", 0o600); os.chmod(staging / "SHA256SUMS", 0o600); os.chmod(staging / "README.txt", 0o600)
    destination = Path(destination).absolute()
    if not str(destination).endswith(".ostpkg.tar"):
        raise store.RefusedError("package output must end in .ostpkg.tar")
    store.validate_location(destination)
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(destination.parent, 0o700)
    temp = destination.with_name("." + destination.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with tarfile.open(temp, "w", format=tarfile.PAX_FORMAT) as archive:
            for path in sorted(staging.rglob("*")):
                rel = path.relative_to(staging).as_posix()
                info = archive.gettarinfo(str(path), arcname=rel)
                info.uid = info.gid = 0; info.uname = info.gname = ""; info.mtime = 0
                info.mode = 0o700 if rel.startswith("scripts/") else (0o600 if info.isfile() else 0o700)
                with path.open("rb") if path.is_file() else _NullContext() as source:
                    archive.addfile(info, source)
        os.chmod(temp, 0o600)
        verify_package(temp)
        # Hard-link publication is atomic and fails with EEXIST. os.replace
        # would overwrite a destination created in the preceding check/use gap.
        try:
            os.link(temp, destination, follow_symlinks=False)
        except FileExistsError as exc:
            raise store.RefusedError("package destination already exists") from exc
        os.unlink(temp)
        dir_fd = os.open(destination.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try: os.fsync(dir_fd)
        finally: os.close(dir_fd)
        return manifest
    except Exception:
        try: temp.unlink()
        except OSError: pass
        raise


class _NullContext:
    def __enter__(self): return None
    def __exit__(self, *_args): return False


def _create_restore(home, device, backup, output):
    source, manifest = _verified_backup(home, device, backup)
    transition_result = _restore_transition(device, manifest)
    staging = Path(tempfile.mkdtemp(prefix=".ostpkg-restore-", dir=str(Path(home) / "packages")))
    try:
        payload = staging / "payload"
        _copy_file(source / "emmc-full.bin", payload / "emmc-full.bin")
        _copy_file(source / "manifest.json", payload / "manifest.json")
        _copy_file(source / "SHA256SUMS", payload / "SHA256SUMS")
        # Both GPTs are already byte-complete in the full disk dump. Expose exact
        # original slices only when the checked GPT manifest has unambiguous LBA
        # bounds; these are conveniences, never substitutes for the full image.
        with (source / "emmc-full.bin").open("rb") as disk:
            for label, start, count in (("gpt-primary.bin", 0, 34), ("gpt-secondary.bin", manifest["sectors"] - 33, 33)):
                target = payload / label
                disk.seek(start * 512)
                remaining = count * 512
                with target.open("xb") as out:
                    while remaining:
                        block = disk.read(min(1024 * 1024, remaining))
                        if not block: raise store.RefusedError("backup is truncated while extracting GPT evidence")
                        out.write(block); remaining -= len(block)
                    out.flush(); os.fsync(out.fileno())
                os.chmod(target, 0o600)
        # Preserve and execute the existing read-only planner as the source of
        # ordered restore ranges. Unsupported layouts are refused here.
        csv_path = payload / "partitions.csv"
        with csv_path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=("name", "start_lba", "end_lba", "offset_bytes", "size_bytes"))
            writer.writeheader()
            for name, row in sorted(manifest["partitions"].items(), key=lambda item: item[1]["start_lba"]):
                start, end = int(row["start_lba"]), int(row["end_lba"])
                writer.writerow({"name": name, "start_lba": start, "end_lba": end,
                                 "offset_bytes": start * 512, "size_bytes": (end - start + 1) * 512})
        os.chmod(csv_path, 0o600)
        planner = Path(__file__).resolve().parents[2] / "tools" / "rollback-plan.py"
        planner_copy = payload / "rollback-plan.py"
        _copy_file(planner, planner_copy)
        os.chmod(planner_copy, 0o500)
        plan_work = staging / "plan-work"
        plan_work.mkdir(mode=0o700)
        plan_command = [sys.executable, "-I", str(planner), "--dump", str(source / "emmc-full.bin"),
                        "--csv", str(csv_path), "--primary", str(payload / "gpt-primary.bin"),
                        "--secondary", str(payload / "gpt-secondary.bin"), "--emmc", str(manifest["sectors"]),
                        "--dump-sha", manifest["sha256"], "--work", str(plan_work),
                        "--loader", "<external-pinned-loader>", "--edl", "<external-edl-client>"]
        planned = subprocess.run(plan_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=300)
        if planned.returncode:
            raise store.RefusedError("existing rollback planner refuses this backup geometry: " + planned.stderr[-800:])
        plan_file = payload / "rollback-plan.txt"
        rendered_plan = planned.stdout
        for original, label in ((str(source / "emmc-full.bin"), "$PACKAGE/payload/emmc-full.bin"),
                                (str(csv_path), "$PACKAGE/payload/partitions.csv"),
                                (str(payload / "gpt-primary.bin"), "$PACKAGE/payload/gpt-primary.bin"),
                                (str(payload / "gpt-secondary.bin"), "$PACKAGE/payload/gpt-secondary.bin"),
                                (str(plan_work), "$WORK")):
            rendered_plan = rendered_plan.replace(original, label)
        plan_file.write_text(rendered_plan, encoding="utf-8")
        os.chmod(plan_file, 0o600)
        shutil.rmtree(plan_work)
        script = staging / "scripts" / "restore.py"
        _write_script(script, _RESTORE_RUNNER.replace("# __STANDALONE_SNAPSHOT__", _STANDALONE_SNAPSHOT))
        meta = {"device_id": device["id"], "backup_id": manifest["id"], "backup_sha256": manifest["sha256"],
                "full_disk_bytes": manifest["size"], "board": device.get("board"),
                "guarantee": "byte-complete full backup; restore ranges are derived from bundled rollback-plan.py"}
        if transition_result:
            transition, files = transition_result
            transition.update({"device_id": device["id"], "backup_sha256": manifest["sha256"]})
            transition_root = payload / "transition"
            transition_root.mkdir(mode=0o700)
            _copy_file(files["gpt/gpt-T-primary.bin"], transition_root / "gpt-T-primary.bin")
            _copy_file(files["gpt/gpt-T-secondary.bin"], transition_root / "gpt-T-secondary.bin")
            _copy_file(files["_source_manifest"], transition_root / "source-manifest.json")
            _copy_file(files["_patha_sums"], transition_root / "pathA-SHA256SUMS")
            transition_path = transition_root / "manifest.json"
            transition_path.write_text(json.dumps(transition, sort_keys=True, indent=2) + "\n", encoding="utf-8")
            os.chmod(transition_path, 0o600)
            meta["transition_state"] = transition
            meta["transition_manifest_sha256"] = _sha(transition_path)
            meta["guarantee"] += "; verified legacy GPT-B Path A transition fingerprints included"
        result = _build_archive(output, "restore", meta, staging)
        return result
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _create_install(home, device, image_set, backup, output):
    backup_path, backup_manifest = _verified_backup(home, device, backup)
    if not image_set:
        raise store.RefusedError("MISSING_INPUTS: install package needs a legacy GPT-B first-install image set")
    root = Path(image_set).absolute()
    if root.is_symlink() or store._has_symlink_component(root) or store._inside_git(root):
        raise store.RefusedError("image-set path is unsafe or inside a Git worktree")
    # flash-board first-install accepts Android sparse boot.bin/rootfs.bin for
    # legacy GPT-B. Current M4 boot.img + userdata rootfs.img is a different GPT-T set.
    required = (root / ("files-" + str(device.get("board")))).resolve()
    if (root / "build.json").is_file() or (root / "boot.img").is_file() or (root / "rootfs.img").is_file():
        raise store.RefusedError("M4 boot.img/rootfs.img output is GPT-T and incompatible with first-install's legacy GPT-B sparse boot.bin/rootfs.bin consumer")
    files = required
    if files.parent != root.resolve() or files.is_symlink() or not files.is_dir():
        raise store.RefusedError("image set lacks the exact files-<board> first-install layout")
    for name in ("boot.bin", "rootfs.bin", "SHA256SUMS"):
        if not (files / name).is_file() or (files / name).is_symlink():
            raise store.RefusedError("MISSING_INPUTS: first-install image set lacks " + name)
    from . import flash as flash_module
    checked = flash_module._checked_sum_file(root, require_all=True)
    board = str(device.get("board", ""))
    needed = {"files-" + board + "/boot.bin", "files-" + board + "/rootfs.bin",
              "files-" + board + "/SHA256SUMS", "fw/layout-" + board + ".env",
              "loader/prog_emmc_firehose_8916.mbn"}
    if not needed.issubset(checked):
        raise store.RefusedError("MISSING_INPUTS: complete legacy GPT-B images, layout and pinned loader are required")
    loader = root / "loader" / "prog_emmc_firehose_8916.mbn"
    if loader.is_symlink() or not loader.is_file() or _sha(loader) != flash_module.common.LOADER_SHA256:
        raise store.RefusedError("MISSING_INPUTS: the pinned Firehose loader is absent or mismatched")
    manifest_path = root / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise store.RefusedError("MISSING_INPUTS: legacy GPT-B image-set provenance manifest is absent")
    try:
        image_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise store.RefusedError("MISSING_INPUTS: image-set manifest is invalid") from exc
    if (image_manifest.get("format") != "openstick-gpt-b-image-set" or
            image_manifest.get("board") != board or image_manifest.get("profile") not in ("dev", "prod") or
            not isinstance(image_manifest.get("build_id"), str) or not image_manifest.get("build_id") or
                    image_manifest.get("redistribution_authorized") is not True or
                    image_manifest.get("loader_sha256") != _sha(loader)):
        raise store.RefusedError("MISSING_INPUTS: board/profile/build provenance and firmware redistribution authorization are required")
    if "manifest.json" not in checked:
        raise store.RefusedError("MISSING_INPUTS: image-set manifest must be covered by SHA256SUMS")
    patha = root / "pathA"
    patha_checked = flash_module._checked_sum_file(patha)
    for group in ("fw", "gpt", "rollback"):
        folder = patha / group
        members = {item.relative_to(patha).as_posix() for item in folder.rglob("*") if item.is_file() and not item.is_symlink()} if folder.is_dir() and not folder.is_symlink() else set()
        if not members or not members.issubset(patha_checked):
            raise store.RefusedError("MISSING_INPUTS: complete hashed Path A " + group + " assets are required")

    staging = Path(tempfile.mkdtemp(prefix=".ostpkg-install-", dir=str(Path(home) / "packages")))
    try:
        payload = staging / "payload"
        backup_dest = payload / "backup"
        for name in ("emmc-full.bin", "manifest.json", "SHA256SUMS"):
            _copy_file(backup_path / name, backup_dest / name)
        image_dest = payload / "image-set"
        for item in sorted(root.rglob("*")):
            if item.is_symlink():
                raise store.RefusedError("image set contains an unsafe symlink")
            if item.is_file():
                rel = item.relative_to(root)
                # Vendor loader is not redistributed. Its exact pinned hash stays
                # in the image manifest/checksum list and is verified at use.
                if rel.as_posix() == "loader/prog_emmc_firehose_8916.mbn":
                    continue
                _copy_file(item, image_dest / rel)
        external_hash = payload / "external" / "firehose-loader.sha256"
        external_hash.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        external_hash.write_text(flash_module.common.LOADER_SHA256 + "\n", encoding="ascii")
        os.chmod(external_hash, 0o600)
        runtime = staging / "scripts" / "runtime"
        repo_root = Path(__file__).resolve().parents[2]
        for item in sorted((repo_root / "tools/installer").glob("*.py")):
            _copy_file(item, runtime / "tools/installer" / item.name)
        bundled_cli = runtime / "tools/installer/cli.py"
        cli_text = bundled_cli.read_text(encoding="utf-8").replace(
            "959439aa5864685999b713c3ed12ad5fa408149648b670a9a9ef77bcc9dcab14",
            flash_module.common.LOADER_SHA256)
        bundled_cli.write_text(cli_text, encoding="utf-8")
        os.chmod(bundled_cli, 0o500)
        bundled_edl_runner = runtime / "tools/installer/edl_runner.py"
        runner_text = bundled_edl_runner.read_text(encoding="utf-8").replace(
            "959439aa5864685999b713c3ed12ad5fa408149648b670a9a9ef77bcc9dcab14",
            flash_module.common.LOADER_SHA256)
        bundled_edl_runner.write_text(runner_text, encoding="utf-8")
        os.chmod(bundled_edl_runner, 0o500)
        for rel in ("tools/flash/flash-board.sh", "tools/pathA/pathA-flash.sh", "tools/pathA/fwlib.sh",
                    "tools/pathA/pathA-rollback.sh", "scripts/common.sh", "config/defaults.env",
                    "boards/" + board + "/board.env"):
            _copy_file(repo_root / rel, runtime / rel)
        fwlib = runtime / "tools/pathA/fwlib.sh"
        fwtext = fwlib.read_text(encoding="utf-8")
        fwtext = fwtext.replace("959439aa5864685999b713c3ed12ad5fa408149648b670a9a9ef77bcc9dcab14",
                                flash_module.common.LOADER_SHA256)
        fwlib.write_text(fwtext, encoding="utf-8"); os.chmod(fwlib, 0o500)
        _write_script(staging / "scripts/install.py", _INSTALL_RUNNER.replace("# __STANDALONE_SNAPSHOT__", _STANDALONE_SNAPSHOT))
        metadata = {"device_id": device["id"], "backup_id": backup_manifest["id"],
                    "required_backup_sha256": backup_manifest["sha256"], "board": board,
                    "full_disk_bytes": backup_manifest["size"],
                    "build_id": image_manifest["build_id"], "profile": image_manifest["profile"],
                    "image_set_manifest_sha256": _sha(manifest_path),
                    "external_dependencies": {"firehose_loader_sha256": flash_module.common.LOADER_SHA256,
                                               "edl_client": "explicit executable path supplied at install time",
                                               "sudo": "not used by the installer EDL client; device access follows installed udev permissions", "fastboot": "existing helper prerequisite",
                                               "simg2img": "existing first-install prerequisite", "python3": "3.9+ standard library", "bash": "Path A"},
                    "firmware_provenance": "source image-set attests redistribution authorization"}
        _build_archive(output, "install", metadata, staging)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def create_package(home, device_id, package_type, output, backup_id=None, image_set=None):
    """Programmatic wizard seam; applies the exact same creation gates as CLI."""
    class Args:
        pass
    args = Args()
    args.package_type = package_type
    args.package_device = device_id
    args.package_backup = backup_id
    args.package_image_set = image_set
    args.package_output = output
    return package_command(args, home, Path(__file__).resolve().parents[2])


def package_command(args, home, repo):
    """Create a portable package; destination is never overwritten."""
    action = getattr(args, "package_type", None)
    if action not in ("restore", "install"):
        raise store.RefusedError("package type must be install or restore")
    registry = store.load_registry(home)
    device = next((d for d in registry.get("devices", []) if str(d.get("id", "")).lower() == str(getattr(args, "package_device", "")).lower()), None)
    if not device:
        raise store.RefusedError("package device is not registered")
    output = getattr(args, "package_output", None)
    if not output:
        raise store.RefusedError("package output path is required")
    if action == "restore":
        _create_restore(home, device, getattr(args, "package_backup", None), output)
    else:
        _create_install(home, device, getattr(args, "package_image_set", None), getattr(args, "package_backup", None), output)
    print("WARNING: packaged install/restore write scripts are UNVALIDATED on hardware; writes require typed confirmation.")
    print("Created SENSITIVE %s package: %s" % (action, output))
    return 0
