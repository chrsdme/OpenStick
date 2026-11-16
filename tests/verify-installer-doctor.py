#!/usr/bin/env python3
"""Doctor prerequisite and USB discovery checks using filesystem fixtures."""
import contextlib
import hashlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.installer import cli, edl_runner


def add_usb_device(sysfs, bus="1", device="2"):
    entry = Path(sysfs) / "1-1"
    entry.mkdir(parents=True)
    (entry / "idVendor").write_text("05c6\n")
    (entry / "idProduct").write_text("9008\n")
    (entry / "busnum").write_text(bus + "\n")
    (entry / "devnum").write_text(device + "\n")
    return entry


class DoctorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="openstick-doctor-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.sysfs = self.root / "sysfs"
        self.sysfs.mkdir()
        self.usb = self.root / "usb"
        self.usb.mkdir()
        self.home = self.root / "private-store"
        self.env = mock.patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES": str(self.root.parent)})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_usb_present_with_access_uses_device_node_path(self):
        add_usb_device(self.sysfs)
        calls = []

        def accessible(path, mode):
            calls.append((Path(path), mode))
            return True

        status, detail, hint = cli._check_edl_usb(self.sysfs, self.usb, accessible, plugdev_exists=False)
        self.assertEqual(status, "PASS")
        self.assertIn(str(self.usb / "001" / "002"), detail)
        self.assertEqual(calls, [(self.usb / "001" / "002", os.R_OK | os.W_OK)])
        self.assertIsNone(hint)

    def test_usb_present_without_access_fails_with_narrow_udev_hint(self):
        add_usb_device(self.sysfs, bus="12", device="34")
        status, detail, hint = cli._check_edl_usb(
            self.sysfs, self.usb, lambda path, mode: False, plugdev_exists=False)
        self.assertEqual(status, "FAIL")
        self.assertIn(str(self.usb / "012" / "034"), detail)
        self.assertIn('SUBSYSTEM=="usb", ATTR{idVendor}=="05c6", ATTR{idProduct}=="9008", TAG+="uaccess"', hint)
        self.assertNotIn("plugdev", hint)

    def test_usb_absent_is_info_and_does_not_fail_doctor(self):
        status, detail, hint = cli._check_edl_usb(self.sysfs, self.usb, lambda path, mode: False)
        self.assertEqual(status, "INFO")
        self.assertEqual(detail, "no EDL device attached; access will be checked when one is")
        self.assertIsNone(hint)

        self._install_resolver_fixtures()
        output = io.StringIO()
        with contextlib.redirect_stdout(output), \
             mock.patch.object(cli, "_which", side_effect=lambda name: "/usr/bin/" + name), \
             mock.patch.object(cli, "_disk_free", return_value=13 * 1024 ** 3):
            result = cli.doctor(cli.argparse.Namespace(home=str(self.home)),
                                usb_sysfs_root=self.sysfs, usb_device_root=self.usb,
                                access_check=lambda path, mode: False)
        self.assertEqual(result, cli.EXIT_OK)
        self.assertIn("INFO USB permissions: no EDL device attached; access will be checked when one is", output.getvalue())

    def test_usb_sysfs_scan_error_is_a_failure_with_a_specific_hint(self):
        status, detail, hint = cli._check_edl_usb(self.root / "no-sysfs", self.usb,
                                                  lambda path, mode: True)
        self.assertEqual(status, "FAIL")
        self.assertIn("could not scan", detail)
        self.assertIn("sysfs is mounted and readable", hint)

    def _install_resolver_fixtures(self):
        edl_dir = self.root / "edl"
        edl_dir.mkdir()
        client = edl_dir / "edl"
        client.write_text("#!/bin/sh\nexit 0\n")
        client.chmod(0o755)
        loader = edl_dir / edl_runner.LOADER_NAME
        loader.write_bytes(b"synthetic doctor loader")
        digest = hashlib.sha256(loader.read_bytes()).hexdigest()
        env_patcher = mock.patch.dict(os.environ, {"OPENSTICK_EDL_DIR": str(edl_dir)})
        env_patcher.start()
        self.addCleanup(env_patcher.stop)
        hash_patcher = mock.patch.object(edl_runner, "PINNED_LOADER_SHA256", digest)
        hash_patcher.start()
        self.addCleanup(hash_patcher.stop)

    def test_edl_resolver_prefers_executable_then_script(self):
        edl_dir = self.root / "resolver"
        edl_dir.mkdir()
        executable = edl_dir / "edl"
        executable.write_text("#!/bin/sh\nexit 0\n")
        executable.chmod(0o755)
        script = edl_dir / "edl.py"
        script.write_text("pass\n")
        with mock.patch.dict(os.environ, {"OPENSTICK_EDL_DIR": str(edl_dir)}):
            self.assertEqual(edl_runner.resolve_client(), [str(executable.resolve())])
            executable.chmod(0o644)
            self.assertEqual(edl_runner.resolve_client(), [os.sys.executable, str(script.resolve())])

    def test_edl_resolver_falls_back_to_path(self):
        bindir = self.root / "path-bin"
        bindir.mkdir()
        executable = bindir / "edl"
        executable.write_text("#!/bin/sh\nexit 0\n")
        executable.chmod(0o755)
        with mock.patch.dict(os.environ, {"PATH": str(bindir)}, clear=True):
            self.assertEqual(edl_runner.resolve_client(), [str(executable.resolve())])

    def test_loader_resolver_refuses_a_digest_mismatch(self):
        edl_dir = self.root / "bad-loader"
        edl_dir.mkdir()
        (edl_dir / edl_runner.LOADER_NAME).write_bytes(b"synthetic wrong loader")
        with mock.patch.dict(os.environ, {"OPENSTICK_EDL_DIR": str(edl_dir)}), \
             mock.patch.object(edl_runner, "PINNED_LOADER_SHA256", "0" * 64):
            with self.assertRaisesRegex(cli.store.RefusedError, "SHA-256 mismatch"):
                edl_runner.resolve_loader()

    def test_doctor_output_uses_shared_resolvers_and_specific_hints(self):
        self._install_resolver_fixtures()
        add_usb_device(self.sysfs)
        output = io.StringIO()
        with contextlib.redirect_stdout(output), \
             mock.patch.object(cli, "_which", side_effect=lambda name: "/usr/bin/" + name), \
             mock.patch.object(cli, "_disk_free", return_value=13 * 1024 ** 3):
            result = cli.doctor(cli.argparse.Namespace(home=str(self.home)),
                                usb_sysfs_root=self.sysfs, usb_device_root=self.usb,
                                access_check=lambda path, mode: True)
        text = output.getvalue()
        self.assertEqual(result, cli.EXIT_OK, text)
        self.assertIn("PASS EDL client:", text)
        self.assertIn("PASS Firehose loader:", text)
        self.assertIn("(sha256 verified)", text)
        self.assertIn("PASS USB permissions:", text)
        self.assertNotIn("Install hint:", text)

    def test_doctor_failure_hints_are_specific_for_edl_loader_and_usb(self):
        missing_edl_dir = self.root / "missing-edl"
        missing_edl_dir.mkdir()
        add_usb_device(self.sysfs)
        output = io.StringIO()
        with mock.patch.dict(os.environ, {"OPENSTICK_EDL_DIR": str(missing_edl_dir)}), \
             contextlib.redirect_stdout(output), \
             mock.patch.object(cli, "_which", side_effect=lambda name: "/usr/bin/" + name), \
             mock.patch.object(cli, "_disk_free", return_value=13 * 1024 ** 3):
            result = cli.doctor(cli.argparse.Namespace(home=str(self.home)),
                                usb_sysfs_root=self.sysfs, usb_device_root=self.usb,
                                access_check=lambda path, mode: False)
        text = output.getvalue()
        self.assertEqual(result, cli.EXIT_ERROR, text)
        self.assertIn("FAIL EDL client:", text)
        self.assertIn("OPENSTICK_EDL_DIR", text)
        self.assertIn("FAIL Firehose loader:", text)
        self.assertIn("pinned Firehose loader is missing", text)
        self.assertIn("FAIL USB permissions:", text)
        self.assertIn('TAG+="uaccess"', text)
        self.assertNotIn("Install hint:", text)

    def test_plugdev_hint_only_mentions_existing_group(self):
        hint = cli._edl_usb_udev_hint(plugdev_exists=True)
        self.assertIn('GROUP="plugdev"', hint)
        self.assertIn('TAG+="uaccess"', hint)
        no_group_hint = cli._edl_usb_udev_hint(plugdev_exists=False)
        self.assertNotIn("plugdev", no_group_hint)


if __name__ == "__main__":
    unittest.main(verbosity=2)
