# WS-B findings

## Findings handled

- F-14: `_inside_git` still walks parent directories without invoking Git and fails closed on marker inspection errors. It honors `GIT_CEILING_DIRECTORIES` by stopping before inspecting a listed directory. Tests cover a foreign marker above the ceiling, `.git` file and directory markers inside the walk, a symlink into a repository, no-ceiling fail-closed behavior, and no Git subprocess. Commit: `056ea30`. Status: VERIFIED-WITH-SHIMS.
- F-01: All stage argument builders include their public API fields, and failed doctor stages can be retried. A public `openstick wizard` subprocess test now initializes state through the ceiling, executes the doctor stage, then attempts backup with the fake EDL available. It confirms doctor passes the fake EDL check but fails for the absent pinned loader and USB access; wizard correctly refuses backup because doctor did not complete. This exercises the public namespace path but does not satisfy the required successful doctor-then-fake-EDL-backup flow. UNFIXED for that end-to-end proof; M7 remains PARTIAL. Commits: `b5daf11`, `7729bc3`, `8355e88`.
- F-08 (CLI help portion): Top-level `package` help states packaged write scripts are UNVALIDATED on hardware; install and restore help state packaged writes require typed confirmation. Covered in `test_cli_store_home_precedence_and_package_output_alias`. Commit: `c154010`.
- F-12: `--home`, `OPENSTICK_HOME`, `XDG_DATA_HOME`, and default home precedence are implemented. The root-level `registry.json` and `events.jsonl` layout is documented. The registry structure was not migrated. Commit: existing `c056761`.
- F-16: The existing CLI maps only `VerificationError` to exit 3, catches unknown exceptions with a one-line type-only message and exit 1, and prints tracebacks only with `--debug`. `_redact` is absent and docs accurately state arbitrary exception messages are not redacted. Tests: `test_cli_unknown_exception_is_one_line_and_exit_one`, `test_cli_only_specific_verification_errors_exit_three`.
- F-20: `--out` aliases `package ... --output`, and docs list other CLI flag differences. Covered by `test_cli_store_home_precedence_and_package_output_alias`. Commit: existing `c056761`.

## Tests and checks

- `python3 tests/verify-installer-m7.py`: 25 tests passed. Added/updated coverage includes `test_git_ceiling_allows_private_store_above_foreign_marker`, `test_git_ceiling_still_blocks_markers_inside_walk_and_symlink_into_repo`, `test_git_ceiling_without_environment_keeps_foreign_marker_fail_closed`, `test_cli_wizard_dispatches_doctor_and_rejects_backup_after_failed_doctor`, `test_every_stage_argument_builder_supplies_public_api_fields`, `test_wizard_failed_stage_can_be_retried`, and the CLI exit-code/package-help tests.
- Targeted ceiling tests: 3 passed. Targeted package alias/help test: 1 passed. Targeted CLI wizard subprocess test: 1 passed.
- `python3 -m py_compile tools/installer/cli.py tools/installer/store.py tools/installer/wizard.py tests/verify-installer-m7.py`: passed.
- `bash -n`: no shell files were touched.
- `tests/run-installer-tests.sh` with a throwaway TMPDIR, `GIT_CEILING_DIRECTORIES` set to that temp root, and the fake shim PATH reached M5 and failed one case: 21/22 M5 tests passed. `test_restore_final_read_timeout_is_exit3_and_blocks_boot_recommendation` expects exit 3, while the A-owned `verify_restore.py` currently raises generic `ValueError`, which the CLI maps to exit 1 under F-16. This is recorded for Phase 2 to change to the shared specific verification exception. M4 passed all 23 tests; the runner did not report a total after its M5 failure.

## Open and offline limitations

- F-01 required non-mock wizard proof remains UNFIXED: the public doctor-to-backup subprocess cannot pass doctor because the pinned Firehose loader is absent and USB access is unavailable. The fake EDL shim is present and doctor detects it. Production doctor or safety gates were not weakened. The successful backup path and flash refusal were not shown in one real-CLI wizard chain.
- M7 remains PARTIAL. No real device, hardware, sudo, network, or secret was used.
- M5 exit-code integration needs the A-owned restore path to raise the shared specific verification exception rather than generic `ValueError`.
