# OpenStick installer implementation report

## M0 status

| Milestone | Status | Commit |
|---|---|---|
| M0 Baseline and design | DONE | pending this commit |
| M1 Skeleton/store/doctor/shims | NOT STARTED | - |
| M2 Backup | NOT STARTED | - |
| M3 Configure/seed/inject | NOT STARTED | - |
| M4 Build/flash wrappers | NOT STARTED | - |
| M5 Verify/restore | NOT STARTED | - |
| M6 Packages | NOT STARTED | - |
| M7 Wizard/docs/tidy | NOT STARTED | - |

No installer implementation has been started. No existing script was changed. The clean source snapshot commit is `e4cf01b`; it is the root commit because no source history/baseline was included in the prepared workspace. There is no `baseline..installer` range.

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
| Installer features | None implemented in M0 | All installer operations remain unimplemented |

## Differences and questions

Specification section comparison and recommended defaults are in [OPEN-QUESTIONS.md](OPEN-QUESTIONS.md). Script interfaces, dependencies, architecture, external input pins and the seed integration decision are in [DESIGN.md](DESIGN.md).

Master prompt says each milestone must have green tests before advancing, while M0 explicitly says record existing test failures and do not fix unrelated failures. Resolution: the four documented legacy failures above are baseline exceptions, not passing tests. They do not block M0 if their causes are unrelated to M0 deliverables and accurately documented. Any new installer test failure blocks its milestone. The `verify-profile.sh` output mismatch remains a baseline issue to investigate if installer profile work relies on that exact message.

## Files added or modified

Added: `DESIGN.md`, `OPEN-QUESTIONS.md`, `INSTALLER-REPORT.md`.

Existing scripts modified: none.

## Known limitations

- Sanitized source copy has no original Git history, build outputs, private config, backups or physical device evidence.
- No build was run; boot/rootfs image hashes cannot be stated.
- Root-dependent tests were skipped, not passed.
- No claim is made that flashing, backup, restore, seed application or installer behavior works; those features are not implemented.
