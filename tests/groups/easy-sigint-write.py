"""easy (round 8): Ctrl-C during a write is ignored, and no edl client outlives easy mode.

A fake SLOW write (shim: OPENSTICK_EDL_SLOW_WRITE=<lba>:<seconds>) keeps the boot write running while the test types a
real Ctrl-C into the pty (the terminal sends SIGINT to easy AND to the edl client, as on a real terminal). The flash must
finish and verify, and 'do not interrupt, wait' must be printed. A SIGTERM at the same point ends easy mode, and the
recorded edl shell must be gone when easy has exited. Mutations: shield off, child reaping off."""
import os, pathlib, re, signal, sys, time
root, tmp = sys.argv[1:3]
sys.path.insert(0, str(pathlib.Path(root) / "tests"))
import easy_support as E

BOOT_LBA = 396384
board = E.Board(root, tmp, "sigint")
image_set = board.make_image_set()
args = [E.FLAG_IMAGE_SET, str(image_set), E.FLAG_YES]
PIDS = board.base / "edl-pids"
SLOW = {"OPENSTICK_EDL_SLOW_WRITE": "%d:6" % BOOT_LBA, "OPENSTICK_EDL_PID_FILE": str(PIDS)}


ALIVE_AT_EXIT = []      # recorded edl shell pids that were still alive the moment easy exited


def run(rule, extra_env=None):
    board.reset_disk()
    PIDS.unlink(missing_ok=True)
    ALIVE_AT_EXIT.clear()
    rules = E.base_rules(board, yes=True) + [rule]
    return E.happy(board, args, rules, env_overrides=dict(SLOW, **(extra_env or {})),
                   on_exit=lambda: ALIVE_AT_EXIT.extend(pid for pid in recorded_pids() if E.pid_alive(pid)))


def recorded_pids():
    return [int(line) for line in PIDS.read_text().split()] if PIDS.exists() else []


def check_survived(rc, out):
    assert rc == 0, "Ctrl-C during the write stopped easy mode (rc=%s):\n%s" % (rc, out[-1500:])
    assert "do not interrupt, wait" in out, "the 'do not interrupt, wait' note is not printed"
    assert "interrupted by the user" not in out and "Traceback" not in out
    E.check_layout(board.disk, board.initial, E.expected_overlays(image_set))
    assert len(recorded_pids()) == 1, "the slow write did not run exactly once"
    assert not ALIVE_AT_EXIT, "an edl shell is still running after easy exited: %s" % ALIVE_AT_EXIT


def check_terminated(rc, out):
    assert rc != 0, "SIGTERM did not stop easy mode"
    assert "terminated by signal 15" in out and "The board was being written" in out, out[-1200:]
    assert re.search(r"openstick --home .* restore --device", out), "no restore command after the signal"
    pids = recorded_pids()
    assert len(pids) == 1, "the slow write was not reached"
    assert not ALIVE_AT_EXIT, "the edl shell (pid %d) was still running when easy exited" % pids[0]


# 1. Real Ctrl-C (\x03 on the pty) while the boot write sleeps.
rc1, out1 = run(E.Rule(r"writing boot:", send=b"\x03", delay=1.5))
check_survived(rc1, out1)
assert E.registry(board)["devices"][0]["flashes"][-1]["outcome"] == "easy-pass-not-boot-verified"

# 1b. Two Ctrl-C in a row are both ignored.
rc1b, out1b = run(E.Rule(r"writing boot:", send=b"\x03\x03", delay=1.0))
check_survived(rc1b, out1b)

# 2. SIGTERM at the same point: easy exits, the edl shell does not survive.
rc2, out2 = run(E.Rule(r"writing boot:", kill=signal.SIGTERM, delay=1.5))
check_terminated(rc2, out2)
assert E.registry(board)["devices"][0]["flashes"][-1]["outcome"] == "partial-failure-reconciliation-required"

# 3. Mutation: shield off. Ctrl-C now reaches easy and the client, so the check_survived oracle must fail.
rc3, out3 = run(E.Rule(r"writing boot:", send=b"\x03", delay=1.5), {"OPENSTICK_TEST_MUTATE": "no-sigint-shield"})
E.must_fail(check_survived, rc3, out3)
assert rc3 != 0 or "do not interrupt, wait" not in out3, "shield-off mutation behaved like the shielded run"

# 4. Mutation: child reaping off. The edl shell outlives easy mode, so the check_terminated oracle must fail.
rc4, out4 = run(E.Rule(r"writing boot:", kill=signal.SIGTERM, delay=1.5), {"OPENSTICK_TEST_MUTATE": "no-child-reap"})
E.must_fail(check_terminated, rc4, out4)
assert ALIVE_AT_EXIT, "mutation run: the leftover shell was not seen alive, so the mutation proves nothing"
deadline = time.monotonic() + 12                           # hygiene: let the leftover shell finish by itself
while E.pid_alive(recorded_pids()[0]) and time.monotonic() < deadline:
    time.sleep(0.2)

# 5. Checker mutations.
E.must_fail(check_survived, rc1, out1.replace("do not interrupt, wait", "x"))
E.must_fail(check_survived, 130, out1)
E.must_fail(check_terminated, 0, out2)
E.must_fail(check_terminated, rc2, out2.replace("terminated by signal 15", "x"))
