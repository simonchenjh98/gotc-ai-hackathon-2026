"""Provider error classification, credential redaction and SDK error envelopes."""

import json
import re

from backend.gotc.errors import APIError


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


class GatewayError(APIError):
    """An error with a sub2api-style classification; plain APIError statuses are classified by STATUS_KINDS."""

    def __init__(self, kind: str, message: str, status: int | None = None, upstream_status: int | None = None,
                 upstream_message: str = "", retry_after: int | None = None, detail: str = "") -> None:
        super().__init__(status or ERROR_KINDS[kind][0], message)
        self.kind, self.upstream_status, self.upstream_message = kind, upstream_status, upstream_message
        self.retry_after, self.detail = retry_after, detail  # detail: recorded for admins, never sent to the team


class ClientGone(Exception):
    """The team's client closed its connection; nothing more can be sent to it."""


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


def safe_header(value, secrets, limit=200):
    return redact_error_payload(value.encode(), secrets).decode("utf-8", errors="replace")[:limit]


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
