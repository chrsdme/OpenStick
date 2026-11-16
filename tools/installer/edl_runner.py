"""Safe process boundary for the external Qualcomm EDL client."""
import hashlib
import os
import re
import shutil
import signal
import subprocess
import sys
from pathlib import Path

from . import store


PINNED_LOADER_SHA256 = "959439aa5864685999b713c3ed12ad5fa408149648b670a9a9ef77bcc9dcab14"
LOADER_NAME = "prog_emmc_firehose_8916.mbn"
IDENTITY_READ_TIMEOUT_SECONDS = 20
RANGE_READ_TIMEOUT_SECONDS = 20
FULL_READ_TIMEOUT_SECONDS = 3600
INTERRUPT_GRACE_SECONDS = 10
POWER_CYCLE_HINT = (
    "The board may be in a half-open EDL session. Unplug USB for 30 s and "
    "reconnect WITHOUT the button, then re-enter EDL."
)


class EdlRunnerError(store.StoreError):
    """An EDL invocation failed or exceeded its safe timeout."""


def _hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def resolve_client():
    """Return the absolute executable command prefix for the EDL client."""
    edl_dir = os.environ.get("OPENSTICK_EDL_DIR")
    if edl_dir:
        directory = Path(edl_dir)
        executable = directory / "edl"
        script = directory / "edl.py"
        if executable.is_file() and os.access(executable, os.X_OK):
            return [str(executable.resolve())]
        if script.is_file():
            return [sys.executable, str(script.resolve())]
        raise store.RefusedError("OPENSTICK_EDL_DIR must contain an executable edl or an edl.py script")
    executable = shutil.which("edl")
    if not executable:
        raise store.RefusedError("EDL client not found; set OPENSTICK_EDL_DIR or add edl to PATH")
    return [str(Path(executable).resolve())]


def resolve_loader():
    """Find the pinned Firehose loader and verify it before returning its path."""
    candidates = []
    edl_dir = os.environ.get("OPENSTICK_EDL_DIR")
    if edl_dir:
        directory = Path(edl_dir)
        candidates.extend((directory / LOADER_NAME, directory / ".." / LOADER_NAME))
    candidates.extend((Path("out") / LOADER_NAME,
                       Path(__file__).resolve().parents[2] / "out" / LOADER_NAME))

    for candidate in candidates:
        try:
            if not candidate.exists():
                continue
            if candidate.is_symlink() or not candidate.is_file():
                raise store.RefusedError(
                    "Firehose loader must be a regular non-symlink file; hint: install the pinned loader in the EDL directory or out/"
                )
            absolute = candidate.resolve(strict=True)
            if _hash(absolute) != PINNED_LOADER_SHA256:
                raise store.RefusedError(
                    "pinned Firehose loader SHA-256 mismatch; hint: replace it with the pinned loader before retrying"
                )
            return absolute
        except OSError as exc:
            raise store.RefusedError(
                "could not verify the Firehose loader; hint: check file access and install the pinned loader"
            ) from exc
    raise store.RefusedError(
        "pinned Firehose loader is missing: " + LOADER_NAME + "; hint: install it in OPENSTICK_EDL_DIR or out/"
    )


def _tail(stdout, stderr, limit=12):
    lines = (str(stdout or "") + "\n" + str(stderr or "")).splitlines()
    return "\n".join(lines[-limit:])


def _failure(message, stdout, stderr, verbose):
    detail = message + "\nHint: check that the board is in EDL mode, the USB connection is stable, and the pinned loader matches this device. Re-enter EDL before retrying."
    if verbose:
        tail = _tail(stdout, stderr)
        if tail:
            detail += "\nEDL client output (last lines):\n" + tail
    return EdlRunnerError(detail)


def _checked_client(client):
    if client is None:
        return resolve_client()
    prefix = [os.fspath(item) for item in (client if isinstance(client, (list, tuple)) else [client])]
    if len(prefix) == 1:
        path = Path(prefix[0])
        if not path.is_absolute() or path.is_symlink() or not path.is_file() or not os.access(path, os.X_OK):
            raise store.RefusedError("EDL client must be an absolute executable path")
        return [str(path.resolve())]
    if (len(prefix) == 2 and Path(prefix[0]).resolve() == Path(sys.executable).resolve()
            and Path(prefix[1]).is_absolute() and Path(prefix[1]).is_file()
            and not Path(prefix[1]).is_symlink()):
        return [str(Path(prefix[0]).resolve()), str(Path(prefix[1]).resolve())]
    raise store.RefusedError("EDL script command must be Python plus an absolute regular script path")


def _checked_loader(loader):
    if loader is None:
        return resolve_loader()
    path = Path(loader)
    if path.is_symlink() or not path.is_absolute() or not path.is_file():
        raise store.RefusedError("Firehose loader must be an absolute regular non-symlink file")
    absolute = path.resolve(strict=True)
    if _hash(absolute) != PINNED_LOADER_SHA256:
        raise store.RefusedError("pinned Firehose loader SHA-256 does not match")
    return absolute


def run(args, timeout, *, env=None, cwd=None, verbose=False, debug=False, client=None, loader=None):
    """Run the real client with the verified absolute loader and eMMC mode.

    Callers pass only EDL operation arguments, for example ``["rs", "0",
    "34", output_path]``. The loader and memory arguments are always added by
    this boundary, so an installer call cannot accidentally use auto-selection.
    """
    loader = _checked_loader(loader)
    client = _checked_client(client)
    operation = [os.fspath(item) for item in args]
    if any(item == "--loader" or item.startswith("--loader=") or
           item == "--memory" or item.startswith("--memory=") for item in operation):
        raise store.RefusedError("EDL runner owns the --loader and --memory arguments")
    command = client + operation + ["--loader=" + str(loader), "--memory=emmc"]
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, env=env, cwd=cwd)
    except OSError as exc:
        raise _failure("could not start the EDL client", "", str(exc), verbose or debug) from exc
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            process.send_signal(signal.SIGINT)
        except ProcessLookupError:
            pass
        try:
            stdout, stderr = process.communicate(timeout=INTERRUPT_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                stdout, stderr = process.communicate(timeout=INTERRUPT_GRACE_SECONDS)
            except subprocess.TimeoutExpired as final_timeout:
                # Report a process that ignored both graceful signals and
                # leave system-level cleanup to the caller.
                stdout = final_timeout.stdout or ""
                stderr = final_timeout.stderr or ""
                if isinstance(stdout, bytes):
                    stdout = stdout.decode("utf-8", "replace")
                if isinstance(stderr, bytes):
                    stderr = stderr.decode("utf-8", "replace")
                try:
                    process.stdout.close()
                    process.stderr.close()
                except OSError:
                    pass
                raise EdlRunnerError(
                    "EDL client timed out and ignored SIGINT and terminate; " + POWER_CYCLE_HINT +
                    ("\nEDL client output (last lines):\n" + _tail(stdout, stderr) if (verbose or debug) and _tail(stdout, stderr) else "")
                )
            raise EdlRunnerError(
                "EDL client timed out and did not exit after SIGINT; " + POWER_CYCLE_HINT +
                ("\nEDL client output (last lines):\n" + _tail(stdout, stderr) if (verbose or debug) and _tail(stdout, stderr) else "")
            )
        raise EdlRunnerError(
            "EDL client timed out. " + POWER_CYCLE_HINT +
            ("\nEDL client output (last lines):\n" + _tail(stdout, stderr) if (verbose or debug) and _tail(stdout, stderr) else "")
        )
    if process.returncode != 0:
        raise _failure("EDL client exited with status %d" % process.returncode,
                       stdout, stderr, verbose or debug)
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def parse_sahara_info(text):
    """Parse the mandatory Sahara identity block emitted before loader upload."""
    if not isinstance(text, str):
        raise store.StoreError("EDL client did not report a valid Sahara identity block")
    serial = re.search(r"(?m)^Serial:\s*0x([0-9a-fA-F]{8,16})\s*$", text)
    hwid = re.search(r"(?m)^HWID:\s*0x([0-9a-fA-F]{8,16})(?:\s|$)", text)
    pk_hash = re.search(r"(?m)^PK_HASH:\s*0x([0-9a-fA-F]{64})\s*$", text)
    if not (serial and hwid and pk_hash):
        raise store.StoreError(
            "EDL client did not report a valid Sahara identity block (Serial, HWID, and PK_HASH required). "
            "Hint: confirm EDL mode and use a client that prints the Sahara information block."
        )
    return {
        "serial": serial.group(1).lower(),
        "hwid": hwid.group(1).lower(),
        "pk_hash": pk_hash.group(1).lower(),
    }
