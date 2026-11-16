"""Interactive seed configuration. Secret input is never echoed or printed."""
import base64
import ctypes
import ctypes.util
import getpass
import os
import re
import secrets
import string
import warnings
from pathlib import Path

from . import seed
from .store import RefusedError, StoreError, _atomic_private_write


def password_hash(password):
    name = ctypes.util.find_library("crypt")
    if not name:
        raise StoreError("libcrypt is required to hash passwords")
    lib = ctypes.CDLL(name)
    crypt = lib.crypt
    crypt.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
    crypt.restype = ctypes.c_char_p
    alphabet = string.ascii_letters + string.digits + "./"
    salt = "".join(secrets.choice(alphabet) for _ in range(16))
    value = crypt(password.encode(), ("$6$" + salt + "$" ).encode())
    if not value:
        raise StoreError("password hashing failed")
    result = value.decode("ascii")
    if not result.startswith("$6$"):
        raise StoreError("SHA-512 crypt password hashing is unavailable")
    return result


def _ask(prompt, default=""):
    suffix = " [%s]" % default if default else ""
    value = input(prompt + suffix + ": ")
    return value or default


def _ask_secret(prompt):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            first = getpass.getpass(prompt + ": ")
    except getpass.GetPassWarning:
        raise StoreError("a terminal is required for hidden secret input")
    if not first:
        return ""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            second = getpass.getpass("Confirm " + prompt.lower() + ": ")
    except getpass.GetPassWarning:
        raise StoreError("a terminal is required for hidden secret input")
    if first != second:
        raise StoreError("secret entries did not match")
    return first


def _public_keys():
    path = _ask("SSH public key file (blank to skip)")
    if not path:
        return ""
    file = Path(path).expanduser()
    try:
        content = file.read_text(encoding="ascii")
    except (OSError, UnicodeError):
        raise StoreError("could not read SSH public key file")
    if "PRIVATE KEY" in content or "BEGIN OPENSSH" in content:
        raise StoreError("private key files are never accepted")
    lines = [line.strip() for line in content.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    encoded = base64.b64encode(("\n".join(lines) + "\n").encode("ascii")).decode("ascii")
    seed.validate({"SEED_VERSION": "1", "PROFILE": "dev", "SSH_AUTHORIZED_KEYS": encoded})
    return encoded


def configure(root, name, profile="dev"):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", name):
        raise RefusedError("seed name must use letters, digits, dot, underscore or dash")
    data = {"SEED_VERSION": "1", "PROFILE": profile}
    for key, prompt, default in (("HOSTNAME", "Hostname", "openstick"), ("TIMEZONE", "Timezone", "Etc/UTC")):
        value = _ask(prompt, default)
        if value: data[key] = value
    keys = _public_keys()
    if keys: data["SSH_AUTHORIZED_KEYS"] = keys
    if profile == "prod" and not keys:
        print("WARNING: a production image without an SSH public key cannot be accessed remotely.")
        if input("Type CONFIRM to continue with an unreachable production seed: ").strip() != "CONFIRM":
            raise RefusedError("production seed requires SSH key or explicit confirmation")
    root_pass = _ask_secret("Root password (blank to keep root locked)")
    if root_pass: data["ROOT_PASSWORD_HASH"] = password_hash(root_pass)
    if profile != "prod":
        user = _ask("Default user (enter - to omit)", "openstick")
        if user == "-": user = ""
        if user:
            data["DEFAULT_USER"] = user
            user_pass = _ask_secret("User password hash input (blank to skip)")
            if user_pass: data["USER_PASSWORD_HASH"] = password_hash(user_pass)
    wifi_ssid = _ask("Wi-Fi SSID (blank to skip)")
    if wifi_ssid:
        data["WIFI_SSID_B64"] = base64.b64encode(wifi_ssid.encode("utf-8")).decode("ascii")
        psk = _ask_secret("Wi-Fi password (PSK)")
        if psk: data["WIFI_PSK_B64"] = base64.b64encode(psk.encode("utf-8")).decode("ascii")
        country = _ask("Wi-Fi country code (optional)")
        if country: data["WIFI_COUNTRY"] = country.upper()
    for key, prompt, default in (("APN", "Cellular APN", "internet"), ("USB_NET_ADDR", "USB network address in CIDR form", ""), ("NTP_SERVERS", "NTP servers, comma separated", "")):
        value = _ask(prompt, default)
        if value:
            if key == "APN": data["APN_B64"] = base64.b64encode(value.encode("utf-8")).decode("ascii")
            else: data[key] = value
    apn_user = _ask_secret("APN username")
    if apn_user: data["APN_USER_B64"] = base64.b64encode(apn_user.encode("utf-8")).decode("ascii")
    ap = _ask_secret("APN password")
    if ap: data["APN_PASSWORD_B64"] = base64.b64encode(ap.encode("utf-8")).decode("ascii")
    seed.validate(data)
    path = root / "seeds" / (name + ".seed")
    _atomic_private_write(path, seed.serialize(data).encode("utf-8"))
    print("Seed saved privately as %s." % name)
    return path


def show(root, name):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", name):
        raise RefusedError("invalid seed name")
    path = root / "seeds" / (name + ".seed")
    try:
        data = seed.parse(path.read_text(encoding="utf-8"))
    except OSError:
        raise StoreError("seed not found")
    for key in sorted(data):
        value = data[key]
        if key in seed.SECRET_FIELDS: value = "[REDACTED]"
        elif key.endswith("_B64") and key in seed.NETWORK_FIELDS: value = "[ENCODED PRIVATE VALUE]"
        elif key == "SSH_AUTHORIZED_KEYS": value = "[public key data]"
        elif key.startswith("LED_"): value = "[reserved, ignored]"
        print("%s=%s" % (key, value))
