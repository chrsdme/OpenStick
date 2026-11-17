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
    plan.total_sectors = ctx.backup_manifest["sectors"]
    if plan.total_sectors != imageset.EMMC_SECTORS:
        raise EasyError("this stick has %d sectors; the GPT-T layout is for %d. Not writing." % (plan.total_sectors, imageset.EMMC_SECTORS))
    if rootfs_override:
        imageset._validate_ext4(plan.rootfs, plan.partitions["userdata"]["sectors"] * SECTOR)
    planned = _planned_rows(plan.primary.read_bytes())
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
    _say("  GPT      : planned GPT-T, primary and secondary")
    _say("  boot     : %s, %d bytes, at LBA %d" % (plan.boot.name, plan.boot.stat().st_size, plan.boot_lba))
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

def _edl(ctx, args, timeout):
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


def _phase(ctx, name):
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
    segments = [(0, plan.primary), (total - GPT_SECONDARY_SECTORS, plan.secondary),
                (plan.boot_lba, plan.boot), (plan.userdata_lba, plan.rootfs)]
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


def step8_flash(ctx, plan):
    from . import flash as flash_module
    ctx.session = edl_runner.Session(sysfs_root=ctx.usb_sysfs_root)
    _say("")
    _say("Step 8/9: writing")
    with tempfile.TemporaryDirectory(prefix="openstick-easy-flash-", dir=str(ctx.home / "images")) as temp:
        os.chmod(temp, 0o700)
        ctx.phase = "identity check"
        serial, observed = flash_module._live_identity(None, None, os.environ.copy(), temp,
                                                       ctx.backup_manifest["partitions"], session=ctx.session)
        if serial != ctx.device_id or observed != ctx.backup_manifest.get("identity"):
            raise EasyError("this is not the stick that was backed up (identity differs). Nothing was written.")
        _say("  identity matches the backup")
        _phase(ctx, "gpt")
        ctx.wrote = True
        _write_and_check(ctx, temp, "gpt secondary", plan.total_sectors - GPT_SECONDARY_SECTORS, plan.secondary, True)
        _write_and_check(ctx, temp, "gpt primary", 0, plan.primary, True)
        _phase(ctx, "boot")
        _write_and_check(ctx, temp, "boot", plan.boot_lba, plan.boot, True)
        _phase(ctx, "userdata")
        _write_and_check(ctx, temp, "userdata", plan.userdata_lba, plan.rootfs, False)
        _phase(ctx, "preserved partitions")
        _restore_preserved(ctx, temp, plan)
        if ctx.args.verify_full:
            _phase(ctx, "full verification")
            _verify_full(ctx, temp, plan)
    _event(ctx, "flash", "complete-not-boot-verified")


# ---------------------------------------------------------------- step 9

def step9_finish(ctx):
    ctx.phase = "finish"
    _say("")
    _say("Step 9/9: done writing. Boot is NOT verified yet.")
    _say("Unplug the stick, wait 30 seconds, plug it in WITHOUT the button.")
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
    try:
        step1_check_tools(ctx)
        if args.dry_run:
            _say("DRY RUN: tools are fine. Easy mode would now wait for EDL, back up, build, flash. Stopping.")
            return EXIT_OK
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
