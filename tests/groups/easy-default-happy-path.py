"""easy: default mode end to end on the fake board (fake build, no sudo). Final fake disk must equal the expected layout."""
import pathlib, re, sys
root, tmp = sys.argv[1:3]
sys.path.insert(0, str(pathlib.Path(root) / "tests"))
import easy_support as E

board = E.Board(root, tmp, "default")
board.enable_fake_build()
rc, out = E.happy(board, [])
assert rc == 0, "easy default mode failed (rc=%s):\n%s" % (rc, out[-2500:])
E.check_no_traceback(out)

sets = board.product_image_sets()
assert len(sets) == 1, "expected one image set prepared by the product, found %s" % sets
overlays = E.expected_overlays(sets[0])
E.check_layout(board.disk, board.initial, overlays)

assert len(board.build_calls()) == 1 and "profile=dev" in board.build_calls()[0], "default mode must build the dev profile once"
assert len(re.findall(E.RE_INSTRUCTION, out)) >= 2, "EDL entry instruction not shown for step 2 and step 6"
assert re.search(E.RE_MODE, out) and re.search(E.RE_CONFIRM, out), "mode question or single YES confirmation missing"
dirs = E.stamp_dirs(board)
assert len(dirs) == 1 and re.fullmatch(r"[0-9a-f]{8,}", dirs[0].parent.name), "backup folder not backups/<device-id>/<stamp>"
assert str(dirs[0]) in out and re.search(r"(?i)openstick.*restore.*--backup", out), "backup path or restore command not printed"
E.check_order(E.write_targets(board))
assert "reset" not in board.edl_ops(), "edl reset was called"
assert re.search(r"(?i)unplug the stick, wait 30 seconds.*without the button", out), "finish instructions missing"

# Mutation proofs: each key assertion must reject a deliberately broken input.
import shutil
broken = board.base / "broken.img"
shutil.copy(board.disk, broken)
with broken.open("r+b") as handle:
    handle.seek(396384 * 512 + 100)
    byte = handle.read(1)
    handle.seek(396384 * 512 + 100)
    handle.write(bytes([(byte[0] + 1) & 255]))
E.must_fail(E.check_layout, broken, board.initial, overlays)              # one flipped payload byte
E.must_fail(E.check_layout, board.disk, board.initial, overlays[:3])      # userdata write missing from the oracle
E.must_fail(E.check_order, list(reversed(E.write_targets(board))))        # wrong phase order
def restore_present(text):
    assert re.search(r"(?i)openstick.*restore.*--backup", text)
E.must_fail(restore_present, out.replace("restore", "rest0re"))
def two_instructions(text):
    assert len(re.findall(E.RE_INSTRUCTION, text)) >= 2
E.must_fail(two_instructions, re.sub(E.RE_INSTRUCTION, "x", out, count=1))
