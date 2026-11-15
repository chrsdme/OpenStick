#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Print-only rollback plan for board #6: current state (Path A, GPT-T) -> ORIGINAL stock layout and content.
# Called by tools/rollback-plan.sh (run with python3 -I). It NEVER runs edl, dd, fastboot or sudo and never writes
# to a device or to any file. It only READS --dump, --csv, --primary, --secondary (and --compare FILE) to compute
# intervals and sha256 values, then PRINTS the commands. On any failed check it prints no command and exits 1.
import argparse, hashlib, struct, sys, zlib, csv, io

REAL_DUMP_SHA = "56f10639f7b1e2401b1a3f3994df948288c033d27a4e2a9d9975510a3c25d53b"
REAL_EMMC = 7634944
# restored (differ between the current state and the original dump), in WRITE ORDER (big data, firmware chain; GPT is added last)
RESTORE = ["userdata", "cache", "persist", "boot", "modemst1", "modemst2", "sbl1", "rpm", "tz", "tzbak", "hyp", "aboot"]
# identical in both dumps: never written
IDENTICAL = ["modem", "system", "recovery", "sbl1bak", "abootbak", "rpmbak", "hypbak", "pad", "misc", "fsc", "ssd",
             "splash", "DDR", "fsg", "sec"]
IDENTITY_GATE = ["fsg", "sec", "fsc"]          # device-unique, never written: proves WHICH board is attached
# independent evidence, only applied when the dump is the real one (sha == REAL_DUMP_SHA)
ANCHORS = {"boot": "8028815bc72771c8d50f730e265e698c32595f804b287955368e1c695b5efb71",
           "pad": "77285b6a0dd889a9886ccc10aff51e49e4972b30ec53ec16006a15539bd2400c",
           "aboot": "cdc99781dd35345f526c0b4d7ea2360e46f46ff6cd3dfce7bdce8b33a352d127",
           "hyp": "f3d43369c6d7a525c1c69e92db616e280e65a510d41271c42d86b7094274e5ae",
           "sbl1": "defd67447a97c4b7786cdc212e5b09dbb139981c5b51fab5fac604e8597d3a2a"}
GPT_P_SECT, GPT_S_SECT = 34, 33
CHUNK = 8 * 1024 * 1024


def fail(msg):
    errors.append(msg)


errors = []


def sha_range(path, start, count):
    h = hashlib.sha256(); left = count * 512
    with open(path, "rb") as f:
        f.seek(start * 512)
        while left > 0:
            b = f.read(min(CHUNK, left))
            if not b:
                raise IOError("short read in %s at sector %d" % (path, start))
            h.update(b); left -= len(b)
    return h.hexdigest()


def sha_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(CHUNK), b""):
            h.update(b)
    return h.hexdigest()


def read_csv(path):
    rows = []
    with open(path, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            rows.append(dict(name=r["name"], start=int(r["start_lba"]), end=int(r["end_lba"]),
                             offset=int(r["offset_bytes"]), size=int(r["size_bytes"])))
    return rows


def parse_gpt_entries(blob, hdr):
    sig, = struct.unpack_from("<8s", hdr, 0)
    if sig != b"EFI PART":
        raise ValueError("no EFI PART signature")
    n, esz, ecrc = struct.unpack_from("<III", hdr, 80)
    ent = blob[:n * esz]
    if len(ent) != n * esz:
        raise ValueError("entry array truncated")
    if zlib.crc32(ent) & 0xffffffff != ecrc:
        raise ValueError("entry array CRC mismatch")
    out = []
    for i in range(n):
        e = ent[i * esz:(i + 1) * esz]
        if e[:16] == b"\0" * 16:
            continue
        first, last = struct.unpack_from("<QQ", e, 32)
        out.append((e[56:128].decode("utf-16-le").rstrip("\0"), first, last))
    return out


def ranges_from(rows, emmc):
    """Every byte of the disk as (label, start, count): GPT, gaps, partitions."""
    rs, pos = [], 0
    rs.append(("GPT-primary", 0, GPT_P_SECT)); pos = GPT_P_SECT
    for r in sorted(rows, key=lambda r: r["start"]):
        if r["start"] > pos:
            rs.append(("gap@%d" % pos, pos, r["start"] - pos))
        rs.append((r["name"], r["start"], r["end"] - r["start"] + 1)); pos = r["end"] + 1
    sec = emmc - GPT_S_SECT
    if sec > pos:
        rs.append(("gap@%d" % pos, pos, sec - pos))
    rs.append(("GPT-secondary", sec, GPT_S_SECT))
    return rs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dump", required=True); ap.add_argument("--csv", required=True)
    ap.add_argument("--primary", required=True); ap.add_argument("--secondary", required=True)
    ap.add_argument("--work", default="$HOME/rollback-work"); ap.add_argument("--loader", default="<prog_emmc_firehose_8916.mbn>")
    ap.add_argument("--edl", default="sudo .venv/bin/python edl/edl.py")
    ap.add_argument("--emmc", type=int, default=REAL_EMMC); ap.add_argument("--dump-sha", default=REAL_DUMP_SHA)
    ap.add_argument("--compare", default=None, help="re-read disk image to compare range by range against --dump")
    a = ap.parse_args()
    emmc = a.emmc; disk_bytes = emmc * 512

    # ---------------- checks (nothing is printed as a command unless all pass) ----------------
    try:
        rows = read_csv(a.csv)
    except Exception as e:
        print("STOP: cannot parse CSV: %s" % e, file=sys.stderr); return 1
    by = {r["name"]: r for r in rows}
    names = set(by)
    if len(by) != len(rows):
        fail("duplicate partition names in CSV")
    if names != set(RESTORE) | set(IDENTICAL) or set(RESTORE) & set(IDENTICAL):
        fail("CSV names != RESTORE + IDENTICAL sets: missing=%s extra=%s" %
             (sorted((set(RESTORE) | set(IDENTICAL)) - names), sorted(names - (set(RESTORE) | set(IDENTICAL)))))
    prev_end = GPT_P_SECT - 1
    for r in sorted(rows, key=lambda r: r["start"]):
        cnt = r["end"] - r["start"] + 1
        if cnt <= 0 or cnt * 512 != r["size"] or r["offset"] != r["start"] * 512:
            fail("%s: interval inconsistent (start=%d end=%d offset=%d size=%d)" % (r["name"], r["start"], r["end"], r["offset"], r["size"]))
        if r["start"] <= prev_end:
            fail("%s: overlaps the previous range or the primary GPT" % r["name"])
        prev_end = max(prev_end, r["end"])
    if prev_end >= emmc - GPT_S_SECT:
        fail("last partition ends at %d, inside the secondary GPT (disk %d sectors)" % (prev_end, emmc))
    for p, want in ((a.primary, GPT_P_SECT * 512), (a.secondary, GPT_S_SECT * 512)):
        try:
            sz = len(open(p, "rb").read())
        except OSError as e:
            fail("cannot read %s: %s" % (p, e)); continue
        if sz != want:
            fail("%s is %d B, expected %d" % (p, sz, want))
    try:
        dsz = __import__("os").stat(a.dump).st_size
    except OSError as e:
        fail("dump missing: %s" % e); dsz = -1
    if dsz >= 0 and dsz != disk_bytes:
        fail("dump is %d B, expected %d (%d sectors)" % (dsz, disk_bytes, emmc))
    if errors:
        return finish()
    dump_sha = sha_file(a.dump)
    if dump_sha != a.dump_sha:
        fail("DUMP HASH MISMATCH: got %s expected %s" % (dump_sha, a.dump_sha))
    real = dump_sha == REAL_DUMP_SHA
    pri, sec_ = open(a.primary, "rb").read(), open(a.secondary, "rb").read()
    # GPT files: parse and compare with the CSV (independent source), and with the dump slices
    try:
        gp = parse_gpt_entries(pri[1024:], pri[512:1024])
        gs = parse_gpt_entries(sec_[:32 * 512], sec_[32 * 512:])
        if gp != gs:
            fail("primary and secondary GPT list different partitions")
        gpd = {n: (f, l) for n, f, l in gp}
        for r in rows:
            if gpd.get(r["name"]) != (r["start"], r["end"]):
                fail("%s: GPT file says %s, CSV says (%d, %d)" % (r["name"], gpd.get(r["name"]), r["start"], r["end"]))
        if set(gpd) != names:
            fail("GPT file partition names != CSV names")
    except Exception as e:
        fail("GPT file check failed: %s" % e)
    if sha_range(a.dump, 0, GPT_P_SECT) != hashlib.sha256(pri).hexdigest():
        fail("primary GPT file != dump LBA 0..%d" % (GPT_P_SECT - 1))
    if sha_range(a.dump, emmc - GPT_S_SECT, GPT_S_SECT) != hashlib.sha256(sec_).hexdigest():
        fail("secondary GPT file != dump LBA %d..%d" % (emmc - GPT_S_SECT, emmc - 1))
    sl = {}
    if not errors:
        for r in rows:
            sl[r["name"]] = sha_range(a.dump, r["start"], r["end"] - r["start"] + 1)
        if real:
            for n, h in ANCHORS.items():
                if sl[n] != h:
                    fail("anchor mismatch for %s: dump slice %s, independent record %s" % (n, sl[n], h))
    if errors:
        return finish()

    # ---------------- plan ----------------
    ph = hashlib.sha256(pri).hexdigest(); sh = hashlib.sha256(sec_).hexdigest()
    W = a.work
    cnt = lambda n: by[n]["end"] - by[n]["start"] + 1
    total = sum(cnt(n) for n in RESTORE) + GPT_P_SECT + GPT_S_SECT
    p = print
    p("# ROLLBACK PLAN board #6: Path A / GPT-T  ->  ORIGINAL stock. PRINT ONLY: nothing below was executed.")
    p("# Tags: [VALIDAT pe #6] = proven on this board; [NEEXERSAT] = never run on this board; [CALCULAT] = derived here from the dump/CSV (read-only).")
    p("# Every real write needs the owner's explicit 'da, flashez' for THAT phase (CLAUDE.md rule 1). No sudo was used to produce this text.")
    p("#")
    p("# ===== file checks (all passed, otherwise no commands would be printed) =====")
    p("# dump     : %d B, sha256 %s  [%s]" % (dsz, dump_sha, "equals the verified 2-machine hash" if real else "NON-DEFAULT dump (test fixture): anchors skipped"))
    p("# primary  : %s  (34 sectors, == dump LBA 0..33, entries parsed: match CSV)" % ph)
    p("# secondary: %s  (33 sectors, == dump LBA %d..%d, entries parsed: match CSV)" % (sh, emmc - GPT_S_SECT, emmc - 1))
    p("# anchors  : %s" % ("aboot, hyp, sbl1, boot, pad slices equal independent hashes (SHA256SUMS / FLASH-PLAN)" if real else "skipped"))
    p("# restore  : %d partitions + 2 GPT = %d sectors = %.1f MiB; identical (NOT written): %s" % (len(RESTORE), total, total * 512 / 1048576.0, ", ".join(IDENTICAL)))
    p("#")
    p("EDL=%r" % a.edl); p("LDR=%s" % a.loader); p("DUMP=%s" % a.dump); p("WORK=%s" % W)
    p("# Slices hold device-unique data (persist, modemst*): private dir, outside the repo, never uploaded. Needs ~%.1f GiB free." % (total * 512 / 2.0**30))
    p('install -d -m 700 "$WORK" && df -h "$WORK"')
    p("")
    p("# ===== PHASE 0a: read-only identity + state [VALIDAT pe #6: rs; classify via tools/pathA/pathA-rollback.sh state] =====")
    p("# Expectation: GPT is NOT the original (GPT-T) and the firmware regions are 'new' (Path A). Any UNKNOWN: STOP.")
    p("LDR=$LDR tools/pathA/pathA-rollback.sh state")
    p("# Identity gate: fsg, sec, fsc are unique per device, identical in both dumps, never written. They must equal the dump of THIS board.")
    for n in IDENTITY_GATE:
        r = by[n]; c = cnt(n)
        p('$EDL rs %d %d "$WORK/id-%s.bin" --loader="$LDR" --memory=emmc' % (r["start"], c, n))
        p('echo "%s  $WORK/id-%s.bin" | sha256sum -c -   # %s, LBA %d..%d [CALCULAT]' % (sl[n], n, n, r["start"], r["end"]))
    p("")
    p("# ===== PHASE 0b (Gate 0): no-op write on 'pad' (identical in both dumps) proves the write path before any real change =====")
    r = by["pad"]; c = cnt("pad")
    p('$EDL rs %d %d "$WORK/pad-before.bin" --loader="$LDR" --memory=emmc' % (r["start"], c))
    p('echo "%s  $WORK/pad-before.bin" | sha256sum -c -   # MUST be OK, else STOP [CALCULAT]' % sl["pad"])
    p('$EDL ws %d "$WORK/pad-before.bin" --sectorsize=512 --skipwrite --loader="$LDR" --memory=emmc   # dry-run' % r["start"])
    p('$EDL ws %d "$WORK/pad-before.bin" --sectorsize=512 --loader="$LDR" --memory=emmc' % r["start"])
    p('$EDL rs %d %d "$WORK/pad-after.bin" --loader="$LDR" --memory=emmc' % (r["start"], c))
    p('echo "%s  $WORK/pad-after.bin" | sha256sum -c - && cmp "$WORK/pad-before.bin" "$WORK/pad-after.bin" && echo NOOP_OK' % sl["pad"])
    p("# ws of 1 MiB with --sectorsize=512, --skipwrite first, rs read-back: syntax identical to put() in tools/pathA/fwlib.sh [VALIDAT pe #6 only if the Path A flash ran with this fwlib.sh: no log on this machine; ~/fwflash is absent here].")
    p("# STOP unless NOOP_OK. Do not continue without the owner's 'da, flashez'.")
    p("")
    p("# ===== PHASE 1: restore, one row at a time. Order: big data -> firmware chain -> GPT last (reasons at the end) =====")
    p("# Per row: slice (dd bs=512 skip count) + hash of the slice, dry-run, write, read back, hash compare. Rows are idempotent and independently resumable.")

    def row(tag, label, start, count, src, h, note):
        p("")
        p("## %s %s: LBA %d..%d, %d sectors = %d B  %s" % (tag, label, start, start + count - 1, count, count * 512, note))
        if src is None:
            p('dd if="$DUMP" of="$WORK/rb-%s.bin" bs=512 skip=%d count=%d status=none' % (label, start, count))
            f = '"$WORK/rb-%s.bin"' % label
            p('echo "%s  $WORK/rb-%s.bin" | sha256sum -c -   # slice hash computed from the dump [CALCULAT]' % (h, label))
        else:
            f = src
            p('echo "%s  %s" | sha256sum -c -   # file == dump slice [CALCULAT]' % (h, src))
        p("$EDL ws %d %s --sectorsize=512 --skipwrite --loader=\"$LDR\" --memory=emmc   # dry-run" % (start, f))
        p("$EDL ws %d %s --sectorsize=512 --loader=\"$LDR\" --memory=emmc" % (start, f))
        p('$EDL rs %d %d "$WORK/rbk-%s.bin" --loader="$LDR" --memory=emmc' % (start, count, label))
        p('echo "%s  $WORK/rbk-%s.bin" | sha256sum -c -   # read-back must equal the slice, else STOP and re-run this row' % (h, label))

    big = {"userdata", "cache", "persist", "boot"}
    for i, n in enumerate(RESTORE, 1):
        r = by[n]
        note = "[NEEXERSAT: large ws; the 'payload too large' firehose warning also appears on successful reads, so it is not evidence of failure; per-row order: --skipwrite, write, rs, sha compare]" if cnt(n) > 2048 else "[VALIDAT pe #6 per Path A state (same put() as fwlib.sh, <=2048 sectors); no log on this machine]"
        row("R%02d" % i, n, r["start"], cnt(n), None, sl[n], note)
    row("G1", "gpt-secondary", emmc - GPT_S_SECT, GPT_S_SECT, a.secondary, sh, "(secondary first, old primary stays valid until the last write) [VALIDAT pe #6 per Path A state: pathA-flash.sh gpt phase; no log here]")
    row("G2", "gpt-primary", 0, GPT_P_SECT, a.primary, ph, "(LAST write: the table switches only after all content is verified) [VALIDAT pe #6 per Path A state: pathA-flash.sh gpt phase; no log here]")
    p("")
    p("# Order, and why: (1) userdata, cache, persist, boot, modemst1/2 first: the largest and slowest writes, the ones most likely to be interrupted;")
    p("#   while they run, the Path A firmware chain and the current GPT-T are untouched, the board stays in EDL and a re-run costs nothing.")
    p("# (2) sbl1, rpm, tz, tzbak, hyp, aboot next: short writes (<=1 MiB), done back to back to keep the 'mixed chain' window small; do not reset in the middle.")
    p("#   If power is lost inside this window the board may not reach EDL by itself: use the reset-button + USB EDL entry that pathA-rollback.sh documents [NEEXERSAT in this state].")
    p("# (3) GPT last, secondary then primary, as in pathA-flash.sh: the 'address book' changes only after all content passed read-back.")

    p("")
    p("# ===== PHASE 2: full-disk verification BEFORE the first Android boot =====")
    p('$EDL rf "$WORK/final.bin" --loader="$LDR" --memory=emmc   # [VALIDAT pe #6: rf of the whole disk, rc=0, 3909091328 B, ~5-6 min; a firehose "payload too large" warning also appears on successful reads]')
    p('test "$(stat -c %%s "$WORK/final.bin")" = %d || echo "STOP: short read"' % disk_bytes)
    p('sha256sum "$WORK/final.bin"   # MUST be exactly %s' % a.dump_sha)
    p("# If the hash differs: do NOT boot Android, do NOT reset. Locate the difference (partitions AND gaps):")
    p('python3 -I tools/rollback-plan.py --dump "$DUMP" --csv <csv> --primary %s --secondary %s --compare "$WORK/final.bin"   # prints MATCH/DIFF per range and the repair commands for the DIFF ranges' % (a.primary, a.secondary))
    p("#   (or: tools/rollback-plan.sh with the same arguments plus --compare). Repair only the DIFF ranges (same dd/dry-run/ws/rs/sha rows), then repeat this phase.")
    p("#   A DIFF in a gap or in an 'identical' partition means the premise (identical) was wrong: report it, do not write further than the printed repair rows.")
    p("# After a PASS: reset, then cold boot (plug/unplug), then check lsusb / the original Android boot [NEEXERSAT].")
    p("")
    p("# ===== PHASE 3: cleanup (slices contain device-unique data) =====")
    p('rm -f "$WORK"/rb-*.bin "$WORK"/rbk-*.bin "$WORK"/id-*.bin "$WORK"/pad-*.bin "$WORK"/final.bin')
    p("")
    p("# ===== REVERSE CYCLE: back to Path A + builder images, then confirm Debian boots [NEEXERSAT: never run end to end] =====")
    p("# 1. EDL, then: LDR=... tools/pathA/pathA-flash.sh pre    (dies unless GPT == original: this doubles as proof that the rollback landed)")
    p("# 2. for each phase gpt tz rpm sbl1 hyp aboot: dry first, then COMMIT=1 CONFIRM=da-flashez-<phase> tools/pathA/pathA-flash.sh <phase> write")
    p("# 3. tools/pathA/pathA-flash.sh final (state must say 'new' everywhere), then the reset it performs")
    p("# 4. builder images (boot + rootfs) with: BOARD=ufi103s BACKUP_DIR=<outside repo> LDR=... tools/flash/flash-board.sh first-install dry, then write (COMMIT=1 CONFIRM=flash-ufi103s-first-install)")
    p("#    (flash-board.sh is marked 'PROPOSAL, NOT EXECUTED' in its own header)")
    p("# 5. cold boot (plug/unplug); expect lk1st/lk2nd in fastboot (fastboot getvar product), then Debian: lsusb, ssh/ping as in README. Pass = login prompt/ssh works.")
    p("# Never mix modemst1, modemst2, fsg, fsc, persist, sec between boards (CLAUDE.md rule 4).")
    p("# Nothing above was executed.")

    if a.compare:
        return compare(a, rows, emmc, sl)
    return 0


def compare(a, rows, emmc, sl):
    import os
    print("")
    print("# ===== COMPARE %s vs dump (read-only) =====" % a.compare)
    if os.stat(a.compare).st_size != emmc * 512:
        print("STOP: %s is not %d B" % (a.compare, emmc * 512), file=sys.stderr); return 1
    bad = []
    for label, s, c in ranges_from(rows, emmc):
        ok = sha_range(a.dump, s, c) == sha_range(a.compare, s, c)
        print("# %-14s LBA %d..%d (%d sectors): %s" % (label, s, s + c - 1, c, "MATCH" if ok else "DIFF"))
        if not ok:
            bad.append((label, s, c))
    for label, s, c in bad:
        h = sha_range(a.dump, s, c); lab = label.replace("@", "-")
        print("dd if=\"$DUMP\" of=\"$WORK/rb-%s.bin\" bs=512 skip=%d count=%d status=none" % (lab, s, c))
        print('echo "%s  $WORK/rb-%s.bin" | sha256sum -c -' % (h, lab))
        print('$EDL ws %d "$WORK/rb-%s.bin" --sectorsize=512 --skipwrite --loader="$LDR" --memory=emmc   # dry-run' % (s, lab))
        print('$EDL ws %d "$WORK/rb-%s.bin" --sectorsize=512 --loader="$LDR" --memory=emmc' % (s, lab))
        print('$EDL rs %d %d "$WORK/rbk-%s.bin" --loader="$LDR" --memory=emmc' % (s, c, lab))
        print('echo "%s  $WORK/rbk-%s.bin" | sha256sum -c -' % (h, lab))
    print("# %d range(s) differ" % len(bad))
    return 0 if not bad else 3


def finish():
    for e in errors:
        print("STOP: %s" % e, file=sys.stderr)
    print("# STOP: %d check(s) failed, NO commands printed (see stderr)" % len(errors))
    return 1


if __name__ == "__main__":
    sys.exit(main())
