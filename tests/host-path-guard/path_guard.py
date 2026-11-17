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
        _block("USB-PATH", "blocked host USB path access", path)


def _inside_test_tmp(path, root):
    if not root:
        return False
    try:
        return os.path.commonpath((os.path.realpath(path), os.path.realpath(root))) == os.path.realpath(root)
    except (OSError, ValueError):
        return False


HIT_FILE_ENV = "OPENSTICK_GUARD_HIT_FILE"
_REAL_OPEN_FD = os.open
_REAL_WRITE = os.write
_REAL_CLOSE = os.close

# KNOWN LIMITS (the guard cannot see these; tests/groups/guard-known-limits.py
# runs them and records the outcome explicitly):
#  - children started with `python -I` or `python -S`: sitecustomize and
#    PYTHONPATH are not loaded, so nothing is patched in that child.
#    (partial cover: python -I keeps the environment, so a Python EDL client it launches still
#    loads the guard via the inherited PYTHONPATH and refuses itself; a non-Python client is unseen)
#  - children started with an emptied environment (`env -i`, env={}) that do not
#    carry PYTHONPATH: same result, and the hit-file variable is gone as well.
#  - exec/spawn done by non-Python code (a bash script calling an EDL client
#    itself, a C helper): only the start of that script is checked, not its
#    inner commands. PATH is shim-first, so a bare `edl` resolves to the shim.
#  - python -c / heredoc code strings are not parsed for client names.
# Every hit that IS seen is appended to the file named by OPENSTICK_GUARD_HIT_FILE
# (set by the runner); the runner fails the run if that file is non-empty.

_WRAPPERS_NOARG = {"env", "nohup", "setsid", "command", "exec", "time", "nice", "ionice", "stdbuf", "timeout", "doas", "sudo", "xargs", "chroot", "unshare", "flock"}
# options that consume the next token, per wrapper
_WRAPPER_OPT_ARG = {
    "sudo": {"-u", "-g", "-C", "-D", "-h", "-p", "-r", "-t", "-U", "-T", "-R"},
    "env": {"-u", "-C", "-S"},
    "nice": {"-n"},
    "ionice": {"-c", "-n", "-p"},
    "stdbuf": {"-i", "-o", "-e"},
    "timeout": {"-s", "-k"},
    "doas": {"-u", "-C"},
    "flock": {"-w", "-E"},
}
_WRAPPERS_POSITIONAL = {"timeout": 1, "flock": 1, "chroot": 1}  # leading positional args
_SHELLS = {"sh", "bash", "dash", "zsh", "ksh", "ash"}


class GuardViolation(BaseException):
    """Deliberately NOT an Exception: product code with `except Exception` must not absorb it.
    The hit file written before raising stays the primary evidence."""


def _log_hit(kind, detail):
    path = os.environ.get(HIT_FILE_ENV)
    if not path:
        return
    try:
        line = "%s pid=%d %s: %s\n" % (kind, os.getpid(), os.path.basename(sys.argv[0]) if sys.argv else "?", detail)
        fd = _REAL_OPEN_FD(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            _REAL_WRITE(fd, line.encode("utf-8", "replace")[:1000])
        finally:
            _REAL_CLOSE(fd)
    except Exception:
        pass


def _block(kind, message, detail):
    _log_hit(kind, detail)
    raise GuardViolation("TEST SAFETY GUARD " + message)


def _looks_like_edl_client(item):
    name = os.path.basename(item).lower()
    return (name.startswith("edl") and name != "edl-power-cycle") or name == "edl.py"


def _tokens(command):
    if isinstance(command, (str, bytes, os.PathLike)):
        text = os.fsdecode(command)
        try:
            lexer = shlex.shlex(text, posix=True, punctuation_chars=True)
            lexer.whitespace_split = True
            return list(lexer)
        except ValueError:
            return text.split()
    return [os.fsdecode(item) for item in command]


def _segments(tokens):
    """Split a token list on shell control operators into simple commands."""
    current = []
    for tok in tokens:
        if tok and all(ch in "();&|<>" for ch in tok):
            if tok in ("<", ">", ">>", "<<", "<&", ">&"):
                current.append(tok)
                continue
            if current:
                yield current
            current = []
        else:
            current.append(tok)
    if current:
        yield current


def _effective_commands(argv, depth=0):
    """Yield every argv that would really be exec'd, looking through
    sudo/env/timeout-style wrappers and sh/bash -c strings."""
    if depth > 6 or not argv:
        return
    i = 0
    while i < len(argv):
        tok = argv[i]
        name = os.path.basename(tok)
        if "=" in tok and not tok.startswith("-") and "/" not in tok.split("=", 1)[0]:
            i += 1  # VAR=value assignment (env or shell prefix)
            continue
        if name in _WRAPPERS_NOARG and i < len(argv) - 1:
            opts = _WRAPPER_OPT_ARG.get(name, set())
            i += 1
            skip_pos = _WRAPPERS_POSITIONAL.get(name, 0)
            while i < len(argv):
                cur = argv[i]
                if cur == "--":
                    i += 1
                    break
                if cur.startswith("-") and len(cur) > 1:
                    i += 2 if cur in opts else 1
                elif "=" in cur and name in ("env", "sudo") and "/" not in cur.split("=", 1)[0]:
                    i += 1
                elif skip_pos:
                    skip_pos -= 1
                    i += 1
                else:
                    break
            continue
        break
    argv = argv[i:]
    if not argv:
        return
    name = os.path.basename(argv[0])
    if name in _SHELLS:
        for idx, item in enumerate(argv[1:], 1):
            if item.startswith("-") and "c" in item[1:] and not item.startswith("--"):
                if idx + 1 < len(argv):
                    for seg in _segments(_tokens(argv[idx + 1])):
                        yield from _effective_commands(seg, depth + 1)
                return
    yield argv


def _guard_edl_process(command, env, temp_root=None):
    argv = _tokens(command)
    if not argv:
        return
    env = env if env is not None else os.environ
    temp_root = temp_root if temp_root is not None else env.get("TMPDIR")
    candidates = []
    # Old behaviour kept: a client in the first 3 words of the raw argv.
    if any(_looks_like_edl_client(item) for item in argv[:3]):
        candidates.append(argv)
    for eff in _effective_commands(argv):
        if _looks_like_edl_client(eff[0]):
            candidates.append(eff)
        else:
            # interpreter running an EDL script: python3 [-opts] edl.py
            if os.path.basename(eff[0]).lower().startswith("python"):
                for item in eff[1:]:
                    if item.startswith("-"):
                        continue
                    if _looks_like_edl_client(item):
                        candidates.append([item] + eff[eff.index(item) + 1:])
                    break
    for cand in candidates:
        client = next((item for item in cand[:3] if _looks_like_edl_client(item)), None)
        if client is None:
            continue
        loader = None
        for index, item in enumerate(cand):
            if item.startswith("--loader="):
                loader = item.split("=", 1)[1]
            elif item == "--loader" and index + 1 < len(cand):
                loader = cand[index + 1]
        resolved = client if os.path.sep in client else shutil.which(client, path=env.get("PATH"))
        if not resolved or not _inside_test_tmp(resolved, temp_root):
            _block("EDL-CLIENT", "blocked EDL client outside the test temp directory", " ".join(argv)[:300])
        if loader and not _inside_test_tmp(loader, temp_root):
            _block("EDL-LOADER", "blocked EDL loader outside the test temp directory", " ".join(argv)[:300])


def _child_env(kwargs_env):
    runner_tmp = os.environ.get("TMPDIR")
    child_env = dict(os.environ if kwargs_env is None else kwargs_env)
    if runner_tmp:
        child_env["TMPDIR"] = runner_tmp
    if HIT_FILE_ENV in os.environ:
        child_env[HIT_FILE_ENV] = os.environ[HIT_FILE_ENV]
    guard_path = os.path.dirname(os.path.abspath(__file__))
    existing_pythonpath = child_env.get("PYTHONPATH", "")
    child_env["PYTHONPATH"] = guard_path + (os.pathsep + existing_pythonpath if existing_pythonpath else "")
    return child_env, runner_tmp


def _guarded_popen(command, *args, **kwargs):
    child_env, runner_tmp = _child_env(kwargs.get("env"))
    kwargs["env"] = child_env
    _guard_edl_process(command, child_env, temp_root=runner_tmp)
    return _REAL_POPEN(command, *args, **kwargs)


def _install_os_guards():
    """Guard os.system, os.exec*, os.posix_spawn*, os.spawn* (argv looked up
    from the real arguments; environment taken from the call when present)."""
    def check_argv(argv, env):
        _guard_edl_process(list(argv), env if env is not None else os.environ,
                           temp_root=os.environ.get("TMPDIR"))

    def wrap_system(function):
        def system(command):
            _guard_edl_process(os.fsdecode(command), os.environ, temp_root=os.environ.get("TMPDIR"))
            return function(command)
        return system

    def wrap_exec_v(function, has_env):
        def guarded(path, argv, *rest):
            env = rest[0] if has_env and rest else None
            check_argv([os.fsdecode(path)] + [os.fsdecode(a) for a in list(argv)[1:]], env)
            return function(path, argv, *rest)
        return guarded

    def wrap_exec_l(function, has_env):
        def guarded(path, *args):
            argv = args[:-1] if has_env else args
            env = args[-1] if has_env else None
            check_argv([os.fsdecode(path)] + [os.fsdecode(a) for a in list(argv)[1:]], env)
            return function(path, *args)
        return guarded

    def wrap_spawn_v(function, has_env):
        def guarded(mode, path, argv, *rest):
            env = rest[0] if has_env and rest else None
            check_argv([os.fsdecode(path)] + [os.fsdecode(a) for a in list(argv)[1:]], env)
            return function(mode, path, argv, *rest)
        return guarded

    def wrap_spawn_l(function, has_env):
        def guarded(mode, path, *args):
            argv = args[:-1] if has_env else args
            env = args[-1] if has_env else None
            check_argv([os.fsdecode(path)] + [os.fsdecode(a) for a in list(argv)[1:]], env)
            return function(mode, path, *args)
        return guarded

    def wrap_posix_spawn(function):
        def guarded(path, argv, env, *args, **kwargs):
            check_argv([os.fsdecode(path)] + [os.fsdecode(a) for a in list(argv)[1:]], env)
            return function(path, argv, env, *args, **kwargs)
        return guarded

    if hasattr(os, "system"):
        os.system = wrap_system(os.system)
    for name, has_env in (("execv", False), ("execve", True), ("execvp", False), ("execvpe", True)):
        if hasattr(os, name):
            setattr(os, name, wrap_exec_v(getattr(os, name), has_env))
    for name, has_env in (("execl", False), ("execle", True), ("execlp", False), ("execlpe", True)):
        if hasattr(os, name):
            setattr(os, name, wrap_exec_l(getattr(os, name), has_env))
    for name, has_env in (("spawnv", False), ("spawnve", True), ("spawnvp", False), ("spawnvpe", True)):
        if hasattr(os, name):
            setattr(os, name, wrap_spawn_v(getattr(os, name), has_env))
    for name, has_env in (("spawnl", False), ("spawnle", True), ("spawnlp", False), ("spawnlpe", True)):
        if hasattr(os, name):
            setattr(os, name, wrap_spawn_l(getattr(os, name), has_env))
    for name in ("posix_spawn", "posix_spawnp"):
        if hasattr(os, name):
            setattr(os, name, wrap_posix_spawn(getattr(os, name)))


def guard_current_edl_script():
    script = sys.argv[0] if sys.argv else ""
    name = os.path.basename(script).lower()
    if (name.startswith("edl") and name != "edl-power-cycle") or name == "edl.py":
        if not _inside_test_tmp(script, os.environ.get("TMPDIR")):
            _block("EDL-CLIENT", "blocked EDL client outside the test temp directory", script)
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
    _install_os_guards()
    install._installed = True
