#!/bin/sh
# SPDX-License-Identifier: MIT
# Cross-build gadget-tools (libusbgx + gt) into work/gt-dist (safe_dir applies; 40-images.sh consumes it).
# Based on scripts/build_gt.sh of kinsamanka/OpenStick-Builder, changed to:
#  - use a throw-away sysroot (libconfig-dev never enters the final rootfs),
#  - use no qemu-aarch64-static (binfmt F flag),
#  - install ONLY into a DESTDIR (the original also ran `make install` on the host).
. "$(dirname "$0")/common.sh"
set -eu
(set -o pipefail) 2>/dev/null && set -o pipefail
need_root; require_binfmt
[ -f "$BASE_TAR" ] || die "missing $BASE_TAR (run scripts/10-base.sh)"
SYSROOT=$WORK/gt-sysroot
DIST=$WORK/gt-dist
BUILD=$WORK/gt-build
safe_rm_dir "$SYSROOT"; safe_rm_dir "$BUILD"; safe_rm_dir "$DIST"
mkdir -p "$SYSROOT" "$BUILD" "$DIST"
tar --numeric-owner -xpzf "$BASE_TAR" -C "$SYSROOT"
trap 'rc=$?; umount_chroot "$SYSROOT" 2>/dev/null || true; [ "$rc" -eq 0 ] || { echo "30-gt.sh FAILED (rc=$rc), removing partial $DIST" >&2; safe_rm_dir "$DIST"; }' EXIT
mount_chroot "$SYSROOT"
chroot_run "$SYSROOT" /bin/sh -c "apt-get update -qq && apt-get install -y -qq --no-install-recommends libconfig-dev libc6-dev"
umount_chroot "$SYSROOT"

# Force ONLY the bookworm sysroot headers. Ubuntu cross-gcc also searches /usr/aarch64-linux-gnu/include
# (glibc >= 2.38), which redirects sscanf/strtol to __isoc23_* (missing in glibc 2.36).
CC=aarch64-linux-gnu-gcc
GCCINC=$($CC -print-file-name=include)
SYSINC="-nostdinc -isystem $GCCINC -isystem $SYSROOT/usr/include/aarch64-linux-gnu -isystem $SYSROOT/usr/include"
CFLAGS_X="--sysroot=$SYSROOT $SYSINC"

( cd "$ROOT/src/libusbgx" && autoreconf -i )
mkdir -p "$BUILD/usbgx"
( cd "$BUILD/usbgx" && \
  PKG_CONFIG_PATH="$SYSROOT/usr/lib/aarch64-linux-gnu/pkgconfig" PKG_CONFIG_SYSROOT_DIR="$SYSROOT" \
  "$ROOT/src/libusbgx/configure" --host aarch64-linux-gnu --prefix=/usr --with-sysroot="$SYSROOT" \
    --disable-examples --disable-tests CFLAGS="$CFLAGS_X" LDFLAGS="--sysroot=$SYSROOT" )
make -C "$BUILD/usbgx" CFLAGS="$CFLAGS_X" -C src
# library + headers + pkgconfig/cmake only (no examples/tests)
make -C "$BUILD/usbgx" DESTDIR="$DIST" -C src install
make -C "$BUILD/usbgx" DESTDIR="$DIST" install-library_includeHEADERS install-function_includeHEADERS install-pkgconfigDATA

mkdir -p "$BUILD/gt"
# LIBDIR (not PATH) hides host .pc files; SYSROOT_DIR prefixes -I/-L with the sysroot (no host /usr/include or /usr/lib).
PKG_CONFIG_LIBDIR="$DIST/usr/lib/pkgconfig:$SYSROOT/usr/lib/aarch64-linux-gnu/pkgconfig:$SYSROOT/usr/share/pkgconfig" \
PKG_CONFIG_SYSROOT_DIR="$SYSROOT" \
    cmake -DCMAKE_INSTALL_PREFIX=/usr \
        -DCMAKE_POLICY_VERSION_MINIMUM=3.5 \
        -DCMAKE_CXX_COMPILER=aarch64-linux-gnu-g++ \
        -DCMAKE_C_COMPILER=aarch64-linux-gnu-gcc \
        -DCMAKE_C_FLAGS="$CFLAGS_X -I$DIST/usr/include -L$DIST/usr/lib" \
        -DCMAKE_FIND_ROOT_PATH_MODE_PROGRAM=NEVER \
        -DCMAKE_SYSROOT="$SYSROOT" \
        -DCMAKE_SYSTEM_PROCESSOR=aarch64 \
        -S "$ROOT/src/gt/source" -B "$BUILD/gt"
make -C "$BUILD/gt" DESTDIR="$DIST" install

rm -rf --one-file-system -- "${DIST:?}/usr/share" "${DIST:?}/usr/lib/cmake" "${DIST:?}/usr/lib/pkgconfig" \
    "${DIST:?}"/usr/lib/*a "${DIST:?}"/usr/bin/ga* "${DIST:?}"/usr/bin/s* "${DIST:?}/usr/include"
install_tree "$ROOT/configs/templates" "$DIST/etc/gt"   # not cp -a: it leaked uid 1000 / mode 664

# ABI check: nothing newer than glibc 2.36, and gt must be aarch64
GT_BIN="$DIST/usr/bin/gt"; GX_LIB=$(ls "$DIST"/usr/lib/libusbgx.so.*.*.* 2>/dev/null | head -1)
[ -x "$GT_BIN" ] || die "missing $GT_BIN"
[ -n "$GX_LIB" ] || die "missing libusbgx.so.*"
file -L "$GT_BIN" | grep -q 'ARM aarch64' || die "gt is not ARM aarch64"
for f in "$GT_BIN" "$GX_LIB"; do
    if aarch64-linux-gnu-objdump -T "$f" | grep -E 'GLIBC_2\.(3[7-9]|[4-9][0-9])|__isoc23_'; then
        die "$f needs glibc > 2.36 (bookworm)"
    fi
done
[ -d "$DIST/etc/gt" ] || die "missing templates in $DIST/etc/gt"
echo "gt ABI check OK"
safe_rm_dir "$SYSROOT"; safe_rm_dir "$BUILD"
find "$DIST" -type f | head -20
