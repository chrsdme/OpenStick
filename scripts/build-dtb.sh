#!/bin/sh
# SPDX-License-Identifier: MIT
# Compile dts/<name>.dts against the kernel headers in src/linux-dts (run fetch-dts.sh first).
# Usage: scripts/build-dtb.sh [dts-basename ...]   (default: derived from DTB_NAME of the board)
. "$(dirname "$0")/common.sh"
cd "$ROOT"
K=src/linux-dts
[ -d "$K/include/dt-bindings" ] || { echo "ERROR: run scripts/fetch-dts.sh first" >&2; exit 1; }
mkdir -p out/dtbs
[ $# -gt 0 ] || set -- "${DTB_NAME%.dtb}"
for n in "$@"; do
    src=dts/$n.dts
    [ -f "$src" ] || { echo "ERROR: missing $src" >&2; exit 1; }
    cpp -nostdinc -undef -x assembler-with-cpp -D__DTS__ -I dts -I "$K/arch/arm64/boot/dts/qcom" \
        -I "$K/arch/arm64/boot/dts" -I "$K/include" -o "out/dtbs/$n.dts.pre" "$src"
    dtc -@ -I dts -O dtb -Wno-unit_address_vs_reg -Wno-graph_child_address -Wno-unique_unit_address -Wno-avoid_unnecessary_addr_size \
        -o "out/dtbs/$n.dtb" "out/dtbs/$n.dts.pre"
    rm -f "out/dtbs/$n.dts.pre"
    ls -l "out/dtbs/$n.dtb"
done
