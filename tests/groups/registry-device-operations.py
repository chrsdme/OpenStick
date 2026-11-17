import json, pathlib, subprocess, sys
root, tmp = sys.argv[1:]
home = pathlib.Path(tmp) / "registry"
from tools.installer.store import ensure_store, save_registry
ensure_store(home)
save_registry(home, {"schema": 1, "devices": [{"id": "DEADBEEF", "label": "stick", "note": ""}]})
for args in (("device", "label", "DEADBEEF", "desk"), ("device", "note", "DEADBEEF", "baseline backup")):
 p = subprocess.run([root + "/openstick", "--home", str(home), *args], capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=20)
 assert p.returncode == 0, (p.stdout, p.stderr)
p = subprocess.run([root + "/openstick", "--home", str(home), "device", "show", "DEADBEEF"], capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=20)
d = json.loads(p.stdout)
assert d["label"] == "desk" and d["note"] == "baseline backup"
assert (home / "events.jsonl").read_text().count("\n") == 2
