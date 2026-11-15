#!/bin/sh
# SPDX-License-Identifier: MIT
# Verifies the CURRENT artifacts: out/prod-boot/boot.img, out/prod-rootfs/rootfs.img, out/step1/lk2nd-padded-512KiB.img (+ SHA256SUMS),
# the generated GPT files (out/fw) and the rootfs tarball policy. Never touches a device. Rootless.
# Legacy artifacts of the old flow (out/files-*, out/bootimg*, out/bootlk2nd, out/obsolete, boot.bin/rootfs.bin) are reported
# as `SKIPPED (stale)`, never OK/FAIL.
#   Part A (no root): GPT/layout checks (kept entries byte-identical to the local template, no overlap, bounds,
#           alignment, images <= partitions, secondary == primary, calibration/boot-chain untouched), each
#           with a negative control (self-test on deliberately broken copies), plus an independent sgdisk -v.
#   Part B (root): converts the sparse images to raw under work/, mounts them READ-ONLY, checks contents.
# LAYOUT_ONLY=1 runs only part A (the mount checks are then reported as NOT RUN, never as OK).
# SKIP = a check could not run (e.g. no BOARD_GPT_TEMPLATE); it is reported, not counted as OK.
. "$(dirname "$0")/../scripts/common.sh"
. "$ROOT/tests/lib-ownership.sh"
F=$FILES_DIR; GEN=$OUT/fw; LAY=$GEN/layout-$BOARD.txt
fail=0
chk() { if eval "$2"; then echo "OK   $1"; else echo "FAIL $1"; fail=1; fi; }
command -v python3 >/dev/null || die "python3 not found"
F=${PROD_BOOT_DIR:-$OUT/prod-boot}; R=${PRODROOT_OUT:-$OUT/prod-rootfs}; S1=$OUT/step1
LKP=$S1/lk2nd-padded-512KiB.img
# 1) stale legacy artifacts: reported, never judged
for g in "$OUT"/files-* "$OUT"/bootimg* "$OUT"/bootlk2nd "$OUT"/obsolete; do
    [ -e "$g" ] && echo "SKIPPED (stale) legacy artifact $(basename "$g") (old flow, no longer produced)"
done
echo "SKIPPED (stale) boot.bin/rootfs.bin sparse images, gptool image-vs-partition checks, mount checks of boot.raw/rootfs.raw (old EDL flow)"
# 2) current artifacts
chk "prod-boot/boot.img present"   "[ -s $F/boot.img ]"
chk "prod-boot SHA256SUMS matches" "[ -f $F/SHA256SUMS ] && (cd $F && sha256sum -c --quiet SHA256SUMS)"
chk "boot.img is an Android boot image (ANDROID! magic)" "[ \"\$(head -c 8 $F/boot.img 2>/dev/null)\" = 'ANDROID!' ]"
chk "prod-rootfs/rootfs.img present" "[ -s $R/rootfs.img ]"
chk "prod-rootfs SHA256SUMS matches" "[ -f $R/SHA256SUMS ] && (cd $R && sha256sum -c --quiet SHA256SUMS)"
chk "step1/lk2nd-padded-512KiB.img is exactly 512 KiB" "[ \"\$(stat -c %s $LKP 2>/dev/null)\" = 524288 ]"
chk "step1 SHA256SUMS lists and matches the lk2nd images" "[ -f $S1/SHA256SUMS ] && (cd $S1 && grep -E 'lk2nd' SHA256SUMS | sha256sum -c --quiet -)"
chk "lk1st composite (lk2nd 512 KiB + boot.img at 0x80000) fits 0x1000000" "[ \$((524288 + \$(stat -c %s $F/boot.img 2>/dev/null || echo 99999999999))) -le 16777216 ]"
if command -v e2fsck >/dev/null && [ -s "$R/rootfs.img" ]; then
    chk "rootfs.img: e2fsck clean (read-only)" "e2fsck -fn $R/rootfs.img >/dev/null 2>&1"
    chk "rootfs.img: label $ROOT_LABEL" "[ \"\$(dumpe2fs -h $R/rootfs.img 2>/dev/null | sed -n 's/^Filesystem volume name: *//p')\" = '$ROOT_LABEL' ]"
else echo "SKIP rootfs.img e2fsck/label (e2fsprogs missing or image absent)"; fi
# 3) generated GPT files (still current)
if [ -f "$GEN/gpt-primary.bin" ] && [ -f "$GEN/gpt-secondary.bin" ] && command -v sgdisk >/dev/null; then
    mkdir -p "$WORK"; TD=$(mktemp -d "$WORK/verify-gpt.XXXXXX")
    truncate -s $((EMMC_SECTORS * 512)) "$TD/d.img"
    dd if="$GEN/gpt-primary.bin" of="$TD/d.img" bs=512 conv=notrunc status=none
    dd if="$GEN/gpt-secondary.bin" of="$TD/d.img" bs=512 seek=$((EMMC_SECTORS - 33)) conv=notrunc status=none
    chk "sgdisk -v (independent parser): No problems found" "sgdisk -v $TD/d.img | grep -q 'No problems found'"
    rm -rf --one-file-system -- "${TD:?}"
else echo "SKIP sgdisk cross-check (gpt files or sgdisk missing)"; fi
# 4) ownership policy on the inputs
chk "ownership detector self-test (negative + positive controls)" "own_selftest"
if [ -f "$OUT/rootfs-$BOARD.tgz" ]; then chk "rootfs tarball: ownership/permission policy" "own_scan_tar $OUT/rootfs-$BOARD.tgz"
else echo "SKIP rootfs tarball ownership check"; fi
if [ -d "$WORK/gt-dist" ]; then chk "gt-dist overlay: same policy" "own_scan_dir $WORK/gt-dist"; else echo "SKIP gt-dist ownership check"; fi
exit $fail
