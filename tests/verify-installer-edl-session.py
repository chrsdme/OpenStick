#!/usr/bin/env python3
"""Focused session-state tests with fixture sysfs and the fake EDL client."""
import hashlib
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.installer import edl_runner, store


class SessionPreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="openstick-edl-session-")
        self.addCleanup(self.temp.cleanup)
        self.base = pathlib.Path(self.temp.name)
        self.sysfs = self.base / "sysfs"
        self.sysfs.mkdir()

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


if __name__ == "__main__":
    unittest.main()
