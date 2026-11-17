"""Documented limits of the EDL guard, run on purpose with the outcome asserted.

These children are NOT seen by the guard (python -I / -S do not load
sitecustomize or PYTHONPATH; env -i without PYTHONPATH loses it; a shell
script's inner commands are not parsed). The probe is a harmless script named
like an EDL client, outside the test temp dir, that only writes a marker file.
If one of these cases ever becomes blocked, this test fails so the document in
tests/host-path-guard/path_guard.py can be updated.
"""
import os, pathlib, shutil, subprocess, sys, tempfile
root, tmp, parent = sys.argv[1:4]
tmp = pathlib.Path(tmp)
_runner_hits = os.environ["OPENSTICK_GUARD_HIT_FILE"]
hits = tmp / "guard-limits-hits"
os.environ["OPENSTICK_GUARD_HIT_FILE"] = str(hits)
outside = pathlib.Path(tempfile.mkdtemp(prefix="guard-limits-", dir=parent))
try:
    marker = tmp / "probe-ran"
    probe = outside / "edl-limit-probe.py"
    probe.write_text("#!/usr/bin/env python3\nimport pathlib, sys\n"
                     "pathlib.Path(sys.argv[1]).write_text('ran')\nraise SystemExit(7)\n", encoding="ascii")
    probe.chmod(0o700)
    py = sys.executable
    # Baseline: a direct call from the guarded parent IS blocked and logged.
    try:
        subprocess.run([str(probe), str(marker)], stdin=subprocess.DEVNULL, timeout=10)
    except BaseException:
        pass
    else:
        raise AssertionError("baseline direct call was not blocked")
    assert not marker.exists()
    baseline_hits = len(hits.read_text().splitlines())
    assert baseline_hits == 1, baseline_hits
    shprobe = outside / "edl-limit-probe.sh"
    shprobe.write_text("#!/bin/sh\nprintf ran > \"$1\"\nexit 7\n", encoding="ascii")
    shprobe.chmod(0o700)
    def code_for(target):
        return "import subprocess, sys; sys.exit(subprocess.run([%r, %r]).returncode)" % (str(target), str(marker))
    shell = tmp / "limit-inner.sh"
    shell.write_text("#!/bin/sh\n%s %s\n" % (shprobe, marker), encoding="ascii")
    shell.chmod(0o700)
    cases = {
        "python -I child starting a non-Python client": [py, "-I", "-c", code_for(shprobe)],
        "python -S child starting a non-Python client": [py, "-S", "-c", code_for(shprobe)],
        "env -i child (no PYTHONPATH) starting a Python client": ["env", "-i", "PATH=/usr/bin:/bin", py, "-c", code_for(probe)],
        "shell script inner command": [str(shell)],
    }
    for label, argv in cases.items():
        if marker.exists():
            marker.unlink()
        r = subprocess.run(argv, stdin=subprocess.DEVNULL, timeout=20, capture_output=True, text=True)
        # Outcome stated explicitly: the child ran the outside client; the guard did not see it.
        assert r.returncode == 7, (label, r.returncode, r.stdout, r.stderr)
        assert marker.exists(), label
        assert len(hits.read_text().splitlines()) == baseline_hits, label
        print("LIMIT-DEMONSTRATED (unguarded, documented): " + label)
    # Partial cover: python -I does not load the guard itself, but it does not clear the
    # environment, so a PYTHON EDL client it starts still loads the guard through the inherited
    # PYTHONPATH and refuses itself (guard_current_edl_script). Not true for env -i (above).
    if marker.exists():
        marker.unlink()
    r = subprocess.run([py, "-I", "-c", code_for(probe)], stdin=subprocess.DEVNULL, timeout=20,
                       capture_output=True, text=True)
    assert r.returncode != 7 and not marker.exists(), (r.returncode, r.stderr)
    assert len(hits.read_text().splitlines()) == baseline_hits + 1
    print("LIMIT-PARTIAL (python -I child, Python client refuses itself via inherited PYTHONPATH)")
finally:
    os.environ["OPENSTICK_GUARD_HIT_FILE"] = _runner_hits
    shutil.rmtree(outside, ignore_errors=True)
