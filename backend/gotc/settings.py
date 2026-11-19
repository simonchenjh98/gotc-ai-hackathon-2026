"""Resolve the public origin used by links, cookie policy and the reverse proxy."""
import ipaddress
import os
from urllib.parse import urlsplit


def public_origin(value=None, *, production=False):
    value = value or os.environ.get("GOTC_PUBLIC_ORIGIN")
    if not value:
        if production:
            raise ValueError("Set GOTC_PUBLIC_ORIGIN to the public HTTPS origin")
        value = f"http://127.0.0.1:{int(os.environ.get('GOTC_PORT', '5274'))}"
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        raise ValueError("Invalid GOTC_PUBLIC_ORIGIN") from None
    if (parts.scheme not in {"http", "https"} or not parts.hostname or parts.username is not None
            or parts.password is not None or parts.path not in {"", "/"} or "?" in value or "#" in value
            or "\\" in value or not value.isascii() or any(ord(ch) < 33 or ord(ch) == 127 for ch in value)
            or (production and parts.scheme != "https") or port == 0):
        raise ValueError("GOTC_PUBLIC_ORIGIN must be an exact origin (HTTPS in production)")
    return value.removesuffix("/")


def hsts_allowed(origin):
    parts = urlsplit(origin)
    if parts.scheme != "https":
        return False
    try:
        ipaddress.ip_address(parts.hostname)
    except ValueError:
        return True
    return False
