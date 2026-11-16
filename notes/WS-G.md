# WS-G EDL call-site audit

## Pre-conversion inventory (base `a8b9ffc`)

| Site | Command | Loader and memory | Timeout | Output handling |
|---|---|---|---|---|
| `tools/installer/flash.py:76` | `<resolved EDL prefix> getstorageinfo` | Neither passed | 30 s identity constant | Nonzero refused; parsed legacy `serial = HEX` regex from stdout/stderr. |
| `tools/installer/flash.py:92` | `<resolved EDL prefix> rs START COUNT FILE` | `--memory=emmc`, no loader | 30 s identity constant | Return code and file size checked; bytes hashed. |
| `tools/installer/flash.py:231` plus Path A invocation at `:274-276` | `EDL_CMD` plus existing Path A script and phase | `LDR` set from image set; fwlib appends `--loader=$LDR --memory=emmc` to `rs`/`ws` | Outer engine had no timeout; per-command behavior belongs to protected `tools/pathA/fwlib.sh` | Engine captures each command in its log; installer checks engine exit status and logs exact outer command plus EDL prefix. No installer-side EDL output parsing. |
| `tools/installer/verify_restore.py:243` | `<edl> getstorageinfo` | Neither passed | 30 s literal | Parsed legacy serial regex from stdout/stderr. |
| `tools/installer/verify_restore.py:249` | `<edl> rf FILE` | Both passed | `INITIAL_FULL_READ_TIMEOUT_SECONDS` (300 s) | Return code, file existence, sector alignment, GPT, and partition hashes checked. |
| `tools/installer/verify_restore.py:259` | `<edl> getstorageinfo` | Neither passed | 30 s literal | Parsed legacy serial regex from stdout/stderr. |
| `tools/installer/verify_restore.py:268-270` | `<edl> rs START COUNT FILE` | Both passed | 120 s literal | Return code and exact file size checked; bytes hashed. |
| `tools/installer/verify_restore.py:449` and `:635` | `_edl(edl, rs ...)` | Both passed at caller | Default 600 s | Return code checked; caller checks output size/hash. |
| `tools/installer/verify_restore.py:685-697` | Existing fwlib engine via `EDL_CMD` and `LDR` | Both passed by protected fwlib command builder | 900 s outer timeout | Engine logs captured, but discarded; nonzero gives generic refusal. |
| `tools/installer/verify_restore.py:707` | `_edl(edl, rf FILE)` | Both passed at caller | `FINAL_FULL_READ_TIMEOUT_SECONDS` (600 s) | Final file size and SHA-256 checked; failure blocks boot recommendation. |
| `tools/installer/packages.py:593` in `_RESTORE_RUNNER_INSPECT` | `a.edl getstorageinfo` | Neither passed | 30 s literal | Parsed legacy serial regex; no loader pin check in this inspect runner. |
| `tools/installer/packages.py:745` in `_RESTORE_RUNNER` | `edl rs START COUNT FILE` | Both passed | 120 s | Return code, exact size, and hash checked. |
| `tools/installer/packages.py:750` in `_RESTORE_RUNNER` | `edl getstorageinfo` | Neither passed | 30 s | Parsed legacy serial regex. |
| `tools/installer/packages.py:904` in `_RESTORE_RUNNER` | `a.edl getstorageinfo` | Neither passed | 30 s | Parsed legacy serial regex. |
| `tools/installer/packages.py:908` in `_RESTORE_RUNNER` | `a.edl rf FILE` | Both passed | 600 s | Return code and full image size checked; identity slices then hashed. |
| `tools/installer/packages.py:982, 1028` in `_RESTORE_RUNNER` | `a.edl rf FILE` | Both passed | 600 s | Return code and final full-disk SHA-256 checked. |
| `tools/installer/packages.py:1001, 1018` in `_RESTORE_RUNNER` | `a.edl ws ...` dry-run/write | Both passed | Default 300 s / write 600 s | Return code checked; writes gated by typed confirmation and readback. |
| `tools/installer/packages.py:1022` in `_RESTORE_RUNNER` | `a.edl rs START COUNT FILE` | Both passed | 600 s | Return code and readback hash checked. |
| `tools/installer/packages.py:1118-1124` in `_INSTALL_RUNNER` | generated `EDL_CMD` adapters for the flash wrapper | External client selected from `--edl`; loader copied from verified package dependency. The flash wrapper/engine appends loader and memory. | Inherits installer wrapper timeouts; engine calls have no new outer timeout | Installer flash wrapper handles results. |
| `tools/installer/wizard.py` | No direct EDL process invocation or `EDL_CMD`/`LDR` construction | N/A | N/A | Wizard dispatches CLI stages; the stage implementations above own EDL. |

## Post-conversion audit

| Site | Command and flags | Timeout | Output handling |
|---|---|---|---|
| `flash.py:_live_identity` | `rs 0 34 FILE`, then `rs START COUNT FILE` for fsg/sec/fsc, through `edl_runner`; every call adds absolute SHA-pinned `--loader=... --memory=emmc` | Identity 20 s; partition ranges 20 s | Shared parser requires `Serial: 0x...`, `HWID: 0x...`, and `PK_HASH: 0x...` across stdout+stderr. Files are size/hash checked. |
| `flash.py:_invoke` | Existing engine receives absolute `EDL_CMD` prefix and verified `LDR`; protected Path A builder adds loader/memory to `rs`/`ws` | Named engine phase timeout 7200 s; SIGINT, grace, then SIGTERM | Prints exact engine command and EDL prefix. Verbose/debug failure includes log tail and recovery hint. |
| `verify_restore.py:_live`, `_live_identity_only` | `rs 0 34 FILE`, range `rs`, initial `rf`, final `rf`, plus reconciliation/pad reads through `_edl` → `edl_runner`; loader and memory appended centrally | Identity/range 20 s; initial/final full reads 3600 s; named other reads 600 s | Sahara identity parser combines stdout+stderr; exact sizes, GPT and hashes checked. |
| `verify_restore.py:_run_fwlib_phase` | Existing fwlib engine receives absolute `EDL_CMD` and verified `LDR`; protected command builder adds flags | Named outer engine timeout 900 s; SIGINT, grace, then SIGTERM | Prints exact outer command; tails on verbose/debug; reports actionable recovery hint. |
| Packaged `_RESTORE_RUNNER_INSPECT` | `rs 0 34 FILE` via `run_edl`, absolute client and verified loader plus eMMC flag | Identity 20 s | Parses all three Sahara identity fields from stdout+stderr. |
| Packaged `_RESTORE_RUNNER` | All operations use `edl_call`: identity `rs 0 34`, range `rs`, initial/final full `rf`, dry-run `ws --skipwrite`, write `ws`, and readback `rs`. Appends absolute pinned loader and `--memory=emmc` every time. | Identity 20 s; range 120 s; write 600 s; full read 3600 s | Identity uses Sahara fields; operation output is captured and exact command printed; timeout interrupts then terminates and emits a power-cycle hint; verbose/debug includes output tail. Failed writes/readbacks retain recovery state. |
| Packaged `_INSTALL_RUNNER` | Validates supplied client and constructs adapter with explicit `edl` wrapper and verified loader; delegates flash to the normal `EDL_CMD`/`LDR` route | Inherits flash phase timeout | No new EDL invocation path. Package metadata states installer EDL does not use sudo and relies on installed udev permissions. |
| `wizard.py` | No direct EDL process invocation, command builder, `EDL_CMD`, or `LDR`; delegates to CLI stages | N/A | Stage implementations above own the EDL boundary. |

No `getstorageinfo`, `edl reset`, or SIGKILL remains in the owned installer paths. Protected `tools/pathA/` and `tools/flash/` were not modified. Test shims enforce the loader and memory flags.

## Changes and validation

Changes were developed test-first: strict M4/M5/M6 fake EDL clients reject invocations without loader and eMMC flags. M4 flash wrapper suite passed 25/25; M5 verify/restore suite passed 25/25; M6 standalone package generation and runner scenarios passed. `python3 -m py_compile` and `git diff --check` also passed before final staged-package validation; rerun after the metadata edit.

The full `tests/run-installer-tests.sh` was not run: its `doctor-missing-dependencies` case executes host doctor checks, which inspect actual host `/dev` and service/device state. Per orchestration direction, validation is limited to the synthetic M4/M5/M6 suites and staged package/notes validation; no host sysfs or `/dev` checks were run.

## Hardware limits

`NEEDS-HARDWARE`: actual EDL client output formatting, USB driver behavior, pinned loader acceptance, real board identity, and operation timing remain unverified. All completed EDL exercise was through synthetic test shims; no hardware, sudo, or network was used.

Secret scan: `SCAN-HIT SYNTHETIC-EDL-FIXTURE` — test-only synthetic Sahara/loader fixture markers detected; no secret values are included here.
