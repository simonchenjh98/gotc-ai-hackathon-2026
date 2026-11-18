"""Process-local ASGI measurements and private health probes for each service."""
import asyncio
from collections import Counter
from contextlib import closing
import ipaddress
import json
import logging
import shutil
import sqlite3
import sys
from threading import Lock
from time import monotonic
import uuid
from urllib.parse import urlsplit

from starlette.responses import JSONResponse, PlainTextResponse


ACCESS = logging.getLogger("gotc.requests")
GATEWAY_RUNTIME_PATH = "/internal/gateway-runtime"
HEALTH_PATHS = frozenset({"/livez", "/readyz", "/metrics"})
BUCKETS = (0.01, 0.05, 0.1, 0.25, 0.5, 1, 5, 30, 120)
LOCK = Lock()
DB_WAITS = Counter()


def record_db_lock_wait(seconds, *, failed=False):
    """Record measured time attempting to acquire a database write transaction."""
    with LOCK:
        DB_WAITS["seconds"] += max(seconds, 0)
        DB_WAITS["attempts"] += 1
        DB_WAITS["failures"] += int(failed)


def _direct_local(scope):
    # Nginx supplies proxy headers on forwarded traffic. Never honor a client-supplied
    # assertion of locality. Uvicorn represents Unix peers with client=None.
    forbidden = {b"forwarded", b"x-real-ip", b"x-forwarded-for", b"x-forwarded-proto", b"x-forwarded-host"}
    if any(name.lower() in forbidden for name, _ in scope["headers"]):
        return False
    hosts = [value for name, value in scope["headers"] if name.lower() == b"host"]
    try:
        host = urlsplit("//" + hosts[0].decode("ascii")).hostname if len(hosts) == 1 else None
        if host != "localhost" and not ipaddress.ip_address(host).is_loopback:
            return False
    except (ValueError, UnicodeError, TypeError):
        return False
    client = scope.get("client")
    if client is None:
        server = scope.get("server")
        return bool(server and server[1] is None and str(server[0]).startswith("/"))
    try:
        return ipaddress.ip_address(client[0]).is_loopback
    except (ValueError, TypeError, IndexError):
        return False


def _group(scope, base_path):
    path = scope["path"]
    if base_path and path.startswith(base_path + "/"):
        path = path[len(base_path):]
    if scope["type"] == "websocket":
        return "websocket"
    for prefix, group in (("/v1/", "sdk"), ("/api/admin/", "admin"), ("/api/", "api"), ("/materials/", "material")):
        if path.startswith(prefix):
            return group
    return "public"


class ObservabilityMiddleware:
    def __init__(self, app, *, service, role):
        self.app, self.service, self.role = app, service, role
        self.counts = Counter()
        self.buckets = Counter()
        self.streams = 0
        self.lock = Lock()
        with LOCK:
            if not ACCESS.handlers:
                ACCESS.addHandler(logging.StreamHandler(sys.stdout))
                ACCESS.setLevel(logging.INFO)
                ACCESS.propagate = False

    def _ready(self):
        paths = [(self.service.db_path, "SELECT 1 FROM settings LIMIT 1")]
        if self.role == "gateway":
            paths.append((self.service.keys.ledger.content.path, "SELECT bytes FROM storage WHERE id=1"))
        try:
            for path, query in paths:
                with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.25)) as db:
                    if db.execute(query).fetchone() is None:
                        return False
            return True
        except (sqlite3.Error, OSError):
            return False

    def _metrics(self):
        with self.lock:
            counts, buckets, streams = self.counts.copy(), self.buckets.copy(), self.streams
        with LOCK:
            waits = DB_WAITS.copy()
        running = queued = 0
        if self.role == "gateway":
            active_teams, queued_teams = self.service.keys.admission_snapshot()
            running, queued = sum(active_teams.values()), sum(queued_teams.values())
        lines = [
            f"gotc_requests_total {counts['requests']}",
            f"gotc_request_errors_total {counts['errors']}",
            f"gotc_gateway_running {running}",
            f"gotc_gateway_queue_depth {queued}",
            f"gotc_active_streams {streams}",
            f"gotc_db_write_wait_seconds_sum {waits['seconds']:.6f}",
            f"gotc_db_write_wait_seconds_count {waits['attempts']}",
            f"gotc_db_lock_failures_total {waits['failures']}",
            f"gotc_disk_free_bytes {shutil.disk_usage(self.service.data_dir).free}",
            "# TYPE gotc_request_duration_seconds histogram",
        ]
        if self.role == 'platform':
            capacity = self.service.password_checks.capacity
            with capacity.lock:
                lines.extend([f"gotc_password_active {capacity.active}", f"gotc_password_waiting {len(capacity.queue)}",
                              f"gotc_password_peak_active {capacity.peak_active}",
                              f"gotc_password_peak_waiting {capacity.peak_waiting}"])
        for bound in BUCKETS:
            lines.append(f'gotc_request_duration_seconds_bucket{{le="{bound}"}} {buckets[bound]}')
        lines.extend([f'gotc_request_duration_seconds_bucket{{le="+Inf"}} {counts["requests"]}',
                      f'gotc_request_duration_seconds_sum {counts["seconds"]:.6f}',
                      f'gotc_request_duration_seconds_count {counts["requests"]}'])
        return "\n".join(lines) + "\n"

    async def __call__(self, scope, receive, send):
        kind = scope["type"]
        if kind not in {"http", "websocket"}:
            return await self.app(scope, receive, send)
        request_id = uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id
        path = scope["path"]
        runtime = path == GATEWAY_RUNTIME_PATH
        private = path in HEALTH_PATHS or runtime
        if kind == "http" and private:
            headers = {"Cache-Control": "no-store", "X-Request-ID": request_id}
            listener = scope.get("server")
            direct_gateway_socket = (self.role == "gateway" and scope.get("client") is None and listener
                                     and listener[1] is None and str(listener[0]).startswith("/"))
            if not _direct_local(scope) or (runtime and not direct_gateway_socket):
                response = JSONResponse({"error": "not found"}, status_code=404, headers=headers)
            elif scope["method"] != "GET":
                response = JSONResponse({"error": "method not allowed"}, status_code=405, headers=headers)
            elif runtime:
                response = JSONResponse(await asyncio.to_thread(self.service.keys.runtime_snapshot), headers=headers)
            elif path == "/livez":
                response = JSONResponse({"status": "live"}, headers=headers)
            elif path == "/readyz":
                ready = await asyncio.to_thread(self._ready)
                response = JSONResponse({"status": "ready" if ready else "unavailable"},
                                        status_code=200 if ready else 503, headers=headers)
            else:
                response = PlainTextResponse(await asyncio.to_thread(self._metrics), media_type="text/plain; version=0.0.4",
                                             headers=headers)
            return await response(scope, receive, send)

        started = monotonic()
        status, wire_status, stream, sent, completed = 500, None, False, False, False
        disconnected = False

        async def observed_receive():
            nonlocal disconnected
            message = await receive()
            if not completed and message["type"] in {"websocket.disconnect", "http.disconnect"}:
                disconnected = True
            return message

        async def report(message):
            nonlocal sent, status, stream, wire_status, completed
            event = message["type"]
            if event in {"http.response.start", "websocket.accept", "websocket.http.response.start"}:
                sent = True
                status = message.get("status", 101)
                fields = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"x-request-id"]
                fields.append((b"x-request-id", request_id.encode("ascii")))
                message = {**message, "headers": fields}
                if not stream and (event == "websocket.accept" or any(
                    k.lower() == b"content-type" and v.lower().startswith(b"text/event-stream") for k, v in fields
                )):
                    stream = True
                    with self.lock:
                        self.streams += 1
            elif event == "websocket.close":
                if not sent:
                    status = 403
                elif message.get("code", 1000) not in (1000, 1001, 1005):
                    status = 502
            await send(message)
            if event in {"http.response.start", "websocket.http.response.start", "websocket.accept"}:
                wire_status = status
            elif event == "websocket.close":
                completed = True
                if wire_status is None and not disconnected:
                    wire_status = 403
            elif event in {"http.response.body", "websocket.http.response.body"} and not message.get("more_body"):
                completed = True

        try:
            await self.app(scope, observed_receive, report)
        except asyncio.CancelledError:
            status = 499
            raise
        except Exception:
            # Never serialize a caught exception, its arguments or its traceback: providers
            # and request handlers can attach credentials, query strings or bodies to them.
            status = 500
            if kind == "http" and not sent:
                await JSONResponse({"error": "service unavailable"}, status_code=500,
                                   headers={"Cache-Control": "no-store"})(scope, receive, report)
            elif kind == "websocket":
                await send({"type": "websocket.close", "code": 1011})
        finally:
            outcome = scope["state"].get("gateway_outcome")
            if type(outcome) is int and 100 <= outcome <= 599:
                status = outcome
            elif disconnected and not completed:
                status = 499
            elapsed = monotonic() - started
            with self.lock:
                self.counts["requests"] += 1
                self.counts["errors"] += int(status >= 400)
                self.counts["seconds"] += elapsed
                for bound in BUCKETS:
                    self.buckets[bound] += int(elapsed <= bound)
                if stream:
                    self.streams -= 1
            method = scope.get("method", "WS")
            ACCESS.info(json.dumps({"event": "request", "service": self.role, "request_id": request_id,
                                    "group": _group(scope, self.service.base_path),
                                    "method": method if method in {"GET", "HEAD", "POST", "OPTIONS", "WS"} else "other",
                                    "status": status, "wire_status": wire_status,
                                    "duration_ms": round(elapsed * 1000, 2)}, separators=(",", ":")))
