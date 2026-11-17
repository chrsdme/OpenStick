"""Resumable, single-stage orchestration for the offline installer workflow.

The wizard deliberately runs one named stage per invocation. Each underlying
command retains ownership of its own validation and confirmation protocol.
"""
import datetime
import hashlib
import json
import os
import re
import secrets
import stat
import time
import uuid
from pathlib import Path

from . import store


STAGES = ("doctor", "backup", "configure", "build", "inject", "flash",
          "verify", "restore", "package")
DESTRUCTIVE = frozenset(("flash", "restore"))
NON_REPLAYABLE = frozenset(("backup", "flash", "restore", "package"))
EXIT_OK, EXIT_ERROR, EXIT_REFUSED = 0, 1, 2
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")


def _open_dir_nofollow(path):
    """Open a directory by walking each component without following links."""
    path = Path(path)
    if ".." in path.parts:
        raise store.RefusedError("wizard path contains a parent traversal component")
    path = path.absolute()
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path.anchor, flags)
    try:
        for component in path.parts[1:]:
            next_fd = os.open(component, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except Exception:
        os.close(fd)
        raise


def _open_regular_nofollow(path):
    """Open a regular file through a descriptor-anchored, no-symlink walk."""
    path = Path(path).absolute()
    parent_fd = _open_dir_nofollow(path.parent)
    try:
        fd = os.open(path.name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
    finally:
        os.close(parent_fd)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise store.RefusedError("wizard file is not a regular file")
    return fd


def _read_text_regular(path, encoding):
    fd = _open_regular_nofollow(path)
    with os.fdopen(fd, "r", encoding=encoding) as stream:
        return stream.read()


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _sha256(path):
    digest = hashlib.sha256()
    fd = _open_regular_nofollow(path)
    with os.fdopen(fd, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_json_regular(path):
    fd = _open_regular_nofollow(path)
    with os.fdopen(fd, "r", encoding="utf-8") as stream:
        return json.load(stream)


def _tree_files(root):
    """Return every regular file and reject symlinks anywhere in a tree."""
    root = Path(root)
    files = set()
    for current, directories, names in os.walk(root, followlinks=False):
        parent = Path(current)
        for name in list(directories):
            path = parent / name
            mode = os.lstat(path).st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                raise store.RefusedError("wizard artifact tree contains an unsafe directory")
        for name in names:
            path = parent / name
            mode = os.lstat(path).st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
                raise store.RefusedError("wizard artifact tree contains an unsafe member")
            files.add(path.relative_to(root).as_posix())
    return files


def _safe_state_dir(home):
    root = Path(home) / "wizard"
    try:
        home_fd = _open_dir_nofollow(home)
        try:
            try:
                os.mkdir("wizard", 0o700, dir_fd=home_fd)
            except FileExistsError:
                pass
            fd = os.open("wizard", os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) |
                         getattr(os, "O_NOFOLLOW", 0), dir_fd=home_fd)
            try:
                os.fchmod(fd, 0o700)
            finally:
                os.close(fd)
        finally:
            os.close(home_fd)
    except OSError as exc:
        raise store.RefusedError("wizard state directory is unsafe") from exc
    return root


def _state_path(home, run_id):
    if not isinstance(run_id, str) or not _ID_RE.fullmatch(run_id):
        raise store.RefusedError("invalid wizard run ID")
    return _safe_state_dir(home) / (run_id + ".json")


def _new_state(run_id):
    return {"schema_version": 1, "run_id": run_id, "created_at": _now(),
            "updated_at": _now(), "status": "active", "selection": {},
            "stages": [], "pending_destructive_stage": None}


def _load(path, allow_pending=False):
    try:
        fd = _open_regular_nofollow(path)
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            state = json.load(stream)
    except FileNotFoundError:
        raise store.StoreError("wizard run does not exist; omit --resume to create a run")
    except OSError as exc:
        raise store.RefusedError("wizard state path is unsafe") from exc
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise store.RefusedError("wizard state is unreadable or corrupt; manual recovery is required")
    if (not isinstance(state, dict) or state.get("schema_version") != 1 or
            not isinstance(state.get("stages"), list) or not isinstance(state.get("selection"), dict) or
            state.get("run_id") != path.stem or
            any(not isinstance(item, dict) or item.get("name") not in STAGES or
                item.get("status") not in ("running", "complete", "failed", "uncertain", "reconciled")
                for item in state["stages"]) or
            state.get("pending_destructive_stage") not in (None, "flash", "restore")):
        raise store.RefusedError("wizard state schema is invalid; manual recovery is required")
    if state.get("pending_destructive_stage") and not allow_pending:
        raise store.RefusedError(
            "wizard has an unresolved destructive stage (%s); inspect device state and reconcile manually before continuing" %
            state["pending_destructive_stage"])
    return state


def _save(path, state):
    state["updated_at"] = _now()
    payload = (json.dumps(state, sort_keys=True, indent=2) + "\n").encode("utf-8")
    parent_fd = _open_dir_nofollow(path.parent)
    temporary = ".%s.%s.tmp" % (path.name, secrets.token_hex(8))
    fd = None
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                     getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=parent_fd)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            fd = None
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        os.fsync(parent_fd)
    finally:
        if fd is not None:
            os.close(fd)
        try:
            os.unlink(temporary, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        os.close(parent_fd)


def _entries(state):
    return {item["name"]: item for item in state["stages"]
            if isinstance(item, dict) and isinstance(item.get("name"), str)}


def _selection(args):
    values = {
        "device_id": getattr(args, "wizard_device", None) or getattr(args, "device", None),
        "backup_id": getattr(args, "wizard_backup", None),
        "build_id": getattr(args, "wizard_build", None),
        "seed_id": getattr(args, "wizard_seed", None),
        "image_set": getattr(args, "wizard_image_set", None),
        "board": getattr(args, "wizard_board", None) or getattr(args, "board", None),
        "profile": getattr(args, "wizard_profile", None),
        "package_type": getattr(args, "package_type", None),
        "package_output": getattr(args, "package_output", None),
        "host": getattr(args, "wizard_host", None),
    }
    result = {}
    for key, value in values.items():
        if value is not None:
            result[key] = str(value)
    return result


def _validate_selection(state, selection):
    old = state.get("selection", {})
    if not isinstance(old, dict):
        raise store.RefusedError("wizard selection state is invalid")
    for key, value in selection.items():
        if key in old and old[key] != value:
            raise store.RefusedError("wizard %s cannot change during resume; start a new run" % key.replace("_", " "))
    old.update(selection)
    state["selection"] = old


def _device_and_backup(home, selection):
    registry = store.load_registry(home)
    device_id = selection.get("device_id")
    if not device_id:
        raise store.RefusedError("select a registered device with --device")
    devices = [item for item in registry.get("devices", [])
               if str(item.get("id", "")).lower() == device_id.lower()]
    if len(devices) != 1:
        raise store.RefusedError("selected device is not registered")
    device = devices[0]
    backup_id = selection.get("backup_id")
    backups = [item for item in device.get("backups", [])
               if backup_id is None or item.get("id") == backup_id]
    for backup in backups:
        path = Path(backup.get("path", ""))
        if path.is_symlink() or not path.is_dir() or store._has_symlink_component(path):
            continue
        try:
            manifest = _read_json_regular(path / "manifest.json")
            dump = path / "emmc-full.bin"
            sums_path = path / "SHA256SUMS"
            if sums_path.is_symlink() or not stat.S_ISREG(os.lstat(sums_path).st_mode):
                continue
            sums = _read_text_regular(sums_path, "ascii").splitlines()
            if not isinstance(manifest, dict):
                continue
            if (dump.is_symlink() or not dump.is_file() or _sha256(dump) != manifest.get("sha256") or
                    backup.get("sha256") != manifest.get("sha256") or
                    sums != [manifest["sha256"] + "  emmc-full.bin"] or
                    manifest.get("identity") != device.get("identity") or
                    "full-read-size" not in manifest.get("verified_by", []) or
                    "gpt-primary-and-backup-crc" not in manifest.get("verified_by", [])):
                continue
            return registry, device, backup, manifest
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            continue
    raise store.RefusedError("selected device has no matching verified full backup")


def _registered_build(home, build_id):
    registry = store.load_registry(home)
    rows = [item for item in registry.get("builds", []) if item.get("build_id") == build_id]
    if len(rows) != 1:
        raise store.RefusedError("selected build is not registered")
    row = rows[0]
    path = Path(row.get("path", ""))
    if path.is_symlink() or store._has_symlink_component(path) or not path.is_dir():
        raise store.RefusedError("selected build path is unsafe or missing")
    try:
        manifest = _read_json_regular(path / "build.json")
        if not isinstance(manifest, dict):
            raise store.RefusedError("selected build manifest is invalid")
        for name, key in (("boot.img", "boot_sha256"), ("rootfs.img", "rootfs_sha256")):
            artifact = path / name
            if artifact.is_symlink() or not artifact.is_file() or _sha256(artifact) != row.get(key):
                raise store.RefusedError("selected build artifact hash mismatch: " + name)
            if manifest.get("artifacts", {}).get(name, {}).get("sha256") != row.get(key):
                raise store.RefusedError("selected build manifest mismatch: " + name)
        if manifest.get("build_id") != build_id or manifest.get("board") != row.get("board"):
            raise store.RefusedError("selected build manifest identity mismatch")
    except (OSError, ValueError, json.JSONDecodeError):
        raise store.RefusedError("selected build manifest is invalid")
    return row, path, manifest


def _verify_image_set(path):
    root = Path(path).absolute()
    if root.is_symlink() or store._has_symlink_component(root) or store._inside_git(root):
        raise store.RefusedError("selected image set is unsafe or inside a Git worktree")
    sums_path = root / "SHA256SUMS"
    if sums_path.is_symlink() or not sums_path.is_file():
        raise store.RefusedError("selected image set lacks SHA256SUMS")
    seen = set()
    for line in _read_text_regular(sums_path, "ascii").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9_./+-]+)", line)
        if not match:
            raise store.RefusedError("selected image set has an invalid checksum entry")
        digest, relative = match.groups()
        relpath = Path(relative)
        target = root / relpath
        if relpath.is_absolute() or ".." in relpath.parts or relative in seen or target.is_symlink() or not target.is_file():
            raise store.RefusedError("selected image set has an unsafe or duplicate member")
        if _sha256(target) != digest:
            raise store.RefusedError("selected image set checksum mismatch: " + relative)
        seen.add(relative)
    actual = _tree_files(root) - {"SHA256SUMS"}
    if not seen or actual != seen:
        raise store.RefusedError("selected image set contains missing or unlisted files")
    return root, _sha256(sums_path)


def _existing_seed(home, name):
    from . import cli
    path, _content = cli._seed_path(home, name)
    return path, _sha256(path)


def _require(state, stage):
    entries = _entries(state)
    dependencies = {
        "doctor": (), "backup": ("doctor",), "configure": ("doctor",),
        "build": ("doctor", "configure"), "inject": ("build", "configure"),
        "flash": ("doctor", "backup", "inject"), "verify": ("doctor",),
        "restore": ("doctor", "backup"), "package": ("doctor", "backup"),
    }[stage]
    missing = [name for name in dependencies if entries.get(name, {}).get("status") != "complete"]
    if missing:
        raise store.RefusedError("wizard stage %s requires completed stage(s): %s" % (stage, ", ".join(missing)))
    selection = state.get("selection", {})
    if stage == "verify" and not selection.get("host"):
        raise store.RefusedError("verify stage requires an explicit SSH --host")
    if stage == "flash" and (not selection.get("device_id") or not selection.get("image_set")):
        raise store.RefusedError("flash stage requires a registered --device and verified --image-set")
    if stage == "restore" and not selection.get("device_id"):
        raise store.RefusedError("restore stage requires a registered --device")
    if stage == "inject" and not selection.get("build_id"):
        raise store.RefusedError("inject stage requires a registered --build ID")
    if stage == "package" and (not selection.get("package_type") or not selection.get("package_output")):
        raise store.RefusedError("package stage requires --package-type and --package-output")
    if stage == "package" and selection.get("package_type") == "install":
        if not selection.get("image_set"):
            raise store.RefusedError("install package stage requires a verified --image-set")
        if entries.get("build", {}).get("status") != "complete":
            raise store.RefusedError("install package stage requires a completed build or verified image-set selection")


def _args_for(args, stage, state, home=None):
    """Build a minimal namespace for the existing public module APIs."""
    from argparse import Namespace
    chosen = state["selection"]
    common = {"home": str(home) if home is not None else getattr(args, "home", None),
              "dry_run": bool(getattr(args, "dry_run", False)),
              "board": chosen.get("board"), "device": chosen.get("device_id"),
              "usb_sysfs_root": getattr(args, "usb_sysfs_root", "/sys/bus/usb/devices"),
              "usb_device_root": getattr(args, "usb_device_root", "/dev/bus/usb")}
    if stage == "backup":
        return Namespace(**common, paranoid=bool(getattr(args, "paranoid", False)),
                         again=bool(getattr(args, "again", False)))
    if stage == "configure":
        return Namespace(**common, seed=chosen.get("seed_id", "default"),
                         profile=chosen.get("profile", "dev"), show=False)
    if stage == "build":
        return Namespace(**common, build_board=chosen.get("board"),
                         profile=chosen.get("profile", "dev"),
                         bake_user_env=bool(getattr(args, "bake_user_env", False)))
    if stage == "inject":
        return Namespace(**common, build=chosen.get("build_id"), seed=chosen.get("seed_id", "default"))
    if stage == "flash":
        return Namespace(**common, flash_device=chosen.get("device_id"),
                         image_set=chosen.get("image_set"),
                         reconcile_state=bool(getattr(args, "reconcile_state", False)))
    if stage in ("verify", "restore", "package"):
        package_type = chosen.get("package_type")
        return Namespace(**common, device_id=chosen.get("device_id"),
                         backup_id=chosen.get("backup_id"), build_id=chosen.get("build_id"),
                         restore_device=chosen.get("device_id"),
                         restore_backup=chosen.get("backup_id"),
                         restore_reconcile_state=bool(getattr(args, "reconcile_state", False)),
                         seed_id=chosen.get("seed_id"), image_set=chosen.get("image_set"),
                         host=chosen.get("host"),
                         package_type=package_type,
                         package_device=chosen.get("device_id"),
                         package_backup=chosen.get("backup_id"),
                         package_image_set=chosen.get("image_set"),
                         package_output=chosen.get("package_output"))
    return Namespace(**common)


def _execute(args, home, repo, stage, state):
    if stage == "doctor":
        from . import cli
        prepared = _args_for(args, stage, state, home)
        return cli.doctor(prepared, prepared.usb_sysfs_root, prepared.usb_device_root)
    if stage == "backup":
        from . import cli
        if state["selection"].get("backup_id"):
            _device_and_backup(home, state["selection"])
            return EXIT_OK
        prepared = _args_for(args, stage, state, home)
        from . import edl_runner
        with edl_runner.Session(sysfs_root=prepared.usb_sysfs_root) as session:
            return cli.backup_command(prepared, home, session=session)
    if stage == "configure":
        from . import configure
        selected = state["selection"]
        seed_path = Path(home) / "seeds" / (selected.get("seed_id", "default") + ".seed")
        if seed_path.exists():
            _existing_seed(home, selected.get("seed_id", "default"))
            return EXIT_OK
        configure.configure(home, selected.get("seed_id", "default"), selected.get("profile", "dev"))
        return EXIT_OK
    if stage == "build":
        from . import build
        selected = state["selection"]
        if selected.get("build_id"):
            _registered_build(home, selected["build_id"])
            return EXIT_OK
        if selected.get("image_set"):
            from . import imageset
            return imageset.verify_command(selected["image_set"])
        return build.build_command(_args_for(args, stage, state, home), home, repo)
    if stage == "inject":
        from . import cli
        return cli.inject_seed(_args_for(args, stage, state, home), home)
    if stage == "flash":
        from . import flash
        return flash.flash_command(_args_for(args, stage, state, home), home, repo)
    if stage in ("verify", "restore"):
        from . import verify_restore
        function = verify_restore.verify_command if stage == "verify" else verify_restore.restore_command
        return function(_args_for(args, stage, state, home), home, repo)
    if stage == "package":
        from . import packages
        return packages.package_command(_args_for(args, stage, state, home), home, repo)
    raise store.RefusedError("unknown wizard stage")


def _result_ids(stage, home, before_registry, after_registry, state):
    """Record identifiers and hashes only; never copy secret or seed contents."""
    evidence = {}
    if stage == "backup":
        before = {backup.get("id") for item in before_registry.get("devices", [])
                  for backup in item.get("backups", [])}
        for device in after_registry.get("devices", []):
            added = [b for b in device.get("backups", []) if b.get("id") not in before]
            if added:
                item = added[-1]
                evidence.update(device_id=device.get("id"), backup_id=item.get("id"), sha256=item.get("sha256"))
                break
        if not evidence and state["selection"].get("backup_id"):
            _registry, device, item, manifest = _device_and_backup(home, state["selection"])
            evidence.update(device_id=device.get("id"), backup_id=item.get("id"),
                            sha256=manifest.get("sha256"))
    elif stage == "build":
        prior = {item.get("build_id") for item in before_registry.get("builds", [])}
        for item in after_registry.get("builds", []):
            if item.get("build_id") not in prior:
                evidence.update(build_id=item.get("build_id"), board=item.get("board"), profile=item.get("profile"),
                                boot_sha256=item.get("boot_sha256"), rootfs_sha256=item.get("rootfs_sha256"))
                break
        if not evidence and state["selection"].get("build_id"):
            row, _path, manifest = _registered_build(home, state["selection"]["build_id"])
            evidence.update(build_id=row.get("build_id"), board=row.get("board"), profile=row.get("profile"),
                            boot_sha256=row.get("boot_sha256"), rootfs_sha256=row.get("rootfs_sha256"))
        elif not evidence and state["selection"].get("image_set"):
            from . import imageset
            manifest = imageset._verify_set(state["selection"]["image_set"])
            evidence.update(build_id=manifest.get("build_id"), board=manifest.get("board"),
                            image_set=state["selection"]["image_set"],
                            image_set_manifest_sha256=_sha256(Path(state["selection"]["image_set"]) / "manifest.json"))
    elif stage == "configure":
        name = state["selection"].get("seed_id", "default")
        seed_path = Path(home) / "seeds" / (name + ".seed")
        if seed_path.is_file() and not seed_path.is_symlink():
            evidence.update(seed_id=name, seed_sha256=_sha256(seed_path))
    elif stage == "inject":
        evidence.update(build_id=state["selection"].get("build_id"), seed_id=state["selection"].get("seed_id", "default"))
    elif stage in ("flash", "verify", "restore", "package"):
        evidence.update({key: state["selection"].get(key) for key in
                         ("device_id", "backup_id", "build_id", "image_set", "host", "package_type", "package_output")
                         if state["selection"].get(key)})
        if stage == "package":
            output = Path(state["selection"].get("package_output", ""))
            if output.is_symlink() or not output.is_file() or not stat.S_ISREG(os.lstat(output).st_mode):
                raise store.RefusedError("package command completed without a regular package output")
            evidence["package_sha256"] = _sha256(output)
            evidence["package_size"] = output.stat().st_size
    return evidence


def _stage_evidence(stage, home, state):
    """Snapshot selected IDs and verified input hashes before stage execution."""
    selected = state["selection"]
    evidence = {key: selected[key] for key in
                ("device_id", "backup_id", "build_id", "seed_id", "board", "profile", "host", "package_type")
                if selected.get(key)}
    if stage in ("backup", "flash", "restore", "package") and selected.get("device_id") and selected.get("backup_id"):
        _registry, device, backup, manifest = _device_and_backup(home, selected)
        evidence.update(device_id=device.get("id"), backup_id=backup.get("id"), backup_sha256=manifest.get("sha256"))
    if stage in ("build", "inject", "flash", "package") and selected.get("build_id"):
        row, _root, manifest = _registered_build(home, selected["build_id"])
        evidence.update(build_id=row.get("build_id"), boot_sha256=row.get("boot_sha256"),
                        rootfs_sha256=row.get("rootfs_sha256"))
    if stage in ("configure", "inject", "flash") and selected.get("seed_id"):
        seed_path = Path(home) / "seeds" / (selected["seed_id"] + ".seed")
        if seed_path.exists():
            _path, digest = _existing_seed(home, selected["seed_id"])
            evidence["seed_sha256"] = digest
    if stage in ("build", "flash", "package") and selected.get("image_set"):
        image_root = Path(selected["image_set"])
        manifest_path = image_root / "manifest.json"
        if manifest_path.is_file() and not manifest_path.is_symlink():
            from . import imageset
            manifest = imageset._verify_set(image_root)
            evidence.update(image_set=selected["image_set"], image_set_build_id=manifest.get("build_id"),
                            image_set_manifest_sha256=_sha256(manifest_path))
        else:
            _root, digest = _verify_image_set(image_root)
            evidence.update(image_set=selected["image_set"], image_set_checksums_sha256=digest)
    if selected.get("package_output"):
        evidence["package_output"] = selected["package_output"]
    evidence["stage"] = stage
    return evidence


def run(args, home, repo):
    """Run one named workflow stage, persisting sanitized evidence and history.

    CLI integrations should set ``args.stage`` and may set ``args.wizard_id``.
    ``args.resume`` loads that run; it never automatically retries a write.
    """
    stage = getattr(args, "stage", None)
    if stage not in STAGES:
        raise store.RefusedError("select one supported wizard --stage: " + ", ".join(STAGES))
    requested_id = getattr(args, "wizard_id", None)
    resume = bool(getattr(args, "resume", False))
    reconcile = bool(getattr(args, "reconcile_state", False))
    if resume and not requested_id:
        raise store.RefusedError("--resume requires a wizard --id")
    if reconcile and (not resume or not requested_id):
        raise store.RefusedError("wizard --reconcile-state requires --resume and a wizard --id")
    if requested_id is None:
        requested_id = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
    path = _state_path(home, requested_id)
    state = _load(path, allow_pending=reconcile) if resume else _new_state(requested_id)
    if not resume and path.exists():
        raise store.RefusedError("wizard run ID already exists; use --resume to continue it")
    _validate_selection(state, _selection(args))
    pending = state.get("pending_destructive_stage")
    if reconcile:
        if pending not in ("flash", "restore") or stage != pending:
            raise store.RefusedError("wizard reconciliation must match the pending flash or restore stage")
        _require(state, pending)
        try:
            result = _execute(args, home, repo, pending, state)
        except BaseException:
            # The underlying flash command owns its state and confirmation gate.
            # Keep wizard uncertainty unless it reported a complete reconciliation.
            raise
        if result not in (None, EXIT_OK):
            return int(result)
        previous = _entries(state).get("flash")
        if pending == "restore":
            previous = _entries(state).get("restore")
        if previous:
            previous["status"] = "reconciled"
            previous["reconciled_at"] = _now()
        state["pending_destructive_stage"] = None
        _save(path, state)
        store.append_event(home, {"at": int(time.time()), "type": "wizard.stage.reconciled", "run_id": requested_id,
                                  "stage": pending, "method": "underlying-no-write-reconciliation"})
        print("Wizard %s reconciliation recorded. No write was performed; request a separate fresh stage to continue." % pending)
        return EXIT_OK
    _require(state, stage)
    completed = _entries(state).get(stage)
    if completed and completed.get("status") == "complete":
        raise store.RefusedError("wizard stage already completed; use a new run ID for a new operation")
    previous = _entries(state).get(stage)
    if previous and previous.get("status") in ("running", "uncertain"):
        if stage in NON_REPLAYABLE or not resume:
            raise store.RefusedError("wizard stage outcome is uncertain; do not repeat it. Inspect outputs/device state and start a new run only after reconciliation")
    if stage in DESTRUCTIVE and not resume and requested_id and path.exists():
        raise store.RefusedError("destructive stage requires a new wizard run or an explicitly resumed safe state")

    if stage in DESTRUCTIVE:
        state["pending_destructive_stage"] = stage
    entry = {"name": stage, "status": "running", "started_at": _now(),
             "selection_evidence": _stage_evidence(stage, home, state)}
    state["stages"].append(entry)
    if stage in DESTRUCTIVE:
        state["pending_destructive_stage"] = stage
    _save(path, state)
    store.append_event(home, {"at": int(time.time()), "type": "wizard.stage.started", "run_id": requested_id, "stage": stage})

    try:
        before_registry = store.load_registry(home)
        result = _execute(args, home, repo, stage, state)
        if result not in (None, EXIT_OK):
            entry["status"] = "uncertain" if stage in DESTRUCTIVE or stage in NON_REPLAYABLE else "failed"
            entry["finished_at"] = _now()
            entry["exit_code"] = int(result)
            if stage in DESTRUCTIVE:
                state["pending_destructive_stage"] = stage
            _save(path, state)
            store.append_event(home, {"at": int(time.time()), "type": "wizard.stage.failed", "run_id": requested_id,
                                      "stage": stage, "uncertain": entry["status"] == "uncertain", "exit_code": int(result)})
            return int(result)
        after_registry = store.load_registry(home)
        selection = state["selection"]
        entry["evidence"] = _result_ids(stage, home, before_registry, after_registry, state)
        if stage == "backup" and entry["evidence"].get("backup_id"):
            selection.setdefault("device_id", entry["evidence"].get("device_id"))
            selection.setdefault("backup_id", entry["evidence"].get("backup_id"))
        if stage == "build" and entry["evidence"].get("build_id"):
            selection.setdefault("build_id", entry["evidence"].get("build_id"))
        entry["status"] = "complete"
        entry["finished_at"] = _now()
        state["selection"] = selection
        if stage in DESTRUCTIVE:
            state["pending_destructive_stage"] = None
        _save(path, state)
        store.append_event(home, {"at": int(time.time()), "type": "wizard.stage.complete", "run_id": requested_id, "stage": stage})
        prefix = "Wizard resumed" if resume else "Wizard"
        print("%s stage %s complete (run %s)." % (prefix, stage, requested_id))
        return EXIT_OK
    except BaseException as exc:
        entry["status"] = "uncertain" if stage in DESTRUCTIVE or stage in NON_REPLAYABLE else "failed"
        entry["finished_at"] = _now()
        entry["error_type"] = type(exc).__name__
        # Store only a safe exception class, not exception text that may contain
        # secret material, command arguments, or user-provided values.
        if stage in DESTRUCTIVE:
            state["pending_destructive_stage"] = stage
        _save(path, state)
        store.append_event(home, {"at": int(time.time()), "type": "wizard.stage.failed", "run_id": requested_id,
                                  "stage": stage, "uncertain": entry["status"] == "uncertain"})
        if isinstance(exc, KeyboardInterrupt):
            raise
        raise
