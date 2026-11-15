#!/bin/sh
# SPDX-License-Identifier: MIT
# Fetch the EDL firehose programmer prog_emmc_firehose_8916.mbn from a URL pinned to a commit SHA,
# verify the full sha256, install to $OUT/prog_emmc_firehose_8916.mbn (mode 644). Never in git (*.mbn is ignored).
# The file is a vendor-signed Qualcomm loader; its redistribution rights are unverified (docs/INCLUDED.md).
# usage: tools/fetch-firehose.sh      (prints the installed path on stdout; messages go to stderr)
# env:   OUT (default: <builder>/out)
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
OUT=${OUT:-$HERE/../out}
NAME=prog_emmc_firehose_8916.mbn
WANT=959439aa5864685999b713c3ed12ad5fa408149648b670a9a9ef77bcc9dcab14
# Both URLs are pinned to a commit SHA and served byte-identical content (checked by sha256).
URLS="https://raw.githubusercontent.com/Basistiy/openstick-installer/93952e39ad2d9824907484567ae7a342c07e5979/loaders/$NAME
https://raw.githubusercontent.com/nikicat/4g-modem/38f895a576e33b2eba2d5fa88c276b9095f59bb9/$NAME"
die() { echo "STOP: $*" >&2; exit 1; }
command -v sha256sum >/dev/null || die "missing sha256sum"
command -v curl >/dev/null || die "missing curl"
mkdir -p "$OUT"
DEST=$OUT/$NAME
if [ -f "$DEST" ] && [ "$(sha256sum < "$DEST" | cut -d' ' -f1)" = "$WANT" ]; then
    echo "present with correct sha256: $DEST" >&2; echo "$DEST"; exit 0
fi
TMP=$(mktemp -d)
trap 'rm -rf --one-file-system -- "${TMP:?}"' EXIT INT TERM
ok=0
for u in $URLS; do
    rm -f "$TMP/dl"
    echo "fetching $u" >&2
    curl -fsSL --proto '=https' --tlsv1.2 --max-time 120 -o "$TMP/dl" "$u" || { echo "download failed" >&2; continue; }
    got=$(sha256sum < "$TMP/dl" | cut -d' ' -f1)
    if [ "$got" = "$WANT" ]; then ok=1; break; fi
    echo "sha256 mismatch from $u: got $got" >&2
done
[ "$ok" = 1 ] || die "no source served $WANT; refusing to install"
install -m 644 "$TMP/dl" "$DEST"
echo "installed $DEST (sha256 $WANT)" >&2
echo "$DEST"
