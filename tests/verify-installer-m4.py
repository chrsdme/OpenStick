#!/usr/bin/env python3
"""Offline M4 wrapper tests. All external effects are mocked or use fake EDL."""
import hashlib
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.installer import build as build_module
from tools.installer import flash as flash_module
from tools.installer import store
from tools.installer.cli import _hash_range, _parse_gpt


def digest(path):
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()


def build_args(board="ufi103s", profile="dev", legacy=False, dry=False):
    return types.SimpleNamespace(profile=profile, build_board=board, bake_user_env=legacy, dry_run=dry)


class FakeTTY(io.StringIO):
    def isatty(self):
        return True


class M4Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="openstick-m4-test-"))
        self.home = store.ensure_store(self.tmp / "private")
        self.repo = self.tmp / "repo"
        (self.repo / "boards" / "ufi103s").mkdir(parents=True)
        (self.repo / "boards" / "ufi103s" / "board.env").write_text("BOARD_DESC=test\n")
        (self.repo / "scripts").mkdir()
        for name in ("20-rootfs.sh", "50-boot-img.sh", "51-rootfs-img.sh"):
            (self.repo / "scripts" / name).write_text("#!/bin/sh\nexit 0\n")
        (self.repo / "out").mkdir()
        (self.repo / "out" / "base-bookworm-arm64.tar.gz").write_bytes(b"fake-base")

    def tearDown(self):
        import shutil
        for path in sorted(self.tmp.rglob("*"), key=lambda item: len(item.parts), reverse=True):
            try:
                os.chmod(path, 0o700 if path.is_dir() else 0o600)
            except OSError:
                pass
        try:
            os.chmod(self.tmp, 0o700)
        except OSError:
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_build_generic_and_legacy_env_order_and_permissions(self):
        real_run = build_module.subprocess.run
        for legacy in (False, True):
            calls = []
            original_env = dict(os.environ, WIFI_PSK="FAKE_PERSONAL_SECRET", ROOT_PASSWORD_HASH="FAKE_HASH")
            def fake_run(command, env=None, **kwargs):
                if command[0] == "git":
                    return types.SimpleNamespace(returncode=128, stdout="")
                calls.append((command, dict(env or {})))
                script = next(pathlib.Path(part).name for part in command if part.endswith(("20-rootfs.sh", "50-boot-img.sh", "51-rootfs-img.sh")))
                if script == "50-boot-img.sh":
                    out = pathlib.Path(env["PROD_OUT"]); out.mkdir(parents=True, exist_ok=True); (out / "boot.img").write_bytes(b"boot")
                if script == "51-rootfs-img.sh":
                    out = pathlib.Path(env["PRODROOT_OUT"]); out.mkdir(parents=True, exist_ok=True); (out / "rootfs.img").write_bytes(b"rootfs")
                return types.SimpleNamespace(returncode=0)
            with mock.patch.dict(os.environ, original_env, clear=True), mock.patch.object(build_module.os, "geteuid", return_value=1000), \
                 mock.patch.object(build_module, "_version", return_value="test-tool"), mock.patch.object(build_module.subprocess, "run", side_effect=fake_run):
                rc = build_module.build_command(build_args(legacy=legacy), self.home, self.repo)
            self.assertEqual(rc, 0)
            self.assertEqual([next(pathlib.Path(part).name for part in c[0] if part.endswith(("20-rootfs.sh", "50-boot-img.sh", "51-rootfs-img.sh"))) for c in calls], ["20-rootfs.sh", "50-boot-img.sh", "51-rootfs-img.sh"])
            privileged = calls[0][0]
            self.assertEqual(privileged[0], "sudo")
            self.assertFalse(calls[1][0][0] == "sudo")
            self.assertFalse(calls[2][0][0] == "sudo")
            if legacy:
                self.assertIn("FAKE_PERSONAL_SECRET", calls[0][1].values())
                self.assertEqual(calls[0][1]["OPENSTICK_CONFIG_MODE"], "legacy")
            else:
                self.assertNotIn("WIFI_PSK", calls[0][1])
                self.assertNotIn("ROOT_PASSWORD_HASH", calls[0][1])
                self.assertEqual(calls[0][1]["OPENSTICK_CONFIG_MODE"], "generic")
                self.assertNotIn("FAKE_PERSONAL_SECRET", calls[0][0])
            registry = store.load_registry(self.home)
            entry = registry["builds"][-1]
            target = pathlib.Path(entry["path"])
            manifest = json.loads((target / "build.json").read_text())
            self.assertEqual(manifest["legacy_user_env"], legacy)
            self.assertEqual(manifest["artifacts"]["boot.img"]["sha256"], digest(target / "boot.img"))
            self.assertEqual(manifest["artifacts"]["rootfs.img"]["sha256"], digest(target / "rootfs.img"))
            self.assertEqual((target / "boot.img").stat().st_mode & 0o777, 0o400)
            self.assertEqual((target / "rootfs.img").stat().st_mode & 0o777, 0o400)
            self.assertEqual(manifest["device_boot_verified"], False)
            calls.clear()

    def test_build_failure_is_not_registered(self):
        calls = []
        def fake_run(command, env=None, **kwargs):
            calls.append(next(pathlib.Path(part).name for part in command if part.endswith(("20-rootfs.sh", "50-boot-img.sh", "51-rootfs-img.sh"))))
            return types.SimpleNamespace(returncode=8)
        with mock.patch.object(build_module.os, "geteuid", return_value=1000), mock.patch.object(build_module.subprocess, "run", side_effect=fake_run):
            with self.assertRaises(store.StoreError):
                build_module.build_command(build_args(), self.home, self.repo)
        self.assertEqual(calls, ["20-rootfs.sh"])
        self.assertEqual(store.load_registry(self.home).get("builds", []), [])
        self.assertFalse(list((self.home / "builds").glob(".build-*")))

    def test_common_config_isolation_and_legacy_precedence(self):
        import shutil
        config = self.repo / "config"; config.mkdir()
        shutil.copyfile(ROOT / "config/defaults.env", config / "defaults.env")
        (config / "local.env").write_text("WIFI_PSK=LOCAL_FAKE\n")
        (config / "user.env").write_text("WIFI_PSK=USER_FAKE\n")
        (config / "user.env").chmod(0o600)
        shutil.copytree(ROOT / "boards", self.repo / "boards", dirs_exist_ok=True)
        shutil.copyfile(ROOT / "scripts/common.sh", self.repo / "scripts/common.sh")
        cmd = ["/bin/sh", "-c", '. "$OPENSTICK_ROOT/scripts/common.sh"; printf "%s" "${WIFI_PSK:-}"']
        base_env = {"OPENSTICK_ROOT": str(self.repo), "BOARD": "ufi103s", "PATH": "/usr/bin:/bin"}
        generic = subprocess.run(cmd, env=dict(base_env, OPENSTICK_CONFIG_MODE="generic"), text=True, capture_output=True, check=True)
        base_env["WIFI_PSK"] = "INHERITED_FAKE"
        legacy = subprocess.run(cmd, env=dict(base_env, OPENSTICK_CONFIG_MODE="legacy"), text=True, capture_output=True, check=True)
        self.assertEqual(generic.stdout, "")
        self.assertEqual(legacy.stdout, "INHERITED_FAKE")

    def _flash_fixture(self, board="ufi103s", with_backup=True, live_identity=True):
        disk = self.tmp / "fake-emmc.bin"
        subprocess.check_call([sys.executable, str(ROOT / "tests/make-fake-emmc.py"), str(disk), "--mib", "2"])
        parsed = _parse_gpt(disk)
        identity = {name: _hash_range(disk, *parsed[name]) for name in ("fsg", "sec", "fsc")}
        manifest_parts = {name: {"start_lba": start, "end_lba": end} for name, (start, end) in parsed.items()}
        device_id = "deadbeef"
        if board != "ufi103s":
            (self.repo / "boards" / board).mkdir(parents=True, exist_ok=True)
            (self.repo / "boards" / board / "board.env").write_text("BOARD_DESC=untested fixture\n")
        dev = {"id": device_id, "board": board, "identity": identity, "backups": [], "flashes": []}
        if with_backup:
            backup = self.home / "backups" / device_id / "fake-backup"
            backup.mkdir(mode=0o700, parents=True, exist_ok=True)
            import shutil
            shutil.copyfile(disk, backup / "emmc-full.bin")
            image_hash = digest(backup / "emmc-full.bin")
            (backup / "manifest.json").write_text(json.dumps({"sha256": image_hash, "identity": identity, "partitions": manifest_parts}))
            (backup / "SHA256SUMS").write_text(image_hash + "  emmc-full.bin\n")
            dev["backups"].append({"id": "fake-backup", "path": str(backup), "sha256": image_hash,
                                   "verified_by": ["full-read-size", "gpt-primary-and-backup-crc"]})
        state = store.load_registry(self.home); state["devices"] = [dev]; store.save_registry(self.home, state)
        # Test the wrapper gate logic with synthetic pinned artifacts. The real
        # flash scripts are replaced below by _invoke, never executed.
        image = self.tmp / "image-set"
        if image.exists():
            import shutil
            shutil.rmtree(image)
        image.mkdir()
        for rel, payload in (("files-" + board + "/boot.bin", b"fake-boot"), ("files-" + board + "/rootfs.bin", b"fake-rootfs"),
                             ("fw/layout-" + board + ".env", b"TEST_LAYOUT=1\n"), ("loader/prog_emmc_firehose_8916.mbn", b"fake-loader")):
            path = image / rel; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(payload)
        local_files = image / ("files-" + board)
        (local_files / "SHA256SUMS").write_text("%s  boot.bin\n%s  rootfs.bin\n" % (digest(local_files / "boot.bin"), digest(local_files / "rootfs.bin")))
        patha_data = image / "pathA"
        for d in ("fw", "gpt", "rollback"): (patha_data / d).mkdir(parents=True, exist_ok=True)
        (patha_data / "fw/test.bin").write_bytes(b"firmware")
        (patha_data / "SHA256SUMS").write_text(digest(patha_data / "fw/test.bin") + "  fw/test.bin\n")
        sums = []
        for path in sorted(image.rglob("*")):
            if path.is_file() and path.name != "SHA256SUMS":
                sums.append("%s  %s" % (digest(path), path.relative_to(image).as_posix()))
        sums.append("%s  files-%s/SHA256SUMS" % (digest(local_files / "SHA256SUMS"), board))
        sums.append("%s  pathA/SHA256SUMS" % digest(patha_data / "SHA256SUMS"))
        (image / "SHA256SUMS").write_text("\n".join(sums) + "\n")
        patha = self.repo / "tools/pathA"; patha.mkdir(parents=True, exist_ok=True)
        import shutil
        for script in ("pathA-flash.sh", "fwlib.sh"):
            shutil.copyfile(ROOT / "tools/pathA" / script, patha / script)
        (self.repo / "tools/flash").mkdir(parents=True, exist_ok=True)
        args = types.SimpleNamespace(image_set=str(image), flash_device=device_id, board=None,
                                     i_accept_untested=False, dry_run=True, reconcile_state=False)
        env = dict(os.environ, PATH=str(ROOT / "tests/shims/bin") + ":/usr/bin:/bin")
        args.fake_disk = str(disk)
        return args, dev, identity, env

    def _fake_live(self, args):
        original = flash_module._live_identity
        def call(edl, env, temp):
            test_env = dict(env, OPENSTICK_FAKE_EMMC=args.fake_disk)
            return original(edl, test_env, temp)
        return call

    def test_flash_refusals_and_input_hashes(self):
        args, dev, identity, env = self._flash_fixture(with_backup=False)
        with mock.patch.dict(os.environ, env, clear=True), self.assertRaises(store.RefusedError):
            flash_module.flash_command(args, self.home, self.repo)

    def test_missing_backup_cli_exit_code_2(self):
        args, dev, identity, env = self._flash_fixture(with_backup=False)
        proc = subprocess.run([str(ROOT / "openstick"), "--home", str(self.home), "flash", "--device", "deadbeef",
                               "--image-set", args.image_set], env=dict(env, OPENSTICK_HOME=""), capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2, (proc.stdout, proc.stderr))
        args, dev, identity, env = self._flash_fixture(board="mf800", with_backup=False)
        with mock.patch.dict(os.environ, env, clear=True), self.assertRaisesRegex(store.RefusedError, "untested board"):
            flash_module.flash_command(args, self.home, self.repo)
        args.i_accept_untested = True
        with mock.patch.dict(os.environ, env, clear=True), self.assertRaisesRegex(store.RefusedError, "verified, hash-matching full backup"):
            flash_module.flash_command(args, self.home, self.repo)
        args, dev, identity, env = self._flash_fixture()
        image = pathlib.Path(args.image_set) / "files-ufi103s/boot.bin"
        image.write_bytes(b"tampered-image")
        with mock.patch.dict(os.environ, env, clear=True), self.assertRaises(store.RefusedError):
            flash_module.flash_command(args, self.home, self.repo)
        args, dev, identity, env = self._flash_fixture()
        (pathlib.Path(args.image_set) / "loader/prog_emmc_firehose_8916.mbn").unlink()
        with mock.patch.dict(os.environ, env, clear=True), self.assertRaisesRegex(store.RefusedError, "image set|loader"):
            flash_module.flash_command(args, self.home, self.repo)

    def test_flash_identity_mismatch_refused(self):
        args, dev, identity, env = self._flash_fixture()
        # Mutate the live fake fsg bytes; the registered backup remains intact.
        disk = self.tmp / "fake-emmc.bin"
        with disk.open("r+b") as stream: stream.seek(34 * 512); stream.write(b"X")
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(flash_module, "_live_identity", side_effect=self._fake_live(args)):
            with self.assertRaisesRegex(store.RefusedError, "identity fingerprints"):
                flash_module.flash_command(args, self.home, self.repo)

    def test_flash_dry_confirmation_protocol_and_failure_stop(self):
        args, dev, identity, env = self._flash_fixture()
        fake_loader = pathlib.Path(args.image_set) / "loader/prog_emmc_firehose_8916.mbn"
        commands = []
        def invoke(command, phase_env):
            commands.append((pathlib.Path(command[0]).name, command[-2], command[-1], phase_env.get("COMMIT"), phase_env.get("CONFIRM")))
            if command[-1] == "write" and command[-2] == "gpt": return 47
            return 0
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(flash_module, "_live_identity", side_effect=self._fake_live(args)), mock.patch.object(flash_module.common, "LOADER_SHA256", digest(fake_loader)), \
             mock.patch.object(flash_module, "_invoke", side_effect=invoke), mock.patch.object(flash_module.sys, "stdin", FakeTTY()), \
             mock.patch.object(flash_module.sys, "stdout", FakeTTY()), mock.patch("builtins.input", return_value="flash deadbeef gpt"):
            args.dry_run = False
            with self.assertRaisesRegex(store.StoreError, "phase gpt failed"):
                flash_module.flash_command(args, self.home, self.repo)
        self.assertEqual([x[2] for x in commands], ["dry", "dry", "write"])
        self.assertEqual(commands[-1][3:], ("1", "da-flashez-gpt"))
        self.assertFalse(any(x[1] == "tz" for x in commands))
        events = [json.loads(line) for line in (self.home / "events.jsonl").read_text().splitlines()]
        self.assertTrue(any(e.get("phase") == "gpt" and e.get("outcome") == "failed" for e in events))
        state = json.loads((self.home / "devices/deadbeef/flash-state.json").read_text())
        self.assertEqual(state["status"], "failed-write")

    def test_flash_wrong_confirmation_and_noninteractive_dry_only(self):
        args, dev, identity, env = self._flash_fixture()
        fake_loader = pathlib.Path(args.image_set) / "loader/prog_emmc_firehose_8916.mbn"
        calls = []
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(flash_module, "_live_identity", side_effect=self._fake_live(args)), mock.patch.object(flash_module.common, "LOADER_SHA256", digest(fake_loader)), \
             mock.patch.object(flash_module, "_invoke", side_effect=lambda c, e: calls.append((c[-1], e.get("COMMIT"))) or 0), \
             mock.patch.object(flash_module.sys, "stdin", FakeTTY()), mock.patch.object(flash_module.sys, "stdout", FakeTTY()), \
             mock.patch("builtins.input", return_value="wrong"):
            args.dry_run = False
            with self.assertRaisesRegex(store.RefusedError, "confirmation text"):
                flash_module.flash_command(args, self.home, self.repo)
        self.assertEqual(calls, [("dry", None), ("dry", None)])

        args, dev, identity, env = self._flash_fixture()
        fake_loader = pathlib.Path(args.image_set) / "loader/prog_emmc_firehose_8916.mbn"
        calls = []
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(flash_module, "_live_identity", side_effect=self._fake_live(args)), mock.patch.object(flash_module.common, "LOADER_SHA256", digest(fake_loader)), \
             mock.patch.object(flash_module, "_invoke", side_effect=lambda c, e: calls.append((c[-1], e.get("COMMIT"))) or 0), \
             mock.patch.object(flash_module.sys, "stdin", mock.Mock(isatty=lambda: False)), mock.patch.object(flash_module.sys, "stdout", mock.Mock(isatty=lambda: False)):
            args.dry_run = False
            self.assertEqual(flash_module.flash_command(args, self.home, self.repo), 0)
        self.assertTrue(calls)
        self.assertFalse(any(mode == "write" or commit == "1" for mode, commit in calls))

        args, dev, identity, env = self._flash_fixture()
        fake_loader = pathlib.Path(args.image_set) / "loader/prog_emmc_firehose_8916.mbn"
        calls = []
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(flash_module, "_live_identity", side_effect=self._fake_live(args)), \
             mock.patch.object(flash_module.common, "LOADER_SHA256", digest(fake_loader)), \
             mock.patch.object(flash_module, "_invoke", side_effect=lambda c, e: calls.append((c[-1], e.get("COMMIT"))) or 0), \
             mock.patch.object(flash_module.sys, "stdin", FakeTTY()), mock.patch.object(flash_module.sys, "stdout", FakeTTY()):
            args.dry_run = True
            self.assertEqual(flash_module.flash_command(args, self.home, self.repo), 0)
        self.assertTrue(calls)
        self.assertFalse(any(mode == "write" or commit == "1" for mode, commit in calls))

    def test_flash_backup_slice_materialization(self):
        disk = self.tmp / "fixture-disk.bin"
        subprocess.check_call([sys.executable, str(ROOT / "tests/make-fake-emmc.py"), str(disk), "--mib", "2"])
        parts = _parse_gpt(disk)
        manifest_parts = {name: {"start_lba": start, "end_lba": end} for name, (start, end) in parts.items()}
        destination = self.tmp / "backup-slices"
        dump_dir = self.tmp / "backup-source"; dump_dir.mkdir()
        import shutil
        shutil.copyfile(disk, dump_dir / "emmc-full.bin")
        flash_module._make_flash_backup(dump_dir, {"partitions": manifest_parts}, destination)
        for name in ("modemst1", "modemst2", "fsc", "fsg", "persist", "sec"):
            start, end = parts[name]
            self.assertEqual((destination / name).read_bytes(), disk.read_bytes()[start * 512:(end + 1) * 512])
        self.assertEqual((destination / "SHA256SUMS").read_text().count("\n"), 6)
        self.assertEqual(destination.stat().st_mode & 0o777, 0o700)

    def test_flash_full_phase_order_and_confirm_tokens(self):
        args, dev, identity, env = self._flash_fixture()
        fake_loader = pathlib.Path(args.image_set) / "loader/prog_emmc_firehose_8916.mbn"
        calls = []
        def invoke(command, phase_env):
            calls.append((command[-2], command[-1], phase_env.get("COMMIT"), phase_env.get("CONFIRM")))
            return 0
        confirmations = ["flash deadbeef " + phase for phase in ("gpt", "tz", "rpm", "sbl1", "hyp", "aboot", "first-install")]
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(flash_module, "_live_identity", side_effect=self._fake_live(args)), \
             mock.patch.object(flash_module.common, "LOADER_SHA256", digest(fake_loader)), mock.patch.object(flash_module, "_invoke", side_effect=invoke), \
             mock.patch.object(flash_module.sys, "stdin", FakeTTY()), mock.patch.object(flash_module.sys, "stdout", FakeTTY()), \
             mock.patch("builtins.input", side_effect=confirmations):
            args.dry_run = False
            self.assertEqual(flash_module.flash_command(args, self.home, self.repo), 0)
        self.assertEqual([phase for phase, mode, _, _ in calls if mode == "write"], ["gpt", "tz", "rpm", "sbl1", "hyp", "aboot", "first-install"])
        self.assertEqual([phase for phase, mode, _, _ in calls if mode == "dry"], ["pre", "gpt", "tz", "rpm", "sbl1", "hyp", "aboot", "final", "first-install"])
        writes = [(phase, confirm) for phase, mode, commit, confirm in calls if mode == "write"]
        self.assertEqual(writes, [(p, ("flash-ufi103s-first-install" if p == "first-install" else "da-flashez-" + p))
                                  for p in ("gpt", "tz", "rpm", "sbl1", "hyp", "aboot", "first-install")])
        self.assertTrue(all(commit == "1" for _, mode, commit, _ in calls if mode == "write"))
        self.assertFalse(any(commit == "1" for phase, mode, commit, _ in calls if phase == "final"))
        self.assertEqual(store.load_registry(self.home)["devices"][0]["flashes"][0]["outcome"], "engine-reported-pass-not-boot-verified")

    def test_interrupted_write_requires_reconciliation_and_is_not_retried(self):
        args, dev, identity, env = self._flash_fixture()
        fake_loader = pathlib.Path(args.image_set) / "loader/prog_emmc_firehose_8916.mbn"
        calls = []
        def interrupt(command, phase_env):
            calls.append((command[-2], command[-1]))
            if command[-1] == "write": raise KeyboardInterrupt()
            return 0
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(flash_module, "_live_identity", side_effect=self._fake_live(args)), \
             mock.patch.object(flash_module.common, "LOADER_SHA256", digest(fake_loader)), mock.patch.object(flash_module, "_invoke", side_effect=interrupt), \
             mock.patch.object(flash_module.sys, "stdin", FakeTTY()), mock.patch.object(flash_module.sys, "stdout", FakeTTY()), \
             mock.patch("builtins.input", return_value="flash deadbeef gpt"):
            args.dry_run = False
            with self.assertRaises(KeyboardInterrupt): flash_module.flash_command(args, self.home, self.repo)
        state = json.loads((self.home / "devices/deadbeef/flash-state.json").read_text())
        self.assertEqual(state["status"], "interrupted")
        before = len(calls)
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(flash_module, "_live_identity", side_effect=self._fake_live(args)), \
             mock.patch.object(flash_module.common, "LOADER_SHA256", digest(fake_loader)), mock.patch.object(flash_module, "_invoke", side_effect=interrupt):
            with self.assertRaisesRegex(store.RefusedError, "unresolved"):
                flash_module.flash_command(args, self.home, self.repo)
        self.assertEqual(len(calls), before)
        device = store.load_registry(self.home)["devices"][0]
        self.assertEqual(device["flashes"][0]["outcome"], "partial-failure-reconciliation-required")
        args.reconcile_state = True
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(flash_module, "_live_identity", side_effect=self._fake_live(args)), \
             mock.patch.object(flash_module.common, "LOADER_SHA256", digest(fake_loader)), mock.patch.object(flash_module, "_invoke", side_effect=AssertionError("reconciliation must not execute scripts")), \
             mock.patch.object(flash_module.sys, "stdin", FakeTTY()), mock.patch.object(flash_module.sys, "stdout", FakeTTY()), \
             mock.patch("builtins.input", return_value="reconcile deadbeef gpt"):
            self.assertEqual(flash_module.flash_command(args, self.home, self.repo), 0)
        self.assertFalse((self.home / "devices/deadbeef/flash-state.json").exists())
        self.assertEqual(store.load_registry(self.home)["devices"][0]["flashes"][0]["outcome"], "device-state-reconciled-by-user")


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(M4Tests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())
