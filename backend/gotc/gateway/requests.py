"""SDK request validation, content limits and text normalization."""

import ipaddress
import json
import unicodedata
import zlib

from backend.gotc.errors import APIError
from .protocol_errors import GatewayError


BODY_LIMIT = 32 * 1024 * 1024  # sub2api allows 32 MB text bodies; screenshots and long agent sessions need it
JSON_DEPTH_LIMIT = 128  # independent of the Python JSON parser's platform/version recursion limit


OUTPUT_DEFAULT = 8192  # default output tokens for Anthropic requests without max_tokens
OUTPUT_CAP = 128000
IMAGE_TOKENS = 1600  # ceiling for one image (Anthropic: width*height/750, images resized to ~1.15 MP)


# Tools must run on the client. Anthropic "client tools" (bash, text editor, computer, memory) are allowed;
# anything else typed (web search, code execution, MCP, tool search, …) runs and is billed upstream.
CLIENT_TOOL_TYPES = ("custom", "function", "bash_", "text_editor_", "computer_", "memory_", "local_shell")
BILLED_FIELDS = {"mcp_servers", "web_search_options", "container"}  # upstream-billed extras outside token usage
PRICING_FIELDS = ("service_tier", "speed", "inference_geo")  # dropped so requests run at the configured (standard) price
# ponytail: denylist of beta prefixes that add upstream charges; extend when providers add more.
BILLED_BETAS = ("mcp-client", "code-execution", "files-api", "context-1m", "skills", "web-fetch", "web-search")


OUTPUT_LIMIT_FIELDS = ("max_tokens", "max_completion_tokens", "max_output_tokens")


ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff"))


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


def read_body(handler):
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
