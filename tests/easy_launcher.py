#!/usr/bin/env python3
"""Test-only launcher for `openstick easy` (never used outside tests/).

Runs the real CLI in-process after two test-only adjustments, both driven by env vars set by tests/easy_support.py:
  OPENSTICK_TEST_LOADER      the synthetic loader file: its SHA-256 replaces the pinned hash everywhere
                             (a real pinned loader is not available offline, and the product must not be told to skip the pin).
  OPENSTICK_TEST_FAKE_BUILD  directory holding boot.img and rootfs.img: build_command is replaced by a function that registers
                             that build (no sudo, no debootstrap). Each call appends a line to OPENSTICK_TEST_BUILD_MARK.
"""
import hashlib
import json
import os
import pathlib
import sys
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.installer import cli, store  # noqa: E402
import importlib  # noqa: E402

for name in ("edl_runner", "flash", "imageset", "verify_restore", "wizard", "packages", "easy", "build"):
    importlib.import_module("tools.installer." + name)

loader = os.environ.get("OPENSTICK_TEST_LOADER")
if loader:
    digest = hashlib.sha256(pathlib.Path(loader).read_bytes()).hexdigest()
    for module in list(sys.modules.values()):
        if getattr(module, "__name__", "").startswith("tools.installer"):
            for attribute in ("PINNED_LOADER_SHA256", "LOADER_SHA256"):
                if hasattr(module, attribute):
                    setattr(module, attribute, digest)

# Test-only, like OPENSTICK_TEST_LOADER: the pinned lk2nd hash is swapped for the synthetic lk2nd image's hash.
_lk2nd_sha = os.environ.get("OPENSTICK_TEST_LK2ND_SHA256")
if _lk2nd_sha:
    from tools.installer import easy as _easy_lk
    _easy_lk.LK2ND_PADDED_SHA256 = _lk2nd_sha

# Test-only, like OPENSTICK_TEST_LOADER: tools/pathA/fwlib.sh (run by the guarded restore) compares the loader to the real
# pinned hash through `sha256sum`. restore's PATH for it is a fixed helper that tests are told to patch (see
# verify_restore._fwlib_path); this prepends a private shim dir whose sha256sum answers the pinned hash for the synthetic loader.
fwlib_shims = os.environ.get("OPENSTICK_TEST_FWLIB_SHIM_DIR")
if fwlib_shims:
    from tools.installer import verify_restore as _vr
    _vr._fwlib_path = lambda: fwlib_shims + ":/usr/bin:/bin"

# Test-only mutation switch (mutation proofs in tests/groups/easy-restore-after-flash.py): drop one product step.
if os.environ.get("OPENSTICK_TEST_MUTATE") == "no-flash-entry":
    from tools.installer import easy as _easy
    _easy._register_flash = lambda ctx, plan: None
    _easy._flash_phase_done = lambda ctx, name: None
    _easy._update_flash_entry = lambda ctx, **changes: None
if os.environ.get("OPENSTICK_TEST_MUTATE") == "no-archive":
    from tools.installer import easy as _easy
    _easy._archive_completed_restore_state = lambda ctx: None

# Round 8 mutation switches (tests/groups/easy-*-guard*.py): each disables ONE new pre-write check so a test can show that
# the check, and nothing else, is what stops the run.
_mutate = os.environ.get("OPENSTICK_TEST_MUTATE")
if _mutate:
    from tools.installer import easy as _easy
    _noop = {"no-live-gpt-check": ("_check_live_gpt", lambda ctx, temp: None),
             "no-identity-compare": ("_check_identity", lambda ctx, serial, observed: None),
             "no-restore-state-check": ("_check_restore_state", lambda ctx: None),
             "no-payload-size-check": ("_check_payload_sizes", lambda plan: None),
             "no-firmware-check": ("_check_firmware_unchanged", lambda backup_rows, planned: None),
             "no-stock-signature": ("_check_stock_signature", lambda ctx: _easy._backup_rows(ctx)),
             "no-child-reap": ("_kill_children", lambda: None),
             # round 9
             "no-tz-range-check": ("_check_tz_range", lambda backup_rows, planned: None),
             "no-composite-fit-check": ("_check_composite_fits", lambda total, capacity: None),
             "no-pkg-gpt-check": ("_check_pkg_gpt", lambda ctx, plan: None),
             "no-region-classification": ("_check_regions_stock", lambda ctx, temp, plan: None),
             "no-edl-arg-guard": ("_check_edl_args", lambda args: None),
             "no-pkg-dir-check": ("_check_pkg_dir", lambda repo, path: __import__("pathlib").Path(path).resolve()),
             "no-fw-manifest-check": ("_check_fw_manifest", lambda pkg, board, emmc, layout: None)}
    _all = set(_mutate.split(","))                     # several switches may be combined: "a,b"
    for _name in _all & set(_noop):
        setattr(_easy, _noop[_name][0], _noop[_name][1])
    if "wrong-phase-order" in _all:
        _easy.FW_PHASE_ORDER = tuple(reversed(_easy.FW_PHASE_ORDER))
    if "no-readback-compare" in _all:
        _easy._same_digest = lambda got, want: True
    if "boot-alone" in _all:
        _easy._build_composite = lambda repo, boot_img, capacity: open(boot_img, "rb").read()
    if "allow-reset-and-final" in _all:
        _easy.FORBIDDEN_PHASES = ()
    if "no-sigint-shield" in _all:
        class _NoShield:
            def __enter__(self): return self
            def __exit__(self, *exc): return False
        _easy._SigintShield = _NoShield

fixture = os.environ.get("OPENSTICK_TEST_FAKE_BUILD")
if fixture:
    from tools.installer import build as build_module

    def fake_build(args, home, repo):
        mark = os.environ.get("OPENSTICK_TEST_BUILD_MARK")
        if mark:
            with open(mark, "a", encoding="ascii") as handle:
                handle.write("profile=%s board=%s\n" % (args.profile, args.build_board))
        build_id = "fakebuild-" + uuid.uuid4().hex[:8]
        directory = pathlib.Path(home) / "builds" / build_id
        directory.mkdir(mode=0o700)
        artifacts = {}
        for name in ("boot.img", "rootfs.img"):
            data = (pathlib.Path(fixture) / name).read_bytes()
            (directory / name).write_bytes(data)
            artifacts[name] = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
        (directory / "build.json").write_text(json.dumps(
            {"schema_version": 1, "build_id": build_id, "board": args.build_board, "profile": args.profile,
             "artifacts": artifacts}))
        registry = store.load_registry(home)
        registry.setdefault("builds", []).append({"build_id": build_id, "path": str(directory), "board": args.build_board,
                                                  "profile": args.profile})
        store.save_registry(home, registry)
        print("Fake build %s registered (test launcher)." % build_id)
        return 0

    build_module.build_command = fake_build

raise SystemExit(cli.main())
