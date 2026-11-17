"""Guard coverage: every exec path must refuse an EDL client outside the test temp dir.

Each blocked case must raise the guard exception (a BaseException subclass) AND append a line to the
hit file. The deliberate hits go to a private file so they do not trip the
runner-wide hit file; the runner-wide variable is restored at the end.
"""
import os, pathlib, subprocess, sys
root, tmp = sys.argv[1:3]
tmp = pathlib.Path(tmp)
_runner_hits = os.environ["OPENSTICK_GUARD_HIT_FILE"]
hits = tmp / "guard-coverage-hits"
os.environ["OPENSTICK_GUARD_HIT_FILE"] = str(hits)
OUT = "/nonexistent-outside/edl"      # never executed: blocked before exec
OUTPY = "/nonexistent-outside/edl.py"
blocked = 0


def hit_lines():
    return len(hits.read_text().splitlines()) if hits.exists() else 0


def expect_block(label, fn):
    global blocked
    before = hit_lines()
    try:
        fn()
    except BaseException as exc:
        assert "TEST SAFETY GUARD" in str(exc), (label, str(exc))
    else:
        raise AssertionError("not blocked: " + label)
    assert hit_lines() == before + 1, ("hit not logged", label)
    blocked += 1


run = lambda argv: subprocess.run(argv, stdin=subprocess.DEVNULL, timeout=10)
envv = dict(os.environ)
# subprocess, looking through wrappers and shell strings
expect_block("plain", lambda: run([OUT, "rs", "0", "1", "x"]))
expect_block("sudo env", lambda: run(["sudo", "env", "X=1", OUT, "rs"]))
expect_block("sudo -u", lambda: run(["sudo", "-u", "root", OUT, "rs"]))
expect_block("env -i", lambda: run(["env", "-i", OUT]))
expect_block("env VAR=", lambda: run(["env", "A=1", "B=2", OUT]))
expect_block("timeout", lambda: run(["timeout", "5", OUT]))
expect_block("nice -n", lambda: run(["nice", "-n", "5", OUT]))
expect_block("python script", lambda: run(["python3", "-u", OUTPY]))
expect_block("bash -c", lambda: run(["bash", "-c", OUT + " rs 0 1 x"]))
expect_block("sh -c chain", lambda: run(["sh", "-c", "true && sudo env X=1 " + OUT]))
expect_block("bash -lc semicolon", lambda: run(["bash", "-lc", "cd /; " + OUT]))
expect_block("sudo bash -c", lambda: run(["sudo", "bash", "-c", "echo hi | " + OUT]))
expect_block("string shell", lambda: subprocess.run("sudo " + OUT + " rs", shell=True, timeout=10))
expect_block("loader outside", lambda: run([str(tmp / "shims/bin/edl"), "rs", "0", "1", "x", "--loader=/nonexistent-outside/l.mbn"]))
# os.system
expect_block("os.system", lambda: os.system(OUT + " rs"))
expect_block("os.system sudo env", lambda: os.system("sudo env X=1 " + OUT))
# os.exec*: blocked before the process image is replaced
expect_block("execv", lambda: os.execv(OUT, [OUT, "rs"]))
expect_block("execve", lambda: os.execve(OUT, [OUT, "rs"], envv))
expect_block("execvp", lambda: os.execvp(OUT, [OUT, "rs"]))
expect_block("execvpe", lambda: os.execvpe(OUT, [OUT, "rs"], envv))
expect_block("execl", lambda: os.execl(OUT, OUT, "rs"))
expect_block("execle", lambda: os.execle(OUT, OUT, "rs", envv))
expect_block("execlp", lambda: os.execlp(OUT, OUT, "rs"))
expect_block("execlpe", lambda: os.execlpe(OUT, OUT, "rs", envv))
expect_block("exec via bash -c", lambda: os.execv("/bin/bash", ["bash", "-c", OUT]))
# posix_spawn*, spawn*
expect_block("posix_spawn", lambda: os.posix_spawn(OUT, [OUT, "rs"], envv))
expect_block("posix_spawnp", lambda: os.posix_spawnp(OUT, [OUT, "rs"], envv))
expect_block("spawnv", lambda: os.spawnv(os.P_WAIT, OUT, [OUT, "rs"]))
expect_block("spawnve", lambda: os.spawnve(os.P_WAIT, OUT, [OUT, "rs"], envv))
expect_block("spawnvp", lambda: os.spawnvp(os.P_WAIT, OUT, [OUT, "rs"]))
expect_block("spawnvpe", lambda: os.spawnvpe(os.P_WAIT, OUT, [OUT, "rs"], envv))
expect_block("spawnl", lambda: os.spawnl(os.P_WAIT, OUT, OUT, "rs"))
expect_block("spawnle", lambda: os.spawnle(os.P_WAIT, OUT, OUT, "rs", envv))
expect_block("spawnlp", lambda: os.spawnlp(os.P_WAIT, OUT, OUT, "rs"))
expect_block("spawnlpe", lambda: os.spawnlpe(os.P_WAIT, OUT, OUT, "rs", envv))
assert blocked == 35, blocked
assert hit_lines() == blocked
# Harmless commands must still work (no over-blocking).
before = hit_lines()
assert run(["env", "A=1", "true"]).returncode == 0
assert run(["bash", "-c", "echo edl-notes >/dev/null; true"]).returncode == 0
assert os.system("true") == 0
assert os.spawnv(os.P_WAIT, "/bin/true", ["true"]) == 0
pid = os.posix_spawn("/bin/true", ["true"], envv)
assert os.waitpid(pid, 0)[1] == 0
assert hit_lines() == before
# An allowed in-tmp client through a wrapper is not blocked.
inside = tmp / "edl-coverage-inside.py"
inside.write_text("#!/usr/bin/env python3\nraise SystemExit(0)\n", encoding="ascii")
inside.chmod(0o700)
assert run(["env", "A=1", str(inside), "rs"]).returncode == 0
assert run(["bash", "-c", str(inside) + " rs"]).returncode == 0
assert hit_lines() == before
os.environ["OPENSTICK_GUARD_HIT_FILE"] = _runner_hits
