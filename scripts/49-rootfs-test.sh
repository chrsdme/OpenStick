#!/bin/sh
# SPDX-License-Identifier: MIT
# Rootfs test image + initramfs v5 for board #6. ROOTLESS (fakeroot only), offline, never touches a device.
# Output dir (default <repo>/../out/rootfs-test): rootfs-test.img, test-boot5-rootfs.img, initramfs5-rootfs.cpio.gz, init5-rootfs.txt, SHA256SUMS
. "$(dirname "$0")/common.sh"
[ "$(id -u)" -ne 0 ] || die "run WITHOUT root"
for t in fakeroot mke2fs e2fsck debugfs python3 gzip cpio tar dpkg-deb sha256sum awk modinfo; do command -v $t >/dev/null || die "missing tool: $t"; done
O=${RT_OUT:-$(cd "$ROOT/.." && pwd)/out/rootfs-test}
W=$O/work
TGZ=${RT_TGZ:-$OUT/rootfs-ufi103s.tgz}
PUBKEY=${RT_PUBKEY:-${SSH_PUBKEY_FILE:-$HOME/.ssh/id_ed25519.pub}}
SIZE_MIB=${RT_SIZE_MIB:-512}
LABEL=osroot
UUID=5a1c0de0-7e57-4b00-8000-0f1e5c0de005
BB_URL=${BB_URL:-http://deb.debian.org/debian/pool/main/b/busybox/busybox-static_1.35.0-4+deb12u1+b1_arm64.deb}
BB_SHA256=${BB_SHA256:-732c9135564fc71337e0e05fb4da4d11e6c28c1834bce3e405e575afef2a52f5}
DTB=${TESTBOOT_DTB:-msm8916-thwc-ufi001c.dtb}
# same cmdline as test-boot3-uart (control: the pipeline below must reproduce that image byte for byte)
CMDLINE="rdinit=/init rw loglevel=7 printk.time=1 earlycon console=ttyMSM0,115200 loglevel=8 ignore_loglevel"
D3=$OUT/bootimg3; D2=$OUT/bootimg2
rm -rf --one-file-system -- "${W:?}"; mkdir -p "$W/apk" "$W/bb" "$W/rd" "$W/tree"

( cd "$D3" && sha256sum -c SHA256SUMS3 >/dev/null ) || die "boot3 images do not verify"
APK=$OUT/cache/$(basename "$KERNEL_APK_URL"); BBDEB=$OUT/cache/$(basename "$BB_URL")
echo "$KERNEL_APK_SHA256  $APK" | sha256sum -c - >/dev/null || die "kernel apk sha256 mismatch"
echo "$BB_SHA256  $BBDEB" | sha256sum -c - >/dev/null || die "busybox deb sha256 mismatch"
tar xzf "$APK" -C "$W/apk" 2>/dev/null || true
KVER=$(ls "$W/apk/lib/modules"); dpkg-deb -x "$BBDEB" "$W/bb"

# ---------------- 1) rootfs ----------------
fakeroot -- sh "$ROOT/scripts/49-inner-fakeroot.sh" "$TGZ" "$W/tree" "$O/rootfs-test.img" "$SIZE_MIB" "$PUBKEY" "$ROOT/configs" "$LABEL" "$UUID"

# module match: file-by-file against the apk the boot3 images were built from, plus vermagic
( cd "$W/apk/lib/modules/$KVER" && find . -type f -print0 | sort -z | xargs -0 sha256sum ) > "$W/mod-apk.sha"
( cd "$W/tree/lib/modules/$KVER" && find . -type f -print0 | sort -z | xargs -0 sha256sum ) > "$W/mod-rootfs.sha"
cmp "$W/mod-apk.sha" "$W/mod-rootfs.sha" || die "rootfs modules differ from the apk modules"
echo "modules: $(wc -l < "$W/mod-apk.sha") files identical to apk ($KVER); modules.dep: $(wc -l < "$W/tree/lib/modules/$KVER/modules.dep") lines"
modinfo -F vermagic "$W/tree/lib/modules/$KVER/kernel/drivers/usb/gadget/libcomposite.ko"

# ---------------- 2) initramfs v5 ----------------
# init5 = init2 with (a) per-device foreground feeders + pid files, (b) the rootfs stage inserted before the ACM shell. Both anchors are checked.
A1='    if watchdog -t 3 $w 2>>$LOG; then R1=yes; log "feeder started on $w"; else log "feeder FAILED on $w"; fi'
A2='# ---- serial shell on the ACM gadget (host: /dev/ttyACM0) ----'
for a in "$A1" "$A2"; do [ "$(grep -cxF -- "$a" "$ROOT/configs/testboot/init2")" = 1 ] || die "anchor not unique in init2: $a"; done
awk -v a1="$A1" -v a2="$A2" -v stage="$ROOT/configs/testboot/rootfs-stage.sh" '
    $0==a1 { print "    watchdog -F -t 3 $w 2>>$LOG &"; print "    WP=$!; sleep 0.2"; print "    if kill -0 $WP 2>/dev/null; then R1=yes; echo $WP > /tmp/wd.$(basename $w); log \"feeder started on $w pid $WP\"; else log \"feeder FAILED on $w\"; fi"; next }
    $0==a2 { while ((getline l < stage) > 0) print l; print ""; print; next }
    { print }' "$ROOT/configs/testboot/init2" > "$W/init5"
"$W/bb/bin/busybox" sh -n "$W/init5" || die "init5 syntax check failed"

build_rd() { # $1 init, $2 out cpio.gz
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
mk() { # $1 ramdisk $2 out
    cat "$W/apk/boot/vmlinuz" "$W/apk/boot/dtbs/qcom/$DTB" > "$W/Image.gz-dtb"
    python3 -I "$ROOT/tools/mkimg.py" boot --kernel "$W/Image.gz-dtb" --ramdisk "$1" --cmdline "${3:-$CMDLINE}" --name testboot2 --out "$2"
}
# control: unchanged init2 must give test-boot3-uart.img exactly
build_rd "$ROOT/configs/testboot/init2" "$W/rd2.cpio.gz"
mk "$W/rd2.cpio.gz" "$W/ctrl.img"
cmp "$W/ctrl.img" "$D3/test-boot3-uart.img" || die "control differs from test-boot3-uart.img: pipeline not reproducible"
echo "CONTROL OK: pipeline reproduces test-boot3-uart.img byte for byte"

build_rd "$W/init5" "$O/initramfs5-rootfs.cpio.gz"
mk "$O/initramfs5-rootfs.cpio.gz" "$O/test-boot5-rootfs.img"
mk "$O/initramfs5-rootfs.cpio.gz" "$O/test-boot5-rootfs-shell.img" "$CMDLINE testboot.noroot"
cp "$W/init5" "$O/init5-rootfs.txt"
cp "$W/mod-apk.sha" "$O/modules.sha256"
( cd "$O" && sha256sum rootfs-test.img test-boot5-rootfs.img test-boot5-rootfs-shell.img initramfs5-rootfs.cpio.gz init5-rootfs.txt > SHA256SUMS && cat SHA256SUMS )
ls -l "$O/rootfs-test.img" "$O/test-boot5-rootfs.img"
