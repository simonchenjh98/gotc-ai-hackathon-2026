"""Built-in AI API gateway: team keys, upstream proxy, usage metering. Standard library only.

Organizers enable a team once; the team then creates its own keys and usage is tracked per team.
Keys are shown once and stored only as SHA-256 hashes. Requests are forwarded with the
organizer's upstream keys from the private gateway.json, which teams never see.
"""
from __future__ import annotations

import csv
import hashlib
import http.client
import io
import ipaddress
import os
import json
import re
import secrets
import select
import socket
import ssl
import threading
import time
import traceback
import unicodedata
import urllib.error
import urllib.request
import uuid
import zlib
from collections import defaultdict, deque
from urllib.parse import parse_qs, urlsplit

from errors import APIError

SCHEMA = """
CREATE TABLE IF NOT EXISTS team_api (
 team_id TEXT PRIMARY KEY REFERENCES teams(id), active INTEGER NOT NULL CHECK(active IN (0,1)), created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS api_keys (
 id TEXT PRIMARY KEY, team_id TEXT NOT NULL REFERENCES teams(id), name TEXT NOT NULL, key_hash TEXT NOT NULL UNIQUE,
 masked TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)), created_at INTEGER NOT NULL, last_used_at INTEGER
);
CREATE TABLE IF NOT EXISTS api_usage (
 id INTEGER PRIMARY KEY AUTOINCREMENT, key_id TEXT NOT NULL REFERENCES api_keys(id), team_id TEXT NOT NULL REFERENCES teams(id),
 at INTEGER NOT NULL, model TEXT NOT NULL, input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
 cache_write_tokens INTEGER NOT NULL, cache_read_tokens INTEGER NOT NULL,
 cost_micro INTEGER NOT NULL, status INTEGER NOT NULL, path TEXT NOT NULL, stream INTEGER NOT NULL,
 duration_ms INTEGER NOT NULL, user_agent TEXT NOT NULL,
 incomplete INTEGER NOT NULL DEFAULT 0, estimated INTEGER NOT NULL DEFAULT 0,
 error_type TEXT NOT NULL DEFAULT '', error_phase TEXT NOT NULL DEFAULT '', error_owner TEXT NOT NULL DEFAULT '',
 upstream_status INTEGER, error_message TEXT NOT NULL DEFAULT '',
 cache_write_1h_tokens INTEGER NOT NULL DEFAULT 0, requested_model TEXT NOT NULL DEFAULT '', first_token_ms INTEGER,
 client_ip TEXT NOT NULL DEFAULT '', upstream_request_id TEXT NOT NULL DEFAULT '', request_id TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS api_usage_at ON api_usage(at);
CREATE INDEX IF NOT EXISTS api_usage_team ON api_usage(team_id, at);
CREATE INDEX IF NOT EXISTS api_usage_key ON api_usage(key_id, at);
CREATE TABLE IF NOT EXISTS gateway_config (
 id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL, updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS api_logs (
 usage_id INTEGER PRIMARY KEY REFERENCES api_usage(id), headers TEXT NOT NULL,
 request BLOB NOT NULL, response BLOB NOT NULL, request_bytes INTEGER NOT NULL, response_bytes INTEGER NOT NULL
);
"""
DEFAULTS = {"keys.maxKeys": 5, "keys.expiresAt": None,
            "keys.logContent": True, "keys.teamRpm": 60, "keys.announcement": "", "keys.blockedWords": [],
            "keys.logRetentionHours": 72, "keys.logMaxMB": 2048}  # request content is bounded (sub2api keeps no bodies at all)
# Public path -> (upstream name, path appended to that upstream's base URL)
ROUTES = {"/v1/chat/completions": ("openai", "/chat/completions"), "/v1/messages": ("anthropic", "/messages"),
          "/v1/responses": ("openai", "/responses")}  # Responses API: Codex CLI
BODY_LIMIT = 32 * 1024 * 1024  # sub2api allows 32 MB text bodies; screenshots and long agent sessions need it
JSON_DEPTH_LIMIT = 128  # independent of the Python JSON parser's platform/version recursion limit
# Error classification copied from sub2api (handler/ops_error_logger.go classifyOpsPhase + owner mapping):
# kind -> (default status, Anthropic error type, OpenAI error type, OpenAI code, phase, owner).
# phase: request|auth|upstream|network|internal; owner: client (the team's request), provider (upstream), platform (gateway).
ERROR_KINDS = {
    "invalid": (400, "invalid_request_error", "invalid_request_error", "invalid_request", "request", "client"),
    "auth": (401, "authentication_error", "invalid_request_error", "invalid_api_key", "auth", "client"),
    "forbidden": (403, "permission_error", "invalid_request_error", "permission_denied", "request", "client"),
    "billing": (403, "billing_error", "insufficient_quota", "insufficient_quota", "request", "client"),
    "not_found": (404, "not_found_error", "invalid_request_error", "model_not_found", "request", "client"),
    "too_large": (413, "request_too_large", "invalid_request_error", "request_too_large", "request", "client"),
    "rate_limit": (429, "rate_limit_error", "rate_limit_error", "rate_limit_exceeded", "request", "client"),
    "client_closed": (499, "api_error", "api_error", "client_closed_request", "internal", "platform"),
    "internal": (500, "api_error", "api_error", "internal_error", "internal", "platform"),
    "upstream": (502, "api_error", "upstream_error", "upstream_error", "upstream", "provider"),
    "upstream_invalid": (400, "invalid_request_error", "invalid_request_error", "invalid_request", "upstream", "provider"),
    "upstream_rate_limit": (429, "rate_limit_error", "rate_limit_error", "rate_limit_exceeded", "upstream", "provider"),
    "overloaded": (503, "overloaded_error", "server_error", "overloaded", "upstream", "provider"),
    "network": (502, "api_error", "upstream_error", "upstream_unreachable", "network", "provider"),
    "timeout": (504, "api_error", "upstream_error", "upstream_timeout", "network", "provider"),
}
STATUS_KINDS = {400: "invalid", 401: "auth", 403: "forbidden", 404: "not_found", 411: "invalid", 413: "too_large",
                415: "invalid", 429: "rate_limit", 499: "client_closed"}
UPSTREAM_TIMEOUT = 300  # waiting for the reply to start
CONNECT_TIMEOUT = 10  # connect + TLS handshake (sub2api dial and TLS timeouts), so a dead upstream frees the slot fast
STREAM_IDLE_TIMEOUT = 180  # silence between stream events before giving up (sub2api stream_data_interval_timeout)
SSE_EVENT_CAP = 1024 * 1024  # inspect at most 1 MiB per event before any of its bytes reach the client
QUEUE_CAP = 20  # waiting requests per team beyond its running ones (sub2api defaultExtraWaitSlots)
AUTH_FAIL_LIMIT, AUTH_FAIL_WINDOW, AUTH_BLOCK = 120, 60, 60  # per client IP, as sub2api's invalid-auth limiter
COUNT_TOKENS_CONCURRENCY, COUNT_TOKENS_RPM = 10, 120  # count_tokens is free but uses our upstream key's rate limit
KEY_CREATE_PER_HOUR = 60  # as sub2api; deleting a key does not give the quota back
QUEUE_WAIT = 30  # seconds a request waits for a free team slot before 429 (agent tools send bursts)
OUTPUT_DEFAULT = 8192  # default output tokens for Anthropic requests without max_tokens
OUTPUT_CAP = 128000
IMAGE_TOKENS = 1600  # ceiling for one image (Anthropic: width*height/750, images resized to ~1.15 MP)
RETRY_STATUSES = {429, 503, 529}  # explicit "not processed" answers; 5xx/timeouts may already be billed upstream
# Tools must run on the client. Anthropic "client tools" (bash, text editor, computer, memory) are allowed;
# anything else typed (web search, code execution, MCP, tool search, …) runs and is billed upstream.
CLIENT_TOOL_TYPES = ("custom", "function", "bash_", "text_editor_", "computer_", "memory_", "local_shell")
BILLED_FIELDS = {"mcp_servers", "web_search_options", "container"}  # upstream-billed extras outside token usage
PRICING_FIELDS = ("service_tier", "speed", "inference_geo")  # dropped so requests run at the configured (standard) price
# ponytail: denylist of beta prefixes that add upstream charges; extend when providers add more.
BILLED_BETAS = ("mcp-client", "code-execution", "files-api", "context-1m", "skills", "web-fetch", "web-search")
RETRY_DELAYS = (0.3, 0.6, 1.2, 2.4)  # sub2api: 5 attempts, 300 ms doubling, 3 s cap  # seconds; retried only before any byte reaches the client
TEAM_CONCURRENCY = 10
DAY_MS = 86_400_000
RESPONSE_LOG_CAP = 16 * 1024 * 1024  # bytes of a reply kept for the content viewer; requests are already capped by BODY_LIMIT
SECRET_HEADERS = {"authorization", "x-api-key", "cookie", "proxy-authorization"}


def now_ms():
    return int(time.time() * 1000)


KEY_HASH_ITERATIONS = 1000  # keys are 256-bit random, so the server-side pepper, not iterations, carries the strength


def load_pepper(data_dir):
    """Private random secret mixed into key hashes, so a leaked database alone cannot be used to check keys."""
    path = data_dir / "api-key-pepper"
    if not path.exists():
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)  # never overwrite: that would void every key
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(secrets.token_hex(32))
        except FileExistsError:
            pass
    return bytes.fromhex(path.read_text(encoding="utf-8").strip())


class GatewayError(APIError):
    """An error with a sub2api-style classification; plain APIError statuses are classified by STATUS_KINDS."""

    def __init__(self, kind, message, status=None, upstream_status=None, upstream_message="", retry_after=None, detail=""):
        super().__init__(status or ERROR_KINDS[kind][0], message)
        self.kind, self.upstream_status, self.upstream_message = kind, upstream_status, upstream_message
        self.retry_after, self.detail = retry_after, detail  # detail: recorded for admins, never sent to the team


def error_info(error):
    """The record and response fields of an error, whichever exception type carried it."""
    kind = getattr(error, "kind", None) or STATUS_KINDS.get(error.status) or ("internal" if error.status >= 500 else "invalid")
    _, a_type, o_type, o_code, phase, owner = ERROR_KINDS[kind]
    return {"kind": kind, "type": a_type, "openai_type": o_type, "code": o_code, "phase": phase, "owner": owner,
            "message": error.message, "upstream_status": getattr(error, "upstream_status", None),
            "upstream_message": getattr(error, "upstream_message", ""), "retry_after": getattr(error, "retry_after", None),
            "detail": getattr(error, "detail", "")}


def error_body(path, info):
    """Error JSON in the native format of the endpoint, as sub2api answers each API in its own shape."""
    if path.startswith("/v1/messages"):
        return {"type": "error", "error": {"type": info["type"], "message": info["message"]}}
    return {"error": {"message": info["message"], "type": info["openai_type"], "code": info["code"]}}


def sse_error(path, info):
    """Terminal error frame appended to a stream that cannot finish (sub2api appends one instead of just closing)."""
    data = json.dumps(error_body(path, info), ensure_ascii=False)
    return (f"event: error\ndata: {data}\n\n" if path.startswith("/v1/messages") else f"data: {data}\n\n").encode()


def redact_error_payload(payload, secrets=()):
    """Redact credentials in provider errors without changing their JSON structure.

    Decode JSON first so escaped keys are not missed. Error strings sometimes contain JSON or URLs themselves;
    match mixed literal, JSON-escaped and percent-encoded characters there as well. Successful replies never use this.
    """
    if isinstance(secrets, str):
        secrets = (secrets,)
    patterns = []
    for secret in sorted(set(secrets), key=len, reverse=True):
        if not secret:
            continue
        pieces = []
        for char in secret:
            units = char.encode("utf-16-be", "surrogatepass")
            unicode_escape = "".join(f"\\u{int.from_bytes(units[i:i + 2], 'big'):04x}" for i in range(0, len(units), 2))
            percent_escape = "".join(f"%{b:02x}" for b in char.encode("utf-8", "surrogatepass"))
            alternatives = {re.escape(char), re.escape(json.dumps(char)[1:-1]),
                            "(?i:" + re.escape(unicode_escape) + ")", "(?i:" + re.escape(percent_escape) + ")"}
            if char == "/":
                alternatives.add(re.escape("\\/"))
            pieces.append("(?:" + "|".join(sorted(alternatives)) + ")")
        patterns.append(re.compile("".join(pieces)))

    def clean_text(text):
        for pattern in patterns:
            text = pattern.sub("***", text)
        text = re.sub(r"(sk-|gk-|Bearer\s+)[A-Za-z0-9._\-]{6,}", r"\1***", text)
        return re.sub(r"(?i)((?:api_?key|key|access_token|refresh_token|client_secret)=)[^&\s]+", r"\1***", text)

    def clean(value):
        if isinstance(value, str):
            return clean_text(value)
        if isinstance(value, list):
            return [clean(item) for item in value]
        if isinstance(value, dict):
            return {clean_text(key): clean(item) for key, item in value.items()}
        return value

    text = payload.decode("utf-8", "replace") if isinstance(payload, bytes) else str(payload)
    try:
        data = json.loads(text)
    except ValueError:
        sanitized = clean_text(text)
        return sanitized.encode() if sanitized != text else payload
    sanitized = clean(data)
    return json.dumps(sanitized, ensure_ascii=False).encode() if sanitized != data else payload


def upstream_message(payload, secret=""):
    """The upstream's own error message, cut down and with our upstream key and key-like strings removed."""
    payload = redact_error_payload(payload, secret)
    text = payload.decode("utf-8", "replace") if isinstance(payload, bytes) else str(payload)
    try:
        data = json.loads(text)
        found = data.get("error") if isinstance(data, dict) else None
        text = found.get("message") if isinstance(found, dict) and isinstance(found.get("message"), str) else \
            found if isinstance(found, str) else data.get("message") if isinstance(data, dict) and isinstance(data.get("message"), str) else text
    except ValueError:
        pass
    return " ".join(text.split())[:500]


def upstream_http_error(path, status, message):
    """Map an upstream HTTP error the way sub2api does (gateway_upstream_response.go / openai_gateway_upstream_errors.go)."""
    if status == 400:
        return None  # passed through as-is, so the team can see what was wrong with its request
    if status in (401, 403):
        return GatewayError("upstream", "上游鉴权失败，请联系组织方检查上游配置", upstream_status=status, upstream_message=message)
    if status == 429:
        return GatewayError("upstream_rate_limit", "上游请求过多，请稍后重试", upstream_status=status, upstream_message=message)
    if status == 529:
        return GatewayError("overloaded", "上游模型服务繁忙，请稍后重试", upstream_status=status, upstream_message=message)
    if status >= 500:
        return GatewayError("upstream", "上游模型服务暂时不可用，请稍后重试", upstream_status=status, upstream_message=message)
    if path.startswith("/v1/messages"):  # Anthropic: other 4xx become 502 without upstream text
        return GatewayError("upstream", "上游请求失败", upstream_status=status, upstream_message=message)
    # OpenAI-compatible: keep the upstream status with its sanitized message
    return GatewayError("upstream_invalid", message or "上游请求失败", status=status, upstream_status=status, upstream_message=message)


class _ShortConnect:
    """Connect and TLS handshake within CONNECT_TIMEOUT; reads then use the request's own timeout."""

    def connect(self):
        read_timeout, self.timeout = self.timeout, CONNECT_TIMEOUT
        try:
            super().connect()
        finally:
            self.timeout = read_timeout
        if self.sock is not None:
            self.sock.settimeout(read_timeout)


class _HTTPConnection(_ShortConnect, http.client.HTTPConnection):
    pass


class _HTTPSConnection(_ShortConnect, http.client.HTTPSConnection):
    pass


class _HTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_HTTPConnection, req)


class _HTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_HTTPSConnection, req, context=self._context)


def make_opener(proxies=None):
    """Upstream HTTP client: no redirects, short connect timeout; proxies from the environment unless given."""
    handlers = [_NoRedirect, _HTTPHandler(), _HTTPSHandler()]
    if proxies is not None:
        handlers.append(urllib.request.ProxyHandler(proxies))
    return urllib.request.build_opener(*handlers)


def set_read_timeout(response, seconds):
    sock = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
    if sock is not None:
        sock.settimeout(seconds)


# Upstream reply headers passed to the team (sub2api responseheaders allowlist). Rate-limit headers are deliberately not
# passed: they describe our shared upstream key, not the team.
PASSED_HEADERS = ("x-request-id", "request-id", "retry-after", "openai-processing-ms")
CLIENT_HEADERS = ("x-app", "x-claude-code-session-id", "accept-language")  # forwarded to Anthropic, as sub2api


def passed_headers(headers):
    return {name: headers[name][:200] for name in PASSED_HEADERS if headers.get(name)}


def gateway_json(value):
    """Compact UTF-8 JSON for upstream bodies: CJK is not escaped (half the bytes) and NaN/Infinity never go out."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()


def validate_json_depth(value):
    """Bound container depth before recursive schema/media inspection or serialization.

    Iterator frames keep this check non-recursive and use memory proportional to
    depth, even for requests containing many sibling messages or long strings.
    """
    stack = [iter((value,))]
    while stack:
        try:
            item = next(stack[-1])
        except StopIteration:
            stack.pop()
            continue
        if isinstance(item, (dict, list)):
            if len(stack) > JSON_DEPTH_LIMIT:
                raise GatewayError("invalid", f"JSON 嵌套过深，最多允许 {JSON_DEPTH_LIMIT} 层")
            stack.append(iter(item.values() if isinstance(item, dict) else item))


class ClientGone(Exception):
    """The team's client closed its connection; nothing more can be sent to it."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # never replay the upstream key to another location


def check_url(value):
    parts = urlsplit(value if isinstance(value, str) else "")
    try:
        loopback = parts.hostname == "localhost" or ipaddress.ip_address(parts.hostname or "").is_loopback
    except ValueError:
        loopback = False
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.query or parts.fragment or parts.username \
            or (parts.scheme == "http" and not loopback):
        raise ValueError("upstream url must be https, or http on loopback")
    return value.rstrip("/")


def load_config(path):
    """Optional private gateway.json, used only to seed the admin panel's upstream settings on first start."""
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    parse_config(data)
    return data


def full_price(p):
    """[input, output, cache write 5m, cache read, cache write 1h]; missing cache prices use Anthropic's ratios to input."""
    p = list(p)
    if len(p) == 2:
        p += [round(p[0] * 1.25, 6), round(p[0] * 0.1, 6)]
    if len(p) == 4:
        p.append(round(p[0] * 2, 6))
    return p


def valid_price(p, lengths):
    return isinstance(p, list) and len(p) in lengths and all(type(x) in (int, float) and 0 <= x <= 10000 for x in p)


def parse_config(data):
    """Validate {"openai"|"anthropic": {"url", "key", "models": {name: price}, "aliases"?: {name|prefix*: model}, "defaultPrice"?: price}}.

    price is [input, output] or [input, output, cacheWrite, cacheRead] USD per million tokens;
    the short form bills cache writes at 1.25x and cache reads at 0.1x the input price.
    A model with price [] uses the upstream's defaultPrice, or bills $0 without one (as sub2api does for unpriced models).
    Returns the runtime form, or None when no upstream is configured.
    """
    if data in (None, {}):
        return None
    if not isinstance(data, dict) or not set(data).issubset({"openai", "anthropic"}):
        raise ValueError("gateway.json supports openai and anthropic upstreams")
    result = {}
    for name, item in data.items():
        if not isinstance(item, dict) or not {"url", "key", "models"} <= set(item) <= {"name", "url", "key", "models", "aliases", "defaultPrice"} \
                or not isinstance(item["key"], str) or not item["key"]:
            raise ValueError(f"{name} upstream needs url, key and models")
        models, default = item["models"], item.get("defaultPrice", [0, 0])
        if not isinstance(item.get("name", ""), str) or len(item.get("name", "")) > 60:
            raise ValueError(f"{name} name must be at most 60 characters")
        if not valid_price(default, (2, 4, 5)):
            raise ValueError(f"{name} defaultPrice must be [input, output(, cacheWrite5m, cacheRead(, cacheWrite1h))] USD per million tokens")
        if not isinstance(models, dict) or not models or len(models) > 500 or not all(
                isinstance(m, str) and 0 < len(m) <= 100 and valid_price(p, (0, 2, 4, 5)) for m, p in models.items()):
            raise ValueError(f"{name} models must map a model name to [] (default price) or [input, output(, cacheWrite5m, cacheRead(, cacheWrite1h))] USD per million tokens")
        aliases = item.get("aliases", {})
        if not isinstance(aliases, dict) or not all(isinstance(a, str) and a and target in models for a, target in aliases.items()):
            raise ValueError(f"{name} aliases must map a name (or prefix ending in *) to one of its models")
        result[name] = {"url": check_url(item["url"]), "key": item["key"], "aliases": aliases,
                        "models": {m: full_price(p or default) for m, p in models.items()}}
    return result


def resolve(upstream, model):
    """Allowed model name for a requested one: exact, then alias, then the longest matching prefix* alias."""
    if not isinstance(model, str):
        return None
    if model in upstream["models"]:
        return model
    aliases = upstream["aliases"]
    if model in aliases:
        return aliases[model]
    prefixes = [a for a in aliases if a.endswith("*") and model.startswith(a[:-1])]
    return aliases[max(prefixes, key=len)] if prefixes else None


USAGE_FIELDS = 5  # [input, output, cache write 5m, cache read, cache write 1h] tokens, in price order


def _count(value):
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def read_usage(upstream, data, usage):
    """Update usage from one response object or stream event, merging like sub2api.

    Anthropic: message_start sets input and cache counts (even 0) and ignores its placeholder output; message_delta
    and final objects only override counts > 0. The cache-write total is split into 5-minute and 1-hour parts when the
    upstream reports cache_creation.ephemeral_5m/1h_input_tokens; without a split it is billed at the 5-minute price.
    OpenAI-compatible: cached prompt tokens are read from the first field present (providers name it differently),
    cache writes from prompt_tokens_details.cache_write_tokens, and input is prompt minus both, floored at zero.
    """
    if not isinstance(data, dict):
        return
    if upstream == "openai":
        u = data.get("usage")
        if not isinstance(u, dict) and isinstance(data.get("response"), dict):
            u = data["response"].get("usage")  # Responses API: response.completed carries the usage
        if not isinstance(u, dict):
            return
        details = u.get("prompt_tokens_details") if isinstance(u.get("prompt_tokens_details"), dict) else {}
        alt = u.get("input_tokens_details") if isinstance(u.get("input_tokens_details"), dict) else {}
        cached = next((_count(src[k]) for src, k in ((details, "cached_tokens"), (alt, "cached_tokens"), (u, "cache_read_input_tokens"),
                                                     (u, "cache_read_tokens"), (u, "cached_tokens"), (u, "prompt_cache_hit_tokens"))
                       if k in src), 0)
        written = _count(details.get("cache_write_tokens") or u.get("cache_creation_input_tokens"))
        prompt = _count(u.get("prompt_tokens") if "prompt_tokens" in u else u.get("input_tokens"))
        output = _count(u.get("completion_tokens") if "completion_tokens" in u else u.get("output_tokens"))
        usage[:] = [max(0, prompt - cached - written), output, written, cached, 0]
        return
    start = data.get("type") == "message_start"
    u = (data.get("message") or {}).get("usage") if start else data.get("usage")
    if not isinstance(u, dict):
        return
    split = u.get("cache_creation") if isinstance(u.get("cache_creation"), dict) else None
    written = _count(u.get("cache_creation_input_tokens"))
    one_hour = _count(split.get("ephemeral_1h_input_tokens")) if split else 0
    five_min = _count(split.get("ephemeral_5m_input_tokens")) if split else 0
    if split and not (five_min or one_hour):
        five_min = written  # a total without a split is billed at the 5-minute price
    elif not split:
        five_min = written
    values = {0: _count(u.get("input_tokens")), 1: _count(u.get("output_tokens")), 2: five_min, 3: _count(u.get("cache_read_input_tokens")),
              4: one_hour}
    for index, value in values.items():
        if start:
            if index != 1:
                usage[index] = value
        elif value > 0:
            usage[index] = value


def stream_progress(upstream, data, call):
    """Track whether the stream reached its terminal event and how much reply text it carried (for estimates)."""
    if not isinstance(data, dict):
        return
    if upstream == "anthropic":
        if data.get("type") == "message_stop":
            call["done"] = True
        delta = data.get("delta") if isinstance(data.get("delta"), dict) else {}
        texts = [delta.get("text"), delta.get("thinking"), delta.get("partial_json")]
    elif str(data.get("type", "")).startswith("response."):  # Responses API events
        if data["type"] in ("response.completed", "response.incomplete"):
            call["done"] = True
        texts = [data.get("delta")] if data["type"].endswith(".delta") else []
    else:
        choices = data.get("choices") if isinstance(data.get("choices"), list) else []
        # Usage may be reported mid-stream; only a terminal marker proves the reply completed.
        if any(isinstance(c, dict) and c.get("finish_reason") for c in choices):
            call["done"] = True
        texts = []
        for choice in choices:
            delta = choice.get("delta") if isinstance(choice, dict) and isinstance(choice.get("delta"), dict) else {}
            texts += [delta.get("content"), delta.get("reasoning_content")]
            calls = delta.get("tool_calls") if isinstance(delta.get("tool_calls"), list) else []
            texts += [c.get("function", {}).get("arguments") for c in calls if isinstance(c, dict) and isinstance(c.get("function"), dict)]
    call["reply_bytes"] += sum(len(t.encode()) for t in texts if isinstance(t, str))


OUTPUT_LIMIT_FIELDS = ("max_tokens", "max_completion_tokens", "max_output_tokens")


def bound_output(body, inject=True):
    """Bound explicit output limits and return an output estimate for missing-usage accounting.

    Any limit above the cap (or a non-integer one) is lowered to the cap; with no limit at all, max_tokens is set to
    the default for Anthropic requests.
    """
    present = [f for f in OUTPUT_LIMIT_FIELDS if f in body]
    limit = OUTPUT_DEFAULT
    for field in present:
        value = body[field]
        value = value if type(value) is int and value > 0 else OUTPUT_CAP
        body[field] = min(value, OUTPUT_CAP)
    if present:
        limit = max(body[f] for f in present)
    elif inject:
        body["max_tokens"] = limit  # Anthropic requires max_tokens anyway
    else:
        # OpenAI-compatible routes get no injected field (o-series/gpt-5 reject max_tokens, some providers reject the
        # alternatives), so use the cap as a fallback estimate when the client gives no limit.
        limit = OUTPUT_CAP
    thinking = body.get("thinking")
    if isinstance(thinking, dict) and type(thinking.get("budget_tokens")) is int and type(body.get("max_tokens")) is int \
            and thinking["budget_tokens"] >= body["max_tokens"] > 1024:
        thinking["budget_tokens"] = body["max_tokens"] - 1  # the clamp must not leave an invalid thinking budget
    return limit


def allowed_betas(value):
    """Forward client anthropic-beta flags except those that add upstream charges outside token prices."""
    flags = [f.strip() for f in (value or "").split(",") if f.strip() and not f.strip().lower().startswith(BILLED_BETAS)]
    return ",".join(flags)


def request_was_sent(error):
    """Whether the upstream can have received the whole request when this error occurred.

    urllib wraps every failure that happens while connecting, during the TLS handshake (certificate checks included)
    or while sending the request in URLError; failures after the request was sent (waiting for or reading the reply)
    are raised unwrapped. HTTPError is a URLError but means the upstream answered.
    """
    return isinstance(error, urllib.error.HTTPError) or not isinstance(error, urllib.error.URLError)


def reject_stored_file_references(body):
    """Teams share upstream credentials, but have no ownership map for files stored there.

    Only inspect API content structures: file_id in tool arguments, schemas or ordinary text is client data.
    Inline files and media URLs do not reference the shared account's stored files and remain supported.
    """
    def visit(node):
        if isinstance(node, list):
            for item in node:
                visit(item)
            return
        if not isinstance(node, dict):
            return
        kind = node.get("type")
        source = node.get("source")
        file = node.get("file")
        stored = kind in ("input_file", "input_image", "computer_screenshot", "container_upload") and node.get("file_id") is not None
        stored = stored or (kind == "file" and isinstance(file, dict) and file.get("file_id") is not None)
        stored = stored or (kind in ("image", "document") and isinstance(source, dict) and source.get("type") == "file")
        if stored:
            raise GatewayError("invalid", "本活动不支持引用上游已存储的文件，请直接提供文件内容或图片/文件 URL")
        if kind in (None, "message", "tool_result"):
            visit(node.get("content"))
        elif kind in ("function_call_output", "computer_call_output"):
            visit(node.get("output"))
        elif kind == "document" and isinstance(source, dict) and source.get("type") == "content":
            visit(source.get("content"))

    for field in ("messages", "input", "system"):
        visit(body.get(field))


def failure_reason(error):
    """Admin-facing explanation of an upstream network failure."""
    reason = getattr(error, "reason", error)
    if isinstance(reason, ssl.SSLCertVerificationError):
        return f"上游 TLS 证书校验失败（请检查上游地址与证书）：{reason.verify_message or reason}"
    if isinstance(reason, ssl.SSLError):
        return f"上游 TLS 握手失败（请检查上游地址是否为 HTTPS）：{reason.reason or reason}"
    if isinstance(reason, socket.gaierror):
        return "上游域名无法解析"
    if isinstance(reason, ConnectionRefusedError):
        return "上游拒绝连接"
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return f"上游超时（{UPSTREAM_TIMEOUT} 秒）"
    return f"上游连接失败：{type(reason).__name__}：{reason}"[:300]


def beijing_midnight():
    return (now_ms() + 8 * 3600_000) // DAY_MS * DAY_MS - 8 * 3600_000


def estimate_input(body):
    """Input tokens for missing-usage estimates: text at bytes/4 with no cap (so large prompts are not underestimated),
    each base64 image at most IMAGE_TOKENS instead of its byte size; other base64 data (PDFs...) stays at bytes/4.
    Only the image payload string itself is set aside; everything around it is still counted."""
    images = 0

    def strip(node):
        nonlocal images
        if isinstance(node, list):
            return [strip(v) for v in node]
        if not isinstance(node, dict):
            return node
        out = {k: strip(v) for k, v in node.items()}
        source = node.get("source")
        if node.get("type") == "image" and isinstance(source, dict) and source.get("type") == "base64" \
                and str(source.get("media_type", "")).startswith("image/") and isinstance(source.get("data"), str):
            images += 1
            out["source"] = {**out["source"], "data": ""}
        url = node.get("image_url")
        if node.get("type") in ("image_url", "input_image"):
            target = url.get("url") if isinstance(url, dict) else url
            if isinstance(target, str) and target.startswith("data:image/"):
                images += 1
                out["image_url"] = {**out["image_url"], "url": ""} if isinstance(url, dict) else ""
        return out
    text = json.dumps(strip(body), ensure_ascii=False).encode()
    return len(text) // 4 + images * IMAGE_TOKENS


def client_ip(handler):
    """The team's address: Nginx's X-Real-IP is trusted only from a loopback peer, as in app.py."""
    peer = handler.client_address[0]
    try:
        if ipaddress.ip_address(peer).is_loopback:
            return str(ipaddress.ip_address(handler.headers.get("X-Real-IP", "")))
    except ValueError:
        pass
    return peer


def unpack(blob):
    return zlib.decompress(blob).decode("utf-8", "replace")


ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff"))


def request_text(body):
    """All text the model will read, normalised like sub2api's moderation input (NFKC, no zero-width, collapsed spaces,
    lowercase). Every string value is read (tool arguments, outputs and summaries too); JSON keys never are, and image
    or base64 payloads are skipped."""
    texts = []

    def media_keys(node):
        """Payload fields of real media structures: Anthropic image/document sources, OpenAI image and file parts."""
        kind = node.get("type")
        if kind in ("image", "document") and isinstance(node.get("source"), dict) and node["source"].get("type") in ("base64", "url"):
            return {"source"}
        if kind in ("image_url", "input_image"):
            return {"image_url", "file_id"}
        if kind in ("input_file", "file"):
            return {"file_data", "file_url", "file_id", "file"}
        return set()

    def walk(node):
        if isinstance(node, str):
            texts.append(node)
        elif isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            skip = media_keys(node) | {"type", "role", "id", "cache_control", "tool_use_id", "call_id"}
            for k, v in node.items():
                if k not in skip:
                    walk(v)
    for field in ("system", "messages", "input", "instructions", "prompt", "tools"):
        walk(body.get(field))
    text = " ".join(texts)
    return " ".join(unicodedata.normalize("NFKC", text).translate(ZERO_WIDTH).split()).lower()


class KeyService:
    def __init__(self, service, config, text):
        self.service, self.text = service, text
        self.pepper = load_pepper(service.data_dir)
        self.seed = config  # raw gateway.json content, copied into the database once
        self.config = parse_config(config)
        self.lock = threading.Lock()
        self.config_publish_lock = threading.Lock()
        self.slot_free = threading.Condition(self.lock)
        # ponytail: in-memory per-team concurrency; correct because the service is a single process.
        self.inflight = defaultdict(int)
        self.recent = defaultdict(deque)  # team -> monotonic times of requests in the last minute
        self.waiting = defaultdict(int)  # team -> requests queued for a slot
        self.counting = defaultdict(int)  # team -> count_tokens calls in flight
        self.last_prune = 0.0
        self.auth_failures, self.auth_blocked = defaultdict(deque), {}
        self.auth_failures_seen = deque(maxlen=100000)  # failed key attempts, for the admin overview
        self.opener = make_opener()  # honours HTTPS_PROXY for reaching upstreams

    def digest(self, token):
        return hashlib.pbkdf2_hmac("sha256", token.encode(), self.pepper, KEY_HASH_ITERATIONS).hex()

    def initialize(self, db):
        db.executescript(SCHEMA)
        columns = {r["name"] for r in db.execute("PRAGMA table_info(api_usage)")}
        added = {"incomplete": "INTEGER NOT NULL DEFAULT 0", "estimated": "INTEGER NOT NULL DEFAULT 0",
                 "error_type": "TEXT NOT NULL DEFAULT ''", "error_phase": "TEXT NOT NULL DEFAULT ''",
                 "error_owner": "TEXT NOT NULL DEFAULT ''", "upstream_status": "INTEGER", "error_message": "TEXT NOT NULL DEFAULT ''",
                 "cache_write_1h_tokens": "INTEGER NOT NULL DEFAULT 0", "requested_model": "TEXT NOT NULL DEFAULT ''",
                 "first_token_ms": "INTEGER", "client_ip": "TEXT NOT NULL DEFAULT ''", "upstream_request_id": "TEXT NOT NULL DEFAULT ''",
                 "request_id": "TEXT NOT NULL DEFAULT ''"}
        for column, kind in added.items():  # databases created before these columns existed
            if column not in columns:
                db.execute(f"ALTER TABLE api_usage ADD COLUMN {column} {kind}")
        for key, value in DEFAULTS.items():
            db.execute("INSERT OR IGNORE INTO settings VALUES (?,?)", (key, json.dumps(value)))
        if self.seed:
            db.execute("INSERT OR IGNORE INTO gateway_config VALUES (1,?,?)", (json.dumps(self.seed, ensure_ascii=False), now_ms()))
        row = db.execute("SELECT data FROM gateway_config WHERE id=1").fetchone()
        self.config = parse_config(json.loads(row["data"])) if row else None

    def raw_config(self, db):
        row = db.execute("SELECT data FROM gateway_config WHERE id=1").fetchone()
        return json.loads(row["data"]) if row else {}

    def public_config(self, db):
        """Upstream settings for the admin panel; the upstream API key is never sent back."""
        return {name: {**{k: v for k, v in item.items() if k != "key"}, "keyHint": "…" + item["key"][-4:] if len(item["key"]) > 8 else "已设置"}
                for name, item in self.raw_config(db).items()}

    def save_upstreams(self, db, actor, body):
        """Replace the upstream list. An empty or missing "key" keeps the saved key for that upstream."""
        if not isinstance(body, dict) or set(body) != {"upstreams"} or not isinstance(body["upstreams"], dict):
            raise APIError(400, "上游设置格式不正确")
        old, new = self.raw_config(db), {}
        for name, item in body["upstreams"].items():
            if name not in {"openai", "anthropic"} or not isinstance(item, dict):
                raise APIError(400, "只支持 OpenAI 兼容与 Anthropic 两类上游")
            item = dict(item)
            if not item.get("key"):
                if name not in old:
                    raise APIError(400, f"{name} 上游需要填写 API Key")
                # Never send the saved key to a different server: changing the host requires re-entering it.
                if not isinstance(item.get("url"), str) or urlsplit(item["url"]).netloc.lower() != urlsplit(old[name]["url"]).netloc.lower():
                    raise APIError(400, f"{name} 上游地址已更改，请重新填写 API Key")
                item["key"] = old[name]["key"]
            for optional in ("name", "aliases", "defaultPrice"):
                if not item.get(optional):
                    item.pop(optional, None)
            new[name] = item
        try:
            config = parse_config(new)
        except ValueError as error:
            raise APIError(400, "上游设置不正确：" + str(error)) from None
        db.execute("INSERT INTO gateway_config VALUES (1,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at",
                   (json.dumps(new, ensure_ascii=False), now_ms()))
        self.service.audit(db, actor, "api-upstreams.save", metadata={name: {"url": item["url"], "models": list(item["models"]),
                                                                            "keyChanged": item["key"] != old.get(name, {}).get("key")}
                                                                     for name, item in new.items()})
        result = {"ok": True, "upstreams": self.public_config(db),
                  "models": {name: list(item["models"]) for name, item in (config or {}).items()}}
        # dispatch already holds the SQLite write transaction. Finish every DB operation before
        # publishing, and serialize commit + publication so a later save cannot publish first.
        # There must be no subsequent DB writes or fallible response building on this path.
        with self.config_publish_lock:
            db.commit()
            self.config = config
        return result

    def settings(self, db):
        return {r["key"][5:]: json.loads(r["value"])
                for r in db.execute("SELECT key,value FROM settings WHERE key LIKE 'keys.%'") if r["key"] in DEFAULTS}

    def models(self):
        # Config order is kept: the first model of each upstream is the default shown in setup snippets.
        return {name: list(item["models"]) for name, item in (self.config or {}).items()}

    def error_secrets(self, call=None):
        # Include the request's snapshot: an organizer can rotate or remove an upstream while a call is running.
        current = tuple(item["key"] for item in (self.config or {}).values())
        return current + tuple((call or {}).get("secrets", ()))

    def spend(self, db, team_id, since=0):
        return db.execute("SELECT COALESCE(SUM(cost_micro),0) FROM api_usage WHERE team_id=? AND at>=?", (team_id, since)).fetchone()[0] / 1e6

    def team_active(self, db, team_id):
        row = db.execute("SELECT active FROM team_api WHERE team_id=?", (team_id,)).fetchone()
        return bool(row and row["active"])

    # Website API (cookie sessions), runs inside the caller's SQLite transaction.
    def dispatch(self, db, actor, method, path, body):
        settings = self.settings(db)
        if path.startswith("/api/admin/"):
            self.service.require(actor, "admin")
            return self.admin(db, actor, method, path, body, settings)
        self.service.require(actor, "team")
        team_id = self.service.bound_submission_team(db, actor)['id']
        enabled = self.config is not None and self.team_active(db, team_id)
        if method == "GET" and path == "/api/team/keys":
            if not enabled:
                return {"enabled": False, "keys": []}
            since = now_ms() - DAY_MS
            keys = [{"id": r["id"], "name": r["name"], "masked": r["masked"], "createdAt": r["created_at"], "lastUsedAt": r["last_used_at"],
                     "used24h": r["day"] / 1e6, "usedTotal": r["total"] / 1e6, "requests": r["requests"]}
                    for r in db.execute("""SELECT k.*, COALESCE(SUM(CASE WHEN u.at>=? THEN u.cost_micro END),0) AS day,
                      COALESCE(SUM(u.cost_micro),0) AS total, COUNT(u.id) AS requests
                      FROM api_keys k LEFT JOIN api_usage u ON u.key_id=k.id WHERE k.team_id=? AND k.active=1
                      GROUP BY k.id ORDER BY k.created_at""", (since, team_id))]
            return {"enabled": True, "baseUrl": self.service.origin + self.service.base_path, "models": self.models(),
                    "maxKeys": settings["maxKeys"], "keys": keys, "logContent": settings["logContent"],
                    "announcement": settings["announcement"],
                    "usage": {"used24h": self.spend(db, team_id, since), "usedTotal": self.spend(db, team_id)},
                    "expiresAt": settings["expiresAt"]}
        if not enabled:
            raise APIError(409, "本队 API 访问权限尚未开通或已停用")
        if method == "GET" and path == "/api/team/keys/logs":
            items = self.logs(db, {"teamId": team_id})  # metadata only; content stays admin-only
            for item in items["items"]:  # teams see what failed and whose fault it was, not upstream internals
                for field in ("errorMessage", "upstreamStatus", "upstreamRequestId", "clientIp"):
                    item.pop(field)
            return items
        if method == "POST" and path == "/api/team/keys":
            if set(body) != {"name"}:
                raise APIError(400, "密钥字段不正确")
            name = self.text(body["name"], "密钥名称", 40, True)
            if db.execute("SELECT COUNT(*) FROM api_keys WHERE team_id=? AND active=1", (team_id,)).fetchone()[0] >= settings["maxKeys"]:
                raise APIError(409, f"每队最多 {settings['maxKeys']} 个密钥，请先删除不用的密钥")
            if db.execute("SELECT COUNT(*) FROM api_keys WHERE team_id=? AND created_at>?", (team_id, now_ms() - 3600_000)).fetchone()[0] >= KEY_CREATE_PER_HOUR:
                # As sub2api: deleting does not give the hourly creations back, so delete-and-recreate cannot loop.
                raise APIError(429, f"每队每小时最多创建 {KEY_CREATE_PER_HOUR} 个密钥，请稍后再试")
            key = "gk-" + secrets.token_urlsafe(32)
            key_id = "K" + secrets.token_hex(8)
            masked = key[:7] + "…" + key[-4:]
            db.execute("INSERT INTO api_keys(id,team_id,name,key_hash,masked,created_at) VALUES (?,?,?,?,?,?)",
                       (key_id, team_id, name, self.digest(key), masked, now_ms()))
            self.service.audit(db, actor, "api-key.create", team_id, {"keyId": key_id})
            # The only response that ever carries the full key; only its hash is stored.
            return {"ok": True, "key": key, "item": {"id": key_id, "name": name, "masked": masked}}
        if method == "POST" and path == "/api/team/keys/delete":
            if set(body) != {"id"} or not isinstance(body["id"], str):
                raise APIError(400, "密钥编号不正确")
            if not db.execute("UPDATE api_keys SET active=0 WHERE id=? AND team_id=? AND active=1", (body["id"], team_id)).rowcount:
                raise APIError(404, "密钥不存在")
            self.service.audit(db, actor, "api-key.delete", team_id, {"keyId": body["id"]})
            return {"ok": True}
        raise APIError(404, "接口不存在")

    def admin(self, db, actor, method, path, body, settings):
        if method == "GET" and path == "/api/admin/keys":
            since = now_ms() - DAY_MS
            rows = db.execute("""SELECT t.team_id, t.active,
              (SELECT COUNT(*) FROM api_keys k WHERE k.team_id=t.team_id AND k.active=1) AS key_count FROM team_api t""").fetchall()
            today = beijing_midnight()
            overview = dict(db.execute("""SELECT COUNT(*) AS requests, COALESCE(SUM(cost_micro),0)/1e6 AS cost,
              COALESCE(SUM(input_tokens),0) AS input, COALESCE(SUM(output_tokens),0) AS output,
              COALESCE(SUM(cache_write_tokens),0) AS cacheWrite, COALESCE(SUM(cache_read_tokens),0) AS cacheRead,
              COALESCE(SUM(status>=400 OR incomplete=1),0) AS errors,
              COUNT(DISTINCT team_id) AS teams, CAST(AVG(CASE WHEN status<400 THEN duration_ms END) AS INTEGER) AS avgMs
              FROM api_usage WHERE at>=?""", (today,)).fetchone())
            recent = db.execute("SELECT COUNT(*), COALESCE(SUM(input_tokens+output_tokens+cache_write_tokens+cache_read_tokens),0) FROM api_usage WHERE at>=?",
                                (now_ms() - 300_000,)).fetchone()
            overview["rpm"], overview["tpm"] = round(recent[0] / 5, 1), round(recent[1] / 5)  # last 5 minutes, as sub2api
            ok = db.execute("SELECT COUNT(*) FROM api_usage WHERE at>=? AND status<400 AND incomplete=0", (today,)).fetchone()[0]
            overview["p95Ms"] = db.execute("""SELECT duration_ms FROM api_usage WHERE at>=? AND status<400 AND incomplete=0
              ORDER BY duration_ms LIMIT 1 OFFSET ?""", (today, max(0, int(ok * 0.95) - 1))).fetchone()[0] if ok else None
            overview["avgFirstTokenMs"] = db.execute("SELECT CAST(AVG(first_token_ms) AS INTEGER) FROM api_usage WHERE at>=?", (today,)).fetchone()[0]
            storage = dict(db.execute("SELECT COUNT(*) AS rows, COALESCE(SUM(length(request)+length(response)),0) AS bytes FROM api_logs").fetchone())
            changes = [{"at": r["at"], "action": r["action"], "target": r["target"], "metadata": json.loads(r["metadata"])}
                       for r in db.execute("SELECT at,action,target,metadata FROM audit WHERE action LIKE 'api-%' ORDER BY id DESC LIMIT 50")]
            with self.lock:
                inflight = {k: v for k, v in self.inflight.items() if v}
                waiting = {k: v for k, v in self.waiting.items() if v}
                hour_ago = time.monotonic() - 3600
                overview_auth = {"authFailuresHour": sum(1 for t in self.auth_failures_seen if t > hour_ago),
                                 "blockedClients": sum(1 for t in self.auth_blocked.values() if t > time.monotonic())}
            overview.update(overview_auth)
            return {"enabled": self.config is not None, "settings": settings, "models": self.models(), "overview": overview,
                    "upstreams": self.public_config(db), "storage": storage,
                    "changes": changes,
                    "teams": [{"teamId": r["team_id"], "active": bool(r["active"]), "keyCount": r["key_count"], "inflight": inflight.get(r["team_id"], 0), "waiting": waiting.get(r["team_id"], 0),
                               "used24h": self.spend(db, r["team_id"], since), "usedTotal": self.spend(db, r["team_id"])} for r in rows]}
        if method == "GET" and path == "/api/admin/keys/export":
            self.service.audit(db, actor, "api-usage.export")
            return self.export(db)
        if method == "POST" and path == "/api/admin/keys/settings":
            if not body or not set(body).issubset({"maxKeys", "expiresAt", "logContent", "teamRpm",
                                                   "announcement", "blockedWords", "logRetentionHours", "logMaxMB"}):
                raise APIError(400, "API 设置字段不正确")
            before = dict(settings)
            for key, value in body.items():
                if key == "logRetentionHours":
                    ok = type(value) is int and 1 <= value <= 8760
                elif key == "logMaxMB":
                    ok = type(value) is int and 50 <= value <= 102400
                elif key == "teamRpm":
                    ok = type(value) is int and 0 <= value <= 10000
                elif key == "announcement":
                    ok = isinstance(value, str) and len(value) <= 500
                elif key == "blockedWords":
                    ok = isinstance(value, list) and len(value) <= 200 and all(isinstance(w, str) and 1 <= len(w.strip()) <= 100 for w in value)
                    value = [w.strip() for w in value] if ok else value
                elif key == "logContent":
                    ok = type(value) is bool
                elif key == "maxKeys":
                    ok = type(value) is int and 1 <= value <= 20
                elif key == "expiresAt":
                    ok = value is None or (type(value) is int and 0 <= value <= 253402300799999)
                if not ok:
                    raise APIError(400, {"maxKeys": "每队密钥上限应为1至20的整数", "expiresAt": "到期时间必须是有效的毫秒时间戳或空值", "logContent": "记录内容开关必须为布尔值",
                                               "teamRpm": "每分钟请求上限应为0至10000的整数（0为不限）", "announcement": "公告最多500字",
                                               "blockedWords": "屏蔽词最多200个，每个1至100字",
                                               "logRetentionHours": "调用内容保留时长应为1至8760小时", "logMaxMB": "调用内容总容量应为50至102400MB"}.get(key, "API 设置值不正确"))
                db.execute("UPDATE settings SET value=? WHERE key=?", (json.dumps(value), "keys." + key))
            changed = {k: [before.get(k), v] for k, v in self.settings(db).items() if k in body and before.get(k) != v}
            self.service.audit(db, actor, "api-key.settings", metadata=changed)
            return {"ok": True, "settings": self.settings(db)}
        if method == "POST" and path == "/api/admin/keys/logs/clear":
            deleted = self.prune_logs(db, settings, everything=True)
            self.service.audit(db, actor, "api-log.clear", metadata={"deleted": deleted})
            return {"ok": True, "deleted": deleted}
        if method == "GET" and path.startswith(("/api/admin/keys/upstreams/test/", "/api/admin/keys/upstreams/models/")):
            # GET on purpose: these call the upstream, and POSTs run inside a write transaction that would block others.
            kind = path.rsplit("/", 1)[1]
            if not self.config or kind not in self.config:
                raise APIError(404, "该类上游尚未配置")
            result = self.probe_upstream(kind, models_only=path.startswith("/api/admin/keys/upstreams/models/"))
            self.service.audit(db, actor, "api-upstreams.test", kind, {"ok": result["ok"], "status": result.get("status")})
            return result
        if method == "POST" and path == "/api/admin/keys/upstreams":
            return self.save_upstreams(db, actor, body)
        if method == "POST" and path == "/api/admin/keys/stats":
            return self.stats(db, body)
        if method == "POST" and path == "/api/admin/keys/logs":
            return self.logs(db, body)
        if method == "POST" and path == "/api/admin/keys/log":
            if set(body) != {"id"} or type(body["id"]) is not int:
                raise APIError(400, "记录编号不正确")
            row = db.execute("SELECT l.*, u.request_id FROM api_logs l JOIN api_usage u ON u.id=l.usage_id WHERE l.usage_id=?", (body["id"],)).fetchone()
            if not row:
                raise APIError(404, "该请求没有记录内容")
            self.service.audit(db, actor, "api-log.view", str(body["id"]))
            return {"id": body["id"], "requestId": row["request_id"], "headers": json.loads(row["headers"]), "request": unpack(row["request"]), "response": unpack(row["response"]),
                    "requestBytes": row["request_bytes"], "responseBytes": row["response_bytes"],
                    "responseTruncated": row["response_bytes"] > RESPONSE_LOG_CAP}
        if method == "POST" and path in {"/api/admin/keys/provision", "/api/admin/keys/revoke"}:
            if set(body) != {"teamId"} or not isinstance(body["teamId"], str):
                raise APIError(400, "队伍不正确")
            team_id = self.service.team_exists(db, body["teamId"])
            if path.endswith("/revoke"):
                if not db.execute("UPDATE team_api SET active=0 WHERE team_id=? AND active=1", (team_id,)).rowcount:
                    raise APIError(409, "该队 API 访问权限未开通")
            else:
                self.service.team_lifecycle.require_available(db,team_id)
                if self.config is None:
                    raise APIError(409, "尚未配置模型上游，请先在“模型上游”中填写")
                if self.team_active(db, team_id):
                    raise APIError(409, "该队 API 访问权限已开通")
                db.execute("INSERT INTO team_api(team_id,active,created_at) VALUES (?,1,?) ON CONFLICT(team_id) DO UPDATE SET active=1", (team_id, now_ms()))
            self.service.audit(db, actor, "api-key." + path.rsplit("/", 1)[1], team_id)
            return {"ok": True}
        raise APIError(404, "接口不存在")

    def detect_models(self, body, cookie, expected_login_id):
        """Read a draft upstream's catalog without saving configuration or holding the writer lock."""
        with self.service.db() as db:
            self.service.require(self.service.account(db, cookie), "admin")
            if not expected_login_id:
                raise APIError(428, "请刷新页面并核对管理员登录身份")
            if expected_login_id != self.service.login_id(cookie):
                raise APIError(401, "登录身份已变化，请重新登录后操作")
            if set(body) != {"kind", "url", "key"} or not isinstance(body["kind"], str) or body["kind"] not in {"openai", "anthropic"}:
                raise APIError(400, "模型列表请求字段不正确")
            kind, key = body["kind"], body["key"]
            try:
                url = check_url(body["url"])
                parts = urlsplit(url)
                if parts.username is not None or parts.password is not None:
                    raise ValueError("credentials in URL")
                parts.port  # Validate explicit ports before constructing an upstream request.
            except (ValueError, TypeError):
                raise APIError(400, "接口地址应为 HTTPS Base URL（本机地址可用 HTTP），不能含查询参数或账号密码") from None
            if not isinstance(key, str) or len(key) > 4096 or any(ord(c) < 32 or ord(c) > 126 for c in key):
                raise APIError(400, "API Key 格式不正确")
            key = key.strip()
            if not key:
                saved = self.raw_config(db).get(kind)
                if not saved:
                    raise APIError(400, "请先填写 API Key")
                if urlsplit(url).netloc.lower() != urlsplit(saved["url"]).netloc.lower():
                    raise APIError(400, "上游地址已更改，请重新填写 API Key")
                key = saved["key"]
        return self.probe_upstream(kind, models_only=True, upstream={"url": url, "key": key})

    def probe_upstream(self, kind, models_only=False, upstream=None):
        """Probe a saved or draft upstream: generate one token or read its model catalog."""
        upstream = upstream if upstream is not None else self.config[kind]
        headers = {"Content-Type": "application/json", "User-Agent": "gotc-gateway/1.0"}
        if kind == "openai":
            headers["Authorization"] = "Bearer " + upstream["key"]
        else:
            headers["x-api-key"], headers["anthropic-version"] = upstream["key"], "2023-06-01"
        if models_only:
            request = urllib.request.Request(upstream["url"] + "/models", headers=headers, method="GET")
        else:
            model = next(iter(upstream["models"]))
            body = {"model": model, "max_tokens": 1, "messages": [{"role": "user", "content": "hi"}]}
            request = urllib.request.Request(upstream["url"] + ("/chat/completions" if kind == "openai" else "/messages"),
                                             data=gateway_json(body), headers=headers, method="POST")
        started = time.monotonic()
        try:
            with self.opener.open(request, timeout=30) as response:
                status, payload = response.status, response.read(2_000_001)
        except urllib.error.HTTPError as error:
            status, payload = error.code, error.read(100_000) if error.fp else b""
        except (OSError, http.client.HTTPException) as error:
            return {"ok": False, "status": None, "latencyMs": int((time.monotonic() - started) * 1000), "message": failure_reason(error)}
        latency = int((time.monotonic() - started) * 1000)
        if len(payload) > 2_000_000:
            return {"ok": False, "status": status, "latencyMs": latency, "message": "上游返回内容过大，请手动填写模型"}
        if status >= 400:
            return {"ok": False, "status": status, "latencyMs": latency,
                    "message": upstream_message(payload, self.error_secrets() + (upstream["key"],))}
        if not models_only:
            return {"ok": True, "status": status, "latencyMs": latency, "model": model, "message": "连接正常，上游已返回回复"}
        try:
            listed = json.loads(payload).get("data")
        except (ValueError, AttributeError, RecursionError):
            listed = None
        ids = sorted({m["id"] for m in listed if isinstance(m, dict) and isinstance(m.get("id"), str)
                      and 0 < len(m["id"]) <= 100 and not any(c.isspace() or ord(c) < 32 for c in m["id"])}) if isinstance(listed, list) else []
        return {"ok": bool(ids), "status": status, "latencyMs": latency, "models": ids[:500],
                "message": f"上游提供 {len(ids)} 个模型" if ids else "上游未返回模型列表"}

    def scope(self, db, body, allowed):
        """Shared filter for stats and logs: optional teamId / keyId."""
        if not isinstance(body, dict) or not set(body).issubset(allowed):
            raise APIError(400, "筛选条件不正确")
        where, params = [], []
        if body.get("teamId") is not None:
            where.append("u.team_id=?")
            params.append(self.service.team_exists(db, body["teamId"]))
        if body.get("keyId") is not None:
            if not isinstance(body["keyId"], str):
                raise APIError(400, "密钥编号不正确")
            where.append("u.key_id=?")
            params.append(body["keyId"])
        return where, params

    def stats(self, db, body):
        where, params = self.scope(db, body, {"teamId", "keyId", "days", "granularity"})
        granularity = body.get("granularity", "day")
        if granularity not in ("day", "hour"):
            raise APIError(400, "统计粒度只能是 day 或 hour")
        bucket = "date(u.at/1000+28800,'unixepoch')" if granularity == "day" else "strftime('%Y-%m-%d %H:00',u.at/1000+28800,'unixepoch')"
        days = body.get("days", 7)
        if type(days) is not int or not 1 <= days <= 90:
            raise APIError(400, "统计天数应为1至90")
        where.append("u.at>=?")
        params.append(beijing_midnight() - (days - 1) * DAY_MS)  # whole Beijing calendar days, today included
        clause = " AND ".join(where)
        sums = ("COUNT(*) AS requests, COALESCE(SUM(u.input_tokens),0) AS input, COALESCE(SUM(u.output_tokens),0) AS output, "
                "COALESCE(SUM(u.cache_write_tokens),0) AS cacheWrite, COALESCE(SUM(u.cache_read_tokens),0) AS cacheRead, "
                "COALESCE(SUM(u.cost_micro),0)/1e6 AS cost")
        rows = lambda sql: [dict(r) for r in db.execute(sql, params)]
        return {
            "days": days,
            "totals": rows(f"SELECT {sums}, SUM(u.status>=400 OR u.incomplete=1) AS errors, "
                           f"CAST(AVG(CASE WHEN u.status<400 AND u.incomplete=0 THEN u.duration_ms END) AS INTEGER) AS avgMs, "
                           f"CAST(AVG(u.first_token_ms) AS INTEGER) AS avgFirstTokenMs FROM api_usage u WHERE {clause}")[0],
            "byKey": rows(f"""SELECT u.key_id AS keyId, k.name, k.masked, k.active, t.number AS teamNumber, t.name AS teamName, {sums},
              MAX(u.at) AS lastAt FROM api_usage u JOIN api_keys k ON k.id=u.key_id JOIN teams t ON t.id=u.team_id
              WHERE {clause} GROUP BY u.key_id ORDER BY cost DESC"""),
            "byModel": rows(f"SELECT u.model, {sums} FROM api_usage u WHERE {clause} GROUP BY u.model ORDER BY cost DESC"),
            "byFailure": rows(f"""SELECT u.error_owner AS owner, u.error_phase AS phase, u.error_type AS type, u.upstream_status AS upstreamStatus,
              {sums} FROM api_usage u WHERE {clause} AND (u.status>=400 OR u.incomplete=1) GROUP BY owner, phase, type, upstreamStatus
              ORDER BY requests DESC"""),
            "byClient": rows(f"SELECT u.user_agent AS userAgent, {sums} FROM api_usage u WHERE {clause} GROUP BY u.user_agent ORDER BY requests DESC LIMIT 20"),
            # Calendar days in Beijing time.
            "granularity": granularity,  # Beijing time buckets
            "daily": rows(f"SELECT {bucket} AS day, {sums} FROM api_usage u WHERE {clause} GROUP BY day ORDER BY day"),
        }

    def logs(self, db, body):
        where, params = self.scope(db, body, {"teamId", "keyId", "beforeId", "errorsOnly", "model", "requestId"})
        for field, column in (("model", "u.model"), ("requestId", "u.request_id")):
            value = body.get(field)
            if value not in (None, ""):
                if not isinstance(value, str) or len(value) > 100:
                    raise APIError(400, "筛选条件不正确")
                where.append(f"({column}=? OR u.requested_model=?)" if field == "model" else f"{column}=?")
                params.extend([value.strip(), value.strip()] if field == "model" else [value.strip()])
        if body.get("beforeId") is not None:
            if type(body["beforeId"]) is not int:
                raise APIError(400, "分页参数不正确")
            where.append("u.id<?")
            params.append(body["beforeId"])
        if body.get("errorsOnly") is True:
            where.append("(u.status>=400 OR u.incomplete=1)")
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        items = [dict(r) for r in db.execute(f"""SELECT u.id, u.at, u.key_id AS keyId, k.name AS keyName, t.number AS teamNumber, t.name AS teamName,
          u.model, u.path, u.stream, u.status, u.input_tokens AS input, u.output_tokens AS output, u.cache_write_tokens AS cacheWrite,
          u.cache_read_tokens AS cacheRead, u.cache_write_1h_tokens AS cacheWrite1h, u.cost_micro/1e6 AS cost,
          u.requested_model AS requestedModel, u.first_token_ms AS firstTokenMs, u.client_ip AS clientIp,
          u.upstream_request_id AS upstreamRequestId, u.request_id AS requestId,
          u.duration_ms AS durationMs, u.user_agent AS userAgent, u.incomplete, u.estimated, u.error_type AS errorType,
          u.error_phase AS errorPhase, u.error_owner AS errorOwner, u.upstream_status AS upstreamStatus, u.error_message AS errorMessage, EXISTS(SELECT 1 FROM api_logs l WHERE l.usage_id=u.id) AS hasContent,
          (SELECT l.request_bytes FROM api_logs l WHERE l.usage_id=u.id) AS requestBytes
          FROM api_usage u JOIN api_keys k ON k.id=u.key_id JOIN teams t ON t.id=u.team_id {clause} ORDER BY u.id DESC LIMIT 50""", params)]
        return {"items": items, "more": len(items) == 50}

    def export(self, db):
        """Usage CSV without request content; formula-like cells are neutralised."""
        out = io.StringIO(newline="")
        writer = csv.writer(out)
        writer.writerow(["时间(UTC毫秒)", "队号", "队名", "密钥", "模型", "接口", "流式", "状态", "输入tokens", "输出tokens", "缓存写入tokens", "缓存读取tokens", "费用USD", "耗时ms", "User-Agent", "中断", "估算计费",
                         "错误类型", "错误阶段", "责任方", "上游状态", "错误信息",
                         "其中1小时缓存写入tokens", "请求模型", "首字耗时ms", "客户端IP", "上游请求ID"])
        for r in db.execute("""SELECT u.*, t.number, t.name AS team_name, k.name AS key_name FROM api_usage u
          JOIN teams t ON t.id=u.team_id JOIN api_keys k ON k.id=u.key_id ORDER BY u.id"""):
            writer.writerow([self.service.csv_safe(v) for v in (r["at"], r["number"], r["team_name"], r["key_name"], r["model"], r["path"],
                             r["stream"], r["status"], r["input_tokens"], r["output_tokens"], r["cache_write_tokens"], r["cache_read_tokens"], r["cost_micro"] / 1e6, r["duration_ms"], r["user_agent"], r["incomplete"], r["estimated"],
                             r["error_type"], r["error_phase"], r["error_owner"], r["upstream_status"], r["error_message"],
                             r["cache_write_1h_tokens"], r["requested_model"], r["first_token_ms"], r["client_ip"], r["upstream_request_id"])])
        return ("\ufeff" + out.getvalue()).encode("utf-8")

    def admit(self, team, rpm, slot=True):
        """Per-team requests-per-minute and concurrency gate; caller must release() when slot=True.

        As sub2api: waiters are capped (QUEUE_CAP), and a request counts towards the RPM only once it actually proceeds,
        so timeouts and queue rejections do not use up the minute. count_tokens takes an RPM entry but no slot.
        """
        def window_checked(now):
            window = self.recent[team]
            while window and window[0] <= now - 60:
                window.popleft()
            if rpm and len(window) >= rpm:
                raise GatewayError("rate_limit", f"本队每分钟最多 {rpm} 次请求，请稍后重试", retry_after=max(1, int(window[0] + 60 - now) + 1))
            return window

        with self.lock:
            window_checked(time.monotonic())
            if slot and self.inflight[team] >= TEAM_CONCURRENCY:
                if self.waiting[team] >= QUEUE_CAP:
                    raise GatewayError("rate_limit", f"本队排队请求已满（最多 {QUEUE_CAP} 个），请稍后重试", retry_after=2)
                self.waiting[team] += 1
                try:
                    # Agent tools fire bursts; queue briefly for a slot instead of failing at once.
                    ready = self.slot_free.wait_for(lambda: self.inflight[team] < TEAM_CONCURRENCY, timeout=QUEUE_WAIT)
                finally:
                    self.waiting[team] -= 1
                if not ready:
                    raise GatewayError("rate_limit", f"本队同时进行的请求最多 {TEAM_CONCURRENCY} 个，排队超时", retry_after=2)
            now = time.monotonic()
            window_checked(now).append(now)
            if slot:
                self.inflight[team] += 1

    def recheck_access(self, handler, call):
        """Re-check key, team and expiry after waiting for a concurrency slot."""
        with self.service.db() as db:
            row = self.lookup(db, handler)
            if not row or row["id"] != call["key"]["id"]:
                raise APIError(401, "API 密钥在排队期间已被停用")
            settings = self.settings(db)
            if settings["expiresAt"] is not None and now_ms() >= settings["expiresAt"]:
                raise GatewayError("billing", "本活动 API 访问权限已到期")

    def release(self, team):
        with self.lock:
            self.inflight[team] -= 1
            self.slot_free.notify_all()

    # Gateway (API keys from SDKs, no cookies or Origin).
    def gateway(self, handler, path):
        call = {"key": None, "path": path, "model": "", "stream": False, "price": None, "usage": [0] * USAGE_FIELDS, "status": 0,
                "request": b"", "response": [], "size": 0, "started": time.monotonic(), "log": False,
                "request_bytes": 0, "reply_bytes": 0, "done": False, "forwarded": False,
                "request_id": uuid.uuid4().hex, "reply_headers": {}, "secrets": self.error_secrets()}
        try:
            query = parse_qs(urlsplit(handler.path).query)
            if "key" in query or "api_key" in query:
                # As sub2api: a key in the URL ends up in proxy and browser logs, so it is refused outright.
                raise GatewayError("invalid", "不支持在网址中携带 API 密钥，请改用 Authorization 或 x-api-key 请求头")
            self.serve(handler, path, call)
        except ClientGone:
            handler.close_connection = True  # the team's client went away; the call was already recorded
        except Exception as error:
            if not isinstance(error, APIError):
                # A gateway bug or unexpected failure: never leave a row with status 0, and tell the team something.
                traceback.print_exc()
                error = GatewayError("internal", "网关内部错误，请联系组织方")
            info = self.note_error(call, error)
            self.safe_record(handler, call)
            handler.close_connection = True
            if not call.get("headers_sent"):
                try:
                    payload = json.dumps(error_body(path, info), ensure_ascii=False).encode()
                    extra = {"X-Client-Request-ID": call["request_id"]}
                    if info.get("retry_after"):
                        extra["Retry-After"] = str(info["retry_after"])
                    handler.headers_out(error.status, "application/json; charset=utf-8", len(payload), extra=extra)
                    handler.wfile.write(payload)
                except OSError:
                    pass
        finally:
            self.safe_record(handler, call)  # no-op when already recorded or unauthenticated

    def note_error(self, call, error):
        """Attach the final status and classification to a call that has not been written yet."""
        info = error_info(error)
        if not call.get("recorded"):
            call["status"], call["error"] = error.status, info
            detail = (f"上游 HTTP {info['upstream_status']}：" if info["upstream_status"] else "") + (info["detail"] or info["upstream_message"])
            detail = detail + "\n" if detail else ""
            self.keep(call, (detail + error.message).encode())
        return info

    def client_gone(self, handler):
        """Whether the team's client has closed its connection (its request body was already read in full)."""
        try:
            readable, _, _ = select.select([handler.connection], [], [], 0)
            return bool(readable) and handler.connection.recv(1, socket.MSG_PEEK) == b""
        except (OSError, ValueError):
            return True

    def prune_logs(self, db, settings, everything=False):
        """Delete stored request content past the retention period, then oldest first above the size cap.
        Call metadata (tokens, cost, status) is retained for usage statistics."""
        if everything:
            return db.execute("DELETE FROM api_logs").rowcount
        cutoff = now_ms() - settings["logRetentionHours"] * 3600_000
        deleted = db.execute("DELETE FROM api_logs WHERE usage_id IN (SELECT id FROM api_usage WHERE at<?)", (cutoff,)).rowcount
        cap = settings["logMaxMB"] * 1048576
        total = db.execute("SELECT COALESCE(SUM(length(request)+length(response)),0) FROM api_logs").fetchone()[0]
        while total > cap:
            rows = db.execute("SELECT usage_id, length(request)+length(response) AS size FROM api_logs ORDER BY usage_id LIMIT 500").fetchall()
            if not rows:
                break
            ids = []
            for r in rows:
                ids.append(r["usage_id"])
                total -= r["size"]
                if total <= cap:
                    break
            db.execute(f"DELETE FROM api_logs WHERE usage_id IN ({','.join('?' * len(ids))})", ids)
            deleted += len(ids)
        return deleted

    def safe_record(self, handler, call):
        """record(), but a storage failure must not hide the original error or leave the client without a reply."""
        try:
            self.record(handler, call)
        except Exception:
            traceback.print_exc()

    def send(self, handler, call, status, content_type, payload=None, length=None):
        """Write to the team's client; a closed connection becomes ClientGone so it is never mistaken for an upstream error."""
        try:
            if not call.get("headers_sent"):
                extra = {"X-Client-Request-ID": call["request_id"], **call["reply_headers"]}
                if payload is None:
                    extra["X-Accel-Buffering"] = "no"
                handler.headers_out(status, content_type, len(payload) if length is None and payload is not None else length, extra=extra)
                call["headers_sent"] = True
            if payload:
                handler.wfile.write(payload)
        except OSError:
            raise ClientGone() from None

    def keep(self, call, data):
        if call["log"]:
            if call["size"] < RESPONSE_LOG_CAP:
                call["response"].append(data)
            call["size"] += len(data)  # true size, even past the cap

    def record(self, handler, call):
        """Store the call once, before the final bytes are sent where possible, so history is never behind the client."""
        if not call["key"] or call.get("recorded"):
            return
        if not call["status"]:
            call["status"] = 500  # defensive: every stored call has a real outcome
        usage, price = list(call["usage"]), call["price"] or (0,) * USAGE_FIELDS
        # Billing follows whether the upstream generated output ("forwarded"), never the recorded status: a stream that
        # failed after output started is a 502 for the record but its tokens were still produced.
        incomplete = call["forwarded"] and not call["done"]
        estimated = False
        if call.get("charge_estimate"):
            usage, incomplete, estimated = [call.get("input_estimate", call["request_bytes"] // 4), call.get("output_estimate", 0), 0, 0, 0], True, True
        elif call["forwarded"]:
            # sub2api bills only what it observed; a call with no usage would then be missing from cost statistics.
            # Fill gaps with an estimate instead: input ~ request bytes / 4, output ~ reply text bytes / 4.
            est_in, est_out = call.get("input_estimate", call["request_bytes"] // 4), call["reply_bytes"] // 4
            if not any(usage):
                usage[0], usage[1], estimated = est_in, est_out, True
            elif incomplete and usage[1] < est_out:
                usage[1], estimated = est_out, True
        cost = round(sum(t * p for t, p in zip(usage, price)))  # USD per 1M tokens == micro-USD per token
        error = call.get("error") or {"type": "", "phase": "", "owner": "", "upstream_status": None, "upstream_message": "", "message": ""}
        at = now_ms()
        with self.service.db() as db:
            usage_id = db.execute("""INSERT INTO api_usage(key_id,team_id,at,model,input_tokens,output_tokens,cache_write_tokens,cache_read_tokens,
              cost_micro,status,path,stream,duration_ms,user_agent,incomplete,estimated,error_type,error_phase,error_owner,
              upstream_status,error_message,cache_write_1h_tokens,requested_model,first_token_ms,client_ip,upstream_request_id,request_id)
              VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                  (call["key"]["id"], call["key"]["team_id"], at, call["model"], usage[0], usage[1], usage[2] + usage[4], usage[3], cost,
                                                     call["status"], call["path"], int(call["stream"]), int((time.monotonic() - call["started"]) * 1000),
                                                     (handler.headers.get("User-Agent") or "")[:300], int(incomplete), int(estimated),
                                                     error["type"], error["phase"], error["owner"], error["upstream_status"],
                                                     (error.get("detail") or error["upstream_message"] or error["message"])[:500], usage[4], call.get("requested_model", "")[:100],
                                                     call.get("stream_first_ms") if call["stream"] else call.get("first_token_ms"),
                                                     client_ip(handler), call.get("upstream_request_id", "")[:100], call["request_id"])).lastrowid
            db.execute("UPDATE api_keys SET last_used_at=? WHERE id=?", (at, call["key"]["id"]))
            if call["log"] and time.monotonic() - self.last_prune > 600:  # amortised: at most every 10 minutes
                self.last_prune = time.monotonic()
                self.prune_logs(db, call.get("settings") or self.settings(db))
            if call["log"]:
                headers = {k: v[:500] for k, v in handler.headers.items() if k.lower() not in SECRET_HEADERS}
                response = b"".join(call["response"])[:RESPONSE_LOG_CAP]
                # Full context, compressed: agent requests resend the whole conversation and compress ~5-10x.
                db.execute("INSERT INTO api_logs VALUES (?,?,?,?,?,?)", (usage_id, json.dumps(headers, ensure_ascii=False),
                           zlib.compress(call["request"], 6), zlib.compress(response, 6), len(call["request"]), call["size"]))
        # Only suppress the finalization retry after the entire transaction has committed.
        call["recorded"] = True

    def lookup(self, db, handler):
        parts = handler.headers.get("Authorization", "").split(None, 1)
        token = parts[1].strip() if len(parts) == 2 and parts[0].lower() == "bearer" else handler.headers.get("x-api-key", "").strip()
        if not token:
            raise GatewayError("auth", "缺少 API 密钥")
        if len(token) > 200:
            return None  # longer than any key we issue; not worth hashing
        # Keys stop with the team: API access disabled, team login revoked, or team withdrawn/disqualified.
        return db.execute("""SELECT k.id, k.team_id FROM api_keys k JOIN team_api t ON t.team_id=k.team_id
          JOIN team_accounts ta ON ta.team_id=k.team_id JOIN accounts a ON a.id=ta.account_id AND a.active=1
          LEFT JOIN team_details d ON d.team_id=k.team_id
          WHERE k.key_hash=? AND k.active=1 AND t.active=1 AND COALESCE(d.status,'active')='active'""", (self.digest(token),)).fetchone()

    def auth_guard(self, handler):
        """sub2api's invalid-auth limiter, applied only to failures: a hackathon venue puts every team behind one NAT
        address, so a valid key is never refused because of someone else's bad ones."""
        ip = client_ip(handler)
        try:
            if ipaddress.ip_address(ip).version == 6:
                ip = str(ipaddress.ip_network(f"{ip}/64", strict=False))  # an IPv6 /64 is one client
        except ValueError:
            pass
        return ip

    def auth_blocked_error(self, ip):
        now = time.monotonic()
        with self.lock:
            blocked = self.auth_blocked.get(ip, 0)
        if blocked > now:
            return GatewayError("rate_limit", "密钥错误次数过多，请稍后再试", retry_after=int(blocked - now) + 1)
        return None

    def auth_failed(self, ip):
        now = time.monotonic()
        with self.lock:
            self.auth_failures_seen.append(now)
            failures = self.auth_failures[ip]
            while failures and failures[0] <= now - AUTH_FAIL_WINDOW:
                failures.popleft()
            failures.append(now)
            if len(failures) >= AUTH_FAIL_LIMIT:
                self.auth_blocked[ip] = now + AUTH_BLOCK
                failures.clear()
            if len(self.auth_failures) > 16384:  # bounded memory, as sub2api
                self.auth_failures = defaultdict(deque, {k: v for k, v in self.auth_failures.items() if v and v[-1] > now - AUTH_FAIL_WINDOW})
                self.auth_blocked = {k: v for k, v in self.auth_blocked.items() if v > now}

    def authenticate(self, handler, call=None):
        ip = self.auth_guard(handler)
        with self.service.db() as db:
            try:
                row = self.lookup(db, handler)
            except APIError as error:
                blocked = self.auth_blocked_error(ip)  # blocked from the attempt after the limit, as sub2api
                self.auth_failed(ip)
                raise blocked or error
            if not row:
                blocked = self.auth_blocked_error(ip)
                self.auth_failed(ip)
                raise blocked or GatewayError("auth", "API 密钥无效或已停用")
            settings = self.settings(db)
            if call is not None:
                call["key"], call["log"], call["settings"] = row, settings["logContent"], settings
            if call is not None:
                current = now_ms()
                if settings["expiresAt"] is not None and current >= settings["expiresAt"]:
                    raise GatewayError("billing", "本活动 API 访问权限已到期")
        return row

    def read_body(self, handler):
        if handler.headers.get("Transfer-Encoding") or len(handler.headers.get_all("Content-Length", [])) != 1:
            raise APIError(411, "请求需要 Content-Length")
        try:
            length = int(handler.headers["Content-Length"])
        except ValueError:
            raise APIError(400, "请求长度不正确") from None
        if not 0 < length <= BODY_LIMIT:
            raise GatewayError("too_large", f"请求内容超过 {BODY_LIMIT // 1048576}MB 限制")
        if handler.headers.get_content_type() != "application/json":
            raise APIError(415, "请使用JSON请求")
        raw = handler.rfile.read(length)
        encoding = (handler.headers.get("Content-Encoding") or "identity").strip().lower()
        if encoding in ("gzip", "deflate"):
            # As sub2api: compressed request bodies are accepted; the expanded size is bounded (no decompression bombs).
            try:
                inflater = zlib.decompressobj(47 if encoding == "gzip" else 15)
                expanded = inflater.decompress(raw, BODY_LIMIT + 1)
            except zlib.error:
                raise APIError(400, "请求内容解压失败") from None
            if len(expanded) > BODY_LIMIT or inflater.unconsumed_tail:
                raise GatewayError("too_large", f"解压后的请求内容超过 {BODY_LIMIT // 1048576}MB 限制")
            raw = expanded
        elif encoding != "identity":
            raise APIError(415, f"不支持的请求压缩格式：{encoding}")
        handler.raw_body = raw

        def reject_constant(name):
            raise ValueError(name)
        try:
            body = json.loads(raw, parse_constant=reject_constant)
            validate_json_depth(body)
            gateway_json(body)  # 1e400 parses to inf: refuse here rather than send invalid JSON upstream
        except (UnicodeError, ValueError, RecursionError):
            raise APIError(400, "JSON格式不正确") from None
        if not isinstance(body, dict):
            raise APIError(400, "JSON内容必须是对象")
        return body

    def serve(self, handler, path, call):
        # A live upstream edit applies to subsequent calls; this request keeps one coherent snapshot.
        config = self.config
        if config is None:
            raise APIError(404, "接口不存在")
        if (path == "/v1/models" or path.startswith("/v1/models/")) and handler.command == "GET":
            self.authenticate(handler)  # not metered or logged
            # Fields of both APIs, so OpenAI and Anthropic SDKs can each parse the list (sub2api answers per platform).
            data = [{"id": m, "object": "model", "owned_by": name, "created": 0, "type": "model", "display_name": m,
                     "created_at": "2024-01-01T00:00:00Z"} for name, item in config.items() for m in item["models"]]
            if path != "/v1/models":
                item = next((d for d in data if d["id"] == path[len("/v1/models/"):]), None)
                if item is None:
                    raise GatewayError("not_found", "模型不在本活动开放列表中")
                return self.send(handler, call, 200, "application/json", gateway_json(item))
            listing = {"object": "list", "data": data, "has_more": False,
                       "first_id": data[0]["id"] if data else None, "last_id": data[-1]["id"] if data else None}
            return self.send(handler, call, 200, "application/json", gateway_json(listing))
        if path == "/v1/messages/count_tokens" and handler.command == "POST" and "anthropic" in config:
            key = self.authenticate(handler, call)  # expiry
            call["log"] = False  # metadata row only, zero cost
            team = key["team_id"]
            self.admit(team, call["settings"]["teamRpm"] or COUNT_TOKENS_RPM, slot=False)  # no team slot, as sub2api
            with self.lock:  # but its own small pool, so free calls cannot flood the upstream with our key
                if self.counting[team] >= COUNT_TOKENS_CONCURRENCY:
                    raise GatewayError("rate_limit", "本队同时进行的 token 计数请求过多，请稍后重试", retry_after=1)
                self.counting[team] += 1
            try:
                self.recheck_access(handler, call)
                return self.count_tokens(handler, call, config["anthropic"])
            finally:
                with self.lock:
                    self.counting[team] -= 1
        name, suffix = ROUTES.get(path, (None, None))
        if handler.command != "POST" or name not in config:
            raise APIError(404, "接口不存在")
        upstream = config[name]
        call["secrets"] += (upstream["key"],)
        key = self.authenticate(handler, call)
        body = self.read_body(handler)
        call["request"] = handler.raw_body if call["log"] else b""  # exactly what the client sent
        call["request_bytes"] = len(handler.raw_body)
        model = resolve(upstream, body.get("model"))
        call["requested_model"] = body["model"] if isinstance(body.get("model"), str) else ""
        call["model"] = model or call["requested_model"][:100]
        if model is None:
            raise GatewayError("not_found", "模型不在本活动开放列表中，可用模型见 /v1/models")
        body["model"] = model
        call["price"] = upstream["models"][model]
        reject_stored_file_references(body)
        settings = call["settings"]
        if settings["blockedWords"]:
            # Normalised like sub2api, but every message and the system prompt are scanned (sub2api reads only the last
            # user turn, which a fake earlier turn would bypass). Values only, so JSON keys never match.
            # ponytail: substring scan, fine for a short keyword list.
            text = request_text(body)
            words = {" ".join(unicodedata.normalize("NFKC", w).split()).lower() for w in settings["blockedWords"]}
            hit = next((w for w in sorted(words) if w and w in text), None)
            if hit:
                raise GatewayError("forbidden", "请求内容包含本活动禁止的词语，已拦截", detail=f"命中屏蔽词：{hit}")
        if "stream" in body and type(body["stream"]) is not bool:
            raise APIError(400, "stream 必须是布尔值")
        tools = body.get("tools") or []
        if BILLED_FIELDS & set(body) or not isinstance(tools, list) or any(
                not isinstance(t, dict) or ("type" in t and not str(t["type"]).startswith(CLIENT_TOOL_TYPES)) for t in tools):
            # Server-side tools are billed by the upstream outside token usage, so their cost cannot be metered here.
            raise APIError(400, "本活动不开放上游服务端工具（网页搜索、代码执行、MCP 等），请在客户端实现工具")
        if body.get("n", 1) != 1 or "best_of" in body:
            # Several completions per call multiply output cost beyond the usage estimate; one reply per request.
            raise APIError(400, "本活动每次请求只生成一条回复（n 必须为 1，不支持 best_of）")
        if path == "/v1/responses":
            # All teams share one upstream account: stored conversations must not cross teams, and background runs would
            # finish after the call, outside billing.
            # All teams share one upstream account, so nothing is stored there and nothing stored can be referenced:
            # store is forced off; previous_response_id, item_reference, conversation and stored prompts are refused; and
            # background runs (which finish after the call, outside billing) are refused. Clients send the full input,
            # as Codex does with an API key.
            items = body.get("input") if isinstance(body.get("input"), list) else []
            if body.get("background") or body.get("conversation") is not None or isinstance(body.get("prompt"), dict) \
                    or body.get("previous_response_id") is not None \
                    or any(isinstance(i, dict) and i.get("type") == "item_reference" for i in items):
                raise GatewayError("invalid", "本活动的 Responses API 不保存上游状态：不支持 previous_response_id、item_reference、"
                                              "conversation、预存 prompt 与 background，请在 input 中提供完整上下文")
            body["store"] = False
        for field in PRICING_FIELDS:
            body.pop(field, None)
        call["stream"] = stream = body.get("stream") is True
        if stream and path == "/v1/chat/completions":  # the Responses API reports usage on its own
            options = body.get("stream_options")
            body["stream_options"] = {**(options if isinstance(options, dict) else {}), "include_usage": True}
        team = key["team_id"]
        call["output_estimate"] = bound_output(body, inject=name == "anthropic")
        call["input_estimate"] = estimate_input(body)
        self.admit(team, settings["teamRpm"])
        try:
            self.recheck_access(handler, call)
            try:
                if self.client_gone(handler):
                    # Left while queued: forwarding now would bill a reply nobody receives (sub2api: 499, nothing sent).
                    raise GatewayError("client_closed", "客户端已断开，请求未发出，不计费")
                self.forward(handler, name, upstream, suffix, body, stream, call)
            except APIError as error:
                # record() writes once, so the final status and reason must be set before the finally below runs.
                self.note_error(call, error)
                raise
            except ClientGone:
                raise
            except Exception as error:
                # Unexpected failure: the upstream may already have run if the reply had started, so charge as post-send.
                traceback.print_exc()
                internal = GatewayError("internal", f"网关内部错误，请联系组织方（{type(error).__name__}）")
                if not call.get("recorded"):
                    call["charge_estimate"] = call["forwarded"]
                self.note_error(call, internal)
                raise internal from None
            finally:
                self.safe_record(handler, call)
        finally:
            self.release(team)

    def open_with_retry(self, request):
        """Retry upstream overload and connection errors; safe because nothing has reached the client yet."""
        for delay in (*RETRY_DELAYS, None):
            try:
                return self.opener.open(request, timeout=UPSTREAM_TIMEOUT)
            except urllib.error.HTTPError as error:
                if delay is None or error.code not in RETRY_STATUSES:
                    raise
                try:  # the upstream says when to come back; honoured up to the 3 s cap
                    delay = min(3.0, max(delay, float(error.headers.get("retry-after") or 0)))
                except ValueError:
                    pass
                error.close()
            except OSError as error:
                # Retry only when the request certainly never reached the upstream and the failure can be transient.
                # TLS errors are configuration problems and timeouts would each wait the full upstream timeout again.
                reason = getattr(error, "reason", error)
                if delay is None or request_was_sent(error) or not isinstance(reason, (ConnectionError, socket.gaierror)):
                    raise
            time.sleep(delay)

    def count_tokens(self, handler, call, upstream):
        """Claude Code calls this before requests; passed through at zero cost, recorded without content."""
        body = self.read_body(handler)
        call["secrets"] += (upstream["key"],)
        model = resolve(upstream, body.get("model"))
        call["model"] = model or ""
        if model is None:
            raise GatewayError("not_found", "模型不在本活动开放列表中，可用模型见 /v1/models")
        body["model"] = model
        reject_stored_file_references(body)
        headers = {"Content-Type": "application/json", "x-api-key": upstream["key"],
                   "anthropic-version": handler.headers.get("anthropic-version", "2023-06-01")}
        beta = allowed_betas(handler.headers.get("anthropic-beta"))
        if beta:
            headers["anthropic-beta"] = beta
        headers["User-Agent"] = (handler.headers.get("User-Agent") or "gotc-gateway/1.0")[:300]
        request = urllib.request.Request(upstream["url"] + "/messages/count_tokens", data=gateway_json(body), headers=headers, method="POST")
        try:
            with self.open_with_retry(request) as response:
                status, payload, content_type = response.status, response.read(1_000_000), response.headers.get("Content-Type", "application/json")
        except urllib.error.HTTPError as error:  # mapped like /v1/messages; count_tokens is never charged
            try:
                payload = error.read(1_000_000)
            except (OSError, http.client.HTTPException):
                payload = b"{}"
            payload = redact_error_payload(payload, self.error_secrets(call))
            message = upstream_message(payload, self.error_secrets(call))
            if error.code in (404, 405, 501):
                # The upstream has no count_tokens (Gitee AI, many compatible providers). Mirror the status, as sub2api
                # does for count_tokens, so clients such as Claude Code fall back to their own estimate.
                raise GatewayError("upstream_invalid", "上游不支持 count_tokens 接口", status=404, upstream_status=error.code,
                                   upstream_message=message) from None
            mapped = upstream_http_error(call["path"], error.code, message)
            if mapped:
                raise mapped from None
            status, content_type = error.code, error.headers.get("Content-Type", "application/json")
            self.note_error(call, GatewayError("upstream_invalid", message, status=status, upstream_status=status, upstream_message=message))
        except (OSError, http.client.HTTPException) as error:  # count_tokens is free, so any failure is just a 502
            raise GatewayError("network", "上游模型服务暂时不可用，请稍后重试", upstream_message=failure_reason(error)) from None
        call["status"] = status
        self.safe_record(handler, call)
        self.send(handler, call, status, content_type, payload)

    def upstream_failed(self, call, error):
        """Turn a network failure into the team's error response, estimating cost when the upstream may have run."""
        reason = getattr(error, "reason", error)
        timed_out = isinstance(reason, (TimeoutError, socket.timeout))
        if not call["forwarded"] and not request_was_sent(error):
            # Failed while connecting, in the TLS handshake or while sending: the upstream never had the whole request,
            # so nothing can have been generated or billed. Decided by where the failure happened, not by its type.
            raise GatewayError("network", "上游模型服务暂时不可用，请稍后重试（本次不计费）",
                               upstream_message=f"{failure_reason(error)}（请求未发出，不计费）") from None
        # The request was sent (or the reply had started): the upstream may be generating and billing,
        # so record the estimated usage rather than nothing.
        call["charge_estimate"] = True
        reason = f"{failure_reason(error)}（请求已发出，按估算用量计费）"
        if timed_out:
            raise GatewayError("timeout", "上游模型服务响应超时，本次按估算用量计费", upstream_message=reason) from None
        raise GatewayError("network", "上游模型服务连接中断，本次按估算用量计费", upstream_message=reason) from None

    def forward(self, handler, name, upstream, suffix, body, stream, call):
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream" if stream else "application/json"}
        if name == "openai":
            headers["Authorization"] = "Bearer " + upstream["key"]
        else:
            headers["x-api-key"] = upstream["key"]
            headers["anthropic-version"] = handler.headers.get("anthropic-version", "2023-06-01")
            beta = allowed_betas(handler.headers.get("anthropic-beta"))
            if beta:
                headers["anthropic-beta"] = beta
            for header, value in handler.headers.items():  # not `name`: that is the upstream kind used below
                if header.lower() in CLIENT_HEADERS or header.lower().startswith("x-stainless-"):
                    headers[header] = value[:500]
        # The client's own User-Agent (sub2api passes it); the urllib default is blocked by Cloudflare-fronted upstreams.
        headers["User-Agent"] = (handler.headers.get("User-Agent") or "gotc-gateway/1.0")[:300]
        request = urllib.request.Request(upstream["url"] + suffix, data=gateway_json(body), headers=headers, method="POST")
        handler.connection.settimeout(UPSTREAM_TIMEOUT)
        try:
            response = self.open_with_retry(request)
        except urllib.error.HTTPError as error:
            # The upstream answered with an error: nothing was generated, so it is never charged.
            try:
                payload = error.read(1_000_000)
            except (OSError, http.client.HTTPException):
                payload = b""
            call["upstream_request_id"] = error.headers.get("request-id") or error.headers.get("x-request-id") or ""
            payload = redact_error_payload(payload, self.error_secrets(call))
            message = upstream_message(payload, self.error_secrets(call)) or f"HTTP {error.code}"
            mapped = upstream_http_error(call["path"], error.code, message)
            if mapped:
                retry = error.headers.get("retry-after")
                mapped.retry_after = retry[:20] if retry and error.code in (429, 503, 529) else None
                raise mapped from None
            # 400: the team's request was wrong; pass the upstream body through so it can fix it (as sub2api does).
            self.note_error(call, GatewayError("upstream_invalid", message, status=400, upstream_status=400, upstream_message=message))
            call["response"], call["size"] = [], 0
            self.keep(call, payload)
            self.safe_record(handler, call)
            handler.close_connection = True
            self.send(handler, call, 400, error.headers.get("Content-Type", "application/json"), payload)
            return
        except (OSError, http.client.BadStatusLine, http.client.LineTooLong) as error:
            # Response parsing failed after sending. Other HTTPExceptions (e.g. InvalidURL) can occur before sending.
            self.upstream_failed(call, error)
        call["status"], call["forwarded"] = response.status, True
        call["upstream_request_id"] = response.headers.get("request-id") or response.headers.get("x-request-id") or ""
        call["reply_headers"] = passed_headers(response.headers)
        if not call.get("first_token_ms"):  # non-streaming: the whole reply arrives with the headers
            call["first_token_ms"] = int((time.monotonic() - call["started"]) * 1000)
        usage = call["usage"]
        with response:
            content_type = response.headers.get("Content-Type", "application/json")
            if content_type.startswith("text/event-stream"):
                self.relay_stream(handler, name, response, content_type, call)
                return
            try:
                payload = response.read(50_000_000)
            except (OSError, http.client.HTTPException) as error:  # nothing sent to the client yet: a clean error
                self.upstream_failed(call, error)
            declared = response.headers.get("Content-Length")
            if declared and declared.isdigit() and len(payload) < int(declared):
                # read(n) returns a short body instead of raising when the upstream drops mid-reply.
                self.upstream_failed(call, ConnectionResetError("reply cut short"))
            call["reply_bytes"] = len(payload)
            try:
                data = json.loads(payload)
            except ValueError:
                # A 200 the gateway cannot parse is not a reply (sub2api: 502, not billed); keep it for admins.
                call["forwarded"] = False
                payload = redact_error_payload(payload, self.error_secrets(call))
                self.keep(call, payload[:2000])
                preview = upstream_message(payload, self.error_secrets(call))
                raise GatewayError("upstream", "上游返回了无法解析的内容，请稍后重试（本次不计费）", upstream_status=response.status,
                                   upstream_message=f"上游返回 HTTP {response.status} 但内容不是 JSON：{preview}") from None
            if isinstance(data, dict) and isinstance(data.get("error"), (dict, str)):
                # Some providers answer errors with HTTP 200 and an error object.
                call["forwarded"] = False
                message = upstream_message(payload, self.error_secrets(call))
                kind = "overloaded" if "overload" in json.dumps(data.get("error")).lower() else "upstream"
                raise GatewayError(kind, "上游模型服务返回错误，请稍后重试（本次不计费）", upstream_status=response.status,
                                   upstream_message=message) from None
            read_usage(name, data, usage)
            call["done"] = True  # a complete JSON body is a finished reply
            self.keep(call, payload)
            self.safe_record(handler, call)
            self.send(handler, call, response.status, content_type, payload)

    def relay_stream(self, handler, name, response, content_type, call):
        """Relay an SSE stream, billing what the upstream produced however the stream or the client ends.

        As in sub2api, the first event is read before anything is sent: an upstream error there becomes a proper HTTP
        error (not billed). After output has started, a failure is reported to the client with a terminal error frame
        and recorded as a failed call (inferred 502/504), billed for what was observed or estimated.
        """
        usage, path = call["usage"], call["path"]
        handler.close_connection = True  # HTTP/1.0: the stream ends when the connection closes
        set_read_timeout(response, STREAM_IDLE_TIMEOUT)  # silence between events, not the whole reply
        def oversized_event():
            return GatewayError("upstream", "上游流式事件过大，请联系组织方检查上游服务", upstream_status=200,
                                upstream_message=f"SSE event buffer exceeded {SSE_EVENT_CAP} bytes")

        def frames():
            frame, size = [], 0
            while True:
                # Limit readline itself: checking only after an unbounded read would still allocate a giant line.
                line = response.readline(SSE_EVENT_CAP - size + 1)
                if not line:
                    break
                size += len(line)
                if size > SSE_EVENT_CAP:
                    raise oversized_event()
                frame.append(line)
                if line in (b"\n", b"\r\n"):
                    yield frame
                    frame, size = [], 0
            if frame:
                yield frame

        def inspect_frame(frame):
            # SSE joins multiple data lines with newlines; inspect the whole event before releasing an error.
            payload = b"\n".join(line[5:].removeprefix(b" ").rstrip(b"\r\n") for line in frame if line.startswith(b"data:"))
            event_name = next((line[6:].strip() for line in reversed(frame) if line.startswith(b"event:")), b"")
            try:
                event = json.loads(payload)
            except ValueError:
                event = None
            error = event_name in (b"error", b"response.failed") or isinstance(event, dict) and (
                event.get("type") in ("error", "response.failed") or isinstance(event.get("error"), (dict, str)))
            return payload, event, error

        stream = frames()
        held, held_size, payload, event, is_error = [], 0, b"", None, False
        try:
            for frame in stream:
                held_size += sum(map(len, frame))
                if held_size > SSE_EVENT_CAP:
                    raise oversized_event()  # bound comment/heartbeat frames held before the first data event too
                held.append(frame)
                payload, event, is_error = inspect_frame(frame)
                if payload:
                    break
        except (OSError, http.client.HTTPException) as error:
            self.upstream_failed(call, error)  # nothing sent to the client yet: a clean error response
        if is_error:
            call["forwarded"] = False  # the upstream refused before generating anything: not billed
            message = upstream_message(payload, self.error_secrets(call))
            raw = payload.decode("utf-8", "replace")
            kind = "overloaded" if "overload" in raw.lower() else "upstream_rate_limit" if "rate_limit" in raw.lower() else "upstream"
            raise GatewayError(kind, "上游模型服务返回错误，请稍后重试（本次不计费）", upstream_status=200, upstream_message=message)
        try:
            self.send(handler, call, 200, content_type)
            client_open = True
        except ClientGone:
            client_open = False  # still drain below: the upstream is generating and billing regardless
        failure = None

        def relay(frame):
            nonlocal client_open
            payload, parsed, is_error = inspect_frame(frame)
            if is_error:
                sanitized = redact_error_payload(payload, self.error_secrets(call))
                if sanitized != payload:
                    replacement, written = [], False
                    for line in frame:
                        if line.startswith(b"data:"):
                            if not written:
                                ending = b"\r\n" if line.endswith(b"\r\n") else b"\n" if line.endswith(b"\n") else b""
                                # Every physical payload line needs its own data field in SSE, including plain-text errors.
                                replacement.extend(b"data: " + part + ending for part in sanitized.split(b"\n"))
                                written = True
                        else:
                            replacement.append(line)
                    frame = replacement
                    try:
                        parsed = json.loads(sanitized)
                    except ValueError:
                        parsed = None
                call["stream_error"] = parsed if isinstance(parsed, dict) else {
                    "error": {"message": upstream_message(sanitized, self.error_secrets(call))}}
                # An error's id/event/comment lines are provider-controlled too; do not leak keys through metadata.
                frame = [line if line.startswith(b"data:") else redact_error_payload(line, self.error_secrets(call))
                         for line in frame]
            if payload.strip() == b"[DONE]":
                call["done"] = True
            else:
                read_usage(name, parsed, usage)
                stream_progress(name, parsed, call)
                if call["reply_bytes"] and call.get("stream_first_ms") is None:
                    call["stream_first_ms"] = int((time.monotonic() - call["started"]) * 1000)
            for line in frame:
                self.keep(call, line)
                if client_open:
                    try:
                        self.send(handler, call, 200, content_type, line)
                    except ClientGone:
                        client_open = False  # keep draining to capture final usage, bounded by the upstream timeout

        for frame in held:
            relay(frame)
        try:
            for frame in stream:
                relay(frame)
        except (OSError, http.client.HTTPException, GatewayError) as error:
            failure = error
        if call["done"] and not call.get("stream_error") and not isinstance(failure, GatewayError):
            return  # finished (if the client left mid-way, the full drained usage is billed, as in sub2api)
        # The stream did not finish: classify, tell the client (if it is still there) and record a failed call.
        if call.get("stream_error"):
            raw = json.dumps(call["stream_error"].get("error", call["stream_error"]))
            kind = "overloaded" if "overload" in raw.lower() else "upstream"
            info = error_info(GatewayError(kind, "上游模型服务在输出过程中返回错误", status=502, upstream_status=200,
                                           upstream_message=upstream_message(raw.encode(), self.error_secrets(call))))
            frame = None  # the upstream's own error frame was already relayed
        elif isinstance(failure, GatewayError):
            info = error_info(failure)
            frame = sse_error(path, info)
        else:
            timed_out = failure is not None and isinstance(getattr(failure, "reason", failure), (TimeoutError, socket.timeout))
            reason = failure_reason(failure) if failure is not None else "上游未发送结束事件就关闭了连接"
            info = error_info(GatewayError("timeout" if timed_out else "network",
                                           "上游输出中断，本次按已产生的用量计费", upstream_message=reason))
            frame = sse_error(path, info)
        call["status"], call["error"] = info["kind"] == "timeout" and 504 or 502, info
        self.keep(call, f"{info['upstream_message']}\n".encode())
        if frame and client_open:
            try:
                self.send(handler, call, 200, content_type, frame)
            except ClientGone:
                pass
