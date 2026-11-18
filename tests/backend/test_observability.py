"""Health and observability contracts, exercised as ASGI requests in memory."""
from collections import defaultdict
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
from types import SimpleNamespace
import unittest

from starlette.responses import PlainTextResponse

from backend.gotc.observability import ObservabilityMiddleware, record_db_lock_wait


class ObservabilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        db_path = root / "event.sqlite3"
        with closing(sqlite3.connect(db_path)) as db:
            db.execute("CREATE TABLE settings(value TEXT)")
            db.execute("INSERT INTO settings VALUES ('ok')")
            db.commit()
        service = SimpleNamespace(data_dir=root, db_path=db_path, base_path="/gotc")
        self.service = service
        self.app = ObservabilityMiddleware(self.endpoint, service=service, role="platform")

    async def endpoint(self, scope, receive, send):
        self.request_id = scope["state"]["request_id"]
        if scope["path"].endswith("failed"):
            raise RuntimeError("PRIVATE_PROVIDER_MESSAGE")
        await PlainTextResponse("ok")(scope, receive, send)

    async def get(self, path, *, headers=(), **options):
        sent = []
        scope = {"type": "http", "method": "GET", "path": path, "client": ("127.0.0.1", 4567),
                 "headers": [(b"host", b"localhost"), *headers], "query_string": b"token=PRIVATE_QUERY",
                 "server": ("127.0.0.1", 8000), **options}
        async def send(message):
            sent.append(message)
        async def receive():
            return {"type": "http.request", "body": b"PRIVATE_BODY"}
        await self.app(scope, receive, send)
        return sent[0]["status"], dict(sent[0]["headers"]), sent[-1]["body"]

    async def test_requests_have_generated_ids_and_sanitized_structured_logs(self):
        with self.assertLogs("gotc.requests", level="INFO") as records:
            for ending, status in (("PRIVATE_FILENAME.html", 200), ("failed", 500)):
                code, headers, _ = await self.get("/gotc/materials/" + ending, headers=[
                    (b"x-request-id", b"PRIVATE_CALLER_ID"), (b"authorization", b"PRIVATE_CREDENTIAL")])
                self.assertEqual(code, status)
                self.assertEqual(headers[b"x-request-id"].decode(), self.request_id)
        self.assertNotIn("PRIVATE", str(records.output))
        entries = [json.loads(record.getMessage()) for record in records.records]
        self.assertEqual([r["group"] for r in entries], ["material", "material"])
        self.assertEqual([r["status"] for r in entries], [200, 500])
        self.assertNotEqual(entries[0]["request_id"], entries[1]["request_id"])

    async def test_probes_are_direct_and_readiness_requires_event_schema(self):
        self.assertEqual((await self.get("/livez"))[0], 200)
        self.assertEqual((await self.get("/readyz", client=None, server=("/run/gotc/platform.sock", None)))[0], 200)
        self.assertEqual((await self.get("/metrics", client=("192.0.2.3", 443)))[0], 404)
        self.assertEqual((await self.get("/metrics", headers=[(b"x-real-ip", b"127.0.0.1")]))[0], 404)
        record_db_lock_wait(0.01, failed=True)
        _, _, body = await self.get("/metrics")
        for name in (b"request_duration_seconds_bucket", b"db_write_wait_seconds_sum", b"disk_free_bytes",
                     b"gateway_queue_depth", b"active_streams", b"request_errors_total"):
            self.assertIn(name, body)
        self.service.db_path.unlink()
        self.assertEqual((await self.get("/readyz"))[0], 503)
        self.assertFalse(self.service.db_path.exists())

    async def test_runtime_snapshot_requires_the_direct_gateway_unix_listener(self):
        self.service.keys = SimpleNamespace(runtime_snapshot=lambda: {"inflight": {"anonymous-team": 1}})
        path = "/internal/gateway-runtime"
        self.assertEqual((await self.get(path, client=None, server=("/run/platform.sock", None)))[0], 404)
        self.app = ObservabilityMiddleware(self.endpoint, service=self.service, role="gateway")
        self.assertEqual((await self.get(path))[0], 404)
        self.assertEqual((await self.get(path, client=None, server=("/run/gateway.sock", None),
                                         headers=[(b"x-forwarded-proto", b"https")]))[0], 404)
        self.assertEqual((await self.get(path, client=None, server=("/run/gateway.sock", None)))[0], 200)

    async def test_active_sse_and_gateway_admission_use_actual_gauges(self):
        self.service.keys = SimpleNamespace(admission_snapshot=lambda: ({"one": 2}, {"one": 3}))
        async def stream(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/event-stream")]})
            metrics = (await self.get("/metrics"))[2]
            self.assertIn(b"gotc_active_streams 1", metrics)
            self.assertIn(b"gotc_gateway_queue_depth 3", metrics)
            await send({"type": "http.response.body", "body": b"data: ok\n\n", "more_body": False})
        self.app = ObservabilityMiddleware(stream, service=self.service, role="gateway")
        with self.assertLogs("gotc.requests"):
            self.assertEqual((await self.get("/gotc/v1/chat/completions"))[0], 200)
        self.assertIn(b"gotc_active_streams 0", (await self.get("/metrics"))[2])

    async def test_terminal_gateway_outcome_is_distinct_from_the_upgrade(self):
        async def receive():
            return {"type": "websocket.disconnect", "code": 1001}

        async def send(message):
            pass

        async def interrupted(scope, receive, send):
            if scope["path"].endswith("/accepted"):
                await send({"type": "websocket.accept"})
            await receive()
            scope["state"]["gateway_outcome"] = 499

        self.app = ObservabilityMiddleware(interrupted, service=self.service, role="platform")
        with self.assertLogs("gotc.requests") as captured:
            for path in ("/speech", "/speech/accepted"):
                await self.app({"type": "websocket", "path": path}, receive, send)
        results = [json.loads(record.getMessage()) for record in captured.records]
        self.assertEqual([(r["status"], r["wire_status"]) for r in results], [(499, None), (499, 101)])
        self.assertIn(b"gotc_request_errors_total 2", (await self.get("/metrics"))[2])
