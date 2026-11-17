"""easy: --image-set DIR uses the prepared set and skips the build (and the mode question) entirely."""
import pathlib, re, sys
root, tmp = sys.argv[1:3]
sys.path.insert(0, str(pathlib.Path(root) / "tests"))
import easy_support as E

board = E.Board(root, tmp, "imageset")
board.enable_fake_build()          # present, but it must never be called
image_set = board.make_image_set("prebuilt")
rc, out = E.happy(board, [E.FLAG_IMAGE_SET, str(image_set), E.FLAG_YES], E.base_rules(board, yes=True))
assert rc == 0, "easy --image-set failed:\n" + out[-2500:]
E.check_no_traceback(out)
E.check_layout(board.disk, board.initial, E.expected_overlays(image_set))
assert str(image_set) in out, "the prepared image set is not named in the output"

def build_skipped(text, calls, sets):
    assert not calls, "the build ran: %s" % calls
    assert "[1] default" not in text, "the mode question was asked although the image set decides"
    assert not sets, "easy prepared its own image set instead of using the given one"
build_skipped(out, board.build_calls(), board.product_image_sets())

# A missing image set must stop before any write.
board.reset_disk()
rc_bad, out_bad = E.happy(board, [E.FLAG_IMAGE_SET, str(board.base / "no-such-set"), E.FLAG_YES], E.base_rules(board, yes=True))
assert rc_bad != 0 and board.writes() == [] and board.unchanged(), "missing image set must refuse without writing"
E.check_no_traceback(out_bad)

# Mutation proofs.
E.must_fail(build_skipped, out, ["profile=dev board=ufi103s"], [])
E.must_fail(build_skipped, out + "\n[1] default  [2] custom", [], [])
E.must_fail(build_skipped, out, [], [board.base / "images/easy-x"])
E.must_fail(E.check_layout, board.disk, board.initial, E.expected_overlays(image_set))
