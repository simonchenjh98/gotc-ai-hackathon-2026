"""Owned task lookup, cancellation and settlement for the asynchronous gateway."""
import asyncio
import json
import sqlite3
import tempfile
from contextlib import aclosing
from functools import partial
from urllib.parse import quote

import httpx

from backend.gotc.errors import APIError
from .gateway_tasks import LargeTaskSnapshot, POLL_INTERVAL, RESPONSE_LIMIT, TERMINAL
from .memory import BULK_BYTES
from .requests import gateway_json
from .transport import UpstreamNetworkError


async def _load(sync, reserve, fn, *args):
    try:
        return await sync(partial(fn, *args, small_only=True))
    except LargeTaskSnapshot:
        await reserve()
        return await sync(fn, *args)


async def _fetch(tasks, provider, sync, task, method="GET", action="", *, reserve=None):
    if not task["upstream_key"]:
        raise APIError(502, "异步任务已结束，无法继续访问上游")
    suffix = "/task/" + quote(task["remote_id"], safe="") + action
    if tasks.transport is not None:
        # Preserve the existing explicit transport injection for offline providers.
        status, payload, headers = await sync(tasks.transport, task, method, suffix)
        if not 200 <= status < 300:
            raise APIError(502, "上游异步任务查询失败")
        return payload, tasks._billing_headers(headers, task["upstream_key"])
    url = task["upstream_url"] + suffix
    headers = {"Authorization": "Bearer " + task["upstream_key"], "Accept": "application/json",
               "Accept-Encoding": "identity", "Content-Type": "application/json"}
    data = b"{}" if method == "POST" else None
    try:
        response = await provider.open(method, url, headers=headers, content=data,
                                       length=len(data) if data else None, timeout=30, retry=False)
        with tempfile.SpooledTemporaryFile(max_size=BULK_BYTES, mode="w+b") as spool:
            size = 0
            async with aclosing(response):
                if not 200 <= response.status_code < 300 or response.headers.get("Content-Encoding", "identity").lower() != "identity":
                    raise APIError(502, "上游异步任务查询失败")
                async for chunk in response.aiter_raw(chunk_size=64 * 1024):
                    end = size + len(chunk)
                    if end > RESPONSE_LIMIT:
                        raise APIError(502, "异步任务返回内容过大")
                    if reserve is not None and size < BULK_BYTES <= end:
                        await reserve()
                    await sync(spool.write, chunk)
                    size = end
            await sync(spool.seek, 0)
            try:
                payload = await sync(json.load, spool)
            except (ValueError, UnicodeError, RecursionError):
                raise APIError(502, "异步任务返回格式不正确") from None
            return payload, tasks._billing_headers(response.headers, task["upstream_key"])
    except (UpstreamNetworkError, httpx.HTTPError):
        raise APIError(502, "上游异步任务暂时不可达") from None


async def _refresh(tasks, provider, sync, task, *, reserve=None):
    if task["settled"]:
        return task
    try:
        payload, headers = await _fetch(tasks, provider, sync, task, reserve=reserve)
    except APIError:
        if task["state"] in TERMINAL:
            return await sync(tasks._settle, task)
        raise
    if tasks.needs_result(task, payload, headers):
        try:
            output, result_headers = await _fetch(tasks, provider, sync, task, "GET", "/get", reserve=reserve)
            if not isinstance(output, dict):
                raise APIError(502, "上游异步任务结果格式不正确")
            payload = dict(payload, output=output)
            headers.update(result_headers)
        except APIError:
            if "output" not in payload:
                raise
    return await sync(tasks.save_refresh, task, payload, headers)


async def register_task(ctx, source, payload, request, headers):
    tasks = ctx.gateway.tasks
    # An idempotent replay can return a large already-stored result.
    await ctx.reserve_bulk_memory()
    task = await ctx.sync(partial(tasks.store_submission, ctx.call, source, payload,
                                  request=request, headers=headers))
    if task["state"] in TERMINAL and not task["settled"]:
        # A registration can already own the memory lease. Do not wait for the
        # poller's lock here. Registration only reads the provider, and the database
        # compare-and-set settles the single existing usage row across both paths.
        if task["state"] == "success":
            task = await _refresh(tasks, ctx.upstream, ctx.sync, task, reserve=ctx.reserve_bulk_memory)
        else:
            task = await ctx.sync(tasks._settle, task)
    return await ctx.sync(tasks._public, task)


async def serve_task(ctx, path):
    tasks = ctx.gateway.tasks
    key = await ctx.authenticate()
    task_id, action = tasks.route(ctx.handler.command, path)
    async with ctx.admitted(lane="task"):
        # Poll/cancel endpoints have no input body. Serialize access to the task
        # before acquiring memory for a large cached or newly received reply.
        async with tasks.async_lock(task_id):
            task = await _load(ctx.sync, ctx.reserve_bulk_memory, tasks._owned, key, task_id)
            if action == "/cancel" and task["state"] not in TERMINAL:
                await _fetch(tasks, ctx.upstream, ctx.sync, task, "POST", "/cancel", reserve=ctx.reserve_bulk_memory)
            await _refresh(tasks, ctx.upstream, ctx.sync, task, reserve=ctx.reserve_bulk_memory)
            task = await _load(ctx.sync, ctx.reserve_bulk_memory, tasks._owned, key, task_id)
            status, payload, headers = await ctx.sync(tasks.reply, task, action)
        ctx.call["recorded"] = True  # One usage row belongs to the original submission.
        ctx.call["reply_headers"].update({k: v for k, v in headers.items() if k.lower() == "retry-after"})
        await ctx.send(status, "application/json", await ctx.sync(gateway_json, payload))


async def recover_once(gateway, provider, sync):
    tasks = gateway.tasks
    for task_id in await sync(tasks.due_ids):
        async with tasks.async_lock(task_id), gateway.memory.lease() as lease:
            try:
                task = await _load(sync, lease.acquire, tasks._get, task_id)
                await _refresh(tasks, provider, sync, task, reserve=lease.acquire)
            except Exception:
                # Persist only the retry time and a constant reason. Provider errors
                # and stored credentials stay out of the background service logs.
                await sync(tasks.defer, task_id)


async def reconcile_tasks(gateway, provider, sync):
    """Gateway lifespan cancels this task before closing its shared HTTPX client."""
    while True:
        try:
            await recover_once(gateway, provider, sync)
        except sqlite3.Error:
            pass  # Retry after a transient database lock or unavailable disk.
        await asyncio.sleep(POLL_INTERVAL)
