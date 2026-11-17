"""Reject installer tests that inspect real USB sysfs or device nodes."""
import builtins
import io
import os
import shlex
import shutil
import subprocess
import sys


_PREFIXES = ("/sys/bus/usb", "/dev/bus/usb")
_REAL_POPEN = subprocess.Popen


def _path(value):
    try:
        raw = os.fspath(value)
    except TypeError:
        return None
    if isinstance(raw, bytes):
        raw = os.fsdecode(raw)
    return os.path.abspath(raw)


def _check(value):
    path = _path(value)
    if path and any(path == prefix or path.startswith(prefix + os.sep) for prefix in _PREFIXES):
        raise RuntimeError("TEST SAFETY GUARD blocked host USB path access")


def _inside_test_tmp(path, root):
    if not root:
        return False
    try:
        return os.path.commonpath((os.path.realpath(path), os.path.realpath(root))) == os.path.realpath(root)
    except (OSError, ValueError):
        return False


def _guard_edl_process(command, env, temp_root=None):
    if isinstance(command, (str, bytes, os.PathLike)):
        try:
            argv = shlex.split(os.fsdecode(command))
        except ValueError:
            argv = [os.fsdecode(command)]
    else:
        argv = [os.fsdecode(item) for item in command]
    if not argv:
        return
    client = None
    for item in argv[:3]:
        name = os.path.basename(item).lower()
        if (name.startswith("edl") and name != "edl-power-cycle") or name == "edl.py":
            client = item
            break
    loader = None
    for index, item in enumerate(argv):
        if item.startswith("--loader="):
            loader = item.split("=", 1)[1]
        elif item == "--loader" and index + 1 < len(argv):
            loader = argv[index + 1]
    is_edl = client is not None or loader is not None
    if not is_edl:
        return
    temp_root = temp_root if temp_root is not None else env.get("TMPDIR")
    if client is not None:
        resolved = client if os.path.sep in client else shutil.which(client, path=env.get("PATH"))
        if not resolved or not _inside_test_tmp(resolved, temp_root):
            raise RuntimeError("TEST SAFETY GUARD blocked EDL client outside the test temp directory")
    if loader and not _inside_test_tmp(loader, temp_root):
        raise RuntimeError("TEST SAFETY GUARD blocked EDL loader outside the test temp directory")


def _guarded_popen(command, *args, **kwargs):
    runner_tmp = os.environ.get("TMPDIR")
    child_env = dict(os.environ if kwargs.get("env") is None else kwargs["env"])
    if runner_tmp:
        child_env["TMPDIR"] = runner_tmp
    guard_path = os.path.dirname(os.path.abspath(__file__))
    existing_pythonpath = child_env.get("PYTHONPATH", "")
    child_env["PYTHONPATH"] = guard_path + (os.pathsep + existing_pythonpath if existing_pythonpath else "")
    kwargs["env"] = child_env
    _guard_edl_process(command, child_env, temp_root=runner_tmp)
    return _REAL_POPEN(command, *args, **kwargs)


def guard_current_edl_script():
    script = sys.argv[0] if sys.argv else ""
    name = os.path.basename(script).lower()
    if (name.startswith("edl") and name != "edl-power-cycle") or name == "edl.py":
        if not _inside_test_tmp(script, os.environ.get("TMPDIR")):
            raise RuntimeError("TEST SAFETY GUARD blocked EDL client outside the test temp directory")
        _guard_edl_process([script, *sys.argv[1:]], os.environ)


def install():
    if getattr(install, "_installed", False):
        return

    def wrap(function):
        def guarded(path, *args, **kwargs):
            _check(path)
            return function(path, *args, **kwargs)
        guarded.__name__ = function.__name__
        return guarded

    builtins.open = wrap(builtins.open)
    io.open = wrap(io.open)
    os.open = wrap(os.open)
    os.scandir = wrap(os.scandir)
    os.listdir = wrap(os.listdir)
    os.stat = wrap(os.stat)
    os.lstat = wrap(os.lstat)
    os.access = wrap(os.access)
    subprocess.Popen = _guarded_popen
    install._installed = True
