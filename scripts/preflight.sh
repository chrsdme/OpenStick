#!/bin/sh
# SPDX-License-Identifier: MIT
# Read-only: prints the paths the root steps will rm/mount, and any mount already below WORK.
# Needs no root. Run it before 10-base.sh / 20-rootfs.sh / 30-gt.sh / 40-images.sh.
. "$(dirname "$0")/common.sh"
echo "ROOT   = $ROOT"
echo "WORK   = $WORK"
echo "OUT    = $OUT"
echo "CHROOT = $CHROOT"
echo "BOARD  = $BOARD  DTB_NAME=$DTB_NAME  RELEASE=$RELEASE"
for d in "$CHROOT" "$WORK/base" "$WORK/gt-sysroot" "$WORK/gt-build" "$WORK/mnt"; do
    ( safe_dir "$d" ) && echo "safe_dir OK: $d" || echo "safe_dir REFUSES: $d"
done
echo "mounts below WORK:"; grep " $WORK/" /proc/mounts || echo "  (none)"
echo "binfmt qemu-aarch64: $(head -1 /proc/sys/fs/binfmt_misc/qemu-aarch64 2>/dev/null || echo MISSING) $(grep -o 'flags: .*' /proc/sys/fs/binfmt_misc/qemu-aarch64 2>/dev/null)"
df -Pm "$WORK" | tail -1 | awk '{print "free MB on WORK fs: " $4}'
# DTB source: kernel apk DTBs are verified after extraction in 20-rootfs.sh; only custom ones are checked here.
APK=$OUT/cache/$(basename "$KERNEL_APK_URL")
if [ -f "$APK" ] && tar tzf "$APK" 2>/dev/null | grep -qx "boot/dtbs/qcom/$DTB_NAME"; then
    echo "DTB $DTB_NAME: provided by kernel apk (boot/dtbs/qcom/), checked again after extraction"
elif [ -e "$ROOT/contrib/dtb/$DTB_NAME" ] || [ -e "$OUT/dtbs/$DTB_NAME" ]; then
    echo "DTB $DTB_NAME: custom (contrib/dtb/ or out/dtbs/)"
else
    echo "WARN: DTB $DTB_NAME not in kernel apk cache, contrib/dtb/ or out/dtbs/ (apk cached? $([ -f "$APK" ] && echo yes || echo no))"
fi
