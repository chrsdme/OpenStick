#!/bin/sh
# SPDX-License-Identifier: MIT
# Stage 1: minimal Debian arm64 base, saved as a tarball in out/.
. "$(dirname "$0")/common.sh"
need_root; require_binfmt
mkdir -p "$OUT" "$WORK"; require_space "$WORK" 3000
BASE_CHROOT=$WORK/base
safe_rm_dir "$BASE_CHROOT"
echo "base chroot: $BASE_CHROOT"

KEYRING=/usr/share/keyrings/debian-archive-keyring.gpg
[ -r "$KEYRING" ] || die "missing $KEYRING (apt install debian-archive-keyring)"
INC=$(echo $BASE_PACKAGES | tr ' ' ',')

trap 'umount_chroot "$BASE_CHROOT" 2>/dev/null || true' EXIT

if ! debootstrap --arch arm64 --variant=minbase --keyring "$KEYRING" \
        --include="$INC" "$RELEASE" "$BASE_CHROOT" "$MIRROR"; then
    echo "single-stage failed, falling back to --foreign + second stage" >&2
    safe_rm_dir "$BASE_CHROOT"
    debootstrap --foreign --arch arm64 --variant=minbase --keyring "$KEYRING" \
        --include="$INC" "$RELEASE" "$BASE_CHROOT" "$MIRROR"
    chroot_run "$BASE_CHROOT" /debootstrap/debootstrap --second-stage
fi

cat > "$BASE_CHROOT/etc/apt/sources.list" <<EOT
deb $MIRROR $RELEASE $COMPONENTS
deb http://deb.debian.org/debian-security/ $RELEASE-security $COMPONENTS
deb $MIRROR $RELEASE-updates $COMPONENTS
EOT

tar --numeric-owner -cpzf "$BASE_TAR" -C "$BASE_CHROOT" .
safe_rm_dir "$BASE_CHROOT"
sha256sum "$BASE_TAR"; ls -l "$BASE_TAR"
