#!/bin/sh
# SPDX-License-Identifier: MIT
# Test image v2 for `fastboot boot` through lk2nd. ROOTLESS, offline, never touches a device, never touches out/bootimg/ (v1).
# Output (out/bootimg2/): test-boot2.img (unpatched pmOS DTB, comparable with v1)
#                         test-boot2-wdt.img (OPTIONAL variant: same + DTB node watchdog@b017000 qcom,kpss-wdt; test of the APSS wdt hypothesis)
# Usage: scripts/47-testboot2.sh
. "$(dirname "$0")/common.sh"
[ "$(id -u)" -ne 0 ] || die "run WITHOUT root"
for t in python3 gzip cpio tar dpkg-deb dtc fdtput fdtget sha256sum; do command -v $t >/dev/null || die "missing tool: $t"; done
BB_URL=${BB_URL:-http://deb.debian.org/debian/pool/main/b/busybox/busybox-static_1.35.0-4+deb12u1+b1_arm64.deb}
BB_SHA256=${BB_SHA256:-732c9135564fc71337e0e05fb4da4d11e6c28c1834bce3e405e575afef2a52f5}
DTB=${TESTBOOT_DTB:-msm8916-thwc-ufi001c.dtb}
CMDLINE=${TESTBOOT_CMDLINE:-rdinit=/init rw loglevel=7 printk.time=1}
D=$OUT/bootimg2; W=$D/work
mkdir -p "$D"; rm -rf --one-file-system -- "${W:?}"; mkdir -p "$W/apk" "$W/bb" "$W/rd"

APK=$OUT/cache/$(basename "$KERNEL_APK_URL"); BBDEB=$OUT/cache/$(basename "$BB_URL")
[ -f "$APK" ] && [ -f "$BBDEB" ] || die "cache missing (run scripts/45-testboot.sh once, or fetch the apk/deb into out/cache)"
echo "$KERNEL_APK_SHA256  $APK" | sha256sum -c - >/dev/null || die "kernel apk sha256 mismatch"
echo "$BB_SHA256  $BBDEB" | sha256sum -c - >/dev/null || die "busybox deb sha256 mismatch"
tar xzf "$APK" -C "$W/apk" 2>/dev/null || true
KVER=$(ls "$W/apk/lib/modules"); [ -f "$W/apk/boot/vmlinuz" ] || die "vmlinuz not in apk"
[ -f "$W/apk/boot/dtbs/qcom/$DTB" ] || die "$DTB not in the kernel apk"
gzip -t "$W/apk/boot/vmlinuz" || die "vmlinuz is not gzip"
dpkg-deb -x "$BBDEB" "$W/bb"
"$W/bb/bin/busybox" --list | grep -qx watchdog || die "busybox lacks the watchdog applet"

R=$W/rd
mkdir -p "$R/bin" "$R/sbin" "$R/dev" "$R/proc" "$R/sys" "$R/tmp" "$R/run" "$R/etc" "$R/lib/modules/$KVER/kernel/drivers/usb/gadget/function"
install -m 755 "$W/bb/bin/busybox" "$R/bin/busybox"; ln -s busybox "$R/bin/sh"
install -m 755 "$ROOT/configs/testboot/init2" "$R/init"
G=drivers/usb/gadget
for m in $G/libcomposite.ko $G/function/u_ether.ko $G/function/usb_f_ecm.ko $G/function/u_serial.ko $G/function/usb_f_acm.ko; do
    install -m 644 "$W/apk/lib/modules/$KVER/kernel/$m" "$R/lib/modules/$KVER/kernel/$m"
done
python3 -I "$ROOT/tools/mkimg.py" cpio --tree "$R" --out "$W/initramfs.cpio" --node /dev/console c 5 1 600 --node /dev/null c 1 3 666
gzip -9n -c "$W/initramfs.cpio" > "$D/initramfs2.cpio.gz"

# variant DTB: add the APSS watchdog node (driver qcom,kpss-wdt is built in: strings-checked; sleep-clk phandle read from the DTB)
SRC=$W/apk/boot/dtbs/qcom/$DTB; PDTB=$W/wdt.dtb; cp "$SRC" "$PDTB"
SC=$(fdtget -t x "$PDTB" /clocks/sleep-clk phandle) || die "sleep-clk phandle not found"
fdtput -c "$PDTB" /soc@0/watchdog@b017000
fdtput -t s "$PDTB" /soc@0/watchdog@b017000 compatible qcom,kpss-wdt
fdtput -t x "$PDTB" /soc@0/watchdog@b017000 reg 0xb017000 0x1000
fdtput -t x "$PDTB" /soc@0/watchdog@b017000 clocks 0x$SC
dtc -I dtb -O dts -q "$PDTB" 2>/dev/null | grep -q 'watchdog@b017000' || die "patched DTB check failed"

mk() { # $1 dtb $2 out
    cat "$W/apk/boot/vmlinuz" "$1" > "$W/Image.gz-dtb.$$"
    python3 -I "$ROOT/tools/mkimg.py" boot --kernel "$W/Image.gz-dtb.$$" --ramdisk "$D/initramfs2.cpio.gz" --cmdline "$CMDLINE" --name testboot2 --out "$2"
}
mk "$SRC" "$D/test-boot2.img"
mk "$PDTB" "$D/test-boot2-wdt.img"
( cd "$D" && sha256sum test-boot2.img test-boot2-wdt.img initramfs2.cpio.gz > SHA256SUMS2 && cat SHA256SUMS2 )
rm -rf --one-file-system -- "${W:?}"
echo "run: fastboot boot $D/test-boot2.img   (variant: $D/test-boot2-wdt.img)"
