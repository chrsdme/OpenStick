"""easy (round 9): Ctrl-C during EVERY write phase of the firmware chain is ignored (INTERRUPT_NOTE), the chain finishes and
verifies, and no edl client outlives easy mode. Usage: easy-fw-sigint.py ROOT TMP [FIRST LAST] = phases FIRST..LAST of
gpt secondary, gpt primary, tz, rpm, sbl1, hyp, aboot, boot (default all). Mutation: shield off (first phase of the slice)."""
import pathlib, sys
root, tmp = sys.argv[1:3]
first, last = (int(sys.argv[3]), int(sys.argv[4])) if len(sys.argv) >= 5 else (0, 7)
sys.path.insert(0, str(pathlib.Path(root) / "tests"))
import easy_support as E

PHASES = [("gpt secondary", str(E.EMMC_SECTORS - 33)), ("gpt primary", "0")] + \
         [(n, str(E.PATHA_REGIONS[n][0])) for n in E.PATHA_ORDER] + [("boot", "396384")]
board = E.Board(root, tmp, "fwsigint%d" % first)
image_set = board.make_image_set()
args = [E.FLAG_IMAGE_SET, str(image_set), E.FLAG_YES]
PIDS = board.base / "edl-pids"
ALIVE = []


def pids():
    return [int(x) for x in PIDS.read_text().split()] if PIDS.exists() else []


def run(label, lba, extra_env=None):
    board.reset_disk()
    PIDS.unlink(missing_ok=True)
    ALIVE.clear()
    rules = E.base_rules(board, yes=True) + [E.Rule(r"writing %s:" % label, send=b"\x03", delay=1.0)]
    env = dict({"OPENSTICK_EDL_SLOW_WRITE": "%s:3" % lba, "OPENSTICK_EDL_PID_FILE": str(PIDS)}, **(extra_env or {}))
    return E.happy(board, args, rules, env_overrides=env, on_exit=lambda: ALIVE.extend(p for p in pids() if E.pid_alive(p)))


def check_survived(rc, out):
    assert rc == 0, "Ctrl-C during the write stopped easy mode (rc=%s):\n%s" % (rc, out[-1200:])
    assert "do not interrupt, wait" in out, "the 'do not interrupt, wait' note is not printed"
    assert "interrupted by the user" not in out and "Traceback" not in out
    E.check_layout(board.disk, board.initial, E.expected_overlays(image_set))
    assert len(pids()) == 1, "the slow write did not run exactly once"
    assert not ALIVE, "an edl shell is still running after easy exited"


kept = None
for index in range(first, last + 1):
    label, lba = PHASES[index]
    rc, out = run(label, lba)
    check_survived(rc, out)
    kept = kept or (label, lba, rc, out)

label, lba, rc, out = kept
E.must_fail(check_survived, rc, out.replace("do not interrupt, wait", "x"))
E.must_fail(check_survived, 130, out)
rc_m, out_m = run(label, lba, {"OPENSTICK_TEST_MUTATE": "no-sigint-shield"})
E.must_fail(check_survived, rc_m, out_m)
assert rc_m != 0 or "do not interrupt, wait" not in out_m, "shield-off mutation behaved like the shielded run"
import time
deadline = time.monotonic() + 8
while pids() and E.pid_alive(pids()[0]) and time.monotonic() < deadline:
    time.sleep(0.2)
