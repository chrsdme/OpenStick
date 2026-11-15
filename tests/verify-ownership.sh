#!/bin/sh
# SPDX-License-Identifier: MIT
# Rootless regression test for the "uid 1000 / group-writable files in the image" defect (FLASH-PLAN.md section 9).
#   1. detector self-test (negative and positive controls, tests/lib-ownership.sh)
#   2. OLD copy logic (cp -a) under fakeroot MUST be flagged (proves this test reproduces the defect)
#   3. NEW copy logic (install_tree from scripts/common.sh) under fakeroot MUST be clean
# It does NOT prove a real build is clean: that is tests/verify-rootfs.sh and tests/verify-images.sh after `sudo scripts/20-rootfs.sh`.
. "$(dirname "$0")/../scripts/common.sh"
. "$ROOT/tests/lib-ownership.sh"
command -v fakeroot >/dev/null || die "fakeroot not found"
fail=0; umask 022   # as in 20-rootfs.sh
own_selftest || fail=1
T=$(mktemp -d "${TMPDIR:-/tmp}/verify-ownership.XXXXXX"); trap 'rm -rf --one-file-system -- "${T:?}"' EXIT INT TERM
export ROOT T
# common.sh derives ROOT from $0, so inside `sh -c` only the function itself is imported
INSTALL_TREE=$(sed -n '/^install_tree() {/,/^}/p' "$ROOT/scripts/common.sh"); [ -n "$INSTALL_TREE" ] || die "install_tree not found in common.sh"
export INSTALL_TREE
# a source tree owned by the invoking user with group-writable modes mimics the repo checkout of the defect
SRC=$T/src; mkdir -p "$SRC"; cp -a "$ROOT/configs/system" "$ROOT/configs/templates" "$SRC/"
chmod -R g+w "$SRC"
run() { # label, copy commands; prints the scan of the result, returns the scan status
    rm -rf --one-file-system -- "${T:?}/img"; mkdir -p "$T/img/etc/systemd/system" "$T/img/etc/gt"
    export COPY="$2"
    fakeroot -- sh -eu -c '
        chown -R 1000:1000 "$T/src"   # fakeroot reports files of the invoking user as 0/0; make the build-host uid 1000 explicit
        die() { echo "ERROR: $*" >&2; exit 1; }
        eval "$INSTALL_TREE"
        eval "$COPY"
        . "$ROOT/tests/lib-ownership.sh"
        own_scan_dir "$T/img"'
}
run old 'cp -a "$T/src/system/"* "$T/img/etc/systemd/system/"; cp -a "$T/src/templates/." "$T/img/etc/gt/"' >"$T/old.out" 2>&1 && oldrc=0 || oldrc=1
if [ "$oldrc" = 0 ] || ! grep -q 'VIOLATION owner 1000/1000' "$T/old.out" || ! grep -q 'VIOLATION group/world-writable' "$T/old.out"
then echo "FAIL old cp -a logic not flagged for BOTH owner 1000 and group-writable (defect not reproduced):"; head -5 "$T/old.out"; fail=1
else echo "OK   old cp -a logic flagged: $(grep -c 'VIOLATION owner' "$T/old.out") owner + $(grep -c 'VIOLATION group/world' "$T/old.out") mode violations (negative control)"; fi
# note: `install` without -o/-g cannot be distinguished here (fakeroot and a real root build both create root-owned files); the owner defect came from cp -a preserving uid 1000
if run new 'install_tree "$T/src/system" "$T/img/etc/systemd/system"; install_tree "$T/src/templates" "$T/img/etc/gt"' >"$T/new.out" 2>&1
then echo "OK   install_tree output is root:root, no group/world-writable (fakeroot, source tree set to 1000:1000 and g+w)"
else echo "FAIL install_tree output has violations:"; head -20 "$T/new.out"; fail=1; fi
exit $fail
