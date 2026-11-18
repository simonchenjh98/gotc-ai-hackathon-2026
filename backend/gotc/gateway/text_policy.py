"""Model selection, provider request policy and token estimates for text SDK calls."""

import unicodedata

from backend.gotc.errors import APIError
from .config import resolve
from .gateway_pricing import PricingError
from .protocol_errors import GatewayError
from .requests import (
    BILLED_FIELDS, CLIENT_TOOL_TYPES, PRICING_FIELDS, bound_output, estimate_input,
    reject_stored_file_references, request_text,
)


def prepare_text(gateway, name, path, upstream, body, call):
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
        # Text operations in the catalog are token- or call-priced. The only request
        # modifier consulted after resolving the operation is the LoRA surcharge.
        call["pricing_request"] = {"lora": any(body.get(k) for k in ("lora_weights", "lora", "lora_id"))}
    reject_stored_file_references(body)
    settings = call["settings"]
    if settings["blockedWords"]:
        # Scan normalized text in all turns, system instructions and tool arguments.
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
        # Teams share an upstream account. Require complete, stateless input and finish all work within the metered call.
        items = body.get("input") if isinstance(body.get("input"), list) else []
        if body.get("background") or body.get("conversation") is not None or isinstance(body.get("prompt"), dict) \
                or body.get("previous_response_id") is not None \
                or any(isinstance(i, dict) and i.get("type") == "item_reference" for i in items):
            raise GatewayError("invalid", "本活动的 Responses API 不保存上游状态：不支持 previous_response_id、item_reference、"
                                          "conversation、预存 prompt 与 background，请在 input 中提供完整上下文")
        body["store"] = False
    for field in PRICING_FIELDS:
        body.pop(field, None)
    call["stream"] = body.get("stream") is True
    if call["stream"] and path == "/v1/chat/completions":  # the Responses API reports usage on its own
        options = body.get("stream_options")
        body["stream_options"] = {**(options if isinstance(options, dict) else {}), "include_usage": True}
    call["output_estimate"] = bound_output(body, inject=name == "anthropic")
    call["input_estimate"] = estimate_input(body)
