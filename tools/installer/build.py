"""Isolated build wrapper for the verified rootfs and production image stages."""
import datetime
import hashlib
import json
import os
import shutil
import shlex
import stat
import subprocess
import tempfile
import uuid
from pathlib import Path

from . import store


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def tree_hash(root):
    digest = hashlib.sha256()
    for path in sorted(Path(root).rglob("*")):
        if path.is_file() and ".git" not in path.parts and "out" not in path.parts and "work" not in path.parts:
            rel = path.relative_to(root).as_posix().encode()
            digest.update(rel + b"\0" + bytes.fromhex(sha256(path)) + b"\n")
    return digest.hexdigest()


def _chmod_tree(root, file_mode, dir_mode, include_root=False, ignore_errors=False):
    """chmod a tree WITHOUT ever following a symlink (lstat only; symlinks are left untouched).

    Directories are made user-accessible first so they can be listed, then get dir_mode after
    their children were handled. Build output can contain absolute symlinks (etc/rmt -> /usr/sbin/rmt)
    that must never be resolved against the host."""
    root = str(root)
    try:
        mode = os.lstat(root).st_mode
        if stat.S_ISLNK(mode):
            return
        if stat.S_ISREG(mode):
            os.chmod(root, file_mode)
            return
        if not stat.S_ISDIR(mode):
            return
        os.chmod(root, 0o700)
        with os.scandir(root) as entries:
            children = [entry.path for entry in entries]
        for child in children:
            _chmod_tree(child, file_mode, dir_mode, True, ignore_errors)
        if include_root:
            os.chmod(root, dir_mode)
    except OSError:
        if not ignore_errors:
            raise


def _version(command):
    tool = shutil.which(command)
    if not tool:
        return "missing"
    try:
        result = subprocess.run([tool, "--version"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=5)
        return result.stdout.splitlines()[0][:200] if result.stdout else "exit-%d" % result.returncode
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable"


def _safe_copy(source, destination):
    source = Path(source)
    if not source.is_file() or source.is_symlink():
        raise store.StoreError("required build input is missing or unsafe: " + source.name)
    shutil.copyfile(source, destination)


def build_command(args, home, repo):
    if args.profile not in ("dev", "prod"):
        raise store.RefusedError("profile must be dev or prod")
    board = args.build_board
    board_dir = repo / "boards" / board
    if not board_dir.is_dir() or board_dir.is_symlink():
        raise store.RefusedError("unknown board: " + board)
    if args.dry_run:
        mode = "legacy settings enabled" if args.bake_user_env else "generic isolated settings"
        print("DRY RUN: build profile=%s board=%s (%s)" % (args.profile, board, mode))
        if args.profile == "dev" and not args.bake_user_env:
            print("Generic dev images retain config/defaults.env credentials: DEFAULT_USER=openstick DEFAULT_PASS=openstick (public defaults; password expires on first login).")
        print("Order: scripts/20-rootfs.sh, scripts/50-boot-img.sh, scripts/51-rootfs-img.sh")
        return 0
    if os.geteuid() == 0:
        raise store.RefusedError("run build as a non-root user; only scripts/20-rootfs.sh is elevated, and the image stages must remain non-root")

    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    build_id = stamp + "-" + uuid.uuid4().hex[:8]
    builds = home / "builds"
    staging = Path(tempfile.mkdtemp(prefix=".build-" + build_id + ".", dir=str(builds)))
    os.chmod(staging, 0o700)
    output = staging / "out"
    work = staging / "work"
    private_home = staging / "home"
    output.mkdir(mode=0o700)
    work.mkdir(mode=0o700)
    private_home.mkdir(mode=0o700)
    source_out = repo / "out"
    final = builds / build_id
    registered = False
    try:
        # The 20-rootfs stage consumes the already prepared base tarball. Inputs
        # are copied into private staging so the scripts cannot overwrite repo output.
        base = source_out / "base-bookworm-arm64.tar.gz"
        cache = source_out / "cache"
        if base.is_file():
            _safe_copy(base, output / base.name)
        if cache.is_dir() and not cache.is_symlink():
            (output / "cache").mkdir(mode=0o700)
            for item in cache.iterdir():
                if item.is_file() and not item.is_symlink():
                    _safe_copy(item, output / "cache" / item.name)
        if not (output / "base-bookworm-arm64.tar.gz").is_file():
            raise store.StoreError("prepared Debian base tar is missing at out/base-bookworm-arm64.tar.gz")

        allowed = {
            "PATH": os.environ.get("PATH", "/usr/sbin:/usr/bin:/sbin:/bin"),
            "HOME": str(private_home),
            "TERM": os.environ.get("TERM", "dumb"),
            "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
            "BOARD": board, "PROFILE": args.profile,
            "OPENSTICK_BOARD": board, "OPENSTICK_PROFILE": args.profile,
            "OUT": str(output), "WORK": str(work),
            "CHROOT": str(work / "rootfs"),
            "PROD_OUT": str(output / "prod-boot"),
            "PRODROOT_OUT": str(output / "prod-rootfs"),
            "ROOTFS_TGZ": str(output / ("rootfs-%s.tgz" % board)),
            "OPENSTICK_OUT": str(output), "OPENSTICK_WORK": str(work),
            "OPENSTICK_CHROOT": str(work / "rootfs"),
            "OPENSTICK_PROD_OUT": str(output / "prod-boot"),
            "OPENSTICK_PRODROOT_OUT": str(output / "prod-rootfs"),
            "OPENSTICK_ROOTFS_TGZ": str(output / ("rootfs-%s.tgz" % board)),
            "OPENSTICK_ROOT": str(repo), "OPENSTICK_CONFIG_MODE": "legacy" if args.bake_user_env else "generic",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        # The explicit legacy option restores the caller environment and lets
        # common.sh retain its exact local/user/board/default precedence.
        if args.bake_user_env:
            allowed = dict(os.environ)
            allowed.update({"BOARD": board, "PROFILE": args.profile, "OUT": str(output), "WORK": str(work),
                           "CHROOT": str(work / "rootfs"), "PROD_OUT": str(output / "prod-boot"),
                           "PRODROOT_OUT": str(output / "prod-rootfs"), "ROOTFS_TGZ": str(output / ("rootfs-%s.tgz" % board)),
                           "OPENSTICK_ROOT": str(repo), "OPENSTICK_CONFIG_MODE": "legacy",
                           "HOME": os.environ.get("HOME", "/root")})

        scripts = [repo / "scripts" / "20-rootfs.sh", repo / "scripts" / "50-boot-img.sh", repo / "scripts" / "51-rootfs-img.sh"]
        for index, script in enumerate(scripts):
            command = ["/bin/sh", str(script)]
            run_env = dict(allowed)
            if index == 0:
                # 20-rootfs emits a numeric-owner tar, then its temporary chroot
                # and output paths need to return to the invoking user so the
                # rootless stages and failure cleanup can operate safely.
                handoff = '"/bin/sh" "$1"; rc=$?; chown -R "$2:$3" "$4" "$5"; owner_rc=$?; [ "$rc" -ne 0 ] && exit "$rc"; exit "$owner_rc"'
                if args.bake_user_env:
                    command = ["sudo", "-E", "/bin/sh", "-c", handoff, "openstick-build", str(script), str(os.getuid()), str(os.getgid()), str(output), str(work)]
                else:
                    command = ["sudo", "env", "-i"] + [k + "=" + v for k, v in sorted(run_env.items())] + ["/bin/sh", "-c", handoff, "openstick-build", str(script), str(os.getuid()), str(os.getgid()), str(output), str(work)]
                print("Required privileged command: " + shlex.join(command))
            else:
                print("Running without root: /bin/sh %s" % script)
            result = subprocess.run(command, env=run_env)
            if result.returncode:
                raise store.StoreError("build stage %s failed with exit %d" % (script.name, result.returncode))

        boot = output / "prod-boot" / "boot.img"
        rootfs = output / "prod-rootfs" / "rootfs.img"
        if not boot.is_file() or not rootfs.is_file():
            raise store.StoreError("build stages completed without both boot.img and rootfs.img")
        # Remove the transient tree FIRST (it holds the chroot with absolute symlinks and restrictive
        # modes), so the permission pass below only walks what stays in the registered set.
        _chmod_tree(work, 0o600, 0o700, include_root=True)
        shutil.rmtree(work)
        # Register only a complete staged output. Artifact permissions become
        # read-only after their hashes and manifest have been recorded.
        boot_hash, rootfs_hash = sha256(boot), sha256(rootfs)
        try:
            os.link(boot, staging / "boot.img")
            os.link(rootfs, staging / "rootfs.img")
        except OSError:
            (staging / "boot.img").unlink(missing_ok=True)
            (staging / "rootfs.img").unlink(missing_ok=True)
            _safe_copy(boot, staging / "boot.img")
            _safe_copy(rootfs, staging / "rootfs.img")
        revision = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        source = {"revision": revision.stdout.strip() if revision.returncode == 0 else None, "tree_sha256": tree_hash(repo)}
        manifest = {"schema_version": 1, "build_id": build_id, "profile": args.profile, "board": board,
                    "source": source, "legacy_user_env": bool(args.bake_user_env),
                    "started": stamp, "inputs": {p.relative_to(output).as_posix(): sha256(p)
                                                   for p in output.rglob("*") if p.is_file() and
                                                   (p.name == "base-bookworm-arm64.tar.gz" or "cache" in p.relative_to(output).parts)},
                    "artifacts": {"boot.img": {"sha256": boot_hash, "size": boot.stat().st_size},
                                  "rootfs.img": {"sha256": rootfs_hash, "size": rootfs.stat().st_size}},
                    "tools": {name: _version(name) for name in ("python3", "bash", "fakeroot", "mke2fs", "e2fsck", "debugfs", "debootstrap")},
                    "created": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "baseline_comparison": {"status": "unavailable", "reason": "M0 image artifacts and pinned external inputs are absent from the sanitized workspace"},
                    "documented_seed_hook_difference": ["apply-seed utility", "openstick-seed.service"],
                    "verified_by": ["artifact-sha256", "build-stage-exit-status"],
                    "device_boot_verified": False}
        (staging / "build.json").write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        for child in sorted(staging.iterdir()):
            _chmod_tree(child, 0o400, 0o500, include_root=True)
        os.chmod(output, 0o500)
        (staging / "build.json").chmod(0o400)
        os.replace(staging, final)
        os.chmod(final, 0o500)
        registry = store.load_registry(home)
        registry.setdefault("builds", []).append({"build_id": build_id, "profile": args.profile, "board": board,
                                                   "path": str(final), "boot_sha256": boot_hash, "rootfs_sha256": rootfs_hash,
                                                   "created": manifest["created"]})
        try:
            store.save_registry(home, registry)
            store.append_event(home, {"at": manifest["created"], "type": "build.registered", "build_id": build_id,
                                      "profile": args.profile, "board": board, "boot_sha256": boot_hash, "rootfs_sha256": rootfs_hash})
            registered = True
        except Exception:
            # Output without registry+event is removed, never treated as a successful build.
            original = store.load_registry(home)
            original["builds"] = [item for item in original.get("builds", []) if item.get("build_id") != build_id]
            store.save_registry(home, original)
            _chmod_tree(final, 0o600, 0o700, include_root=True, ignore_errors=True)
            shutil.rmtree(final, ignore_errors=True)
            raise
        print("Build %s registered. Images are immutable; device boot is not verified." % build_id)
        return 0
    except BaseException:
        if staging.exists():
            _chmod_tree(staging, 0o600, 0o700, include_root=True, ignore_errors=True)
            shutil.rmtree(staging, ignore_errors=True)
        if final.exists() and not registered:
            try:
                _chmod_tree(final, 0o600, 0o700, include_root=True, ignore_errors=True)
                shutil.rmtree(final, ignore_errors=True)
            except OSError:
                pass
        raise
