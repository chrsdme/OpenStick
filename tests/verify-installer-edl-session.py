#!/usr/bin/env python3
"""Focused session-state tests with fixture sysfs and the fake EDL client."""
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.installer import edl_runner, store
from tools.installer import cli, flash, packages, verify_restore


class SessionPreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="openstick-edl-session-")
        self.addCleanup(self.temp.cleanup)
        self.base = pathlib.Path(self.temp.name)
        self.sysfs = self.base / "sysfs"
        self.sysfs.mkdir()
        self.edl_dir = self.base / "edl-tools"
        self.edl_dir.mkdir()
        self.shim = self.edl_dir / "edl-stateful.py"
        self.shim.write_text(STATEFUL_EDL_SHIM)
        self.shim.chmod(0o700)
        self.loader = self.edl_dir / edl_runner.LOADER_NAME
        self.loader.write_bytes(b"synthetic pinned loader fixture")
        self.disk = self.base / "fake-emmc.bin"
        subprocess.check_call([sys.executable, str(ROOT / "tests/make-fake-emmc.py"),
                               str(self.disk), "--mib", "2"])
        self.env = dict(os.environ, OPENSTICK_FAKE_EMMC=str(self.disk),
                        OPENSTICK_EDL_SESSION_FILE=str(self.base / "session-state"))

    def make_session(self):
        return edl_runner.Session(sysfs_root=self.sysfs,
                                  client=[sys.executable, str(self.shim)],
                                  loader=self.loader, env=self.env)

    def fresh_edl_session(self):
        if not list(self.sysfs.iterdir()):
            self.add_usb("1-1", "05c6", "9008", "001", "009")
        state = self.base / "session-state"
        if state.exists():
            state.unlink()
        return self.make_session()

    def patch_loader_pin(self):
        return mock.patch.object(edl_runner, "PINNED_LOADER_SHA256",
                                 hashlib.sha256(self.loader.read_bytes()).hexdigest())

    def restore_runner_namespace(self):
        source = packages._RESTORE_RUNNER.replace(
            "# __STANDALONE_SNAPSHOT__", packages._STANDALONE_SNAPSHOT)
        namespace = {"__name__": "synthetic_restore_runner"}
        exec(compile(source, "<restore-runner>", "exec"), namespace)
        namespace["EDL_CLIENT"] = [sys.executable, str(self.shim)]
        namespace["EDL_LOADER"] = self.loader
        namespace["PINNED_LOADER_SHA256"] = hashlib.sha256(self.loader.read_bytes()).hexdigest()
        namespace["EDL_SYSFS_ROOT"] = self.sysfs
        return namespace

    def add_usb(self, name, vendor, product, bus="001", dev="009"):
        device = self.sysfs / name
        device.mkdir()
        for key, value in (("idVendor", vendor), ("idProduct", product),
                           ("busnum", bus), ("devnum", dev)):
            (device / key).write_text(value + "\n")
        return device

    def test_preflight_classifies_absent_and_normal_without_client_invocation(self):
        absent = edl_runner.preflight(self.sysfs)
        self.assertEqual(absent.state, "ABSENT")
        session = edl_runner.Session(sysfs_root=self.sysfs)
        with self.assertRaises(edl_runner.AbsentDeviceError):
            session.run(["rs", "0", "34", str(self.base / "unused.bin")], timeout=1)
        self.assertEqual(session.invocations, 0)
        normal = self.add_usb("1-1", "05c6", "90b4")
        state = edl_runner.preflight(self.sysfs)
        self.assertEqual(state.state, "NORMAL-MODE")
        self.assertEqual(state.product_id, "90b4")
        with self.assertRaises(edl_runner.NormalModeError):
            session.run(["rs", "0", "34", str(self.base / "unused.bin")], timeout=1)
        self.assertEqual(session.invocations, 0)

    def test_preflight_captures_edl_bus_and_device_numbers(self):
        self.add_usb("1-2", "05c6", "9008", "003", "017")
        state = edl_runner.preflight(self.sysfs)
        self.assertEqual(state.state, "EDL-PRESENT")
        self.assertEqual((state.busnum, state.devnum), ("003", "017"))
        session = edl_runner.Session(sysfs_root=self.sysfs)
        session._before_call()
        (self.sysfs / "1-2" / "devnum").write_text("018\n")
        with self.assertRaises(edl_runner.BoardSwapError):
            session._before_call()

    def test_edl_output_tail_masks_serial_by_default(self):
        tail = edl_runner._tail("Serial:            0xdeadbeef\nother output")
        self.assertNotIn("deadbeef", tail)
        self.assertIn("Serial:            0xde****ef", tail)

    def test_flash_identity_ranges_share_one_session_and_reuse_identity(self):
        parts = cli._parse_gpt(self.disk)
        recorded = {name: {"start_lba": start, "end_lba": end}
                    for name, (start, end) in parts.items()}
        session = self.fresh_edl_session()
        with self.patch_loader_pin():
            serial, identity = flash._live_identity(
                session.client, self.loader, self.env, self.base, recorded, session=session)
        self.assertEqual(serial, "deadbeef")
        self.assertEqual(session.identity["serial"], "deadbeef")
        self.assertEqual(session.invocations, 4)
        self.assertEqual(int((self.base / "session-state").read_text()), 4)
        self.assertEqual(set(identity), {"fsg", "sec", "fsc"})

    def test_verify_restore_full_read_and_identity_ranges_share_sessions(self):
        session = self.fresh_edl_session()
        with self.patch_loader_pin():
            serial, fingerprints, dump, parts = verify_restore._live(
                session.client, self.env, self.loader, self.base, "deadbeef", session=session)
        self.assertEqual(serial, "deadbeef")
        self.assertTrue(dump.is_file())
        self.assertEqual(session.invocations, 2)
        self.assertEqual(int((self.base / "session-state").read_text()), 2)

        session = self.fresh_edl_session()
        with self.patch_loader_pin():
            verify_restore._live_identity_only(
                session.client, self.env, self.loader, self.base, "deadbeef", parts,
                fingerprints, session=session)
        self.assertEqual(session.invocations, 4)
        self.assertEqual(int((self.base / "session-state").read_text()), 4)

    def test_generated_restore_runner_reuses_identity_across_edl_calls(self):
        namespace = self.restore_runner_namespace()
        self.fresh_edl_session()
        with self.patch_loader_pin(), mock.patch.dict(os.environ, self.env, clear=True):
            namespace["edl_call"](["rs", "0", "34", str(self.base / "gpt.bin")], 2)
            namespace["edl_call"](["rf", str(self.base / "full.bin")], 2)
        self.assertEqual(namespace.get("EDL_SESSION_IDENTITY", {}).get("serial"), "deadbeef")
        self.assertEqual(namespace.get("EDL_SESSION_CALLS"), 2)
        self.assertEqual(int((self.base / "session-state").read_text()), 2)

    def test_generated_restore_runner_blocks_board_swap_before_later_call(self):
        namespace = self.restore_runner_namespace()
        self.fresh_edl_session()
        with self.patch_loader_pin(), mock.patch.dict(os.environ, self.env, clear=True):
            namespace["edl_call"](["rs", "0", "34", str(self.base / "gpt.bin")], 2)
            (self.sysfs / "1-1" / "devnum").write_text("010\n")
            with self.assertRaisesRegex(RuntimeError, "bus/device number changed"):
                namespace["edl_call"](["rf", str(self.base / "full.bin")], 2)
        self.assertEqual(namespace.get("EDL_SESSION_CALLS"), 1)
        self.assertEqual(int((self.base / "session-state").read_text()), 1)


STATEFUL_EDL_SHIM = r'''#!/usr/bin/env python3
import os, pathlib, shutil, sys
args = sys.argv[1:]
operation = args[0]
positionals = [arg for arg in args[1:] if not arg.startswith("--")]
session = pathlib.Path(os.environ["OPENSTICK_EDL_SESSION_FILE"])
calls = int(session.read_text()) if session.exists() else 0
calls += 1
session.write_text(str(calls))
if calls == 1:
    print("main - Device detected :)")
    print("main - Mode detected: sahara")
    print("HWID:              0x007050e100000000")
    print("PK_HASH:           0x" + "0" * 64)
    print("Serial:            0xdeadbeef")
    print("sahara - Loader successfully uploaded.")
else:
    print("main - Device detected :)")
    print("main - Mode detected: firehose")
disk = pathlib.Path(os.environ["OPENSTICK_FAKE_EMMC"])
if operation == "rs":
    start, count, output = int(positionals[0]), int(positionals[1]), pathlib.Path(positionals[2])
    with disk.open("rb") as source, output.open("wb") as target:
        source.seek(start * 512)
        target.write(source.read(count * 512))
elif operation == "rf":
    shutil.copyfile(disk, positionals[0])
else:
    raise SystemExit(2)
'''


if __name__ == "__main__":
    unittest.main()
