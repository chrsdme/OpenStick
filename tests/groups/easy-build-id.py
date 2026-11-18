"""easy: --build ID uses an already registered build (no build, no mode question), prepares its own image set from THAT build,
and writes it. Unknown ID and --build together with --image-set stop before any write."""
import pathlib, re, shutil, sys
root, tmp = sys.argv[1:3]
sys.path.insert(0, str(pathlib.Path(root) / "tests"))
import easy_support as E

board = E.Board(root, tmp, "buildid")
board.enable_fake_build()                      # present, but it must never be called
wanted_id, wanted_boot, wanted_rootfs = board.register_build("wanted", tag=b"wanted-build")
other_id, other_boot, other_rootfs = board.register_build("other", tag=b"other-build")
assert wanted_boot != other_boot, "the two registered builds must be distinguishable"

rc, out = E.happy(board, [E.FLAG_BUILD, wanted_id, E.FLAG_YES], E.base_rules(board, yes=True))
assert rc == 0, "easy --build ID failed (rc=%s):\n%s" % (rc, out[-2500:])
E.check_no_traceback(out)

sets = board.product_image_sets()
assert len(sets) == 1, "expected one image set prepared from the registered build, found %s" % sets
overlays = E.expected_overlays(sets[0])
E.check_layout(board.disk, board.initial, overlays)
payload_boot = (sets[0] / "payload/boot.img").read_bytes()
payload_rootfs = (sets[0] / "payload/rootfs.img").read_bytes()
assert payload_boot == wanted_boot and payload_boot != other_boot, "the image set does not hold the selected build's boot.img"
assert payload_rootfs == wanted_rootfs, "the image set does not hold the selected build's rootfs.img"
assert wanted_id in sets[0].name, "image set is not named after the selected build: %s" % sets[0].name

def build_skipped(text, calls):
    assert not calls, "a build ran: %s" % calls
    assert "[1] default" not in text, "the mode question was asked although --build decides"
build_skipped(out, board.build_calls())
assert re.search(E.RE_CONFIRM, out) is None, "--yes run still asked for confirmation"
E.check_order(E.write_targets(board))
assert "reset" not in board.edl_ops(), "edl reset was called"
assert re.search(r"(?i)openstick.*restore.*--backup", out), "restore command not printed"
assert re.search(r"(?i)Step 5/9", out) and re.search(E.RE_STEP6, out), "steps 5 and 6 not shown"

# The registry still holds both builds and the run did not register a new one (no build ran).
sys.path.insert(0, root)
from tools.installer import store
registered = sorted(b["build_id"] for b in store.load_registry(board.home)["builds"])
assert registered == sorted([wanted_id, other_id]), "registered builds changed: %s" % registered

# The other build, selected the same way, produces ITS payload (the selection is real, not a coincidence).
board.reset_disk()
rc2, out2 = E.happy(board, [E.FLAG_BUILD, other_id, E.FLAG_YES], E.base_rules(board, yes=True))
assert rc2 == 0, "easy --build (second build) failed:\n" + out2[-2500:]
sets2 = [p for p in board.product_image_sets() if p not in sets]
assert len(sets2) == 1 and (sets2[0] / "payload/boot.img").read_bytes() == other_boot, "second selection did not use the second build"
E.check_layout(board.disk, board.initial, E.expected_overlays(sets2[0]))

# Without --yes the single confirmation is asked, with the build selection unchanged.
board.reset_disk()
rc3, out3 = E.happy(board, [E.FLAG_BUILD, wanted_id], E.base_rules(board))
assert rc3 == 0 and re.search(E.RE_CONFIRM, out3), "interactive --build run failed or asked no confirmation:\n" + out3[-1500:]

# Unknown ID: stops before any write, no traceback, names the problem.
board.reset_disk()
rc_bad, out_bad = E.happy(board, [E.FLAG_BUILD, "no-such-build", E.FLAG_YES], E.base_rules(board, yes=True))
assert rc_bad != 0, "unknown build ID exited 0"
assert board.writes() == [] and board.unchanged(), "unknown build ID wrote to the board"
E.check_no_traceback(out_bad)
assert re.search(r"(?i)FAILED in phase: ", out_bad) and re.search(r"(?i)build", out_bad.split("FAILED in phase:")[-1]), \
    "failure does not say it is about the build:\n" + out_bad[-800:]
assert board.build_calls() == [], "unknown build ID fell back to building"

# --build together with --image-set is refused up front.
board.reset_disk()
image_set = board.make_image_set("both")
rc_both, out_both = E.happy(board, [E.FLAG_BUILD, wanted_id, E.FLAG_IMAGE_SET, str(image_set), E.FLAG_YES],
                            E.base_rules(board, yes=True))
assert rc_both != 0 and re.search(r"either --image-set or --build", out_both), "--build with --image-set was not refused:\n" + out_both[-800:]
assert board.writes() == [] and board.unchanged(), "refused invocation wrote to the board"

# ---------------------------------------------------------------- mutation proofs (each check must reject broken input)
broken = board.base / "broken.img"
board.reset_disk()
rc4, out4 = E.happy(board, [E.FLAG_BUILD, wanted_id, E.FLAG_YES], E.base_rules(board, yes=True))
assert rc4 == 0
shutil.copy(board.disk, broken)
mutated_sets = [p for p in board.product_image_sets() if wanted_id in p.name]
mutated_overlays = E.expected_overlays(mutated_sets[-1])
with broken.open("r+b") as handle:
    handle.seek(396384 * 512 + 100)
    byte = handle.read(1)
    handle.seek(396384 * 512 + 100)
    handle.write(bytes([(byte[0] + 1) & 255]))
E.must_fail(E.check_layout, broken, board.initial, mutated_overlays)                       # one flipped payload byte
wrong_boot = [(o, other_boot if i == 2 else d) for i, (o, d) in enumerate(mutated_overlays)]
E.must_fail(E.check_layout, board.disk, board.initial, wrong_boot)                          # other build's boot expected
E.must_fail(build_skipped, out, ["profile=dev board=ufi103s"])                              # a build call is noticed
E.must_fail(build_skipped, out + "\n[1] default  [2] custom", [])                           # a mode question is noticed
def same_build(boot_bytes, expected):
    assert boot_bytes == expected
E.must_fail(same_build, other_boot, wanted_boot)                                             # wrong build in the set is noticed
def refused(rc_value, text):
    assert rc_value != 0 and re.search(r"either --image-set or --build", text)
E.must_fail(refused, 0, out_both)
E.must_fail(refused, rc_both, out_both.replace("either", "xx"))
