"""Streaming media requests and provider responses for the ASGI gateway."""
from contextlib import aclosing, ExitStack
import json
import os
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


async def _prepare_request(ctx, media, source, path, files):
    from .async_text import ReplayableFile, read_json, request_chunks, spool_json

    headers = ctx.handler.headers
    mime = headers.get_content_type()
    metadata_file = None
    spool_bytes = 0
    if mime == "application/json":
        body, decoded_size = await read_json(ctx)
        content_type = "application/json"
        if decoded_size >= BULK_BYTES:
            metadata_file, length = await ctx.sync(spool_json, body)
            files.enter_context(metadata_file)
            content = ReplayableFile(metadata_file, ctx.sync)
            metadata_size = length
            spool_bytes = length
        else:
            content = await ctx.sync(gateway_json, body)
            length = len(content)
    else:
        length = _length(ctx.handler)
        if headers.get("Content-Encoding", "identity").lower() != "identity":
            raise APIError(415, "媒体表单不支持 Content-Encoding")
        if mime not in ("multipart/form-data", "application/x-www-form-urlencoded"):
            raise APIError(415, "请使用 JSON、multipart/form-data 或表单请求")
        content_type = headers.get("Content-Type", "")
        spool = files.enter_context(tempfile.SpooledTemporaryFile(max_size=SPOOL_MEMORY, mode="w+b"))
        if length >= SPOOL_MEMORY:
            await ctx.sync(spool.rollover)
            spool_bytes = length
        async for chunk in request_chunks(ctx.request, length):
            await ctx.sync(spool.write, chunk)
            del chunk
        body, metadata_bytes = await ctx.sync(media.parse_form, spool, mime, content_type, length)
        content = _file_chunks(ctx, spool)
        if metadata_bytes >= BULK_BYTES:
            metadata_file, metadata_size = await ctx.sync(spool_json, body)
            files.enter_context(metadata_file)
            spool_bytes += metadata_size

    # prepare_request normally captures the validated wire JSON (or form metadata).
    # Large captures are already on disk. Give the ledger its own handle since
    # errors are recorded after this function's caller closes its temporary files.
    encoded = b"" if metadata_file is not None else (content if mime == "application/json" else None)
    try:
        await ctx.sync(media.prepare_request, ctx.handler, path, ctx.call, source, body, length, encoded)
    finally:
        if metadata_file is not None and ctx.call["log"] and ctx.call["request_bytes"]:
            metadata_file.flush()
            ctx.call["request_file"] = os.fdopen(os.dup(metadata_file.fileno()), "rb")
            ctx.call["request_capture_bytes"] = metadata_size
    if metadata_file is not None:
        # The same metadata can be read later for pricing, under the response lease.
        del ctx.call["pricing_request"]
    if spool_bytes:
        ctx.retain_spool(spool_bytes)
    return content, content_type, length, metadata_file


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
    bulk = declared is not None and int(declared) >= BULK_BYTES
    if bulk:
        await ctx.reserve_bulk_memory()
    total = 0
    async for chunk in response.aiter_raw(chunk_size=CHUNK):
        total += len(chunk)
        if total > REPLY_LIMIT:
            raise GatewayError("upstream", "上游媒体响应超过 64MB 限制")
        if not bulk and total >= BULK_BYTES:
            await ctx.reserve_bulk_memory()
            bulk = True
        await ctx.sync(output.write, chunk)
    if declared is not None and int(declared) != total:
        raise GatewayError("network", "上游媒体响应未完整接收，费用等待核对")
    await ctx.sync(output.seek, 0)
    return total


async def _relay(ctx, response, path, source, metadata_file):
    from .async_text import _load_file, relay_stream

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
    if metadata_file is not None:
        await ctx.reserve_bulk_memory()
        call["pricing_request"] = await ctx.sync(_load_file, metadata_file, json.loads)
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
            raw = await ctx.sync(output.read)
            data = await ctx.sync(media.response_json, raw, call, status)
            if submission:
                data = await register_task(ctx, source, data, call["pricing_request"], response.headers)
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
    with ExitStack() as files:
        async with ctx.uploading():
            call["user_agent"] = (ctx.handler.headers.get("User-Agent") or "")[:300]
            call["client_ip"] = client_ip(ctx.handler)
            content, content_type, length, metadata_file = await _prepare_request(ctx, media, source, path, files)
        async with ctx.admitted():
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
                    await _relay(ctx, response, path, source, metadata_file)
                except httpx.HTTPError as error:
                    raise GatewayError("network", "上游媒体响应中断，费用等待核对",
                                       upstream_message=transport.failure_reason(error)) from None
