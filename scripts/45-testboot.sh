#!/bin/sh
# SPDX-License-Identifier: MIT
# Test image for `fastboot boot` through lk2nd. ROOTLESS, offline, never touches a device.
# kernel (pmOS apk, sha256 pinned) + DTB + minimal initramfs (static busybox arm64 from Debian, sha256 pinned).
# Output: out/bootimg/test-boot.img (+ SHA256SUMS). Usage: scripts/45-testboot.sh
. "$(dirname "$0")/common.sh"
[ "$(id -u)" -ne 0 ] || die "run WITHOUT root (this build needs none)"
for t in python3 gzip cpio curl tar dpkg-deb dtc sha256sum; do command -v $t >/dev/null || die "missing tool: $t"; done
BB_URL=${BB_URL:-http://deb.debian.org/debian/pool/main/b/busybox/busybox-static_1.35.0-4+deb12u1+b1_arm64.deb}
BB_SHA256=${BB_SHA256:-732c9135564fc71337e0e05fb4da4d11e6c28c1834bce3e405e575afef2a52f5}
DTB=${TESTBOOT_DTB:-msm8916-thwc-ufi001c.dtb}
CMDLINE=${TESTBOOT_CMDLINE:-rdinit=/init rw loglevel=7 printk.time=1}   # no console= (no serial console)
D=$OUT/bootimg; W=$D/work
mkdir -p "$OUT/cache" "$D"; rm -rf --one-file-system -- "${W:?}"; mkdir -p "$W/apk" "$W/bb" "$W/rd"

APK=$OUT/cache/$(basename "$KERNEL_APK_URL")
[ -f "$APK" ] || curl -fL -o "$APK" "$KERNEL_APK_URL"
echo "$KERNEL_APK_SHA256  $APK" | sha256sum -c - >/dev/null || die "kernel apk sha256 mismatch"
tar xzf "$APK" -C "$W/apk" 2>/dev/null || true   # apk-tools extended headers are ignored by tar
KVER=$(ls "$W/apk/lib/modules"); [ -f "$W/apk/boot/vmlinuz" ] || die "vmlinuz not in apk"
[ -f "$W/apk/boot/dtbs/qcom/$DTB" ] || die "$DTB not in the kernel apk"
gzip -t "$W/apk/boot/vmlinuz" || die "vmlinuz is not a gzip file"

BBDEB=$OUT/cache/$(basename "$BB_URL")
[ -f "$BBDEB" ] || curl -fL -o "$BBDEB" "$BB_URL"
echo "$BB_SHA256  $BBDEB" | sha256sum -c - >/dev/null || die "busybox deb sha256 mismatch"
dpkg-deb -x "$BBDEB" "$W/bb"

# initramfs tree
R=$W/rd
mkdir -p "$R/bin" "$R/sbin" "$R/dev" "$R/proc" "$R/sys" "$R/tmp" "$R/run" "$R/etc" "$R/lib/modules/$KVER/kernel/drivers/usb/gadget/function"
install -m 755 "$W/bb/bin/busybox" "$R/bin/busybox"
ln -s busybox "$R/bin/sh"
install -m 755 "$ROOT/configs/testboot/init" "$R/init"
G=drivers/usb/gadget
for m in $G/libcomposite.ko $G/function/u_ether.ko $G/function/usb_f_ecm.ko $G/function/u_serial.ko $G/function/usb_f_acm.ko; do
    install -m 644 "$W/apk/lib/modules/$KVER/kernel/$m" "$R/lib/modules/$KVER/kernel/$m"
done
python3 -I "$ROOT/tools/mkimg.py" cpio --tree "$R" --out "$W/initramfs.cpio" \
    --node /dev/console c 5 1 600 --node /dev/null c 1 3 666
gzip -9n -c "$W/initramfs.cpio" > "$D/initramfs.cpio.gz"

# kernel = Image.gz + one appended DTB (what lk2nd/aboot expects for a boot.img: dtb right after the gzip stream)
cat "$W/apk/boot/vmlinuz" "$W/apk/boot/dtbs/qcom/$DTB" > "$D/Image.gz-dtb"
python3 -I "$ROOT/tools/mkimg.py" boot --kernel "$D/Image.gz-dtb" --ramdisk "$D/initramfs.cpio.gz" \
    --cmdline "$CMDLINE" --name testboot --out "$D/test-boot.img"
( cd "$D" && sha256sum test-boot.img initramfs.cpio.gz Image.gz-dtb > SHA256SUMS && cat SHA256SUMS )
echo "image: $D/test-boot.img ($(stat -c %s "$D/test-boot.img") bytes, max-download 268435456)"
echo "run:   fastboot boot $D/test-boot.img"
rm -rf --one-file-system -- "${W:?}"
