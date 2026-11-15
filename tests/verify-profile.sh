#!/bin/sh
# SPDX-License-Identifier: MIT
# Rootless test of PROFILE validation and the prod credential gate (scripts/common.sh). No sudo, no chroot.
. "$(dirname "$0")/../scripts/common.sh"
fail=0
T=$(mktemp -d "${TMPDIR:-/tmp}/verify-profile.XXXXXX"); trap 'rm -rf --one-file-system -- "${T:?}"' EXIT INT TERM
chk() { if eval "$2"; then echo "OK   $1"; else echo "FAIL $1"; fail=1; fi; }
K=$T/good.pub; echo 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl test@example' > "$K"
BAD=$T/bad.pub; echo 'not a key' > "$BAD"
EMPTY=$T/empty.pub; : > "$EMPTY"
# run the gate in a subshell; common.sh functions are inherited, exit from die only ends the subshell
gate() { ( PROFILE=$1; SSH_PUBKEY_FILE=$2; ROOT_PASSWORD_HASH=$3; validate_profile_credentials ) >/dev/null 2>&1; }
# PROFILE value check runs at source time: run common.sh in a child with a bad value
pv() { PROFILE=$1 sh -c '. "$(dirname "$0")/common.sh"' "$ROOT/scripts/x.sh" >/dev/null 2>&1; }
HASH6='$6$saltsalt$abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789./abcdefghijklmnopqrstuv'
chk "PROFILE=dev accepted" "pv dev"
chk "PROFILE=prod accepted" "pv prod"
chk "PROFILE=staging rejected" "! pv staging"
chk "PROFILE= (empty) falls back to dev" "pv ''"
chk "dev gate: no credentials is fine" "gate dev '' ''"
chk "prod gate: no credentials refused" "! gate prod '' ''"
chk "prod gate: valid key accepted" "gate prod $K ''"
chk "prod gate: unreadable key file refused" "! gate prod $T/missing.pub ''"
chk "prod gate: file without a key line refused" "! gate prod $BAD ''"
chk "prod gate: empty key file refused" "! gate prod $EMPTY ''"
chk "prod gate: \$6\$ hash accepted" "gate prod '' '$HASH6'"
chk "prod gate: \$y\$ hash accepted" "gate prod '' '\$y\$j9T\$abc\$def'"
chk "prod gate: \$5\$ hash accepted" "gate prod '' '\$5\$abc\$def'"
chk "prod gate: plaintext password refused" "! gate prod '' 'hunter2'"
chk "prod gate: md5 \$1\$ hash refused" "! gate prod '' '\$1\$abc\$def'"
chk "prod gate: hash with quote refused" "! gate prod '' \"\\\$6\\\$a'b\""
# the refusal must come BEFORE need_root in 20-rootfs.sh (so it is testable without sudo)
chk "20-rootfs.sh calls validate_profile_credentials before need_root" "[ \$(grep -n 'validate_profile_credentials' $ROOT/scripts/20-rootfs.sh | head -1 | cut -d: -f1) -lt \$(grep -n '^need_root' $ROOT/scripts/20-rootfs.sh | head -1 | cut -d: -f1) ]"
# end to end without sudo: prod without credentials must stop with the credentials error, not with 'run as root'
OUTTXT=$(PROFILE=prod SSH_PUBKEY_FILE= ROOT_PASSWORD_HASH= OPENSTICK_USER_CONFIG=/nonexistent "$ROOT/scripts/20-rootfs.sh" 2>&1 </dev/null) && _rc=0 || _rc=$?
chk "20-rootfs.sh PROFILE=prod without credentials exits non-zero ($_rc)" "[ $_rc -ne 0 ]"
chk "...with the credentials message" "echo \"\$OUTTXT\" | grep -q 'needs credentials'"
chk "dev defaults: PROFILE default is dev in defaults.env" "grep -q '^PROFILE=dev\$' $ROOT/config/defaults.env"

# --- prod: no sudo, no non-root user, serial getty policy (static; nothing is built here)
R20=$ROOT/scripts/20-rootfs.sh; SU=$ROOT/scripts/setup.sh
chk "prod: build.env blanks DEFAULT_USER (no non-root account)" "grep -q '^DEFAULT_USER=.\$(\[ \"\$PROFILE\" = prod \] && echo \"\" || echo \"\$DEFAULT_USER\").\$' $R20"
chk "prod: setup.sh creates a user only for non-empty DEFAULT_USER (prod branch)" "sed -n '/PROFILE\" = prod \]/,/^else/p' $SU | grep -q 'if \[ -n \"\$DEFAULT_USER\" \]'"
chk "setup.sh strips sudo from ROOTFS_PACKAGES/EXTRA_PACKAGES and adds it only for dev" "grep -q '\[ \"\$p\" = sudo \] || NEWPKGS' $SU && grep -q '^\[ \"\$PROFILE\" != dev \] || PKGS=\"\$PKGS sudo\"' $SU"
chk "defaults.env ROOTFS_PACKAGES has no sudo" "! grep '^ROOTFS_PACKAGES=' $ROOT/config/defaults.env | grep -qw sudo"
chk "prod: root locked or hash only, no passwd -d on prod branch" "! sed -n '/PROFILE\" = prod \]/,/^else/p' $SU | grep -q 'passwd -d'"
chk "prod sshd: root key-only, passwords off" "grep -q '^PermitRootLogin prohibit-password' $ROOT/configs/sshd/10-openstick-hardening.prod.conf && grep -q '^PasswordAuthentication no' $ROOT/configs/sshd/10-openstick-hardening.prod.conf"
chk "prod branch of 20-rootfs.sh masks serial-getty@ttyGS0 to /dev/null" "sed -n '/^else\$/,/^fi\$/p' $R20 | grep -q 'ln -s /dev/null \"\$GETTY_MASK\"'"
chk "prod branch does not install configs/dev nor enable ttyGS0 getty" "! sed -n '/^else\$/,/^fi\$/p' $R20 | grep -qE 'configs/dev|getty.target.wants'"
chk "dev branch enables serial-getty@ttyGS0 only for *acm* gadget modes" "grep -q 'case \"\$GADGET_MODE\" in \*acm\*) ln -sfn /lib/systemd/system/serial-getty@.service' $R20"
chk "dev ttyGS0 drop-in autologin (configs/dev)" "grep -q -- --autologin $ROOT/configs/dev/serial-getty@ttyGS0.service.d/*.conf"
chk "no ttyGS0 autologin in common configs/system (would leak to prod)" "! find $ROOT/configs/system -path '*ttyGS0*' | grep -q ."
# true only if the unit is a symlink whose target is /dev/null (`ls -l` does not print symlink targets; `stat` does)
getty_masked() { debugfs -R 'stat /etc/systemd/system/serial-getty@ttyGS0.service' "$1" 2>/dev/null | grep -q '^Fast link dest: "/dev/null"$'; }
# mutation: tiny rootless ext4 images (mke2fs -d); a symlink to another target (or a regular file) must NOT pass
_mkimg() { mkdir -p "$T/$1/etc/systemd/system"; ( cd "$T/$1/etc/systemd/system" && eval "$2" ); mke2fs -q -t ext4 -d "$T/$1" "$T/$1.img" 2M >/dev/null 2>&1; }
if command -v debugfs >/dev/null 2>&1 && command -v mke2fs >/dev/null 2>&1; then
    _mkimg gm-ok 'ln -s /dev/null serial-getty@ttyGS0.service'
    _mkimg gm-bad 'ln -s /lib/systemd/system/serial-getty@.service serial-getty@ttyGS0.service'
    _mkimg gm-file ': > serial-getty@ttyGS0.service'
    chk "getty_masked accepts a symlink to /dev/null (control)" "getty_masked $T/gm-ok.img"
    chk "getty_masked mutation: symlink to another target is rejected" "! getty_masked $T/gm-bad.img"
    chk "getty_masked mutation: regular file is rejected" "! getty_masked $T/gm-file.img"
else
    echo "SKIP getty_masked mutation tests (debugfs/mke2fs not found)"
fi
# built prod rootfs image, rootless via debugfs (only if out/*/rootfs.img exists and is a prod image)
_found=0
for img in $OUT/prod-rootfs/rootfs.img $OUT/rootfs-$BOARD.img; do
    [ -f "$img" ] && command -v debugfs >/dev/null 2>&1 || continue
    ip=$(debugfs -R 'cat /etc/openstick/profile' "$img" 2>/dev/null | sed -n 's/^PROFILE=//p' | head -1)
    if [ "$ip" != prod ]; then echo "SKIP built image $img is profile '${ip:-unknown}', not prod (rebuild with PROFILE=prod as root to verify)"; continue; fi
    _found=1
    chk "prod image $img: no /usr/bin/sudo" "! debugfs -R 'stat /usr/bin/sudo' $img 2>/dev/null | grep -q Inode"
    chk "prod image: sudo not in dpkg status" "! debugfs -R 'cat /var/lib/dpkg/status' $img 2>/dev/null | grep -q '^Package: sudo\$'"
    chk "prod image: no uid>=1000 account" "! debugfs -R 'cat /etc/passwd' $img 2>/dev/null | awk -F: '\$3>=1000 && \$3<65534{f=1} END{exit !f}'"
    chk "prod image: ttyGS0 getty masked, no autologin drop-in" "getty_masked $img && ! debugfs -R 'ls /etc/systemd/system/serial-getty@ttyGS0.service.d' $img 2>&1 | grep -q override"
done
[ "$_found" = 1 ] || echo "SKIP no built prod rootfs image under out/: image-level sudo/getty checks not run (not verifiable rootless without an image)"
exit $fail
