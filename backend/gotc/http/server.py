"""Combined loopback listener for local development and socket-level contract tests."""
from __future__ import annotations

from contextlib import AsyncExitStack, asynccontextmanager
import ipaddress
import socket
import threading

from starlette.applications import Starlette
from starlette.routing import Mount, Route, WebSocketRoute
import uvicorn

from .app import create_platform_app


def combined_app(service):
    from ..gateway.asgi import create_gateway_app

    platform = create_platform_app(service)
    gateway = create_gateway_app(service)
    sdk_path = service.base_path + "/v1/{path:path}"

    @asynccontextmanager
    async def lifespan(app):
        try:
            async with AsyncExitStack() as stack:
                await stack.enter_async_context(platform.router.lifespan_context(platform))
                await stack.enter_async_context(gateway.router.lifespan_context(gateway))
                yield
        finally:
            service.connections.close()

    return Starlette(routes=[Route(sdk_path, gateway), WebSocketRoute(sdk_path, gateway), Mount("/", app=platform)],
                     lifespan=lifespan)


class EventHTTPServer:
    def __init__(self, address, service):
        host, port = address
        ip = ipaddress.ip_address(host)
        if not ip.is_loopback:
            raise ValueError("Development HTTP must use a loopback address")
        self.service = service
        config = uvicorn.Config(combined_app(service), host=host, port=port, http="h11", ws="websockets-sansio",
                                ws_max_size=1024 * 1024, ws_per_message_deflate=False, ws_max_queue=4,
                                proxy_headers=False, access_log=False, server_header=False, log_config=None,
                                log_level="warning", timeout_graceful_shutdown=5)
        self._server = uvicorn.Server(config)
        self._socket = socket.create_server((host, port), family=socket.AF_INET6 if ip.version == 6 else socket.AF_INET)
        self.server_port = self._socket.getsockname()[1]
        self._running = threading.Event()
        self._stopped = threading.Event()

    def serve_forever(self, poll_interval=None):
        self._running.set()
        try:
            if not self._server.should_exit:
                self._server.run(sockets=[self._socket])
        finally:
            self._stopped.set()

    def shutdown(self):
        self._server.should_exit = True
        if self._running.is_set():
            self._stopped.wait()

    def server_close(self):
        self.shutdown()
        self._socket.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.server_close()
