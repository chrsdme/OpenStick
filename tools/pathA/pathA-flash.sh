#!/bin/bash
# SPDX-License-Identifier: MIT
# Path A flash for board #6 (UFI103S): GPT-T, tz, rpm, sbl1, hyp (qhypstub), aboot (lk1st). ONE EDL session, no boot in between.
# usage: LDR=... ./pathA-flash.sh <phase> [dry|write]      phases: pre gpt tz rpm sbl1 hyp aboot final
# Default mode is dry. A real write needs: COMMIT=1 CONFIRM=da-flashez-<phase>. Each phase = one separate "da, flashez".
# Writes use --sectorsize=512, never --lun, always --skipwrite first, read-back sha256 compare. Reset (phase final) has no --memory.
. "$(dirname "$0")/fwlib.sh"
PH=${1:-}; MODE=${2:-dry}; TOKEN=da-flashez-$PH
[ "$MODE" = dry ] || [ "$MODE" = write ] || die "mode must be dry|write"
init
F=$PKG/fw; G=$PKG/gpt; R=$PKG/rollback
ORIG_P=$(sha "$R/gpt-orig-primary.bin"); ORIG_S=$(sha "$R/gpt-orig-secondary.bin")
NEW_P=$(sha "$G/gpt-T-primary.bin");     NEW_S=$(sha "$G/gpt-T-secondary.bin")
state() { # read-only picture of everything (also used by pre/final)
    classify gptP 0 34 orig=$ORIG_P new=$NEW_P
    classify gptS $SEC_LBA 33 orig=$ORIG_S new=$NEW_S
    classify sbl1 262144 1024 stock=$(sha "$R/stock-sbl1.bin") new=$(sha "$F/sbl1.img")
    classify rpm 268288 1024 stock=$(sha "$R/stock-rpm.bin") new=$(sha "$F/rpm.img")
    classify tz 270336 2048 stock=$(sha "$R/stock-tz-tzbak.bin") new=$(sha "$F/tz.img")
    classify hyp 272384 1024 stock=$(sha "$R/stock-hyp.bin") new=$(sha "$F/hyp.img")
    classify aboot 264192 2048 stock=$(sha "$R/stock-aboot.bin") new=$(sha "$F/aboot.img")
}
case "$PH" in
pre)   # read only
    echo "## pre: read-only identity + state"; state
    [ "$ST_gptP" = orig ] && [ "$ST_gptS" = orig ] || die "GPT on the device is not the known original (this is not the expected board/state)"
    [ "$ST_sbl1" = stock ] && [ "$ST_rpm" = stock ] && [ "$ST_tz" = stock ] && [ "$ST_hyp" = stock ] && [ "$ST_aboot" = stock ] || echo "NOTE: some region is not stock (resuming a partial run is fine; anything UNKNOWN is not)";;
gpt)   # secondary first (old primary stays valid until the very last write), then primary
    put gptS $SEC_LBA 33 "$G/gpt-T-secondary.bin" "$MODE"
    put gptP 0 34 "$G/gpt-T-primary.bin" "$MODE";;
tz)    put tz 270336 2048 "$F/tz.img" "$MODE";;
rpm)   put rpm 268288 1024 "$F/rpm.img" "$MODE";;
sbl1)  put sbl1 262144 1024 "$F/sbl1.img" "$MODE";;
hyp)   put hyp 272384 1024 "$F/hyp.img" "$MODE";;
aboot) put aboot 264192 2048 "$F/aboot.img" "$MODE";;
final) # read back everything, then reset (no --memory). No cold boot until you decide.
    state
    for k in gptP gptS sbl1 rpm tz hyp aboot; do eval "v=\$ST_$k"; [ "$v" = new ] || die "$k is $v, expected new"; done
    echo "ALL REGIONS == NEW IMAGES"
    if [ "$MODE" = write ]; then
        [ "${COMMIT:-}" = 1 ] && [ "${CONFIRM:-}" = "$TOKEN" ] || die "reset needs COMMIT=1 CONFIRM=$TOKEN"
        $EDL reset --resetmode=reset --loader="$LDR" >"$W/last-edl.log" 2>&1 || die "reset failed"
        echo "reset sent. Now do a COLD boot (unplug/plug) when ready and observe lsusb / fastboot."
    fi;;
*) echo "phases: pre gpt tz rpm sbl1 hyp aboot final"; exit 2;;
esac
