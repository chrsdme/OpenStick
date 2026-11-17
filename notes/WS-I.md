# WS-I notes

## Work completed

- H-07 shim fidelity: first invocation records a Sahara block and loader upload; later invocations report firehose without an identity block. Added absent and hung transcript modes and a synthetic power-cycle helper.
- H-06 test isolation: doctor and wizard tests inject fixture sysfs and USB roots through hidden CLI options. The suite's Python host-path guard blocks access to real `/sys/bus/usb` and `/dev/bus/usb` paths, including subprocesses.
- F-14 test environment: M6 sets its own Git ceiling around the per-run temporary directory and restores the caller's value afterward.
- M6's historical non-interactive `terminal` assertion passes against current code. Its expected refusal text is present, so no product or assertion change was needed.

## Commits

- `78541ce fix(H-07): model EDL session state in shim`
- `71190fc fix(H-07): match fresh loader transcript`
- `54cea00 fix(F-14): isolate M6 git ceiling from caller env`
- `91d7055 fix(H-06): keep doctor tests on USB fixtures`

## Validation

- M2: 13/13 passed.
- Doctor fixture suite: 10/10 passed.
- M6: composite package checks passed with caller `GIT_CEILING_DIRECTORIES` unset and set.
- M7: 27 tests passed with caller `GIT_CEILING_DIRECTORIES` unset and set.
- Aggregate `tests/run-installer-tests.sh`: 24 PASS, 0 FAIL, 0 SKIP with caller `GIT_CEILING_DIRECTORIES` unset and again set. The runner used its throwaway `TMPDIR`, shim-first `PATH`, and host USB guard. The guard probe passed.
- `bash -n`, Python compilation, and `git diff --check` passed for touched files.
- Secret scan classification for additions: SCAN-HIT FAKE-FIXTURE only. Synthetic transcript fixtures; no stop-class hit.

## Remaining and offline limits

- The positive generated install runner E2E remains **PARTIAL**: the fixture reached the install runner but it refused with `EDL client exited with status 1`; the final diagnostic rerun was interrupted before producing the logged fake-client call tail. No real device path was opened; the guard remained loaded. The bounded repair budget was exhausted, so this path is recorded rather than retried.
- No real board, USB device, EDL client, sudo, or network was used. Hardware behavior remains unverified offline.

## Phase 2 — Session and identity behavior

- Added G-1 Session lifecycle tests for ABSENT, NORMAL-MODE, FRESH Sahara identity, CONSUMED firehose, HUNG handshake, bus/devnum swap, serial masking, and doctor zero client invocations.
- Added R7 integration coverage: create and verify a backup using the stateful synthetic EDL client, clear fake session state to model power-cycle, then verify `backup --dry-run` refuses because the registered verified backup exists and prints the EDL session-used note.
- The focused lifecycle suite passes **17/17**. It is now included in `tests/run-installer-tests.sh`.
- M6 EDL package subprocesses now call generated `main(sysfs_root)` through a temporary `runpy` wrapper. Wrapper, fixture sysfs, and session state live under the runner's throwaway `TMPDIR`, outside the package tree, and are removed in `finally`. Synthetic package EDL transcripts now report Sahara identity only on the first invocation and firehose thereafter. The aggregate host-path guard stayed enabled. The positive install runner remains PARTIAL as documented above.

## Invocation audit

Counts below are fake EDL client invocations per operation; `rs`/`rf` are individual invocations. Identity comes from the first Sahara transcript in the invocation's Session. Subsequent calls must be firehose and reuse the captured identity.

| Operation | Invocation count | Identity source / notes |
|---|---:|---|
| doctor | 0 | USB sysfs fixture only; test asserts no client invocation |
| device list/show/label/note | 0 | Local registry identifier; no EDL identity |
| backup | 2 (3 with `--paranoid`) | 1 GPT `rs` captures Sahara identity, then 1 full `rf` (plus comparison `rf` when paranoid) |
| backup `--dry-run` | 1 | GPT `rs` captures identity, then prints dry-run result; verified-backup refusal follows this same one-call path and prints the session note |
| verify | 0 | SSH/host verification; no EDL identity |
| restore `--dry-run` | 2 | Initial GPT `rs` captures identity, then full-disk `rf`; destructive restore adds per-phase engine calls and four identity reads before each phase |
| flash `--dry-run` | 4 identity reads, plus phase-engine skip-write calls | GPT plus `fsg`, `sec`, `fsc` reads share one Session; identity is Sahara serial and fingerprint ranges |
| package create/verify | 0 | Registered verified backup/image-set metadata; no EDL identity |
| packaged restore runner | Variable; at least GPT + full read before phase flow | First `rs` captures identity; generated runner reuses it for later calls and checks bus/devnum before each call |
| packaged install runner | PARTIAL; positive E2E refused after an EDL client returned status 1 | Fixture sysfs is injected; no identity was accepted by the wrapper flow |
| image-set verify | 0 | Local manifest/member hashes; no EDL identity |
| wizard: doctor | 0 | Delegates to doctor; no client invocation |
| wizard: backup | 2 (3 with `--paranoid`) | Same capture and full-read sequence as `backup` |
| wizard: configure/build/inject/package/verify | 0 | Local, build, package, or SSH stages; no EDL identity |
| wizard: flash | Same as flash | Delegates to flash; one Session for identity and engine calls |
| wizard: restore | Variable; 2 initial reads plus per-phase calls | Delegates to restore; one Session through live reads and all phase-engine calls |

The M6 generated restore runner reuses captured identity across its multi-call restore flow. The generated install runner reaches its positive EDL flow but the synthetic client exits status 1; this remains PARTIAL pending a later targeted diagnosis.

## Phase 2 validation

- Focused Session lifecycle: **17 PASS / 0 FAIL / 0 SKIP**.
- Final focused suites after integration-test repairs: M2 **13/13**, M4 **25/25**, M5 **25/25**, and M7 **27/27**.
- M2 now asserts the typed HUNG refusal and uses fixture sysfs/dev roots; M4 verifies that the generated EDL proxy config points to the resolved fake client and pinned loader, while actual proxy calls preserve loader and memory flags. M5 fake-live helpers seed the passed Session identity before proxy use.
- Wizard backup now passes its injected sysfs root into a scoped Session. The M7 fake-board integration creates and removes its synthetic 05c6:9008 sysfs entry around the backup flow.
- M6 was not green: the last completed focused run failed in the generated install positive E2E with fake client exit status 1. A later diagnostic attempt was interrupted and produced no further evidence. The host path guard blocked no attempted access in the completed run; fixture roots were injected and cleaned.
- Phase 2 secret-scan classifications: **SCAN-HIT FAKE-FIXTURE only** for synthetic transcript identifiers and test-only serial strings. No stop-class secret hit was found.
