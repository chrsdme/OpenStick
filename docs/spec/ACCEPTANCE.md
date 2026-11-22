# Acceptance criteria and test plan

Everything here must run offline, without root and without hardware. Real boards are never touched. If a check cannot be done under those limits, say so in INSTALLER-REPORT.md under "needs real hardware".

## Milestones (do them in order, one local commit each: "M<n>: <title>")

M0 Baseline and design
- Run every existing test that can run without root, hardware or network. Record the result of each (pass, fail, skipped and why) in INSTALLER-REPORT.md. Do not fix unrelated failures; list them.
- Write DESIGN.md: inventory of the existing scripts (inputs, outputs, env variables, exit codes), the list of external inputs with pinned hashes where known, any difference between the tree and spec/INSTALLER-SPEC.md, the seed decisions (keys, removal after apply, how apply-seed hooks into the build), and the build-hash baseline from spec 6.6.
- Acceptance: DESIGN.md exists, every spec section is either covered or has an entry in OPEN-QUESTIONS.md.

M1 Skeleton, private store, registry, doctor, shims
- `./openstick`, `tools/installer/` package, argument parsing, exit codes, logging without secrets.
- Private store creation (mode 700), refusal inside a git work tree, registry read/write, events.jsonl append, device list/show/label/note.
- `doctor` with the checks in spec section 4.
- Test framework and shims (see "Shims" below). `tests/run-installer-tests.sh` runs everything and exits non-zero on any failure.
- Acceptance: store inside a git tree is refused (exit 2); permissions are 700/600; registry round trip; doctor reports missing tools correctly when PATH is empty of them.

M2 Backup
- Backup through the fake EDL client: size check from the GPT, GPT CRC checks (reuse tools/rollback-plan.py code where possible instead of duplicating), sha256, identity fingerprints (fsg, sec, fsc), manifest, SHA256SUMS, registry entry, `--paranoid`, `--again`.
- Acceptance tests: good backup passes; truncated read fails with exit 3; corrupted GPT fails; second backup refused without `--again`; fake EDL failure mid-read leaves no half-registered backup; fingerprints match the fake disk's known content.

M3 Configure, seed, inject
- Seed parser (strict), `configure` (questions, hidden prompts, masked `--show`), seed written mode 600.
- `apply-seed` and `openstick-seed.service` added to the image build (narrow hook, no change to existing logic), tested as far as possible without root: shell-level tests of apply-seed against a fake root directory (parameterised root path, stubbed hostnamectl, timedatectl, nmcli, chpasswd).
- `seed inject` with debugfs into a COPY of a test ext4 image created with `mke2fs -t ext4 -d <dir>` (no root), then e2fsck -fn and read-back compare.
- Acceptance tests: invalid seed rejected with a clear message; secret never appears in any output or file other than the seed itself (G3 grep test); inject refuses to touch the generic original; read-back hash matches; prod seed without ssh key triggers G4; `seed push` refused when no ssh access can exist.

M4 Build wrapper and flash wrapper
- `build` wraps the existing scripts with a clean environment; generic build; `--bake-user-env` keeps the old behaviour. Root-requiring steps are not executed in tests (stub the build scripts, assert the environment they receive contains nothing personal).
- `flash` with gates G1, G2, G5, G7: dry-run first, typed confirmation, COMMIT=1 and CONFIRM=<token> exactly as the existing scripts require, stop on first failure, events per phase.
- Acceptance tests: refusal without backup (exit 2); refusal when the fake board's fingerprints differ from the backup's; refusal of any non-dry-run without the typed confirmation, including with `--yes`-style flags; wrong confirmation text refused; failure in phase N stops and does not run phase N+1; untested board refused without `--i-accept-untested`.

M5 Verify and restore
- `verify` over a fake ssh. `restore` driving tools/rollback-plan.py output, same gates, final full read and hash compare against the backup manifest, refusal to say "safe to boot" unless the hash matches.
- Acceptance tests: restore on the fake disk returns it byte-identical to the backup; a deliberately wrong final read is reported as failure (exit 3) and boot is NOT recommended; restore refuses when the board identity differs.

M6 Packages
- `package restore`, `package install`, `package verify`, standalone scripts inside the package.
- Acceptance tests: package verify passes on a fresh package and fails after flipping one byte; the packaged restore.sh and install.sh run against the fake EDL/fastboot with the same gates; packages are refused inside a git tree; SENSITIVE marking present in manifest and README.

M7 Wizard, docs, tidy
- `wizard` chaining the stages, resumable after an interrupted run (test by killing the fake EDL mid-flash and resuming).
- docs/INSTALLER.md; update README.md with a short quickstart linking to it. In README.md and FLASH-PLAN.md replace the obsolete "GPT-B" mentions with the current GPT-T description from ROLLBACK.md (docs-only change, separate commit "docs: GPT-B -> GPT-T").
- Acceptance: a full wizard run on the fake board (backup, configure, build stub, inject, flash, verify) ends with a coherent registry and event log.

## Shims (tests/shims/)
- `bin/edl`: fake EDL client with the subcommands the installer uses (`rf`, `rs`, `ws` with `--skipwrite`, and the identity read), operating on a small fake eMMC image file (GPT, partitions named like the real ones: fsg, sec, fsc, pad, boot, userdata, cache, persist, sbl1, rpm, tz, tzbak, hyp, aboot, modemst1, modemst2 ...), prints a Sahara-style header with a fake serial. Failure injection through environment variables (fail at Nth call, short read, corrupt byte).
- `bin/fastboot`, `bin/ssh`, `bin/scp`, `bin/sudo` (pass-through to the next command), and stubs for the build scripts.
- `make-fake-emmc.py`: generates the fake disk deterministically.
- Tests put `tests/shims/bin` first on PATH and must fail loudly if a real `edl` or `fastboot` would be used (for example: run with a PATH that contains only the shims plus the minimal system tools).

## Self-checks before finishing (all must pass, results go in INSTALLER-REPORT.md)
1. `tests/run-installer-tests.sh` exits 0; the existing tests that passed at M0 still pass.
2. `bash -n` on every .sh file; `python3 -m py_compile` on every .py file; shellcheck on the new shell files if installed.
3. Secret grep: no private key blocks, no non-empty WIFI_PSK/PASSWORD values other than the fake test values, no real-looking serials.
4. Size check: no file larger than 5 MB added; no images, dumps, loaders or firmware blobs in the tree.
5. `git status` clean on branch `installer`; `git log --oneline baseline..installer` shows one commit per milestone.
6. Every gate G1 to G9 in spec section 8 has at least one test that proves the refusal, listed by name in INSTALLER-REPORT.md.

## INSTALLER-REPORT.md must contain
- Status table per milestone (done, partial, not started) with the commit hash.
- Two tables per feature: "tested with shims" (test names) and "needs real hardware" (what exactly cannot be proven offline).
- Differences from the spec and why. Link to OPEN-QUESTIONS.md.
- List of files added or changed, and which existing scripts were touched, with the reason.
- Known limitations, written plainly.
