#!/usr/bin/env python3
"""Synthetic M6 package integrity and fail-closed compatibility checks."""
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tarfile
import tempfile
import struct
import uuid
import zlib
import re
import pty
import select
import time
import fcntl
from unittest import mock
from contextlib import redirect_stdout
import io
from functools import wraps

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.installer import packages, store
from tools.installer import build as build_module


def verify_build_safety(root):
    home = store.ensure_store(root / "build-safety-home")
    repo = root / "build-safety-repo"
    (repo / "boards/ufi103s").mkdir(parents=True)
    (repo / "boards/ufi103s/board.env").write_text("BOARD_DESC=synthetic\n")
    (repo / "scripts").mkdir()
    for name in ("20-rootfs.sh", "50-boot-img.sh", "51-rootfs-img.sh"):
        (repo / "scripts" / name).write_text("#!/bin/sh\nexit 0\n")
    (repo / "out").mkdir()
    (repo / "out/base-bookworm-arm64.tar.gz").write_bytes(b"synthetic base")
    args = type("Args", (), {"profile": "prod", "build_board": "ufi103s",
                              "bake_user_env": False, "dry_run": False})()
    captured = []

    def fake_run(command, env=None, **kwargs):
        if command[0] == "git":
            return type("Result", (), {"returncode": 128, "stdout": ""})()
        captured.append((list(command), dict(env or {})))
        if "50-boot-img.sh" in command[-1]:
            out = pathlib.Path(env["PROD_OUT"]); out.mkdir(parents=True, exist_ok=True)
            (out / "boot.img").write_bytes(b"synthetic boot")
        if "51-rootfs-img.sh" in command[-1]:
            out = pathlib.Path(env["PRODROOT_OUT"]); out.mkdir(parents=True, exist_ok=True)
            (out / "rootfs.img").write_bytes(b"synthetic rootfs")
        return type("Result", (), {"returncode": 0})()

    with mock.patch.object(build_module.os, "geteuid", return_value=1000), \
         mock.patch.object(build_module.os, "getuid", return_value=1000), \
         mock.patch.object(build_module.os, "getgid", return_value=1000), \
         mock.patch.object(build_module, "_version", return_value="synthetic"), \
         mock.patch.object(build_module.subprocess, "run", side_effect=fake_run):
        assert build_module.build_command(args, home, repo) == 0
    privileged, env = captured[0]
    handoff = '"/bin/sh" "$1"; rc=$?; chown -R "$2:$3" "$4" "$5"; owner_rc=$?; [ "$rc" -ne 0 ] && exit "$rc"; exit "$owner_rc"'
    expected = ["sudo", "env", "-i"] + [key + "=" + value for key, value in sorted(env.items())] + [
        "/bin/sh", "-c", handoff, "openstick-build", str(repo / "scripts/20-rootfs.sh"),
        "1000", "1000", env["OUT"], env["WORK"]]
    assert privileged == expected, (privileged[:3], expected[:3])

    root_args = type("Args", (), {"profile": "prod", "build_board": "ufi103s",
                                   "bake_user_env": False, "dry_run": False})()
    with mock.patch.object(build_module.os, "geteuid", return_value=0), \
         mock.patch.object(build_module.subprocess, "run", side_effect=AssertionError("root refusal ran a command")):
        try:
            build_module.build_command(root_args, home, repo)
        except store.RefusedError as exc:
            assert "non-root" in str(exc)
        else:
            raise AssertionError("build as root was accepted")

def test_build_dry_run_default_login(root):
    home = store.ensure_store(root / "dry-run-home")
    repo = root / "build-safety-repo"
    args = type("Args", (), {"profile": "dev", "build_board": "ufi103s",
                              "bake_user_env": False, "dry_run": True})()
    output = io.StringIO()
    with redirect_stdout(output):
        assert build_module.build_command(args, home, repo) == 0
    assert "DEFAULT_USER=openstick" in output.getvalue() and "DEFAULT_PASS=openstick" in output.getvalue()


def test_common_rejects_empty_openstick_out():
    for mode in ("legacy", "generic"):
        env = dict(os.environ, OPENSTICK_ROOT=str(ROOT), OPENSTICK_CONFIG_MODE=mode,
                   OPENSTICK_OUT="", PATH="/usr/bin:/bin")
        result = subprocess.run(["/bin/sh", "-c", '. "$OPENSTICK_ROOT/scripts/common.sh"'],
                                env=env, capture_output=True, text=True)
        assert result.returncode != 0 and "OPENSTICK_OUT" in result.stderr, (mode, result.returncode, result.stderr)
    generic_missing = dict(os.environ, OPENSTICK_ROOT=str(ROOT), OPENSTICK_CONFIG_MODE="generic",
                           PATH="/usr/bin:/bin")
    generic_missing.pop("OPENSTICK_OUT", None)
    result = subprocess.run(["/bin/sh", "-c", '. "$OPENSTICK_ROOT/scripts/common.sh"'],
                            env=generic_missing, capture_output=True, text=True)
    assert result.returncode != 0 and "OPENSTICK_OUT" in result.stderr, (result.returncode, result.stderr)


def make_backup(home):
    device_id = "deadbeef"
    backup_id = "synthetic-backup"
    backup = home / "backups" / device_id / backup_id
    backup.mkdir(parents=True, mode=0o700)
    # Small explicitly synthetic disk fixture; the package code verifies the
    # full image hash and metadata without treating this as real board evidence.
    source = home.parent / "synthetic-emmc.img"
    names = ("userdata", "cache", "persist", "boot", "modemst1", "modemst2", "sbl1", "rpm", "tz", "tzbak", "hyp", "aboot",
             "modem", "system", "recovery", "sbl1bak", "abootbak", "rpmbak", "hypbak", "pad", "misc", "fsc", "ssd",
             "splash", "DDR", "fsg", "sec")
    sectors = 4096
    with source.open("wb") as image:
        image.write(bytes(sectors * 512))
    entries = bytearray(128 * 128)
    type_guid = uuid.UUID("0fc63daf-8483-4772-8e79-3d69d8477de4")
    starts = {}
    for index, name in enumerate(names):
        first = 34 + index * 2; last = first + 1; starts[name] = (first, last)
        unique = uuid.uuid5(uuid.NAMESPACE_DNS, "openstick-m6-synthetic-" + name)
        entry = type_guid.bytes_le + unique.bytes_le + struct.pack("<QQQ", first, last, 0)
        entry += name.encode("utf-16le").ljust(72, b"\0")
        entries[index * 128:(index + 1) * 128] = entry
    ecrc = zlib.crc32(entries) & 0xffffffff
    disk_guid = uuid.UUID("12345678-1234-5678-9abc-def012345678")
    def header(current, backup_lba, entries_lba):
        raw = bytearray(512)
        struct.pack_into("<8sIIIIQQQQ16sQIII", raw, 0, b"EFI PART", 0x10000, 92, 0, 0,
                         current, backup_lba, 34, sectors - 34, disk_guid.bytes_le,
                         entries_lba, 128, 128, ecrc)
        struct.pack_into("<I", raw, 16, zlib.crc32(raw[:92]) & 0xffffffff)
        return raw
    with source.open("r+b") as image:
        image.seek(512); image.write(header(1, sectors - 1, 2))
        image.seek(1024); image.write(entries)
        image.seek((sectors - 33) * 512); image.write(entries)
        image.seek((sectors - 1) * 512); image.write(header(sectors - 1, 1, sectors - 33))
    data = source.read_bytes()
    (backup / "emmc-full.bin").write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    parts = __import__("tools.installer.cli", fromlist=["_parse_gpt"])._parse_gpt(backup / "emmc-full.bin")
    identity = {name: __import__("tools.installer.cli", fromlist=["_hash_range"])._hash_range(backup / "emmc-full.bin", *parts[name]) for name in ("fsg", "sec", "fsc")}
    manifest = {"schema_version": 1, "id": backup_id, "device_id": device_id, "size": len(data),
                "sectors": len(data) // 512, "sha256": digest, "identity": identity,
                "partitions": {name: {"start_lba": start, "end_lba": end} for name, (start, end) in parts.items()},
                "verified_by": ["full-read-size", "gpt-primary-and-backup-crc", "sha256"]}
    (backup / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (backup / "SHA256SUMS").write_text(digest + "  emmc-full.bin\n", encoding="ascii")
    device = {"id": device_id, "board": "ufi103s", "identity": identity,
              "backups": [{"id": backup_id, "path": str(backup), "size": len(data), "sha256": digest,
                           "verified_by": manifest["verified_by"]}]}
    store.save_registry(home, {"schema": 1, "devices": [device]})
    return device


def make_raw_package(path, payload=b"safe"):
    with tempfile.TemporaryDirectory() as td:
        stage = pathlib.Path(td)
        (stage / "payload").mkdir()
        (stage / "scripts").mkdir()
        (stage / "payload/emmc-full.bin").write_bytes(payload)
        (stage / "payload/manifest.json").write_text("{}")
        (stage / "payload/gpt-primary.bin").write_bytes(b"p")
        (stage / "payload/gpt-secondary.bin").write_bytes(b"s")
        (stage / "scripts/restore.py").write_text("# synthetic runner\n")
        files = {}
        for item in sorted(stage.rglob("*")):
            if item.is_file():
                rel = item.relative_to(stage).as_posix()
                raw = item.read_bytes()
                files[rel] = {"sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw)}
        manifest = {"schema_version": 1, "format": "ostpkg", "package_type": "restore",
                    "sensitive": True, "files": files, "device_id": "deadbeef"}
        (stage / "manifest.json").write_text(json.dumps(manifest, sort_keys=True))
        (stage / "SHA256SUMS").write_text("".join(files[n]["sha256"] + "  " + n + "\n" for n in sorted(files)))
        (stage / "README.txt").write_text("Synthetic SENSITIVE fixture\n")
        with tarfile.open(path, "w") as archive:
            for item in sorted(stage.rglob("*")):
                archive.add(item, arcname=item.relative_to(stage).as_posix(), recursive=False)


def run_typed(command, env, token):
    command, env = prepare_restore_state(command, env)
    master, slave = pty.openpty()
    process = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave, env=env, close_fds=True)
    os.close(slave)
    output = bytearray(); sent = False; deadline = time.time() + 30
    while process.poll() is None and time.time() < deadline:
        ready, _, _ = select.select([master], [], [], .2)
        if ready:
            try: block = os.read(master, 4096)
            except OSError: break
            output.extend(block)
            if not sent and token.encode() in output:
                os.write(master, token.encode() + b"\n"); sent = True
    if process.poll() is None: process.kill()
    rc = process.wait(); os.close(master)
    return rc, output.decode(errors="replace")


def prepare_restore_state(command, env):
    command = list(command); env = dict(env)
    if "--state" not in command:
        return command, env
    index = command.index("--state")
    requested = pathlib.Path(command[index + 1])
    home = requested.parent / (requested.stem + "-home")
    env["HOME"] = str(home)
    backup_sha = env["OPENSTICK_TEST_BACKUP_SHA"]
    canonical = canonical_restore_state(requested, backup_sha)
    command[index + 1] = str(canonical)
    return command, env


def canonical_restore_state(requested, backup_sha):
    requested = pathlib.Path(requested)
    home = requested.parent / (requested.stem + "-home")
    return home / ".local/state/openstick" / ("restore-deadbeef-" + backup_sha + ".json")


def run_restore(command, env, **kwargs):
    command, env = prepare_restore_state(command, env)
    return subprocess.run(command, env=env, **kwargs)


def run_flash_session(command, env):
    master, slave = pty.openpty()
    process = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave, env=env, close_fds=True)
    os.close(slave)
    output = bytearray(); sent = set(); deadline = time.time() + 180
    pattern = re.compile(rb"Type exactly: (flash [0-9a-f]{8} [a-z0-9-]+)")
    while process.poll() is None and time.time() < deadline:
        ready, _, _ = select.select([master], [], [], .2)
        if ready:
            try: block = os.read(master, 65536)
            except OSError: break
            output.extend(block)
            for match in pattern.findall(output):
                if match not in sent:
                    os.write(master, match + b"\n"); sent.add(match)
    if process.poll() is None: process.kill()
    rc = process.wait(); os.close(master)
    return rc, output.decode(errors="replace"), {item.decode() for item in sent}


def make_fake_edl(path):
    path.write_text('''#!/usr/bin/env python3
import os, shutil, signal, sys, time
from pathlib import Path
a=sys.argv[1:]; op=a[0]; disk=Path(os.environ['OPENSTICK_FAKE_EMMC'])
with Path(os.environ['OPENSTICK_EDL_LOG']).open('a') as log: log.write(' '.join(a)+'\\n')
loader=next((x.split('=',1)[1] for x in a if x.startswith('--loader=')), '')
memory=next((x.split('=',1)[1] for x in a if x.startswith('--memory=')), '')
if not loader.startswith('/') or memory!='emmc': print('FAKE EDL ASSERTION FAILED: required loader and memory flags missing',file=sys.stderr); raise SystemExit(91)
print('Qualcomm Sahara / Firehose Client V3.62 (c) B.Kerler 2018-2025.')
print('HWID:              0x007050e100000000 (MSM_ID:0x007050e1,OEM_ID:0x0000,MODEL_ID:0x0000)')
print('PK_HASH:           0x0000000000000000000000000000000000000000000000000000000000000000')
print('Serial:            0xdeadbeef')
if op=='getstorageinfo': print("GetStorageInfo command isn't supported."); raise SystemExit(0)
if op=='rf':
 out=Path(next(x for x in a[1:] if not x.startswith('--'))); shutil.copyfile(disk,out)
 if os.environ.get('CORRUPT_RF')=='1':
  with out.open('r+b') as f: f.seek(200*512); f.write(b'CORRUPTED-READBACK')
 raise SystemExit(0)
if op=='ws':
 lba=int(a[1]); src=Path(a[2])
 if '--skipwrite' in a: raise SystemExit(0)
 if os.environ.get('FAIL_WRITE_LBA')==str(lba):
  print('SYNTHETIC EDL WRITE FAILURE DETAIL',file=sys.stderr); raise SystemExit(91)
 if os.environ.get('PARTIAL_WRITE_LBA')==str(lba):
  data=src.read_bytes()
  with disk.open('r+b') as f: f.seek(lba*512); f.write(data[:max(1,len(data)//2)])
  raise SystemExit(92)
 with disk.open('r+b') as f: f.seek(lba*512); f.write(src.read_bytes())
 raise SystemExit(0)
if op=='rs':
 lba,count=int(a[1]),int(a[2]); out=Path(next(x for x in a[3:] if not x.startswith('--')))
 if lba==0 and os.environ.get('HANG_IDENTITY'):
  def interrupted(signum,frame):
   print('SYNTHETIC EDL SIGINT OUTPUT TAIL',flush=True); raise SystemExit(130)
  signal.signal(signal.SIGINT,interrupted)
  while True: time.sleep(1)
 if lba==0 and count==34 and os.environ.get('RACE_PACKAGE_ROOT'):
  (Path(os.environ['RACE_PACKAGE_ROOT'])/'payload/emmc-full.bin').write_bytes(b'changed-after-validation')
 with disk.open('rb') as f: f.seek(lba*512); data=f.read(count*512)
 out.write_bytes(data); raise SystemExit(0)
raise SystemExit(2)
''', encoding="utf-8")
    os.chmod(path, 0o700)


def make_legacy_image_set(root, loader, loader_hash, backup_path, live_path, sectors_override=None):
    board = "ufi103s"
    files = root / ("files-" + board); files.mkdir(parents=True)
    (files / "boot.bin").write_bytes(b"B" * 512)
    (files / "rootfs.bin").write_bytes(b"R" * 512)
    nested = [p for p in files.iterdir() if p.is_file()]
    (files / "SHA256SUMS").write_text("".join("%s  %s\n" % (hashlib.sha256(p.read_bytes()).hexdigest(), p.name) for p in sorted(nested)))
    fw = root / "pathA/fw"; gpt = root / "pathA/gpt"; rollback = root / "pathA/rollback"
    fw.mkdir(parents=True); gpt.mkdir(parents=True); rollback.mkdir(parents=True)
    backup_manifest = json.loads((backup_path / "manifest.json").read_text())
    sectors = sectors_override or 7634944
    backup = backup_path / "emmc-full.bin"
    entry_array = backup.read_bytes()[2 * 512:34 * 512]
    entries_crc = zlib.crc32(entry_array) & 0xffffffff
    disk_guid = uuid.UUID("12345678-1234-5678-9abc-def012345678")
    mbr = backup.read_bytes()[:512]
    def gpt_pair(guid):
        def header(current, alternate, entries_lba):
            raw = bytearray(512)
            struct.pack_into("<8sIIIIQQQQ16sQIII", raw, 0, b"EFI PART", 0x10000, 92, 0, 0,
                             current, alternate, 34, sectors - 34, guid.bytes_le,
                             entries_lba, 128, 128, entries_crc)
            struct.pack_into("<I", raw, 16, zlib.crc32(raw[:92]) & 0xffffffff)
            return bytes(raw)
        primary = mbr + header(1, sectors - 1, 2) + entry_array
        secondary = entry_array + header(sectors - 1, 1, sectors - 33)
        return primary, secondary
    original_primary, original_secondary = gpt_pair(disk_guid)
    new_primary, new_secondary = gpt_pair(uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"))
    live_path.parent.mkdir(parents=True, exist_ok=True)
    with live_path.open("wb") as live:
        live.truncate(sectors * 512)
    with live_path.open("r+b") as live:
        live.seek(0); live.write(original_primary)
        live.seek((sectors - 33) * 512); live.write(original_secondary)
        with backup.open("rb") as src:
            for name in ("fsg", "sec", "fsc"):
                part = backup_manifest["partitions"][name]
                start = part["start_lba"] * 512; size = (part["end_lba"] - part["start_lba"] + 1) * 512
                src.seek(start); live.seek(start); live.write(src.read(size))
    (rollback / "gpt-orig-primary.bin").write_bytes(original_primary)
    (rollback / "gpt-orig-secondary.bin").write_bytes(original_secondary)
    (gpt / "gpt-T-primary.bin").write_bytes(new_primary)
    (gpt / "gpt-T-secondary.bin").write_bytes(new_secondary)
    if sectors_override:
        fw_specs = {name: (backup_manifest["partitions"][name]["start_lba"],
                           backup_manifest["partitions"][name]["end_lba"] - backup_manifest["partitions"][name]["start_lba"] + 1)
                    for name in ("sbl1", "rpm", "tz", "hyp", "aboot")}
    else:
        fw_specs = {"sbl1": (262144, 1024), "rpm": (268288, 1024), "tz": (270336, 2048),
                    "hyp": (272384, 1024), "aboot": (264192, 2048)}
    for name, (start, count) in fw_specs.items():
        stock_name = "stock-tz-tzbak.bin" if name == "tz" else "stock-" + name + ".bin"
        if sectors_override:
            with backup.open("rb") as original:
                original.seek(start * 512); stock = original.read(count * 512)
        else:
            stock = bytes((ord(name[0]) + 1,)) * (count * 512)
        (rollback / stock_name).write_bytes(stock)
        (fw / (name + ".img")).write_bytes(bytes((ord(name[0]) + 20,)) * (count * 512))
        with live_path.open("r+b") as live:
            live.seek(start * 512); live.write(stock)
    (root / ("fw/layout-" + board + ".env")).parent.mkdir(parents=True, exist_ok=True)
    (root / ("fw/layout-" + board + ".env")).write_text("BOOT_START=500000\nROOTFS_START=600000\nROOTFS_BYTES=512\n")
    # Rebuild the primary and nested sums after all exact-size synthetic files exist.
    for name in ("gpt-T-primary.bin", "gpt-T-secondary.bin"):
        assert (gpt / name).stat().st_size == (34 if "primary" in name else 33) * 512
    pa = root / "pathA"
    rows = []
    for path in sorted(p for p in pa.rglob("*") if p.is_file()):
        rows.append("%s  %s\n" % (hashlib.sha256(path.read_bytes()).hexdigest(), path.relative_to(pa).as_posix()))
    (pa / "SHA256SUMS").write_text("".join(rows))
    loader_dir = root / "loader"; loader_dir.mkdir()
    (loader_dir / "prog_emmc_firehose_8916.mbn").write_bytes(loader.read_bytes())
    manifest = {"schema_version": 1, "format": "openstick-gpt-b-image-set", "image_set_id": "synthetic-gpt-b",
                "board": board, "profile": "prod", "build_id": "synthetic-build",
                "loader_sha256": loader_hash, "redistribution_authorized": True}
    (root / "manifest.json").write_text(json.dumps(manifest, sort_keys=True))
    sums = []
    for path in sorted(p for p in root.rglob("*") if p.is_file() and p != root / "SHA256SUMS"):
        sums.append("%s  %s\n" % (hashlib.sha256(path.read_bytes()).hexdigest(), path.relative_to(root).as_posix()))
    (root / "SHA256SUMS").write_text("".join(sums))


def make_install_tools(directory, live_path, log_path):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    edl = directory / "fake-install-edl"
    edl.write_text('''#!/usr/bin/env python3
import errno, os, shutil, sys
from pathlib import Path
disk=Path(%r); a=sys.argv[1:]; op=a[0]
with Path(%r).open('a') as f: f.write(' '.join(a)+'\\n')
loader=next((x.split('=',1)[1] for x in a if x.startswith('--loader=')), '')
memory=next((x.split('=',1)[1] for x in a if x.startswith('--memory=')), '')
if not loader.startswith('/') or memory!='emmc': print('FAKE EDL ASSERTION FAILED: required loader and memory flags missing',file=sys.stderr); raise SystemExit(91)
print('Qualcomm Sahara / Firehose Client V3.62 (c) B.Kerler 2018-2025.')
print('HWID:              0x007050e100000000 (MSM_ID:0x007050e1,OEM_ID:0x0000,MODEL_ID:0x0000)')
print('PK_HASH:           0x0000000000000000000000000000000000000000000000000000000000000000')
print('Serial:            0xdeadbeef')
if op=='getstorageinfo': print("GetStorageInfo command isn't supported."); raise SystemExit(0)
if op=='rf':
 out=Path(next(x for x in a[1:] if not x.startswith('--')))
 with disk.open('rb') as src, out.open('wb') as dst:
  dst.truncate(disk.stat().st_size); pos=0
  while True:
   try: data=os.lseek(src.fileno(),pos,os.SEEK_DATA)
   except OSError as ex:
    if ex.errno==errno.ENXIO: break
    raise
   hole=os.lseek(src.fileno(),data,os.SEEK_HOLE); src.seek(data); dst.seek(data); left=hole-data
   while left:
    block=src.read(min(1048576,left)); dst.write(block); left-=len(block)
   pos=hole
 raise SystemExit(0)
if op=='rs':
 start,count=int(a[1]),int(a[2]); out=Path(next(x for x in a[3:] if not x.startswith('--')))
 with disk.open('rb') as src, out.open('wb') as dst:
  src.seek(start*512); left=count*512
  while left:
   block=src.read(min(1048576,left))
   if not block: raise SystemExit(92)
   dst.write(block); left-=len(block)
 raise SystemExit(0)
if op=='ws':
 start=int(a[1]); source=Path(a[2])
 if '--skipwrite' in a: raise SystemExit(0)
 with disk.open('r+b') as dst, source.open('rb') as src:
  dst.seek(start*512)
  shutil.copyfileobj(src,dst,1048576)
 raise SystemExit(0)
raise SystemExit(2)
''' % (str(live_path), str(log_path)), encoding="utf-8")
    os.chmod(edl, 0o700)
    simg2img = directory / "simg2img"
    simg2img.write_text("#!/bin/sh\ncp -- \"$1\" \"$2\"\n", encoding="utf-8")
    os.chmod(simg2img, 0o700)
    sudo = directory / "sudo"
    sudo.write_text("#!/bin/sh\nexec \"$@\"\n", encoding="utf-8")
    os.chmod(sudo, 0o700)
    # Keep every flash helper lookup inside this synthetic tool directory.
    # The accepted first-install path must not invoke fastboot; if it does,
    # fail closed and leave evidence for the enclosing assertion.
    fastboot_log = directory / "fastboot.calls"
    fastboot = directory / "fastboot"
    fastboot.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"%s\"\nexit 97\n" % ("%s", str(fastboot_log)), encoding="utf-8")
    os.chmod(fastboot, 0o700)
    return edl, fastboot_log


def isolate_git_ceiling(function):
    """Make the runner independent of the caller's ceiling-directory setting."""
    @wraps(function)
    def wrapped(*args, **kwargs):
        prior = os.environ.get("GIT_CEILING_DIRECTORIES")
        ceiling = pathlib.Path(tempfile.gettempdir()).resolve()
        os.environ["GIT_CEILING_DIRECTORIES"] = str(ceiling)
        try:
            return function(*args, **kwargs)
        finally:
            if prior is None:
                os.environ.pop("GIT_CEILING_DIRECTORIES", None)
            else:
                os.environ["GIT_CEILING_DIRECTORIES"] = prior
    return wrapped


@isolate_git_ceiling
def run():
    with tempfile.TemporaryDirectory(prefix="ostpkg-test-") as td:
        root = pathlib.Path(td)
        verify_build_safety(root)
        test_build_dry_run_default_login(root)
        test_common_rejects_empty_openstick_out()
        test_verify_package_requires_sensitive_marking(root)
        home = store.ensure_store(root / "state")
        device = make_backup(home)
        output = root / "restore.ostpkg.tar"
        args = type("Args", (), {"package_type": "restore", "package_device": device["id"],
                                  "package_backup": "synthetic-backup", "package_image_set": None,
                                  "package_output": str(output)})()
        assert packages.package_command(args, home, ROOT) == 0
        manifest = packages.verify_package(output)
        assert manifest["package_type"] == "restore" and manifest["sensitive"] is True
        assert manifest["backup_sha256"] == device["backups"][0]["sha256"]
        assert (output.stat().st_mode & 0o777) == 0o600
        with tarfile.open(output) as archive:
            names = archive.getnames()
            package_readme = archive.extractfile("README.txt").read().decode()
            assert "SENSITIVE" in package_readme
            assert "UNVALIDATED on hardware" in package_readme
            assert "payload/emmc-full.bin" in names
            assert "payload/rollback-plan.py" in names and "payload/rollback-plan.txt" in names
            script = archive.extractfile("scripts/restore.py").read().decode()
            assert "PINNED_LOADER_SHA256" in script and "fsg" in script and "CONFIRM" in script
            assert "builder" not in script.lower()

        # Package publication refuses any destination within a Git worktree.
        git_tree = root / "package-git-tree"; git_tree.mkdir()
        subprocess.run(["git", "init", "-q", str(git_tree)], check=True)
        staging = root / "minimal-package-stage"; staging.mkdir()
        (staging / "payload").mkdir(); (staging / "scripts").mkdir()
        try:
            packages._build_archive(git_tree / "blocked.ostpkg.tar", "restore", {}, staging)
        except store.RefusedError as exc:
            assert "Git" in str(exc) or "git" in str(exc)
        else:
            raise AssertionError("package output inside a Git worktree was accepted")
        assert not (git_tree / "blocked.ostpkg.tar").exists()

        # Standalone script works from a clean directory with no repository on
        # PATH; its only external command is the explicit fake EDL executable.
        standalone = root / "unpacked-elsewhere"
        standalone.mkdir()
        with tarfile.open(output) as archive:
            archive.extractall(standalone)
        script_path = standalone / "scripts/restore.py"
        disk = home / "backups" / "deadbeef" / "synthetic-backup" / "emmc-full.bin"
        fake_disk = root / "fake-device.img"; fake_disk.write_bytes(disk.read_bytes())
        # Synthetic target differs in a planner-managed partition; protected
        # identity slices remain byte-identical.
        userdata = json.loads((disk.parent / "manifest.json").read_text())["partitions"]["userdata"]
        with fake_disk.open("r+b") as stream:
            stream.seek(userdata["start_lba"] * 512); stream.write(b"SYNTHETIC-TARGET" * 64)
        fake_edl = root / "fake-edl"
        make_fake_edl(fake_edl)
        loader = root / "synthetic-loader.bin"; loader.write_bytes(b"EXPLICITLY SYNTHETIC TEST LOADER")
        loader_hash = hashlib.sha256(loader.read_bytes()).hexdigest()
        base_env = dict(os.environ, PATH="/usr/bin:/bin", OPENSTICK_FAKE_EMMC=str(fake_disk), OPENSTICK_EDL_LOG=str(root / "edl.log"),
                        OPENSTICK_TEST_BACKUP_SHA=json.loads((disk.parent / "manifest.json").read_text())["sha256"])

        # Production-pinned loader check must reject arbitrary synthetic bytes.
        original_run = subprocess.run([sys.executable, str(script_path), "--device", "deadbeef", "--edl", str(fake_edl),
                                       "--loader", str(loader)], env=base_env, capture_output=True, text=True)
        assert original_run.returncode == 2 and "pinned SHA-256" in original_run.stderr, (original_run.returncode, original_run.stdout, original_run.stderr)

        # Test-only dependency injection: patch a temporary package copy with
        # this fixture loader digest, then regenerate its integrity metadata.
        runner_text = script_path.read_text().replace(packages._RESTORE_RUNNER.split('PINNED_LOADER_SHA256="')[1].split('"')[0], loader_hash)
        script_path.write_text(runner_text)
        outer_manifest_path = standalone / "manifest.json"
        outer = json.loads(outer_manifest_path.read_text())
        outer["files"]["scripts/restore.py"] = {"sha256": hashlib.sha256(script_path.read_bytes()).hexdigest(), "size": script_path.stat().st_size}
        outer_manifest_path.write_text(json.dumps(outer, sort_keys=True, indent=2) + "\n")
        sums_path = standalone / "SHA256SUMS"
        sums_path.write_text("".join("%s  %s\n" % (outer["files"][n]["sha256"], n) for n in sorted(outer["files"])))
        args = [sys.executable, str(script_path), "--device", "deadbeef", "--edl", str(fake_edl), "--loader", str(loader)]
        inspect = subprocess.run(args, env=base_env, capture_output=True, text=True)
        assert inspect.returncode == 0 and "fsg/sec/fsc fingerprints verified" in inspect.stdout, (inspect.returncode, inspect.stdout, inspect.stderr)
        # Deterministically replace a validated member from the fake EDL
        # callback, after the runner has taken its private snapshot. Continued
        # success proves all later reads use the stable snapshot, not the
        # attacker-writable extracted package directory.
        race_root = root / "race-elsewhere"
        shutil.copytree(standalone, race_root)
        race_env = dict(base_env, RACE_PACKAGE_ROOT=str(race_root))
        race_script = race_root / "scripts/restore.py"
        race_result = subprocess.run([sys.executable, str(race_script), "--device", "deadbeef", "--edl", str(fake_edl),
                                      "--loader", str(loader)], env=race_env, capture_output=True, text=True)
        assert race_result.returncode == 0 and "fingerprints verified" in race_result.stdout, (race_result.returncode, race_result.stdout, race_result.stderr)
        assert (race_root / "payload/emmc-full.bin").read_bytes() == b"changed-after-validation"
        ordered_phases = re.search(r"Ordered restore phases: (.+)", inspect.stdout).group(1).split(", ")
        # A second process sharing restore state must fail before a write.
        lock_state = root / "locked-state.json"
        locked_command, locked_env = prepare_restore_state(args + ["--phase", "userdata", "--state", str(lock_state)],
                                                            dict(base_env, COMMIT="1", CONFIRM="restore deadbeef userdata"))
        lock_state = pathlib.Path(locked_command[locked_command.index("--state") + 1])
        lock_state.parent.mkdir(parents=True, exist_ok=True)
        lock_fd = os.open(str(lock_state) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        locked = subprocess.run(locked_command, env=locked_env, capture_output=True, text=True)
        assert locked.returncode == 2 and "another package operation is active" in locked.stderr, (locked.returncode, locked.stderr)
        lock_log = pathlib.Path(base_env["OPENSTICK_EDL_LOG"])
        assert not lock_log.exists() or not any(line.startswith("ws ") and "--skipwrite" not in line
                                                for line in lock_log.read_text().splitlines())
        os.close(lock_fd)
        out_of_order_token = "restore deadbeef " + ordered_phases[1]
        out_of_order_env = dict(base_env, COMMIT="1", CONFIRM=out_of_order_token)
        rc, order_output = run_typed(args + ["--phase", ordered_phases[1], "--state", str(root / "out-of-order.json")],
                                     out_of_order_env, out_of_order_token)
        assert rc == 2 and "out of order" in order_output, order_output
        wrong_disk = root / "wrong-identity.img"; wrong_disk.write_bytes(disk.read_bytes())
        fsg = json.loads((disk.parent / "manifest.json").read_text())["partitions"]["fsg"]
        with wrong_disk.open("r+b") as stream: stream.seek(fsg["start_lba"] * 512); stream.write(b"WRONG-IDENTITY")
        wrong_identity = dict(base_env, OPENSTICK_FAKE_EMMC=str(wrong_disk), OPENSTICK_EDL_LOG=str(root / "wrong-id.log"))
        wrong = subprocess.run(args, env=wrong_identity, capture_output=True, text=True)
        assert wrong.returncode == 2 and "fsg fingerprint mismatch" in wrong.stderr
        wrong_gpt_disk = root / "wrong-gpt-device.img"; wrong_gpt_disk.write_bytes(disk.read_bytes())
        with wrong_gpt_disk.open("r+b") as stream:
            stream.seek(2 * 512); stream.write(b"UNKNOWN-GPT-STATE")
        wrong_gpt_env = dict(base_env, OPENSTICK_FAKE_EMMC=str(wrong_gpt_disk),
                             OPENSTICK_EDL_LOG=str(root / "wrong-gpt.log"))
        wrong_gpt = run_restore(args + ["--phase", "userdata", "--state", str(root / "wrong-gpt-state.json")],
                                wrong_gpt_env, capture_output=True, text=True)
        assert wrong_gpt.returncode == 2 and "GPT matches neither known target nor backup" in wrong_gpt.stderr, (wrong_gpt.returncode, wrong_gpt.stderr)
        assert not pathlib.Path(wrong_gpt_env["OPENSTICK_EDL_LOG"]).exists() or not any(
            line.startswith("ws ") and "--skipwrite" not in line
            for line in pathlib.Path(wrong_gpt_env["OPENSTICK_EDL_LOG"]).read_text().splitlines())

        # Wrong env confirmation and non-tty invocation refuse before writing.
        token = "restore deadbeef userdata"
        no_tty = dict(base_env, COMMIT="1", CONFIRM=token, OPENSTICK_RESTORE_STATE=str(root / "never-used"))
        refused = run_restore(args + ["--phase", "userdata", "--state", str(root / "state-nontty.json")], env=no_tty, capture_output=True, text=True)
        assert refused.returncode == 2 and "terminal" in refused.stderr
        wrong_confirm = dict(no_tty, CONFIRM="restore deadbeef wrong")
        refused = run_restore(args + ["--phase", "userdata", "--state", str(root / "state-wrong.json")], env=wrong_confirm, capture_output=True, text=True)
        assert refused.returncode == 2 and "CONFIRM" in refused.stderr
        unknown_state = root / "unknown-state.json"
        unknown_state = canonical_restore_state(unknown_state, base_env["OPENSTICK_TEST_BACKUP_SHA"])
        unknown_state.parent.mkdir(parents=True, exist_ok=True)
        unknown_state.write_text(json.dumps({"schema": 1, "device_id": "deadbeef", "backup_sha256":
                                             json.loads((disk.parent / "manifest.json").read_text())["sha256"],
                                             "status": "mystery", "completed": []}))
        refused = run_restore(args + ["--phase", "userdata", "--state", str(root / "unknown-state.json")],
                              no_tty, capture_output=True, text=True)
        assert refused.returncode == 2 and "status is unknown" in refused.stderr, (refused.returncode, refused.stderr)

        # Injected write failure records unresolved state; retry is refused and
        # the fake device sees exactly one actual (non-skipwrite) attempt.
        failed_disk = root / "failed-device.img"; failed_disk.write_bytes(fake_disk.read_bytes())
        fail_log = root / "failed-edl.log"; fail_state = root / "failed-state.json"
        fail_env = dict(no_tty, OPENSTICK_FAKE_EMMC=str(failed_disk), OPENSTICK_EDL_LOG=str(fail_log),
                        PARTIAL_WRITE_LBA=str(userdata["start_lba"]))
        fail_env.pop("PARTIAL_WRITE_LBA")
        fail_env["FAIL_WRITE_LBA"] = str(userdata["start_lba"])
        rc, output_text = run_typed(args + ["--verbose", "--phase", "userdata", "--state", str(fail_state)], fail_env, token)
        assert rc == 2 and "will not be retried" in output_text
        assert "SYNTHETIC EDL WRITE FAILURE DETAIL" in output_text
        assert "Hint:" in output_text and "re-enter EDL" in output_text
        pinned_fail_state = canonical_restore_state(fail_state, base_env["OPENSTICK_TEST_BACKUP_SHA"])
        assert json.loads(pinned_fail_state.read_text())["status"] == "interrupted"
        fail_env.pop("FAIL_WRITE_LBA")
        rc, output_text = run_typed(args + ["--phase", "userdata", "--state", str(fail_state)], fail_env, token)
        assert rc == 2 and "unresolved" in output_text
        # A caller cannot bypass the unresolved write-ahead record by choosing
        # a second state path; the explicit path must be the deterministic
        # per-device+backup state file, and refusal precedes EDL write.
        alternate = root / "alternate-state.json"
        alternate_command = args + ["--phase", "userdata", "--state", str(alternate)]
        alternate_env = dict(fail_env, HOME=str(pinned_fail_state.parents[3]))
        alternate = subprocess.run(alternate_command, env=alternate_env, capture_output=True, text=True)
        assert alternate.returncode == 2 and "state path is pinned" in alternate.stderr, (alternate.returncode, alternate.stderr)
        writes = [line for line in fail_log.read_text().splitlines() if line.startswith("ws ") and "--skipwrite" not in line]
        assert len(writes) == 1, writes

        # A timed-out standalone EDL call must preserve the client's captured
        # SIGINT tail when verbose output is requested.
        timeout_copy = root / "timeout-elsewhere"
        shutil.copytree(standalone, timeout_copy)
        timeout_script = timeout_copy / "scripts/restore.py"
        timeout_text = timeout_script.read_text().replace("IDENTITY_READ_TIMEOUT_SECONDS=20", "IDENTITY_READ_TIMEOUT_SECONDS=0.5")
        timeout_script.write_text(timeout_text)
        timeout_manifest_path = timeout_copy / "manifest.json"
        timeout_manifest = json.loads(timeout_manifest_path.read_text())
        timeout_manifest["files"]["scripts/restore.py"] = {
            "sha256": hashlib.sha256(timeout_script.read_bytes()).hexdigest(),
            "size": timeout_script.stat().st_size}
        timeout_manifest_path.write_text(json.dumps(timeout_manifest, sort_keys=True, indent=2) + "\n")
        timeout_sums = timeout_copy / "SHA256SUMS"
        timeout_sums.write_text("".join("%s  %s\n" % (timeout_manifest["files"][n]["sha256"], n)
                                         for n in sorted(timeout_manifest["files"])))
        timeout_args = [sys.executable, str(timeout_script), "--device", "deadbeef", "--edl", str(fake_edl),
                        "--loader", str(loader), "--verbose"]
        timeout_result = subprocess.run(timeout_args, env=dict(base_env, HANG_IDENTITY="1"), capture_output=True, text=True)
        assert timeout_result.returncode == 2
        assert "SYNTHETIC EDL SIGINT OUTPUT TAIL" in timeout_result.stderr, timeout_result.stderr
        assert "Hint:" in timeout_result.stderr and "re-enter EDL" in timeout_result.stderr

        # Even if a ready state claims the prior data phase completed, a
        # partial live partition must be detected before the next phase can
        # issue a write. This models a torn/corrupted completed data range.
        partial_disk = root / "partial-completed-device.img"; partial_disk.write_bytes(disk.read_bytes())
        partial_state_request = root / "partial-completed-state.json"
        partial_state = canonical_restore_state(partial_state_request, base_env["OPENSTICK_TEST_BACKUP_SHA"])
        partial_state.parent.mkdir(parents=True, exist_ok=True)
        partial_state.write_text(json.dumps({"schema": 1, "device_id": "deadbeef",
            "backup_sha256": base_env["OPENSTICK_TEST_BACKUP_SHA"], "status": "ready",
            "phase": "userdata", "completed": ["userdata"], "gpt_origin": "backup"}))
        with partial_disk.open("r+b") as stream:
            stream.seek(userdata["start_lba"] * 512); stream.write(b"TORN-PARTITION-WRITE")
        partial_log = root / "partial-completed-edl.log"
        partial_env = dict(base_env, OPENSTICK_FAKE_EMMC=str(partial_disk), OPENSTICK_EDL_LOG=str(partial_log))
        next_token = "restore deadbeef " + ordered_phases[1]
        partial_refusal, partial_output = run_typed(args + ["--phase", ordered_phases[1], "--state", str(partial_state_request)],
            dict(partial_env, COMMIT="1", CONFIRM=next_token), next_token)
        assert partial_refusal == 2 and "completed restore data partition differs from backup: userdata" in partial_output, partial_output
        assert not partial_log.exists() or not any(line.startswith("ws ") and "--skipwrite" not in line
                                                   for line in partial_log.read_text().splitlines())

        # Successful synthetic restore runs every planner row separately with
        # fresh confirmations, then exact full-disk readback passes.
        good_disk = root / "good-device.img"; good_disk.write_bytes(fake_disk.read_bytes())
        good_log = root / "good-edl.log"; good_state = root / "good-state.json"
        good_env = dict(no_tty, OPENSTICK_FAKE_EMMC=str(good_disk), OPENSTICK_EDL_LOG=str(good_log))
        phase_names = ordered_phases
        for phase in phase_names:
            phase_token = "restore deadbeef " + phase
            phase_env = dict(good_env, COMMIT="1", CONFIRM=phase_token)
            rc, phase_output = run_typed(args + ["--phase", phase, "--state", str(good_state)],
                                         phase_env, phase_token)
            assert rc == 0, (phase, rc, phase_output)
        assert good_disk.read_bytes() == disk.read_bytes()
        assert "full-disk readback match" in phase_output

        # Corrupt the final full-device readback without altering the target;
        # the final phase must exit 3 and persist failure, never imply safe boot.
        corrupt_disk = root / "corrupt-device.img"; corrupt_disk.write_bytes(fake_disk.read_bytes())
        corrupt_env = dict(good_env, OPENSTICK_FAKE_EMMC=str(corrupt_disk), CORRUPT_RF="1")
        corrupt_state = root / "corrupt-state.json"
        for phase in phase_names:
            phase_token = "restore deadbeef " + phase
            phase_env = dict(corrupt_env, COMMIT="1", CONFIRM=phase_token)
            rc, verify_output = run_typed(args + ["--phase", phase, "--state", str(corrupt_state)], phase_env, phase_token)
        assert rc == 3 and "VERIFY FAILED" in verify_output and "Do not boot" in verify_output
        assert json.loads(canonical_restore_state(corrupt_state, base_env["OPENSTICK_TEST_BACKUP_SHA"]).read_text())["status"] == "failed-verify"

        # Modified member bytes are refused even if the file remains listed.
        tampered = root / "tampered.ostpkg.tar"
        with tarfile.open(output) as src, tarfile.open(tampered, "w") as dst:
            for member in src.getmembers():
                content = src.extractfile(member).read() if member.isfile() else None
                if member.name == "payload/emmc-full.bin":
                    offset = 200 * 512
                    content = content[:offset] + bytes([content[offset] ^ 1]) + content[offset + 1:]
                dst.addfile(member, __import__("io").BytesIO(content) if content is not None else None)
        try:
            packages.verify_package(tampered)
        except store.RefusedError:
            pass
        else:
            raise AssertionError("modified payload accepted")

        # Traversal, symlink and duplicate members are rejected before content use.
        bad_path = root / "traversal.ostpkg.tar"
        with tarfile.open(bad_path, "w") as archive:
            member = tarfile.TarInfo("../escape")
            member.size = 1
            archive.addfile(member, __import__("io").BytesIO(b"x"))
        try:
            packages.verify_package(bad_path)
        except store.RefusedError:
            pass
        else:
            raise AssertionError("traversal member accepted")

        link_path = root / "symlink.ostpkg.tar"
        with tarfile.open(link_path, "w") as archive:
            member = tarfile.TarInfo("payload/escape")
            member.type = tarfile.SYMTYPE; member.linkname = "../../outside"
            archive.addfile(member)
        try:
            packages.verify_package(link_path)
        except store.RefusedError:
            pass
        else:
            raise AssertionError("unsafe symlink member accepted")

        duplicate = root / "duplicate.ostpkg.tar"
        with tarfile.open(output) as src, tarfile.open(duplicate, "w") as dst:
            for member in src.getmembers():
                content = src.extractfile(member).read() if member.isfile() else None
                dst.addfile(member, __import__("io").BytesIO(content) if content is not None else None)
            member = src.getmember("payload/gpt-primary.bin")
            dst.addfile(member, __import__("io").BytesIO(src.extractfile(member).read()))
        try:
            packages.verify_package(duplicate)
        except store.RefusedError:
            pass
        else:
            raise AssertionError("duplicate member accepted")

        empty_dir = root / "empty-dir.ostpkg.tar"
        with tarfile.open(output) as src, tarfile.open(empty_dir, "w") as dst:
            for member in src.getmembers():
                content = src.extractfile(member).read() if member.isfile() else None
                dst.addfile(member, __import__("io").BytesIO(content) if content is not None else None)
            directory_member = tarfile.TarInfo("payload/unlisted-empty-dir/")
            directory_member.type = tarfile.DIRTYPE
            dst.addfile(directory_member)
        try:
            packages.verify_package(empty_dir)
        except store.RefusedError:
            pass
        else:
            raise AssertionError("unlisted empty directory member accepted")

        archive_link = root / "archive-link.ostpkg.tar"
        archive_link.symlink_to(output)
        try:
            packages.verify_package(archive_link)
        except store.RefusedError:
            pass
        else:
            raise AssertionError("symlinked archive path accepted")

        # Current M4 build format is GPT-T boot.img + userdata rootfs.img and
        # cannot be passed to the legacy GPT-B boot.bin/rootfs.bin first-install consumer.
        m4 = root / "m4-build"
        m4.mkdir(); (m4 / "boot.img").write_bytes(b"synthetic boot container")
        (m4 / "rootfs.img").write_bytes(b"synthetic ext4 rootfs")
        try:
            packages._create_install(home, device, m4, "synthetic-backup", root / "install.ostpkg.tar")
        except store.RefusedError as exc:
            assert "incompatible" in str(exc)
        else:
            raise AssertionError("incompatible M4 image set accepted")

        # Missing verified matching full backup cannot produce install package.
        try:
            packages._verified_backup(home, device, "absent")
        except store.RefusedError:
            pass
        else:
            raise AssertionError("missing backup accepted")

        # A structurally complete legacy GPT-B fixture produces a standalone
        # package only through the explicit test-only pinned-loader injection.
        loader = root / "loader.mbn"; loader.write_bytes(b"SYNTHETIC TEST LOADER ONLY")
        loader_hash = hashlib.sha256(loader.read_bytes()).hexdigest()
        legacy = root / "legacy-gpt-b"; legacy.mkdir()
        install_live = root / "legacy-live-emmc.img"
        make_legacy_image_set(legacy, loader, loader_hash,
                             home / "backups" / "deadbeef" / "synthetic-backup", install_live)
        from tools.installer import cli as common
        install_package = root / "install.ostpkg.tar"
        with mock.patch.object(common, "LOADER_SHA256", loader_hash):
            packages._create_install(home, device, legacy, "synthetic-backup", install_package)
        install_manifest = packages.verify_package(install_package)
        assert install_manifest["package_type"] == "install"
        assert install_manifest["required_backup_sha256"] == device["backups"][0]["sha256"]
        assert "payload/image-set/loader/prog_emmc_firehose_8916.mbn" not in install_manifest["files"]
        assert install_manifest["external_dependencies"]["firehose_loader_sha256"] == loader_hash
        clean_install = root / "install-elsewhere"; clean_install.mkdir()
        with tarfile.open(install_package) as archive:
            archive.extractall(clean_install)
        # The install runner uses the same no-follow private-snapshot helper;
        # verify that a post-snapshot source mutation cannot alter its bytes.
        import importlib.util
        install_race = root / "install-race-elsewhere"
        shutil.copytree(clean_install, install_race)
        runner_spec = importlib.util.spec_from_file_location("synthetic_install_runner", install_race / "scripts/install.py")
        runner_module = importlib.util.module_from_spec(runner_spec)
        old_dont_write_bytecode = sys.dont_write_bytecode; sys.dont_write_bytecode = True
        try: runner_spec.loader.exec_module(runner_module)
        finally: sys.dont_write_bytecode = old_dont_write_bytecode
        install_snapshot, _ = runner_module._snapshot_package(install_race, "install")
        boot_rel = "payload/image-set/files-ufi103s/boot.bin"
        before = (install_snapshot / boot_rel).read_bytes()
        (install_race / boot_rel).write_bytes(b"changed-after-validation")
        assert (install_snapshot / boot_rel).read_bytes() == before
        install_script = clean_install / "scripts/install.py"
        install_help = subprocess.run([sys.executable, str(install_script), "--help"],
                                      env=dict(os.environ, PATH="/usr/bin:/bin"), capture_output=True, text=True)
        assert "UNVALIDATED on hardware" in install_help.stdout
        wrong_device = subprocess.run([sys.executable, str(install_script), "--device", "cafebabe", "--edl", "/missing/edl",
                                       "--loader", str(loader), "--state-dir", str(root / "install-state")],
                                      env=dict(os.environ, PATH="/usr/bin:/bin"), capture_output=True, text=True)
        assert wrong_device.returncode == 2 and "selected device" in wrong_device.stderr, (wrong_device.returncode, wrong_device.stdout, wrong_device.stderr)
        wrong_loader = root / "wrong-loader"; wrong_loader.write_bytes(b"not pinned")
        refuse_loader = subprocess.run([sys.executable, str(install_script), "--device", "deadbeef", "--edl", "/missing/edl",
                                        "--loader", str(wrong_loader), "--state-dir", str(root / "install-state")],
                                       env=dict(os.environ, PATH="/usr/bin:/bin"), capture_output=True, text=True)
        assert refuse_loader.returncode == 2 and "hash mismatched" in refuse_loader.stderr
        no_edl = subprocess.run([sys.executable, str(install_script), "--device", "deadbeef", "--edl", "/missing/edl",
                                 "--loader", str(loader), "--state-dir", str(root / "install-state")],
                                env=dict(os.environ, PATH="/usr/bin:/bin"), capture_output=True, text=True)
        assert no_edl.returncode == 2 and "live EDL" in no_edl.stderr, (no_edl.returncode, no_edl.stdout, no_edl.stderr)
        # Positive standalone fake-board installation: no builder checkout on
        # PATH, only fake EDL/sudo/simg2img. The package runner delegates to the
        # bundled legacy phase engines and we answer their typed gates via pty.
        accepted_package = root / "install-accepted-elsewhere"; accepted_package.mkdir()
        with tarfile.open(install_package) as archive:
            archive.extractall(accepted_package)
        accepted_script = accepted_package / "scripts/install.py"
        fake_tools = root / "install-fake-tools"
        install_edl_log = root / "install-edl.log"
        install_edl, fastboot_log = make_install_tools(fake_tools, install_live, install_edl_log)
        accepted_env = dict(os.environ, PATH=str(fake_tools) + ":/usr/bin:/bin")
        assert shutil.which("fastboot", path=accepted_env["PATH"]) == str(fake_tools / "fastboot")
        assert shutil.which("sudo", path=accepted_env["PATH"]) == str(fake_tools / "sudo")
        assert shutil.which("simg2img", path=accepted_env["PATH"]) == str(fake_tools / "simg2img")
        rc, install_output, confirmations = run_flash_session(
            [sys.executable, str(accepted_script), "--device", "deadbeef", "--edl", str(install_edl),
             "--loader", str(loader), "--state-dir", str(root / "accepted-install-state")], accepted_env)
        assert rc == 2 and "stopped after GPT" in install_output, (rc, install_output[-5000:])
        assert not fastboot_log.exists() or fastboot_log.read_text(encoding="utf-8") == "", "standalone first-install unexpectedly invoked fastboot"
        assert confirmations == {"flash deadbeef gpt"}, confirmations
        actual_writes = [line.split() for line in install_edl_log.read_text().splitlines()
                         if line.startswith("ws ") and "--skipwrite" not in line]
        target_gpt_secondary = install_live.stat().st_size // 512 - 33
        assert {int(row[1]) for row in actual_writes} == {0, target_gpt_secondary} and len(actual_writes) == 2, actual_writes

        # A standalone restore package carries hashed state evidence from a
        # registered legacy GPT-B Path A image set. The fake target starts with that
        # exact GPT/firmware state and must return byte-for-byte to its backup.
        transition_set = root / "transition-gpt-b"
        transition_live = root / "transition-source-live.img"
        make_legacy_image_set(transition_set, loader, loader_hash,
                             home / "backups" / "deadbeef" / "synthetic-backup", transition_live,
                             sectors_override=4096)
        device["flashes"] = [{"image_set": str(transition_set),
                              "outcome": "engine-reported-pass-not-boot-verified",
                              "phases": [{"name": name, "verified": True} for name in
                                         ("pre", "gpt", "tz", "rpm", "sbl1", "hyp", "aboot", "final", "first-install")]}]
        store.save_registry(home, {"schema": 1, "devices": [device]})
        transition_package = root / "restore-transition.ostpkg.tar"
        packages._create_restore(home, device, "synthetic-backup", transition_package)
        transition_manifest = packages.verify_package(transition_package)
        assert transition_manifest["transition_state"]["format"] == "openstick-gpt-b-transition"
        clean_restore = root / "restore-transition-elsewhere"; clean_restore.mkdir()
        with tarfile.open(transition_package) as archive:
            archive.extractall(clean_restore)
        restore_script = clean_restore / "scripts/restore.py"
        restore_script.write_text(restore_script.read_text().replace(
            packages._RESTORE_RUNNER.split('PINNED_LOADER_SHA256="')[1].split('"')[0], loader_hash))
        outer = json.loads((clean_restore / "manifest.json").read_text())
        outer["files"]["scripts/restore.py"] = {"sha256": hashlib.sha256(restore_script.read_bytes()).hexdigest(),
                                                  "size": restore_script.stat().st_size}
        (clean_restore / "manifest.json").write_text(json.dumps(outer, sort_keys=True, indent=2) + "\n")
        (clean_restore / "SHA256SUMS").write_text("".join("%s  %s\n" % (outer["files"][n]["sha256"], n)
                                                              for n in sorted(outer["files"])))
        transition_disk = root / "transition-device.img"
        shutil.copyfile(home / "backups" / "deadbeef" / "synthetic-backup" / "emmc-full.bin", transition_disk)
        tm = transition_manifest["transition_state"]
        for name, start, count, rel in (
                ("gpt-T-primary.bin", 0, 34, "gpt/gpt-T-primary.bin"),
                ("gpt-T-secondary.bin", 4096 - 33, 33, "gpt/gpt-T-secondary.bin")):
            with transition_disk.open("r+b") as target, (transition_set / "pathA" / rel).open("rb") as source_file:
                target.seek(start * 512); target.write(source_file.read(count * 512))
        for name, evidence in tm["firmware"].items():
            path = {"sbl1": "fw/sbl1.img", "rpm": "fw/rpm.img", "tz": "fw/tz.img",
                    "hyp": "fw/hyp.img", "aboot": "fw/aboot.img"}[name]
            item = transition_set / "pathA" / path
            with transition_disk.open("r+b") as target, item.open("rb") as source_file:
                target.seek(evidence["start_lba"] * 512); target.write(source_file.read())
        transition_edl = root / "transition-edl"; make_fake_edl(transition_edl)
        transition_log = root / "transition-edl.log"
        transition_env = dict(base_env, OPENSTICK_FAKE_EMMC=str(transition_disk), OPENSTICK_EDL_LOG=str(transition_log))
        transition_loader = root / "transition-loader"; transition_loader.write_bytes(loader.read_bytes())
        transition_args = [sys.executable, str(restore_script), "--device", "deadbeef", "--edl", str(transition_edl),
                           "--loader", str(transition_loader)]
        restore_help = subprocess.run(transition_args + ["--help"], env=transition_env,
                                      capture_output=True, text=True)
        assert "UNVALIDATED on hardware" in restore_help.stdout
        inspect = subprocess.run(transition_args, env=transition_env, capture_output=True, text=True)
        assert inspect.returncode == 0 and "Ordered restore phases" in inspect.stdout, (inspect.returncode, inspect.stdout, inspect.stderr)
        restore_phases = re.search(r"Ordered restore phases: (.+)", inspect.stdout).group(1).split(", ")
        transition_state = root / "transition-restore-state.json"
        for phase in restore_phases:
            token = "restore deadbeef " + phase
            phase_env = dict(transition_env, COMMIT="1", CONFIRM=token)
            rc, output_text = run_typed(transition_args + ["--phase", phase, "--state", str(transition_state)],
                                        phase_env, token)
            assert rc == 0, (phase, rc, output_text[-1000:])
            assert "UNVALIDATED on hardware" in output_text
        assert transition_disk.read_bytes() == (home / "backups" / "deadbeef" / "synthetic-backup" / "emmc-full.bin").read_bytes()
        assert "stopped after GPT" in install_output and "first-install" not in install_output
    print("M6 synthetic package checks PASS")


def test_verify_package_requires_sensitive_marking(root):
    """A package with valid member hashes still needs the SENSITIVE marker."""
    source = root / "sensitive-valid.ostpkg.tar"
    mutant = root / "sensitive-marker-removed.ostpkg.tar"
    make_raw_package(source)
    with tarfile.open(source, "r:") as original, tarfile.open(mutant, "w") as changed:
        for member in original.getmembers():
            data = original.extractfile(member).read() if member.isfile() else None
            if member.name == "manifest.json":
                manifest = json.loads(data.decode("utf-8"))
                manifest["sensitive"] = False
                data = (json.dumps(manifest, sort_keys=True) + "\n").encode("utf-8")
                member.size = len(data)
            changed.addfile(member, __import__("io").BytesIO(data) if data is not None else None)
    try:
        packages.verify_package(mutant)
    except store.RefusedError as exc:
        assert "SENSITIVE" in str(exc)
    else:
        raise AssertionError("package without the SENSITIVE marker was accepted")


if __name__ == "__main__":
    run()
