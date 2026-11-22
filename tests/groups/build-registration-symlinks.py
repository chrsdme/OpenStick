"""build: registration after the stage scripts must never follow symlinks and must remove work/ before the permission pass.

A fake 20-rootfs stage creates work/rootfs with nested 0o755 directories, an absolute symlink to a
root-owned host file, a dangling symlink, symlinks to a user-owned file and directory OUTSIDE the build,
plus 0o500 and 0o000 directories. Hermetic: stages are mocked (no sudo, no root, no hardware, no network).
Mutation proofs run the same scenario against deliberately broken copies of build.py."""
import hashlib, json, os, pathlib, shutil, stat, sys, tempfile, types
from unittest import mock
root, tmp = sys.argv[1:3]
sys.path.insert(0, root)
from tools.installer import build as real_build, store

SRC = pathlib.Path(real_build.__file__).read_text(encoding="utf-8")
MANIFEST_KEYS = ["artifacts", "baseline_comparison", "board", "build_id", "created", "device_boot_verified",
                 "documented_seed_hook_difference", "inputs", "legacy_user_env", "profile", "schema_version", "source", "started",
                 "tools", "verified_by"]
ROOT_OWNED = next((p for p in ("/usr/sbin/rmt", "/bin/sh", "/usr/bin/env") if os.path.exists(p) and os.stat(p).st_uid == 0), None)
assert ROOT_OWNED, "no root-owned host file to use as a symlink target"


def mode(path):
    return stat.S_IMODE(os.lstat(str(path)).st_mode)


def sha(path):
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()


def load_variant(name, source):
    module = types.ModuleType("tools.installer." + name)
    module.__package__ = "tools.installer"
    module.__file__ = name
    exec(compile(source, name, "exec"), module.__dict__)
    return module


def rmtree_tolerant(path):
    for dirpath, dirs, files in os.walk(path, followlinks=False):
        try: os.chmod(dirpath, 0o700)
        except OSError: pass
    shutil.rmtree(path, ignore_errors=True)


def scenario(build_module, fail_stage=None):
    """Return assertions-as-exceptions: raises AssertionError when a property is violated."""
    base = pathlib.Path(tempfile.mkdtemp(prefix="openstick-build-symlinks-", dir=tmp))
    try:
        home = store.ensure_store(base / "private")
        repo = base / "repo"
        (repo / "boards/ufi103s").mkdir(parents=True)
        (repo / "boards/ufi103s/board.env").write_text("BOARD_DESC=test\n")
        (repo / "scripts").mkdir()
        for name in ("20-rootfs.sh", "50-boot-img.sh", "51-rootfs-img.sh"):
            (repo / "scripts" / name).write_text("#!/bin/sh\nexit 0\n")
        (repo / "out").mkdir()
        (repo / "out/base-bookworm-arm64.tar.gz").write_bytes(b"fake-base")
        outside = base / "outside"
        outside.mkdir(mode=0o755)
        outside_file = outside / "user-file"
        outside_file.write_bytes(b"outside-content")
        os.chmod(outside_file, 0o644)
        outside_dir = outside / "user-dir"
        outside_dir.mkdir(mode=0o755)
        (outside_dir / "inner").write_bytes(b"inner")
        os.chmod(outside_dir / "inner", 0o644)
        root_before = (mode(ROOT_OWNED), sha(ROOT_OWNED))

        def fake_run(command, env=None, **kwargs):
            if command[0] == "git":
                return types.SimpleNamespace(returncode=128, stdout="")
            script = next(pathlib.Path(p).name for p in command if p.endswith(("20-rootfs.sh", "50-boot-img.sh", "51-rootfs-img.sh")))
            if script == "20-rootfs.sh":
                chroot = pathlib.Path(env["CHROOT"])
                (chroot / "etc/nested/deeper").mkdir(parents=True, mode=0o755)
                (chroot / "usr/sbin").mkdir(parents=True, mode=0o755)
                (chroot / "etc/nested/deeper/file").write_bytes(b"x")
                (chroot / "etc/rmt").symlink_to(ROOT_OWNED)                   # absolute, root-owned host target
                (chroot / "etc/dangling").symlink_to("/nonexistent-openstick-target")
                (chroot / "etc/user-file-link").symlink_to(outside_file)      # user-owned file outside the build
                (chroot / "etc/user-dir-link").symlink_to(outside_dir)        # user-owned directory outside the build
                (chroot / "etc/nested/rel-link").symlink_to("deeper/file")
                (chroot / "ro").mkdir(mode=0o755); (chroot / "ro/f").write_bytes(b"r"); os.chmod(chroot / "ro", 0o500)
                (chroot / "closed").mkdir(mode=0o755); (chroot / "closed/f").write_bytes(b"c"); os.chmod(chroot / "closed", 0o000)
            if script == "50-boot-img.sh":
                out = pathlib.Path(env["PROD_OUT"]); out.mkdir(parents=True, exist_ok=True); (out / "boot.img").write_bytes(b"boot")
            if script == "51-rootfs-img.sh":
                out = pathlib.Path(env["PRODROOT_OUT"]); out.mkdir(parents=True, exist_ok=True); (out / "rootfs.img").write_bytes(b"rootfs")
            return types.SimpleNamespace(returncode=8 if script == fail_stage else 0)

        args = types.SimpleNamespace(profile="dev", build_board="ufi103s", bake_user_env=False, dry_run=False)
        try:
            with mock.patch.object(build_module.os, "geteuid", return_value=1000), \
                 mock.patch.object(build_module, "_version", return_value="test-tool"), \
                 mock.patch.object(build_module.subprocess, "run", side_effect=fake_run):
                rc = build_module.build_command(args, home, repo)
            raised = None
        except BaseException as error:          # noqa: BLE001 - reported as a test failure below
            rc, raised = None, error

        # Host targets are untouched in every case (success or failure).
        assert (mode(ROOT_OWNED), sha(ROOT_OWNED)) == root_before, "root-owned host target was modified"
        assert mode(outside_file) == 0o644 and outside_file.read_bytes() == b"outside-content", "user-owned outside file was modified"
        assert mode(outside_dir) == 0o755 and mode(outside_dir / "inner") == 0o644, "user-owned outside directory tree was modified"
        builds = home / "builds"
        if fail_stage:
            assert isinstance(raised, store.StoreError), "failing stage did not raise StoreError: %r" % (raised,)
            assert not list(builds.iterdir()), "failed build left something behind: %s" % list(builds.iterdir())
            assert store.load_registry(home).get("builds", []) == [], "failed build was registered"
            return
        assert raised is None and rc == 0, "build did not register: %r" % (raised,)
        entry = store.load_registry(home)["builds"][-1]
        final = pathlib.Path(entry["path"])
        assert final.is_dir() and not (final / "work").exists(), "work/ is still in the registered set"
        assert not list(builds.glob(".build-*")), "staging left behind"
        found_files = 0
        for dirpath, dirnames, filenames in os.walk(final, followlinks=False):
            for name in dirnames + filenames:
                path = pathlib.Path(dirpath) / name
                assert not path.is_symlink(), "registered set holds a symlink: %s" % path
                assert "work" not in path.relative_to(final).parts, "registered set holds work/: %s" % path
                if path.is_file():
                    found_files += 1
                    assert mode(path) == 0o400, "file mode %o: %s" % (mode(path), path)
                else:
                    assert mode(path) == 0o500, "dir mode %o: %s" % (mode(path), path)
        assert mode(final) == 0o500 and found_files >= 3
        manifest = json.loads((final / "build.json").read_text())
        assert sorted(manifest) == MANIFEST_KEYS, "build.json keys changed: %s" % sorted(manifest)
        assert manifest["artifacts"]["boot.img"]["sha256"] == sha(final / "boot.img")
        assert manifest["artifacts"]["rootfs.img"]["sha256"] == sha(final / "rootfs.img")
        assert sorted(manifest["artifacts"]["boot.img"]) == ["sha256", "size"]
        assert manifest["inputs"] == {"base-bookworm-arm64.tar.gz": hashlib.sha256(b"fake-base").hexdigest()}
        assert mode(final / "build.json") == 0o400 and mode(final / "boot.img") == 0o400
        assert entry["boot_sha256"] == manifest["artifacts"]["boot.img"]["sha256"]
    finally:
        rmtree_tolerant(base)


def must_fail(label, build_module, fail_stage=None):
    try:
        scenario(build_module, fail_stage)
    except AssertionError:
        return
    raise SystemExit("MUTATION SURVIVED: %s" % label)


if os.geteuid() == 0:
    raise SystemExit("this group must run as a non-root user (restrictive directory modes are only enforced for non-root)")

# ---- Real code: success path and failure path.
scenario(real_build)
scenario(real_build, fail_stage="50-boot-img.sh")
assert True  # reached: both scenarios passed

# ---- Mutation (a): rmtree(work) after the chmod loop (the original order) must be caught.
REMOVE_BLOCK = """        _chmod_tree(work, 0o600, 0o700, include_root=True)
        shutil.rmtree(work)
"""
assert SRC.count(REMOVE_BLOCK) == 1
AFTER = "        os.chmod(output, 0o500)\n"
assert SRC.count(AFTER) == 1
mut_a = SRC.replace(REMOVE_BLOCK, "").replace(AFTER, AFTER + "        shutil.rmtree(work)\n")
assert mut_a != SRC
must_fail("rmtree(work) after the chmod loop", load_variant("build_mut_a", mut_a))

# ---- Mutation (b): lstat -> stat (symlinks followed); chmod errors strict (host root target => EPERM) ...
assert SRC.count("os.lstat(root)") == 1
mut_b = SRC.replace("os.lstat(root)", "os.stat(root)")
must_fail("chmod follows symlinks (strict)", load_variant("build_mut_b", mut_b))
# ... and with errors swallowed everywhere, the user-owned outside file must still be seen as modified.
mut_b2 = mut_b.replace("        if not ignore_errors:\n            raise", "        pass")
assert mut_b2 != mut_b
must_fail("chmod follows symlinks (errors swallowed)", load_variant("build_mut_b2", mut_b2))
must_fail("chmod follows symlinks on the failure cleanup path", load_variant("build_mut_b3", mut_b2), fail_stage="50-boot-img.sh")
