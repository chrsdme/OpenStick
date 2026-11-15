# OpenStick installer handoff

## Current result

The workspace is on branch `installer` at accepted M4 HEAD `da4f9c8a2770b11b181611fe5ff525cd2d1be197`. It was already dirty at session start, with M4 integration changes and untracked M4-M7 implementation/report files. Those changes were preserved. No M4 adapter, M5, M6, M7, or documentation commit was created because `.git` is read-only and Git could not create `.git/index.lock`; the index remained unchanged. The owner must review the uncommitted changes and create the milestone commits in a workspace with writable Git metadata.

## Existing commit history

These are the existing local commits in order. M5, M6, M7, and the GPT-T docs correction have no commit because `.git` is read-only in this workspace.

| Milestone | Commit SHA | Subject |
|---|---|---|
| M0 import | `e4cf01b00189a7f2d402410bba2cd9f9d45edca6` | M0: import clean builder snapshot |
| M0 baseline/design | `de86b3d0dbb3832823029b069fee8aca9f9a1b72` | M0: Baseline and design |
| M1 | `32b411b03b67956a2e0187f30e4ace8560ab29bf` | M1: Skeleton, registry, doctor and shims |
| M2 | `16a9ac2e663e1f771b608771d5ae84f8b3beea62` | M2: Automated EDL backup and verification |
| M3 | `5b70d1fcbdbf7c97a89e5925a3497741805722ef` | M3: Configure, seed and image injection |
| M4 | `da4f9c8a2770b11b181611fe5ff525cd2d1be197` | M4: Build and flash wrappers |

The result is **PARTIAL_OFFLINE**. The latest full permitted installer runner passed 26 top-level checks with no failures or runner-level skips. M4, M5 and M7 targeted suites passed 22/22, 22/22 and 14/14 respectively; M6 synthetic package tests passed, and that script prints no numeric count. B independently accepted the final wizard state-file hardening and race regression. M5 includes synthetic full-disk restore verification. M7 drives doctor, selection of a verified synthetic backup, and restore through the wizard and fake EDL rollback engine, then confirms byte-identical full-disk recovery. Its test substitutes synthetic live identity and confirmation responses; separate M5 tests cover mismatch and wrong-confirmation refusals. The GPT-T image-set adapter maps boot-v0 `boot.img` and raw ext4 `rootfs.img` to GPT-T `boot` and `userdata`, but `flash` refuses it with `HARDWARE_REQUIRED` because a safe live-identity-preserving write/readback path is not validated. Therefore the requested integrated first-install E2E remains blocked. No physical device flash was attempted.

The requested `spec/INSTALLER-SPEC.md` and `spec/ACCEPTANCE.md` are missing from this copy. Canonical G1-G9 definitions and their mapping are unavailable, so this handoff does not claim canonical G1-G9 acceptance.

## Commands

Run non-destructive local checks first:

~~~sh
./openstick doctor
./openstick --dry-run build --board ufi103s --profile prod
./openstick verify --host USER@HOST
./openstick package verify /path/to/package.ostpkg.tar
./openstick image-set verify /path/to/gpt-t-image-set
~~~

The image-set commands are offline format/provenance checks. Preparation requires a registered build and paired GPT-T files:

~~~sh
./openstick image-set prepare --build BUILD_ID --layout /path/to/gpt-t-layout --output /path/to/private-image-set
./openstick image-set verify /path/to/private-image-set
~~~

Other CLI entrypoints, to use only with the required private data and reviewed prerequisites:

~~~sh
./openstick backup --paranoid
./openstick configure --profile prod
./openstick build --board ufi103s --profile prod
./openstick seed inject --build BUILD_ID --seed default
./openstick restore --device DEVICE_ID --backup BACKUP_ID --dry-run
./openstick package restore --device DEVICE_ID --backup BACKUP_ID --output /private/path/restore.ostpkg.tar
./openstick package install --device DEVICE_ID --backup BACKUP_ID --image-set /path/to/image-set --output /private/path/install.ostpkg.tar
./openstick wizard --id RUN_ID --stage doctor
~~~

These examples are interface references, not hardware instructions. The existing restore and flash commands can enter destructive phases after their own gates; do not invoke real hardware operations from this workspace. In particular, the GPT-T set produced by image-set prepare is intentionally refused by flash until an independently validated writer and readback path exist. M6 synthetic package checks pass, including standalone package snapshot/integrity regressions, but production package creation still lacks complete external build/image/backup inputs and provenance. The M6 standalone path race found during review was repaired with no-follow access and private verified snapshots, then regression-tested.

## Test evidence

The final permitted full installer run after the M5, M6, and wizard state-race repairs was:

~~~text
bash tests/run-installer-tests.sh
exit 0
TOTAL PASS=26 FAIL=0 SKIP=0
~~~

The runner includes M3, M4, M5, M6 and M7 suites plus seven passing legacy suites. Final targeted commands and results:

~~~text
python3 tests/verify-installer-m4.py  # exit 0, 22 tests
python3 tests/verify-installer-m5.py  # exit 0, 22 tests
python3 tests/verify-installer-m6.py  # exit 0, prints PASS without a numeric count
python3 tests/verify-installer-m7.py  # exit 0, 14 tests
~~~

Nested optional skips were missing aarch64 compiler tools, missing rootfs tar/Docker for an optional tier, and absent real rollback files. The rollback test received nonexistent temporary `REAL_BACKUP` and `REAL_GPT_DIR` paths; it did not access owner backup data. These tests do not establish production package creation or hardware operation.

Final static checks and results:

- `bash -n` over all 53 shell scripts: PASS, zero failures.
- `sh -n` over all 43 POSIX-shebang scripts: PASS, zero failures.
- `python3 -m py_compile` over all 21 repository Python files: PASS.
- `git diff --check`: PASS, exit 0.
- ShellCheck: unavailable.
- Added/changed-file size scan over 10 MiB: none. High-confidence secret-marker scan: zero.

The M4 adapter test command passed 22/22, M5 passed 22/22, M7 passed 14/14, and the M6 synthetic package suite exited 0 without printing a numeric count. M6's standalone install-package test uses a synthetic legacy GPT-B image set, fake EDL, and no builder checkout on PATH; it verifies that the fake fastboot executable is discoverable and asserts that no fastboot invocation occurred. This does not connect M4's GPT-T output to first-install. The M6 no-follow/private-snapshot race repair has regression coverage for restore and install package runners. No fastboot invocation occurred.

No test artifact from a real device is retained. Synthetic fixtures are created in temporary directories and cleaned up by their tests. No package generated from real device data or firmware is present.

## G1-G9 evidence boundary

Because the canonical spec files are absent, no canonical G1-G9 mapping or completion claim is possible. Relevant named negative/refusal evidence in the available tests includes:

- Backup tamper, live identity mismatch, and unpinned loader refusal: tests/verify-installer-m5.py functions test_backup_tamper_or_identity_change_refused and test_restore_rejects_unpinned_loader_and_live_identity_mismatch.
- Wrong typed confirmation and refusal to use shell syntax in an EDL path: tests/verify-installer-m5.py functions test_wrong_typed_confirmation_and_fwlib_test_override_do_not_write and test_fwlib_refuses_shell_syntax_in_edl_path.
- Final full-readback mismatch exits 3 and blocks retry: tests/verify-installer-m5.py function test_restore_final_full_readback_mismatch_is_exit3_and_blocks_retry.
- Interrupted write/restore reconciliation is read-only and partial bytes block retry: tests/verify-installer-m5.py functions test_interrupted_phase_reconciliation_is_no_write_and_resumes_at_that_phase and test_partial_phase_bytes_block_reconciliation_without_retry.
- Package tamper/path traversal, standalone safety and verified-snapshot mutation cases: tests/verify-installer-m6.py (synthetic package checks; production inputs unavailable).
- Wizard corrupt/symlinked state, selection mutation, completed replay, uncertain destructive-stage refusal and fake-EDL byte-identical restore: tests/verify-installer-m7.py functions test_corrupt_or_symlinked_state_refuses, test_selection_cannot_change_on_resume, test_completed_stage_cannot_be_replayed, test_interrupted_destructive_stage_is_never_repeated_by_resume, and test_wizard_restore_runs_guarded_fake_edl_and_requires_full_disk_match.
- Wizard state-leaf swap at the actual descriptor-relative `os.open` boundary refuses the symlink; a symlinked home refuses without creating state under its target: tests/verify-installer-m7.py functions test_state_load_is_anchored_before_a_leaf_swap_and_rejects_parent_symlink and test_state_directory_refuses_symlink_home_without_creating_under_target. B cross-reviewed and accepted these changes.
- GPT-T adapter tamper, unknown/mixed format, GPT geometry, symlink swap and output collision refusals: tests/verify-installer-m4.py functions test_gpt_t_image_set_tampering_is_detected_and_flash_stops_hardware_required, test_gpt_t_adapter_rejects_wrong_build_identity_and_unknown_layout, test_gpt_t_image_set_rejects_file_replaced_by_symlink_after_tree_scan, and test_gpt_t_image_set_publication_never_replaces_concurrent_empty_directory.

These are source-level test references only; they do not assign meanings to absent canonical gate definitions.

## Known limits and baseline exceptions

The four documented M0 failures remain unchanged: generated image artifacts are absent for boot/image checks; fakeroot integration is blocked by the sandbox socket restriction; and the prepared-copy execute bit prevents the profile test from reaching its intended check. They remain baseline exceptions, not passes.

There was no Debian build because the prepared Bookworm base and pinned external inputs are absent and root/network use is forbidden. Real image comparison, firmware provenance/signature chain, real SSH verification, real EDL identity/readback, and actual boot remain unverified. Production package generation from complete external inputs is blocked. The standalone install-package test uses only a synthetic legacy GPT-B fixture and fake EDL; it recorded no fastboot call. The integrated wizard first-install path is blocked at the GPT-T `HARDWARE_REQUIRED` refusal; M7 does not demonstrate successful stock-to-Debian first-install or real boot. M5/M7 byte-identity results apply only to the fake eMMC fixture. The initial worktree was dirty and remains uncommitted because `.git` is read-only. Current branch and HEAD are `installer` and `da4f9c8a2770b11b181611fe5ff525cd2d1be197`; no M5, M6, M7, or docs-only commit was created.

## Owner-supervised hardware acceptance plan

Perform this only outside the unattended offline session, with the owner present and after recovering the missing spec/acceptance inputs. No physical-device operation was attempted here.

1. Confirm the exact board silkscreen/revision and MSM8916 identity. Record tool-reported EDL serial, hardware identity, and independently read fsg/sec/fsc fingerprints; compare all against the verified per-device backup. Do not infer identity across EDL/fastboot transitions without separately supported evidence.
2. Establish provenance and hashes for the exact Debian Bookworm base, kernel APK, BusyBox, DTB, bootloader/Path A blobs, original paired GPTs, and generated GPT-T tables. Compare the real build output and M0 baseline where validated inputs exist.
3. Review a complete verified backup and independent offline copy. Verify that calibration/identity partitions stay device-specific and that no package omits required recovery data.
4. Only after an independently validated GPT-T writer/readback path exists, have the owner supervise the original Android-to-Debian cycle. Review the GPT-T mapping and every phase, require dry runs and typed confirmations, verify bootloader mode and partition geometry, and require readback before recommending a boot.
5. Observe the real Debian boot and verify over SSH: firewall state/syntax, watchdog, modem, NTP, Wi-Fi, kernel and rootfs versions. Capture direct evidence; absent services remain SKIP/FAIL.
6. Restore the same unit to Android using its verified original backup and the existing rollback plan with per-phase confirmations. Re-read the full device and require exact full-disk SHA-256 equality with the original backup before recommending boot.
7. Boot the restored Android system and record the result. Preserve the private backup and logs offline; do not distribute calibration data.

This is a future, owner-supervised hardware acceptance plan, not evidence of a completed test. The GPT-T boot/userdata writer and integrated first-install E2E remain implementation blockers. The synthetic wizard restore E2E passed, but neither physical restore nor physical device flash or real boot was attempted in this work.

## Resumed M6 completion update (2026-11-15)

The canonical files were recovered one level above this checkout: `../spec/INSTALLER-SPEC.md` and `../spec/ACCEPTANCE.md`. Earlier statements in this handoff that they were absent are superseded. G1-G9 are now mapped to source and negative-test evidence in INSTALLER-REPORT.md.

M6 state-path bypass was already fixed and covered by a fake-EDL interrupted-write test. The missing completed-data verification has now been added: standalone restore hashes every completed data partition against the verified full backup before any subsequent phase. A negative fake-disk regression corrupts `userdata` while state claims completion, attempts `cache`, and verifies refusal with no actual write. Independent review confirmed that the test reaches the new check and the implementation is sound. M6 also now proves package creation refuses an output inside a real temporary Git worktree and verifies the archive README carries the SENSITIVE marking.

Latest results: M4 23/23, M5 22/22, M6 synthetic package checks pass, M7 14/14; full installer regression runner 26 pass, 0 fail, 0 top-level skips. All 68 shell scripts pass `bash -n`; all 16 installer/test Python files pass `py_compile`; `git diff --check` passes. A broad compile over every repository Python file reaches two untouched Python-2-era files and fails there; those are `src/lk2nd/platform/at91sam7/mkboard.py` and `src/lk2nd/scripts/aboot_test.py`. ShellCheck remains unavailable. The only file above 5 MiB found by the tree-wide size scan is the pre-existing nested `src/linux-dts/.git/index`; no added or changed file exceeds the limit.

Status remains PARTIAL_OFFLINE. GPT-T first-install is intentionally fail-closed with HARDWARE_REQUIRED, production package inputs and hardware acceptance remain unavailable, and all resumed work is uncommitted because Git metadata is read-only. No hardware, sudo, network, or destructive operation occurred.
