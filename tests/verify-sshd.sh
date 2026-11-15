#!/bin/sh
# SPDX-License-Identifier: MIT
# Run after scripts/20-rootfs.sh. Prints the effective sshd settings of the built rootfs,
# evaluated by the real arm64 sshd inside the chroot (via binfmt). Needs root.
. "$(dirname "$0")/../scripts/common.sh"
need_root; require_binfmt; safe_dir "$CHROOT"
fail=0
chk() { if eval "$2"; then echo "OK   $1"; else echo "FAIL $1"; fail=1; fi; }
chk "no ssh_host_* in image" "! ls $CHROOT/etc/ssh/ssh_host_* >/dev/null 2>&1"
chk "hostkeys unit installed" "test -f $CHROOT/etc/systemd/system/openstick-ssh-hostkeys.service"
chk "hostkeys unit enabled" "test -L $CHROOT/etc/systemd/system/multi-user.target.wants/openstick-ssh-hostkeys.service"
chk "hostkeys unit ordered before ssh" "grep -q '^Before=ssh.service' $CHROOT/etc/systemd/system/openstick-ssh-hostkeys.service"
chk "hostkeys unit runs ssh-keygen -A" "grep -q 'ssh-keygen -A' $CHROOT/etc/systemd/system/openstick-ssh-hostkeys.service"
K=/tmp/verify-hostkey
chroot_run "$CHROOT" /usr/bin/ssh-keygen -q -t ed25519 -N '' -f $K
chroot_run "$CHROOT" /usr/sbin/sshd -T -h $K 2>&1 | grep -iE '^(permitemptypasswords|permitrootlogin|passwordauthentication|pubkeyauthentication) '
rm -f "$CHROOT$K" "$CHROOT$K.pub"
exit $fail
