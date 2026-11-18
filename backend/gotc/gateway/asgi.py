"""Dedicated ASGI application for model gateway HTTP and WebSocket traffic."""
import asyncio
from contextlib import asynccontextmanager, suppress

import anyio
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request, ClientDisconnect
from starlette.responses import JSONResponse, Response
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket

from ..errors import APIError
from ..http.policy import HTTPPolicy, api_error
from . import keys
from .asgi_context import GatewayContext
from .async_media import reconcile_tasks, serve_media, serve_task
from .async_text import ROUTES as TEXT_ROUTES, serve_text
from .gateway_cors import origin_allowed, preflight_allowed, response_headers
from .gateway_websocket import SPEECH_PATHS, serve_websocket
from .protocol_errors import ClientGone, GatewayError, error_body



async def models(ctx, path):
    gateway = ctx.gateway
    if ctx.request.method != "GET":
        raise GatewayError("not_found", "接口不存在")
    await ctx.sync(gateway.authenticate, ctx.handler)
    data = [{"id": model, "object": "model", "owned_by": name, "created": 0, "type": "model", "display_name": model,
             "created_at": "2024-01-01T00:00:00Z",
             **({"capabilities": gateway.catalog.model_info(model)} if gateway.is_gitee(item) else {})}
            for name, item in ctx.config.items() for model in item["models"]]
    if path == "/v1/models":
        value = {"object": "list", "data": data, "has_more": False,
                 "first_id": data[0]["id"] if data else None, "last_id": data[-1]["id"] if data else None}
    else:
        value = next((item for item in data if item["id"] == path[len("/v1/models/"):]), None)
        if value is None:
            raise GatewayError("not_found", "模型不在本活动开放列表中")
    await ctx.send(200, "application/json", JSONResponse(value).body)


async def missing(ctx, path):
    raise GatewayError("not_found", "接口不存在")


async def speech_upgrade(ctx, path):
    raise GatewayError("invalid", "需要 WebSocket 升级请求")


class GatewayEndpoint:
    def __init__(self, service, operation, *, gitee_only=False):
        self.service, self.operation, self.gitee_only = service, operation, gitee_only

    async def __call__(self, scope, receive, send):
        websocket = scope["type"] == "websocket"
        async def body_event():
            try:
                async with asyncio.timeout(15):
                    return await receive()
            except TimeoutError:
                raise APIError(408, "请求上传超时") from None
        request = WebSocket(scope, receive, send) if websocket else Request(scope, body_event)
        scope["gateway_send"] = send
        path = scope["path"][len(self.service.base_path):]
        ctx = GatewayContext(self.service.keys, request, path)
        gateway, headers = ctx.gateway, ctx.handler.headers

        async def immediate(response):
            if websocket:
                await request.send_denial_response(response)
            else:
                await response(scope, receive, send)

        if not origin_allowed(headers, self.service.origin):
            return await immediate(api_error(APIError(403, "此浏览器来源不允许调用中转 API", code="origin_not_allowed")))
        if not websocket and request.method == "OPTIONS":
            if not preflight_allowed(headers, self.service.origin):
                return await immediate(api_error(APIError(403, "中转 API 不允许此跨域预检请求", code="cors_preflight_not_allowed")))
            return await immediate(Response(status_code=204, headers=response_headers(headers, self.service.origin, preflight=True)))

        disconnected = False
        entered = False
        try:
            state = scope["app"].state
            if state.ingress >= keys.GLOBAL_RUNNING + keys.GLOBAL_QUEUE_CAP:
                raise GatewayError("rate_limit", "中转服务排队请求已满，请稍后重试", retry_after=2)
            state.ingress += 1
            entered = True
            if {"key", "api_key"} & set(request.query_params):
                raise GatewayError("invalid", "不支持在网址中携带 API 密钥，请改用 Authorization 或 x-api-key 请求头")
            ctx.config = await ctx.sync(gateway.refresh_config)
            if websocket:
                return await serve_websocket(request, ctx, path)
            if self.operation is not serve_task and (ctx.config is None or
                    (self.gitee_only and not gateway.is_gitee(ctx.config.get("openai", {})))):
                raise GatewayError("not_found", "接口不存在")
            return await self.operation(ctx, path)
        except asyncio.CancelledError:
            disconnected = True
            raise
        except (ClientGone, ClientDisconnect):
            disconnected = True
            if not ctx.call.get("status"):
                gateway.note_error(ctx.call, GatewayError("client_closed", "客户端已断开，请求未完成"))
        except Exception as exc:
            if isinstance(exc, APIError):
                error = exc
            else:
                ctx.call["charge_estimate"] = ctx.call["forwarded"]
                error = GatewayError("internal", "网关内部错误，请联系组织方")
            info = gateway.note_error(ctx.call, error)
            if not websocket:
                await ctx.record()
            if not ctx.call.get("headers_sent"):
                extra = {"X-Client-Request-ID": ctx.call["request_id"], **response_headers(headers, self.service.origin)}
                if info.get("retry_after"):
                    extra["Retry-After"] = str(info["retry_after"])
                await immediate(JSONResponse(error_body(path, info), status_code=error.status, headers=extra))
        finally:
            try:
                if not websocket:
                    await ctx.record()
                    await ctx.finish()
            finally:
                ctx.close()
                outcome = 499 if disconnected else ctx.call["status"]
                if outcome:
                    scope.setdefault("state", {})["gateway_outcome"] = outcome
                if entered:
                    state.ingress -= 1


def create_gateway_app(service, *, trusted_proxy_socket=None):
    gateway = service.keys
    base = service.base_path
    def route(path, operation, methods, *, gitee_only=False):
        return Route(base + path, GatewayEndpoint(service, operation, gitee_only=gitee_only), methods=methods)

    routes = [route("/v1/models", models, ["GET"]), route("/v1/models/{model:path}", models, ["GET"]),
              route("/v1/task/{task:path}", serve_task, ["GET", "POST"])]
    routes.extend(route(path, serve_text, ["POST"]) for path in TEXT_ROUTES)
    routes.extend(route(path, serve_media, ["POST"], gitee_only=True) for path in gateway.catalog.paths()
                  if path not in TEXT_ROUTES and not path.endswith("/ws"))
    routes.extend(route(path, speech_upgrade, ["GET"], gitee_only=True) for path in SPEECH_PATHS)
    # SDK-native errors and source policy also apply to unsupported paths, preflights and failed upgrades.
    routes.extend([route("/v1/{operation:path}", missing, ["GET", "HEAD", "POST", "OPTIONS"]),
                   WebSocketRoute(base + "/v1/{operation:path}", GatewayEndpoint(service, serve_websocket))])

    @asynccontextmanager
    async def lifespan(app):
        async def sync(fn, *args):
            return await anyio.to_thread.run_sync(fn, *args, limiter=app.state.gateway_limiter)
        provider_config = gateway.opener
        gateway.ledger.start()
        worker = asyncio.create_task(reconcile_tasks(gateway, provider_config.async_provider(), sync))
        try:
            yield
        finally:
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker
            await provider_config.close()
            if gateway.opener is not provider_config:
                await gateway.opener.close()
            await sync(gateway.ledger.stop)

    from ..observability import ObservabilityMiddleware
    app = Starlette(routes=routes,
                    middleware=[Middleware(ObservabilityMiddleware, service=service, role="gateway"),
                                Middleware(HTTPPolicy, service=service)], lifespan=lifespan)
    app.router.redirect_slashes = False
    app.state.ingress = 0
    app.state.trusted_proxy_socket = trusted_proxy_socket
    app.state.gateway_limiter = anyio.CapacityLimiter(4)
    return app
