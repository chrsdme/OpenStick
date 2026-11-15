#!/bin/sh
# SPDX-License-Identifier: MIT
# NOTE: CURRENT path for board #6 is scripts/50-boot-img.sh (boot.img = kernel+DTB+initramfs, `fastboot flash boot`, rootfs in
# userdata via scripts/51-rootfs-img.sh). This script (lk2nd + extlinux in a 64 MiB GPT-B boot partition) is the FUTURE extlinux path.
# boot-lk2nd.img: the layout lk2nd needs inside the 64 MiB GPT-B `boot` partition. ROOTLESS, offline, no device.
#   [0, 512 KiB)       lk2nd-padded-512KiB.img (stock aboot loads it as an Android boot image from the start of `boot`)
#   [512 KiB, 64 MiB)  ext2 with /extlinux/extlinux.conf, /vmlinuz, /dtbs (lk2nd splits `boot` at 512 KiB and scans it as ext2)
# Source of /boot: out/rootfs-<board>.tgz (same as scripts/40-images.sh). Does NOT touch boot.bin / rootfs.bin.
# Output: out/bootlk2nd/boot-lk2nd.img (raw, sparse on disk, exactly the `boot` size) + SHA256SUMS.
. "$(dirname "$0")/common.sh"
[ "$(id -u)" -ne 0 ] || die "run WITHOUT root"
for t in fakeroot mke2fs tar truncate dd sha256sum; do command -v $t >/dev/null || die "missing tool: $t"; done
LK=${LK2ND_IMG:-$OUT/step1/lk2nd-padded-512KiB.img}
LK_SHA256=${LK2ND_PADDED_SHA256:-d82f3535972869deb2f963ac33a33ea2cd01cbf1cbd661a4bcc23f491fbcbfde}
TGZ=$OUT/rootfs-$BOARD.tgz; LAYOUT=$OUT/fw/layout-$BOARD.env
[ -f "$LK" ] && [ -f "$TGZ" ] && [ -f "$LAYOUT" ] || die "missing $LK, $TGZ or $LAYOUT"
echo "$LK_SHA256  $LK" | sha256sum -c - >/dev/null || die "lk2nd image sha256 mismatch (set LK2ND_PADDED_SHA256 if you rebuilt it on purpose)"
. "$LAYOUT"
LK_BYTES=$((512 * 1024)); [ "$(stat -c %s "$LK")" -eq "$LK_BYTES" ] || die "lk2nd image must be exactly 512 KiB"
FS_BYTES=$(( (BOOT_BYTES - LK_BYTES) / 4096 * 4096 ))
D=$OUT/bootlk2nd; S=$D/stage; mkdir -p "$D"; rm -rf --one-file-system -- "${S:?}"; mkdir -p "$S"
trap 'rm -rf --one-file-system -- "${S:?}"' EXIT INT TERM
IMG=$D/boot-lk2nd.img
export TGZ S D FS_BYTES
fakeroot -- sh -eu -c '
    tar xf "$TGZ" -C "$S" ./boot --exclude="./boot/linux.efi" --strip-components=2
    truncate -s "$FS_BYTES" "$D/fs.raw"; mke2fs -q -t ext2 -d "$S" "$D/fs.raw"
'
rm -f "$IMG"; truncate -s "$BOOT_BYTES" "$IMG"
dd if="$LK" of="$IMG" bs=4096 conv=notrunc status=none
dd if="$D/fs.raw" of="$IMG" bs=4096 seek=$((LK_BYTES / 4096)) conv=notrunc status=none
rm -f "$D/fs.raw"
[ "$(stat -c %s "$IMG")" -eq "$BOOT_BYTES" ] || die "wrong image size"
( cd "$D" && sha256sum boot-lk2nd.img > SHA256SUMS && cat SHA256SUMS )
echo "boot-lk2nd.img: $BOOT_BYTES B = lk2nd 512 KiB + ext2 $FS_BYTES B; write target: GPT-B boot partition (LBA $BOOT_START, $BOOT_SECTORS sectors) [NOT written by this script]"
