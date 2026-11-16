"""Strict v1 seed format and validation."""
import base64
import binascii
import ipaddress
import re
import unicodedata

FIELDS = {
    "SEED_VERSION", "PROFILE", "HOSTNAME", "TIMEZONE", "SSH_AUTHORIZED_KEYS",
    "ROOT_PASSWORD_HASH", "DEFAULT_USER", "USER_PASSWORD_HASH",
    "WIFI_SSID", "WIFI_PSK", "WIFI_COUNTRY", "APN", "APN_USER",
    "APN_PASSWORD", "USB_NET_ADDR", "NTP_SERVERS",
    "WIFI_SSID_B64", "WIFI_PSK_B64", "APN_B64", "APN_USER_B64", "APN_PASSWORD_B64",
}
SECRET_FIELDS = {"ROOT_PASSWORD_HASH", "USER_PASSWORD_HASH", "WIFI_PSK_B64", "APN_USER_B64", "APN_PASSWORD_B64"}
NETWORK_FIELDS = {
    "WIFI_SSID_B64": "WIFI_SSID", "WIFI_PSK_B64": "WIFI_PSK", "APN_B64": "APN",
    "APN_USER_B64": "APN_USER", "APN_PASSWORD_B64": "APN_PASSWORD",
}
HASH_RE = re.compile(r"^\$(?:6|5|y)\$[A-Za-z0-9./$=_-]{20,200}$")
HOST_RE = re.compile(r"^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)(?:\.(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?))*$")
USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")


class SeedError(ValueError):
    pass


def validate(data):
    if data.get("SEED_VERSION") != "1":
        raise SeedError("SEED_VERSION must be 1")
    if data.get("PROFILE") not in ("dev", "prod"):
        raise SeedError("PROFILE must be dev or prod")
    for key in data:
        if key not in FIELDS and not key.startswith("LED_"):
            raise SeedError("unknown seed field: " + key)
    if "HOSTNAME" in data:
        hostname = data["HOSTNAME"]
        if hostname.count("{serial8}") > 1 or not HOST_RE.fullmatch(hostname.replace("{serial8}", "0123abcd")):
            raise SeedError("invalid HOSTNAME")
    if "TIMEZONE" in data and (not re.fullmatch(r"[A-Za-z0-9_+.-]+(?:/[A-Za-z0-9_+.-]+)*", data["TIMEZONE"]) or ".." in data["TIMEZONE"]):
        raise SeedError("invalid TIMEZONE")
    for key in ("ROOT_PASSWORD_HASH", "USER_PASSWORD_HASH"):
        if key in data and not HASH_RE.fullmatch(data[key]):
            raise SeedError("invalid " + key)
    if "DEFAULT_USER" in data and not USER_RE.fullmatch(data["DEFAULT_USER"]):
        raise SeedError("invalid DEFAULT_USER")
    if data.get("DEFAULT_USER") == "root":
        raise SeedError("DEFAULT_USER cannot be root")
    if data.get("PROFILE") == "prod" and "DEFAULT_USER" in data:
        raise SeedError("prod seeds cannot define DEFAULT_USER")
    if "USER_PASSWORD_HASH" in data and "DEFAULT_USER" not in data:
        raise SeedError("USER_PASSWORD_HASH requires DEFAULT_USER")
    decoded = {}
    for encoded_key, key in NETWORK_FIELDS.items():
        if encoded_key not in data:
            continue
        try:
            raw = base64.b64decode(data[encoded_key], validate=True)
            value = raw.decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            raise SeedError("invalid " + encoded_key)
        if not value or any(unicodedata.category(char).startswith("C") for char in value):
            raise SeedError("invalid " + encoded_key)
        decoded[key] = value
    if "APN_USER_B64" in data and "APN_B64" not in data or "APN_PASSWORD_B64" in data and "APN_B64" not in data:
        raise SeedError("APN credentials require APN")
    if "WIFI_COUNTRY" in data and "WIFI_SSID_B64" not in data:
        raise SeedError("WIFI_COUNTRY requires WIFI_SSID")
    if "WIFI_SSID_B64" in data and len(decoded["WIFI_SSID"].encode("utf-8")) > 32:
        raise SeedError("invalid WIFI_SSID length")
    if "WIFI_PSK_B64" in data and not (8 <= len(decoded["WIFI_PSK"].encode("utf-8")) <= 63):
        raise SeedError("invalid WIFI_PSK length")
    if "WIFI_PSK_B64" in data and "WIFI_SSID_B64" not in data:
        raise SeedError("WIFI_PSK requires WIFI_SSID")
    if "SSH_AUTHORIZED_KEYS" in data:
        try:
            keys = base64.b64decode(data["SSH_AUTHORIZED_KEYS"], validate=True).decode("ascii")
        except (binascii.Error, UnicodeDecodeError):
            raise SeedError("invalid SSH_AUTHORIZED_KEYS")
        lines = keys.splitlines()
        key_re = re.compile(r"^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(?:256|384|521)) ([A-Za-z0-9+/]+={0,3})(?: [^\r\n]*)?$")
        if not lines or any(not line for line in lines):
            raise SeedError("invalid SSH public key list")
        for line in lines:
            match = key_re.fullmatch(line)
            if not match:
                raise SeedError("invalid SSH public key list")
            try:
                blob = base64.b64decode(match.group(2), validate=True)
            except binascii.Error:
                raise SeedError("invalid SSH public key list")
            if len(blob) < 4:
                raise SeedError("invalid SSH public key list")
            length = int.from_bytes(blob[:4], "big")
            if length < 1 or length > len(blob) - 4 or blob[4:4 + length].decode("ascii", "ignore") != match.group(1):
                raise SeedError("invalid SSH public key list")
            offset = 4 + length
            fields = []
            while offset < len(blob):
                if offset + 4 > len(blob):
                    raise SeedError("invalid SSH public key list")
                field_length = int.from_bytes(blob[offset:offset + 4], "big")
                offset += 4
                if field_length < 1 or offset + field_length > len(blob):
                    raise SeedError("invalid SSH public key list")
                fields.append(blob[offset:offset + field_length])
                offset += field_length
            key_type = match.group(1)
            if key_type == "ssh-ed25519" and (len(fields) != 1 or len(fields[0]) != 32):
                raise SeedError("invalid SSH public key list")
            if key_type == "ssh-rsa" and len(fields) != 2:
                raise SeedError("invalid SSH public key list")
            if key_type.startswith("ecdsa-"):
                curve = key_type.removeprefix("ecdsa-sha2-")
                if len(fields) != 2 or fields[0].decode("ascii", "ignore") != curve:
                    raise SeedError("invalid SSH public key list")
    if "WIFI_COUNTRY" in data and not re.fullmatch(r"[A-Z]{2}", data["WIFI_COUNTRY"]):
        raise SeedError("invalid WIFI_COUNTRY")
    if "USB_NET_ADDR" in data:
        try:
            if ipaddress.ip_interface(data["USB_NET_ADDR"]).version != 4:
                raise ValueError()
        except ValueError:
            raise SeedError("invalid USB_NET_ADDR")
    if "NTP_SERVERS" in data:
        if not data["NTP_SERVERS"] or not all(re.fullmatch(r"[A-Za-z0-9.-]+", h) for h in data["NTP_SERVERS"].split(",")):
            raise SeedError("invalid NTP_SERVERS")
    return data


def parse(text):
    if not isinstance(text, str) or len(text) > 65536 or "\x00" in text or "\r" in text:
        raise SeedError("seed contains invalid bytes")
    result = {}
    for number, line in enumerate(text.splitlines(), 1):
        if not line:
            continue
        if "\r" in line or "\n" in line or line.count("=") < 1:
            raise SeedError("malformed seed line %d" % number)
        key, value = line.split("=", 1)
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or not value or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise SeedError("malformed seed line %d" % number)
        if key in result:
            raise SeedError("duplicate seed field: " + key)
        result[key] = value
    return validate(result)


def serialize(data):
    validate(data)
    return "".join("%s=%s\n" % (key, data[key]) for key in sorted(data))

