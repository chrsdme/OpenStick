# OpenStick installer implementation report

## Milestone status

| Milestone | Status | Commit |
|---|---|---|
| M0 Baseline and design | DONE | `de86b3d` |
| M1 Skeleton/store/doctor/shims | DONE | `32b411b03b67956a2e0187f30e4ace8560ab29bf` |
| M2 Backup | DONE | M2 commit (SHA in completion report) |
| M3 Configure/seed/inject | DONE | this commit |
| M4 Build/flash wrappers | DONE | this commit |
| M5 Verify/restore | SYNTHETIC TESTS PASS (22/22); fake-disk restore and final full-hash checks pass; real EDL/hardware acceptance not performed | No commit: blocked by read-only Git metadata |
| M6 Packages | Synthetic package checks and post-snapshot mutation regressions pass; real production inputs unavailable | No commit: blocked by read-only Git metadata |
| M7 Wizard/docs/tidy | Final tests PASS (14/14), including synthetic fake-EDL byte-identical restore E2E; GPT-T install flash and hardware E2E remain blocked | Uncommitted; read-only Git metadata prevents local milestone commits |

The clean source snapshot commit is `e4cf01b`; it is the root commit because no source history/baseline was included in the prepared workspace. The M1 implementation does not modify existing build, flash, initramfs, or rollback scripts. There is no `baseline..installer` range.

## Existing offline tests: M0 baseline

Results below are from direct runs without sudo, hardware, EDL, fastboot or network. “Predates installer” is confirmed by these tests running against the untouched imported tree before any installer implementation; repository history before the imported snapshot is unavailable, so older origin timing cannot be proven.

| Test | Result / exit | Exact result and cause | Classification / installer relevance |
|---|---|---|---|
| `tests/verify-boot-img.sh` | FAIL, 1 | Prints `ERROR: run scripts/50-boot-img.sh first`; required `out/prod-boot` artifacts absent. | Missing generated build artifacts in sanitized copy, confirmed. Baseline failure before installer work. No code defect established. Installer build wrapper must create/locate these outputs. |
| `tests/verify-images.sh` | FAIL, 1 | Eight required-artifact assertions fail: `prod-boot/boot.img`, matching checksum, Android `ANDROID!` magic, `prod-rootfs/rootfs.img`, checksum, 512 KiB `step1/lk2nd-padded-512KiB.img`, step1 checksums, composite size. It separately skips e2fsck/label due missing image and sgdisk check because GPT files are absent. Ownership selftests pass. | Missing generated artifacts confirmed; `out/` absent. The test's own `SKIPPED (stale)` note also marks old GPT-B image checks obsolete. No evidence of image-code defect because required inputs/artifacts were not built. Baseline; installer must run its own fake build wrapper tests. |
| `tests/verify-ownership.sh` | FAIL, 1 | Ownership detector selftests pass. Two fakeroot integration checks fail: old `cp -a` defect reproduction and clean `install_tree` output. Both emit `fakeroot daemon: socket (Operation not permitted)` and subsequent fakeroot/kill errors. | Environment restriction confirmed: fakeroot daemon cannot create socket under this sandbox. It does not establish a source defect. Baseline. Ownership helper is relevant to building rootfs images, but installer state/features do not depend on fakeroot; preserve helper and rerun in a permitted fake environment if available. |
| `tests/verify-profile.sh` | FAIL, 1 | Credentials/profile predicate assertions pass. It invokes `scripts/20-rootfs.sh` expecting the credentials error; actual rc is 126 and output is `/bin/bash: line 1: scripts/20-rootfs.sh: Permission denied`. The imported Git index records this file as mode `100644`, so the test never reaches credential validation. Remaining prod/dev static checks pass; built-image check skips because no image. | Confirmed prepared-copy file-mode issue. It predates installer code; original pre-sanitization mode cannot be proven from the available Git history. Profile behavior remains relevant; the test should run in an executable-preserving prepared copy. No chmod or script change made. |
| `tests/verify-fetch-firehose.sh` | PASS, 0 | Fake download behavior validates pinned-hash mismatch refusal and no installation. | M0 pass; relevant to external blob integrity. |
| `tests/verify-flash-board.sh` | PASS, 0 | Fake fastboot validates lk1st/lk2nd branching, readback, refusal cases and confirmation guard. | M0 pass; critical regression baseline. |
| `tests/verify-harden.sh` | PASS, 0 | Hardening behavior validated in temp fixtures. | M0 pass; relevant to prod profile. |
| `tests/verify-initramfs-fail.sh` | PASS, 0 | Initramfs failure/reboot behavior via host dry-run and mutations; aarch64 cross-tool branch skipped. | M0 pass. Hardware boot remains untested. |
| `tests/verify-nft-ruleset.sh` | PASS, 0 | Static ruleset lint/mutation tests pass; real nft/container cross-check skipped. | M0 pass. |
| `tests/verify-rollback-plan.sh` | PASS, 0 | Fake control, malformed inputs, hash compare and plan safety checks pass; real-data checks skipped because backups are absent. | M0 pass; critical restore-plan regression baseline. |
| `tests/verify-sshd-config.sh` | PASS, 0 | Host sshd config checks pass. | M0 pass. |
| `tests/verify-firewall.sh` | SKIP | Not run: test includes privileged namespace/kernel operations; acceptance forbids root and sudo. | Hardware/root-independent static portions were not separately isolated. |
| `tests/verify-rootfs.sh` | SKIP | Not run: root-dependent rootfs checks. | Needs a built rootfs and privileged test environment. |
| `tests/verify-sshd.sh` | SKIP | Not run: root-dependent effective sshd checks. | Needs built rootfs/root. |

No generated image baseline SHA256 values are available. `out/`, cached APK/deb inputs and production images were excluded from the sanitized workspace. Obtaining inputs would require network, and running root-dependent build stages is forbidden. Script fingerprints are not image hashes. See DESIGN.md.

## M0 static validation

| Check | Result |
|---|---|
| `bash -n` on every `.sh` file | PASS, 62 files |
| `python3 -m py_compile` on every `.py` file | FAIL, 2 vendored Python 2 files under `src/lk2nd/` use Python 2 syntax; project-owned installer/build Python files had no syntax failures |
| `shellcheck` | SKIP, executable is unavailable |
| Installer-specific suite | NOT APPLICABLE; suite does not exist until M1 |

## Feature verification

| Feature | Tested with shims | Needs real hardware |
|---|---|---|
| Existing flash path selection | `verify-flash-board.sh` fake fastboot | Physical product variables, real partition readback, boot and recovery |
| Existing restore plan | `verify-rollback-plan.sh` fake artifacts | EDL behavior and device-specific backup identity |
| Existing external loader pinning | `verify-fetch-firehose.sh` fake fetch | No download performed; real loader provenance/EDL compatibility |
| Installer M1 CLI and registry | M1 acceptance tests listed below | Real device identity and USB behavior need hardware |
| Installer M1 private store | mode, symlink, worktree and event tests listed below | Filesystem behavior beyond the tested Linux environment |
| Installer M1 doctor | controlled-PATH missing-tool test | Actual host package readiness and USB permission behavior |
| Installer M1 fake device shims | deterministic fake GPT/emmc, EDL read and failure injection, fastboot guard, build stubs | No claim about actual EDL/fastboot protocol compatibility |

## Differences and questions

Specification section comparison and recommended defaults are in [OPEN-QUESTIONS.md](OPEN-QUESTIONS.md). Script interfaces, dependencies, architecture, external input pins and the seed integration decision are in [DESIGN.md](DESIGN.md).

Master prompt says each milestone must have green tests before advancing, while M0 explicitly says record existing test failures and do not fix unrelated failures. Resolution: the four documented legacy failures above are baseline exceptions, not passing tests. They do not block M0 if their causes are unrelated to M0 deliverables and accurately documented. Any new installer test failure blocks its milestone. The `verify-profile.sh` output mismatch remains a baseline issue to investigate if installer profile work relies on that exact message.

## Files added or modified

Added: `DESIGN.md`, `OPEN-QUESTIONS.md`, `INSTALLER-REPORT.md`.

Existing scripts modified: none.

## M1 implementation and evidence

Implemented `./openstick` with global flags (`--home`, `--dry-run`, `--verbose`, `--no-color`, `--board`, `--device`), help and exit code constants 0/1/2/3. The implemented commands are `doctor` and `device list|show|label|note`. The private store rejects Git worktrees and symlinked path components, creates private directories as mode 700, creates state files as mode 600, reads state without following symlinks, and updates the registry through an atomic replacement. Events append to `events.jsonl`; event records omit user-provided labels and notes.

Doctor checks Python, Bash, e2fsprogs, fastboot, EDL discovery by `OPENSTICK_EDL_DIR` or `PATH`, the pinned loader SHA-256, USB access with a udev suggestion, 12 GiB free space, and private-store location/mode. It reports apt/dnf installation hints. It does not install dependencies or udev rules.

| Feature | Tested with shims / tests |
|---|---|
| Worktree gate | `store-refuses-git` (exit 2) |
| Private permissions and registry round trip | `store-permissions` (directory 700, registry/events 600, read-back) |
| Symlink defense | `store-refuses-symlink` |
| Registry operations and append-only events | `registry-device-operations` (label, note, show, two event lines) |
| Dependency discovery with controlled PATH | `doctor-missing-dependencies` |
| Fake hardware isolation | `shim-fastboot-fails-closed`, `shim-build-script-stub`, `fake-emmc-deterministic`, `shim-edl-read-and-failure-injection` |
| M0 regression set | seven previously passing legacy suites run by `tests/run-installer-tests.sh` |

| Feature | Needs real hardware |
|---|---|
| USB diagnostics | Confirm which host udev identifiers and group access are correct for the user's adapters. No udev rule was installed. |
| EDL / fastboot shims | Compare shim command line compatibility with actual installed clients. Shims cannot prove hardware behavior and reject writes. |
| Device identity | Capture the available identity fields from a physical stick; no real serial or IMEI was used. |

M1 suite result: **PASS 16, FAIL 0, SKIP 0**. Static validation: `python3 -m py_compile openstick tools/installer/*.py tests/make-fake-emmc.py` PASS; `bash -n` across every repository `.sh` file PASS; shellcheck SKIP because it is unavailable. The M1 suite reran all seven M0-passing tests. Nested optional checks inside legacy tests report their own SKIPs (cross compiler, nft/container and absent real backup data); the legacy suites still pass. The four unrelated M0 failures remain as documented baseline exceptions and were not rerun or changed.

Files added: `openstick`, `tools/installer/__init__.py`, `tools/installer/cli.py`, `tools/installer/store.py`, `tests/run-installer-tests.sh`, `tests/make-fake-emmc.py`, `tests/shims/bin/{edl,fastboot,ssh,scp,sudo,10-base.sh,20-rootfs.sh,50-boot-img.sh,51-rootfs-img.sh,gen-gpt.sh}`. Files changed: `INSTALLER-REPORT.md`. Existing validated build, flash, initramfs and rollback scripts touched: none.

## M2 backup implementation and evidence

`./openstick backup` discovers the EDL serial and optional numeric hardware ID fields from the client output, normalizes serial to lowercase for registry matching, and reads the entire eMMC with `edl rf`. It verifies exact whole-sector image geometry against the GPT alternate LBA, validates both GPT headers and entry arrays including CRCs, checks table agreement and partition bounds/overlap, and requires `fsg`, `sec` and `fsc`. It writes a full image SHA-256, those three partition fingerprints, `manifest.json` and `SHA256SUMS` into mode-700 staging with mode-600 files. Registration and a redacted event happen only after validation. `--paranoid` performs and hashes a second full read; `--again` permits additional backups. A verified backup is never overwritten. The command warns about IMEI, calibration and persist contents and recommends an independent offline copy.

| Feature | Tested with shims |
|---|---|
| Complete read, image hash, GPT geometry and manifest | `backup-success-and-integrity` |
| Primary and backup GPT header CRC corruption refusal | `backup-gpt-corruption-and-paranoid-mismatch` |
| Truncated read returns exit 3 | `backup-failures-cleanup` |
| Known fsg/sec/fsc fixture fingerprints | `backup-success-and-integrity` |
| Duplicate refusal exit 2 and additional backup | `backup-duplicate-and-again` |
| Paranoid successful comparison and mismatch refusal | `backup-paranoid-success`, `backup-gpt-corruption-and-paranoid-mismatch` |
| Mid-read EDL failure and no partial registry/manifest | `backup-failures-cleanup` |
| Registry/event consistency and private modes | `backup-success-and-integrity` |
| Store worktree and symlink path safety | `store-refuses-git`, `store-refuses-symlink` |
| Previous M1 and M0 passing regression suites | `tests/run-installer-tests.sh` (all retained suites) |

| Feature | Needs real hardware |
|---|---|
| EDL output parsing and complete `rf` compatibility | Confirm installed client output, Firehose loader behavior and full read completion on the supported board. No hardware command was run. |
| Identity data availability | Confirm real serial/HWID/MSM ID/public-key hash fields. Optional fields remain nullable; device identity gate is backed by dump fingerprints. |
| eMMC geometry and offline recovery | Confirm against a physical board and retain the resulting sensitive backup offline. |

M2 acceptance suite: **PASS 21, FAIL 0, SKIP 0**. Existing generated-artifact and privileged M0 baseline failures remain documented above and were not run as part of the M1/M2 runner. Runtime tests used fake EDL only. Static checks are recorded in the completion response. Existing validated build, flash, initramfs and rollback logic was not modified.

## Known limitations

- Sanitized source copy has no original Git history, build outputs, private config, backups or physical device evidence.
- No build was run; boot/rootfs image hashes cannot be stated.
- Root-dependent tests were skipped, not passed.
- M1/M2 shims provide deterministic fake GPT metadata and eMMC reads. They do not prove real EDL behavior. Configure, build, seed, flash, verify, restore, package and wizard flows are later milestones.
- The fake GPT contains one-sector placeholder partitions and is metadata for test isolation, not a realistic eMMC image.
- The user-facing registry note field is plain text. Event history does not copy note or label content.
- No claim is made about physical USB permissions, installed EDL compatibility, flashing, backup, restore or boot behavior.

## M3 configure, seed application, injection and push

Implemented `configure [--seed NAME]`, `configure --show`, `seed inject`, and `seed push`. The configurator supports every section 6.2 field: `SEED_VERSION`, `HOSTNAME`, `TIMEZONE`, `SSH_AUTHORIZED_KEYS`, `ROOT_PASSWORD_HASH`, `DEFAULT_USER`, `USER_PASSWORD_HASH`, `WIFI_SSID`, `WIFI_PSK`, `WIFI_COUNTRY`, `APN`, `APN_USER`, `APN_PASSWORD`, `USB_NET_ADDR`, and `NTP_SERVERS`. Reserved `LED_*` fields are reported as ignored by the device utility. Public SSH key input is imported from a file and validated; private-key files are refused. `SSH_AUTHORIZED_KEYS` stores base64-encoded, LF-separated public key lines so the seed remains strict one-line `KEY=VALUE` data. Passwords are collected with hidden prompts and converted using libcrypt SHA-512 crypt; plaintext passwords are never serialized. Configure output masks password hashes, Wi-Fi PSK, APN credentials, and public key payloads.

The device-side `/usr/lib/openstick/apply-seed` reads `/etc/openstick/seed.env` as data, validates its allowlisted fields without `source` or `eval`, and applies hostname, timezone, SSH authorized keys, password hashes, a dev account, Wi-Fi and GSM NetworkManager profiles, systemd-networkd USB address, and timesyncd NTP servers. `{serial8}` uses the last eight hex characters of validated `/proc/cpuinfo` Serial. `openstick-seed.service` runs before `network-pre.target`, NetworkManager, and systemd-networkd. Failure logs contain fixed descriptions only, the oneshot does not block boot, and no success receipt is written on failure. On success, the active seed is unlinked and `/etc/openstick/seed.applied` contains only the seed SHA-256. Unlinking does not securely erase ext4 data.

`seed inject` accepts only a valid stored seed and an existing generic `builds/<ID>/rootfs.img`. It runs read-only `e2fsck -fn`, copies the generic image into a hidden private staging directory, writes the seed with `debugfs`, checks the new image read-only, reads the seed back with `debugfs`, compares SHA-256, writes a sensitive manifest and atomically publishes `personal-<NAME>/`. Failed operations remove staging; the generic image is hash-checked unchanged by the fixture. `seed push` requires explicit `USER@HOST`, performs a batch SSH access check, transfers only after access succeeds, starts the service, and checks the receipt hash. An unreachable production image requires flashing a seeded image.

| M3 acceptance check | Result |
|---|---|
| Strict parsing, all field validation, malformed/unknown/duplicate/newline and shell injection rejection | PASS |
| Public-key import and private-key refusal | PASS |
| Hidden-input path, password hashes only, masked configure show, private 700/600 modes | PASS |
| G4 production seed warning, confirmation, and decline refusal | PASS |
| Fake-root application, supported settings, secret-safe logs, LED notice, validated `{serial8}`, repeat application | PASS |
| Invalid seed leaves generic state, keeps seed, writes no receipt, exits successfully for boot | PASS |
| Service ordering and `scripts/20-rootfs.sh` installation hook | PASS (static and shell syntax checks) |
| Debugfs injection, source image unchanged, seed SHA-256 read-back, sensitive marker | PASS |
| Corrupt image rejection and interrupted debugfs staging cleanup | PASS |
| `seed push` without host or usable SSH | PASS (SSH shim fails closed) |
| Real EDL, fastboot, SSH, root, network or device operations | NONE performed |

M3 suite: **11 PASS, 0 FAIL**. Full `tests/run-installer-tests.sh`: **22 top-level PASS, 0 FAIL, 0 top-level SKIP**. It includes all M1/M2 checks and the seven previously passing M0 regression suites. Nested legacy checks reported three optional skips (aarch64 cross-tool, container-backed nft check, and absent real backup data); the nft suite also did not run its rootfs cross-check because no built rootfs exists. No root-required build or artifact test was attempted. Static validation: Python compile PASS for 18 project-owned Python files; `bash -n` PASS for 68 shell files; `sh -n configs/apply-seed` PASS; `git diff --check` PASS; shellcheck SKIP because it is unavailable.

Existing builder scripts modified: `scripts/20-rootfs.sh` only. It installs the seed apply utility and enables the optional service in the rootfs, the narrow image assembly hook required for M3. No flashing, rollback, firmware, kernel, initramfs, or proven networking template logic was changed. Image SHA-256 baseline remains unavailable because building requires excluded external inputs and root; no real or synthetic full OS build was run.

## Current unattended session verification (2026-11-15)

The workspace was **not clean at session launch**: it already contained uncommitted M4–M7 source, tests, documentation, status, and handoff files. The branch was `installer`; HEAD was exactly `da4f9c8a2770b11b181611fe5ff525cd2d1be197`, and that accepted M4 commit is an ancestor of HEAD. No history was rewritten. The Git metadata is read-only in this execution environment, so no milestone commits could be created; all M5–M7 work remains uncommitted and cannot be represented as accepted local milestone commits.

Focused test evidence from this session and worker reviews:

| Area | Result | Scope |
|---|---|---|
| M4 | **22/22 PASS**, exit 0 (`python3 tests/verify-installer-m4.py`) | Synthetic GPT-T mapping, exact artifact/hash checks, invalid format/layout and size refusal, symlink/race cases, and fail-closed GPT-T flash refusal. No physical flash validation. |
| M5 | **22/22 PASS**, exit 0 | Synthetic verify/restore, identity mismatch, interrupted-phase reconciliation, full fake-disk readback/hash, and byte-identical restore. Independent review accepted. |
| M6 | Synthetic focused suite PASS, exit 0; numeric count not captured in the coordinator's evidence | Package integrity/tamper and standalone fake-only checks. GPT-T install package integration remains blocked by the absent validated GPT-T flash path and unavailable real inputs. |
| M7 | **14/14 PASS**, exit 0 | Wizard contract and synthetic restore workflow; B's independent cross-review accepted the corrected race-handling change. This does not establish the requested stock-to-install hardware cycle. |
| Full permitted installer runner | **26 PASS, 0 FAIL, 0 SKIP**, exit 0 | Includes the installer regression suite; legacy tests retain their documented M0 exceptions. |

Static checks reported after the final D fixes: Python project compile passed, `bash -n` and POSIX `sh -n` checks passed for applicable scripts, and `git diff --check` passed. ShellCheck was unavailable and therefore not run. No network, root, real EDL/fastboot/SSH, block device, owner backup, or physical device was accessed; no physical flash was attempted. The canonical `spec/INSTALLER-SPEC.md` and `spec/ACCEPTANCE.md` are absent, so G1–G9 cannot be mapped or claimed as canonically accepted. Overall status remains **PARTIAL_OFFLINE**. Do not mark `READY_OFFLINE` or claim milestone commits/acceptance.

Needs real hardware before claiming device operation: validate systemd ordering on the assembled Bookworm image; confirm hostname/timezone behavior; confirm NetworkManager Wi-Fi and GSM profile acceptance and systemd-networkd USB settings; validate device Serial format; exercise remote SSH/sudo push against an intentionally provisioned test stick. These are not M3 offline-test claims.

## M4 build and flash wrappers

`build --profile dev|prod --board BOARD` wraps the inspected stage order `20-rootfs.sh`, `50-boot-img.sh`, `51-rootfs-img.sh`. It requires the prebuilt `out/base-bookworm-arm64.tar.gz` input from the existing `10-base.sh` flow and cached pinned kernel/BusyBox artifacts. It creates the working output and temporary home inside a private staging directory, records source revision and tree hash, input hashes, tool versions, timestamps, profile, board, artifact sizes and SHA-256 values, and registers only after both expected images exist and hash successfully. On stage or registry/event failure, it removes the staged/final output and does not leave a successful build registration. Read-only image permissions and directory permissions are applied before registration. Build manifests explicitly say that device boot is not verified and that M0 image comparison is unavailable in this sanitized copy.

Generic builds use a clean `env -i` allowlist and a new private `HOME`. The narrow `scripts/common.sh` hook skips local/user config loading and inherited-environment reapplication. Defaults and selected board profile remain active. `--bake-user-env` explicitly restores the old environment and local/user configuration source order. The installer reserves output path variables to keep the generated artifacts in the private store; other legacy configuration precedence is retained. `20-rootfs.sh` is the only privileged stage. The wrapper prints the exact command first, uses the caller's interactive sudo only for that stage, and runs the two image stages without root. No build was run because the prepared base and external pinned artifacts are absent and root/network execution is forbidden.

`flash --device ID --image-set PATH` requires the selected device's verified full backup and rehashes it before each phase. It hashes the live EDL full read to compare serial and fsg/sec/fsc with the backup before destructive phases. Every image-set file is required in the checksum manifest; the Firehose loader must match its pinned SHA-256; Path A firmware/GPT/rollback assets require their own SHA256SUMS. It extracts the six backup slices required by `flash-board.sh` into a private temporary directory. No default board or firmware path is selected. Phase order is `pre`, `gpt`, `tz`, `rpm`, `sbl1`, `hyp`, `aboot`, read-only `final` validation, then `first-install`. Every write has a dry-run followed by `flash <last8> <phase>` and the engine's own COMMIT/CONFIRM token. Path A tokens are `da-flashez-<phase>`; the first-install token is `flash-<board>-first-install`. The Path A final write sends reset, so the wrapper invokes final only in dry mode. It never resets, reboots or retries. A failed/interrupted phase leaves event, registry and state-file evidence and blocks writes until an interactive, no-write `--reconcile-state` acknowledgement.

| M4 feature | Tested with shims / tests |
|---|---|
| Generic config isolation, synthetic user/local config exclusion, explicit legacy precedence | `test_build_generic_and_legacy_env_order_and_permissions`, `test_common_config_isolation_and_legacy_precedence` |
| Build order, root/non-root boundary, artifact hashes and immutable modes | `test_build_generic_and_legacy_env_order_and_permissions` |
| Failed build leaves no build registry/output | `test_build_failure_is_not_registered` |
| Backup slice generation and SHA list for existing fastboot gate | `test_flash_backup_slice_materialization` |
| Missing backup refusal and CLI exit 2 | `test_flash_refusals_and_input_hashes`, `test_missing_backup_cli_exit_code_2` |
| Untested boards are always refused; no override exists | `test_flash_refusals_and_input_hashes`, `test_new_cli_contracts_are_explicit_and_no_tested_board_bypass` |
| Live identity mismatch refusal | `test_flash_identity_mismatch_refused` |
| Missing/mismatched image or loader refusal | `test_flash_refusals_and_input_hashes` |
| Dry-run then write ordering, all phase order, exact typed phrases, COMMIT and CONFIRM tokens | `test_flash_dry_confirmation_protocol_and_failure_stop`, `test_flash_full_phase_order_and_confirm_tokens` |
| Wrong confirmation, non-interactive and global dry-run cannot write | `test_flash_wrong_confirmation_and_noninteractive_dry_only` |
| Phase failure blocks next phase and leaves consistent registry/event/state failure | `test_flash_dry_confirmation_protocol_and_failure_stop` |
| Interrupted phase is not retried; explicit reconciliation only clears state without invoking a script | `test_interrupted_write_requires_reconciliation_and_is_not_retried` |
| Existing M1-M3 and previously passing M0 regression suites | `tests/run-installer-tests.sh` |

| M4 feature | Needs real hardware / not proven offline |
|---|---|
| Build artifacts and baseline | No base tar/kernel/BusyBox cache was present. No real build ran. Baseline image SHA-256 comparison remains unavailable. |
| Path A and first-install | Tests intercept script invocations and use the fake EDL for identity. No write command, real EDL client, Firehose loader, fastboot, USB or block device ran. Real readback quality, device-state transitions and firmware provenance remain unverified. |
| Boot outcome | No target boot was attempted. Successful build/flash engine exit is not evidence that a device boots. |

M4 suite: **11 PASS, 0 FAIL, 0 SKIP**. Full `tests/run-installer-tests.sh`: **23 top-level PASS, 0 FAIL, 0 SKIP**. It retains M1-M3 and all seven previously passing M0 suites. Nested legacy checks report three optional skips: missing aarch64 cross compiler, no working container for nft tier 2, and no real backup files for rollback-plan real-data checks. No network, sudo, real build or physical hardware was used.

M4 files added: `tools/installer/build.py`, `tools/installer/flash.py`, `tests/verify-installer-m4.py`. Files changed: `tools/installer/cli.py`, `scripts/common.sh`, `tests/run-installer-tests.sh`, `DESIGN.md`, `OPEN-QUESTIONS.md`, and this report. Existing flash engines were not modified. `scripts/common.sh` is the only existing script changed for M4, adding the narrow generic configuration branch. `scripts/20-rootfs.sh` remains the M3 seed installation hook.

## M4 final static checks and limitations

| Check | Result |
|---|---|
| `bash -n` all shell files | PASS, 68 files |
| `python3 -m py_compile` all visible Python files | PASS, 14 files |
| `git diff --check` | PASS |
| `shellcheck` | SKIP, not installed |
| Secret and added-file size scan | PASS; no private key block, no non-test personal PSK value, no added file over 5 MB |
| Real build, sudo, network, hardware, USB or block device | NONE used |

The four documented M0 baseline failures remain unchanged: missing generated images, fakeroot socket restriction, and prepared-copy execute-mode issue in the profile test. No M4 fix touched those failures.

Remaining limitations: no Debian image was built because the prepared base and external pinned artifacts are absent and root/network use is forbidden. The M0 byte baseline cannot be compared. The GPT-T image-set adapter only checks and maps the production image formats; `flash` refuses that set until a safe write/readback engine is validated. Supplied Path A blobs require complete SHA256SUMS, but their provenance beyond the pinned Firehose loader is not established in this sanitized workspace. Hardware checks remain real EDL identity output, partition fingerprint agreement, Path A and first-install readback, mode transition, firmware provenance review, and a manual boot observation. See [OPEN-QUESTIONS.md](OPEN-QUESTIONS.md).

M5 readiness: M4 leaves build manifests plus gated flash events and unresolved-state handling for later verify/restore work. This M4 implementation makes no claim that a target device boots.

## Autonomous completion update (offline evidence only)

Branch is `installer`; current HEAD remains the accepted M4 SHA `da4f9c8a2770b11b181611fe5ff525cd2d1be197`. No new milestone commit has been created. The working tree contains uncommitted M5–M7 and integration work. Required `spec/INSTALLER-SPEC.md` and `spec/ACCEPTANCE.md` are absent, so canonical G1–G9 definitions and complete spec acceptance cannot be claimed.

M4 compatibility has a tested synthetic preparation path. `openstick image-set prepare --build ID --layout DIR --output DIR` requires a registered UFI103S build and paired GPT-T primary/secondary tables. It checks source artifact hashes/sizes and build provenance, Android boot-v0 structure, ext4 superblock/size, supported GPT geometry and CRCs, then stages byte-identical `boot.img` and `rootfs.img` as payloads mapped to GPT-T `boot` and `userdata`. `image-set verify PATH` verifies the set read-only. Tests cover malformed or tampered images, wrong build/board/profile, changed GPT geometry, bad protective MBR, zero GUID, symlinks, post-scan symlink swaps, mixed/unknown layouts, and concurrent destination creation.

This is not a flash engine. It does not prove device identity across EDL/fastboot transitions, the boot payload required by the live loader mode, or safe boot/userdata write and readback. `flash` verifies a GPT-T manifest and refuses with `HARDWARE_REQUIRED`. The legacy GPT-B first-install input is kept separate; no renaming or format conversion is performed. No physical device flash was attempted.

| Milestone | Offline evidence | Remaining boundary |
|---|---|---|
| M5 verify/restore | Final focused suite PASS 22/22. Includes late GPT-secondary reconciliation/resume, byte-identical fake-disk restore, timeout and full-read mismatch exit 3, refusal of partial phase bytes, and mismatched/reordered saved-prefix cases with zero writes. Shim-backed call-order tests assert full `rf` and identity reads precede the first resumed write. | Real SSH, EDL behavior, loader semantics and hardware restoration remain untested. |
| M6 packages | Final synthetic package suite PASS. Standalone runners verify and copy inputs through no-follow file descriptors into private snapshots, then use the snapshots. Restore and install tests mutate source payloads after snapshot and confirm the private snapshot remains unchanged. Fake fastboot PATH assertion passes. | No real EDL/fastboot invocation or production package creation from complete real build/backup inputs. The first-install GPT-T writer remains unavailable. |
| M7 wizard/docs | Final focused suite `python3 tests/verify-installer-m7.py` PASS 14/14. Includes guarded wizard restore route through synthetic EDL, each typed phase confirmation, final full-disk SHA-256, and byte-identical fake disk. | No real board run. The synthetic install flow cannot exercise the GPT-T flash write path, which remains fail-closed. |

Final D QA and coordinator review: `bash tests/run-installer-tests.sh` exited 0 with `TOTAL PASS=26 FAIL=0 SKIP=0` at the runner level. M4 passed 22/22, M5 passed 22/22, M6 synthetic package checks passed, and final M7 passed 14/14 after the corrective wizard changes. Nested optional skips: aarch64 gcc/objdump unavailable; rootfs tar/Docker optional tier unavailable; rollback real-data checks skipped with nonexistent temporary `REAL_BACKUP` and `REAL_GPT_DIR` paths. No owner data was inspected. This aggregate does not establish production package creation from unavailable external inputs or hardware execution.

Final static checks: `bash -n` on 68 shell scripts passed; `sh -n` on 48 POSIX-shebang scripts passed; `python3 -m py_compile` on 17 installer/test Python files passed; `git diff --check` passed; ShellCheck was unavailable. An earlier whole-repository py_compile exited 1 only on two untouched Python-2-era sources, `src/lk2nd/platform/at91sam7/mkboard.py` and `src/lk2nd/scripts/aboot_test.py`. Earlier size and high-confidence secret-marker scans found no added or changed file exceeds 5 MiB; the tree-wide scan found only the pre-existing nested `src/linux-dts/.git/index` above that threshold or marker findings.

No root, network, real build, USB/block-device access, real EDL or SSH, or real backup access occurred. The initial M6 harness omitted the fake fastboot shim from PATH; D traced the exercised code and confirmed it cannot call fastboot. C added an explicit fake-fastboot path assertion, the corrected synthetic suite passed, and no fastboot invocation occurred. The root-required Debian build, base/kernel/BusyBox inputs, production package assets, real Path A provenance, real image comparison and supervised hardware acceptance remain untested or unavailable. The four M0 baseline exceptions remain unchanged and are not passes. Their original baseline results were recorded earlier; the full installer runner does not rerun those failures. Root-required firewall/rootfs/sshd checks and optional artifact builds remain unrun.

Local milestone commits were not created. Selective staging stopped before any index change because the workspace mounts `.git` read-only; Git reported it could not create `.git/index.lock`. `git diff --cached --stat` was empty after the failure. Branch and HEAD remain `installer` at the accepted M4 SHA. Do not treat the uncommitted tree as a release commit; retry the accepted M4/M5/M6 commit sequence only in an owner-provided writable Git metadata workspace.

## Coordinator audit update (2026-11-15 UTC)

The workspace was already dirty at session start: it contained edits to M4/shared CLI, tests and documentation, plus untracked M4-M7 modules and status/handoff files. HEAD was exactly the accepted M4 SHA on branch `installer`, and that SHA is an ancestor of HEAD. No pre-existing work was reset or discarded. Git metadata remains read-only, so no milestone commits can be made here; production work is uncommitted.

The initial installer specification files `spec/INSTALLER-SPEC.md` and `spec/ACCEPTANCE.md` are absent. Therefore the canonical G1-G9 definitions and required mapping cannot be established from this copy. Do not claim full canonical G1-G9 acceptance.

M4 audit traced the actual formats: `50-boot-img.sh` emits Android boot v0 `boot.img`, and `51-rootfs-img.sh` emits raw ext4 `rootfs.img` for GPT-T `userdata`. The legacy first-install consumer expects sparse `boot.bin`/`rootfs.bin` and a GPT-B layout, then calls `simg2img`. The synthetic image-set adapter validates GPT-T geometry and hashes, preserves byte-identical boot/ext4 payloads, and maps them to GPT-T `boot`/`userdata`. It explicitly rejects routing those artifacts through the GPT-B path. The M4 suite passed 22/22. No valid GPT-T device write/readback integration exists; `flash` returns `HARDWARE_REQUIRED` and remains fail-closed. This is an offline synthetic compatibility path, not hardware support.

M5 cross-review: `_saved_completed_phases` requires matching device ID, backup ID and SHA-256, a strict prefix of the fixed phase order, and status/phase consistency. `_verify_completed_phase_bytes` hashes every skipped range in the preflight full live dump against the verified backup before a resumed write. `_known_rollback_state` constrains GPT/firmware state to the matching completed prefix. Negative tests cover wrong device, wrong backup/hash, reordered/inconsistent state and changed live bytes with no writes. Shim-backed call-order tests run the real `_live` helper and assert full `rf`, identity reads, dry-run gates and confirmations precede a resumed write. Late GPT-secondary reconciliation, partial-byte refusal, byte-identical restore and final-read timeout exit 3 are covered. Final focused suite passed 22/22.

M7 restore E2E passed in the focused final 14/14 suite. It exercises wizard doctor/backup/restore stages with synthetic EDL, all exact phase confirmations, recorded wizard/engine completion state and byte-identical full-disk result. It does not validate physical EDL, real USB transitions, GPT-T install flashing, or real boot. It is a synthetic restore success path, not a full stock-to-install hardware E2E.

M6 race repair/review: standalone restore and install runners traverse and open package members with no-follow descriptors, copy verified contents into a private snapshot, validate copied size/hash and metadata, and use the snapshot after validation. Synthetic regressions mutate source payloads after snapshot creation for both package types and confirm the private snapshot is unchanged. The final M6 suite passed. This is offline synthetic race evidence and does not establish behavior under every filesystem or physical workload.

No network, root, hardware, real EDL/fastboot/SSH, block device, owner backup or real firmware was accessed. No physical device flash was attempted. Overall status remains `PARTIAL_OFFLINE`: canonical spec/G1-G9 mapping, a safe GPT-T first-install write/readback path, clean launch tree restoration/ownership, and writable Git metadata for milestone commits remain outstanding.

## Coordinator continuation (2026-11-15, resumed M6)

The recovered canonical inputs are present one directory above this builder checkout at `../spec/INSTALLER-SPEC.md` and `../spec/ACCEPTANCE.md`. Earlier entries in this report and OPEN-QUESTIONS.md saying the canonical files were absent describe the previous audit's view and are superseded by this section. The restored spec defines G1-G9 as follows; each row links the implementation area to a negative/refusal test that exercises the gate.

| Gate | Source mapping | Refusal evidence |
|---|---|---|
| G1 verified backup and live identity before flash | `tools/installer/flash.py` backup and identity preflight | `tests/verify-installer-m4.py`: `test_missing_backup_cli_exit_code_2`, `test_flash_identity_mismatch_refused` |
| G2 dry-run, typed confirmation, interactive only | `tools/installer/flash.py`, `tools/installer/verify_restore.py`, standalone restore/install runners in `tools/installer/packages.py` | M4: `test_flash_wrong_confirmation_and_noninteractive_dry_only`; M5: `test_wrong_typed_confirmation_and_fwlib_test_override_do_not_write`; M6: non-TTY and wrong-confirmation standalone restore cases |
| G3 secrets absent from argv, logs, registry, events, and error text | `tools/installer/configure.py`, `tools/installer/seed.py`, `tools/installer/store.py`, `tools/installer/cli.py` | `tests/verify-installer-m3.py`: `secret-redaction-in-exceptions-registry-and-events`, `fake-root-apply-idempotency-and-secret-safe-logs`; M7: `test_destructive_exception_and_secret_text_not_written_to_state` |
| G4 prod seed without SSH key needs explicit acknowledgement | `tools/installer/configure.py` | M3 G4 refusal case: declining the explicit unreachable-prod confirmation is refused |
| G5 only tested board without override | `tools/installer/flash.py` and board validation helpers | M4: `test_flash_refusals_and_input_hashes`, including untested board refusal |
| G6 external blobs require pinned hashes; mismatch stops | `tools/installer/flash.py`, `tools/installer/packages.py`, `tools/installer/imageset.py` | M4: `test_flash_refusals_and_input_hashes`, `test_gpt_t_image_set_tampering_is_detected_and_flash_stops_hardware_required`; M5: `test_restore_rejects_unpinned_loader_and_live_identity_mismatch`; M6 package tamper/integrity cases |
| G7 stop at failure and do not retry uncertain writes | `tools/installer/flash.py`, `tools/installer/verify_restore.py`, portable restore/install scripts in `tools/installer/packages.py`, `tools/installer/wizard.py` | M4: `test_flash_dry_confirmation_protocol_and_failure_stop`, `test_interrupted_write_requires_reconciliation_and_is_not_retried`; M5: `test_partial_phase_bytes_block_reconciliation_without_retry`; M6: interrupted-write, alternate-state, completed-data-partition corruption, Git-worktree output refusal, and SENSITIVE README regressions |
| G8 private store/package outside Git; private permissions | `tools/installer/store.py`, `tools/installer/packages.py` | Full runner: `store-refuses-git`, `store-permissions`; M6 package output Git-tree refusal case |
| G9 never cache/store sudo; print exact privileged command first | `tools/installer/build.py` | M4: `test_build_generic_and_legacy_env_order_and_permissions` asserts command preview and sudo ordering; no sudo command was executed in this audit |

### Resumed M6 repair and review

The alternate state-path defense was already present. Standalone restore pins the canonical state path to the selected device and backup hash, rejects alternate `--state` paths before EDL calls, and the existing interrupted-write test proves only one actual fake write occurred. The partial-data gap was fixed in `tools/installer/packages.py`: before a later restore phase, every completed data partition (`userdata`, `cache`, `persist`, `boot`, `modemst1`, `modemst2`) is read in full and compared with the corresponding verified backup range. An added negative regression marks `userdata` complete, corrupts its fake live bytes, attempts `cache`, and confirms refusal before any actual write. Focused M6 passed after this change. Independent review accepted the final patch and confirmed that the regression reaches the full-range mismatch check before any actual write.

### Latest offline validation

M4 focused tests passed 23/23, M5 passed 22/22, M6 synthetic package checks passed including both M6 adversarial cases, and M7 passed 14/14. `bash tests/run-installer-tests.sh` exited 0 with 26 passes, 0 failures, and 0 runner-level skips. The runner's optional nested skips remain as described above. `git diff --check`, `bash -n` over every repository `.sh`, and `python3 -m py_compile` passed for all 16 installer and installer-test Python files. A whole-repository compile still fails on two untouched Python-2-era sources: `src/lk2nd/platform/at91sam7/mkboard.py` and `src/lk2nd/scripts/aboot_test.py`. ShellCheck availability was not rechecked in this continuation; prior audit recorded it unavailable.

The restored specs make G1-G9 mappable, but acceptance remains `PARTIAL_OFFLINE`: GPT-T first-install still returns `HARDWARE_REQUIRED`, physical board behavior and production inputs are unavailable, and the worktree is uncommitted because `.git` is read-only. No M0-M4 work was restarted. No dashboard, Windows support, hardware access, sudo, or network operation was performed.

### Commit boundary plan

No index or Git metadata was modified. In an owner-provided writable Git metadata workspace, first inspect the existing `installer` branch and stage only the already-intended milestone deltas. Suggested boundaries from the current dirty tree: retain accepted baseline M4 SHA `da4f9c8a2770b11b181611fe5ff525cd2d1be197`; make separate M5, M6, and M7 commits, with the required README/FLASH-PLAN GPT-T documentation correction in its own `docs: GPT-B -> GPT-T` commit. M6 includes `tools/installer/packages.py`, `tests/verify-installer-m6.py`, and the relevant M6 report/status/open-question updates; do not mark any boundary committed until `git status`, staged diff, commit SHA, and tests are checked in that writable workspace. Existing shared M4/M5/M7 files need deliberate per-commit staging based on their actual diffs; do not stage the whole tree as a single milestone.

### File-level staging boundaries for owner terminal

The existing uncommitted shared CLI and runner contain several milestones, so stage their hunks separately after reviewing each staged diff. Intended file ownership is:

- **M4 integration follow-up** (the dirty work already present atop accepted M4, not a restart): `tools/installer/cli.py`, `tools/installer/flash.py`, `tools/installer/imageset.py`, `tests/verify-installer-m4.py`, and only M4 hunks from `tests/run-installer-tests.sh`, plus M4-specific design notes. Preserve accepted M4 SHA as parent.
- **M5**: `tools/installer/verify_restore.py`, `tests/verify-installer-m5.py`, and only M5 CLI/runner/report hunks.
- **M6**: `tools/installer/packages.py`, `tests/verify-installer-m6.py`, and only M6 CLI/runner/report/status/open-question hunks.
- **M7**: `tools/installer/wizard.py`, `tests/verify-installer-m7.py`, and only M7 CLI/runner/report/status/handoff hunks.
- **Docs correction**: `README.md` and `FLASH-PLAN.md` GPT-T correction hunks, commit title exactly `docs: GPT-B -> GPT-T` as required by acceptance. If their quickstart edits share hunks, split by staging patch after reviewing the index.

`DESIGN.md`, `OPEN-QUESTIONS.md`, `INSTALLER-REPORT.md`, `AUTONOMOUS-STATUS.json`, `FINAL-HANDOFF.md`, `OWNERSHIP-MAP.md`, and `docs/INSTALLER.md` contain cross-milestone material and need hunk-level placement with the milestone they document. Do not include generated caches or the nested `src/linux-dts/.git` metadata. Before each commit, inspect `git diff --cached --stat` and `git diff --cached`; after each, record the resulting SHA and rerun the affected focused suite. This is a proposed safe staging map, not evidence that any commit exists.
