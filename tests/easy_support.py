"""Shared fixtures for the `openstick easy` test groups (tests/groups/easy-*.py).

Everything here is test-only: a fake board (shim edl with opt-in writes + a full-size sparse fake eMMC + a fake USB sysfs
tree, all inside the runner temp dir), synthetic images and an expect-style pty driver. No real sysfs, no sudo, no
hardware. Interface guesses about the easy command (prompt wording) live in the CONSTANTS block.
"""
import contextlib
import errno
import json
import os
import pathlib
import re
import select
import shlex
import shutil
import signal
import struct
import subprocess
import sys
import threading
import time
import types
import uuid
import zlib

# ---------------------------------------------------------------- CONSTANTS (the easy command's user-facing text)
EASY_COMMAND = ["easy"]
FLAG_YES = "--yes"
FLAG_IMAGE_SET = "--image-set"
FLAG_BUILD = "--build"
POLL_ENV = {"OPENSTICK_EASY_POLL_TIMEOUT": "3", "OPENSTICK_EASY_POLL_INTERVAL": "0.2"}
RE_INSTRUCTION = r"Hold the button"
RE_CONFIRM = r"Type YES"
RE_REUSE = r"(?i)reuse it"
RE_MODE = r"\[1\] default"
RE_STEP6 = r"Step 6/9"

EMMC_SECTORS = 7634944
SECTOR = 512
QUIET = 0.4  # seconds without output before a prompt is answered


# ---------------------------------------------------------------- generic helpers
def strip_ansi(text):
    return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", text).replace("\r", "")


def must_fail(function, *args, **kwargs):
    """Mutation proof helper: the check MUST raise AssertionError on the deliberately broken input."""
    try:
        function(*args, **kwargs)
    except AssertionError:
        return True
    raise SystemExit("MUTATION SURVIVED: check %s accepted a deliberately broken input" % function.__name__)


class Rule:
    """Answer a prompt: reply text (sent with newline), or run `action` immediately when the pattern shows up."""

    def __init__(self, pattern, reply=None, fires=1, action=None, send=None, kill=None, delay=0.0):
        """send: bytes typed into the pty (b"\\x03" is a real Ctrl-C). kill: signal sent to the easy process.
        delay: seconds after the pattern shows up before send/kill happen."""
        self.pattern, self.reply, self.fires, self.action = re.compile(pattern), reply, fires, action
        self.send, self.kill, self.delay = send, kill, delay
        self.immediate = bool(action or send or kill)


def _fire(rule, pid, fd):
    def work():
        if rule.delay:
            time.sleep(rule.delay)
        if rule.action:
            rule.action()
        if rule.send:
            with contextlib.suppress(OSError):
                os.write(fd, rule.send)
        if rule.kill:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, rule.kill)
    if rule.delay:
        threading.Thread(target=work, daemon=True).start()
    else:
        work()


def pid_alive(pid):
    """True while the process exists and is not a zombie (Linux /proc)."""
    try:
        state = pathlib.Path("/proc/%d/stat" % pid).read_text().rpartition(")")[2].split()[0]
    except (OSError, IndexError):
        return False
    return state != "Z"


def run_pty(argv, env, rules, timeout=300, cwd=None, on_exit=None):
    """Run argv under a pty and answer prompts from `rules` (each at most `fires` times). Returns (rc, transcript).

    Reply rules fire only on text printed since the last reply, after QUIET seconds of silence, so a prompt is answered
    only once it is fully shown. Action rules fire as soon as their pattern appears."""
    pid, fd = os.forkpty()
    if pid == 0:
        try:
            if cwd:
                os.chdir(cwd)
            os.execvpe(argv[0], argv, env)
        finally:
            os._exit(127)
    out, mark, last = "", 0, time.monotonic()
    exited = None          # wait status of the easy process, noticed as soon as it exits
    deadline = last + timeout
    fires = {id(rule): rule.fires for rule in rules}
    action_pos = {id(rule): 0 for rule in rules}
    try:
        while True:
            if time.monotonic() > deadline:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
                raise AssertionError("pty run timed out; transcript tail:\n" + strip_ansi(out)[-1500:])
            if exited is None:
                done, status = os.waitpid(pid, os.WNOHANG)
                if done:
                    exited = status
                    if on_exit:
                        on_exit()       # runs at the moment easy has exited, before leftover children can finish
            ready, _, _ = select.select([fd], [], [], 0.1)
            if exited is not None and not ready:
                break           # a leftover child that still holds the pty must not keep us waiting
            if ready:
                try:
                    chunk = os.read(fd, 65536)
                except OSError:
                    chunk = b""
                if not chunk:
                    break
                out += chunk.decode("utf-8", "replace")
                last = time.monotonic()
                plain = strip_ansi(out)
                for rule in rules:
                    if rule.immediate and fires[id(rule)] > 0:
                        match = rule.pattern.search(plain, action_pos[id(rule)])
                        if match:
                            fires[id(rule)] -= 1
                            action_pos[id(rule)] = match.end()
                            _fire(rule, pid, fd)
                continue
            if time.monotonic() - last >= QUIET:
                tail = strip_ansi(out[mark:])
                best = None
                for rule in rules:
                    if rule.immediate or fires[id(rule)] <= 0:
                        continue
                    match = rule.pattern.search(tail)
                    if match and (best is None or match.start() < best[0]):
                        best = (match.start(), rule)
                if best:
                    fires[id(best[1])] -= 1
                    os.write(fd, (best[1].reply + "\n").encode())
                    mark = len(out)
                    last = time.monotonic()
        if exited is None:
            _, exited = os.waitpid(pid, 0)
        return os.waitstatus_to_exitcode(exited), strip_ansi(out)
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)


# ---------------------------------------------------------------- sparse-file comparison
def write_pkg_sums(pkg):
    pkg = pathlib.Path(pkg)
    lines = []
    for item in sorted(pkg.rglob("*")):
        if item.is_file() and not item.is_symlink() and item.name != "SHA256SUMS":
            lines.append("%s  %s" % (sha256_hex(item.read_bytes()), item.relative_to(pkg).as_posix()))
    (pkg / "SHA256SUMS").write_text("\n".join(lines) + "\n")


def data_extents(path):
    """(start, end) byte ranges that hold data (sparse holes are skipped); the whole file if holes are not reported."""
    extents, size = [], os.stat(path).st_size
    fd = os.open(path, os.O_RDONLY)
    try:
        position = 0
        while position < size:
            try:
                start = os.lseek(fd, position, os.SEEK_DATA)
            except OSError as exc:
                if exc.errno == errno.ENXIO:
                    break
                return [(0, size)]
            end = os.lseek(fd, start, os.SEEK_HOLE)
            extents.append((start, end))
            position = end
    finally:
        os.close(fd)
    return extents


def _merge(ranges):
    merged = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def check_layout(final_path, initial_path, overlays, chunk=1 << 20):
    """Assert final == initial with `overlays` [(byte offset, bytes)] applied, byte for byte, on a sparse 3.9 GB image."""
    final_path, initial_path = pathlib.Path(final_path), pathlib.Path(initial_path)
    size = max([initial_path.stat().st_size] + [offset + len(data) for offset, data in overlays])
    assert final_path.stat().st_size == size, "final disk size %d, expected %d" % (final_path.stat().st_size, size)
    ranges = _merge(data_extents(final_path) + data_extents(initial_path) +
                    [(offset, offset + len(data)) for offset, data in overlays])
    with final_path.open("rb") as final, initial_path.open("rb") as initial:
        for start, end in ranges:
            position = start
            while position < end:
                length = min(chunk, end - position)
                final.seek(position)
                got = final.read(length).ljust(length, b"\0")
                initial.seek(position)
                expect = bytearray(initial.read(length).ljust(length, b"\0"))
                for offset, data in overlays:
                    lo, hi = max(offset, position), min(offset + len(data), position + length)
                    if lo < hi:
                        expect[lo - position:hi - position] = data[lo - offset:hi - offset]
                if bytes(expect) != got:
                    first = next(i for i, (a, b) in enumerate(zip(expect, got)) if a != b)
                    raise AssertionError("final disk differs from the expected layout at byte %d (sector %d)"
                                         % (position + first, (position + first) // SECTOR))
                position += length


def same_files(a, b):
    try:
        check_layout(a, b, [])
    except AssertionError:
        return False
    return True


# ---------------------------------------------------------------- synthetic images and the fake original disk
def _gptool(root):
    import importlib.util
    spec = importlib.util.spec_from_file_location("easy_test_gptool", pathlib.Path(root) / "scripts/gptool.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Stock-like layout of the fake board: names and sizes follow what tools/gen-gpt-tz1m.py and the GPT-T adapter require
# (tz and tzbak 1024 sectors and adjacent; boot/system/userdata at the documented LBAs). All data is synthetic.
FAKE_PARTITIONS = [
    ("sbl1", 262144, 263167), ("rpm", 268288, 269311), ("tz", 270336, 271359), ("tzbak", 271360, 272383),
    ("hyp", 272384, 273407), ("aboot", 264192, 266239), ("fsg", 8192, 9215), ("sec", 9216, 9727), ("fsc", 9728, 10239),
    ("modemst1", 10240, 12287), ("modemst2", 12288, 14335), ("persist", 14336, 30719),
    ("boot", 396384, 429151), ("system", 429152, 2067551), ("cache", 2067552, 2427999), ("userdata", 2428000, 7634910),
    # The remaining stock names the rollback planner (tools/rollback-plan.py) requires. They sit in free space below boot,
    # so no LBA above moves and the existing layout oracles are unchanged.
    ("modem", 32768, 33791), ("sbl1bak", 34816, 35839), ("abootbak", 36864, 37887), ("rpmbak", 38912, 39935),
    ("hypbak", 40960, 41983), ("pad", 43008, 43519), ("misc", 44032, 44543), ("ssd", 45056, 45119),
    ("splash", 46080, 47103), ("DDR", 48128, 48639), ("recovery", 49152, 50175),
]


def make_original_disk(root, path, partitions=None):
    """Full-size SPARSE fake eMMC with a stock-like GPT (primary and secondary) and recognisable partition data."""
    gptool = _gptool(root)
    entries = []
    partitions = partitions or FAKE_PARTITIONS
    for name, first, last in partitions:
        type_guid = gptool.BOOT_TYPE if name == "boot" else gptool.ROOT_TYPE
        entries.append(gptool.mk_entry(type_guid, str(uuid.uuid5(uuid.NAMESPACE_DNS, "openstick-easy-" + name)),
                                       first, last, 0, name))
    table = b"".join(entries).ljust(28 * 128, b"\0")
    crc = zlib.crc32(table) & 0xffffffff
    region = table.ljust(32 * SECTOR, b"\0")
    guid = uuid.uuid5(uuid.NAMESPACE_DNS, "openstick-easy-disk").bytes_le
    base = struct.pack(gptool.HDR, gptool.SIG, 0x10000, gptool.HDR_SZ, 0, 0, 1, EMMC_SECTORS - 1, 34,
                       EMMC_SECTORS - 34, guid, 2, 28, 128, crc).ljust(SECTOR, b"\0")
    primary_header = gptool.make_hdr(base, 1, EMMC_SECTORS - 1, 2, crc)
    secondary_header = gptool.make_hdr(base, EMMC_SECTORS - 1, 1, EMMC_SECTORS - 33, crc)
    mbr = bytearray(SECTOR)
    mbr[510:512] = b"\x55\xaa"
    mbr[450] = 0xee
    struct.pack_into("<II", mbr, 454, 1, EMMC_SECTORS - 1)
    with open(path, "wb") as disk:
        disk.truncate(EMMC_SECTORS * SECTOR)
        disk.write(bytes(mbr) + primary_header + region)
        disk.seek((EMMC_SECTORS - 33) * SECTOR)
        disk.write(region + secondary_header)
        for name, first, last in partitions:
            if name in ("boot", "system", "cache", "userdata"):
                continue
            disk.seek(first * SECTOR)
            disk.write((("unique-fake-%s-" % name).encode() * 64)[:SECTOR * 2])


# ---------------------------------------------------------------- round 9: firmware package and lk2nd (synthetic, deterministic)
PATHA_REGIONS = {"sbl1": (262144, 1024), "rpm": (268288, 1024), "tz": (270336, 2048), "hyp": (272384, 1024), "aboot": (264192, 2048)}
PATHA_ORDER = ("tz", "rpm", "sbl1", "hyp", "aboot")
LAYOUT_ID = "ufi103s-gpt-t-board6-v1"


def fw_image(name):
    """Deterministic synthetic 'new firmware' of the exact region size (never equal to the fake stock bytes)."""
    import hashlib
    return hashlib.shake_256(b"openstick-test-fw-" + name.encode()).digest(PATHA_REGIONS[name][1] * SECTOR)


def synthetic_lk2nd():
    import hashlib
    return hashlib.shake_256(b"openstick-test-lk2nd").digest(524288)


def sha256_hex(data):
    import hashlib
    return hashlib.sha256(data).hexdigest()


def synthetic_boot_image(tag=b"easy"):
    page = 2048
    kernel, ramdisk = b"K" * page, b"R" * page
    header = struct.pack("<8s10I16s512s32s1024s", b"ANDROID!", len(kernel), 0x80080000, len(ramdisk), 0x82000000,
                         0, 0x80f00000, 0x81e00000, page, 0, 0, tag, b"test", b"\0" * 32, b"")
    return header.ljust(page, b"\0") + kernel + ramdisk


def synthetic_ext4_stub(size=65536):
    """Superblock-only ext4 look-alike: enough for the image-set adapter, not for debugfs."""
    image = bytearray(size)
    sb = memoryview(image)[1024:2048]
    struct.pack_into("<I", sb, 4, size // 4096)
    struct.pack_into("<I", sb, 24, 2)
    sb[56:58] = b"\x53\xef"
    return bytes(image)


def real_ext4(path, workdir):
    """A tiny REAL ext4 with /etc/openstick, so the seed injection (debugfs) works."""
    source = pathlib.Path(workdir) / "ext4-src"
    (source / "etc/openstick").mkdir(parents=True, exist_ok=True)
    (source / "etc/openstick/.keep").write_text("")
    subprocess.check_call(["mke2fs", "-q", "-F", "-t", "ext4", "-d", str(source), str(path), "4M"],
                          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)


def _make_removable(path, remove=True):
    """Image sets and registered builds are read-only trees; make them writable so the runner can delete them."""
    for dirpath, dirnames, filenames in os.walk(path):
        with contextlib.suppress(OSError):
            os.chmod(dirpath, 0o700)
        for name in filenames:
            with contextlib.suppress(OSError):
                os.chmod(os.path.join(dirpath, name), 0o600)
    if remove:
        shutil.rmtree(path, ignore_errors=True)


# ---------------------------------------------------------------- the fake board
class Board:
    """One fake board: private store, sparse fake eMMC, fake USB tree, EDL dir, logs. Lives under the runner temp dir."""

    def __init__(self, root, tmp, name, partitions=None):
        self.root = pathlib.Path(root)
        self.runner_tmp = pathlib.Path(tmp)
        self.base = self.runner_tmp / ("easy-" + name)
        _make_removable(self.base)
        self.base.mkdir(mode=0o700)
        import atexit
        atexit.register(_make_removable, self.base, True)  # image sets and builds are created read-only; free the 3.9 GB fake disks when the group exits
        self.home = self.base / "private"
        self.sysfs = self.base / "usb/sys/bus/usb/devices"
        self.devroot = self.base / "usb/dev/bus/usb"
        self.sysfs.mkdir(parents=True)
        (self.devroot / "001").mkdir(parents=True)
        self.edl_dir = self.base / "edl"
        self.edl_dir.mkdir()
        shutil.copy(self.runner_tmp / "shims/bin/edl", self.edl_dir / "edl")
        (self.edl_dir / "edl").chmod(0o755)
        self.loader = self.edl_dir / "prog_emmc_firehose_8916.mbn"
        self.loader.write_bytes(b"test-only synthetic loader")  # the launcher swaps the pinned hash for this file's hash
        self.disk = self.base / "fake-emmc.img"
        self.initial = self.base / "fake-emmc.initial"
        make_original_disk(self.root, self.initial, partitions)
        self.lk2nd = self.base / "lk2nd-padded-512KiB.img"
        self.lk2nd.write_bytes(synthetic_lk2nd())
        self.fw_pkg = None
        self.write_log = self.base / "write.log"
        self.cmd_log = self.base / "edl-commands.log"
        self.build_mark = self.base / "build-calls.log"
        for path in (self.write_log, self.cmd_log, self.build_mark):
            path.write_text("")
        self.reset_disk()
        (self.base / "home").mkdir()
        self.fake_build_dir = None
        self.extra_env = {}

    # -- USB
    def plug(self):
        device = self.sysfs / "1-1"
        device.mkdir(exist_ok=True)
        for field, value in (("idVendor", "05c6"), ("idProduct", "9008"), ("busnum", "001"), ("devnum", "009")):
            (device / field).write_text(value + "\n")
        node = self.devroot / "001/009"
        node.write_text("")
        node.chmod(0o600)

    def unplug(self):
        shutil.rmtree(self.sysfs / "1-1", ignore_errors=True)

    def replug_action(self, before_plug=None, fresh=True):
        """For a Rule: simulate unplug, a second of silence, then a cold re-entry into EDL (fresh session).
        before_plug: called while unplugged (to change the board). fresh=False keeps the used Sahara session (not a cold boot)."""
        def worker():
            self.unplug()
            time.sleep(1.0)
            if fresh:
                (self.base / "edl-session").unlink(missing_ok=True)
            if before_plug:
                before_plug()
            self.plug()
        return lambda: threading.Thread(target=worker, daemon=True).start()

    def reset_disk(self):
        subprocess.check_call(["cp", "--sparse=always", str(self.initial), str(self.disk)], stdin=subprocess.DEVNULL)
        (self.base / "fake-emmc.img.writes").unlink(missing_ok=True)
        for log in (self.write_log, self.cmd_log):
            log.write_text("")

    def unchanged(self):
        return same_files(self.disk, self.initial)

    # -- fixtures built with the product's own code, like tests/verify-installer-m4.py does
    def _tools(self):
        sys.path.insert(0, str(self.root))
        from tools.installer import store, imageset
        return store, imageset

    def enable_fake_build(self, real_rootfs=False):
        """Make the launcher's replacement build_command available (no sudo, no debootstrap)."""
        self.fake_build_dir = self.base / "fake-build"
        self.fake_build_dir.mkdir()
        (self.fake_build_dir / "boot.img").write_bytes(synthetic_boot_image())
        if real_rootfs:
            real_ext4(self.fake_build_dir / "rootfs.img", self.base)
        else:
            (self.fake_build_dir / "rootfs.img").write_bytes(synthetic_ext4_stub())

    def register_build(self, name, tag=b"easy", boot=None):
        """Register a fake build (boot.img + rootfs.img + build.json) the way `openstick build` does; returns (build_id, boot, rootfs)."""
        import hashlib
        store, imageset = self._tools()
        store.ensure_store(self.home)
        build_id = "easy-set-build-" + name
        build_dir = self.home / "builds" / build_id
        build_dir.mkdir(mode=0o700)
        boot, rootfs = boot or synthetic_boot_image(tag), synthetic_ext4_stub()
        (build_dir / "boot.img").write_bytes(boot)
        (build_dir / "rootfs.img").write_bytes(rootfs)
        manifest = {"schema_version": 1, "build_id": build_id, "board": "ufi103s", "profile": "dev",
                    "artifacts": {"boot.img": {"sha256": hashlib.sha256(boot).hexdigest(), "size": len(boot)},
                                  "rootfs.img": {"sha256": hashlib.sha256(rootfs).hexdigest(), "size": len(rootfs)}}}
        (build_dir / "build.json").write_text(json.dumps(manifest))
        registry = store.load_registry(self.home)
        registry.setdefault("builds", []).append({"build_id": build_id, "path": str(build_dir), "board": "ufi103s",
                                                  "profile": "dev"})
        store.save_registry(self.home, registry)
        return build_id, boot, rootfs

    def make_image_set(self, name="image-set", boot=None, edit_gpt=None):
        """GPT-T image set for this fake board: layout derived from the fake board's own GPT by tools/gen-gpt-tz1m.py."""
        store, imageset = self._tools()
        build_id, _boot, _rootfs = self.register_build(name, boot=boot)
        primary = self.base / ("primary-" + name + ".bin")
        with self.initial.open("rb") as disk:
            primary.write_bytes(disk.read(34 * SECTOR))
        layout = self.base / ("layout-" + name)
        subprocess.check_call([sys.executable, "-I", str(self.root / "tools/gen-gpt-tz1m.py"), "--primary", str(primary),
                               "--emmc", str(EMMC_SECTORS), "--outdir", str(layout)],
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, timeout=60)
        if edit_gpt:
            edit_gpt(layout)
        output = self.base / name
        args = types.SimpleNamespace(build=build_id, layout=str(layout), output=str(output))
        assert imageset.prepare_command(args, self.home, self.root) == 0
        return output

    def make_fw_pkg(self, name="fw-pkg", edit=None, sums=True):
        """Private firmware package for this fake board: fw/*.img, gpt/gpt-T-*.bin (derived from the board's own GPT, like the
        product does), manifest, SHA256SUMS. `edit(dir)` may break it before SHA256SUMS is written (sums=True) or after."""
        pkg = self.base / name
        (pkg / "fw").mkdir(parents=True)
        (pkg / "gpt").mkdir()
        for fw in PATHA_ORDER:
            (pkg / "fw" / (fw + ".img")).write_bytes(fw_image(fw))
        primary = self.base / ("pkgprimary-" + name + ".bin")
        with self.initial.open("rb") as disk:
            primary.write_bytes(disk.read(34 * SECTOR))
        layout = self.base / ("pkglayout-" + name)
        subprocess.check_call([sys.executable, "-I", str(self.root / "tools/gen-gpt-tz1m.py"), "--primary", str(primary),
                               "--emmc", str(EMMC_SECTORS), "--outdir", str(layout)],
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, timeout=60)
        for kind in ("primary", "secondary"):
            shutil.copy(layout / ("gpt-T-%s.bin" % kind), pkg / "gpt" / ("gpt-T-%s.bin" % kind))
        (pkg / "easy-fw-manifest.env").write_text("BOARD=ufi103s\nEMMC_SECTORS=%d\nLAYOUT_ID=%s\n" % (EMMC_SECTORS, LAYOUT_ID))
        if edit and not sums:
            write_pkg_sums(pkg)
            edit(pkg)
        else:
            if edit:
                edit(pkg)
            write_pkg_sums(pkg)
        return pkg

    def product_image_sets(self):
        images = self.home / "images"
        return sorted(images.glob("easy-*")) if images.is_dir() else []

    # -- running
    def env(self, **overrides):
        env = {
            "HOME": str(self.base / "home"),
            "PATH": str(self.runner_tmp / "shims/bin") + ":/usr/bin:/bin",
            "LANG": "C.UTF-8", "TERM": "dumb",
            "TMPDIR": str(self.runner_tmp),
            "GIT_CEILING_DIRECTORIES": str(self.runner_tmp.parent),
            "PYTHONPATH": "%s:%s" % (self.root / "tests/host-path-guard", self.root),
            "OPENSTICK_USB_SYSFS_ROOT": str(self.sysfs),
            "OPENSTICK_USB_DEVICE_ROOT": str(self.devroot),
            "OPENSTICK_TEST_USB_ROOTS_DIR": str(self.runner_tmp),
            "OPENSTICK_EDL_DIR": str(self.edl_dir),
            "OPENSTICK_FAKE_EMMC": str(self.disk),
            "OPENSTICK_EDL_ALLOW_WRITES": "1",
            "OPENSTICK_EDL_WRITE_LOG": str(self.write_log),
            "OPENSTICK_EDL_COMMAND_FILE": str(self.cmd_log),
            "OPENSTICK_EDL_SESSION_FILE": str(self.base / "edl-session"),
            "OPENSTICK_TEST_LOADER": str(self.loader),
            "OPENSTICK_TEST_BUILD_MARK": str(self.build_mark),
            "LK2ND_IMG": str(self.lk2nd),
            "OPENSTICK_TEST_LK2ND_SHA256": sha256_hex(self.lk2nd.read_bytes()),
        }
        env.update(POLL_ENV)
        if self.fake_build_dir:
            env["OPENSTICK_TEST_FAKE_BUILD"] = str(self.fake_build_dir)
        if os.environ.get("OPENSTICK_GUARD_HIT_FILE"):
            env["OPENSTICK_GUARD_HIT_FILE"] = os.environ["OPENSTICK_GUARD_HIT_FILE"]
        env.update(self.extra_env)
        env.update(overrides)
        return env

    def easy(self, extra_args, rules, env_overrides=None, timeout=600, on_exit=None, fw_pkg=True):
        (self.base / "edl-session").unlink(missing_ok=True)
        extra_args = list(extra_args)
        if fw_pkg and "--fw-pkg" not in extra_args:
            if self.fw_pkg is None:
                self.fw_pkg = self.make_fw_pkg()
            extra_args += ["--fw-pkg", str(self.fw_pkg)]
        argv = [sys.executable, str(self.root / "tests/easy_launcher.py"), "--home", str(self.home), "--no-color",
                "--usb-sysfs-root", str(self.sysfs), "--usb-device-root", str(self.devroot)] + EASY_COMMAND + list(extra_args)
        return run_pty(argv, self.env(**(env_overrides or {})), rules, timeout=timeout, cwd=str(self.root), on_exit=on_exit)

    def bake_edl_env(self):
        """The guarded `restore` hands the EDL client a minimal environment (PATH, HOME, LANG). Test-only: replace the
        board's edl by a wrapper that supplies the fake-board variables itself, then runs the unchanged shim."""
        real = self.edl_dir / "edl.shim"
        if not real.exists():
            shutil.move(str(self.edl_dir / "edl"), str(real))
        lines = ["#!/usr/bin/env bash"]
        for name in ("OPENSTICK_FAKE_EMMC", "OPENSTICK_EDL_ALLOW_WRITES", "OPENSTICK_EDL_WRITE_LOG",
                     "OPENSTICK_EDL_COMMAND_FILE", "OPENSTICK_EDL_SESSION_FILE"):
            lines.append("export %s=${%s:-%s}" % (name, name, shlex.quote(self.env()[name])))
        lines.append("exec %s \"$@\"" % shlex.quote(str(real)))
        (self.edl_dir / "edl").write_text("\n".join(lines) + "\n")
        (self.edl_dir / "edl").chmod(0o755)
        shims = self.base / "fwlib-shims"
        shims.mkdir(exist_ok=True)
        (shims / "sha256sum").write_text(
            '#!/bin/bash\nset -eu\nfor arg in "$@"; do\n  if [[ "$arg" == %s ]]; then\n'
            "    printf '959439aa5864685999b713c3ed12ad5fa408149648b670a9a9ef77bcc9dcab14  %%s\\n' \"$arg\"; exit 0\n  fi\ndone\n"
            'exec /usr/bin/sha256sum "$@"\n' % shlex.quote(str(self.loader)))
        (shims / "sha256sum").chmod(0o755)
        self.extra_env["OPENSTICK_TEST_FWLIB_SHIM_DIR"] = str(shims)

    # -- observations
    def writes(self):
        return [line.split() for line in self.write_log.read_text().splitlines() if line.strip()]

    def edl_ops(self):
        return [line.split()[0] for line in self.cmd_log.read_text().splitlines() if line.strip()]

    def build_calls(self):
        return [line for line in self.build_mark.read_text().splitlines() if line.strip()]

    def backup_dirs(self):
        root = self.home / "backups"
        return sorted(p for p in root.glob("*/*") if p.is_dir() and not p.name.startswith(".")) if root.is_dir() else []

    def seeds(self):
        seeds = self.home / "seeds"
        return sorted(seeds.glob("*.seed")) if seeds.is_dir() else []


def base_rules(board, yes=False, mode="1"):
    """Prompt answers common to every happy-path run (unlisted prompts block until the run times out: a failure)."""
    rules = [Rule(RE_MODE, mode), Rule(RE_REUSE, ""), Rule(RE_STEP6, action=board.replug_action())]
    if not yes:
        rules.append(Rule(RE_CONFIRM, "YES"))
    return rules


def happy(board, extra, rules=None, **kw):
    """Plug the board and run `openstick easy`."""
    board.plug()
    return board.easy(extra, rules if rules is not None else base_rules(board), **kw)


# ---------------------------------------------------------------- expected-layout oracle and shared checks
def expected_overlays(image_set, rootfs=None, with_firmware=True):
    """Writes the finished disk must contain, from the image set and the documented GPT-T geometry:
    primary GPT at LBA 0, secondary in the last 33 sectors, boot and userdata at their planned LBAs.
    Preserved partitions keep their LBAs in the planned GPT-T, so they stay byte-identical to the initial disk."""
    image_set = pathlib.Path(image_set)
    manifest = json.loads((image_set / "manifest.json").read_text())
    parts = manifest["partitions"]
    overlays = [
        (0, (image_set / "layout/gpt-T-primary.bin").read_bytes()),
        ((EMMC_SECTORS - 33) * SECTOR, (image_set / "layout/gpt-T-secondary.bin").read_bytes()),
        (parts["boot"]["start_lba"] * SECTOR, synthetic_lk2nd() + (image_set / "payload/boot.img").read_bytes()),
        (parts["userdata"]["start_lba"] * SECTOR, pathlib.Path(rootfs or image_set / "payload/rootfs.img").read_bytes()),
    ]
    if with_firmware:
        overlays += [(lba * SECTOR, fw_image(name)) for name, (lba, _n) in PATHA_REGIONS.items()]
    return overlays


def stamp_dirs(board):
    """backups/<device-id>/<UTC-stamp>/ folders (plan step 3)."""
    found = board.backup_dirs()
    for path in found:
        assert re.match(r"\d{8}T\d{6}", path.name), "backup folder is not named by a UTC stamp: " + path.name
    return found


def check_no_traceback(output):
    assert "Traceback (most recent call last)" not in output, "expected failure printed a stack trace"


def check_restore_hint(output):
    """Failure path (E-5): the restore command and the unplug/re-enter advice are printed."""
    assert re.search(r"(?im)^.*\bopenstick\b.*\brestore\b.*--backup.*$", output), "no restore command printed"
    assert re.search(r"(?i)unplug 30 s and re-enter EDL", output), "no unplug-30-s advice printed"


def write_targets(board):
    return [line[1] for line in board.writes()]


def check_order(targets, boot_lba=396384, userdata_lba=2428000):
    """Secondary GPT, primary GPT, boot, userdata (plan step 8; P writes the secondary first)."""
    def at(names):
        for index, target in enumerate(targets):
            if target in names:
                return index
        raise AssertionError("no write to any of %s in %s" % (sorted(names), targets))
    gpt = max(at({"0"}), at({str(EMMC_SECTORS - 33)}))
    fw = [at({str(PATHA_REGIONS[n][0])}) for n in PATHA_ORDER]
    boot, userdata = at({str(boot_lba)}), at({str(userdata_lba)})
    assert at({str(EMMC_SECTORS - 33)}) < at({"0"}), "secondary GPT must be written before the primary: %s" % targets
    assert gpt < fw[0] and fw == sorted(fw) and fw[-1] < boot < userdata, "write order is not gpt, tz rpm sbl1 hyp aboot, boot, userdata: %s" % targets


# ---------------------------------------------------------------- the printed restore command (guarded `restore`)
RESTORE_PHASES = ("pad", "userdata", "cache", "persist", "boot", "modemst1", "modemst2", "sbl1", "rpm", "tz", "tzbak",
                  "hyp", "aboot", "gpt-secondary", "gpt-primary")


def printed_restore_command(output):
    """The command exactly as easy mode printed it ('Restore command: ...' after a flash, 'To go back: ...' after a failure)."""
    match = re.search(r"(?m)^(?:Restore command: |To go back: )(openstick --home .* restore --device \S+ --backup \S+?)\.?(?: First unplug.*)?$", output)
    assert match, "no restore command printed:\n" + output[-800:]
    return match.group(1)


def run_printed_restore(board, command, dry_run=False, timeout=900):
    """Run the printed command (argv[0] 'openstick' replaced by the test launcher), answering each phase prompt exactly as
    the guarded command demands. A fresh cold EDL session is simulated first, as the printed advice says."""
    board.bake_edl_env()
    board.plug()
    (board.base / "edl-session").unlink(missing_ok=True)
    words = shlex.split(command)
    assert words[0] == "openstick" and words[1] == "--home" and words[3] == "restore", words
    argv = [sys.executable, str(board.root / "tests/easy_launcher.py"), "--no-color", "--usb-sysfs-root", str(board.sysfs),
            "--usb-device-root", str(board.devroot)] + words[1:]
    if dry_run:
        argv.insert(2, "--dry-run")
    rules = [Rule(r"Type exactly: restore \S+ %s\b" % re.escape(phase), None, action=None) for phase in ()]
    rules = []
    for phase in RESTORE_PHASES:
        rules.append(Rule(r"Type exactly: restore [0-9a-f]+ %s\b" % re.escape(phase), "restore %s %s" % (_device_suffix(words), phase)))
    return run_pty(argv, board.env(), rules, timeout=timeout, cwd=str(board.root))


def _device_suffix(words):
    return words[words.index("--device") + 1][-8:]


def registry(board):
    sys.path.insert(0, str(board.root))
    from tools.installer import store
    return store.load_registry(board.home)


def flash_entries(board):
    devices = registry(board)["devices"]
    assert len(devices) == 1, "expected one registered device, found %d" % len(devices)
    return devices[0].get("flashes", [])


def events(board):
    path = board.home / "events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


def check_restored(board, rc, out):
    """After the printed restore command: exit 0, the product's own final full-disk readback passed, and the fake disk is
    byte-identical to the verified backup (sparse compare with the original AND the backup manifest's SHA-256)."""
    import hashlib
    assert rc == 0, "printed restore command failed (rc=%s):\n%s" % (rc, out[-1500:])
    assert "PASS final full-disk readback exactly matches the verified backup SHA-256" in out, "no final readback pass printed"
    assert same_files(board.disk, board.initial), "fake disk differs from the original after restore"
    backup = board.backup_dirs()[0]
    want = json.loads((backup / "manifest.json").read_text())["sha256"]
    digest = hashlib.sha256()
    with board.disk.open("rb") as handle:
        for block in iter(lambda: handle.read(8 << 20), b""):
            digest.update(block)
    assert digest.hexdigest() == want, "fake disk SHA-256 differs from the verified backup SHA-256"


# ---------------------------------------------------------------- round 8: refusals before the first write
def check_refused(board, rc, out, pattern, reference=None):
    """A run that must stop BEFORE any write: non-zero exit, 'Nothing was written', the reason, no write to the fake
    disk (write log empty) and the disk byte-identical to `reference` (default: the original disk)."""
    assert rc != 0, "the run was not refused (rc=0):\n" + out[-1500:]
    check_no_traceback(out)
    assert "Nothing was written to the board." in out, "'Nothing was written' is not printed:\n" + out[-1200:]
    assert re.search(pattern, out), "refusal reason %r not printed:\n%s" % (pattern, out[-1200:])
    assert board.writes() == [], "the fake board was written: %s" % board.writes()[:3]
    assert same_files(board.disk, reference or board.initial), "the fake disk changed"


def device_id(board):
    devices = registry(board)["devices"]
    assert len(devices) == 1, "expected one registered device"
    return devices[0]["id"]


def snapshot_disk(board, name):
    path = board.base / name
    subprocess.check_call(["cp", "--sparse=always", str(board.disk), str(path)], stdin=subprocess.DEVNULL)
    return path


def edit_gpt(layout_dir, change):
    """Rewrite GPT-T files in `layout_dir`: change(entries) mutates {name: [start, end]}; CRCs and the secondary are rebuilt."""
    layout_dir = pathlib.Path(layout_dir)
    gptool = _gptool(pathlib.Path(__file__).resolve().parents[1])
    primary = (layout_dir / "gpt-T-primary.bin").read_bytes()
    template = gptool.load_gpt(primary, "primary", EMMC_SECTORS)
    entries = {e["name"]: [e["start"], e["end"], e["raw"]] for e in gptool.used(template)}
    change(entries)
    raws = []
    for name, (start, end, raw) in entries.items():
        raws.append(raw[:32] + struct.pack("<QQ", start, end) + raw[48:])
    new_primary, new_secondary = gptool.build(template, raws, EMMC_SECTORS)
    (layout_dir / "gpt-T-primary.bin").write_bytes(new_primary)
    (layout_dir / "gpt-T-secondary.bin").write_bytes(new_secondary)


def synthetic_boot_image_sized(total_bytes, tag=b"easy-big"):
    """Android boot v0 image of exactly `total_bytes` (page 2048): one kernel page, the rest ramdisk."""
    page = 2048
    assert total_bytes % page == 0 and total_bytes >= 3 * page
    kernel, ramdisk = b"K" * page, b"R" * (total_bytes - 2 * page)
    header = struct.pack("<8s10I16s512s32s1024s", b"ANDROID!", len(kernel), 0x80080000, len(ramdisk), 0x82000000,
                         0, 0x80f00000, 0x81e00000, page, 0, 0, tag, b"test", b"\0" * 32, b"")
    return header.ljust(page, b"\0") + kernel + ramdisk
