#!/bin/bash
# SPDX-License-Identifier: MIT
# Firewall checks. Rootless: ruleset text from `openstick-ssh-cellular print`. With root: the kernel part runs in a
# throw-away network namespace (unshare -n), host firewall untouched: table loads (policy drop), foreign
# (NetworkManager-like) table survives, on/off toggles work. Needs the host `nft` binary.
. "$(dirname "$0")/../scripts/common.sh"
TOOL=$ROOT/configs/openstick-ssh-cellular
fail=0
chk() { if eval "$2"; then echo "OK   $1"; else echo "FAIL $1"; fail=1; fi; }
# --- rootless part: the generated ruleset text (the tool's `print` action), no nft/kernel involved
T=$(mktemp -d "${TMPDIR:-/tmp}/verify-fw.XXXXXX"); trap 'rm -rf --one-file-system -- "${T:?}"' EXIT INT TERM
mkdir -p "$T/nm"; : > "$T/conf"
rs() { # SSH_ON_CELLULAR hotspot(0|1) -> ruleset on stdout
    rm -f "$T/nm/hotspot.nmconnection"; [ "$2" != 1 ] || : > "$T/nm/hotspot.nmconnection"
    sed "s|^CONF=.*|CONF=$T/conf|" "$TOOL" > "$T/tool.sh"; printf 'SSH_ON_CELLULAR=%s\n' "$1" > "$T/conf"
    NM_DIR=$T/nm sh "$T/tool.sh" print
}
rs 0 0 > "$T/r00"; rs 1 0 > "$T/r10"; rs 0 1 > "$T/r01"
chk "input policy is drop" "grep -q 'hook input priority filter; policy drop' $T/r00"
chk "loopback, established/related, icmp, icmpv6 accepted" "grep -q 'iifname \"lo\" accept' $T/r00 && grep -q 'ct state established,related accept' $T/r00 && grep -q 'l4proto icmp accept' $T/r00 && grep -q 'l4proto icmpv6 accept' $T/r00"
chk "DHCP client (v4 and v6) accepted" "grep -q 'udp sport 67 udp dport 68 accept' $T/r00 && grep -q 'udp sport 547 udp dport 546 accept' $T/r00"
chk "ssh only on usb0 and wlan0" "grep -q 'iifname { \"usb0\", \"wlan0\" } tcp dport 22 accept' $T/r00"
chk "no wwan rule when cellular ssh is off" "! grep -q wwan $T/r00"
chk "no wwan rule on the ssh-allowing rules (nothing broader than usb0/wlan0)" "! grep 'dport 22' $T/r00 | grep -v 'usb0.*wlan0'"
chk "switch on: wwan* tcp/22 accept added, rest unchanged" "grep -q 'iifname \"wwan\*\" tcp dport 22 accept' $T/r10 && diff <(grep -v wwan $T/r10) $T/r00 >/dev/null"
chk "no hotspot: no wlan0 dns/dhcp rules" "! grep -q 'iifname \"wlan0\" udp' $T/r00"
chk "hotspot installed: wlan0 dns/dhcp rules" "grep -q 'iifname \"wlan0\" udp dport { 53, 67 } accept' $T/r01 && grep -q 'iifname \"wlan0\" tcp dport 53 accept' $T/r01"
chk "usb0 (NM shared) dns/dhcp rules" "grep -q 'iifname \"usb0\" udp dport { 53, 67 } accept' $T/r00"
chk "no 'flush ruleset', only our table is flushed" "! grep -q 'flush ruleset' $T/r00 && grep -qx 'flush table inet openstick_fw' $T/r00"
chk "tool is valid sh" "sh -n $TOOL"
command -v nft >/dev/null || die "nft not found on host"
[ "$(id -u)" -eq 0 ] || { echo "SKIP kernel part (netns test) needs root: sudo $0"; exit $fail; }
unshare -n sh -eu -c '
TOOL=$1
CONF=$(mktemp); trap "rm -f $CONF" EXIT
sed "s|^CONF=.*|CONF=$CONF|" "$TOOL" > "$CONF.sh"; trap "rm -f $CONF $CONF.sh" EXIT
nft add table ip nm-shared-usb0
nft add chain ip nm-shared-usb0 pr "{ type nat hook postrouting priority 100; }"
echo "SSH_ON_CELLULAR=0" > "$CONF"; sh "$CONF.sh" apply; sh "$CONF.sh" apply   # idempotent
nft list table inet openstick_fw
nft list table inet openstick_fw | grep -q "policy drop" && echo "OK: policy drop loaded" || { echo "FAIL: no policy drop"; exit 1; }
nft list table inet openstick_fw | grep -q "wwan" && { echo "FAIL: wwan rule while blocked"; exit 1; } || echo "OK: no wwan accept while blocked"
nft list table ip nm-shared-usb0 >/dev/null && echo "OK: foreign table preserved"
echo "SSH_ON_CELLULAR=1" > "$CONF"; sh "$CONF.sh" apply
nft list table inet openstick_fw | grep -q "wwan.*tcp dport 22 accept\|tcp dport 22 accept.*wwan" && echo "OK: wwan ssh accepted when switched on" || { echo "FAIL: no wwan accept when on"; exit 1; }
nft list table inet openstick_fw | grep -q "policy drop" && echo "OK: policy drop kept when on" || { echo "FAIL: policy lost"; exit 1; }
nft list table ip nm-shared-usb0 >/dev/null && echo "OK: foreign table preserved"
' sh "$TOOL"
exit $fail
