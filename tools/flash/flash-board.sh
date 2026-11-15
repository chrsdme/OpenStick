#!/bin/sh
# SPDX-License-Identifier: MIT
# PROPOSAL, NOT EXECUTED, NOT TESTED. Flash helper for one board. Dry-run by default.
#   update        fastboot (lk2nd running): flash boot + rootfs from out/files-<BOARD>/ . Needs the GPT-B layout live.
#   boot          CURRENT path (board #6): flash out/prod-boot/boot.img. Reads `fastboot getvar product` and branches:
#                   lk2nd-msm8916 -> `fastboot flash boot boot.img` (lk2nd applies the 512 KiB offset itself)
#                   lk1st-msm8916 -> NEVER a plain boot.img (it erases lk2nd, which lives in the first 512 KiB of the physical
#                                    boot partition). Composes lk2nd-padded-512KiB.img + boot.img at 0x80000, checks it fits
#                                    in 0x1000000, writes it, reads it back with `fastboot fetch` and compares.
#                   anything else -> refuse.
#   first-install EDL: prints/dry-runs the EDL commands for boot + rootfs (bootloader/GPT steps stay in tools/pathA/pathA-flash.sh).
# usage: BOARD=ufi103s BACKUP_DIR=<outside repo> [LDR=<firehose>] flash-board.sh <update|first-install> [dry|write]
# A write needs: COMMIT=1 CONFIRM=flash-<BOARD>-<mode>  (owner's explicit yes for THIS board and mode; project rules 1 and 2).
OPENSTICK_ROOT=$(cd "$(dirname "$0")/../.." && pwd); export OPENSTICK_ROOT
. "$OPENSTICK_ROOT/scripts/common.sh"
MODE=${1:-}; RUN=${2:-dry}; TOKEN=flash-$BOARD-$MODE
case "$MODE" in update|first-install|boot) ;; *) die "usage: $0 <update|first-install|boot> [dry|write]";; esac
case "$RUN" in dry|write) ;; *) die "run mode must be dry|write";; esac
FILES=$OUT/files-$BOARD; LAYOUT=$OUT/fw/layout-$BOARD.env
[ "$MODE" = boot ] && FILES=${PROD_BOOT_DIR:-$OUT/prod-boot}
[ "$MODE" = boot ] || { [ -f "$LAYOUT" ] && . "$LAYOUT" || die "missing $LAYOUT"; }
need() { command -v "$1" >/dev/null || die "missing tool $1"; }; need sha256sum

# 1) gates that never depend on the device
[ -n "${BACKUP_DIR:-}" ] && [ -f "$BACKUP_DIR/SHA256SUMS" ] || die "BACKUP_DIR with a SHA256SUMS of the board's own dump is required (rule 1)"
case "$BACKUP_DIR" in "$ROOT"/*) die "BACKUP_DIR must be outside the repo (rules 4, 6)";; esac
( cd "$BACKUP_DIR" && sha256sum -c --quiet SHA256SUMS ) || die "backup does not verify"
# rmtfs/msm-firmware-loader are enabled in the image: first boot WRITES modemst1/2, fsc, fsg (rule 4). Require them in the backup.
for p in modemst1 modemst2 fsc fsg persist sec; do ls "$BACKUP_DIR"/$p* >/dev/null 2>&1 || die "backup lacks $p"; done
( cd "$FILES" && sha256sum -c --quiet SHA256SUMS ) || die "image SHA256SUMS check failed"
[ "$RUN" = dry ] || { [ "${COMMIT:-}" = 1 ] && [ "${CONFIRM:-}" = "$TOKEN" ] || die "write needs COMMIT=1 CONFIRM=$TOKEN"; }

case "$MODE" in
boot)
    need fastboot; need stat; need cmp; need dd; need truncate
    IMG=$FILES/boot.img; [ -f "$IMG" ] || die "missing $IMG (run scripts/50-boot-img.sh)"
    LK=${LK2ND_IMG:-$OUT/step1/lk2nd-padded-512KiB.img}   # same source and pin as scripts/46-boot-lk2nd.sh, never the user's backup
    LK_SHA256=${LK2ND_PADDED_SHA256:-d82f3535972869deb2f963ac33a33ea2cd01cbf1cbd661a4bcc23f491fbcbfde}
    LK_OFF=$((0x80000)); LK1ST_BOOT=$((0x1000000)); LK2ND_BOOT=$((0xf80000))
    [ "$(fastboot devices | wc -l)" = 1 ] || die "exactly one fastboot device required"
    prod=$(fastboot getvar product 2>&1 | sed -n 's/^product: *//p')
    psz=$(fastboot getvar partition-size:boot 2>&1 | sed -n 's/^partition-size:boot: *//p')
    [ -n "$psz" ] || die "cannot read partition-size:boot"
    isz=$(stat -c %s "$IMG")
    echo "device: product=$prod partition-size:boot=$psz boot.img=$isz B"
    case "$prod" in
    lk2nd-msm8916)
        [ $((psz)) -eq "$LK2ND_BOOT" ] || die "unexpected partition-size:boot $psz for lk2nd (expected 0xf80000)"
        [ "$isz" -le "$LK2ND_BOOT" ] || die "boot.img ($isz B) does not fit in $LK2ND_BOOT B"
        got=$(fastboot getvar lk2nd:compatible 2>&1 | sed -n 's/^lk2nd:compatible: *//p')   # [ASSUMED variable name]
        [ "$got" = "$LK2ND_COMPATIBLE" ] || die "board identity mismatch: device '$got', expected '$LK2ND_COMPATIBLE' (also read the PCB silkscreen, rule 2)"
        echo "would run: fastboot flash boot $IMG   (lk2nd applies the 512 KiB offset)"
        [ "$RUN" = write ] && { fastboot flash boot "$IMG" || die "flash failed"; echo "flashed. Reboot by hand, then verify over ssh."; } || true;;
    lk1st-msm8916)
        # Physical boot partition: [0,512 KiB) is lk2nd. A plain boot.img here would overwrite it.
        # validated on hardware (board #6): `fastboot fetch boot` returns the physical partition and partition-size:boot = 0x1000000 on lk1st.
        [ $((psz)) -eq "$LK1ST_BOOT" ] || die "unexpected partition-size:boot $psz for lk1st (expected 0x1000000)"
        [ -f "$LK" ] || die "missing $LK"
        echo "$LK_SHA256  $LK" | sha256sum -c - >/dev/null || die "lk2nd image sha256 mismatch (set LK2ND_PADDED_SHA256 if rebuilt on purpose)"
        [ "$(stat -c %s "$LK")" -eq "$LK_OFF" ] || die "lk2nd image must be exactly 512 KiB"
        total=$((LK_OFF + isz))
        [ "$total" -le "$LK1ST_BOOT" ] || die "lk2nd + boot.img ($total B) does not fit in $LK1ST_BOOT B"
        echo "lk1st: identity (lk2nd:compatible) is NOT readable here; confirm the PCB silkscreen by eye (rule 2)"
        W=$(mktemp -d "${TMPDIR:-/tmp}/flash-board.XXXXXX"); chmod 700 "$W"; trap 'rm -rf -- "$W"' EXIT INT TERM
        dd if="$LK" of="$W/composed.img" bs=4096 status=none
        dd if="$IMG" of="$W/composed.img" bs=4096 seek=$((LK_OFF / 4096)) conv=notrunc status=none
        [ "$(stat -c %s "$W/composed.img")" -eq "$total" ] || die "composed image has wrong size"
        cmp -n "$LK_OFF" "$LK" "$W/composed.img" || die "lk2nd part of composed image differs"
        # lk2nd head vs the board's own backup boot (owner used the backup head on #6). Warning only: never a failure.
        BB=${BACKUP_BOOT:-}
        [ -n "$BB" ] || for c in "$BACKUP_DIR"/boot.img "$BACKUP_DIR"/boot.bin "$BACKUP_DIR"/boot.raw "$BACKUP_DIR"/boot*.img "$BACKUP_DIR"/boot*.bin; do [ -f "$c" ] && { BB=$c; break; }; done
        cs=$(head -c "$LK_OFF" "$W/composed.img" | sha256sum | cut -d' ' -f1)
        if [ -n "$BB" ] && [ -f "$BB" ] && [ "$(stat -c %s "$BB")" -ge "$LK_OFF" ]; then
            bs=$(head -c "$LK_OFF" "$BB" | sha256sum | cut -d' ' -f1)
            if [ "$cs" = "$bs" ]; then echo "composite head (512 KiB) == backup boot head: $cs"
            else echo "WARNING: composite head sha256 $cs != backup boot head sha256 $bs ($BB). Not fatal: confirm the lk2nd pin on purpose." >&2; fi
        else echo "WARNING: no usable backup boot image in BACKUP_DIR (set BACKUP_BOOT); composite head sha256 $cs not compared" >&2; fi
        echo "composed: $total B = lk2nd 512 KiB + boot.img at 0x80000; sha256 $(sha256sum < "$W/composed.img" | cut -d' ' -f1)"
        echo "would run: fastboot flash boot $W/composed.img ; fastboot fetch boot <file> ; compare first $total B"
        if [ "$RUN" = write ]; then
            fastboot flash boot "$W/composed.img" || die "flash failed"
            fastboot fetch boot "$W/rb.img" || die "fastboot fetch failed (read-back impossible: treat the write as UNVERIFIED)"   # validated on hardware (board #6): fetch returns the physical partition
            [ "$(stat -c %s "$W/rb.img")" -ge "$total" ] || die "read-back shorter than the written data"
            cmp -n "$total" "$W/composed.img" "$W/rb.img" || die "READ-BACK MISMATCH boot"
            echo "flashed and read-back verified ($total B). Reboot by hand, then verify over ssh."
        fi;;
    *) die "refusing: unknown fastboot product '$prod' (expected lk2nd-msm8916 or lk1st-msm8916)";;
    esac;;
update)
    need fastboot
    [ "$(fastboot devices | wc -l)" = 1 ] || die "exactly one fastboot device required"
    # 2) identity: lk2nd compatible string must match board.env. Variable name lk2nd:compatible is [ASSUMED], check with `fastboot getvar all`.
    got=$(fastboot getvar lk2nd:compatible 2>&1 | sed -n 's/^lk2nd:compatible: *//p')
    [ "$got" = "$LK2ND_COMPATIBLE" ] || die "board identity mismatch: device '$got', expected '$LK2ND_COMPATIBLE' (also read the PCB silkscreen, rule 2)"
    # 3) live layout must be the new one: partition names AND sizes (the stick may still have the ORIGINAL GPT with root on userdata) [ASSUMED getvar names]
    for p in boot rootfs; do
        sz=$(fastboot getvar partition-size:$p 2>&1 | sed -n "s/^partition-size:$p: *//p")
        [ -n "$sz" ] || die "partition '$p' not found: the GPT-B layout is not live; use first-install (EDL) instead"
    done
    [ $((sz)) -ge "$ROOTFS_BYTES" ] || die "rootfs partition smaller than the image"
    echo "would run: fastboot flash boot   $FILES/boot.bin"
    echo "would run: fastboot flash rootfs $FILES/rootfs.bin"
    if [ "$RUN" = write ]; then
        fastboot flash boot "$FILES/boot.bin"
        fastboot flash rootfs "$FILES/rootfs.bin"
        echo "flashed. Reboot by hand (fastboot reboot), then verify over ssh."
    fi;;
first-install)
    # bootloader + GPT first, with the existing phased tool (own COMMIT/CONFIRM tokens per phase):
    echo "run first: tools/pathA/pathA-flash.sh pre|gpt|tz|rpm|sbl1|hyp|aboot|final  (one EDL session, see FLASH-PLAN.md)"
    need simg2img; [ -n "${LDR:-}" ] || LDR=$(sh "$(dirname "$0")/../fetch-firehose.sh") || die "firehose fetch failed"
    EDLC=${EDL_CMD:-sudo .venv/bin/python edl/edl.py}; W=${W:-$HOME/fwflash-work}; mkdir -p "$W"; chmod 700 "$W"
    simg2img "$FILES/boot.bin" "$W/boot.raw"; simg2img "$FILES/rootfs.bin" "$W/rootfs.raw"   # EDL needs RAW [ASSUMED]
    for x in "boot $BOOT_START $W/boot.raw" "rootfs $ROOTFS_START $W/rootfs.raw"; do
        set -- $x
        echo "## $1 at LBA $2"; $EDLC ws "$2" "$3" --sectorsize=512 --skipwrite --loader="$LDR" --memory=emmc || die "dry-run failed for $1"
        if [ "$RUN" = write ]; then
            $EDLC ws "$2" "$3" --sectorsize=512 --loader="$LDR" --memory=emmc || die "write failed for $1"
            n=$(( $(stat -c %s "$3") / 512 ))
            $EDLC rs "$2" "$n" "$W/rb-$1.bin" --loader="$LDR" --memory=emmc
            [ "$(sha256sum < "$3")" = "$(sha256sum < "$W/rb-$1.bin")" ] || die "READ-BACK MISMATCH $1"
        fi
    done;;
esac
