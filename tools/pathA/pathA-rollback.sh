#!/bin/bash
# SPDX-License-Identifier: MIT
# Rollback of Path A to the stock chain, from EDL (reset button + USB). Everything verified by sha256 on write and read-back.
# usage: LDR=... ./pathA-rollback.sh <phase> [dry|write]
#   phases: state  tz  rpm  sbl1  hyp  aboot  boot  gpt  all
#   Order of "all": tz(+tzbak) rpm sbl1 hyp aboot [boot if BOOT_ORIG set] gpt-secondary gpt-primary. GPT goes LAST (firmware chain first).
# `boot` restores the Android boot partition (lk2nd from step 1 -> original): needs BOOT_ORIG=<boot_orig_pre.bin> whose sha256 must be
#   8028815bc72771c8d50f730e265e698c32595f804b287955368e1c695b5efb71 (16 MiB, LBA 396384).
# Real write: COMMIT=1 CONFIRM=rollback-<phase>. Default dry. Never --lun, always --sectorsize=512.
. "$(dirname "$0")/fwlib.sh"
PH=${1:-}; MODE=${2:-dry}; TOKEN=rollback-$PH
[ "$MODE" = dry ] || [ "$MODE" = write ] || die "mode must be dry|write"
init
R=$PKG/rollback
BOOT_SHA=8028815bc72771c8d50f730e265e698c32595f804b287955368e1c695b5efb71
do_tz()    { put tz 270336 2048 "$R/stock-tz-tzbak.bin" "$MODE"; }   # tz at 270336 + tzbak at 271360 (= original 1024+1024)
do_rpm()   { put rpm 268288 1024 "$R/stock-rpm.bin" "$MODE"; }
do_sbl1()  { put sbl1 262144 1024 "$R/stock-sbl1.bin" "$MODE"; }
do_hyp()   { put hyp 272384 1024 "$R/stock-hyp.bin" "$MODE"; }
do_aboot() { put aboot 264192 2048 "$R/stock-aboot.bin" "$MODE"; }
do_boot()  { [ -n "${BOOT_ORIG:-}" ] && [ -f "$BOOT_ORIG" ] || die "BOOT_ORIG=<boot_orig_pre.bin> required"
             [ "$(sha "$BOOT_ORIG")" = "$BOOT_SHA" ] || die "BOOT_ORIG sha256 != $BOOT_SHA"
             put boot 396384 32768 "$BOOT_ORIG" "$MODE"; }
do_gpt()   { put gptS $SEC_LBA 33 "$R/gpt-orig-secondary.bin" "$MODE"; put gptP 0 34 "$R/gpt-orig-primary.bin" "$MODE"; }
case "$PH" in
state) echo "## read-only state"
    classify gptP 0 34 orig=$(sha "$R/gpt-orig-primary.bin") new=$(sha "$PKG/gpt/gpt-T-primary.bin")
    classify gptS $SEC_LBA 33 orig=$(sha "$R/gpt-orig-secondary.bin") new=$(sha "$PKG/gpt/gpt-T-secondary.bin")
    classify sbl1 262144 1024 stock=$(sha "$R/stock-sbl1.bin") new=$(sha "$PKG/fw/sbl1.img")
    classify rpm 268288 1024 stock=$(sha "$R/stock-rpm.bin") new=$(sha "$PKG/fw/rpm.img")
    classify tz 270336 2048 stock=$(sha "$R/stock-tz-tzbak.bin") new=$(sha "$PKG/fw/tz.img")
    classify hyp 272384 1024 stock=$(sha "$R/stock-hyp.bin") new=$(sha "$PKG/fw/hyp.img")
    classify aboot 264192 2048 stock=$(sha "$R/stock-aboot.bin") new=$(sha "$PKG/fw/aboot.img");;
tz) do_tz;; rpm) do_rpm;; sbl1) do_sbl1;; hyp) do_hyp;; aboot) do_aboot;; boot) do_boot;; gpt) do_gpt;;
all) do_tz; do_rpm; do_sbl1; do_hyp; do_aboot; [ -n "${BOOT_ORIG:-}" ] && do_boot; do_gpt
     [ "$MODE" = write ] && { echo "ROLLBACK COMPLETE (all read-backs passed). Reset with: $EDL reset --resetmode=reset --loader=\$LDR , then cold boot."; };;
*) echo "phases: state tz rpm sbl1 hyp aboot boot gpt all"; exit 2;;
esac
