#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
PYTHON=${PYTHON:-python3}
# Stage handling (R-5.2). Externally supplied INSTALLER_TEST_TMP / INSTALLER_TEST_TMP_PARENT are
# never read. Stage 2 is recognised only by a private nonce that stage 1 wrote INSIDE the temp dir
# it created itself (and passed through env -i), so a caller cannot skip the scrub or abort the
# script by exporting any of these variables.
STAGE2_OK=0
if [[ -n ${_OSRUNNER_TMP-} && -n ${_OSRUNNER_NONCE-} && -n ${_OSRUNNER_PARENT-} \
      && -f ${_OSRUNNER_TMP}/.runner-nonce && -O ${_OSRUNNER_TMP} ]] \
   && [[ $(cat -- "${_OSRUNNER_TMP}/.runner-nonce" 2>/dev/null) == "${_OSRUNNER_NONCE}" ]]; then
  STAGE2_OK=1
fi
if (( STAGE2_OK == 0 )); then
  TEST_TMP_PARENT=$(cd -- "${TMPDIR:-/tmp}" && pwd -P)
  TEST_TMP=$(mktemp -d "$TEST_TMP_PARENT/openstick-installer-tests.XXXXXX")
  NONCE="$$.$RANDOM.$RANDOM.$(date +%s%N)"
  printf '%s' "$NONCE" > "$TEST_TMP/.runner-nonce"
  mkdir -p "$TEST_TMP/shims/bin"
  cp "$ROOT"/tests/shims/bin/* "$TEST_TMP/shims/bin/"
  chmod u+x "$TEST_TMP/shims/bin/"*
  RUN_HOME=${HOME:-$TEST_TMP/home}
  RUN_LANG=${LANG:-C.UTF-8}
  exec /usr/bin/env -i HOME="$RUN_HOME" PATH="$TEST_TMP/shims/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin" \
    LANG="$RUN_LANG" TMPDIR="$TEST_TMP" _OSRUNNER_TMP="$TEST_TMP" _OSRUNNER_NONCE="$NONCE" \
    _OSRUNNER_PARENT="$TEST_TMP_PARENT" \
    /bin/bash "$0" "$@" </dev/null
fi
TMP=$_OSRUNNER_TMP
TMP_PARENT=$_OSRUNNER_PARENT
unset OPENSTICK_USB_SYSFS_ROOT OPENSTICK_USB_DEVICE_ROOT OPENSTICK_TEST_USB_ROOTS_DIR _OSRUNNER_TMP _OSRUNNER_NONCE _OSRUNNER_PARENT INSTALLER_TEST_TMP INSTALLER_TEST_TMP_PARENT
trap 'rm -rf -- "$TMP"' EXIT
export TMPDIR="$TMP"
export GIT_CEILING_DIRECTORIES="$(dirname "$TMP")"
mkdir -p "$TMP/usb/sys/bus/usb/devices" "$TMP/usb/dev/bus/usb" "$TMP/counts"
export OPENSTICK_USB_SYSFS_ROOT="$TMP/usb/sys/bus/usb/devices"
export OPENSTICK_USB_DEVICE_ROOT="$TMP/usb/dev/bus/usb"
# Marker that makes the product honour the two roots above (only roots inside this dir are accepted).
export OPENSTICK_TEST_USB_ROOTS_DIR="$TMP"
export PYTHONPATH="$ROOT/tests/host-path-guard:$ROOT${PYTHONPATH:+:$PYTHONPATH}"
export OPENSTICK_EDL_SESSION_FILE="$TMP/edl-session"
# Runner-visible guard hit file (R-5.4): any line in it fails the whole run.
export OPENSTICK_GUARD_HIT_FILE="$TMP/guard-hits"
: > "$OPENSTICK_GUARD_HIT_FILE"
shopt -s nullglob
m4_tmp_before=("$TMP"/openstick-m4-test-*)

ONLY=
if [[ ${1-} == --only ]]; then ONLY=${2:?--only needs an extended regex}; fi
pass=0 fail=0 skip=0
GROUP_TIMEOUT=300
GROUP_GRACE=5
CANARY=0
LAST_STATUS=
LAST_COUNT=0
declare -a SUMMARY=()
# run NAME CMD...      one group, own process group (tests/run-group.py), timeout 300s, TERM then KILL after grace.
# runx NAME CMD...     same, but the group is a Python file run through the assert-counting harness.
run() {
  local name=$1; shift
  if (( CANARY == 0 )) && [[ -n $ONLY ]] && ! [[ $name =~ $ONLY ]]; then return 0; fi
  local rc=0 count=0 extra=()
  [[ ${EXIT_CHECK:-0} == 1 ]] && extra=(--exit-check)
  "$PYTHON" "$ROOT/tests/run-group.py" --count-file "$TMP/counts/$name" --timeout "$GROUP_TIMEOUT" \
    --grace "$GROUP_GRACE" "${extra[@]}" -- "$@" </dev/null >"$TMP/last-group.log" 2>&1 || rc=$?
  if (( CANARY == 0 )); then cat -- "$TMP/last-group.log"; fi
  count=$(cat "$TMP/counts/$name" 2>/dev/null || echo 0)
  LAST_COUNT=$count
  if (( rc == 242 )); then LAST_STATUS="FAIL (timeout)"
  elif (( rc == 243 )); then LAST_STATUS="FAIL (orphan processes left behind)"
  elif (( rc != 0 )); then LAST_STATUS=FAIL
  elif (( count == 0 )); then LAST_STATUS="FAIL (vacuous: 0 checks ran)"
  else LAST_STATUS=PASS; fi
  if (( CANARY == 1 )); then return 0; fi
  printf '%s %s (checks=%s)\n' "$LAST_STATUS" "$name" "$count"
  SUMMARY+=("$name $count")
  if [[ $LAST_STATUS == PASS ]]; then pass=$((pass + 1)); else fail=$((fail + 1)); fi
}
runx() { local name=$1 file=$2; shift 2; run "$name" "$PYTHON" "$ROOT/tests/groups/_harness.py" "$ROOT/tests/groups/$file" "$@"; }

# Canaries (R-5.1): groups known to be broken must come out as FAIL through the very same run() path.
# If either passes, the runner is blind (vacuous pass) and the whole run aborts.
CANARY=1
runx canary-fail canary-fail.py "$ROOT" "$TMP"
canary_fail_status=$LAST_STATUS
runx canary-vacuous canary-vacuous.py "$ROOT" "$TMP"
canary_vacuous_status=$LAST_STATUS
CANARY=0
if [[ $canary_fail_status != FAIL || $canary_vacuous_status != "FAIL (vacuous: 0 checks ran)" ]]; then
  printf 'ABORT: vacuous pass detected (canary-fail=%s canary-vacuous=%s); this runner cannot be trusted\n' \
    "$canary_fail_status" "$canary_vacuous_status"
  exit 2
fi
printf 'CANARY ok: failing group => FAIL, assertion-free group => vacuous FAIL\n'

# Timeout and orphan self-test (R-5.3): a group that hangs and leaves a TERM-ignoring grandchild.
GROUP_TIMEOUT=3 GROUP_GRACE=1 CANARY=1
RANDOM_TAG=$RANDOM
selftest_marker="orphan-marker-$$-$RANDOM_TAG"
cat > "$TMP/hang-group.py" <<PYH
import signal, subprocess, sys, time
child_code = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(600)  # $selftest_marker"
subprocess.Popen([sys.executable, "-c", child_code])
time.sleep(600)
PYH
run timeout-selftest "$PYTHON" "$TMP/hang-group.py"
timeout_status=$LAST_STATUS
GROUP_TIMEOUT=300 GROUP_GRACE=5 CANARY=0
sleep 0.3
# the bracket keeps grep's own command line from matching its own pattern
orphans=$(grep -l -- "orphan-[m]arker-$$-$RANDOM_TAG" /proc/[0-9]*/cmdline 2>/dev/null | wc -l || true)
if [[ $timeout_status != "FAIL (timeout)" ]] || (( orphans != 0 )); then
  printf 'ABORT: timeout self-test failed (status=%s orphans=%s)\n' "$timeout_status" "$orphans"
  exit 2
fi
printf 'SELFTEST ok: hanging group => FAIL (timeout), no orphan process left\n'

runx host-usb-path-guard host-usb-path-guard.py "$ROOT" "$TMP" "$TMP_PARENT"

runx store-refuses-git store-refuses-git.py "$ROOT" "$TMP"
runx store-permissions store-permissions.py "$ROOT" "$TMP"
runx store-refuses-symlink store-refuses-symlink.py "$ROOT" "$TMP"
runx registry-device-operations registry-device-operations.py "$ROOT" "$TMP"
runx doctor-missing-dependencies doctor-missing-dependencies.py "$ROOT" "$TMP"
EXIT_CHECK=1 run shim-fastboot-fails-closed env OPENSTICK_FAKE_DEVICE=1 PATH="$ROOT/tests/shims/bin:/usr/bin:/bin" fastboot devices
EXIT_CHECK=1 run shim-build-script-stub env PATH="$ROOT/tests/shims/bin:/usr/bin:/bin" 50-boot-img.sh
runx guard-coverage guard-coverage.py "$ROOT" "$TMP"
runx guard-known-limits guard-known-limits.py "$ROOT" "$TMP" "$TMP_PARENT"
runx fake-emmc-deterministic fake-emmc-deterministic.py "$ROOT" "$TMP"
run m2-backup-edl-runner python3 "$ROOT/tests/verify-installer-m2.py"
run m2-doctor-edl-usb python3 "$ROOT/tests/verify-installer-doctor.py"
run m2-edl-session-lifecycle python3 "$ROOT/tests/verify-installer-edl-session.py"
runx shim-edl-read-and-failure-injection shim-edl-read-and-failure-injection.py "$ROOT" "$TMP"

run m3-configure-seed-apply-injection-push python3 "$ROOT/tests/verify-installer-m3.py" "$ROOT"
run m4-build-flash-wrappers python3 "$ROOT/tests/verify-installer-m4.py"
run m5-verify-restore python3 "$ROOT/tests/verify-installer-m5.py"
run m6-portable-packages "$PYTHON" "$ROOT/tests/groups/_harness.py" "$ROOT/tests/verify-installer-m6.py"
run m7-wizard python3 "$ROOT/tests/verify-installer-m7.py"

# easy mode (round 6): scripted stdin through a pty against the fake board (shim edl with opt-in writes).
runx easy-default-happy-path easy-default-happy-path.py "$ROOT" "$TMP"
runx easy-custom-seed easy-custom-seed.py "$ROOT" "$TMP"
runx easy-reuse-backup easy-reuse-backup.py "$ROOT" "$TMP"
runx easy-poll-timeout easy-poll-timeout.py "$ROOT" "$TMP"
runx easy-phase-failure easy-phase-failure.py "$ROOT" "$TMP"
runx easy-yes-flag easy-yes-flag.py "$ROOT" "$TMP"
runx easy-image-set easy-image-set.py "$ROOT" "$TMP"
runx easy-build-id easy-build-id.py "$ROOT" "$TMP"

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

if [[ -s $OPENSTICK_GUARD_HIT_FILE ]]; then
  printf 'FAIL guard hit file is not empty (the EDL/USB guard fired outside a deliberate self-test):\n'
  sed 's/^/  guard-hit: /' "$OPENSTICK_GUARD_HIT_FILE"
  fail=$((fail + 1))
fi
printf 'CHECKS PER GROUP:\n'
printf '  %s\n' "${SUMMARY[@]}"
printf 'TOTAL PASS=%d FAIL=%d SKIP=%d\n' "$pass" "$fail" "$skip"
(( fail == 0 ))
