#!/bin/sh
# SPDX-License-Identifier: MIT
# Rootless: evaluates the sshd drop-ins of configs/sshd/ with the HOST sshd -T (-C user=...), prod and dev.
# Also checks the dev drop-in Match block does not leak into settings that follow the Include.
. "$(dirname "$0")/../scripts/common.sh"
SSHD=$(command -v sshd || echo /usr/sbin/sshd); [ -x "$SSHD" ] || die "sshd not found on host"
fail=0
T=$(mktemp -d "${TMPDIR:-/tmp}/verify-sshdcfg.XXXXXX"); trap 'rm -rf --one-file-system -- "${T:?}"' EXIT INT TERM
chk() { if eval "$2"; then echo "OK   $1"; else echo "FAIL $1"; fail=1; fi; }
ssh-keygen -q -t ed25519 -N '' -f "$T/hk"
TU=testuser
mkdir -p "$T/prod" "$T/dev"
cp "$ROOT/configs/sshd/10-openstick-hardening.prod.conf" "$T/prod/10-openstick-hardening.conf"
sed "s|@TEST_USER@|$TU|g" "$ROOT/configs/sshd/10-openstick-hardening.dev.conf.in" > "$T/dev/10-openstick-hardening.conf"
for d in prod dev; do printf 'Include %s/%s/*.conf\nMaxAuthTries 3\nUsePAM no\n' "$T" "$d" > "$T/$d.cfg"; done
# eff PROFILE USER KEY -> effective value (sshd -T prints lower-case keys)
eff() { "$SSHD" -T -f "$T/$1.cfg" -h "$T/hk" -C "user=$2,host=localhost,addr=127.0.0.1" 2>"$T/err" | awk -v k="$3" '$1==k{print $2; exit}'; }
for u in root "$TU" other; do
    chk "prod/$u: PasswordAuthentication no" "[ \"\$(eff prod $u passwordauthentication)\" = no ]"
    chk "prod/$u: KbdInteractiveAuthentication no" "[ \"\$(eff prod $u kbdinteractiveauthentication)\" = no ]"
    chk "prod/$u: PermitRootLogin prohibit-password" "[ \"\$(eff prod $u permitrootlogin)\" = prohibit-password ]"
    chk "prod/$u: PermitEmptyPasswords no" "[ \"\$(eff prod $u permitemptypasswords)\" = no ]"
done
chk "dev/$TU: PasswordAuthentication yes (test user only)" "[ \"\$(eff dev $TU passwordauthentication)\" = yes ]"
for u in root other; do
    chk "dev/$u: PasswordAuthentication no" "[ \"\$(eff dev $u passwordauthentication)\" = no ]"
done
for u in root "$TU" other; do
    chk "dev/$u: KbdInteractiveAuthentication no" "[ \"\$(eff dev $u kbdinteractiveauthentication)\" = no ]"
    chk "dev/$u: PermitRootLogin prohibit-password" "[ \"\$(eff dev $u permitrootlogin)\" = prohibit-password ]"
    chk "dev/$u: PermitEmptyPasswords no" "[ \"\$(eff dev $u permitemptypasswords)\" = no ]"
    chk "dev/$u: settings after the Include are not captured by Match (MaxAuthTries 3)" "[ \"\$(eff dev $u maxauthtries)\" = 3 ]"
done
chk "dev: no @ placeholder left" "! grep -q '@' $T/dev/10-openstick-hardening.conf"
exit $fail
