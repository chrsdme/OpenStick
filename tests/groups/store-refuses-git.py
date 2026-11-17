import os, pathlib, subprocess, sys
root, tmp = sys.argv[1:]
repo = pathlib.Path(tmp) / "synthetic-repo"
(repo / ".git").mkdir(parents=True)
env = dict(os.environ)
env.pop("GIT_CEILING_DIRECTORIES", None)
p = subprocess.run([root + "/openstick", "--home", str(repo / "store"), "device", "list"],
                   text=True, capture_output=True, env=env, stdin=subprocess.DEVNULL, timeout=20)
assert p.returncode == 2, (p.returncode, p.stdout, p.stderr)
assert "REFUSED" in p.stderr
