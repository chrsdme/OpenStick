#!/bin/bash
# SPDX-License-Identifier: MIT
# Validates the ruleset of configs/openstick-ssh-cellular against nftables 1.0.6 (Debian 12), the version on the stick.
# Why this exists: tests/verify-firewall.sh runs the HOST nft (e.g. 1.1.6) with the HOST /etc/protocols. The image is
# debootstrap minbase: no netbase, so no /etc/protocols, and nft 1.0.6 only knows its builtin protocol names ("icmpv6").
# `meta l4proto ipv6-icmp` therefore passed on the host and failed on the stick ("Could not resolve protocol name").
# Tier 1 (rootless, offline, always runs): every `l4proto` name must be a builtin of the target libnftables (cross-checked
#   against out/rootfs-*.tgz when present), plus a mutation check that proves the lint FAILS on the old `ipv6-icmp` variant.
# Tier 2 (needs docker/podman + network): real `nft -c -f -` on Debian 12 nft 1.0.6 installed WITHOUT recommends (no netbase).
#   REQUIRE_NFT_CHECK=1 turns a skipped tier 2 into a failure.
. "$(dirname "$0")/../scripts/common.sh"
TOOL=${NFT_TOOL:-$ROOT/configs/openstick-ssh-cellular}
fail=0
chk() { if eval "$2"; then echo "OK   $1"; else echo "FAIL $1"; fail=1; fi; }
T=$(mktemp -d "${TMPDIR:-/tmp}/verify-nft.XXXXXX"); trap 'rm -rf --one-file-system -- "${T:?}"' EXIT INT TERM
mkdir -p "$T/nm"
rs() { # tool SSH_ON_CELLULAR hotspot(0|1) -> ruleset on stdout
    rm -f "$T/nm/hotspot.nmconnection"; [ "$3" != 1 ] || : > "$T/nm/hotspot.nmconnection"
    sed "s|^CONF=.*|CONF=$T/conf|" "$1" > "$T/tool.sh"; printf 'SSH_ON_CELLULAR=%s\n' "$2" > "$T/conf"
    NM_DIR=$T/nm sh "$T/tool.sh" print
}
# builtin protocol names of nft 1.0.6 (checked against the target libnftables below when a rootfs tarball exists)
BUILTIN="icmp icmpv6 tcp udp udplite sctp dccp ah esp comp igmp gre"
names() { for v in "0 0" "1 0" "0 1" "1 1"; do rs "$1" $v; done | sed -n 's/.*meta l4proto \([A-Za-z0-9_-]*\).*/\1/p' | sort -u; }
unknown() { local n; for n in $(names "$1"); do case " $BUILTIN " in *" $n "*) ;; *) echo "$n";; esac; done; }
chk "tool is valid sh" "sh -n $TOOL"
chk "ruleset has l4proto rules (lint is not vacuous)" "[ -n \"\$(names $TOOL)\" ]"
chk "every l4proto name is an nft 1.0.6 builtin (no /etc/protocols dependency)" "[ -z \"\$(unknown $TOOL)\" ]"
# mutation: the pre-fix variant must be rejected
sed 's/meta l4proto icmpv6 accept/meta l4proto ipv6-icmp accept/' "$TOOL" > "$T/old-variant"
chk "MUTATION: old 'ipv6-icmp' variant is rejected by the lint" "[ \"\$(unknown $T/old-variant)\" = ipv6-icmp ]"
# cross-check the allow-list against the real target library, when a built rootfs tarball is around
TGZ=$(ls "$OUT"/rootfs-*.tgz 2>/dev/null | head -n1 || true)
if [ -n "$TGZ" ]; then
    LIB=$(tar tzf "$TGZ" | grep -m1 'libnftables\.so\.1\.[0-9.]*$' || true)
    if [ -n "$LIB" ]; then
        tar xzOf "$TGZ" "$LIB" > "$T/libnft"
        chk "target libnftables knows every name used (builtin strings)" "(for n in \$(names $TOOL); do strings -a $T/libnft | grep -qx -- \$n || exit 1; done)"
        chk "target libnftables does NOT know 'ipv6-icmp' (confirms the failure cause)" "! strings -a $T/libnft | grep -qx ipv6-icmp"
    fi
    chk "target rootfs has no /etc/protocols (no fallback resolution)" "! tar tzf $TGZ | grep -qx '\./etc/protocols'"
else echo "NOTE no out/rootfs-*.tgz: library cross-check skipped"; fi
# --- tier 2: real nft 1.0.6
ENG=; for e in docker podman; do command -v $e >/dev/null && $e info >/dev/null 2>&1 && { ENG=$e; break; }; done
if [ -z "$ENG" ]; then
    echo "SKIP tier 2 (real nft 1.0.6 -c): no working docker/podman"; [ "${REQUIRE_NFT_CHECK:-0}" != 1 ] || fail=1
else
    for v in "0 0" "1 0" "0 1" "1 1"; do rs "$TOOL" $v > "$T/rs.$$.${v// /}"; done
    for f in "$T"/rs.$$.*; do
        out=$(timeout 300 $ENG run --rm -i --cap-add NET_ADMIN debian:12 sh -c \
          'apt-get -qq update >/dev/null && apt-get -qq install -y --no-install-recommends nftables >/dev/null && nft --version >&2 && [ ! -e /etc/protocols ] && nft -c -f -' < "$f" 2>&1) \
          && echo "OK   nft 1.0.6 accepts ${f##*.} ($(echo "$out" | grep -m1 -o 'nftables v[0-9.]*'))" || { echo "FAIL nft 1.0.6 rejects ${f##*.}: $out"; fail=1; }
    done
fi
exit $fail
