#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
PYTHON=${PYTHON:-python3}
if [[ -z ${INSTALLER_TEST_TMP-} ]]; then
  TEST_TMP=$(mktemp -d /tmp/openstick-installer-tests.XXXXXX)
  mkdir -p "$TEST_TMP/shims/bin"
  cp "$ROOT"/tests/shims/bin/* "$TEST_TMP/shims/bin/"
  chmod u+x "$TEST_TMP/shims/bin/"*
  RUN_HOME=${HOME:-$TEST_TMP/home}
  RUN_LANG=${LANG:-C.UTF-8}
  exec /usr/bin/env -i HOME="$RUN_HOME" PATH="$TEST_TMP/shims/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin" \
    LANG="$RUN_LANG" TMPDIR="$TEST_TMP" INSTALLER_TEST_TMP="$TEST_TMP" \
    /bin/bash "$0" "$@" </dev/null
fi
TMP=$INSTALLER_TEST_TMP
unset INSTALLER_TEST_TMP
trap 'rm -rf -- "$TMP"' EXIT
export TMPDIR="$TMP"
mkdir -p "$TMP/usb/sys/bus/usb/devices" "$TMP/usb/dev/bus/usb"
export OPENSTICK_USB_SYSFS_ROOT="$TMP/usb/sys/bus/usb/devices"
export OPENSTICK_USB_DEVICE_ROOT="$TMP/usb/dev/bus/usb"
export PYTHONPATH="$ROOT/tests/host-path-guard:$ROOT${PYTHONPATH:+:$PYTHONPATH}"
export OPENSTICK_EDL_SESSION_FILE="$TMP/edl-session"
shopt -s nullglob
m4_tmp_before=("$TMP"/openstick-m4-test-*)

pass=0 fail=0 skip=0
run() {
  local name=$1; shift
  if timeout --signal=TERM --kill-after=5 300 "$@" </dev/null; then printf 'PASS %s\n' "$name"; pass=$((pass + 1)); else printf 'FAIL %s\n' "$name"; fail=$((fail + 1)); fi
}

run host-usb-path-guard python3 - "$ROOT" "$TMP" <<'PY'
import os, pathlib, subprocess, sys
root, tmp = sys.argv[1:]
assert not ("OPENSTICK_EDL_DIR" in os.environ or "OPENSTICK_HOME" in os.environ)
assert not any(name.startswith("GIT_") for name in os.environ)
assert os.environ["PATH"].split(os.pathsep)[0].endswith("/shims/bin")
assert os.environ.get("LANG")
assert os.environ.get("TMPDIR")
for path in ("/sys/bus/usb/devices", "/dev/bus/usb"):
    try:
        os.open(path, os.O_RDONLY)
    except RuntimeError as exc:
        assert "TEST SAFETY GUARD" in str(exc), str(exc)
    else:
        raise AssertionError("host USB path access was not blocked")
outside_client = pathlib.Path(root) / "tests/shims/bin/edl"
try:
    subprocess.run([str(outside_client), "rs", "0", "1", str(pathlib.Path(tmp) / "out.bin")],
                   stdin=subprocess.DEVNULL, timeout=5)
except RuntimeError as exc:
    assert "TEST SAFETY GUARD" in str(exc), str(exc)
else:
    raise AssertionError("EDL client outside the test temp directory was allowed")
child_code = (
    "import subprocess, sys; "
    "try: subprocess.run([sys.argv[1], 'rs', '0', '1', sys.argv[2], '--loader=' + sys.argv[3]], "
    "stdin=subprocess.DEVNULL, timeout=5)\n"
    "except RuntimeError as exc: sys.exit(0 if 'TEST SAFETY GUARD' in str(exc) else 3)\n"
    "sys.exit(4)"
)
inside_client = pathlib.Path(tmp) / "shims/bin/edl"
child = subprocess.run([sys.executable, "-c", child_code, str(inside_client), str(pathlib.Path(tmp) / "child.bin"), "/nonexistent/outside-loader"],
                       stdin=subprocess.DEVNULL, timeout=10, capture_output=True, text=True)
assert child.returncode == 0, (child.returncode, child.stdout, child.stderr)
env = dict(os.environ)
env.pop("OPENSTICK_EDL_DIR", None)
p = subprocess.run([sys.executable, str(pathlib.Path(root) / "openstick"),
                    "--home", str(pathlib.Path(tmp) / "default-roots"), "doctor"],
                   env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=20)
assert "TEST SAFETY GUARD" not in p.stdout + p.stderr, (p.stdout, p.stderr)
assert "no EDL device attached" in p.stdout, p.stdout
child = subprocess.run([sys.executable, "-c",
                       "from tools.installer.edl_runner import preflight; assert preflight().state == 'ABSENT'"],
                      env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=20)
assert child.returncode == 0, (child.stdout, child.stderr)
PY

run store-refuses-git python3 - "$ROOT" "$TMP" <<'PY'
import os, pathlib, subprocess, sys
root, tmp = sys.argv[1:]
repo = pathlib.Path(tmp) / "synthetic-repo"
(repo / ".git").mkdir(parents=True)
env = dict(os.environ)
env.pop("GIT_CEILING_DIRECTORIES", None)
p = subprocess.run([root + "/openstick", "--home", str(repo / "store"), "device", "list"],
                   text=True, capture_output=True, env=env, stdin=subprocess.DEVNULL, timeout=20)
assert p.returncode == 2, (p.returncode, p.stdout, p.stderr)
assert "REFUSED" in p.stderr
PY
run store-permissions python3 - "$ROOT" "$TMP" <<'PY'
import json, os, pathlib, sys
from tools.installer.store import ensure_store, save_registry, load_registry, append_event
root = pathlib.Path(sys.argv[2]) / "private"
ensure_store(root)
assert root.stat().st_mode & 0o777 == 0o700
save_registry(root, {"schema": 1, "devices": [{"id": "DEADBEEF", "label": "test"}]})
assert (root / "registry.json").stat().st_mode & 0o777 == 0o600
assert load_registry(root)["devices"][0]["label"] == "test"
append_event(root, {"type": "test", "device_id": "DEADBEEF"})
assert (root / "events.jsonl").stat().st_mode & 0o777 == 0o600
events = [json.loads(line) for line in (root / "events.jsonl").read_text().splitlines()]
assert events == [{"type": "test", "device_id": "DEADBEEF"}]
PY
run store-refuses-symlink python3 - "$ROOT" "$TMP" <<'PY'
import pathlib, sys
from tools.installer.store import ensure_store, RefusedError
tmp = pathlib.Path(sys.argv[2]); target = tmp / "target"; target.mkdir()
link = tmp / "linked"; link.symlink_to(target, target_is_directory=True)
try: ensure_store(link / "store")
except RefusedError: pass
else: raise AssertionError("symlink path was accepted")
PY
run registry-device-operations python3 - "$ROOT" "$TMP" <<'PY'
import json, pathlib, subprocess, sys
root, tmp = sys.argv[1:]
home = pathlib.Path(tmp) / "registry"
from tools.installer.store import ensure_store, save_registry
ensure_store(home)
save_registry(home, {"schema": 1, "devices": [{"id": "DEADBEEF", "label": "stick", "note": ""}]})
for args in (("device", "label", "DEADBEEF", "desk"), ("device", "note", "DEADBEEF", "baseline backup")):
 p = subprocess.run([root + "/openstick", "--home", str(home), *args], capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=20)
 assert p.returncode == 0, (p.stdout, p.stderr)
p = subprocess.run([root + "/openstick", "--home", str(home), "device", "show", "DEADBEEF"], capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=20)
d = json.loads(p.stdout)
assert d["label"] == "desk" and d["note"] == "baseline backup"
assert (home / "events.jsonl").read_text().count("\n") == 2
PY
run doctor-missing-dependencies python3 - "$ROOT" "$TMP" <<'PY'
import os, pathlib, subprocess, sys
root, tmp = sys.argv[1:]
sysfs = pathlib.Path(tmp) / "sysfs"; sysfs.mkdir()
dev = pathlib.Path(tmp) / "dev"; dev.mkdir()
env = dict(os.environ, PATH="/nonexistent", OPENSTICK_EDL_DIR=str(pathlib.Path(tmp) / "absent"))
p = subprocess.run([sys.executable, root + "/openstick", "--usb-sysfs-root", str(sysfs),
                    "--usb-device-root", str(dev), "--home", str(pathlib.Path(tmp) / "doctor"), "doctor"],
                   env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=20)
assert p.returncode == 1
for term in ("python3", "bash", "e2fsprogs", "fastboot", "EDL client", "Firehose loader"):
 assert "FAIL " + term in p.stdout, (term, p.stdout)
PY
run shim-fastboot-fails-closed env OPENSTICK_FAKE_DEVICE=1 PATH="$ROOT/tests/shims/bin:/usr/bin:/bin" fastboot devices
run shim-build-script-stub env PATH="$ROOT/tests/shims/bin:/usr/bin:/bin" 50-boot-img.sh
run fake-emmc-deterministic python3 - "$ROOT" "$TMP" <<'PY'
import hashlib, pathlib, subprocess, sys
root, tmp = sys.argv[1:]
a, b = pathlib.Path(tmp)/"a.img", pathlib.Path(tmp)/"b.img"
for p in (a, b): subprocess.check_call([sys.executable, str(pathlib.Path(root)/"tests/make-fake-emmc.py"), str(p), "--mib", "2"], stdin=subprocess.DEVNULL, timeout=20)
assert hashlib.sha256(a.read_bytes()).digest() == hashlib.sha256(b.read_bytes()).digest()
assert a.read_bytes()[512:520] == b"EFI PART"
assert "userdata".encode("utf-16le") in a.read_bytes()
PY
run m2-backup-edl-runner python3 "$ROOT/tests/verify-installer-m2.py"
run m2-doctor-edl-usb python3 "$ROOT/tests/verify-installer-doctor.py"
run m2-edl-session-lifecycle python3 "$ROOT/tests/verify-installer-edl-session.py"
run shim-edl-read-and-failure-injection python3 - "$ROOT" "$TMP" <<'PY'
import os, pathlib, subprocess, sys
root, tmp = sys.argv[1:]
tmp = pathlib.Path(tmp); disk = tmp / "disk.img"; out = tmp / "read.bin"
subprocess.check_call([sys.executable, str(pathlib.Path(root)/"tests/make-fake-emmc.py"), str(disk), "--mib", "2"], stdin=subprocess.DEVNULL, timeout=20)
loader = tmp / "synthetic-loader.mbn"; loader.write_bytes(b"test-only shim loader")
env = dict(os.environ, OPENSTICK_FAKE_EMMC=str(disk), PATH=str(pathlib.Path(root)/"tests/shims/bin") + ":/usr/bin:/bin")
subprocess.check_call(["edl", "rs", "0", "2", str(out), "--loader=" + str(loader), "--memory=emmc"], env=env, stdin=subprocess.DEVNULL, timeout=20)
assert out.stat().st_size == 1024
env["OPENSTICK_EDL_CALL_FILE"] = str(tmp / "calls"); env["OPENSTICK_EDL_FAIL_AT"] = "1"
p = subprocess.run(["edl", "rs", "0", "2", str(tmp / "failed.bin"), "--loader=" + str(loader), "--memory=emmc"], env=env, capture_output=True, stdin=subprocess.DEVNULL, timeout=20)
assert p.returncode == 90 and b"injected failure" in p.stderr
PY

run m3-configure-seed-apply-injection-push python3 "$ROOT/tests/verify-installer-m3.py" "$ROOT"
run m4-build-flash-wrappers python3 "$ROOT/tests/verify-installer-m4.py"
run m5-verify-restore python3 "$ROOT/tests/verify-installer-m5.py"
run m6-portable-packages python3 "$ROOT/tests/verify-installer-m6.py"
run m7-wizard python3 "$ROOT/tests/verify-installer-m7.py"

# Retain the M0 passing regression set. Known baseline failures remain documented and are not installer failures.
for test in verify-fetch-firehose.sh verify-flash-board.sh verify-harden.sh verify-initramfs-fail.sh verify-nft-ruleset.sh verify-rollback-plan.sh verify-sshd-config.sh; do
  if [[ $test == verify-rollback-plan.sh ]]; then
    run "legacy-$test" env REAL_BACKUP="$TMP/no-real-backup" REAL_GPT_DIR="$TMP/no-real-gpt" bash "$ROOT/tests/$test"
  else
    run "legacy-$test" bash "$ROOT/tests/$test"
  fi
done

m4_tmp_after=("$TMP"/openstick-m4-test-*)
if (( ${#m4_tmp_after[@]} > ${#m4_tmp_before[@]} )); then
  printf 'FAIL installer temp cleanup: openstick-m4-test-* count grew from %d to %d under TMPDIR\n' \
    "${#m4_tmp_before[@]}" "${#m4_tmp_after[@]}"
  fail=$((fail + 1))
fi

printf 'TOTAL PASS=%d FAIL=%d SKIP=%d\n' "$pass" "$fail" "$skip"
(( fail == 0 ))
