"""Streaming media requests and provider responses for the ASGI gateway."""
from contextlib import aclosing, asynccontextmanager
import tempfile
import time

import httpx

from backend.gotc.errors import APIError
from . import transport
from .async_tasks import reconcile_tasks, register_task, serve_task
from .gateway_media import CHUNK, FIELD_LIMIT, REPLY_LIMIT, SPOOL_MEMORY, _length, _pending, _wav_duration, _zero
from .memory import BULK_BYTES
from .protocol_errors import GatewayError, redact_error_payload, safe_header, upstream_http_error, upstream_message
from .requests import client_ip, gateway_json

__all__ = ["serve_media", "serve_task", "reconcile_tasks"]


@asynccontextmanager
async def _request_body(ctx, media):
    from .async_text import read_json, request_chunks

    headers = ctx.handler.headers
    mime = headers.get_content_type()
    if mime == "application/json":
        body = (await read_json(ctx))[0]
        raw = await ctx.sync(gateway_json, body)
        yield body, raw, "application/json", len(raw)
        return
    length = _length(ctx.handler)
    if headers.get("Content-Encoding", "identity").lower() != "identity":
        raise APIError(415, "媒体表单不支持 Content-Encoding")
    if mime not in ("multipart/form-data", "application/x-www-form-urlencoded"):
        raise APIError(415, "请使用 JSON、multipart/form-data 或表单请求")
    content_type = headers.get("Content-Type", "")
    with tempfile.SpooledTemporaryFile(max_size=SPOOL_MEMORY, mode="w+b") as spool:
        async for chunk in request_chunks(ctx.request, length):
            await ctx.sync(spool.write, chunk)
            del chunk
        if length >= BULK_BYTES:
            await ctx.reserve_bulk_memory()
        body, metadata_bytes = await ctx.sync(media.parse_form, spool, mime, content_type, length)
        if length >= BULK_BYTES and metadata_bytes < BULK_BYTES:
            ctx.release_bulk_memory()  # The file stays on disk. Only its metadata survives parsing.
        yield body, _file_chunks(ctx, spool), content_type, length


async def _file_chunks(ctx, spool):
    await ctx.sync(spool.seek, 0)
    while chunk := await ctx.sync(spool.read, CHUNK):
        yield chunk


async def _provider_error(ctx, response, path):
    secrets = ctx.gateway.error_secrets(ctx.call)
    raw = bytearray()
    async for chunk in response.aiter_raw(chunk_size=CHUNK):
        if len(raw) + len(chunk) >= BULK_BYTES:
            await ctx.reserve_bulk_memory()
        raw.extend(chunk[:FIELD_LIMIT - len(raw)])
        if len(raw) == FIELD_LIMIT:
            break
    message = upstream_message(redact_error_payload(bytes(raw), secrets), secrets) or f"HTTP {response.status_code}"
    mapped = upstream_http_error(path, response.status_code, message)
    if mapped:
        raise mapped
    raise GatewayError("upstream_invalid", message, status=400, upstream_status=response.status_code,
                       upstream_message=message)


async def _spool_response(ctx, response, output):
    declared = response.headers.get("Content-Length")
    if declared is not None and (not declared.isascii() or not declared.isdigit() or len(declared) > 20 or int(declared) > REPLY_LIMIT):
        raise GatewayError("upstream", "上游媒体响应长度不正确或超过 64MB 限制")
    total = 0
    async for chunk in response.aiter_raw(chunk_size=CHUNK):
        total += len(chunk)
        if total > REPLY_LIMIT:
            raise GatewayError("upstream", "上游媒体响应超过 64MB 限制")
        await ctx.sync(output.write, chunk)
    if declared is not None and int(declared) != total:
        raise GatewayError("network", "上游媒体响应未完整接收，费用等待核对")
    await ctx.sync(output.seek, 0)
    return total


async def _relay(ctx, response, path, source, body):
    from .async_text import relay_stream

    media, call = ctx.gateway.media, ctx.call
    status = response.status_code
    secrets = ctx.gateway.error_secrets(call)
    call["upstream_request_id"] = safe_header(response.headers.get("request-id") or response.headers.get("x-request-id") or "", secrets)
    if not 200 <= status < 300:
        await _provider_error(ctx, response, path)
    call["forwarded"], call["status"] = True, status
    call["billing"] = _pending("provider_usage_not_received")
    call["reply_headers"] = transport.passed_headers(response.headers, secrets)
    call["pricing_headers"] = {k: v for k, v in response.headers.items()
                               if k.lower() in {"inference-cost", "inference-currency", "inference-cost-currency"}}
    call["first_token_ms"] = int((time.monotonic() - call["started"]) * 1000)
    mime = safe_header(response.headers.get("Content-Type", "application/json"), secrets).split(";", 1)[0].strip().lower()
    if response.headers.get("Content-Encoding", "identity").lower() != "identity":
        raise GatewayError("upstream", "上游返回了不支持的媒体压缩编码")
    submission = path.startswith("/v1/async/")
    if mime == "text/event-stream":
        if submission:
            raise GatewayError("upstream", "异步任务提交未返回任务 JSON")
        call["stream"] = True
        call["billing_finalize"] = lambda: media._quote(call)
        try:
            await relay_stream(ctx, "openai", response, mime)
        finally:
            media._quote(call)
        return
    with tempfile.SpooledTemporaryFile(max_size=SPOOL_MEMORY, mode="w+b") as output:
        size = await _spool_response(ctx, response, output)
        call["reply_bytes"] = size
        if mime == "application/json" or mime.endswith("+json"):
            if size >= BULK_BYTES:
                await ctx.reserve_bulk_memory()
            raw = await ctx.sync(output.read)
            data = await ctx.sync(media.response_json, raw, call, status)
            if submission:
                data = await register_task(ctx, source, data, body, response.headers)
                raw = await ctx.sync(gateway_json, data)
            else:
                await ctx.sync(media._quote, call, data)
            call["done"] = True
            ctx.gateway.keep(call, raw)
            await ctx.record()
            await ctx.send(status, "application/json", raw)
            return
        if submission or not (mime.startswith(("audio/", "image/")) or mime == "application/octet-stream"):
            raise GatewayError("upstream", "上游返回了不支持的媒体类型")
        metadata = {"_binary": {"bytes": size, "content_type": mime}}
        duration = await ctx.sync(_wav_duration, output, 0, size)
        if duration is not None:
            metadata["duration"] = duration
        media._quote(call, metadata)
        call["done"] = True
        ctx.gateway.keep(call, gateway_json(metadata))
        await ctx.record()
        await ctx.send(status, mime, length=size)
        async for chunk in _file_chunks(ctx, output):
            await ctx.send(status, mime, chunk)


async def serve_media(ctx, path):
    gateway, call = ctx.gateway, ctx.call
    media = gateway.media
    if ctx.handler.command != "POST" or not media.supports(path):
        raise APIError(404, "接口不存在")
    if ctx.request.url.query:
        raise APIError(400, "模型请求参数请放在请求体中")
    source = ctx.config["openai"]
    call["metered_media"], call["requested_at"] = True, time.time()
    call["billing"] = _zero("not_forwarded")
    call["secrets"] += (source["key"],)
    async with ctx.admitted():
        call["user_agent"] = (ctx.handler.headers.get("User-Agent") or "")[:300]
        call["client_ip"] = client_ip(ctx.handler)
        async with _request_body(ctx, media) as (body, content, content_type, length):
            await ctx.sync(media.prepare_request, ctx.handler, path, call, source, body, length,
                           content if content_type == "application/json" else None)
            if await ctx.disconnected():
                raise GatewayError("client_closed", "客户端已断开，请求未发出，不计费")
            headers = {"Authorization": "Bearer " + source["key"], "Content-Type": content_type,
                       "Accept-Encoding": "identity", "Content-Length": str(length), "User-Agent": call["user_agent"] or "gotc-gateway/1.0",
                       **call["routing_headers"]}
            if "X-Compute-Id" in headers:
                headers["X-Failover-Enabled"] = "false"
            url = source["url"].rstrip("/") + path[len("/v1"):]
            try:
                response = await ctx.upstream.open("POST", url, headers=headers, content=content, length=length,
                                                   retry=False, recheck=ctx.recheck)
            except transport.UpstreamNetworkError as error:
                if error.request_sent:
                    call["billing"] = _pending("submission_outcome_unknown")
                raise GatewayError("network", "上游连接失败，已提交请求的费用等待核对",
                                   upstream_message=transport.failure_reason(error)) from None
            async with aclosing(response):
                try:
                    await _relay(ctx, response, path, source, body)
                except httpx.HTTPError as error:
                    raise GatewayError("network", "上游媒体响应中断，费用等待核对",
                                       upstream_message=transport.failure_reason(error)) from None
