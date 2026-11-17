# WS-K: runner and guards

## Findings handled

- R-T1: VERIFIED-WITH-SHIMS. The runner re-execs under `env -i` with HOME, PATH, LANG, and a fresh TMPDIR. After startup it sets a harness-owned `GIT_CEILING_DIRECTORIES` to the temp directory's parent, along with fixture USB roots and test state paths. The guard confirmed caller `OPENSTICK_EDL_DIR`, `OPENSTICK_HOME`, other `GIT_*`, and PATH entries are discarded while the expected ceiling remains.
- R-T2: VERIFIED-WITH-SHIMS. The Python host path guard rejects EDL client paths and loader paths outside TMPDIR. The guard runs in child interpreters through `sitecustomize`; its regression check covers an outside client and a child process with an outside loader. The full M2 EDL runner group passed.
- R-T3: PARTIAL. Each runner group has a 300 second timeout and receives `/dev/null` stdin. Subprocess calls added to the runner's inline cases use explicit stdin and timeout. The remaining test files still need the suite-wide subprocess audit assigned to WS-L.
- R-T5: VERIFIED-WITH-SHIMS. The runner exports fixture sysfs and USB device roots; CLI, EDL session defaults, and packaged restore entry points use them. The default-root doctor and child preflight checks passed under the host path guard.

## Tests run

- Hostile environment full suite: 22 PASS / 7 FAIL / 0 SKIP. The runner environment, path guard, default fixture roots, M2 group, and M3 group passed. The remaining failures include the M4 temp cleanup count and issues assigned to WS-L; this is not a complete suite pass.
- `bash -n tests/run-installer-tests.sh`: PASS.
- `python3 -m py_compile` on touched Python files: PASS.
- `git diff --check`: PASS.

## Remaining limits

- No real hardware was used. Device detection, USB access, and EDL commands were exercised only with fixtures and shims.
- R-T3 remains PARTIAL until the suite-wide subprocess call-site audit and final matrix are complete.
- The M4 temp cleanup failure and other suite failures remain for integration.
