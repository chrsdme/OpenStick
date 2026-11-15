"""Private installer state with defensive path handling."""
import json
import os
import stat
import subprocess
import shutil
import tempfile
from pathlib import Path


class RefusedError(Exception):
    """An operation was blocked by a safety gate."""


class StoreError(Exception):
    """A store operation failed."""


def _has_symlink_component(path):
    path = Path(os.path.abspath(os.fspath(path)))
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            if stat.S_ISLNK(os.lstat(current).st_mode):
                return True
        except FileNotFoundError:
            continue
    return False


def _inside_git(path):
    current = Path(path)
    while True:
        try:
            git = shutil.which("git") or ("/usr/bin/git" if os.path.exists("/usr/bin/git") else "git")
            result = subprocess.run(
                [git, "-C", str(current), "rev-parse", "--is-inside-work-tree"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=2,
            )
            if result.returncode == 0 and result.stdout.strip() == "true":
                return True
        except (OSError, subprocess.TimeoutExpired):
            pass
        if current.parent == current:
            return False
        current = current.parent


def validate_location(path):
    path = Path(os.path.abspath(os.fspath(path)))
    if _has_symlink_component(path):
        raise RefusedError("private store path contains a symlink")
    if _inside_git(path):
        raise RefusedError("private store must be outside every Git worktree")
    return path


def ensure_store(path):
    path = validate_location(path)
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not stat.S_ISDIR(os.lstat(path).st_mode):
            raise RefusedError("private store is not a directory")
        os.chmod(path, 0o700)
        for name in ("devices", "backups", "seeds", "images", "packages", "logs"):
            child = path / name
            child.mkdir(mode=0o700, exist_ok=True)
            if stat.S_ISLNK(os.lstat(child).st_mode) or not child.is_dir():
                raise RefusedError("private store contains an unsafe directory")
            os.chmod(child, 0o700)
    except RefusedError:
        raise
    except OSError as exc:
        raise StoreError("could not create private store") from exc
    return path


def _atomic_private_write(path, data):
    path = Path(path)
    if _has_symlink_component(path.parent) or path.is_symlink():
        raise RefusedError("refusing to write through a symlink")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
        os.chmod(path, 0o600)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _read_json(path, default):
    path = Path(path)
    if not path.exists():
        return default
    if _has_symlink_component(path) or not stat.S_ISREG(os.lstat(path).st_mode):
        raise RefusedError("refusing to read an unsafe state file")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "r", encoding="utf-8") as stream:
        return json.load(stream)


def load_registry(root):
    data = _read_json(Path(root) / "registry.json", {"schema": 1, "devices": []})
    if not isinstance(data, dict) or data.get("schema") != 1 or not isinstance(data.get("devices"), list):
        raise StoreError("registry has an unsupported or invalid format")
    return data


def save_registry(root, registry):
    if registry.get("schema") != 1 or not isinstance(registry.get("devices"), list):
        raise StoreError("refusing to save an invalid registry")
    _atomic_private_write(Path(root) / "registry.json", (json.dumps(registry, sort_keys=True, indent=2) + "\n").encode())


def append_event(root, event):
    path = Path(root) / "events.jsonl"
    if _has_symlink_component(path) or path.is_symlink():
        raise RefusedError("refusing to append through a symlink")
    payload = (json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
