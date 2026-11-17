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
- The generated one-call restore inspector remains a single invocation and already validates its Sahara identity. Package install delegates to the flash wrapper for identity reads, then runs the proven Path A shell engine with `EDL_CMD` directly. That engine process does not share the Python `Session` bus/device guard. Extending the guard across that boundary requires an EDL command proxy; this remains UNFIXED and is reported to the orchestrator for an ownership decision. No proven engine was edited.

### R3 tests

- `test_flash_identity_ranges_share_one_session_and_reuse_identity` failed against the parent `flash.py` with `_live_identity` rejecting the `session` argument, then passed after the shared-session change.
- `test_verify_restore_full_read_and_identity_ranges_share_sessions` failed before the verify/restore changes because `_live` rejected the `session` argument, then passed.
- `test_generated_restore_runner_reuses_identity_across_edl_calls` failed before the package runner change because two calls captured no session identity, then passed.
- `test_generated_restore_runner_blocks_board_swap_before_later_call` changes the fixture USB devnum after the first invocation and proves the next client call is rejected before process execution.
- Full focused session suite after R3: 7/7 passed. Production and focused test Python compilation passed; `git diff --check` passed.

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
- Package install's proven shell engine does not share the parent Python bus/devnum guard and remains UNFIXED pending an orchestrator decision on a safe proxy boundary.
- Hardware/client behavior remains NEEDS-HARDWARE.

## Secret scan

SCAN-HIT FAKE-FIXTURE: the new focused test contains the plan's synthetic EDL serial fixture. No real identifiers were introduced. The final patch scan remains for integration.
