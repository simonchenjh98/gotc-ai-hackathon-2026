"""Async ownership, immutable upstream identity and exactly-once accounting."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import http.client
from pathlib import Path
import sqlite3
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from errors import APIError
from gateway_tasks import AsyncTasks, TASK_ROUTE
import keys
import test_keys


class Service:
    base_path = "/gotc"

    def __init__(self, path):
        self.db_path = path

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.db_path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()


class AsyncTaskTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = Service(str(Path(self.temp.name) / "tasks.sqlite"))
        self.expiry = None
        self.gateway = SimpleNamespace(service=self.service, settings=lambda db: {"expiresAt": self.expiry})
        self.gateway.config = {"openai": {"url": "https://new.invalid/v1", "key": "rotated-secret"}}
        self.owner = {"id": "key-a", "team_id": "team-a"}
        self.other = {"id": "key-b", "team_id": "team-b"}
        self.sibling = {"id": "key-a2", "team_id": "team-a"}
        self.upstream = {"url": "https://original.invalid/v1", "key": "original-credential"}
        self.requests = []
        self.current = {"task_id": "upstream-task-1", "status": "waiting", "urls": {"get": "https://evil.invalid/steal"}}
        self.response_headers = {}
        self.pricing_calls = []
        self.tasks = AsyncTasks(self.gateway, pricing=self.price, transport=self.transport)
        with self.service.db() as db:
            db.executescript("""
              CREATE TABLE teams(id TEXT PRIMARY KEY);
              CREATE TABLE accounts(id TEXT PRIMARY KEY,active INTEGER);
              CREATE TABLE team_accounts(team_id TEXT,account_id TEXT);
              CREATE TABLE team_details(team_id TEXT,status TEXT);
              INSERT INTO teams VALUES ('team-a'),('team-b');
              INSERT INTO accounts VALUES ('account-a',1),('account-b',1);
              INSERT INTO team_accounts VALUES ('team-a','account-a'),('team-b','account-b');
            """)
            db.executescript(keys.SCHEMA)
            columns = {r["name"] for r in db.execute("PRAGMA table_info(api_usage)")}
            for name, definition in {"currency": "TEXT NOT NULL DEFAULT 'USD'",
                                     "billing_status": "TEXT NOT NULL DEFAULT 'settled'",
                                     "billing_details": "TEXT NOT NULL DEFAULT '{}'"}.items():
                if name not in columns:
                    db.execute("ALTER TABLE api_usage ADD COLUMN " + name + " " + definition)
            db.execute("INSERT INTO team_api VALUES ('team-a',1,0),('team-b',1,0)")
            for key in (self.owner, self.other, self.sibling):
                db.execute("INSERT INTO api_keys(id,team_id,name,key_hash,masked,created_at) VALUES (?,?,?,?,?,0)",
                           (key["id"], key["team_id"], key["id"], key["id"], "masked"))
            self.tasks.initialize(db)

    def price(self, task, payload, headers):
        self.pricing_calls.append(task)
        cost = payload.get("price")
        return {"cost_micro": None if cost is None else int(Decimal(str(cost)) * 1000000),
                "usage": [0, 0, 0, 0, 0], "currency": "CNY", "billing": {"source": "provider", "price": cost}}

    def transport(self, task, method, suffix):
        self.requests.append((task["upstream_url"], task["upstream_key"], method, suffix))
        if suffix.endswith("/cancel"):
            self.current = dict(self.current, status="cancelled", price=0)
            return 200, {}, {}
        if suffix.endswith("/get"):
            return 200, dict(self.current.get("output", {})), dict(self.response_headers)
        return 200, dict(self.current), dict(self.response_headers)

    def register(self, **overrides):
        call = {"key": self.owner, "model": "video-model", "path": "/v1/async/videos/generations",
                "request_id": "request-1", "pricing_snapshot": {"unit": "second", "price": "0.5"}}
        call.update(overrides)
        public = self.tasks.register(call, self.upstream, dict(self.current),
                                     request={"duration": 5, "prompt": "do not retain this prompt", "image": "secret-image-data"})
        return call, public

    def succeed(self, price="1.25"):
        self.current = dict(self.current, status="success", price=price, currency="CNY",
                            output={"url": "https://media.invalid/result.mp4"})
        self.response_headers = {"Inference-Cost": str(price), "X-Request-Id": "remote-request", "Authorization": "unsafe"}

    def rows(self, table):
        with self.service.db() as db:
            return [dict(row) for row in db.execute("SELECT * FROM " + table)]

    def route(self, public, action=""):
        return "/v1/task/" + public["task_id"] + action

    def assert_error(self, status, callback):
        with self.assertRaises(APIError) as caught:
            callback()
        self.assertEqual(caught.exception.status, status)

    def due(self):
        with self.service.db() as db:
            db.execute("UPDATE api_async_tasks SET next_poll_at=0 WHERE settled=0")

    def test_registration_persists_local_id_and_one_pending_usage_before_reply(self):
        call, public = self.register()
        self.assertRegex(self.route(public), TASK_ROUTE)
        self.assertNotIn("upstream-task-1", json.dumps(public))
        self.assertNotIn("evil.invalid", json.dumps(public))
        self.assertTrue(call["recorded"])
        self.assertEqual(call["task_id"], public["task_id"])
        row = self.rows("api_usage")[0]
        self.assertEqual((row["status"], row["cost_micro"], row["incomplete"], row["currency"], row["billing_status"]),
                         (202, 0, 1, "CNY", "pending"))
        stored = self.rows("api_async_tasks")[0]
        self.assertEqual(json.loads(stored["request_snapshot"]), {"duration": 5})
        self.assertNotIn("retain this prompt", stored["request_snapshot"])

    def test_duplicate_registration_is_idempotent_and_cross_owner_collision_refused(self):
        first_call, public = self.register()
        second_call, again = self.register()
        self.assertEqual(public["task_id"], again["task_id"])
        self.assertEqual(len(self.rows("api_usage")), 1)
        self.assert_error(409, lambda: self.register(key=self.other, request_id="other-request"))
        self.assertEqual(len(self.rows("api_async_tasks")), 1)

    def test_cross_team_and_same_team_different_key_cannot_access_or_cancel(self):
        _, public = self.register()
        for key in (self.other, self.sibling):
            for method, action in (("GET", ""), ("GET", "/status"), ("GET", "/get"), ("POST", "/cancel")):
                self.assert_error(404, lambda: self.tasks.request(key, method, self.route(public, action)))
        self.assertEqual(self.requests, [])

    def test_remote_identifiers_list_query_and_unknown_routes_are_not_passed_through(self):
        _, public = self.register()
        for path in ("/v1/tasks", "/v1/tasks/available-quota", "/v1/task/upstream-task-1",
                     self.route(public, "/get?url=https://evil.invalid"), self.route(public, "/delete")):
            self.assert_error(404, lambda: self.tasks.request(self.owner, "GET", path))
        self.assert_error(405, lambda: self.tasks.request(self.owner, "GET", self.route(public, "/cancel")))
        self.assertEqual(self.requests, [])

    def test_restart_and_config_rotation_keep_original_upstream_snapshot(self):
        _, public = self.register()
        self.succeed()
        restarted = AsyncTasks(self.gateway, pricing=self.price, transport=self.transport)
        status, output, headers = restarted.request(self.owner, "GET", self.route(public, "/get"))
        self.assertEqual(status, 200)
        self.assertEqual(output, self.current["output"])
        self.assertEqual(self.requests, [(self.upstream["url"], self.upstream["key"], "GET", "/task/upstream-task-1")])
        row = self.rows("api_usage")[0]
        self.assertEqual((row["cost_micro"], row["status"], row["incomplete"], row["currency"], row["billing_status"]),
                         (1250000, 200, 0, "CNY", "settled"))
        self.assertEqual(self.rows("api_async_tasks")[0]["upstream_key"], "")
        self.assertNotIn("authorization", headers)
        self.assertNotIn("upstream_key", self.pricing_calls[0])
        # Cached retrieval needs neither a live credential nor the current config.
        restarted.request(self.owner, "GET", self.route(public, "/get"))
        self.assertEqual(len(self.requests), 1)

    def test_parallel_polling_across_instances_settles_one_existing_usage(self):
        _, public = self.register()
        self.succeed()
        others = [AsyncTasks(self.gateway, pricing=self.price, transport=self.transport) for _ in range(4)]
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda i: others[i % 4].request(self.owner, "GET", self.route(public, "/status")), range(24)))
        self.assertTrue(all(value[1] == "success" for value in results))
        self.assertEqual(len(self.rows("api_usage")), 1)
        self.assertEqual(self.rows("api_usage")[0]["cost_micro"], 1250000)
        self.assertEqual(self.rows("api_async_tasks")[0]["settled"], 1)

    def test_revocation_after_network_response_hides_result_but_bills_incurred_task(self):
        _, public = self.register()
        self.succeed()
        original = self.transport

        def revoke(task, method, suffix):
            result = original(task, method, suffix)
            with self.service.db() as db:
                db.execute("UPDATE api_keys SET active=0 WHERE id=?", (self.owner["id"],))
            return result

        self.tasks.transport = revoke
        self.assert_error(404, lambda: self.tasks.request(self.owner, "GET", self.route(public, "/get")))
        self.assertEqual(self.rows("api_usage")[0]["cost_micro"], 1250000)

    def test_revoked_team_key_and_expiry_are_rechecked(self):
        _, public = self.register()
        cases = [("UPDATE api_keys SET active=0 WHERE id='key-a'", "UPDATE api_keys SET active=1", 404),
                 ("UPDATE team_api SET active=0 WHERE team_id='team-a'", "UPDATE team_api SET active=1", 404),
                 ("UPDATE accounts SET active=0 WHERE id='account-a'", "UPDATE accounts SET active=1", 404),
                 ("INSERT INTO team_details VALUES ('team-a','withdrawn')", "DELETE FROM team_details", 404)]
        for disable, restore, code in cases:
            with self.service.db() as db:
                db.execute(disable)
            self.assert_error(code, lambda: self.tasks.request(self.owner, "GET", self.route(public)))
            with self.service.db() as db:
                db.execute(restore)
        self.expiry = 0
        self.assert_error(403, lambda: self.tasks.request(self.owner, "GET", self.route(public)))
        self.assertEqual(self.requests, [])

    def test_background_recovery_bills_after_revocation_without_returning_data(self):
        self.register()
        self.succeed()
        with self.service.db() as db:
            db.execute("UPDATE api_keys SET active=0")
        self.due()
        result = AsyncTasks(self.gateway, pricing=self.price, transport=self.transport).recover_once()
        self.assertEqual(result, {"checked": 1, "settled": 1, "failed": 0})
        self.assertEqual(self.rows("api_usage")[0]["cost_micro"], 1250000)

    def test_missing_price_is_pending_and_result_is_available_then_reconciled(self):
        _, public = self.register()
        self.succeed(price=None)
        output = self.tasks.request(self.owner, "GET", self.route(public, "/get"))[1]
        self.assertEqual(output, self.current["output"])
        self.assertEqual(self.rows("api_async_tasks")[0]["settled"], 0)
        self.assertEqual(self.rows("api_usage")[0]["billing_status"], "pending")
        self.succeed(price="2")
        self.due()
        self.assertEqual(self.tasks.recover_once()["settled"], 1)
        self.assertEqual(self.rows("api_usage")[0]["cost_micro"], 2000000)

    def test_official_result_endpoint_cost_and_currency_reach_real_pricing_hook(self):
        _, public = self.register()
        self.succeed(price=None)
        original = self.transport

        def result_cost(task, method, suffix):
            status, payload, _ = original(task, method, suffix)
            return status, payload, ({"Inference-Cost": "2.5", "Inference-Currency": "CNY"} if suffix.endswith("/get") else {})

        self.tasks.transport = result_cost
        self.tasks.pricing = lambda t, p, h: keys.KeyService.task_price(self.gateway, t, p, h)
        self.tasks.request(self.owner, "GET", self.route(public, "/get"))
        self.assertEqual(self.rows("api_usage")[0]["cost_micro"], 2500000)
        self.assertEqual([request[3] for request in self.requests], ["/task/upstream-task-1", "/task/upstream-task-1/get"])

    def test_private_text_quantities_and_submission_time_survive_restart(self):
        call = {"key": self.owner, "model": "speech-model", "path": "/v1/async/audio/speech", "requested_at": 1800000000.25}
        public = self.tasks.register(call, self.upstream, self.current,
                                     request={"input": "你好abc", "_billing_text_characters": 999999,
                                              "file": {"filename": "private.wav", "duration": 2.5, "content_type": "audio/wav"}})
        task = AsyncTasks(self.gateway)._get(public["task_id"])
        self.assertEqual(task["requested_at"], 1800000000.25)
        self.assertEqual(task["quantities"], {"characters": 5, "items": 1})
        self.assertNotIn("你好abc", json.dumps(task))
        self.assertNotIn("filename", task["request"]["file"])

    def test_remote_task_id_cannot_inject_upstream_path(self):
        for remote_id in ("..", ".", "../tasks", "https://evil.invalid", "%2Ftasks", "a\\b"):
            self.current["task_id"] = remote_id
            self.assert_error(502, self.register)
        self.assertEqual(self.rows("api_usage"), [])

    def test_cancel_is_owner_scoped_not_retried_and_terminal_status_is_cached(self):
        _, public = self.register()
        result = self.tasks.request(self.owner, "POST", self.route(public, "/cancel"))[1]
        self.assertEqual(result["status"], "cancelled")
        self.tasks.request(self.owner, "POST", self.route(public, "/cancel"))
        self.assertEqual([request[2] for request in self.requests], ["POST", "GET"])
        self.assertEqual(self.rows("api_usage")[0]["status"], 499)
        self.assert_error(409, lambda: self.tasks.request(self.owner, "GET", self.route(public, "/get")))

    def test_mismatched_upstream_task_identity_does_not_update_or_bill(self):
        _, public = self.register()
        self.succeed()
        self.current["task_id"] = "other-upstream-task"
        self.assert_error(502, lambda: self.tasks.request(self.owner, "GET", self.route(public)))
        self.assertEqual(self.rows("api_async_tasks")[0]["state"], "waiting")
        self.assertEqual(self.rows("api_usage")[0]["cost_micro"], 0)

    def test_failed_task_errors_redacted_before_terminal_credential_is_erased(self):
        self.current.update(status="failure", price=0, error={"message": "bad " + self.upstream["key"]})
        _, public = self.register()
        self.assertNotIn(self.upstream["key"], json.dumps(public))
        self.assertEqual(self.rows("api_async_tasks")[0]["upstream_key"], "")
        self.assertNotIn(self.upstream["key"], self.rows("api_async_tasks")[0]["payload"])
        self.assertEqual(self.rows("api_usage")[0]["status"], 502)

    def test_registration_commit_failure_exposes_no_local_identity_and_retries_without_duplicate(self):
        original_db = self.service.db
        failed = False

        @contextmanager
        def fail_commit():
            nonlocal failed
            with original_db() as db:
                yield db
                if not failed and db.execute("SELECT COUNT(*) FROM api_async_tasks").fetchone()[0]:
                    failed = True
                    raise sqlite3.OperationalError("injected commit failure")

        call = {"key": self.owner, "model": "video-model", "path": "/v1/async/videos/generations", "request_id": "retry"}
        with patch.object(self.service, "db", fail_commit):
            with self.assertRaises(sqlite3.OperationalError):
                self.tasks.register(call, self.upstream, self.current)
        self.assertNotIn("recorded", call)
        self.assertNotIn("task_id", call)
        self.assertEqual(self.rows("api_usage"), [])
        self.tasks.register(call, self.upstream, self.current)
        self.assertEqual(len(self.rows("api_usage")), 1)

    def test_settlement_commit_failure_rolls_back_usage_and_recovery_settles_once(self):
        _, public = self.register()
        self.succeed()
        original_db = self.service.db
        failed = False

        @contextmanager
        def fail_commit():
            nonlocal failed
            with original_db() as db:
                yield db
                settled = db.execute("SELECT settled FROM api_async_tasks").fetchone()[0]
                if settled and not failed:
                    failed = True
                    raise sqlite3.OperationalError("injected commit failure")

        with patch.object(self.service, "db", fail_commit):
            with self.assertRaises(sqlite3.OperationalError):
                self.tasks.request(self.owner, "GET", self.route(public))
        self.assertEqual(self.rows("api_async_tasks")[0]["settled"], 0)
        self.assertEqual(self.rows("api_usage")[0]["cost_micro"], 0)
        self.due()
        self.tasks.recover_once()
        self.assertEqual(len(self.rows("api_usage")), 1)
        self.assertEqual(self.rows("api_usage")[0]["cost_micro"], 1250000)

    def test_registration_retries_local_write_lock_without_resubmitting_upstream(self):
        original_db = self.service.db
        failed = False

        @contextmanager
        def locked_commit():
            nonlocal failed
            with original_db() as db:
                yield db
                if not failed and db.execute("SELECT COUNT(*) FROM api_async_tasks").fetchone()[0]:
                    failed = True
                    raise sqlite3.OperationalError("database is locked")

        with patch.object(self.service, "db", locked_commit):
            call, public = self.register()
        self.assertTrue(call["recorded"])
        self.assertEqual(len(self.rows("api_async_tasks")), 1)
        self.assertEqual(len(self.rows("api_usage")), 1)
        self.assertEqual(self.requests, [])

    def test_transient_error_preserves_pending_and_never_resubmits_inference(self):
        self.register()
        self.due()
        self.tasks.transport = lambda *_: (_ for _ in ()).throw(APIError(502, "upstream secret " + self.upstream["key"]))
        self.assertEqual(self.tasks.recover_once()["failed"], 1)
        row = self.rows("api_async_tasks")[0]
        self.assertEqual(row["last_error"], "reconciliation_pending")
        self.assertEqual(row["settled"], 0)
        self.assertEqual(self.requests, [])

    def test_no_redirect_or_provider_error_body_exposes_snapshot_credential(self):
        received = []

        class Redirector(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                received.append(self.path)
                self.send_response(302)
                self.send_header("Location", "/stolen")
                self.end_headers()

        server = ThreadingHTTPServer(("127.0.0.1", 0), Redirector)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            snapshot = {"upstream_url": "http://127.0.0.1:" + str(server.server_port), "upstream_key": self.upstream["key"]}
            original_opener = keys.make_opener
            with patch.object(keys, "make_opener", side_effect=lambda: original_opener(proxies={})), self.assertRaises(APIError) as caught:
                self.tasks._transport(snapshot, "GET", "/task/remote")
            self.assertEqual(received, ["/task/remote"])
            self.assertNotIn(self.upstream["key"], caught.exception.message)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=1)


class AsyncTaskHTTPTests(unittest.TestCase):
    setUp = test_keys.GatewayTests.setUp
    tearDown = test_keys.GatewayTests.tearDown
    team = test_keys.GatewayTests.team
    team_client = test_keys.GatewayTests.team_client
    enabled_key = test_keys.GatewayTests.enabled_key
    call = test_keys.GatewayTests.call
    assertReleased = test_keys.GatewayTests.assertReleased

    def make_task(self):
        team_id, _, token = self.enabled_key()
        with self.service.db() as db:
            key = dict(db.execute("SELECT id,team_id FROM api_keys WHERE team_id=?", (team_id,)).fetchone())
        initial = {"task_id": "remote-http-task", "status": "waiting"}
        call = {"key": key, "model": "video-model", "path": "/v1/async/videos/generations", "pricing_snapshot": {}}
        public = self.service.keys.tasks.register(call, self.upstreams["openai"], initial)
        return team_id, token, public

    def test_http_result_and_string_status_use_json_and_survive_config_removal(self):
        team_id, token, public = self.make_task()
        self.service.keys.tasks.transport = lambda *_: (200, {"task_id": "remote-http-task", "status": "success",
                                                              "price": 1.25, "currency": "CNY", "output": {"audio": "https://example.invalid/a.wav"}}, {})
        self.assertEqual(self.admin.post("/api/admin/keys/upstreams", {"upstreams": {}})[0], 200)
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        try:
            connection.request("GET", public["urls"]["get"], headers={"Authorization": "Bearer " + token})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertTrue(response.headers["Content-Type"].startswith("application/json"))
            self.assertEqual(json.loads(response.read()), {"audio": "https://example.invalid/a.wav"})
        finally:
            connection.close()
        path = "/v1/task/" + public["task_id"]
        status, raw = self.call(path + "/status", None, token, method="GET")
        self.assertEqual((status, json.loads(raw)), (200, "success"))
        with self.service.db() as db:
            rows = list(db.execute("SELECT * FROM api_usage"))
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["cost_micro"], rows[0]["currency"]), (1250000, "CNY"))
        self.assertReleased(team_id)

    def test_http_owner_is_authenticated_and_foreign_task_attempt_is_zero_cost(self):
        team_id, token, public = self.make_task()
        other_team = self.team(2, "other team")
        other_client = self.team_client(other_team)
        self.assertEqual(self.admin.post("/api/admin/keys/provision", {"teamId": other_team})[0], 200)
        status, created, _ = other_client.post("/api/team/keys", {"name": "other key"})
        self.assertEqual(status, 200)
        other_token = created["key"]
        path = "/v1/task/" + public["task_id"]
        self.assertEqual(self.call(path, None, method="GET")[0], 401)
        self.assertEqual(self.call(path, None, other_token, method="GET")[0], 404)
        self.assertEqual(self.call(path + "/cancel", {}, other_token)[0], 404)
        with self.service.db() as db:
            rows = list(db.execute("SELECT * FROM api_usage"))
        self.assertEqual(len(rows), 3)  # initial accepted task plus two denied authenticated attempts
        self.assertTrue(all(row["cost_micro"] == 0 for row in rows))
        self.assertReleased(team_id)


if __name__ == "__main__":
    unittest.main()
