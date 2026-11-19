"""easy (round 8): before the first write the stick must be the one that was backed up, in the state it was backed up.

Covers: different live identity (serial, and partition content), unreadable identity, consumed (not cold) session,
and a reused OLD backup whose GPT no longer matches the stick. Every refusal is checked with the same function
(check_refused), and every guard has a mutation proof."""
import os, pathlib, sys
root, tmp = sys.argv[1:3]
sys.path.insert(0, str(pathlib.Path(root) / "tests"))
import easy_support as E

board = E.Board(root, tmp, "identity")
image_set = board.make_image_set()
args = [E.FLAG_IMAGE_SET, str(image_set), E.FLAG_YES]
CALLS = str(board.base / "calls")
SERIAL = board.base / "serial"

# Reference run: the fixture works, a backup and a registered device exist.
rc0, out0 = E.happy(board, args, E.base_rules(board, yes=True))
assert rc0 == 0, "reference run failed:\n" + out0[-2000:]
assert board.writes(), "the reference run wrote nothing: the refusal checks below would be vacuous"
assert len(E.stamp_dirs(board)) == 1


def rules_with(action):
    return [E.Rule(E.RE_MODE, "1"), E.Rule(E.RE_REUSE, ""), E.Rule(E.RE_STEP6, action=action)]


def reuse_run(action, env=None, extra=None):
    """Fresh stock disk, the OLD backup is reused, `action` runs at the cold re-entry."""
    board.reset_disk()
    SERIAL.unlink(missing_ok=True)
    (board.base / "calls").unlink(missing_ok=True)
    return E.happy(board, args, rules_with(action), env_overrides=env or {})


# 1. Different live identity: same GPT, other serial (the Sahara serial changes after the re-entry).
def other_serial():
    SERIAL.write_text("0xfeedf00d\n")
serial_action = board.replug_action(before_plug=other_serial)
rc, out = reuse_run(serial_action, {"OPENSTICK_EDL_SERIAL_FILE": str(SERIAL)})
E.check_refused(board, rc, out, r"identity differs")
assert len(E.flash_entries(board)) == 1, "a refused run recorded a flash"

# 1b. Different live identity: same serial, other fsg/sec/fsc content (another board, same serial).
def other_fsg():
    with board.disk.open("r+b") as disk:
        disk.seek(8192 * E.SECTOR)
        disk.write(b"another-boards-fsg" * 20)
rc, out = reuse_run(board.replug_action(before_plug=other_fsg))
assert rc != 0 and "identity differs" in out and "Nothing was written to the board." in out, out[-1200:]
assert board.writes() == [], "writes after a content identity mismatch"

# 2. Unreadable identity: the first EDL call of the write step fails. A reuse run makes exactly one call before it.
rc, out = reuse_run(board.replug_action(), {"OPENSTICK_EDL_CALL_FILE": CALLS, "OPENSTICK_EDL_FAIL_AT": "2"})
E.check_refused(board, rc, out, r"(?i)EDL client exited|unexpected error|identity")
assert board.edl_ops() == ["rs", "rs"], "the failure was not at the write-step identity read: %s" % board.edl_ops()

# 3. Consumed session: the re-entry did not give a fresh Sahara session (no cold boot).
rc, out = reuse_run(board.replug_action(fresh=False))
E.check_refused(board, rc, out, r"Sahara session was already used")

# 4. Reused OLD backup: the stick was flashed after the backup (live GPT is GPT-T, backup GPT is stock).
board.reset_disk()
rc_f, out_f = E.happy(board, args, E.base_rules(board, yes=True))
assert rc_f == 0, "flash before the old-backup run failed:\n" + out_f[-1500:]
flashed = E.snapshot_disk(board, "flashed.img")
board.write_log.write_text("")
board.cmd_log.write_text("")
rc_old, out_old = E.happy(board, args, E.base_rules(board, yes=True))
E.check_refused(board, rc_old, out_old, r"GPT \(primary\) on the stick differs from the backup", reference=flashed)

# ---- Mutation proofs (product): each guard disabled on its own scenario lets the run write.
board.reset_disk()
SERIAL.unlink(missing_ok=True)
rc_m, out_m = E.happy(board, args, rules_with(serial_action),
                      env_overrides={"OPENSTICK_EDL_SERIAL_FILE": str(SERIAL), "OPENSTICK_TEST_MUTATE": "no-identity-compare"})
E.must_fail(E.check_refused, board, rc_m, out_m, r"identity differs")
assert board.writes(), "identity mutation: the run did not write, so the mutation proves nothing"

board.reset_disk()
rc_f2, out_f2 = E.happy(board, args, E.base_rules(board, yes=True))
assert rc_f2 == 0
flashed2 = E.snapshot_disk(board, "flashed2.img")
board.write_log.write_text("")
rc_m2, out_m2 = E.happy(board, args, E.base_rules(board, yes=True), env_overrides={"OPENSTICK_TEST_MUTATE": "no-live-gpt-check"})
E.must_fail(E.check_refused, board, rc_m2, out_m2, r"differs from the backup", reference=flashed2)
assert board.writes(), "live-GPT mutation: the run did not write, so the mutation proves nothing"

# ---- Mutation proofs (checker): a run that is NOT refused, and broken inputs, are rejected by check_refused.
board.reset_disk()
rc_ok, out_ok = E.happy(board, args, E.base_rules(board, yes=True))
assert rc_ok == 0
E.must_fail(E.check_refused, board, rc_ok, out_ok, r"identity differs")                  # consumed/unreadable: a clean run is not a refusal
E.must_fail(E.check_refused, board, 1, out.replace("Nothing was written", "x"), r"Sahara")  # missing 'Nothing was written'
E.must_fail(E.check_refused, board, 1, out, r"no such reason")                              # wrong reason
E.must_fail(E.check_refused, board, 1, out + "\nTraceback (most recent call last)", r"Sahara")
