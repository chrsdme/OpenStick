# PROGRESS

## State

- Current: P0 / T0
- Working tree: 13 files changed (6 pending round-10 files + the freshly installed contract)
- Last good commit: 8e50f79 easy: --offline mode (no stick, no writes, HARDWARE_REQUIRED markers) with tests
- Branch: `installer` (local markers: `round9-W`, `round9-V`, `round8-U`)
- Project progress (Cristian's own estimate): ~29% overall, ~42% of the main objective
  (a functional installer, proven on board #6)
- Proven on hardware so far: the Path A chain written and read back 7/7, boot with kernel 6.6,
  Debian 12 on eMMC, SSH by key, WiFi, LTE, NTP, watchdog, nft firewall, the failure path to
  lk1st in ~38 s, full reflash by fastboot with `cmp`, revert to Android with a byte-identical
  sha256 read-back
- Never proven on hardware: the installer writing to a board. The prod profile booting. LED.
  The failure path on an untouched Android `userdata`. Kernel 6.12 (never booted).

## Current task

ID: T0
Title: land the round-10 work in progress and install the contract
Why: the tree is dirty and the contract is empty; the guard cannot work on an unclear baseline.
Allowed files: `EASY-MODE-4-SUMMARY.md`, `fix-round-9.patch`, `fix-round-10.patch`,
`fix-round-10.patch.sha256`, `tests/groups/build-registration-symlinks.py`,
`tests/run-installer-tests.sh`, `docs/spec/**`
Done when: `bash tests/run-installer-tests.sh` passes and `git status` is clean
Do not touch: `src/**`, `openstick/**`, `tools/installer/**`, `tools/pathA/**`, `tools/flash/**`,
`scripts/**`, `boards/**`, `config/**`, `dts/**`

## Next 3 tasks (titles only)

- T1 - verify the round-10 patch against the baseline (SHA_OK, deny: 0, CHECK_OK)
- T2 - apply the patch and run the suite to green (TOTAL PASS=49 FAIL=0 SKIP=0)
- T3 - produce and register one real build on ai-pc

## Done (last 5)

- T-1 - easy: --offline mode (no stick, no writes, HARDWARE_REQUIRED markers) - commit 8e50f79
- T-2 - easy: Path A firmware chain + lk2nd composite boot, full read-back, region classification - commit 3c19e25
- T-3 - tests: easy round 8 guard groups (identity, plan, restore state, SIGINT) - commit 2390d64

## Verification already run (on the laptop, 2026-11-22)

- `bash tests/run-installer-tests.sh` -> GREEN: canary ok, selftest ok, 35 groups,
  189 PASS lines, 5 lines "FAIL ..." that are the negative tests proving the guards refuse
  (the `ok` right after each one is the unittest passing). The suite prints NO global verdict
  line, which is why the verification is wrapped by `verify.sh`.
  Verdict from the wrapper: `VERIFY: PASS | groups=35 checks=189 negative-evidence=5`.

## Observations (problems seen, NOT repaired, to be decided)

- **THE BASELINE IS MISSING FROM THIS REPOSITORY.** `git cat-file -t 611686a` answers "Not a
  valid object name", and the tag `r9-48of48` is not here either (only `baseline` and
  `round4-base` exist). The plan says the round-10 patch must be verified on `611686a`; that
  commit is not in this clone, whose HEAD is `8e50f79` (Codex's base). Where the canonical
  baseline lives - ai-pc, `~/openstick/builder`, or nowhere - is still open.
- **THE ROUND-10 PATCH IS ALREADY APPLIED in this working tree.** `tools/installer/build.py`
  and `tests/run-installer-tests.sh` are modified: the build-registration fix is in the tree,
  applied on `8e50f79`, not on `611686a` - a base the plan never cleared. Nothing is committed.
- **The plan names a different path.** It says the patch is applied on `~/openstick/builder`;
  this checkout is `~/openstick/openstick-codex/builder`. Same repo in two places, or two clones?
- **THE PUSH TO GITHUB HAPPENED BEFORE THE CONTRACT COMMIT.** The private remote
  `github.com/chrsdme/OpenStick.git` now has the `installer` branch plus the tags `baseline`
  and `round4-base` - the code has an off-machine backup - but without the contract and
  without the round-10 fix. Both are still local.
- **Before any push:** check `git ls-files config/` - `config/user.env`, `config/local.env` and
  `config/precommit-deny.txt` must not be tracked. The secret scan from `hq init` flagged
  `boards/ufi103s/board.env`, `config/defaults.env` and `config/dts-pin.env` BY NAME: those are
  tracked board configuration, NOT secrets. Do not gitignore them - the builder needs them.
- The 14 "password/key" content hits are false positives: the words appear in WiFi/PSK handling
  code.
- The workspace `/home/cristian/openstick/openstick-codex` used to track `builder/` and the
  worktrees `wt-A`..`wt-Y` as embedded repositories (empty gitlinks). They are now gitignored
  there. Cleaning the worktrees themselves is task T8.
- The ~40 `.md` files at the workspace root (EASY-MODE-PLAN, FIX-ROUND-*, REVIEW-ROUND-*,
  TEST-HARDENING-*, RESUME.md) are a hand-made memory system from before the contract. They stay
  as history; the contract takes over from here. Do not migrate them.
