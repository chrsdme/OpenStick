"""easy: an existing verified backup is reused (one question, default yes); no second backup is made."""
import pathlib, re, sys
root, tmp = sys.argv[1:3]
sys.path.insert(0, str(pathlib.Path(root) / "tests"))
import easy_support as E

board = E.Board(root, tmp, "reuse")
image_set = board.make_image_set()
args = [E.FLAG_IMAGE_SET, str(image_set)]
rc1, out1 = E.happy(board, args, E.base_rules(board))
assert rc1 == 0, "first run failed:\n" + out1[-2500:]
first = E.stamp_dirs(board)
assert len(first) == 1
ops1 = board.edl_ops()
assert ops1.count("rf") >= 1, "oracle precondition: the first run must read the whole disk (rf) to back it up"

board.reset_disk()
rc2, out2 = E.happy(board, args, E.base_rules(board))
assert rc2 == 0, "second run failed:\n" + out2[-2500:]
assert E.stamp_dirs(board) == first, "a second backup folder was created instead of reusing the first"
ops2 = board.edl_ops()

def no_full_read(ops):
    assert "rf" not in ops, "a full-disk read happened although a verified backup exists"
def reuse_reported(text, folder):
    assert re.search(r"(?i)verified backup .* already exists", text) and str(folder) in text, "reuse of the existing backup not reported"
no_full_read(ops2)
reuse_reported(out2, first[0])
E.check_layout(board.disk, board.initial, E.expected_overlays(image_set))

# Mutation proofs.
E.must_fail(no_full_read, ops1)                                  # the run that DID back up trips the check
E.must_fail(reuse_reported, out1, first[0])                      # first run never says "already exists"
E.must_fail(reuse_reported, out2, board.base / "no-such-folder")
