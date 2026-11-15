#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
PYTHON=${PYTHON:-python3}
TMP=$(mktemp -d)
trap 'rm -rf -- "$TMP"' EXIT
export TMPDIR="$TMP"
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"

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
run backup-success-and-integrity python3 - "$ROOT" "$TMP" <<'PY'
import hashlib,json,os,pathlib,subprocess,sys
root,tmp=pathlib.Path(sys.argv[1]),pathlib.Path(sys.argv[2]); disk=tmp/'backup-disk'; home=tmp/'backup-home'
subprocess.check_call([sys.executable,str(root/'tests/make-fake-emmc.py'),str(disk),'--mib','2'])
env=dict(os.environ,OPENSTICK_FAKE_EMMC=str(disk),PATH=str(root/'tests/shims/bin')+':/usr/bin:/bin')
cmd=[str(root/'openstick'),'--home',str(home),'backup']
p=subprocess.run(cmd,env=env,text=True,capture_output=True); assert p.returncode==0,(p.stdout,p.stderr)
dev=json.loads((home/'registry.json').read_text())['devices'][0]; b=home/'backups'/'deadbeef'/dev['backups'][0]['id']; m=json.loads((b/'manifest.json').read_text())
assert m['size']==disk.stat().st_size and m['sha256']==hashlib.sha256(disk.read_bytes()).hexdigest()
assert m['identity']=={n:hashlib.sha256(disk.read_bytes()[s*512:(e+1)*512]).hexdigest() for n,(s,e) in {'fsg':(34,35),'sec':(36,37),'fsc':(38,39)}.items()}
assert (b/'SHA256SUMS').read_text().startswith(m['sha256'])
assert (home/'events.jsonl').read_text().count('backup.verified')==1
assert (home.stat().st_mode&0o777)==0o700 and (b/'emmc-full.bin').stat().st_mode&0o777==0o600
PY
run backup-duplicate-and-again python3 - "$ROOT" "$TMP" <<'PY'
import os,pathlib,subprocess,sys
root,tmp=pathlib.Path(sys.argv[1]),pathlib.Path(sys.argv[2]); disk=tmp/'again-disk'; home=tmp/'again-home'
subprocess.check_call([sys.executable,str(root/'tests/make-fake-emmc.py'),str(disk),'--mib','2'])
env=dict(os.environ,OPENSTICK_FAKE_EMMC=str(disk),PATH=str(root/'tests/shims/bin')+':/usr/bin:/bin')
base=[str(root/'openstick'),'--home',str(home),'backup']
assert subprocess.run(base,env=env,capture_output=True).returncode==0
assert subprocess.run(base,env=env,capture_output=True).returncode==2
assert subprocess.run(base+['--again'],env=env,capture_output=True).returncode==0
PY
run backup-paranoid-success python3 - "$ROOT" "$TMP" <<'PY'
import os,pathlib,subprocess,sys
root,tmp=pathlib.Path(sys.argv[1]),pathlib.Path(sys.argv[2]); disk=tmp/'paranoid-disk'; home=tmp/'paranoid-home'
subprocess.check_call([sys.executable,str(root/'tests/make-fake-emmc.py'),str(disk),'--mib','2'])
env=dict(os.environ,OPENSTICK_FAKE_EMMC=str(disk),PATH=str(root/'tests/shims/bin')+':/usr/bin:/bin')
p=subprocess.run([str(root/'openstick'),'--home',str(home),'backup','--paranoid'],env=env,capture_output=True)
assert p.returncode==0,p.stderr
assert 'paranoid-second-read' in (home/'backups'/'deadbeef'/next((home/'backups'/'deadbeef').iterdir()).name/'manifest.json').read_text()
PY
run backup-failures-cleanup python3 - "$ROOT" "$TMP" <<'PY'
import json,os,pathlib,subprocess,sys
root,tmp=pathlib.Path(sys.argv[1]),pathlib.Path(sys.argv[2]); shims=str(root/'tests/shims/bin')+':/usr/bin:/bin'
for case,extra,want in [('truncated',{'OPENSTICK_EDL_SHORT_READ':'1'},3),('midread',{'OPENSTICK_EDL_FAIL_AT':'2'},1)]:
 disk=tmp/(case+'-disk'); home=tmp/(case+'-home'); subprocess.check_call([sys.executable,str(root/'tests/make-fake-emmc.py'),str(disk),'--mib','2'])
 env=dict(os.environ,OPENSTICK_FAKE_EMMC=str(disk),PATH=shims,OPENSTICK_EDL_CALL_FILE=str(tmp/(case+'-calls')));env.update(extra)
 p=subprocess.run([str(root/'openstick'),'--home',str(home),'backup'],env=env,capture_output=True)
 assert p.returncode==want,(case,p.returncode,p.stderr)
 assert not (home/'registry.json').exists() or not json.loads((home/'registry.json').read_text())['devices']
 assert not list((home/'backups'/'deadbeef').glob('*/manifest.json')) if (home/'backups'/'deadbeef').exists() else True
PY
run backup-gpt-corruption-and-paranoid-mismatch python3 - "$ROOT" "$TMP" <<'PY'
import json,os,pathlib,subprocess,sys
root,tmp=pathlib.Path(sys.argv[1]),pathlib.Path(sys.argv[2]); shims=str(root/'tests/shims/bin')+':/usr/bin:/bin'
for label,offset in [('primary',512+16),('backup',2*1024*1024-512+16)]:
 disk=tmp/(label+'-gpt-disk');home=tmp/(label+'-gpt-home');subprocess.check_call([sys.executable,str(root/'tests/make-fake-emmc.py'),str(disk),'--mib','2'])
 env=dict(os.environ,OPENSTICK_FAKE_EMMC=str(disk),PATH=shims,OPENSTICK_EDL_CORRUPT_OFFSET=str(offset))
 p=subprocess.run([str(root/'openstick'),'--home',str(home),'backup'],env=env,capture_output=True);assert p.returncode==3,(label,p.returncode,p.stderr)
 assert not (home/'registry.json').exists() or not json.loads((home/'registry.json').read_text())['devices']
disk=tmp/'mismatch-disk';home=tmp/'mismatch-home';subprocess.check_call([sys.executable,str(root/'tests/make-fake-emmc.py'),str(disk),'--mib','2'])
env=dict(os.environ,OPENSTICK_FAKE_EMMC=str(disk),PATH=shims,OPENSTICK_EDL_CALL_FILE=str(tmp/'mismatch-calls'),OPENSTICK_EDL_CORRUPT_OFFSET='0',OPENSTICK_EDL_CORRUPT_AT_CALL='3')
p=subprocess.run([str(root/'openstick'),'--home',str(home),'backup','--paranoid'],env=env,capture_output=True);assert p.returncode==3
PY
run shim-edl-read-and-failure-injection python3 - "$ROOT" "$TMP" <<'PY'
import os, pathlib, subprocess, sys
root, tmp = sys.argv[1:]
tmp = pathlib.Path(tmp); disk = tmp / "disk.img"; out = tmp / "read.bin"
subprocess.check_call([sys.executable, str(pathlib.Path(root)/"tests/make-fake-emmc.py"), str(disk), "--mib", "2"])
env = dict(os.environ, OPENSTICK_FAKE_EMMC=str(disk), PATH=str(pathlib.Path(root)/"tests/shims/bin") + ":/usr/bin:/bin")
subprocess.check_call(["edl", "rs", "0", "2", str(out)], env=env)
assert out.stat().st_size == 1024
env["OPENSTICK_EDL_CALL_FILE"] = str(tmp / "calls"); env["OPENSTICK_EDL_FAIL_AT"] = "1"
p = subprocess.run(["edl", "rs", "0", "2", str(tmp / "failed.bin")], env=env, capture_output=True)
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

printf 'TOTAL PASS=%d FAIL=%d SKIP=%d\n' "$pass" "$fail" "$skip"
(( fail == 0 ))
