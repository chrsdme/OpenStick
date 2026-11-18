"""easy: flash, restore, flash again, fail midway, restore again. A completed restore refuses to replay for the same backup;
easy mode archives only a state whose status is exactly "complete" when a NEW flash starts, so the second printed restore works.
Mutation proof: without that archive step the second restore is refused."""
import pathlib, re, sys
root, tmp = sys.argv[1:3]
sys.path.insert(0, str(pathlib.Path(root) / "tests"))
import easy_support as E

board = E.Board(root, tmp, "restore-twice")
image_set = board.make_image_set()
args = [E.FLAG_IMAGE_SET, str(image_set), E.FLAG_YES]
rc1, out1 = E.happy(board, args, E.base_rules(board, yes=True))
assert rc1 == 0, "first easy flash failed:\n" + out1[-1500:]
command = E.printed_restore_command(out1)
rc_r1, out_r1 = E.run_printed_restore(board, command)
E.check_restored(board, rc_r1, out_r1)
state_file = board.home / "devices" / E.registry(board)["devices"][0]["id"] / "restore-state.json"
assert state_file.is_file(), "the completed restore left no state file"
assert '"complete"' in state_file.read_text(), "restore state is not complete"

# Second flash on the restored board; it fails at the userdata write (4th write of this run).
rc2, out2 = E.happy(board, args, E.base_rules(board, yes=True),
                    env_overrides={"OPENSTICK_EDL_FAIL_WRITE_AT": "4", "OPENSTICK_EDL_WRITE_COUNT_FILE": str(board.base / "wc2")})
assert rc2 != 0 and not board.unchanged(), "second flash was expected to fail midway with a changed disk"
E.check_no_traceback(out2)
E.check_restore_hint(out2)
assert len(E.flash_entries(board)) == 2, "second flash is not a second registry entry"
archived = sorted(state_file.parent.glob("restore-state.complete-*.json"))
assert len(archived) == 1 and not state_file.exists(), "completed restore state was not archived (renamed) by the new flash: %s" % archived
assert any(e["outcome"] == "completed-restore-state-archived" for e in E.events(board) if e["type"] == "flash.phase"), "no archive event"
command2 = E.printed_restore_command(out2)
assert command2 == command, "same backup, so the printed restore command must be identical"
rc_r2, out_r2 = E.run_printed_restore(board, command2)
E.check_restored(board, rc_r2, out_r2)

# Mutation proof: the same sequence WITHOUT the archive step: the second restore is refused ("will not replay").
mut = E.Board(root, tmp, "restore-twice-mutant")
mut_set = mut.make_image_set()
margs = [E.FLAG_IMAGE_SET, str(mut_set), E.FLAG_YES]
m1, mo1 = E.happy(mut, margs, E.base_rules(mut, yes=True), env_overrides={"OPENSTICK_TEST_MUTATE": "no-archive"})
assert m1 == 0
mcmd = E.printed_restore_command(mo1)
mr1, mor1 = E.run_printed_restore(mut, mcmd)
E.check_restored(mut, mr1, mor1)
m2, mo2 = E.happy(mut, margs, E.base_rules(mut, yes=True),
                  env_overrides={"OPENSTICK_TEST_MUTATE": "no-archive", "OPENSTICK_EDL_FAIL_WRITE_AT": "4",
                                 "OPENSTICK_EDL_WRITE_COUNT_FILE": str(mut.base / "wc2")})
assert m2 != 0 and not mut.unchanged()
mr2, mor2 = E.run_printed_restore(mut, E.printed_restore_command(mo2))
assert mr2 != 0 and "already complete" in mor2, "restore after a second flash should be refused without the archive step:\n" + mor2[-500:]
E.must_fail(E.check_restored, mut, mr2, mor2)
