"""Upstream HTTP connections, retry eligibility and network failure classification."""

import http.client
import socket
import ssl
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from .gateway_uploads import LARGE_BODY_THRESHOLD, UploadLimitedConnectionMixin, UploadQueueFull
from .protocol_errors import GatewayError


UPSTREAM_TIMEOUT = 1200  # waiting for the reply to start
CONNECT_TIMEOUT = 20  # connect + TLS handshake (sub2api dial and TLS timeouts), so a dead upstream frees the slot fast


RETRY_STATUSES = {429, 503, 529}  # explicit "not processed" answers; 5xx/timeouts may already be billed upstream


RETRY_DELAYS = (0.3, 0.6, 1.2, 2.4)  # sub2api: 5 attempts, 300 ms doubling, 3 s cap  # seconds; retried only before any byte reaches the client


# Upstream reply headers passed to the team (sub2api responseheaders allowlist). Rate-limit headers are deliberately not
# passed: they describe our shared upstream key, not the team.
PASSED_HEADERS = ("x-request-id", "request-id", "retry-after", "openai-processing-ms")
CLIENT_HEADERS = ("x-app", "x-claude-code-session-id", "accept-language")  # forwarded to Anthropic, as sub2api


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # never replay the upstream key to another location


class _ShortConnect(UploadLimitedConnectionMixin):
    """Connect and TLS handshake within CONNECT_TIMEOUT; reads then use the request's own timeout."""

    def connect(self):
        read_timeout, self.timeout = self.timeout, CONNECT_TIMEOUT
        try:
            super().connect()
        finally:
            self.timeout = read_timeout
        if self.sock is not None:
            self.sock.settimeout(read_timeout)


class _HTTPConnection(_ShortConnect, http.client.HTTPConnection):
    pass


class _HTTPSConnection(_ShortConnect, http.client.HTTPSConnection):
    pass


class _HTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        try:
            return self.do_open(_HTTPConnection, req, upload_admitted=getattr(req, "_gotc_upload_admitted", None))
        except UploadQueueFull:
            raise GatewayError("rate_limit", "中转站大请求上传繁忙，请稍后重试（请求尚未发出，不计费）", retry_after=5) from None


class _HTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        try:
            return self.do_open(_HTTPSConnection, req, context=self._context,
                                upload_admitted=getattr(req, "_gotc_upload_admitted", None))
        except UploadQueueFull:
            raise GatewayError("rate_limit", "中转站大请求上传繁忙，请稍后重试（请求尚未发出，不计费）", retry_after=5) from None


def make_opener(proxies=None):
    """Upstream HTTP client: no redirects, short connect timeout; proxies from the environment unless given."""
    handlers = [_NoRedirect, _HTTPHandler(), _HTTPSHandler()]
    if proxies is not None:
        handlers.append(urllib.request.ProxyHandler(proxies))
    return urllib.request.build_opener(*handlers)


def passed_headers(headers):
    return {name: headers[name][:200] for name in PASSED_HEADERS if headers.get(name)}


def set_read_timeout(response, seconds):
    sock = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
    if sock is not None:
        sock.settimeout(seconds)


def request_was_sent(error):
    """Whether the upstream can have received the whole request when this error occurred.

    urllib wraps every failure that happens while connecting, during the TLS handshake (certificate checks included)
    or while sending the request in URLError; failures after the request was sent (waiting for or reading the reply)
    are raised unwrapped. HTTPError is a URLError but means the upstream answered.
    """
    return getattr(getattr(error, "reason", error), "request_sent", False) or isinstance(error, urllib.error.HTTPError) or not isinstance(error, urllib.error.URLError)


def failure_reason(error):
    """Admin-facing explanation of an upstream network failure."""
    reason = getattr(error, "reason", error)
    if isinstance(reason, ssl.SSLCertVerificationError):
        return f"上游 TLS 证书校验失败（请检查上游地址与证书）：{reason.verify_message or reason}"
    if isinstance(reason, ssl.SSLError):
        return f"上游 TLS 握手失败（请检查上游地址是否为 HTTPS）：{reason.reason or reason}"
    if isinstance(reason, socket.gaierror):
        return "上游域名无法解析"
    if isinstance(reason, ConnectionRefusedError):
        return "上游拒绝连接"
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return f"上游超时（{UPSTREAM_TIMEOUT} 秒）"
    return f"上游连接失败：{type(reason).__name__}：{reason}"[:300]


def open_with_retry(opener, request):
    """Retry upstream overload and connection errors; safe because nothing has reached the client yet."""
    large_upload = (urlsplit(request.full_url).hostname == "ai.gitee.com" and
                    isinstance(request.data, (bytes, bytearray)) and len(request.data) >= LARGE_BODY_THRESHOLD)
    # Replaying a large context up to five times worsens an already saturated exit link.
    # Surface the provider's overload and Retry-After so the client can back off instead.
    for delay in (*(RETRY_DELAYS if not large_upload else ()), None):
        try:
            return opener.open(request, timeout=UPSTREAM_TIMEOUT)
        except urllib.error.HTTPError as error:
            if delay is None or error.code not in RETRY_STATUSES:
                raise
            try:  # the upstream says when to come back; honoured up to the 3 s cap
                delay = min(3.0, max(delay, float(error.headers.get("retry-after") or 0)))
            except ValueError:
                pass
            error.close()
        except OSError as error:
            # Retry only when the request certainly never reached the upstream and the failure can be transient.
            # TLS errors are configuration problems and timeouts would each wait the full upstream timeout again.
            reason = getattr(error, "reason", error)
            if delay is None or request_was_sent(error) or not isinstance(reason, (ConnectionError, socket.gaierror)):
                raise
        time.sleep(delay)
