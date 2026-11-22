# AGENTS.md - OpenStick installer

> Read this whole file before touching anything. It is short on purpose.
> A rule that is not written here does not exist - propose it, do not invent it.
> Agent reports are NOT evidence. Only a run that produced output is evidence.

## Read first, in this order

1. `PLAN.md` - what we build and why (changes only with explicit OK)
2. `PROGRESS.md` - where we are NOW and which task is current
3. `DECISIONS.md` - what was already decided and what we do NOT do
4. `docs/spec/INSTALLER-SPEC.md` + `docs/spec/ACCEPTANCE.md` - the task itself

## Scope

One package, Linux only, with a single `openstick easy` command that on an MSM8916 board
(UFI103S first) backs up, prepares the images, writes the firmware chain that already worked
on board #6, and boots a slim Debian 12 - with a proven way back to Android.

## Out of scope (we do NOT do this now)

- Windows, DietPi, the GPT-B layout, kernel 6.12, kernel through apt, modem/LED dashboard, trixie.
- Publishing on GitHub. The repo stays private until reviewed.
- The other 9 boards (own backup and manual first firmware run each); editing protected files
  (see "Reguli permanente") without explicit permission.

## Invariants

- INV-1: No hardware access. Never run edl, fastboot, adb, or anything that touches USB
  devices, block devices or /dev nodes.
- INV-2: Never use sudo. Builds that need sudo/chroot are done by Cristian on ai-pc.
- INV-3: No network in the build agent. No downloading firmware, loaders, images or packages.
  Pinned sha256 values in code are fine; the blobs never enter the tree.
- INV-4: Private data never enters the repo or the cloud: dumps, IMEI, GUIDs, calibrations, PSK.
- INV-5: Never claim something works unless a test you ran shows it. Separate "tested with
  shims" from "needs real hardware"; mark an inference as an inference.
- INV-6: No refactoring, renaming or cleanup of code outside the current task. Do not rewrite
  proven build / flash / rollback / initramfs logic: wrap it, or add a narrow hook, with a test
  and an entry in INSTALLER-REPORT.md.
- INV-7: Python 3.9+ standard library only. Shell scripts pass `bash -n` (and shellcheck if
  installed). Linux only. English only: code, comments, UI text, docs. No em-dashes in docs.
- INV-8: git: local commits, small and descriptive, on branch `installer`. The private remote
  `github.com/chrsdme/OpenStick.git` is allowed; a PUBLIC one is not, until reviewed. No force.
- INV-9: Do not read or display `config/user.env`, `config/local.env`, `config/precommit-deny.txt`.
- INV-10: These files are protected: `tools/installer/verify_restore.py`. Editing one needs
  explicit permission from Cristian. If the task needs it: STOP and ask.
- INV-11: If a requirement is ambiguous, or two conflict, do not guess silently: add the question
  and your recommended default to OPEN-QUESTIONS.md, then continue with that default. If the tree
  differs from the spec, the tree wins - record the difference in OPEN-QUESTIONS.md.

## Reguli permanente (hardware, EDL, process)

- **Writes to the stick:** none without Cristian's explicit instruction. Exception: the Path A
  chain and `easy` mode, which run with a single YES, and the commands are run by Cristian.
- **EDL:** enter with the button held at plug-in. After EDL: 30 s out of USB, then cold boot
  without the button. NEVER `edl reset`. NEVER Ctrl+C during a fastboot or edl transfer.
  Writes with `--sectorsize=512`, no `--lun`, first `--skipwrite`, then read back with sha256.
- **Process:** Codex implements, Claude Code reviews read-only, Cristian applies with sha256,
  deny-count, `git apply --check` and the suite in the background. Tag only after
  `TOTAL PASS=N FAIL=0 SKIP=0`.
- **Reports are not evidence.** A tag is evidence.

## Rules

- You work on the CURRENT task in `PLAN.md` only. Nothing extra.
- You touch ONLY the files listed under "Scope". If you need others: STOP and ask.
- Do not invent requirements, rules, validations or "improvements" not in `PLAN.md`.
- If you notice a problem outside the task: NOTE it in `PROGRESS.md` -> Observations. Do not fix it.
- If the task contradicts `PLAN.md` or `DECISIONS.md`: STOP and say exactly where the conflict is.
- Before changing anything, say briefly: what you change, what else may be affected, how you verify.
- **A verification command must print a verdict line.** If it prints pages of evidence instead,
  wrap it: run it to a log file and print ONE line with the verdict and the log path. The report
  carries the verdict; the log stays on disk. Never paste hundreds of lines into the chat.

## Checkpoint (mandatory)

- **Before a task:** `git status` must be clean. If it is not, STOP and say so.
- **After a task:** one single commit, message `task <ID>: <what was done>`. Only after the
  verification passed.
- **Repairs: maximum 2 attempts.** If it is not fixed in 2: `git restore .`, go back to the last
  good commit and report. Do not keep patching.
- **A repair that needs files outside the scope = a NEW task**, with its own scope, approved by
  the human. Never "I also touched Y, it was needed".
- **The scope is read from the last COMMIT of `PLAN.md`**, not from the working tree.
- **Questions for the human** -> `OPEN-QUESTIONS.md`. A decision taken -> `DECISIONS.md`.

## Loop

1. Read `PLAN.md` and `PROGRESS.md`.
2. One short message: the goal, the current task, what you change, what may be affected.
3. Change the minimum.
4. Run the verification command. Paste the real output, not a summary of it.
5. Update `PROGRESS.md`, tick the box in `PLAN.md`, move the CURRENT pointer, commit.
   - **At the end of a task, tick the task AND move the `CURRENT` pointer to the next task** -
     only to a task that already exists in `PLAN.md`. If it does not exist yet, leave the
     pointer: do not invent tasks, and do not untick the box.
   - One single commit: code + tick + pointer + `PROGRESS.md`.
6. Anything you noticed and did not touch -> `PROGRESS.md` -> Observations.

## Final report (fixed format, short)

```
TASK: <ID> - <title>
STATUS: done / blocked / partial
CHANGED: <files>
VERIFIED: <command + real result>
AFFECTS ANYTHING ELSE?: yes/no + what
OBSERVATIONS (not repaired): <list or "none">
COMMIT: <hash or "no">
```

## The PENTRU CHAT package (mandatory on EVERY final answer)

Cristian copies your answers into a web chat that CANNOT see the repo. Every final answer ends
with one code block; TASK / SCOPE / GATA CAND / NU FACEM come from the files, never from memory.

```
=== PENTRU CHAT ===
PROIECT: openstick | ETAPA: <Px> | TASK: <T-nr> - <title>
SCOPE: <the allowed files>
GATA CAND: <the command and what you must see>
NU FACEM: <1-3 points from "Out of scope">
---
STATUS: done / blocked / partial / analysis only
CE AM FACUT: <2-4 lines, plain language>
SCHIMBAT: <files or "nothing">
VERIFICAT: <command> -> <real result, short>
AFECTEAZA ALTCEVA?: yes/no + what
OBSERVATII (nereparate): <list or "nothing">
COMMIT: <hash or "no">
INTREBARE PENTRU OM: <what must be decided, or "nothing"> (also write it in OPEN-QUESTIONS.md)
=== SFARSIT ===
```

If you receive a `=== PENTRU TERMINAL ===` block, that is your task. Before you start, check
whether it matches `PROGRESS.md`. If it does not, STOP and say exactly what the difference is.
Do not decide by yourself which one is right.

## Layout

- `src/`, `openstick/`, `tools/` - builder, flasher, per-board configuration
- `tests/` - the test suite (shims only, no hardware); `scripts/` - build scripts (root: read only)
- `boards/`, `config/`, `dts/` - board config, env files, DTBs; `docs/` - docs incl. `docs/spec/`
- `notes/`, `proposals/` - working material, not part of the contract

## Reference

- `PLAN.md` - phases, tasks, CURRENT pointer, verification commands; `PROGRESS.md` - state, next step
- `DECISIONS.md` - decision log (append-only) + blacklist; `OPEN-QUESTIONS.md` - questions
- `INSTALLER-REPORT.md` - what was built, tested and not tested; `FLASH-PLAN.md`, `ROLLBACK.md` - hashes
