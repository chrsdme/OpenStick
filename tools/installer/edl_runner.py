"""Safe process boundary for the external Qualcomm EDL client."""
import hashlib
import os
import re
import shutil
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from . import store


PINNED_LOADER_SHA256 = "959439aa5864685999b713c3ed12ad5fa408149648b670a9a9ef77bcc9dcab14"
LOADER_NAME = "prog_emmc_firehose_8916.mbn"
IDENTITY_READ_TIMEOUT_SECONDS = 60
RANGE_READ_TIMEOUT_SECONDS = 60
FULL_READ_TIMEOUT_SECONDS = 3600
INTERRUPT_GRACE_SECONDS = 10
POWER_CYCLE_HINT = (
    "The board may be in a half-open EDL session. Unplug USB for 30 s and "
    "reconnect WITHOUT the button, then re-enter EDL."
)


class EdlRunnerError(store.StoreError):
    """An EDL invocation failed or exceeded its safe timeout."""

    def __init__(self, message, stdout="", stderr=""):
        super().__init__(message)
        self.stdout = stdout or ""
        self.stderr = stderr or ""


class EDLSessionError(store.RefusedError):
    """The current USB/EDL session cannot safely run the requested command."""


class AbsentDeviceError(EDLSessionError):
    pass


class NormalModeError(EDLSessionError):
    pass


class ConsumedSessionError(EDLSessionError):
    pass


class HungSessionError(EDLSessionError):
    pass


class BoardSwapError(EDLSessionError):
    pass


@dataclass(frozen=True)
class PreflightResult:
    state: str
    product_id: str = ""
    busnum: str = ""
    devnum: str = ""


def preflight(sysfs_root="/sys/bus/usb/devices"):
    """Inspect fixture-injectable USB sysfs metadata without opening /dev nodes."""
    if sysfs_root is None:
        return PreflightResult("UNKNOWN")
    root = Path(sysfs_root)
    try:
        entries = sorted(root.iterdir(), key=lambda item: item.name)
    except OSError:
        return PreflightResult("UNKNOWN")
    found_normal = None
    try:
        for entry in entries:
            vendor_file = entry / "idVendor"
            product_file = entry / "idProduct"
            if not vendor_file.is_file() or not product_file.is_file():
                continue
            vendor = vendor_file.read_text(encoding="ascii").strip().lower()
            product = product_file.read_text(encoding="ascii").strip().lower()
            if vendor != "05c6":
                continue
            if product == "9008":
                bus_file, dev_file = entry / "busnum", entry / "devnum"
                busnum = bus_file.read_text(encoding="ascii").strip() if bus_file.is_file() else ""
                devnum = dev_file.read_text(encoding="ascii").strip() if dev_file.is_file() else ""
                return PreflightResult("EDL-PRESENT", product, busnum, devnum)
            if found_normal is None:
                found_normal = product
    except (OSError, UnicodeError):
        return PreflightResult("UNKNOWN")
    if found_normal is not None:
        return PreflightResult("NORMAL-MODE", found_normal)
    return PreflightResult("ABSENT")


def _has_sahara(text):
    return "Mode detected: sahara" in (text or "") and "Serial:" in (text or "")


def _has_firehose(text):
    return "Mode detected: firehose" in (text or "")


def _has_device(text):
    return "Device detected :)" in (text or "")


def _absent_error():
    return AbsentDeviceError(
        "No Qualcomm EDL device (05c6:9008) is connected. Put the board in EDL mode, then run this command again as the first EDL command of that session."
    )


class Session:
    """Own identity and USB bus/devnum for one top-level installer command."""

    def __init__(self, *, sysfs_root="/sys/bus/usb/devices", client=None,
                 loader=None, env=None):
        self.sysfs_root = sysfs_root
        self.client = client
        self.loader = loader
        self.env = env
        self.identity = None
        self._bus_key = None
        self.invocations = 0

    def _before_call(self):
        observed = preflight(self.sysfs_root)
        if self._bus_key is None:
            if observed.state == "ABSENT":
                raise _absent_error()
            if observed.state == "NORMAL-MODE":
                raise NormalModeError(
                    "The board is connected in normal mode (05c6:%s), not EDL. Enter EDL mode, then run this command again." % observed.product_id
                )
            if observed.state == "EDL-PRESENT" and observed.busnum and observed.devnum:
                self._bus_key = (observed.busnum, observed.devnum)
            return observed
        if observed.state == "EDL-PRESENT" and observed.busnum and observed.devnum:
            if (observed.busnum, observed.devnum) != self._bus_key:
                raise BoardSwapError(
                    "The 05c6:9008 USB bus/device number changed during this command; possible board swap. Stop and reconnect the original board before retrying."
                )
        elif observed.state != "UNKNOWN":
            raise BoardSwapError(
                "The original 05c6:9008 USB device disappeared during this command; possible board swap. Stop and reconnect the original board before retrying."
            )
        return observed

    def run(self, args, timeout, *, env=None, verbose=False, debug=False):
        self._before_call()
        self.invocations += 1
        try:
            result = run(args, timeout=timeout, env=env if env is not None else self.env, verbose=verbose,
                         debug=debug, client=self.client, loader=self.loader)
        except EdlRunnerError as exc:
            transcript = exc.stdout + "\n" + exc.stderr
            if self.identity is None and _has_sahara(transcript) and not _has_firehose(transcript):
                raise HungSessionError(
                    "The EDL client started the handshake but the board stopped responding (half-open session). Unplug USB for 30 s, reconnect WITHOUT the button, then re-enter EDL."
                ) from exc
            if self.identity is None and "Waiting for the device" in transcript and not _has_device(transcript):
                raise _absent_error() from exc
            if self.identity is None and _has_firehose(transcript) and not _has_sahara(transcript):
                raise ConsumedSessionError(
                    "The board is in EDL, but its Sahara session was already used by an earlier command, so its identity cannot be read again. Unplug USB for 30 s, reconnect WITHOUT the button, re-enter EDL, and run this command first."
                ) from exc
            raise
        transcript = result.stdout + "\n" + result.stderr
        if self.identity is None:
            if "Waiting for the device" in transcript and not _has_device(transcript):
                raise _absent_error()
            if _has_firehose(transcript) and not _has_sahara(transcript):
                raise ConsumedSessionError(
                    "The board is in EDL, but its Sahara session was already used by an earlier command, so its identity cannot be read again. Unplug USB for 30 s, reconnect WITHOUT the button, re-enter EDL, and run this command first."
                )
            if not _has_sahara(transcript):
                raise store.StoreError(
                    "EDL client output did not identify a fresh Sahara session. Hint: confirm EDL mode and use a client that prints the Sahara information block."
                )
            self.identity = parse_sahara_info(transcript)
        elif _has_firehose(transcript):
            pass
        elif _has_sahara(transcript):
            current = parse_sahara_info(transcript)
            if current != self.identity:
                raise BoardSwapError(
                    "The EDL client reported a different identity during this command; possible board swap. Stop and reconnect the original board before retrying."
                )
        else:
            raise store.StoreError("EDL client did not report firehose mode for a later session invocation")
        return result


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


def _tail(stdout, stderr="", limit=12):
    lines = (str(stdout or "") + "\n" + str(stderr or "")).splitlines()
    tail = lines[-limit:]
    def mask_serial(match):
        value = match.group(2)
        return match.group(1) + value[:2] + "*" * max(0, len(value) - 4) + value[-2:]
    return "\n".join(re.sub(r"(?m)(^Serial:\s*0x)([0-9a-fA-F]+)", mask_serial, line)
                     for line in tail)


def _failure(message, stdout, stderr, verbose):
    detail = message + "\nHint: check that the board is in EDL mode, the USB connection is stable, and the pinned loader matches this device. Re-enter EDL before retrying."
    if verbose:
        tail = _tail(stdout, stderr)
        if tail:
            detail += "\nEDL client output (last lines):\n" + tail
    return EdlRunnerError(detail, stdout, stderr)


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
                    ("\nEDL client output (last lines):\n" + _tail(stdout, stderr) if (verbose or debug) and _tail(stdout, stderr) else ""),
                    stdout, stderr,
                )
            raise EdlRunnerError(
                "EDL client timed out and did not exit after SIGINT; " + POWER_CYCLE_HINT +
                ("\nEDL client output (last lines):\n" + _tail(stdout, stderr) if (verbose or debug) and _tail(stdout, stderr) else ""),
                stdout, stderr,
            )
        raise EdlRunnerError(
            "EDL client timed out. " + POWER_CYCLE_HINT +
            ("\nEDL client output (last lines):\n" + _tail(stdout, stderr) if (verbose or debug) and _tail(stdout, stderr) else ""),
            stdout, stderr,
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
