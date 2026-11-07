"""Browser-source policy for API-key gateway routes; no authentication side effects.

The HTTP dispatcher must enforce origin_allowed before authentication, metering,
body processing or forwarding. A browser preflight uses preflight_allowed and
does not consume an API key or usage entry. SDK requests without Origin remain
supported, but an explicit cross-site Sec-Fetch-Site never bypasses the policy.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit


ALLOWED_METHODS = frozenset({"GET", "HEAD", "POST"})
ALLOWED_HEADERS = (
    "Authorization",
    "Content-Type",
    "X-API-Key",
    "Anthropic-Version",
    "Anthropic-Beta",
    "Anthropic-Dangerous-Direct-Browser-Access",
    "OpenAI-Beta",
    "X-Client-Request-ID",
)
_ALLOWED_HEADER_NAMES = frozenset(name.lower() for name in ALLOWED_HEADERS)
_BAD_VALUE = re.compile(r"[\x00-\x1f\x7f]")


def _values(headers, name):
    """Preserve duplicates from HTTPMessage, including differently cased fields."""
    if hasattr(headers, "get_all"):
        return headers.get_all(name, []) or []
    # Plain mappings are useful to callers/tests, while the actual HTTP handler
    # supplies get_all. Do not flatten list values or combine ambiguous fields.
    return [value for key, value in headers.items()
            if isinstance(key, str) and key.lower() == name.lower()]


def _single(headers, name, required=False):
    values = _values(headers, name)
    if not values:
        return not required, None
    if len(values) != 1 or not isinstance(values[0], str) or _BAD_VALUE.search(values[0]):
        return False, None
    return True, values[0]


def _trusted_origin(value):
    if not isinstance(value, str) or not value or any(c.isspace() for c in value):
        return False
    try:
        parsed = urlsplit(value)
        return (parsed.scheme in {"http", "https"} and bool(parsed.hostname) and parsed.username is None
                and parsed.password is None and parsed.path == "" and not parsed.query and not parsed.fragment
                and parsed.port != 0)
    except ValueError:
        return False


def origin_allowed(headers, trusted_origin):
    """Allow the exact configured browser origin or a genuinely origin-less SDK."""
    valid, source = _single(headers, "Sec-Fetch-Site")
    if not valid or (source is not None and source not in {"same-origin", "none"}):
        return False
    valid, origin = _single(headers, "Origin")
    if not valid:
        return False
    if origin is None:
        return True
    # An empty or opaque/null origin is not an SDK request. Do not trim, parse
    # out credentials, normalize ports, or use a suffix/prefix host comparison.
    return bool(origin and origin != "null" and _trusted_origin(trusted_origin) and origin == trusted_origin)


def preflight_allowed(headers, trusted_origin):
    """Validate the browser source, requested method and explicit header allowlist."""
    valid, origin = _single(headers, "Origin", required=True)
    if not valid or not origin or not origin_allowed(headers, trusted_origin):
        return False
    valid, method = _single(headers, "Access-Control-Request-Method", required=True)
    if not valid or method not in ALLOWED_METHODS:
        return False
    valid, value = _single(headers, "Access-Control-Request-Headers")
    if not valid:
        return False
    if value is None:
        return True
    if len(value) > 8192 or not value.isascii():
        return False
    # Reject non-ASCII before trimming or case folding (e.g. Kelvin sign -> k).
    # Every allowlisted name is already a valid ASCII token, so membership also
    # rejects punctuation/invalid syntax without matching untrusted input to a regex.
    names = [name.strip().lower() for name in value.split(",") if name.strip()]
    # Reject duplicate requested names too: browser-generated lists are unique,
    # and accepting ambiguous manual preflights has no compatibility benefit.
    return len(names) == len(set(names)) and all(name in _ALLOWED_HEADER_NAMES for name in names)


def response_headers(headers, trusted_origin, preflight=False):
    """CORS headers only for an allowed explicit origin, never wildcard/credentials."""
    allowed = preflight_allowed(headers, trusted_origin) if preflight else origin_allowed(headers, trusted_origin)
    if not allowed:
        return {}
    _, origin = _single(headers, "Origin")
    if origin is None:
        return {}
    # The checks above require exact equality with the validated configuration.
    # Emit that configuration, never data copied from an incoming HTTP header.
    result = {"Access-Control-Allow-Origin": trusted_origin, "Vary": "Origin"}
    if preflight:
        result["Access-Control-Allow-Methods"] = "GET, HEAD, POST, OPTIONS"
        result["Access-Control-Allow-Headers"] = ", ".join(ALLOWED_HEADERS)
    else:
        result["Access-Control-Expose-Headers"] = "X-Client-Request-ID, Retry-After"
    return result
