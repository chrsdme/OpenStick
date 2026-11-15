# Historical FLASH-PLAN: board #6 GPT-B proposal (superseded)

> **Historical and superseded. Do not use this GPT-B plan for current install or restore.** `ROLLBACK.md` identifies the current documented rollback context as GPT-T and describes restoring the original stock layout. This file is retained as historical GPT-B design evidence only. It is not an install plan, not a restore plan and not hardware authorization.

Status of this document's original work: DOCUMENTATION + GENERATED ARTIFACTS ONLY. It records no device write by that work. This workspace has not repeated its claimed owner hardware checks.
Legend: [VERIFIED] = checked on files or on hardware (EDL read-only) , [ASSUMED] = not verified, check before use.
Unique GUIDs, disk GUID and dumps are never written in this repo.

## 0. Verdict

- Historical owner decision recorded here: **GPT-B** (keep every original entry except the Android-only ones; boot 64 MiB, rootfs 2.62 GiB). It is superseded for the documented GPT-T rollback path.
- `scripts/gen-gpt.sh` is now template based (section 2). The old hardcoded "stock-like" layout (`gpt_both0.bin` from the
  first version) was a BLOCKER and is retired (`out/obsolete/`, never flash it).
- `out/files-ufi103s/` is now the CANONICAL build (root-owned, built with `sudo scripts/40-images.sh` on the fixed sources, layout GPT-B).
  The earlier stale tree (rootfs 3.47 GiB, old layout) was replaced in place; its old hashes (`dc056c9a...`, `2fff13c5...`) are OBSOLETE.
- `out/files-ufi103s-rootless/` is OBSOLETE (built from the defective tarball, section 9): never flash it. Safe to delete (`rm -rf out/files-ufi103s-rootless`).
- Canonical rebuild verified (owner report, 2026-11-13): `tests/verify-rootfs.sh` and `tests/verify-images.sh` (as run then, including a
  mount-based part B as root; that mount part no longer exists in the current rootless `tests/verify-images.sh`) finished with 0 FAIL. Hashes of the images were re-computed here and match the owner report (section 4).
  NOT re-run by this session: the logs `out/verify-*.log` and `out/40-images.log` in `out/` are from 05:58 (previous build) and do NOT describe the current images.
- New minimal route (owner request): section 5c (step 0, no-op write on `pad`) and section 5d (step 1, lk2nd as boot image in the ORIGINAL `boot` partition,
  original GPT unchanged, `aboot`/`hyp` untouched). Details and the lk2nd build: `out/raport-step1.md`.
- 2026-11-13 update (step 1b, owner inputs, no device access here): EDL write syntax taken from `edl.py --help` (section 6), `--sectorsize=512` on every write, step 0 on `pad` with expected sha256, step 1 with the expected `boot` sha256, secure boot read result (section 1), G0 split into G0a/G0b (section 5). Report: `out/raport-step1b.md`.
- No flash until: the owner says "da, flashez" per step. Canonical images are built and `tests/verify-images.sh` passed as root (see above); G0a gates step 1 (section 5d), G0b gates A/B (section 5).

## 1. Verified facts (board #6, EDL read-only, per owner)

- Loader `prog_emmc_firehose_8916.mbn` sha256 `959439aa5864685999b713c3ed12ad5fa408149648b670a9a9ef77bcc9dcab14` accepted [VERIFIED].
- MSM8916, PK_HASH `cc3153a80293939b90d02d3bf8b23e0292e452fef662c74998421adad42a380f`.
- Secure boot [READ-ONLY RESULT on #6, owner, `edl secureboot` with `prog_emmc_firehose_8916.mbn`]: Sec_Boot0..3 OEM_PKHash False, Auth_Enabled False, "Secure boot disabled."
  Status: secure boot DISABLED per QFPROM read (INFERENCE: depends on the loader's memory-read support for the fuse registers; not "unverified" any more, but not proven by a boot of an unsigned image either).
  The QFPROM dump (`qfp-6.bin`) stays on the owner's laptop only: not in the repo, not read by this session.
- The GPT of #6 has no `keystore`, `devinfo`, `oem` or `config` partitions: hint that the stock `aboot` does not do verified boot / lock-state checks (INFERENCE, not verified).
- Disk 7634944 sectors = 3909091328 B [VERIFIED]; 27 partitions; sbl1/aboot/hyp/rpm/tz live in the user area with `*bak` copies;
  boot0/boot1 are 4 MiB of zeros; primary and secondary GPT identical in two dumps.
- Template files (outside the repo): primary `gpt-6-primary.bin` 17408 B sha256 `34c8a2d9...ace04ea`,
  secondary `gpt-6-secondary.bin` 16896 B sha256 `91538825...c2861a5` [VERIFIED by hash and by an identity rebuild: the generator
  reproduces both dumps byte for byte from the unchanged entries].
- EDL read syntax: `sudo .venv/bin/python edl/edl.py rs <start_LBA> <sectors> <file> --loader=<LDR> --memory=emmc` [VERIFIED on hardware, owner history].
- EDL write syntax: taken from the real `edl.py --help` (section 6) [VERIFIED FROM HELP, NOT EXECUTED]. Reading the help is not a write test: step 0 is the first execution.
- Warning seen on reads: `Sector size in XML 4096 does not match disk sector size 512`. The reads came out correct at 512 B LBAs (matching the GPT and the dump). Because of it every write uses `--sectorsize=512` explicitly (section 6).

## 2. GPT-B layout (generated by `scripts/gen-gpt.sh`) [VERIFIED by tests/verify-images.sh, sgdisk -v]

Configuration: `BOARD_GPT_TEMPLATE` (and optional `BOARD_GPT_TEMPLATE_SECONDARY`) in `config/local.env` (git-ignored). Without it the
script stops with a clear error. Kept entries are copied byte-identical (name, type GUID, unique GUID, LBAs, attributes).
New entries: `boot` and `rootfs`, with unique GUIDs `BOOT_PARTUUID` / `ROOT_PARTUUID` from `config/defaults.env`
(the same values the images embed in extlinux and fstab). The table stays dense (24 entries, header keeps 28 slots).

| # | name | start LBA | end LBA | sectors | status vs original |
|---|---|---|---|---|---|
| 1 | modem | 131072 | 262143 | 131072 | untouched |
| 2 | sbl1 | 262144 | 263167 | 1024 | untouched (boot chain) |
| 3 | sbl1bak | 263168 | 264191 | 1024 | untouched (boot chain) |
| 4 | aboot | 264192 | 266239 | 2048 | entry untouched, CONTENT rewritten (step A) |
| 5 | abootbak | 266240 | 268287 | 2048 | untouched |
| 6 | rpm | 268288 | 269311 | 1024 | untouched (boot chain) |
| 7 | rpmbak | 269312 | 270335 | 1024 | untouched (boot chain) |
| 8 | tz | 270336 | 271359 | 1024 | untouched (boot chain) |
| 9 | tzbak | 271360 | 272383 | 1024 | untouched (boot chain) |
| 10 | hyp | 272384 | 273407 | 1024 | entry untouched, CONTENT rewritten (step B) |
| 11 | hypbak | 273408 | 274431 | 1024 | untouched |
| 12 | pad | 274432 | 276479 | 2048 | untouched (boot chain) |
| 13 | modemst1 | 276480 | 279551 | 3072 | untouched (calibration; rmtfs finds it by name) |
| 14 | modemst2 | 279552 | 282623 | 3072 | untouched (calibration; rmtfs finds it by name) |
| 15 | misc | 282624 | 284671 | 2048 | untouched (retained; the no-op write test now uses `pad`, row 12) |
| 16 | fsc | 284672 | 284673 | 2 | untouched (calibration) |
| 17 | ssd | 284674 | 284689 | 16 | untouched (calibration) |
| 18 | splash | 284690 | 305169 | 20480 | untouched (calibration group per owner) |
| 19 | DDR | 393216 | 393279 | 64 | untouched (calibration) |
| 20 | fsg | 393280 | 396351 | 3072 | untouched (calibration) |
| 21 | sec | 396352 | 396383 | 32 | untouched (calibration) |
| 22 | **boot** | **429152** | **560223** | **131072** (64 MiB) | NEW (old boot 396384..429151 / 32768 removed) |
| 23 | persist | 2067552 | 2133087 | 65536 | untouched, byte-identical, moved from slot 24 to 23 |
| 24 | **rootfs** | **2133088** | **7634903** | **5501816** (2.62 GiB) | NEW |

Removed from the table: old `boot`, `system` (429152..2067551), `cache`, `recovery`, `userdata`.
Free (unlisted) space: 305170..393215, 396384..429151, 560224..2067551 (~736 MiB gap before `persist`), 7634904..7634910 (7 sectors, alignment).
Only the Android stock userspace is given up (OWNER decision GPT-B); returning to stock needs the full dump (ROLLBACK.md).

### Comparison with the original `gpt-6-primary.bin` (what GPT-B changes, nothing else) [VERIFIED]
- Entries: 22 of 27 byte-identical. CHANGED `boot` (396384/32768 -> 429152/131072), REMOVED `system`, `cache`, `recovery`, `userdata`, ADDED `rootfs`. Slot move: `persist` 24 -> 23.
- Primary (34 sectors): differs only at LBA 1 (header: entries CRC, header CRC), LBA 7 and 8 (entry array). LBA 0 (protective MBR) and 31 other sectors are identical.
- Secondary (33 sectors, from LBA 7634911): differs only at LBA 7634916, 7634917 (entry array) and 7634943 (header).
- Header fields identical: disk GUID, usable range 34..7634910, backup LBA, entry count 28, entry size 128.

## 3. What is rewritten and what stays

REWRITTEN (the only writes planned, in this order):

| Step | Write | Target | Note |
|---|---|---|---|
| A | `out/fw/aboot.mbn` (lk2nd, 318620 B) | content of `aboot` 264192/2048 | stock aboot lost unless restored from dump |
| B | `out/fw/hyp.mbn` (10424 B) | content of `hyp` 272384/1024 | test-signed; boots if secure boot is off: CLAIMED (secure boot disabled per QFPROM read, section 1), TO BE CONFIRMED at the first real boot |
| C | `out/fw/gpt-primary.bin` (LBA 0..33) + `out/fw/gpt-secondary.bin` (LBA 7634911..7634943) | GPT sectors | after step A/B proved the bootloader |
| D | `boot.bin` (64 MiB) | `boot` 429152/131072 | overwrites the head of old `system` |
| E | `rootfs.bin` (sparse, EDL needs RAW) | `rootfs` 2133088/5501816 | overwrites old cache, recovery, most of userdata |

UNTOUCHED (never written): sbl1, sbl1bak, rpm, rpmbak, tz, tzbak, pad (boot chain); modemst1, modemst2, fsc, ssd, DDR, fsg, sec,
persist, splash (calibration); modem, abootbak, hypbak, misc; boot0/boot1.
`abootbak` and `hypbak` stay as in the original GPT. Whether the SBL falls back to them when `aboot`/`hyp` fail verification is [ASSUMED], not verified.
Whether EDL stays reachable after a bad `aboot`/`hyp` is [UNPROVEN]. PBL is in ROM, but it normally falls back to EDL when SBL cannot be loaded; `sbl1` is untouched
here, so a bad `aboot`/`hyp` makes SBL stage fail later, not PBL: that fallback is probably NOT triggered by this failure. Do not rely on it: see GATE G0b. `abootbak` is not a copy of `aboot` on #6 (ROLLBACK.md).

## 4. Images [artifacts]

Sizes come from `out/fw/layout-ufi103s.env` (rounded down to 4096 B, never above the partition): boot 67108864 B, rootfs 2816929792 B.

Current artifacts (canonical build, `sudo scripts/40-images.sh`, verified as root by the owner; hashes re-computed here, no mounts):

| File | sha256 |
|---|---|
| `out/fw/gpt-primary.bin` | `d418884e2181daebe2e5871090c005a0ff7623591a32183e8082c932257e0b49` |
| `out/fw/gpt-secondary.bin` | `73df38ecb49fc6a7d9e2aeb8b6848559158654145ebc2116a9847f4a0d306b4c` |
| `out/fw/gpt_both0.bin` (= primary + secondary; validity as a fastboot `partition` image for lk2nd is [ASSUMED], not used by this plan) | `2a9f143048aab377853950c15893cb8c0d5966714489c5952b2daa1683f241fb` |
| `out/files-ufi103s/boot.bin` (26193380 B, sparse) | `4f19fc744e9cdcea2ef27451ea61056d75fd5002d8b4f847f3793ae0a5c0af46` |
| `out/files-ufi103s/rootfs.bin` (222020948 B, sparse) | `a04712a1968561b8a3e8d98a91bf79bf5ed39bc2111ddbe155bac04599d78b64` |
| `out/step1/lk2nd.img` (376848 B, lk2nd-msm8916, section 5d) | `2a7f8a23ab940db82f68079ae5f2c7dea597bfc49399f33d986b9e4f9c8b7099` |
| `out/step1/lk2nd-padded-512KiB.img` (524288 B, the file to write in step 1) | `d82f3535972869deb2f963ac33a33ea2cd01cbf1cbd661a4bcc23f491fbcbfde` |

Raw sizes (`work/img/boot.raw` 67108864 B, `work/img/rootfs.raw` 2816929792 B) come from `out/fw/layout-ufi103s.env`. `boot.bin`/`rootfs.bin` are sparse: convert with `simg2img` before any EDL write [ASSUMED].
OBSOLETE, never flash: `out/files-ufi103s-rootless/*` (rootfs `a614f928...`, boot `9c735212...`) and `out/obsolete/*`.

## 5. Minimal safe bootloader test

### GATE G0a: EDL reachable while the image in `boot` is bad (criterion for step 1)

UPDATE 2026-11-13 (owner, #6 with lk2nd in `boot` after step 1): `fastboot oem reboot-edl` reaches `05c6:9008` in about 3 s, and the reset button + USB also gives `05c6:9008` with lk2nd in `boot`. G0a RECONFIRMED with lk2nd running (owner report, not re-run by this session).

Criterion: with `sbl1`, `aboot`, `hyp` UNTOUCHED, EDL must be reachable when the image in `boot` is wrong (no Android). Method: reset button held while plugging in USB.
Verified on #6 with Android intact: `lsusb` showed `05c6:9008` [VERIFIED by owner]. Re-confirm after any write (after step 0 and again after step 1, before relying on it).
Why it should hold (INFERENCE): the PBL loads `sbl1` and EDL entry by button does not depend on `boot`; `sbl1` is untouched by step 1. Not proven with a bad `boot` image until the owner re-checks it after step 0.
Step 1 (section 5d) needs G0a only; it does not need G0b.

### GATE G0b: Android-independent EDL entry with `aboot`/`hyp` replaced (OPEN BLOCKER, not assumed)

Steps A and B replace `aboot` and `hyp`. Until now every EDL session on #6 was reached through a running Android (`adb reboot edl`): that path
does not exist once `aboot`/`hyp` are replaced and fail to boot, and it proves nothing about recovery. **Section 5 step 2 (and any A/B write) is BLOCKED until G0b is closed.** (G0a does not close G0b: the button method was only observed with stock `sbl1`/`aboot`/`hyp`.)
Closing criterion for G0b (owner, on this very board, with Android NOT running): `lsusb` shows `05c6:9008` AND a read-only `rs` succeeds, reached by a method
that does not use adb, stock aboot or any bootloader code. Candidate leads, ALL [NOT VERIFIED] on UFI103S_V02:
- an EDL/test point or short on the PCB (location unknown for this board; needs photos of the PCB and an owner check);
- a USB-plug-in key combination (varies per stick; not documented for this PCB);
- `fastboot oem reboot-edl` of lk2nd (`src/lk2nd/lk2nd/fastboot/misc.c:16-21`, `Documentation/fastboot.md:13`): usable only AFTER lk2nd boots, so it cannot be the gate for the first A/B write;
- forcing EDL by corrupting `sbl1`: never do this deliberately.
Do not treat any lead as verified until the owner reports the observed result.

1. Pre-flash checklist (section 7) complete, rollback gate passed (ROLLBACK.md, "Gate"), G0b closed.
2. EDL: write ONLY `aboot.mbn` -> `aboot` and `hyp.mbn` -> `hyp`, by LBA against the ORIGINAL GPT (no GPT write) [syntax from help, section 6, with `--sectorsize=512`, dry-run with `--skipwrite` first].
3. Read both back (`rs`), compare sha256 with the files.
4. Cold boot (unplug/plug, not `edl reset`). Expect lk2nd fastboot (`lsusb`, `fastboot getvar all`) [ASSUMED USB id].
5. Stop. Report. No GPT/boot/rootfs until the owner confirms.
If it does not boot: EDL is only guaranteed by a method proven in G0b (not by this sentence); restore `aboot`/`hyp` from the dump (ROLLBACK.md layer b). EDL test point for UFI103S_V02 [NOT VERIFIED].

## 5b. Alternative test that leaves `aboot`/`hyp` untouched: lk2nd as a boot image (EVALUATION; now concretised in section 5d)

Idea: build `lk2nd-msm8916` instead of `lk1st-msm8916` (`src/lk2nd/Documentation/building.md:28-36`: produces `build-lk2nd-msmXXXX/lk2nd.img`) and let the STOCK `aboot` load it
from the `boot` partition, "just like the original Android image" (`src/lk2nd/README.md:23-30`). `scripts/build_hyp_aboot.sh` builds only lk1st (`aboot.mbn`, `hyp.mbn`); `lk2nd.img` has since been built: `out/step1/lk2nd.img` (section 5d).

| Claim | Status |
|---|---|
| lk2nd is installed by writing `lk2nd.img` to `boot`; the real OS goes at 512 KiB offset or on an ext2 partition | VERIFIED in sources: `README.md:44-52,69-71`, `Documentation/boot.md:5-10` |
| lk2nd finds `/extlinux/extlinux.conf` on any ext2 partition bigger than 16 MiB and on `boot` offset 512k | VERIFIED: `Documentation/boot.md:26-28`. The original `boot` (396384..429151, 16 MiB) is NOT "bigger than 16 MiB"; extlinux would live on the GPT-B `boot` (64 MiB), i.e. step C is still needed for a full OS boot |
| Minimal value without step C: lk2nd starts under stock aboot and offers fastboot (`getvar`, `oem reboot-edl`) | ASSUMED (lk2nd documented flow; not tried on a UFI103S) |
| `hyp` can stay stock: qhypstub can be loaded by lk2nd at RUNTIME from a virtual `qhypstub` partition, no write to `hyp` (`src/qhypstub/README.md`, "Loading qhypstub at runtime") | ASSUMED. Only the qhypstub README describes it (commit 4562184e). The pinned lk2nd (99297666) does NOT contain the runtime loader: `grep -rIi 'qhyp\|hypstub\|0x86400000' src/lk2nd` finds only a comment in `msm8916-512mb-mtp.dts` (VERIFIED, `out/raport-step2lite.md` section 3). `fastboot flash qhypstub` will not work with the current `out/step1/lk2nd.img`; it would need a newer lk2nd pin. Works on #6: ASSUMED |
| Booting an aarch64 kernel through lk2nd with the stock 32-bit LK/hyp chain works without qhypstub | ASSUMED (`qhypstub/README.md`: partial/SMP bug with the stock hyp) |
| Stock aboot accepts an unsigned/test-signed boot image | CLAIMED, to confirm at the first real boot: secure boot disabled per QFPROM read (section 1, inference), no keystore/devinfo in the GPT (inference). lk2nd can test-sign images (`Documentation/building.md:87-98`), a test key does NOT satisfy real secure boot |
| lk2nd compatible/DTB for #6: `thwc,ufi001c` (`boards/ufi103s/board.env:9`); `build_hyp_aboot.sh` falls back to `yiming,uz801-v3` if the variable is unset | VERIFIED text; correctness on the hardware ASSUMED |

What the variant removes from the first test: the risk of a bad `aboot` or `hyp` (the stock bootloader chain stays intact, so the current "may brick without EDL" scenario of steps A/B disappears).
Write target would be the original `boot` partition 396384..429151 (32768 sectors), restorable from the dump slice `skip=396384 count=32768`; it overwrites stock Android `boot`, so Android no longer boots from it (ROLLBACK row to add if adopted).
Residual risks (they remain): the boot image may be rejected by stock aboot (secure boot, ASSUMED); the 512 KiB offset layout; stock Android is lost until the slice is restored; **G0a applies** (Android would no longer boot, so the adb path is gone);
it does NOT test acceptance of the test-signed `aboot`/`hyp`, so that risk is deferred to GPT-B steps A/B, not removed. It also exercises `lk2nd` (`lk2nd-msm8916`), a different binary from the `lk1st` `out/fw/aboot.mbn` that GPT-B ships: success here does not validate the artifact that would actually be flashed. Recommendation (owner decides): do this variant first, GPT-B afterwards.

## 5c. Step 0: no-op write on `pad` (proves the EDL write path, changes nothing)

Target: partition `pad`, LBA 274432, 2048 sectors (1 MiB), per the original GPT (section 2 table, row 12). Owner statement: unused. Because the write is the SAME bytes, the content does not matter for the result.
Expected hash (computed by the owner from the EDL dump; this session did not read the dump): `pad` LBA 274432, 2048 sectors, sha256 `77285b6a0dd889a9886ccc10aff51e49e4972b30ec53ec16006a15539bd2400c`.
Precondition: Android running on #6, `adb reboot edl` path (the only EDL entry proven so far for the write session); no bootloader byte changes, so G0a/G0b are not needed for this step.
Write syntax (from `edl.py --help`, not executed): `ws <start_sector> <filename> [--memory=emmc] [--skipwrite]`, `w <partitionname> <filename>`. `--skipwrite` is the dry-run: run it before every real write.
FORBIDDEN: `--lun=1` or `--lun=2` on any write (on eMMC they are boot0/boot1). Never pass `--lun` to a write at all.

```sh
# working dir OUTSIDE the repo, e.g. W=~/6-edl-step0 ; LDR=<path>/prog_emmc_firehose_8916.mbn (sha256 959439aa...cab14)
EDL="sudo .venv/bin/python edl/edl.py"; COMMON="--loader=$LDR --memory=emmc"
PAD_SHA=77285b6a0dd889a9886ccc10aff51e49e4972b30ec53ec16006a15539bd2400c
sha256sum "$LDR"                                          # must be 959439aa5864685999b713c3ed12ad5fa408149648b670a9a9ef77bcc9dcab14
$EDL rs 0 34 $W/gpt_live.bin $COMMON                       # [VERIFIED syntax]
sha256sum $W/gpt_live.bin                                  # must equal sha256 of gpt-6-primary.bin (34c8a2d9...ace04ea): identity check
$EDL rs 274432 2048 $W/pad_r1.bin $COMMON                  # [VERIFIED syntax]
$EDL rs 274432 2048 $W/pad_r2.bin $COMMON
cmp $W/pad_r1.bin $W/pad_r2.bin && echo STABLE             # two reads identical, else STOP (content not static)
echo "$PAD_SHA  $W/pad_r1.bin" | sha256sum -c -            # MUST be OK before any write: read of `pad` == expected hash from the dump. Else STOP.
cp $W/pad_r1.bin $W/pad_orig.bin                           # restore source (same hash as the dump slice)
# dry-run (nothing is written):
$EDL ws 274432 $W/pad_orig.bin --sectorsize=512 --skipwrite $COMMON    # [syntax from help]
# real write (only after the dry-run looked sane):
$EDL ws 274432 $W/pad_orig.bin --sectorsize=512 $COMMON    # [syntax from help, not executed yet]
$EDL rs 274432 2048 $W/pad_after.bin $COMMON
echo "$PAD_SHA  $W/pad_after.bin" | sha256sum -c -  &&  cmp $W/pad_orig.bin $W/pad_after.bin && echo PAD_NOOP_PASS
$EDL rs 0 34 $W/gpt_after.bin $COMMON; cmp $W/gpt_live.bin $W/gpt_after.bin && echo GPT_UNCHANGED
$EDL reset --resetmode=reset --loader=$LDR              # NO --memory: `reset` has no such option in the help (only --resetmode and --loader); leave EDL, then Android must boot as before
```

How to run it: NOT pasted whole. The block is a script (`set -e`, stops at the first failing command) split into phases, and it exists on the owner laptop as `~/step0.sh` (not in this repo; this session did not read or run it):
1. `check`: loader hash, GPT identity, two `pad` reads, `cmp`, read hash == `PAD_SHA`. Read-only.
2. `dry`: the `ws ... --skipwrite` line only. Writes nothing.
3. `write`: the real `ws`, the read-backs and the `reset`. Requires `CONFIRM=da-flashez` in the environment, otherwise it refuses to start.
Order: `check`, then `dry`, then (after "da, flashez") `write`. Each phase is a separate invocation; a failure in one stops the run (`set -e`) and the next phase is not started.

PASS = pad read equals the expected hash BEFORE the write, the read after the write equals it too, GPT unchanged, Android boots. Any `ws` error or mismatch: STOP, do not go to step 1.
A mismatch is repaired by writing `pad_orig.bin` again (same command), or from the dump slice `skip=274432 count=2048` (the owner compares its hash with `77285b6a0dd889a9886ccc10aff51e49e4972b30ec53ec16006a15539bd2400c`).
After step 0, re-check G0a (reset button + USB, `lsusb` shows `05c6:9008`).
What this proves: write syntax, loader, eMMC write path for 1 MiB at an exact LBA, and that the `--sectorsize=512` form behaves. What it does NOT prove: large writes (step 1 is 512 KiB, steps D/E are much larger), sparse handling, or EDL reachability with a bad `boot`.
This step replaces the `misc` no-op gate of ROLLBACK.md (gate step 2); ROLLBACK.md now names `pad` as well. `pad` is not touched by any later GPT-B step either.

## 5d. Step 1 (minimal): lk2nd as a boot image in the ORIGINAL `boot` partition

Supersedes the recommendation text of section 5b: lk2nd-msm8916 IS buildable in this builder (details and log in `out/raport-step1.md`, `out/step1/build-lk2nd-msm8916.log`).
Scope: ONE write, 512 KiB at the start of the original `boot` partition (LBA 396384, 32768 sectors total = 16 MiB). Original GPT unchanged (no GPT write), `aboot`, `hyp`, `abootbak`, `hypbak`, sbl1, rpm, tz untouched. Android `boot` is lost until restored, so Android no longer boots: **G0a applies** (section 5): EDL via reset button + USB, to be re-confirmed after step 0. G0b (A/B) stays OPEN and is NOT needed here.

Artifact: `out/step1/lk2nd-padded-512KiB.img`, 524288 B = 1024 sectors, sha256 `d82f3535972869deb2f963ac33a33ea2cd01cbf1cbd661a4bcc23f491fbcbfde`
(= `lk2nd.img` 376848 B, sha256 `2a7f8a23...b7099`, zero padded to 512 KiB, the `lk2nd` partition size of the build, `LK2ND_PARTITION_SIZE ?= 512*1024`).
Build is reproducible here: clean rebuild gave the same sha256. Not test-signed (`SIGN_BOOTIMG=0`) and not signed for secure boot.

```sh
# Pre: step 0 PASS, G0a re-confirmed after step 0, "da, flashez" for step 1, W as above. FORBIDDEN: --lun on any write.
BOOT_SHA=8028815bc72771c8d50f730e265e698c32595f804b287955368e1c695b5efb71   # expected, computed by the owner from the EDL dump (LBA 396384, 32768 sectors)
$EDL rs 396384 32768 $W/boot_orig_pre.bin $COMMON          # pre-image of the whole original boot partition, 16 MiB
echo "$BOOT_SHA  $W/boot_orig_pre.bin" | sha256sum -c -    # MUST be OK before the write, else STOP
# keep $W/boot_orig_pre.bin as the LOCAL rollback artifact (outside the repo, never uploaded); copy it to a second location
$EDL ws 396384 out/step1/lk2nd-padded-512KiB.img --sectorsize=512 --skipwrite $COMMON   # dry-run, nothing written
$EDL ws 396384 out/step1/lk2nd-padded-512KiB.img --sectorsize=512 $COMMON    # [syntax from help] 1024 sectors only
$EDL rs 396384 1024 $W/boot_after_head.bin $COMMON
cmp out/step1/lk2nd-padded-512KiB.img $W/boot_after_head.bin && echo LK2ND_WRITE_PASS
$EDL rs 396384 32768 $W/boot_after_full.bin $COMMON         # tail 396384+1024.. must still equal boot_orig_pre.bin
cmp -n $((31744*512)) -i $((1024*512)) $W/boot_orig_pre.bin $W/boot_after_full.bin && echo TAIL_UNTOUCHED
$EDL rs 0 34 $W/gpt_after1.bin $COMMON; cmp $W/gpt_live.bin $W/gpt_after1.bin && echo GPT_UNCHANGED
# then COLD BOOT (unplug/plug USB, no `edl reset`), observe lsusb + fastboot
```

Expected on the stick [ASSUMED unless noted]:
- stock aboot loads the boot image (accepts an unsigned image: ASSUMED, depends on secure boot enforcement and the device lock state, both unverified), lk2nd starts;
- no usable OS at offset 512 KiB (old Android data, no valid image) and no extlinux (original boot is 16 MiB, "bigger than 16 MiB" is false), so lk2nd should fall to fastboot;
- `lsusb` shows `18d1:d00d` (VID/PID in `src/lk2nd/app/aboot/fastboot.c:71-72`, VERIFIED in source; the stock aboot fastboot uses the same IDs, so this ID alone does NOT distinguish lk2nd from stock);
- `fastboot getvar all` should report lk2nd identifiers (version string built without git: `lk2nd/version` fallback, exact text unknown); `fastboot oem reboot-edl` exists (`lk2nd/fastboot/misc.c:21`, VERIFIED in source) and is the first software route back to EDL, usable only if lk2nd boots;
- the stick has no display or volume keys on this PCB (ASSUMED): there is nothing to see on screen; only USB tells.
Stop after the observation. No qhypstub, no GPT, no boot.bin/rootfs.bin until the owner decides. Optional later (not part of step 1): `fastboot flash qhypstub out/step1/qhypstub.bin` (sha256 below), runtime load, `hyp` stays stock [ASSUMED; the pinned lk2nd 99297666 has no runtime loader, see section 5b, so this needs a newer lk2nd pin].

Restore (back to the original `boot`): rewrite exactly the 32768 sectors from the saved pre-image (`boot_orig_pre.bin`, hash verified against `BOOT_SHA` before the write), read back and verify with the same hash:
```sh
echo "$BOOT_SHA  $W/boot_orig_pre.bin" | sha256sum -c -    # the rollback artifact is intact
$EDL ws 396384 $W/boot_orig_pre.bin --sectorsize=512 --skipwrite $COMMON   # dry-run
$EDL ws 396384 $W/boot_orig_pre.bin --sectorsize=512 $COMMON               # [syntax from help]
$EDL rs 396384 32768 $W/boot_rb.bin $COMMON
echo "$BOOT_SHA  $W/boot_rb.bin" | sha256sum -c -         # restore verified by the expected hash
```
Fallback if the local artifact is lost: a slice cut from the dump (`dd if=<6-...-emmc-user.bin> bs=512 skip=396384 count=32768`), whose sha256 must equal `BOOT_SHA`. The whole partition is rewritten (not only the 1024 changed sectors) so the hash check covers all of it.

Residual risks after step 1 (what is NOT removed):
1. If lk2nd does not start or stock aboot rejects the image, #6 has no Android and no known software route to EDL (`adb reboot edl` is gone, lk2nd's `oem reboot-edl` unavailable). `sbl1` is intact, so PBL will NOT fall back to EDL by itself. EDL then depends on the reset-button method (G0a, verified only with Android intact, to be re-confirmed after step 0) or a test point. This is the main remaining risk; it is why G0a must be re-confirmed first (it is the recovery path for this very risk).
2. Stock aboot behaviour on a bad boot image is UNVERIFIED; it may enter its own fastboot (also a route to `reboot-edl` [ASSUMED, depends on the stock build]).
3. Secure boot is DISABLED per QFPROM read (section 1, inference, depends on loader memory-read support); acceptance of an unsigned boot image and of test-signed `aboot`/`hyp` is still CLAIMED until a real boot.
4. lk2nd mmc init uses default caps; `lk1st` builds in this repo add `USE_TARGET_HS200_CAPS=1` for flaky eMMC (`scripts/build_hyp_aboot.sh`). This `lk2nd.img` does NOT carry that patch (stock aboot already initialised mmc before lk2nd; behaviour on #6 UNVERIFIED).
5. The 512 KiB offset area holds stale Android bytes; lk2nd may try to parse it (ASSUMED: rejected, then fastboot).
6. Success validates lk2nd-msm8916, not the lk1st `aboot.mbn`/`hyp.mbn` (steps A/B) nor GPT-B, nor the 64 MiB `boot.bin` layout.
7. Unique partitions (modemst1/2, fsg, fsc, sec, persist) are not touched. Android stock needs the restore above.

`out/step1/qhypstub.bin` sha256: see `out/raport-step1.md`.

## 6. EDL commands

Source: the real `edl.py --help` (owner). Syntax VERIFIED FROM HELP, execution NOT yet verified (step 0 is the first run).

- `ws <start_sector> <filename> [--memory=emmc] [--skipwrite]`: write by LBA. `--skipwrite` = dry-run, use it before every real write.
- `w <partitionname> <filename>`: write by partition name.
- `reset [--resetmode=reset|off|edl]`.
- `secureboot`, `qfp`, `pbl` are READ commands (not writes).
- `rs <start_LBA> <sectors> <file>`: read, VERIFIED on hardware.

Rules for every write command in this repo:
1. Always add `--sectorsize=512` explicitly (the reads warn `Sector size in XML 4096 does not match disk sector size 512`; reads were correct at 512 B LBAs).
2. Run it first with `--skipwrite`.
3. NEVER `--lun=1` or `--lun=2` on a write (boot0/boot1 on eMMC). Do not pass `--lun` to writes at all.
4. `rootfs.bin` is sparse and must be converted with `simg2img` before an EDL write [ASSUMED, README]. All restore commands are printed (not run) by `tools/rollback-plan.sh` (it does not yet add `--sectorsize=512`: add it by hand).

## 7. Pre-flash checklist

- [ ] PCB silkscreen `UFI103S_V02` (HW1.3) confirmed on the board; NOT `ro.product.model` or adb serial (CLAUDE.md rule 2).
- [ ] Full dump verified (sha256) in 2 locations; unique partitions outside git and uploads; per-partition images available for ROLLBACK layer (b).
- [ ] `tools/rollback-plan.sh` run with a sha256 manifest: all hashes VERIFIED.
- [ ] Rollback gate: EDL read passed, then no-op write on `pad` passed, with the read of `pad` equal to the expected hash before the write (ROLLBACK.md).
- [x] `tests/verify-images.sh` passed on the canonical images (owner report; it then included a mount-based part B run as root, which no longer exists; hashes in section 4); stale tree replaced.
- [ ] GATE G0a (step 1): EDL via reset button + USB re-confirmed after step 0, `05c6:9008` (observed once with Android intact).
- [ ] GATE G0b (A/B only): EDL reached WITHOUT Android and with `aboot`/`hyp` replaced or bad (section 5). OPEN BLOCKER.
- [ ] `lsusb` shows `05c6:9008`; `edl printgpt` (read-only) equals `gpt-6-primary.bin`.
- [ ] `COMMIT=1` guard in any flashing script + identity check before writes.
- [ ] Explicit "da, flashez" from the owner, per step.
- [ ] Default passwords changed (root has an empty password in the image, CLAUDE.md rule 7).

## 8. Open items

- **G0b (section 5): EDL entry with `aboot`/`hyp` replaced (A/B).** OPEN BLOCKER. G0a (step 1) has a method, to be re-confirmed after step 0.
- Section 5d (lk2nd as boot image): image built, step 0 and step 1 specified; waiting for the owner (G0a re-check and "da, flashez").
- `6-edl-emmc-user.sha256` lists a hash for another path; the dump hash matches `SHA256SUMS` and the Windows record (ROLLBACK.md, Evidence).
- Second copy of the dump (pre-flash checklist) not verified.
- Test-signed aboot+hyp acceptance on #6: secure boot disabled per QFPROM read (inference), to confirm at the first real boot.
- SBL fallback to `abootbak`/`hypbak`; EDL test point; whether lk2nd finds `boot` by its new position (name `boot`, type unchanged).
- Windows full dump must be re-verified as exactly 3909091328 B before any layer (b)/(c) rollback.
- GPT-C (keep all 27 entries) was not built; GPT-B chosen.

## 9. Ownership / permission defect (RESOLVED on the real canonical build)

Defect: `out/rootfs-ufi103s.tgz` carried files owned by uid/gid 1000 (user `openstick` in the image) and group-writable (664/775) under `/etc`:
`msm-firmware-loader.service`, `usb-gadget.service`, `serial-getty@ttyMSM0.service.d/override.conf` and the `.wants` entries (14 VIOLATION lines from `tests/lib-ownership.sh`).
The same defect existed in the gadget overlay `work/gt-dist/etc/gt/*` (templates, added by `scripts/40-images.sh`, NOT visible in the tarball).
Cause: `cp -a` from the repo checkout (owner and mode of the build host) in `scripts/20-rootfs.sh` and `scripts/30-gt.sh`; `40-images.sh` repeated `cp -a`.
Fix: `install_tree` (`scripts/common.sh`) writes root:root, dirs 755, files 644/755; every `install` in `20-rootfs.sh` has `-o root -g root`; the apk extraction uses `--no-same-owner`;
`40-images.sh` copies the overlay with `--no-preserve=ownership` and refuses a dirty `gt-dist`; `20-rootfs.sh` refuses to write the tarball if `own_scan_dir` finds a violation (hard failure, no silent chown).
Policy (checked by `tests/lib-ownership.sh`): nothing outside `/home/$DEFAULT_USER` with uid or gid >= 1000 (uid/gid 65534 is NOT exempt), nothing group/world-writable under `/etc`, `/usr`, `/boot` (symlinks excepted).
Tests: `tests/verify-ownership.sh` (rootless, fakeroot, negative controls), plus checks in `tests/verify-rootfs.sh` and `tests/verify-images.sh` (tarball, `gt-dist`, mounted rootfs and boot).
Resolution (owner report): `sudo scripts/30-gt.sh`, `sudo scripts/20-rootfs.sh`, `sudo scripts/40-images.sh` were run on the fixed sources; `tests/verify-rootfs.sh` and `tests/verify-images.sh`
(part B, mount-based, included) gave 0 FAIL. New image hashes: section 4. The `out/verify-*.log` files in this tree predate that run (05:58) and were not regenerated by this session.
