"""SDK route selection, request admission and metered call lifecycle."""

import json
import time
import traceback
import unicodedata
import uuid
from urllib.parse import parse_qs, urlsplit

from backend.gotc.errors import APIError
from .config import resolve
from .gateway_pricing import PricingError
from .gateway_websocket import serve_websocket
from .protocol_errors import ClientGone, GatewayError, error_body
from .relay import count_tokens
from .requests import (
    BILLED_FIELDS, CLIENT_TOOL_TYPES, PRICING_FIELDS, bound_output, estimate_input,
    gateway_json, reject_stored_file_references, request_text,
)
from .usage import USAGE_FIELDS


# Public path -> (upstream name, path appended to that upstream's base URL)
ROUTES = {"/v1/chat/completions": ("openai", "/chat/completions"), "/v1/messages": ("anthropic", "/messages"),
          "/v1/responses": ("openai", "/responses")}  # Responses API: Codex CLI


COUNT_TOKENS_CONCURRENCY, COUNT_TOKENS_RPM = 15, 120  # count_tokens is free but uses our upstream key's rate limit


def handle_request(gateway, handler, path):
    call = {"key": None, "path": path, "model": "", "stream": False, "price": None, "usage": [0] * USAGE_FIELDS, "status": 0,
            "request": b"", "response": [], "size": 0, "started": time.monotonic(), "log": False,
            "request_bytes": 0, "reply_bytes": 0, "done": False, "forwarded": False,
            "request_id": uuid.uuid4().hex, "reply_headers": {}, "secrets": gateway.error_secrets(), "requested_at": time.time()}
    try:
        query = parse_qs(urlsplit(handler.path).query)
        if "key" in query or "api_key" in query:
            # As sub2api: a key in the URL ends up in proxy and browser logs, so it is refused outright.
            raise GatewayError("invalid", "不支持在网址中携带 API 密钥，请改用 Authorization 或 x-api-key 请求头")
        serve(gateway, handler, path, call)
    except ClientGone:
        handler.close_connection = True  # the team's client went away; the call was already recorded
    except Exception as error:
        if not isinstance(error, APIError):
            # A gateway bug or unexpected failure: never leave a row with status 0, and tell the team something.
            traceback.print_exc()
            error = GatewayError("internal", "网关内部错误，请联系组织方")
        info = gateway.note_error(call, error)
        gateway.safe_record(handler, call)
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
        gateway.safe_record(handler, call)  # no-op when already recorded or unauthenticated


def serve(gateway, handler, path, call):
    # A live upstream edit applies to subsequent calls; this request keeps one coherent snapshot.
    config = gateway.config
    if path.startswith("/v1/task/"):
        key = gateway.authenticate(handler, call)
        gateway.admit(key["team_id"], call["settings"]["teamRpm"])
        try:
            gateway.recheck_access(handler, call)
            status, payload, headers = gateway.tasks.request(key, handler.command, path)
        finally:
            gateway.release(key["team_id"])
        call["recorded"] = True  # task submission owns the single usage record
        call["reply_headers"].update({k: v for k, v in headers.items() if k.lower() in {"retry-after"}})
        return gateway.send(handler, call, status, "application/json", gateway_json(payload))
    if config is None:
        raise APIError(404, "接口不存在")
    media_upstream = config.get("openai", {})
    if gateway.is_gitee(media_upstream):
        if path in {"/v1/audio/speech/ws", "/v1/audio/transcriptions/ws"}:
            return serve_websocket(gateway, handler, path, call, media_upstream)
        if gateway.media.supports(path):
            return gateway.media.serve(handler, path, call, media_upstream)
    if (path == "/v1/models" or path.startswith("/v1/models/")) and handler.command == "GET":
        gateway.authenticate(handler)  # not metered or logged
        # Fields of both APIs, so OpenAI and Anthropic SDKs can each parse the list (sub2api answers per platform).
        data = [{"id": m, "object": "model", "owned_by": name, "created": 0, "type": "model", "display_name": m,
                 "created_at": "2024-01-01T00:00:00Z",
                 **({"capabilities": gateway.catalog.model_info(m)} if gateway.is_gitee(item) else {})}
                for name, item in config.items() for m in item["models"]]
        if path != "/v1/models":
            item = next((d for d in data if d["id"] == path[len("/v1/models/"):]), None)
            if item is None:
                raise GatewayError("not_found", "模型不在本活动开放列表中")
            return gateway.send(handler, call, 200, "application/json", gateway_json(item))
        listing = {"object": "list", "data": data, "has_more": False,
                   "first_id": data[0]["id"] if data else None, "last_id": data[-1]["id"] if data else None}
        return gateway.send(handler, call, 200, "application/json", gateway_json(listing))
    if path == "/v1/messages/count_tokens" and handler.command == "POST" and "anthropic" in config:
        key = gateway.authenticate(handler, call)  # expiry
        call["log"] = False  # metadata row only, zero cost
        team = key["team_id"]
        gateway.admit(team, call["settings"]["teamRpm"] or COUNT_TOKENS_RPM, slot=False)  # no team slot, as sub2api
        with gateway.lock:  # but its own small pool, so free calls cannot flood the upstream with our key
            if gateway.counting[team] >= COUNT_TOKENS_CONCURRENCY:
                raise GatewayError("rate_limit", "本队同时进行的 token 计数请求过多，请稍后重试", retry_after=1)
            gateway.counting[team] += 1
        try:
            gateway.recheck_access(handler, call)
            return count_tokens(gateway, handler, call, config["anthropic"])
        finally:
            with gateway.lock:
                gateway.counting[team] -= 1
    name, suffix = ROUTES.get(path, (None, None))
    if handler.command != "POST" or name not in config:
        raise APIError(404, "接口不存在")
    upstream = config[name]
    call["secrets"] += (upstream["key"],)
    key = gateway.authenticate(handler, call)
    body = gateway.read_body(handler)
    call["request"] = handler.raw_body if call["log"] else b""  # exactly what the client sent
    call["request_bytes"] = len(handler.raw_body)
    model = resolve(upstream, body.get("model"))
    call["requested_model"] = body["model"] if isinstance(body.get("model"), str) else ""
    call["model"] = model or call["requested_model"][:100]
    if model is None:
        raise GatewayError("not_found", "模型不在本活动开放列表中，可用模型见 /v1/models")
    body["model"] = model
    call["price"] = upstream["models"][model]
    call["currency"] = "CNY" if gateway.is_gitee(upstream) else "USD"
    if gateway.is_gitee(upstream):
        try:
            call["pricing_snapshot"] = gateway.catalog.resolve(model, "/v1/chat/completions", body)
        except PricingError:
            raise GatewayError("not_found", "该模型不支持此对话接口，请使用模型目录中的对应接口") from None
        call["pricing_request"] = body.copy()
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
    gateway.admit(team, settings["teamRpm"])
    try:
        gateway.recheck_access(handler, call)
        try:
            if gateway.client_gone(handler):
                # Left while queued: forwarding now would bill a reply nobody receives (sub2api: 499, nothing sent).
                raise GatewayError("client_closed", "客户端已断开，请求未发出，不计费")
            gateway.forward(handler, name, upstream, suffix, body, stream, call)
        except APIError as error:
            # record() writes once, so the final status and reason must be set before the finally below runs.
            gateway.note_error(call, error)
            raise
        except ClientGone:
            raise
        except Exception as error:
            # Unexpected failure: the upstream may already have run if the reply had started, so charge as post-send.
            traceback.print_exc()
            internal = GatewayError("internal", f"网关内部错误，请联系组织方（{type(error).__name__}）")
            if not call.get("recorded"):
                call["charge_estimate"] = call["forwarded"]
            gateway.note_error(call, internal)
            raise internal from None
        finally:
            gateway.safe_record(handler, call)
    finally:
        gateway.release(team)
