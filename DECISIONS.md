# DECISIONS

Append-only. New entries go at the end, nothing is ever deleted. An old decision is superseded
by a new one, it is not rewritten.
Format: `- D-NN (YYYY-MM-DD) - <decision>. Why: <reason>. Affects: <where>`

## From the project plan and PRD (pre-2026-11-22)

- D-01 (pre-2026-11-22) - Boot path: community lk2nd (extlinux) + mainline msm8916 kernel
  (6.6, pmOS 6.6-r5, already run on the board; 6.12 = future option) + custom DTBs compiled
  from sources. Why: it is the only path that has actually booted on the hardware.
  Affects: `boards/`, `dts/`, `tools/pathA/`, the kernel packaging hook.
- D-02 (pre-2026-11-22) - Path A as a per-board fallback, chosen in each board's config file
  via `BOOT_MODE=mainline|stock-repack`: factory aboot + repacked stock boot.img.
  Why: some boards do not boot mainline; the choice must be data, not code.
  Affects: `boards/*/`, `config/`, `tools/pathA/`.
- D-03 (pre-2026-11-22) - Code base: fork of `kinsamanka/OpenStick-Builder` (branch main =
  Debian), slimming after the method in `snowy-yang/openstick-sp970-scripts`, kernel updatable
  after `qpzm1258/action-build-openstick-kernel` (a postinst repack hook).
  Why: reuse what already works instead of rewriting it.
  Affects: the whole tree, `tools/installer/`.
- D-04 (pre-2026-11-22) - One single rootfs for all boards. Per board only: DTB, lk2nd
  compatible, modem backup. `msm-firmware-loader.sh` mounts the firmware at runtime from the
  device partitions. Why: one image to build and test, not N.
  Affects: `src/`, `config/`, `tools/`.
- D-05 (pre-2026-11-22) - No web-ui in the base image. In the base only a small `osled`
  (list/set/trigger through `/sys/class/leds`) plus a oneshot service that applies the saved
  state. Optional UIs: `isShawnHsu/OpenStick-ZU801-WEB` (check the licence before
  redistribution), `murdokoja-max/openstick-config` (text menu).
  Why: keep the base small; extras are installed on demand.
  Affects: `src/`, `tools/installer/packages.py`, the per-board packages.
- D-06 (pre-2026-11-22) - No round 9b. The 7 known gaps (guided `restore` refusing after a
  partial firmware failure, the missing tests on those failures, the `KNOWN LIMIT` display, the
  missing "tzbak kept" mutation, the stale `_build_rollback_evidence` docstring, `_read_identity`
  without a guard, the offline mode without an explicit `_edl` guard) are NOT closed now;
  recovery stays manual via `pathA-rollback.sh` with the verified backup of board #6.
  Why: the gaps are in the failure paths; a rushed fix there is more dangerous than the gap.
  Affects: `tools/installer/`, `tools/pathA/`, `tests/`.
- D-07 (pre-2026-11-22) - The repo stays PRIVATE until it is reviewed. No push to a public
  remote. The private repo `github.com/chrsdme/OpenStick.git` is the intended home.
  Why: category 6 of the readiness checklist (real hashes, README, licences, personal-data scan,
  cleaned worktrees) is only 3/9 done.
  Affects: git remote setup, `.gitignore`, the T8 checklist.
- D-08 (pre-2026-11-22) - The pivot of 17 Nov: a functional installer first, the restrictions
  after. It does not change the permanent rules.
  Why: everything that is not "a working installer on a board" waits.
  Affects: the order of the phases in `PLAN.md`.
- D-09 (pre-2026-11-22) - Process: Codex implements, Claude Code reviews read-only, Cristian
  applies with sha256 + deny-count + `git apply --check` + the suite in the background, and a
  tag only after `TOTAL PASS=N FAIL=0 SKIP=0`.
  Why: agent reports are not evidence - Codex has already reported "DONE" on things it never ran.
  Affects: every task in `PLAN.md`.
- D-10 (pre-2026-11-22) - `config/user.env`, `config/local.env` and `config/precommit-deny.txt`
  are never read and never displayed by an agent. The deny-count only counts.
  Why: they hold personal configuration and the deny-list itself.
  Affects: `AGENTS.md` INV-9, every agent session.
- D-11 (pre-2026-11-22) - `tools/installer/verify_restore.py` is protected: not edited without
  explicit permission from Cristian.
  Why: it is the file that proves the way back to Android.
  Affects: `AGENTS.md` INV-10, the scope of every task.

## From the working rules (pre-2026-11-22)

- D-12 (pre-2026-11-22) - The tree wins over the spec. If the tree differs from what the spec
  describes, the difference is recorded in `OPEN-QUESTIONS.md` and the tree is followed.
  Why: the spec drifts, the code is what runs on the stick.
  Affects: `docs/spec/`, `OPEN-QUESTIONS.md`.

## From the Project OS installation (2026-11-22)

- D-13 (2026-11-22) - The contract lives in `builder/`, the repository that holds the code,
  not in the workspace `/home/cristian/openstick/openstick-codex`.
  Why: the guard compares changed files against the scope using the git repository it runs in.
  A contract in the workspace would leave the guard blind to every code change.
  Affects: `AGENTS.md`, `PLAN.md`, `PROGRESS.md`, `DECISIONS.md`, `tools/guard.py`.
- D-14 (2026-11-22) - `hq/projects/<p>/` is a MIRROR, written by the post-commit hook from the
  project. It is never edited by hand.
  Why: the next snapshot overwrites it. The source of truth is the project.
  Affects: `hq/tools/hq.py`, the install procedure.
- D-15 (2026-11-22) - The scope of a task is read from the LAST COMMIT of `PLAN.md`, never
  from the working tree. Why: an agent that widens its own scope mid-task gains nothing.
  Affects: `tools/guard.py`, the checkpoint rule in `AGENTS.md`.
- D-16 (2026-11-22) - `--no-verify` is never used, not even for the first commit. The guard
  hook is installed AFTER the first commit, so there is nothing to bypass.
  Why: the habit of bypassing the control is exactly what the system must eliminate.
  Affects: the install order, `tools/bootstrap.py`, `hooks/pre-commit`.
- D-17 (2026-11-22) - The workspace keeps its own git repository, but `builder/` and the
  worktrees `wt-*` are gitignored there.
  Why: `git add .` on embedded repositories records them as empty gitlinks instead of content.
  Affects: `/home/cristian/openstick/openstick-codex/.gitignore`.
- D-18 (2026-11-22) - The project's name in HQ is `openstick` (via `.hq-name`), not `builder`.
  Why: the folder name is an implementation detail; the project name is what shows up in the
  web chat. Affects: `.hq-name`, `hq anchor openstick`.
- D-19 (2026-11-22) - The private remote `github.com/chrsdme/OpenStick.git` is ALLOWED, and
  pushing to it is expected: it is the only off-machine copy of the code that exists.
  Why: D-07 forbids a PUBLIC remote until review, not a private one. Before the first push,
  `config/user.env`, `config/local.env` and `config/precommit-deny.txt` must be verified as
  untracked.
  Affects: `AGENTS.md` INV-8, the remote setup, the T8 checklist.

- D-20 (2026-11-22) - A verification command must print a VERDICT LINE. If it prints pages of
  evidence instead, it gets wrapped: run it to a log file, print one line with the verdict and
  the log path. The report carries the verdict; the log stays on disk.
  Why: the OpenStick suite prints 969 lines and no verdict, so the agent cannot check the
  "Done when" mechanically - it would either paste 969 lines into the chat or summarize, and
  summarizing is exactly what the contract forbids.
  Affected: `AGENTS.md` (Rules), `PLAN.md` (Verify lines), `verify.sh`.

## NU facem (blacklist - so they do not reappear "out of nowhere")

- Putting a web-ui, SMS forwarding or a dashboard in the base image - they are optional packages.
- Touching real hardware from an agent - no edl, no fastboot, no adb, no /dev, no sudo.
- Downloading firmware, loaders, images or packages - no network in the build agent.
- Rewriting the validated build, flash, rollback and initramfs logic - wrap it, or add a narrow
  hook, with a test and an entry in `INSTALLER-REPORT.md`.
- Real secrets anywhere in the tree - no WiFi keys, passwords, private keys, IMEI, GUIDs, serials.
- Debian 13 (trixie) before bookworm is proven on real hardware.
- A public remote, a force-push or a rewrite of the `baseline` commit.
- Editing `tools/installer/verify_restore.py` without explicit permission.
- Reading or displaying `config/user.env`, `config/local.env`, `config/precommit-deny.txt`.
- Closing the round 9b gaps now - recovery stays manual.
- Gitignoring `boards/*/board.env`, `config/defaults.env` or `config/dts-pin.env` - they are
  tracked board configuration. The secret scan flags them by name; that is a false positive.
- Editing the HQ mirror by hand - `hq/projects/<p>/` is written by the hook.
