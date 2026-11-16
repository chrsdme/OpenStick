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
import struct
import uuid
import zlib
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.installer import build as build_module
from tools.installer import flash as flash_module
from tools.installer import imageset as imageset_module
from tools.installer import packages as packages_module
from tools.installer import store
from tools.installer import cli as cli_module
from tools.installer import verify_restore as verify_restore_module
from tools.installer.cli import _hash_range, _parse_gpt, parser


def digest(path):
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()


def build_args(board="ufi103s", profile="dev", legacy=False, dry=False):
    return types.SimpleNamespace(profile=profile, build_board=board, bake_user_env=legacy, dry_run=dry)


class FakeTTY(io.StringIO):
    def isatty(self):
        return True


def synthetic_gpt_t(directory, boot_start=396384, protective_start=1, disk_guid=None):
    import importlib.util
    source = ROOT / "scripts/gptool.py"
    spec = importlib.util.spec_from_file_location("test_gptool", source)
    gptool = importlib.util.module_from_spec(spec); spec.loader.exec_module(gptool)
    entries = [
        gptool.mk_entry(gptool.BOOT_TYPE, str(uuid.uuid4()), boot_start, boot_start + 32767, 0, "boot"),
        gptool.mk_entry(gptool.ROOT_TYPE, str(uuid.uuid4()), 429152, 2067551, 0, "system"),
        gptool.mk_entry(gptool.ROOT_TYPE, str(uuid.uuid4()), 2428000, 7634910, 0, "userdata"),
    ]
    table = b"".join(entries).ljust(28 * 128, b"\0")
    crc = zlib.crc32(table) & 0xffffffff
    region = table.ljust(32 * 512, b"\0")
    disk_guid = disk_guid if disk_guid is not None else uuid.uuid4().bytes_le
    base = struct.pack(gptool.HDR, gptool.SIG, 0x10000, gptool.HDR_SZ, 0, 0, 1,
                       imageset_module.EMMC_SECTORS - 1, 34, imageset_module.EMMC_SECTORS - 34,
                       disk_guid, 2, 28, 128, crc).ljust(512, b"\0")
    primary_header = gptool.make_hdr(base, 1, imageset_module.EMMC_SECTORS - 1, 2, crc)
    secondary_header = gptool.make_hdr(base, imageset_module.EMMC_SECTORS - 1, 1,
                                       imageset_module.EMMC_SECTORS - 33, crc)
    mbr = bytearray(512); mbr[510:512] = b"\x55\xaa"; mbr[450] = 0xee
    struct.pack_into("<II", mbr, 454, protective_start, imageset_module.EMMC_SECTORS - protective_start)
    primary = bytes(mbr) + primary_header + region
    secondary = region + secondary_header
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "gpt-T-primary.bin").write_bytes(primary)
    (directory / "gpt-T-secondary.bin").write_bytes(secondary)


def synthetic_boot_image():
    page = 2048
    kernel = b"K" * page
    ramdisk = b"R" * page
    header = struct.pack("<8s10I16s512s32s1024s", b"ANDROID!", len(kernel), 0x80080000,
                         len(ramdisk), 0x82000000, 0, 0x80f00000, 0x81e00000,
                         page, 0, 0, b"fixture", b"test", b"\0" * 32, b"")
    return header.ljust(page, b"\0") + kernel + ramdisk


def synthetic_ext4_image(size=65536):
    image = bytearray(size)
    sb = memoryview(image)[1024:2048]
    struct.pack_into("<I", sb, 4, size // 4096)
    struct.pack_into("<I", sb, 24, 2)
    sb[56:58] = b"\x53\xef"
    return bytes(image)


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

    def test_new_cli_contracts_are_explicit_and_no_tested_board_bypass(self):
        verify = parser().parse_args(["verify", "--host", "synthetic.invalid"])
        self.assertEqual((verify.command, verify.host), ("verify", "synthetic.invalid"))
        restore = parser().parse_args(["restore", "--device", "deadbeef", "--backup", "fixture-backup"])
        self.assertEqual((restore.command, restore.restore_device, restore.restore_backup),
                         ("restore", "deadbeef", "fixture-backup"))
        install = parser().parse_args(["package", "install", "--output", "/tmp/openstick-test.ostpkg.tar",
                                       "--device", "deadbeef", "--backup", "fixture-backup", "--image-set", "/tmp/images"])
        self.assertEqual((install.package_command, install.package_type, install.package_device,
                          install.package_backup, install.package_image_set),
                         ("install", "install", "deadbeef", "fixture-backup", "/tmp/images"))
        verify_package = parser().parse_args(["package", "verify", "/tmp/fixture.ostpkg.tar"])
        self.assertEqual((verify_package.package_command, verify_package.path), ("verify", "/tmp/fixture.ostpkg.tar"))
        wizard = parser().parse_args(["wizard", "--id", "fixture-run", "--stage", "build", "--resume",
                                      "--board", "ufi103s", "--profile", "prod", "--package-type", "install",
                                      "--package-output", "/tmp/fixture.ostpkg.tar"])
        self.assertEqual((wizard.command, wizard.wizard_id, wizard.stage, wizard.resume,
                          wizard.wizard_board, wizard.wizard_profile, wizard.package_type, wizard.package_output),
                         ("wizard", "fixture-run", "build", True, "ufi103s", "prod", "install", "/tmp/fixture.ostpkg.tar"))
        image_set = parser().parse_args(["image-set", "prepare", "--build", "build-1", "--layout", "/tmp/layout", "--output", "/tmp/set"])
        self.assertEqual((image_set.image_set_command, image_set.build, image_set.layout, image_set.output),
                         ("prepare", "build-1", "/tmp/layout", "/tmp/set"))
        with self.assertRaises(SystemExit), mock.patch("sys.stderr", io.StringIO()):
            parser().parse_args(["flash", "--device", "deadbeef", "--image-set", "/tmp/images", "--i-accept-untested"])
        with self.assertRaises(SystemExit), mock.patch("sys.stderr", io.StringIO()):
            parser().parse_args(["verify"])

    def test_read_only_cli_commands_do_not_initialize_private_store(self):
        store_path = self.tmp / "must-not-be-created"
        with mock.patch.object(cli_module.store, "ensure_store", side_effect=AssertionError("read-only command mutated store")), \
             mock.patch.object(verify_restore_module, "verify_command", return_value=0):
            self.assertEqual(cli_module.main(["--home", str(store_path), "verify", "--host", "synthetic.invalid"]), 0)
        self.assertFalse(store_path.exists())
        with mock.patch.object(cli_module.store, "ensure_store", side_effect=AssertionError("read-only command mutated store")), \
             mock.patch.object(packages_module, "verify_package", return_value={"package_type": "restore"}):
            self.assertEqual(cli_module.main(["--home", str(store_path), "package", "verify", "/tmp/fake-package"]), 0)
        self.assertFalse(store_path.exists())

    def test_cli_interrupt_guides_reconciliation_for_flash_and_restore(self):
        stderr = io.StringIO()
        with mock.patch.object(verify_restore_module, "verify_command", side_effect=KeyboardInterrupt()), \
             mock.patch("sys.stderr", stderr):
            self.assertEqual(cli_module.main(["verify", "--host", "synthetic.invalid"]), cli_module.EXIT_ERROR)
        message = stderr.getvalue()
        self.assertIn("flash --reconcile-state", message)
        self.assertIn("restore --reconcile-state", message)

    def _registered_synthetic_build(self, build_id="fixture-build", board="ufi103s", profile="prod",
                                    boot_bytes=None, rootfs_bytes=None):
        build_dir = self.home / "builds" / build_id
        build_dir.mkdir(mode=0o700)
        boot_path, rootfs_path = build_dir / "boot.img", build_dir / "rootfs.img"
        boot_path.write_bytes(synthetic_boot_image() if boot_bytes is None else boot_bytes)
        rootfs_path.write_bytes(synthetic_ext4_image() if rootfs_bytes is None else rootfs_bytes)
        manifest = {"schema_version": 1, "build_id": build_id, "board": board, "profile": profile,
                    "artifacts": {"boot.img": {"sha256": digest(boot_path), "size": boot_path.stat().st_size},
                                  "rootfs.img": {"sha256": digest(rootfs_path), "size": rootfs_path.stat().st_size}}}
        manifest_path = build_dir / "build.json"
        manifest_path.write_text(json.dumps(manifest))
        registry = store.load_registry(self.home)
        registry.setdefault("builds", []).append({"build_id": build_id, "path": str(build_dir), "board": board, "profile": profile})
        store.save_registry(self.home, registry)
        return build_dir, manifest

    def test_gpt_t_image_set_maps_exact_artifacts_and_verifies(self):
        self._registered_synthetic_build()
        layout = self.tmp / "gpt-t"; synthetic_gpt_t(layout)
        output = self.tmp / "prepared-image-set"
        args = types.SimpleNamespace(build="fixture-build", layout=str(layout), output=str(output))
        self.assertEqual(imageset_module.prepare_command(args, self.home, self.repo), 0)
        manifest = json.loads((output / "manifest.json").read_text())
        self.assertEqual(manifest["format"], "openstick-gpt-t-image-set")
        self.assertEqual(manifest["layout_id"], imageset_module.LAYOUT_ID)
        self.assertEqual(manifest["board"], "ufi103s")
        self.assertEqual(manifest["build_id"], "fixture-build")
        self.assertEqual(manifest["artifacts"]["boot.img"]["partition"], "boot")
        self.assertEqual(manifest["artifacts"]["rootfs.img"]["partition"], "userdata")
        self.assertEqual((output / "payload/boot.img").read_bytes(), (self.home / "builds/fixture-build/boot.img").read_bytes())
        self.assertEqual((output / "payload/rootfs.img").read_bytes(), (self.home / "builds/fixture-build/rootfs.img").read_bytes())
        self.assertEqual(imageset_module.verify_command(str(output)), 0)
        with self.assertRaisesRegex(store.RefusedError, "GPT-T first-install is not hardware-validated.*docs/INSTALLER.md"):
            flash_module.flash_command(types.SimpleNamespace(image_set=str(output)), self.home, self.repo)
        manifest["format"] = "unknown-mixed-format"
        (output / "manifest.json").chmod(0o600)
        (output / "manifest.json").write_text(json.dumps(manifest))
        with self.assertRaisesRegex(store.RefusedError, "unsupported image-set manifest format"):
            flash_module.flash_command(types.SimpleNamespace(image_set=str(output)), self.home, self.repo)

    def test_gpt_t_adapter_rejects_wrong_build_identity_and_unknown_layout(self):
        self._registered_synthetic_build(board="other-board")
        layout = self.tmp / "gpt-t"; synthetic_gpt_t(layout)
        args = types.SimpleNamespace(build="fixture-build", layout=str(layout), output=str(self.tmp / "bad-board-set"))
        with self.assertRaisesRegex(store.RefusedError, "board"):
            imageset_module.prepare_command(args, self.home, self.repo)
        # A GPT-B style rootfs partition or altered boot geometry is not inferred
        # as the current GPT-T image destination.
        self._registered_synthetic_build(build_id="valid-build")
        bad_layout = self.tmp / "bad-gpt"; synthetic_gpt_t(bad_layout, boot_start=429152)
        args = types.SimpleNamespace(build="valid-build", layout=str(bad_layout), output=str(self.tmp / "bad-layout-set"))
        with self.assertRaisesRegex(store.RefusedError, "partitions overlap|not the supported GPT-T"):
            imageset_module.prepare_command(args, self.home, self.repo)
        self._registered_synthetic_build(build_id="bad-profile", profile="other")
        args = types.SimpleNamespace(build="bad-profile", layout=str(layout), output=str(self.tmp / "bad-profile-set"))
        with self.assertRaisesRegex(store.RefusedError, "profile"):
            imageset_module.prepare_command(args, self.home, self.repo)

    def test_gpt_t_adapter_rejects_invalid_protective_mbr_and_zero_disk_guid(self):
        self._registered_synthetic_build()
        for name, options, message in (("bad-mbr", {"protective_start": 0}, "protective MBR"),
                                       ("zero-guid", {"disk_guid": b"\0" * 16}, "disk GUID")):
            layout = self.tmp / name; synthetic_gpt_t(layout, **options)
            args = types.SimpleNamespace(build="fixture-build", layout=str(layout), output=str(self.tmp / (name + "-set")))
            with self.assertRaisesRegex(store.RefusedError, message):
                imageset_module.prepare_command(args, self.home, self.repo)

    def test_gpt_t_adapter_rejects_bad_build_hash_boot_format_and_oversize_ext4(self):
        build, manifest = self._registered_synthetic_build(build_id="bad-hash")
        (build / "boot.img").write_bytes(b"changed")
        layout = self.tmp / "gpt-t"; synthetic_gpt_t(layout)
        args = types.SimpleNamespace(build="bad-hash", layout=str(layout), output=str(self.tmp / "bad-hash-set"))
        with self.assertRaisesRegex(store.RefusedError, "hash or size"):
            imageset_module.prepare_command(args, self.home, self.repo)
        self._registered_synthetic_build(build_id="bad-boot", boot_bytes=b"not-android")
        args = types.SimpleNamespace(build="bad-boot", layout=str(layout), output=str(self.tmp / "bad-boot-set"))
        with self.assertRaisesRegex(store.RefusedError, "not an Android boot image"):
            imageset_module.prepare_command(args, self.home, self.repo)
        oversized = bytearray(synthetic_ext4_image())
        struct.pack_into("<I", memoryview(oversized)[1024:2048], 4, imageset_module.EMMC_SECTORS)
        self._registered_synthetic_build(build_id="oversize", rootfs_bytes=bytes(oversized))
        args = types.SimpleNamespace(build="oversize", layout=str(layout), output=str(self.tmp / "oversize-set"))
        with self.assertRaisesRegex(store.RefusedError, "exceeds"):
            imageset_module.prepare_command(args, self.home, self.repo)

    def test_gpt_t_image_set_tampering_is_detected_and_flash_stops_hardware_required(self):
        self._registered_synthetic_build()
        layout = self.tmp / "gpt-t"; synthetic_gpt_t(layout)
        output = self.tmp / "prepared-image-set"
        args = types.SimpleNamespace(build="fixture-build", layout=str(layout), output=str(output))
        imageset_module.prepare_command(args, self.home, self.repo)
        (output / "payload/rootfs.img").chmod(0o600)
        with (output / "payload/rootfs.img").open("ab") as stream: stream.write(b"X")
        with self.assertRaisesRegex(store.RefusedError, "checksum mismatch"):
            imageset_module._verify_set(output)

    def test_gpt_t_image_set_rejects_file_replaced_by_symlink_after_tree_scan(self):
        self._registered_synthetic_build()
        layout = self.tmp / "gpt-t"; synthetic_gpt_t(layout)
        output = self.tmp / "prepared-race-set"
        args = types.SimpleNamespace(build="fixture-build", layout=str(layout), output=str(output))
        imageset_module.prepare_command(args, self.home, self.repo)
        real_scan = imageset_module._scan_tree_fd
        swapped = []
        def scan_then_swap(root_fd):
            result = real_scan(root_fd)
            if not swapped:
                swapped.append(True)
                payload_dir = output / "payload"
                payload_dir.chmod(0o700)
                (payload_dir / "rootfs.img").unlink()
                (payload_dir / "rootfs.img").symlink_to("/etc/passwd")
            return result
        with mock.patch.object(imageset_module, "_scan_tree_fd", side_effect=scan_then_swap):
            with self.assertRaisesRegex(store.RefusedError, "changed|symlink"):
                imageset_module._verify_set(output)

    def test_gpt_t_image_set_rejects_symlink_directory(self):
        self._registered_synthetic_build()
        layout = self.tmp / "gpt-t"; synthetic_gpt_t(layout)
        output = self.tmp / "prepared-link-set"
        args = types.SimpleNamespace(build="fixture-build", layout=str(layout), output=str(output))
        imageset_module.prepare_command(args, self.home, self.repo)
        output.chmod(0o700)
        (output / "foreign").symlink_to(self.tmp, target_is_directory=True)
        with self.assertRaisesRegex(store.RefusedError, "symlink"):
            imageset_module._verify_set(output)

    def test_gpt_t_image_set_publication_never_replaces_concurrent_empty_directory(self):
        self._registered_synthetic_build()
        layout = self.tmp / "gpt-t"; synthetic_gpt_t(layout)
        output = self.tmp / "publication-race"
        args = types.SimpleNamespace(build="fixture-build", layout=str(layout), output=str(output))
        real_publish = imageset_module._rename_noreplace
        def create_destination_before_publish(source, destination):
            pathlib.Path(destination).mkdir(mode=0o700)
            return real_publish(source, destination)
        with mock.patch.object(imageset_module, "_rename_noreplace", side_effect=create_destination_before_publish):
            with self.assertRaisesRegex(store.RefusedError, "appeared during preparation"):
                imageset_module.prepare_command(args, self.home, self.repo)
        self.assertTrue(output.is_dir())
        self.assertEqual(list(output.iterdir()), [])
        self.assertFalse(list(self.tmp.glob(".openstick-image-set-*")))

    def test_legacy_flash_refuses_mixed_gpt_t_files_without_manifest(self):
        image_set = self.tmp / "mixed-image-set"
        image_set.mkdir()
        (image_set / "boot.img").write_bytes(synthetic_boot_image())
        with self.assertRaisesRegex(store.RefusedError, "mixed or unknown image-set layout"):
            flash_module.flash_command(types.SimpleNamespace(image_set=str(image_set)), self.home, self.repo)

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
        generic_out = self.tmp / "generic-out"
        generic_out.mkdir()
        base_env = {"OPENSTICK_ROOT": str(self.repo), "BOARD": "ufi103s", "PATH": "/usr/bin:/bin"}
        generic = subprocess.run(cmd, env=dict(base_env, OPENSTICK_CONFIG_MODE="generic",
                                              OPENSTICK_OUT=str(generic_out)), text=True, capture_output=True, check=True)
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
        fakebin = self.tmp / "fake-edl-bin"; fakebin.mkdir(exist_ok=True)
        loader_name = flash_module.edl_runner.LOADER_NAME
        loader_fixture = fakebin / loader_name
        import shutil
        shutil.copyfile(image / "loader/prog_emmc_firehose_8916.mbn", loader_fixture)
        flash_module.edl_runner.PINNED_LOADER_SHA256 = digest(loader_fixture)
        byte_log = self.tmp / "edl-read-bytes.log"
        exec_log = self.tmp / "edl-executions.log"
        edl_wrapper = fakebin / "edl"
        edl_wrapper.write_text("#!/bin/sh\nprintf '%%s %%s\\n' \"$0\" \"$*\" >> %s\nOPENSTICK_FAKE_EMMC=%s OPENSTICK_EDL_BYTES_FILE=%s OPENSTICK_EDL_CALL_FILE=%s exec %s \"$@\"\n" %
                               (exec_log, disk, byte_log, self.tmp / "edl-calls", ROOT / "tests/shims/bin/edl"))
        edl_wrapper.chmod(0o700)
        env = dict(os.environ, PATH=str(fakebin) + ":" + str(ROOT / "tests/shims/bin") + ":/usr/bin:/bin",
                   OPENSTICK_EDL_DIR=str(fakebin))
        args.fake_disk = str(disk)
        args.edl_bytes_file = str(byte_log)
        args.edl_exec_log = str(exec_log)
        args.edl_dir = str(fakebin)
        return args, dev, identity, env

    def _install_fake_flash_engines(self):
        patha = self.repo / "tools/pathA/pathA-flash.sh"
        patha.write_text("#!/bin/sh\n$EDL_CMD ws 0 /dev/null --sectorsize=512 --skipwrite --loader=\"$LDR\" --memory=emmc >/dev/null\n")
        patha.chmod(0o700)
        fastboot = self.repo / "tools/flash/flash-board.sh"
        fastboot.write_text("#!/bin/sh\n$EDL_CMD ws 0 /dev/null --sectorsize=512 --skipwrite --loader=\"$LDR\" --memory=emmc >/dev/null\n")
        fastboot.chmod(0o700)

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
        with mock.patch.dict(os.environ, env, clear=True), self.assertRaisesRegex(store.RefusedError, "only ufi103s.*always refused.*--i-accept-untested does not exist"):
            flash_module.flash_command(args, self.home, self.repo)
        with mock.patch.dict(os.environ, env, clear=True), self.assertRaisesRegex(store.RefusedError, "only ufi103s.*always refused.*--i-accept-untested does not exist"):
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
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(store.RefusedError, "identity fingerprints"):
                flash_module.flash_command(args, self.home, self.repo)

    def test_flash_identity_check_reads_only_backup_identity_ranges(self):
        args, dev, identity, env = self._flash_fixture()
        self._install_fake_flash_engines()
        byte_log = pathlib.Path(args.edl_bytes_file)
        env["OPENSTICK_EDL_DIR"] = args.edl_dir
        from contextlib import redirect_stdout
        captured = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(flash_module.common, "LOADER_SHA256", digest(pathlib.Path(args.image_set) / "loader/prog_emmc_firehose_8916.mbn")), redirect_stdout(captured):
            self.assertEqual(flash_module.flash_command(args, self.home, self.repo), 0)
        rows = byte_log.read_text().splitlines()
        reads = [row for row in rows if row.startswith("rs ")]
        self.assertEqual(len(reads), 4, rows)
        self.assertTrue(all(row.startswith("rs ") for row in reads), rows)
        self.assertFalse(any(row.startswith("rf ") for row in rows), rows)
        total = sum(int(row.rsplit(" ", 1)[-1]) for row in reads)
        self.assertLessEqual(total, 2 * 1024 * 1024)
        manifest = json.loads((self.home / "backups/deadbeef/fake-backup/manifest.json").read_text())
        expected = ["rs %d %d %d" % (manifest["partitions"][name]["start_lba"],
                                      manifest["partitions"][name]["end_lba"] - manifest["partitions"][name]["start_lba"] + 1,
                                      (manifest["partitions"][name]["end_lba"] - manifest["partitions"][name]["start_lba"] + 1) * 512)
                    for name in flash_module.IDENTITY_PARTS]
        self.assertEqual(reads[1:], expected)
        self.assertEqual(reads[0], "rs 0 34 17408")

    def test_flash_uses_one_edl_command_for_identity_and_engine(self):
        args, dev, identity, env = self._flash_fixture()
        self._install_fake_flash_engines()
        env["OPENSTICK_EDL_DIR"] = args.edl_dir
        edl = str(pathlib.Path(args.edl_dir) / "edl")
        from contextlib import redirect_stdout
        captured = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(flash_module.common, "LOADER_SHA256", digest(pathlib.Path(args.image_set) / "loader/prog_emmc_firehose_8916.mbn")), \
             redirect_stdout(captured):
            self.assertEqual(flash_module.flash_command(args, self.home, self.repo), 0)
        calls = pathlib.Path(args.edl_exec_log).read_text().splitlines()
        self.assertTrue(any(" ws " in line for line in calls), calls)
        self.assertTrue(all("--loader=" in line and "--memory=emmc" in line for line in calls), calls)
        self.assertTrue(any(" rs " in line for line in calls), calls)
        self.assertTrue(all(line.startswith(edl + " ") for line in calls), calls)
        self.assertIn("EDL command: " + edl, captured.getvalue())

    def test_flash_dry_confirmation_protocol_and_failure_stop(self):
        args, dev, identity, env = self._flash_fixture()
        fake_loader = pathlib.Path(args.image_set) / "loader/prog_emmc_firehose_8916.mbn"
        commands = []
        def invoke(command, phase_env):
            commands.append((pathlib.Path(command[0]).name, command[-2], command[-1], phase_env.get("COMMIT"), phase_env.get("CONFIRM")))
            if command[-1] == "write" and command[-2] == "gpt": return 47
            return 0
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(flash_module.common, "LOADER_SHA256", digest(fake_loader)), \
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
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(flash_module.common, "LOADER_SHA256", digest(fake_loader)), \
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
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(flash_module.common, "LOADER_SHA256", digest(fake_loader)), \
             mock.patch.object(flash_module, "_invoke", side_effect=lambda c, e: calls.append((c[-1], e.get("COMMIT"))) or 0), \
             mock.patch.object(flash_module.sys, "stdin", mock.Mock(isatty=lambda: False)), mock.patch.object(flash_module.sys, "stdout", mock.Mock(isatty=lambda: False)):
            args.dry_run = False
            self.assertEqual(flash_module.flash_command(args, self.home, self.repo), 0)
        self.assertTrue(calls)
        self.assertFalse(any(mode == "write" or commit == "1" for mode, commit in calls))

        args, dev, identity, env = self._flash_fixture()
        fake_loader = pathlib.Path(args.image_set) / "loader/prog_emmc_firehose_8916.mbn"
        calls = []
        with mock.patch.dict(os.environ, env, clear=True), \
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
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(flash_module.common, "LOADER_SHA256", digest(fake_loader)), mock.patch.object(flash_module, "_invoke", side_effect=invoke), \
             mock.patch.object(flash_module.sys, "stdin", FakeTTY()), mock.patch.object(flash_module.sys, "stdout", FakeTTY()), \
             mock.patch("builtins.input", side_effect=confirmations):
            args.dry_run = False
            with self.assertRaisesRegex(store.RefusedError, "stopped after GPT"):
                flash_module.flash_command(args, self.home, self.repo)
        self.assertEqual([phase for phase, mode, _, _ in calls if mode == "write"], ["gpt"])
        self.assertEqual([phase for phase, mode, _, _ in calls if mode == "dry"], ["pre", "gpt"])
        writes = [(phase, confirm) for phase, mode, commit, confirm in calls if mode == "write"]
        self.assertEqual(writes, [("gpt", "da-flashez-gpt")])
        self.assertTrue(all(commit == "1" for _, mode, commit, _ in calls if mode == "write"))
        self.assertFalse(any(commit == "1" for phase, mode, commit, _ in calls if phase == "final"))
        self.assertEqual(store.load_registry(self.home)["devices"][0]["flashes"][0]["phases"],
                         [{"name": "gpt", "result": "engine-reported-pass", "verified": True}])

    def test_interrupted_write_requires_reconciliation_and_is_not_retried(self):
        args, dev, identity, env = self._flash_fixture()
        fake_loader = pathlib.Path(args.image_set) / "loader/prog_emmc_firehose_8916.mbn"
        calls = []
        def interrupt(command, phase_env):
            calls.append((command[-2], command[-1]))
            if command[-1] == "write": raise KeyboardInterrupt()
            return 0
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(flash_module.common, "LOADER_SHA256", digest(fake_loader)), mock.patch.object(flash_module, "_invoke", side_effect=interrupt), \
             mock.patch.object(flash_module.sys, "stdin", FakeTTY()), mock.patch.object(flash_module.sys, "stdout", FakeTTY()), \
             mock.patch("builtins.input", return_value="flash deadbeef gpt"):
            args.dry_run = False
            with self.assertRaises(KeyboardInterrupt): flash_module.flash_command(args, self.home, self.repo)
        state = json.loads((self.home / "devices/deadbeef/flash-state.json").read_text())
        self.assertEqual(state["status"], "interrupted")
        before = len(calls)
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(flash_module.common, "LOADER_SHA256", digest(fake_loader)), mock.patch.object(flash_module, "_invoke", side_effect=interrupt):
            with self.assertRaisesRegex(store.RefusedError, "unresolved"):
                flash_module.flash_command(args, self.home, self.repo)
        self.assertEqual(len(calls), before)
        device = store.load_registry(self.home)["devices"][0]
        self.assertEqual(device["flashes"][0]["outcome"], "partial-failure-reconciliation-required")
        args.reconcile_state = True
        with mock.patch.dict(os.environ, env, clear=True), \
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
