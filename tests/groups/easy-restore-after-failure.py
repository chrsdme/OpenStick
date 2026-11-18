"""easy: a flash that fails midway still leaves a restore command that works (failure after the GPT-T is complete: at the boot
write and at the userdata write). Failures INSIDE the GPT phase are fail-closed by the guarded restore; their exact
behaviour is pinned here as known limits (restore refuses, writes nothing)."""
import pathlib, re, sys
root, tmp = sys.argv[1:3]
sys.path.insert(0, str(pathlib.Path(root) / "tests"))
import easy_support as E

BOOT_LBA, USERDATA_LBA = "396384", "2428000"

# Reference run: which write attempt is which (order is gpt secondary, gpt primary, boot, userdata).
ref = E.Board(root, tmp, "fail-ref")
ref_set = ref.make_image_set()
rc0, out0 = E.happy(ref, [E.FLAG_IMAGE_SET, str(ref_set), E.FLAG_YES], E.base_rules(ref, yes=True))
assert rc0 == 0, "reference run failed:\n" + out0[-1500:]
targets = E.write_targets(ref)
at = {"gpt-secondary": 1, "gpt-primary": 2, "boot": targets.index(BOOT_LBA) + 1, "userdata": targets.index(USERDATA_LBA) + 1}
assert at["boot"] == 3 and at["userdata"] == 4, "unexpected write order %s" % targets


def failing_run(name, n):
    board = E.Board(root, tmp, name)
    image_set = board.make_image_set()
    rc, out = E.happy(board, [E.FLAG_IMAGE_SET, str(image_set), E.FLAG_YES], E.base_rules(board, yes=True),
                      env_overrides={"OPENSTICK_EDL_FAIL_WRITE_AT": str(n), "OPENSTICK_EDL_WRITE_COUNT_FILE": str(board.base / "wc")})
    return board, rc, out


def check_failed_run(board, rc, out, n):
    assert rc != 0, "injected failure exited 0"
    E.check_no_traceback(out)
    E.check_restore_hint(out)
    assert len(board.writes()) == n, "writes continued after the failure: %d attempted, failure at %d" % (len(board.writes()), n)
    entries = E.flash_entries(board)
    assert len(entries) == 1 and entries[0]["outcome"] == "partial-failure-reconciliation-required", entries
    assert entries[0]["phases"][-1]["result"] == "failed" and entries[0]["phases"][-1]["verified"] is False, entries[0]["phases"]


# Failure at the boot write and at the userdata write: the printed "To go back" command restores the disk exactly.
for label in ("boot", "userdata"):
    board, rc, out = failing_run("fail-" + label, at[label])
    check_failed_run(board, rc, out, at[label])
    assert not board.unchanged(), "the %s failure left the disk unchanged: the case proves nothing" % label
    command = E.printed_restore_command(out)
    rc_r, out_r = E.run_printed_restore(board, command)
    E.check_restored(board, rc_r, out_r)
    if label == "boot":
        boot_board, boot_rc, boot_out, boot_restore = board, rc, out, (rc_r, out_r)

# Known limit 1: the very first write fails, the disk is untouched. The guarded restore refuses (live GPT is the original, not a
# recorded GPT-T) and writes nothing; there is nothing to restore.
board1, rc1, out1 = failing_run("fail-first", at["gpt-secondary"])
check_failed_run(board1, rc1, out1, 1)
assert board1.unchanged(), "first-write failure changed the disk"
rc1r, out1r = E.run_printed_restore(board1, E.printed_restore_command(out1))
assert rc1r != 0 and "live GPT does not match a recorded rollback phase" in out1r, out1r[-500:]
assert len(board1.writes()) == 1 and board1.unchanged(), "refused restore wrote to the board"

# Known limit 2: secondary GPT written, primary write fails (mixed GPT). The guarded restore refuses to start from it.
board2, rc2, out2 = failing_run("fail-mixed", at["gpt-primary"])
check_failed_run(board2, rc2, out2, 2)
rc2r, out2r = E.run_printed_restore(board2, E.printed_restore_command(out2))
assert rc2r != 0 and "primary and backup GPT disagree" in out2r, out2r[-500:]
assert len(board2.writes()) == 2, "refused restore wrote to the board"

# Mutation proofs.
E.must_fail(check_failed_run, board, rc, out, at["userdata"] + 1)                      # wrong failure point
E.must_fail(check_failed_run, board, 0, out, at["userdata"])                           # exit 0 instead of failure
E.must_fail(check_failed_run, board, rc, re.sub(r"(?i)restore", "xx", out), at["userdata"])
E.must_fail(E.check_restored, board1, rc1r, out1r)                                     # refused restore is not "restored"
E.must_fail(E.check_restored, boot_board, boot_restore[0], boot_restore[1].replace("PASS final", "x"))
