"""Read-only SSH verification and guarded, synthetic-testable rollback execution.

The rollback planner remains the authority for supported geometry and phase
ordering. This module consumes its print-only result, then runs the equivalent
operations one phase at a time; it never evaluates planner output as shell.
"""
import hashlib
import json
import os
import re
import shlex
import signal
import shutil
import shlex
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import cli as common
from . import edl_runner
from . import store


IDENTITY = ("fsg", "sec", "fsc")
INITIAL_FULL_READ_TIMEOUT_SECONDS = edl_runner.FULL_READ_TIMEOUT_SECONDS
FINAL_FULL_READ_TIMEOUT_SECONDS = edl_runner.FULL_READ_TIMEOUT_SECONDS
EDL_COMMAND_TIMEOUT_SECONDS = 600
IDENTITY_READ_TIMEOUT_SECONDS = edl_runner.IDENTITY_READ_TIMEOUT_SECONDS
RANGE_READ_TIMEOUT_SECONDS = edl_runner.RANGE_READ_TIMEOUT_SECONDS
ROLLBACK_ENGINE_TIMEOUT_SECONDS = 900
RESTORE_ORDER = ("userdata", "cache", "persist", "boot", "modemst1", "modemst2",
                 "sbl1", "rpm", "tz", "tzbak", "hyp", "aboot")
PLAN_PHASES = ("pad",) + RESTORE_ORDER + ("gpt-secondary", "gpt-primary")
REMOTE_CHECKS = r'''set +e
check() { name=$1; shift; evidence=$("$@" 2>&1); rc=$?; evidence=$(printf '%s' "$evidence" | tr '\n\t' '  ' | cut -c1-240); if [ "$rc" -eq 0 ]; then printf 'PASS\t%s\t%s\n' "$name" "$evidence"; else printf 'FAIL\t%s\t%s\n' "$name" "$evidence"; fi; }
if command -v nft >/dev/null 2>&1 && nft list table inet openstick_fw >/dev/null 2>&1; then
  printf 'PASS\tfirewall_state\tinet openstick_fw exists\n'
  if [ -x /usr/sbin/openstick-ssh-cellular ]; then /usr/sbin/openstick-ssh-cellular print 2>/dev/null | nft -c -f - >/dev/null 2>&1; rc=$?; else rc=2; fi
  if [ "$rc" -eq 0 ]; then printf 'PASS\tfirewall_syntax\tnft check accepted installed rules file\n'; else printf 'SKIP\tfirewall_syntax\trules file unavailable for independent syntax check\n'; fi
else printf 'FAIL\tfirewall_state\tinet openstick_fw table unavailable\n'; printf 'SKIP\tfirewall_syntax\tno active rules table\n'; fi
if [ -e /dev/watchdog0 ] && command -v systemctl >/dev/null 2>&1; then w=$(systemctl show -p RuntimeWatchdogUSec --value 2>/dev/null); case "$w" in ''|0|0s) printf 'FAIL\twatchdog\tdevice present but RuntimeWatchdogUSec is disabled\n';; *) printf 'PASS\twatchdog\t/dev/watchdog0 RuntimeWatchdogUSec=%s\n' "$w";; esac; elif [ ! -e /dev/watchdog0 ]; then printf 'FAIL\twatchdog\t/dev/watchdog0 absent\n'; else printf 'SKIP\twatchdog\tsystemd watchdog state unavailable\n'; fi
if command -v mmcli >/dev/null 2>&1; then x=$(mmcli -L 2>&1); rc=$?; if [ "$rc" -eq 0 ] && printf '%s' "$x" | grep -qi 'No modems were found'; then printf 'SKIP\tmodem\tno modem listed\n'; elif [ "$rc" -eq 0 ] && [ -n "$x" ]; then printf 'PASS\tmodem\t%s\n' "$(printf '%s' "$x" | tr '\n\t' '  ' | cut -c1-240)"; elif [ "$rc" -eq 0 ]; then printf 'SKIP\tmodem\tno modem listed\n'; else printf 'FAIL\tmodem\tmmcli failed\n'; fi; else printf 'SKIP\tmodem\tmmcli unavailable\n'; fi
if command -v timedatectl >/dev/null 2>&1; then x=$(timedatectl show -p NTPSynchronized --value 2>&1); rc=$?; if [ "$rc" -eq 0 ] && [ "$x" = yes ]; then printf 'PASS\tntp\tNTPSynchronized=yes\n'; elif [ "$rc" -eq 0 ] && [ "$x" = no ]; then printf 'FAIL\tntp\tNTPSynchronized=no\n'; else printf 'SKIP\tntp\ttimedatectl state unavailable\n'; fi; else printf 'SKIP\tntp\ttimedatectl unavailable\n'; fi
if command -v nmcli >/dev/null 2>&1; then x=$(nmcli -t -f DEVICE,STATE device status 2>&1); rc=$?; if [ "$rc" -ne 0 ]; then printf 'FAIL\twifi\tnmcli failed\n'; elif printf '%s\n' "$x" | grep -q '^wlan0:connected$'; then printf 'PASS\twifi\twlan0:connected\n'; elif printf '%s\n' "$x" | grep -q '^wlan0:'; then printf 'FAIL\twifi\twlan0 is present but not connected\n'; else printf 'SKIP\twifi\twlan0 absent\n'; fi; else printf 'SKIP\twifi\tnmcli unavailable\n'; fi
k=$(uname -r 2>/dev/null); v=$(sed -n 's/^VERSION_ID=//p' /etc/os-release 2>/dev/null | head -1 | tr -d '"'); if [ -n "$k" ] && [ -n "$v" ] && [ -r /etc/openstick-rootfs ]; then r=$(tr '\n' ' ' </etc/openstick-rootfs | cut -c1-120); printf 'PASS\tversions\tkernel=%s debian=%s marker=%s\n' "$k" "$v" "$r"; else printf 'SKIP\tversions\tkernel or Debian/OpenStick rootfs version evidence unavailable\n'; fi
'''


def _sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _edl_verbose(env):
    return env.get("OPENSTICK_EDL_VERBOSE") == "1", env.get("OPENSTICK_EDL_DEBUG") == "1"


def _safe_host(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+@[A-Za-z0-9][A-Za-z0-9_.-]*", value):
        raise store.RefusedError("host must be a plain USER@HOST without shell syntax")
    return value


def verify_command(args, home, repo):
    host = _safe_host(getattr(args, "host", None))
    ssh = shutil.which("ssh")
    if not ssh:
        print("FAIL SSH: ssh executable unavailable")
        return common.EXIT_VERIFY
    cmd = [ssh, "-F", "/dev/null", "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
           "-o", "ConnectTimeout=10", host, "sh", "-s"]
    try:
        result = subprocess.run(cmd, input=REMOTE_CHECKS, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, timeout=60,
                                env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                                     "HOME": os.environ.get("HOME", ""), "LANG": "C.UTF-8",
                                     "LC_ALL": "C.UTF-8"})
    except (OSError, subprocess.TimeoutExpired):
        print("FAIL SSH: connection or verification command failed")
        return common.EXIT_VERIFY
    if result.returncode:
        print("FAIL SSH: remote read-only verification exited %d" % result.returncode)
        return common.EXIT_VERIFY
    seen = set()
    failed = False
    for line in result.stdout.splitlines():
        fields = line.split("\t", 2)
        if len(fields) != 3 or fields[0] not in ("PASS", "FAIL", "SKIP") or fields[1] in seen:
            print("FAIL verification: malformed or duplicate evidence")
            return common.EXIT_VERIFY
        seen.add(fields[1])
        print("%s %s: %s" % tuple(fields))
        failed |= fields[0] == "FAIL"
    required = {"firewall_state", "firewall_syntax", "watchdog", "modem", "ntp", "wifi", "versions"}
    if seen != required:
        print("FAIL verification: incomplete evidence set")
        return common.EXIT_VERIFY
    return common.EXIT_VERIFY if failed else common.EXIT_OK


def _backup(home, device, backup_id):
    choices = [b for b in device.get("backups", []) if backup_id is None or b.get("id") == backup_id]
    for item in choices:
        if not {"full-read-size", "gpt-primary-and-backup-crc", "sha256"}.issubset(set(item.get("verified_by", []))):
            continue
        directory = Path(item.get("path", ""))
        try:
            if directory.is_symlink() or store._has_symlink_component(directory) or not directory.is_dir():
                continue
            if stat.S_IMODE(directory.stat().st_mode) & 0o077:
                raise ValueError("backup directory is not private")
            dump, manifest_path, sums_path = directory / "emmc-full.bin", directory / "manifest.json", directory / "SHA256SUMS"
            for path in (dump, manifest_path, sums_path):
                if path.is_symlink() or not path.is_file() or not stat.S_ISREG(os.lstat(path).st_mode):
                    raise ValueError("unsafe backup member")
                if stat.S_IMODE(path.stat().st_mode) & 0o077:
                    raise ValueError("backup member is not private")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            digest = item.get("sha256")
            if (manifest.get("schema_version") != 1 or manifest.get("device_id", "").lower() != device.get("id", "").lower()
                    or manifest.get("sha256") != digest or _sha(dump) != digest
                    or dump.stat().st_size != manifest.get("size")
                    or sums_path.read_text(encoding="ascii").splitlines() != [digest + "  emmc-full.bin"]
                    or manifest.get("identity") != device.get("identity")):
                raise ValueError("backup manifest or digest mismatch")
            parsed = common._parse_gpt(dump)
            parts = manifest.get("partitions")
            expected = {name: {"start_lba": start, "end_lba": end} for name, (start, end) in parsed.items()}
            if parts != expected or not set(IDENTITY).issubset(parsed):
                raise ValueError("backup partition manifest differs from GPT")
            fps = {name: common._hash_range(dump, *parsed[name]) for name in IDENTITY}
            if fps != manifest.get("identity"):
                raise ValueError("backup identity fingerprint mismatch")
            return item, directory, manifest, parsed
        except (OSError, ValueError, TypeError, json.JSONDecodeError, KeyError):
            continue
    raise store.RefusedError("a complete verified backup matching the device and requested backup ID is required")


def _planner(repo, directory, manifest, parsed, work):
    """Require the existing print-only planner to accept this exact synthetic backup."""
    work.mkdir(mode=0o700, parents=True, exist_ok=True)
    primary = work / "planner-primary.bin"
    secondary = work / "planner-secondary.bin"
    rows = work / "planner-parts.csv"
    dump = directory / "emmc-full.bin"
    emmc = dump.stat().st_size // 512
    with dump.open("rb") as stream:
        stream.seek(0); p = stream.read(34 * 512)
        stream.seek((emmc - 33) * 512); s = stream.read(33 * 512)
    if len(p) != 34 * 512 or len(s) != 33 * 512:
        raise store.RefusedError("backup GPT geometry is unsupported by the rollback planner")
    primary.write_bytes(p); secondary.write_bytes(s)
    with rows.open("w", encoding="ascii", newline="") as stream:
        stream.write("number,name,start_lba,end_lba,offset_bytes,size_bytes\n")
        for i, name in enumerate(sorted(parsed, key=lambda n: parsed[n][0]), 1):
            start, end = parsed[name]
            stream.write("%d,%s,%d,%d,%d,%d\n" % (i, name, start, end, start * 512, (end - start + 1) * 512))
    command = [sys.executable, "-I", str(Path(repo) / "tools/rollback-plan.py"), "--dump", str(dump),
               "--csv", str(rows), "--primary", str(primary), "--secondary", str(secondary),
               "--work", str(work), "--emmc", str(emmc), "--dump-sha", manifest["sha256"]]
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=120,
                            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"})
    if result.returncode or "PHASE 0b" not in result.stdout or "PHASE 2" not in result.stdout:
        raise store.RefusedError("rollback planner rejected backup geometry or did not produce all required phases")
    lbs = re.findall(r'^\$EDL ws ([0-9]+) ', result.stdout, re.M)
    if len(lbs) != 30 or "MUST be exactly " + manifest["sha256"] not in result.stdout:
        raise store.RefusedError("rollback planner output does not match the expected phase structure")
    return result.stdout


def _parse_plan(plan, planner_dir, manifest, parsed):
    """Convert only recognized planner rows into argv data; never evaluate text."""
    spans = {}
    disk_sectors = manifest["size"] // 512
    spans["pad"] = (parsed["pad"][0], parsed["pad"][1])
    for name in RESTORE_ORDER:
        spans[name] = parsed[name]
    spans["gpt-secondary"] = (disk_sectors - 33, disk_sectors - 1)
    spans["gpt-primary"] = (0, 33)
    rows = []
    active = None
    stop = False
    row_re = re.compile(r"^## (?:R[0-9]+|G[0-9]+) ([a-z0-9-]+): LBA ([0-9]+)\.\.([0-9]+), ([0-9]+) sectors = ([0-9]+) B")
    for line in plan.splitlines():
        if line.startswith("# ===== PHASE 2:"):
            stop = True
        if stop:
            break
        match = row_re.match(line)
        if match:
            phase, start, end, count, size = match.groups()
            if phase not in spans:
                raise store.RefusedError("rollback planner printed an unsupported restore row")
            start, end, count, size = map(int, (start, end, count, size))
            if (start, end) != spans[phase] or count != end - start + 1 or size != count * 512:
                raise store.RefusedError("rollback planner row geometry differs from the verified backup")
            active = {"phase": phase, "start": start, "end": end, "commands": [], "title": line}
            rows.append(active)
            continue
        if line.startswith("# ===== PHASE 0b"):
            active = {"phase": "pad", "start": spans["pad"][0], "end": spans["pad"][1],
                      "commands": [], "title": line}
            rows.append(active)
            continue
        if line.startswith("# ===== PHASE 1:"):
            active = None
        if active is not None and line.startswith("$EDL ws "):
            command = shlex.split(line.split("#", 1)[0])
            if len(command) < 6 or command[:2] != ["$EDL", "ws"]:
                raise store.RefusedError("rollback planner emitted an unrecognized EDL row")
            active["commands"].append(command)
    expected_order = ("pad",) + RESTORE_ORDER + ("gpt-secondary", "gpt-primary")
    if tuple(row["phase"] for row in rows) != expected_order:
        raise store.RefusedError("rollback planner phase order is incomplete or unexpected")
    planner_dir = Path(planner_dir).resolve()
    for row in rows:
        if len(row["commands"]) != 2:
            raise store.RefusedError("rollback planner phase must have one dry-run and one write row")
        dry, write = row["commands"]
        if "--skipwrite" not in dry or "--skipwrite" in write:
            raise store.RefusedError("rollback planner dry-run/write pair is malformed")
        dry_normal = [x for x in dry if x != "--skipwrite"]
        if dry_normal != write:
            raise store.RefusedError("rollback planner dry-run/write rows do not match")
        phase, start, end = row["phase"], row["start"], row["end"]
        source = write[3]
        if int(write[2]) != start:
            raise store.RefusedError("rollback planner write LBA differs from its row")
        if write[4:] != ["--sectorsize=512", "--loader=$LDR", "--memory=emmc"]:
            raise store.RefusedError("rollback planner write options are not the approved EDL form")
        if phase == "pad":
            wanted = "$WORK/pad-before.bin"
        elif phase in RESTORE_ORDER:
            wanted = "$WORK/rb-%s.bin" % phase
        elif phase == "gpt-secondary":
            wanted = str(planner_dir / "planner-secondary.bin")
        else:
            wanted = str(planner_dir / "planner-primary.bin")
        if source != wanted:
            raise store.RefusedError("rollback planner write source is not a verified private input")
        row["source_ref"] = source
    return rows


def _live(edl, env, loader, directory, expected_id, session=None):
    verbose, debug = _edl_verbose(env)
    session = session or edl_runner.Session(sysfs_root=None, client=edl, loader=loader, env=env)
    identity_path = directory / "live-primary-gpt.bin"
    session.run(["rs", "0", "34", str(identity_path)],
                timeout=IDENTITY_READ_TIMEOUT_SECONDS, env=env,
                verbose=verbose, debug=debug)
    if not identity_path.is_file() or identity_path.stat().st_size != 34 * 512:
        raise store.RefusedError("live EDL primary GPT identity read was incomplete; hint: check EDL mode and USB before retrying")
    identity = session.identity
    dump = directory / "live-before.bin"
    session.run(["rf", str(dump)], timeout=INITIAL_FULL_READ_TIMEOUT_SECONDS,
                env=env, verbose=verbose, debug=debug)
    if not dump.is_file() or dump.stat().st_size % 512:
        raise store.RefusedError("live EDL full-disk read failed")
    parts = common._parse_gpt(dump)
    fingerprints = {name: common._hash_range(dump, *parts[name]) for name in IDENTITY}
    return identity["serial"], fingerprints, dump, parts


def _live_identity_only(edl, env, loader, directory, expected_id, parsed, expected, session=None):
    verbose, debug = _edl_verbose(env)
    session = session or edl_runner.Session(sysfs_root=None, client=edl, loader=loader, env=env)
    identity_path = directory / "identity-primary-gpt.bin"
    session.run(["rs", "0", "34", str(identity_path)],
                timeout=IDENTITY_READ_TIMEOUT_SECONDS, env=env,
                verbose=verbose, debug=debug)
    if not identity_path.is_file() or identity_path.stat().st_size != 34 * 512:
        raise store.RefusedError("live EDL identity range read was incomplete")
    identity = session.identity
    if identity["serial"] != expected_id:
        raise store.RefusedError("live EDL serial changed before destructive phase")
    observed = {}
    for name in IDENTITY:
        start, end = parsed[name]
        target = directory / ("identity-" + name + ".bin")
        session.run(["rs", str(start), str(end - start + 1), str(target)],
                    timeout=RANGE_READ_TIMEOUT_SECONDS, env=env,
                    verbose=verbose, debug=debug)
        if not target.is_file() or target.stat().st_size != (end - start + 1) * 512:
            raise store.RefusedError("live EDL identity partition read failed")
        observed[name] = _sha(target)
    if observed != expected:
        raise store.RefusedError("live EDL fsg/sec/fsc fingerprints changed before destructive phase")


def _known_rollback_state(device, backup_dir, live_dump, parts, completed_phases=()):
    """Classify current GPT and Path A firmware against the last verified image set.

    Unknown or missing evidence blocks restore. The rollback planner's documented
    phase 0a requires this classification before any write.
    """
    from .flash import _checked_sum_file

    image_set = None
    for flash in reversed(device.get("flashes", [])):
        candidate = Path(flash.get("image_set", ""))
        if candidate.is_dir() and not candidate.is_symlink() and not store._has_symlink_component(candidate) and not store._inside_git(candidate):
            image_set = candidate
            break
    if image_set is None:
        raise store.RefusedError("no registered flash image set is available to classify current rollback state")
    sums = _checked_sum_file(image_set, require_all=True)
    patha = image_set / "pathA"
    patha_sums = _checked_sum_file(patha)
    required = ("gpt/gpt-T-primary.bin", "gpt/gpt-T-secondary.bin",
                "fw/sbl1.img", "fw/rpm.img", "fw/tz.img", "fw/hyp.img", "fw/aboot.img",
                "rollback/stock-sbl1.bin", "rollback/stock-rpm.bin", "rollback/stock-tz-tzbak.bin",
                "rollback/stock-hyp.bin", "rollback/stock-aboot.bin",
                "rollback/gpt-orig-primary.bin", "rollback/gpt-orig-secondary.bin")
    if not set("pathA/" + name for name in required).issubset(sums):
        raise store.RefusedError("registered image set lacks complete rollback state fingerprints")
    if not set(required).issubset(patha_sums):
        raise store.RefusedError("rollback state files are not covered by their checksum manifest")
    paths = {name: patha / name for name in required}
    for path in paths.values():
        if path.is_symlink() or not path.is_file():
            raise store.RefusedError("rollback state evidence contains a missing or unsafe file")
    disk_sectors = live_dump.stat().st_size // 512
    ranges = {
        "gpt/gpt-T-primary.bin": (0, 33),
        "gpt/gpt-T-secondary.bin": (disk_sectors - 33, disk_sectors - 1),
        "rollback/gpt-orig-primary.bin": (0, 33),
        "rollback/gpt-orig-secondary.bin": (disk_sectors - 33, disk_sectors - 1),
        "fw/sbl1.img": parts["sbl1"], "rollback/stock-sbl1.bin": parts["sbl1"],
        "fw/rpm.img": parts["rpm"], "rollback/stock-rpm.bin": parts["rpm"],
        "fw/tz.img": parts["tz"], "rollback/stock-tz-tzbak.bin": parts["tz"],
        "fw/hyp.img": parts["hyp"], "rollback/stock-hyp.bin": parts["hyp"],
        "fw/aboot.img": parts["aboot"], "rollback/stock-aboot.bin": parts["aboot"],
    }
    gpt_checks = (
        ("gpt/gpt-T-primary.bin", "rollback/gpt-orig-primary.bin", (0, 33), "gpt-primary"),
        ("gpt/gpt-T-secondary.bin", "rollback/gpt-orig-secondary.bin",
         (disk_sectors - 33, disk_sectors - 1), "gpt-secondary"),
    )
    for patha_key, stock_key, (start, end), phase in gpt_checks:
        live_hash = common._hash_range(live_dump, start, end)
        accepted = {_sha(paths[patha_key])}
        # A completed GPT phase is accepted only when the saved restore record
        # proves this is a continuation of the same verified backup.
        if phase in completed_phases:
            accepted.add(_sha(paths[stock_key]))
        if live_hash not in accepted:
            raise store.RefusedError("live GPT does not match a recorded rollback phase; restore refused")
    new_seen = False
    for new_key, stock_key in (("fw/sbl1.img", "rollback/stock-sbl1.bin"),
                               ("fw/rpm.img", "rollback/stock-rpm.bin"),
                               ("fw/tz.img", "rollback/stock-tz-tzbak.bin"),
                               ("fw/hyp.img", "rollback/stock-hyp.bin"),
                               ("fw/aboot.img", "rollback/stock-aboot.bin")):
        start, end = ranges[new_key]
        live_hash = common._hash_range(live_dump, start, end)
        choices = {_sha(paths[new_key]), _sha(paths[stock_key])}
        if live_hash not in choices:
            raise store.RefusedError("live firmware state is UNKNOWN for " + new_key.split("/")[-1])
        new_seen |= live_hash == _sha(paths[new_key])
    if not new_seen and "aboot" not in completed_phases:
        raise store.RefusedError("live firmware is entirely stock; expected Path A state for rollback")


def _saved_completed_phases(state, backup, device_id):
    """Validate and return a completed prefix from the same restore record."""
    if state is None:
        return ()
    if not isinstance(state, dict) or state.get("schema") != 1:
        raise store.RefusedError("saved restore state has an unsupported schema")
    if state.get("device_id") != device_id:
        raise store.RefusedError("saved restore state belongs to a different device")
    if state.get("backup_id") != backup.get("id") or state.get("backup_sha256") != backup.get("sha256"):
        raise store.RefusedError("saved restore state belongs to a different verified backup")
    completed = state.get("completed_phases", [])
    if not isinstance(completed, list) or len(completed) > len(PLAN_PHASES):
        raise store.RefusedError("saved restore phase history is malformed")
    if completed != list(PLAN_PHASES[:len(completed)]):
        raise store.RefusedError("saved restore phases are not a completed plan prefix")
    status = state.get("status")
    phase = state.get("phase")
    if status in ("phase-complete", "reconciled-phase-complete", "complete"):
        if completed and phase != completed[-1]:
            raise store.RefusedError("saved restore phase and completed history disagree")
        if not completed and phase is not None:
            raise store.RefusedError("saved restore phase and completed history disagree")
        if status == "complete" and len(completed) != len(PLAN_PHASES):
            raise store.RefusedError("saved restore completion record is incomplete")
        if status != "complete" and not completed:
            raise store.RefusedError("saved restore completion record has no completed phase")
    elif status in ("in-progress", "partial-failure-reconciliation-required"):
        if len(completed) >= len(PLAN_PHASES) or phase != PLAN_PHASES[len(completed)]:
            raise store.RefusedError("saved interrupted phase does not follow its completed prefix")
    else:
        raise store.RefusedError("saved restore state cannot be resumed safely")
    return tuple(completed)


def _verify_completed_phase_bytes(completed, backup_path, live_dump, manifest, parsed):
    """A saved prefix may skip writes only when the live bytes prove it completed."""
    for phase in completed:
        start, end = _phase_span(phase, manifest, parsed)
        expected = common._hash_range(backup_path, start, end)
        observed = common._hash_range(live_dump, start, end)
        if observed != expected:
            raise store.RefusedError("saved restore history claims completed phase %s but live bytes differ" % phase)


def _state_path(home, device_id):
    return Path(home) / "devices" / device_id / "restore-state.json"


def _save_state(path, data):
    data["updated_at"] = int(time.time())
    store._atomic_private_write(path, (json.dumps(data, sort_keys=True, indent=2) + "\n").encode())


def _confirm(phase, device_id):
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise store.RefusedError("non-interactive restore is read-only")
    expected = "restore %s %s" % (device_id[-8:], phase)
    print("Type exactly: " + expected)
    if input("> ") != expected:
        raise store.RefusedError("restore confirmation did not match")
    return expected


def restore_command(args, home, repo, *, usb_sysfs_root=None, session=None):
    registry = store.load_registry(home)
    device = common._find_device(registry, args.restore_device)
    if not device:
        raise store.RefusedError("device is not registered")
    device_id = device.get("id", "").lower()
    if not re.fullmatch(r"[0-9a-f]{8,64}", device_id):
        raise store.RefusedError("registered device ID is invalid")
    item, backup_dir, manifest, parsed = _backup(home, device, getattr(args, "restore_backup", None))
    if (not {"gpt-primary-and-backup-crc", "full-read-size"}.issubset(set(item.get("verified_by", [])))
            or manifest.get("size") != backup_dir.joinpath("emmc-full.bin").stat().st_size):
        raise store.RefusedError("backup verification evidence is incomplete")
    state_path = _state_path(home, device_id)
    prior_state = store._read_json(state_path, {}) if state_path.exists() else None
    completed_evidence = _saved_completed_phases(prior_state, item, device_id)
    if getattr(args, "restore_reconcile_state", False):
        if not prior_state or prior_state.get("status") not in ("in-progress", "partial-failure-reconciliation-required"):
            raise store.RefusedError("there is no interrupted destructive restore phase to reconcile")
        phase = prior_state.get("phase")
        if phase not in PLAN_PHASES:
            raise store.RefusedError("restore state has an invalid unresolved phase")
        if prior_state.get("backup_sha256") != manifest["sha256"] or prior_state.get("backup_id") != item.get("id"):
            raise store.RefusedError("reconciliation backup differs from the interrupted restore")
        edl = edl_runner.resolve_client()
        loader = edl_runner.resolve_loader()
        with tempfile.TemporaryDirectory(prefix="openstick-restore-reconcile-") as temp:
            os.chmod(temp, 0o700)
            identity_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", ""),
                            "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
            identity_env["OPENSTICK_EDL_VERBOSE"] = "1" if getattr(args, "verbose", False) else "0"
            identity_env["OPENSTICK_EDL_DEBUG"] = "1" if getattr(args, "debug", False) else "0"
            session = session or edl_runner.Session(sysfs_root=usb_sysfs_root, client=edl,
                                                    loader=loader, env=identity_env)
            session.client = edl; session.loader = loader; session.env = identity_env
            _live_identity_only(edl, identity_env, loader, Path(temp), device_id,
                                parsed, manifest["identity"], session=session)
            start, end = _phase_span(phase, manifest, parsed)
            readback = Path(temp) / "phase-reconcile.bin"
            _edl(edl, ["rs", str(start), str(end - start + 1), str(readback),
                       "--loader=" + str(loader), "--memory=emmc"], identity_env, loader,
                 session=session,
                  )
            if not readback.is_file() or readback.stat().st_size != (end - start + 1) * 512:
                raise store.RefusedError("read-only phase reconciliation readback failed")
            observed = _sha(readback)
            if phase == "gpt-primary":
                final_state = {}
                try:
                    _verify_final_disk(edl, identity_env, loader, Path(temp), manifest,
                                       final_state, session=session)
                except ValueError:
                    prior_state.update({"status": "final-readback-failed", "reconciliation_result": "full-disk-mismatch"})
                    _save_state(state_path, prior_state)
                    store.append_event(home, {"at": int(time.time()), "type": "restore.final-readback-failed",
                                              "device_id": device_id, "backup_id": item["id"], "phase": phase})
                    raise
        expected_hash = common._hash_range(backup_dir / "emmc-full.bin", start, end)
        if observed != expected_hash:
            prior_state.update({"status": "manual-action-required", "observed_sha256": observed,
                                "expected_sha256": expected_hash, "reconciliation_result": "phase-range-mismatch"})
            _save_state(state_path, prior_state)
            store.append_event(home, {"at": int(time.time()), "type": "restore.reconciliation-mismatch",
                                      "device_id": device_id, "backup_id": item["id"], "phase": phase,
                                      "observed_sha256": observed, "expected_sha256": expected_hash})
            raise store.RefusedError("phase bytes are partial or unknown; manual repair is required and no retry was attempted")
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise store.RefusedError("restore reconciliation requires an interactive terminal")
        expected = "reconcile %s %s" % (device_id[-8:], phase)
        print("Read-only phase reconciliation matches the verified backup bytes.")
        print("Type exactly: " + expected)
        if input("> ").strip() != expected:
            raise store.RefusedError("restore reconciliation text did not match")
        completed = list(prior_state.get("completed_phases", []))
        if phase not in completed:
            completed.append(phase)
        prior_state.update({"status": "reconciled-phase-complete", "completed_phases": completed,
                            "reconciled_at": int(time.time()), "expected_sha256": expected_hash,
                            "observed_sha256": observed,
                            "reconciliation": "read-only-range-hash-plus-interactive-acknowledgement"})
        _save_state(state_path, prior_state)
        store.append_event(home, {"at": int(time.time()), "type": "restore.reconciled", "device_id": device_id,
                                  "backup_id": prior_state.get("backup_id"), "phase": phase,
                                  "method": "interactive-user-acknowledgement"})
        print("Reconciliation recorded. No device command or write was performed.")
        return common.EXIT_OK
    if state_path.exists():
        state = prior_state
        if state.get("status") == "complete":
            raise store.RefusedError("this restore is already complete; it will not replay destructive phases")
        if state.get("status") not in ("complete", "cancelled", "phase-complete", "reconciled-phase-complete"):
            raise store.RefusedError("an interrupted restore requires manual reconciliation; no phase will be repeated")
        if state.get("backup_sha256") and state.get("backup_sha256") != manifest["sha256"]:
            raise store.RefusedError("restore backup differs from the previously recorded phase state")
    edl = edl_runner.resolve_client()
    parent = Path(home) / "devices" / device_id
    parent.mkdir(mode=0o700, parents=True, exist_ok=True); os.chmod(parent, 0o700)
    state = {"schema": 1, "device_id": device_id, "backup_id": item["id"],
             "backup_sha256": manifest["sha256"], "phase": None,
             "status": "preparing", "events": []}
    with tempfile.TemporaryDirectory(prefix="openstick-restore-") as tmp:
        os.chmod(tmp, 0o700); work = Path(tmp)
        planner_dir = work / "planner"
        plan = _planner(repo, backup_dir, manifest, parsed, planner_dir)
        plan_steps = _parse_plan(plan, planner_dir, manifest, parsed)
        engine = _make_fwlib_engine(repo, work / "rollback-engine")
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", ""),
               "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1"}
        env["OPENSTICK_EDL_VERBOSE"] = "1" if getattr(args, "verbose", False) else "0"
        env["OPENSTICK_EDL_DEBUG"] = "1" if getattr(args, "debug", False) else "0"
        loader = edl_runner.resolve_loader()
        session = session or edl_runner.Session(sysfs_root=usb_sysfs_root, client=edl,
                                                loader=loader, env=env)
        session.client = edl; session.loader = loader; session.env = env
        serial, live_identity, live_dump, live_parts = _live(
            edl, env, loader, work, device_id, session=session)
        if serial != device_id or live_identity != manifest.get("identity") or live_identity != device.get("identity"):
            raise store.RefusedError("live EDL serial and fsg/sec/fsc fingerprints must match verified backup")
        if live_dump.stat().st_size != manifest["size"]:
            raise store.RefusedError("live disk size differs from verified backup")
        _verify_completed_phase_bytes(completed_evidence, backup_dir / "emmc-full.bin",
                                      live_dump, manifest, parsed)
        _known_rollback_state(device, backup_dir, live_dump, live_parts, completed_evidence)
        if getattr(args, "dry_run", False):
            print("DRY RUN: planner accepted geometry; live identity matched; no writes performed.")
            return common.EXIT_OK
        phases = [step["phase"] for step in plan_steps]
        completed = list(prior_state.get("completed_phases", [])) if prior_state else []
        state["completed_phases"] = completed[:]
        if prior_state and prior_state.get("status") == "reconciled-phase-complete":
            if prior_state.get("phase") not in phases:
                raise store.RefusedError("reconciled restore phase is invalid")
            phases = phases[phases.index(prior_state["phase"]) + 1:]
        elif prior_state and prior_state.get("status") == "phase-complete":
            if prior_state.get("phase") not in phases:
                raise store.RefusedError("completed restore phase is invalid")
            phases = phases[phases.index(prior_state["phase"]) + 1:]
        for phase in phases:
            step = next(item for item in plan_steps if item["phase"] == phase)
            state.update({"phase": phase, "status": "awaiting-confirmation", "started_at": int(time.time()),
                          "live_size": live_dump.stat().st_size})
            prepared = _prepare_plan_phase(edl, env, loader, step, backup_dir / "emmc-full.bin",
                                           manifest, live_dump, work, session=session)
            print("Rollback plan phase %s: LBA %d..%d (%d sectors), SHA-256 %s" %
                  (phase, step["start"], step["end"], step["end"] - step["start"] + 1,
                   prepared["expected_sha256"]))
            _run_fwlib_phase(engine, edl, env, loader, prepared, work, write=False,
                             session=session)
            print("DRY RUN PASS for %s; no write has been invoked." % phase)
            state["confirmation"] = _confirm(phase, device_id)
            if _sha(backup_dir / "emmc-full.bin") != manifest["sha256"]:
                raise store.RefusedError("verified backup changed after phase confirmation")
            _live_identity_only(edl, env, loader, work, device_id, parsed,
                                manifest["identity"], session=session)
            state["status"] = "in-progress"
            _save_state(state_path, state)
            try:
                _run_fwlib_phase(engine, edl, env, loader, prepared, work, write=True,
                                 session=session)
                readback = work / "fwlib-logs" / ("after-" + phase + ".bin")
                if not readback.is_file() or readback.stat().st_size != (prepared["end"] - prepared["start"] + 1) * 512 or _sha(readback) != prepared["expected_sha256"]:
                    raise store.RefusedError("existing rollback helper readback mismatch")
                print("PASS write and per-phase readback: %s" % phase)
                if phase == "gpt-primary":
                    _verify_final_disk(edl, env, loader, work, manifest,
                                       state, session=session)
            except KeyboardInterrupt:
                state.update({"status": "in-progress", "interrupted_at": int(time.time()),
                              "interruption": "keyboard-interrupt-during-destructive-phase"})
                _save_state(state_path, state)
                store.append_event(home, {"at": int(time.time()), "type": "restore.interrupted", "device_id": device_id,
                                          "backup_id": item["id"], "phase": phase})
                raise
            except (store.RefusedError, ValueError) as exc:
                if state.get("status") != "final-readback-failed":
                    state["status"] = "partial-failure-reconciliation-required"
                state["failure"] = str(exc)[:160]
                _save_state(state_path, state)
                store.append_event(home, {"at": int(time.time()), "type": "restore.failed", "device_id": device_id,
                                          "backup_id": item["id"], "phase": phase})
                raise
            state["status"] = "phase-complete"
            completed.append(phase)
            state["completed_phases"] = completed[:]
            _save_state(state_path, state)
            store.append_event(home, {"at": int(time.time()), "type": "restore.phase-complete", "device_id": device_id,
                                      "backup_id": item["id"], "phase": phase})
        state.update({"status": "complete", "completed_at": int(time.time())})
        _save_state(state_path, state)
        return common.EXIT_OK


def _phase_span(phase, manifest, parsed):
    if phase == "pad" or phase in RESTORE_ORDER:
        return parsed[phase]
    sectors = manifest["size"] // 512
    if phase == "gpt-secondary":
        return sectors - 33, sectors - 1
    if phase == "gpt-primary":
        return 0, 33
    raise store.RefusedError("unknown rollback planner phase")


def _prepare_plan_phase(edl, env, loader, step, backup, manifest, live_dump, work, session=None):
    phase, start, end = step["phase"], step["start"], step["end"]
    count = end - start + 1
    if phase == "pad":
        source = work / "pad-before.bin"
        with open(live_dump, "rb") as src, open(source, "xb") as dst:
            src.seek(start * 512); remaining = (end - start + 1) * 512
            while remaining:
                block = src.read(min(1024 * 1024, remaining))
                if not block: raise store.RefusedError("live pad read is truncated")
                dst.write(block); remaining -= len(block)
            dst.flush(); os.fsync(dst.fileno())
    elif phase in RESTORE_ORDER:
        source = work / ("rb-" + phase + ".bin")
        with open(backup, "rb") as src, open(source, "xb") as dst:
            src.seek(start * 512); remaining = (end - start + 1) * 512
            while remaining:
                block = src.read(min(1024 * 1024, remaining))
                if not block: raise store.RefusedError("verified backup slice is truncated")
                dst.write(block); remaining -= len(block)
            dst.flush(); os.fsync(dst.fileno())
        os.chmod(source, 0o600)
    elif phase in ("gpt-secondary", "gpt-primary"):
        source = Path(step["source_ref"])
    else:
        raise store.RefusedError("unrecognized planner phase")
    want = common._hash_range(backup, start, end)
    if _sha(source) != want or source.stat().st_size != count * 512:
        raise store.RefusedError("phase source does not match verified backup")
    if phase == "pad":
        # Gate 0 read -> hash -> dry-run noop -> write same bytes -> readback hash.
        _edl(edl, ["rs", str(start), str(count), str(work / "pad-current.bin"), "--loader=" + str(loader), "--memory=emmc"], env, loader,
             verbose=_edl_verbose(env)[0], debug=_edl_verbose(env)[1], session=session)
        if _sha(work / "pad-current.bin") != want:
            raise store.RefusedError("pad pre-write hash mismatch")
    return {"phase": phase, "name": phase, "start": start, "end": end,
            "source": source, "expected_sha256": want, "planned_dry": step["commands"][0],
            "planned_write": step["commands"][1], "source_ref": step["source_ref"]}


def _fwlib_path():
    """Fixed production PATH; tests patch this helper to prepend private shims."""
    return "/usr/bin:/bin"


def _make_fwlib_engine(repo, directory):
    directory = Path(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    source = Path(repo) / "tools/pathA/fwlib.sh"
    if source.is_symlink() or not source.is_file():
        raise store.RefusedError("existing rollback helper is unavailable")
    helper = directory / "fwlib.sh"
    shutil.copyfile(source, helper); os.chmod(helper, 0o500)
    sums = directory / "SHA256SUMS"
    sums.write_text("%s  fwlib.sh\n" % _sha(helper), encoding="ascii")
    os.chmod(sums, 0o400)
    runner = directory / "run-phase.sh"
    runner.write_text('''#!/bin/bash
set -eu
. "$(dirname "$0")/fwlib.sh"
PHASE=$1
MODE=$2
LABEL=$3
LBA=$4
SECTORS=$5
SOURCE=$6
TOKEN="rollback-$PHASE"
init
put "$LABEL" "$LBA" "$SECTORS" "$SOURCE" "$MODE"
''', encoding="ascii")
    os.chmod(runner, 0o500)
    return {"directory": directory, "runner": runner}


def _safe_helper_stderr_line(stderr):
    """Return one useful helper error line without serials or filesystem paths."""
    lines = [line.strip() for line in str(stderr or "").splitlines() if line.strip()]
    if not lines:
        return ""
    line = edl_runner._tail("", lines[-1]).splitlines()[-1]
    # The helper's scratch files live outside the user's store. Do not expose
    # any absolute path the helper may include in its diagnostic.
    return re.sub(r"(?<![A-Za-z0-9_.-])/(?:[^\s]+)", "[path]", line)[:240]


def _run_fwlib_phase(engine, edl, env, loader, prepared, work, write,
                     verbose=False, debug=False, session=None):
    env_verbose, env_debug = _edl_verbose(env)
    verbose = verbose or env_verbose
    debug = debug or env_debug
    phase = prepared["phase"]
    try:
        edl_prefix = (session.proxy_command(Path(work) / "fwlib-logs" / "edl-session-proxy.py")
                      if session is not None else
                      [os.fspath(item) for item in edl] if isinstance(edl, (list, tuple)) else shlex.split(edl))
    except ValueError:
        edl_prefix = []
    if (not edl_prefix or any(not Path(item).is_absolute() or not re.fullmatch(r"/[A-Za-z0-9._/-]+", item)
                              for item in edl_prefix)
            or (len(edl_prefix) == 1 and (not Path(edl_prefix[0]).is_file() or not os.access(edl_prefix[0], os.X_OK)))
            or (len(edl_prefix) == 2 and (Path(edl_prefix[0]).resolve() != Path(sys.executable).resolve()
                                         or not Path(edl_prefix[1]).is_file()))
            or len(edl_prefix) > 2):
        raise store.RefusedError("EDL client path cannot be safely passed to the existing rollback helper")
    logs = work / "fwlib-logs"; logs.mkdir(mode=0o700, exist_ok=True)
    helper_home = work / "fwlib-home"; helper_home.mkdir(mode=0o700, exist_ok=True)
    child_env = {"PATH": _fwlib_path(), "HOME": str(helper_home), "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
                 "LDR": str(loader), "W": str(logs), "EDL_CMD": shlex.join(edl_prefix),
                 "PYTHONDONTWRITEBYTECODE": "1"}
    if session is not None and isinstance(session.env, dict):
        for name, value in session.env.items():
            child_env.setdefault(name, value)
    # Never inherit fwlib's test-only loader override from a caller environment.
    child_env.pop("FWLIB_TEST_LDR_SHA", None)
    mode = "write" if write else "dry"
    if write:
        child_env["COMMIT"] = "1"
        child_env["CONFIRM"] = "rollback-" + phase
    command = ["/bin/bash", str(engine["runner"]), phase, mode, phase,
               str(prepared["start"]), str(prepared["end"] - prepared["start"] + 1), str(prepared["source"])]
    print("EDL engine command: " + shlex.join(command))
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                   env=child_env, start_new_session=True)
        try:
            stdout, stderr = process.communicate(timeout=ROLLBACK_ENGINE_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGINT)
            except ProcessLookupError:
                pass
            try:
                stdout, stderr = process.communicate(timeout=edl_runner.INTERRUPT_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    stdout, stderr = process.communicate(timeout=edl_runner.INTERRUPT_GRACE_SECONDS)
                except subprocess.TimeoutExpired as exc:
                    if verbose or debug:
                        tail = edl_runner._tail(exc.output or "", exc.stderr or "")
                        if tail:
                            print("EDL engine output (last lines):\n" + tail, file=sys.stderr)
                    raise store.RefusedError("rollback EDL engine ignored SIGINT and terminate; " + edl_runner.POWER_CYCLE_HINT) from exc
                if verbose or debug:
                    tail = edl_runner._tail(stdout, stderr)
                    if tail:
                        print("EDL engine output (last lines):\n" + tail, file=sys.stderr)
                raise store.RefusedError("rollback EDL engine timed out after SIGINT; " + edl_runner.POWER_CYCLE_HINT)
            if verbose or debug:
                tail = edl_runner._tail(stdout, stderr)
                if tail:
                    print("EDL engine output (last lines):\n" + tail, file=sys.stderr)
            raise store.RefusedError("rollback EDL engine timed out; " + edl_runner.POWER_CYCLE_HINT)
    except OSError as exc:
        raise store.RefusedError("existing rollback helper invocation failed; hint: verify the EDL client path and retry") from exc
    if process.returncode:
        if verbose or debug:
            tail = edl_runner._tail(stdout, stderr)
            if tail:
                print("EDL engine output (last lines):\n" + tail, file=sys.stderr)
        message = "existing rollback helper refused or failed phase " + phase + ". Hint: check EDL mode, USB stability, and the phase log before reconciling state."
        helper_error = _safe_helper_stderr_line(stderr)
        if helper_error:
            message += " Helper: " + helper_error
        raise store.RefusedError(message)


def _verify_final_disk(edl, env, loader, work, manifest, state, session=None):
    final = work / "final-full.bin"
    try:
        _edl(edl, ["rf", str(final), "--loader=" + str(loader), "--memory=emmc"], env, loader,
             timeout=FINAL_FULL_READ_TIMEOUT_SECONDS, verbose=_edl_verbose(env)[0],
             debug=_edl_verbose(env)[1], session=session)
        matched = final.is_file() and final.stat().st_size == manifest["size"] and _sha(final) == manifest["sha256"]
    except (store.RefusedError, edl_runner.EdlRunnerError, OSError, subprocess.TimeoutExpired):
        matched = False
    if not matched:
        print("FAIL final full-disk readback failed or SHA-256 mismatched; do not boot", file=sys.stderr)
        state["status"] = "final-readback-failed"
        raise common.VerificationError("final full-disk readback failed size or SHA-256 comparison")
    print("PASS final full-disk readback exactly matches the verified backup SHA-256")


def _edl(edl, args, env, loader, timeout=EDL_COMMAND_TIMEOUT_SECONDS,
         verbose=False, debug=False, session=None):
    env_verbose, env_debug = _edl_verbose(env)
    verbose = verbose or env_verbose
    debug = debug or env_debug
    operation = []
    for item in args:
        if item.startswith("--loader="):
            if item != "--loader=" + str(loader):
                raise store.RefusedError("EDL phase requested a loader different from the verified loader")
        elif item.startswith("--memory="):
            if item != "--memory=emmc":
                raise store.RefusedError("EDL phase requested a non-eMMC memory target")
        else:
            operation.append(item)
    if session is not None:
        return session.run(operation, timeout=timeout, env=env,
                           verbose=verbose, debug=debug)
    return edl_runner.run(operation, timeout=timeout, env=env, client=edl, loader=loader,
                          verbose=verbose, debug=debug)
