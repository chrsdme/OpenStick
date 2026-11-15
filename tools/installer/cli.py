"""M1 command line, diagnostics, and device registry operations."""
import argparse
import hashlib
import json
import os
import shutil
import sys
import time
import tempfile
import re
import struct
import stat
import signal
import zlib
import subprocess
import datetime
from pathlib import Path

from . import store
from . import seed as seed_format
from . import configure as configure_module

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_REFUSED = 2
EXIT_VERIFY = 3
LOADER_SHA256 = "959439aa5864685999b713c3ed12ad5fa408149648b670a9a9ef77bcc9dcab14"
LOADER_NAME = "prog_emmc_firehose_8916.mbn"


def _redact(message):
    text = str(message)
    for key in ("WIFI_PSK", "PASSWORD", "APN_PASSWORD", "ROOT_PASSWORD_HASH", "SSH_PRIVATE_KEY"):
        # Do not print environment-provided values if an exception includes one.
        value = os.environ.get(key)
        if value:
            text = text.replace(value, "[REDACTED]")
    return text


def log(message, verbose=False, enabled=False):
    if not verbose or enabled:
        print(_redact(message), file=sys.stderr if enabled else sys.stdout)


def default_home():
    return Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))) / "openstick"


def parser():
    p = argparse.ArgumentParser(prog="openstick", description="Safe OpenStick installer and device registry.")
    p.add_argument("--home", metavar="DIR", help="private state directory (default: %(default)s)", default=str(default_home()))
    p.add_argument("--dry-run", action="store_true", help="show planned changes without writing")
    p.add_argument("--verbose", action="store_true", help="show diagnostic details")
    p.add_argument("--no-color", action="store_true", help="disable colored output")
    p.add_argument("--board", metavar="NAME", help="select a board class")
    p.add_argument("--device", metavar="ID", help="select a registered device")
    subs = p.add_subparsers(dest="command", required=True)
    subs.add_parser("doctor", help="check local prerequisites and private store")
    device = subs.add_parser("device", help="manage the local device registry")
    ds = device.add_subparsers(dest="device_command", required=True)
    ds.add_parser("list", help="list registered devices")
    show = ds.add_parser("show", help="show one registered device")
    show.add_argument("id")
    label = ds.add_parser("label", help="set a device label")
    label.add_argument("id")
    label.add_argument("label")
    note = ds.add_parser("note", help="set a device note")
    note.add_argument("id")
    note.add_argument("note")
    backup = subs.add_parser("backup", help="read and verify a complete EDL eMMC backup")
    backup.add_argument("--paranoid", action="store_true", help="read the eMMC twice and compare")
    backup.add_argument("--again", action="store_true", help="allow an additional verified backup")
    conf = subs.add_parser("configure", help="create or display a private personalization seed")
    conf.add_argument("--seed", default="default", metavar="NAME", help="seed name (default: default)")
    conf.add_argument("--profile", choices=("dev", "prod"), default="dev", help="seed account policy")
    conf.add_argument("--show", action="store_true", help="show a seed with secrets masked")
    seed_cmd = subs.add_parser("seed", help="inject or push a personalization seed")
    ss = seed_cmd.add_subparsers(dest="seed_command", required=True)
    inject = ss.add_parser("inject", help="inject a seed into a copy of a generic rootfs image")
    inject.add_argument("--build", required=True, metavar="ID")
    inject.add_argument("--seed", required=True, metavar="NAME")
    push = ss.add_parser("push", help="push a seed to a running, reachable stick")
    push.add_argument("--seed", required=True, metavar="NAME")
    push.add_argument("--host", metavar="USER@HOST")
    return p


def _which(name):
    return shutil.which(name)


def _loader_candidate():
    edl_dir = os.environ.get("OPENSTICK_EDL_DIR")
    candidates = []
    if edl_dir:
        candidates.extend([Path(edl_dir) / LOADER_NAME, Path(edl_dir) / "../" / LOADER_NAME])
    candidates.extend([Path("out") / LOADER_NAME, Path(__file__).resolve().parents[2] / "out" / LOADER_NAME])
    return next((p.resolve() for p in candidates if p.is_file()), None)


def _disk_free(path):
    probe = Path(path)
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return shutil.disk_usage(probe).free


def doctor(args):
    checks = []
    for name, candidates in (("python3", ("python3",)), ("bash", ("bash",)), ("e2fsprogs", ("debugfs", "e2fsck", "mke2fs")), ("fastboot", ("fastboot",))):
        found = [_which(c) for c in candidates]
        ok = all(found)
        checks.append((name, ok, ", ".join(found) if ok else "missing: " + ", ".join(c for c, f in zip(candidates, found) if not f)))
    edl_dir = os.environ.get("OPENSTICK_EDL_DIR")
    edl = (Path(edl_dir) / "edl.py") if edl_dir else None
    edl_ok = bool(edl and edl.is_file()) or bool(_which("edl"))
    checks.append(("EDL client", edl_ok, str(edl) if edl and edl.is_file() else (_which("edl") or "missing: set OPENSTICK_EDL_DIR or add edl to PATH")))
    loader = _loader_candidate()
    if loader:
        digest = hashlib.sha256(loader.read_bytes()).hexdigest()
        checks.append(("Firehose loader", digest == LOADER_SHA256, str(loader) + (" (sha256 verified)" if digest == LOADER_SHA256 else " (sha256 mismatch)")))
    else:
        checks.append(("Firehose loader", False, "missing: " + LOADER_NAME))
    checks.append(("USB permissions", os.access("/dev/bus/usb", os.R_OK | os.W_OK), "USB access available" if os.access("/dev/bus/usb", os.R_OK | os.W_OK) else "USB access unavailable; suggested udev rule: SUBSYSTEM==\"usb\", ATTR{idVendor}==\"05c6\", MODE=\"0660\", GROUP=\"plugdev\""))
    free = _disk_free(args.home)
    checks.append(("Free disk space", free >= 12 * 1024 ** 3, "%.1f GiB free (12 GiB required)" % (free / 1024 ** 3)))
    try:
        store.validate_location(args.home)
        checks.append(("Private store location", True, str(Path(args.home).absolute())))
        if Path(args.home).exists():
            mode = Path(args.home).stat().st_mode & 0o777
            checks.append(("Private store permissions", mode == 0o700, "mode %03o" % mode))
        else:
            checks.append(("Private store permissions", True, "will be created with mode 700"))
    except store.RefusedError as exc:
        checks.append(("Private store location", False, str(exc)))
    for name, ok, detail in checks:
        print("%s %s: %s" % ("PASS" if ok else "FAIL", name, _redact(detail)))
        if not ok:
            if name in ("python3", "bash", "e2fsprogs", "fastboot", "EDL client", "Firehose loader"):
                print("  Install hint: Debian: apt install python3 bash e2fsprogs android-sdk-platform-tools; Fedora: dnf install python3 bash e2fsprogs android-tools")
    return EXIT_OK if all(ok for _, ok, _ in checks) else EXIT_ERROR


def _find_device(registry, ident):
    for entry in registry["devices"]:
        if str(entry.get("id", "")).lower() == ident.lower():
            return entry
    return None


def _parse_gpt(path):
    size = path.stat().st_size
    if size % 512 or size < 1024:
        raise ValueError("image size is not a whole number of sectors")
    sectors = size // 512
    def hdr(lba):
        with path.open("rb") as f:
            f.seek(lba * 512); raw = f.read(512)
        if len(raw) != 512 or raw[:8] != b"EFI PART": raise ValueError("GPT header signature is invalid")
        rev, hsize, crc, reserved = struct.unpack_from("<IIII", raw, 8)
        cur, alt, first, last = struct.unpack_from("<QQQQ", raw, 24)
        elba, count, esize, ecrc = struct.unpack_from("<QIII", raw, 72)
        if hsize < 92 or hsize > 512 or reserved or cur != lba or alt >= sectors or first > last or last >= sectors:
            raise ValueError("GPT header geometry is invalid")
        check = bytearray(raw[:hsize]); struct.pack_into("<I", check, 16, 0)
        if zlib.crc32(check) & 0xffffffff != crc: raise ValueError("GPT header CRC mismatch")
        if count < 1 or esize < 128 or esize % 8 or count * esize > size:
            raise ValueError("GPT entry geometry is invalid")
        with path.open("rb") as f:
            f.seek(elba * 512); entries = f.read(count * esize)
        if len(entries) != count * esize or zlib.crc32(entries) & 0xffffffff != ecrc:
            raise ValueError("GPT partition table CRC mismatch")
        rows=[]
        for i in range(count):
            e=entries[i*esize:(i+1)*esize]
            if e[:16] == b"\0"*16: continue
            start,end=struct.unpack_from("<QQ",e,32)
            name=e[56: min(128,esize)].decode("utf-16le", "strict").rstrip("\0")
            if not name or start < first or end < start or end > last: raise ValueError("GPT partition boundary is invalid")
            rows.append((name,start,end))
        return alt, first, last, rows
    p=hdr(1)
    if p[0] != sectors-1: raise ValueError("primary GPT does not point to the final disk sector")
    b=hdr(sectors-1)
    if b[0] != 1 or p[1:3] != b[1:3] or p[3] != b[3]: raise ValueError("primary and backup GPT disagree")
    ordered=sorted(p[3], key=lambda row: row[1]); prior=p[1]-1
    for _, start, end in ordered:
        if start <= prior: raise ValueError("GPT partitions overlap")
        prior=end
    by={n:(s,e) for n,s,e in ordered}
    if not all(n in by for n in ("fsg","sec","fsc")): raise ValueError("GPT lacks required identity partitions")
    return by


def _hash_range(path, start, end):
    h=hashlib.sha256()
    with path.open("rb") as f:
        f.seek(start*512); left=(end-start+1)*512
        while left:
            data=f.read(min(left, 8*1024*1024))
            if not data: raise ValueError("partition data is truncated")
            h.update(data); left-=len(data)
    return h.hexdigest()


def _sha_file(path):
    h=hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda:f.read(8*1024*1024),b""): h.update(block)
    return h.hexdigest()


def _debugfs_quote(path):
    value = str(path)
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("unsafe debugfs path")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def backup_command(args, root):
    print("WARNING: this backup contains IMEI, calibration and persist data. Keep it private and make an independent offline recovery copy.")
    edl = shutil.which("edl")
    if not edl: raise store.StoreError("EDL client is missing")
    env=os.environ.copy()
    # The shim's identity response is metadata only; real clients report Sahara serial on stderr.
    try:
        ident_run=subprocess.run([edl,"getstorageinfo"],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=env,timeout=30)
    except (OSError,subprocess.TimeoutExpired): raise store.StoreError("could not read EDL identity")
    text=ident_run.stdout+"\n"+ident_run.stderr
    match=re.search(r"serial\s*=\s*([0-9a-fA-F]+)",text)
    if not match: raise store.StoreError("EDL client did not report a device serial")
    device_id=match.group(1).lower()
    hwid_match=re.search(r"(?:hwid|msm_id|pk_hash)\s*[=:]\s*([0-9a-fA-Fx]+)",text,re.I)
    existing=store.load_registry(root); device=_find_device(existing,device_id)
    if args.device and args.device.lower()!=device_id: raise store.RefusedError("connected EDL identity does not match --device")
    if device and device.get("edl",{}).get("serial",device_id).lower()!=device_id: raise store.RefusedError("EDL identity conflicts with registry")
    if device and device.get("backups") and not args.again: raise store.RefusedError("a verified backup already exists; use --again for another")
    if args.dry_run:
        print("DRY RUN: would read and verify eMMC for device %s."%device_id); return EXIT_OK
    backup_id=datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    parent=root/"backups"/device_id; parent.mkdir(mode=0o700,parents=True,exist_ok=True); os.chmod(parent,0o700)
    final=parent/backup_id
    if final.exists(): raise store.RefusedError("backup destination already exists")
    staging=Path(tempfile.mkdtemp(prefix=".staging-",dir=str(parent))); os.chmod(staging,0o700)
    try:
        dump=staging/"emmc-full.bin"
        def read_to(dest):
            p=subprocess.run([edl,"rf",str(dest)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env)
            if p.returncode: raise store.StoreError("EDL full read failed")
        read_to(dump)
        if args.paranoid:
            second=staging/"comparison.bin"; read_to(second)
            if dump.stat().st_size != second.stat().st_size or _sha_file(dump)!=_sha_file(second):
                raise ValueError("paranoid full read comparison failed")
            second.unlink()
        parts=_parse_gpt(dump)
        image_hash=hashlib.sha256()
        with dump.open("rb") as f:
            for block in iter(lambda:f.read(8*1024*1024),b""): image_hash.update(block)
        fingerprints={n:_hash_range(dump,*parts[n]) for n in ("fsg","sec","fsc")}
        old=device.get("identity",{}) if device else {}
        if old and old != fingerprints:
            raise store.RefusedError("EDL identity fingerprints conflict with registered device")
        manifest={"schema_version":1,"id":backup_id,"device_id":device_id,"edl":{"serial":device_id,"hwid":hwid_match.group(1) if hwid_match else None},"size":dump.stat().st_size,"sectors":dump.stat().st_size//512,"sha256":image_hash.hexdigest(),"identity":fingerprints,"partitions":{n:{"start_lba":s,"end_lba":e} for n,(s,e) in parts.items()},"verified_by":["full-read-size","gpt-primary-and-backup-crc","sha256"]+( ["paranoid-second-read"] if args.paranoid else []),"created":backup_id}
        (staging/"manifest.json").write_text(json.dumps(manifest,sort_keys=True,indent=2)+"\n")
        (staging/"SHA256SUMS").write_text("%s  emmc-full.bin\n"%manifest["sha256"])
        for f in staging.iterdir(): os.chmod(f,0o600)
        os.replace(staging,final); os.chmod(final,0o700)
        if device is None:
            device={"id":device_id,"label":"","board":args.board or "ufi103s","pcb_label":"","notes":"","edl":{"serial":device_id,"hwid":manifest["edl"]["hwid"]},"usb_aliases":[],"identity":fingerprints,"backups":[],"builds":[],"flashes":[],"seeds_applied":[],"packages":[],"created":backup_id}
            existing["devices"].append(device)
        else:
            device["identity"]=fingerprints
        device.setdefault("backups",[]).append({"id":backup_id,"path":str(final),"size":manifest["size"],"sha256":manifest["sha256"],"verified_by":manifest["verified_by"],"created":backup_id})
        device["updated_at"]=int(time.time())
        registry_saved=False
        try:
            store.save_registry(root,existing); registry_saved=True
            store.append_event(root,{"at":int(time.time()),"type":"backup.verified","device_id":device_id,"backup_id":backup_id,"sha256":manifest["sha256"],"size":manifest["size"]})
        except Exception:
            if registry_saved:
                # Restore the pre-operation registry if event persistence fails.
                original=store.load_registry(root)
                original_device=_find_device(original,device_id)
                if original_device:
                    original_device["backups"]=[b for b in original_device.get("backups",[]) if b.get("id")!=backup_id]
                    if not original_device["backups"]:
                        original_device["identity"]={}
                else:
                    original["devices"]=[d for d in original["devices"] if d.get("id","").lower()!=device_id]
                store.save_registry(root,original)
            shutil.rmtree(final,ignore_errors=True)
            raise
        print("Verified backup %s for device %s (%d bytes)."%(backup_id,device_id,manifest["size"]))
        return EXIT_OK
    except Exception:
        shutil.rmtree(staging,ignore_errors=True)
        raise


def device_command(args, root):
    registry = store.load_registry(root)
    command = args.device_command
    if command == "list":
        for item in registry["devices"]:
            print("%s\t%s" % (item.get("id", ""), item.get("label", "")))
        return EXIT_OK
    item = _find_device(registry, args.id)
    if not item:
        print("device not found", file=sys.stderr)
        return EXIT_ERROR
    if command == "show":
        print(json.dumps(item, sort_keys=True, indent=2))
        return EXIT_OK
    field = "label" if command == "label" else "note"
    item[field] = getattr(args, field)
    item["updated_at"] = int(time.time())
    if args.dry_run:
        print("DRY RUN: would update device %s %s." % (item["id"], field))
        return EXIT_OK
    store.save_registry(root, registry)
    # Event history records that a field changed, never its user supplied value.
    store.append_event(root, {"at": int(time.time()), "type": "device." + command, "device_id": item["id"], "field": field})
    print("Device %s %s updated." % (item["id"], field))
    return EXIT_OK


def _seed_path(root, name):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", name):
        raise store.RefusedError("invalid seed name")
    path = root / "seeds" / (name + ".seed")
    if path.is_symlink() or not path.is_file():
        raise store.StoreError("seed not found")
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise store.RefusedError("seed permissions must be 600")
    try:
        content = path.read_text(encoding="utf-8")
        seed_format.parse(content)
    except (OSError, UnicodeError, seed_format.SeedError):
        raise store.StoreError("seed is invalid")
    return path, content


def inject_seed(args, root):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", args.build):
        raise store.RefusedError("invalid build ID")
    seed_path, content = _seed_path(root, args.seed)
    build_dir = root / "builds" / args.build
    original = build_dir / "rootfs.img"
    if build_dir.is_symlink() or not original.is_file() or original.is_symlink():
        raise store.StoreError("generic build rootfs.img not found")
    debugfs, e2fsck = shutil.which("debugfs"), shutil.which("e2fsck")
    if not debugfs or not e2fsck:
        raise store.StoreError("debugfs and e2fsck are required")
    original_sha = _sha_file(original)
    check = subprocess.run([e2fsck, "-fn", str(original)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if check.returncode != 0:
        raise ValueError("generic rootfs image failed read-only filesystem validation")
    target_dir = build_dir / ("personal-" + args.seed)
    if target_dir.exists() or target_dir.is_symlink():
        raise store.RefusedError("personalized image destination already exists")
    if args.dry_run:
        print("DRY RUN: validated seed and generic image; would create a sensitive personalized copy.")
        return EXIT_OK
    staging = None
    tempdir = None
    previous_signals = {}
    try:
        staging = Path(tempfile.mkdtemp(prefix=".personal-" + args.seed + ".staging-", dir=str(build_dir)))
        os.chmod(staging, 0o700)
        target = staging / "rootfs.img"
        tempdir = Path(tempfile.mkdtemp(prefix=".seed-inject-", dir=str(root / "images")))
        os.chmod(tempdir, 0o700)
        payload = tempdir / "seed.payload"
        readback = tempdir / "seed.readback"
        def interrupted(signum, frame):
            raise KeyboardInterrupt()
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_signals[signum] = signal.signal(signum, interrupted)
        payload.write_text(content, encoding="utf-8")
        os.chmod(payload, 0o600)
        shutil.copyfile(original, target)
        os.chmod(target, 0o600)
        inject = subprocess.run([debugfs, "-w", "-R", "write %s /etc/openstick/seed.env" % _debugfs_quote(payload), str(target)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if inject.returncode:
            raise ValueError("debugfs seed injection failed")
        for field in ("uid", "gid"):
            owner = subprocess.run([debugfs, "-w", "-R", "set_inode_field /etc/openstick/seed.env %s 0" % field, str(target)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if owner.returncode:
                raise ValueError("debugfs seed ownership update failed")
        check = subprocess.run([e2fsck, "-fn", str(target)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if check.returncode != 0:
            raise ValueError("personalized rootfs failed read-only filesystem validation")
        dump = subprocess.run([debugfs, "-R", "dump -p /etc/openstick/seed.env %s" % _debugfs_quote(readback), str(target)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if dump.returncode or not readback.is_file() or _sha_file(readback) != _sha_file(seed_path):
            raise ValueError("injected seed read-back SHA-256 mismatch")
        if _sha_file(original) != original_sha:
            raise ValueError("generic rootfs image changed during injection")
        manifest = {"sensitive": True, "build_id": args.build, "seed_name": args.seed, "seed_sha256": _sha_file(seed_path), "rootfs_sha256": _sha_file(target), "verified_by": ["debugfs-injection", "e2fsck-read-only", "debugfs-readback-sha256"]}
        out = staging / "personal.json"
        out.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        os.chmod(out, 0o600)
        os.replace(staging, target_dir)
        print("Sensitive personalized rootfs created and verified by SHA-256 read-back.")
        return EXIT_OK
    except BaseException:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
        raise
    finally:
        for signum, handler in previous_signals.items():
            signal.signal(signum, handler)
        if tempdir is not None:
            shutil.rmtree(tempdir, ignore_errors=True)


def push_seed(args, root):
    seed_path, _ = _seed_path(root, args.seed)
    if not args.host or not re.fullmatch(r"[A-Za-z0-9_.-]+@[A-Za-z0-9.-]+", args.host):
        print("REFUSED: seed push requires an explicit USER@HOST with working SSH access. An unseeded production image without remote access must be recovered by flashing a seeded image.", file=sys.stderr)
        return EXIT_REFUSED
    ssh, scp = shutil.which("ssh"), shutil.which("scp")
    if not ssh or not scp:
        raise store.StoreError("ssh and scp are required")
    if args.dry_run:
        print("DRY RUN: would check SSH access, transfer a private seed, and start the seed service.")
        return EXIT_OK
    probe = subprocess.run([ssh, "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", args.host, "true"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8)
    if probe.returncode:
        print("REFUSED: SSH access is unavailable. An unseeded production image cannot be provisioned remotely; recovery requires flashing a seeded image.", file=sys.stderr)
        return EXIT_REFUSED
    wanted_hash = _sha_file(seed_path)
    remote_tmp = "/tmp/openstick-seed-%s.env" % wanted_hash[:16]
    copied = subprocess.run([scp, "-q", str(seed_path), args.host + ":" + remote_tmp], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if copied.returncode:
        raise store.StoreError("could not transfer seed over SSH")
    privileged = "" if args.host.startswith("root@") else "sudo "
    remote = "%sinstall -o root -g root -m 600 %s /etc/openstick/seed.env && %ssystemctl start openstick-seed.service && %sgrep -qx %s /etc/openstick/seed.applied; rc=$?; rm -f %s; exit $rc" % (privileged, remote_tmp, privileged, privileged, wanted_hash, remote_tmp)
    ssh_args = [ssh, "-o", "BatchMode=yes", args.host, remote] if not privileged else [ssh, "-t", args.host, remote]
    started = subprocess.run(ssh_args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if started.returncode:
        raise store.StoreError("remote seed application failed")
    print("Seed transferred to the reachable stick; application service completed.")
    return EXIT_OK


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "doctor":
            return doctor(args)
        root = store.ensure_store(args.home)
        if args.command == "backup": return backup_command(args,root)
        if args.command == "configure":
            if args.show: configure_module.show(root, args.seed); return EXIT_OK
            configure_module.configure(root, args.seed, args.profile); return EXIT_OK
        if args.command == "seed":
            if args.seed_command == "inject": return inject_seed(args, root)
            return push_seed(args, root)
        return device_command(args, root)
    except store.RefusedError as exc:
        print("REFUSED: " + _redact(exc), file=sys.stderr)
        return EXIT_REFUSED
    except KeyboardInterrupt:
        print("ERROR: operation interrupted; temporary staging removed.", file=sys.stderr)
        return EXIT_ERROR
    except (store.StoreError, OSError, ValueError, json.JSONDecodeError) as exc:
        print("ERROR: " + _redact(exc), file=sys.stderr)
        return EXIT_VERIFY if isinstance(exc, ValueError) else EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
