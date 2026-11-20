"""openstick easy: one linear, interactive install flow.

Steps (one function each, called in order by run()):
  1 tool check          5 build / prepare the image set
  2 wait for EDL        6 cold re-entry into EDL
  3 backup              7 one confirmation
  4 mode (default or custom)  8 flash (GPT, boot, userdata, preserved partitions)
                        9 finish

This is an ADDITIONAL command. The guarded commands (backup, flash, restore,
wizard, doctor) are not changed; easy mode reuses their functions.

Test-friendly knobs (all optional):
  OPENSTICK_EDL_DIR, PATH        which edl client and loader are used (shim edl)
  OPENSTICK_USB_SYSFS_ROOT, OPENSTICK_USB_DEVICE_ROOT, OPENSTICK_TEST_USB_ROOTS_DIR
                                 fake USB tree (same rules as the other commands)
  OPENSTICK_HOME or --home       private store
  OPENSTICK_EASY_POLL_TIMEOUT    seconds to wait for 05c6:9008 per attempt (default 120)
  OPENSTICK_EASY_POLL_INTERVAL   seconds between USB polls (default 1)
  OPENSTICK_EASY_ASSUME_REPLUGGED=1
                                 step 6 does not wait for the fake USB device to
                                 disappear first (a fixture cannot be unplugged)
Prompts use plain input() and getpass, so a pty-driven or piped stdin works:
  openstick --home DIR easy --image-set SET_DIR < answers.txt
Answers in order: [reuse backup 1|2], [mode 1|2 and custom values], YES.
"""
import argparse
import os
import shlex
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import datetime
from pathlib import Path

from . import store
from . import seed as seed_format
from . import configure as configure_module
from . import edl_runner

# F1: whether WRITE commands work in a later EDL call without a cold boot is not
# proven on hardware. True = tell the user to re-enter EDL cold before writing.
REENTER_EDL_AFTER_BACKUP = True

POLL_TIMEOUT_SECONDS = 120
POLL_INTERVAL_SECONDS = 1.0
WRITE_TIMEOUT_SECONDS = 7200
READ_TIMEOUT_SECONDS = edl_runner.FULL_READ_TIMEOUT_SECONDS
SECTOR = 512
SAMPLE_SECTORS = 2048  # userdata read-back: this many sectors at each end
PRESERVED = ("fsg", "sec", "fsc", "modemst1", "modemst2", "persist")
SUPPORTED_BOARD = "ufi103s"
GPT_PRIMARY_SECTORS, GPT_SECONDARY_SECTORS = 34, 33
EXIT_OK, EXIT_ERROR = 0, 1

# Stock signature of the supported layout (ufi103s-gpt-t-board6-v1). Derived from imageset.EXPECTED_PARTITIONS (boot,
# userdata), FLASH-PLAN.md / ROLLBACK.md (27 named partitions, firmware sizes, tz and tzbak adjacent) and
# tools/gen-gpt-tz1m.py. Other LBAs are NOT pinned here: the check that no unwritten partition moves compares the
# planned GPT-T with the board's own backup GPT instead.
STOCK_NAMES = frozenset((
    "sbl1", "rpm", "tz", "tzbak", "hyp", "aboot", "fsg", "sec", "fsc", "modemst1", "modemst2", "persist", "boot",
    "system", "cache", "userdata", "modem", "sbl1bak", "abootbak", "rpmbak", "hypbak", "pad", "misc", "ssd",
    "splash", "DDR", "recovery"))
STOCK_FIRMWARE_SECTORS = {"sbl1": 1024, "rpm": 1024, "tz": 1024, "tzbak": 1024, "hyp": 1024, "aboot": 2048}
STOCK_FIRMWARE_LBA = {"sbl1": 262144, "rpm": 268288, "tz": 270336, "tzbak": 271360, "hyp": 272384, "aboot": 264192}

# Path A chain, mirrored from tools/pathA/pathA-flash.sh (name: (LBA, sectors)). tz grows to 2048 sectors by consuming tzbak.
FW_REGIONS = {"sbl1": (262144, 1024), "rpm": (268288, 1024), "tz": (270336, 2048), "hyp": (272384, 1024), "aboot": (264192, 2048)}
FW_PHASE_ORDER = ("tz", "rpm", "sbl1", "hyp", "aboot")          # after the GPT phase, exactly as pathA-flash.sh
ABSORBED_NAMES = {"tzbak": "tz"}                                # backup partitions whose bytes a written region swallows
PAYLOAD_NAMES = ("boot", "userdata")
WRITTEN_NAMES = PAYLOAD_NAMES + FW_PHASE_ORDER
FORBIDDEN_PHASES = ("final", "reset")                           # never run by easy mode: cold boot is the only supported reboot
ALLOWED_EDL_OPERATIONS = ("rs", "ws", "rf")
# boot composite, mirrored from tools/flash/flash-board.sh (lk1st branch)
BOOT_COMPOSITE_LBA, BOOT_CAPACITY_SECTORS = 396384, 32768
LK2ND_PADDED_SHA256 = "d82f3535972869deb2f963ac33a33ea2cd01cbf1cbd661a4bcc23f491fbcbfde"
LK2ND_SIZE, LK2ND_BOOT_OFFSET = 524288, 0x80000
LK2ND_SOURCE = "out/step1/lk2nd-padded-512KiB.img"
# firmware package (private, outside the repo): fw/*.img, gpt/gpt-T-*.bin, manifest, SHA256SUMS
FW_PKG_MANIFEST = "easy-fw-manifest.env"
FW_PKG_BOARD, FW_PKG_EMMC_SECTORS = "ufi103s", 7634944
FW_PKG_FILES = tuple("fw/%s.img" % n for n in FW_PHASE_ORDER) + ("gpt/gpt-T-primary.bin", "gpt/gpt-T-secondary.bin", FW_PKG_MANIFEST)
INTERRUPT_NOTE = "Ctrl-C ignored: do not interrupt, wait. The stick is being written."


class EasyError(Exception):
    """An expected failure: printed without a traceback."""


class Context:
    """State shared by the steps; also what the failure message needs."""

    def __init__(self, args, home, repo, usb_sysfs_root, usb_device_root):
        self.args, self.home, self.repo = args, Path(home), Path(repo)
        self.usb_sysfs_root, self.usb_device_root = usb_sysfs_root, usb_device_root
        self.phase = "start"
        self.wrote = False
        self.session = None
        self.device_id = None
        self.backup_dir = None
        self.backup_manifest = None
        self.board = getattr(args, "board", None) or SUPPORTED_BOARD
        self.seed_data = None
        self.fw_pkg = None                        # verified firmware package
        self.flash_index = None                   # position of this run's entry in device["flashes"]

    @property
    def backup_id(self):
        return self.backup_dir.name if self.backup_dir else None

    def restore_command(self):
        return "openstick --home %s restore --device %s --backup %s" % (
            shlex.quote(str(self.home)), self.device_id, self.backup_id)


class Plan:
    """What step 8 will write, fully derived before the first write."""

    def __init__(self):
        self.set_dir = None
        self.primary = self.secondary = None      # Path of the planned GPT files
        self.boot = self.rootfs = None            # Path of the payload files
        self.boot_lba = self.userdata_lba = None
        self.partitions = {}                      # imageset manifest partitions
        self.preserved = []                       # (name, old_start, old_end, new_start, new_end)
        self.total_sectors = None
        self.layout_id = None
        self.board = None
        self.fw = None                            # verified firmware package (dict) or None
        self.regions = []                         # (label, lba, sectors, stock_sha, new_sha) in write order
        self.composite = None                     # bytes: lk2nd (512 KiB) + boot.img at 0x80000
        self.composite_sha256 = None
        self.composite_file = None


# ---------------------------------------------------------------- small helpers

def _say(text=""):
    print(text, flush=True)


def _ask(prompt, default=""):
    try:
        return configure_module._ask(prompt, default)
    except EOFError:
        raise EasyError("input ended while waiting for an answer")


def _env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return float(default)


def _sha_file(path, offset=0, length=None):
    import hashlib
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        stream.seek(offset)
        left = length
        while left is None or left > 0:
            block = stream.read(1024 * 1024 if left is None else min(1024 * 1024, left))
            if not block:
                break
            digest.update(block)
            if left is not None:
                left -= len(block)
    return digest.hexdigest()


class _Ticker:
    """Progress line while a long EDL call runs (dots, or MB on a terminal)."""

    def __init__(self, label, probe=None):
        self.label, self.probe = label, probe
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def __enter__(self):
        self._started = time.monotonic()
        self._printed = False
        if self.label:
            _say(self.label)
        self._thread.start()
        return self

    def _loop(self):
        tty = sys.stdout.isatty()
        while not self._stop.wait(5.0):
            seconds = int(time.monotonic() - self._started)
            extra = ""
            if self.probe:
                try:
                    extra = ", %d MB" % (self.probe() // (1024 * 1024))
                except OSError:
                    pass
            self._printed = True
            if tty:
                print("\r  %ds%s   " % (seconds, extra), end="", flush=True)
            else:
                print(".", end="", flush=True)

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join()
        if self._printed:
            print("", flush=True)
        return False


def _event(ctx, phase, outcome, **details):
    event = {"at": int(time.time()), "type": "easy.phase", "device_id": ctx.device_id,
             "phase": phase, "outcome": outcome}
    event.update(details)
    store.append_event(ctx.home, event)


# ---------------------------------------------------------------- step 1

def step1b_firmware_package(ctx):
    """Verify the private firmware package before anything touches the stick (no package: stop)."""
    ctx.phase = "firmware package"
    path = getattr(ctx.args, "fw_pkg", None)
    if not path:
        raise EasyError("no firmware package: pass --fw-pkg DIR (private package OUTSIDE the repo with fw/{tz,rpm,sbl1,hyp,aboot}.img, "
                        "gpt/gpt-T-{primary,secondary}.bin, %s and SHA256SUMS). Easy mode writes the full Path A chain or nothing. "
                        "Nothing was written." % FW_PKG_MANIFEST)
    ctx.fw_pkg = _load_fw_pkg(ctx.repo, path)
    _say("  firmware package: %s (SHA256SUMS and sizes verified)" % ctx.fw_pkg["dir"])


def step1_check_tools(ctx):
    ctx.phase = "tool check"
    _say("Step 1/9: checking tools")
    try:
        client = edl_runner.resolve_client()
    except (store.RefusedError, store.StoreError) as exc:
        raise EasyError("EDL client not found (%s). Install the edl client, then either put 'edl' on PATH "
                        "or set OPENSTICK_EDL_DIR to its directory." % exc)
    try:
        loader = edl_runner.resolve_loader()
    except (store.RefusedError, store.StoreError) as exc:
        raise EasyError("Firehose loader problem: %s. Run tools/fetch-firehose.sh, or copy the pinned "
                        "prog_emmc_firehose_8916.mbn into out/ or OPENSTICK_EDL_DIR. "
                        "(Easy mode cannot continue with a loader that does not match the pin.)" % exc)
    _say("  edl client: %s" % " ".join(client))
    _say("  loader: %s (pinned hash OK)" % loader)
    if ctx.board != SUPPORTED_BOARD:
        raise EasyError("only board %s is supported (got %s)" % (SUPPORTED_BOARD, ctx.board))


# ---------------------------------------------------------------- step 2 and 6

def _instructions(again):
    _say("")
    _say("Unplug the stick.")
    _say("Hold the button.")
    _say("Plug it in. Release the button after 2 seconds.")
    _say("Waiting for the stick (USB 05c6:9008)%s" % (" ..." if not again else " (second try) ..."))


def _poll_for_edl(ctx, timeout, require_replug):
    interval = _env_float("OPENSTICK_EASY_POLL_INTERVAL", POLL_INTERVAL_SECONDS)
    seen_gone = (not require_replug) or os.environ.get("OPENSTICK_EASY_ASSUME_REPLUGGED") == "1"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = edl_runner.preflight(ctx.usb_sysfs_root).state
        if state == "EDL-PRESENT":
            if seen_gone:
                print("", flush=True)
                return True
        else:
            seen_gone = True
        print(".", end="", flush=True)
        time.sleep(interval)
    print("", flush=True)
    return False


def _wait_for_edl(ctx, require_replug=False):
    timeout = _env_float("OPENSTICK_EASY_POLL_TIMEOUT", POLL_TIMEOUT_SECONDS)
    for attempt in (1, 2):
        _instructions(attempt == 2)
        if _poll_for_edl(ctx, timeout, require_replug):
            break
        _say("The stick did not show up in %d seconds." % timeout)
    else:
        raise EasyError("no EDL device (05c6:9008) appeared. Check the cable, hold the button while plugging in, then run openstick easy again.")
    from . import cli as common
    status, detail, hint = common._check_edl_usb(ctx.usb_sysfs_root, ctx.usb_device_root)
    if status == "FAIL":
        raise EasyError("USB access problem: %s. Fix: %s" % (detail, hint))
    _say("Stick found in EDL mode.")


def step2_wait_for_edl(ctx):
    ctx.phase = "wait for EDL"
    _say("")
    _say("Step 2/9: put the stick in EDL mode")
    _wait_for_edl(ctx)


def step6_reenter_edl(ctx):
    ctx.phase = "cold re-entry into EDL"
    _say("")
    _say("Step 6/9: the stick must be put in EDL mode again (writes need a fresh session)")
    _wait_for_edl(ctx, require_replug=True)


# ---------------------------------------------------------------- step 3

def _find_verified_backup(ctx, device):
    from . import flash as flash_module
    newest_first = dict(device, backups=list(reversed(device.get("backups", []))))
    try:
        return flash_module._verified_backup(ctx.home, newest_first)
    except store.RefusedError:
        return None, None


def _read_identity(ctx):
    """First EDL call: prints the Sahara block, gives the device id (serial)."""
    with tempfile.TemporaryDirectory(prefix="openstick-easy-id-") as temp:
        out = Path(temp) / "primary-gpt.bin"
        ctx.session.run(["rs", "0", str(GPT_PRIMARY_SECTORS), str(out)],
                        timeout=edl_runner.IDENTITY_READ_TIMEOUT_SECONDS, env=os.environ.copy(),
                        verbose=getattr(ctx.args, "verbose", False), debug=getattr(ctx.args, "debug", False))
        if not out.is_file() or out.stat().st_size != GPT_PRIMARY_SECTORS * SECTOR:
            raise EasyError("could not read the stick identity. Unplug, re-enter EDL and try again.")
    return ctx.session.identity["serial"]


def _backup_progress_probe(ctx):
    def probe():
        sizes = [p.stat().st_size for p in (ctx.home / "backups" / ctx.device_id).glob(".staging-*/emmc-full.bin")]
        return max(sizes) if sizes else 0
    return probe


def step3_backup(ctx):
    from . import cli as common
    ctx.phase = "backup"
    _say("")
    _say("Step 3/9: backup")
    ctx.session = edl_runner.Session(sysfs_root=ctx.usb_sysfs_root)
    ctx.device_id = _read_identity(ctx)
    registry = store.load_registry(ctx.home)
    device = common._find_device(registry, ctx.device_id)
    existing_dir, existing_manifest = (None, None)
    if device and device.get("backups"):
        existing_dir, existing_manifest = _find_verified_backup(ctx, device)
    if existing_dir is not None:
        _say("A verified backup of this stick already exists:")
        _say("  %s" % existing_dir)
        answer = "1" if ctx.args.yes else _ask("[1] reuse it  [2] make a new one", "1")
        if answer != "2":
            ctx.backup_dir, ctx.backup_manifest = existing_dir, existing_manifest
    if ctx.backup_dir is None:
        _say("Reading the full eMMC (about 3.9 GB, roughly 4 minutes). Do not unplug.")
        bargs = argparse.Namespace(device=getattr(ctx.args, "device", None), again=bool(device and device.get("backups")),
                                   paranoid=False, dry_run=False, verbose=getattr(ctx.args, "verbose", False),
                                   debug=getattr(ctx.args, "debug", False), board=ctx.board,
                                   show_serial=True)
        with _Ticker("Backup running (progress shows every few seconds)", _backup_progress_probe(ctx)):
            code = common.backup_command(bargs, ctx.home, ctx.session)
        if code != common.EXIT_OK:
            raise EasyError("backup did not complete")
        device = common._find_device(store.load_registry(ctx.home), ctx.device_id)
        ctx.backup_dir, ctx.backup_manifest = _find_verified_backup(ctx, device)
        if ctx.backup_dir is None:
            raise EasyError("the new backup could not be verified")
    _check_restore_state(ctx)
    _check_stock_signature(ctx)
    _say("Backup folder: %s" % ctx.backup_dir)
    _say("To go back to the original state later, run:")
    _say("  %s" % ctx.restore_command())
    _event(ctx, "backup", "ready", backup_id=ctx.backup_id)


# ---------------------------------------------------------------- step 4

def _ssh_key_candidates():
    return sorted(Path.home().glob(".ssh/*.pub"))


def _ask_ssh_key():
    candidates = _ssh_key_candidates()
    if candidates:
        pick = candidates[0]
        if _ask("Use SSH public key %s? [Y/n]" % pick, "y").lower() in ("y", "yes"):
            return configure_module.read_public_key_file(pick)
    path = _ask("SSH public key file (blank to skip)")
    return configure_module.read_public_key_file(path) if path else ""


def _custom_seed(ctx):
    import base64
    profile = _ask("Profile dev or prod", "dev")
    if profile not in ("dev", "prod"):
        raise EasyError("profile must be dev or prod")
    data = {"SEED_VERSION": "1", "PROFILE": profile}
    data["HOSTNAME"] = _ask("Hostname", "openstick")
    ssid = _ask("Wi-Fi SSID (blank to skip)")
    if ssid:
        data["WIFI_SSID_B64"] = base64.b64encode(ssid.encode("utf-8")).decode("ascii")
        psk = configure_module._ask_secret("Wi-Fi password (blank for an open network)")
        if psk:
            data["WIFI_PSK_B64"] = base64.b64encode(psk.encode("utf-8")).decode("ascii")
    apn = _ask("Cellular APN (blank to skip)")
    if apn:
        data["APN_B64"] = base64.b64encode(apn.encode("utf-8")).decode("ascii")
    key = _ask_ssh_key()
    if key:
        data["SSH_AUTHORIZED_KEYS"] = key
    elif profile == "prod" and _ask("A prod image without an SSH key has no remote access. Continue? [y/N]", "n").lower() not in ("y", "yes"):
        raise EasyError("stopped: add an SSH public key for a prod image")
    try:
        seed_format.validate(data)
    except seed_format.SeedError as exc:
        raise EasyError("invalid value: %s" % exc)
    return data


def step4_choose_mode(ctx):
    """Returns the profile to build; sets ctx.seed_data for custom mode."""
    ctx.phase = "mode"
    if ctx.args.image_set or ctx.args.build:
        return "dev"
    _say("")
    _say("Step 4/9: image mode")
    answer = "1" if ctx.args.yes else _ask("[1] default  [2] custom", "1")
    if answer != "2":
        _say("Default mode: dev profile with the built-in defaults (public default login, see config/defaults.env).")
        return "dev"
    if not (shutil.which("debugfs") and shutil.which("e2fsck")):
        raise EasyError("custom mode needs debugfs and e2fsck (package e2fsprogs). Install it and run again.")
    ctx.seed_data = _custom_seed(ctx)
    return ctx.seed_data["PROFILE"]


# ---------------------------------------------------------------- checks that run before any write

class _Terminated(EasyError):
    """SIGTERM or SIGHUP reached easy mode."""


def _backup_dump(ctx):
    return ctx.backup_dir / "emmc-full.bin"


def _backup_slice(ctx, first, count):
    with _backup_dump(ctx).open("rb") as dump:
        dump.seek(first * SECTOR)
        data = dump.read(count * SECTOR)
    if len(data) != count * SECTOR:
        raise EasyError("the backup is truncated at LBA %d. Nothing was written." % first)
    return data


def _backup_rows(ctx):
    """name -> (start, end) read from the backup's own primary GPT (not from the planned one)."""
    try:
        return _planned_rows(_backup_slice(ctx, 0, GPT_PRIMARY_SECTORS))
    except (struct.error, UnicodeDecodeError, ValueError):
        raise EasyError("the backup GPT cannot be read. Nothing was written.")


def _check_stock_signature(ctx):
    """The backup must look like the stock board the selected layout was made for; otherwise stop."""
    from . import imageset
    total = ctx.backup_manifest["sectors"]
    if total != imageset.EMMC_SECTORS or _backup_dump(ctx).stat().st_size != total * SECTOR:
        raise EasyError("the backup has %d sectors (file %d bytes); layout %s is for %d sectors. Nothing was written."
                        % (total, _backup_dump(ctx).stat().st_size, imageset.LAYOUT_ID, imageset.EMMC_SECTORS))
    rows = _backup_rows(ctx)
    if set(rows) != STOCK_NAMES:
        raise EasyError("the backup partition names do not match the stock signature of layout %s (missing: %s; extra: %s). "
                        "Nothing was written." % (imageset.LAYOUT_ID, ", ".join(sorted(STOCK_NAMES - set(rows))) or "none",
                                                  ", ".join(sorted(set(rows) - STOCK_NAMES)) or "none"))
    for name, want in imageset.EXPECTED_PARTITIONS.items():
        if rows[name] != want:
            raise EasyError("backup partition %s is at LBA %d..%d; layout %s needs %d..%d. Nothing was written."
                            % (name, rows[name][0], rows[name][1], imageset.LAYOUT_ID, want[0], want[1]))
    for name, lba in STOCK_FIRMWARE_LBA.items():
        if rows[name][0] != lba:
            raise EasyError("backup partition %s starts at LBA %d; the Path A chain (tools/pathA/pathA-flash.sh) needs %d. "
                            "Nothing was written." % (name, rows[name][0], lba))
    for name, sectors in STOCK_FIRMWARE_SECTORS.items():
        if rows[name][1] - rows[name][0] + 1 != sectors:
            raise EasyError("backup partition %s has %d sectors; the stock signature says %d. Nothing was written."
                            % (name, rows[name][1] - rows[name][0] + 1, sectors))
    if rows["tzbak"][0] != rows["tz"][1] + 1 or rows["tzbak"][0] != rows["tz"][0] + 1024:
        raise EasyError("backup tz and tzbak are not adjacent (need LBA(tzbak) == LBA(tz) + 1024). Nothing was written.")
    recorded = ctx.backup_manifest.get("partitions", {})
    for name, (start, end) in rows.items():
        row = recorded.get(name)
        if row is not None and (row.get("start_lba"), row.get("end_lba")) != (start, end):
            raise EasyError("the backup manifest and the backup GPT disagree about %s. Nothing was written." % name)
    return rows


def _check_tz_range(backup_rows, planned):
    """The ONE documented layout change (tools/gen-gpt-tz1m.py): tz grows to end where tzbak ended; tzbak is removed.
    Kept separate from the generic rule so that extending WRITTEN_NAMES can never exempt it."""
    want = (backup_rows["tz"][0], backup_rows["tzbak"][1])
    if planned.get("tz") != want:
        raise EasyError("the planned GPT-T moves or resizes tz (must be %d..%d = start of tz to end of tzbak, planned %s). "
                        "Nothing was written." % (want[0], want[1], "missing" if "tz" not in planned else "%d..%d" % planned["tz"]))
    if "tzbak" in planned:
        raise EasyError("the planned GPT-T keeps tzbak, but the documented layout removes it. Nothing was written.")


def _check_firmware_unchanged(backup_rows, planned):
    """Every partition easy mode does not rewrite BY CONTENT keeps its LBA range and size in the planned GPT-T.

    Boot and userdata (payload) and the PRESERVED partitions (rewritten by name, same size checked in _derive_plan) are
    exempt. The written firmware names (sbl1, rpm, hyp, aboot) are NOT exempt: their bytes change but their LBA range
    must not. tz is checked by _check_tz_range (tz..tzbak), tzbak is absorbed by tz."""
    exempt = set(PAYLOAD_NAMES) | set(PRESERVED) | set(ABSORBED_NAMES) | {"tz"}
    for name, old in backup_rows.items():
        if name in exempt:
            continue
        if planned.get(name) != old:
            raise EasyError("the planned GPT-T moves or resizes %s (backup %s, planned %s). Partitions that easy mode "
                            "does not move must keep their LBA range and size. Nothing was written."
                            % (name, "%d..%d" % old, "missing" if name not in planned else "%d..%d" % planned[name]))
    _check_tz_range(backup_rows, planned)
    extra = sorted(set(planned) - set(backup_rows))
    if extra:
        raise EasyError("the planned GPT-T adds partitions that are not in the backup (%s). Nothing was written." % ", ".join(extra))


def _check_payload_sizes(plan):
    """boot.img must fit the boot partition; rootfs.img the userdata partition."""
    for name, path, label in (("boot", plan.boot, "boot.img"), ("userdata", plan.rootfs, "rootfs.img")):
        capacity = plan.partitions[name]["sectors"] * SECTOR
        size = Path(path).stat().st_size
        if size > capacity:
            raise EasyError("%s is %d bytes, larger than the %s partition (%d bytes). Nothing was written."
                            % (label, size, name, capacity))


def _check_restore_state(ctx):
    """A restore that is not complete must be finished by the guarded restore before anything new is written."""
    from . import verify_restore
    path = verify_restore._state_path(ctx.home, ctx.device_id)
    if not (path.exists() or path.is_symlink()):
        return
    try:
        state = store._read_json(path, {})
    except Exception:
        state = None
    status = state.get("status") if isinstance(state, dict) else None
    if status != "complete":
        raise EasyError("a restore of this stick is not complete (state: %s). Finish it with the guarded restore first: %s. "
                        "Nothing was written." % (status if isinstance(status, str) else "unreadable", ctx.restore_command()))


def _check_identity(ctx, serial, observed):
    if serial != ctx.device_id or observed != ctx.backup_manifest.get("identity"):
        raise EasyError("this is not the stick that was backed up (identity differs). Nothing was written.")


def _check_live_gpt(ctx, temp):
    """Before the first write the stick's current GPT (primary and secondary) must equal the backup's."""
    total = ctx.backup_manifest["sectors"]
    for label, lba, count in (("primary", 0, GPT_PRIMARY_SECTORS), ("secondary", total - GPT_SECONDARY_SECTORS, GPT_SECONDARY_SECTORS)):
        live = _read_sectors(ctx, temp, lba, count)
        if live.read_bytes() != _backup_slice(ctx, lba, count):
            raise EasyError("the GPT (%s) on the stick differs from the backup's: the stick changed after the backup "
                            "(or an earlier flash is in place). Make a new backup. Nothing was written." % label)


# ---------------------------------------------------------------- firmware package, composite, region classification

def _parse_sums(text):
    """SHA256SUMS -> {relative path: sha256}. Refuses absolute paths, '..', backslashes and duplicates."""
    sums = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        digest, _sep, name = line.partition("  ")
        name = name[1:] if name.startswith("*") else name
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest) or not name:
            raise EasyError("firmware package: malformed SHA256SUMS line. Nothing was written.")
        parts = name.split("/")
        if name.startswith("/") or "\\" in name or ".." in parts or "" in parts or "." in parts:
            raise EasyError("firmware package: unsafe path %r in SHA256SUMS. Nothing was written." % name)
        if name in sums:
            raise EasyError("firmware package: %s listed twice in SHA256SUMS. Nothing was written." % name)
        sums[name] = digest
    return sums


def _check_pkg_dir(repo, path):
    """Same rule as BACKUP_DIR in flash-board.sh: outside the repo, and not a symlink (nor under one)."""
    raw = Path(path)
    if raw.is_symlink() or store._has_symlink_component(raw.absolute()):
        raise EasyError("--fw-pkg must not be a symlink or sit under one. Nothing was written.")
    resolved = raw.resolve()
    if not resolved.is_dir():
        raise EasyError("--fw-pkg %s is not a directory. Nothing was written." % path)
    repo = Path(repo).resolve()
    if resolved == repo or repo in resolved.parents or store._inside_git(resolved):
        raise EasyError("--fw-pkg must be OUTSIDE the repo (firmware blobs never enter the repo). Nothing was written.")
    return resolved


def _parse_manifest(text):
    data = {}
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            key, sep, value = line.partition("=")
            if not sep:
                raise EasyError("firmware package: malformed manifest line. Nothing was written.")
            data[key.strip()] = value.strip().strip('"')
    return data


def _load_fw_pkg(repo, path):
    """Verify the package BEFORE any write: every listed file against SHA256SUMS, every needed file present, sizes exact.

    Returns {"dir", "fw": {name: Path}, "gptP", "gptS", "manifest", "sha": {relative path: sha256}}. Blobs are never copied
    into the repo or the registry; only their hashes are recorded."""
    root = _check_pkg_dir(repo, path)
    sums_file = root / "SHA256SUMS"
    if sums_file.is_symlink() or not sums_file.is_file():
        raise EasyError("firmware package: SHA256SUMS is missing. Nothing was written.")
    sums = _parse_sums(sums_file.read_text(encoding="ascii", errors="replace"))
    missing = [name for name in FW_PKG_FILES if name not in sums]
    if missing:
        raise EasyError("firmware package: SHA256SUMS does not list %s. Nothing was written." % ", ".join(missing))
    for name, digest in sums.items():
        item = root / name
        if item.is_symlink() or store._has_symlink_component(item) or not item.is_file():
            raise EasyError("firmware package: %s is missing or is a symlink. Nothing was written." % name)
        if _sha_file(item) != digest:
            raise EasyError("firmware package: %s does not match SHA256SUMS. Nothing was written." % name)
    sizes = {"fw/%s.img" % n: FW_REGIONS[n][1] * SECTOR for n in FW_PHASE_ORDER}
    sizes.update({"gpt/gpt-T-primary.bin": GPT_PRIMARY_SECTORS * SECTOR, "gpt/gpt-T-secondary.bin": GPT_SECONDARY_SECTORS * SECTOR})
    for name, size in sizes.items():
        if (root / name).stat().st_size != size:
            raise EasyError("firmware package: %s is %d bytes, the region needs exactly %d. Nothing was written."
                            % (name, (root / name).stat().st_size, size))
    manifest = _parse_manifest((root / FW_PKG_MANIFEST).read_text(encoding="ascii", errors="replace"))
    return {"dir": root, "fw": {n: root / "fw" / (n + ".img") for n in FW_PHASE_ORDER},
            "gptP": root / "gpt" / "gpt-T-primary.bin", "gptS": root / "gpt" / "gpt-T-secondary.bin",
            "manifest": manifest, "sha": {name: sums[name] for name in FW_PKG_FILES}}


def _check_fw_manifest(pkg, board, emmc_sectors, layout_id):
    """Board id, EMMC_SECTORS and GPT-T layout id of the package must match the selected board and the verified backup.
    GPT-T, the loader pin and EMMC_SECTORS=7634944 are board #6 only."""
    m = pkg["manifest"]
    if board != FW_PKG_BOARD or m.get("BOARD") != board:
        raise EasyError("firmware package is for board %s, selected %s (only %s is supported). Nothing was written."
                        % (m.get("BOARD"), board, FW_PKG_BOARD))
    if m.get("EMMC_SECTORS") != str(emmc_sectors) or emmc_sectors != FW_PKG_EMMC_SECTORS:
        raise EasyError("firmware package EMMC_SECTORS=%s, the verified backup has %s (board #6 is %d). Nothing was written."
                        % (m.get("EMMC_SECTORS"), emmc_sectors, FW_PKG_EMMC_SECTORS))
    if m.get("LAYOUT_ID") != layout_id:
        raise EasyError("firmware package GPT-T layout id %s, the image set uses %s. Nothing was written."
                        % (m.get("LAYOUT_ID"), layout_id))


def _check_pkg_gpt(ctx, plan):
    """The GPT-T derived from the backup (gen-gpt-tz1m.py) is the source of truth: the image set's GPT and the package's
    gpt/gpt-T-*.bin must be BYTE-IDENTICAL to it."""
    with tempfile.TemporaryDirectory(prefix="openstick-easy-derive-") as temp:
        os.chmod(temp, 0o700)
        layout = _layout_from_backup(ctx, temp)
        derived = {"primary": (layout / "gpt-T-primary.bin").read_bytes(), "secondary": (layout / "gpt-T-secondary.bin").read_bytes()}
    for label, path in (("image set", plan.primary), ("firmware package", plan.fw["gptP"])):
        if path.read_bytes() != derived["primary"]:
            raise EasyError("the %s primary GPT-T is not byte-identical to the one derived from this stick's backup. Nothing was written." % label)
    for label, path in (("image set", plan.secondary), ("firmware package", plan.fw["gptS"])):
        if path.read_bytes() != derived["secondary"]:
            raise EasyError("the %s secondary GPT-T is not byte-identical to the one derived from this stick's backup. Nothing was written." % label)


def _lk2nd_path(repo):
    return Path(os.environ.get("LK2ND_IMG") or (Path(repo) / LK2ND_SOURCE))


def _build_composite(repo, boot_img, capacity_sectors):
    """lk2nd-padded-512KiB.img + boot.img at 0x80000 (flash-board.sh, lk1st branch). boot.img alone at offset 0 would
    erase lk2nd, so it is never written alone."""
    path = _lk2nd_path(repo)
    if path.is_symlink() or not path.is_file():
        raise EasyError("missing lk2nd image %s (same source as flash-board.sh). Nothing was written." % path)
    lk = path.read_bytes()
    if len(lk) != LK2ND_SIZE:
        raise EasyError("the lk2nd image must be exactly %d bytes. Nothing was written." % LK2ND_SIZE)
    import hashlib
    if hashlib.sha256(lk).hexdigest() != LK2ND_PADDED_SHA256:
        raise EasyError("lk2nd image sha256 does not match the pin %s. Nothing was written." % LK2ND_PADDED_SHA256)
    image = Path(boot_img).read_bytes()
    composite = lk + image
    if len(composite) % SECTOR:
        raise EasyError("boot.img is not a whole number of sectors. Nothing was written.")
    _check_composite_fits(len(composite), capacity_sectors)
    return composite


def _check_composite_fits(total, capacity_sectors):
    if total > capacity_sectors * SECTOR:
        raise EasyError("lk2nd + boot.img (%d bytes) does not fit the boot partition (%d bytes). Nothing was written."
                        % (total, capacity_sectors * SECTOR))


def _plan_firmware(ctx, plan):
    """Everything about the firmware chain that can be known before the first write (also used by offline mode)."""
    if plan.fw is None:
        raise EasyError("no firmware package. Nothing was written.")
    _check_fw_manifest(plan.fw, ctx.board, plan.total_sectors, plan.layout_id)
    _check_pkg_gpt(ctx, plan)
    boot = plan.partitions["boot"]
    if boot["start_lba"] != BOOT_COMPOSITE_LBA or boot["sectors"] != BOOT_CAPACITY_SECTORS:
        raise EasyError("the planned boot partition is not LBA %d x %d sectors. Nothing was written." % (BOOT_COMPOSITE_LBA, BOOT_CAPACITY_SECTORS))
    plan.composite = _build_composite(ctx.repo, plan.boot, boot["sectors"])
    import hashlib
    plan.composite_sha256 = hashlib.sha256(plan.composite).hexdigest()
    plan.regions = _region_table(ctx, plan)


def _backup_digest(ctx, first, last):
    import hashlib
    return hashlib.sha256(_backup_slice(ctx, first, last - first + 1)).hexdigest()


def _region_table(ctx, plan):
    """(label, lba, sectors, stock_sha, new_sha) for gptP, gptS, sbl1, rpm, tz, hyp, aboot. Stock hashes come from the board's
    own VERIFIED BACKUP (never from a package rollback/ dir). tz: stock = tz (1024) + tzbak (1024) from the backup."""
    rows = _backup_rows(ctx)
    total = plan.total_sectors
    secondary = total - GPT_SECONDARY_SECTORS
    table = [("gptP", 0, GPT_PRIMARY_SECTORS, _backup_digest(ctx, 0, GPT_PRIMARY_SECTORS - 1), _sha_file(plan.fw["gptP"])),
             ("gptS", secondary, GPT_SECONDARY_SECTORS, _backup_digest(ctx, secondary, total - 1), _sha_file(plan.fw["gptS"]))]
    for name in ("sbl1", "rpm", "tz", "hyp", "aboot"):
        lba, sectors = FW_REGIONS[name]
        if rows[name][0] != lba:
            raise EasyError("backup %s is at LBA %d, the Path A chain needs %d. Nothing was written." % (name, rows[name][0], lba))
        last = rows["tzbak"][1] if name == "tz" else rows[name][1]
        if name == "tz" and rows["tzbak"][0] != lba + 1024:
            raise EasyError("LBA(tzbak) != LBA(tz) + 1024 in the backup GPT. Nothing was written.")
        if last - lba + 1 != sectors:
            raise EasyError("backup %s covers %d sectors, the chain writes %d. Nothing was written." % (name, last - lba + 1, sectors))
        table.append((name, lba, sectors, _backup_digest(ctx, lba, last), _sha_file(plan.fw["fw"][name])))
    return table


def _classify_regions(ctx, temp, plan):
    """Read every region and say stock / new / UNKNOWN (like state() in pathA-flash.sh; stock = the backup)."""
    states = {}
    for label, lba, sectors, stock, new in plan.regions:
        digest = _sha_file(_read_sectors(ctx, temp, lba, sectors))
        states[label] = "stock" if digest == stock else "new" if digest == new else "UNKNOWN"
        _say("  %-5s LBA %d x %d : %s" % (label, lba, sectors, states[label]))
    return states


def _check_regions_stock(ctx, temp, plan):
    states = _classify_regions(ctx, temp, plan)
    bad = {k: v for k, v in states.items() if v != "stock"}
    if bad:
        raise EasyError("before the first write every region must be the stock bytes of the verified backup; found %s. "
                        "A chain that was already (partly) written is handled by the guided restore, not resumed: %s. "
                        "Nothing was written." % (", ".join("%s=%s" % kv for kv in sorted(bad.items())), ctx.restore_command()))


def _final_check(ctx, temp, plan):
    """Read-only equivalent of `final` in pathA-flash.sh WITHOUT the reset: all regions and both GPTs == NEW IMAGES."""
    _say("  final check (read only, no reset): reading every firmware region and both GPTs back")
    states = _classify_regions(ctx, temp, plan)
    bad = {k: v for k, v in states.items() if v != "new"}
    if bad:
        raise EasyError("final check: %s, expected new. Do not boot. Use the guided restore." % ", ".join("%s=%s" % kv for kv in sorted(bad.items())))
    _say("  ALL REGIONS == NEW IMAGES")


class _SigintShield:
    """Ctrl-C is ignored while the stick is written.

    SIGINT is BLOCKED (not handled): a blocked signal mask is inherited across fork and exec, so the edl client and its
    children also do not see the terminal's Ctrl-C (a handler would be reset by exec and the client would die mid-write).
    A watcher thread consumes pending SIGINTs and prints the note; leftovers are drained before the mask is restored."""

    def __enter__(self):
        signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT})
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._watch, daemon=True)
        self._thread.start()
        return self

    def _watch(self):
        while not self._stop.is_set():
            if signal.sigtimedwait({signal.SIGINT}, 0.2) is not None:
                _say(INTERRUPT_NOTE)

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join()
        while signal.sigtimedwait({signal.SIGINT}, 0) is not None:
            _say(INTERRUPT_NOTE)
        signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGINT})
        return False


def _descendants():
    """Pids of every process below this one (from /proc; Linux only)."""
    children = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry / "stat").read_text().rpartition(")")[2].split()
        except OSError:
            continue
        if len(fields) > 1 and fields[0] != "Z":
            children.setdefault(int(fields[1]), []).append(int(entry.name))
    found, queue = [], [os.getpid()]
    while queue:
        for pid in children.get(queue.pop(), []):
            found.append(pid)
            queue.append(pid)
    return found


def _kill_children():
    """No edl client (or helper of it) outlives easy mode."""
    for sig, wait in ((signal.SIGTERM, 3.0), (signal.SIGKILL, 2.0)):
        pids = _descendants()
        if not pids:
            return
        for pid in pids:
            try:
                os.kill(pid, sig)
            except OSError:
                pass
        deadline = time.monotonic() + wait
        while _descendants() and time.monotonic() < deadline:
            time.sleep(0.05)


def _on_terminate(signum, frame):
    raise _Terminated("terminated by signal %d" % signum)


# ---------------------------------------------------------------- step 5

def _new_build_id(before, after):
    old = {b.get("build_id") for b in before.get("builds", [])}
    new = [b["build_id"] for b in after.get("builds", []) if b.get("build_id") not in old]
    if len(new) != 1:
        raise EasyError("could not tell which build was just registered")
    return new[0]


def _build_image(ctx, profile):
    from . import build as build_module
    if not shutil.which("sudo"):
        raise EasyError("sudo is needed for the rootfs build stage. Install sudo, or build elsewhere and use --image-set DIR.")
    _say("The build asks for your sudo password once (rootfs stage). Enter it when prompted.")
    before = store.load_registry(ctx.home)
    bargs = argparse.Namespace(profile=profile, build_board=ctx.board, bake_user_env=False, dry_run=False)
    code = build_module.build_command(bargs, ctx.home, ctx.repo)
    if code:
        raise EasyError("build failed")
    return _new_build_id(before, store.load_registry(ctx.home))


def _personalize(ctx, build_id):
    """Inject the custom seed into a copy of the build rootfs; returns its path."""
    from . import cli as common
    name = "easy-" + datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = ctx.home / "seeds" / (name + ".seed")
    store._atomic_private_write(path, seed_format.serialize(ctx.seed_data).encode("utf-8"))
    iargs = argparse.Namespace(build=build_id, seed=name, dry_run=False)
    if common.inject_seed(iargs, ctx.home) != common.EXIT_OK:
        raise EasyError("seed injection failed")
    return ctx.home / "builds" / build_id / ("personal-" + name) / "rootfs.img"


def _layout_from_backup(ctx, directory):
    """GPT-T files derived from the board's own backup GPT (tools/gen-gpt-tz1m.py)."""
    primary = Path(directory) / "backup-primary-gpt.bin"
    with (ctx.backup_dir / "emmc-full.bin").open("rb") as dump, primary.open("wb") as out:
        out.write(dump.read(GPT_PRIMARY_SECTORS * SECTOR))
    layout = Path(directory) / "layout"
    result = subprocess.run([sys.executable, "-I", str(ctx.repo / "tools" / "gen-gpt-tz1m.py"),
                             "--primary", str(primary), "--emmc", str(ctx.backup_manifest["sectors"]),
                             "--outdir", str(layout)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode:
        raise EasyError("could not derive the GPT-T layout from this stick's backup: " + result.stderr.strip()[-200:])
    return layout


def _prepare_set(ctx, build_id):
    from . import imageset
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = ctx.home / "images" / ("easy-%s-%s" % (build_id, stamp))
    with tempfile.TemporaryDirectory(prefix="openstick-easy-layout-", dir=str(ctx.home / "images")) as temp:
        os.chmod(temp, 0o700)
        layout = _layout_from_backup(ctx, temp)
        pargs = argparse.Namespace(build=build_id, layout=str(layout), output=str(output))
        if imageset.prepare_command(pargs, ctx.home, ctx.repo):
            raise EasyError("image set preparation failed")
    return output


def _planned_rows(primary_bytes):
    """name -> (start, end) from the planned GPT-T primary (already CRC-checked by imageset)."""
    elba, count, esize = struct.unpack_from("<QII", primary_bytes, SECTOR + 72)
    table = primary_bytes[elba * SECTOR:elba * SECTOR + count * esize]
    rows = {}
    for index in range(count):
        entry = table[index * esize:(index + 1) * esize]
        if len(entry) < 128 or entry[:16] == b"\0" * 16:
            continue
        start, end = struct.unpack_from("<QQ", entry, 32)
        rows[entry[56:128].decode("utf-16le").rstrip("\0")] = (start, end)
    return rows


def _derive_plan(ctx, set_dir, rootfs_override):
    """E-8: preserved partitions are matched BY NAME between the backup GPT and the planned GPT-T.

    If a name or size cannot be matched, nothing is invented: easy mode stops
    before the first write (NEEDS-HARDWARE for that board/layout).
    """
    from . import imageset
    manifest = imageset._verify_set(set_dir)
    plan = Plan()
    plan.set_dir = Path(set_dir)
    plan.primary = plan.set_dir / "layout" / "gpt-T-primary.bin"
    plan.secondary = plan.set_dir / "layout" / "gpt-T-secondary.bin"
    plan.boot = plan.set_dir / "payload" / "boot.img"
    plan.rootfs = Path(rootfs_override) if rootfs_override else plan.set_dir / "payload" / "rootfs.img"
    plan.partitions = manifest["partitions"]
    plan.boot_lba = plan.partitions["boot"]["start_lba"]
    plan.userdata_lba = plan.partitions["userdata"]["start_lba"]
    plan.layout_id, plan.board = manifest["layout_id"], manifest["board"]
    plan.total_sectors = ctx.backup_manifest["sectors"]
    if plan.total_sectors != imageset.EMMC_SECTORS:
        raise EasyError("this stick has %d sectors; the GPT-T layout is for %d. Not writing." % (plan.total_sectors, imageset.EMMC_SECTORS))
    if plan.board != ctx.board:
        raise EasyError("the image set is for board %s, not %s. Nothing was written." % (plan.board, ctx.board))
    backup_rows = _check_stock_signature(ctx)
    _check_payload_sizes(plan)
    if rootfs_override:
        imageset._validate_ext4(plan.rootfs, plan.partitions["userdata"]["sectors"] * SECTOR)
    planned = _planned_rows(plan.primary.read_bytes())
    _check_firmware_unchanged(backup_rows, planned)
    backup_parts = ctx.backup_manifest["partitions"]
    written = [(plan.partitions[n]["start_lba"], plan.partitions[n]["end_lba"]) for n in ("boot", "userdata")]
    for name in PRESERVED:
        new, old = planned.get(name), backup_parts.get(name)
        if new is None or old is None:
            raise EasyError("NEEDS-HARDWARE: cannot derive where %s goes (missing in %s). Not writing."
                            % (name, "the planned GPT-T" if new is None else "the backup GPT"))
        old = (old["start_lba"], old["end_lba"])
        if new[1] - new[0] != old[1] - old[0]:
            raise EasyError("NEEDS-HARDWARE: %s changes size in the planned GPT-T. Not writing." % name)
        if any(new[0] <= end and start <= new[1] for start, end in written):
            raise EasyError("NEEDS-HARDWARE: %s overlaps a payload range in the planned GPT-T. Not writing." % name)
        plan.preserved.append((name, old[0], old[1], new[0], new[1]))
    plan.fw = ctx.fw_pkg
    _plan_firmware(ctx, plan)
    return plan


def step5_prepare_images(ctx, profile):
    from . import imageset
    ctx.phase = "build"
    _say("")
    _say("Step 5/9: image")
    rootfs_override = None
    if ctx.args.image_set:
        set_dir = Path(ctx.args.image_set)
        _say("Using the prepared image set: %s" % set_dir)
    else:
        build_id = ctx.args.build or _build_image(ctx, profile)
        if ctx.seed_data:
            rootfs_override = _personalize(ctx, build_id)
        ctx.phase = "image set"
        set_dir = _prepare_set(ctx, build_id)
    ctx.phase = "image set"
    return _derive_plan(ctx, set_dir, rootfs_override)


# ---------------------------------------------------------------- step 7

def step7_confirm(ctx, plan):
    ctx.phase = "confirmation"
    _say("")
    _say("Step 7/9: confirmation")
    _say("About to WRITE to the stick (device %s):" % ctx.device_id)
    _say("  board    : %s, layout %s (backup matches its stock signature: %d sectors, %d partitions)"
         % (ctx.board, plan.layout_id, plan.total_sectors, len(STOCK_NAMES)))
    _say("  GPT      : planned GPT-T, primary and secondary (identical to the one derived from the backup)")
    _say("  firmware : %s, in this order after the GPT (package %s, hashes verified)" % (", ".join(FW_PHASE_ORDER), plan.fw["dir"]))
    _say("  boot     : composite lk2nd (%d bytes, sha256 %s...) + %s at 0x%x, %d bytes total, at LBA %d, sha256 %s"
         % (LK2ND_SIZE, LK2ND_PADDED_SHA256[:8], plan.boot.name, LK2ND_BOOT_OFFSET, len(plan.composite), plan.boot_lba, plan.composite_sha256))
    _say("  reboot   : no reset is ever sent; you do a cold boot at the end")
    _say("  userdata : %s, %d bytes, at LBA %d" % (plan.rootfs.name, plan.rootfs.stat().st_size, plan.userdata_lba))
    _say("  kept     : %s (verified against the backup by name, rewritten only if different)"
         % ", ".join("%s %d->%d" % (n, o, nw) for n, o, _, nw, _ in plan.preserved))
    _say("  backup   : %s" % ctx.backup_dir)
    if ctx.args.yes:
        _say("--yes given: continuing.")
        return
    try:
        answer = input("Type YES to start writing: ")
    except EOFError:
        raise EasyError("input ended; nothing was written")
    if answer.strip() != "YES":
        raise EasyError("not confirmed; nothing was written")


# ---------------------------------------------------------------- step 8

def _check_edl_args(args):
    """Easy mode only reads and writes. No reset (the `final` phase of Path A is not ported), no other operation."""
    if not args or str(args[0]) not in ALLOWED_EDL_OPERATIONS or any("reset" in str(item).lower() for item in args):
        raise EasyError("refusing the EDL call %r: easy mode only runs rs, ws and rf, never a reset. Cold boot is the only supported reboot." % (list(map(str, args)),))


def _edl(ctx, args, timeout):
    _check_edl_args(args)
    return ctx.session.run(args, timeout=timeout, env=os.environ.copy(),
                           verbose=getattr(ctx.args, "verbose", False), debug=getattr(ctx.args, "debug", False))


def _read_sectors(ctx, temp, lba, count):
    out = Path(temp) / "readback.bin"
    out.unlink(missing_ok=True)
    _edl(ctx, ["rs", str(lba), str(count), str(out)], READ_TIMEOUT_SECONDS)
    if not out.is_file() or out.stat().st_size != count * SECTOR:
        raise EasyError("read-back returned the wrong size")
    return out


def _check_equal(label, live, live_off, other, other_off, nbytes):
    if _sha_file(live, live_off, nbytes) != _sha_file(other, other_off, nbytes):
        raise EasyError("read-back mismatch in %s" % label)


def _write_and_check(ctx, temp, label, lba, path, whole):
    """ws one file, then read back all of it (or its first and last SAMPLE_SECTORS)."""
    sectors = Path(path).stat().st_size // SECTOR
    _say("  writing %s: %d sectors at LBA %d" % (label, sectors, lba))
    with _Ticker(None):
        _edl(ctx, ["ws", str(lba), str(path), "--sectorsize=512"], WRITE_TIMEOUT_SECONDS)
        if whole or sectors <= 2 * SAMPLE_SECTORS:
            spans = [(0, sectors)]
        else:
            spans = [(0, SAMPLE_SECTORS), (sectors - SAMPLE_SECTORS, SAMPLE_SECTORS)]
        for offset, count in spans:
            live = _read_sectors(ctx, temp, lba + offset, count)
            _check_equal(label, live, 0, path, offset * SECTOR, count * SECTOR)
    _say("  %s written and read back%s" % (label, "" if whole else " (first and last 1 MiB)"))


def _same_digest(got, want):
    return got == want


def _put(ctx, temp, label, lba, path, sectors):
    """Mirror of put() in tools/pathA/fwlib.sh: --sectorsize=512, never --lun, a --skipwrite dry pass first, then the
    write, then a FULL read-back and sha256 compare; the first mismatch stops everything.

    Differs from _write_and_check: that one has no dry pass and (userdata only) reads back just the first and last MiB."""
    path = Path(path)
    if path.stat().st_size != sectors * SECTOR:
        raise EasyError("%s: %s is not exactly %d sectors. Nothing was written." % (label, path.name, sectors))
    want = _sha_file(path)
    _say("  %s: LBA %d x %d sectors, expected sha256 %s" % (label, lba, sectors, want))
    with _Ticker(None):
        _edl(ctx, ["ws", str(lba), str(path), "--sectorsize=512", "--skipwrite"], WRITE_TIMEOUT_SECONDS)
        _say("  dry run %s OK (nothing written)" % label)
        _say("  writing %s: %d sectors at LBA %d" % (label, sectors, lba))
        _edl(ctx, ["ws", str(lba), str(path), "--sectorsize=512"], WRITE_TIMEOUT_SECONDS)
        got = _sha_file(_read_sectors(ctx, temp, lba, sectors))
        if not _same_digest(got, want):
            raise EasyError("READ-BACK MISMATCH in %s (got %s). Do not boot. Use the guided restore." % (label, got))
    _say("  %s written and fully read back (sha256 %s)" % (label, want))


def _phase(ctx, name):
    if name in FORBIDDEN_PHASES:
        raise EasyError("phase %r is not part of easy mode (no final, no reset; cold boot only)" % name)
    ctx.phase = name
    _event(ctx, name, "started")


def _restore_preserved(ctx, temp, plan):
    """Each preserved partition goes to its NEW planned position, BY NAME, from the backup."""
    dump = ctx.backup_dir / "emmc-full.bin"
    for name, old_start, old_end, new_start, new_end in plan.preserved:
        count = old_end - old_start + 1
        live = _read_sectors(ctx, temp, new_start, count)
        if _sha_file(live) == _sha_file(dump, old_start * SECTOR, count * SECTOR):
            _say("  %s already matches the backup at LBA %d" % (name, new_start))
            continue
        piece = Path(temp) / ("restore-" + name + ".bin")
        with dump.open("rb") as source, piece.open("wb") as out:
            source.seek(old_start * SECTOR)
            out.write(source.read(count * SECTOR))
        ctx.wrote = True
        _write_and_check(ctx, temp, name, new_start, piece, True)


def _verify_full(ctx, temp, plan):
    """Full read of the eMMC; everything outside the written ranges must equal the backup."""
    _say("  full verification: reading the whole eMMC (about 4 minutes)")
    live = Path(temp) / "full-after.bin"
    with _Ticker(None):
        _edl(ctx, ["rf", str(live)], READ_TIMEOUT_SECONDS)
    dump = ctx.backup_dir / "emmc-full.bin"
    if live.stat().st_size != dump.stat().st_size:
        raise EasyError("full read has the wrong size")
    total = plan.total_sectors
    segments = [(0, plan.fw["gptP"]), (total - GPT_SECONDARY_SECTORS, plan.fw["gptS"]),
                (plan.boot_lba, plan.composite_file), (plan.userdata_lba, plan.rootfs)]
    for name in FW_PHASE_ORDER:
        segments.append((FW_REGIONS[name][0], plan.fw["fw"][name]))
    for name, old_start, old_end, new_start, new_end in plan.preserved:
        if new_start != old_start:
            segments.append((new_start, None, name, old_start, old_end))
    spans = []
    for item in segments:
        if len(item) == 2:
            spans.append((item[0], item[1].stat().st_size // SECTOR, item[1], 0))
        else:
            spans.append((item[0], item[4] - item[3] + 1, dump, item[3] * SECTOR))
    spans.sort(key=lambda span: span[0])
    cursor = 0
    for lba, count, source, source_off in spans + [(total, 0, None, 0)]:
        if lba > cursor:
            _check_equal("untouched range at LBA %d" % cursor, live, cursor * SECTOR, dump, cursor * SECTOR, (lba - cursor) * SECTOR)
        if count:
            _check_equal("range at LBA %d" % lba, live, lba * SECTOR, source, source_off, count * SECTOR)
        cursor = max(cursor, lba + count)
    _say("  full verification passed")


# Firmware partitions the guarded restore classifies (tools/installer/verify_restore.py _known_rollback_state).
# fw file, stock file, partition name.
_FW_EVIDENCE = (("sbl1.img", "stock-sbl1.bin", "sbl1"), ("rpm.img", "stock-rpm.bin", "rpm"),
                ("tz.img", "stock-tz-tzbak.bin", "tz"), ("hyp.img", "stock-hyp.bin", "hyp"),
                ("aboot.img", "stock-aboot.bin", "aboot"))


def _private_file(path, data):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    store._atomic_private_write(path, data)


def _sums_lines(root, skip):
    lines = []
    for item in sorted(Path(root).rglob("*")):
        if item.is_file() and not item.is_symlink() and item != skip:
            lines.append("%s  %s" % (_sha_file(item), item.relative_to(root).as_posix()))
    return "\n".join(lines) + "\n"


def _build_rollback_evidence(ctx, plan):
    """Private evidence set that the guarded `restore` accepts, describing what THIS easy flash does.

    Same layout the guarded flash records for its image set: pathA/{gpt,fw,rollback} plus two SHA256SUMS files.
    Easy mode writes no firmware (aboot, sbl1, rpm, tz, hyp), so for each firmware range of the PLANNED GPT-T the
    "new" file and the "stock" file are the same bytes: the backup's bytes at that range. That is true only if no easy
    write touches the range; otherwise nothing is recorded and easy mode stops before the first write.
    """
    dump = ctx.backup_dir / "emmc-full.bin"
    total = plan.total_sectors
    planned = _planned_rows(plan.primary.read_bytes())
    written = [(0, GPT_PRIMARY_SECTORS - 1), (total - GPT_SECONDARY_SECTORS, total - 1),
               (plan.boot_lba, plan.partitions["boot"]["end_lba"]),
               (plan.userdata_lba, plan.partitions["userdata"]["end_lba"])]
    written += [(new_start, new_end) for _n, _a, _b, new_start, new_end in plan.preserved]
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    root = ctx.home / "images" / ("evidence-%s-%s" % (ctx.device_id[-8:], stamp))
    root.mkdir(mode=0o700)
    patha = root / "pathA"
    with dump.open("rb") as source:
        def backup_bytes(first, last):
            source.seek(first * SECTOR)
            data = source.read((last - first + 1) * SECTOR)
            if len(data) != (last - first + 1) * SECTOR:
                raise EasyError("the backup is truncated at LBA %d" % first)
            return data
        primary, secondary = backup_bytes(0, GPT_PRIMARY_SECTORS - 1), backup_bytes(total - GPT_SECONDARY_SECTORS, total - 1)
        _private_file(patha / "gpt" / "gpt-T-primary.bin", plan.primary.read_bytes())
        _private_file(patha / "gpt" / "gpt-T-secondary.bin", plan.secondary.read_bytes())
        _private_file(patha / "rollback" / "gpt-orig-primary.bin", primary)
        _private_file(patha / "rollback" / "gpt-orig-secondary.bin", secondary)
        for fw_name, stock_name, part in _FW_EVIDENCE:
            span = planned.get(part)
            if span is None:
                raise EasyError("NEEDS-HARDWARE: %s is missing in the planned GPT-T, so no rollback evidence can be recorded. Not writing." % part)
            if any(span[0] <= end and start <= span[1] for start, end in written):
                raise EasyError("NEEDS-HARDWARE: %s overlaps an easy-mode write range, so its content is not known. Not writing." % part)
            # "new" = the package image that easy mode writes; "stock" = the backup's bytes at the planned range (tz: tz + tzbak)
            _private_file(patha / "fw" / fw_name, plan.fw["fw"][part].read_bytes())
            _private_file(patha / "rollback" / stock_name, backup_bytes(*span))
    _private_file(patha / "SHA256SUMS", _sums_lines(patha, patha / "SHA256SUMS").encode("ascii"))
    _private_file(root / "SHA256SUMS", _sums_lines(root, root / "SHA256SUMS").encode("ascii"))
    return root


def _update_flash_entry(ctx, **changes):
    from . import cli as common
    registry = store.load_registry(ctx.home)
    device = common._find_device(registry, ctx.device_id)
    entry = device["flashes"][ctx.flash_index]
    phase = changes.pop("phase", None)
    if phase:
        entry["phases"].append(phase)
    entry.update(changes)
    store.save_registry(ctx.home, registry)


def _flash_event(ctx, phase, outcome, **details):
    event = {"at": int(time.time()), "type": "flash.phase", "device_id": ctx.device_id,
             "phase": phase, "outcome": outcome, "mode": "easy"}
    event.update(details)
    store.append_event(ctx.home, event)


def _archive_completed_restore_state(ctx):
    """A restore that already ran to completion refuses to run again for the same backup ("will not replay destructive
    phases"). A NEW flash makes that record stale: without this, flash -> restore -> flash again -> restore is refused.
    Only a state whose status is exactly "complete" is moved aside (renamed, never deleted); in-progress, partial and
    reconciliation states are left alone so the guarded restore still sees them. The restore code is not changed."""
    from . import verify_restore
    path = verify_restore._state_path(ctx.home, ctx.device_id)
    if not path.exists():
        return
    state = store._read_json(path, {})
    if not isinstance(state, dict) or state.get("status") != "complete":
        return
    archived = path.with_name("restore-state.complete-%s.json" % datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
    os.rename(path, archived)
    _flash_event(ctx, "restore-state", "completed-restore-state-archived", archived=archived.name)


def _phase_order():
    return ["gpt"] + list(FW_PHASE_ORDER) + ["boot", "userdata", "preserved partitions", "final check"]


def _firmware_record(plan):
    """Hashes and the phase order only (blobs never enter the registry). Recorded BEFORE the first write."""
    return {"phase_order": _phase_order(),
            "new_sha256": {label: new for label, _l, _s, _stock, new in plan.regions},
            "stock_sha256": {label: stock for label, _l, _s, stock, _new in plan.regions},
            "composite_sha256": plan.composite_sha256, "composite_bytes": len(plan.composite),
            "lk2nd_sha256": LK2ND_PADDED_SHA256}


def _register_flash(ctx, plan):
    """Record the flash in the registry BEFORE the first write, in the form the guarded `restore` reads."""
    from . import cli as common
    evidence = _build_rollback_evidence(ctx, plan)
    _archive_completed_restore_state(ctx)
    registry = store.load_registry(ctx.home)
    device = common._find_device(registry, ctx.device_id)
    if not device:
        raise EasyError("the stick is not registered, so no flash record can be written. Nothing was written.")
    device.setdefault("flashes", []).append({"ts": int(time.time()), "image_set": str(evidence), "mode": "easy",
                                             "source_image_set": str(plan.set_dir), "phases": [],
                                             "outcome": "in-progress", "firmware": _firmware_record(plan)})
    ctx.flash_index = len(device["flashes"]) - 1
    store.save_registry(ctx.home, registry)
    _flash_event(ctx, "flash", "registered", evidence=str(evidence), firmware=_firmware_record(plan))


def _flash_phase_done(ctx, name):
    _update_flash_entry(ctx, phase={"name": name, "result": "easy-readback-pass", "verified": True})
    _flash_event(ctx, name, "easy-readback-pass", verified=True)


def step8_flash(ctx, plan):
    from . import flash as flash_module
    ctx.session = edl_runner.Session(sysfs_root=ctx.usb_sysfs_root)
    _say("")
    _say("Step 8/9: writing")
    with tempfile.TemporaryDirectory(prefix="openstick-easy-flash-", dir=str(ctx.home / "images")) as temp:
        os.chmod(temp, 0o700)
        ctx.phase = "identity check"
        _check_restore_state(ctx)
        _check_stock_signature(ctx)
        _check_payload_sizes(plan)
        serial, observed = flash_module._live_identity(None, None, os.environ.copy(), temp,
                                                       ctx.backup_manifest["partitions"], session=ctx.session)
        _check_identity(ctx, serial, observed)
        _say("  identity matches the backup")
        ctx.phase = "GPT check"
        _check_live_gpt(ctx, temp)
        _say("  the GPT on the stick equals the backup's")
        ctx.phase = "region check"
        _say("  classifying every firmware region against the backup")
        _check_regions_stock(ctx, temp, plan)
        plan.composite_file = Path(temp) / "boot-composite.img"
        plan.composite_file.write_bytes(plan.composite)
        with _SigintShield():
            _write_phases(ctx, temp, plan)
            _update_flash_entry(ctx, outcome="easy-pass-not-boot-verified")
            _event(ctx, "flash", "complete-not-boot-verified")


def _write_phases(ctx, temp, plan):
    _register_flash(ctx, plan)
    try:
        _phase(ctx, "gpt")
        ctx.wrote = True
        _put(ctx, temp, "gpt secondary", plan.total_sectors - GPT_SECONDARY_SECTORS, plan.fw["gptS"], GPT_SECONDARY_SECTORS)
        _put(ctx, temp, "gpt primary", 0, plan.fw["gptP"], GPT_PRIMARY_SECTORS)
        _flash_phase_done(ctx, "gpt")
        for name in FW_PHASE_ORDER:
            lba, sectors = FW_REGIONS[name]
            _phase(ctx, name)
            _put(ctx, temp, name, lba, plan.fw["fw"][name], sectors)
            _flash_phase_done(ctx, name)
        _phase(ctx, "boot")
        _put(ctx, temp, "boot", plan.boot_lba, plan.composite_file, len(plan.composite) // SECTOR)
        _flash_phase_done(ctx, "boot")
        _phase(ctx, "userdata")
        _write_and_check(ctx, temp, "userdata", plan.userdata_lba, plan.rootfs, False)
        _flash_phase_done(ctx, "userdata")
        _phase(ctx, "preserved partitions")
        _restore_preserved(ctx, temp, plan)
        _flash_phase_done(ctx, "preserved partitions")
        _phase(ctx, "final check")
        _final_check(ctx, temp, plan)
        _flash_phase_done(ctx, "final check")
        if ctx.args.verify_full:
            _phase(ctx, "full verification")
            _verify_full(ctx, temp, plan)
    except BaseException:
        _update_flash_entry(ctx, outcome="partial-failure-reconciliation-required",
                            phase={"name": ctx.phase, "result": "failed", "verified": False})
        _flash_event(ctx, ctx.phase, "failed", verified=False)
        raise


# ---------------------------------------------------------------- step 9

def step9_finish(ctx):
    ctx.phase = "finish"
    _say("")
    _say("Step 9/9: done writing. Boot is NOT verified yet.")
    _say("All firmware regions and both GPTs read back as the new images. No reset was sent.")
    _say("Cold boot is the only supported reboot: unplug USB, wait 30 s, plug in WITHOUT the button, then ssh.")
    data = ctx.seed_data or {}
    _say("How to reach it:")
    if data.get("HOSTNAME") or data.get("USB_NET_ADDR"):
        _say("  hostname %s, USB network address %s (from your answers)"
             % (data.get("HOSTNAME", "unset"), data.get("USB_NET_ADDR", "unset")))
    else:
        _say("  the address depends on the image configuration; see docs/INSTALLER.md")
    if not ctx.seed_data and not ctx.args.image_set:
        _say("  default mode keeps the public dev login from config/defaults.env; change the password at first login")
    _say("Your backup: %s" % ctx.backup_dir)
    _say("Restore command: %s" % ctx.restore_command())


# ---------------------------------------------------------------- failure and entry point

def _fail(ctx, reason, args):
    err = sys.stderr
    print("", file=err)
    print("FAILED in phase: %s" % ctx.phase, file=err)
    print("Reason: %s" % reason, file=err)
    if ctx.wrote:
        print("The board was being written. Its state is partial; do not retry blindly.", file=err)
    else:
        print("Nothing was written to the board.", file=err)
    if ctx.backup_dir:
        print("Backup: %s" % ctx.backup_dir, file=err)
        print("To go back: %s. First unplug 30 s and re-enter EDL." % ctx.restore_command(), file=err)
    else:
        print("No backup was made yet, so there is nothing to restore.", file=err)
    return EXIT_ERROR


def run(args, home, repo, *, usb_sysfs_root=None, usb_device_root=None):
    ctx = Context(args, home, repo, usb_sysfs_root,
                  usb_device_root or edl_runner.default_usb_device_root())
    from . import cli as common
    previous = {}
    for number in (signal.SIGTERM, signal.SIGHUP):
        try:
            previous[number] = signal.signal(number, _on_terminate)
        except ValueError:                        # not the main thread: keep the defaults
            pass
    try:
        return _run_steps(ctx, args, common)
    finally:
        _kill_children()
        for number, handler in previous.items():
            signal.signal(number, handler)


def _run_steps(ctx, args, common):
    try:
        step1_check_tools(ctx)
        if args.dry_run:
            _say("DRY RUN: tools are fine. Easy mode would now wait for EDL, back up, build, flash. Stopping.")
            return EXIT_OK
        step1b_firmware_package(ctx)
        step2_wait_for_edl(ctx)
        step3_backup(ctx)
        profile = step4_choose_mode(ctx)
        plan = step5_prepare_images(ctx, profile)
        if REENTER_EDL_AFTER_BACKUP:
            step6_reenter_edl(ctx)
        step7_confirm(ctx, plan)
        step8_flash(ctx, plan)
        step9_finish(ctx)
        return EXIT_OK
    except EasyError as exc:
        return _fail(ctx, str(exc), args)
    except KeyboardInterrupt:
        return _fail(ctx, "interrupted by the user", args)
    except EOFError:
        return _fail(ctx, "input ended unexpectedly", args)
    except (store.RefusedError, store.StoreError, common.VerificationError, OSError, ValueError) as exc:
        return _fail(ctx, str(exc), args)
    except Exception as exc:
        if getattr(args, "debug", False):
            traceback.print_exc(file=sys.stderr)
        return _fail(ctx, "unexpected error (%s); rerun with --debug for details" % type(exc).__name__, args)
