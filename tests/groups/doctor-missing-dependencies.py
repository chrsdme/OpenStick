import os, pathlib, subprocess, sys
root, tmp = sys.argv[1:]
sysfs = pathlib.Path(tmp) / "sysfs"; sysfs.mkdir()
dev = pathlib.Path(tmp) / "dev"; dev.mkdir()
env = dict(os.environ, PATH="/nonexistent", OPENSTICK_EDL_DIR=str(pathlib.Path(tmp) / "absent"))
p = subprocess.run([sys.executable, root + "/openstick", "--usb-sysfs-root", str(sysfs),
                    "--usb-device-root", str(dev), "--home", str(pathlib.Path(tmp) / "doctor"), "doctor"],
                   env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=20)
assert p.returncode == 1
for term in ("python3", "bash", "e2fsprogs", "fastboot", "EDL client", "Firehose loader"):
 assert "FAIL " + term in p.stdout, (term, p.stdout)
