"""Run one production ASGI service on its Unix socket or both apps on local loopback."""
import argparse
import ipaddress
import os
from pathlib import Path

import uvicorn

from .http.server import EventHTTPServer
from .listener import unix_listener
from .service import EventService
from .settings import public_origin


def main(argv=()):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service", choices=("platform", "gateway"))
    parser.add_argument("--uds", help="Absolute path under the service's private runtime directory")
    args = parser.parse_args(argv)
    os.umask(0o077)
    if bool(args.service) != bool(args.uds):
        parser.error("--service and --uds must be supplied together")
    gateway_socket = None
    if args.service == "platform":
        gateway_socket = os.environ.get("GOTC_GATEWAY_SOCKET", "/run/gotc-gateway/http.sock")
        if not Path(gateway_socket).is_absolute() or ".." in Path(gateway_socket).parts or "\0" in gateway_socket:
            parser.error("GOTC_GATEWAY_SOCKET must be an absolute Unix socket path")
    if args.service:
        origin = public_origin(production=True)
    else:
        host = os.environ.get("GOTC_HOST", "127.0.0.1")
        try:
            if not ipaddress.ip_address(host).is_loopback:
                raise ValueError
        except ValueError:
            raise SystemExit("GOTC_HOST must be a loopback IP address") from None
        origin = public_origin()
    service = EventService(origin=origin)
    try:
        if gateway_socket is not None:
            service.keys.connect_runtime_socket(gateway_socket)
        if args.service:
            from .http.app import create_platform_app
            from .gateway.asgi import create_gateway_app

            factory = create_platform_app if args.service == "platform" else create_gateway_app
            app = factory(service, trusted_proxy_socket=args.uds)
            config = uvicorn.Config(
                app, uds=args.uds, http="h11", ws="websockets-sansio" if args.service == "gateway" else "none",
                ws_max_size=1024 * 1024, ws_per_message_deflate=False, ws_max_queue=4,
                proxy_headers=False, access_log=False, server_header=False, log_level="warning",
                timeout_graceful_shutdown=20, lifespan="on",
            )
            with unix_listener(args.uds) as sock:
                server = uvicorn.Server(config)
                server.run(sockets=[sock])
                if not server.started:
                    raise SystemExit("ASGI startup did not complete")
        else:
            server = EventHTTPServer((host, int(os.environ.get("GOTC_PORT", "5274"))), service)
            try:
                print(f"GOTC service ready on {host}:{server.server_port}{service.base_path}/", flush=True)
                server.serve_forever()
            finally:
                server.server_close()
    except KeyboardInterrupt:
        pass
    finally:
        if service.keys.runtime_client is not None:
            service.keys.runtime_client.close()
        service.connections.close()
