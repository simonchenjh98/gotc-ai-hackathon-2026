"""Starlette routes for the event platform. SQLite work stays off the event loop."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import partial
import json
import sqlite3
from urllib.parse import parse_qs

import anyio
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.requests import ClientDisconnect, Request
from starlette.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import Route

from ..constants import COOKIE_NAME, SESSION_SECONDS
from ..errors import APIError
from ..password_capacity import PASSWORD_PATHS
from ..submissions.repository_backups import ArchiveDownload
from ..submissions.submission_records import query_path
from ..teams.team_registration import PUBLIC_ORIGINS, PUBLIC_PATHS
from .policy import HTTPPolicy, api_error, client_ip
from .routes import Endpoint, endpoints
from .static import immutable_assets, public_response


@dataclass(frozen=True)
class Identity:
    cookie: str
    login_id: str | None
    finals_judge: str | None
    finals_access: str | None

    @classmethod
    def from_headers(cls, headers: Headers):
        return cls(headers.get("cookie", ""), headers.get("x-gotc-login"),
                   headers.get("x-gotc-finals-judge"), headers.get("x-gotc-finals-access"))


def check_actor(service, endpoint: Endpoint, identity: Identity):
    if endpoint.actor == "public":
        return
    with service.db() as db:
        if endpoint.actor == "finals-access":
            service.finals.require_access(db, identity.finals_access)
        else:
            actor = service.account(db, identity.cookie)
            if endpoint.actor == "team-or-admin" and actor and actor["role"] == "admin":
                return
            service.require(actor, "team" if endpoint.actor == "team-or-admin" else endpoint.actor)


def checked_length(headers: Headers) -> int:
    lengths = headers.getlist("content-length")
    if headers.getlist("transfer-encoding") or len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit():
        raise APIError(400, "请求长度不正确")
    try:
        return int(lengths[0])
    except ValueError:
        raise APIError(400, "请求长度不正确") from None


def checked_query(request: Request, path: str) -> dict:
    raw = request.scope["query_string"]
    if not raw:
        return {}
    if request.method != "GET" or not (path.startswith("/api/admin/data/") or query_path(path)):
        raise APIError(400, "此接口不接受查询参数")
    try:
        fields = parse_qs(raw.decode("ascii"), keep_blank_values=True, strict_parsing=True,
                          max_num_fields=20, encoding="utf-8", errors="strict")
    except (ValueError, UnicodeError):
        raise APIError(400, "查询参数格式不正确") from None
    if any(len(values) != 1 or len(key) > 80 or len(values[0]) > 2000 for key, values in fields.items()):
        raise APIError(400, "查询参数不能重复或超长")
    return {key: values[0] for key, values in fields.items()}


def public_origin(request: Request, path: str) -> str | None:
    origins = request.headers.getlist("origin")
    return origins[0] if path in PUBLIC_PATHS and len(origins) == 1 and origins[0] in PUBLIC_ORIGINS else None


def same_origin(request: Request, service, path: str):
    if public_origin(request, path):
        return
    if request.headers.getlist("origin") != [service.origin] or request.headers.getlist("sec-fetch-site") not in (
            [], ["same-origin"], ["none"]):
        raise APIError(403, "仅允许本站发起操作，请从活动官网打开")


async def json_body(request: Request, length: int, endpoint: Endpoint) -> dict:
    if length > endpoint.limit:
        raise APIError(413, "请求内容超过端点大小限制")
    if request.headers.get("content-type", "").partition(";")[0].strip().lower() != "application/json":
        raise APIError(415, "请使用JSON请求")
    body = bytearray()
    try:
        async with asyncio.timeout(15):
            async for data in request.stream():
                if len(body) + len(data) > length:
                    raise APIError(400, "请求长度不正确")
                body.extend(data)
        if len(body) != length:
            raise APIError(400, "请求长度不正确")
        value = json.loads(body.decode("utf-8"), parse_constant=invalid_json_constant)
    except TimeoutError:
        raise APIError(408, "请求超时") from None
    except (ClientDisconnect, UnicodeError, ValueError, RecursionError):
        raise APIError(400, "JSON格式不正确") from None
    return value


def invalid_json_constant(value):
    raise ValueError(value)


class MaterialBody:
    """Bridge the domain's bounded pull reads to Starlette's async body stream."""
    def __init__(self, request: Request):
        self.stream = request.stream()
        self.pending = memoryview(b"")
        self.deadline = asyncio.get_running_loop().time() + 300

    async def read(self, size):
        if not self.pending:
            try:
                async with asyncio.timeout_at(min(self.deadline, asyncio.get_running_loop().time() + 15)):
                    self.pending = memoryview(await anext(self.stream, b""))
            except TimeoutError:
                raise APIError(408, "上传超时，请重新上传", "upload_timeout") from None
            except ClientDisconnect:
                return b""
        part, self.pending = self.pending[:size], self.pending[size:]
        return part


def create_platform_app(service, *, trusted_proxy_socket=None):
    from ..observability import ObservabilityMiddleware

    limiter = anyio.CapacityLimiter(8)
    password_workers = anyio.CapacityLimiter(service.password_checks.capacity.running)
    # Long uploads wait on client bytes. Spare workers return upload_busy promptly.
    upload_workers = anyio.CapacityLimiter(service.material_uploads.CONCURRENCY + 2)
    immutable = immutable_assets(service.root)
    prefix = service.base_path

    async def run(function, *args, **kwargs):
        return await anyio.to_thread.run_sync(partial(function, *args, **kwargs), limiter=limiter)

    def endpoint_route(endpoint):
        async def handle(request):
            path = request.scope["path"][len(prefix):]
            if request.method == "HEAD":
                raise APIError(405, "接口不支持此方法")
            query = checked_query(request, path)
            identity = Identity.from_headers(request.headers)
            body, received_at = {}, None
            if request.method == "POST":
                same_origin(request, service, path)
                length = checked_length(request.headers)
                if endpoint.actor != "public":
                    await run(check_actor, service, endpoint, identity)
                body = await json_body(request, length, endpoint)
                if path == "/api/submission":
                    received_at = service.clock()
            dispatch = partial(service.dispatch_endpoint, endpoint, path, body, identity.cookie,
                               client_ip(request.scope, trusted_proxy_socket), query=query,
                               expected_login_id=identity.login_id, server_received_at=received_at,
                               finals_judge_id=identity.finals_judge, finals_access_token=identity.finals_access)
            if path in PASSWORD_PATHS:
                value, token, action = await service.password_checks.run(
                    lambda: anyio.to_thread.run_sync(dispatch, limiter=password_workers))
            else:
                value, token, action = await run(dispatch)
            if isinstance(value, ArchiveDownload):
                async def chunks():
                    try:
                        while data := await run(value.stream.read, 65536):
                            yield data
                    finally:
                        value.stream.close()
                return StreamingResponse(chunks(), media_type=value.content_type, headers={
                    "Content-Disposition": f'attachment; filename="{value.filename}"', "Content-Length": str(value.size)})
            if isinstance(value, bytes):
                name = ("gotc-submissions" if path == "/api/admin/submissions/export" else
                        "gotc-data" if path.startswith("/api/admin/data/") else
                        "gotc-api-usage" if path == "/api/admin/keys/export" else "gotc-scores")
                return Response(value, media_type="text/csv; charset=utf-8",
                                headers={"Content-Disposition": f'attachment; filename="{name}.csv"'})
            response = JSONResponse(value, media_type="application/json; charset=utf-8")
            if action:
                response.set_cookie(COOKIE_NAME, token or "", max_age=SESSION_SECONDS if action == "set" else 0,
                                    path=prefix or "/", httponly=True, secure=service.secure,
                                    # Preserve the historical wire casing accepted by Starlette.
                                    samesite="Strict")  # type: ignore[arg-type]
            return response

        return Route(prefix + endpoint.path, handle, methods=[endpoint.method], name=f"{endpoint.method} {endpoint.path}")

    async def upload(request):
        path = request.scope["path"][len(prefix):]
        same_origin(request, service, path)
        checked_query(request, path)
        kind = service.material_uploads.kind(path)
        if not kind:
            raise APIError(404, "接口不存在")
        length = checked_length(request.headers)
        stream = MaterialBody(request)

        def read(size):
            return anyio.from_thread.run(stream.read, size)

        result = await anyio.to_thread.run_sync(
            partial(service.material_uploads.upload, request.headers, read, kind, length), limiter=upload_workers)
        return JSONResponse(result, media_type="application/json; charset=utf-8")

    async def material(request):
        path = request.scope["path"][len(prefix):]
        checked_query(request, path)
        source, mime, headers = await run(service.material_uploads.download, path)
        return FileResponse(source, media_type=mime, headers=headers)

    async def static(request):
        path = request.scope["path"][len(prefix):]
        checked_query(request, path)
        return await run(public_response, service.root, path, request.headers, immutable)

    async def base_redirect(request):
        return RedirectResponse(prefix + "/", status_code=308)

    async def missing(request):
        if request.method == "POST":
            same_origin(request, service, request.scope["path"][len(prefix):])
        raise APIError(405 if request.method == "HEAD" else 404, "接口不存在")

    async def preflight(request):
        path = request.scope["path"][len(prefix):]
        origin = public_origin(request, path)
        if (not origin or request.headers.getlist("access-control-request-method") not in (["GET"], ["POST"])
                or [v.lower() for v in request.headers.getlist("access-control-request-headers")] not in (
                    [], [""], ["content-type"])):
            raise APIError(403, "不允许跨站请求")
        return Response(status_code=204, headers={"Access-Control-Allow-Origin": origin, "Vary": "Origin",
                        "Access-Control-Allow-Methods": "GET, POST, OPTIONS", "Access-Control-Allow-Headers": "Content-Type"})

    async def handle_error(_request, exc):
        if isinstance(exc, APIError):
            return api_error(exc)
        if isinstance(exc, HTTPException):
            return api_error(APIError(exc.status_code, "页面不存在" if exc.status_code == 404 else "请求方法不支持"))
        if isinstance(exc, sqlite3.IntegrityError):
            return api_error(APIError(409, "队号或记录已存在，请刷新后核对"))
        return api_error(APIError(500, "服务暂时不可用，请稍后重试"))

    @asynccontextmanager
    async def lifespan(app):
        service.repository_backups.start()
        try:
            service.database_maintenance.start()
            try:
                yield
            finally:
                service.database_maintenance.stop()
        finally:
            service.repository_backups.stop()
            service.repositories.close()
            await service.keys.opener.close()

    routes = [endpoint_route(endpoint) for endpoint in endpoints()]
    routes.extend([
        Route(prefix + "/api/team/uploads/{kind}", upload, methods=["POST"]),
        Route(prefix + "/{path:path}", preflight, methods=["OPTIONS"]),
        Route(prefix + "/api/{path:path}", missing, methods=["GET", "POST"]),
        Route(prefix + "/materials/{path:path}", material, methods=["GET"]),
        Route(prefix + "/{path:path}", static, methods=["GET"]),
    ])
    if prefix:
        routes.append(Route(prefix, base_redirect, methods=["GET"]))
    app = Starlette(routes=routes, lifespan=lifespan,
                    middleware=[Middleware(ObservabilityMiddleware, service=service, role="platform"),
                                Middleware(HTTPPolicy, service=service)],
                    exception_handlers={APIError: handle_error, HTTPException: handle_error,
                                        sqlite3.IntegrityError: handle_error, Exception: handle_error})
    app.router.redirect_slashes = False
    return app
