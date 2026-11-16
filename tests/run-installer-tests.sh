#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
PYTHON=${PYTHON:-python3}
TMP=$(mktemp -d)
trap 'rm -rf -- "$TMP"' EXIT
export TMPDIR="$TMP"
export GIT_CEILING_DIRECTORIES="$(dirname "$TMP")"
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
shopt -s nullglob
m4_tmp_before=("$TMP"/openstick-m4-test-*)

pass=0 fail=0 skip=0
run() {
  local name=$1; shift
  if "$@"; then printf 'PASS %s\n' "$name"; pass=$((pass + 1)); else printf 'FAIL %s\n' "$name"; fail=$((fail + 1)); fi
}

run store-refuses-git python3 - "$ROOT" "$TMP" <<'PY'
import subprocess, sys
root, tmp = sys.argv[1:]
p = subprocess.run([root + "/openstick", "--home", root + "/.m1-test-store", "device", "list"], text=True, capture_output=True)
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
 p = subprocess.run([root + "/openstick", "--home", str(home), *args], capture_output=True, text=True)
 assert p.returncode == 0, (p.stdout, p.stderr)
p = subprocess.run([root + "/openstick", "--home", str(home), "device", "show", "DEADBEEF"], capture_output=True, text=True)
d = json.loads(p.stdout)
assert d["label"] == "desk" and d["note"] == "baseline backup"
assert (home / "events.jsonl").read_text().count("\n") == 2
PY
run doctor-missing-dependencies python3 - "$ROOT" "$TMP" <<'PY'
import os, pathlib, subprocess, sys
root, tmp = sys.argv[1:]
env = dict(os.environ, PATH="/nonexistent", OPENSTICK_EDL_DIR=str(pathlib.Path(tmp) / "absent"))
p = subprocess.run([sys.executable, root + "/openstick", "--home", str(pathlib.Path(tmp) / "doctor"), "doctor"], env=env, capture_output=True, text=True)
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
for p in (a, b): subprocess.check_call([sys.executable, str(pathlib.Path(root)/"tests/make-fake-emmc.py"), str(p), "--mib", "2"])
assert hashlib.sha256(a.read_bytes()).digest() == hashlib.sha256(b.read_bytes()).digest()
assert a.read_bytes()[512:520] == b"EFI PART"
assert "userdata".encode("utf-16le") in a.read_bytes()
PY
run m2-backup-edl-runner python3 "$ROOT/tests/verify-installer-m2.py"
run shim-edl-read-and-failure-injection python3 - "$ROOT" "$TMP" <<'PY'
import os, pathlib, subprocess, sys
root, tmp = sys.argv[1:]
tmp = pathlib.Path(tmp); disk = tmp / "disk.img"; out = tmp / "read.bin"
subprocess.check_call([sys.executable, str(pathlib.Path(root)/"tests/make-fake-emmc.py"), str(disk), "--mib", "2"])
loader = tmp / "synthetic-loader.mbn"; loader.write_bytes(b"test-only shim loader")
env = dict(os.environ, OPENSTICK_FAKE_EMMC=str(disk), PATH=str(pathlib.Path(root)/"tests/shims/bin") + ":/usr/bin:/bin")
subprocess.check_call(["edl", "rs", "0", "2", str(out), "--loader=" + str(loader), "--memory=emmc"], env=env)
assert out.stat().st_size == 1024
env["OPENSTICK_EDL_CALL_FILE"] = str(tmp / "calls"); env["OPENSTICK_EDL_FAIL_AT"] = "1"
p = subprocess.run(["edl", "rs", "0", "2", str(tmp / "failed.bin"), "--loader=" + str(loader), "--memory=emmc"], env=env, capture_output=True)
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
