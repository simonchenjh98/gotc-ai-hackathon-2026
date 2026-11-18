"""ASGI request state shared by the text, media and realtime gateway routes."""
import asyncio
import time
import uuid
from contextlib import asynccontextmanager
from email.message import Message
from types import SimpleNamespace

import anyio
from starlette.responses import Response

from .gateway_cors import response_headers
from .memory import BULK_BYTES, bulk_input
from .protocol_errors import ClientGone, GatewayError
from .usage import USAGE_FIELDS


async def await_owned(task):
    """Let the current resource owner finish before propagating a caller cancellation."""
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        with anyio.CancelScope(shield=True):
            while not task.done():
                try:
                    await asyncio.wait({task})
                except asyncio.CancelledError:
                    continue
        if not task.cancelled():
            task.exception()  # consume any failure while preserving the caller cancellation
        raise


class GatewayContext:
    def __init__(self, gateway, request, path):
        self.gateway, self.request = gateway, request
        headers = Message()
        for name, value in request.headers.items():
            headers[name] = value
        scope = request.scope
        state = getattr(scope.get("app"), "state", None)
        self.limiter = getattr(state, "gateway_limiter", None)
        from ..http.policy import client_ip
        ip = client_ip(scope, getattr(state, "trusted_proxy_socket", None))
        self.handler = SimpleNamespace(headers=headers, command=scope.get("method", "GET"),
                                       path=str(request.url.path) + ("?" + request.url.query if request.url.query else ""),
                                       client_ip=ip, client_address=(ip, 0))
        self.call = {"key": None, "path": path, "model": "", "stream": False, "price": None, "usage": [0] * USAGE_FIELDS,
                     "status": 0, "request": b"", "response": [], "size": 0, "started": time.monotonic(), "log": False,
                     "request_bytes": 0, "reply_bytes": 0, "done": False, "forwarded": False, "reply_headers": {},
                     "request_id": scope.get("state", {}).get("request_id") or uuid.uuid4().hex,
                     "secrets": gateway.error_secrets(), "requested_at": time.time()}
        self.config = gateway.config
        self.bulk_lease = None
        self._record_task = None
        self._complete = False

    @property
    def upstream(self):
        return self.gateway.opener.async_provider()

    async def sync(self, fn, *args):
        # A native ASGI task.cancel() can interrupt an AnyIO worker await while
        # its thread keeps reading a file or using a large Python object graph.
        job = asyncio.create_task(anyio.to_thread.run_sync(fn, *args, limiter=self.limiter))
        return await await_owned(job)

    async def disconnected(self):
        # is_disconnected() polls ASGI receive. Only poll after the consumer has
        # exhausted request.stream(), or we might steal a pending request body event.
        if self.request.scope["type"] == "http" and getattr(self.request, "_stream_consumed", False):
            return await self.request.is_disconnected()
        return False

    async def reserve_bulk_memory(self):
        if self.bulk_lease is None:
            self.bulk_lease = self.gateway.memory.lease()
        await self.bulk_lease.acquire()

    def release_bulk_memory(self):
        if self.bulk_lease:
            self.bulk_lease.release()

    def close(self):
        try:
            capture = self.call.pop("request_file", None)
            if capture is not None:
                capture.close()
        finally:
            self.release_bulk_memory()

    async def recheck(self):
        await self.sync(self.gateway.recheck_access, self.handler, self.call)
        if await self.disconnected():
            raise GatewayError("client_closed", "客户端已断开，请求未发出，不计费")

    @asynccontextmanager
    async def admitted(self, count_tokens=False, defer_rpm=False):
        key = await self.sync(self.gateway.authenticate, self.handler, self.call)
        team = key["team_id"]
        rpm = self.call["settings"]["teamRpm"]
        options = {"defer_rpm": True} if defer_rpm else {}
        if self.handler.command == "POST" and bulk_input(self.handler.headers):
            self.bulk_lease = self.gateway.memory.lease()
            options["bulk_lease"] = self.bulk_lease
        await self.gateway.admit_async(team, rpm or (120 if count_tokens else 0), count_tokens=count_tokens, **options)
        try:
            await self.recheck()
            yield key
        finally:
            self.gateway.release(team, count_tokens=count_tokens)

    async def charge_rpm(self):
        self.gateway.charge_rpm(self.call["key"]["team_id"], self.call["settings"]["teamRpm"])

    async def record(self):
        if self._record_task is None:
            if not self.call["key"] or self.call.get("recorded"):
                return
            self._record_task = asyncio.create_task(self._persist())
        task = self._record_task
        try:
            await await_owned(task)
        finally:
            if task.done() and self._record_task is task:
                self._record_task = None

    async def _persist(self):
        if self.call.get("request_file") is not None:
            await self.reserve_bulk_memory()
        await self.sync(self.gateway.safe_record, self.handler, self.call)
        if not self.call.get("recorded") and not self.call.get("retried_record"):
            # A transient write failure must not let a complete response outrun
            # its ledger entry. Retry persistence only, never the provider call.
            self.call["retried_record"] = True
            await self.sync(self.gateway.safe_record, self.handler, self.call)

    async def send(self, status, content_type, payload=None, length=None):
        if self._complete:
            raise RuntimeError("Response already completed")
        if not self.call.get("headers_sent") and payload is not None and len(payload) >= BULK_BYTES:
            # Uvicorn applies socket backpressure between ASGI sends. One giant
            # write per slow client would retain an entire reply beyond its lease.
            await self.send(status, content_type, length=len(payload))
            for offset in range(0, len(payload), 65536):
                await self.send(status, content_type, payload[offset:offset + 65536])
            await self.finish()
            return
        try:
            if await self.disconnected():
                raise ClientGone()
            if not self.call.get("headers_sent"):
                headers = {"Content-Type": content_type, "X-Client-Request-ID": self.call["request_id"],
                           **response_headers(self.handler.headers, self.gateway.service.origin), **self.call["reply_headers"]}
                self.call["headers_sent"] = True
                if payload is not None:
                    response = Response(payload, status_code=status, headers=headers)
                    await response(self.request.scope, self.request.receive, self.request.scope["gateway_send"])
                    self._complete = True
                    return
                if length is None:
                    headers["X-Accel-Buffering"] = "no"
                else:
                    headers["Content-Length"] = str(length)
                response = Response(status_code=status, headers=headers)
                if length is None and "Content-Length" in response.headers:
                    del response.headers["Content-Length"]
                await self.request.scope["gateway_send"]({"type": "http.response.start", "status": status,
                                                          "headers": response.raw_headers})
            if payload:
                await self.request.scope["gateway_send"]({"type": "http.response.body", "body": payload, "more_body": True})
        except OSError:
            raise ClientGone() from None

    async def finish(self):
        if self.call.get("headers_sent") and not self._complete:
            self._complete = True
            try:
                await self.request.scope["gateway_send"]({"type": "http.response.body", "body": b""})
            except OSError:
                pass
