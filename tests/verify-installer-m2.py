#!/usr/bin/env python3
"""M2 EDL runner and backup regressions using only the fake EDL shim."""
import contextlib
import hashlib
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
import signal
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.installer import cli, edl_runner, store


class EdlRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="openstick-m2-")
        self.addCleanup(self.temp.cleanup)
        self.base = pathlib.Path(self.temp.name)
        self.usb_sysfs_root = self.base / "sysfs"
        self.usb_device_root = self.base / "dev"
        usb = self.usb_sysfs_root / "1-1"
        usb.mkdir(parents=True)
        self.usb_device_root.mkdir()
        for name, value in (("idVendor", "05c6"), ("idProduct", "9008"),
                            ("busnum", "001"), ("devnum", "009")):
            (usb / name).write_text(value + "\n", encoding="ascii")
        self.old_ceiling = os.environ.get("GIT_CEILING_DIRECTORIES")
        os.environ["GIT_CEILING_DIRECTORIES"] = str(self.base.parent)
        self.addCleanup(self._restore_ceiling)
        self.edl_dir = self.base / "edl-tools"
        self.edl_dir.mkdir()
        self.shim = self.edl_dir / "edl"
        shutil.copy2(ROOT / "tests/shims/bin/edl", self.shim)
        self.shim.chmod(0o700)
        self.loader = self.edl_dir / edl_runner.LOADER_NAME
        self.loader.write_bytes(b"synthetic pinned loader fixture")
        self.env = dict(os.environ)
        self.env.update({
            "OPENSTICK_EDL_DIR": str(self.edl_dir),
            "PATH": str(self.edl_dir) + os.pathsep + str(ROOT / "tests/shims/bin") + os.pathsep + "/usr/bin:/bin",
            "OPENSTICK_EDL_COMMAND_FILE": str(self.base / "edl-commands.log"),
            "OPENSTICK_EDL_CALL_FILE": str(self.base / "edl-count"),
            "OPENSTICK_EDL_SESSION_FILE": str(self.base / "edl-session"),
            "OPENSTICK_FAKE_EMMC": str(self.base / "disk.img"),
        })
        subprocess.check_call([sys.executable, str(ROOT / "tests/make-fake-emmc.py"), self.env["OPENSTICK_FAKE_EMMC"], "--mib", "2"])

    def _restore_ceiling(self):
        if self.old_ceiling is None:
            os.environ.pop("GIT_CEILING_DIRECTORIES", None)
        else:
            os.environ["GIT_CEILING_DIRECTORIES"] = self.old_ceiling

    def _with_fixture_pin(self):
        return mock.patch.object(edl_runner, "PINNED_LOADER_SHA256", hashlib.sha256(self.loader.read_bytes()).hexdigest())

    def run_cli_backup(self, home, options=(), env_extra=None, verbose=False):
        subprocess.check_call([str(ROOT / "tests/shims/bin/edl-power-cycle"),
                               self.env["OPENSTICK_EDL_SESSION_FILE"]])
        environment = dict(self.env)
        environment["OPENSTICK_EDL_CALL_FILE"] = str(self.base / ("calls-" + str(len(list(self.base.glob("calls-*"))))))
        environment.update(env_extra or {})
        argv = ["--home", str(home)]
        if verbose:
            argv.append("--verbose")
        argv.extend(("backup", *options))
        output = io.StringIO()
        with mock.patch.dict(os.environ, environment, clear=True), self._with_fixture_pin(), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            status = cli.main(argv, usb_sysfs_root=self.usb_sysfs_root,
                              usb_device_root=self.usb_device_root)
        return status, output.getvalue(), environment

    def test_backup_identity_uses_sahara_serial_and_required_loader_flags(self):
        home = self.base / "home"
        result, output, _ = self.run_cli_backup(home)
        self.assertEqual(result, cli.EXIT_OK, output)
        registry = json.loads((home / "registry.json").read_text())
        self.assertEqual(registry["devices"][0]["id"], "deadbeef")
        commands = (self.base / "edl-commands.log").read_text().splitlines()
        self.assertEqual(len(commands), 2, commands)
        for command in commands:
            self.assertIn("--loader=" + str(self.loader.resolve()), command)
            self.assertIn("--memory=emmc", command)

    def test_resolvers_choose_executable_then_python_script_and_verify_loader(self):
        with mock.patch.dict(os.environ, self.env, clear=True):
            self.assertEqual(edl_runner.resolve_client(), [str(self.shim.resolve())])
        saved = self.shim.read_bytes()
        self.shim.unlink()
        script = self.edl_dir / "edl.py"
        script.write_text("print('synthetic edl')\n")
        with mock.patch.dict(os.environ, dict(self.env, OPENSTICK_EDL_DIR=str(self.edl_dir)), clear=True):
            self.assertEqual(edl_runner.resolve_client(), [sys.executable, str(script.resolve())])
        script.unlink()
        self.shim.write_bytes(saved)
        self.shim.chmod(0o700)
        with mock.patch.dict(os.environ, self.env, clear=True), self._with_fixture_pin():
            self.assertEqual(edl_runner.resolve_loader(), self.loader.resolve())
            self.loader.write_bytes(b"unverified")
            with self.assertRaisesRegex(store.RefusedError, "SHA-256"):
                edl_runner.resolve_loader()

    def test_missing_or_unverified_loader_is_refused_before_client_call(self):
        with self._with_fixture_pin():
            self.loader.write_bytes(b"wrong loader")
            with mock.patch.dict(os.environ, self.env, clear=True):
                with self.assertRaisesRegex(store.RefusedError, "SHA-256"):
                    edl_runner.run(["rs", "0", "1", str(self.base / "bad-read.bin")], timeout=1)
            self.loader.write_bytes(b"synthetic pinned loader fixture")
            self.loader.unlink()
            with mock.patch.dict(os.environ, self.env, clear=True):
                with self.assertRaisesRegex(store.RefusedError, "loader") as missing:
                    edl_runner.run(["rs", "0", "1", str(self.base / "read.bin")], timeout=1)
            self.assertIn("hint:", str(missing.exception).lower())
        self.assertFalse((self.base / "edl-count").exists())

    def test_parse_sahara_info_accepts_transcript_and_rejects_malformed_or_absent_blocks(self):
        transcript = "HWID: 0x007050e100000000\nPK_HASH: 0x" + "0" * 64 + "\nSerial:            0xdeadbeef\n"
        self.assertEqual(edl_runner.parse_sahara_info(transcript), {
            "serial": "deadbeef", "hwid": "007050e100000000", "pk_hash": "0" * 64,
        })
        for invalid in ("", "Serial: 0xdeadbee\nHWID: 0x1234\nPK_HASH: 0x" + "0" * 64,
                        "Serial: deadbeef\nHWID: 0x1234\nPK_HASH: 0x" + "0" * 64):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(store.StoreError, "Sahara") as caught:
                edl_runner.parse_sahara_info(invalid)
            self.assertIn("Hint:", str(caught.exception))

    def test_hang_sends_sigint_reports_power_cycle_and_does_not_register_backup(self):
        home = self.base / "hang-home"
        env = dict(self.env, OPENSTICK_EDL_HANG="1", OPENSTICK_EDL_SIGNAL_FILE=str(self.base / "signal-seen"))
        args = ["--home", str(home), "--verbose", "backup"]
        output = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), self._with_fixture_pin(), \
                mock.patch.object(edl_runner, "IDENTITY_READ_TIMEOUT_SECONDS", 0.15), \
                contextlib.redirect_stderr(output), contextlib.redirect_stdout(output):
            result = cli.main(args, usb_sysfs_root=self.usb_sysfs_root,
                              usb_device_root=self.usb_device_root)
        self.assertEqual(result, cli.EXIT_REFUSED)
        self.assertIn("Unplug USB for 30 s", output.getvalue())
        self.assertIn("The EDL client started the handshake but the board stopped responding", output.getvalue())
        self.assertTrue((self.base / "signal-seen").exists(), "shim did not observe SIGINT")
        self.assertFalse((home / "registry.json").exists())
        self.assertFalse(list((home / "backups").glob("**/manifest.json")))

    def test_verbose_failure_includes_client_output_tail(self):
        env = dict(self.env, OPENSTICK_EDL_FAIL_AT="1")
        with mock.patch.dict(os.environ, env, clear=True), self._with_fixture_pin():
            with self.assertRaises(store.StoreError) as caught:
                edl_runner.run(["rs", "0", "1", str(self.base / "read.bin")], timeout=1, verbose=True)
        self.assertIn("FAKE EDL injected failure", str(caught.exception))

    def test_no_loader_shim_mode_matches_transcript_and_exits_cleanly_on_sigint(self):
        env = dict(self.env, OPENSTICK_EDL_NO_LOADER_SIM="1", OPENSTICK_EDL_SIGNAL_FILE=str(self.base / "no-loader-signal"))
        process = subprocess.Popen([str(self.shim), "getstorageinfo"], env=env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            import time
            time.sleep(0.1)
            process.send_signal(signal.SIGINT)
            stdout, stderr = process.communicate(timeout=3)
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=3)
        self.assertIn("Trying with no loader given", stdout)
        self.assertIn("Couldn't find a loader for given hwid and pkhash", stdout)
        self.assertIn("[!] Exiting cleanly", stdout)
        self.assertTrue((self.base / "no-loader-signal").exists())

    def test_shim_fails_calls_missing_required_flags(self):
        result = subprocess.run([str(self.shim), "getstorageinfo"], env=self.env,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 91)
        self.assertIn("every call must include --loader", result.stderr)

    def test_shim_models_fresh_then_consumed_session_and_power_cycle(self):
        session = self.base / "session-state"
        env = dict(self.env, OPENSTICK_EDL_SESSION_FILE=str(session))
        first = subprocess.run([str(self.shim), "rs", "0", "1", str(self.base / "first.bin"),
                                "--loader=" + str(self.loader), "--memory=emmc"],
                               env=env, capture_output=True, text=True)
        second = subprocess.run([str(self.shim), "rs", "1", "1", str(self.base / "second.bin"),
                                 "--loader=" + str(self.loader), "--memory=emmc"],
                                env=env, capture_output=True, text=True)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn("Mode detected: sahara", first.stdout)
        self.assertIn("Loader successfully uploaded", first.stdout)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("Mode detected: firehose", second.stdout)
        self.assertNotIn("Serial:", second.stdout)

        helper = ROOT / "tests/shims/bin/edl-power-cycle"
        reset = subprocess.run([str(helper), str(session)], capture_output=True, text=True)
        self.assertEqual(reset.returncode, 0, reset.stderr)
        third = subprocess.run([str(self.shim), "rs", "2", "1", str(self.base / "third.bin"),
                                "--loader=" + str(self.loader), "--memory=emmc"],
                               env=env, capture_output=True, text=True)
        self.assertIn("Mode detected: sahara", third.stdout)

    def test_shim_absent_transcript_has_no_device_detected_marker(self):
        env = dict(self.env, OPENSTICK_EDL_DEVICE_STATE="absent")
        result = subprocess.run([str(self.shim), "rs", "0", "1", str(self.base / "absent.bin"),
                                 "--loader=" + str(self.loader), "--memory=emmc"],
                                env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Waiting for the device", result.stdout)
        self.assertIn("Exiting cleanly", result.stdout)
        self.assertNotIn("Device detected", result.stdout)

    def test_shim_hung_mode_stops_after_sahara_loader_upload(self):
        env = dict(self.env, OPENSTICK_EDL_HANG="sahara", OPENSTICK_EDL_SIGNAL_FILE=str(self.base / "hung-signal"))
        process = subprocess.Popen([str(self.shim), "rs", "0", "1", str(self.base / "hung.bin"),
                                     "--loader=" + str(self.loader), "--memory=emmc"],
                                    env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            import time
            time.sleep(0.1)
            process.send_signal(signal.SIGINT)
            stdout, stderr = process.communicate(timeout=3)
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=3)
        self.assertIn("Mode detected: sahara", stdout)
        self.assertIn("Loader successfully uploaded", stdout)
        self.assertNotIn("Mode detected: firehose", stdout)
        self.assertIn("Exiting cleanly", stdout)

    def test_backup_again_paranoid_short_read_and_failures_keep_registry_atomic(self):
        home = self.base / "repeat-home"
        self.assertEqual(self.run_cli_backup(home)[0], cli.EXIT_OK)
        self.assertEqual(self.run_cli_backup(home)[0], cli.EXIT_REFUSED)
        self.assertEqual(self.run_cli_backup(home, ("--again",))[0], cli.EXIT_OK)
        self.assertEqual(self.run_cli_backup(self.base / "paranoid-home", ("--paranoid",))[0], cli.EXIT_OK)

        for label, extra, expected in (
            ("short", {"OPENSTICK_EDL_SHORT_READ": "1"}, cli.EXIT_VERIFY),
            ("midread", {"OPENSTICK_EDL_FAIL_AT": "2"}, cli.EXIT_ERROR),
        ):
            failure_home = self.base / (label + "-home")
            status, output, _ = self.run_cli_backup(failure_home, env_extra=extra)
            self.assertEqual(status, expected, output)
            registry = failure_home / "registry.json"
            self.assertFalse(registry.exists() and json.loads(registry.read_text())["devices"])
            self.assertFalse(list((failure_home / "backups").glob("**/manifest.json")))

    def test_backup_rejects_gpt_corruption_and_paranoid_mismatch(self):
        for label, offset in (("primary", 512 + 16), ("backup", 2 * 1024 * 1024 - 512 + 16)):
            home = self.base / (label + "-gpt-home")
            status, output, _ = self.run_cli_backup(
                home, env_extra={"OPENSTICK_EDL_CORRUPT_OFFSET": str(offset)})
            self.assertEqual(status, cli.EXIT_VERIFY, output)
            self.assertFalse((home / "registry.json").exists() and json.loads((home / "registry.json").read_text())["devices"])
        home = self.base / "paranoid-mismatch-home"
        status, output, _ = self.run_cli_backup(home, ("--paranoid",), {
            "OPENSTICK_EDL_CORRUPT_OFFSET": "0", "OPENSTICK_EDL_CORRUPT_AT_CALL": "3"})
        self.assertEqual(status, cli.EXIT_VERIFY, output)


if __name__ == "__main__":
    unittest.main(verbosity=2)
