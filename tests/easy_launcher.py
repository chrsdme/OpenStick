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
