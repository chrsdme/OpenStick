"""Fail-closed adapter for the proven Path A and fastboot first-install scripts."""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import store
from . import cli as common


PHASES = ("pre", "gpt", "tz", "rpm", "sbl1", "hyp", "aboot", "final", "first-install")
WRITE_PHASES = PHASES[1:]
IDENTITY_PARTS = ("fsg", "sec", "fsc")


def _hash(path):
    return common._sha_file(Path(path))


def _checked_sum_file(root, filename="SHA256SUMS", require_all=False):
    sums = Path(root) / filename
    if sums.is_symlink() or not sums.is_file():
        raise store.RefusedError("image set is missing a regular " + filename)
    seen = set()
    for line in sums.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9_./+-]+)", line)
        if not match:
            raise store.RefusedError("image set SHA256SUMS has an invalid entry")
        digest, rel = match.groups()
        path = Path(root) / rel
        if rel.startswith("/") or ".." in Path(rel).parts or path.is_symlink() or not path.is_file():
            raise store.RefusedError("image set contains a missing or unsafe hashed input")
        if rel in seen or _hash(path) != digest:
            raise store.RefusedError("image set input SHA-256 mismatch: " + rel)
        seen.add(rel)
    if not seen:
        raise store.RefusedError("image set SHA256SUMS is empty")
    if require_all:
        expected = {p.relative_to(root).as_posix() for p in Path(root).rglob("*")
                    if p.is_file() and not p.is_symlink() and p != sums}
        if not expected.issubset(seen):
            raise store.RefusedError("one or more image-set inputs are not covered by SHA256SUMS")
    return seen


def _live_identity(edl, env, temp):
    result = subprocess.run([edl, "getstorageinfo"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, timeout=30)
    if result.returncode:
        raise store.RefusedError("could not read live EDL identity")
    combined = result.stdout + "\n" + result.stderr
    serial = re.search(r"serial\s*=\s*([0-9a-fA-F]+)", combined)
    if not serial:
        raise store.RefusedError("EDL did not report a live device identity")
    dump = Path(temp) / "live-emmc.bin"
    result = subprocess.run([edl, "rf", str(dump)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=300)
    if result.returncode or not dump.is_file():
        raise store.RefusedError("could not read live EDL partition fingerprints")
    parts = common._parse_gpt(dump)
    return serial.group(1).lower(), {name: common._hash_range(dump, *parts[name]) for name in IDENTITY_PARTS}


def _verified_backup(home, device):
    matches = [b for b in device.get("backups", []) if "full-read-size" in b.get("verified_by", []) and "gpt-primary-and-backup-crc" in b.get("verified_by", [])]
    for backup in matches:
        path = Path(backup.get("path", ""))
        if path.is_symlink() or not path.is_dir():
            continue
        try:
            manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
            dump = path / "emmc-full.bin"
            sums = (path / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
            if (not dump.is_symlink() and dump.is_file() and _hash(dump) == manifest.get("sha256") == backup.get("sha256")
                    and sums == [manifest.get("sha256") + "  emmc-full.bin"]
                    and manifest.get("identity") == device.get("identity")):
                return path, manifest
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    raise store.RefusedError("a verified, hash-matching full backup for this device is required")


def _event(home, device_id, phase, outcome, **details):
    event = {"at": int(time.time()), "type": "flash.phase", "device_id": device_id, "phase": phase, "outcome": outcome}
    event.update(details)
    store.append_event(home, event)


def _make_flash_backup(dump_dir, manifest, destination):
    """Materialize the six named slices required by flash-board.sh, privately."""
    dump = Path(dump_dir) / "emmc-full.bin"
    parts = manifest.get("partitions", {})
    names = ("modemst1", "modemst2", "fsc", "fsg", "persist", "sec")
    destination = Path(destination)
    destination.mkdir(mode=0o700)
    sums = []
    for name in names:
        row = parts.get(name)
        if not isinstance(row, dict):
            raise store.RefusedError("verified backup lacks required partition " + name)
        start, end = row.get("start_lba"), row.get("end_lba")
        if not isinstance(start, int) or not isinstance(end, int) or start < 0 or end < start:
            raise store.RefusedError("verified backup has invalid geometry for " + name)
        target = destination / name
        left = (end - start + 1) * 512
        with dump.open("rb") as source, target.open("xb") as output:
            source.seek(start * 512)
            remaining = left
            while remaining:
                block = source.read(min(1024 * 1024, remaining))
                if not block:
                    raise store.RefusedError("verified backup is truncated at " + name)
                output.write(block)
                remaining -= len(block)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(target, 0o600)
        sums.append("%s  %s" % (_hash(target), name))
    sums_path = destination / "SHA256SUMS"
    sums_path.write_text("\n".join(sums) + "\n", encoding="ascii")
    os.chmod(sums_path, 0o600)
    return destination


def _write_state(path, status, phase):
    store._atomic_private_write(path, (json.dumps({"status": status, "phase": phase, "at": int(time.time())}) + "\n").encode())


def _invoke(command, env):
    print("Executing: " + " ".join(command))
    return subprocess.run(command, env=env).returncode


def flash_command(args, home, repo):
    image_input = Path(args.image_set).absolute()
    if image_input.is_symlink() or store._has_symlink_component(image_input):
        raise store.RefusedError("image-set path contains a symlink")
    if store._inside_git(image_input):
        raise store.RefusedError("flash image set must be outside every Git worktree")
    image_set = image_input.resolve()
    gpt_t_manifest = image_set / "manifest.json"
    if gpt_t_manifest.is_symlink():
        raise store.RefusedError("image-set manifest is a symlink")
    if gpt_t_manifest.is_file() and not gpt_t_manifest.is_symlink():
        try:
            manifest = json.loads(gpt_t_manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            raise store.RefusedError("image-set manifest is invalid")
        if manifest.get("format") == "openstick-gpt-t-image-set":
            from . import imageset
            imageset._verify_set(image_set)
            raise store.RefusedError("HARDWARE_REQUIRED: GPT-T images are mapped and verified offline, but no safe live-identity-preserving boot/userdata write and read-back path is validated")
        raise store.RefusedError("unsupported image-set manifest format")
    if any((image_set / rel).exists() for rel in ("boot.img", "rootfs.img", "payload/boot.img", "payload/rootfs.img",
                                                   "layout/gpt-T-primary.bin", "layout/gpt-T-secondary.bin")):
        raise store.RefusedError("mixed or unknown image-set layout; GPT-T boot.img/rootfs.img cannot enter the legacy GPT-B sparse path")
    registry = store.load_registry(home)
    device = common._find_device(registry, args.flash_device)
    if not device:
        raise store.RefusedError("device is not registered")
    device_id = device.get("id", "").lower()
    if not re.fullmatch(r"[0-9a-f]{8,64}", device_id):
        raise store.RefusedError("registered device ID is invalid")
    board = args.board or device.get("board")
    if not board or board != device.get("board"):
        raise store.RefusedError("selected board does not match the registered device profile")
    profile_path = repo / "boards" / board / "board.env"
    if profile_path.is_symlink() or not profile_path.is_file():
        raise store.RefusedError("board profile is missing")
    tested = board == "ufi103s"
    if not tested:
        raise store.RefusedError("untested board refused; this installer has no verified flash path for the selected board")
    backup_dir, backup_manifest = _verified_backup(home, device)
    checked = _checked_sum_file(image_set, require_all=True)
    image_manifest_hash = _hash(image_set / "SHA256SUMS")
    profile_hash = _hash(profile_path)
    if not args.dry_run and (not sys.stdin.isatty() or not sys.stdout.isatty()):
        print("REFUSED: non-interactive flash is read-only; use --dry-run to inspect phases.", file=sys.stderr)
        args.dry_run = True
    edl = shutil.which("edl")
    if not edl:
        raise store.RefusedError("EDL client not found; no hardware command will be substituted")

    with tempfile.TemporaryDirectory(prefix="openstick-flash-") as temp:
        os.chmod(temp, 0o700)
        flash_backup = _make_flash_backup(backup_dir, backup_manifest, Path(temp) / "backup-slices")
        Path(temp, "home").mkdir(mode=0o700)
        env = {"BOARD": board, "PROFILE": "prod", "BACKUP_DIR": str(backup_dir), "OUT": str(image_set),
               "OPENSTICK_ROOT": str(repo), "OPENSTICK_CONFIG_MODE": "generic", "OPENSTICK_BOARD": board,
               "OPENSTICK_PROFILE": "prod", "OPENSTICK_OUT": str(image_set),
               "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(Path(temp) / "home"),
               "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TERM": "dumb",
               "PYTHONDONTWRITEBYTECODE": "1", "LDR": str(image_set / "loader" / "prog_emmc_firehose_8916.mbn"),
               "EDL_CMD": "sudo .venv/bin/python edl/edl.py"}
        env["BACKUP_DIR"] = str(flash_backup)
        serial, identity = _live_identity(edl, env, temp)
        if serial != device_id or identity != backup_manifest.get("identity") or identity != device.get("identity"):
            raise store.RefusedError("live EDL identity fingerprints do not match the registered verified backup")
        print("Live EDL identity and fsg/sec/fsc fingerprints match the verified backup.")
        # Preserve the source-tree Path A engines but supply firmware only from
        # the explicitly selected, hash-verified image set.
        source_patha = repo / "tools" / "pathA"
        patha = Path(temp) / "pathA"
        patha.mkdir(mode=0o700)
        for script in ("pathA-flash.sh", "fwlib.sh"):
            shutil.copyfile(source_patha / script, patha / script)
            os.chmod(patha / script, 0o500)
        data_patha = image_set / "pathA"
        patha_checked = _checked_sum_file(data_patha)
        for group in ("fw", "gpt", "rollback"):
            source_group = data_patha / group
            if source_group.is_symlink() or not source_group.is_dir():
                raise store.RefusedError("image set lacks Path A " + group + " artifacts")
            patha_required = {p.relative_to(data_patha).as_posix() for p in source_group.rglob("*")
                              if p.is_file() and not p.is_symlink()}
            if not patha_required.issubset(patha_checked):
                raise store.RefusedError("one or more Path A " + group + " inputs are not hashed")
            shutil.copytree(source_group, patha / group)
        shutil.copyfile(data_patha / "SHA256SUMS", patha / "SHA256SUMS")
        os.chmod(patha / "SHA256SUMS", 0o400)
        loader = image_set / "loader" / "prog_emmc_firehose_8916.mbn"
        if loader.is_symlink() or not loader.is_file() or _hash(loader) != common.LOADER_SHA256:
            raise store.RefusedError("verified pinned Firehose loader is missing or mismatched")
        if "loader/prog_emmc_firehose_8916.mbn" not in checked:
            raise store.RefusedError("Firehose loader is not covered by image-set SHA256SUMS")
        for rel in ("files-" + board + "/boot.bin", "files-" + board + "/rootfs.bin", "files-" + board + "/SHA256SUMS", "fw/layout-" + board + ".env"):
            if not (image_set / rel).is_file():
                raise store.RefusedError("first-install image set is incomplete: " + rel)
        # Also require all package inputs to be covered by the image-set manifest.
        required = {"files-" + board + "/boot.bin", "files-" + board + "/rootfs.bin", "fw/layout-" + board + ".env"}
        if not required.issubset(checked):
            raise store.RefusedError("first-install images or layout are not covered by SHA256SUMS")
        _checked_sum_file(image_set / ("files-" + board), require_all=True)
        patha_manifest_hash = _hash(data_patha / "SHA256SUMS")

        sequence = [("pre", [str(patha / "pathA-flash.sh"), "pre", "dry"], False)]
        for phase in ("gpt", "tz", "rpm", "sbl1", "hyp", "aboot"):
            sequence.append((phase, [str(patha / "pathA-flash.sh"), phase, None], True))
        # Existing final write mode resets EDL. Only run the read-only verification;
        # this wrapper never resets or reboots the device.
        sequence.extend([("final", [str(patha / "pathA-flash.sh"), "final", "dry"], False),
                         ("first-install", [str(repo / "tools/flash/flash-board.sh"), "first-install", None], True)])

        devdir = home / "devices" / device_id
        state_file = devdir / "flash-state.json"
        if state_file.exists():
            state = json.loads(state_file.read_text(encoding="utf-8"))
            if state.get("status") in ("write-started", "interrupted", "failed-write"):
                if not args.reconcile_state:
                    raise store.RefusedError("a previous destructive phase is unresolved; reconcile device state before any further write")
                phase = state.get("phase")
                if phase not in PHASES:
                    raise store.RefusedError("unresolved flash state has an invalid phase")
                if not sys.stdin.isatty() or not sys.stdout.isatty():
                    raise store.RefusedError("device-state reconciliation requires an interactive terminal")
                print("Inspect the device state for phase %s before continuing. This records your reconciliation; it performs no writes." % phase)
                expected = "reconcile %s %s" % (device_id[-8:], phase)
                print("Type exactly after inspection: " + expected)
                if input("> ") != expected:
                    raise store.RefusedError("device-state reconciliation text did not match")
                state_file.unlink()
                store.append_event(home, {"at": int(time.time()), "type": "flash.reconciled", "device_id": device_id,
                                          "phase": phase, "method": "interactive-user-acknowledgement"})
                for prior in reversed(device.get("flashes", [])):
                    if prior.get("outcome") == "partial-failure-reconciliation-required":
                        prior["outcome"] = "device-state-reconciled-by-user"
                        prior.setdefault("reconciled_phases", []).append(phase)
                        break
                store.save_registry(home, registry)
                print("Reconciliation recorded. No write was performed. Start a new flash run to receive fresh phase gates.")
                return 0
        elif args.reconcile_state:
            raise store.RefusedError("there is no unresolved flash state to reconcile")
        flash_entry = None
        for phase, command, writes in sequence:
            if _hash(profile_path) != profile_hash or _hash(image_set / "SHA256SUMS") != image_manifest_hash:
                raise store.RefusedError("board profile or image-set manifest changed during flash")
            current_backup, current_manifest = _verified_backup(home, device)
            if current_backup != backup_dir or current_manifest.get("sha256") != backup_manifest.get("sha256"):
                raise store.RefusedError("verified backup changed during flash")
            checked_now = _checked_sum_file(image_set, require_all=True)
            if not checked.issubset(checked_now):
                raise store.RefusedError("image-set manifest changed during flash")
            if _hash(loader) != common.LOADER_SHA256:
                raise store.RefusedError("pinned Firehose loader changed during flash")
            serial, identity = _live_identity(edl, env, temp)
            if serial != device_id or identity != backup_manifest.get("identity") or identity != device.get("identity"):
                raise store.RefusedError("live EDL identity fingerprints do not match the registered verified backup")
            print("Live EDL identity and fsg/sec/fsc fingerprints match the verified backup.")
            if _hash(data_patha / "SHA256SUMS") != patha_manifest_hash:
                raise store.RefusedError("Path A artifact manifest changed during flash")
            patha_current = _checked_sum_file(data_patha, "SHA256SUMS")
            if not patha_required.issubset(patha_current):
                raise store.RefusedError("Path A firmware/GPT/rollback hashes are incomplete")
            print("\nIntended operation: %s" % ("write " + phase if writes else "read-only preflight"))
            phase_env = dict(env)
            phase_env.pop("COMMIT", None); phase_env.pop("CONFIRM", None)
            dry_command = command[:]
            if dry_command[-1] is None: dry_command[-1] = "dry"
            if phase == "first-install":
                dry_command[-1] = "dry"
            if _invoke(dry_command, phase_env):
                _event(home, device_id, phase, "dry-run-failed")
                raise store.StoreError("dry-run failed for phase " + phase)
            if not writes:
                _event(home, device_id, phase, "read-only-pass", evidence="underlying dry-run exit 0")
                continue
            if args.dry_run:
                _event(home, device_id, phase, "dry-run-only", evidence="write not invoked")
                continue
            expected = "flash %s %s" % (device_id[-8:], phase)
            print("Type exactly: " + expected)
            try:
                typed = input("> ")
            except EOFError:
                _event(home, device_id, phase, "confirmation-missing")
                raise store.RefusedError("confirmation input was unavailable")
            if typed != expected:
                _event(home, device_id, phase, "confirmation-refused")
                raise store.RefusedError("confirmation text did not match")
            # Recheck after the interactive wait and immediately before COMMIT.
            if _hash(profile_path) != profile_hash or _hash(image_set / "SHA256SUMS") != image_manifest_hash:
                raise store.RefusedError("board profile or image-set manifest changed after confirmation")
            current_backup, current_manifest = _verified_backup(home, device)
            if current_backup != backup_dir or current_manifest.get("sha256") != backup_manifest.get("sha256"):
                raise store.RefusedError("verified backup changed after confirmation")
            _checked_sum_file(image_set, require_all=True)
            _checked_sum_file(image_set / ("files-" + board), require_all=True)
            if _hash(loader) != common.LOADER_SHA256 or _hash(data_patha / "SHA256SUMS") != patha_manifest_hash:
                raise store.RefusedError("verified firmware input changed after confirmation")
            patha_current = _checked_sum_file(data_patha, "SHA256SUMS")
            if not patha_required.issubset(patha_current):
                raise store.RefusedError("Path A firmware/GPT/rollback hashes are incomplete")
            serial, identity = _live_identity(edl, env, temp)
            if serial != device_id or identity != backup_manifest.get("identity") or identity != device.get("identity"):
                raise store.RefusedError("live EDL identity fingerprints changed after confirmation")
            phase_env["COMMIT"] = "1"
            phase_env["CONFIRM"] = "flash-%s-first-install" % board if phase == "first-install" else "da-flashez-" + phase
            if flash_entry is None:
                flash_entry = {"ts": int(time.time()), "image_set": str(image_set), "phases": [], "outcome": "in-progress"}
                device.setdefault("flashes", []).append(flash_entry)
                store.save_registry(home, registry)
            state_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            _write_state(state_file, "write-started", phase)
            _event(home, device_id, phase, "write-started", confirmation="accepted")
            try:
                code = _invoke(command[:-1] + ["write"], phase_env)
            except KeyboardInterrupt:
                _write_state(state_file, "interrupted", phase)
                flash_entry["phases"].append({"name": phase, "result": "interrupted", "verified": False})
                flash_entry["outcome"] = "partial-failure-reconciliation-required"
                store.save_registry(home, registry)
                _event(home, device_id, phase, "interrupted", verified=False)
                raise
            if code:
                _write_state(state_file, "failed-write", phase)
                flash_entry["phases"].append({"name": phase, "result": "failed", "verified": False, "exit_code": code})
                flash_entry["outcome"] = "partial-failure-reconciliation-required"
                store.save_registry(home, registry)
                _event(home, device_id, phase, "failed", verified=False, exit_code=code)
                raise store.StoreError("write phase %s failed; do not retry automatically. Inspect device state, then use flash --reconcile-state. Event log: %s" % (phase, home / "events.jsonl"))
            _event(home, device_id, phase, "command-pass-evidence-reported-by-engine", verified=True)
            flash_entry["phases"].append({"name": phase, "result": "engine-reported-pass", "verified": True})
            store.save_registry(home, registry)
            state_file.unlink(missing_ok=True)
        if flash_entry is not None:
            flash_entry["outcome"] = "engine-reported-pass-not-boot-verified"
            store.save_registry(home, registry)
        print("Flash sequence completed. Device boot is NOT verified; do not infer boot success from command exit alone.")
        return 0
