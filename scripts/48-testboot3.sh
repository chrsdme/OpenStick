#!/bin/sh
# SPDX-License-Identifier: MIT
# Test images v3 for `fastboot boot` through lk2nd. ROOTLESS, offline, never touches a device, never touches out/bootimg/ or out/bootimg2/.
# Base: exactly test-boot2.img (same kernel, same unpatched pmOS DTB, same base cmdline); only /init and/or cmdline differ.
# Output (out/bootimg3/):
#   test-boot3-live.img       init2 + forced-reboot lifesign right after the /proc mount (sysrq b at 3 s, reboot -f at 6 s)
#   test-boot3-live-up.img    same + cmdline maxcpus=1
#   test-boot3-uart.img       unchanged init2 (stays alive, never reboots), cmdline + "earlycon console=ttyMSM0,115200 loglevel=8 ignore_loglevel"
#   test-boot3-uart-addr.img  same as -uart but with explicit earlycon=msm_serial_dm,0x78b0000
#   test-boot3-ctrl.img       CONTROL: must be byte-identical to test-boot2.img (proves this script reproduces the v2 pipeline)
. "$(dirname "$0")/common.sh"
[ "$(id -u)" -ne 0 ] || die "run WITHOUT root"
for t in python3 gzip cpio tar dpkg-deb sha256sum sed; do command -v $t >/dev/null || die "missing tool: $t"; done
BB_URL=${BB_URL:-http://deb.debian.org/debian/pool/main/b/busybox/busybox-static_1.35.0-4+deb12u1+b1_arm64.deb}
BB_SHA256=${BB_SHA256:-732c9135564fc71337e0e05fb4da4d11e6c28c1834bce3e405e575afef2a52f5}
DTB=${TESTBOOT_DTB:-msm8916-thwc-ufi001c.dtb}
BASE_CMDLINE="rdinit=/init rw loglevel=7 printk.time=1"
D=$OUT/bootimg3; W=$D/work; D2=$OUT/bootimg2
mkdir -p "$D"; rm -rf --one-file-system -- "${W:?}"; mkdir -p "$W/apk" "$W/bb" "$W/rd"

( cd "$D2" && sha256sum -c SHA256SUMS2 >/dev/null ) || die "v2 images do not verify"
APK=$OUT/cache/$(basename "$KERNEL_APK_URL"); BBDEB=$OUT/cache/$(basename "$BB_URL")
[ -f "$APK" ] && [ -f "$BBDEB" ] || die "cache missing"
echo "$KERNEL_APK_SHA256  $APK" | sha256sum -c - >/dev/null || die "kernel apk sha256 mismatch"
echo "$BB_SHA256  $BBDEB" | sha256sum -c - >/dev/null || die "busybox deb sha256 mismatch"
tar xzf "$APK" -C "$W/apk" 2>/dev/null || true
KVER=$(ls "$W/apk/lib/modules"); [ -f "$W/apk/boot/vmlinuz" ] || die "vmlinuz not in apk"
SRC=$W/apk/boot/dtbs/qcom/$DTB; [ -f "$SRC" ] || die "$DTB not in the kernel apk"
dpkg-deb -x "$BBDEB" "$W/bb"

# init3-live = init2 with a lifesign block injected right after the /proc mount (single anchor, checked)
LIVE_BLOCK='# ---- v3 lifesign (injected by 48-testboot3.sh): forced reboot proves userspace is alive; detached so a hang in one path cannot block the other ----
echo 1 > /proc/sys/kernel/sysrq 2>/dev/null
( sleep 3; echo b > /proc/sysrq-trigger ) &
( sleep 6; reboot -f ) &'
ANCHOR='mount -t proc proc /proc'
[ "$(grep -cx "$ANCHOR" "$ROOT/configs/testboot/init2")" = 1 ] || die "anchor line not unique in init2"
mkinit() { # $1 out
    awk -v a="$ANCHOR" -v b="$LIVE_BLOCK" '{print} $0==a{print b}' "$ROOT/configs/testboot/init2" > "$1"
}

build_rd() { # $1 init file, $2 out cpio.gz
    R=$W/rd; rm -rf --one-file-system -- "${R:?}"; mkdir -p "$R/bin" "$R/sbin" "$R/dev" "$R/proc" "$R/sys" "$R/tmp" "$R/run" "$R/etc" "$R/lib/modules/$KVER/kernel/drivers/usb/gadget/function"
    install -m 755 "$W/bb/bin/busybox" "$R/bin/busybox"; ln -s busybox "$R/bin/sh"
    install -m 755 "$1" "$R/init"
    G=drivers/usb/gadget
    for m in $G/libcomposite.ko $G/function/u_ether.ko $G/function/usb_f_ecm.ko $G/function/u_serial.ko $G/function/usb_f_acm.ko; do
        install -m 644 "$W/apk/lib/modules/$KVER/kernel/$m" "$R/lib/modules/$KVER/kernel/$m"
    done
    python3 -I "$ROOT/tools/mkimg.py" cpio --tree "$R" --out "$W/rd.cpio" --node /dev/console c 5 1 600 --node /dev/null c 1 3 666
    gzip -9n -c "$W/rd.cpio" > "$2"
}
mk() { # $1 ramdisk $2 cmdline $3 out
    cat "$W/apk/boot/vmlinuz" "$SRC" > "$W/Image.gz-dtb.$$"
    python3 -I "$ROOT/tools/mkimg.py" boot --kernel "$W/Image.gz-dtb.$$" --ramdisk "$1" --cmdline "$2" --name testboot2 --out "$3"
}

# control: unchanged init2 -> must equal v2 (initramfs2 and image)
build_rd "$ROOT/configs/testboot/init2" "$W/rd2.cpio.gz"
mk "$W/rd2.cpio.gz" "$BASE_CMDLINE" "$D/test-boot3-ctrl.img"
cmp "$D/test-boot3-ctrl.img" "$D2/test-boot2.img" || die "control differs from test-boot2.img: pipeline not reproducible, do not trust v3"

mkinit "$W/init3-live"
build_rd "$W/init3-live" "$D/initramfs3-live.cpio.gz"
mk "$D/initramfs3-live.cpio.gz" "$BASE_CMDLINE" "$D/test-boot3-live.img"
mk "$D/initramfs3-live.cpio.gz" "$BASE_CMDLINE maxcpus=1" "$D/test-boot3-live-up.img"
mk "$D2/initramfs2.cpio.gz" "$BASE_CMDLINE earlycon console=ttyMSM0,115200 loglevel=8 ignore_loglevel" "$D/test-boot3-uart.img"
mk "$D2/initramfs2.cpio.gz" "$BASE_CMDLINE earlycon=msm_serial_dm,0x78b0000 console=ttyMSM0,115200 loglevel=8 ignore_loglevel" "$D/test-boot3-uart-addr.img"
rm -f "$D/test-boot3-ctrl.img"
cp "$W/init3-live" "$D/init3-live.txt"
( cd "$D" && sha256sum test-boot3-live.img test-boot3-live-up.img test-boot3-uart.img test-boot3-uart-addr.img initramfs3-live.cpio.gz > SHA256SUMS3 && cat SHA256SUMS3 )
rm -rf --one-file-system -- "${W:?}"
