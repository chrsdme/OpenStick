# WS-H: EDL session state and backup behavior

## Changes

- Added injectable USB sysfs preflight, typed session exceptions, and `Session` in `tools/installer/edl_runner.py`.
- A session captures the first Sahara identity, accepts subsequent firehose invocations, and checks the original bus/device number before later invocations when sysfs provides those values.
- ABSENT, NORMAL-MODE, CONSUMED, HUNG, and board-swap cases have specific exceptions. If sysfs is unavailable, the client output decides the state.
- Raised identity and range read timeouts to 60 seconds. The full-read timeout remains 3600 seconds. Timeout handling still sends SIGINT before terminate.
- `backup` now uses one session for its identity and full-read calls, masks serials in ordinary console output, supports `--show-serial`, and prints the required session-used note after any EDL invocation, including refusals.
- `cli.main` accepts Python-level sysfs and device-root injection so tests can stay off host USB paths. No new environment variable was added.
- GPT-T first-install refusal and flash stop-after-GPT behavior were not changed.

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
- Backup dry-run refusal, backup serial masking, and session-used note need the WS-I integration tests against the stateful shim before they can be marked VERIFIED-WITH-SHIMS.
- R3 still needs an orchestrator decision for other call sites. `tools/installer/flash.py` and `tools/installer/verify_restore.py` call `edl_runner.run` directly for multiple invocations rather than sharing `Session`; generated package runners also embed direct per-call runner code. These are outside WS-H ownership and should be audited before final acceptance.
- Hardware/client behavior remains NEEDS-HARDWARE.

## Secret scan

SCAN-HIT FAKE-FIXTURE: the new focused test contains the plan's synthetic EDL serial fixture. No real identifiers were introduced. The final patch scan remains for integration.
