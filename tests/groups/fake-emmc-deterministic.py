import hashlib, pathlib, subprocess, sys
root, tmp = sys.argv[1:]
a, b = pathlib.Path(tmp)/"a.img", pathlib.Path(tmp)/"b.img"
for p in (a, b): subprocess.check_call([sys.executable, str(pathlib.Path(root)/"tests/make-fake-emmc.py"), str(p), "--mib", "2"], stdin=subprocess.DEVNULL, timeout=20)
assert hashlib.sha256(a.read_bytes()).digest() == hashlib.sha256(b.read_bytes()).digest()
assert a.read_bytes()[512:520] == b"EFI PART"
assert "userdata".encode("utf-16le") in a.read_bytes()
