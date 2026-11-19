"""easy (round 8): a restore state whose status is not "complete" stops easy mode before any write.

A complete state is not a reason to stop (control: it is archived, as before). In-progress, partial and unreadable
states are refused. The guard is proven by a product mutation and by checker mutations."""
import json, pathlib, sys
root, tmp = sys.argv[1:3]
sys.path.insert(0, str(pathlib.Path(root) / "tests"))
import easy_support as E

board = E.Board(root, tmp, "restore-state")
image_set = board.make_image_set()
args = [E.FLAG_IMAGE_SET, str(image_set), E.FLAG_YES]
rc0, out0 = E.happy(board, args, E.base_rules(board, yes=True))
assert rc0 == 0, "reference run failed:\n" + out0[-2000:]
assert board.writes(), "reference run wrote nothing"
state = board.home / "devices" / E.device_id(board) / "restore-state.json"


def run_with_state(content, env=None):
    board.reset_disk()
    state.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    for old in state.parent.glob("restore-state*"):
        old.unlink()
    if content is not None:
        state.write_text(content)
    return E.happy(board, args, E.base_rules(board, yes=True), env_overrides=env or {})


for label, content, shown in (("in-progress", json.dumps({"status": "in-progress", "phase": "boot"}), r"state: in-progress"),
                              ("partial", json.dumps({"status": "partial-failure-reconciliation-required"}), r"state: partial-failure"),
                              ("unreadable", "{not json", r"state: unreadable"),
                              ("no status", json.dumps({"phase": "boot"}), r"state: unreadable")):
    rc, out = run_with_state(content)
    E.check_refused(board, rc, out, r"a restore of this stick is not complete \(" + shown)
    assert "restore --device" in out and "--backup" in out, "the guarded restore command is not shown for %s" % label
    assert state.read_text() == content, "the non-complete state file was modified (%s)" % label

# Control: a COMPLETE state does not stop the run; it is archived (renamed, not deleted).
rc_c, out_c = run_with_state(json.dumps({"status": "complete"}))
assert rc_c == 0, "a complete restore state stopped easy mode:\n" + out_c[-1500:]
assert board.writes(), "the control run did not write"
assert not state.exists() and list(state.parent.glob("restore-state.complete-*.json")), "the complete state was not archived"

# Product mutation: without the check an in-progress state does not stop the run.
rc_m, out_m = run_with_state(json.dumps({"status": "in-progress"}), {"OPENSTICK_TEST_MUTATE": "no-restore-state-check"})
E.must_fail(E.check_refused, board, rc_m, out_m, r"restore of this stick is not complete")
assert board.writes(), "mutation run wrote nothing, so it proves nothing"

# Checker mutations.
rc_i, out_i = run_with_state(json.dumps({"status": "in-progress"}))
E.check_refused(board, rc_i, out_i, r"not complete")
E.must_fail(E.check_refused, board, 0, out_i, r"not complete")
E.must_fail(E.check_refused, board, rc_i, out_i.replace("not complete", "ok"), r"not complete")
E.must_fail(E.check_refused, board, rc_c, out_c, r"not complete")
