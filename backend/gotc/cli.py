"""Bind the loopback HTTP server and manage event background workers."""
from __future__ import annotations

from contextlib import ExitStack
import ipaddress
import os
import signal

from .http.server import EventHTTPServer
from .service import EventService


def main():
    # Private files must stay private before SQLite creates its journal and WAL.
    os.umask(0o077)
    host = os.environ.get("GOTC_HOST", "127.0.0.1")
    try:
        if not ipaddress.ip_address(host).is_loopback:
            raise ValueError("Service must bind to a loopback address behind Nginx")
    except ValueError as error:
        raise SystemExit("GOTC_HOST must be a loopback IP address") from error
    service = EventService()
    server = EventHTTPServer((host, int(os.environ.get("GOTC_PORT", "5274"))), service)

    def interrupted(_signum, _frame):
        raise KeyboardInterrupt()

    previous_handler = signal.signal(signal.SIGTERM, interrupted)
    with ExitStack() as cleanup:
        cleanup.callback(signal.signal, signal.SIGTERM, previous_handler)
        cleanup.callback(server.server_close)
        cleanup.callback(service.keys.tasks.stop)
        cleanup.callback(service.repository_backups.stop)
        try:
            service.repository_backups.start()
            service.keys.tasks.start()
            print(
                f"GOTC service ready on {host}:{server.server_port}{service.base_path}/ "
                "(access code is in private data directory)", flush=True,
            )
            server.serve_forever()
        except KeyboardInterrupt:
            pass
