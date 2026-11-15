#!/bin/sh
# SPDX-License-Identifier: MIT
# Rootless, boardless test of the PRODUCTION initramfs failure path (configs/initramfs/init, block "rootfs-stage")
# plus the reboot-bootloader binary. Never calls a real reboot, never touches a device.
# The rootfs-stage block is extracted from the real init and run with stub mount/umount/switch_root/reboot-bootloader.
set -u
ROOT=$(cd "$(dirname "$0")/.." && pwd); INIT=${INIT:-$ROOT/configs/initramfs/init}
T=$(mktemp -d "${TMPDIR:-/tmp}/vif.XXXXXX"); trap 'rm -rf --one-file-system -- "$T"' EXIT INT TERM
FAIL=0; ok() { echo "OK   $1"; }; bad() { echo "FAIL $1"; FAIL=1; }
sed -n '/^# >>> rootfs-stage/,/^# <<< rootfs-stage/p' "$INIT" > "$T/stage.sh"
[ "$(wc -l < "$T/stage.sh")" -gt 20 ] && ok "rootfs-stage block extracted from $INIT" || { bad "block extraction"; exit 1; }
sh -n "$T/stage.sh" && ok "stage block syntax" || bad "stage syntax"
mkdir -p "$T/bin"
printf '#!/bin/sh\n[ "${MOCK_MOUNT:-ok}" = ok ] || exit 1\necho "mount $*" >> "$T/calls"\n' > "$T/bin/mount"
for c in umount sync; do printf '#!/bin/sh\nexit 0\n' > "$T/bin/$c"; done
printf '#!/bin/sh\necho "switch_root $*" >> "$T/calls"\n' > "$T/bin/switch_root"
printf '#!/bin/sh\necho "reboot-bootloader" >> "$T/calls"\n' > "$T/bin/reboot-bootloader"
chmod +x "$T"/bin/*
run() {   # env: MOCK_PART MOCK_MARKER MOCK_MOUNT WAIT RB
    rm -rf "$T/sys" "$T/nr" "$T/calls"; mkdir -p "$T/sys/mmcblk0p1" "$T/nr/etc" "$T/nr/usr/lib/systemd" "$T/nr/sbin" "$T/nr/proc" "$T/nr/sys" "$T/nr/dev"
    echo PARTNAME=boot > "$T/sys/mmcblk0p1/uevent"; : > "$T/calls"; : > "$T/log"
    [ "${MOCK_PART:-yes}" = yes ] && { mkdir -p "$T/sys/mmcblk0p2"; echo PARTNAME=userdata > "$T/sys/mmcblk0p2/uevent"; }
    [ "${MOCK_MARKER:-yes}" = yes ] && { echo m > "$T/nr/etc/openstick-rootfs"; : > "$T/nr/usr/lib/systemd/systemd"; chmod +x "$T/nr/usr/lib/systemd/systemd"; ln -s /lib/systemd/systemd "$T/nr/sbin/init"; }
    S=$(date +%s)
    ( export T PATH="$T/bin:$PATH"; LOG="$T/log"; NEWROOT="$T/nr"; SYSBLK="$T/sys"; INITRAMFS_WAIT_SECS=${WAIT:-1}; REBOOT_BL="${RB:-$T/bin/reboot-bootloader}"
      log() { echo "$*" >> "$LOG"; }
      . "$T/stage.sh" ) >/dev/null 2>&1
    EL=$(( $(date +%s) - S ))
}
has() { grep -q "$1" "$T/calls"; }
MOCK_PART=no run;     has reboot-bootloader && ! has switch_root && [ $EL -ge 1 ] && [ $EL -le 3 ] && ok "no userdata -> reboot-bootloader after ~1s (${EL}s)" || bad "no userdata"
MOCK_MOUNT=fail run;  has reboot-bootloader && ! has switch_root && ok "mount fails -> reboot-bootloader" || bad "mount fails"
MOCK_MARKER=no run;   has reboot-bootloader && ! has switch_root && ok "no marker -> reboot-bootloader" || bad "no marker"
run;                  has switch_root && ! has reboot-bootloader && [ $EL -le 1 ] && ok "marker present -> switch_root, no reboot" || bad "happy path"
RB=$T/missing MOCK_PART=no run; ! has reboot-bootloader && grep -q 'missing' "$T/log" && ok "missing reboot binary -> falls through (shell fallback)" || bad "missing binary"
WAIT=2 MOCK_PART=no run; [ $EL -ge 2 ] && ok "INITRAMFS_WAIT_SECS=2 honoured (${EL}s)" || bad "wait secs"
grep -q 'INITRAMFS_WAIT_SECS:-20' "$INIT" && ok "default wait is 20 s" || bad "default wait is not 20 s"
# mutation: a stage that never reboots must be caught by the same assertions
sed 's#^if \[ -x "\$REBOOT_BL" \].*#:#' "$T/stage.sh" > "$T/stage-mut.sh"; cp "$T/stage.sh" "$T/stage.orig"; cp "$T/stage-mut.sh" "$T/stage.sh"
MOCK_PART=no run; has reboot-bootloader && bad "mutation (no reboot call) NOT detected" || ok "mutation without the reboot call is detected"
cp "$T/stage.orig" "$T/stage.sh"
# reboot-bootloader.c: hosted dry-run (host), freestanding aarch64 build + disassembly of the syscall numbers (never executed)
C=$ROOT/configs/initramfs/reboot-bootloader.c
if command -v gcc >/dev/null; then
    gcc -Wall -Wextra -Werror -DREBOOT_DRYRUN -o "$T/rb" "$C" && O=$("$T/rb") && echo "$O" | grep -q 'magic1=0xfee1dead, magic2=0x28121969, cmd=0xa1b2c3d4, arg="bootloader"' && ok "host dry-run: $O" || bad "host dry-run"
else echo "SKIP no host gcc"; fi
CC=${CROSS_CC:-aarch64-linux-gnu-gcc}
if command -v "$CC" >/dev/null && command -v aarch64-linux-gnu-objdump >/dev/null; then
    "$CC" -Wall -Wextra -Werror -Os -static -nostdlib -ffreestanding -fno-stack-protector -Wl,--build-id=none -s -o "$T/rb64" "$C" && ok "aarch64 freestanding build ($(wc -c < "$T/rb64") bytes)" || bad "aarch64 build"
    D=$(aarch64-linux-gnu-objdump -d "$T/rb64")
    for n in '#142' '#81' '#93'; do echo "$D" | grep -Eq "mov[[:space:]]+x8, #0x[0-9a-f]+[[:space:]]+// $n\$" && ok "syscall number $n present" || bad "syscall number $n"; done
    echo "$D" | grep -q 'movk.*#0xfee1' && echo "$D" | grep -q 'movk.*#0x2812' && echo "$D" | grep -q 'movk.*#0xa1b2' && ok "reboot magic1/magic2/RESTART2 constants present" || bad "reboot constants"
else echo "SKIP no aarch64 cross gcc/objdump"; fi
exit $FAIL
