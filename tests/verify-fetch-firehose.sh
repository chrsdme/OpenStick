#!/bin/sh
# SPDX-License-Identifier: MIT
# Offline test of the hash-mismatch branch of tools/fetch-firehose.sh: a stub `curl` serves wrong bytes for every URL.
# Expected: exit 1, "sha256 mismatch" printed for each URL, "refusing to install", nothing installed, a pre-existing
# wrong file at the destination is neither accepted as "present" nor overwritten. No network, no root.
set -eu
D=$(cd "$(dirname "$0")/.." && pwd); T=$(mktemp -d); trap 'rm -rf --one-file-system -- "${T:?}"' EXIT INT TERM
fail=0; chk() { if eval "$2"; then echo "OK   $1"; else echo "FAIL $1"; fail=1; fi; }
mkdir -p "$T/bin" "$T/out"
cat > "$T/bin/curl" <<'STUB'
#!/bin/sh
echo "$*" >> "$STUB_LOG"
while [ $# -gt 0 ]; do [ "$1" = -o ] && { printf 'tampered firehose bytes\n' > "$2"; exit 0; }; shift; done
exit 22
STUB
chmod +x "$T/bin/curl"
printf 'stale wrong file\n' > "$T/out/prog_emmc_firehose_8916.mbn"
BEFORE=$(sha256sum < "$T/out/prog_emmc_firehose_8916.mbn")
rc=0; STUB_LOG=$T/curl.log PATH="$T/bin:$PATH" OUT=$T/out sh "$D/tools/fetch-firehose.sh" >"$T/stdout" 2>"$T/stderr" || rc=$?
chk "exit status is 1" "[ $rc -eq 1 ]"
chk "both pinned URLs were tried" "[ \$(wc -l < $T/curl.log) -eq 2 ]"
chk "sha256 mismatch reported for both URLs" "[ \$(grep -c 'sha256 mismatch' $T/stderr) -eq 2 ]"
chk "refuses to install" "grep -q 'refusing to install' $T/stderr"
chk "nothing printed on stdout (no path handed to callers)" "[ ! -s $T/stdout ]"
chk "pre-existing wrong file untouched" "[ \"\$(sha256sum < $T/out/prog_emmc_firehose_8916.mbn)\" = \"$BEFORE\" ]"
rm -f "$T/out/prog_emmc_firehose_8916.mbn"; : > "$T/curl.log"
rc=0; STUB_LOG=$T/curl.log PATH="$T/bin:$PATH" OUT=$T/out sh "$D/tools/fetch-firehose.sh" >/dev/null 2>&1 || rc=$?
chk "fresh run: fails and installs nothing" "[ $rc -eq 1 ] && [ ! -e $T/out/prog_emmc_firehose_8916.mbn ]"
exit $fail
