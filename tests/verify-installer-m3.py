#!/usr/bin/env python3
"""Offline acceptance checks for M3. Uses synthetic credentials and fake roots only."""
import base64
import hashlib
import json
import os
import pathlib
import pty
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import traceback
from unittest import mock

ROOT = pathlib.Path(sys.argv[1]).resolve()
sys.path.insert(0, str(ROOT))
from tools.installer import seed
from tools.installer.configure import configure, show
from tools.installer.store import ensure_store

PASS = 0
FAIL = 0


def check(name, fn):
    global PASS, FAIL
    try:
        fn()
        PASS += 1
        print("PASS " + name)
    except Exception as exc:
        FAIL += 1
        print("FAIL %s: %s: %s" % (name, type(exc).__name__, exc))
        traceback.print_exc()


def fixture_seed():
    algorithm = b"ssh-ed25519"
    key_blob = struct.pack(">I", len(algorithm)) + algorithm + struct.pack(">I", 32) + (b"K" * 32)
    key = "ssh-ed25519 " + base64.b64encode(key_blob).decode() + " fixture"
    return {
        "SEED_VERSION": "1", "HOSTNAME": "stick-{serial8}", "TIMEZONE": "Etc/UTC",
        "SSH_AUTHORIZED_KEYS": base64.b64encode((key + "\n").encode()).decode(),
        "ROOT_PASSWORD_HASH": "$6$synthetic-salt$" + "a" * 86,
        "DEFAULT_USER": "tester", "USER_PASSWORD_HASH": "$6$synthetic-salt$" + "b" * 86,
        "WIFI_SSID": "test-net", "WIFI_PSK": "test-psk-not-real", "WIFI_COUNTRY": "GB",
        "APN": "test.apn", "APN_USER": "testuser", "APN_PASSWORD": "synthetic-apn-secret",
        "USB_NET_ADDR": "172.16.42.1/24", "NTP_SERVERS": "time.example,ntp.example", "LED_STATUS": "on",
    }


def parser_test():
    content = seed.serialize(fixture_seed())
    assert seed.parse(content) == fixture_seed()
    for bad in (content + "UNKNOWN=1\n", "SEED_VERSION=1\nHOSTNAME=good\nHOSTNAME=bad\n", "SEED_VERSION=1\nHOSTNAME=x\n$(touch /tmp/nope)", "SEED_VERSION=1\nHOSTNAME=x\nAPN=a\nHOSTNAME=y\n", "SEED_VERSION=1\rHOSTNAME=bad\r"):
        try: seed.parse(bad)
        except seed.SeedError: pass
        else: raise AssertionError("invalid seed was accepted")


def configure_test(tmp):
    home = ensure_store(tmp / "config-home")
    answers = iter(["testhost", "Etc/UTC", "", "", "", "internet", "", "", ""])
    # dev defaults ask for SSH source, root password, account, account password, Wi-Fi, APN details.
    with mock.patch("builtins.input", side_effect=lambda _p: next(answers)), mock.patch("getpass.getpass", side_effect=["synthetic-login", "synthetic-login", "synthetic-user", "synthetic-user", ""]):
        configure(home, "private", "dev")
    content = (home / "seeds/private.seed").read_text()
    assert "synthetic-login" not in content and "synthetic-user" not in content
    assert "$6$" in content and (home / "seeds/private.seed").stat().st_mode & 0o777 == 0o600
    # show always masks values and never reveals the encoded public key payload.
    with mock.patch("builtins.print") as printer:
        show(home, "private")
    output = "\n".join(str(c.args[0]) for c in printer.call_args_list)
    assert "synthetic-login" not in output and "synthetic-user" not in output and "[REDACTED]" in output
    shown = subprocess.run([str(ROOT / "openstick"), "--home", str(home), "configure", "--seed", "private", "--show"], capture_output=True, text=True)
    assert shown.returncode == 0 and "[REDACTED]" in shown.stdout and "synthetic-login" not in shown.stdout
    assert home.stat().st_mode & 0o777 == 0o700 and (home / "seeds").stat().st_mode & 0o777 == 0o700
    private_file = tmp / "synthetic-private-key"
    private_file.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nfake\n-----END OPENSSH PRIVATE KEY-----\n")
    key_answers = iter(["host", "Etc/UTC", str(private_file)])
    try:
        with mock.patch("builtins.input", side_effect=lambda _p: next(key_answers)):
            configure(home, "must-not-exist", "prod")
    except Exception as exc:
        assert "private key" in str(exc)
    else:
        raise AssertionError("private key import was accepted")
    assert not (home / "seeds/must-not-exist.seed").exists()
    assert not (home / "registry.json").exists() and not (home / "events.jsonl").exists()
    alg = b"ssh-ed25519"
    blob = struct.pack(">I", len(alg)) + alg + struct.pack(">I", 32) + (b"P" * 32)
    public = tmp / "synthetic-public-key.pub"
    public.write_text("ssh-ed25519 %s fixture\n" % base64.b64encode(blob).decode())
    pub_answers = iter(["pubhost", "Etc/UTC", str(public), "openstick", "", "", "", "", ""])
    with mock.patch("builtins.input", side_effect=lambda _p: next(pub_answers)), mock.patch("getpass.getpass", return_value=""):
        configure(home, "pub", "dev")
    imported = seed.parse((home / "seeds/pub.seed").read_text())
    assert imported["SSH_AUTHORIZED_KEYS"] == base64.b64encode(public.read_bytes()).decode()


def g4_test(tmp):
    home = ensure_store(tmp / "g4-home")
    inputs = iter(["prodhost", "Etc/UTC", "", "CONFIRM", "", "internet", "", "", ""])
    with mock.patch("builtins.input", side_effect=lambda _p: next(inputs)), mock.patch("getpass.getpass", return_value=""):
        configure(home, "prod", "prod")
    assert (home / "seeds/prod.seed").exists()
    refused = ensure_store(tmp / "g4-refused-home")
    declined = iter(["prodhost", "Etc/UTC", "", "NO"])
    try:
        with mock.patch("builtins.input", side_effect=lambda _p: next(declined)):
            configure(refused, "prod", "prod")
    except Exception as exc:
        assert "explicit confirmation" in str(exc)
    else:
        raise AssertionError("G4 refusal was not enforced")
    assert not (refused / "seeds/prod.seed").exists()


def fake_root(tmp):
    root = tmp / "fake-root"
    for rel in ("etc/openstick", "etc/ssh", "etc/NetworkManager/system-connections", "etc/NetworkManager/conf.d", "etc/systemd/network", "etc/systemd/timesyncd.conf.d", "proc", "tmp", "root", "usr/share/zoneinfo/Etc", "home"):
        (root / rel).mkdir(parents=True, exist_ok=True)
    (root / "proc/cpuinfo").write_text("Processor: 0\nSerial: 0123456789ABCDEF\n")
    (root / "usr/share/zoneinfo/Etc/UTC").write_text("fixture")
    bindir = tmp / "bin"
    bindir.mkdir()
    log = tmp / "shim.log"
    for name, body in {
        "logger": "printf '%s\\n' \"$*\" >> \"$SHIM_LOG\"\n",
        "chpasswd": "cat >/dev/null; echo chpasswd-called >> \"$SHIM_LOG\"\n",
        "id": "exit 1\n",
        "useradd": "printf '%s\\n' \"$*\" >> \"$SHIM_LOG\"\n",
        "chown": "exit 0\n",
        "hostnamectl": "printf 'hostnamectl %s\\n' \"$*\" >> \"$SHIM_LOG\"\n",
        "timedatectl": "printf 'timedatectl %s\\n' \"$*\" >> \"$SHIM_LOG\"\n",
        "ssh-keygen": "exit 0\n",
    }.items():
        path = bindir / name
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(0o755)
    return root, bindir, log


def apply_test(tmp):
    root, bindir, log = fake_root(tmp / "apply")
    data = fixture_seed()
    seedfile = root / "etc/openstick/seed.env"
    seedfile.write_text(seed.serialize(data))
    seedfile.chmod(0o600)
    env = dict(os.environ, OPENSTICK_ROOT=str(root), PATH=str(bindir) + ":/usr/bin:/bin", SHIM_LOG=str(log))
    p = subprocess.run([str(ROOT / "configs/apply-seed")], env=env, text=True, capture_output=True)
    assert p.returncode == 0 and "secret" not in p.stdout + p.stderr
    assert not seedfile.exists(), "apply rejected: " + (log.read_text() if log.exists() else "no journal output")
    assert (root / "etc/hostname").read_text().strip() == "stick_89abcdef" or (root / "etc/hostname").read_text().strip() == "stick-89abcdef"
    assert (root / "etc/openstick/seed.applied").read_text().strip() == hashlib.sha256(seed.serialize(data).encode()).hexdigest()
    assert (root / "etc/openstick/seed.applied").stat().st_mode & 0o777 == 0o600, "receipt mode"
    assert (root / "home/tester/.ssh").stat().st_mode & 0o777 == 0o700, "ssh directory mode"
    assert (root / "home/tester/.ssh/authorized_keys").stat().st_mode & 0o777 == 0o600, "authorized_keys mode"
    assert (root / "etc/NetworkManager/system-connections/wifi-client.nmconnection").stat().st_mode & 0o777 == 0o600, "wifi mode"
    assert (root / "etc/NetworkManager/system-connections/lte.nmconnection").stat().st_mode & 0o777 == 0o600, "lte mode"
    assert "apn=test.apn" in (root / "etc/NetworkManager/system-connections/lte.nmconnection").read_text()
    assert "Address=172.16.42.1/24" in (root / "etc/systemd/network/10-usb0.network").read_text()
    assert "NTP=time.example ntp.example" in (root / "etc/systemd/timesyncd.conf.d/20-openstick.conf").read_text()
    assert "ssid=test-net" in (root / "etc/NetworkManager/system-connections/wifi-client.nmconnection").read_text()
    assert "ssh-ed25519" in (root / "home/tester/.ssh/authorized_keys").read_text()
    assert log.read_text().count("chpasswd-called") == 2 and "tester" in log.read_text()
    assert not seedfile.exists()
    assert "synthetic-apn-secret" not in p.stdout + p.stderr + log.read_text()
    assert fixture_seed()["ROOT_PASSWORD_HASH"] not in p.stdout + p.stderr + log.read_text()
    assert fixture_seed()["USER_PASSWORD_HASH"] not in p.stdout + p.stderr + log.read_text()
    assert "hostnamectl --root" in log.read_text() and "timedatectl --root" in log.read_text()
    assert "reserved LED field ignored" in log.read_text() and not (root / "sys/class/leds").exists()
    # Recreate identical pending input and apply a second time; all resulting files stay stable.
    seedfile.write_text(seed.serialize(data))
    seedfile.chmod(0o600)
    p = subprocess.run([str(ROOT / "configs/apply-seed")], env=env, text=True, capture_output=True)
    assert p.returncode == 0 and not seedfile.exists()


def invalid_apply_test(tmp):
    root, bindir, log = fake_root(tmp / "invalid")
    seedfile = root / "etc/openstick/seed.env"
    seedfile.write_text("SEED_VERSION=1\nHOSTNAME=would-have-changed\nUNKNOWN=x\n")
    seedfile.chmod(0o600)
    env = dict(os.environ, OPENSTICK_ROOT=str(root), PATH=str(bindir) + ":/usr/bin:/bin", SHIM_LOG=str(log))
    p = subprocess.run([str(ROOT / "configs/apply-seed")], env=env, text=True, capture_output=True)
    assert p.returncode == 0 and seedfile.exists() and not (root / "etc/hostname").exists()
    assert not (root / "etc/openstick/seed.applied").exists()
    (root / "etc/openstick/seed.env").write_text("SEED_VERSION=1\nHOSTNAME=would-change\nAPN=$(touch %s)\n" % (tmp / "shell-executed"))
    (root / "etc/openstick/seed.env").chmod(0o600)
    p = subprocess.run([str(ROOT / "configs/apply-seed")], env=env, text=True, capture_output=True)
    assert p.returncode == 0 and not (root / "etc/hostname").exists() and not (tmp / "shell-executed").exists()


def secret_redaction_test(tmp):
    home = ensure_store(tmp / "redaction-home")
    secret = "synthetic-exception-secret"
    (home / "seeds/leak.seed").write_text("SEED_VERSION=1\nWIFI_PSK=%s\n" % secret)
    (home / "seeds/leak.seed").chmod(0o600)
    p = subprocess.run([str(ROOT / "openstick"), "--home", str(home), "configure", "--seed", "leak", "--show"], capture_output=True, text=True)
    assert p.returncode != 0 and secret not in p.stdout + p.stderr
    assert not (home / "registry.json").exists() and not (home / "events.jsonl").exists()


def injection_test(tmp):
    home = ensure_store(tmp / "inject home")
    build = home / "builds/demo"
    tree = tmp / "image-tree"
    (tree / "etc/openstick").mkdir(parents=True)
    (tree / "etc/openstick-rootfs").write_text("fixture\n")
    build.mkdir(parents=True)
    image = build / "rootfs.img"
    subprocess.run(["mke2fs", "-q", "-t", "ext4", "-F", "-d", str(tree), str(image), "24M"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    original_hash = hashlib.sha256(image.read_bytes()).hexdigest()
    seed_path = home / "seeds/test.seed"
    seed_path.write_text(seed.serialize(fixture_seed()))
    seed_path.chmod(0o600)
    p = subprocess.run([str(ROOT / "openstick"), "--home", str(home), "seed", "inject", "--build", "demo", "--seed", "test"], text=True, capture_output=True)
    assert p.returncode == 0, (p.stdout, p.stderr)
    assert hashlib.sha256(image.read_bytes()).hexdigest() == original_hash
    personal = build / "personal-test/rootfs.img"
    assert personal.exists() and personal.stat().st_mode & 0o777 == 0o600
    assert personal.parent.stat().st_mode & 0o777 == 0o700
    out = subprocess.run(["debugfs", "-R", "cat /etc/openstick/seed.env", str(personal)], text=True, capture_output=True, check=True)
    assert hashlib.sha256(out.stdout.encode()).hexdigest() == hashlib.sha256(seed_path.read_bytes()).hexdigest()
    meta = json.loads((personal.parent / "personal.json").read_text())
    assert meta["sensitive"] is True and meta["seed_sha256"] == hashlib.sha256(seed_path.read_bytes()).hexdigest()
    assert (personal.parent / "personal.json").stat().st_mode & 0o777 == 0o600
    stat_output = subprocess.run(["debugfs", "-R", "stat /etc/openstick/seed.env", str(personal)], text=True, capture_output=True, check=True).stdout
    assert "User:     0   Group:     0" in stat_output and "Mode:  0600" in stat_output


def bad_injection_test(tmp):
    home = ensure_store(tmp / "bad-inject-home")
    build = home / "builds/demo"
    build.mkdir(parents=True)
    original = build / "rootfs.img"
    original.write_bytes(b"corrupt image" * 128)
    seedfile = home / "seeds/x.seed"
    seedfile.write_text("SEED_VERSION=1\n")
    seedfile.chmod(0o600)
    p = subprocess.run([str(ROOT / "openstick"), "--home", str(home), "seed", "inject", "--build", "demo", "--seed", "x"], capture_output=True, text=True)
    assert p.returncode == 3 and not (build / "personal-x").exists()
    original.write_bytes(b"unchanged")


def interrupted_injection_test(tmp):
    home = ensure_store(tmp / "interrupted-home")
    build = home / "builds/demo"
    build.mkdir(parents=True)
    tree = tmp / "interrupted-tree"
    (tree / "etc/openstick").mkdir(parents=True)
    source = tmp / "valid-rootfs.img"
    subprocess.run(["mke2fs", "-q", "-t", "ext4", "-F", "-d", str(tree), str(source), "24M"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    shutil.copyfile(source, build / "rootfs.img")
    original_hash = hashlib.sha256((build / "rootfs.img").read_bytes()).hexdigest()
    (home / "seeds/x.seed").write_text("SEED_VERSION=1\n")
    (home / "seeds/x.seed").chmod(0o600)
    bindir = tmp / "broken-debugfs"
    bindir.mkdir()
    shim = bindir / "debugfs"
    shim.write_text("#!/bin/sh\nexit 17\n")
    shim.chmod(0o755)
    env = dict(os.environ, PATH=str(bindir) + ":/usr/bin:/bin")
    p = subprocess.run([str(ROOT / "openstick"), "--home", str(home), "seed", "inject", "--build", "demo", "--seed", "x"], env=env, capture_output=True, text=True)
    assert p.returncode == 3 and not (build / "personal-x").exists()
    assert not list(build.glob(".personal-x.staging-*"))
    assert hashlib.sha256((build / "rootfs.img").read_bytes()).hexdigest() == original_hash
    shim.write_text("#!/bin/sh\nkill -TERM \"$PPID\"\nexit 0\n")
    shim.chmod(0o755)
    p = subprocess.run([str(ROOT / "openstick"), "--home", str(home), "seed", "inject", "--build", "demo", "--seed", "x"], env=env, capture_output=True, text=True)
    assert p.returncode == 1 and "operation interrupted" in p.stderr
    assert not (build / "personal-x").exists() and not list(build.glob(".personal-x.staging-*"))


def push_test(tmp):
    home = ensure_store(tmp / "push-home")
    (home / "seeds").joinpath("x.seed").write_text("SEED_VERSION=1\n")
    (home / "seeds/x.seed").chmod(0o600)
    p = subprocess.run([str(ROOT / "openstick"), "--home", str(home), "seed", "push", "--seed", "x"], capture_output=True, text=True)
    assert p.returncode == 2 and "flashing a seeded image" in p.stderr
    bindir = tmp / "sshbin"
    bindir.mkdir()
    for name in ("ssh", "scp"):
        f = bindir / name
        f.write_text("#!/bin/sh\nexit 255\n")
        f.chmod(0o755)
    env = dict(os.environ, PATH=str(bindir) + ":/usr/bin:/bin")
    p = subprocess.run([str(ROOT / "openstick"), "--home", str(home), "seed", "push", "--seed", "x", "--host", "tester@example.invalid"], env=env, capture_output=True, text=True)
    assert p.returncode == 2 and "flashing a seeded image" in p.stderr


def installation_test(_tmp):
    service = (ROOT / "configs/system/openstick-seed.service").read_text()
    hook = (ROOT / "scripts/20-rootfs.sh").read_text()
    assert "Before=network-pre.target NetworkManager.service systemd-networkd.service" in service
    assert "enable_unit network-pre.target openstick-seed.service" in hook
    assert "usr/lib/openstick/apply-seed" in hook
    p = subprocess.run(["bash", "-n", str(ROOT / "scripts/20-rootfs.sh")], capture_output=True)
    assert p.returncode == 0
    p = subprocess.run(["sh", "-n", str(ROOT / "configs/apply-seed")], capture_output=True)
    assert p.returncode == 0


with tempfile.TemporaryDirectory(prefix="openstick-m3-") as temp:
    t = pathlib.Path(temp)
    for name, fn in (
        ("seed-parser-validation-and-newline-rejection", parser_test),
        ("configure-hashes-and-private-mode", lambda: configure_test(t)),
        ("production-ssh-key-gate-g4", lambda: g4_test(t)),
        ("fake-root-apply-idempotency-and-secret-safe-logs", lambda: apply_test(t)),
        ("invalid-seed-does-not-block-boot", lambda: invalid_apply_test(t)),
        ("secret-redaction-in-exceptions-registry-and-events", lambda: secret_redaction_test(t)),
        ("debugfs-copy-injection-and-readback", lambda: injection_test(t)),
        ("corrupt-image-rejected-and-cleanup", lambda: bad_injection_test(t)),
        ("interrupted-injection-staging-cleanup", lambda: interrupted_injection_test(t)),
        ("seed-push-refuses-unavailable-ssh", lambda: push_test(t)),
        ("service-order-and-builder-installation", lambda: installation_test(t)),
    ):
        check(name, fn)
print("M3 TOTAL PASS=%d FAIL=%d" % (PASS, FAIL))
raise SystemExit(1 if FAIL else 0)
