"""easy: a failed write at the first, a middle and the last write stops at once, prints the restore command, exits non-zero."""
import pathlib, re, sys
root, tmp = sys.argv[1:3]
sys.path.insert(0, str(pathlib.Path(root) / "tests"))
import easy_support as E

board = E.Board(root, tmp, "failure")
image_set = board.make_image_set()
args = [E.FLAG_IMAGE_SET, str(image_set), E.FLAG_YES]
rc0, out0 = E.happy(board, args, E.base_rules(board, yes=True))
assert rc0 == 0, "reference run failed:\n" + out0[-2500:]
total = len(board.writes())
assert total >= 3, "need at least three write operations to inject three distinct failure points, saw %d" % total
points = sorted({1, total // 2 + 1, total})
assert len(points) == 3, points

def check_failure(rc, out, n, attempted, backups):
    assert rc != 0, "failed phase exited 0"
    E.check_no_traceback(out)
    E.check_restore_hint(out)
    assert backups and str(backups[0]) in out, "backup path not printed"
    assert re.search(r"FAILED in phase: ", out), "failed phase not named"
    assert attempted == n, "writes continued after the failure: %d attempted, failure was at write %d" % (attempted, n)

results = []
for n in points:
    board.reset_disk()
    rc, out = E.happy(board, args, E.base_rules(board, yes=True),
                      env_overrides={"OPENSTICK_EDL_FAIL_WRITE_AT": str(n), "OPENSTICK_EDL_WRITE_COUNT_FILE": str(board.base / "wc")})
    attempted = len(board.writes())
    check_failure(rc, out, n, attempted, E.stamp_dirs(board))
    results.append((rc, out, n, attempted))
    (board.base / "wc").unlink(missing_ok=True)
assert len({r[3] for r in results}) == 3, "the three failure points were not distinct"

# Mutation proofs on the first failure case.
rc, out, n, attempted = results[0]
backups = E.stamp_dirs(board)
E.must_fail(check_failure, 0, out, n, attempted, backups)
E.must_fail(check_failure, rc, re.sub(r"(?i)restore", "xx", out), n, attempted, backups)
E.must_fail(check_failure, rc, out, n, attempted + 1, backups)
E.must_fail(check_failure, rc, out + "\nTraceback (most recent call last)", n, attempted, backups)
