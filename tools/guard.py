#!/usr/bin/env python3
"""
guard.py — mechanical drift guard for a project that uses the Project OS contract.

The agent's good intentions are not a control. This script is the control: it checks,
without reading a single chat message, whether the working session respects the contract
written in AGENTS.md / PLAN.md / PROGRESS.md / DECISIONS.md.

Modes
  guard              after-work check: did this session stay inside the contract?
  guard --pre        pre-flight check: is it safe to start a task right now?
  guard --report     print the anchor block for the current task (paste-ready)

Checks (default mode)
  1. contract      the four contract files exist
  2. brevity      AGENTS.md and PLAN.md stay short enough to be read in one sitting
  3. pointer      exactly one CURRENT pointer in PLAN.md, and PROGRESS.md agrees with it
  4. scope        changed files stay inside the declared Scope of the current task
  5. avoid        changed files do not touch the task's "Nu atinge" list
  6. frozen       the contract was not rewritten mid-task (no scope widening)
  7. append-only  DECISIONS.md only grows; old decisions are never rewritten
  8. hygiene      working tree is committed; current task is not already ticked

Checks (--pre mode)
  1. contract      the four contract files exist
  2. pointer      PLAN.md and PROGRESS.md agree on the current task
  3. clean        the working tree has no uncommitted changes
  4. not-done     the current task is not already ticked

Usage
  python3 guard.py [--project DIR] [--pre | --report] [--quiet]

Exit codes
  0  contract honoured
  1  drift detected (violations listed)
  2  could not run (not a project, missing contract files)
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import unicodedata
from pathlib import Path

CONTRACT_FILES = ("AGENTS.md", "PLAN.md", "PROGRESS.md", "DECISIONS.md")

# Contract files that DEFINE THE SCOPE. Editing them while code changes are in flight is
# how a task silently widens its own scope — the agent relaxes its own boundary and
# nothing notices, because PLAN.md is always allowed to change.
# DECISIONS.md and OPEN-QUESTIONS.md are contract too, but they are append-only and
# don't define scope, so they're checked by their own rules instead.
# PROGRESS.md is excluded on purpose: recording the result of a task is its job.
FROZEN_CONTRACT = ("AGENTS.md", "PLAN.md")

# Bookkeeping and infrastructure files are always allowed to change — they are how a
# session is recorded, and the tooling must never block itself.
ALWAYS_ALLOWED = (
    "AGENTS.md",
    "PLAN.md",
    "PROGRESS.md",
    "DECISIONS.md",
    "CLAUDE.md",
    "COORDINATOR.md",
    "README.md",
    "CHANGELOG.md",
    "PROJECTS.md",
    "OPEN-QUESTIONS.md",
    ".hq-name",
    "docs/sessions/",
    ".scratch/",
    "tools/",
)

LINE_CAPS = {"AGENTS.md": 150, "PLAN.md": 400}

CURRENT_RE = re.compile(r"^CURRENT:\s*(\S+)\s*/\s*(\S+)\s*$", re.M)
PROGRESS_CURRENT_RE = re.compile(r"^\s*-\s*Current:\s*(\S+)\s*/\s*(\S+)", re.M)
BACKTICK_RE = re.compile(r"`([^`]+)`")
TASK_LINE_RE = r"^\s*[-*]?\s*\[[ xX]\]\s*(?P<id>\S+)"


# --------------------------------------------------------------------------- helpers


def norm(text: str) -> str:
    """Lowercase and strip diacritics, so section headers match in any spelling."""
    stripped = unicodedata.normalize("NFKD", text)
    return "".join(c for c in stripped if not unicodedata.combining(c)).lower()


def git(root: Path, *args: str) -> str | None:
    """Run a git command; return stdout, or None if git failed / not a repo."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate a path glob to a regex.

    '**' spans any number of path segments, '*' stays inside one segment,
    '?' is a single character. Patterns are matched against repo-relative POSIX paths.
    """
    out = ["^"]
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    out.append("$")
    return re.compile("".join(out))


def path_allowed(path: str, patterns: list[str]) -> bool:
    for pattern in patterns:
        p = pattern.strip().rstrip("/")
        if not p:
            continue
        if path == p or path.startswith(p + "/"):
            return True
        if glob_to_regex(p).match(path):
            return True
    return False


def changed_files(root: Path) -> list[str] | None:
    """All paths touched in the working tree (staged, unstaged, untracked)."""
    out = git(root, "status", "--porcelain", "-uall")
    if out is None:
        return None
    files: list[str] = []
    for line in out.splitlines():
        if len(line) < 4:
            continue
        raw = line[3:].strip()
        if " -> " in raw:  # rename
            raw = raw.split(" -> ")[-1].strip()
        raw = raw.strip('"')
        if raw:
            files.append(raw)
    return files


def uncommitted_files(root: Path) -> list[str] | None:
    """Paths that are NOT part of the commit being made.

    Staged changes belong to the commit; unstaged modifications and untracked files do not.
    Without this split, the hygiene warning fires on every single commit (the staged files
    are exactly what is being committed) and a warning that always fires is a warning the
    agent learns to ignore.
    """
    out = git(root, "status", "--porcelain", "-uall")
    if out is None:
        return None
    files: list[str] = []
    for line in out.splitlines():
        if len(line) < 4:
            continue
        status = line[:2]
        if status == "??":
            raw = line[3:].strip()
        elif status[1] != " ":
            raw = line[3:].strip()
        else:
            continue
        if " -> " in raw:  # rename
            raw = raw.split(" -> ")[-1].strip()
        raw = raw.strip('"')
        if raw:
            files.append(raw)
    return files


def extract_paths(line: str) -> list[str]:
    """Backticked paths if present, else the comma-separated list after the colon."""
    backticked = BACKTICK_RE.findall(line)
    if backticked:
        return backticked
    _, _, rest = line.partition(":")
    return [p.strip() for p in rest.split(",") if p.strip()]


def is_path_list(line: str) -> bool:
    """True when the line contains nothing but backticked paths, commas and spaces.

    Used to tell a Scope continuation line apart from ordinary prose. Prose that happens
    to mention a path ("the `scripts/` folder is read-only") must NOT widen the scope.
    """
    rest = BACKTICK_RE.sub("", line).replace("`", "").replace(",", "").strip()
    return rest == ""


class Task:
    """The current task, as declared in PLAN.md."""

    def __init__(self) -> None:
        self.task_id: str | None = None
        self.phase: str | None = None
        self.title: str = ""
        self.scope: list[str] = []
        self.avoid: list[str] = []
        self.verify: str = ""
        self.done_when: str = ""


def parse_task_block(plan_text: str, task_id: str | None) -> Task:
    """Read the current task's block out of PLAN.md.

    A plan holds one Scope and one "Nu atinge" line per task. Collecting them all would
    let a change land anywhere in the project, which defeats the point: the scope of the
    *current* task is the only boundary that means anything right now.
    """
    task = Task()
    task.task_id = task_id
    lines = plan_text.splitlines()
    if not task_id:
        return task

    start = None
    for idx, line in enumerate(lines):
        m = re.match(TASK_LINE_RE, line)
        if m and m.group("id") == task_id:
            start = idx
            task.title = line.strip().lstrip("-*[] xX").strip()
            # strip the leading "[ ] T7 —" marker from the title
            task.title = re.sub(r"^[^\s]+\s*[-—:]\s*", "", task.title).strip()
            break
    if start is None:
        return task

    last_key = ""
    for line in lines[start + 1 :]:
        if re.match(TASK_LINE_RE, line):  # next task starts here
            break
        if re.match(r"^#{1,6}\s", line):  # next phase starts here
            break
        stripped = line.strip()
        low = norm(stripped)
        if low.startswith("scope:"):
            task.scope.extend(extract_paths(stripped))
            last_key = "scope"
        elif low.startswith("nu atinge:") or low.startswith("do not touch:"):
            task.avoid.extend(extract_paths(stripped))
            last_key = "avoid"
        elif low.startswith("verify:"):
            task.verify = stripped.split(":", 1)[1].strip()
            last_key = ""
        elif low.startswith("done when:") or low.startswith("gata cand:"):
            task.done_when = stripped.split(":", 1)[1].strip()
            last_key = "done_when"
        elif last_key in ("scope", "avoid") and is_path_list(stripped):
            # A Scope / "Nu atinge" list that spans several lines. Only lines that are
            # nothing but backticked paths count, so prose can never widen the scope.
            paths = BACKTICK_RE.findall(stripped)
            if last_key == "scope":
                task.scope.extend(paths)
            else:
                task.avoid.extend(paths)
        elif task.done_when and not low.startswith(("scope:", "nu atinge:", "verify:", "done when:", "gata cand:")):
            # continuation of the previous "Done when" line
            task.done_when = f"{task.done_when} {stripped}"
    return task


def parse_non_goals(agents_text: str) -> list[str]:
    """Bullets from the 'In afara scopului' / 'Non-goals' section of AGENTS.md."""
    lines = agents_text.splitlines()
    out: list[str] = []
    in_section = False
    for line in lines:
        if line.startswith("#"):
            head = norm(line.lstrip("#").strip())
            if in_section:
                break
            in_section = (
                "in afara scopului" in head
                or head.startswith("non-goal")
                or head.startswith("non goal")
                or head.startswith("out of scope")
            )
            continue
        if in_section and re.match(r"^\s*[-*]\s+\S", line):
            out.append(line.strip().lstrip("-*").strip())
    return [o for o in out if o]


def parse_any_scope(text: str) -> list[str]:
    """The first Scope line found anywhere in a document."""
    for line in text.splitlines():
        stripped = line.strip()
        if norm(stripped).startswith("scope:"):
            patterns = extract_paths(stripped)
            if patterns:
                return patterns
    return []


def task_in_plan(plan_text: str, task_id: str | None) -> bool:
    """Is the current task actually a task line in PLAN.md?"""
    if not task_id:
        return False
    for line in plan_text.splitlines():
        m = re.match(TASK_LINE_RE, line)
        if m and m.group("id") == task_id:
            return True
    return False


def current_task_ticked(plan_text: str, task_id: str) -> bool:
    """Is the current task's checkbox already ticked in PLAN.md?"""
    for line in plan_text.splitlines():
        m = re.match(TASK_LINE_RE, line)
        if m and m.group("id") == task_id:
            return "[x]" in line.lower()
    return False


# ---------------------------------------------------------------------------- report


class Report:
    def __init__(self) -> None:
        self.violations: list[str] = []
        self.warnings: list[str] = []
        self.checks: list[tuple[str, str]] = []

    def ok(self, name: str, detail: str = "") -> None:
        self.checks.append((name, detail))

    def fail(self, name: str, detail: str) -> None:
        self.violations.append(f"{name}: {detail}")

    def warn(self, name: str, detail: str) -> None:
        self.warnings.append(f"{name}: {detail}")


# ------------------------------------------------------------------------------ run


def read_contract(root: Path) -> tuple[str, str, str, str]:
    return (
        (root / "AGENTS.md").read_text(encoding="utf-8", errors="replace"),
        (root / "PLAN.md").read_text(encoding="utf-8", errors="replace"),
        (root / "PROGRESS.md").read_text(encoding="utf-8", errors="replace"),
        (root / "DECISIONS.md").read_text(encoding="utf-8", errors="replace"),
    )


def check_pointer(rep: Report, plan: str, progress: str) -> tuple[str | None, str | None]:
    """Resolve the current task and make sure PLAN and PROGRESS agree on it."""
    pointers = CURRENT_RE.findall(plan)
    if not pointers:
        rep.fail(
            "pointer",
            "PLAN.md has no CURRENT pointer — expected a line like 'CURRENT: P0 / T0'",
        )
        return None, None
    if len(pointers) > 1:
        rep.fail(
            "pointer",
            f"PLAN.md has {len(pointers)} CURRENT pointers "
            f"({', '.join(f'{p}/{t}' for p, t in pointers)}) — exactly one task can be current",
        )
        current = pointers[0]
    else:
        current = pointers[0]
        rep.ok("pointer", f"CURRENT = {current[0]} / {current[1]}")

    prog_current = PROGRESS_CURRENT_RE.findall(progress)
    if not prog_current:
        rep.fail(
            "pointer",
            "PROGRESS.md has no '- Current: P0 / T0' line — the agent cannot tell where it is",
        )
    elif prog_current[0] != current:
        rep.fail(
            "pointer",
            f"PROGRESS.md says {prog_current[0][0]} / {prog_current[0][1]} but PLAN.md says "
            f"{current[0]} / {current[1]} — they drifted apart",
        )
    else:
        rep.ok("pointer", "PROGRESS.md agrees with PLAN.md")

    return current[0], current[1]


def run_preflight(root: Path) -> Report:
    """Checks that must pass BEFORE a task starts."""
    rep = Report()

    missing = [f for f in CONTRACT_FILES if not (root / f).is_file()]
    if missing:
        rep.fail(
            "contract",
            f"missing {', '.join(missing)} — run bootstrap.py to create the contract",
        )
        return rep
    rep.ok("contract", "all four contract files present")

    _, plan, progress, _ = read_contract(root)
    _, task_id = check_pointer(rep, plan, progress)

    if task_id and not task_in_plan(plan, task_id):
        rep.fail(
            "pointer",
            f"current task {task_id} is not a task line in PLAN.md — the plan and the CURRENT "
            "pointer have drifted apart",
        )

    if task_id and current_task_ticked(plan, task_id):
        rep.fail(
            "not-done",
            f"current task {task_id} is already ticked in PLAN.md — advance the CURRENT pointer "
            "or untick the task before starting",
        )

    touched = changed_files(root)
    if touched is None:
        rep.warn("clean", "not a git repository — cannot verify the working tree is clean")
    elif touched:
        rep.fail(
            "clean",
            f"working tree has {len(touched)} uncommitted change(s): {', '.join(touched[:8])}"
            + (" …" if len(touched) > 8 else "")
            + " — commit or restore them first, otherwise there is no clean baseline to return to",
        )
    else:
        rep.ok("clean", "working tree is clean")

    return rep


def run(root: Path) -> Report:
    rep = Report()

    missing = [f for f in CONTRACT_FILES if not (root / f).is_file()]
    if missing:
        rep.fail(
            "contract",
            f"missing {', '.join(missing)} — run bootstrap.py to create the contract",
        )
        return rep
    rep.ok("contract", "all four contract files present")

    agents, plan, progress, decisions = read_contract(root)

    # brevity
    for fname, cap in LINE_CAPS.items():
        text = agents if fname == "AGENTS.md" else plan
        n = len(text.splitlines())
        if n > cap:
            rep.warn(
                "brevity",
                f"{fname} has {n} lines (cap {cap}) — past this, agents skim instead of read",
            )
        else:
            rep.ok("brevity", f"{fname} {n}/{cap} lines")

    _, task_id = check_pointer(rep, plan, progress)

    if task_id and not task_in_plan(plan, task_id):
        rep.fail(
            "pointer",
            f"current task {task_id} is not a task line in PLAN.md — the plan and the CURRENT "
            "pointer have drifted apart",
        )

    # Scope-ul se citeste din PLAN.md asa cum era la ULTIMUL COMMIT, nu din working tree.
    # Asa, un agent care isi largeste scope-ul editand PLAN.md in timpul task-ului nu castiga
    # nimic, iar bifarea task-ului + mutarea CURRENT in acelasi commit cu codul ramane permisa.
    plan_head = git(root, "show", "HEAD:PLAN.md")
    head_ptr = CURRENT_RE.findall(plan_head) if plan_head else []
    scope_task_id = head_ptr[0][1] if head_ptr else task_id
    task = parse_task_block(plan_head if head_ptr else plan, scope_task_id)
    if head_ptr:
        now_task = parse_task_block(plan, scope_task_id)
        if (now_task.scope, now_task.avoid) != (task.scope, task.avoid):
            rep.warn(
                "frozen",
                f"Scope/'Nu atinge' pentru {scope_task_id} a fost schimbat in PLAN.md; guard-ul foloseste "
                "varianta din ultimul commit. O schimbare de scope = task nou, comis separat.",
            )

    # scope + avoid list
    scope = task.scope or parse_any_scope(agents)
    if not scope:
        rep.warn(
            "scope",
            f"no Scope declared for the current task{' (' + task_id + ')' if task_id else ''} — "
            "nothing constrains where changes may land",
        )

    touched = changed_files(root)
    if touched is None:
        rep.warn("scope", "not a git repository — scope cannot be checked against changes")
    else:
        always = list(ALWAYS_ALLOWED)
        outside = [f for f in touched if not path_allowed(f, scope) and not path_allowed(f, always)]
        if outside:
            rep.fail(
                "scope",
                f"{len(outside)} changed file(s) outside the scope of {task_id} "
                f"(allowed: {', '.join(scope)}): {', '.join(outside[:8])}"
                + (" …" if len(outside) > 8 else ""),
            )
        else:
            rep.ok("scope", f"all {len(touched)} changed file(s) inside scope of {task_id}")

        if task.avoid:
            violated = [f for f in touched if path_allowed(f, task.avoid)]
            if violated:
                rep.fail(
                    "avoid",
                    f"{len(violated)} changed file(s) are on the 'Nu atinge' list of {task_id} "
                    f"({', '.join(task.avoid)}): {', '.join(violated[:8])}",
                )
            else:
                rep.ok("avoid", f"no file on the 'Nu atinge' list of {task_id} was touched")


    # DECISIONS.md is append-only
    old_decisions = git(root, "show", f"HEAD:DECISIONS.md")
    if old_decisions is None:
        rep.ok("append-only", "DECISIONS.md not yet committed — nothing to compare")
    elif not decisions.startswith(old_decisions):
        old_lines = old_decisions.splitlines()
        new_lines = decisions.splitlines()
        kept = 0
        for a, b in zip(old_lines, new_lines):
            if a == b:
                kept += 1
            else:
                break
        rep.fail(
            "append-only",
            f"DECISIONS.md was rewritten (first divergence at line {kept + 1}) — decisions are "
            "append-only; supersede with a new D-NN instead of editing an old one",
        )
    else:
        rep.ok("append-only", "DECISIONS.md grew only")

    # hygiene
    leftovers = uncommitted_files(root)
    if leftovers is None:
        rep.warn("hygiene", "not a git repository — cannot check for leftovers")
    elif leftovers:
        rep.warn(
            "hygiene",
            f"{len(leftovers)} change(s) left out of the commit — commit them or restore them "
            "before the session ends",
        )
    else:
        rep.ok("hygiene", "nothing left out of the commit")

    if task_id and current_task_ticked(plan, task_id):
        # ATENTIE: asta e o AVERTIZARE, nu un blocaj. Blocajul ar refuza commitul de
        # final de faza (cand nu exista inca un task urmator in PLAN.md de care sa
        # avanseze pointerul CURRENT), iar agentul ar inventa --no-verify - exact
        # obiceiul pe care sistemul trebuie sa-l elimine. In modul --pre ramane blocaj:
        # acolo chiar nu vrei sa incepi un task deja bifat.
        rep.warn(
            "hygiene",
            f"taskul curent {task_id} este bifat, dar pointerul CURRENT tot la el indica — "
            "avanseaza-l la urmatorul task din PLAN.md (sau lasa-l, daca urmatorul task "
            "nu a fost scris inca)",
        )

    return rep


def report(root: Path) -> int:
    """Print the anchor block for the current task — paste-ready for any chat."""
    missing = [f for f in CONTRACT_FILES if not (root / f).is_file()]
    if missing:
        print(f"guard: missing {', '.join(missing)}", file=sys.stderr)
        return 2

    agents, plan, progress, _ = read_contract(root)
    pointers = CURRENT_RE.findall(plan)
    if not pointers:
        print("guard: PLAN.md has no CURRENT pointer", file=sys.stderr)
        return 2
    phase, task_id = pointers[0]
    task = parse_task_block(plan, task_id)

    non_goals = parse_non_goals(agents)
    scope = task.scope or parse_any_scope(agents)

    done = task.done_when or task.verify
    if task.verify and task.done_when:
        done = f"{task.verify} → {task.done_when}"
    done = " ".join(done.split())

    def shorten(text: str, limit: int = 70) -> str:
        first = text.split(". ")[0].strip().rstrip(".")
        return first if len(first) <= limit else first[: limit - 1].rstrip() + "…"

    print(f"TASK: {task_id} — {task.title}" if task.title else f"TASK: {task_id}")
    print(f"SCOPE: {', '.join(scope) if scope else '(neclarit — vezi PLAN.md)'}")
    print(f"NU FACEM: {'; '.join(shorten(g) for g in non_goals[:2]) if non_goals else '(din AGENTS.md)'}")
    print(f"GATA CÂND: {done if done else '(vezi PLAN.md)'}")
    print(f"[faza {phase}]")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Check that this session respects the project contract."
    )
    ap.add_argument("--project", default=".", help="project root (default: cwd)")
    ap.add_argument(
        "--pre",
        action="store_true",
        help="pre-flight mode: is it safe to start a task right now?",
    )
    ap.add_argument(
        "--report",
        action="store_true",
        help="print the anchor block for the current task (paste-ready)",
    )
    ap.add_argument("--quiet", action="store_true", help="only print violations")
    args = ap.parse_args()

    root = Path(args.project).expanduser().resolve()
    if not root.is_dir():
        print(f"guard: {root} is not a directory", file=sys.stderr)
        return 2

    if args.report:
        return report(root)

    rep = run_preflight(root) if args.pre else run(root)

    if not args.quiet:
        for name, detail in rep.checks:
            print(f"  ok    {name}: {detail}")

    for w in rep.warnings:
        print(f"  warn  {w}")
    for v in rep.violations:
        print(f"  DRIFT {v}")

    if rep.violations:
        if args.pre:
            print(f"\nguard: {len(rep.violations)} blocker(s) — do not start the task yet.")
        else:
            print(f"\nguard: {len(rep.violations)} violation(s) — the session drifted from the contract.")
        return 1

    if rep.warnings:
        print(f"\nguard: contract honoured, {len(rep.warnings)} warning(s).")
        return 0

    print("\nguard: contract honoured." if not args.pre else "\nguard: ready to start.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
