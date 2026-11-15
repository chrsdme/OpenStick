#!/bin/sh
# SPDX-License-Identifier: MIT
# !!! WARNING: boot.bin produced here must NEVER be written to the p21/boot partition: it ERASES lk2nd (the bootloader
# !!! chain would then need EDL recovery). Current paths: scripts/50-boot-img.sh (production boot.img, `fastboot flash boot`),
# !!! scripts/46-boot-lk2nd.sh (lk2nd + extlinux layout), scripts/51-rootfs-img.sh (rootfs for userdata).
# rootfs tarball -> sparse fastboot images. Adapted from build_images.sh of
# kinsamanka/OpenStick-Builder. Writes only under out/ and work/. Never touches a device.
# Image sizes come from the GPT-B layout (out/fw/layout-<BOARD>.env, written by scripts/gen-gpt.sh),
# rounded DOWN to 4096 B, and the script refuses any image larger than its partition.
# Modes:
#   sudo scripts/40-images.sh              canonical: loop mounts (needs root)
#   ROOTLESS=1 scripts/40-images.sh        no root: fakeroot + mke2fs -d (same content and ownership as seen by
#                                          the image, but NOT the canonical path; rebuild with sudo before flashing)
. "$(dirname "$0")/common.sh"
ROOTLESS=${ROOTLESS:-0}
if [ "$ROOTLESS" = 1 ]; then
    command -v fakeroot >/dev/null || die "ROOTLESS=1 needs fakeroot"
    command -v mke2fs >/dev/null || die "ROOTLESS=1 needs mke2fs (e2fsprogs >= 1.43 for -d)"
else
    need_root
fi
command -v img2simg >/dev/null || die "img2simg not found (android-sdk-libsparse-utils)"
TGZ=$OUT/rootfs-$BOARD.tgz; [ -f "$TGZ" ] || die "missing $TGZ"
# image sizes come from the GPT layout generated for this board (scripts/gen-gpt.sh)
LAYOUT=$OUT/fw/layout-$BOARD.env; [ -f "$LAYOUT" ] || die "missing $LAYOUT (run scripts/gen-gpt.sh)"
. "$LAYOUT"
for v in ROOTFS_BYTES BOOT_BYTES; do
    eval "val=\${$v:-}"
    case "$val" in ''|*[!0-9]*) die "$v missing or not a number in $LAYOUT";; esac
done
ROOTFS_SIZE=$(( ${ROOTFS_SIZE:-$ROOTFS_BYTES} / 4096 * 4096 ))   # round DOWN to 4 KiB: never larger than the partition
BOOT_SIZE=$(( ${BOOT_SIZE:-$BOOT_BYTES} / 4096 * 4096 ))
[ "$ROOTFS_SIZE" -le "$ROOTFS_BYTES" ] && [ "$BOOT_SIZE" -le "$BOOT_BYTES" ] || die "image larger than its partition"
[ "$ROOTFS_SIZE" -ge $((512 * 1048576)) ] || die "rootfs image too small ($ROOTFS_SIZE B)"
echo "sizes from $LAYOUT: boot $BOOT_SIZE B (partition $BOOT_BYTES B), rootfs $ROOTFS_SIZE B (partition $ROOTFS_BYTES B)"
if [ "$ROOTLESS" = 1 ]; then SFX=user; else SFX=root; fi
MNT=$WORK/mnt; STAGE=$WORK/stage-$SFX; IMG=$WORK/img
[ "$ROOTLESS" = 1 ] && IMG=$WORK/img-user
safe_dir "$MNT"; safe_dir "$STAGE"; safe_dir "$IMG"
mkdir -p "$FILES_DIR" 2>/dev/null || true
[ -w "$FILES_DIR" ] || die "$FILES_DIR is not writable"
GT=$WORK/gt-dist
GT_OK=1
. "$ROOT/tests/lib-ownership.sh"
{ [ -x "$GT/usr/bin/gt" ] && [ -n "$(ls "$GT"/usr/lib/libusbgx.so.* 2>/dev/null)" ] && [ -d "$GT/etc/gt" ] && [ -n "$(ls -A "$GT/etc/gt" 2>/dev/null)" ]; } || GT_OK=0
if [ "$GT_OK" != 1 ] && [ "${ALLOW_NO_GADGET:-0}" != 1 ]; then
    die "incomplete $GT (need usr/bin/gt, usr/lib/libusbgx.so.*, etc/gt templates; run scripts/30-gt.sh). Override with ALLOW_NO_GADGET=1"
fi

[ "$GT_OK" != 1 ] || own_scan_dir "$GT" || die "ownership/permission violations in $GT (rebuild with sudo scripts/30-gt.sh)"
if [ "$ROOTLESS" = 1 ]; then
    cleanup() { chmod -R u+rwX "$STAGE" 2>/dev/null || true; rm -rf --one-file-system -- "${STAGE:?}" "${IMG:?}/boot.raw" "${IMG:?}/rootfs.raw"; return 0; }
    trap cleanup EXIT INT TERM
    rm -rf --one-file-system -- "${STAGE:?}"; mkdir -p "$STAGE/boot" "$STAGE/rootfs" "$IMG"; rm -f "$IMG/rootfs.raw" "$IMG/boot.raw"
    export TGZ STAGE IMG GT GT_OK BOOT_SIZE ROOTFS_SIZE
    # one fakeroot session: tar extraction, gadget overlay and mke2fs -d all see the same faked ownership
    fakeroot -- sh -eu -c '
        tar xf "$TGZ" -C "$STAGE/boot" ./boot --exclude="./boot/linux.efi" --strip-components=2
        truncate -s "$BOOT_SIZE" "$IMG/boot.raw"; mke2fs -q -t ext2 -d "$STAGE/boot" "$IMG/boot.raw"
        tar xpf "$TGZ" -C "$STAGE/rootfs" --exclude="./boot/*" --exclude="./dev/*"
        if [ "$GT_OK" = 1 ]; then cp -a --no-preserve=ownership "$GT"/. "$STAGE/rootfs"/; else echo "WARN: ALLOW_NO_GADGET=1, image built WITHOUT gadget tools (no usb0)"; fi
        du -sk "$STAGE/rootfs" | awk "{print \"rootfs content (KiB): \" \$1}"
        truncate -s "$ROOTFS_SIZE" "$IMG/rootfs.raw"; mke2fs -q -t ext4 -d "$STAGE/rootfs" "$IMG/rootfs.raw"
    '
else
    # cleanup on success, failure or interrupt: unmount (-d also detaches the loop device)
    cleanup() {
        mountpoint -q "$MNT" && umount -d "$MNT"
        for f in "$IMG/boot.raw" "$IMG/rootfs.raw"; do
            for l in $(losetup -j "$f" 2>/dev/null | cut -d: -f1); do losetup -d "$l" || true; done
        done
        return 0
    }
    trap cleanup EXIT INT TERM
    mkdir -p "$MNT" "$IMG"
    rm -f "$IMG/rootfs.raw" "$IMG/boot.raw"
    truncate -s "$BOOT_SIZE" "$IMG/boot.raw"; mkfs.ext2 -q "$IMG/boot.raw"
    mount -o loop "$IMG/boot.raw" "$MNT"
    tar xf "$TGZ" -C "$MNT" ./boot --exclude='./boot/linux.efi' --strip-components=2
    umount "$MNT"

    truncate -s "$ROOTFS_SIZE" "$IMG/rootfs.raw"; mkfs.ext4 -q "$IMG/rootfs.raw"
    mount -o loop "$IMG/rootfs.raw" "$MNT"
    tar xpf "$TGZ" -C "$MNT" --exclude='./boot/*' --exclude='./dev/*'
    if [ "$GT_OK" = 1 ]; then cp -a --no-preserve=ownership "$GT"/. "$MNT"/; else echo "WARN: ALLOW_NO_GADGET=1, image built WITHOUT gadget tools (no usb0)"; fi
    df -Pk "$MNT" | tail -1
    umount "$MNT"
fi

# raw size sanity before conversion (stat, sparse file apparent size)
[ "$(stat -c %s "$IMG/boot.raw")" -le "$BOOT_BYTES" ] && [ "$(stat -c %s "$IMG/rootfs.raw")" -le "$ROOTFS_BYTES" ] || die "raw image larger than its partition"
img2simg "$IMG/rootfs.raw" "$FILES_DIR/rootfs.bin"
img2simg "$IMG/boot.raw" "$FILES_DIR/boot.bin"
( cd "$FILES_DIR" && sha256sum boot.bin rootfs.bin > SHA256SUMS && cat SHA256SUMS )
ls -l "$FILES_DIR"
echo "WARNING: boot.bin must NOT be flashed to boot (erases lk2nd). Use scripts/46-boot-lk2nd.sh or scripts/50-boot-img.sh."
