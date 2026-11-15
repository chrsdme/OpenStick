#!/bin/sh
# SPDX-License-Identifier: MIT
# SMALL rootfs image for the `userdata` partition of the CURRENT GPT. ROOTLESS (fakeroot + mke2fs -d), offline, no device.
# out/rootfs-<BOARD>.tgz -> out/prod-rootfs/rootfs.img (ext4, label osroot, fixed UUID; /boot stripped, boot is separate).
# Size = content (allocated, du) + 5% + 16 MiB fs overhead + ROOTFS_IMG_SLACK_MIB (default 64), rounded up to 1 MiB.
# openstick-growfs (in the rootfs) grows the filesystem to the whole partition at first boot.
# Flash by hand only (never from scripts), after the backup/EDL gate: fastboot flash userdata rootfs.img
# Env: ROOTFS_IMG_SLACK_MIB, PRODROOT_OUT, ROOTFS_TGZ.
. "$(dirname "$0")/common.sh"
if [ "${1:-}" = --inner ]; then
    # ---- runs INSIDE one fakeroot session ----
    set -eu; umask 022
    TGZ=$2 TREE=$3 IMG=$4 SLACK=$5 LABEL=$6 UUID=$7
    tar --numeric-owner -xpzf "$TGZ" -C "$TREE"
    find "$TREE/boot" -mindepth 1 -delete 2>/dev/null || true
    USED=$(du -sm "$TREE" | cut -f1)
    SIZE=$(( USED + USED / 20 + 16 + SLACK ))
    echo "content ${USED} MiB (allocated) -> image ${SIZE} MiB (slack ${SLACK})"
    E2FSPROGS_FAKE_TIME=$(stat -c %Y "$TGZ"); export E2FSPROGS_FAKE_TIME
    rm -f "$IMG"; truncate -s "${SIZE}M" "$IMG"
    mke2fs -q -t ext4 -L "$LABEL" -U "$UUID" -m 1 -O ^orphan_file,^metadata_csum_seed \
        -E root_owner=0:0,hash_seed=$UUID,lazy_itable_init=0 -d "$TREE" "$IMG" || { echo "mke2fs failed (increase ROOTFS_IMG_SLACK_MIB?)" >&2; exit 1; }
    e2fsck -fn "$IMG"
    if debugfs -R 'stat /etc/openstick-rootfs' "$IMG" 2>/dev/null | grep -q 'Inode:'; then echo "marker /etc/openstick-rootfs: present"
    else echo "UNVERIFIED: /etc/openstick-rootfs absent (rootfs tgz predates the production rootfs; the initramfs will refuse to switch)"; fi
    exit 0
fi
[ "$(id -u)" -ne 0 ] || die "run WITHOUT root"
for t in fakeroot mke2fs e2fsck debugfs tar truncate du sha256sum; do command -v $t >/dev/null || die "missing tool: $t"; done
O=${PRODROOT_OUT:-$OUT/prod-rootfs}
W=$WORK/prod-rootfs
TGZ=${ROOTFS_TGZ:-$OUT/rootfs-$BOARD.tgz}
SLACK=${ROOTFS_IMG_SLACK_MIB:-64}
case "$SLACK" in ''|*[!0-9]*) die "ROOTFS_IMG_SLACK_MIB must be a non-negative integer";; esac
[ -f "$TGZ" ] || die "missing $TGZ (run scripts/20-rootfs.sh)"
rm -rf --one-file-system -- "${W:?}"; mkdir -p "$W/tree" "$O"
fakeroot -- sh "$ROOT/scripts/51-rootfs-img.sh" --inner "$TGZ" "$W/tree" "$O/rootfs.img" "$SLACK" osroot 5a1c0de0-7e57-4b00-8000-0f1e5c0de005
rm -rf --one-file-system -- "${W:?}/tree"
( cd "$O" && sha256sum rootfs.img > SHA256SUMS && cat SHA256SUMS ); ls -l "$O/rootfs.img"
echo "NOT flashed. Manual step (owner confirmation + verified backup + EDL first): fastboot flash userdata $O/rootfs.img"
