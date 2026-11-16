# OpenStick installer verification report

This report reflects the merged `installer` tree after the Phase 2 integration run. Tests used fake EDL, SSH, fastboot, and build shims. No hardware, sudo, network, real image, or real backup was used.

## Milestones

| Milestone | Commit | Status |
|---|---|---|
| M0 Baseline and design | `de86b3d` | PARTIAL |
| M1 Skeleton, store, doctor, shims | `32b411b` | VERIFIED-WITH-SHIMS |
| M2 Backup and verification | `16a9ac2` | VERIFIED-WITH-SHIMS |
| M3 Configure, seed, injection | `5b70d1f` | VERIFIED-WITH-SHIMS |
| M4 Build and flash wrappers | `da4f9c8` | PARTIAL |
| M5 Verify and restore | `8593ca0` | VERIFIED-WITH-SHIMS |
| M6 Packages | `8593ca0` | VERIFIED-WITH-SHIMS |
| M7 Wizard and docs | `8593ca0` | PARTIAL |

The required merged branch order was B, A, C, D. Merge commits are `1a5bf0b`, `c11522e`, `3d9cbe3`, and `d0eb749`. Phase 2 integration fixes are in `245af35`; the isolated package hash regression is in `2118b07`. The `baseline` tag remains at `e4cf01b`. M5, M6, and M7 remain grouped in the existing checkpoint commit; history was not rewritten.

The final aggregate runner result was **26 PASS, 0 FAIL, 0 SKIP**. Standalone results: M3 **15/15**, M4 **25/25**, M5 **25/25**, M6 composite assertion suite **PASS**, M7 **25/25**. M6 is an assert-based composite script and emits no numeric case total. Nested optional legacy checks reported their own skips for unavailable cross compiler, container-backed nft check, or real backup data.

## Feature evidence

### CLI, private store, and doctor

**Tested with shims:** `test_git_ceiling_allows_private_store_above_foreign_marker`, `test_git_ceiling_still_blocks_markers_inside_walk_and_symlink_into_repo`, `test_git_ceiling_without_environment_keeps_foreign_marker_fail_closed`, `test_cli_unknown_exception_is_one_line_and_exit_one`, `test_cli_only_specific_verification_errors_exit_three`, and the aggregate runner's store and doctor checks.

**Needs real hardware:** Actual USB permissions, installed EDL client output, and pinned loader availability. On this host, doctor reports the loader and USB prerequisites unavailable.

### Backup, verify, and restore

**Tested with shims:** `backup-success-and-integrity`, `backup-failures-cleanup`, `test_restore_fake_disk_is_byte_identical_and_records_each_phase`, `test_restore_final_read_timeout_is_exit3_and_blocks_boot_recommendation`, `test_restore_final_read_checks_size_even_when_hash_matches`, and the M5 suite. The final-read timeout raises the shared `VerificationError`, maps to exit 3, persists the failed state, and blocks boot guidance.

**Needs real hardware:** EDL protocol compatibility, device-specific ranges, real flash read/write behavior, USB reliability, and physical restore/readback.

### Configure, seed, and injection

**Tested with shims:** M3's 15 cases, including `seed-apply-transaction-and-best-effort-tools`, `seed-profile-policy`, `seed-network-values-base64-roundtrip`, `seed-shell-network-values-roundtrip-and-keyfile-escaping`, `debugfs-copy-injection-and-readback`, and `seed-push-refuses-unavailable-ssh`. Seed inject and push events record a seed hash, time, and target without storing seed contents in registry events.

**Needs real hardware:** Real boot ordering, systemd behavior, NetworkManager keyfile acceptance, modem behavior, and SSH transfer to a device.

### Build and image sets

**Tested with shims:** M4's 25 cases, including `test_build_generic_and_legacy_env_order_and_permissions`, `test_common_config_isolation_and_legacy_precedence`, the build argv and root-refusal checks, and GPT-T image-set hash and layout validation.

**Needs real hardware:** A rootful image build with the prepared base tarball and pinned inputs, boot behavior, filesystem content on the target image, and device acceptance of GPT-T.

### Flash and restore gates

**Tested with shims:** `test_flash_identity_check_reads_only_backup_identity_ranges`, `test_flash_uses_one_edl_command_for_identity_and_engine`, `test_flash_wrong_confirmation_and_noninteractive_dry_only`, `test_flash_dry_confirmation_protocol_and_failure_stop`, and the M6 portable install scenario. The portable install writes only the two GPT ranges in its fake target, then stops because post-GPT identity LBAs are not hardware-validated. The GPT-T first-install path refuses with `HARDWARE_REQUIRED`.

**Needs real hardware:** Post-GPT identity ranges, EDL/fastboot identity binding, loader compatibility, real phase readback, and any GPT-T first-install validation. M4 remains PARTIAL and the stop-after-GPT guard remains enabled.

### Packages

**Tested with shims:** `verify-installer-m6.py` completed with `M6 synthetic package checks PASS`; named checks include `verify_build_safety`, `test_common_rejects_empty_openstick_out`, `test_verify_package_requires_sensitive_marking`, and the fake install/restore package scenarios. The installed package scripts warn that writes are UNVALIDATED on hardware and require typed confirmation.

**Needs real hardware:** Production packages from verified external inputs, hardware provenance, and physical install or restore. Package install and restore remain explicitly UNVALIDATED on hardware.

### Wizard

**Tested with shims:** M7's 25 cases, including `test_cli_wizard_dispatches_doctor_and_rejects_backup_after_failed_doctor`, `test_every_stage_argument_builder_supplies_public_api_fields`, `test_wizard_failed_stage_can_be_retried`, and `test_wizard_restore_runs_guarded_fake_edl_and_requires_full_disk_match`.

**Needs real hardware:** A successful non-mock wizard doctor-then-backup flow and full stock-to-install-to-verify-and-restore acceptance. The doctor stage correctly fails here because the host lacks the pinned loader and USB access; the following backup is refused. F-01 E2E has not passed. M7 remains PARTIAL.

## Findings F-01 to F-22

| Finding | Status | Evidence test or reason |
|---|---|---|
| F-01 | NEEDS-HARDWARE | The successful end-to-end doctor-then-backup run requires the pinned loader and USB access. The CLI regression `test_cli_wizard_dispatches_doctor_and_rejects_backup_after_failed_doctor` verifies that missing prerequisites stop safely; it is not the successful end-to-end run. |
| F-02 | PARTIAL | `test_gpt_t_image_set_tampering_is_detected_and_flash_stops_hardware_required`; GPT-T writing remains refused pending hardware validation. |
| F-03 | PARTIAL | `test_flash_identity_check_reads_only_backup_identity_ranges`; M6 fake install confirms strict stop after GPT. Post-GPT identity ranges need hardware evidence. |
| F-04 | VERIFIED-WITH-SHIMS | Aggregate runner and final report audit; this report replaces the prior diary and records current commits and counts. |
| F-05 | PARTIAL | `git log` and `refs/tags/baseline` audit; baseline tag exists, but M5 to M7 remain in the checkpoint commit. No history rewriting was allowed. |
| F-06 | VERIFIED-WITH-SHIMS | `seed-apply-transaction-and-best-effort-tools`; real service boot ordering still needs a device. |
| F-07 | VERIFIED-WITH-SHIMS | `seed-profile-policy`; dev/prod seed and image profiles are checked. |
| F-08 | VERIFIED-WITH-SHIMS | `test_verify_package_requires_sensitive_marking`; package write scripts identify themselves as UNVALIDATED on hardware. |
| F-09 | VERIFIED-WITH-SHIMS | `seed-network-values-base64-roundtrip`, `seed-shell-network-values-roundtrip-and-keyfile-escaping`. |
| F-10 | VERIFIED-WITH-SHIMS | `test_flash_uses_one_edl_command_for_identity_and_engine`; fake EDL confirms the selected executable is shared and shown. |
| F-11 | VERIFIED-WITH-SHIMS | `test_flash_refusals_and_input_hashes`; unsupported boards are always refused and the absent bypass is documented. |
| F-12 | PARTIAL | M3 seed push and injection registry assertions; seed history is recorded, while the registry layout remains root-level and USB aliases are not populated. |
| F-13 | VERIFIED-WITH-SHIMS | Scratch-copy mutants were killed for restore TTY guard, `sudo env -i`, root refusal, SENSITIVE marking, non-GPT package member hash, and typed flash confirmation. Tests include `test_restore_confirmation_requires_interactive_terminal`, `verify_build_safety`, `test_verify_package_requires_sensitive_marking`, the M6 modified-member case, and `test_flash_wrong_confirmation_and_noninteractive_dry_only`. |
| F-14 | VERIFIED-WITH-SHIMS | Three `test_git_ceiling_*` cases; parent walking remains fail-closed and respects Git ceiling directories. |
| F-15 | VERIFIED-WITH-SHIMS | `seed-apply-transaction-and-best-effort-tools` and `seed-push-refuses-unavailable-ssh`; temporary paths use private generated directories and reject symlinks. |
| F-16 | VERIFIED-WITH-SHIMS | `test_cli_unknown_exception_is_one_line_and_exit_one`, `test_cli_only_specific_verification_errors_exit_three`. |
| F-17 | VERIFIED-WITH-SHIMS | `FIX-ROUND-1-EXCLUDE.txt` process files, copied spec, and tracked agent notes are excluded from this product tree. |
| F-18 | VERIFIED-WITH-SHIMS | Aggregate runner checks that `openstick-m4-test-*` count does not grow under its temporary directory. |
| F-19 | VERIFIED-WITH-SHIMS | README, FLASH-PLAN, and installer docs label remaining GPT-B references as legacy and point to GPT-T. |
| F-20 | VERIFIED-WITH-SHIMS | `test_cli_store_home_precedence_and_package_output_alias`; `--out` is supported and remaining CLI differences are documented. |
| F-21 | VERIFIED-WITH-SHIMS | `test_build_dry_run_default_login`; generic dev defaults are documented and shown by dry run. |
| F-22 | VERIFIED-WITH-SHIMS | Closed by owner decision. Owner-private deny-list stays outside the repository; it was not read, copied, quoted, or applied. |

## Limitations and tree scope

- No hardware, sudo, network, real EDL, real SSH, or production image was used.
- F-01 remains UNFIXED. The CLI wizard test proves the prerequisite failure is handled and backup is refused, not that successful end-to-end backup works.
- M4 and M7 are PARTIAL. GPT-T first-install and complete wizard end-to-end acceptance lack non-mock hardware evidence.
- M6 package install and restore remain UNVALIDATED on hardware.
- Existing `tools/flash/`, `tools/pathA/`, rollback engines, initramfs code, and the protected flash scripts were not changed by this round.
- Process artifacts listed in `../FIX-ROUND-1-EXCLUDE.txt`, including the worktree copy of `spec/` and `notes/`, are excluded from product integration.
