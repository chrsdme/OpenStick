# WS-L test hardening

## Findings handled

- R-T3: non-interactive test subprocess helpers now supply `stdin=subprocess.DEVNULL` and a bounded timeout. The m6 interactive confirmation helpers keep their PTY input and enforce explicit 30-second and 180-second deadlines. The m2 signal tests keep their PTY-free pipes and use timed `communicate()` calls.
- R-T4: M5 sets and restores `GIT_CEILING_DIRECTORIES` to the parent of its own temporary directory before creating a private store. The M7 no-ceiling test creates its own synthetic `.git` marker. K owns the runner's `store-refuses-git` fixture update.
- R-T5: the M5 final-read timeout test passes its own synthetic 05c6:9008 sysfs fixture to `cli.main`, which propagates that root to child proxy configuration.
- G-8: M4 fixture directories now use `TemporaryDirectory` cleanup. A direct check found zero `openstick-m4-test-*` leftovers.
- Tree/spec mismatch recorded in `OPEN-QUESTIONS.md`: the workspace has `AGENTS.md` and `CLAUDE.md`, but the builder tree lacks its local `CLAUDE.md` and both specified files.

## Validation

All results below are VERIFIED-WITH-SHIMS. No hardware, sudo, network, or real EDL client was used.

- `tests/verify-installer-m5.py`: 25 tests passed; the isolated `test_restore_final_read_timeout_is_exit3_and_blocks_boot_recommendation` passed after fixture root and git ceiling injection.
- `tests/verify-installer-m6.py`: synthetic package checks passed.
- `tests/verify-installer-m7.py`: 27 tests passed.
- `tests/verify-installer-edl-session.py`: 17 tests passed.
- `tests/verify-installer-m4.py`: 25 tests passed; temporary leak check found 0 leftovers.
- The four affected groups ran with allowlisted test environment values, fixture-first shim PATH, host USB path guard, temporary HOME/TMPDIR, and per-group process-tree deadlines. The broader suite and environment matrix remain for integration.
- The M5 isolated test first failed when run without a ceiling, at `store.ensure_store`: the clean test environment correctly encountered the foreign `/tmp/.git`. After M5 set the ceiling itself, the isolated test passed in 3.2 seconds.

## Limits

- The generated rollback helper is exercised with fixtures and fake EDL. Its behavior on real hardware is not tested.
- The m6 test's PTY subprocesses cannot use `stdin=DEVNULL`; they are intentionally interactive and bounded by their explicit polling deadlines.
- Full acceptance matrix, compileall, shell syntax checks, final secret scan, and patch generation are integration tasks.
