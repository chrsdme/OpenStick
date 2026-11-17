"""Reject installer tests that inspect real USB sysfs or device nodes."""
import builtins
import io
import os


_PREFIXES = ("/sys/bus/usb", "/dev/bus/usb")


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
    install._installed = True
