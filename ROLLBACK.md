# ROLLBACK: board #6, from the CURRENT state (Path A, GPT-T) back to the original stock

Replaces the old GPT-B document (that flow is obsolete). Tags: **[VALIDAT pe #6]** proven on this board, **[NEEXERSAT]** never run on this board,
**[CALCULAT]** derived read-only from the dump/CSV by `tools/rollback-plan.sh`.
Nothing here was executed on the board. `tools/rollback-plan.sh` only PRINTS commands; it never runs edl, dd, fastboot or sudo.

## Sources (outside the repo, never uploaded)

| What | Where |
|---|---|
| Full original dump (3909091328 B) | `~/openstick/6-Alb-Stick-4313d69f-backup/6-Alb-Stick-4313d69f-emmc-user.bin`, sha256 `56f10639f7b1e2401b1a3f3994df948288c033d27a4e2a9d9975510a3c25d53b` (confirmed on 2 machines and in the Windows record) |
| Partition table of that dump | `.../6-Alb-Stick-4313d69f-emmc-user-partitions.csv` (UTF-8 with BOM; `end_lba` inclusive) |
| Original GPTs | `~/openstick/gpt-6-primary.bin` (34 sectors, sha256 `34c8a2d9...ace04ea`), `gpt-6-secondary.bin` (33 sectors, `91538825...c2861a5`) |

## EDL sessions

The first command that talks to the board after entering EDL reads the Sahara identity. Every command that talks to the board consumes that session. Before another board command, unplug USB for 30 seconds, reconnect without holding the button, and re-enter EDL. Never use `edl reset`.

```sh
tools/rollback-plan.sh --dump <dump.bin> --csv <partitions.csv> --primary <gpt-6-primary.bin> --secondary <gpt-6-secondary.bin> \
    [--work <private dir>] [--loader <prog_emmc_firehose_8916.mbn>] [--compare <full re-read.bin>]
```
Checks before printing anything (any failure: no command is printed, exit 1): dump size and sha256; CSV rows consistent (`count*512 == size_bytes`,
`offset == start*512`, no overlap, inside the disk); the 27 CSV names equal RESTORE (12) + IDENTICAL (15); both GPT files parse, their entries equal the CSV,
and they equal the dump slices; for the real dump, the aboot/hyp/sbl1/boot/pad slice hashes equal the independent records (`SHA256SUMS`, FLASH-PLAN).
All intervals are computed from the CSV, none is typed by hand. A run on the real files takes about 5 s and exits 0 [CALCULAT].

## What is restored (measured by the owner: EDL dump of the current state vs the original dump, on the ORIGINAL partitions)

Restored: GPT primary + secondary, `sbl1`, `aboot`, `rpm`, `tz`, `tzbak`, `hyp`, `modemst1`, `modemst2`, `boot`, `persist`, `cache`, `userdata`.
Identical, never written: `modem`, `system`, `recovery`, `sbl1bak`, `abootbak`, `rpmbak`, `hypbak`, `pad`, `misc`, `fsc`, `ssd`, `splash`, `DDR`, `fsg`, `sec`, and the gaps between partitions.
(The measurement file `~/openstick-private/6-state/diff-vs-original.txt` is NOT on the machine where this was written; the lists above come from the task text.
The script asserts RESTORE and IDENTICAL together equal the CSV partition set, and the final full read (phase 2) is the real check of the "identical" claim.)

| Row | Partition | LBA (from CSV) | Sectors | Bytes |
|---|---|---|---|---|
| R01 | userdata | 2428000..7634910 | 5206911 | 2665938432 |
| R02 | cache | 2133088..2395231 | 262144 | 134217728 |
| R03 | persist | 2067552..2133087 | 65536 | 33554432 |
| R04 | boot | 396384..429151 | 32768 | 16777216 |
| R05 | modemst1 | 276480..279551 | 3072 | 1572864 |
| R06 | modemst2 | 279552..282623 | 3072 | 1572864 |
| R07 | sbl1 | 262144..263167 | 1024 | 524288 |
| R08 | rpm | 268288..269311 | 1024 | 524288 |
| R09 | tz | 270336..271359 | 1024 | 524288 |
| R10 | tzbak | 271360..272383 | 1024 | 524288 |
| R11 | hyp | 272384..273407 | 1024 | 524288 |
| R12 | aboot | 264192..266239 | 2048 | 1048576 |
| G1 | GPT secondary | 7634911..7634943 | 33 | 16896 |
| G2 | GPT primary | 0..33 | 34 | 17408 |

Total about 2725 MiB. `abootbak` is not a copy of `aboot` on #6: never restore `aboot` from `abootbak`.

## Write syntax

`$EDL ws <lba> <file> --sectorsize=512 [--skipwrite] --loader=$LDR --memory=emmc`, read-back `$EDL rs <lba> <sectors> <file> --loader=$LDR --memory=emmc`.
Same as `put()` in `tools/pathA/fwlib.sh` (the package the Path A flash used). Never `--lun`.
- `rs`: [VALIDAT pe #6].
- `ws` <= 2048 sectors with `--skipwrite` first and rs read-back: [VALIDAT pe #6] to the extent that the Path A flash of #6 ran with this fwlib.sh;
  I could not confirm it: `~/fwflash/` and `~/fwflash-work/` do not exist on the machine where this was written (it is on the laptop). If the laptop scripts used
  a different syntax, use theirs and tell me.
- `ws` of more than 2048 sectors (userdata, cache, persist, boot, modemst1/2): [NEEXERSAT]. For each of these rows the plan runs `--skipwrite`, then the write, then `rs` and a hash compare, row by row.
- Whole-disk read: `$EDL rf <file> --loader=$LDR --memory=emmc` ([VALIDAT pe #6], run from `~/2-ALB-backup-0` on the laptop: rc=0, 3909091328 B, about 5-6 min). The firehose "payload too large" warning also appears on successful reads.

## Phases (every real write needs the owner's "da, flashez" for that phase)

**Phase 0a, read-only identity and state.** `tools/pathA/pathA-rollback.sh state` (expect GPT-T and "new" firmware; any UNKNOWN: STOP) [VALIDAT pe #6 as to rs/classify].
Identity gate: `rs` of `fsg`, `sec`, `fsc` (unique per device, identical in both dumps, never written) must equal the dump slices. This proves the attached board is #6 without trusting adb or serial numbers.

**Phase 0b, Gate 0: no-op write on `pad`** (identical in both dumps; LBA 274432, 2048 sectors). Read `pad`, compare with the dump slice hash (`77285b6a...2400c`), dry-run, write the same bytes, read back, hash and `cmp`.
Proves the write path before any real change. STOP unless `NOOP_OK`.

**Phase 1, restore rows R01..R12, G1, G2.** Per row the script prints: slice (`dd if=<dump> bs=512 skip=<start> count=<n>`), the slice sha256 [CALCULAT], dry-run (`--skipwrite`), real write, read-back, sha256 compare.
The GPT rows use the original GPT files directly (checked equal to the dump slices). Rows are idempotent and resumable: after a failure, repeat only that row.
Order and why:
1. Big data first (userdata, cache, persist, boot, modemst1/2): the slowest writes, most likely to be interrupted. While they run, the Path A firmware chain and GPT-T are untouched, the board stays in EDL and a re-run costs nothing.
2. Firmware chain (sbl1, rpm, tz, tzbak, hyp, aboot): short writes done back to back so the stock/Path A "mixed chain" window is minimal. Do not reset in the middle.
   If power is lost inside it the board may not reach EDL by itself; the reset-button + USB EDL entry (documented in `pathA-rollback.sh`) is the way back [NEEXERSAT in that state].
3. GPT last, secondary then primary (as in `pathA-flash.sh`): the table switches only after all content passed read-back, and the old primary stays valid until the very last write.

**Phase 2, full verification BEFORE the first Android boot.** Read the whole disk with `rf` (3909091328 B; check the size), `sha256sum` must be exactly `56f10639f7b1e2401b1a3f3994df948288c033d27a4e2a9d9975510a3c25d53b`.
If it differs: do NOT boot Android and do NOT reset. Run the same script with `--compare <final.bin>`: it compares range by range, partitions AND the gaps (LBA 0..33 table, 34..131071, 305170..393215, the tail before the secondary GPT),
prints MATCH/DIFF per range and the dd/dry-run/ws/rs/sha rows to repair only the DIFF ranges (exit 3 when something differs). Repair, then repeat the full read.
A DIFF in a gap or in an "identical" partition means the premise was wrong: report it instead of writing further. Then reset and cold boot (plug/unplug) [NEEXERSAT].

Caveat: the earlier probe dump of this board (`6-edl-emmc-user.sha256`, `33944e82...`) has a different hash from the verified dump. The exact-hash gate assumes the board returns to byte-identical content; if some region is volatile
(for example a counter inside an "identical" partition), `--compare` will show which one.

**Phase 3, cleanup.** The slices contain device-unique data (`persist`, `modemst*`): the work dir is created mode 700 outside the repo, needs about 2.7 GiB free, and the plan ends with an `rm`.

## Reverse cycle: back to Path A + builder images [NEEXERSAT end to end]

1. EDL, `tools/pathA/pathA-flash.sh pre` (dies unless the GPT equals the original: this doubles as proof that the rollback landed).
2. Phases `gpt tz rpm sbl1 hyp aboot`: dry first, then `COMMIT=1 CONFIRM=da-flashez-<phase> ... write`.
3. Phase `final` (all regions must read "new"), which also performs the reset.
4. Builder boot + rootfs: `tools/flash/flash-board.sh first-install dry`, then write (its own header says "PROPOSAL, NOT EXECUTED").
5. Cold boot; `fastboot getvar product` shows the lk1st/lk2nd bootloader, then Debian answers (`lsusb`, ssh/ping). That is the pass condition.

Never mix `modemst1`, `modemst2`, `fsg`, `fsc`, `persist`, `sec` between boards (CLAUDE.md rule 4).

## Tests

`tests/verify-rollback-plan.sh`: synthetic fixture (no board, no root) with a positive control (exit 0, every `ws` has `--sectorsize=512`, no `--lun`, each real write preceded by its `--skipwrite`, pad first, GPT last, order),
failure cases (wrong expected hash, corrupted dump byte, wrong CSV interval, self-consistent shifted interval caught by the GPT cross-check, wrong dump size, missing dump, bad GPT size, bad `--emmc`) that must exit non-zero with zero `ws`/`dd` lines,
`--compare` cases (identical, byte changed in a gap, byte changed in a partition), and a read-only run on the real #6 files when present.
