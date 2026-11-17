#!/usr/bin/env python3
"""Run one test group in its own process group with a timeout and a check count.

Usage: run-group.py --count-file F [--timeout S] [--grace S] [--exit-check] -- CMD...
Exit codes: the group's own code, 242 = timeout (TERM then KILL of the whole
process group), 243 = group exited 0 but left processes behind (killed).
Check count: assert count from tests/groups/_harness.py, else unittest "Ran N
tests", else lines starting with OK/ok/PASS; --exit-check counts the exit
status as one check.
"""
import argparse
import os
import re
import select
import signal
import subprocess
import sys
import time

TIMEOUT_RC = 242
ORPHAN_RC = 243


def alive(pgid):
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def kill_group(pgid, grace):
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    end = time.monotonic() + grace
    while time.monotonic() < end and alive(pgid):
        time.sleep(0.05)
    if alive(pgid):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        for _ in range(100):
            if not alive(pgid):
                break
            time.sleep(0.05)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--count-file", required=True)
    ap.add_argument("--timeout", type=float, default=300)
    ap.add_argument("--grace", type=float, default=5)
    ap.add_argument("--exit-check", action="store_true")
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    args = ap.parse_args()
    cmd = args.cmd[1:] if args.cmd and args.cmd[0] == "--" else args.cmd
    harness_file = args.count_file + ".harness"
    env = dict(os.environ, OPENSTICK_CHECK_COUNT_FILE=harness_file)
    try:
        os.unlink(harness_file)
    except FileNotFoundError:
        pass
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, env=env, start_new_session=True)
    pgid = proc.pid
    deadline = time.monotonic() + args.timeout
    buf = b""
    timed_out = False
    fd = proc.stdout.fileno()
    eof = False
    while not eof:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            break
        ready, _, _ = select.select([fd], [], [], min(remaining, 0.5))
        if ready:
            chunk = os.read(fd, 65536)
            if not chunk:
                eof = True
            else:
                buf += chunk
                sys.stdout.buffer.write(chunk)
                sys.stdout.buffer.flush()
        elif proc.poll() is not None and not alive(pgid):
            eof = True
    if timed_out:
        kill_group(pgid, args.grace)
        proc.wait()
        rc = TIMEOUT_RC
    else:
        rc = proc.wait()
        if alive(pgid):
            kill_group(pgid, args.grace)
            sys.stdout.write("run-group: processes left behind in the group were killed\n")
            if rc == 0:
                rc = ORPHAN_RC
    # drain anything still buffered
    try:
        os.set_blocking(fd, False)
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            buf += chunk
            sys.stdout.buffer.write(chunk)
    except (BlockingIOError, OSError):
        pass
    sys.stdout.flush()
    text = buf.decode("utf-8", "replace")
    count = 0
    if os.path.exists(harness_file):
        count = int(open(harness_file).read().strip() or 0)
    else:
        ran = re.findall(r"^Ran (\d+) tests?", text, re.M)
        if ran:
            count = sum(int(x) for x in ran)
        else:
            count = len(re.findall(r"^(?:OK|ok|PASS)\b", text, re.M))
    if args.exit_check and count == 0 and rc == 0:
        count = 1
    with open(args.count_file, "w", encoding="ascii") as out:
        out.write(str(count))
    return rc


if __name__ == "__main__":
    sys.exit(main())
