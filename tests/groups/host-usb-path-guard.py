import os, pathlib, subprocess, sys
root, tmp, requested_parent = sys.argv[1:]
assert pathlib.Path(tmp).resolve().parent == pathlib.Path(requested_parent).resolve()
assert pathlib.Path(tmp).name.startswith("openstick-installer-tests.")
assert pathlib.Path(os.environ["TMPDIR"]).resolve() == pathlib.Path(tmp).resolve()
assert not any(n in os.environ for n in ("INSTALLER_TEST_TMP", "INSTALLER_TEST_TMP_PARENT", "_OSRUNNER_TMP", "_OSRUNNER_NONCE", "_OSRUNNER_PARENT"))
assert pathlib.Path(os.environ["OPENSTICK_USB_SYSFS_ROOT"]).is_relative_to(tmp) and os.environ["OPENSTICK_TEST_USB_ROOTS_DIR"] == tmp
assert not ("OPENSTICK_EDL_DIR" in os.environ or "OPENSTICK_HOME" in os.environ)
assert os.environ.get("GIT_CEILING_DIRECTORIES") == str(pathlib.Path(tmp).parent)
assert not any(name.startswith("GIT_") and name != "GIT_CEILING_DIRECTORIES" for name in os.environ)
assert os.environ["PATH"].split(os.pathsep)[0].endswith("/shims/bin")
assert os.environ.get("LANG")
assert os.environ.get("TMPDIR")
# The deliberate guard hits below go to a private file so they do not trip the runner-wide hit file.
_runner_hits = os.environ["OPENSTICK_GUARD_HIT_FILE"]
selftest_hits = pathlib.Path(tmp) / "guard-selftest-hits"
os.environ["OPENSTICK_GUARD_HIT_FILE"] = str(selftest_hits)
for path in ("/sys/bus/usb/devices", "/dev/bus/usb"):
    try:
        os.open(path, os.O_RDONLY)
    except BaseException as exc:
        assert "TEST SAFETY GUARD" in str(exc), str(exc)
    else:
        raise AssertionError("host USB path access was not blocked")
outside_client = pathlib.Path(root) / "tests/shims/bin/edl"
try:
    subprocess.run([str(outside_client), "rs", "0", "1", str(pathlib.Path(tmp) / "out.bin")],
                   stdin=subprocess.DEVNULL, timeout=5)
except BaseException as exc:
    assert "TEST SAFETY GUARD" in str(exc), str(exc)
else:
    raise AssertionError("EDL client outside the test temp directory was allowed")
try:
    subprocess.run(["sudo", str(outside_client), "rs", "0", "1", str(pathlib.Path(tmp) / "sudo-out.bin")],
                   stdin=subprocess.DEVNULL, timeout=5)
except BaseException as exc:
    assert "TEST SAFETY GUARD" in str(exc), str(exc)
else:
    raise AssertionError("wrapped EDL client outside the test temp directory was allowed")
planner = pathlib.Path(tmp) / "rollback-plan.py"
planner.write_text("raise SystemExit(0)\n", encoding="ascii")
metadata = subprocess.run([sys.executable, "-I", str(planner), "--edl", "/outside/edl-client",
                           "--loader", "/outside/pinned-loader"],
                          stdin=subprocess.DEVNULL, timeout=5, capture_output=True, text=True)
assert metadata.returncode == 0, (metadata.returncode, metadata.stdout, metadata.stderr)
allowed_client = pathlib.Path(tmp) / "edl-child-stub"
allowed_loader = pathlib.Path(tmp) / "synthetic-loader.mbn"
allowed_client = allowed_client.with_suffix(".py")
preserved_pythonpath = str(pathlib.Path(tmp) / "existing-pythonpath")
allowed_client.write_text(
    "#!/usr/bin/env python3\n"
    "import os, pathlib, subprocess, sys\n"
    "assert pathlib.Path(os.environ['TMPDIR']).resolve() == pathlib.Path(__file__).parent.resolve()\n"
    "paths = os.environ['PYTHONPATH'].split(os.pathsep)\n"
    "assert pathlib.Path(paths[0]).name == 'host-path-guard'\n"
    "assert paths[1] == " + repr(preserved_pythonpath) + "\n"
    "if 'nested' not in sys.argv:\n"
    "    env = {'PATH': os.environ['PATH']}\n"
    "    try:\n"
    "        subprocess.run([sys.executable, __file__, 'nested', '--loader=/nonexistent/outside-loader'],\n"
    "                       env=env, stdin=subprocess.DEVNULL, timeout=5)\n"
    "    except BaseException as exc:\n"
    "        sys.exit(0 if 'TEST SAFETY GUARD' in str(exc) else 3)\n"
    "    sys.exit(4)\n"
    "sys.exit(5)\n",
    encoding="utf-8",
)
allowed_client.chmod(0o700)
allowed_loader.write_bytes(b"synthetic-loader")
child_env = {"PATH": os.environ["PATH"], "PYTHONPATH": preserved_pythonpath}
allowed = subprocess.run([str(allowed_client), "rs", "0", "1", str(pathlib.Path(tmp) / "allowed.bin"),
                          "--loader=" + str(allowed_loader)], env=child_env,
                         stdin=subprocess.DEVNULL, timeout=5, capture_output=True, text=True)
assert allowed.returncode == 0, (allowed.returncode, allowed.stdout, allowed.stderr)
try:
    subprocess.run([str(allowed_client), "rs", "0", "1", str(pathlib.Path(tmp) / "outside-loader.bin"),
                    "--loader=/nonexistent/outside-loader"], env=child_env,
                   stdin=subprocess.DEVNULL, timeout=5)
except BaseException as exc:
    assert "TEST SAFETY GUARD" in str(exc), str(exc)
else:
    raise AssertionError("EDL loader outside the test temp directory was allowed")
child_code = (
    "import subprocess, sys\n"
    "try:\n"
    "    subprocess.run([sys.argv[1], 'rs', '0', '1', sys.argv[2], '--loader=' + sys.argv[3]],\n"
    "                   stdin=subprocess.DEVNULL, timeout=5)\n"
    "except BaseException as exc:\n"
    "    sys.exit(0 if 'TEST SAFETY GUARD' in str(exc) else 3)\n"
    "sys.exit(4)\n"
)
inside_client = pathlib.Path(tmp) / "shims/bin/edl"
child = subprocess.run([sys.executable, "-c", child_code, str(inside_client), str(pathlib.Path(tmp) / "child.bin"), "/nonexistent/outside-loader"],
                       stdin=subprocess.DEVNULL, timeout=10, capture_output=True, text=True)
assert child.returncode == 0, (child.returncode, child.stdout, child.stderr)
os.environ["OPENSTICK_GUARD_HIT_FILE"] = _runner_hits
hit_text = selftest_hits.read_text()
assert "USB-PATH" in hit_text and "EDL-CLIENT" in hit_text and "EDL-LOADER" in hit_text, hit_text
env = dict(os.environ)
env.pop("OPENSTICK_EDL_DIR", None)
p = subprocess.run([sys.executable, str(pathlib.Path(root) / "openstick"),
                    "--home", str(pathlib.Path(tmp) / "default-roots"), "doctor"],
                   env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=20)
assert "TEST SAFETY GUARD" not in p.stdout + p.stderr, (p.stdout, p.stderr)
assert "no EDL device attached" in p.stdout, p.stdout
child = subprocess.run([sys.executable, "-c",
                       "from tools.installer.edl_runner import preflight; assert preflight().state == 'ABSENT'"],
                      env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=20)
assert child.returncode == 0, (child.stdout, child.stderr)
