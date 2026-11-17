# WS-H: EDL session state and backup behavior

## Changes

- Added injectable USB sysfs preflight, typed session exceptions, and `Session` in `tools/installer/edl_runner.py`.
- A session captures the first Sahara identity, accepts subsequent firehose invocations, and checks the original bus/device number before later invocations when sysfs provides those values.
- ABSENT, NORMAL-MODE, CONSUMED, HUNG, and board-swap cases have specific exceptions. If sysfs is unavailable, the client output decides the state.
- Raised identity and range read timeouts to 60 seconds. The full-read timeout remains 3600 seconds. Timeout handling still sends SIGINT before terminate.
- `backup` now uses one session for its identity and full-read calls, masks serials in ordinary console output, supports `--show-serial`, and prints the required session-used note after any EDL invocation, including refusals.
- `cli.main` accepts Python-level sysfs and device-root injection so tests can stay off host USB paths. No new environment variable was added.
- GPT-T first-install refusal and flash stop-after-GPT behavior were not changed.

## R3 caller follow-up

- `flash._live_identity` now uses one `Session` for the primary GPT identity read and all three fsg/sec/fsc reads.
- `verify_restore._live` shares one `Session` across its identity and full-disk reads. `_live_identity_only`, `_edl`, `_prepare_plan_phase`, and `_verify_final_disk` accept and reuse the command's session. Top-level restore passes the injected sysfs root from `cli.main`.
- The generated standalone restore runner now captures Sahara identity on its first EDL call, requires firehose markers on later calls, and preflights sysfs before every EDL invocation. Its Python `main(sysfs_root=...)` parameter provides test injection; direct CLI execution uses `/sys/bus/usb/devices`. No test-only environment variable or public CLI override was added.
- The generated one-call restore inspector remains a single invocation and validates its Sahara identity. The Python `Session` now supplies a private `EDL_CMD` proxy to flash's Path A shell engine and verify/restore fwlib phases. The proxy verifies the pinned loader and session identity, then checks the original sysfs bus/devnum before each EDL client call. It is outside the proven engine and no engine file was edited.
- Standalone generated package install uses the same flash proxy, with `/sys/bus/usb/devices` as its production default. Tests inject a synthetic sysfs root through the generated runner's Python `main(sysfs_root=...)` parameter; no public CLI override or test environment variable was added.
- `cli.main` wraps backup, flash, and restore dispatch in one session. Generated standalone restore/inspect/install runners print the exact session-used note after EDL calls, including refusals and exceptions. Other audited top-level commands do not invoke EDL: verify uses SSH; doctor, package creation, and image-set operations do not open a board connection. No remaining EDL caller lacking session identity/bus-devnum enforcement was found in the audited installer call graph.

### R3 tests

- `test_flash_identity_ranges_share_one_session_and_reuse_identity` failed against the parent `flash.py` with `_live_identity` rejecting the `session` argument, then passed after the shared-session change.
- `test_verify_restore_full_read_and_identity_ranges_share_sessions` failed before the verify/restore changes because `_live` rejected the `session` argument, then passed.
- `test_generated_restore_runner_reuses_identity_across_edl_calls` failed before the package runner change because two calls captured no session identity, then passed.
- `test_generated_restore_runner_blocks_board_swap_before_later_call` changes the fixture USB devnum after the first invocation and proves the next client call is rejected before process execution.
- `test_session_proxy_checks_board_swap_before_later_client_call` exercises the Path A `EDL_CMD` proxy against a stateful synthetic client and proves a changed devnum blocks the later call.
- Full focused session suite after R3: 7/7 passed. Production and focused test Python compilation passed; `git diff --check` passed.

### R2 note, proxy, and package follow-up

- `test_session_proxy_checks_board_swap_before_later_client_call` and `test_restore_fwlib_engine_uses_session_proxy` cover the Path A proxy and fwlib sequence with synthetic sysfs/client fixtures.
- `test_cli_restore_refusal_after_edl_emits_exact_session_note` fails against committed parent `cli.py` (the expected note is absent) and passes with session-scoped CLI dispatch. `test_backup_registry_refusal_emits_exact_session_note` verifies backup refusal behavior. `test_flash_identity_refusal_emits_exact_session_note` verifies the direct flash refusal note.
- `test_generated_restore_runner_reuses_identity_across_edl_calls` checks the exact note after its calls. Against committed parent `packages.py`, the test fails with `KeyError: 'finish_session'`; it passes after the standalone runner wrapper was added.
- Standalone package install delegates EDL invocations through flash's proxy and accepts a Python-only injected root, but its full package transaction is not exercised end-to-end by this focused suite. Mark package install integration PARTIAL pending a generated-package fixture test; the proxy boundary itself passes synthetic bus-swap tests. Hardware behavior remains NEEDS-HARDWARE.
- Focused session suite after proxy/note follow-up: 12/12 passed. `py_compile` for the five touched production/test Python modules and `git diff --check` passed.

## Tests and validation

- Added `tests/verify-installer-edl-session.py`.
- `test_preflight_classifies_absent_and_normal_without_client_invocation` and `test_preflight_captures_edl_bus_and_device_numbers` failed before implementation because `edl_runner.preflight` did not exist. They now pass.
- `test_edl_output_tail_masks_serial_by_default` checks the console diagnostic masking format.
- Focused suite: 3/3 passed.
- `python3 -m py_compile tools/installer/edl_runner.py tools/installer/cli.py`: passed.
- `git diff --check`: passed.
- Aggregate runner deferred until WS-I adds its real `/sys/bus/usb` and `/dev/bus/usb` access guard and hermetic fixtures. No hardware, sudo, network, host sysfs, or `/dev` checks were used.

## Status and remaining work

- Session preflight and board-swap checks: VERIFIED-WITH-SHIMS at unit/fixture level.
- R3 direct Python flows in flash and restore are VERIFIED-WITH-SHIMS by the synthetic stateful client tests.
- Backup dry-run refusal, backup serial masking, and session-used note need the WS-I integration tests against its full shim before they can be marked VERIFIED-WITH-SHIMS.
- Direct CLI, generated restore, Path A, and fwlib EDL paths use shared session identity and bus/devnum checks. Generated package install is PARTIAL because the complete transaction is not covered end-to-end, though it delegates through the tested proxy. Real USB behavior remains NEEDS-HARDWARE.
- Hardware/client behavior remains NEEDS-HARDWARE.

## Secret scan

SCAN-HIT FAKE-FIXTURE: matches are USB VID/PID constants and the synthetic test serial `deadbeef`; no real board identifier or credential was introduced. No unresolved secret-scan hit remains in this patch.
