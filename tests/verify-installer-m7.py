#!/usr/bin/env python3
"""Offline tests for wizard state, ordering, and fail-closed resume behavior."""
import json
import os
import pathlib
import hashlib
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.installer import store, verify_restore as m5, wizard


def args(stage, **values):
    defaults = dict(stage=stage, wizard_id="synthetic-run", resume=False,
                    wizard_device="deadbeef", wizard_backup="synthetic-backup",
                    wizard_build="synthetic-build", wizard_seed="synthetic-seed",
                    wizard_image_set="/tmp/synthetic-image-set", wizard_board="ufi103s",
                    wizard_profile="prod", wizard_host=None, dry_run=False,
                    bake_user_env=False, paranoid=False, i_accept_untested=False,
                    reconcile_state=False)
    defaults.update(values)
    return types.SimpleNamespace(**defaults)


class WizardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="openstick-wizard-m7-"))
        self.home = store.ensure_store(self.tmp / "private")
        self.repo = self.tmp / "builder"
        self.repo.mkdir()

    def tearDown(self):
        import shutil
        for path in sorted(self.tmp.rglob("*"), key=lambda item: len(item.parts), reverse=True):
            try:
                os.chmod(path, 0o700 if path.is_dir() else 0o600)
            except OSError:
                pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _state(self, names, pending=None):
        path = wizard._state_path(self.home, "synthetic-run")
        state = wizard._new_state("synthetic-run")
        state["selection"] = {"device_id": "deadbeef", "backup_id": "synthetic-backup",
                              "build_id": "synthetic-build", "seed_id": "synthetic-seed",
                              "image_set": "/tmp/synthetic-image-set", "board": "ufi103s",
                              "profile": "prod"}
        state["stages"] = [{"name": name, "status": "complete", "started_at": wizard._now(),
                            "finished_at": wizard._now(), "evidence": {}} for name in names]
        state["pending_destructive_stage"] = pending
        wizard._save(path, state)
        return path

    def test_requires_explicit_stage_and_dependency_order(self):
        with self.assertRaises(store.RefusedError):
            wizard.run(args(None), self.home, self.repo)
        with self.assertRaisesRegex(store.RefusedError, "requires completed stage"):
            with mock.patch.object(wizard, "_execute", return_value=0):
                wizard.run(args("flash"), self.home, self.repo)
        self.assertFalse(list((self.home / "wizard").glob("*.json")))

    def test_atomic_state_order_and_private_event_history(self):
        with mock.patch.object(wizard, "_execute", return_value=0) as execute:
            self.assertEqual(wizard.run(args("doctor"), self.home, self.repo), 0)
            self.assertEqual(wizard.run(args("configure", resume=True), self.home, self.repo), 0)
        path = self.home / "wizard" / "synthetic-run.json"
        state = json.loads(path.read_text())
        self.assertEqual([item["name"] for item in state["stages"]], ["doctor", "configure"])
        self.assertTrue(all(item["status"] == "complete" for item in state["stages"]))
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual((path.parent.stat().st_mode & 0o777), 0o700)
        self.assertEqual(execute.call_count, 2)
        events = [json.loads(line) for line in (self.home / "events.jsonl").read_text().splitlines()]
        self.assertEqual([event["type"] for event in events], [
            "wizard.stage.started", "wizard.stage.complete", "wizard.stage.started", "wizard.stage.complete"])

    def test_completed_stage_cannot_be_replayed(self):
        with mock.patch.object(wizard, "_execute", return_value=0):
            wizard.run(args("doctor"), self.home, self.repo)
        with self.assertRaisesRegex(store.RefusedError, "already completed"):
            with mock.patch.object(wizard, "_execute") as execute:
                wizard.run(args("doctor", resume=True), self.home, self.repo)
        execute.assert_not_called()

    def test_interrupted_destructive_stage_is_never_repeated_by_resume(self):
        self._state(("doctor", "backup", "inject"))
        with mock.patch.object(wizard, "_execute", side_effect=KeyboardInterrupt) as execute, \
             mock.patch.object(wizard, "_stage_evidence", return_value={"stage": "flash"}):
            with self.assertRaises(KeyboardInterrupt):
                wizard.run(args("flash", resume=True), self.home, self.repo)
        self.assertEqual(execute.call_count, 1)
        state = json.loads((self.home / "wizard" / "synthetic-run.json").read_text())
        self.assertEqual(state["pending_destructive_stage"], "flash")
        self.assertEqual(state["stages"][-1]["status"], "uncertain")
        with self.assertRaisesRegex(store.RefusedError, "unresolved destructive stage"):
            with mock.patch.object(wizard, "_execute") as retry:
                wizard.run(args("flash", resume=True), self.home, self.repo)
        retry.assert_not_called()

    def test_interrupted_backup_and_package_are_not_retried_in_same_run(self):
        """An uncertain non-device stage must not execute twice on resume."""
        for stage, selection in (
                ("backup", dict(wizard_device="deadbeef", wizard_backup=None,
                                 wizard_build=None, wizard_seed=None, wizard_image_set=None,
                                 wizard_board="ufi103s", wizard_profile=None, wizard_host=None,
                                 package_type=None, package_output=None)),
                ("package", dict(wizard_device="deadbeef", wizard_backup="synthetic-backup",
                                 wizard_build=None, wizard_seed=None, wizard_image_set=None,
                                 wizard_board="ufi103s", wizard_profile=None, wizard_host=None,
                                 package_type="restore", package_output=str(self.tmp / "restore.ostpkg.tar")))):
            run_id = "uncertain-" + stage
            prior = ("doctor",) if stage == "backup" else ("doctor", "backup")
            self._state(prior)
            state_path = self.home / "wizard" / "synthetic-run.json"
            if run_id != "synthetic-run":
                state_path = wizard._state_path(self.home, run_id)
                state = wizard._new_state(run_id)
                state["selection"] = {"device_id": "deadbeef", "backup_id": "synthetic-backup"}
                state["stages"] = [{"name": name, "status": "complete"} for name in prior]
                wizard._save(state_path, state)
            stage_args = args(stage, wizard_id=run_id, resume=True, **selection)
            with mock.patch.object(wizard, "_execute", side_effect=KeyboardInterrupt), \
                    mock.patch.object(wizard, "_stage_evidence", return_value={"stage": stage}):
                with self.assertRaises(KeyboardInterrupt):
                    wizard.run(stage_args, self.home, self.repo)
            interrupted = json.loads(state_path.read_text())
            self.assertEqual(wizard._entries(interrupted)[stage]["status"], "uncertain")
            with mock.patch.object(wizard, "_execute") as retry:
                with self.assertRaisesRegex(store.RefusedError, "outcome is uncertain"):
                    wizard.run(stage_args, self.home, self.repo)
            retry.assert_not_called()

    def test_reconciliation_is_a_separate_no_write_call(self):
        flash_path = self._state(("doctor", "backup", "inject"), pending="flash")
        flash_state = json.loads(flash_path.read_text())
        flash_state["stages"].append({"name": "flash", "status": "uncertain"})
        wizard._save(flash_path, flash_state)
        with mock.patch.object(wizard, "_execute", return_value=0) as reconcile:
            self.assertEqual(wizard.run(args("flash", resume=True, reconcile_state=True), self.home, self.repo), 0)
        reconcile.assert_called_once()
        state = json.loads((self.home / "wizard" / "synthetic-run.json").read_text())
        self.assertIsNone(state["pending_destructive_stage"])
        self.assertEqual(state["stages"][-1]["status"], "reconciled")
        with mock.patch.object(wizard, "_execute", return_value=0) as fresh_stage, \
             mock.patch.object(wizard, "_stage_evidence", return_value={"stage": "flash"}):
            self.assertEqual(wizard.run(args("flash", resume=True), self.home, self.repo), 0)
        fresh_stage.assert_called_once()

        restore_path = wizard._state_path(self.home, "synthetic-restore")
        restore_state = wizard._new_state("synthetic-restore")
        restore_state["selection"] = {"device_id": "deadbeef", "backup_id": "synthetic-backup"}
        restore_state["stages"] = [{"name": stage, "status": "complete"} for stage in ("doctor", "backup")]
        restore_state["stages"].append({"name": "restore", "status": "uncertain"})
        restore_state["pending_destructive_stage"] = "restore"
        wizard._save(restore_path, restore_state)
        with mock.patch.object(wizard, "_execute", return_value=0) as restore_reconcile:
            self.assertEqual(wizard.run(args("restore", wizard_id="synthetic-restore", resume=True,
                                             reconcile_state=True, wizard_seed=None, wizard_build=None,
                                             wizard_image_set=None, wizard_board=None, wizard_profile=None,
                                             wizard_host=None, package_type=None, package_output=None),
                                       self.home, self.repo), 0)
        restore_reconcile.assert_called_once()
        restored = json.loads(restore_path.read_text())
        self.assertIsNone(restored["pending_destructive_stage"])
        self.assertEqual(wizard._entries(restored)["restore"]["status"], "reconciled")

    def test_destructive_exception_and_secret_text_not_written_to_state(self):
        self._state(("doctor", "backup", "inject"))
        with mock.patch.object(wizard, "_execute", side_effect=RuntimeError("SYNTHETIC_SECRET command detail")), \
             mock.patch.object(wizard, "_stage_evidence", return_value={"stage": "flash"}):
            with self.assertRaisesRegex(RuntimeError, "SYNTHETIC_SECRET"):
                wizard.run(args("flash", resume=True), self.home, self.repo)
        raw = (self.home / "wizard" / "synthetic-run.json").read_text()
        self.assertNotIn("SYNTHETIC_SECRET", raw)
        self.assertNotIn("command detail", raw)

    def test_corrupt_or_symlinked_state_refuses(self):
        path = wizard._state_path(self.home, "synthetic-run")
        path.write_text("{")
        path.chmod(0o600)
        with self.assertRaisesRegex(store.RefusedError, "corrupt"):
            wizard.run(args("doctor", resume=True), self.home, self.repo)
        path.unlink()
        outside = self.tmp / "outside.json"
        outside.write_text("{}")
        path.symlink_to(outside)
        with self.assertRaisesRegex(store.RefusedError, "symlink|unsafe"):
            wizard.run(args("doctor", resume=True), self.home, self.repo)

    def test_state_load_is_anchored_before_a_leaf_swap_and_rejects_parent_symlink(self):
        path = wizard._state_path(self.home, "synthetic-run")
        original = wizard._new_state("synthetic-run")
        wizard._save(path, original)
        outside = self.tmp / "outside.json"
        forged = wizard._new_state("synthetic-run")
        forged["selection"] = {"forged": "synthetic-value"}
        outside.write_text(json.dumps(forged))
        real_open = os.open
        swap_count = []

        def swap_before_leaf_open(name, flags, *positional, **keywords):
            if name == path.name and keywords.get("dir_fd") is not None:
                path.unlink()
                path.symlink_to(outside)
                swap_count.append(name)
            return real_open(name, flags, *positional, **keywords)

        # Replace the leaf after its parent is pinned but before openat. O_NOFOLLOW must reject it.
        with mock.patch.object(wizard.os, "open", swap_before_leaf_open):
            with self.assertRaisesRegex(store.RefusedError, "unsafe"):
                wizard._load(path)
        self.assertEqual(swap_count, [path.name])
        self.assertTrue(path.is_symlink())
        path.unlink()
        wizard_dir = self.home / "wizard"
        moved_dir = self.home / "wizard-moved"
        wizard_dir.rename(moved_dir)
        wizard_dir.symlink_to(moved_dir, target_is_directory=True)
        with self.assertRaisesRegex(store.RefusedError, "symlink|unsafe"):
            wizard._load(path)

    def test_state_directory_refuses_symlink_home_without_creating_under_target(self):
        real_home = self.tmp / "real-home"
        real_home.mkdir()
        linked_home = self.tmp / "linked-home"
        linked_home.symlink_to(real_home, target_is_directory=True)
        with self.assertRaisesRegex(store.RefusedError, "symlink|unsafe"):
            wizard._safe_state_dir(linked_home)
        self.assertFalse((real_home / "wizard").exists())

    def test_selection_cannot_change_on_resume(self):
        with mock.patch.object(wizard, "_execute", return_value=0):
            wizard.run(args("doctor"), self.home, self.repo)
        with self.assertRaisesRegex(store.RefusedError, "cannot change during resume"):
            wizard.run(args("doctor", resume=True, wizard_device="cafebabe"), self.home, self.repo)

    def test_selected_image_set_checks_hashes_and_rejects_unlisted_files(self):
        image_set = self.tmp / "image-set"
        image_set.mkdir()
        artifact = image_set / "boot.bin"
        artifact.write_bytes(b"synthetic-boot")
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        sums = image_set / "SHA256SUMS"
        sums.write_text("%s  boot.bin\n" % digest)
        root, manifest_hash = wizard._verify_image_set(image_set)
        self.assertEqual(root, image_set)
        self.assertEqual(manifest_hash, hashlib.sha256(sums.read_bytes()).hexdigest())
        (image_set / "extra.bin").write_bytes(b"unlisted")
        with self.assertRaisesRegex(store.RefusedError, "unlisted"):
            wizard._verify_image_set(image_set)
        (image_set / "extra.bin").unlink()
        (image_set / "linked-dir").symlink_to(self.tmp, target_is_directory=True)
        with self.assertRaisesRegex(store.RefusedError, "unsafe directory"):
            wizard._verify_image_set(image_set)
        (image_set / "linked-dir").unlink()
        artifact.write_bytes(b"tampered")
        with self.assertRaisesRegex(store.RefusedError, "checksum mismatch"):
            wizard._verify_image_set(image_set)

    def test_synthetic_install_sequence_records_order_once(self):
        sequence = ("doctor", "backup", "configure", "build", "inject", "flash", "verify", "package")
        selections = dict(wizard_device="deadbeef", wizard_backup="synthetic-backup",
                          wizard_build="synthetic-build", wizard_seed="synthetic-seed",
                          wizard_image_set="/tmp/synthetic-image-set", wizard_board="ufi103s",
                          wizard_profile="prod", wizard_host="synthetic-user@synthetic-host",
                          package_type="install", package_output=str(self.tmp / "synthetic-package.ostpkg.tar"))
        calls = []
        def fake_execute(a, h, r, stage, state):
            calls.append(stage)
            if stage == "package":
                pathlib.Path(state["selection"]["package_output"]).write_bytes(b"synthetic package bytes")
            return 0
        real_result_ids = wizard._result_ids
        def fake_result_ids(stage, home, before, after, state):
            if stage == "package":
                return real_result_ids(stage, home, before, after, state)
            return {}
        with mock.patch.object(wizard, "_execute", side_effect=fake_execute), \
             mock.patch.object(wizard, "_result_ids", side_effect=fake_result_ids), \
             mock.patch.object(wizard, "_stage_evidence", side_effect=lambda stage, home, state: {"stage": stage}):
            for index, stage in enumerate(sequence):
                self.assertEqual(wizard.run(args(stage, resume=index > 0, **selections), self.home, self.repo), 0)
        self.assertEqual(calls, list(sequence))
        state = json.loads((self.home / "wizard" / "synthetic-run.json").read_text())
        self.assertEqual([item["name"] for item in state["stages"]], list(sequence))
        self.assertTrue(all(item["status"] == "complete" for item in state["stages"]))
        package_evidence = state["stages"][-1]["evidence"]
        self.assertEqual(package_evidence["package_sha256"], hashlib.sha256(b"synthetic package bytes").hexdigest())
        self.assertIsNone(state["pending_destructive_stage"])
        with self.assertRaisesRegex(store.RefusedError, "already completed"):
            with mock.patch.object(wizard, "_execute") as repeated:
                wizard.run(args("flash", resume=True, **selections), self.home, self.repo)
        repeated.assert_not_called()

        restore_calls = []
        restore_selection = dict(wizard_id="synthetic-restore", wizard_device="deadbeef",
                                 wizard_backup="synthetic-backup", wizard_host=None)
        with mock.patch.object(wizard, "_execute", side_effect=lambda a, h, r, s, state: restore_calls.append(s) or 0), \
             mock.patch.object(wizard, "_result_ids", return_value={}), \
             mock.patch.object(wizard, "_stage_evidence", side_effect=lambda stage, home, state: {"stage": stage}):
            for index, stage in enumerate(("doctor", "backup", "restore")):
                self.assertEqual(wizard.run(args(stage, resume=index > 0, **restore_selection), self.home, self.repo), 0)
        self.assertEqual(restore_calls, ["doctor", "backup", "restore"])
        restore_state = json.loads((self.home / "wizard" / "synthetic-restore.json").read_text())
        self.assertIsNone(restore_state["pending_destructive_stage"])

    def test_fake_board_module_integration_and_explicit_hardware_boundary(self):
        from tools.installer import imageset
        from tools.installer import seed as seed_format

        # Reuse the deterministic M4 builders for valid synthetic GPT-T and
        # Android boot structures. No hardware, USB or network is involved.
        import importlib.util
        spec = importlib.util.spec_from_file_location("m4_synthetic_helpers", ROOT / "tests/verify-installer-m4.py")
        m4 = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m4)

        disk = self.tmp / "fake-emmc.bin"
        m5_spec = importlib.util.spec_from_file_location("m5_synthetic_helpers", ROOT / "tests/verify-installer-m5.py")
        m5_helpers = importlib.util.module_from_spec(m5_spec)
        m5_spec.loader.exec_module(m5_helpers)
        m5_helpers.make_disk(disk)
        bin_dir = self.tmp / "fake-bin"
        bin_dir.mkdir()
        ssh = bin_dir / "ssh"
        ssh.write_text("#!/bin/sh\ncat >/dev/null\n" + "".join(
            "printf '%s\\t%s\\t%s\\n'\n" % ("PASS", key, "synthetic evidence")
            for key in ("firewall_state", "firewall_syntax", "watchdog", "modem", "ntp", "wifi", "versions")))
        ssh.chmod(0o700)
        env = dict(os.environ, OPENSTICK_FAKE_EMMC=str(disk),
                   OPENSTICK_EDL_CALL_FILE=str(self.tmp / "edl-calls"),
                   PATH=str(bin_dir) + ":" + str(ROOT / "tests/shims/bin") + ":/usr/bin:/bin")

        # A valid seed and registered generic build are fixture inputs; the
        # wizard still calls the real parser/manifest validators and injector.
        seed_path = self.home / "seeds" / "synthetic.seed"
        store._atomic_private_write(seed_path, seed_format.serialize({"SEED_VERSION": "1", "HOSTNAME": "synthetic-stick"}).encode())
        build_id = "fixture-build"
        build_dir = self.home / "builds" / build_id
        build_dir.mkdir(mode=0o700)
        boot = build_dir / "boot.img"
        rootfs = build_dir / "rootfs.img"
        boot.write_bytes(m4.synthetic_boot_image())
        tree = self.tmp / "rootfs-tree"
        (tree / "etc/openstick").mkdir(parents=True)
        subprocess.run(["mke2fs", "-q", "-t", "ext4", "-F", "-d", str(tree), str(rootfs), "16M"], check=True)
        boot_sha, rootfs_sha = hashlib.sha256(boot.read_bytes()).hexdigest(), hashlib.sha256(rootfs.read_bytes()).hexdigest()
        manifest = {"schema_version": 1, "build_id": build_id, "board": "ufi103s", "profile": "prod",
                    "artifacts": {"boot.img": {"sha256": boot_sha, "size": boot.stat().st_size},
                                  "rootfs.img": {"sha256": rootfs_sha, "size": rootfs.stat().st_size}}}
        (build_dir / "build.json").write_text(json.dumps(manifest))
        registry = store.load_registry(self.home)
        registry.setdefault("builds", []).append({"build_id": build_id, "path": str(build_dir),
                                                   "board": "ufi103s", "profile": "prod",
                                                   "boot_sha256": boot_sha, "rootfs_sha256": rootfs_sha})
        store.save_registry(self.home, registry)

        layout = self.tmp / "layout"
        m4.synthetic_gpt_t(layout)
        image_set = self.tmp / "gpt-t-image-set"

        selections = dict(wizard_device=None, wizard_backup=None, wizard_build=build_id,
                          wizard_seed="synthetic", wizard_image_set=None, wizard_board="ufi103s",
                          wizard_profile="prod", wizard_host=None, package_type=None, package_output=None)
        with mock.patch.dict(os.environ, env, clear=True), mock.patch("tools.installer.cli.doctor", return_value=0):
            self.assertEqual(wizard.run(args("doctor", wizard_id="fake-install", **selections), self.home, ROOT), 0)
            self.assertEqual(wizard.run(args("backup", wizard_id="fake-install", resume=True, **selections), self.home, ROOT), 0)
            state = json.loads((self.home / "wizard/fake-install.json").read_text())
            self.assertEqual(state["selection"]["device_id"], "deadbeef")
            self.assertTrue(state["selection"]["backup_id"])
            self.assertEqual(wizard.run(args("configure", wizard_id="fake-install", resume=True, **selections), self.home, ROOT), 0)
            self.assertEqual(wizard.run(args("build", wizard_id="fake-install", resume=True, **selections), self.home, ROOT), 0)
            self.assertEqual(wizard.run(args("inject", wizard_id="fake-install", resume=True, **selections), self.home, ROOT), 0)
            self.assertTrue((build_dir / "personal-synthetic" / "rootfs.img").is_file())
            self.assertEqual(imageset.prepare_command(types.SimpleNamespace(build=build_id, layout=str(layout), output=str(image_set)),
                                                      self.home, ROOT), 0)
            with self.assertRaisesRegex(store.RefusedError, "HARDWARE_REQUIRED"):
                wizard.run(args("flash", wizard_id="fake-install", resume=True,
                                **dict(selections, wizard_image_set=str(image_set))), self.home, ROOT)

            # SSH verification uses a local deterministic executable, not SSH.
            verify_args = dict(wizard_device="deadbeef", wizard_backup=None, wizard_build=None,
                               wizard_seed=None, wizard_image_set=None, wizard_board="ufi103s",
                               wizard_profile=None, wizard_host="synthetic-user@device.invalid",
                               package_type=None, package_output=None)
            self.assertEqual(wizard.run(args("doctor", wizard_id="fake-verify", **verify_args), self.home, ROOT), 0)
            self.assertEqual(wizard.run(args("verify", wizard_id="fake-verify", resume=True, **verify_args), self.home, ROOT), 0)

            package_out = self.tmp / "restore.ostpkg.tar"
            package_args = dict(wizard_device="deadbeef", wizard_backup=state["selection"]["backup_id"],
                                wizard_build=None, wizard_seed=None, wizard_image_set=None,
                                wizard_board="ufi103s", wizard_profile=None, wizard_host=None,
                                package_type="restore", package_output=str(package_out))
            self.assertEqual(wizard.run(args("doctor", wizard_id="fake-package", **package_args), self.home, ROOT), 0)
            self.assertEqual(wizard.run(args("backup", wizard_id="fake-package", resume=True, **package_args), self.home, ROOT), 0)
            self.assertEqual(wizard.run(args("package", wizard_id="fake-package", resume=True, **package_args), self.home, ROOT), 0)
        pkg_state = json.loads((self.home / "wizard/fake-package.json").read_text())
        self.assertEqual(pkg_state["stages"][-1]["evidence"]["package_sha256"], hashlib.sha256(package_out.read_bytes()).hexdigest())
        edl_calls = (self.tmp / "edl-calls").read_text().splitlines()
        self.assertEqual(edl_calls, ["2"])  # only identity read + full backup read; flash refuses before EDL

    def test_wizard_restore_runs_guarded_fake_edl_and_requires_full_disk_match(self):
        """Exercise the wizard restore route through M5's existing fake EDL gates."""
        import importlib.util
        spec = importlib.util.spec_from_file_location("m5_synthetic_helpers_for_wizard", ROOT / "tests/verify-installer-m5.py")
        helpers = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helpers)
        fixture = helpers.M5Tests("test_restore_fake_disk_is_byte_identical_and_records_each_phase")
        fixture.setUp()
        try:
            # Simulate a changed userdata partition after the verified stock backup.
            with fixture.disk.open("r+b") as stream:
                stream.seek(fixture.parts["userdata"][0] * 512 + 17)
                stream.write(b"changed")
            expected = (fixture.backup / "emmc-full.bin").read_bytes()
            expected_hash = hashlib.sha256(expected).hexdigest()
            wizard_selection = dict(
                wizard_id="wizard-fake-restore", wizard_device="deadbeef",
                wizard_backup="synthetic", wizard_build=None, wizard_seed=None,
                wizard_image_set=None, wizard_board="ufi103s", wizard_profile=None,
                wizard_host=None, package_type=None, package_output=None)
            confirmations = []

            def confirm(phase, device_id):
                typed = "restore %s %s" % (device_id[-8:], phase)
                confirmations.append(typed)
                return typed

            with mock.patch.dict(os.environ, fixture.env, clear=True), \
                 mock.patch("tools.installer.cli.doctor", return_value=0), \
                 mock.patch.object(m5, "_live", return_value=("deadbeef", fixture.identity, fixture.disk, fixture.parts)), \
                 mock.patch.object(m5.shutil, "which", return_value=str(fixture.fake_edl_path)), \
                 mock.patch.object(m5.common, "_loader_candidate", return_value=fixture.loader), \
                 mock.patch.object(m5.common, "LOADER_SHA256", hashlib.sha256(fixture.loader.read_bytes()).hexdigest()), \
                 mock.patch.object(m5, "_fwlib_path", return_value=str(fixture.fakebin) + ":/usr/bin:/bin"), \
                 mock.patch.object(m5, "_live_identity_only"), \
                 mock.patch.object(m5, "_confirm", side_effect=confirm):
                self.assertEqual(wizard.run(args("doctor", **wizard_selection), fixture.home, ROOT), 0)
                self.assertEqual(wizard.run(args("backup", resume=True, **wizard_selection), fixture.home, ROOT), 0)
                self.assertEqual(wizard.run(args("restore", resume=True, **wizard_selection), fixture.home, ROOT), 0)

            self.assertEqual(fixture.disk.read_bytes(), expected)
            self.assertEqual(hashlib.sha256(fixture.disk.read_bytes()).hexdigest(), expected_hash)
            self.assertEqual(len(confirmations), len(m5.PLAN_PHASES))
            restore_state = json.loads((fixture.home / "wizard/wizard-fake-restore.json").read_text())
            self.assertEqual(restore_state["pending_destructive_stage"], None)
            self.assertEqual(wizard._entries(restore_state)["restore"]["status"], "complete")
            engine_state = json.loads((fixture.home / "devices/deadbeef/restore-state.json").read_text())
            self.assertEqual(engine_state["status"], "complete")
            self.assertEqual(engine_state["completed_phases"], list(m5.PLAN_PHASES))
        finally:
            fixture.tearDown()


if __name__ == "__main__":
    unittest.main(verbosity=2)
