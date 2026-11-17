"""easy: custom mode values (hostname, Wi-Fi, APN, SSH key, profile) must land in the saved seed."""
import base64
import pathlib, re, sys
root, tmp = sys.argv[1:3]
sys.path.insert(0, str(pathlib.Path(root) / "tests"))
import easy_support as E

sys.path.insert(0, root)
from tools.installer import seed as seed_module

board = E.Board(root, tmp, "custom")
board.enable_fake_build(real_rootfs=True)   # custom mode injects the seed into the rootfs with debugfs
key_blob = (len(b"ssh-ed25519").to_bytes(4, "big") + b"ssh-ed25519" + (32).to_bytes(4, "big") + bytes(range(32)))
key_line = "ssh-ed25519 " + base64.b64encode(key_blob).decode() + " easy-test"
ssh_dir = board.base / "home/.ssh"
ssh_dir.mkdir(parents=True)
(ssh_dir / "id_easy_test.pub").write_text(key_line + "\n")
VALUES = {"host": "easy-test-host", "ssid": "EasyTestNet", "psk": "easy-test-psk-123", "apn": "easy.test.apn", "profile": "prod"}
rules = E.base_rules(board, mode="2") + [
    E.Rule(r"Profile dev or prod", VALUES["profile"]),
    E.Rule(r"Hostname", VALUES["host"]),
    E.Rule(r"Wi-Fi SSID", VALUES["ssid"]),
    E.Rule(r"(?i)(?<!confirm )wi-fi password", VALUES["psk"]),
    E.Rule(r"(?i)confirm wi-fi password", VALUES["psk"]),
    E.Rule(r"Cellular APN", VALUES["apn"]),
    E.Rule(r"Use SSH public key", "y"),
]
rc, out = E.happy(board, [], rules)
assert rc == 0, "easy custom mode failed (rc=%s):\n%s" % (rc, out[-2500:])
E.check_no_traceback(out)
assert len(board.build_calls()) == 1 and "profile=prod" in board.build_calls()[0], "the chosen profile must reach the build"
seeds = board.seeds()
assert len(seeds) == 1, "expected exactly one saved seed, found %d" % len(seeds)
text = seeds[0].read_text()

def check_seed(text):
    try:
        data = seed_module.validate(seed_module.parse(text))
    except seed_module.SeedError as exc:
        raise AssertionError("seed is invalid: %s" % exc)
    def dec(key):
        assert key in data, "seed lacks " + key
        return base64.b64decode(data[key]).decode()
    assert data.get("HOSTNAME") == VALUES["host"]
    assert dec("WIFI_SSID_B64") == VALUES["ssid"]
    assert dec("WIFI_PSK_B64") == VALUES["psk"]
    assert dec("APN_B64") == VALUES["apn"]
    assert base64.b64decode(data.get("SSH_AUTHORIZED_KEYS", "")).decode().strip() == key_line
    assert data.get("PROFILE") == VALUES["profile"]

check_seed(text)
def psk_not_echoed(output):
    assert VALUES["psk"] not in output, "the Wi-Fi key was echoed to the console"
psk_not_echoed(out)

# The personalised rootfs (what is flashed to userdata) must differ from the generic one and carry the seed file.
import subprocess
personal = sorted((board.home / "builds").glob("*/personal-*/rootfs.img"))
assert len(personal) == 1, "no personalised rootfs found"
dump = subprocess.run(["debugfs", "-R", "cat /etc/openstick/seed.env", str(personal[0])], capture_output=True, text=True, stdin=subprocess.DEVNULL)
assert dump.stdout == text, "the seed stored in the flashed rootfs differs from the saved seed"
sets = board.product_image_sets()
assert len(sets) == 1
E.check_layout(board.disk, board.initial, E.expected_overlays(sets[0], rootfs=personal[0]))

# Mutation proofs: break one value at a time; each must be caught.
E.must_fail(check_seed, text.replace(VALUES["host"], "other-host"))
E.must_fail(check_seed, text.replace("PROFILE=prod", "PROFILE=dev"))
E.must_fail(check_seed, re.sub(r"WIFI_SSID_B64=\S+", "WIFI_SSID_B64=" + base64.b64encode(b"WrongNet").decode(), text))
E.must_fail(check_seed, "\n".join(l for l in text.splitlines() if not l.startswith("APN_B64=")) + "\n")
E.must_fail(psk_not_echoed, out + VALUES["psk"])
E.must_fail(E.check_layout, board.disk, board.initial, E.expected_overlays(sets[0]))  # generic rootfs would not match
