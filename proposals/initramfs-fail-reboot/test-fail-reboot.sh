#!/bin/sh
# Rootless, boardless test of the proposed rootfs-stage timeout logic + native build of reboot-bootloader.c. Never calls real reboot.
# Usage: sh test-fail-reboot.sh   (STAGE=<file> to test another stage file)
set -u
H=$(cd "$(dirname "$0")" && pwd); STAGE=${STAGE:-$H/rootfs-stage.new.sh}
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
FAIL=0; ok() { echo "PASS $1"; }; bad() { echo "FAIL $1"; FAIL=1; }
mkdir -p "$T/bin"
cat > "$T/bin/mount" <<'S'
#!/bin/sh
[ "${MOCK_MOUNT:-ok}" = ok ] || exit 1
echo "mount $*" >> "$T/calls"
S
for c in umount killall sync dmesg; do printf '#!/bin/sh\nexit 0\n' > "$T/bin/$c"; done
printf '#!/bin/sh\necho "switch_root $*" >> "$T/calls"\n' > "$T/bin/switch_root"
printf '#!/bin/sh\necho "reboot-bootloader" >> "$T/calls"\n' > "$T/bin/reboot-bootloader"
chmod +x "$T"/bin/*
# run <name> : fresh fake sysfs/newroot, runs the stage in a subshell
run() {
    rm -rf "$T/sys" "$T/nr" "$T/calls"; mkdir -p "$T/sys/mmcblk0p1" "$T/nr/etc" "$T/nr/usr/lib/systemd" "$T/nr/usr/sbin"
    echo PARTNAME=boot > "$T/sys/mmcblk0p1/uevent"; : > "$T/calls"
    [ "${MOCK_PART:-yes}" = yes ] && { mkdir -p "$T/sys/mmcblk0p2"; echo PARTNAME=userdata > "$T/sys/mmcblk0p2/uevent"; }
    [ "${MOCK_MARKER:-yes}" = yes ] && { echo m > "$T/nr/etc/openstick-test-rootfs"; : > "$T/nr/usr/lib/systemd/systemd"; chmod +x "$T/nr/usr/lib/systemd/systemd"; ln -s /lib/systemd/systemd "$T/nr/usr/sbin/init"; }
    echo "${MOCK_CMDLINE:-rw}" > "$T/cmdline"; : > "$T/log"
    S=$(date +%s)
    ( export T LOG="$T/log" NEWROOT="$T/nr" SYSBLK="$T/sys" CMDLINE_FILE="$T/cmdline" INITRAMFS_WAIT_SECS=${WAIT:-1} REBOOT_BL="${RB:-$T/bin/reboot-bootloader}" PATH="$T/bin:$PATH"
      log() { echo "$*" >> "$LOG"; }
      mount() { command mount "$@"; }   # keep stub resolution via PATH
      . "$STAGE" ) >/dev/null 2>&1
    EL=$(( $(date +%s) - S ))
}
has() { grep -q "$1" "$T/calls"; }
MOCK_PART=no run;                          has reboot-bootloader && [ $EL -ge 1 ] && [ $EL -le 3 ] && ok "no partition -> reboot after ~1s (${EL}s)" || bad "no partition"
MOCK_MOUNT=fail run;                       has reboot-bootloader && ! has switch_root && ok "mount fails -> reboot" || bad "mount fails"
MOCK_MARKER=no run;                        has reboot-bootloader && ! has switch_root && ok "no marker -> reboot" || bad "no marker"
run;                                       has switch_root && ! has reboot-bootloader && [ $EL -le 1 ] && ok "marker present -> switch_root, no reboot" || bad "happy path"
MOCK_CMDLINE="rw testboot.noroot" run;     ! has reboot-bootloader && ! has switch_root && ok "testboot.noroot -> shell, no reboot" || bad "noroot"
RB=$T/missing MOCK_PART=no run;            ! has reboot-bootloader && grep -q 'missing' "$T/log" && ok "missing reboot binary -> falls through to shell" || bad "missing binary"
WAIT=2 MOCK_PART=no run;                   [ $EL -ge 2 ] && ok "INITRAMFS_WAIT_SECS=2 honoured (${EL}s)" || bad "wait secs"
# C source: native strict build + dry-run
if command -v gcc >/dev/null; then
    gcc -Wall -Wextra -Werror -static -DREBOOT_DRYRUN -o "$T/rb" "$H/reboot-bootloader.c" && O=$("$T/rb") && echo "$O" | grep -q 'cmd=0xa1b2c3d4, arg="bootloader"' && ok "native dry-run: $O" || bad "native build/dry-run"
    gcc -Wall -Wextra -Werror -c -o "$T/prod.o" "$H/reboot-bootloader.c" && ok "native production-variant compiles (not run)" || bad "prod compile"
fi
if command -v aarch64-linux-gnu-gcc >/dev/null; then
    if aarch64-linux-gnu-gcc -Wall -Wextra -Werror -static -DREBOOT_DRYRUN -o "$T/rb64" "$H/reboot-bootloader.c" 2>"$T/e"; then
        if command -v qemu-aarch64 >/dev/null; then O=$(qemu-aarch64 "$T/rb64") && echo "$O" | grep -q 'arg="bootloader"' && ok "aarch64 static dry-run under qemu-user: $O" || bad "qemu run"; else echo "SKIP qemu-aarch64 missing"; fi
        aarch64-linux-gnu-gcc -Wall -Werror -static -Os -s -o "$T/rb64prod" "$H/reboot-bootloader.c" && file "$T/rb64prod" | cut -c1-120
    else echo "SKIP aarch64 static build failed: $(head -c 200 "$T/e")"; fi
else echo "SKIP no aarch64 cross compiler"; fi
exit $FAIL
