"""easy: 05c6:9008 never appears -> instruction shown, repeated once, then stop; non-zero, no write, no backup."""
import time
import pathlib, re, sys
root, tmp = sys.argv[1:3]
sys.path.insert(0, str(pathlib.Path(root) / "tests"))
import easy_support as E

board = E.Board(root, tmp, "timeout")
image_set = board.make_image_set()
board.unplug()
started = time.monotonic()
rc, out = board.easy([E.FLAG_IMAGE_SET, str(image_set)], E.base_rules(board), timeout=120)
elapsed = time.monotonic() - started
E.check_no_traceback(out)
def shown_twice(text):
    found = len(re.findall(E.RE_INSTRUCTION, text))
    assert found == 2, "instruction must appear exactly twice, saw %d" % found
def stopped(rc_value, writes, backups):
    assert rc_value != 0 and not writes and not backups, "timeout must exit non-zero with no write and no backup"
assert elapsed < 60, "poll timeout override was not honoured (took %.0f s)" % elapsed
shown_twice(out)
assert "(second try)" in out, "second attempt not announced"
stopped(rc, board.writes(), E.stamp_dirs(board))
assert board.unchanged(), "the fake disk changed although the board never appeared"
assert re.search(r"(?i)no EDL device .*appeared", out), "no stop message"

# Mutation proofs.
E.must_fail(shown_twice, out + "\n" + "Hold the button")               # three times
E.must_fail(shown_twice, re.sub(E.RE_INSTRUCTION, "x", out, count=1))   # once
E.must_fail(stopped, 0, [], [])
E.must_fail(stopped, rc, [["ws", "0", "1"]], [])
E.must_fail(stopped, rc, [], ["a-backup-dir"])
