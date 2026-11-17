#!/usr/bin/env python3
"""Synthetic M5 tests. No device, network, root, or external artifacts."""
import hashlib
import io
import json
import os
import pathlib
import shlex
import struct
import subprocess
import sys
import tempfile
import types
import unittest
import zlib
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.installer import store, verify_restore as m5


NAMES = ["modem", "sbl1", "sbl1bak", "aboot", "abootbak", "rpm", "rpmbak", "tz", "tzbak", "hyp", "hypbak", "pad", "modemst1", "modemst2", "misc", "fsc", "ssd", "splash", "DDR", "fsg", "sec", "boot", "system", "persist", "cache", "recovery", "userdata"]


class FakeTTY(io.StringIO):
    def isatty(self):
        return True


def make_disk(path):
    sectors = 1000
    entries = bytearray(128 * 128)
    parts = {}
    for i, name in enumerate(NAMES):
        start, end = 64 + i * 8, 67 + i * 8
        parts[name] = (start, end)
        e = memoryview(entries)[i * 128:(i + 1) * 128]
        e[:16] = bytes([i + 1]) * 16
        e[16:32] = bytes([i + 2]) * 16
        struct.pack_into("<QQ", e, 32, start, end)
        encoded = name.encode("utf-16le")
        e[56:56 + len(encoded)] = encoded
    entries_crc = zlib.crc32(entries) & 0xffffffff

    def header(current, alternate, entry_lba):
        h = bytearray(512); h[:8] = b"EFI PART"
        struct.pack_into("<IIII", h, 8, 0x00010000, 92, 0, 0)
        struct.pack_into("<QQQQ", h, 24, current, alternate, 34, sectors - 34)
        struct.pack_into("<QIII", h, 72, entry_lba, 128, 128, entries_crc)
        checksum = bytearray(h[:92]); struct.pack_into("<I", checksum, 16, 0)
        struct.pack_into("<I", h, 16, zlib.crc32(checksum) & 0xffffffff)
        return bytes(h)

    disk = bytearray(os.urandom(sectors * 512))
    disk[512:1024] = header(1, sectors - 1, 2)
    disk[1024:1024 + len(entries)] = entries
    disk[(sectors - 33) * 512:(sectors - 1) * 512] = entries
    disk[(sectors - 1) * 512:] = header(sectors - 1, 1, sectors - 33)
    path.write_bytes(disk)
    return parts


def seed_fake_session_identity(session):
    """Represent identity already captured by a fake `_live` implementation."""
    if session is not None:
        session.identity = {"serial": "deadbeef", "hwid": "007050e100000000", "pk_hash": "0" * 64}


class M5Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="openstick-m5-"))
        self._old_git_ceiling = os.environ.get("GIT_CEILING_DIRECTORIES")
        os.environ["GIT_CEILING_DIRECTORIES"] = str(self.tmp.parent)
        self.home = store.ensure_store(self.tmp / "private")
        self.disk = self.tmp / "fake-emmc.bin"
        self.parts = make_disk(self.disk)
        self.sysfs = self.tmp / "sysfs"
        device = self.sysfs / "1-1"
        device.mkdir(parents=True)
        for name, value in (("idVendor", "05c6"), ("idProduct", "9008"),
                            ("busnum", "001"), ("devnum", "009")):
            (device / name).write_text(value + "\n", encoding="ascii")
        self.identity = {n: hashlib.sha256(self.disk.read_bytes()[s * 512:(e + 1) * 512]).hexdigest()
                         for n, (s, e) in ((n, self.parts[n]) for n in m5.IDENTITY)}
        self.device = {"id": "deadbeef", "board": "ufi103s", "identity": self.identity,
                       "backups": [], "flashes": []}
        backup = self.home / "backups/deadbeef/synthetic"
        backup.mkdir(parents=True, mode=0o700)
        import shutil
        shutil.copyfile(self.disk, backup / "emmc-full.bin")
        manifest_parts = {n: {"start_lba": s, "end_lba": e} for n, (s, e) in self.parts.items()}
        self.digest = hashlib.sha256(self.disk.read_bytes()).hexdigest()
        manifest = {"schema_version": 1, "id": "synthetic", "device_id": "deadbeef", "size": self.disk.stat().st_size,
                    "sectors": self.disk.stat().st_size // 512, "sha256": self.digest, "identity": self.identity,
                    "partitions": manifest_parts, "verified_by": ["full-read-size", "gpt-primary-and-backup-crc", "sha256"]}
        (backup / "manifest.json").write_text(json.dumps(manifest))
        (backup / "SHA256SUMS").write_text(self.digest + "  emmc-full.bin\n")
        for member in backup.iterdir(): member.chmod(0o600)
        self.device["backups"].append({"id": "synthetic", "path": str(backup), "sha256": self.digest,
                                       "verified_by": manifest["verified_by"]})
        store.save_registry(self.home, {"schema": 1, "devices": [self.device]})
        self.backup = backup
        self.loader = self.tmp / "loader.mbn"; self.loader.write_bytes(b"synthetic loader")
        self.fakebin = self.tmp / "fake-bin"; self.fakebin.mkdir()
        self.write_log = self.tmp / "fake-edl-writes.log"
        self.call_log = self.tmp / "fake-edl-calls.log"
        self.fail_control = self.tmp / "fake-edl-fail.txt"
        self.fake_edl_path = self.fakebin / "edl"
        self.fake_edl_path.write_text(self._fake_edl_script())
        self.fake_edl_path.chmod(0o700)
        self.fake_loader_path = self.fakebin / m5.edl_runner.LOADER_NAME
        import shutil
        shutil.copyfile(self.loader, self.fake_loader_path)
        m5.edl_runner.PINNED_LOADER_SHA256 = hashlib.sha256(self.fake_loader_path.read_bytes()).hexdigest()
        self.fake_sha_path = self.fakebin / "sha256sum"
        self.fake_sha_path.write_text(self._fake_sha_script())
        self.fake_sha_path.chmod(0o700)
        self.env = dict(os.environ, OPENSTICK_FAKE_EMMC=str(self.disk),
                        OPENSTICK_EDL_DIR=str(self.fakebin),
                        PATH=str(self.fakebin) + ":/usr/bin:/bin")
        self._register_patha_fixture()

    def _fake_edl_script(self):
        template = r'''#!/bin/bash
set -eu
op=${1:-}
loader= memory=
for arg in "$@"; do
  case "$arg" in --loader=*) loader=${arg#--loader=};; --memory=*) memory=${arg#--memory=};; esac
done
if [[ -z $loader || $loader != /* || $memory != emmc ]]; then echo 'FAKE EDL ASSERTION FAILED: required loader and memory flags missing' >&2; exit 91; fi
printf 'Qualcomm Sahara / Firehose Client V3.62 (c) B.Kerler 2018-2025.\n'
printf 'main - Mode detected: sahara\nsahara -\n\nVersion 0x2\n------------------------\n'
printf 'HWID:              0x007050e100000000 (MSM_ID:0x007050e1,OEM_ID:0x0000,MODEL_ID:0x0000)\n'
printf 'CPU detected:      "MSM8916"\nPK_HASH:           0x0000000000000000000000000000000000000000000000000000000000000000\nSerial:            0xdeadbeef\n\n'
case "$op" in
  ws)
    if [[ " $* " == *' --skipwrite '* ]]; then printf 'ws-dry\n' >> __CALLS__; exit 0; fi
    printf 'ws-write\n' >> __CALLS__
    lba=$2; src=$3
    printf '%s %s %s %s\n' "$op" "$lba" "${COMMIT:-}" "${CONFIRM:-}" >> __LOG__
    if [[ -f __FAIL__ ]]; then
      read -r fail_lba fail_mode < __FAIL__
      if [[ "$lba" == "$fail_lba" ]]; then
        rm -f __FAIL__
        if [[ "$fail_mode" == partial ]]; then dd if="$src" of=__DISK__ bs=512 seek="$lba" count=1 conv=notrunc status=none; fi
        if [[ "$fail_mode" == complete ]]; then dd if="$src" of=__DISK__ bs=512 seek="$lba" conv=notrunc status=none; fi
        exit 90
      fi
    fi
    dd if="$src" of=__DISK__ bs=512 seek="$lba" conv=notrunc status=none
    ;;
  rs)
    printf 'rs\n' >> __CALLS__
    start=$(( $2 * 512 )); count=$(( $3 * 512 )); out=$4
    dd if=__DISK__ of="$out" iflag=skip_bytes,count_bytes skip="$start" count="$count" status=none
    ;;
  rf) printf 'rf\n' >> __CALLS__; cp -- __DISK__ "$2";;
  getstorageinfo) printf 'getstorageinfo\n' >> __CALLS__; echo "GetStorageInfo command isn't supported.";;
  *) exit 2;;
esac
'''
        return (template.replace("__LOG__", shlex.quote(str(self.write_log)))
                .replace("__CALLS__", shlex.quote(str(self.call_log)))
                .replace("__FAIL__", shlex.quote(str(self.fail_control)))
                .replace("__DISK__", shlex.quote(str(self.disk))))

    def _fake_sha_script(self):
        template = r'''#!/bin/bash
set -eu
for arg in "$@"; do
  if [[ "$arg" == __LOADER__ ]]; then printf '959439aa5864685999b713c3ed12ad5fa408149648b670a9a9ef77bcc9dcab14  %%s\n' "$arg"; exit 0; fi
done
exec /usr/bin/sha256sum "$@"
'''
        return template.replace("__LOADER__", shlex.quote(str(self.fake_loader_path)))

    def _register_patha_fixture(self):
        """Create fully synthetic rollback provenance matching the fake board."""
        import shutil
        image = self.tmp / "registered-image-set"
        patha = image / "pathA"
        for group in ("gpt", "fw", "rollback"):
            (patha / group).mkdir(parents=True, exist_ok=True)
        dump = self.backup / "emmc-full.bin"
        raw = dump.read_bytes()
        sectors = len(raw) // 512
        (patha / "gpt/gpt-T-primary.bin").write_bytes(raw[:34 * 512])
        (patha / "gpt/gpt-T-secondary.bin").write_bytes(raw[(sectors - 33) * 512:])
        (patha / "rollback/gpt-orig-primary.bin").write_bytes(raw[:34 * 512])
        (patha / "rollback/gpt-orig-secondary.bin").write_bytes(raw[(sectors - 33) * 512:])
        for name, part in (("sbl1", "sbl1"), ("rpm", "rpm"), ("tz", "tz"), ("hyp", "hyp"), ("aboot", "aboot")):
            start, end = self.parts[part]
            payload = raw[start * 512:(end + 1) * 512]
            file_name = "tz.img" if name == "tz" else name + ".img"
            (patha / "fw" / file_name).write_bytes(payload)
            stock_name = "stock-tz-tzbak.bin" if name == "tz" else "stock-" + name + ".bin"
            (patha / "rollback" / stock_name).write_bytes(payload)
        rows = []
        for p in sorted(patha.rglob("*")):
            if p.is_file() and p.name != "SHA256SUMS":
                rows.append(hashlib.sha256(p.read_bytes()).hexdigest() + "  " + p.relative_to(patha).as_posix())
        (patha / "SHA256SUMS").write_text("\n".join(rows) + "\n")
        image.mkdir(exist_ok=True)
        all_rows = []
        for p in sorted(image.rglob("*")):
            if p.is_file() and p != image / "SHA256SUMS":
                all_rows.append(hashlib.sha256(p.read_bytes()).hexdigest() + "  " + p.relative_to(image).as_posix())
        (image / "SHA256SUMS").write_text("\n".join(all_rows) + "\n")
        self.device["flashes"].append({"image_set": str(image), "outcome": "engine-reported-pass-not-boot-verified"})
        store.save_registry(self.home, {"schema": 1, "devices": [self.device]})
        self.image_set = image

    def fake_edl(self, edl, args, env):
        op = args[0]
        with self.disk.open("r+b") as device:
            if op == "ws":
                if "--skipwrite" in args:
                    return
                lba = int(args[1]); payload = pathlib.Path(args[2]).read_bytes()
                device.seek(lba * 512); device.write(payload); device.flush()
            elif op == "rs":
                lba, count = int(args[1]), int(args[2]); target = pathlib.Path(args[3])
                device.seek(lba * 512); data = device.read(count * 512)
                target.write_bytes(data)
            elif op == "rf":
                target = pathlib.Path(args[1]); target.write_bytes(self.disk.read_bytes())
            else:
                raise AssertionError("unexpected fake EDL op " + op)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)
        if self._old_git_ceiling is None:
            os.environ.pop("GIT_CEILING_DIRECTORIES", None)
        else:
            os.environ["GIT_CEILING_DIRECTORIES"] = self._old_git_ceiling

    def _saved_restore_state(self, **overrides):
        data = {"schema": 1, "device_id": "deadbeef", "backup_id": "synthetic",
                "backup_sha256": self.digest, "phase": "userdata", "status": "phase-complete",
                "completed_phases": ["pad", "userdata"]}
        data.update(overrides)
        path = self.home / "devices/deadbeef/restore-state.json"
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text(json.dumps(data))
        path.chmod(0o600)
        return path

    def _assert_tampered_state_refused_without_writes(self, **overrides):
        self._saved_restore_state(**overrides)
        args = types.SimpleNamespace(restore_device="deadbeef", restore_backup="synthetic", dry_run=False,
                                     restore_reconcile_state=False)
        with self.assertRaises(store.RefusedError):
            m5.restore_command(args, self.home, ROOT)
        self.assertFalse(self.write_log.exists())

    def test_resume_rejects_state_for_wrong_device(self):
        self._assert_tampered_state_refused_without_writes(device_id="cafebabe")

    def test_resume_rejects_state_for_wrong_backup(self):
        self._assert_tampered_state_refused_without_writes(backup_id="other-synthetic")

    def test_resume_rejects_state_with_wrong_backup_sha(self):
        self._assert_tampered_state_refused_without_writes(backup_sha256="0" * 64)

    def test_resume_rejects_nonprefix_completed_phase_order(self):
        self._assert_tampered_state_refused_without_writes(completed_phases=["pad", "cache"], phase="cache")

    def test_resume_rejects_phase_status_history_inconsistency(self):
        self._assert_tampered_state_refused_without_writes(phase="cache")

    def test_resume_rejects_completed_prefix_when_live_phase_bytes_differ(self):
        self._saved_restore_state()
        with self.disk.open("r+b") as f:
            f.seek(self.parts["userdata"][0] * 512 + 17); f.write(b"changed")
        fake_loader = self.tmp / "loader.mbn"; fake_loader.write_bytes(b"synthetic loader")
        fake_live = lambda edl, env, loader, directory, ident, session=None: (
            seed_fake_session_identity(session) or ("deadbeef", self.identity, self.disk, self.parts))
        args = types.SimpleNamespace(restore_device="deadbeef", restore_backup="synthetic", dry_run=False,
                                     restore_reconcile_state=False)
        with mock.patch.dict(os.environ, self.env, clear=True), \
             mock.patch.object(m5, "_live", side_effect=fake_live), \
             mock.patch.object(m5.shutil, "which", return_value=str(self.fake_edl_path)), \
             mock.patch.object(m5.common, "_loader_candidate", return_value=fake_loader), \
             mock.patch.object(m5.common, "LOADER_SHA256", hashlib.sha256(fake_loader.read_bytes()).hexdigest()):
            with self.assertRaisesRegex(store.RefusedError, "live bytes differ"):
                m5.restore_command(args, self.home, ROOT)
        self.assertFalse(self.write_log.exists())

    def test_resume_full_live_read_and_identity_reads_precede_first_write(self):
        self._saved_restore_state()
        fake_loader = self.tmp / "loader.mbn"; fake_loader.write_bytes(b"synthetic loader")
        args = types.SimpleNamespace(restore_device="deadbeef", restore_backup="synthetic", dry_run=False,
                                     restore_reconcile_state=False)
        with mock.patch.dict(os.environ, self.env, clear=True), \
             mock.patch.object(m5.shutil, "which", return_value=str(self.fake_edl_path)), \
             mock.patch.object(m5.common, "_loader_candidate", return_value=fake_loader), \
             mock.patch.object(m5.common, "LOADER_SHA256", hashlib.sha256(fake_loader.read_bytes()).hexdigest()), \
             mock.patch.object(m5, "_fwlib_path", return_value=str(self.fakebin) + ":/usr/bin:/bin"), \
             mock.patch.object(m5, "_confirm", side_effect=lambda phase, ident: "restore %s %s" % (ident[-8:], phase)):
            self.assertEqual(m5.restore_command(args, self.home, ROOT), 0)
        calls = self.call_log.read_text().splitlines()
        self.assertEqual(calls[:2], ["rs", "rf"])
        first_write = calls.index("ws-write")
        self.assertIn("ws-dry", calls[:first_write])
        self.assertGreaterEqual(calls[:first_write].count("rs"), 3)
        self.assertEqual(calls[:first_write].count("getstorageinfo"), 0)
        self.assertEqual(self.disk.read_bytes(), (self.backup / "emmc-full.bin").read_bytes())

    def test_resume_full_live_read_rejects_completed_range_mismatch_before_write(self):
        self._saved_restore_state()
        with self.disk.open("r+b") as f:
            f.seek(self.parts["userdata"][0] * 512 + 17); f.write(b"changed")
        fake_loader = self.tmp / "loader.mbn"; fake_loader.write_bytes(b"synthetic loader")
        args = types.SimpleNamespace(restore_device="deadbeef", restore_backup="synthetic", dry_run=False,
                                     restore_reconcile_state=False)
        with mock.patch.dict(os.environ, self.env, clear=True), \
             mock.patch.object(m5.shutil, "which", return_value=str(self.fake_edl_path)), \
             mock.patch.object(m5.common, "_loader_candidate", return_value=fake_loader), \
             mock.patch.object(m5.common, "LOADER_SHA256", hashlib.sha256(fake_loader.read_bytes()).hexdigest()), \
             mock.patch.object(m5, "_fwlib_path", return_value=str(self.fakebin) + ":/usr/bin:/bin"):
            with self.assertRaisesRegex(store.RefusedError, "live bytes differ"):
                m5.restore_command(args, self.home, ROOT)
        self.assertEqual(self.call_log.read_text().splitlines(), ["rs", "rf"])
        self.assertFalse(self.write_log.exists())

    def test_verified_planner_accepts_synthetic_gpt_geometry(self):
        item, folder, manifest, parts = m5._backup(self.home, self.device, "synthetic")
        plan = m5._planner(ROOT, folder, manifest, parts, self.tmp / "planner")
        self.assertIn("PHASE 0b", plan)
        self.assertIn("PHASE 2", plan)
        self.assertEqual(len(__import__("re").findall(r"^\$EDL ws ", plan, __import__("re").M)), 30)
        self.assertFalse((folder / "planner-primary.bin").exists())

    def test_backup_tamper_or_identity_change_refused(self):
        dump = self.backup / "emmc-full.bin"
        with dump.open("r+b") as f:
            f.seek(1200); f.write(b"X")
        with self.assertRaises(store.RefusedError):
            m5._backup(self.home, self.device, "synthetic")

    def test_restore_dry_run_is_read_only_and_hostile_host_refused(self):
        args = types.SimpleNamespace(restore_device="deadbeef", restore_backup="synthetic", dry_run=True)
        live_parts = self.parts
        def fake_live(edl, env, loader, directory, device_id, session=None):
            seed_fake_session_identity(session)
            copy = pathlib.Path(directory) / "fake-live.bin"
            copy.write_bytes(self.disk.read_bytes())
            return "deadbeef", self.identity, copy, live_parts
        fake_loader = self.tmp / "loader.mbn"; fake_loader.write_bytes(b"synthetic loader")
        with mock.patch.dict(os.environ, self.env, clear=True), mock.patch.object(m5, "_live", side_effect=fake_live), \
             mock.patch.object(m5.common, "_loader_candidate", return_value=fake_loader), \
             mock.patch.object(m5.common, "LOADER_SHA256", hashlib.sha256(fake_loader.read_bytes()).hexdigest()):
            self.assertEqual(m5.restore_command(args, self.home, ROOT), 0)
        self.assertFalse((self.home / "devices/deadbeef/restore-state.json").exists())
        with self.assertRaises(store.RefusedError):
            m5._safe_host("user@host;touch /tmp/nope")

    def test_restore_rejects_unpinned_loader_and_live_identity_mismatch(self):
        args = types.SimpleNamespace(restore_device="deadbeef", restore_backup="synthetic", dry_run=True)
        fake_loader = self.tmp / "loader.mbn"; fake_loader.write_bytes(b"synthetic loader")
        fake_live = lambda edl, env, loader, directory, ident, session=None: (
            seed_fake_session_identity(session) or ("deadbeef", self.identity, self.disk, self.parts))
        with mock.patch.dict(os.environ, self.env, clear=True), \
             mock.patch.object(m5, "_live", side_effect=fake_live), \
             mock.patch.object(m5.edl_runner, "PINNED_LOADER_SHA256", "0" * 64):
            with self.assertRaisesRegex(store.RefusedError, "pinned Firehose loader"):
                m5.restore_command(args, self.home, ROOT)
        wrong = dict(self.identity); wrong["fsg"] = "0" * 64
        fake_live_wrong = lambda edl, env, loader, directory, ident, session=None: (
            seed_fake_session_identity(session) or ("deadbeef", wrong, self.disk, self.parts))
        with mock.patch.dict(os.environ, self.env, clear=True), \
             mock.patch.object(m5, "_live", side_effect=fake_live_wrong), \
             mock.patch.object(m5.shutil, "which", return_value=str(self.fake_edl_path)), \
             mock.patch.object(m5.common, "_loader_candidate", return_value=fake_loader), \
             mock.patch.object(m5.common, "LOADER_SHA256", hashlib.sha256(fake_loader.read_bytes()).hexdigest()):
            with self.assertRaisesRegex(store.RefusedError, "fsg/sec/fsc"):
                m5.restore_command(args, self.home, ROOT)
        self.assertFalse((self.home / "devices/deadbeef/restore-state.json").exists())

    def test_wrong_typed_confirmation_and_fwlib_test_override_do_not_write(self):
        with self.disk.open("r+b") as f:
            f.seek(self.parts["userdata"][0] * 512 + 17); f.write(b"changed")
        fake_live = lambda edl, env, loader, directory, ident, session=None: (
            seed_fake_session_identity(session) or ("deadbeef", self.identity, self.disk, self.parts))
        args = types.SimpleNamespace(restore_device="deadbeef", restore_backup="synthetic", dry_run=False)
        with mock.patch.dict(os.environ, dict(self.env, FWLIB_TEST_LDR_SHA="0" * 64), clear=True), \
             mock.patch.object(m5, "_live", side_effect=fake_live), \
             mock.patch.object(m5.shutil, "which", return_value=str(self.fake_edl_path)), \
             mock.patch.object(m5.common, "_loader_candidate", return_value=self.loader), \
             mock.patch.object(m5.common, "LOADER_SHA256", hashlib.sha256(self.loader.read_bytes()).hexdigest()), \
             mock.patch.object(m5, "_fwlib_path", return_value=str(self.fakebin) + ":/usr/bin:/bin"), \
             mock.patch.object(m5, "_live_identity_only"), \
             mock.patch.object(m5.sys, "stdin", FakeTTY()), mock.patch.object(m5.sys, "stdout", FakeTTY()), \
             mock.patch("builtins.input", return_value="wrong confirmation"):
            with self.assertRaisesRegex(store.RefusedError, "confirmation"):
                m5.restore_command(args, self.home, ROOT)
        self.assertFalse(self.write_log.exists())
        self.assertFalse((self.home / "devices/deadbeef/restore-state.json").exists())

    def test_restore_confirmation_requires_interactive_terminal(self):
        with mock.patch.object(m5.sys, "stdin", io.StringIO("restore deadbeef gpt\n")), \
             mock.patch.object(m5.sys, "stdout", io.StringIO()):
            with self.assertRaisesRegex(store.RefusedError, "non-interactive restore is read-only"):
                m5._confirm("gpt", "deadbeef")

    def test_restore_confirmation_does_not_strip_typed_text(self):
        with mock.patch.object(m5.sys, "stdin", FakeTTY()), mock.patch.object(m5.sys, "stdout", FakeTTY()), \
             mock.patch("builtins.input", return_value=" restore deadbeef gpt "):
            with self.assertRaisesRegex(store.RefusedError, "confirmation did not match"):
                m5._confirm("gpt", "deadbeef")

    def test_restore_final_read_checks_size_even_when_hash_matches(self):
        target_size = self.disk.stat().st_size
        manifest = {"size": target_size + 512, "sha256": hashlib.sha256(self.disk.read_bytes()).hexdigest()}
        state = {}
        with self.assertRaises(ValueError):
            m5._verify_final_disk(str(self.fake_edl_path), self.env, self.loader, self.tmp, manifest, state)
        self.assertEqual(state["status"], "final-readback-failed")

    def test_fwlib_refuses_shell_syntax_in_edl_path(self):
        engine = m5._make_fwlib_engine(ROOT, self.tmp / "hostile-engine")
        source = self.tmp / "safe-source.bin"; source.write_bytes(b"x" * 512)
        prepared = {"phase": "pad", "start": 0, "end": 0, "source": source}
        with self.assertRaisesRegex(store.RefusedError, "EDL client path"):
            m5._run_fwlib_phase(engine, "/tmp/edl;touch", {}, self.loader, prepared, self.tmp, write=False)

    def test_verify_reports_each_check_and_does_not_claim_absent_services(self):
        stdout = "".join("%s\t%s\tevidence\n" % ("SKIP" if key in ("modem", "wifi") else "PASS", key)
                          for key in sorted({"firewall_state", "firewall_syntax", "watchdog", "modem", "ntp", "wifi", "versions"}))
        proc = types.SimpleNamespace(returncode=0, stdout=stdout, stderr="")
        args = types.SimpleNamespace(host="user@stick.local")
        with mock.patch.object(m5.shutil, "which", return_value="/usr/bin/ssh"), mock.patch.object(m5.subprocess, "run", return_value=proc) as run:
            self.assertEqual(m5.verify_command(args, self.home, ROOT), 0)
            self.assertIn("StrictHostKeyChecking=yes", run.call_args.args[0])
            self.assertEqual(run.call_args.kwargs["input"], m5.REMOTE_CHECKS)

    def test_malformed_or_missing_ssh_evidence_fails(self):
        args = types.SimpleNamespace(host="user@stick.local")
        with mock.patch.object(m5.shutil, "which", return_value="/usr/bin/ssh"), mock.patch.object(
                m5.subprocess, "run", return_value=types.SimpleNamespace(returncode=0, stdout="PASS\tfirewall_state\tok\n", stderr="")):
            self.assertEqual(m5.verify_command(args, self.home, ROOT), 3)

    def test_restore_fake_disk_is_byte_identical_and_records_each_phase(self):
        # Simulate current Path A content differing in a restorable userdata range.
        with self.disk.open("r+b") as f:
            f.seek(self.parts["userdata"][0] * 512 + 17); f.write(b"changed")
        original = (self.backup / "emmc-full.bin").read_bytes()
        fake_loader = self.tmp / "loader.mbn"; fake_loader.write_bytes(b"synthetic loader")
        args = types.SimpleNamespace(restore_device="deadbeef", restore_backup="synthetic", dry_run=False)
        fake_live = lambda edl, env, loader, directory, ident, session=None: (
            seed_fake_session_identity(session) or ("deadbeef", self.identity, self.disk, self.parts))
        confirmations = []
        def confirm(phase, ident):
            value = "restore %s %s" % (ident[-8:], phase); confirmations.append(value); return value
        with mock.patch.dict(os.environ, self.env, clear=True), \
             mock.patch.object(m5, "_live", side_effect=fake_live), \
             mock.patch.object(m5.shutil, "which", return_value=str(self.fake_edl_path)), \
             mock.patch.object(m5.common, "_loader_candidate", return_value=fake_loader), \
             mock.patch.object(m5.common, "LOADER_SHA256", hashlib.sha256(fake_loader.read_bytes()).hexdigest()), \
             mock.patch.object(m5, "_fwlib_path", return_value=str(self.fakebin) + ":/usr/bin:/bin"), \
             mock.patch.object(m5, "_live_identity_only"), \
             mock.patch.object(m5, "_confirm", side_effect=confirm), \
             mock.patch.object(m5, "_edl", wraps=m5._edl) as edl_mock:
            self.assertEqual(m5.restore_command(args, self.home, ROOT), 0)
        self.assertEqual(self.disk.read_bytes(), original)
        self.assertEqual(len(confirmations), len(m5.PLAN_PHASES))
        state = json.loads((self.home / "devices/deadbeef/restore-state.json").read_text())
        self.assertEqual(state["status"], "complete")
        self.assertEqual(state["completed_phases"], list(m5.PLAN_PHASES))
        calls_after_success = self.write_log.read_text().splitlines()
        with self.assertRaisesRegex(store.RefusedError, "already complete"):
            m5.restore_command(args, self.home, ROOT)
        self.assertEqual(self.write_log.read_text().splitlines(), calls_after_success)
        self.assertEqual(len(calls_after_success), len(m5.PLAN_PHASES))
        self.assertTrue(all(line.endswith("1 rollback-" + phase) for line, phase in zip(calls_after_success, m5.PLAN_PHASES)))

    def test_restore_final_full_readback_mismatch_is_exit3_and_blocks_retry(self):
        with self.disk.open("r+b") as f:
            f.seek(self.parts["userdata"][0] * 512 + 17); f.write(b"changed")
        fake_loader = self.tmp / "loader.mbn"; fake_loader.write_bytes(b"synthetic loader")
        args = types.SimpleNamespace(restore_device="deadbeef", restore_backup="synthetic", dry_run=False)
        fake_live = lambda edl, env, loader, directory, ident, session=None: (
            seed_fake_session_identity(session) or ("deadbeef", self.identity, self.disk, self.parts))
        def bad_final(edl, edl_args, env, loader=None, **kwargs):
            self.fake_edl(edl, edl_args, env)
            if edl_args[0] == "rf":
                target = pathlib.Path(edl_args[1])
                with target.open("r+b") as f: f.seek(0); f.write(b"X")
        with mock.patch.dict(os.environ, self.env, clear=True), \
             mock.patch.object(m5, "_live", side_effect=fake_live), \
             mock.patch.object(m5.shutil, "which", return_value=str(self.fake_edl_path)), \
             mock.patch.object(m5.common, "_loader_candidate", return_value=fake_loader), \
             mock.patch.object(m5.common, "LOADER_SHA256", hashlib.sha256(fake_loader.read_bytes()).hexdigest()), \
             mock.patch.object(m5, "_fwlib_path", return_value=str(self.fakebin) + ":/usr/bin:/bin"), \
             mock.patch.object(m5, "_live_identity_only"), \
             mock.patch.object(m5, "_confirm", side_effect=lambda phase, ident: "restore %s %s" % (ident[-8:], phase)), \
             mock.patch.object(m5, "_edl", side_effect=bad_final):
            with self.assertRaises(ValueError):
                m5.restore_command(args, self.home, ROOT)
        state = json.loads((self.home / "devices/deadbeef/restore-state.json").read_text())
        self.assertEqual(state["status"], "final-readback-failed")
        with self.assertRaises(store.RefusedError):
            m5.restore_command(args, self.home, ROOT)

    def test_restore_final_read_timeout_is_exit3_and_blocks_boot_recommendation(self):
        with self.disk.open("r+b") as f:
            f.seek(self.parts["userdata"][0] * 512 + 17); f.write(b"changed")
        fake_loader = self.tmp / "loader.mbn"; fake_loader.write_bytes(b"synthetic loader")
        fake_live = lambda edl, env, loader, directory, ident, session=None: (
            seed_fake_session_identity(session) or ("deadbeef", self.identity, self.disk, self.parts))
        real_edl = m5._edl
        def timeout_final(edl, edl_args, env, loader=None, **kwargs):
            if edl_args[0] == "rf":
                raise subprocess.TimeoutExpired([edl] + edl_args, 600)
            return real_edl(edl, edl_args, env, loader)
        argv = ["--home", str(self.home), "restore", "--device", "deadbeef", "--backup", "synthetic"]
        with mock.patch.dict(os.environ, self.env, clear=True), \
             mock.patch.object(m5, "_live", side_effect=fake_live), \
             mock.patch.object(m5.shutil, "which", return_value=str(self.fake_edl_path)), \
             mock.patch.object(m5.common, "_loader_candidate", return_value=fake_loader), \
             mock.patch.object(m5.common, "LOADER_SHA256", hashlib.sha256(fake_loader.read_bytes()).hexdigest()), \
             mock.patch.object(m5, "_fwlib_path", return_value=str(self.fakebin) + ":/usr/bin:/bin"), \
             mock.patch.object(m5, "_live_identity_only"), \
             mock.patch.object(m5, "_confirm", side_effect=lambda phase, ident: "restore %s %s" % (ident[-8:], phase)), \
             mock.patch.object(m5, "_edl", side_effect=timeout_final):
            self.assertEqual(m5.common.main(argv, usb_sysfs_root=self.sysfs), m5.common.EXIT_VERIFY)
        state = json.loads((self.home / "devices/deadbeef/restore-state.json").read_text())
        self.assertEqual(state["status"], "final-readback-failed")
        self.assertIn("full-disk readback failed", state["failure"])
        with self.assertRaises(store.RefusedError):
            m5.restore_command(types.SimpleNamespace(restore_device="deadbeef", restore_backup="synthetic",
                                                     dry_run=False, restore_reconcile_state=False), self.home, ROOT)

    def test_interrupted_phase_reconciliation_is_no_write_and_resumes_at_that_phase(self):
        with self.disk.open("r+b") as f:
            f.seek(self.parts["userdata"][0] * 512 + 17); f.write(b"changed")
        original = (self.backup / "emmc-full.bin").read_bytes()
        fake_loader = self.tmp / "loader.mbn"; fake_loader.write_bytes(b"synthetic loader")
        args = types.SimpleNamespace(restore_device="deadbeef", restore_backup="synthetic", dry_run=False,
                                     restore_reconcile_state=False)
        fake_live = lambda edl, env, loader, directory, ident, session=None: (
            seed_fake_session_identity(session) or ("deadbeef", self.identity, self.disk, self.parts))
        self.fail_control.write_text("%d fail\n" % self.parts["cache"][0])
        patches = [mock.patch.dict(os.environ, self.env, clear=True),
                   mock.patch.object(m5, "_live", side_effect=fake_live),
                   mock.patch.object(m5.shutil, "which", return_value=str(self.fake_edl_path)),
                   mock.patch.object(m5.common, "_loader_candidate", return_value=fake_loader),
                   mock.patch.object(m5.common, "LOADER_SHA256", hashlib.sha256(fake_loader.read_bytes()).hexdigest()),
                   mock.patch.object(m5, "_fwlib_path", return_value=str(self.fakebin) + ":/usr/bin:/bin"),
                   mock.patch.object(m5, "_live_identity_only"),
                   mock.patch.object(m5, "_confirm", side_effect=lambda phase, ident: "restore %s %s" % (ident[-8:], phase))]
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7]:
            with self.assertRaisesRegex(store.RefusedError, "helper refused or failed phase cache"):
                m5.restore_command(args, self.home, ROOT)
        state_path = self.home / "devices/deadbeef/restore-state.json"
        failed_state = json.loads(state_path.read_text())
        self.assertEqual(failed_state["status"], "partial-failure-reconciliation-required")
        self.assertEqual(failed_state["phase"], "cache")
        self.assertEqual(failed_state["completed_phases"], ["pad", "userdata"])
        writes_before = self.write_log.read_text().splitlines()
        args.restore_reconcile_state = True
        args.dry_run = False
        fake_loader = self.tmp / "loader.mbn"; fake_loader.write_bytes(b"synthetic loader")
        with mock.patch.dict(os.environ, self.env, clear=True), \
             mock.patch.object(m5.shutil, "which", return_value=str(self.fake_edl_path)), \
             mock.patch.object(m5.common, "_loader_candidate", return_value=fake_loader), \
             mock.patch.object(m5.common, "LOADER_SHA256", hashlib.sha256(fake_loader.read_bytes()).hexdigest()), \
             mock.patch.object(m5, "_live_identity_only"), \
             mock.patch("builtins.input", return_value="reconcile deadbeef cache"), \
             mock.patch.object(m5.sys, "stdin", FakeTTY()), mock.patch.object(m5.sys, "stdout", FakeTTY()):
            self.assertEqual(m5.restore_command(args, self.home, ROOT), 0)
        self.assertEqual(self.write_log.read_text().splitlines(), writes_before)
        args.restore_reconcile_state = False
        with mock.patch.dict(os.environ, self.env, clear=True), \
             mock.patch.object(m5, "_live", side_effect=fake_live), \
             mock.patch.object(m5.shutil, "which", return_value=str(self.fake_edl_path)), \
             mock.patch.object(m5.common, "_loader_candidate", return_value=fake_loader), \
             mock.patch.object(m5.common, "LOADER_SHA256", hashlib.sha256(fake_loader.read_bytes()).hexdigest()), \
             mock.patch.object(m5, "_fwlib_path", return_value=str(self.fakebin) + ":/usr/bin:/bin"), \
             mock.patch.object(m5, "_live_identity_only"), \
             mock.patch.object(m5, "_confirm", side_effect=lambda phase, ident: "restore %s %s" % (ident[-8:], phase)):
            self.assertEqual(m5.restore_command(args, self.home, ROOT), 0)
        self.assertEqual(self.disk.read_bytes(), original)
        writes_after = self.write_log.read_text().splitlines()
        self.assertFalse(any(line.split()[1] == str(self.parts["userdata"][0]) for line in writes_after[len(writes_before):]))
        self.assertEqual(json.loads(state_path.read_text())["status"], "complete")

    def test_reconcile_after_gpt_secondary_accepts_stock_firmware_and_resumes(self):
        with self.disk.open("r+b") as f:
            f.seek(self.parts["userdata"][0] * 512 + 17); f.write(b"changed")
        original = (self.backup / "emmc-full.bin").read_bytes()
        fake_loader = self.tmp / "loader.mbn"; fake_loader.write_bytes(b"synthetic loader")
        args = types.SimpleNamespace(restore_device="deadbeef", restore_backup="synthetic", dry_run=False,
                                     restore_reconcile_state=False)
        fake_live = lambda edl, env, loader, directory, ident, session=None: (
            seed_fake_session_identity(session) or ("deadbeef", self.identity, self.disk, self.parts))
        gpt_secondary = self.disk.stat().st_size // 512 - 33
        self.fail_control.write_text("%d complete\n" % gpt_secondary)
        with mock.patch.dict(os.environ, self.env, clear=True), \
             mock.patch.object(m5, "_live", side_effect=fake_live), \
             mock.patch.object(m5.shutil, "which", return_value=str(self.fake_edl_path)), \
             mock.patch.object(m5.common, "_loader_candidate", return_value=fake_loader), \
             mock.patch.object(m5.common, "LOADER_SHA256", hashlib.sha256(fake_loader.read_bytes()).hexdigest()), \
             mock.patch.object(m5, "_fwlib_path", return_value=str(self.fakebin) + ":/usr/bin:/bin"), \
             mock.patch.object(m5, "_live_identity_only"), \
             mock.patch.object(m5, "_confirm", side_effect=lambda phase, ident: "restore %s %s" % (ident[-8:], phase)):
            with self.assertRaisesRegex(store.RefusedError, "helper refused or failed phase gpt-secondary"):
                m5.restore_command(args, self.home, ROOT)
        state_path = self.home / "devices/deadbeef/restore-state.json"
        interrupted = json.loads(state_path.read_text())
        self.assertEqual(interrupted["phase"], "gpt-secondary")
        self.assertIn("aboot", interrupted["completed_phases"])
        writes_before = self.write_log.read_text().splitlines()
        args.restore_reconcile_state = True
        with mock.patch.dict(os.environ, self.env, clear=True), \
             mock.patch.object(m5.shutil, "which", return_value=str(self.fake_edl_path)), \
             mock.patch.object(m5.common, "_loader_candidate", return_value=fake_loader), \
             mock.patch.object(m5.common, "LOADER_SHA256", hashlib.sha256(fake_loader.read_bytes()).hexdigest()), \
             mock.patch.object(m5, "_live_identity_only"), \
             mock.patch.object(m5.sys, "stdin", FakeTTY()), mock.patch.object(m5.sys, "stdout", FakeTTY()), \
             mock.patch("builtins.input", return_value="reconcile deadbeef gpt-secondary"):
            self.assertEqual(m5.restore_command(args, self.home, ROOT), 0)
        self.assertEqual(self.write_log.read_text().splitlines(), writes_before)
        args.restore_reconcile_state = False
        with mock.patch.dict(os.environ, self.env, clear=True), \
             mock.patch.object(m5, "_live", side_effect=fake_live), \
             mock.patch.object(m5.shutil, "which", return_value=str(self.fake_edl_path)), \
             mock.patch.object(m5.common, "_loader_candidate", return_value=fake_loader), \
             mock.patch.object(m5.common, "LOADER_SHA256", hashlib.sha256(fake_loader.read_bytes()).hexdigest()), \
             mock.patch.object(m5, "_fwlib_path", return_value=str(self.fakebin) + ":/usr/bin:/bin"), \
             mock.patch.object(m5, "_live_identity_only"), \
             mock.patch.object(m5, "_confirm", side_effect=lambda phase, ident: "restore %s %s" % (ident[-8:], phase)):
            self.assertEqual(m5.restore_command(args, self.home, ROOT), 0)
        self.assertEqual(self.disk.read_bytes(), original)
        state = json.loads(state_path.read_text())
        self.assertEqual(state["status"], "complete")
        self.assertEqual(state["completed_phases"], list(m5.PLAN_PHASES))

    def test_partial_phase_bytes_block_reconciliation_without_retry(self):
        with self.disk.open("r+b") as f:
            f.seek(self.parts["userdata"][0] * 512 + 17); f.write(b"changed")
            f.seek(self.parts["cache"][0] * 512 + 600); f.write(b"wrong cache sector")
        fake_loader = self.tmp / "loader.mbn"; fake_loader.write_bytes(b"synthetic loader")
        args = types.SimpleNamespace(restore_device="deadbeef", restore_backup="synthetic", dry_run=False,
                                     restore_reconcile_state=False)
        fake_live = lambda edl, env, loader, directory, ident, session=None: (
            seed_fake_session_identity(session) or ("deadbeef", self.identity, self.disk, self.parts))
        self.fail_control.write_text("%d partial\n" % self.parts["cache"][0])
        with mock.patch.dict(os.environ, self.env, clear=True), \
             mock.patch.object(m5, "_live", side_effect=fake_live), \
             mock.patch.object(m5.shutil, "which", return_value=str(self.fake_edl_path)), \
             mock.patch.object(m5.common, "_loader_candidate", return_value=fake_loader), \
             mock.patch.object(m5.common, "LOADER_SHA256", hashlib.sha256(fake_loader.read_bytes()).hexdigest()), \
             mock.patch.object(m5, "_fwlib_path", return_value=str(self.fakebin) + ":/usr/bin:/bin"), \
             mock.patch.object(m5, "_live_identity_only"), \
             mock.patch.object(m5, "_confirm", side_effect=lambda phase, ident: "restore %s %s" % (ident[-8:], phase)):
            with self.assertRaisesRegex(store.RefusedError, "helper refused or failed phase cache"):
                m5.restore_command(args, self.home, ROOT)
        state_path = self.home / "devices/deadbeef/restore-state.json"
        self.assertEqual(json.loads(state_path.read_text())["status"], "partial-failure-reconciliation-required")
        writes_before = self.write_log.read_text().splitlines()
        args.restore_reconcile_state = True
        with mock.patch.dict(os.environ, self.env, clear=True), \
             mock.patch.object(m5.shutil, "which", return_value=str(self.fake_edl_path)), \
             mock.patch.object(m5.common, "_loader_candidate", return_value=fake_loader), \
             mock.patch.object(m5.common, "LOADER_SHA256", hashlib.sha256(fake_loader.read_bytes()).hexdigest()), \
             mock.patch.object(m5, "_live_identity_only"), \
             mock.patch.object(m5.sys, "stdin", FakeTTY()), mock.patch.object(m5.sys, "stdout", FakeTTY()), \
             mock.patch("builtins.input") as input_mock:
            with self.assertRaisesRegex(store.RefusedError, "partial or unknown"):
                m5.restore_command(args, self.home, ROOT)
        input_mock.assert_not_called()
        self.assertEqual(self.write_log.read_text().splitlines(), writes_before)
        self.assertEqual(json.loads(state_path.read_text())["status"], "manual-action-required")


if __name__ == "__main__":
    unittest.main(verbosity=2)
