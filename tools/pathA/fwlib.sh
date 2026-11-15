# SPDX-License-Identifier: MIT
# Shared helpers for pathA-flash.sh / pathA-rollback.sh (sourced). Never run on its own.
# Package layout (PKG): fw/ gpt/ rollback/ SHA256SUMS ; loader NOT in the package (LDR env, sha checked).
# Env: LDR (loader path), EDL_CMD (default: sudo .venv/bin/python edl/edl.py), W (work dir, outside any repo),
#      COMMIT=1 and CONFIRM=<phase token> for any real write (otherwise dry-run only).
set -eu
PKG=$(cd "$(dirname "$0")" && pwd)
W=${W:-$HOME/fwflash-work}
EDL=${EDL_CMD:-sudo .venv/bin/python edl/edl.py}
LDR_SHA=${FWLIB_TEST_LDR_SHA:-959439aa5864685999b713c3ed12ad5fa408149648b670a9a9ef77bcc9dcab14}
EMMC_SECTORS=7634944
SEC_LBA=$((EMMC_SECTORS - 33))
die() { echo "STOP: $*" >&2; exit 1; }
sha() { sha256sum "$1" | cut -d' ' -f1; }
init() {
    # no LDR given: fetch the pinned loader (sha256 verified by the fetch script, again below)
    [ -n "${LDR:-}" ] || LDR=$(sh "$PKG/../fetch-firehose.sh") || die "firehose fetch failed"
    [ -f "$LDR" ] || die "LDR=$LDR is not a file"
    [ "$(sha "$LDR")" = "$LDR_SHA" ] || die "loader sha256 is not the one proven on #6 ($LDR_SHA)"
    (cd "$PKG" && sha256sum -c --quiet SHA256SUMS) || die "package SHA256SUMS check failed"
    mkdir -p "$W"; chmod 700 "$W"
    COMMON="--loader=$LDR --memory=emmc"
}
# rs <lba> <sectors> <file>   (read only)
rs() { $EDL rs "$1" "$2" "$3" $COMMON >"$W/last-edl.log" 2>&1 || { tail -5 "$W/last-edl.log" >&2; die "rs $1 $2 failed"; }; [ "$(stat -c %s "$3")" -eq $(($2 * 512)) ] || die "rs $1 $2: short read"; }
# put <label> <lba> <sectors> <file> <mode dry|write> : dry-run, or write + read back + sha compare
put() {
    n=$1; lba=$2; sec=$3; f=$4; mode=$5
    [ -f "$f" ] || die "$f missing"
    [ "$(stat -c %s "$f")" -eq $((sec * 512)) ] || die "$n: $f is not exactly $sec sectors"
    want=$(sha "$f")
    echo "## $n: LBA $lba x $sec sectors, expected sha256 $want"
    $EDL ws "$lba" "$f" --sectorsize=512 --skipwrite $COMMON >"$W/last-edl.log" 2>&1 || { tail -5 "$W/last-edl.log" >&2; die "$n: dry-run failed"; }
    echo "   dry-run OK (nothing written)"
    [ "$mode" = write ] || return 0
    [ "${COMMIT:-}" = 1 ] || die "$n: COMMIT=1 required for a real write"
    [ "${CONFIRM:-}" = "$TOKEN" ] || die "$n: CONFIRM=$TOKEN required (token = owner's explicit yes for THIS phase)"
    $EDL ws "$lba" "$f" --sectorsize=512 $COMMON >"$W/last-edl.log" 2>&1 || { tail -5 "$W/last-edl.log" >&2; die "$n: write failed"; }
    rs "$lba" "$sec" "$W/after-$n.bin"
    got=$(sha "$W/after-$n.bin")
    [ "$got" = "$want" ] || die "$n: READ-BACK MISMATCH (got $got). Do not reset. Re-run this phase or rollback."
    echo "   WRITE+READBACK PASS $n sha256=$want"
}
# classify <label> <lba> <sectors> <name=sha>...  : read and say which known image is there
classify() {
    n=$1; lba=$2; sec=$3; shift 3
    rs "$lba" "$sec" "$W/pre-$n.bin"; h=$(sha "$W/pre-$n.bin"); r=UNKNOWN
    for kv in "$@"; do [ "${kv#*=}" = "$h" ] && r=${kv%%=*}; done
    echo "   $n @ $lba x $sec : $r (sha256 ${h%????????????????????????????????????????????????})"
    eval "ST_$n=$r"
}
