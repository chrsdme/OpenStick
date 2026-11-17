#!/usr/bin/env python3
"""Focused session-state tests with fixture sysfs and the fake EDL client."""
import hashlib
import contextlib
import io
import json
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

def run_test_process(command, *args, **kwargs):
    """Run non-interactive test children with bounded time and closed stdin."""
    kwargs.setdefault("stdin", subprocess.DEVNULL)
    kwargs.setdefault("timeout", 120)
    return subprocess.run(command, *args, **kwargs)

def check_test_process(command, *args, **kwargs):
    """check_call equivalent with closed stdin and a per-process timeout."""
    kwargs.setdefault("stdin", subprocess.DEVNULL)
    kwargs.setdefault("timeout", 120)
    return subprocess.check_call(command, *args, **kwargs)

from tools.installer import edl_runner, store
from tools.installer import cli, flash, packages, verify_restore


class SessionPreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="openstick-edl-session-")
        self.addCleanup(self.temp.cleanup)
        self.base = pathlib.Path(self.temp.name)
        self.old_ceiling = os.environ.get("GIT_CEILING_DIRECTORIES")
        os.environ["GIT_CEILING_DIRECTORIES"] = str(self.base.parent)
        self.addCleanup(self._restore_ceiling)
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
        check_test_process([sys.executable, str(ROOT / "tests/make-fake-emmc.py"),
                               str(self.disk), "--mib", "2"])
        self.env = dict(os.environ, OPENSTICK_FAKE_EMMC=str(self.disk),
                        OPENSTICK_EDL_SESSION_FILE=str(self.base / "session-state"))

    def _restore_ceiling(self):
        if self.old_ceiling is None:
            os.environ.pop("GIT_CEILING_DIRECTORIES", None)
        else:
            os.environ["GIT_CEILING_DIRECTORIES"] = self.old_ceiling

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

    def test_session_classifies_fresh_sahara_and_captures_identity_once(self):
        session = self.fresh_edl_session()
        with self.patch_loader_pin():
            result = session.run(["rs", "0", "34", str(self.base / "fresh.bin")], timeout=2)
        self.assertIn("Mode detected: sahara", result.stdout)
        self.assertEqual(session.identity["serial"], "deadbeef")
        self.assertEqual(session.invocation_count, 1)

    def test_session_classifies_consumed_firehose_without_identity(self):
        self.add_usb("1-1", "05c6", "9008", "001", "009")
        (self.base / "session-state").write_text("1")
        session = self.make_session()
        with self.patch_loader_pin(), self.assertRaises(edl_runner.ConsumedSessionError):
            session.run(["rs", "0", "34", str(self.base / "consumed.bin")], timeout=2)
        self.assertIsNone(session.identity)
        self.assertEqual(session.invocation_count, 1)

    def test_session_classifies_sahara_handshake_timeout_as_hung(self):
        session = self.fresh_edl_session()
        session.env = dict(self.env, HANG_IDENTITY="1")
        with self.patch_loader_pin():
            with self.assertRaises(edl_runner.HungSessionError):
                session.run(["rs", "0", "34", str(self.base / "hung.bin")], timeout=0.2)

    def test_doctor_makes_zero_edl_client_invocations(self):
        device = self.add_usb("1-1", "05c6", "9008", "001", "009")
        (device / "busnum").write_text("001\n")
        (device / "devnum").write_text("009\n")
        cli_edl_dir = self.base / "doctor-edl-tools"
        cli_edl_dir.mkdir()
        client = cli_edl_dir / "edl"
        shutil.copy2(self.shim, client)
        client.chmod(0o700)
        shutil.copy2(self.loader, cli_edl_dir / edl_runner.LOADER_NAME)
        output = io.StringIO()
        environment = dict(self.env, OPENSTICK_EDL_DIR=str(cli_edl_dir))
        with self.patch_loader_pin(), mock.patch.dict(os.environ, environment, clear=True), \
                contextlib.redirect_stdout(output):
            result = cli.doctor(type("Args", (), {"home": str(self.base / "doctor-home")})(),
                                usb_sysfs_root=self.sysfs, usb_device_root=self.base / "dev-fixture",
                                access_check=lambda path, mode: True)
        self.assertEqual(result, cli.EXIT_OK, output.getvalue())
        self.assertFalse((self.base / "session-state").exists())
        self.assertNotIn("Mode detected", output.getvalue())

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
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            namespace["finish_session"]()
        self.assertEqual(output.getvalue(), edl_runner.EDL_SESSION_USED_NOTE + "\n")

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

    def test_session_proxy_checks_board_swap_before_later_client_call(self):
        session = self.fresh_edl_session()
        output = io.StringIO()
        with self.patch_loader_pin():
            with contextlib.redirect_stdout(output), mock.patch.dict(os.environ, self.env, clear=True):
                with session:
                    session.run(["rs", "0", "34", str(self.base / "identity.bin")],
                                timeout=2, env=self.env)
                    proxy = session.proxy_command(self.base / "edl-session-proxy.py")
                    target = self.base / "proxy-read.bin"
                    args = proxy + ["rf", str(target), "--loader=" + str(self.loader), "--memory=emmc"]
                    first = run_test_process(args, capture_output=True, text=True)
                    self.assertEqual(first.returncode, 0, first.stderr)
                    self.assertTrue(target.is_file())
                    (self.sysfs / "1-1" / "devnum").write_text("010\n")
                    changed = run_test_process(args, capture_output=True, text=True)
        self.assertNotEqual(changed.returncode, 0)
        self.assertIn("bus/device number changed", changed.stderr)
        self.assertEqual(int((self.base / "session-state").read_text()), 2)
        self.assertEqual(output.getvalue(), edl_runner.EDL_SESSION_USED_NOTE + "\n")

    def test_flash_identity_refusal_emits_exact_session_note(self):
        parts = cli._parse_gpt(self.disk)
        recorded = {name: {"start_lba": start, "end_lba": end}
                    for name, (start, end) in parts.items()}
        session = self.fresh_edl_session()
        output = io.StringIO()
        with self.patch_loader_pin(), contextlib.redirect_stdout(output):
            try:
                with session:
                    flash._live_identity(session.client, self.loader, self.env, self.base,
                                         recorded, session=session)
                    raise store.RefusedError("synthetic pre-write refusal")
            except store.RefusedError:
                pass
        self.assertEqual(output.getvalue(), edl_runner.EDL_SESSION_USED_NOTE + "\n")

    def test_restore_fwlib_engine_uses_session_proxy(self):
        session = self.fresh_edl_session()
        target = self.base / "engine-read.bin"
        runner = self.base / "fake-fwlib-runner.sh"
        runner.write_text("#!/bin/bash\nset -eu\n$EDL_CMD rf %s --loader=$LDR --memory=emmc\n" % target)
        runner.chmod(0o700)
        engine = {"runner": runner}
        source = self.base / "phase-source.bin"
        source.write_bytes(b"synthetic phase")
        prepared = {"phase": "userdata", "start": 0, "end": 0, "source": source}
        with self.patch_loader_pin():
            session.run(["rs", "0", "34", str(self.base / "identity.bin")],
                        timeout=2, env=self.env)
            verify_restore._run_fwlib_phase(engine, session.client, self.env, self.loader,
                                            prepared, self.base, write=False, session=session)
        self.assertTrue(target.is_file())
        self.assertEqual(session.invocation_count, 2)
        self.assertEqual(int((self.base / "session-state").read_text()), 2)

    def test_backup_registry_refusal_emits_exact_session_note(self):
        self.add_usb("1-1", "05c6", "9008", "001", "009")
        cli_edl_dir = self.base / "cli-edl-tools"
        cli_edl_dir.mkdir()
        cli_client = cli_edl_dir / "edl"
        shutil.copy2(self.shim, cli_client); cli_client.chmod(0o700)
        cli_loader = cli_edl_dir / edl_runner.LOADER_NAME
        shutil.copy2(self.loader, cli_loader)
        home = self.base / "cli-home"
        store.ensure_store(home)
        store.save_registry(home, {"schema": 1, "devices": [{
            "id": "deadbeef", "edl": {"serial": "deadbeef"},
            "backups": [{"id": "existing"}],
        }]})
        environment = dict(self.env, OPENSTICK_EDL_DIR=str(cli_edl_dir))
        output, errors = io.StringIO(), io.StringIO()
        with self.patch_loader_pin(), mock.patch.dict(os.environ, environment, clear=True), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            result = cli.main(["--home", str(home), "--dry-run", "backup"],
                              usb_sysfs_root=self.sysfs)
        self.assertEqual(result, cli.EXIT_REFUSED)
        self.assertEqual(output.getvalue().splitlines()[-1], edl_runner.EDL_SESSION_USED_NOTE)
        self.assertIn("a verified backup already exists", errors.getvalue())

    def test_dry_run_refuses_after_a_real_verified_backup_in_a_fresh_fake_session(self):
        self.add_usb("1-1", "05c6", "9008", "001", "009")
        cli_edl_dir = self.base / "verified-backup-edl-tools"
        cli_edl_dir.mkdir()
        client = cli_edl_dir / "edl"
        shutil.copy2(self.shim, client)
        client.chmod(0o700)
        shutil.copy2(self.loader, cli_edl_dir / edl_runner.LOADER_NAME)
        home = self.base / "verified-backup-home"
        environment = dict(self.env, OPENSTICK_EDL_DIR=str(cli_edl_dir),
                           PATH=str(cli_edl_dir) + os.pathsep + "/usr/bin:/bin")
        backup_output, backup_error = io.StringIO(), io.StringIO()
        with self.patch_loader_pin(), mock.patch.dict(os.environ, environment, clear=True), \
                contextlib.redirect_stdout(backup_output), contextlib.redirect_stderr(backup_error):
            result = cli.main(["--home", str(home), "backup"], usb_sysfs_root=self.sysfs,
                              usb_device_root=self.base / "dev-fixture")
        self.assertEqual(result, cli.EXIT_OK, backup_error.getvalue())
        self.assertIn("Verified backup", backup_output.getvalue())
        self.assertIn("de****ef", backup_output.getvalue())
        self.assertNotIn("deadbeef", backup_output.getvalue())
        device = store.load_registry(home)["devices"][0]
        self.assertEqual(device["id"], "deadbeef")
        manifest_path = home / "backups" / "deadbeef" / device["backups"][0]["id"] / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        self.assertEqual(manifest["verified_by"], ["full-read-size", "gpt-primary-and-backup-crc", "sha256"])
        self.assertEqual(int((self.base / "session-state").read_text()), 2)

        power_cycle = run_test_process([str(ROOT / "tests/shims/bin/edl-power-cycle"),
                                      str(self.base / "session-state")], capture_output=True, text=True)
        self.assertEqual(power_cycle.returncode, 0, power_cycle.stderr)
        dry_output, dry_error = io.StringIO(), io.StringIO()
        with self.patch_loader_pin(), mock.patch.dict(os.environ, environment, clear=True), \
                contextlib.redirect_stdout(dry_output), contextlib.redirect_stderr(dry_error):
            result = cli.main(["--home", str(home), "--dry-run", "backup"],
                              usb_sysfs_root=self.sysfs, usb_device_root=self.base / "dev-fixture")
        self.assertEqual(result, cli.EXIT_REFUSED)
        self.assertIn("a verified backup already exists", dry_error.getvalue())
        self.assertNotIn("DRY RUN", dry_output.getvalue())
        self.assertEqual(dry_output.getvalue().splitlines()[-1], edl_runner.EDL_SESSION_USED_NOTE)
        self.assertEqual(int((self.base / "session-state").read_text()), 1)

    def test_cli_restore_refusal_after_edl_emits_exact_session_note(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "synthetic_m5_fixture", ROOT / "tests/verify-installer-m5.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        fixture = module.M5Tests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        self.add_usb("1-1", "05c6", "9008", "001", "009")
        with fixture.disk.open("r+b") as stream:
            stream.seek(fixture.parts["fsg"][0] * 512)
            stream.write(b"live-difference")
        output, errors = io.StringIO(), io.StringIO()
        with mock.patch.object(edl_runner, "PINNED_LOADER_SHA256",
                               hashlib.sha256(fixture.fake_loader_path.read_bytes()).hexdigest()), \
                mock.patch.dict(os.environ, fixture.env, clear=True), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            result = cli.main(["--home", str(fixture.home), "restore", "--device", "deadbeef"],
                              usb_sysfs_root=self.sysfs)
        self.assertEqual(result, cli.EXIT_REFUSED)
        self.assertIn("live EDL serial and fsg/sec/fsc fingerprints must match", errors.getvalue())
        self.assertEqual(output.getvalue().splitlines()[-1], edl_runner.EDL_SESSION_USED_NOTE)


STATEFUL_EDL_SHIM = r'''#!/usr/bin/env python3
import os, pathlib, shutil, signal, sys, time
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
    if os.environ.get("HANG_IDENTITY"):
        def interrupted(signum, frame):
            print("[!] Exiting cleanly…", flush=True)
            raise SystemExit(130)
        signal.signal(signal.SIGINT, interrupted)
        while True:
            time.sleep(1)
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
