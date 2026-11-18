"""easy: after a successful easy flash on the fake board, the PRINTED restore command works through the unchanged guarded
`restore`, and the fake disk ends byte-identical to the verified backup. The guarded checks still bite (tampered evidence,
missing flash entry), and removing easy mode's flash-entry write makes the restore fail (mutation proof)."""
import pathlib, re, sys
root, tmp = sys.argv[1:3]
sys.path.insert(0, str(pathlib.Path(root) / "tests"))
import easy_support as E

board = E.Board(root, tmp, "restore-ok")
image_set = board.make_image_set()
rc, out = E.happy(board, [E.FLAG_IMAGE_SET, str(image_set), E.FLAG_YES], E.base_rules(board, yes=True))
assert rc == 0, "easy flash failed:\n" + out[-2500:]
E.check_no_traceback(out)
E.check_layout(board.disk, board.initial, E.expected_overlays(image_set))
command = E.printed_restore_command(out)
assert re.search(r"restore --device [0-9a-f]+ --backup \d{8}T", command), command

# The flash is recorded the way the guarded restore reads it.
entries = E.flash_entries(board)
assert len(entries) == 1 and entries[0]["mode"] == "easy", entries
assert entries[0]["outcome"] == "easy-pass-not-boot-verified", entries[0]["outcome"]
assert [p["name"] for p in entries[0]["phases"]] == ["gpt", "boot", "userdata", "preserved partitions"], entries[0]["phases"]
assert all(p["verified"] and p["result"] == "easy-readback-pass" for p in entries[0]["phases"]), entries[0]["phases"]
evidence = pathlib.Path(entries[0]["image_set"])
for name in ("gpt/gpt-T-primary.bin", "gpt/gpt-T-secondary.bin", "rollback/gpt-orig-primary.bin", "rollback/gpt-orig-secondary.bin",
             "fw/sbl1.img", "fw/rpm.img", "fw/tz.img", "fw/hyp.img", "fw/aboot.img", "rollback/stock-sbl1.bin",
             "rollback/stock-rpm.bin", "rollback/stock-tz-tzbak.bin", "rollback/stock-hyp.bin", "rollback/stock-aboot.bin"):
    assert (evidence / "pathA" / name).is_file(), "evidence file missing: " + name
assert (evidence / "SHA256SUMS").is_file() and (evidence / "pathA/SHA256SUMS").is_file(), "evidence checksum lists missing"
assert (evidence / "pathA/gpt/gpt-T-primary.bin").read_bytes() == (image_set / "layout/gpt-T-primary.bin").read_bytes(), \
    "evidence GPT-T is not the GPT-T that easy mode wrote"
flash_events = [e for e in E.events(board) if e["type"] == "flash.phase"]
assert [e["outcome"] for e in flash_events] == ["registered"] + ["easy-readback-pass"] * 4, flash_events

# Guarded checks are not weakened: tampered evidence and a missing flash entry are still refused (dry run reaches the same check).
writes_before = len(board.writes())
target = evidence / "pathA/gpt/gpt-T-primary.bin"
good = target.read_bytes()
target.write_bytes(good[:100] + bytes([good[100] ^ 1]) + good[101:])
rc_t, out_t = E.run_printed_restore(board, command, dry_run=True)
assert rc_t != 0 and re.search(r"SHA-256 mismatch|SHA256", out_t), "tampered evidence was accepted:\n" + out_t[-600:]
target.write_bytes(good)
import json
from tools.installer import store
saved = store.load_registry(board.home)
stripped = json.loads(json.dumps(saved))
stripped["devices"][0]["flashes"] = []
store.save_registry(board.home, stripped)
rc_n, out_n = E.run_printed_restore(board, command, dry_run=True)
assert rc_n != 0 and "no registered flash image set" in out_n, "restore without a flash entry was accepted:\n" + out_n[-600:]
store.save_registry(board.home, saved)
assert len(board.writes()) == writes_before, "a refused dry run wrote to the board"

# Dry run first (no writes), then the real thing, exactly as printed.
rc_d, out_d = E.run_printed_restore(board, command, dry_run=True)
assert rc_d == 0 and "no writes performed" in out_d, "dry run of the printed command failed:\n" + out_d[-600:]
assert len(board.writes()) == writes_before, "dry run wrote to the board"
rc_r, out_r = E.run_printed_restore(board, command)
E.check_restored(board, rc_r, out_r)

# Mutation proof 1: easy mode WITHOUT the flash-entry write leaves a restore command that does not work.
mutant = E.Board(root, tmp, "restore-mutant")
mutant_set = mutant.make_image_set()
rc_m, out_m = E.happy(mutant, [E.FLAG_IMAGE_SET, str(mutant_set), E.FLAG_YES], E.base_rules(mutant, yes=True),
                      env_overrides={"OPENSTICK_TEST_MUTATE": "no-flash-entry"})
assert rc_m == 0, "mutated easy flash itself failed:\n" + out_m[-1500:]
assert E.flash_entries(mutant) == [], "mutation did not remove the flash entry"
mutant_command = E.printed_restore_command(out_m)
rc_mr, out_mr = E.run_printed_restore(mutant, mutant_command)
assert rc_mr != 0 and "no registered flash image set" in out_mr, "restore without the entry should be refused:\n" + out_mr[-600:]
E.must_fail(E.check_restored, mutant, rc_mr, out_mr)
# Mutation proof 2: the oracle itself rejects a disk that was not restored, a missing readback line and a non-zero exit.
E.must_fail(E.check_restored, mutant, 0, out_r)                              # right text, but this disk is still flashed
E.must_fail(E.check_restored, board, 0, out_r.replace("PASS final full-disk readback", "pass"))
E.must_fail(E.check_restored, board, 1, out_r)
