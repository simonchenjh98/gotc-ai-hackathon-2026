"""Pooled async HTTPX transport, bounded provider uploads and explicit generation retry policy."""
import asyncio
from http.cookiejar import CookieJar, DefaultCookiePolicy
import re
import socket
import ssl
from urllib.parse import urlsplit
from weakref import WeakKeyDictionary

import httpx

from . import gateway_uploads
from .admission import GatewayCapacity
from .memory import BULK_BYTES
from .protocol_errors import GatewayError, safe_header


UPSTREAM_TIMEOUT = 1200
CONNECT_TIMEOUT = 20
POOL_TIMEOUT = 0.25  # Admission owns waiting; an exhausted pool is a capacity error.
RETRY_STATUSES = {429, 503, 529}
RETRY_DELAYS = (0.3, 0.6, 1.2, 2.4)
PASSED_HEADERS = ("x-request-id", "request-id", "retry-after", "openai-processing-ms")
CLIENT_HEADERS = ("x-app", "x-claude-code-session-id", "accept-language")


class UpstreamNetworkError(OSError):
    def __init__(self, error, *, sent):
        self.reason = error
        self.request_sent = sent
        super().__init__(type(error).__name__)  # Never put credentials, provider URLs or request contents in exceptions.


def passed_headers(headers, secrets=()):
    return {name: safe_header(headers[name], secrets) for name in PASSED_HEADERS if headers.get(name)}


def request_was_sent(error):
    return getattr(error, "request_sent", True)


def _causes(error):
    pending = [getattr(error, "reason", error)]
    seen = set()
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        pending.append(current.__cause__ or current.__context__)
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)


def is_timeout(error):
    return any(isinstance(cause, (TimeoutError, httpx.TimeoutException)) for cause in _causes(error))


def failure_reason(error):
    causes = tuple(_causes(error))
    if any(isinstance(cause, ssl.SSLCertVerificationError) for cause in causes):
        return "上游 TLS 证书校验失败（请检查上游地址与证书）"
    if is_timeout(error):
        return f"上游超时（{UPSTREAM_TIMEOUT} 秒）"
    if any(isinstance(cause, ssl.SSLError) for cause in causes):
        return "上游 TLS 握手失败（请检查上游地址是否为 HTTPS）"
    if any(isinstance(cause, socket.gaierror) for cause in causes):
        return "上游域名无法解析"
    if any(isinstance(cause, ConnectionRefusedError) for cause in causes):
        return "上游拒绝连接"
    if any(isinstance(cause, httpx.ProxyError) for cause in causes):
        return "上游代理隧道连接失败（Tunnel connection failed）"
    return f"上游连接失败：{type(causes[0]).__name__}" if causes else "上游连接失败"


async def _body_chunks(data):
    # An exhausted async generator frees its payload. HTTPX and httpcore retain
    # the request stream alongside the reply, including for long-lived SSE.
    for start in range(0, len(data), 64 * 1024):
        yield data[start:start + 64 * 1024]


class AsyncProvider:
    def __init__(self, *, proxies=None, transport=None, max_connections=None):
        # HTTPX verifies TLS, honors environment proxies/NO_PROXY and has zero transport retries by default.
        max_connections = max_connections or GatewayCapacity().provider_connections
        limits = httpx.Limits(max_connections=max_connections, max_keepalive_connections=min(16, max_connections))
        options = {"trust_env": proxies is None, "limits": limits, "follow_redirects": False}
        if transport is not None:
            options["transport"] = transport
        if proxies:
            options["mounts"] = {scheme + "://": httpx.AsyncHTTPTransport(proxy=url, limits=limits)
                                 for scheme, url in proxies.items() if scheme in {"http", "https"}}
        self.client = httpx.AsyncClient(cookies=CookieJar(policy=DefaultCookiePolicy(allowed_domains=())), **options)
        self.uploads = asyncio.Semaphore(gateway_uploads.UPLOAD_CONCURRENCY)

    async def aclose(self):
        await self.client.aclose()

    async def open(self, method, url, *, headers, content=None, length=None, timeout=None, retry=False, recheck=None):
        if re.search(r"[\x00-\x20\x7f]", url):
            raise ValueError("Invalid upstream URL")
        if length is None and isinstance(content, (bytes, bytearray)):
            length = len(content)
        if length is not None and not any(name.lower() == "content-length" for name in headers):
            headers = {**headers, "Content-Length": str(length)}
        large = urlsplit(url).hostname == "ai.gitee.com" and length is not None and length >= gateway_uploads.LARGE_BODY_THRESHOLD
        replay = retry and not large and (content is None or isinstance(content, (bytes, bytearray))
                                           or getattr(content, "replayable", False))
        delays = RETRY_DELAYS if replay else ()
        for delay in (*delays, None):
            try:
                response = await self._once(method, url, headers, content, timeout, large, recheck)
            except UpstreamNetworkError as error:
                transient = isinstance(error.reason, httpx.ConnectError) and not any(
                    isinstance(cause, ssl.SSLError) for cause in _causes(error))
                if delay is None or error.request_sent or not transient:
                    raise
            else:
                if response.status_code not in RETRY_STATUSES or delay is None:
                    return response
                try:
                    delay = min(3.0, max(delay, float(response.headers.get("retry-after", 0))))
                except ValueError:
                    pass
                finally:
                    await response.aclose()
            await asyncio.sleep(delay)

    async def _once(self, method, url, headers, content, timeout, large, recheck):
        held = False
        provider_write = False
        sent = False

        async def trace(name, info):
            nonlocal held, provider_write, sent
            # HTTPS proxy CONNECT shares the trace callback with the actual generation.
            if name.endswith(".send_request_headers.started"):
                provider_write = info["request"].method != b"CONNECT"
            if provider_write and name.endswith((".send_request_body.complete", ".send_request_body.failed",
                                                 ".send_request_headers.failed")):
                sent = True  # A complete write or an ambiguous partial write cannot be replayed.
                if held:
                    self.uploads.release()
                    held = False

        try:
            if large:
                try:
                    await asyncio.wait_for(self.uploads.acquire(), gateway_uploads.QUEUE_TIMEOUT)
                except TimeoutError:
                    raise GatewayError("rate_limit", "中转站大请求上传繁忙，请稍后重试（请求尚未发出，不计费）", retry_after=5) from None
                held = True
            if recheck is not None:
                await recheck()
            read = UPSTREAM_TIMEOUT if timeout is None else timeout
            timing = httpx.Timeout(connect=CONNECT_TIMEOUT, read=read, write=read, pool=POOL_TIMEOUT)
            # Build an independent request so a provider's Set-Cookie cannot introduce shared user state.
            if isinstance(content, bytes) and len(content) >= BULK_BYTES:
                content = _body_chunks(content)
            request = httpx.Request(method, url, headers={"Accept-Encoding": "identity", **headers}, content=content,
                                    extensions={"trace": trace, "timeout": timing.as_dict()})
            try:
                response = await self.client.send(request, stream=True, follow_redirects=False)
                # Bound already parsed provider metadata even when the networking library
                # receives an entire oversized header in one read.
                fields = response.headers.raw
                if len(fields) > 100 or any(len(key) + len(value) + 4 > 65536 for key, value in fields):
                    await response.aclose()
                    raise UpstreamNetworkError(httpx.RemoteProtocolError("Upstream header limit"), sent=True)
                return response
            except httpx.PoolTimeout:
                raise GatewayError("overloaded", "中转站上游连接池繁忙，请稍后重试（请求未发出，不计费）", retry_after=2) from None
            except httpx.TransportError as error:
                raise UpstreamNetworkError(error, sent=sent) from None
        finally:
            if held:
                self.uploads.release()


class ProviderClients:
    """One pool per serving event loop. Tests can supply a direct or synthetic transport."""
    def __init__(self, proxies=None, transport=None, max_connections=None):
        self.proxies, self.transport = proxies, transport
        self.max_connections = max_connections
        self._providers = WeakKeyDictionary()

    def async_provider(self):
        loop = asyncio.get_running_loop()
        if loop not in self._providers:
            self._providers[loop] = AsyncProvider(proxies=self.proxies, transport=self.transport,
                                                  max_connections=self.max_connections)
        return self._providers[loop]

    async def close(self):
        provider = self._providers.pop(asyncio.get_running_loop(), None)
        if provider:
            await provider.aclose()


def make_opener(proxies=None, *, transport=None, max_connections=None):
    return ProviderClients(proxies, transport, max_connections)


async def probe(opener, method, url, headers, content):
    response = await opener.async_provider().open(method, url, headers=headers, content=content, timeout=30)
    try:
        limit = 2_000_001 if response.is_success else 100_000
        raw = bytearray()
        async for part in response.aiter_raw():
            raw.extend(part[:limit - len(raw)])
            if len(raw) >= limit:
                break
        return response.status_code, bytes(raw)
    except httpx.TransportError as error:
        raise UpstreamNetworkError(error, sent=True) from None
    finally:
        await response.aclose()
