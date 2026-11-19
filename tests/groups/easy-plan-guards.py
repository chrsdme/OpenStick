"""easy (round 8): the plan is checked against the BACKUP before the first write.

Covers: --image-set whose GPT moves or resizes firmware (hyp moved, modem shrunk), a boot.img larger than the boot
partition (and one of exactly the capacity as the boundary control), a backup that does not match the layout's stock
signature (changed aboot size on the fake board, wrong sector count), and the board name and layout id on the single
YES screen. Product mutations and checker mutations prove each guard."""
import pathlib, re, sys, types
root, tmp = sys.argv[1:3]
sys.path.insert(0, str(pathlib.Path(root) / "tests"))
import easy_support as E

BOOT_CAPACITY = 32768 * E.SECTOR   # boot partition 396384..429151

board = E.Board(root, tmp, "plan")
good_set = board.make_image_set("good")
rc0, out0 = E.happy(board, [E.FLAG_IMAGE_SET, str(good_set), E.FLAG_YES], E.base_rules(board, yes=True))
assert rc0 == 0, "reference run failed:\n" + out0[-2000:]
assert board.writes(), "reference run wrote nothing; checks below would be vacuous"


def refused_run(image_set, env=None):
    board.reset_disk()
    return E.happy(board, [E.FLAG_IMAGE_SET, str(image_set), E.FLAG_YES], E.base_rules(board, yes=True),
                   env_overrides=env or {})


# 1. --image-set with a GPT that moves firmware (hyp) or resizes a non-written partition (modem).
def move_hyp(entries):
    entries["hyp"][0:2] = [60000, 61023]
def shrink_modem(entries):
    entries["modem"][1] = entries["modem"][0] + 511
sets = {}
for name, change, pattern in (("hyp-moved", move_hyp, r"moves or resizes hyp"), ("modem-small", shrink_modem, r"moves or resizes modem")):
    sets[name] = board.make_image_set(name, edit_gpt=lambda layout, change=change: E.edit_gpt(layout, change))
    rc, out = refused_run(sets[name])
    E.check_refused(board, rc, out, pattern)

# The documented tz growth (tz swallows tzbak) is NOT refused: the reference run above already flashed with it.
assert "moves or resizes tz" not in out0

# 2. boot.img larger than the boot partition; exactly the capacity is accepted.
big = board.make_image_set("bigboot", boot=E.synthetic_boot_image_sized(BOOT_CAPACITY + 2048))
rc_big, out_big = refused_run(big)
E.check_refused(board, rc_big, out_big, r"boot\.img is %d bytes, larger than the boot partition \(%d bytes\)" % (BOOT_CAPACITY + 2048, BOOT_CAPACITY))
exact = board.make_image_set("exactboot", boot=E.synthetic_boot_image_sized(BOOT_CAPACITY))
board.reset_disk()
rc_exact, out_exact = E.happy(board, [E.FLAG_IMAGE_SET, str(exact), E.FLAG_YES], E.base_rules(board, yes=True))
assert rc_exact == 0, "a boot.img of exactly the partition size was refused:\n" + out_exact[-1500:]
E.check_layout(board.disk, board.initial, E.expected_overlays(exact))

# 3. Stock signature: a backup whose aboot has another size is not the board the layout was made for.
odd = [(n, f, l - 1 if n == "aboot" else l) for n, f, l in E.FAKE_PARTITIONS]
board2 = E.Board(root, tmp, "plan-odd", partitions=odd)
set2 = board2.make_image_set("odd")
board2.plug()
rc_odd, out_odd = board2.easy([E.FLAG_IMAGE_SET, str(set2), E.FLAG_YES], E.base_rules(board2, yes=True))
E.check_refused(board2, rc_odd, out_odd, r"backup partition aboot has 2047 sectors; the stock signature says 2048")

# 3b. Stock signature: wrong sector count (direct call, no stick needed).
sys.path.insert(0, root)
from tools.installer import easy
dump_dir = board.backup_dirs()[0]
manifest = __import__("json").loads((dump_dir / "manifest.json").read_text())
ctx = types.SimpleNamespace(backup_dir=dump_dir, backup_manifest=dict(manifest))
rows = easy._check_stock_signature(ctx)                       # the real backup passes
assert rows["boot"] == (396384, 429151) and "tzbak" in rows
def short_count():
    easy._check_stock_signature(types.SimpleNamespace(backup_dir=dump_dir, backup_manifest=dict(manifest, sectors=manifest["sectors"] - 1)))
try:
    short_count()
    raise SystemExit("a backup with the wrong sector count passed the stock signature")
except easy.EasyError as exc:
    assert "Nothing was written" in str(exc) and str(manifest["sectors"] - 1) in str(exc)

# 4. The single YES screen names the board and the layout id (and comes before the question).
board.reset_disk()
rc_y, out_y = E.happy(board, [E.FLAG_IMAGE_SET, str(good_set)], E.base_rules(board))
assert rc_y == 0, out_y[-1500:]
def check_screen(text):
    match = re.search(r"board\s*:\s*ufi103s, layout ufi103s-gpt-t-board6-v1 \(backup matches its stock signature: 7634944 sectors, 27 partitions\)", text)
    assert match, "board and layout id are not on the confirmation screen"
    assert match.start() < text.index("Type YES"), "board line is printed after the YES question"
    assert text.count("Type YES") == 1, "more than one confirmation"
check_screen(out_y)

# ---- Mutation proofs (product): each guard off lets the run write.
for image_set, label, mutation in ((sets["hyp-moved"], "firmware", "no-firmware-check"), (big, "boot size", "no-payload-size-check")):
    rc_m, out_m = refused_run(image_set, {"OPENSTICK_TEST_MUTATE": mutation})
    E.must_fail(E.check_refused, board, rc_m, out_m, r"moves or resizes|larger than the boot")
    assert board.writes(), "%s mutation: nothing was written, so the mutation proves nothing" % label
board2.reset_disk()
board2.plug()
rc_m, out_m = board2.easy([E.FLAG_IMAGE_SET, str(set2), E.FLAG_YES], E.base_rules(board2, yes=True),
                          env_overrides={"OPENSTICK_TEST_MUTATE": "no-stock-signature"})
E.must_fail(E.check_refused, board2, rc_m, out_m, r"stock signature")
assert board2.writes(), "stock-signature mutation: nothing was written"

# ---- Mutation proofs (checker).
E.must_fail(check_screen, out_y.replace("layout ufi103s-gpt-t-board6-v1", "layout"))
E.must_fail(check_screen, out_y.replace("board    : ufi103s", "board    : "))
E.must_fail(check_screen, out_y.replace("Type YES", "Type YES").replace("  board    :", "  later    :") + "\n  board    : ufi103s, layout ufi103s-gpt-t-board6-v1 (backup matches its stock signature: 7634944 sectors, 27 partitions)")
E.must_fail(E.check_refused, board, 0, out_big, r"larger than the boot")
E.must_fail(E.check_refused, board, rc_big, out_big.replace("Nothing was written", "x"), r"larger")
