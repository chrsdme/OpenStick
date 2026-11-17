# WS-M: rollback helper failure detail

Implemented R-T6. A failing existing rollback helper now appends its final non-empty stderr line to the refusal message. The line is serial-masked, absolute paths are replaced with `[path]`, and the final refusal remains a single line.

Tests added or run:

- `test_helper_failure_appends_masked_stderr_without_external_paths` passed with a synthetic helper executable that writes a fake serial and an outside-store path to stderr.
- Test-first check: the new test failed against the base `verify_restore.py` because the refusal omitted the helper stderr; it passed after the change.
- `python3 -m py_compile tools/installer/verify_restore.py tests/verify-installer-m5.py` passed.
- `GIT_CEILING_DIRECTORIES=/tmp python3 tests/verify-installer-m5.py` ran 26 tests; 25 passed and the known R-T5 timeout case failed because the test does not inject fixture sysfs. WS-L owns that fix.

No real hardware, EDL client, sudo, or network was used. This work verifies only the synthetic helper error formatting. The full acceptance matrix remains for integration after WS-K and WS-L are merged.
