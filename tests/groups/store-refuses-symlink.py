import pathlib, sys
from tools.installer.store import ensure_store, RefusedError
tmp = pathlib.Path(sys.argv[2]); target = tmp / "target"; target.mkdir()
link = tmp / "linked"; link.symlink_to(target, target_is_directory=True)
refused = False
try: ensure_store(link / "store")
except RefusedError: refused = True
assert refused, "symlink path was accepted"
assert not (target / "store").exists(), "store was created through the symlink"
