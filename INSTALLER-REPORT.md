# OpenStick installer verification report

This report reflects the merged `installer` tree after hardware fix round 1. Tests use fake EDL, SSH, fastboot, and build shims. No EDL client, sudo, network, real image, or real backup was used. During an earlier M7 validation, the doctor stage read host sysfs and queried device-node permission metadata; it did not run EDL or open, read, or write the device node. The aggregate runner was not rerun after that was identified.

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

The prior installer work merged branches B, A, C, D in order with merge commits `1a5bf0b`, `c11522e`, `3d9cbe3`, and `d0eb749`. Phase 2 integration fixes are in `245af35`; the isolated package hash regression is in `2118b07`. Hardware fix round 1 merged E, F, G in order with merge commits `a8b9ffc`, `45f3cbd`, and `54268f8`, from work commits `79bf49f` through `1911983`. The `baseline` tag remains at `e4cf01b`; history was not rewritten.

The pre-hardware-round aggregate runner result was **26 PASS, 0 FAIL, 0 SKIP**. After hardware fix round 1, the aggregate runner was withheld because its M7 doctor invocation inspects host sysfs and `/dev`. Focused synthetic suites passed: M2 **10/10**, doctor **10/10**, M3 **15/15**, M4 **25/25**, M5 **25/25**, and M6 composite package checks **PASS**. M7 **25/25** was recorded before the host-device check restriction and was not rerun. M6 is an assert-based composite script and emits no numeric case total.

## Test hardening round 4

The round started from `315ab96`. The required EOF-only base commit is `f6284ca`, tagged `round4-base`; the implementation integration merge is `38bedd3`, followed by this report-only update. All six workstreams below were tested with synthetic fixtures and shims. No real hardware, EDL device, `sudo`, network, production image, or real backup was used.

| Requirement | Status | Evidence |
|---|---|---|
| R-T1 clean runner environment | VERIFIED-WITH-SHIMS | Matrix cases (a), (b), and (c) all passed. Hostile `OPENSTICK_*`, `GIT_*`, and PATH values were discarded. |
| R-T2 client and loader path guard | VERIFIED-WITH-SHIMS | The guard blocks out-of-temp EDL clients and loaders in direct and Python child invocations. Planner metadata placeholders are not treated as EDL invocations. |
| R-T3 bounded tests | VERIFIED-WITH-SHIMS | Runner groups have a 300-second timeout and `/dev/null` stdin. Noninteractive test subprocesses have explicit timeouts and `DEVNULL`; intentional PTY confirmation helpers use explicit deadlines. |
| R-T4 Git layout independence | VERIFIED-WITH-SHIMS | M7 creates its own marker; M5 and other store tests set their own ceilings. The copy run in case (e) used the runner ceiling described below. |
| R-T5 fixture USB roots | VERIFIED-WITH-SHIMS | Matrix runs passed with fixture sysfs and USB roots exported to parent and child processes. M5's timeout regression injects its synthetic EDL sysfs root. |
| R-T6 helper failure message | VERIFIED-WITH-SHIMS | `test_helper_failure_appends_masked_stderr_without_external_paths` checks the final stderr line, serial masking, and path removal. |

| Acceptance case | Result | Details |
|---|---|---|
| (a) regular runner invocation | 25 PASS / 0 FAIL / 0 SKIP | Passed with caller stdin redirected to `/dev/null`. |
| (b) allowlisted `env -i` | 25 PASS / 0 FAIL / 0 SKIP | Passed with `HOME`, `PATH`, `LANG`, and a workspace-local temp parent. |
| (c) hostile environment | 25 PASS / 0 FAIL / 0 SKIP | Passed with hostile EDL directory, home, Git ceiling, and a PATH `edl` stub. The stub's execution sentinel remained absent. |
| (d) pseudo-terminal stdin | 25 PASS / 0 FAIL / 0 SKIP | Passed using Python `pty`. |
| (e) copy without `.git` in the tree | 25 PASS / 0 FAIL / 0 SKIP | The copy excluded `.git` and passed with a Git ceiling that hid the outer workspace marker. A physical copy with no `.git` anywhere above it was not possible inside the requested workspace path because the workspace root is itself a Git worktree. This is a simulated no-Git layout. |
| (f) Git worktree | 25 PASS / 0 FAIL / 0 SKIP | Passed from the `installer` worktree. |

`python3 -m compileall -q tools tests`, `bash -n tests/run-installer-tests.sh`, and `git diff --check round4-base..installer` passed. `shellcheck` was unavailable. The patch scan reported `SCAN-HIT class=FAKE-FIXTURE` for the synthetic serial fixture in `tests/verify-installer-m5.py`; no private key block, real-looking secret, or unknown secret was found.

## Feature evidence

### CLI, private store, and doctor

**Tested with shims:** `test_git_ceiling_allows_private_store_above_foreign_marker`, `test_git_ceiling_still_blocks_markers_inside_walk_and_symlink_into_repo`, `test_git_ceiling_without_environment_keeps_foreign_marker_fail_closed`, `test_cli_unknown_exception_is_one_line_and_exit_one`, `test_cli_only_specific_verification_errors_exit_three`, and `tests/verify-installer-doctor.py` (10/10).

**Needs real hardware:** Actual EDL client output, pinned loader acceptance, and USB permissions on the target. Fixture tests cover USB device present with and without access, absent device, and resolver behavior.

### Backup, verify, and restore

**Tested with shims:** M2 10/10, including `test_backup_identity_uses_sahara_serial_and_required_loader_flags`, `test_parse_sahara_info_accepts_transcript_and_rejects_malformed_or_absent_blocks`, and `test_hang_sends_sigint_reports_power_cycle_and_does_not_register_backup`; M5 25/25, including `test_restore_fake_disk_is_byte_identical_and_records_each_phase`, `test_restore_final_read_timeout_is_exit3_and_blocks_boot_recommendation`, and `test_restore_final_read_checks_size_even_when_hash_matches`. Backup identity uses the Sahara block from a short GPT-range read; full backup and restore reads use named long timeouts. Timeout failures interrupt the client, terminate it after the grace period, and block registration or boot guidance.

**Needs real hardware:** EDL protocol compatibility, device-specific ranges, real flash read/write behavior, USB reliability, and physical restore/readback.

### Configure, seed, and injection

**Tested with shims:** M3's 15 cases, including `seed-apply-transaction-and-best-effort-tools`, `seed-profile-policy`, `seed-network-values-base64-roundtrip`, `seed-shell-network-values-roundtrip-and-keyfile-escaping`, `debugfs-copy-injection-and-readback`, and `seed-push-refuses-unavailable-ssh`. Seed inject and push events record a seed hash, time, and target without storing seed contents in registry events.

**Needs real hardware:** Real boot ordering, systemd behavior, NetworkManager keyfile acceptance, modem behavior, and SSH transfer to a device.

### Build and image sets

**Tested with shims:** M4's 25 cases, including `test_build_generic_and_legacy_env_order_and_permissions`, `test_common_config_isolation_and_legacy_precedence`, the build argv and root-refusal checks, GPT-T image-set hash and layout validation, `test_flash_identity_check_reads_only_backup_identity_ranges`, and `test_flash_uses_one_edl_command_for_identity_and_engine`.

**Needs real hardware:** A rootful image build with the prepared base tarball and pinned inputs, boot behavior, filesystem content on the target image, and device acceptance of GPT-T.

### Flash and restore gates

**Tested with shims:** M4 25/25, M5 25/25, and M6 synthetic package checks. Direct identity and range reads, restore verification, and packaged restore operations now pass through the shared EDL runner or a self-contained generated package adapter with the verified absolute loader and `--memory=emmc`. Engine invocations log their command and supply the resolved client and loader. The portable install writes only the two GPT ranges in its fake target, then stops because post-GPT identity LBAs are not hardware-validated. The GPT-T first-install path refuses with `HARDWARE_REQUIRED`.

**Needs real hardware:** Post-GPT identity ranges, EDL/fastboot identity binding, loader compatibility, real phase readback, and any GPT-T first-install validation. M4 remains PARTIAL and the stop-after-GPT guard remains enabled.

### Packages

**Tested with shims:** `verify-installer-m6.py` completed with `M6 synthetic package checks PASS`; named checks include `verify_build_safety`, `test_common_rejects_empty_openstick_out`, `test_verify_package_requires_sensitive_marking`, and the fake install/restore package scenarios. The installed package scripts warn that writes are UNVALIDATED on hardware and require typed confirmation.

**Needs real hardware:** Production packages from verified external inputs, hardware provenance, and physical install or restore. Package install and restore remain explicitly UNVALIDATED on hardware.

### Wizard

**Tested with shims:** M7's previously recorded 25 cases, including `test_cli_wizard_dispatches_doctor_and_rejects_backup_after_failed_doctor`, `test_every_stage_argument_builder_supplies_public_api_fields`, `test_wizard_failed_stage_can_be_retried`, and `test_wizard_restore_runs_guarded_fake_edl_and_requires_full_disk_match`. The standalone M7 suite was not rerun after the host-device check restriction.

**Needs real hardware:** A successful real-client wizard doctor-then-backup flow and full stock-to-install-to-verify-and-restore acceptance. The CLI regression covers safe refusal when prerequisites are missing, but no successful non-mock EDL session has been run. M7 remains PARTIAL.

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

## Hardware findings H-01 to H-09

| Finding | Status | Evidence |
|---|---|---|
| H-01 | VERIFIED-WITH-SHIMS | `test_backup_identity_uses_sahara_serial_and_required_loader_flags`; backup obtains Sahara identity from a short GPT range read instead of `getstorageinfo`. |
| H-02 | VERIFIED-WITH-SHIMS | `test_missing_or_unverified_loader_is_refused_before_client_call` and `test_backup_identity_uses_sahara_serial_and_required_loader_flags`; client calls require the verified absolute loader and `--memory=emmc`. |
| H-03 | VERIFIED-WITH-SHIMS | `test_hang_sends_sigint_reports_power_cycle_and_does_not_register_backup`; timeout interrupts, waits, then terminates and refuses registration. |
| H-04 | VERIFIED-WITH-SHIMS | `test_parse_sahara_info_accepts_transcript_and_rejects_malformed_or_absent_blocks`; flash and restore range reads use the same Sahara parsing format. |
| H-05 | VERIFIED-WITH-SHIMS | `tests/verify-installer-doctor.py` (10/10), including `test_doctor_output_uses_shared_resolvers_and_specific_hints`; doctor uses the shared client and loader resolvers. |
| H-06 | VERIFIED-WITH-SHIMS | `test_usb_present_with_access_uses_device_node_path`, `test_usb_present_without_access_fails_with_narrow_udev_hint`, and `test_usb_absent_is_info_and_does_not_fail_doctor`; cases use fixture sysfs and injected access callbacks. |
| H-07 | VERIFIED-WITH-SHIMS | `test_no_loader_shim_mode_matches_transcript_and_exits_cleanly_on_sigint` and `test_shim_fails_calls_missing_required_flags`; shim reproduces the synthetic Sahara and Firehose formats and rejects missing flags. |
| H-08 | VERIFIED-WITH-SHIMS | `test_verbose_failure_includes_client_output_tail`; the M6 `run()` package integration assertions verify `SYNTHETIC EDL SIGINT OUTPUT TAIL` plus the recovery hint on timeout, and `SYNTHETIC EDL WRITE FAILURE DETAIL` plus the recovery hint on failed writes. |
| H-09 | VERIFIED-WITH-SHIMS | `test_doctor_failure_hints_are_specific_for_edl_loader_and_usb`; doctor hints identify the relevant EDL, loader, or USB next step. |

## Hardware acceptance checklist

Owner-run checklist. This round did not exercise it on a board.

1. In EDL mode, run `doctor`; confirm EDL client PASS, loader SHA-256 verified, and USB PASS with the device attached.
2. Run `--dry-run backup`; confirm identity is printed and no files are written.
3. Run `backup`; confirm the full read completes, GPT is verified, and fingerprints are recorded.
4. Compare the recorded `fsg`, `sec`, and `fsc` fingerprints with the owner's original dump slices.
5. Exit EDL only by cold boot.

Do not perform writes during this acceptance checklist.

## Limitations and tree scope

- No EDL command, sudo, network, real SSH, or production image was used. One earlier M7 invocation ran doctor against the host and read sysfs plus device-node permission metadata; it did not invoke the client or open/read/write the device node. The aggregate runner was not rerun after this host check was identified.
- F-01 remains UNFIXED. The CLI wizard test proves the prerequisite failure is handled and backup is refused, not that successful end-to-end backup works.
- M4 and M7 are PARTIAL. GPT-T first-install and complete wizard end-to-end acceptance lack non-mock EDL evidence.
- After hardware fix round 1, the aggregate runner was withheld because its M7 doctor check inspected host sysfs and `/dev`. Round 4 added fixture USB roots and child-process guards, then passed the six matrix runs above with shims. No real hardware behavior was verified.
- M6 package install and restore remain UNVALIDATED on hardware.
- Existing `tools/flash/`, `tools/pathA/`, rollback engines, initramfs code, and the protected flash scripts were not changed by this round.
- Process artifacts listed in `../FIX-ROUND-1-EXCLUDE.txt`, including the worktree copy of `spec/` and `notes/`, are excluded from product integration.
