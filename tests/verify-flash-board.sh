#!/bin/sh
# SPDX-License-Identifier: MIT
# Rootless test of tools/flash/flash-board.sh `boot` mode against a STUB fastboot (no device). Checks the product branching.
set -eu
D=$(cd "$(dirname "$0")/.." && pwd); T=$(mktemp -d); trap 'rm -rf -- "$T"' EXIT INT TERM
fail=0; chk() { if eval "$2"; then echo "OK   $1"; else echo "FAIL $1"; fail=1; fi; }
mkdir -p "$T/bin" "$T/out/prod-boot" "$T/out/step1" "$T/bk"
head -c 524288 /dev/zero | tr '\0' 'L' > "$T/out/step1/lk2nd-padded-512KiB.img"
head -c 1000000 /dev/zero | tr '\0' 'B' > "$T/out/prod-boot/boot.img"
( cd "$T/out/prod-boot" && sha256sum boot.img > SHA256SUMS )
for p in modemst1 modemst2 fsc fsg persist sec; do echo $p > "$T/bk/$p.bin"; done
( cd "$T/bk" && sha256sum *.bin > SHA256SUMS )
LKSHA=$(sha256sum < "$T/out/step1/lk2nd-padded-512KiB.img" | cut -d' ' -f1)
cat > "$T/bin/fastboot" <<'STUB'
#!/bin/sh
# STUB: product/psize from env; logs calls; `fetch` returns what was last flashed (or corrupted if STUB_CORRUPT=1)
echo "$*" >> "$STUB_LOG"
case "$1 $2" in
  "devices ") echo "ABC fastboot";;
  "getvar product") echo "product: $STUB_PRODUCT" >&2;;
  "getvar partition-size:boot") echo "partition-size:boot: $STUB_PSIZE" >&2;;
  "getvar lk2nd:compatible") echo "lk2nd:compatible: thwc,ufi001c" >&2;;
  "flash boot") cp "$3" "$STUB_STATE";;
  "fetch boot") cp "$STUB_STATE" "$3"; [ "${STUB_CORRUPT:-0}" = 1 ] && printf X | dd of="$3" bs=1 seek=600000 conv=notrunc status=none; exit 0;;
esac
exit 0
STUB
chmod +x "$T/bin/fastboot"
run() { # $1 product $2 psize $3.. extra env
  p=$1; s=$2; shift 2
  : > "$T/log"; env PATH="$T/bin:$PATH" OUT="$T/out" BACKUP_DIR="$T/bk" LK2ND_PADDED_SHA256="$LKSHA" STUB_LOG="$T/log" STUB_STATE="$T/state" \
    STUB_PRODUCT="$p" STUB_PSIZE="$s" COMMIT=1 CONFIRM=flash-ufi103s-boot "$@" sh "$D/tools/flash/flash-board.sh" boot write >"$T/o" 2>&1
}
BK=$T/bk; case "$BK" in "$D"/*) echo "tmp inside repo?"; exit 1;; esac
chk "lk2nd: exit 0"                 'run lk2nd-msm8916 0xf80000'
chk "lk2nd: flashes plain boot.img" 'grep -q "^flash boot .*prod-boot/boot.img" $T/log'
chk "lk2nd: no fetch"               '! grep -q "^fetch" $T/log'
chk "lk1st: exit 0"                 'run lk1st-msm8916 0x1000000'
chk "lk1st: never plain boot.img"   '! grep -q "^flash boot .*prod-boot/boot.img" $T/log && grep -q "^flash boot .*composed.img" $T/log'
chk "lk1st: read-back done"         'grep -q "^fetch boot" $T/log && grep -q "read-back verified" $T/o'
chk "lk1st: flashed content = lk2nd + boot.img@0x80000" 'cmp -n 524288 $T/state $T/out/step1/lk2nd-padded-512KiB.img && [ "$(tail -c +524289 $T/state | head -c 1000000 | sha256sum)" = "$(sha256sum < $T/out/prod-boot/boot.img)" ]'
chk "lk1st: corrupted read-back fails" '! run lk1st-msm8916 0x1000000 STUB_CORRUPT=1 && grep -q "READ-BACK MISMATCH" $T/o'
chk "other product refused, no flash" '! run fastboot-other 0x1000000 && ! grep -q "^flash" $T/log && grep -q refusing $T/o'
chk "lk1st with lk2nd psize refused" '! run lk1st-msm8916 0xf80000 && ! grep -q "^flash" $T/log'
# composite head vs backup boot head (warning only, never a failure)
cp "$T/out/step1/lk2nd-padded-512KiB.img" "$T/bk/boot.img"; head -c 100000 /dev/zero >> "$T/bk/boot.img"
chk "lk1st: head == backup head reported" 'run lk1st-msm8916 0x1000000 && grep -q "composite head (512 KiB) == backup boot head: $LKSHA" $T/o'
head -c 524288 /dev/zero | tr '\0' 'Z' > "$T/bk/boot.img"
chk "lk1st: head != backup head warns, both shas, exit 0" 'run lk1st-msm8916 0x1000000 && grep -q "WARNING: composite head sha256 $LKSHA != backup boot head sha256 [0-9a-f]\{64\}" $T/o'
rm -f "$T/bk/boot.img"
chk "lk1st: no backup boot -> warning, exit 0" 'run lk1st-msm8916 0x1000000 && grep -q "no usable backup boot image" $T/o'
head -c 16300000 /dev/zero > "$T/out/prod-boot/boot.img"; ( cd "$T/out/prod-boot" && sha256sum boot.img > SHA256SUMS )
chk "lk1st: oversize refused"       '! run lk1st-msm8916 0x1000000 && ! grep -q "^flash" $T/log && grep -q "does not fit" $T/o'
chk "write without CONFIRM refused" '! run lk2nd-msm8916 0xf80000 CONFIRM=no && ! grep -q "^flash" $T/log'
exit $fail
