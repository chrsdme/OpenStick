"""M1 command line, diagnostics, and device registry operations."""
import argparse
import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path

from . import store

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
        if entry.get("id") == ident:
            return entry
    return None


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


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "doctor":
            return doctor(args)
        root = store.ensure_store(args.home)
        return device_command(args, root)
    except store.RefusedError as exc:
        print("REFUSED: " + _redact(exc), file=sys.stderr)
        return EXIT_REFUSED
    except (store.StoreError, OSError, ValueError, json.JSONDecodeError) as exc:
        print("ERROR: " + _redact(exc), file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
