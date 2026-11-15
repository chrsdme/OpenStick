#!/bin/sh
# SPDX-License-Identifier: MIT
# Tests tools/rollback-plan.sh (print-only rollback plan, board #6, Path A -> original). Rootless, never runs edl.
# Uses a small synthetic fixture (27 partitions, 1000-sector "disk", valid GPT files) built in a temp dir.
# Cases: positive control, wrong dump hash, wrong dump content, wrong interval (CSV), GPT/CSV disagreement,
#        wrong dump size, missing dump, --compare (identical / DIFF in a gap / DIFF in a partition).
# Optional real-data check (read-only, ~5 s): REAL_BACKUP=<dir with the 6-Alb-... dump/csv> REAL_GPT_DIR=<dir with gpt-6-*.bin>
set -eu
D=$(cd "$(dirname "$0")/.." && pwd); S=$D/tools/rollback-plan.sh
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT; fail=0
chk() { if eval "$2"; then echo "OK   $1"; else echo "FAIL $1"; fail=1; fi; }

python3 -I - "$T" <<'PY'
import os, struct, zlib, hashlib, sys
t = sys.argv[1]
names = ["modem","sbl1","sbl1bak","aboot","abootbak","rpm","rpmbak","tz","tzbak","hyp","hypbak","pad","modemst1","modemst2",
         "misc","fsc","ssd","splash","DDR","fsg","sec","boot","system","persist","cache","recovery","userdata"]
EMMC = 1000
rows = []; ents = b""
for i, n in enumerate(names):
    s = 64 + i * 8; e = s + 3
    rows.append((i + 1, n, s, e)); 
    ents += b"\0" * 0
    ent = bytearray(128); ent[0:16] = b"\x01" * 16; ent[16:32] = bytes([i + 1]) * 16
    struct.pack_into("<QQ", ent, 32, s, e); ent[56:56 + 2 * len(n)] = n.encode("utf-16-le"); ents += bytes(ent)
ents += b"\0" * (128 * 128 - len(ents))
hdr = bytearray(512); hdr[0:8] = b"EFI PART"; struct.pack_into("<III", hdr, 80, 128, 128, zlib.crc32(ents) & 0xffffffff)
prim = b"\0" * 512 + bytes(hdr) + ents; sec = ents + bytes(hdr)
assert len(prim) == 34 * 512 and len(sec) == 33 * 512
disk = bytearray(os.urandom(EMMC * 512)); disk[0:len(prim)] = prim; disk[(EMMC - 33) * 512:] = sec
open(t + "/dump.bin", "wb").write(disk); open(t + "/p.bin", "wb").write(prim); open(t + "/s.bin", "wb").write(sec)
open(t + "/dump.sha", "w").write(hashlib.sha256(disk).hexdigest())
with open(t + "/parts.csv", "w", encoding="utf-8-sig") as f:
    f.write("number,name,start_lba,end_lba,offset_bytes,size_bytes,size_mib,signature\n")
    for n_, n, s, e in rows:
        f.write("%d,%s,%d,%d,%d,%d,0.0,raw\n" % (n_, n, s, e, s * 512, (e - s + 1) * 512))
PY
SHA=$(cat "$T/dump.sha")
run() { sh "$S" --dump "${DUMP:-$T/dump.bin}" --csv "${CSV:-$T/parts.csv}" --primary "${PRI:-$T/p.bin}" --secondary "$T/s.bin" --emmc "${EMMC:-1000}" --dump-sha "${DSHA:-$SHA}" "$@"; }
wslines() { grep -c -E '^\$EDL ws ' "$1" || true; }

# ---- positive control ----
run >"$T/ok.out" 2>"$T/ok.err"; rc=$?
chk "control: exit 0" "[ $rc -eq 0 ]"
chk "control: every ws line has --sectorsize=512" "! grep -E '^\\\$EDL ws ' $T/ok.out | grep -v -- '--sectorsize=512' | grep -q ."
chk "control: no --lun anywhere in a command" "! grep -v '^#' $T/ok.out | grep -q -- '--lun'"
chk "control: 15 dry-runs and 15 real writes (pad + 12 partitions + 2 GPT)" "[ \$(grep -c -E '^\\\$EDL ws .*--skipwrite' $T/ok.out) -eq 15 ] && [ \$(grep -E '^\\\$EDL ws ' $T/ok.out | grep -vc -- '--skipwrite') -eq 15 ]"
chk "control: each real ws is immediately preceded by its dry-run on the same LBA/file" "python3 -I -c \"
import re,sys
L=[l for l in open('$T/ok.out') if l.startswith('\\\$EDL ws ')]
L=[l.split('#')[0].split() for l in L]
ok=len(L)%2==0 and all([w for w in L[i] if w!='--skipwrite']==L[i+1] for i in range(0,len(L),2)) and all('--skipwrite' in L[i] and '--skipwrite' not in L[i+1] for i in range(0,len(L),2))
sys.exit(0 if ok else 1)\""
chk "control: pad is the first write, GPT primary (LBA 0) the last" "[ \$(grep -E '^\\\$EDL ws ' $T/ok.out | head -1 | awk '{print \$3}') -eq $((64 + 11 * 8)) ] && grep -E '^\\\$EDL ws ' $T/ok.out | tail -1 | grep -q '^\\\$EDL ws 0 '"
chk "control: GPT secondary is written before the primary" "[ \$(grep -n 'ws 967 ' $T/ok.out | head -1 | cut -d: -f1) -lt \$(grep -n 'ws 0 ' $T/ok.out | head -1 | cut -d: -f1) ]"
chk "control: intervals come from the CSV (userdata row: LBA 272..275, 4 sectors)" "grep -q 'R01 userdata: LBA 272..275, 4 sectors' $T/ok.out"
chk "control: restore order userdata,cache,persist,boot,modemst1,modemst2,sbl1,rpm,tz,tzbak,hyp,aboot" "[ \"\$(grep -E '^## R' $T/ok.out | sed -E 's/^## R[0-9]+ ([a-z0-9]+):.*/\\1/' | tr '\\n' ,)\" = userdata,cache,persist,boot,modemst1,modemst2,sbl1,rpm,tz,tzbak,hyp,aboot, ]"
chk "control: never prints dd of a non-restored partition (system, recovery, modem)" "! grep -E '^dd ' $T/ok.out | grep -q -E 'rb-(system|recovery|modem|pad|misc)\\.bin'"
chk "control: final full read uses rf, not rs 0" "grep -q 'EDL rf \"\$WORK/final.bin\"' $T/ok.out && ! grep -q 'EDL rs .*final.bin' $T/ok.out"
chk "control: final verification demands the dump hash" "grep -q 'MUST be exactly $SHA' $T/ok.out"

# ---- failure cases: non-zero exit AND zero ws/dd lines ----
nocmd() { [ "$1" -ne 0 ] && [ "$(grep -c -E '^(\$EDL ws|dd ) ' "$2" || true)" -eq 0 ]; }
DSHA=0000000000000000000000000000000000000000000000000000000000000000 run >"$T/f1.out" 2>"$T/f1.err" && rc=0 || rc=$?
chk "wrong hash (expected sha not the dump's): exit != 0, no commands, message names the mismatch" "nocmd $rc $T/f1.out && grep -q 'DUMP HASH MISMATCH' $T/f1.err"
cp "$T/dump.bin" "$T/dump-bad.bin"; printf '\377' | dd of="$T/dump-bad.bin" bs=1 seek=$((100 * 512 + 7)) conv=notrunc status=none
DUMP=$T/dump-bad.bin run >"$T/f2.out" 2>"$T/f2.err" && rc=0 || rc=$?
chk "wrong hash (one corrupted byte in the dump): exit != 0, no commands" "nocmd $rc $T/f2.out && grep -q 'DUMP HASH MISMATCH' $T/f2.err"
sed 's/^22,boot,\([0-9]*\),\([0-9]*\),/22,boot,\1,999,/' "$T/parts.csv" > "$T/bad-interval.csv"
CSV=$T/bad-interval.csv run >"$T/f3.out" 2>"$T/f3.err" && rc=0 || rc=$?
chk "wrong interval (boot end_lba altered, size/offset not): exit != 0, no commands" "nocmd $rc $T/f3.out && grep -q 'boot: interval inconsistent' $T/f3.err"
python3 -I - "$T" <<'PY'
import sys,re
t=sys.argv[1]; s=open(t+"/parts.csv",encoding="utf-8-sig").read()
# consistent but WRONG interval: shift userdata by 8 sectors (start/end/offset all consistent), so only the GPT cross-check can notice
s=re.sub(r"^(27,userdata,)(\d+),(\d+),(\d+),", lambda m:"%s%d,%d,%d,"%(m.group(1),int(m.group(2))+8,int(m.group(3))+8,(int(m.group(2))+8)*512), s, flags=re.M)
open(t+"/shift.csv","w",encoding="utf-8-sig").write(s)
PY
CSV=$T/shift.csv run >"$T/f4.out" 2>"$T/f4.err" && rc=0 || rc=$?
chk "wrong interval (self-consistent shift, caught by the GPT cross-check): exit != 0, no commands" "nocmd $rc $T/f4.out && grep -q 'userdata: GPT file says' $T/f4.err"
head -c $((1000 * 512 - 512)) "$T/dump.bin" > "$T/dump-short.bin"
DUMP=$T/dump-short.bin run >"$T/f5.out" 2>"$T/f5.err" && rc=0 || rc=$?
chk "wrong dump size: exit != 0, no commands" "nocmd $rc $T/f5.out && grep -q 'dump is' $T/f5.err"
DUMP=$T/does-not-exist.bin run >"$T/f6.out" 2>"$T/f6.err" && rc=0 || rc=$?
chk "dump missing: exit != 0, no commands" "nocmd $rc $T/f6.out && grep -q 'dump missing' $T/f6.err"
printf 'x' >> "$T/p.bin"; run >"$T/f7.out" 2>"$T/f7.err" && rc=0 || rc=$?
chk "primary GPT file with the wrong size: exit != 0, no commands" "nocmd $rc $T/f7.out"
head -c 17408 "$T/p.bin" > "$T/p2.bin"; mv "$T/p2.bin" "$T/p.bin"
EMMC=1001 run >"$T/f8.out" 2>"$T/f8.err" && rc=0 || rc=$?
chk "wrong disk size parameter (--emmc 1001): exit != 0, no commands" "nocmd $rc $T/f8.out"

# ---- --compare ----
run --compare "$T/dump.bin" >"$T/c1.out" 2>&1 && rc=0 || rc=$?
chk "compare identical image: exit 0, 0 ranges differ" "[ $rc -eq 0 ] && grep -q '# 0 range(s) differ' $T/c1.out"
cp "$T/dump.bin" "$T/re-gap.bin"; printf '\1' | dd of="$T/re-gap.bin" bs=1 seek=$((40 * 512)) conv=notrunc status=none
run --compare "$T/re-gap.bin" >"$T/c2.out" 2>&1 && rc=0 || rc=$?
chk "compare: byte changed in a GAP (LBA 40) is reported (partitions alone would all match)" "[ $rc -eq 3 ] && grep -q 'gap@34 .*DIFF' $T/c2.out && grep -q 'ws 34 .*--sectorsize=512' $T/c2.out"
cp "$T/dump.bin" "$T/re-part.bin"; printf '\1' | dd of="$T/re-part.bin" bs=1 seek=$((272 * 512 + 3)) conv=notrunc status=none
run --compare "$T/re-part.bin" >"$T/c3.out" 2>&1 && rc=0 || rc=$?
chk "compare: byte changed inside userdata is reported with its repair row" "[ $rc -eq 3 ] && grep -q 'userdata .*DIFF' $T/c3.out && grep -q 'ws 272 .*--sectorsize=512' $T/c3.out && ! grep -q 'cache .*DIFF' $T/c3.out"

# ---- optional: the real board #6 files (read-only) ----
RB=${REAL_BACKUP:-$HOME/openstick/6-Alb-Stick-4313d69f-backup}; RG=${REAL_GPT_DIR:-$HOME/openstick}
R=$RB/6-Alb-Stick-4313d69f-emmc-user
if [ -f "$R.bin" ] && [ -f "$R-partitions.csv" ] && [ -f "$RG/gpt-6-primary.bin" ] && [ -f "$RG/gpt-6-secondary.bin" ]; then
    sh "$S" --dump "$R.bin" --csv "$R-partitions.csv" --primary "$RG/gpt-6-primary.bin" --secondary "$RG/gpt-6-secondary.bin" >"$T/real.out" 2>"$T/real.err" && rc=0 || rc=$?
    chk "REAL #6 data: exit 0 (size, sha 56f10639, GPT==CSV==dump, anchors)" "[ $rc -eq 0 ] && grep -q 'anchors  : aboot, hyp, sbl1, boot, pad' $T/real.out"
    chk "REAL #6 data: userdata 2428000..7634910, boot 396384..429151, pad hash 77285b6a" "grep -q 'R01 userdata: LBA 2428000..7634910' $T/real.out && grep -q 'R04 boot: LBA 396384..429151' $T/real.out && grep -q '77285b6a0dd889a9886ccc10aff51e49e4972b30ec53ec16006a15539bd2400c  \$WORK/pad-before.bin' $T/real.out"
    chk "REAL #6 data: every ws has --sectorsize=512" "! grep -E '^\\\$EDL ws ' $T/real.out | grep -v -- '--sectorsize=512' | grep -q ."
else echo "SKIP real-data checks (backup files not found)"; fi
exit $fail
