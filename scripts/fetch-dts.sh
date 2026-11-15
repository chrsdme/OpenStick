#!/bin/sh
# SPDX-License-Identifier: MIT
# Sparse, shallow checkout of the kernel DT sources into src/linux-dts (git-ignored).
# Only include/dt-bindings and arch/arm64/boot/dts/qcom are materialised.
set -eu
cd "$(dirname "$0")/.."
. config/dts-pin.env
d=src/linux-dts
if [ -f "$d/.fetched-$DTS_COMMIT" ]; then echo "skip $d"; exit 0; fi
rm -rf --one-file-system -- "${d:?}"; mkdir -p "$d"
git -C "$d" init -q
git -C "$d" remote add origin "$DTS_REPO"
git -C "$d" sparse-checkout init --no-cone
git -C "$d" sparse-checkout set '/include/dt-bindings/' '/arch/arm64/boot/dts/qcom/' '/arch/arm64/boot/dts/include/' '/scripts/dtc/include-prefixes/' '/include/uapi/linux/input-event-codes.h' '/COPYING' '/LICENSES/'
git -C "$d" fetch -q --depth 1 --filter=blob:none origin "$DTS_COMMIT"
git -C "$d" checkout -q FETCH_HEAD
touch "$d/.fetched-$DTS_COMMIT"
du -sh "$d" | cut -f1
echo "ok   $d @ $(echo "$DTS_COMMIT" | cut -c1-8)"
