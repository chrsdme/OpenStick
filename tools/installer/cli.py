"""M1 command line, diagnostics, and device registry operations."""
import argparse
import hashlib
import grp
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
import traceback
from pathlib import Path

from . import store
from . import seed as seed_format
from . import configure as configure_module
from . import edl_runner

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_REFUSED = 2
EXIT_VERIFY = 3
class VerificationError(ValueError):
    """A concrete integrity or read-back verification failure."""

LOADER_SHA256 = edl_runner.PINNED_LOADER_SHA256
LOADER_NAME = edl_runner.LOADER_NAME



def log(message, verbose=False, enabled=False):
    if not verbose or enabled:
        print(str(message), file=sys.stderr if enabled else sys.stdout)


def default_home():
    return resolve_home(None)


def resolve_home(explicit=None):
    if explicit:
        return Path(explicit).expanduser()
    if os.environ.get("OPENSTICK_HOME"):
        return Path(os.environ["OPENSTICK_HOME"]).expanduser()
    if os.environ.get("XDG_DATA_HOME"):
        return Path(os.environ["XDG_DATA_HOME"]).expanduser() / "openstick"
    return Path.home() / ".local" / "share" / "openstick"


def parser():
    p = argparse.ArgumentParser(prog="openstick", description="Safe OpenStick installer and device registry.")
    p.add_argument("--home", metavar="DIR", help="private state directory (default: OPENSTICK_HOME, XDG_DATA_HOME, or ~/.local/share/openstick)")
    p.add_argument("--dry-run", action="store_true", help="show planned changes without writing")
    p.add_argument("--verbose", action="store_true", help="show diagnostic details")
    p.add_argument("--debug", action="store_true", help="show a traceback for unexpected errors")
    p.add_argument("--no-color", action="store_true", help="disable colored output")
    p.add_argument("--show-serial", action="store_true", help="show the full device serial in console output")
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
    build = subs.add_parser("build", help="build and register a generic Debian image set")
    build.add_argument("--profile", choices=("dev", "prod"), required=True)
    build.add_argument("--board", dest="build_board", required=True, metavar="BOARD")
    build.add_argument("--bake-user-env", action="store_true", help="explicitly enable legacy local/user configuration")
    flash = subs.add_parser("flash", help="run the gated Path A first-install sequence")
    flash.add_argument("--device", dest="flash_device", required=True, metavar="ID")
    flash.add_argument("--image-set", required=True, metavar="PATH")
    flash.add_argument("--reconcile-state", action="store_true", help="record explicit interactive reconciliation of an interrupted phase; performs no writes")
    verify = subs.add_parser("verify", help="verify a running stick through read-only SSH checks")
    verify.add_argument("--host", required=True, metavar="HOST", help="SSH destination for read-only checks")
    restore = subs.add_parser("restore", help="restore a verified full-device backup through EDL")
    restore.add_argument("--device", dest="restore_device", required=True, metavar="ID")
    restore.add_argument("--backup", dest="restore_backup", metavar="ID")
    restore.add_argument("--reconcile-state", dest="restore_reconcile_state", action="store_true",
                         help="record explicit interactive reconciliation of an interrupted restore; performs no writes")
    package = subs.add_parser(
        "package",
        help="create or verify portable install or restore packages (write scripts UNVALIDATED on hardware)",
        description="Create or verify portable install or restore packages. write scripts UNVALIDATED on hardware.")
    ps = package.add_subparsers(dest="package_command", required=True)
    for package_type in ("install", "restore"):
        create = ps.add_parser(
            package_type,
            help="create a portable %s package; packaged writes are UNVALIDATED on hardware and require typed confirmation" % package_type,
            description="Create a portable %s package. Packaged writes are UNVALIDATED on hardware and require typed confirmation." % package_type)
        create.set_defaults(package_type=package_type)
        create.add_argument("--device", dest="package_device", metavar="ID")
        create.add_argument("--backup", dest="package_backup", metavar="ID")
        create.add_argument("--image-set", dest="package_image_set", metavar="PATH")
        create.add_argument("--output", "--out", dest="package_output", required=True, metavar="PATH")
    package_verify = ps.add_parser("verify", help="verify package integrity without executing its contents")
    package_verify.add_argument("path", metavar="PATH")
    image_set = subs.add_parser("image-set", help="prepare or verify a hash-bound GPT-T build image set")
    image_sub = image_set.add_subparsers(dest="image_set_command", required=True)
    prepare = image_sub.add_parser("prepare", help="map a registered build to its validated GPT-T boot and userdata partitions")
    prepare.add_argument("--build", required=True, metavar="ID")
    prepare.add_argument("--layout", required=True, metavar="DIR", help="directory with gpt-T-primary.bin and gpt-T-secondary.bin")
    prepare.add_argument("--output", required=True, metavar="DIR")
    image_verify = image_sub.add_parser("verify", help="read-only verification of a prepared GPT-T image set")
    image_verify.add_argument("path", metavar="DIR")
    wizard = subs.add_parser("wizard", help="run or resume one explicit installer workflow stage")
    wizard.add_argument("--id", dest="wizard_id", metavar="ID")
    wizard.add_argument("--stage", choices=("doctor", "backup", "configure", "build", "inject", "flash", "verify", "restore", "package"))
    wizard.add_argument("--resume", action="store_true", help="resume a saved workflow after reconciling its state")
    wizard.add_argument("--reconcile-state", action="store_true", help="record a no-write reconciliation and stop")
    wizard.add_argument("--device", dest="wizard_device")
    wizard.add_argument("--backup", dest="wizard_backup")
    wizard.add_argument("--build", dest="wizard_build")
    wizard.add_argument("--seed", dest="wizard_seed")
    wizard.add_argument("--image-set", dest="wizard_image_set")
    wizard.add_argument("--board", dest="wizard_board")
    wizard.add_argument("--profile", dest="wizard_profile", choices=("dev", "prod"))
    wizard.add_argument("--host", dest="wizard_host")
    wizard.add_argument("--package-type", dest="package_type", choices=("install", "restore"))
    wizard.add_argument("--package-output", dest="package_output")
    wizard.add_argument("--bake-user-env", action="store_true")
    return p


def _which(name):
    return shutil.which(name)


def _disk_free(path):
    probe = Path(path)
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return shutil.disk_usage(probe).free


def _loader_candidate():
    """Compatibility wrapper for callers being migrated to edl_runner."""
    try:
        return edl_runner.resolve_loader()
    except (store.RefusedError, store.StoreError):
        return None


def _plugdev_group_exists():
    try:
        grp.getgrnam("plugdev")
        return True
    except KeyError:
        return False


def _edl_usb_udev_hint(plugdev_exists=None):
    if plugdev_exists is None:
        plugdev_exists = _plugdev_group_exists()
    rule = 'SUBSYSTEM=="usb", ATTR{idVendor}=="05c6", ATTR{idProduct}=="9008", TAG+="uaccess"'
    hint = "add this narrow udev rule, reload udev rules, then reconnect the board: " + rule
    if plugdev_exists:
        hint += '; alternatively use MODE="0660", GROUP="plugdev" and add the user to plugdev'
    return hint


def _check_edl_usb(sysfs_root="/sys/bus/usb/devices", usb_device_root="/dev/bus/usb",
                   access_check=os.access, plugdev_exists=None):
    """Check R/W access to an attached Qualcomm 9008 USB device, if present."""
    root = Path(sysfs_root)
    try:
        entries = list(root.iterdir())
    except OSError:
        return "FAIL", "could not scan " + str(root) + " for an attached EDL device", \
            "confirm sysfs is mounted and readable, then rerun doctor"
    for entry in entries:
        try:
            vendor = (entry / "idVendor").read_text().strip().lower()
            product = (entry / "idProduct").read_text().strip().lower()
        except OSError:
            continue
        if (vendor, product) != ("05c6", "9008"):
            continue
        try:
            busnum = (entry / "busnum").read_text().strip()
            devnum = (entry / "devnum").read_text().strip()
            if not (busnum.isdecimal() and devnum.isdecimal()):
                raise ValueError("busnum/devnum are not numeric")
            device_path = Path(usb_device_root) / busnum.zfill(3) / devnum.zfill(3)
        except (OSError, ValueError):
            return "FAIL", "could not derive the USB device node for attached EDL device", _edl_usb_udev_hint(plugdev_exists)
        try:
            accessible = access_check(str(device_path), os.R_OK | os.W_OK)
        except OSError:
            accessible = False
        if accessible:
            return "PASS", "read/write access to " + str(device_path), None
        return "FAIL", "read/write access unavailable for " + str(device_path), _edl_usb_udev_hint(plugdev_exists)
    return "INFO", "no EDL device attached; access will be checked when one is", None


def doctor(args, usb_sysfs_root="/sys/bus/usb/devices", usb_device_root="/dev/bus/usb",
           access_check=os.access):
    checks = []
    for name, candidates in (("python3", ("python3",)), ("bash", ("bash",)), ("e2fsprogs", ("debugfs", "e2fsck", "mke2fs")), ("fastboot", ("fastboot",))):
        found = [_which(c) for c in candidates]
        ok = all(found)
        checks.append(("PASS" if ok else "FAIL", name,
                       ", ".join(found) if ok else "missing: " + ", ".join(c for c, f in zip(candidates, found) if not f), None))
    try:
        client = edl_runner.resolve_client()
        checks.append(("PASS", "EDL client", " ".join(client), None))
    except (store.RefusedError, store.StoreError) as exc:
        checks.append(("FAIL", "EDL client", str(exc) + "; hint: set OPENSTICK_EDL_DIR to a directory with executable edl or edl.py, or add edl to PATH", None))
    try:
        loader = edl_runner.resolve_loader()
        checks.append(("PASS", "Firehose loader", str(loader) + " (sha256 verified)", None))
    except (store.RefusedError, store.StoreError) as exc:
        checks.append(("FAIL", "Firehose loader", str(exc), None))
    usb_status, usb_detail, usb_hint = _check_edl_usb(
        usb_sysfs_root, usb_device_root, access_check=access_check)
    checks.append((usb_status, "USB permissions", usb_detail, usb_hint))
    free = _disk_free(args.home)
    checks.append(("PASS" if free >= 12 * 1024 ** 3 else "FAIL", "Free disk space",
                   "%.1f GiB free (12 GiB required)" % (free / 1024 ** 3), None))
    try:
        store.validate_location(args.home)
        checks.append(("PASS", "Private store location", str(Path(args.home).absolute()), None))
        if Path(args.home).exists():
            mode = Path(args.home).stat().st_mode & 0o777
            checks.append(("PASS" if mode == 0o700 else "FAIL", "Private store permissions", "mode %03o" % mode, None))
        else:
            checks.append(("PASS", "Private store permissions", "will be created with mode 700", None))
    except store.RefusedError as exc:
        checks.append(("FAIL", "Private store location", str(exc), None))
    for status, name, detail, hint in checks:
        print("%s %s: %s" % (status, name, detail))
        if hint:
            print("  Hint: " + hint)
        elif status == "FAIL" and name in ("python3", "bash", "e2fsprogs", "fastboot"):
            print("  Install hint: Debian: apt install python3 bash e2fsprogs android-sdk-platform-tools; Fedora: dnf install python3 bash e2fsprogs android-tools")
    return EXIT_OK if not any(status == "FAIL" for status, _, _, _ in checks) else EXIT_ERROR


def _find_device(registry, ident):
    for entry in registry["devices"]:
        if str(entry.get("id", "")).lower() == ident.lower():
            return entry
    return None


def _parse_gpt(path):
    size = path.stat().st_size
    if size % 512 or size < 1024:
        raise VerificationError("image size is not a whole number of sectors")
    sectors = size // 512
    def hdr(lba):
        with path.open("rb") as f:
            f.seek(lba * 512); raw = f.read(512)
        if len(raw) != 512 or raw[:8] != b"EFI PART": raise VerificationError("GPT header signature is invalid")
        rev, hsize, crc, reserved = struct.unpack_from("<IIII", raw, 8)
        cur, alt, first, last = struct.unpack_from("<QQQQ", raw, 24)
        elba, count, esize, ecrc = struct.unpack_from("<QIII", raw, 72)
        if hsize < 92 or hsize > 512 or reserved or cur != lba or alt >= sectors or first > last or last >= sectors:
            raise VerificationError("GPT header geometry is invalid")
        check = bytearray(raw[:hsize]); struct.pack_into("<I", check, 16, 0)
        if zlib.crc32(check) & 0xffffffff != crc: raise VerificationError("GPT header CRC mismatch")
        if count < 1 or esize < 128 or esize % 8 or count * esize > size:
            raise VerificationError("GPT entry geometry is invalid")
        with path.open("rb") as f:
            f.seek(elba * 512); entries = f.read(count * esize)
        if len(entries) != count * esize or zlib.crc32(entries) & 0xffffffff != ecrc:
            raise VerificationError("GPT partition table CRC mismatch")
        rows=[]
        for i in range(count):
            e=entries[i*esize:(i+1)*esize]
            if e[:16] == b"\0"*16: continue
            start,end=struct.unpack_from("<QQ",e,32)
            name=e[56: min(128,esize)].decode("utf-16le", "strict").rstrip("\0")
            if not name or start < first or end < start or end > last: raise VerificationError("GPT partition boundary is invalid")
            rows.append((name,start,end))
        return alt, first, last, rows
    p=hdr(1)
    if p[0] != sectors-1: raise VerificationError("primary GPT does not point to the final disk sector")
    b=hdr(sectors-1)
    if b[0] != 1 or p[1:3] != b[1:3] or p[3] != b[3]: raise VerificationError("primary and backup GPT disagree")
    ordered=sorted(p[3], key=lambda row: row[1]); prior=p[1]-1
    for _, start, end in ordered:
        if start <= prior: raise VerificationError("GPT partitions overlap")
        prior=end
    by={n:(s,e) for n,s,e in ordered}
    if not all(n in by for n in ("fsg","sec","fsc")): raise VerificationError("GPT lacks required identity partitions")
    return by


def _hash_range(path, start, end):
    h=hashlib.sha256()
    with path.open("rb") as f:
        f.seek(start*512); left=(end-start+1)*512
        while left:
            data=f.read(min(left, 8*1024*1024))
            if not data: raise VerificationError("partition data is truncated")
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


def _display_serial(serial, show=False):
    if show or len(serial) <= 4:
        return serial
    return serial[:2] + "*" * (len(serial) - 4) + serial[-2:]


def backup_command(args, root, session=None):
    print("WARNING: this backup contains IMEI, calibration and persist data. Keep it private and make an independent offline recovery copy.")
    env=os.environ.copy()
    # Sahara identity is printed before Firehose setup. A short primary-GPT
    # range read triggers that block without using unsupported getstorageinfo.
    with tempfile.TemporaryDirectory(prefix="openstick-edl-identity-") as identity_dir:
        identity_path = Path(identity_dir) / "primary-gpt.bin"
        session = session or edl_runner.Session()
        session.run(
            ["rs", "0", "34", str(identity_path)],
            timeout=edl_runner.IDENTITY_READ_TIMEOUT_SECONDS,
            env=env, verbose=getattr(args, "verbose", False), debug=getattr(args, "debug", False),
        )
        if not identity_path.is_file() or identity_path.stat().st_size != 34 * 512:
            raise store.StoreError(
                "EDL identity range read was incomplete. Hint: confirm the device is in EDL mode and reconnect USB before retrying."
            )
        identity = session.identity
    device_id=identity["serial"]
    existing=store.load_registry(root); device=_find_device(existing,device_id)
    if args.device and args.device.lower()!=device_id: raise store.RefusedError("connected EDL identity does not match --device")
    if device and device.get("edl",{}).get("serial",device_id).lower()!=device_id: raise store.RefusedError("EDL identity conflicts with registry")
    if device and device.get("backups") and not args.again: raise store.RefusedError("a verified backup already exists; use --again for another")
    if args.dry_run:
        print("DRY RUN: would read and verify eMMC for device %s." %
              _display_serial(device_id, getattr(args, "show_serial", False))); return EXIT_OK
    backup_id=datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    parent=root/"backups"/device_id; parent.mkdir(mode=0o700,parents=True,exist_ok=True); os.chmod(parent,0o700)
    final=parent/backup_id
    if final.exists(): raise store.RefusedError("backup destination already exists")
    staging=Path(tempfile.mkdtemp(prefix=".staging-",dir=str(parent))); os.chmod(staging,0o700)
    try:
        dump=staging/"emmc-full.bin"
        def read_to(dest):
            session.run(["rf", str(dest)], timeout=edl_runner.FULL_READ_TIMEOUT_SECONDS, env=env,
                        verbose=getattr(args, "verbose", False), debug=getattr(args, "debug", False))
            if not dest.is_file():
                raise store.StoreError("EDL client completed without creating the full read. Hint: check the output path and retry after confirming EDL mode.")
        read_to(dump)
        if args.paranoid:
            second=staging/"comparison.bin"; read_to(second)
            if dump.stat().st_size != second.stat().st_size or _sha_file(dump)!=_sha_file(second):
                raise VerificationError("paranoid full read comparison failed")
            second.unlink()
        parts=_parse_gpt(dump)
        image_hash=hashlib.sha256()
        with dump.open("rb") as f:
            for block in iter(lambda:f.read(8*1024*1024),b""): image_hash.update(block)
        fingerprints={n:_hash_range(dump,*parts[n]) for n in ("fsg","sec","fsc")}
        old=device.get("identity",{}) if device else {}
        if old and old != fingerprints:
            raise store.RefusedError("EDL identity fingerprints conflict with registered device")
        manifest={"schema_version":1,"id":backup_id,"device_id":device_id,"edl":identity,"size":dump.stat().st_size,"sectors":dump.stat().st_size//512,"sha256":image_hash.hexdigest(),"identity":fingerprints,"partitions":{n:{"start_lba":s,"end_lba":e} for n,(s,e) in parts.items()},"verified_by":["full-read-size","gpt-primary-and-backup-crc","sha256"]+( ["paranoid-second-read"] if args.paranoid else []),"created":backup_id}
        (staging/"manifest.json").write_text(json.dumps(manifest,sort_keys=True,indent=2)+"\n")
        (staging/"SHA256SUMS").write_text("%s  emmc-full.bin\n"%manifest["sha256"])
        for f in staging.iterdir(): os.chmod(f,0o600)
        os.replace(staging,final); os.chmod(final,0o700)
        if device is None:
            device={"id":device_id,"label":"","board":args.board or "ufi103s","pcb_label":"","notes":"","edl":identity,"usb_aliases":[],"identity":fingerprints,"backups":[],"builds":[],"flashes":[],"seeds_applied":[],"packages":[],"created":backup_id}
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
        print("Verified backup %s for device %s (%d bytes)." %
              (backup_id, _display_serial(device_id, getattr(args, "show_serial", False)), manifest["size"]))
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
    manifest_path = build_dir / "build.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise store.StoreError("build profile marker is missing")
    try:
        build_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        seed_profile = seed_format.parse(content).get("PROFILE")
    except (OSError, UnicodeError, json.JSONDecodeError, seed_format.SeedError):
        raise store.StoreError("build profile marker or seed is invalid")
    if build_manifest.get("build_id") != args.build or build_manifest.get("profile") not in ("dev", "prod"):
        raise store.StoreError("build profile marker is invalid")
    if seed_profile != build_manifest["profile"]:
        raise store.RefusedError("seed profile does not match the image build profile")
    debugfs, e2fsck = shutil.which("debugfs"), shutil.which("e2fsck")
    if not debugfs or not e2fsck:
        raise store.StoreError("debugfs and e2fsck are required")
    original_sha = _sha_file(original)
    check = subprocess.run([e2fsck, "-fn", str(original)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if check.returncode != 0:
        raise VerificationError("generic rootfs image failed read-only filesystem validation")
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
            raise VerificationError("debugfs seed injection failed")
        for field in ("uid", "gid"):
            owner = subprocess.run([debugfs, "-w", "-R", "set_inode_field /etc/openstick/seed.env %s 0" % field, str(target)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if owner.returncode:
                raise VerificationError("debugfs seed ownership update failed")
        check = subprocess.run([e2fsck, "-fn", str(target)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if check.returncode != 0:
            raise VerificationError("personalized rootfs failed read-only filesystem validation")
        dump = subprocess.run([debugfs, "-R", "dump -p /etc/openstick/seed.env %s" % _debugfs_quote(readback), str(target)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if dump.returncode or not readback.is_file() or _sha_file(readback) != _sha_file(seed_path):
            raise VerificationError("injected seed read-back SHA-256 mismatch")
        if _sha_file(original) != original_sha:
            raise VerificationError("generic rootfs image changed during injection")
        manifest = {"sensitive": True, "build_id": args.build, "seed_name": args.seed, "seed_sha256": _sha_file(seed_path), "rootfs_sha256": _sha_file(target), "verified_by": ["debugfs-injection", "e2fsck-read-only", "debugfs-readback-sha256"]}
        out = staging / "personal.json"
        out.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        os.chmod(out, 0o600)
        os.replace(staging, target_dir)
        registry = store.load_registry(root)
        entry = {"seed_name": args.seed, "seed_sha256": _sha_file(seed_path), "method": "inject",
                 "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(), "target": "build:" + args.build}
        registry.setdefault("seeds_applied", []).append(entry)
        try:
            store.save_registry(root, registry)
            store.append_event(root, {"at": entry["ts"], "type": "seed.applied", "seed_name": args.seed,
                                      "seed_sha256": entry["seed_sha256"], "method": "inject", "target": entry["target"]})
        except Exception:
            os.chmod(target_dir, 0o700)
            for path in target_dir.rglob("*"):
                if path.is_file(): os.chmod(path, 0o600)
                elif path.is_dir(): os.chmod(path, 0o700)
            shutil.rmtree(target_dir, ignore_errors=True)
            raise
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
    privileged = "" if args.host.startswith("root@") else "sudo "
    prepare = 'set -eu; umask 077; d=$(mktemp -d /run/openstick-seed.XXXXXX 2>/dev/null || mktemp -d /var/tmp/openstick-seed.XXXXXX); [ ! -L "$d" ]; chmod 700 "$d"; printf "%s\\n" "$d"'
    prepare_args = [ssh, "-o", "BatchMode=yes", args.host, prepare] if not privileged else [ssh, "-t", args.host, prepare]
    prepared = subprocess.run(prepare_args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    remote_dir = prepared.stdout.strip()
    if prepared.returncode or not re.fullmatch(r"/(?:run|var/tmp)/openstick-seed\.[A-Za-z0-9]+", remote_dir):
        print("REFUSED: could not create a private remote seed directory.", file=sys.stderr)
        return EXIT_REFUSED
    remote_tmp = remote_dir + "/seed.env"
    copied = subprocess.run([scp, "-q", str(seed_path), args.host + ":" + remote_tmp], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if copied.returncode:
        cleanup = "rm -rf -- " + remote_dir
        subprocess.run(prepare_args[:-1] + [cleanup], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        raise store.StoreError("could not transfer seed over SSH")
    remote = "set -eu; d=%s; trap 'rm -rf -- \"$d\"' EXIT HUP INT TERM; [ ! -L \"$d\" ] && [ ! -L \"$d/seed.env\" ]; %sinstall -o root -g root -m 600 \"$d/seed.env\" /etc/openstick/seed.env; %ssystemctl start openstick-seed.service; %sgrep -qx %s /etc/openstick/seed.applied" % (remote_dir, privileged, privileged, privileged, wanted_hash)
    ssh_args = [ssh, "-o", "BatchMode=yes", args.host, remote] if not privileged else [ssh, "-t", args.host, remote]
    started = subprocess.run(ssh_args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if started.returncode:
        raise store.StoreError("remote seed application failed")
    registry = store.load_registry(root)
    entry = {"seed_name": args.seed, "seed_sha256": wanted_hash, "method": "push",
             "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(), "target": "host:" + args.host}
    registry.setdefault("seeds_applied", []).append(entry)
    store.save_registry(root, registry)
    store.append_event(root, {"at": entry["ts"], "type": "seed.applied", "seed_name": args.seed,
                              "seed_sha256": wanted_hash, "method": "push", "target": entry["target"]})
    print("Seed transferred to the reachable stick; application service completed.")
    return EXIT_OK


def main(argv=None, *, usb_sysfs_root="/sys/bus/usb/devices", usb_device_root="/dev/bus/usb"):
    args = parser().parse_args(argv)
    args.home = str(resolve_home(args.home))
    try:
        if args.command == "doctor":
            return doctor(args, usb_sysfs_root=usb_sysfs_root, usb_device_root=usb_device_root)
        repo = Path(__file__).resolve().parents[2]
        if args.command == "verify":
            from . import verify_restore
            return verify_restore.verify_command(args, Path(args.home), repo)
        if args.command == "package" and args.package_command == "verify":
            from . import packages
            manifest = packages.verify_package(args.path)
            print("PASS package integrity: type=%s" % manifest["package_type"])
            return EXIT_OK
        if args.command == "image-set" and args.image_set_command == "verify":
            from . import imageset
            return imageset.verify_command(args.path)
        root = store.ensure_store(args.home)
        if args.command in ("backup", "flash", "restore"):
            session = edl_runner.Session(sysfs_root=usb_sysfs_root)
            with session:
                if args.command == "backup":
                    return backup_command(args, root, session)
                if args.command == "flash":
                    from . import flash as flash_module
                    return flash_module.flash_command(args, root, repo,
                                                      usb_sysfs_root=usb_sysfs_root,
                                                      session=session)
                from . import verify_restore
                return verify_restore.restore_command(args, root, repo,
                                                      usb_sysfs_root=usb_sysfs_root,
                                                      session=session)
        if args.command == "configure":
            if args.show: configure_module.show(root, args.seed); return EXIT_OK
            configure_module.configure(root, args.seed, args.profile); return EXIT_OK
        if args.command == "seed":
            if args.seed_command == "inject": return inject_seed(args, root)
            return push_seed(args, root)
        if args.command == "build":
            from . import build as build_module
            return build_module.build_command(args, root, Path(__file__).resolve().parents[2])
        if args.command == "package":
            from . import packages
            return packages.package_command(args, root, Path(__file__).resolve().parents[2])
        if args.command == "image-set":
            from . import imageset
            if args.image_set_command == "prepare":
                return imageset.prepare_command(args, root, Path(__file__).resolve().parents[2])
        if args.command == "wizard":
            from . import wizard
            return wizard.run(args, root, Path(__file__).resolve().parents[2])
        return device_command(args, root)
    except store.RefusedError as exc:
        print("REFUSED: " + str(exc), file=sys.stderr)
        return EXIT_REFUSED
    except KeyboardInterrupt:
        print("ERROR: operation interrupted; temporary staging removed. If a destructive flash or restore phase had started, inspect device state and run the matching flash --reconcile-state or restore --reconcile-state before any new write.", file=sys.stderr)
        return EXIT_ERROR
    except VerificationError as exc:
        print("FAIL verification: " + str(exc), file=sys.stderr)
        return EXIT_VERIFY
    except (store.StoreError, OSError, ValueError, json.JSONDecodeError) as exc:
        print("ERROR: " + str(exc), file=sys.stderr)
        return EXIT_ERROR
    except Exception as exc:
        if args.debug:
            traceback.print_exc(file=sys.stderr)
        else:
            print("ERROR: unexpected exception (%s)" % type(exc).__name__, file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
