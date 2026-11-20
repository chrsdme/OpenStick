"""easy (round 9): everything that must stop BEFORE the first write of the firmware chain.

Unit level (direct calls, fast): firmware package verification (missing/altered file, wrong size, bad SHA256SUMS, symlink,
inside the repo), manifest vs board/backup, the tz..tzbak range check with WRITTEN_NAMES extended, lk2nd composite (pin, size,
fit). End to end on the fake board: no package, altered package, pkg GPT-T not byte-identical to the derived one, wrong
EMMC_SECTORS, package in the repo or behind a symlink, region UNKNOWN or already NEW, backup firmware at the wrong LBA,
composite too large. Every check has a mutation proof."""
import os, pathlib, shutil, sys
root, tmp = sys.argv[1:3]
sys.path.insert(0, str(pathlib.Path(root) / "tests"))
sys.path.insert(0, root)
import easy_support as E
from tools.installer import easy

SEC = E.SECTOR
board = E.Board(root, tmp, "fwguards")
UNIT = pathlib.Path(tmp) / "fwguards-unit"
UNIT.mkdir()


def fresh(name, edit=None, sums=True):
    return board.make_fw_pkg(name, edit=edit, sums=sums)


def refused(pkg, pattern):
    """Unit check: easy._load_fw_pkg(repo, pkg) must raise EasyError matching `pattern`."""
    import re
    try:
        easy._load_fw_pkg(root, pkg)
    except easy.EasyError as exc:
        assert re.search(pattern, str(exc)), "wrong reason: %s" % exc
        assert "Nothing was written" in str(exc)
        return
    raise AssertionError("the package was accepted (expected %r)" % pattern)


good = fresh("pkg-good")
loaded = easy._load_fw_pkg(root, good)
assert set(loaded["fw"]) == set(easy.FW_PHASE_ORDER) and loaded["manifest"]["BOARD"] == "ufi103s"


def flip(path):
    data = bytearray(path.read_bytes()); data[100] ^= 1; path.write_bytes(bytes(data))


cases = {}
cases["missing"] = (fresh("pkg-missing", lambda d: (d / "fw/rpm.img").unlink(), sums=False), r"fw/rpm\.img is missing")
cases["altered"] = (fresh("pkg-altered", lambda d: flip(d / "fw/aboot.img"), sums=False), r"fw/aboot\.img does not match SHA256SUMS")
cases["short"] = (fresh("pkg-short", lambda d: (d / "fw/tz.img").write_bytes((d / "fw/tz.img").read_bytes()[:-512])), r"fw/tz\.img is 1048064 bytes, the region needs exactly 1048576")
def junk_sums(d):
    (d / "SHA256SUMS").write_text((d / "SHA256SUMS").read_text() + "this is not a sums line\n")
cases["junk"] = (fresh("pkg-junk", junk_sums, sums=False), r"malformed SHA256SUMS")
def drop_tz(d):
    (d / "SHA256SUMS").write_text("".join(l + "\n" for l in (d / "SHA256SUMS").read_text().splitlines() if "fw/tz.img" not in l))
cases["unlisted"] = (fresh("pkg-unlisted", drop_tz, sums=False), r"does not list fw/tz\.img")
def traversal(d):
    (d / "SHA256SUMS").write_text((d / "SHA256SUMS").read_text() + "%s  ../escape\n" % ("0" * 64))
cases["traversal"] = (fresh("pkg-trav", traversal, sums=False), r"unsafe path")
def wrong_digest(d):
    lines = (d / "SHA256SUMS").read_text().splitlines()
    lines = [("0" * 64 + l[64:]) if "fw/sbl1.img" in l else l for l in lines]
    (d / "SHA256SUMS").write_text("\n".join(lines) + "\n")
cases["digest"] = (fresh("pkg-digest", wrong_digest, sums=False), r"fw/sbl1\.img does not match SHA256SUMS")
def nosums(d):
    (d / "SHA256SUMS").unlink()
cases["nosums"] = (fresh("pkg-nosums", nosums, sums=False), r"SHA256SUMS is missing")
def link_member(d):
    data = (d / "fw/hyp.img").read_bytes(); (d / "fw/hyp.real").write_bytes(data); (d / "fw/hyp.img").unlink(); (d / "fw/hyp.img").symlink_to("hyp.real")
cases["member-symlink"] = (fresh("pkg-msym", link_member, sums=False), r"fw/hyp\.img is missing or is a symlink")
link = board.base / "pkg-link"
link.symlink_to(good)
cases["dir-symlink"] = (link, r"must not be a symlink")
inrepo = pathlib.Path(root) / (".fwpkg-test-%d" % os.getpid())
try:
    shutil.copytree(good, inrepo)
    cases["in-repo"] = (inrepo, r"OUTSIDE the repo")
    for name, (pkg, pattern) in cases.items():
        refused(pkg, pattern)
    # ---- mutation proofs (unit): the one check that stops each case, removed, lets it through
    real_sha, real_parse, real_dir, real_regions, real_files = easy._sha_file, easy._parse_sums, easy._check_pkg_dir, dict(easy.FW_REGIONS), easy.FW_PKG_FILES
    listed = {}
    for l in (cases["altered"][0] / "SHA256SUMS").read_text().splitlines():
        listed[l[66:]] = l[:64]
    easy._sha_file = lambda path, offset=0, length=None: listed.get(pathlib.Path(path).relative_to(cases["altered"][0]).as_posix(), "x") if str(path).startswith(str(cases["altered"][0])) else real_sha(path, offset, length)
    E.must_fail(refused, *cases["altered"]); easy._sha_file = real_sha
    easy.FW_REGIONS["tz"] = (270336, 2047)
    E.must_fail(refused, *cases["short"]); easy.FW_REGIONS.update(real_regions)
    easy._parse_sums = lambda text: {l.split("  ", 1)[1]: l.split("  ", 1)[0] for l in text.splitlines() if "  " in l}
    E.must_fail(refused, *cases["junk"]); E.must_fail(refused, *cases["traversal"]); easy._parse_sums = real_parse
    easy._check_pkg_dir = lambda repo, path: pathlib.Path(path).resolve()
    E.must_fail(refused, *cases["dir-symlink"]); E.must_fail(refused, *cases["in-repo"]); easy._check_pkg_dir = real_dir
    easy.FW_PKG_FILES = ()
    E.must_fail(refused, *cases["unlisted"]); easy.FW_PKG_FILES = real_files
    E.must_fail(refused, good, r"missing")                      # checker: a good package is not refused
finally:
    shutil.rmtree(inrepo, ignore_errors=True)
assert not inrepo.exists()

# ---------------------------------------------------------------- manifest vs board and backup (unit)
def manifest_refused(**change):
    import re
    pkg = dict(loaded, manifest=dict(loaded["manifest"], **{k: v for k, v in change.items() if k.isupper()}))
    board_id = change.get("board", "ufi103s"); emmc = change.get("emmc", E.EMMC_SECTORS); layout = change.get("layout", E.LAYOUT_ID)
    try:
        easy._check_fw_manifest(pkg, board_id, emmc, layout)
    except easy.EasyError as exc:
        assert "Nothing was written" in str(exc)
        return
    raise AssertionError("manifest accepted: %s" % change)
easy._check_fw_manifest(loaded, "ufi103s", E.EMMC_SECTORS, E.LAYOUT_ID)
for change in ({"BOARD": "mf800b"}, {"board": "mf800b"}, {"EMMC_SECTORS": "7634945"}, {"emmc": 7634945}, {"LAYOUT_ID": "other-layout"}, {"layout": "other"}):
    manifest_refused(**change)
real_manifest = easy._check_fw_manifest
easy._check_fw_manifest = lambda *a: None
E.must_fail(manifest_refused, BOARD="mf800b")
easy._check_fw_manifest = real_manifest

# ---------------------------------------------------------------- tz..tzbak range with WRITTEN_NAMES extended (unit)
assert set(easy.FW_PHASE_ORDER) <= set(easy.WRITTEN_NAMES) and set(easy.PAYLOAD_NAMES) <= set(easy.WRITTEN_NAMES)
assert easy.ABSORBED_NAMES == {"tzbak": "tz"} and easy.STOCK_FIRMWARE_LBA["tzbak"] == easy.STOCK_FIRMWARE_LBA["tz"] + 1024
with board.initial.open("rb") as disk:
    backup_rows = easy._planned_rows(disk.read(34 * SEC))
set_dir = board.make_image_set("unit")
good_planned = easy._planned_rows((set_dir / "layout/gpt-T-primary.bin").read_bytes())
easy._check_firmware_unchanged(backup_rows, good_planned)
assert good_planned["tz"] == (270336, 272383) and "tzbak" not in good_planned


def tz_refused(planned):
    try:
        easy._check_firmware_unchanged(backup_rows, planned)
    except easy.EasyError as exc:
        assert "Nothing was written" in str(exc)
        return
    raise AssertionError("a planned GPT with tz %s tzbak=%s was accepted" % (planned.get("tz"), "tzbak" in planned))


bad = [dict(good_planned, tz=(270336, 271359)),                       # tz stays 1024, tzbak removed
       dict(good_planned, tz=(270336, 272383 - 512)),                 # short of the end of tzbak
       dict(good_planned, tz=(270336, 272383 + 512)),                 # beyond the end of tzbak
       dict(good_planned, tz=(270337, 272383)),                       # moved
       dict(good_planned, tzbak=(271360, 272383)),                    # tzbak kept
       dict(good_planned, sbl1=(262145, 263168)),                     # written firmware may not move either
       {k: v for k, v in good_planned.items() if k != "tz"}]
for planned in bad:
    tz_refused(planned)
real_tz = easy._check_tz_range
easy._check_tz_range = lambda backup_rows, planned: None              # mutation: the tz..tzbak range check is broken
E.must_fail(tz_refused, bad[0]); E.must_fail(tz_refused, bad[1]); E.must_fail(tz_refused, bad[2]); E.must_fail(tz_refused, bad[3])
tz_refused(bad[5])                                                    # the generic rule still stops a moved sbl1 (independent guard)
easy._check_tz_range = real_tz
E.must_fail(tz_refused, good_planned)                                 # checker: the good plan is not refused

# ---------------------------------------------------------------- lk2nd composite (unit)
lk_path = board.lk2nd
os.environ["LK2ND_IMG"] = str(lk_path)
easy.LK2ND_PADDED_SHA256 = E.sha256_hex(lk_path.read_bytes())
small = board.base / "small-boot.img"
small.write_bytes(E.synthetic_boot_image())
composite = easy._build_composite(root, small, 32768)
assert composite[:0x80000] == E.synthetic_lk2nd() and composite[0x80000:] == small.read_bytes() and len(composite) == 0x80000 + small.stat().st_size


def composite_refused(boot=small, cap=32768, pattern=r"Nothing was written"):
    import re
    try:
        easy._build_composite(root, boot, cap)
    except easy.EasyError as exc:
        assert re.search(pattern, str(exc)), str(exc)
        return
    raise AssertionError("composite accepted")


exact = board.base / "exact-boot.img"; exact.write_bytes(E.synthetic_boot_image_sized(32768 * SEC - 0x80000))
over = board.base / "over-boot.img"; over.write_bytes(E.synthetic_boot_image_sized(32768 * SEC - 0x80000 + 2048))
assert len(easy._build_composite(root, exact, 32768)) == 32768 * SEC            # boundary control
composite_refused(over, 32768, r"does not fit the boot partition")
odd = board.base / "odd-boot.img"; odd.write_bytes(small.read_bytes() + b"x"); composite_refused(odd, 32768, r"whole number of sectors")
os.environ["LK2ND_IMG"] = str(board.base / "nope.img"); composite_refused(pattern=r"missing lk2nd image")
wrong_size = board.base / "lk-short.img"; wrong_size.write_bytes(E.synthetic_lk2nd()[:-512]); os.environ["LK2ND_IMG"] = str(wrong_size)
composite_refused(pattern=r"exactly 524288")
wrong_pin = board.base / "lk-wrong.img"; wrong_pin.write_bytes(b"\1" + E.synthetic_lk2nd()[1:]); os.environ["LK2ND_IMG"] = str(wrong_pin)
composite_refused(pattern=r"does not match the pin")
os.environ["LK2ND_IMG"] = str(lk_path)
real_fit = easy._check_composite_fits
easy._check_composite_fits = lambda total, capacity: None
E.must_fail(composite_refused, over, 32768, r"does not fit")
easy._check_composite_fits = real_fit
real_pin = easy.LK2ND_PADDED_SHA256
easy.LK2ND_PADDED_SHA256 = E.sha256_hex(wrong_pin.read_bytes())
os.environ["LK2ND_IMG"] = str(wrong_pin)
E.must_fail(composite_refused, pattern=r"does not match the pin")
easy.LK2ND_PADDED_SHA256 = real_pin
os.environ["LK2ND_IMG"] = str(lk_path)

# ---------------------------------------------------------------- end to end: every one stops with 'Nothing was written'
IMAGE = board.make_image_set("e2e")
BASE = [E.FLAG_IMAGE_SET, str(IMAGE), E.FLAG_YES]


def e2e(extra=None, pkg=None, env=None, rules=None, fw_pkg=True):
    board.reset_disk()
    board.plug()
    args = list(BASE) + (extra or [])
    if pkg is not None:
        args += ["--fw-pkg", str(pkg)]
    return board.easy(args, rules or E.base_rules(board, yes=True), env_overrides=env or {}, fw_pkg=fw_pkg and pkg is None)


# reference: the good package flashes (so each refusal below is about its own cause)
rc0, out0 = e2e()
assert rc0 == 0 and board.writes(), out0[-1500:]

rc, out = e2e(fw_pkg=False)
E.check_refused(board, rc, out, r"no firmware package: pass --fw-pkg DIR")
rc, out = e2e(pkg=cases["altered"][0])
E.check_refused(board, rc, out, r"fw/aboot\.img does not match SHA256SUMS")
rc, out = e2e(pkg=cases["missing"][0])
E.check_refused(board, rc, out, r"fw/rpm\.img is missing")
rc, out = e2e(pkg=link)
E.check_refused(board, rc, out, r"must not be a symlink")
inrepo2 = pathlib.Path(root) / (".fwpkg-test-e2e-%d" % os.getpid())
try:
    shutil.copytree(good, inrepo2)
    rc, out = e2e(pkg=inrepo2)
    E.check_refused(board, rc, out, r"OUTSIDE the repo")
    rc_m, out_m = e2e(pkg=inrepo2, env={"OPENSTICK_TEST_MUTATE": "no-pkg-dir-check"})
    E.must_fail(E.check_refused, board, rc_m, out_m, r"OUTSIDE the repo")
    assert board.writes(), "pkg-dir mutation: the run did not write, so it proves nothing"
finally:
    shutil.rmtree(inrepo2, ignore_errors=True)

# package GPT-T not byte-identical to the one derived from the backup
def alter_gpt(d):
    flip(d / "gpt/gpt-T-primary.bin")
gpt_pkg = fresh("pkg-gpt", alter_gpt)
rc, out = e2e(pkg=gpt_pkg)
E.check_refused(board, rc, out, r"firmware package primary GPT-T is not byte-identical")
rc_m, out_m = e2e(pkg=gpt_pkg, env={"OPENSTICK_TEST_MUTATE": "no-pkg-gpt-check"})
E.must_fail(E.check_refused, board, rc_m, out_m, r"not byte-identical")
assert board.writes(), "pkg-GPT mutation: the run did not write, so it proves nothing"

# wrong EMMC_SECTORS and wrong board in the manifest
def other_emmc(d):
    (d / "easy-fw-manifest.env").write_text("BOARD=ufi103s\nEMMC_SECTORS=7634945\nLAYOUT_ID=%s\n" % E.LAYOUT_ID)
rc, out = e2e(pkg=fresh("pkg-emmc", other_emmc))
E.check_refused(board, rc, out, r"EMMC_SECTORS=7634945, the verified backup has 7634944")
def other_board(d):
    (d / "easy-fw-manifest.env").write_text("BOARD=mf800b\nEMMC_SECTORS=7634944\nLAYOUT_ID=%s\n" % E.LAYOUT_ID)
rc, out = e2e(pkg=fresh("pkg-board", other_board))
E.check_refused(board, rc, out, r"firmware package is for board mf800b")
real_text = easy._check_fw_manifest
rc_m, out_m = e2e(pkg=fresh("pkg-board2", other_board), env={"OPENSTICK_TEST_MUTATE": "no-fw-manifest-check"})
E.must_fail(E.check_refused, board, rc_m, out_m, r"firmware package is for board")
assert board.writes(), "manifest mutation: the run did not write, so it proves nothing"

# region state before the first write: UNKNOWN and already-NEW both stop; the guided restore handles a partial chain
def region_action(lba, data):
    def change():
        with board.disk.open("r+b") as disk:
            disk.seek(lba * SEC); disk.write(data)
    return board.replug_action(before_plug=change)
def modified_reference(lba, data):
    ref = board.base / ("ref-%d.img" % lba)
    shutil.copy(board.initial, ref)
    with ref.open("r+b") as disk:
        disk.seek(lba * SEC); disk.write(data)
    return ref
def region_run(action, env=None):
    board.reset_disk()
    board.plug()
    return board.easy(BASE, [E.Rule(E.RE_MODE, "1"), E.Rule(E.RE_REUSE, ""), E.Rule(E.RE_STEP6, action=action)], env_overrides=env or {})
rc, out = region_run(region_action(262144, b"someone-elses-sbl1" * 30))
E.check_refused(board, rc, out, r"sbl1=UNKNOWN", reference=modified_reference(262144, b"someone-elses-sbl1" * 30))
rc_m, out_m = region_run(region_action(262144, b"someone-elses-sbl1" * 30), {"OPENSTICK_TEST_MUTATE": "no-region-classification"})
E.must_fail(E.check_refused, board, rc_m, out_m, r"sbl1=UNKNOWN", reference=modified_reference(262144, b"someone-elses-sbl1" * 30))
assert board.writes(), "classification mutation: the run did not write, so it proves nothing"
rc, out = region_run(region_action(268288, E.fw_image("rpm")))
E.check_refused(board, rc, out, r"rpm=new", reference=modified_reference(268288, E.fw_image("rpm")))
assert "guided restore" in out and "not resumed" in out

# backup firmware at the wrong LBA (hyp shifted by one sector): the Path A constants are not met
shifted = [(n, f + 1, l + 1) if n == "hyp" else (n, f, l) for n, f, l in E.FAKE_PARTITIONS]
board_h = E.Board(root, tmp, "fwguards-hyp", partitions=shifted)
set_h = board_h.make_image_set("h")
board_h.plug()
rc, out = board_h.easy([E.FLAG_IMAGE_SET, str(set_h), E.FLAG_YES], E.base_rules(board_h, yes=True))
E.check_refused(board_h, rc, out, r"backup partition hyp starts at LBA 272385; the Path A chain .* needs 272384")

# composite too large: the image set's boot.img fits the partition alone but not behind lk2nd
too_big = board.make_image_set("toobig", boot=E.synthetic_boot_image_sized(32768 * SEC - 0x80000 + 2048))
rc, out = (board.reset_disk(), board.plug(), board.easy([E.FLAG_IMAGE_SET, str(too_big), E.FLAG_YES], E.base_rules(board, yes=True)))[2:][0]
E.check_refused(board, rc, out, r"lk2nd \+ boot\.img \(\d+ bytes\) does not fit the boot partition")
board.reset_disk(); board.plug()
rc_m, out_m = board.easy([E.FLAG_IMAGE_SET, str(too_big), E.FLAG_YES], E.base_rules(board, yes=True), env_overrides={"OPENSTICK_TEST_MUTATE": "no-composite-fit-check"})
E.must_fail(E.check_refused, board, rc_m, out_m, r"does not fit the boot partition")
assert board.writes(), "composite mutation: the run did not write, so it proves nothing"

# missing lk2nd image
board.reset_disk(); board.plug()
rc, out = board.easy(BASE, E.base_rules(board, yes=True), env_overrides={"LK2ND_IMG": str(board.base / "absent.img")})
E.check_refused(board, rc, out, r"missing lk2nd image")

# checker mutations
E.must_fail(E.check_refused, board, 0, out, r"missing lk2nd")
E.must_fail(E.check_refused, board, rc, out.replace("Nothing was written", "x"), r"missing lk2nd")
