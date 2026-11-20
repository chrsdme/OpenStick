"""easy (round 9): `openstick easy --offline [--fw-pkg DIR] [--image-set DIR]` needs no stick and writes nothing.

With a registered backup: backup integrity, GPT signature/layout, package hashes and sizes, composite and the plan are
verified and the exact writes are printed; everything that needs the stick says HARDWARE_REQUIRED. No EDL call, no store
change, no write. Without a backup or package the missing part is reported, never invented. Mutation proofs: an offline run
that touches the store or calls edl, a wrong printed phase order."""
import hashlib, os, pathlib, re, sys
root, tmp = sys.argv[1:3]
sys.path.insert(0, str(pathlib.Path(root) / "tests"))
import easy_support as E

SEC = E.SECTOR
board = E.Board(root, tmp, "offline")
image_set = board.make_image_set()
board.fw_pkg = board.make_fw_pkg()
rc0, out0 = E.happy(board, [E.FLAG_IMAGE_SET, str(image_set), E.FLAG_YES], E.base_rules(board, yes=True))
assert rc0 == 0, "reference flash failed:\n" + out0[-1500:]
board.reset_disk()                       # the stick is stock again; the backup stays in the store
board.unplug()                           # and it is NOT connected during the offline runs


def tree(path):
    path = pathlib.Path(path)
    return {str(p.relative_to(path)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(path.rglob("*")) if p.is_file() and not p.is_symlink()}


def offline(extra, env=None, fw_pkg=False):
    before = tree(board.home)
    board.cmd_log.write_text("")
    rc, out = board.easy(["--offline"] + extra, [], env_overrides=env, fw_pkg=fw_pkg, timeout=120)
    return rc, out, before


ARGS = ["--image-set", str(image_set), "--fw-pkg", str(board.fw_pkg)]
rc, out, before = offline(ARGS)
assert rc == 0, "offline run failed (rc=%s):\n%s" % (rc, out[-2000:])
E.check_no_traceback(out)


def check_clean(rc, out, before):
    """Offline wrote nothing: no edl call, no write log line, disk and store byte-identical, no prompt."""
    assert rc == 0, "offline run failed:\n" + out[-800:]
    assert board.cmd_log.read_text().strip() == "", "offline mode called edl: %s" % board.cmd_log.read_text()[:200]
    assert board.writes() == [], "offline mode wrote to the fake board"
    assert board.unchanged(), "the fake disk changed"
    assert tree(board.home) == before, "offline mode changed the private store"
    assert "OFFLINE CHECKS DONE. Nothing was written." in out
    assert "Type YES" not in out and "Hold the button" not in out


def check_hardware(out):
    assert len(re.findall(r"HARDWARE_REQUIRED: ", out)) >= 4
    assert "HARDWARE_REQUIRED: the writes themselves" in out and "HARDWARE_REQUIRED: EDL entry" in out
    assert "backup           : verified" in out


PHASES = ["gpt secondary", "gpt primary", "tz", "rpm", "sbl1", "hyp", "aboot", "boot (composite)", "userdata"]


def check_plan(out):
    """The printed plan: phases in Path A order with the real LBAs, sectors and sha256 of the package/composite."""
    section = out[out.index("WOULD WRITE"):]
    positions = []
    for label in PHASES:
        match = re.search(r"(?m)^  %s\s+LBA (\d+)\s+x (\d+)\s*(?:sha256 ([0-9a-f]{64}))?" % re.escape(label), section)
        assert match, "phase %s not in the plan" % label
        positions.append(match.start())
        if label in E.PATHA_REGIONS:
            lba, sectors = E.PATHA_REGIONS[label]
            assert (int(match.group(1)), int(match.group(2)), match.group(3)) == (lba, sectors, E.sha256_hex(E.fw_image(label))), label
    assert positions == sorted(positions), "plan phases are not in Path A order"
    composite = E.synthetic_lk2nd() + (image_set / "payload/boot.img").read_bytes()
    assert E.sha256_hex(composite) in section and "composite boot   : OK" in out
    assert re.search(r"(?m)^  gpt secondary\s+LBA %d\s+x 33 " % (E.EMMC_SECTORS - 33), section)
    assert re.search(r"(?m)^  gpt primary\s+LBA 0\s+x 34 ", section)
    assert "Never sent: edl reset" in section


check_clean(rc, out, before)
check_hardware(out)
check_plan(out)
assert "GPT and layout   : OK" in out and "firmware package : OK" in out and "image set        : OK" in out

# no package, no image set: reported as NOT VERIFIED, still exit 0, still nothing written
rc1, out1, before1 = offline([])
check_clean(rc1, out1, before1)
assert "firmware package : NOT VERIFIED" in out1 and "image set        : NOT VERIFIED" in out1 and "WOULD WRITE: not shown" in out1

# a bad package fails the offline check with the reason
bad = board.make_fw_pkg("pkg-bad", edit=lambda d: (d / "fw/rpm.img").write_bytes(b"x"), sums=False)
rc2, out2, before2 = offline(["--image-set", str(image_set), "--fw-pkg", str(bad)])
assert rc2 != 0 and "does not match SHA256SUMS" in out2 and "Nothing was written" in out2, out2[-800:]
assert board.cmd_log.read_text().strip() == "" and board.writes() == [] and tree(board.home) == before2

# a store without any backup: HARDWARE_REQUIRED for the backup, the package is still verified
empty = E.Board(root, tmp, "offline-empty")
set_e = empty.make_image_set()
pkg_e = empty.make_fw_pkg()
empty.unplug()
before_e = tree(empty.home)
rc3, out3 = empty.easy(["--offline", "--image-set", str(set_e), "--fw-pkg", str(pkg_e)], [], fw_pkg=False, timeout=120)
assert rc3 == 0, out3[-1200:]
assert "backup           : HARDWARE_REQUIRED" in out3 and "plan against the backup HARDWARE_REQUIRED" in out3 and "firmware package : OK" in out3
assert empty.writes() == [] and empty.cmd_log.read_text().strip() == "" and tree(empty.home) == before_e

# ---- checker mutations
E.must_fail(check_clean, 1, out, before)
E.must_fail(check_clean, rc, out.replace("Nothing was written", "x"), before)
E.must_fail(check_clean, rc, out, dict(before, extra="0"))
E.must_fail(check_hardware, out.replace("HARDWARE_REQUIRED", "x"))
E.must_fail(check_plan, out.replace("LBA 270336", "LBA 270337"))
swap = out.replace("  tz ", "  XX ").replace("  rpm ", "  tz ", 1).replace("  XX ", "  rpm ", 1)
E.must_fail(check_plan, swap)
E.must_fail(check_plan, out.replace("Never sent: edl reset", "x"))

# ---- product mutations
rc_m, out_m, before_m = offline(ARGS, {"OPENSTICK_TEST_MUTATE": "offline-writes"})
E.must_fail(check_clean, rc_m, out_m, before_m)
assert (board.home / "offline-marker").exists() or board.cmd_log.read_text().strip(), "mutation did nothing, so it proves nothing"
(board.home / "offline-marker").unlink(missing_ok=True)
rc_o, out_o, _b = offline(ARGS, {"OPENSTICK_TEST_MUTATE": "wrong-phase-order"})
E.must_fail(check_plan, out_o)
