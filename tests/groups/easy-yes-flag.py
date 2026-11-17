"""easy: --yes skips the single confirmation; without it the confirmation is asked, and declining writes nothing."""
import pathlib, re, sys
root, tmp = sys.argv[1:3]
sys.path.insert(0, str(pathlib.Path(root) / "tests"))
import easy_support as E

board = E.Board(root, tmp, "yes")
image_set = board.make_image_set()
overlays = E.expected_overlays(image_set)
rc_yes, out_yes = E.happy(board, [E.FLAG_IMAGE_SET, str(image_set), E.FLAG_YES], E.base_rules(board, yes=True))
assert rc_yes == 0, "--yes run failed:\n" + out_yes[-2500:]
E.check_layout(board.disk, board.initial, overlays)

board.reset_disk()
rc_ask, out_ask = E.happy(board, [E.FLAG_IMAGE_SET, str(image_set)], E.base_rules(board))
assert rc_ask == 0, "interactive confirmed run failed:\n" + out_ask[-2500:]
E.check_layout(board.disk, board.initial, overlays)

def no_confirmation(text):
    assert not re.search(E.RE_CONFIRM, text), "a confirmation was asked"
def confirmation_asked(text):
    assert re.search(E.RE_CONFIRM, text), "no confirmation was asked"
no_confirmation(out_yes)
confirmation_asked(out_ask)

# Declining the confirmation writes nothing.
board.reset_disk()
rc_no, out_no = E.happy(board, [E.FLAG_IMAGE_SET, str(image_set)],
                        E.base_rules(board, yes=True) + [E.Rule(E.RE_CONFIRM, "no")])
assert board.writes() == [], "writes happened after the confirmation was declined"
assert board.unchanged(), "disk changed after the confirmation was declined"
assert rc_no != 0, "declined confirmation must exit non-zero"
E.check_restore_hint(out_no)

# Mutation proofs.
E.must_fail(no_confirmation, out_ask)
E.must_fail(confirmation_asked, out_yes)
E.must_fail(E.check_layout, board.disk, board.initial, overlays)   # an untouched disk is not the expected layout
