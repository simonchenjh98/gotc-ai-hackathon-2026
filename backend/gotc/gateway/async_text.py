"""Async HTTPX relay for OpenAI-compatible and Anthropic text and token-count endpoints."""

import asyncio
import codecs
import io
import json
import mmap
import tempfile
import time

import httpx
from starlette.requests import ClientDisconnect

from backend.gotc.errors import APIError
from . import requests, transport, usage
from .config import resolve
from .memory import BULK_BYTES
from .protocol_errors import (
    ClientGone, GatewayError, error_info, redact_error_payload, safe_header, sse_error, upstream_http_error, upstream_message,
)
from .stream_events import event_blocks, oversized_event, redacted_block
from .text_policy import prepare_text


ROUTES = {"/v1/chat/completions": ("openai", "/chat/completions"),
          "/v1/messages": ("anthropic", "/messages"), "/v1/responses": ("openai", "/responses"),
          "/v1/messages/count_tokens": ("anthropic", "/messages/count_tokens")}
STREAM_IDLE_TIMEOUT = 360
SSE_EVENT_CAP = 1024 * 1024
JSON_REPLY_CAP = 50_000_000
ERROR_REPLY_CAP = 1_000_000
BODY_IDLE_TIMEOUT, BODY_TOTAL_TIMEOUT = 15, 300


async def request_chunks(request, expected_length):
    """Read one SDK upload within its declared size and the platform's 15s idle / 300s total deadline."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + BODY_TOTAL_TIMEOUT
    received = 0
    source = request.stream()
    while True:
        try:
            async with asyncio.timeout_at(min(deadline, loop.time() + BODY_IDLE_TIMEOUT)):
                chunk = await anext(source, None)
        except TimeoutError:
            raise APIError(408, "上传超时，请重新上传", "upload_timeout") from None
        except ClientDisconnect:
            raise GatewayError("client_closed", "客户端已断开，请求未发出，不计费") from None
        if chunk is None:
            break
        received += len(chunk)
        if received > expected_length:
            raise APIError(400, "请求长度不正确")
        if chunk:
            yield chunk
    if received != expected_length:
        raise APIError(400, "请求内容未完整发送")


def _load_file(file, decode):
    # json.loads on raw bytes creates a whole decoded string. Decode the mapped
    # file, then unmap it before building the Python object graph.
    file.flush()
    with mmap.mmap(file.fileno(), 0, access=mmap.ACCESS_READ) as source:
        text = codecs.decode(source, json.detect_encoding(source[:4]), "surrogatepass")
    return decode(text)


async def read_json(ctx, capture=False):
    length = requests.json_body_length(ctx.handler.headers)
    inflater = requests.json_inflater(ctx.handler.headers.get("Content-Encoding"))
    disk = length >= BULK_BYTES or inflater is not None
    buffer = tempfile.TemporaryFile() if disk else io.BytesIO()
    try:
        async for chunk in request_chunks(ctx.request, length):
            if inflater is not None:
                await ctx.sync(requests.inflate_into, buffer, inflater, chunk)
            elif disk:
                await ctx.sync(buffer.write, chunk)
            else:
                buffer.write(chunk)
        if inflater is not None and not inflater.eof:
            raise APIError(400, "请求内容解压失败")
        size = buffer.tell()
        if size >= BULK_BYTES:
            try:
                body = await ctx.sync(_load_file, buffer, requests.decode_json_body)
            except UnicodeError:
                raise APIError(400, "JSON格式不正确") from None
            if capture:
                buffer.seek(0)
                ctx.call["request_file"] = buffer
                buffer = None  # ownership passes to the call finalizer
        else:
            buffer.seek(0)
            raw = buffer.read()
            body = await ctx.sync(requests.decode_json_body, raw)
            if capture:
                ctx.call["request"] = raw
        return body, size
    finally:
        if buffer is not None:
            buffer.close()


class ReplayableFile:
    """Expose a private seekable file as replayable async chunks."""
    replayable = True

    def __init__(self, file, sync):
        self.file = file
        self.sync = sync

    async def __aiter__(self):
        await self.sync(self.file.seek, 0)
        while chunk := await self.sync(self.file.read, 65536):
            yield chunk


def spool_json(value):
    """Use the standard JSON encoder for scalar escaping, including long strings in bounded pieces."""
    output = tempfile.TemporaryFile()

    def write(node):
        if isinstance(node, str):
            output.write(b'"')
            for start in range(0, len(node), 32768):
                fragment = json.encoder.encode_basestring(node[start:start + 32768])
                output.write(fragment[1:-1].encode())
            output.write(b'"')
        elif isinstance(node, dict):
            output.write(b"{")
            for index, (key, item) in enumerate(node.items()):
                if index:
                    output.write(b",")
                write(key)
                output.write(b":")
                write(item)
            output.write(b"}")
        elif isinstance(node, list):
            output.write(b"[")
            for index, item in enumerate(node):
                if index:
                    output.write(b",")
                write(item)
            output.write(b"]")
        else:
            output.write(requests.gateway_json(node))

    try:
        write(value)
        return output, output.tell()
    except BaseException:
        output.close()
        raise


def _outbound_headers(headers, name, key, stream, counting):
    outgoing = {"Content-Type": "application/json", "Accept-Encoding": "identity",
                "Accept": "text/event-stream" if stream else "application/json",
                "User-Agent": (headers.get("User-Agent") or "gotc-gateway/1.0")[:300]}
    if name == "openai":
        outgoing["Authorization"] = "Bearer " + key
    else:
        outgoing["x-api-key"] = key
        outgoing["anthropic-version"] = headers.get("anthropic-version", "2023-06-01")[:200]
        beta = requests.allowed_betas(headers.get("anthropic-beta"))
        if beta:
            outgoing["anthropic-beta"] = beta
        if not counting:
            for name, value in headers.items():
                if name.lower() in transport.CLIENT_HEADERS or name.lower().startswith("x-stainless-"):
                    outgoing[name] = value[:500]
    return outgoing


async def _chunks(response, idle_timeout=None):
    if response.is_stream_consumed:  # e.g. an already-buffered HTTPX transport response
        yield response.content
        return
    iterator = response.aiter_raw().__aiter__()
    while True:
        try:
            if idle_timeout is not None:
                async with asyncio.timeout(idle_timeout):
                    block = await anext(iterator)
            else:
                block = await anext(iterator)
        except StopAsyncIteration:
            return
        except (httpx.TransportError, TimeoutError) as error:
            raise transport.UpstreamNetworkError(error, sent=True) from error
        yield block


async def _read(ctx, response, limit, truncate=False):
    declared = response.headers.get("content-length", "")
    bulk = declared.isdigit() and len(declared) < 20 and min(int(declared), limit) >= BULK_BYTES
    if bulk:
        await ctx.reserve_bulk_memory()
    with io.BytesIO() as body:
        async for chunk in _chunks(response):
            room = limit - body.tell()
            if len(chunk) > room:
                if not truncate:
                    raise GatewayError("upstream", "上游返回内容过大，请稍后重试", upstream_status=response.status_code)
                body.write(chunk[:room])
                break
            if not bulk and body.tell() + len(chunk) >= BULK_BYTES:
                await ctx.reserve_bulk_memory()
                bulk = True
            body.write(chunk)
        return body.getvalue()


def _network_failure(call, error):
    if not call["forwarded"] and not transport.request_was_sent(error):
        raise GatewayError("network", "上游模型服务暂时不可用，请稍后重试",
                           upstream_message=f"{transport.failure_reason(error)}（请求未发出，不计费）") from None
    call["charge_estimate"] = True
    reason = f"{transport.failure_reason(error)}（请求已发出，按估算用量计费）"
    if transport.is_timeout(error):
        raise GatewayError("timeout", "上游模型服务响应超时，本次按估算用量计费", upstream_message=reason) from None
    raise GatewayError("network", "上游模型服务连接中断，本次按估算用量计费", upstream_message=reason) from None


async def _send_provider_error(ctx, response, content_type, counting):
    call, gateway = ctx.call, ctx.gateway
    status = response.status_code
    try:
        payload = await _read(ctx, response, ERROR_REPLY_CAP, truncate=True)
    except OSError:
        payload = b""
    secrets = gateway.error_secrets(call)
    payload = redact_error_payload(payload, secrets)
    message = upstream_message(payload, secrets) or f"HTTP {status}"
    if counting and status in (404, 405, 501):
        raise GatewayError("upstream_invalid", "上游不支持 count_tokens 接口", status=404, upstream_status=status,
                           upstream_message=message)
    error = upstream_http_error(call["path"], status, message)
    if error:
        retry = response.headers.get("retry-after")
        if retry and status in (429, 503, 529):
            error.retry_after = safe_header(retry, secrets, 20)
        raise error
    gateway.note_error(call, GatewayError("upstream_invalid", message, status=status, upstream_status=status,
                                         upstream_message=message))
    if not counting:
        # A passthrough error log contains the sanitized provider body, like the reply sent to the SDK.
        call["response"], call["size"] = [], 0
        gateway.keep(call, payload)
    await ctx.record()
    await ctx.send(status, content_type, payload)


async def _inspect_json(ctx, name, response, file, size, payload):
    call, gateway = ctx.call, ctx.gateway
    call["reply_bytes"] = size
    try:
        if payload is not None:
            data = await ctx.sync(json.loads, payload)
        else:
            data = await ctx.sync(_load_file, file, json.loads)
    except (ValueError, RecursionError):
        call["forwarded"] = False
        secrets = gateway.error_secrets(call)
        if payload is not None and len(payload) <= ERROR_REPLY_CAP:
            safe = await ctx.sync(redact_error_payload, payload, secrets)
            gateway.keep(call, safe[:2000])
            preview = upstream_message(safe[:2000], secrets)
        else:
            preview = "上游响应不是有效 JSON，原始内容过大"
        raise GatewayError("upstream", "上游返回了无法解析的内容，请稍后重试（本次不计费）", upstream_status=response.status_code,
                           upstream_message=f"上游返回 HTTP {response.status_code} 但内容不是 JSON：{preview}") from None
    if isinstance(data, dict) and isinstance(data.get("error"), (dict, str)):
        call["forwarded"] = False
        if payload is None:
            error = data["error"]
            # The SDK receives a classified error envelope; never reserialize an
            # entire oversized provider body solely to build a short diagnostic.
            message = error.get("message") if isinstance(error, dict) else error
            kind = error.get("type", "") if isinstance(error, dict) else ""
            detail = message if isinstance(message, str) and len(message) <= 2000 else "上游模型服务返回错误"
            tag = kind[:100] if isinstance(kind, str) else ""
            kind = "overloaded" if "overload" in (tag + detail).lower() else "upstream"
            detail = upstream_message(detail.encode("utf-8", "replace"), gateway.error_secrets(call))
        else:
            kind = "overloaded" if "overload" in json.dumps(data["error"]).lower() else "upstream"
            detail = upstream_message(payload, gateway.error_secrets(call))
        raise GatewayError(kind, "上游模型服务返回错误，请稍后重试（本次不计费）", upstream_status=response.status_code,
                           upstream_message=detail)
    usage.read_usage(name, data, call["usage"], call)
    call["done"] = True


async def _send_json(ctx, name, response, content_type):
    call, gateway = ctx.call, ctx.gateway
    declared = response.headers.get("content-length", "")
    bulk = declared.isdigit() and len(declared) < 20 and int(declared) >= BULK_BYTES
    if bulk:
        await ctx.reserve_bulk_memory()
    with tempfile.SpooledTemporaryFile(max_size=BULK_BYTES, mode="w+b") as body:
        if bulk:
            await ctx.sync(body.rollover)
        size = 0
        async for chunk in _chunks(response):
            size += len(chunk)
            if size > JSON_REPLY_CAP:
                raise GatewayError("upstream", "上游返回内容过大，请稍后重试", upstream_status=response.status_code)
            if not bulk and size >= BULK_BYTES:
                await ctx.reserve_bulk_memory()
                await ctx.sync(body.rollover)
                bulk = True
            if bulk:
                await ctx.sync(body.write, chunk)
            else:
                body.write(chunk)
        body.seek(0)
        payload = body.read() if size < BULK_BYTES else None
        await _inspect_json(ctx, name, response, body, size, payload)
        if payload is not None:
            gateway.keep(call, payload)
            await ctx.record()
            await ctx.send(response.status_code, content_type, payload)
        else:
            if call["log"]:
                async for chunk in ReplayableFile(body, ctx.sync):
                    gateway.keep(call, chunk)
            await ctx.record()
            await ctx.send(response.status_code, content_type, length=size)
            async for chunk in ReplayableFile(body, ctx.sync):
                await ctx.send(response.status_code, content_type, chunk)


async def serve_text(ctx, path):
    name, suffix = ROUTES.get(path, (None, None))
    config = ctx.config or {}
    if ctx.handler.command != "POST" or name not in config:
        raise APIError(404, "接口不存在")
    upstream = config[name]
    call = ctx.call
    call["secrets"] += (upstream["key"],)
    counting = path == "/v1/messages/count_tokens"
    async with ctx.admitted(count_tokens=counting, defer_rpm=not counting):
        if counting:
            call["log"] = False
        body, size = await read_json(ctx, capture=call["log"] and not counting)
        large = size >= BULK_BYTES
        if counting:
            model = resolve(upstream, body.get("model"))
            call["model"] = model or ""
            if not model:
                raise GatewayError("not_found", "模型不在本活动开放列表中，可用模型见 /v1/models")
            body["model"] = model
            requests.reject_stored_file_references(body)
        else:
            call["request_bytes"] = size
            await ctx.sync(prepare_text, ctx.gateway, name, path, upstream, body, call)
            await ctx.charge_rpm()
        file = None
        if large:
            file, size = await ctx.sync(spool_json, body)
            data = ReplayableFile(file, ctx.sync)
        else:
            data = await ctx.sync(requests.gateway_json, body)
            size = len(data)
        del body
        ctx.release_bulk_memory()
        headers = _outbound_headers(ctx.handler.headers, name, upstream["key"], call["stream"], counting)
        try:
            try:
                headers["Content-Length"] = str(size)
                response = await ctx.upstream.open("POST", upstream["url"] + suffix, headers=headers, content=data,
                                                   length=size, retry=True, recheck=ctx.recheck)
            finally:
                if file is not None:
                    file.close()
                del data
            try:
                secrets = ctx.gateway.error_secrets(call)
                content_type = safe_header(response.headers.get("Content-Type", "application/json"), secrets)
                status = response.status_code
                call["status"] = status
                request_id = response.headers.get("request-id") or response.headers.get("x-request-id") or ""
                call["upstream_request_id"] = safe_header(request_id, secrets)
                if not 200 <= status < 300:
                    return await _send_provider_error(ctx, response, content_type, counting)
                if counting:
                    payload = await _read(ctx, response, ERROR_REPLY_CAP, truncate=True)
                    await ctx.record()
                    return await ctx.send(status, content_type, payload)
                call["forwarded"] = True
                call["reply_headers"] = transport.passed_headers(response.headers, secrets)
                call["pricing_headers"] = {k: v for k, v in response.headers.items()
                                           if k.lower() in {"inference-cost", "inference-currency", "inference-cost-currency"}}
                call["first_token_ms"] = int((time.monotonic() - call["started"]) * 1000)
                if content_type.partition(";")[0].strip().lower() == "text/event-stream":
                    return await relay_stream(ctx, name, response, content_type)
                return await _send_json(ctx, name, response, content_type)
            finally:
                await response.aclose()
        except OSError as error:
            if counting:
                raise GatewayError("network", "上游模型服务暂时不可用，请稍后重试",
                                   upstream_message=transport.failure_reason(error)) from None
            _network_failure(call, error)


async def relay_stream(ctx, name, response, content_type):
    call, gateway = ctx.call, ctx.gateway
    stream = event_blocks(_chunks(response, STREAM_IDLE_TIMEOUT), SSE_EVENT_CAP)
    prelude = bytearray()
    first = None
    async for block in stream:
        if len(prelude) + len(block.wire) > SSE_EVENT_CAP:
            raise oversized_event(SSE_EVENT_CAP)
        if block.error:
            call["forwarded"] = False
            message = upstream_message(block.payload, gateway.error_secrets(call))
            raw = block.payload.lower()
            kind = "overloaded" if b"overload" in raw else "upstream_rate_limit" if b"rate_limit" in raw else "upstream"
            raise GatewayError(kind, "上游模型服务返回错误，请稍后重试（本次不计费）", upstream_status=200,
                               upstream_message=message)
        if block.payload:
            first = block
            break
        prelude.extend(block.wire)
    client_open = True

    async def write(wire=None):
        nonlocal client_open
        if wire:
            if call.get("log") and call["size"] < BULK_BYTES <= call["size"] + len(wire):
                await ctx.reserve_bulk_memory()
            gateway.keep(call, wire)
        if client_open:
            try:
                await ctx.send(200, content_type, wire)
            except ClientGone:
                client_open = False  # drain final usage even after a downstream disappears

    async def relay(block):
        wire, value = block.wire, block.value
        if block.error:
            secrets = gateway.error_secrets(call)
            wire, payload = redacted_block(block, secrets)
            call["stream_error"] = upstream_message(payload, secrets) or "上游流式错误"
            call["stream_error_kind"] = "overloaded" if b"overload" in payload.lower() else "upstream"
        if block.payload.strip() == b"[DONE]":
            call["done"] = True
        else:
            usage.read_usage(name, value, call["usage"], call)
            usage.stream_progress(name, value, call)
            if call["reply_bytes"] and call.get("stream_first_ms") is None:
                call["stream_first_ms"] = int((time.monotonic() - call["started"]) * 1000)
        await write(wire)

    await write()
    if prelude:
        await write(bytes(prelude))
        prelude.clear()
    if first:
        await relay(first)
        del first, block
    failure = None
    try:
        async for block in stream:
            await relay(block)
    except (OSError, GatewayError) as error:
        failure = error
    if call["done"] and not call.get("stream_error") and not isinstance(failure, GatewayError):
        return
    if call.get("stream_error"):
        error = GatewayError(call["stream_error_kind"], "上游模型服务在输出过程中返回错误", status=502, upstream_status=200,
                             upstream_message=call["stream_error"])
    elif isinstance(failure, GatewayError):
        error = failure
    else:
        timed_out = transport.is_timeout(failure)
        reason = transport.failure_reason(failure) if failure else "上游未发送结束事件就关闭了连接"
        error = GatewayError("timeout" if timed_out else "network", "上游输出中断，本次按已产生的用量计费",
                             upstream_message=reason)
    info = error_info(error)
    call["status"], call["error"] = error.status, info
    gateway.keep(call, (info["upstream_message"] + "\n").encode())
    if not call.get("stream_error") and client_open:
        try:
            await ctx.send(200, content_type, sse_error(call["path"], info))
        except ClientGone:
            pass
