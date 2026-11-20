"""easy (round 9): the firmware chain of board #6 (tools/pathA/pathA-flash.sh) plus the lk2nd composite boot.

Happy path on the fake board: exact write order (GPT secondary, GPT primary, tz, rpm, sbl1, hyp, aboot, composite boot,
userdata), --skipwrite dry pass before every firmware/boot write, full read-back, no reset and no `final`, composite
layout (lk2nd 512 KiB + boot.img at 0x80000, never boot.img alone at offset 0), evidence recorded before the first write,
read-only final check after userdata. Constant-sync tests against the shell scripts. Each check has a mutation proof."""
import json, pathlib, re, shlex, sys
root, tmp = sys.argv[1:3]
sys.path.insert(0, str(pathlib.Path(root) / "tests"))
sys.path.insert(0, root)
import easy_support as E
from tools.installer import easy, edl_runner

R = pathlib.Path(root)
SEC = E.SECTOR
BOOT_LBA = 396384
ORDER = [str(E.EMMC_SECTORS - 33), "0"] + [str(E.PATHA_REGIONS[n][0]) for n in E.PATHA_ORDER] + [str(BOOT_LBA), "2428000"]


# ---------------------------------------------------------------- constants are the shell scripts' constants
def sync_check(flash_sh, fwlib_sh, flash_board_sh, rollback_sh):
    regions = {}
    order = []
    for match in re.finditer(r"(?m)^(tz|rpm|sbl1|hyp|aboot)\)\s+put (\w+) (\d+) (\d+)", flash_sh):
        regions[match.group(2)] = (int(match.group(3)), int(match.group(4)))
        order.append(match.group(2))
    assert len(regions) == 5, "could not read the five firmware phases from pathA-flash.sh"
    assert regions == easy.FW_REGIONS, "FW_REGIONS differ from pathA-flash.sh: %s vs %s" % (regions, easy.FW_REGIONS)
    assert tuple(order) == easy.FW_PHASE_ORDER, "phase order differs from pathA-flash.sh: %s" % order
    assert re.search(r"put gptS \$SEC_LBA 33 ", flash_sh) and re.search(r"put gptP 0 34 ", flash_sh)
    assert flash_sh.index("put gptS") < flash_sh.index("put gptP"), "pathA-flash.sh writes the secondary GPT first"
    assert (easy.GPT_SECONDARY_SECTORS, easy.GPT_PRIMARY_SECTORS) == (33, 34)
    loader = re.search(r"LDR_SHA=\$\{FWLIB_TEST_LDR_SHA:-([0-9a-f]{64})\}", fwlib_sh)
    assert loader and loader.group(1) == edl_runner.PINNED_LOADER_SHA256, "edl_runner PINNED_LOADER_SHA256 != fwlib.sh LDR_SHA"
    assert loader.group(1).startswith("959439aa")
    assert re.search(r"EMMC_SECTORS=(\d+)", fwlib_sh).group(1) == str(easy.FW_PKG_EMMC_SECTORS)
    pin = re.search(r"LK2ND_PADDED_SHA256:-([0-9a-f]{64})\}", flash_board_sh)
    assert pin and pin.group(1) == easy.LK2ND_PADDED_SHA256 and pin.group(1).startswith("d82f3535")
    assert "LK_OFF=$((0x80000))" in flash_board_sh and easy.LK2ND_BOOT_OFFSET == 0x80000 and easy.LK2ND_SIZE == 524288
    assert "LK1ST_BOOT=$((0x1000000))" in flash_board_sh and easy.BOOT_CAPACITY_SECTORS * SEC == 0x1000000
    assert re.search(r"put boot 396384 32768 ", rollback_sh) and easy.BOOT_COMPOSITE_LBA == 396384
    # easy.py keeps the same defaults as the scripts: no reset ported
    assert "reset" not in " ".join(easy.ALLOWED_EDL_OPERATIONS) and "final" in easy.FORBIDDEN_PHASES


texts = [(R / "tools" / name).read_text() for name in ("pathA/pathA-flash.sh", "pathA/fwlib.sh", "flash/flash-board.sh", "pathA/pathA-rollback.sh")]
sync_check(*texts)
E.must_fail(sync_check, texts[0].replace("put aboot 264192 2048", "put aboot 264192 1024"), *texts[1:])
E.must_fail(sync_check, texts[0].replace("tz)    put tz", "tx)    put tz"), *texts[1:])
E.must_fail(sync_check, texts[0], texts[1].replace("959439aa", "959439ab"), *texts[2:])
E.must_fail(sync_check, texts[0], texts[1], texts[2].replace("d82f3535", "d82f3536"), texts[3])
E.must_fail(sync_check, texts[0], texts[1], texts[2].replace("0x80000", "0x40000"), texts[3])
E.must_fail(sync_check, texts[0], texts[1], texts[2], texts[3].replace("396384", "396385"))

# ---------------------------------------------------------------- happy path
board = E.Board(root, tmp, "fwchain")
image_set = board.make_image_set()
board.fw_pkg = board.make_fw_pkg()
BYTES = board.base / "edl-bytes.log"
BYTES.write_text("")
rc, out = E.happy(board, [E.FLAG_IMAGE_SET, str(image_set), E.FLAG_YES], E.base_rules(board, yes=True),
                  env_overrides={"OPENSTICK_EDL_BYTES_FILE": str(BYTES)})
assert rc == 0, "reference run failed:\n" + out[-2500:]
E.check_no_traceback(out)
overlays = E.expected_overlays(image_set)
E.check_layout(board.disk, board.initial, overlays)

commands = [shlex.split(line) for line in board.cmd_log.read_text().splitlines() if line.strip()]
boot_composite = E.synthetic_lk2nd() + (image_set / "payload/boot.img").read_bytes()


def check_order(targets):
    assert targets == ORDER, "write order is not gpt secondary, gpt primary, tz, rpm, sbl1, hyp, aboot, boot, userdata: %s" % targets


def check_dry_then_write(cmds):
    """Mirror of put() in fwlib.sh: every non-userdata write is preceded by a --skipwrite pass of the SAME file, and every ws
    has --sectorsize=512; --lun never appears."""
    ws = [c for c in cmds if c[0] == "ws"]
    assert ws, "no ws command ran"
    for index, c in enumerate(ws):
        assert "--sectorsize=512" in c, "ws without --sectorsize=512: %s" % c
        assert not any(a.startswith("--lun") for a in c), "--lun used: %s" % c
    seen_dry = set()
    for c in ws:
        key = (c[1], c[2])
        if "--skipwrite" in c:
            seen_dry.add(key)
        elif c[1] != "2428000":                      # userdata keeps the sampled path of _write_and_check
            assert key in seen_dry, "write to LBA %s without a --skipwrite pass first" % c[1]
            seen_dry.discard(key)
    assert sum(1 for c in ws if "--skipwrite" in c) == 8, "expected 8 dry passes (2 GPT, 5 firmware, composite boot)"


def check_no_reset(cmds, text):
    for c in cmds:
        assert c[0] in ("rs", "ws", "rf"), "unexpected edl operation: %s" % c
        assert not any("reset" in a.lower() for a in c), "an edl argument contains reset: %s" % c
    assert "No reset was sent" in text


E.check_order(E.write_targets(board))
check_order(E.write_targets(board))
check_dry_then_write(commands)
check_no_reset(commands, out)

# full read-back (whole region) after each firmware/GPT/boot write; sampling for userdata only
reads = [tuple(line.split()[1:3]) for line in BYTES.read_text().splitlines() if line.startswith("rs ")]
for name in E.PATHA_ORDER:
    lba, sectors = E.PATHA_REGIONS[name]
    assert (str(lba), str(sectors)) in reads, "no full read of %s" % name
assert (str(BOOT_LBA), str(len(boot_composite) // SEC)) in reads, "no full read-back of the composite boot"
assert any(lba == "2428000" for lba, _n in reads), "userdata is not read back"
assert not any(lba == "2428000" and int(n) > 4096 for lba, n in reads), "userdata read-back is more than its two samples"

# composite layout: lk2nd first, boot.img at 0x80000, never boot.img alone at offset 0
def check_composite(disk):
    with open(disk, "rb") as f:
        f.seek(BOOT_LBA * SEC)
        head = f.read(len(boot_composite))
    assert head[:0x80000] == E.synthetic_lk2nd(), "lk2nd is not at the start of the boot partition"
    assert head[0x80000:] == (image_set / "payload/boot.img").read_bytes(), "boot.img is not at 0x80000"
    assert head[:8] != b"ANDROID!" or head[:0x80000] == E.synthetic_lk2nd(), "boot.img was written alone at offset 0"
check_composite(board.disk)
boot_write = [c for c in commands if c[0] == "ws" and c[1] == str(BOOT_LBA) and "--skipwrite" not in c][0]
assert pathlib.Path(boot_write[2]).name == "boot-composite.img"

# final check: after userdata and the preserved step, read only, before the cold-boot message
i_user, i_final, i_cold = out.index("userdata written"), out.index("ALL REGIONS == NEW IMAGES"), out.index("Cold boot is the only supported reboot")
assert i_user < i_final < i_cold, "final check is not between the userdata write and the cold boot message"
last_ws = max(i for i, c in enumerate(commands) if c[0] == "ws")
assert any(c[0] == "rs" and c[1] == str(E.PATHA_REGIONS["aboot"][0]) for c in commands[last_ws + 1:]), "firmware is not read back after the last write"
assert any(c[0] == "rs" and c[1] == "0" for c in commands[last_ws + 1:]) and any(c[0] == "rs" and c[1] == str(E.EMMC_SECTORS - 33) for c in commands[last_ws + 1:]), "GPTs are not read back after the last write"
assert re.search(r"unplug USB, wait 30 s, plug in WITHOUT the button, then ssh", out)
E.check_order(E.write_targets(board))

# evidence and registry: hashes and phase order, recorded for the guided restore; no blobs in the registry
entry = E.flash_entries(board)[-1]
fw = entry["firmware"]
assert fw["phase_order"] == ["gpt", "tz", "rpm", "sbl1", "hyp", "aboot", "boot", "userdata", "preserved partitions", "final check"], fw["phase_order"]
assert fw["composite_sha256"] == E.sha256_hex(boot_composite) and fw["composite_bytes"] == len(boot_composite)
for name in E.PATHA_ORDER:
    assert fw["new_sha256"][name] == E.sha256_hex(E.fw_image(name)), name
def initial(lba, n):
    with board.initial.open("rb") as disk:
        disk.seek(lba * SEC); return disk.read(n * SEC)
assert fw["stock_sha256"]["tz"] == E.sha256_hex(initial(270336, 2048)), "stock tz hash must be tz + tzbak from the BACKUP"
assert fw["stock_sha256"]["aboot"] == E.sha256_hex(initial(264192, 2048))
registry_text = (board.home / "registry.json").read_text() if (board.home / "registry.json").exists() else json.dumps(E.registry(board))
assert len(registry_text) < 200000 and E.fw_image("tz")[:64].hex() not in registry_text, "blob content leaked into the registry"
evidence = pathlib.Path(entry["image_set"])
assert (evidence / "pathA/fw/tz.img").read_bytes() == E.fw_image("tz")
assert (evidence / "pathA/rollback/stock-tz-tzbak.bin").read_bytes() == initial(270336, 2048)

# ---------------------------------------------------------------- source-level: the SIGINT shield covers every phase
import inspect
def shield_covers_all(src):
    body = src[src.index("with _SigintShield():"):]
    assert "_write_phases(ctx, temp, plan)" in body.split("\n")[1], "_write_phases is not inside the SIGINT shield"
    wp = inspect.getsource(easy._write_phases)
    for needle in ('"gpt"', "FW_PHASE_ORDER", '"boot"', '"userdata"', '"final check"'):
        assert needle in wp
    assert "_SigintShield" not in wp, "phases must not open their own shield"
shield_covers_all(inspect.getsource(easy.step8_flash))
E.must_fail(shield_covers_all, inspect.getsource(easy.step8_flash).replace("with _SigintShield():\n            _write_phases", "with _SigintShield():\n            pass\n        if True:\n            _write_phases"))

# ---------------------------------------------------------------- no reset / no final, ever
class _Ctx:
    session = type("S", (), {"run": staticmethod(lambda *a, **k: (_ for _ in ()).throw(AssertionError("edl was called")))})()
    phase = "x"; home = pathlib.Path(tmp); device_id = "d"; args = type("A", (), {})()
def must_refuse_reset():
    try:
        easy._edl(_Ctx, ["reset", "--resetmode=reset"], 5)
    except easy.EasyError as exc:
        assert "never a reset" in str(exc)
        return
    raise AssertionError("an edl reset was not refused")
must_refuse_reset()
try:
    easy._edl(_Ctx, ["ws", "0", "x", "--resetmode=reset"], 5)
    raise SystemExit("an argument containing reset was accepted")
except easy.EasyError:
    pass
def phase_final_refused():
    try:
        easy._phase(_Ctx, "final")
    except easy.EasyError:
        return
    raise AssertionError("phase final was accepted")
phase_final_refused()
real_guard, real_forbidden = easy._check_edl_args, easy.FORBIDDEN_PHASES
easy._check_edl_args = lambda args: None                # mutation: the argument guard is gone
E.must_fail(must_refuse_reset)
easy._check_edl_args = real_guard
easy.FORBIDDEN_PHASES = ()
E.must_fail(phase_final_refused)
easy.FORBIDDEN_PHASES = real_forbidden

# ---------------------------------------------------------------- mutation proofs on the happy-path oracles
E.must_fail(check_order, list(reversed(E.write_targets(board))))
E.must_fail(check_order, [ORDER[0]] + ORDER[2:3] + ORDER[1:2] + ORDER[3:])        # wrong GPT order
swapped = ORDER[:2] + [ORDER[3], ORDER[2]] + ORDER[4:]
E.must_fail(check_order, swapped)                                                  # rpm before tz
E.must_fail(check_dry_then_write, [c for c in commands if "--skipwrite" not in c])  # no dry pass at all
E.must_fail(check_dry_then_write, commands + [["ws", "270336", "x", "--sectorsize=512"]])      # a write without its own dry pass
E.must_fail(check_dry_then_write, [c + ["--lun=0"] if c[0] == "ws" else c for c in commands])
E.must_fail(check_dry_then_write, [[a for a in c if a != "--sectorsize=512"] for c in commands])
E.must_fail(check_no_reset, commands + [["reset", "--resetmode=reset"]], out)
E.must_fail(check_no_reset, commands, out.replace("No reset was sent", "x"))
E.must_fail(check_no_reset, commands + [["ws", "0", "f", "--resetmode=reset"]], out)
import shutil
broken = board.base / "alone.img"
shutil.copy(board.disk, broken)
with broken.open("r+b") as f:                       # lk2nd replaced by boot.img at offset 0 (the forbidden layout)
    f.seek(BOOT_LBA * SEC); f.write((image_set / "payload/boot.img").read_bytes())
E.must_fail(check_composite, broken)
E.must_fail(E.check_layout, broken, board.initial, overlays)
E.must_fail(E.check_layout, board.disk, board.initial, [o for o in overlays if o[0] != 270336 * SEC])   # tz write missing from the oracle
def final_after_user(text):
    for needle in ("userdata written", "ALL REGIONS == NEW IMAGES", "Cold boot is the only"):
        assert needle in text, needle + " missing"
    assert text.index("userdata written") < text.index("ALL REGIONS == NEW IMAGES") < text.index("Cold boot is the only")
final_after_user(out)
E.must_fail(final_after_user, "ALL REGIONS == NEW IMAGES\n" + out.replace("ALL REGIONS == NEW IMAGES", "x"))   # final check before userdata
E.must_fail(final_after_user, out.replace("ALL REGIONS == NEW IMAGES", "x"))                                      # no final check
E.must_fail(final_after_user, out.replace("Cold boot is the only", "x"))                                          # no cold boot message

# ---------------------------------------------------------------- product mutations
def run_mut(name, mutation):
    board.reset_disk()
    return E.happy(board, [E.FLAG_IMAGE_SET, str(image_set), E.FLAG_YES], E.base_rules(board, yes=True),
                   env_overrides={"OPENSTICK_TEST_MUTATE": mutation})
rc_o, out_o = run_mut("order", "wrong-phase-order")
assert board.writes(), "wrong-order mutation wrote nothing: it proves nothing"
E.must_fail(check_order, E.write_targets(board))
rc_b, out_b = run_mut("alone", "boot-alone")
assert board.writes(), "boot-alone mutation wrote nothing: it proves nothing"
E.must_fail(check_composite, board.disk)
