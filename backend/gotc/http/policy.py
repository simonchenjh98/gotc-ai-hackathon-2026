"""Application-level policies over already parsed ASGI requests and responses."""
from __future__ import annotations

import ipaddress
from urllib.parse import unquote

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse

from ..errors import APIError
from ..settings import hsts_allowed
from ..teams.team_registration import PUBLIC_ORIGINS, PUBLIC_PATHS


def client_ip(scope, trusted_proxy_socket=None) -> str:
    """Trust the one address Nginx sets only on its configured Unix listener."""
    listener = scope.get("server")
    if trusted_proxy_socket and listener and tuple(listener) == (str(trusted_proxy_socket), None):
        values = [value.decode("latin-1") for name, value in scope["headers"] if name.lower() == b"x-real-ip"]
        if len(values) == 1:
            try:
                return str(ipaddress.ip_address(values[0]))
            except ValueError:
                pass
    peer = scope.get("client")
    return peer[0] if peer else "unknown"


def api_error(error: APIError) -> JSONResponse:
    data = {"error": error.message}
    if error.code:
        data["code"] = error.code
    if getattr(error, "request_id", None):
        data["requestId"] = error.request_id
    return JSONResponse(data, status_code=error.status, media_type="application/json; charset=utf-8")


class HTTPPolicy:
    def __init__(self, app, *, service):
        self.app = app
        self.service = service

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def secured(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("Cache-Control", "no-store")
                headers.setdefault("X-Content-Type-Options", "nosniff")
                headers.setdefault("X-Frame-Options", "DENY")
                headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
                headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
                headers.setdefault("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; "
                                   "img-src 'self' data:; font-src 'self'; connect-src 'self' " + self.service.origin +
                                   "; object-src 'none'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
                if scope["path"] in {self.service.base_path + path for path in PUBLIC_PATHS}:
                    origins = Headers(scope=scope).getlist("origin")
                    if len(origins) == 1 and origins[0] in PUBLIC_ORIGINS:
                        headers.setdefault("Access-Control-Allow-Origin", origins[0])
                        headers.setdefault("Vary", "Origin")
                if hsts_allowed(self.service.origin):
                    headers.setdefault("Strict-Transport-Security", "max-age=31536000")
            await send(message)

        # Frameworks expose the raw *target* separately from the decoded route.
        # Validate it once, without decoding the framework's path a second time.
        try:
            raw = scope.get("raw_path")
            path = unquote(raw.decode("ascii"), errors="strict") if raw is not None else scope["path"]
            if (not path.startswith("/") or path.startswith("//") or b"#" in (raw or b"")
                    or "\\" in path or "\0" in path or any(p in {".", ".."} for p in path.split("/"))):
                raise ValueError
        except (UnicodeError, ValueError):
            response = api_error(APIError(400, "请求路径不正确"))
            return await response(scope, receive, secured)
        if scope["method"] not in {"GET", "HEAD", "POST", "OPTIONS"}:
            return await api_error(APIError(501, "请求方法不支持"))(scope, receive, secured)
        await self.app(scope, receive, secured)
