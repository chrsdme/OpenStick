#!/bin/sh
# SPDX-License-Identifier: MIT
# PRODUCTION boot.img for board #6 (lk2nd): Android boot image v0 = vmlinuz + board DTB (Image.gz-dtb style) + initramfs.
# Flash by hand with `fastboot flash boot boot.img` (lk2nd places it at offset 512 KiB). This script NEVER flashes anything.
# ROOTLESS, offline, reproducible (mkimg.py sorts, mtime 0; gzip -9n). Extlinux is future work and not touched here.
# Output dir (default $OUT/prod-boot): boot.img, initramfs.cpio.gz, init.txt, SHA256SUMS
# Also builds /sbin/reboot-bootloader (configs/initramfs/reboot-bootloader.c, freestanding aarch64, ~1 KiB) with a cross gcc.
# Env: CROSS_CC (default aarch64-linux-gnu-gcc), REBOOT_BL_BIN (use a prebuilt binary instead of compiling), PROD_CMDLINE (kernel cmdline override), PROD_OUT, DTB_NAME (board.env), BB_URL/BB_SHA256 (pinned busybox deb in $OUT/cache).
. "$(dirname "$0")/common.sh"
[ "$(id -u)" -ne 0 ] || die "run WITHOUT root"
for t in python3 gzip tar dpkg-deb sha256sum; do command -v $t >/dev/null || die "missing tool: $t"; done
O=${PROD_OUT:-$OUT/prod-boot}
W=$WORK/prod-boot
BB_URL=${BB_URL:-http://deb.debian.org/debian/pool/main/b/busybox/busybox-static_1.35.0-4+deb12u1+b1_arm64.deb}
BB_SHA256=${BB_SHA256:-732c9135564fc71337e0e05fb4da4d11e6c28c1834bce3e405e575afef2a52f5}
DTB=${DTB_NAME:-msm8916-thwc-ufi001c.dtb}
CMDLINE=${PROD_CMDLINE:-rdinit=/init rw console=ttyMSM0,115200 loglevel=4}
INIT=$ROOT/configs/initramfs/init
[ -f "$INIT" ] || die "missing $INIT"
RBC=$ROOT/configs/initramfs/reboot-bootloader.c; CROSS_CC=${CROSS_CC:-aarch64-linux-gnu-gcc}
[ -f "$RBC" ] || die "missing $RBC"
[ -n "${REBOOT_BL_BIN:-}" ] || command -v "$CROSS_CC" >/dev/null || die "missing $CROSS_CC (needs only the cross gcc, no libc: dnf/apt install gcc-aarch64-linux-gnu) or set REBOOT_BL_BIN"
rm -rf --one-file-system -- "${W:?}"; mkdir -p "$W/apk" "$W/bb" "$W/rd" "$O"

APK=$OUT/cache/$(basename "$KERNEL_APK_URL"); BBDEB=$OUT/cache/$(basename "$BB_URL")
[ -f "$APK" ] || die "kernel apk missing: $APK"
[ -f "$BBDEB" ] || die "busybox deb missing: $BBDEB (download it from $BB_URL into $OUT/cache)"
echo "$KERNEL_APK_SHA256  $APK" | sha256sum -c - >/dev/null || die "kernel apk sha256 mismatch"
echo "$BB_SHA256  $BBDEB" | sha256sum -c - >/dev/null || die "busybox deb sha256 mismatch"
tar xzf "$APK" -C "$W/apk" boot/vmlinuz "boot/dtbs/qcom/$DTB" 2>/dev/null || die "cannot extract vmlinuz / $DTB from the kernel apk"
dpkg-deb -x "$BBDEB" "$W/bb"
"$W/bb/bin/busybox" sh -n "$INIT" || die "init syntax check failed"

R=$W/rd
mkdir -p "$R/bin" "$R/sbin" "$R/dev" "$R/proc" "$R/sys" "$R/tmp" "$R/run" "$R/etc"
install -m 755 "$W/bb/bin/busybox" "$R/bin/busybox"; ln -s busybox "$R/bin/sh"
install -m 755 "$INIT" "$R/init"
if [ -n "${REBOOT_BL_BIN:-}" ]; then install -m 755 "$REBOOT_BL_BIN" "$R/sbin/reboot-bootloader"
else
    "$CROSS_CC" -Wall -Wextra -Werror -Os -static -nostdlib -ffreestanding -fno-stack-protector -Wl,--build-id=none -s -o "$W/reboot-bootloader" "$RBC" || die "reboot-bootloader build failed"
    install -m 755 "$W/reboot-bootloader" "$R/sbin/reboot-bootloader"
fi
python3 -I "$ROOT/tools/mkimg.py" cpio --tree "$R" --out "$W/rd.cpio" --node /dev/console c 5 1 600 --node /dev/null c 1 3 666
gzip -9n -c "$W/rd.cpio" > "$O/initramfs.cpio.gz"
cat "$W/apk/boot/vmlinuz" "$W/apk/boot/dtbs/qcom/$DTB" > "$W/Image.gz-dtb"
python3 -I "$ROOT/tools/mkimg.py" boot --kernel "$W/Image.gz-dtb" --ramdisk "$O/initramfs.cpio.gz" --cmdline "$CMDLINE" --name openstick --out "$O/boot.img"
cp "$INIT" "$O/init.txt"
( cd "$O" && sha256sum boot.img initramfs.cpio.gz init.txt > SHA256SUMS && cat SHA256SUMS )
echo "cmdline: $CMDLINE"
echo "NOT flashed. Manual step (owner confirmation + verified backup + EDL first): fastboot flash boot $O/boot.img"
