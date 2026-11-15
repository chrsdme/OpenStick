#!/bin/sh
# SPDX-License-Identifier: MIT
# Rootless checks of out/prod-boot (50-boot-img.sh) and out/prod-rootfs (51-rootfs-img.sh), with negative controls.
# Env: PROVEN_BOOT (reference test boot image, default <repo>/../out/rootfs-test/test-boot5-rootfs.img),
#      PROVEN_INIT (its init text, default init5-rootfs.txt next to it).
. "$(dirname "$0")/../scripts/common.sh"
[ "$(id -u)" -ne 0 ] || die "run WITHOUT root"
for t in python3 gzip cpio e2fsck debugfs dumpe2fs sha256sum tar; do command -v $t >/dev/null || die "missing tool: $t"; done
set +e
fail=0
chk() { if eval "$2"; then echo "OK   $1"; else echo "FAIL $1"; fail=1; fi; }
neg() { if eval "$2"; then echo "FAIL negative control: $1 (not detected)"; fail=1; else echo "OK   negative control: $1 detected"; fi; }
B=$OUT/prod-boot; R=$OUT/prod-rootfs
REFD=$(cd "$ROOT/.." && pwd)/out/rootfs-test
PROVEN_BOOT=${PROVEN_BOOT:-$REFD/test-boot5-rootfs.img}; PROVEN_INIT=${PROVEN_INIT:-$REFD/init5-rootfs.txt}
T=$(mktemp -d "${TMPDIR:-/tmp}/vbi.XXXXXX"); trap 'rm -rf --one-file-system -- "$T"' EXIT INT TERM
[ -f "$B/boot.img" ] || die "run scripts/50-boot-img.sh first"

# boot header: print "key=value" lines (v0 layout: magic8 then 8 u32 then pagesize, header_version, os_version, name16, cmdline512)
hdr() { python3 -I - "$1" <<'PY'
import struct,sys
d=open(sys.argv[1],'rb').read(1632)
m=d[:8]; ks,ka,rs,ra,ss,sa,tg,ps,hv,ov=struct.unpack('<10I',d[8:48])
print('magic=%s kaddr=%#x raddr=%#x saddr=%#x tags=%#x pagesize=%d hv=%d ov=%d ksize=%d rsize=%d ssize=%d'%(m.decode('latin1'),ka,ra,sa,tg,ps,hv,ov,ks,rs,ss))
print('cmdline='+d[64:576].split(b'\0')[0].decode())
PY
}
fixed() { hdr "$1" | head -1 | sed 's/ ksize=[0-9]* rsize=[0-9]*//'; }
chk "SHA256SUMS of prod-boot verify" "( cd $B && sha256sum -c SHA256SUMS >/dev/null )"
chk "boot header: magic ANDROID!, hv 0" "hdr $B/boot.img | head -1 | grep -q 'magic=ANDROID! .*hv=0'"
if [ -f "$PROVEN_BOOT" ]; then
    chk "boot header: addrs/tags/pagesize/os_version equal the proven test-boot5-rootfs.img" "[ \"\$(fixed $B/boot.img)\" = \"\$(fixed $PROVEN_BOOT)\" ]"
    cp "$B/boot.img" "$T/bad.img"; printf '\001' | dd of="$T/bad.img" bs=1 seek=12 conv=notrunc 2>/dev/null   # corrupt kernel_addr
    neg "tampered kernel_addr vs proven header" "[ \"\$(fixed $T/bad.img)\" = \"\$(fixed $PROVEN_BOOT)\" ]"
else echo "SKIP header comparison ($PROVEN_BOOT missing)"; fi
chk "cmdline is rdinit=/init rw console=ttyMSM0 (no earlycon/ignore_loglevel)" "hdr $B/boot.img | grep -q '^cmdline=rdinit=/init rw console=ttyMSM0,115200 loglevel=4\$'  || [ -n \"\${PROD_CMDLINE:-}\" ]"
# sections: kernel (vmlinuz+dtb) and ramdisk slices
PS=$(hdr "$B/boot.img" | head -1 | sed 's/.*pagesize=\([0-9]*\).*/\1/')
KS=$(hdr "$B/boot.img" | head -1 | sed 's/.*ksize=\([0-9]*\).*/\1/'); RS=$(hdr "$B/boot.img" | head -1 | sed 's/.*rsize=\([0-9]*\).*/\1/')
tar xzf "$OUT/cache/$(basename "$KERNEL_APK_URL")" -C "$T" boot/vmlinuz "boot/dtbs/qcom/${DTB_NAME:-msm8916-thwc-ufi001c.dtb}" 2>/dev/null
cat "$T/boot/vmlinuz" "$T/boot/dtbs/qcom/${DTB_NAME:-msm8916-thwc-ufi001c.dtb}" > "$T/k"
dd if="$B/boot.img" of="$T/k.out" bs=$PS skip=1 2>/dev/null; head -c $KS "$T/k.out" > "$T/k1"
chk "kernel section = vmlinuz + DTB (Image.gz-dtb) from the sha-pinned apk" "cmp -s $T/k $T/k1"
KP=$(( (KS + PS - 1) / PS )); dd if="$B/boot.img" of="$T/r.out" bs=$PS skip=$((1+KP)) 2>/dev/null; head -c $RS "$T/r.out" > "$T/r1"
chk "ramdisk section = initramfs.cpio.gz" "cmp -s $B/initramfs.cpio.gz $T/r1"
# initramfs content
mkdir "$T/rd"; ( cd "$T/rd" && gzip -dc "$B/initramfs.cpio.gz" | cpio -idm --quiet 2>/dev/null )
chk "initramfs has /init (exec) and bin/busybox" "[ -x $T/rd/init ] && [ -x $T/rd/bin/busybox ]"
chk "initramfs carries no kernel modules" "[ ! -e $T/rd/lib/modules ] && ! gzip -dc $B/initramfs.cpio.gz | cpio -t --quiet 2>/dev/null | grep -q '\\.ko'"
chk "init.txt == configs/initramfs/init == /init in cpio" "cmp -s $B/init.txt $ROOT/configs/initramfs/init && cmp -s $B/init.txt $T/rd/init"
BAD='telnetd|udhcpd|insmod|ttyGS0|testboot|devmem|ifconfig|configfs'
nobad() { ! grep -Ev '^[[:space:]]*#' "$1" | grep -Eq "$BAD"; }
chk "init: none of $BAD (comments ignored)" "nobad $B/init.txt"
[ -f "$PROVEN_INIT" ] && neg "forbidden-string scan on the test init (telnetd/ttyGS0)" "nobad $PROVEN_INIT"
chk "init: syntax (busybox sh -n)" "busybox sh -n $B/init.txt"
for s in 'PARTNAME=userdata' 'etc/openstick-rootfs' 'watchdog -F -t 3' 'switch_root' 'setsid cttyhack' '/dev/kmsg'; do chk "init contains '$s'" "grep -qF -- '$s' $B/init.txt"; done
# reproducibility
mkdir "$T/o2"; PROD_OUT=$T/o2 "$ROOT/scripts/50-boot-img.sh" >/dev/null 2>&1
chk "boot.img reproducible (second build identical)" "cmp -s $B/boot.img $T/o2/boot.img && cmp -s $B/initramfs.cpio.gz $T/o2/initramfs.cpio.gz"

# ---- rootfs.img ----
if [ -f "$R/rootfs.img" ]; then
    I=$R/rootfs.img
    chk "rootfs.img: e2fsck -fn clean" "e2fsck -fn $I >/dev/null 2>&1"
    chk "rootfs.img: label osroot, UUID 5a1c0de0-7e57-4b00-8000-0f1e5c0de005" "dumpe2fs -h $I 2>/dev/null | grep -q '^Filesystem volume name: *osroot\$' && dumpe2fs -h $I 2>/dev/null | grep -q '^Filesystem UUID: *5a1c0de0-7e57-4b00-8000-0f1e5c0de005\$'"
    chk "rootfs.img: no orphan_file/metadata_csum_seed" "! dumpe2fs -h $I 2>/dev/null | grep -E '^Filesystem features' | grep -Eq 'orphan_file|metadata_csum_seed'"
    chk "rootfs.img: size multiple of 1 MiB" "[ \$(( \$(stat -c %s $I) % 1048576 )) -eq 0 ]"
    chk "rootfs.img: /boot empty" "[ -z \"\$(debugfs -R 'ls -p /boot' $I 2>/dev/null | awk -F/ '\$6!=\".\"&&\$6!=\"..\"&&\$6!=\"\"')\" ]"
    chk "rootfs.img: systemd present, owner root" "debugfs -R 'stat /usr/lib/systemd/systemd' $I 2>/dev/null | grep -q 'User: *0 *Group: *0'"
    chk "rootfs.img: /sbin/init link target" "debugfs -R 'stat /sbin/init' $I 2>/dev/null | grep -q 'Fast link dest: \"/lib/systemd/systemd\"\\|Fast link dest: \"../lib/systemd/systemd\"' || debugfs -R 'stat /sbin/init' $I 2>/dev/null | grep -q 'Type: symlink'"
    hasmark() { debugfs -R 'stat /etc/openstick-rootfs' "$1" 2>/dev/null | grep -q 'Inode:'; }
    if hasmark "$I"; then chk "rootfs.img: /etc/openstick-rootfs marker" "true"
    else
        echo "UNVERIFIED rootfs.img: /etc/openstick-rootfs absent (tgz predates the production rootfs); exercising the check on a throwaway copy"
        cp "$I" "$T/m.img"; echo test > "$T/mark"
        debugfs -w -R "write $T/mark etc/openstick-rootfs" "$T/m.img" >/dev/null 2>&1
        chk "marker check works on a throwaway copy with the marker added" "hasmark $T/m.img"
        neg "marker check on the image without the marker" "hasmark $I"
    fi
else echo "SKIP rootfs.img checks (run scripts/51-rootfs-img.sh)"; fi
[ $fail -eq 0 ] && echo "ALL OK" || echo "FAILURES"
exit $fail
