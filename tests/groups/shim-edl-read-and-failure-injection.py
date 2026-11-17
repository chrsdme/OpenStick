import os, pathlib, subprocess, sys
root, tmp = sys.argv[1:]
tmp = pathlib.Path(tmp); disk = tmp / "disk.img"; out = tmp / "read.bin"
subprocess.check_call([sys.executable, str(pathlib.Path(root)/"tests/make-fake-emmc.py"), str(disk), "--mib", "2"], stdin=subprocess.DEVNULL, timeout=20)
loader = tmp / "synthetic-loader.mbn"; loader.write_bytes(b"test-only shim loader")
env = dict(os.environ, OPENSTICK_FAKE_EMMC=str(disk), PATH=str(tmp/"shims/bin") + ":/usr/bin:/bin")
subprocess.check_call(["edl", "rs", "0", "2", str(out), "--loader=" + str(loader), "--memory=emmc"], env=env, stdin=subprocess.DEVNULL, timeout=20)
assert out.stat().st_size == 1024
env["OPENSTICK_EDL_CALL_FILE"] = str(tmp / "calls"); env["OPENSTICK_EDL_FAIL_AT"] = "1"
p = subprocess.run(["edl", "rs", "0", "2", str(tmp / "failed.bin"), "--loader=" + str(loader), "--memory=emmc"], env=env, capture_output=True, stdin=subprocess.DEVNULL, timeout=20)
assert p.returncode == 90 and b"injected failure" in p.stderr
