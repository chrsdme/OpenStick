# Third-party files in this tree

Generated 2026-11-14. Licenses are listed only where a LICENSE file or an SPDX/file header states them; the evidence path is given. Source repos are the clones in `$OPENSTICK_ROOT/upstream/` (sha256 matched). "Tested on board" is "yes" only if a log under `out/` or a board backup/diagnostic dir shows that file booting on that board; none of the logs searched do for the DTBs below.

## contrib/dtb (precompiled binaries)

| file | sha256 | source | license | tested on board | DTS source |
|---|---|---|---|---|---|
| `msm8916-fy-mf800.dtb` | `ff4408c48df1be2717dfb80bea7403a619c09e1a0345ac1dd66fa86b79fb4760` | kinsamanka/OpenStick-Builder @57244976de2dccec5d1c9eb527d3cc2793d580c2 `dtbs/msm8916-fy-mf800.dtb` (sha256 match; x7780 copy differs) | unknown for the binary itself. Repo LICENSE is MIT, (c) 2024 GP Orcullo (`upstream/kinsamanka_OpenStick-Builder/LICENSE`); it does not state the license of the DTB contents (kernel DTS are normally GPL-2.0, unverified here) | no | unknown (no .dts for this name in upstream clones or `src/linux-dts`) |
| `msm8916-jz01-45-v33.dtb` | `9ca7406570ca2058e2dd1e1bf77d81910fafebaa5a77a9e055396e63471bacb2` | byte-identical in kinsamanka/OpenStick-Builder @57244976de2dccec5d1c9eb527d3cc2793d580c2 and x7780/OpenStick-Builder @ef731fa31eefdf5730f87e31d9aecafe61159ed2 (`dtbs/`) | unknown for the binary itself. Repo LICENSE is MIT, (c) 2024 GP Orcullo (`upstream/kinsamanka_OpenStick-Builder/LICENSE`); it does not state the license of the DTB contents (kernel DTS are normally GPL-2.0, unverified here) | no | unknown (no .dts for this name in upstream clones or `src/linux-dts`) |

Note: the original upstream builder source repo of these DTBs is not documented; "unknown (no match in upstream clones)" applies to anything beyond the two kinsamanka/x7780 repos above. Matching for `msm8916-fy-mf800.dtb` was also found in `work/rootfs/boot/dtbs/qcom/` (a local build output, not a source).

## dts/ (device tree sources)

| file | sha256 | source | license (header) | tested on board | notes |
|---|---|---|---|---|---|
| `dts/msm8916-thwc-ufi103s.dts` | `b1306b3f460576ab1f3d50443679f4c74b727df0eecf55c7ed7d6a65747d1fa8` | byte-identical to `upstream/x7780_OpenStick-Builder/dtbs/msm8916-thwc-ufi103s.dts` (x7780/OpenStick-Builder @ef731fa31eefdf5730f87e31d9aecafe61159ed2) | GPL-2.0-only (SPDX line 1 of the file) | no (no boot log for the DTB built from it) | compiled to `out/dtbs/` by `scripts/build-dtb.sh` |
| `dts/msm8916-ufi.dtsi` | `5b205e5839a758887e4e49d8548543fb0f769489bada71ffe9086ebb74fedfa6` | modified copy of x7780 `dtbs/msm8916-ufi.dtsi` (x7780/OpenStick-Builder @ef731fa31eefdf5730f87e31d9aecafe61159ed2); no byte match | GPL-2.0-only (SPDX line 1) | no | modified for the 6.6 tree (see header comment) |

## Other copied files

| file(s) | sha256 | source | license | tested | notes |
|---|---|---|---|---|---|
| `scripts/msm-firmware-loader.sh` | `23d769b904d00f720f90ffe7d09b49fc1ee84ccfeefc41920515102f0f11777a` | byte-identical to `kinsamanka/OpenStick-Builder @57244976de2dccec5d1c9eb527d3cc2793d580c2` `scripts/msm-firmware-loader.sh` | MIT (SPDX line 2 of the file) | not shown by logs | unmodified |
| `configs/templates/*.scheme` (15 files, hashes below) | see below | byte-identical to the same paths in kinsamanka and x7780 clones (K and X above) | MIT per repo LICENSE (`upstream/kinsamanka_OpenStick-Builder/LICENSE`); no per-file header | n/a | |
| `scripts/gptool.py` | `9e0ccba4927b981af7e4c20745921f6f131fdca9ee8fcb58eb58104873963248` | no match in upstream clones (rg for `gptool` finds nothing under `$OPENSTICK_ROOT/upstream/`); own work, inferred from the project history | MIT (SPDX line 2 of the file) | partly (used for board #6 GPT, see out/gen-gpt.log) | standard library only |
| `tools/gen-gpt-tz1m.py`, `tools/mkimg.py` | `574266eaec8840f62a3d2c9371d42cc5d32dcd0742dad489b874cea90e8c1ded`, `b81086ec97ab32b9a9228edb253f1ffd17309cb7a7143e7beab2c4d70ee24c12` | no match in upstream clones (own work, inferred) | project license (`LICENSE`) | n/a | |
| `scripts/20-rootfs.sh`, `setup.sh`, `30-gt.sh`, `40-images.sh`, `extract_fw.sh`, `build_hyp_aboot.sh`, `configs/*` | n/a | derived (modified) from kinsamanka/OpenStick-Builder @57244976de2dccec5d1c9eb527d3cc2793d580c2 per `NOTICE.md`; no byte match | MIT per repo LICENSE | n/a | modified derivatives |

### configs/templates sha256

```
26dbb946c777465dd16f8bd2ce39fd81afdd10ed76d68714623a56ab6871218c  configs/templates/acm-ecm.scheme
9e19c080b3415f37cab1a3cbb335be61d9c0ccd36911afe6491be88f8130ad47  configs/templates/acm.scheme
9d7750485a91d13a5c67acc4bcef9bd441ac79d10ea9badf9fe38f94c8b5533b  configs/templates/bluetooth.scheme
3c0f37386b8534814f3310b5ac5562df396f1216560526b37de18e5981dadf5c  configs/templates/bmc.scheme
de66b99854c93338e3c509645d45a0acc7528093c51c1c63f8838f4fc0c90415  configs/templates/cdc-ecm.scheme
77410498861bfa9dfa593c13bb8efbb280eafa2b10668d4b4a1757c178da4b70  configs/templates/ecm.scheme
122e201b2f39b046071a1b84d3db09546e2f4aa8aa71a6896a68114e8a43610a  configs/templates/ffs.scheme
d87e73f60c21765598598bafbed99b73e50e3983d9c370d4518bf7bd0cfa0959  configs/templates/hid.scheme
b97b6498f2e652a4ebc7a8b472adca3c886d67d4b3e1419ec0cae9a4bfcd3938  configs/templates/mass.scheme
aa8747c0c5e563b23984a15ad130817a35783971b1748cbd2dc0df0f7f7f3cd5  configs/templates/midi.scheme
976a5a4cb222d8c31d6bb2e326baf528409624af8817b95af9c0cce03e28416d  configs/templates/midi2.scheme
8eac69b10caf40582d82c65d7af3dd5e1d049d1501b89a97c9f0e4431b0493d2  configs/templates/rndis-os-desc.scheme
3e31e84ce5e21372519cb718d6f37e0d591ef8a4f3e766cfa3f6b26f7bb02136  configs/templates/uac1.scheme
1d50b39d074e7e06415f76aae1023fd8f652c00b211e5d5f0e7d8a7bf805478d  configs/templates/uac2.scheme
2b6cd92265cbbcafbd62709c39420046e439a62c117616afb0e6812a836dcfc4  configs/templates/uvc.scheme
```

## Fetched at build/flash time (not in git)

| item | full sha256 | source | license | notes |
|---|---|---|---|---|
| `prog_emmc_firehose_8916.mbn` (93288 bytes) | `959439aa5864685999b713c3ed12ad5fa408149648b670a9a9ef77bcc9dcab14` | pinned fetch by `tools/fetch-firehose.sh`: primary `https://raw.githubusercontent.com/Basistiy/openstick-installer/93952e39ad2d9824907484567ae7a342c07e5979/loaders/prog_emmc_firehose_8916.mbn`; fallback `https://raw.githubusercontent.com/nikicat/4g-modem/38f895a576e33b2eba2d5fa88c276b9095f59bb9/prog_emmc_firehose_8916.mbn` | Basistiy repo: MIT, (c) 2026 Evgeny Basistiy (`upstream/Basistiy_openstick-installer/LICENSE`); nikicat/4g-modem: unknown (no LICENSE file found). The repo license does not establish rights over the loader itself. | Vendor-signed Qualcomm programmer; redistribution rights unverified, hence NOT in git (`*.mbn` is in `.gitignore`, line 18). Byte-identical copies in both clones above (sha256 match on disk); both pinned URLs downloaded on 2026-11-14 and hashed to the same value. The hash is the one accepted in `FLASH-PLAN.md:25`. |

Other build-time fetches (lk2nd, qhypstub, qtestsign, gt, libusbgx, pmOS kernel apk, DB410c firmware) are described in `NOTICE.md` and pinned by `scripts/fetch-src.sh`; they are not redistributed.

## Status of contrib/dtb and open license questions (added 2026-11-14)

- `contrib/dtb/msm8916-fy-mf800.dtb` (board #1, MF800B): **EXPERIMENTAL, never booted on a board.** License: **necunoscuta** (binary without a matching DTS in any clone). Proposal: remove the binary and build the DTB from a DTS with a stated license (kernel-style DTS are normally GPL-2.0-only, but that must be confirmed per file), or keep it out of any redistributed image until then.
- `contrib/dtb/msm8916-jz01-45-v33.dtb` (boards #2/#3): **EXPERIMENTAL, never booted on a board.** License: **necunoscuta** (same reason). Same proposal.
- `dts/*`: GPL-2.0-only by SPDX header, byte-identical or modified from x7780 (above). `msm8916-thwc-ufi103s.dts` is the only DTS target of board #6; no boot log of a DTB built from it, the validated #6 boot used the pmOS `thwc-ufi001c` DTB.
- `configs/templates/*.scheme`: identical in two upstream clones, no per-file header, covered only by the upstream MIT repo LICENSE (c) 2024 GP Orcullo. Provenance of the schemes before that repo: **necunoscuta**. Low risk (text templates); if the question matters, regenerate them from the libusbgx documentation instead.
- `scripts/gptool.py`, `tools/mkimg.py`, `tools/gen-gpt-tz1m.py`: own work, MIT header; no third-party code found by search (a search is not a proof of originality).
