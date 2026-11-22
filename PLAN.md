# PLAN

<!-- Source of truth for "what we are doing now". -->
<!-- Tasks are ticked as they finish. The CURRENT pointer moves to the next task, in the same commit as the tick. -->
<!-- The Scope list may span several lines, if the lines contain ONLY paths between backticks. -->
<!-- A line with prose after "Scope:" does not widen the scope. -->
<!-- Do not invent tasks. If the next task is not written here yet, leave the CURRENT pointer where it is. -->

CURRENT: P1 / T1

## Phases

### P0 - Clean baseline and contract

Goal: the repository has a committed baseline, a filled-in contract and the guard active.
Exit criteria: `python3 tools/guard.py --report` prints an anchor, `hq` lists `openstick`,
and `git status` is clean.

- [x] T0 - Land the round-10 work in progress and install the contract
      Scope: `EASY-MODE-4-SUMMARY.md`, `fix-round-9.patch`, `fix-round-10.patch`, `fix-round-10.patch.sha256`, `tests/groups/build-registration-symlinks.py`, `tests/run-installer-tests.sh`, `tools/installer/build.py`, `docs/spec/**`, `.gitignore`, `config/user.env`, `config/local.env`, `config/precommit-deny.txt`
      Do not touch: `src/**`, `openstick/**`, `tools/installer/verify_restore.py`, `tools/pathA/**`, `tools/flash/**`, `scripts/**`, `boards/**`, `config/defaults.env`, `config/dts-pin.env`, `dts/**`, `notes/**`, `proposals/**`
      Verify: `bash ~/os-contract/verify.sh`
      Done when: it prints `VERIFY: PASS` (the full log stays in /tmp), the three private
                 config files are NOT tracked by git, and `git status` is clean

### P1 - The round-10 patch, verified on the right base

Goal: the round-10 fix (the build registration bug in `build.py`) is proven to apply cleanly
on the baseline the project actually uses.
Exit criteria: the three outputs are `SHA_OK`, `deny: 0`, `CHECK_OK`.

- [ ] T1 - Verify the round-10 patch against the baseline
      Scope: `fix-round-10.patch`, `fix-round-10.patch.sha256`, `fix-round-9.patch`
      Do not touch: `src/**`, `openstick/**`, `tools/**`, `tests/**`, `scripts/**`, `config/**`, `boards/**`
      Verify: sha256 of the patch against `fix-round-10.patch.sha256`; `git apply --check` on the
               baseline named in OPEN-QUESTIONS.md; deny-count run by Cristian
      Done when: `SHA_OK`, `deny: 0` and `CHECK_OK` are pasted into the report

### P2 - Apply and prove the suite

Goal: the patch is applied and the whole suite is green on the real baseline.
Exit criteria: `TOTAL PASS=49 FAIL=0 SKIP=0`.

- [ ] T2 - Apply the patch and run the suite to green
      Scope: `src/**`, `tools/**`, `tests/**`, `INSTALLER-REPORT.md`
      Do not touch: `tools/installer/verify_restore.py`, `boards/**`, `config/**`, `dts/**`
      Verify: `bash ~/os-contract/verify.sh` (the full log stays in /tmp, read it only if it fails)
      Done when: it prints `VERIFY: PASS`, then one commit and one tag

### P3 - A real build on ai-pc

Goal: one real build is produced and registered, not just a dry run.
Exit criteria: the build line "Build ... registered" with an id.

- [ ] T3 - Produce and register one real build on ai-pc
      Scope: `tools/installer/build.py`, `tools/installer/**`, `INSTALLER-REPORT.md`
      Do not touch: `tools/installer/verify_restore.py`, `boards/**`, `config/**`
      Verify: `./openstick build --profile dev --board ufi103s`
      Done when: "Build ... registered" with an id, and the full output pasted

### P4 - image-set prepare

Goal: the image set for board #6 is prepared from a real build.
Exit criteria: the output directory holds a complete, hashed image set.

- [ ] T4 - Prepare the image set for board #6
      Scope: `tools/installer/**`, `docs/spec/**`
      Do not touch: `tools/installer/verify_restore.py`, `config/**`, `boards/**`
      Verify: `./openstick image-set prepare --build <ID> --layout ~/gpt-board6 --output <DIR>`
      Done when: the set is complete and every hash verifies

### P5 - easy --offline

Goal: `easy` accepts or refuses the real package and the real manifest, without the board.
Exit criteria: a clear accept or refuse, with the reason.

- [ ] T5 - Run easy --offline against the real package and image set
      Scope: `tools/installer/**`, `docs/spec/**`
      Do not touch: `tools/installer/verify_restore.py`, `config/**`, `boards/**`
      Verify: `./openstick easy --offline --fw-pkg ~/openstick-private/fwpkg-board6 --image-set <DIR>`
      Done when: the accept/refuse decision and its reason are pasted, whole

### P6 - The first real run on board #6

Goal: the installer writes to a real board for the first time, with a manual rollback at hand.
Exit criteria: Debian 12 boots on #6 from an image written by the installer.
NOTE: the agent does NOT touch the hardware. Cristian runs every command, at the board.

- [ ] T6 - Record the first real run on board #6
      Scope: `EASY-MODE-SUMMARY.md`, `INSTALLER-REPORT.md`, `docs/spec/**`
      Do not touch: `src/**`, `tools/**`, `tests/**`, `scripts/**`, `boards/**`, `config/**`
      Verify: the commands from "First real run on board #6" are run by Cristian, at the board
      Done when: the report states, separately, what was tested with shims and what was proven
                 on hardware; no step is claimed unless its output was pasted

### P7 - restore and the prod profile

Goal: the way back to Android is proven by the installer, then the prod profile boots.
Exit criteria: `restore` works on hardware and `prod` starts on the board.

- [ ] T7 - Prove restore and prod on board #6
      Scope: `EASY-MODE-SUMMARY.md`, `INSTALLER-REPORT.md`
      Do not touch: `src/**`, `tools/**`, `tests/**`, `scripts/**`, `boards/**`, `config/**`
      Verify: `restore` from the installer, then the prod profile started on the board
      Done when: both are proven on hardware, with outputs pasted

### P8 - The private repo, ready for review

Goal: the repository is clean enough that a public release is a decision, not a project.
Exit criteria: the checklist from category 6 is done.

- [ ] T8 - Repo hygiene for review
      Scope: `README.md`, `FLASH-PLAN.md`, `ROLLBACK.md`, `contrib/dtb/**`, `tools/rollback-plan.py`, `tools/pathA/pathA-rollback.sh`, `.gitignore`
      Do not touch: `src/**`, `tests/**`, `config/user.env`, `config/local.env`, `config/precommit-deny.txt`
      Verify: the hash commands from FLASH-PLAN.md, `tools/rollback-plan.py` and `pathA-rollback.sh`
      Done when: real hashes are in the docs, the GPT-B mentions are gone, the `*.dtb` licence is
                 settled, the personal-data scan is clean and the `wt-*` worktrees are gone

## Not in the plan yet (deliberately)

- The other 9 boards - each one gets its own backup and its own manual first firmware run.
- Round 9b gaps (guided `restore` after a partial firmware failure, the missing tests, the
  `tzbak kept` mutation, the stale docstring, the `_read_identity` guard, the offline guard):
  decision taken - not done, recovery stays manual.
- LED, regdb, kernel through apt, trixie, 6.6 vs 6.12, modem dashboard, `update` mode.
- Any public push.
