"""easy (round 9): failures inside the firmware chain.

* the registry/evidence of the chain (hashes, phase order, composite) exists BEFORE the first write (failure at write 1);
* a read-back mismatch in the middle of the chain (rpm) stops at once: no later phase is written, flash entry says
  partial-failure, a restore command is printed;
* a failure between phases (after tz, after rpm+sbl1, at boot) leaves a state the printed guided `restore` handles
  (or, where the guarded restore is fail-closed by design, refuses and writes nothing: pinned as a known limit).
Mutation proofs: read-back compare off, checker mutations."""
import pathlib, re, sys
root, tmp = sys.argv[1:3]
sys.path.insert(0, str(pathlib.Path(root) / "tests"))
import easy_support as E

LBA = {n: str(v[0]) for n, v in E.PATHA_REGIONS.items()}
SEC_GPT = str(E.EMMC_SECTORS - 33)
WRITE_ORDER = [SEC_GPT, "0", LBA["tz"], LBA["rpm"], LBA["sbl1"], LBA["hyp"], LBA["aboot"], "396384", "2428000"]


def failing_run(name, env, image_set=None):
    board = E.Board(root, tmp, name)
    image_set = image_set or board.make_image_set()
    env = dict(env, OPENSTICK_EDL_WRITE_COUNT_FILE=str(board.base / "wc"))
    rc, out = E.happy(board, [E.FLAG_IMAGE_SET, str(image_set), E.FLAG_YES], E.base_rules(board, yes=True), env_overrides=env)
    return board, rc, out


def check_stopped(board, rc, out, n, pattern):
    assert rc != 0, "the injected failure exited 0"
    E.check_no_traceback(out)
    E.check_restore_hint(out)
    assert re.search(pattern, out), "reason %r not printed:\n%s" % (pattern, out[-900:])
    assert E.write_targets(board) == WRITE_ORDER[:n], "writes after the failure point: %s" % E.write_targets(board)
    entries = E.flash_entries(board)
    assert len(entries) == 1 and entries[0]["outcome"] == "partial-failure-reconciliation-required", entries
    assert entries[0]["phases"][-1]["result"] == "failed" and entries[0]["phases"][-1]["verified"] is False


# 1. A failure at the very first write: the chain's hashes and phase order are already registered, evidence exists.
board1, rc1, out1 = failing_run("fwfail-first", {"OPENSTICK_EDL_FAIL_WRITE_AT": "1"})
check_stopped(board1, rc1, out1, 1, r"EDL client exited")
entry = E.flash_entries(board1)[0]
assert entry["firmware"]["phase_order"][:6] == ["gpt", "tz", "rpm", "sbl1", "hyp", "aboot"]
assert entry["firmware"]["composite_sha256"] and set(entry["firmware"]["new_sha256"]) == {"gptP", "gptS", "sbl1", "rpm", "tz", "hyp", "aboot"}
evidence = pathlib.Path(entry["image_set"])
for name in E.PATHA_ORDER:
    assert (evidence / "pathA/fw" / (name + ".img")).read_bytes() == E.fw_image(name), name
assert (evidence / "pathA/SHA256SUMS").is_file() and (evidence / "SHA256SUMS").is_file()
events = [e for e in E.events(board1) if e.get("outcome") == "registered"]
assert events and events[0]["firmware"]["phase_order"] == entry["firmware"]["phase_order"], "no registered event with the plan"
assert board1.unchanged(), "the first write failed but the disk changed"

# 2. Read-back mismatch at rpm (the 4th write): nothing after rpm is written.
RB = {"OPENSTICK_EDL_CORRUPT_RS_LBA": LBA["rpm"], "OPENSTICK_EDL_CORRUPT_RS_AFTER_WRITES": "4"}
board2, rc2, out2 = failing_run("fwfail-readback", RB)
check_stopped(board2, rc2, out2, 4, r"READ-BACK MISMATCH in rpm")
assert not board2.unchanged()

# 2b. Mutation: read-back compare off. The chain no longer stops at rpm, so the oracle must fail.
board2m, rc2m, out2m = failing_run("fwfail-readback-mut", dict(RB, OPENSTICK_TEST_MUTATE="no-readback-compare"))
E.must_fail(check_stopped, board2m, rc2m, out2m, 4, r"READ-BACK MISMATCH in rpm")
assert len(E.write_targets(board2m)) > 4, "mutation run: the chain did not continue past rpm, so it proves nothing"

# 3. Interrupted between phases, then the printed guided restore.
results = {}
for label, n in (("tz-failed", 3), ("after-rpm-sbl1", 6), ("boot-failed", 8)):
    board, rc, out = failing_run("fwfail-" + label, {"OPENSTICK_EDL_FAIL_WRITE_AT": str(n)})
    check_stopped(board, rc, out, n, r"EDL client exited")
    command = E.printed_restore_command(out)
    rc_r, out_r = E.run_printed_restore(board, command)
    results[label] = (board, rc_r, out_r)
    if rc_r == 0:
        E.check_restored(board, rc_r, out_r)
    else:                                                   # fail-closed by design: refuses, writes nothing more
        assert board.writes()[:n] and len(board.writes()) == n, "a refused restore wrote to the board"
        print("KNOWN LIMIT: restore after a failure at write %d refused: %s" % (n, out_r.strip().splitlines()[-1][:160]))

# At least the states with a real new firmware region must be restorable; the first-phase tz failure is the fail-closed case.
for label in ("after-rpm-sbl1", "boot-failed"):
    board, rc_r, out_r = results[label]
    E.check_restored(board, rc_r, out_r)
board_f, rc_f, out_f = results["after-rpm-sbl1"]
E.must_fail(E.check_restored, board_f, rc_f, out_f.replace("PASS final", "x"))
E.must_fail(E.check_restored, results["tz-failed"][0], 1, "refused")                       # a refused restore is not "restored"
E.must_fail(check_stopped, board2, 0, out2, 4, r"READ-BACK")                              # exit 0 instead of a stop
E.must_fail(check_stopped, board2, rc2, out2, 5, r"READ-BACK")                            # wrong stopping point
E.must_fail(check_stopped, board2, rc2, out2.replace("MISMATCH", "ok"), 4, r"READ-BACK MISMATCH")
E.must_fail(check_stopped, board2, rc2, re.sub(r"(?i)restore", "xx", out2), 4, r"READ-BACK")
