#!/bin/sh
# SPDX-License-Identifier: MIT
# Rootless test of configs/openstick-harden against a fake root dir (ROOT_PREFIX). No sudo, no systemd.
. "$(dirname "$0")/../scripts/common.sh"
H=$ROOT/configs/openstick-harden
fail=0
T=$(mktemp -d "${TMPDIR:-/tmp}/verify-harden.XXXXXX"); trap 'rm -rf --one-file-system -- "${T:?}"' EXIT INT TERM
chk() { if eval "$2"; then echo "OK   $1"; else echo "FAIL $1"; fail=1; fi; }
KEY='ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl'
mkfake() { # dir: dev-style tree
    rm -rf --one-file-system -- "${1:?}"; mkdir -p "$1/etc/openstick" "$1/etc/ssh/sshd_config.d" "$1/root" "$1/home/openstick" \
        "$1/etc/systemd/system/serial-getty@ttyMSM0.service.d" "$1/etc/systemd/system/serial-getty@ttyGS0.service.d" "$1/etc/systemd/system/getty.target.wants"
    printf 'PasswordAuthentication no\nMatch User openstick\n    PasswordAuthentication yes\n' > "$1/etc/ssh/sshd_config.d/10-openstick-hardening.conf"
    printf 'root::19000:0:99999:7:::\nopenstick:$y$abc$def:0:0:99999:7:::\ndaemon:*:19000:0:99999:7:::\n' > "$1/etc/shadow"
    printf 'PROFILE=dev\nTEST_USER=openstick\n' > "$1/etc/openstick/profile"
    printf 'DEV IMAGE - not for production. Run: openstick-harden\n' > "$1/etc/motd"
    echo '[Service]' > "$1/etc/systemd/system/serial-getty@ttyMSM0.service.d/override.conf"
    echo '[Service]' > "$1/etc/systemd/system/serial-getty@ttyGS0.service.d/override.conf"
    ln -s /lib/systemd/system/serial-getty@.service "$1/etc/systemd/system/getty.target.wants/serial-getty@ttyGS0.service"
}
snap() { ( cd "$1" && find . | sort && cat etc/shadow etc/motd etc/openstick/profile ) | sha256sum; }
run() { ROOT_PREFIX=$1 sh "$H" "${2:-}"; }

# 1) no key anywhere: ABORT, nothing changed
F=$T/nokey; mkfake "$F"; B=$(snap "$F")
run "$F" >"$T/o1" 2>&1 && _rc=0 || _rc=$?
chk "no authorized_keys: exits non-zero ($_rc)" "[ $_rc -ne 0 ]"
chk "no authorized_keys: ABORT message" "grep -q ABORT $T/o1"
chk "no authorized_keys: tree unchanged" "[ \"\$(snap $F)\" = \"$B\" ]"
# empty authorized_keys is still no key
mkdir -p "$F/root/.ssh"; : > "$F/root/.ssh/authorized_keys"; mkdir -p "$F/home/openstick/.ssh"; echo '# only a comment' > "$F/home/openstick/.ssh/authorized_keys"
chk "empty/comment-only authorized_keys still aborts" "! run $F >/dev/null 2>&1"
chk "dry-run without keys still aborts" "! run $F --dry-run >/dev/null 2>&1"

# 2) dry-run with a key: prints actions, changes nothing
F=$T/dry; mkfake "$F"; mkdir -p "$F/root/.ssh"; echo "$KEY" > "$F/root/.ssh/authorized_keys"; B=$(snap "$F")
run "$F" --dry-run >"$T/o2" 2>&1 && _rc=0 || _rc=$?
chk "dry-run with key: exit 0" "[ $_rc -eq 0 ]"
chk "dry-run prints actions" "grep -q 'would: lock root' $T/o2 && grep -q 'would: mask serial-getty@ttyGS0' $T/o2"
chk "dry-run changes nothing" "[ \"\$(snap $F)\" = \"$B\" ]"

# 3) key for root: full hardening
F=$T/root; mkfake "$F"; mkdir -p "$F/root/.ssh"; echo "$KEY" > "$F/root/.ssh/authorized_keys"
chk "root key: harden succeeds" "run $F >$T/o3 2>&1"
chk "autologin drop-ins removed" "! test -e $F/etc/systemd/system/serial-getty@ttyMSM0.service.d && ! test -e $F/etc/systemd/system/serial-getty@ttyGS0.service.d"
chk "root locked (was empty, now '!')" "awk -F: '\$1==\"root\"{exit !(\$2==\"!\")}' $F/etc/shadow"
chk "test user locked and expired" "awk -F: '\$1==\"openstick\"{exit !(\$2 ~ /^!/ && \$8==0)}' $F/etc/shadow"
chk "other accounts untouched" "grep -qx 'daemon:\\*:19000:0:99999:7:::' $F/etc/shadow"
chk "sshd drop-in key-only" "grep -qx 'PasswordAuthentication no' $F/etc/ssh/sshd_config.d/05-openstick-hardened.conf && grep -qx 'KbdInteractiveAuthentication no' $F/etc/ssh/sshd_config.d/05-openstick-hardened.conf && grep -qx 'PermitRootLogin prohibit-password' $F/etc/ssh/sshd_config.d/05-openstick-hardened.conf"
chk "dev drop-in 10-openstick-hardening.conf (Match User) removed by harden" "! test -e $F/etc/ssh/sshd_config.d/10-openstick-hardening.conf"
chk "ttyGS0 getty masked, wants link gone" "test \"\$(readlink $F/etc/systemd/system/serial-getty@ttyGS0.service)\" = /dev/null && ! test -e $F/etc/systemd/system/getty.target.wants/serial-getty@ttyGS0.service"
chk "motd replaced (no DEV text)" "! grep -q 'DEV IMAGE' $F/etc/motd && test -s $F/etc/motd"
chk "profile is PROFILE=prod-hardened" "grep -qx 'PROFILE=prod-hardened' $F/etc/openstick/profile"
chk "idempotent: second run succeeds and keeps shadow" "S1=\$(cat $F/etc/shadow); run $F >/dev/null 2>&1 && [ \"\$S1\" = \"\$(cat $F/etc/shadow)\" ]"

# 4) key only on the test user: must not expire that account (its key is the only way in)
F=$T/user; mkfake "$F"; mkdir -p "$F/home/openstick/.ssh"; echo "$KEY" > "$F/home/openstick/.ssh/authorized_keys"
chk "user key only: harden succeeds" "run $F >/dev/null 2>&1"
chk "user key only: test user password-locked but NOT expired" "awk -F: '\$1==\"openstick\"{exit !(\$2 ~ /^!/ && \$8!=\"0\")}' $F/etc/shadow"
chk "user key only: root locked" "awk -F: '\$1==\"root\"{exit !(\$2==\"!\")}' $F/etc/shadow"
chk "script is syntactically valid sh" "sh -n $H"
exit $fail
