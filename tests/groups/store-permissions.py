import json, os, pathlib, sys
from tools.installer.store import ensure_store, save_registry, load_registry, append_event
root = pathlib.Path(sys.argv[2]) / "private"
ensure_store(root)
assert root.stat().st_mode & 0o777 == 0o700
save_registry(root, {"schema": 1, "devices": [{"id": "DEADBEEF", "label": "test"}]})
assert (root / "registry.json").stat().st_mode & 0o777 == 0o600
assert load_registry(root)["devices"][0]["label"] == "test"
append_event(root, {"type": "test", "device_id": "DEADBEEF"})
assert (root / "events.jsonl").stat().st_mode & 0o777 == 0o600
events = [json.loads(line) for line in (root / "events.jsonl").read_text().splitlines()]
assert events == [{"type": "test", "device_id": "DEADBEEF"}]
