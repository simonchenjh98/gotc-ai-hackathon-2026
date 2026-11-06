"""Built-in AI gateway: team keys, usage metering against fake upstream providers."""
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import json
import shutil
import socket
import ssl
import subprocess
import tempfile
import urllib.request
from unittest import mock
import sqlite3
import threading
import unittest

import test_api
from app import EventHandler
test_api.EventHandler = EventHandler
from test_api import Client
import keys
from keys import load_config


class FakeUpstream(BaseHTTPRequestHandler):
    calls = []
    status = 200
    fail_first = 0  # answer 529 this many times before succeeding
    hang = None  # "before": no reply at all; "body": headers, then the body stalls
    usage = None  # overrides the non-streaming usage object
    reply = None  # (status, content type, raw bytes): answer exactly this

    def log_message(self, *_):
        pass

    def do_GET(self):  # upstream model list, for the admin "fetch models" button
        FakeUpstream.calls.append((self.path, dict(self.headers), None))
        payload = json.dumps({"data": [{"id": "model-b"}, {"id": "model-a"}, {"id": "model-a"}]}).encode()
        self.send_response(FakeUpstream.status if FakeUpstream.status != 200 else 200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeUpstream.calls.append((self.path, dict(self.headers), body))
        if FakeUpstream.hang:
            if FakeUpstream.hang == "body":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "1000")
                self.end_headers()
                self.wfile.write(b'{"id":')
                self.wfile.flush()
            if FakeUpstream.hang == "drop":  # request fully received, connection closed without any reply
                self.close_connection = True
                self.connection.shutdown(socket.SHUT_RDWR)
                return
            if FakeUpstream.hang == "reset":  # answer starts, then the connection drops mid-body
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "1000")
                self.end_headers()
                self.wfile.write(b'{"id":')
                self.wfile.flush()
                self.connection.shutdown(2)
                return
            threading.Event().wait(2)  # longer than the shortened upstream timeout in the test
            return
        if FakeUpstream.reply:
            status, content_type, payload = FakeUpstream.reply
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            if content_type != "text/event-stream":
                self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if FakeUpstream.fail_first:
            FakeUpstream.fail_first -= 1
            self.send_response(529)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path.endswith("/count_tokens"):
            payload = b'{"input_tokens":42}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            return self.wfile.write(payload)
        if FakeUpstream.status != 200:
            payload = b'{"error":{"message":"upstream says no"}}'
            self.send_response(FakeUpstream.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            return self.wfile.write(payload)
        if body.get("stream"):
            if self.path.endswith("/messages"):
                events = [{"type": "message_start", "message": {"id": "msg_1", "type": "message", "role": "assistant", "content": [], "model": body["model"],
                                                                "stop_reason": None, "usage": {"input_tokens": 1000, "output_tokens": 1}}},
                          {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                          {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "hi"}},
                          {"type": "content_block_stop", "index": 0},
                          {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 500}},
                          {"type": "message_stop"}]
            else:
                events = [{"choices": [{"delta": {"content": "hi"}}], "usage": None},
                          {"choices": [], "usage": {"prompt_tokens": 1000, "completion_tokens": 500}}]
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for event in events:
                if "type" in event:  # Anthropic SSE names each event
                    self.wfile.write(b"event: " + event["type"].encode() + b"\n")
                self.wfile.write(b"data: " + json.dumps(event).encode() + b"\n\n")
            if not self.path.endswith("/messages"):
                self.wfile.write(b"data: [DONE]\n\n")
            return
        usage = FakeUpstream.usage or ({"input_tokens": 2000, "output_tokens": 1000} if self.path.endswith("/messages") else {"prompt_tokens": 2000, "completion_tokens": 1000})
        payload = json.dumps({"id": "x", "type": "message", "role": "assistant", "model": body["model"], "content": [{"type": "text", "text": "hi"}],
                              "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}], "usage": usage}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class TeamKeyClient(Client):
    """Model a key page retaining its login marker, independently of shared cookies."""
    login_id = None

    def request(self, method, path, payload=None, raw=None, origin=True, headers=None):
        headers = dict(headers or {})
        if (path == '/api/team/keys' or path.startswith('/api/team/keys/')) and self.login_id:
            headers.setdefault('X-GOTC-Login', self.login_id)
        result = super().request(method, path, payload, raw, origin, headers)
        if method == 'POST' and path == '/api/login' and result[0] == 200:
            self.login_id = result[1]['loginId']
        if method == 'POST' and path == '/api/logout' and result[0] == 200:
            self.login_id = None
        return result


class GatewayTests(unittest.TestCase):
    team = test_api.APITests.team  # reuse helpers without re-running APITests

    def setUp(self):
        FakeUpstream.calls, FakeUpstream.status, FakeUpstream.fail_first, FakeUpstream.usage, FakeUpstream.hang = [], 200, 0, None, None
        FakeUpstream.reply = None
        self.delays, keys.RETRY_DELAYS = keys.RETRY_DELAYS, (0, 0)
        self.fake = ThreadingHTTPServer(("127.0.0.1", 0), FakeUpstream)
        threading.Thread(target=self.fake.serve_forever, daemon=True).start()
        test_api.APITests.setUp(self)
        self.direct = keys.make_opener({})
        self.service.keys.opener = self.direct  # ignore this machine's proxy settings
        url = f"http://127.0.0.1:{self.fake.server_port}/v1"
        # Prices are USD per million tokens: [input, output(, cacheWrite, cacheRead)]. Configured through the admin panel API.
        self.upstreams = {"openai": {"url": url, "key": "sk-upstream-openai", "models": {"cheap": [1, 2]}},
                          "anthropic": {"url": url, "key": "sk-upstream-anthropic", "models": {"claude-x": [3, 15], "claude-small": [1, 5, 2, 0.5]},
                                        "aliases": {"claude-latest": "claude-x", "claude-haiku-*": "claude-small"}}}
        status, body, _ = self.admin.post("/api/admin/keys/upstreams", {"upstreams": self.upstreams})
        self.assertEqual(status, 200, body)

    def tearDown(self):
        keys.RETRY_DELAYS = self.delays
        test_api.APITests.tearDown(self)
        self.fake.shutdown()
        self.fake.server_close()

    def team_client(self, team_id):
        status, access, _ = self.admin.post("/api/admin/team-access", {"teamId": team_id, "action": "issue"})
        self.assertEqual(status, 200)
        client = TeamKeyClient(self.server)
        self.assertEqual(client.post("/api/login", {"role": "team", "code": access["code"]})[0], 200)
        return client

    def call(self, path, body, key=None, headers=None, method="POST"):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=20)
        payload = json.dumps(body).encode() if body is not None else None
        sent = {"Content-Type": "application/json", **({"Authorization": "Bearer " + key} if key else {}), **(headers or {})}
        conn.request(method, self.service.base_path + path, payload, sent)  # no Origin or cookie, like an SDK
        response = conn.getresponse()
        content = response.read()
        conn.close()
        return response.status, content

    def enabled_key(self):
        team_id = self.team()
        client = self.team_client(team_id)
        self.assertEqual(self.admin.post("/api/admin/keys/provision", {"teamId": team_id})[0], 200)
        status, created, _ = client.post("/api/team/keys", {"name": "alice"})
        self.assertEqual(status, 200, created)
        return team_id, client, created["key"]

    def test_dispense_once_show_once_and_hash_only(self):
        team_id = self.team()
        client = self.team_client(team_id)
        self.assertEqual(client.get("/api/team/keys")[1], {"enabled": False, "keys": []})
        self.assertEqual(client.post("/api/team/keys", {"name": "early"})[0], 409)
        self.assertEqual(self.admin.post("/api/admin/keys/settings", {"maxKeys": 2})[0], 200)
        self.assertNotIn("keys.maxKeys", self.public.get("/api/board")[1]["config"])
        self.assertEqual(self.admin.post("/api/admin/keys/provision", {"teamId": team_id})[0], 200)
        self.assertEqual(self.admin.post("/api/admin/keys/provision", {"teamId": team_id})[0], 409)

        status, created, _ = client.post("/api/team/keys", {"name": "alice"})
        self.assertEqual(status, 200)
        key = created["key"]
        self.assertTrue(key.startswith("gk-"))
        listing = client.get("/api/team/keys")[1]
        self.assertNotIn(key, json.dumps(listing))
        self.assertEqual(listing["usage"], {"used24h": 0, "usedTotal": 0})
        self.assertEqual(listing["models"], {"openai": ["cheap"], "anthropic": ["claude-x", "claude-small"]})
        with closing(sqlite3.connect(self.root / "private" / "event.sqlite3")) as db:
            self.assertFalse(any(key in str(row) for row in db.execute("SELECT * FROM api_keys")))
            stored = db.execute("SELECT key_hash FROM api_keys").fetchone()[0]
        import hashlib, os, stat
        self.assertNotEqual(stored, hashlib.sha256(key.encode()).hexdigest())  # peppered, not a bare hash
        pepper = self.root / "private" / "api-key-pepper"
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(pepper.stat().st_mode), 0o600)
        restarted = test_api.EventService(data_dir=self.root / "private", origin=self.service.origin, static_root=self.root / "public")
        self.assertEqual(restarted.keys.digest(key), stored)  # same secret after restart: keys keep working

        self.assertEqual(client.post("/api/team/keys", {"name": "bob"})[0], 200)
        self.assertEqual(client.post("/api/team/keys", {"name": "carol"})[0], 409)  # cap of 2
        self.assertEqual(client.post("/api/team/keys/delete", {"id": created["item"]["id"]})[0], 200)
        self.assertEqual(self.call("/v1/models", None, key, method="GET")[0], 401)  # deleted key stops at once
        self.assertEqual(self.admin.get("/api/team/keys")[0], 403)
        self.assertEqual(client.get("/api/admin/keys")[0], 403)

    def test_proxy_meters_both_upstreams_and_enforces_access(self):
        team_id, client, key = self.enabled_key()
        status, models = self.call("/v1/models", None, key, method="GET")
        self.assertEqual(status, 200)
        self.assertEqual({m["id"] for m in json.loads(models)["data"]}, {"cheap", "claude-x", "claude-small"})

        status, _ = self.call("/v1/chat/completions", {"model": "cheap", "messages": []}, key)
        self.assertEqual(status, 200)
        path, headers, _ = FakeUpstream.calls[-1]
        self.assertEqual(path, "/v1/chat/completions")
        self.assertEqual(headers["Authorization"], "Bearer sk-upstream-openai")  # team key never forwarded

        status, content = self.call("/v1/chat/completions", {"model": "cheap", "stream": True}, key)
        self.assertEqual(status, 200)
        self.assertIn(b"data:", content)
        self.assertEqual(FakeUpstream.calls[-1][2]["stream_options"], {"include_usage": True})
        streamed = self.admin.post("/api/admin/keys/logs", {})[1]["items"][0]
        self.assertEqual((streamed["status"], streamed["incomplete"], streamed["estimated"], streamed["errorType"]), (200, 0, 0, ""))

        status, content = self.call("/v1/messages", {"model": "claude-x", "stream": True}, headers={"x-api-key": key})
        self.assertEqual(status, 200)
        streamed = self.admin.post("/api/admin/keys/logs", {})[1]["items"][0]
        # A normal Anthropic stream is complete: message_stop seen, observed usage, no estimate, no error.
        self.assertEqual((streamed["status"], streamed["incomplete"], streamed["estimated"], streamed["errorType"]), (200, 0, 0, ""))
        self.assertEqual(FakeUpstream.calls[-1][1]["X-Api-Key"], "sk-upstream-anthropic")
        self.assertEqual(self.call("/v1/messages", {"model": "cheap"}, key)[0], 404)  # model of the other upstream

        # 2000*1 + 1000*2 = 4000 micro-USD; 1000*1 + 500*2 = 2000; 1000*3 + 500*15 = 10500
        usage = client.get("/api/team/keys")[1]["usage"]
        self.assertAlmostEqual(usage["usedTotal"], 0.0165)
        self.assertEqual(client.get("/api/team/keys")[1]["keys"][0]["requests"], 4)  # the rejected model call counts too

        self.assertEqual(self.admin.post("/api/admin/keys/settings", {"expiresAt": 1})[0], 200)
        self.assertEqual(self.call("/v1/chat/completions", {"model": "cheap"}, key)[0], 403)
        self.assertEqual(self.admin.post("/api/admin/keys/settings", {"expiresAt": None})[0], 200)
        self.assertEqual(self.admin.post("/api/admin/keys/revoke", {"teamId": team_id})[0], 200)
        self.assertEqual(self.call("/v1/chat/completions", {"model": "cheap"}, key)[0], 401)
        self.assertEqual(self.admin.post("/api/admin/keys/provision", {"teamId": team_id})[0], 200)  # keys come back
        self.assertEqual(self.call("/v1/chat/completions", {"model": "cheap"}, key)[0], 200)

    def test_upstream_errors_and_bad_requests(self):
        _, _, key = self.enabled_key()
        self.assertEqual(self.call("/v1/chat/completions", {"model": "cheap"})[0], 401)
        self.assertEqual(self.call("/v1/chat/completions", {"model": "cheap"}, "gk-wrong")[0], 401)
        self.assertEqual(self.call("/v1/chat/completions", {"model": "unknown"}, key)[0], 404)
        FakeUpstream.status = 401  # our upstream credential is wrong: hide it
        status, content = self.call("/v1/chat/completions", {"model": "cheap"}, key)
        self.assertEqual(status, 502)
        self.assertNotIn(b"upstream says no", content)
        FakeUpstream.status = 400  # caller's mistake: pass through for debugging
        status, content = self.call("/v1/chat/completions", {"model": "cheap"}, key)
        self.assertEqual(status, 400)
        self.assertIn(b"upstream says no", content)
        self.fake.shutdown()
        self.fake.server_close()
        self.assertEqual(self.call("/v1/chat/completions", {"model": "cheap"}, key)[0], 502)
        self.assertEqual(self.public.get("/api/board")[0], 200)

    def test_admin_history_content_and_stats(self):
        team_id, _, key = self.enabled_key()
        self.call("/v1/chat/completions", {"model": "cheap", "messages": [{"role": "user", "content": "SECRET_PROMPT"}]}, key,
                  headers={"User-Agent": "my-agent/1.0", "X-Trace": "t1"})
        self.call("/v1/messages", {"model": "claude-x", "stream": True}, headers={"x-api-key": key})
        self.call("/v1/chat/completions", {"model": "unknown"}, key)
        status, logs, _ = self.admin.post("/api/admin/keys/logs", {"teamId": team_id})
        self.assertEqual(status, 200, logs)
        self.assertEqual([i["status"] for i in logs["items"]], [404, 200, 200])
        first = logs["items"][2]
        self.assertEqual((first["userAgent"], first["input"], first["keyName"]), ("my-agent/1.0", 2000, "alice"))
        self.assertEqual(len(self.admin.post("/api/admin/keys/logs", {"errorsOnly": True})[1]["items"]), 1)
        self.assertEqual(self.admin.post("/api/admin/keys/logs", {"beforeId": first["id"]})[1]["items"], [])

        status, detail, _ = self.admin.post("/api/admin/keys/log", {"id": first["id"]})
        self.assertEqual(status, 200)
        self.assertIn("SECRET_PROMPT", detail["request"])
        self.assertIn('"prompt_tokens"', detail["response"])
        self.assertEqual(detail["headers"]["X-Trace"], "t1")
        self.assertNotIn(key, json.dumps(detail))  # credentials never stored
        self.assertIn("message_stop", self.admin.post("/api/admin/keys/log", {"id": logs["items"][1]["id"]})[1]["response"])

        status, stats, _ = self.admin.post("/api/admin/keys/stats", {"teamId": team_id, "days": 7})
        self.assertEqual(status, 200, stats)
        self.assertEqual((stats["totals"]["requests"], stats["totals"]["errors"]), (3, 1))
        self.assertAlmostEqual(stats["totals"]["cost"], 0.0145)  # 4000 + 10500 micro-USD
        self.assertEqual({m["model"] for m in stats["byModel"]}, {"cheap", "claude-x", "unknown"})
        self.assertEqual(stats["byKey"][0]["name"], "alice")
        self.assertIn("my-agent/1.0", [c["userAgent"] for c in stats["byClient"]])

        self.assertEqual(self.admin.post("/api/admin/keys/settings", {"logContent": False})[0], 200)
        self.call("/v1/chat/completions", {"model": "cheap"}, key)
        newest = self.admin.post("/api/admin/keys/logs", {})[1]["items"][0]
        self.assertFalse(newest["hasContent"])
        self.assertEqual(self.admin.post("/api/admin/keys/log", {"id": newest["id"]})[0], 404)
        _, team_client = self.team_client_for(team_id)
        self.assertEqual(team_client.post("/api/admin/keys/logs", {})[0], 403)

    def team_client_for(self, team_id):
        status, access, _ = self.admin.post("/api/admin/team-access", {"teamId": team_id, "action": "rotate"})
        client = TeamKeyClient(self.server)
        client.post("/api/login", {"role": "team", "code": access["code"]})
        return access, client

    def test_full_context_is_kept_compressed(self):
        team_id, _, key = self.enabled_key()
        history = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"第{i}轮：" + "上下文" * 4000} for i in range(40)]
        raw = {"model": "claude-latest", "system": "你是代码助手", "messages": history, "stream": True}
        self.assertEqual(self.call("/v1/messages", raw, key)[0], 200)
        item = self.admin.post("/api/admin/keys/logs", {})[1]["items"][0]
        sent = len(json.dumps(raw).encode())
        self.assertEqual(item["requestBytes"], sent)
        self.assertGreater(sent, 64 * 1024)
        detail = self.admin.post("/api/admin/keys/log", {"id": item["id"]})[1]
        self.assertEqual(json.loads(detail["request"]), raw)  # every message, and the model name the client sent
        self.assertIn("message_stop", detail["response"])
        self.assertFalse(detail["responseTruncated"])
        with closing(sqlite3.connect(self.root / "private" / "event.sqlite3")) as db:
            stored = db.execute("SELECT length(request) FROM api_logs WHERE usage_id=?", (item["id"],)).fetchone()[0]
        self.assertLess(stored, sent / 5)  # compressed on disk

    def test_client_headers_are_unrestricted_with_legacy_rules_after_restart(self):
        _, client, key = self.enabled_key()
        rules = [{"action": "allow", "header": "User-Agent", "contains": "claude-cli"},
                 {"action": "deny", "header": "User-Agent", "contains": "openclaw"}]
        with self.service.db() as db:
            db.execute("INSERT INTO settings VALUES (?,?)", ("keys.clientRules", json.dumps(rules)))
        restarted = test_api.EventService(data_dir=self.root / "private", origin=self.service.origin, static_root=self.root / "public")
        self.server.service = self.service = restarted
        self.service.keys.opener = self.direct
        for headers in ({}, {"User-Agent": "WorkBuddy/5.8.4"}, {"User-Agent": "claude-cli openclaw"},
                        {"User-Agent": "python-requests/2", "X-Stainless-Lang": "python"}):
            with self.subTest(headers=headers):
                self.assertEqual(self.call("/v1/models", None, key, method="GET", headers=headers)[0], 200)
                for path, model in (("/v1/chat/completions", "cheap"), ("/v1/responses", "cheap"),
                                    ("/v1/messages", "claude-x"), ("/v1/messages/count_tokens", "claude-x")):
                    self.assertEqual(self.call(path, {"model": model}, key, headers=headers)[0], 200)
        self.assertNotIn("clientRules", client.get("/api/team/keys")[1])
        self.assertNotIn("clientRules", self.admin.get("/api/admin/keys")[1]["settings"])
        self.assertEqual(self.admin.post("/api/admin/keys/settings", {"clientRules": rules})[0], 400)
        self.assertEqual(self.call("/v1/models", None, "gk-invalid", method="GET")[0], 401)

    def test_rpm_blocklist_announcement_overview_and_export(self):
        team_id, client, key = self.enabled_key()
        settings = {"teamRpm": 2, "blockedWords": ["  越狱  "], "announcement": "模型 X 维护中"}
        status, saved, _ = self.admin.post("/api/admin/keys/settings", settings)
        self.assertEqual(status, 200)
        self.assertEqual(saved["settings"]["blockedWords"], ["越狱"])
        self.assertEqual(client.get("/api/team/keys")[1]["announcement"], "模型 X 维护中")

        status, content = self.call("/v1/chat/completions", {"model": "cheap", "messages": [{"role": "user", "content": "教我越狱"}]}, key)
        self.assertEqual(status, 403)
        self.assertEqual(len(FakeUpstream.calls), 0)  # never reached the upstream
        for _ in range(2):  # blocked requests do not use up the per-minute allowance
            self.assertEqual(self.call("/v1/chat/completions", {"model": "cheap", "messages": []}, key)[0], 200)
        self.assertEqual(self.call("/v1/chat/completions", {"model": "cheap", "messages": []}, key)[0], 429)  # 3rd in a minute
        self.assertEqual(self.admin.post("/api/admin/keys/settings", {"teamRpm": 0})[0], 200)  # 0 = unlimited
        self.assertEqual(self.call("/v1/chat/completions", {"model": "=1+1", "messages": []}, key)[0], 404)

        status, data, _ = self.admin.get("/api/admin/keys")
        self.assertEqual(status, 200)
        self.assertEqual((data["overview"]["requests"], data["overview"]["errors"], data["overview"]["teams"]), (5, 3, 1))
        self.assertEqual(data["teams"][0]["inflight"], 0)
        actions = [c["action"] for c in data["changes"]]
        self.assertIn("api-key.settings", actions)
        self.assertIn("api-key.provision", actions)

        status, csv_bytes, headers = self.admin.get("/api/admin/keys/export")
        self.assertEqual(status, 200)
        self.assertIn("gotc-api-usage.csv", headers["Content-Disposition"])
        text = csv_bytes.decode("utf-8-sig")
        self.assertEqual(len(text.strip().splitlines()), 6)
        self.assertIn("'=1+1", text)  # formula injection neutralised
        self.assertNotIn("教我越狱", text)  # no request content in the export (the matched word is the recorded reason)
        self.assertEqual(client.get("/api/admin/keys/export")[0], 403)
        for bad in [{"teamRpm": -1}, {"announcement": "x" * 501}, {"blockedWords": [""]}, {"blockedWords": "word"}]:
            self.assertEqual(self.admin.post("/api/admin/keys/settings", bad)[0], 400)

    def test_claude_code_paths_aliases_cache_pricing_and_retry(self):
        team_id, client, key = self.enabled_key()
        # Claude Code: query string, Bearer token, alias and prefix alias, count_tokens.
        status, _ = self.call("/v1/messages?beta=true", {"model": "claude-latest", "max_tokens": 5}, key)
        self.assertEqual(status, 200)
        self.assertEqual((FakeUpstream.calls[-1][0], FakeUpstream.calls[-1][2]["model"]), ("/v1/messages", "claude-x"))
        self.assertEqual(self.call("/v1/messages", {"model": "claude-haiku-4-5-20251001"}, key)[0], 200)
        self.assertEqual(FakeUpstream.calls[-1][2]["model"], "claude-small")
        self.assertEqual(self.public.get("/api/board?x=1")[0], 400)  # query strings stay rejected elsewhere
        status, content = self.call("/v1/messages/count_tokens?beta=true", {"model": "claude-latest", "messages": []}, key)
        self.assertEqual((status, json.loads(content)), (200, {"input_tokens": 42}))
        self.assertEqual(FakeUpstream.calls[-1][0], "/v1/messages/count_tokens")

        # Cache tokens: explicit prices, default 1.25x / 0.1x, and OpenAI cached prompt tokens.
        FakeUpstream.usage = {"input_tokens": 100, "output_tokens": 10, "cache_creation_input_tokens": 1000, "cache_read_input_tokens": 10000}
        self.call("/v1/messages", {"model": "claude-small"}, key)  # 100*1 + 10*5 + 1000*2 + 10000*0.5 = 7150
        self.call("/v1/messages", {"model": "claude-x"}, key)  # 300 + 150 + 1000*3.75 + 10000*0.3 = 7200
        FakeUpstream.usage = {"prompt_tokens": 1000, "completion_tokens": 10, "prompt_tokens_details": {"cached_tokens": 800}}
        self.call("/v1/chat/completions", {"model": "cheap"}, key)  # 200*1 + 10*2 + 800*0.1 = 300
        items = self.admin.post("/api/admin/keys/logs", {})[1]["items"]
        self.assertEqual([round(i["cost"] * 1e6) for i in items[:3]], [300, 7200, 7150])
        self.assertEqual((items[0]["input"], items[0]["cacheRead"]), (200, 800))

        # Upstream overload is retried before anything reaches the client.
        FakeUpstream.usage, FakeUpstream.fail_first = None, 2
        before = len(FakeUpstream.calls)
        self.assertEqual(self.call("/v1/chat/completions", {"model": "cheap"}, key)[0], 200)
        self.assertEqual(len(FakeUpstream.calls) - before, 3)
        FakeUpstream.fail_first = 5  # still overloaded after all 5 attempts: 503 overloaded_error, as sub2api maps 529
        self.assertEqual(self.call("/v1/chat/completions", {"model": "cheap"}, key)[0], 503)

        # Teams see their own call history, without content.
        status, own, _ = client.get("/api/team/keys/logs")
        self.assertEqual(status, 200)
        self.assertEqual(len(own["items"]), 8)  # count_tokens is recorded at zero cost
        counted = next(i for i in own["items"] if i["path"] == "/v1/messages/count_tokens")
        self.assertEqual((counted["cost"], counted["model"]), (0, "claude-x"))
        self.assertNotIn("request", own["items"][0])

    def test_expiry_and_unmetered_tools_are_checked(self):
        _, _, key = self.enabled_key()
        # count_tokens obeys expiry like every other call.
        self.assertEqual(self.admin.post("/api/admin/keys/settings", {"expiresAt": 1})[0], 200)
        self.assertEqual(self.call("/v1/messages/count_tokens", {"model": "claude-x"}, key)[0], 403)
        self.assertEqual(self.admin.post("/api/admin/keys/settings", {"expiresAt": None})[0], 200)
        # Upstream-billed server tools and non-boolean stream flags are refused before reaching the upstream.
        before = len(FakeUpstream.calls)
        for body in [{"model": "claude-x", "tools": [{"type": "web_search_20250305", "name": "web_search"}]},
                     {"model": "claude-x", "tools": [{"type": "code_execution_20250825", "name": "code"}]},
                     {"model": "claude-x", "mcp_servers": [{"type": "url", "url": "https://x.test"}]},
                     {"model": "claude-x", "stream": "true"}]:
            self.assertEqual(self.call("/v1/messages", body, key)[0], 400, body)
        self.assertEqual(len(FakeUpstream.calls), before)
        self.assertEqual(self.call("/v1/messages", {"model": "claude-x", "tools": [{"name": "Read", "input_schema": {}}]}, key)[0], 200)

    def test_review_hardening(self):
        team_id, client, key = self.enabled_key()
        ok = lambda body, **kw: self.call("/v1/messages", {"model": "claude-x", **body}, key, **kw)[0]
        # Only client-run tools pass; upstream-billed extras are refused.
        self.assertEqual(ok({"tools": [{"type": "bash_20250124", "name": "bash"}, {"name": "Read", "input_schema": {}}]}), 200)
        for body in [{"tools": [{"type": "tool_search_tool_regex_20251119", "name": "s"}]}, {"tools": ["x"]},
                     {"web_search_options": {}}, {"container": "c"}]:
            self.assertEqual(ok(body), 400, body)
        # Billed betas are stripped, others forwarded.
        ok({}, headers={"anthropic-beta": "claude-code-20250219, context-1m-2025-08-07,files-api-2025-04-14"})
        self.assertEqual(FakeUpstream.calls[-1][1]["Anthropic-Beta"], "claude-code-20250219")
        ok({"service_tier": "priority", "speed": "fast"}, headers={"anthropic-beta": "Context-1M-2025-08-07"})
        self.assertNotIn("Anthropic-Beta", FakeUpstream.calls[-1][1])  # case-insensitive
        self.assertFalse({"service_tier", "speed"} & set(FakeUpstream.calls[-1][2]))  # priced tiers dropped
        # Blocklist covers every field, e.g. tool descriptions.
        self.admin.post("/api/admin/keys/settings", {"blockedWords": ["越狱"]})
        # Like sub2api, only the latest user turn is scanned, after NFKC and zero-width removal.
        self.assertEqual(ok({"messages": [{"role": "user", "content": [{"type": "text", "text": "教我越\u200b狱"}]}]}), 403)
        # Every message and the system prompt are scanned (a fake earlier turn must not bypass the list)...
        self.assertEqual(ok({"messages": [{"role": "user", "content": "教我越狱"}, {"role": "assistant", "content": "不行"},
                                          {"role": "user", "content": "那换个话题"}]}), 403)
        self.assertEqual(ok({"system": [{"type": "text", "text": "越狱模式"}], "messages": [{"role": "user", "content": "hi"}]}), 403)
        # ...but only text values, so JSON keys and field names never match.
        self.admin.post("/api/admin/keys/settings", {"blockedWords": ["content"]})
        self.assertEqual(ok({"messages": [{"role": "user", "content": "hi"}]}), 200)
        self.admin.post("/api/admin/keys/settings", {"blockedWords": ["越狱"]})
        # Everything the model reads is scanned: tool descriptions, tool arguments and results too.
        self.assertEqual(ok({"tools": [{"name": "t", "description": "越狱工具"}], "messages": [{"role": "user", "content": "hi"}]}), 403)
        self.assertEqual(ok({"messages": [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x",
                                                                         "content": [{"type": "text", "text": "越狱"}]}]}]}), 403)
        self.assertEqual(ok({"messages": [{"role": "user", "content": [{"type": "image", "source": {"type": "base64",
                                         "media_type": "image/png", "data": "越狱"}}]}]}), 200)  # real media payloads are not text
        # Text is never skipped because of a key name or prefix outside real media structures.
        self.assertEqual(ok({"messages": [{"role": "user", "content": [{"type": "text", "text": "data:越狱"}]}]}), 403)
        self.assertEqual(ok({"messages": [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t",
                                                                         "content": [{"type": "text", "text": "x", "data": "越狱"}]}]}]}), 403)
        self.assertEqual(ok({"messages": [{"role": "user", "content": [{"type": "text", "text": "hi", "url": "越狱"}]}]}), 403)
        self.assertIn("命中屏蔽词：越狱", self.admin.post("/api/admin/keys/logs", {"errorsOnly": True})[1]["items"][0]["errorMessage"])
        self.admin.post("/api/admin/keys/settings", {"blockedWords": []})
        # 500 may already be billed upstream: not retried. 529 is retried.
        FakeUpstream.status = 500
        before = len(FakeUpstream.calls)
        self.assertEqual(ok({}), 502)
        self.assertEqual(len(FakeUpstream.calls) - before, 1)
        FakeUpstream.status = 200
        # Saved upstream key is not reused for a different host.
        moved = {"openai": {"url": "https://evil.example/v1", "key": "", "models": {"cheap": [1, 2]}}}
        self.assertEqual(self.admin.post("/api/admin/keys/upstreams", {"upstreams": moved})[0], 400)
        same_host = {"openai": {**self.upstreams["openai"], "key": ""}, "anthropic": {**self.upstreams["anthropic"], "key": ""}}
        self.assertEqual(self.admin.post("/api/admin/keys/upstreams", {"upstreams": same_host})[0], 200)
        # Keys stop with the team: withdrawn status, then revoked team login.
        with closing(sqlite3.connect(self.root / "private" / "event.sqlite3")) as db:
            db.execute("UPDATE team_details SET status='withdrawn', status_reason='退赛' WHERE team_id=?", (team_id,))
            db.commit()
            self.assertEqual(ok({}), 401)
            db.execute("UPDATE team_details SET status='active' WHERE team_id=?", (team_id,))
            db.commit()
        self.assertEqual(ok({}), 200)
        self.assertEqual(self.admin.post("/api/admin/team-access", {"teamId": team_id, "action": "revoke"})[0], 200)
        self.assertEqual(ok({}), 401)

    def assertReleased(self, team_id, expected=0):
        """The slot is released just after the reply is sent, so allow the server thread to finish."""
        for _ in range(100):
            if self.service.keys.inflight[team_id] == expected:
                return
            threading.Event().wait(0.02)
        self.assertEqual(self.service.keys.inflight[team_id], expected)

    def latest(self):
        return self.admin.post("/api/admin/keys/logs", {})[1]["items"][0]

    def test_missing_usage_is_estimated_not_free(self):
        _, _, key = self.enabled_key()
        FakeUpstream.usage = {"note": "no token counts"}
        body = {"model": "cheap", "messages": [{"role": "user", "content": "x" * 4000}]}
        self.assertEqual(self.call("/v1/chat/completions", body, key)[0], 200)
        item = self.latest()
        self.assertTrue(item["estimated"])
        self.assertFalse(item["incomplete"])
        self.assertGreaterEqual(item["input"], 1000)  # ~ request bytes / 4
        self.assertGreater(item["cost"], 0)

    def test_cut_stream_is_flagged_incomplete_and_billed(self):
        _, client, key = self.enabled_key()
        original = FakeUpstream.do_POST
        reply = "回答" * 200

        def cut(handler):  # input usage and some text, then the upstream dies before message_delta / message_stop
            handler.rfile.read(int(handler.headers["Content-Length"]))
            handler.send_response(200)
            handler.send_header("Content-Type", "text/event-stream")
            handler.end_headers()
            for event in [{"type": "message_start", "message": {"usage": {"input_tokens": 300, "output_tokens": 1}}},
                          {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": reply}}]:
                handler.wfile.write(b"data: " + json.dumps(event).encode() + b"\n\n")
            handler.wfile.flush()
            handler.connection.close()
        FakeUpstream.do_POST = cut
        try:
            self.call("/v1/messages", {"model": "claude-x", "stream": True}, key)
        finally:
            FakeUpstream.do_POST = original
        item = self.latest()
        self.assertEqual((item["status"], item["incomplete"], item["estimated"]), (502, 1, 1))  # inferred, as sub2api
        self.assertEqual(item["input"], 300)  # observed usage kept
        self.assertGreaterEqual(item["output"], len(reply.encode()) // 4)  # output estimated from streamed text
        self.assertEqual(self.admin.get("/api/admin/keys")[1]["overview"]["errors"], 1)  # counted as a failure
        self.assertEqual(len(self.admin.post("/api/admin/keys/logs", {"errorsOnly": True})[1]["items"]), 1)
        self.assertTrue(client.get("/api/team/keys/logs")[1]["items"][0]["incomplete"])

    def test_queued_request_is_rechecked_after_the_wait(self):
        team_id, _, key = self.enabled_key()
        service = self.service.keys
        service.inflight[team_id] = keys.TEAM_CONCURRENCY  # team busy: the next request queues
        result = {}
        waiter = threading.Thread(target=lambda: result.update(status=self.call("/v1/chat/completions", {"model": "cheap"}, key)[0]))
        waiter.start()
        for _ in range(100):
            if service.slot_free._waiters:  # the request is now waiting for a slot
                break
            threading.Event().wait(0.02)
        self.assertEqual(self.admin.post("/api/admin/keys/revoke", {"teamId": team_id})[0], 200)
        before = len(FakeUpstream.calls)
        service.release(team_id)
        waiter.join(10)
        self.assertEqual(result["status"], 401)
        self.assertEqual(len(FakeUpstream.calls), before)  # never reached the upstream
        self.assertEqual(service.inflight[team_id], keys.TEAM_CONCURRENCY - 1)  # its slot was given back
        self.assertReleased(team_id, keys.TEAM_CONCURRENCY - 1)

    def test_output_limits_and_single_completions(self):
        team_id, _, key = self.enabled_key()
        self.assertEqual(self.call("/v1/messages", {"model": "claude-x", "max_tokens": 100}, key)[0], 200)
        self.assertEqual(FakeUpstream.calls[-1][2]["max_tokens"], 100)
        self.call("/v1/messages", {"model": "claude-x"}, key)
        self.assertEqual(FakeUpstream.calls[-1][2]["max_tokens"], keys.OUTPUT_DEFAULT)  # injected when absent
        self.call("/v1/messages", {"model": "claude-x", "max_tokens": 10**9}, key)
        self.assertEqual(FakeUpstream.calls[-1][2]["max_tokens"], keys.OUTPUT_CAP)  # clamped
        self.call("/v1/chat/completions", {"model": "cheap", "max_completion_tokens": "lots"}, key)
        self.assertEqual(FakeUpstream.calls[-1][2]["max_completion_tokens"], keys.OUTPUT_CAP)  # non-integer
        self.assertNotIn("max_tokens", FakeUpstream.calls[-1][2])  # existing field used, none added
        before = len(FakeUpstream.calls)
        for body in [{"model": "cheap", "n": 4}, {"model": "cheap", "n": "2"}, {"model": "cheap", "best_of": 3}]:
            self.assertEqual(self.call("/v1/chat/completions", body, key)[0], 400, body)
        self.assertEqual(len(FakeUpstream.calls), before)  # multi-completion requests never reach the upstream
        self.assertEqual(self.call("/v1/chat/completions", {"model": "cheap", "n": 1}, key)[0], 200)

    def test_existing_budget_settings_do_not_limit_metered_requests_after_restart(self):
        team_id, client, key = self.enabled_key()
        self.assertEqual(self.call("/v1/messages", {"model": "claude-x"}, key)[0], 200)
        before = client.get("/api/team/keys")[1]["usage"]["usedTotal"]
        with self.service.db() as db:
            for name in ("dailyBudget", "totalBudget"):
                db.execute("INSERT INTO settings VALUES (?,?)", ("keys." + name, "0"))
            db.execute("ALTER TABLE team_api ADD COLUMN daily_budget REAL")
            db.execute("ALTER TABLE team_api ADD COLUMN total_budget REAL")
            db.execute("ALTER TABLE team_api ADD COLUMN note TEXT NOT NULL DEFAULT ''")
            db.execute("UPDATE team_api SET daily_budget=0,total_budget=0 WHERE team_id=?", (team_id,))
        restarted = test_api.EventService(data_dir=self.root / "private", origin=self.service.origin, static_root=self.root / "public")
        self.server.service = self.service = restarted
        self.service.keys.opener = self.direct
        for path, model in (("/v1/messages", "claude-x"), ("/v1/chat/completions", "cheap"), ("/v1/responses", "cheap")):
            self.assertEqual(self.call(path, {"model": model}, key)[0], 200)
        self.assertEqual(self.call("/v1/messages/count_tokens", {"model": "claude-x"}, key)[0], 200)
        listing = client.get("/api/team/keys")[1]
        self.assertGreater(listing["usage"]["usedTotal"], before)
        self.assertEqual(listing["usage"]["used24h"], listing["usage"]["usedTotal"])
        settings = self.admin.get("/api/admin/keys")[1]["settings"]
        self.assertNotIn("dailyBudget", settings)
        self.assertNotIn("totalBudget", settings)
        self.assertEqual(self.admin.post("/api/admin/keys/settings", {"dailyBudget": 1})[0], 400)
        self.assertEqual(self.admin.post("/api/admin/keys/team", {"teamId": team_id, "dailyBudget": 1, "totalBudget": 1, "note": ""})[0], 404)

    def test_old_usage_table_gains_flag_columns(self):
        with closing(sqlite3.connect(self.root / "private" / "event.sqlite3")) as db:
            db.execute("ALTER TABLE api_usage DROP COLUMN incomplete")
            db.execute("ALTER TABLE api_usage DROP COLUMN estimated")
            db.commit()
        restarted = test_api.EventService(data_dir=self.root / "private", origin=self.service.origin, static_root=self.root / "public")
        with closing(sqlite3.connect(self.root / "private" / "event.sqlite3")) as db:
            columns = {r[1] for r in db.execute("PRAGMA table_info(api_usage)")}
        self.assertTrue({"incomplete", "estimated"} <= columns)
        self.assertIsNotNone(restarted)

    def test_upstream_failures_record_their_final_status(self):
        team_id, _, key = self.enabled_key()
        errors = lambda: self.admin.get("/api/admin/keys")[1]["overview"]["errors"]
        for count, code in enumerate((401, 403, 500, 503), 1):
            FakeUpstream.status = code
            self.assertEqual(self.call("/v1/chat/completions", {"model": "cheap"}, key)[0], 502)
            item = self.latest()
            self.assertEqual((item["status"], item["cost"]), (502, 0), code)  # was recorded as status 0
            self.assertEqual(errors(), count)
            detail = self.admin.post("/api/admin/keys/log", {"id": item["id"]})[1]
            self.assertIn(f"上游 HTTP {code}", detail["response"])  # the admin can see why
        self.assertReleased(team_id)

    def test_upstream_timeouts_are_charged_estimated_usage(self):
        team_id, _, key = self.enabled_key()
        timeout, keys.UPSTREAM_TIMEOUT = keys.UPSTREAM_TIMEOUT, 0.5
        try:
            for hang in ("before", "body"):
                FakeUpstream.hang = hang
                body = {"model": "claude-x", "max_tokens": 1000, "messages": [{"role": "user", "content": "x" * 400}]}
                self.assertEqual(self.call("/v1/messages", body, key)[0], 504, hang)
                item = self.latest()
                self.assertEqual((item["status"], item["incomplete"], item["estimated"]), (504, 1, 1), hang)
                self.assertEqual(item["output"], 1000)  # the estimated output, not zero
                self.assertGreater(item["cost"], 0.015)  # 1000 output tokens at $15 / 1M
                detail = self.admin.post("/api/admin/keys/log", {"id": item["id"]})[1]
                self.assertIn("请求已发出，按估算用量计费", detail["response"])
            self.assertEqual(self.admin.get("/api/admin/keys")[1]["overview"]["errors"], 2)
            self.assertReleased(team_id)  # released after the charge landed
        finally:
            keys.UPSTREAM_TIMEOUT, FakeUpstream.hang = timeout, None

    def test_reply_cut_mid_body_is_charged_and_refused_connection_is_free(self):
        _, _, key = self.enabled_key()
        FakeUpstream.hang = "reset"
        body = {"model": "claude-x", "max_tokens": 1000, "messages": [{"role": "user", "content": "hi"}]}
        self.assertEqual(self.call("/v1/messages", body, key)[0], 502)
        item = self.latest()
        self.assertEqual((item["status"], item["incomplete"], item["estimated"], item["output"]), (502, 1, 1, 1000))
        self.assertGreater(item["cost"], 0)  # upstream had started answering: not free
        FakeUpstream.hang = None
        self.fake.shutdown()
        self.fake.server_close()  # nothing listening: connection refused, the request never left
        self.assertEqual(self.call("/v1/messages", body, key)[0], 502)
        item = self.latest()
        self.assertEqual((item["status"], item["cost"], item["estimated"]), (502, 0, 0))

    def test_every_upstream_failure_is_billed_by_whether_the_request_was_sent(self):
        """Free when the upstream never received the whole request; charged estimated usage once it was sent."""
        team_id, _, key = self.enabled_key()
        servers = []

        def serve(server):
            servers.append(server)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            return server.server_port

        def silent_listener():  # accepts TCP, never answers: a TLS handshake against it times out before sending
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            sock.listen(8)
            servers.append(sock)
            return sock.getsockname()[1]

        class RefusingProxy(BaseHTTPRequestHandler):  # what an HTTPS_PROXY does when it cannot reach the upstream
            def log_message(self, *_):
                pass

            def do_CONNECT(self):
                self.send_response(502)
                self.end_headers()
        proxy = f"http://127.0.0.1:{serve(ThreadingHTTPServer(('127.0.0.1', 0), RefusingProxy))}"
        plain_port = self.fake.server_port
        cases = [  # name, upstream URL, fake mode, student status, charged, upstream saw the request, admin reason
            ("connection refused", "http://127.0.0.1:1/v1", None, 502, False, False, "上游拒绝连接"),
            ("unknown host", "https://gotc-gateway-test.invalid/v1", None, 502, False, False, "上游域名无法解析"),
            ("HTTPS to a plain HTTP port", f"https://localhost:{plain_port}/v1", None, 502, False, False, "TLS 握手失败"),
            ("TLS handshake timeout", f"https://localhost:{silent_listener()}/v1", None, 502, False, False, "上游超时"),
            ("HTTPS_PROXY cannot reach upstream", "https://api.example.com/v1", "proxy", 502, False, False, "Tunnel connection failed"),
            ("upstream 401", f"http://127.0.0.1:{plain_port}/v1", 401, 502, False, True, "上游 HTTP 401"),
            ("upstream 500", f"http://127.0.0.1:{plain_port}/v1", 500, 502, False, True, "上游 HTTP 500"),
            ("timeout after sending", f"http://127.0.0.1:{plain_port}/v1", "before", 504, True, True, "请求已发出"),
            ("closed after receiving", f"http://127.0.0.1:{plain_port}/v1", "drop", 502, True, True, "请求已发出"),
            ("reply cut mid-body", f"http://127.0.0.1:{plain_port}/v1", "reset", 502, True, True, "请求已发出"),
            ("reply body stalls", f"http://127.0.0.1:{plain_port}/v1", "body", 504, True, True, "请求已发出"),
        ]
        if shutil.which("openssl"):
            certs = tempfile.mkdtemp()
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", f"{certs}/key.pem",
                            "-out", f"{certs}/cert.pem", "-days", "1", "-subj", "/CN=localhost"], check=True, capture_output=True)
            tls = ThreadingHTTPServer(("127.0.0.1", 0), FakeUpstream)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(f"{certs}/cert.pem", f"{certs}/key.pem")
            tls.socket = context.wrap_socket(tls.socket, server_side=True)
            cases.insert(2, ("untrusted certificate", f"https://localhost:{serve(tls)}/v1", None, 502, False, False, "TLS 证书校验失败"))
        timeout, keys.UPSTREAM_TIMEOUT = keys.UPSTREAM_TIMEOUT, 0.5
        connect, keys.CONNECT_TIMEOUT = keys.CONNECT_TIMEOUT, 0.5
        try:
            for name, url, mode, student, charged, reached, reason in cases:
                with self.subTest(name):
                    upstream = {"anthropic": {"url": url, "key": "sk-upstream-anthropic", "models": {"claude-x": [3, 15]}}}
                    self.assertEqual(self.admin.post("/api/admin/keys/upstreams", {"upstreams": upstream})[0], 200)
                    self.service.keys.opener = keys.make_opener({"https": proxy}) if mode == "proxy" else self.direct
                    FakeUpstream.hang, FakeUpstream.status = (None, 200) if mode in (None, "proxy") else (mode, 200) if isinstance(mode, str) else (None, mode)
                    before = len(FakeUpstream.calls)
                    real_lookup = socket.getaddrinfo

                    def lookup(host, *args, **kwargs):  # deterministic DNS failure, whatever resolver this machine uses
                        if host.endswith(".invalid"):
                            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
                        return real_lookup(host, *args, **kwargs)
                    with mock.patch("socket.getaddrinfo", lookup):
                        status, _ = self.call("/v1/messages", {"model": "claude-x", "max_tokens": 1000}, key)
                    item = self.latest()
                    detail = self.admin.post("/api/admin/keys/log", {"id": item["id"]})[1]["response"]
                    self.assertEqual((status, item["status"]), (student, student))
                    self.assertEqual(item["cost"] > 0, charged, f"cost={item['cost']}")
                    self.assertEqual(bool(item["estimated"]), charged)
                    self.assertEqual(len(FakeUpstream.calls) > before, reached)
                    self.assertIn(reason, detail)
                    if charged:
                        self.assertIn("请求已发出，按估算用量计费", detail)
                    elif not reached:
                        self.assertIn("请求未发出，不计费", detail)
                    self.assertReleased(team_id)
        finally:
            keys.UPSTREAM_TIMEOUT, keys.CONNECT_TIMEOUT, FakeUpstream.hang, FakeUpstream.status = timeout, connect, None, 200
            for server in servers:
                (server.close if isinstance(server, socket.socket) else server.shutdown)()

    def test_unexpected_gateway_error_never_records_status_zero(self):
        team_id, _, key = self.enabled_key()
        with mock.patch.object(keys, "read_usage", side_effect=RuntimeError("bug")):
            status, _ = self.call("/v1/messages", {"model": "claude-x", "max_tokens": 1000}, key)
        self.assertEqual(status, 500)
        item = self.latest()
        self.assertEqual((item["status"], item["estimated"]), (500, 1))  # the upstream had answered: charged, not free
        self.assertGreater(item["cost"], 0)
        self.assertEqual(self.admin.get("/api/admin/keys")[1]["overview"]["errors"], 1)
        self.assertReleased(team_id)
        self.assertEqual(self.call("/v1/messages", {"model": "claude-x", "max_tokens": 10}, key)[0], 200)  # gateway still healthy

    def test_concurrency_slot_is_released_even_if_recording_fails(self):
        team_id, _, key = self.enabled_key()
        with mock.patch.object(self.service.keys, "record", side_effect=lambda *a: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked"))):
            status, _ = self.call("/v1/messages", {"model": "claude-x", "max_tokens": 1000}, key)
        self.assertEqual(status, 200)  # the team still got its reply
        self.assertReleased(team_id)  # and its slot is released

    def test_stream_is_drained_and_billed_when_the_client_left_before_headers(self):
        _, client, key = self.enabled_key()
        real_headers_out = test_api.EventHandler.headers_out

        def broken(handler, status, content_type, *args, **kwargs):
            if content_type.startswith("text/event-stream"):
                raise BrokenPipeError()
            return real_headers_out(handler, status, content_type, *args, **kwargs)
        with mock.patch.object(test_api.EventHandler, "headers_out", broken):
            with self.assertRaises(http.client.RemoteDisconnected):  # the client is "gone": nothing is sent back
                self.call("/v1/messages", {"model": "claude-x", "stream": True, "max_tokens": 1000}, key)
        item = self.latest()
        self.assertEqual((item["status"], item["input"], item["output"], item["incomplete"]), (200, 1000, 500, 0))  # usage read to the end
        self.assertGreater(item["cost"], 0)

    def sse(self, *events, anthropic=True):
        out = b""
        for event in events:
            if anthropic and isinstance(event, dict) and "type" in event:
                out += b"event: " + event["type"].encode() + b"\n"
            out += b"data: " + (event if isinstance(event, bytes) else json.dumps(event).encode()) + b"\n\n"
        return out

    def test_abnormal_requests_follow_sub2api_records_and_billing(self):
        """Every abnormal case: what the team receives, what is recorded (sub2api classification) and what is charged."""
        team_id, client, key = self.enabled_key()
        secret = self.upstreams["anthropic"]["key"]
        start = {"type": "message_start", "message": {"usage": {"input_tokens": 300, "output_tokens": 1}}}
        delta = {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "回答" * 100}}
        overloaded = {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}
        A, O = "/v1/messages", "/v1/chat/completions"
        cases = [
            # name, path, request, fake upstream reply, status, error type, phase, owner, upstream status, charged
            ("model not allowed", A, {"model": "nope"}, None, 404, "not_found_error", "request", "client", None, False),
            ("upstream 400 passed through", A, {"model": "claude-x"}, (400, "application/json", b'{"type":"error","error":{"type":"invalid_request_error","message":"max_tokens: too big"}}'),
             400, "invalid_request_error", "upstream", "provider", 400, False),
            ("upstream 401 hidden", A, {"model": "claude-x"}, (401, "application/json", f'{{"error":{{"message":"invalid x-api-key {secret}"}}}}'.encode()),
             502, "api_error", "upstream", "provider", 401, False),
            ("upstream 404 on messages", A, {"model": "claude-x"}, (404, "application/json", b'{"error":{"message":"no such model"}}'),
             502, "api_error", "upstream", "provider", 404, False),
            ("upstream 404 on chat", O, {"model": "cheap"}, (404, "application/json", b'{"error":{"message":"no such model"}}'),
             404, "invalid_request_error", "upstream", "provider", 404, False),
            ("upstream 429", A, {"model": "claude-x"}, (429, "application/json", b'{"error":{"message":"slow down"}}'),
             429, "rate_limit_error", "upstream", "provider", 429, False),
            ("upstream 529", A, {"model": "claude-x"}, (529, "application/json", b'{"error":{"message":"Overloaded"}}'),
             503, "overloaded_error", "upstream", "provider", 529, False),
            ("200 that is not JSON", A, {"model": "claude-x"}, (200, "text/html", b"<html>gateway page</html>"),
             502, "api_error", "upstream", "provider", 200, False),
            ("200 carrying an error", O, {"model": "cheap"}, (200, "application/json", b'{"error":{"message":"server overloaded"}}'),
             503, "overloaded_error", "upstream", "provider", 200, False),
            ("stream error before output", A, {"model": "claude-x", "stream": True}, (200, "text/event-stream", self.sse(overloaded)),
             503, "overloaded_error", "upstream", "provider", 200, False),
            ("stream error after output", A, {"model": "claude-x", "stream": True}, (200, "text/event-stream", self.sse(start, delta, overloaded)),
             200, "overloaded_error", "upstream", "provider", 200, True),
            ("stream ends without terminal event", A, {"model": "claude-x", "stream": True}, (200, "text/event-stream", self.sse(start, delta)),
             200, "api_error", "network", "provider", None, True),
            ("chat stream ends without terminal event", O, {"model": "cheap", "stream": True},
             (200, "text/event-stream", self.sse({"choices": [{"delta": {"content": "hi" * 100}}]}, anthropic=False)),
             200, "api_error", "network", "provider", None, True),
        ]
        for name, path, request, reply, status, etype, phase, owner, ustatus, charged in cases:
            with self.subTest(name):
                FakeUpstream.reply = reply
                got, content = self.call(path, request, key)
                item = self.latest()
                self.assertEqual(got, status)
                recorded = 502 if got == 200 else got  # a stream that failed after the 200 is recorded as a failure
                self.assertEqual((item["status"], item["errorType"], item["errorPhase"], item["errorOwner"], item["upstreamStatus"]),
                                 (recorded, etype, phase, owner, ustatus))
                self.assertEqual(item["cost"] > 0, charged, f"cost={item['cost']}")
                self.assertNotIn(secret, json.dumps(item))  # our upstream key never reaches records
                self.assertNotIn(secret.encode(), content)
                if got != 200:  # native error shape of each API
                    body = json.loads(content)
                    if path == A and got != 400:
                        self.assertEqual((body["type"], body["error"]["type"]), ("error", etype))
                    elif path == O:  # OpenAI shape and type names in the response; the record keeps one canonical type
                        self.assertEqual(set(body), {"error"})
                        self.assertEqual(set(body["error"]), {"message", "type", "code"})
                if name == "upstream 400 passed through":
                    self.assertIn(b"max_tokens: too big", content)
                if name == "stream ends without terminal event":
                    self.assertIn(b"event: error", content)  # the client is told, not just cut off
                if name == "chat stream ends without terminal event":
                    self.assertIn(b'data: {"error"', content)
                if name == "stream error after output":
                    self.assertEqual(content.count(b"event: error"), 1)  # the upstream's frame, not a second one
        FakeUpstream.reply = None
        errors = self.admin.get("/api/admin/keys")[1]["overview"]["errors"]
        self.assertEqual(errors, len(cases))
        self.assertReleased(team_id)
        own = client.get("/api/team/keys/logs")[1]["items"]
        self.assertEqual(own[0]["errorOwner"], "provider")  # teams can see whose fault a failure was

    def test_client_that_left_while_queued_is_not_forwarded(self):
        team_id, _, key = self.enabled_key()
        service = self.service.keys
        service.inflight[team_id] = keys.TEAM_CONCURRENCY
        body = json.dumps({"model": "cheap"}).encode()
        sock = socket.create_connection(("127.0.0.1", self.server.server_port))
        sock.sendall(b"POST " + self.service.base_path.encode() + b"/v1/chat/completions HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                     b"Authorization: Bearer " + key.encode() + b"\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
        for _ in range(100):
            if service.slot_free._waiters:
                break
            threading.Event().wait(0.02)
        sock.close()  # the student gives up while queued
        before = len(FakeUpstream.calls)
        service.release(team_id)
        for _ in range(100):
            items = self.admin.post("/api/admin/keys/logs", {})[1]["items"]
            if items:
                break
            threading.Event().wait(0.05)
        self.assertEqual((items[0]["status"], items[0]["errorType"], items[0]["cost"]), (499, "api_error", 0))
        self.assertEqual(len(FakeUpstream.calls), before)  # never forwarded
        self.assertReleased(team_id, keys.TEAM_CONCURRENCY - 1)

    def test_count_tokens_mirrors_upstream_errors_and_is_never_charged(self):
        _, _, key = self.enabled_key()
        for reply, status in [((404, "text/html", b"<html><body><h1>Resource not found</h1></body></html>"), 404),  # Gitee AI
                              ((529, "application/json", b'{"error":{"message":"Overloaded"}}'), 503),
                              ((429, "application/json", b'{"error":{"message":"slow"}}'), 429),
                              ((400, "application/json", b'{"error":{"message":"bad"}}'), 400)]:
            FakeUpstream.reply = reply
            self.assertEqual(self.call("/v1/messages/count_tokens", {"model": "claude-x"}, key)[0], status)
            item = self.latest()
            self.assertEqual((item["status"], item["cost"], item["upstreamStatus"]), (status, 0, reply[0]))
        FakeUpstream.reply = None

    def test_old_usage_table_gains_error_columns(self):
        with closing(sqlite3.connect(self.root / "private" / "event.sqlite3")) as db:
            for column in ("error_type", "error_phase", "error_owner", "upstream_status", "error_message"):
                db.execute(f"ALTER TABLE api_usage DROP COLUMN {column}")
            db.commit()
        test_api.EventService(data_dir=self.root / "private", origin=self.service.origin, static_root=self.root / "public")
        with closing(sqlite3.connect(self.root / "private" / "event.sqlite3")) as db:
            columns = {r[1] for r in db.execute("PRAGMA table_info(api_usage)")}
        self.assertTrue({"error_type", "error_phase", "error_owner", "upstream_status", "error_message"} <= columns)

    def test_token_accounting_matches_sub2api(self):
        """Usage parsing and pricing rules ported from sub2api's billing service."""
        merged = [0] * keys.USAGE_FIELDS
        keys.read_usage("anthropic", {"type": "message_start", "message": {"usage": {"input_tokens": 100, "output_tokens": 1,
                        "cache_creation_input_tokens": 300, "cache_creation": {"ephemeral_5m_input_tokens": 100, "ephemeral_1h_input_tokens": 200},
                        "cache_read_input_tokens": 50}}}, merged)
        self.assertEqual(merged, [100, 0, 100, 50, 200])  # placeholder output ignored; 5m/1h split kept
        keys.read_usage("anthropic", {"type": "message_delta", "usage": {"output_tokens": 70, "input_tokens": 0}}, merged)
        self.assertEqual(merged, [100, 70, 100, 50, 200])  # a later zero never erases a count
        total_only = [0] * keys.USAGE_FIELDS
        keys.read_usage("anthropic", {"usage": {"input_tokens": 1, "output_tokens": 1, "cache_creation_input_tokens": 40}}, total_only)
        self.assertEqual(total_only[2:], [40, 0, 0])  # no split: all at the 5-minute price
        for usage_obj, expected in [
                ({"prompt_tokens": 1000, "completion_tokens": 10, "prompt_tokens_details": {"cached_tokens": 800}}, [200, 10, 0, 800, 0]),
                ({"prompt_tokens": 1000, "completion_tokens": 10, "prompt_cache_hit_tokens": 600}, [400, 10, 0, 600, 0]),  # DeepSeek naming
                ({"prompt_tokens": 1000, "completion_tokens": 10, "prompt_tokens_details": {"cached_tokens": 300, "cache_write_tokens": 200}},
                 [500, 10, 200, 300, 0]),
                ({"prompt_tokens": 10, "completion_tokens": 1, "prompt_tokens_details": {"cached_tokens": 50}}, [0, 1, 0, 50, 0]),  # floored at 0
                ({"input_tokens": 70, "output_tokens": 5, "input_tokens_details": {"cached_tokens": 20}}, [50, 5, 0, 20, 0])]:  # Responses API naming
            got = [0] * keys.USAGE_FIELDS
            keys.read_usage("openai", {"usage": usage_obj}, got)
            self.assertEqual(got, expected, usage_obj)
        self.assertEqual(keys.full_price([3, 15]), [3, 15, 3.75, 0.3, 6])  # 1h writes default to 2x input
        self.assertEqual(keys.full_price([3, 15, 4, 0.2]), [3, 15, 4, 0.2, 6])
        self.assertEqual(keys.full_price([3, 15, 4, 0.2, 9]), [3, 15, 4, 0.2, 9])
        # Name-only models (as fetched from /models) use the upstream default price, or $0 without one.
        url = "https://x.test/v1"
        cfg = keys.parse_config({"openai": {"url": url, "key": "k", "models": {"a": [], "b": [1, 2]}, "defaultPrice": [0.5, 2]}})
        self.assertEqual(cfg["openai"]["models"], {"a": [0.5, 2, 0.625, 0.05, 1], "b": [1, 2, 1.25, 0.1, 2]})
        self.assertEqual(keys.parse_config({"openai": {"url": url, "key": "k", "models": {"a": []}}})["openai"]["models"]["a"], [0, 0, 0, 0, 0])
        for bad in ([1], [], [-1, 2]):
            with self.assertRaises(ValueError):
                keys.parse_config({"openai": {"url": url, "key": "k", "models": {"a": []}, "defaultPrice": bad}})

        _, _, key = self.enabled_key()
        FakeUpstream.usage = {"input_tokens": 100, "output_tokens": 10, "cache_creation_input_tokens": 1000,
                              "cache_creation": {"ephemeral_5m_input_tokens": 400, "ephemeral_1h_input_tokens": 600}}
        self.call("/v1/messages", {"model": "claude-x", "max_tokens": 50}, key, headers={"User-Agent": "ua-test", "X-Real-IP": "10.1.2.3"})
        item = self.latest()
        # claude-x [3, 15] -> 5m write 3.75, 1h write 6: 100*3 + 10*15 + 400*3.75 + 600*6 = 5550 micro-USD
        self.assertEqual(round(item["cost"] * 1e6), 5550)
        self.assertEqual((item["cacheWrite1h"], item["clientIp"], item["requestedModel"]), (600, "10.1.2.3", "claude-x"))
        self.assertIsNotNone(item["firstTokenMs"])
        self.call("/v1/messages", {"model": "claude-latest", "max_tokens": 50}, key)
        self.assertEqual((self.latest()["requestedModel"], self.latest()["model"]), ("claude-latest", "claude-x"))
        stats = self.admin.post("/api/admin/keys/stats", {"days": 1})[1]
        self.assertEqual(stats["totals"]["cacheWrite"], 2000)  # cache writes are counted (two calls of 1000)
        self.assertEqual(self.admin.get("/api/admin/keys")[1]["overview"]["cacheWrite"], 2000)

    def raw_call(self, path, body, headers):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=20)
        conn.request("POST", self.service.base_path + path, json.dumps(body).encode(), {"Content-Type": "application/json", **headers})
        response = conn.getresponse()
        content, retry_after = response.read(), response.getheader("Retry-After")
        conn.close()
        return response.status, content, retry_after

    def test_auth_limits_and_key_handling_match_sub2api(self):
        team_id, client, key = self.enabled_key()
        # Bearer is case-insensitive; keys in the URL are refused; absurdly long keys are not hashed.
        self.assertEqual(self.raw_call("/v1/chat/completions", {"model": "cheap"}, {"Authorization": "bearer " + key})[0], 200)
        self.assertEqual(self.raw_call("/v1/chat/completions?key=" + key, {"model": "cheap"}, {})[0], 400)
        self.assertEqual(self.raw_call("/v1/chat/completions", {"model": "cheap"}, {"Authorization": "Bearer " + "x" * 5000})[0], 401)
        # Failed keys from one address are blocked after the limit, before any hashing, with Retry-After.
        limit, keys.AUTH_FAIL_LIMIT = keys.AUTH_FAIL_LIMIT, 3
        self.service.keys.auth_failures.clear()  # the over-long key above already counted as one failure
        try:
            for _ in range(3):
                self.assertEqual(self.raw_call("/v1/chat/completions", {"model": "cheap"}, {"Authorization": "Bearer gk-wrong"})[0], 401)
            status, _, retry = self.raw_call("/v1/chat/completions", {"model": "cheap"}, {"Authorization": "Bearer gk-wrong-again"})
            self.assertEqual((status, retry is not None), (429, True))  # further bad keys from that address: blocked
            # A valid key is never refused because of others' bad keys: a venue NAT puts every team behind one address.
            self.assertEqual(self.raw_call("/v1/chat/completions", {"model": "cheap"}, {"Authorization": "Bearer " + key})[0], 200)
            overview = self.admin.get("/api/admin/keys")[1]["overview"]
            self.assertGreaterEqual(overview["authFailuresHour"], 4)
            self.assertEqual(overview["blockedClients"], 1)
            other = self.raw_call("/v1/chat/completions", {"model": "cheap"}, {"Authorization": "Bearer " + key, "X-Real-IP": "10.9.9.9"})
            self.assertEqual(other[0], 200)  # other addresses are unaffected
        finally:
            keys.AUTH_FAIL_LIMIT = limit
            self.service.keys.auth_blocked.clear()
        # Key creation is limited per hour; deleting does not give creations back.
        per_hour, keys.KEY_CREATE_PER_HOUR = keys.KEY_CREATE_PER_HOUR, 2
        try:
            created = client.post("/api/team/keys", {"name": "b"})[1]["item"]["id"]
            client.post("/api/team/keys/delete", {"id": created})
            self.assertEqual(client.post("/api/team/keys", {"name": "c"})[0], 429)
        finally:
            keys.KEY_CREATE_PER_HOUR = per_hour

    def test_queue_and_rpm_follow_sub2api(self):
        team_id, _, key = self.enabled_key()
        service = self.service.keys
        cap, keys.QUEUE_CAP = keys.QUEUE_CAP, 0
        try:
            service.inflight[team_id] = keys.TEAM_CONCURRENCY
            status, content, retry = self.raw_call("/v1/chat/completions", {"model": "cheap"}, {"Authorization": "Bearer " + key})
            self.assertEqual((status, retry), (429, "2"))  # queue full: rejected at once, not after 30 s
            self.assertIn("排队请求已满".encode(), content)
            # count_tokens takes no concurrency slot, so it still works while the team is busy.
            self.assertEqual(self.call("/v1/messages/count_tokens", {"model": "claude-x"}, key)[0], 200)
        finally:
            keys.QUEUE_CAP = cap
            service.inflight[team_id] = 0
        self.assertEqual(self.admin.post("/api/admin/keys/settings", {"teamRpm": 2})[0], 200)
        service.recent[team_id].clear()
        wait, keys.QUEUE_WAIT = keys.QUEUE_WAIT, 0.2
        try:
            service.inflight[team_id] = keys.TEAM_CONCURRENCY
            self.assertEqual(self.call("/v1/chat/completions", {"model": "cheap"}, key)[0], 429)  # queue timeout
            service.inflight[team_id] = 0
            self.assertEqual(len(service.recent[team_id]), 0)  # a request that never ran does not use up the minute
            for _ in range(2):
                self.assertEqual(self.call("/v1/chat/completions", {"model": "cheap"}, key)[0], 200)
            status, _, retry = self.raw_call("/v1/chat/completions", {"model": "cheap"}, {"Authorization": "Bearer " + key})
            self.assertEqual(status, 429)
            self.assertTrue(1 <= int(retry) <= 61)  # tells the SDK when the minute frees up
        finally:
            keys.QUEUE_WAIT = wait

    def test_settings_audit_keeps_before_and_after(self):
        self.assertEqual(self.admin.post("/api/admin/keys/settings", {"maxKeys": 7, "teamRpm": 60})[0], 200)
        change = next(c for c in self.admin.get("/api/admin/keys")[1]["changes"] if c["action"] == "api-key.settings")
        self.assertEqual(change["metadata"], {"maxKeys": [5, 7]})  # unchanged teamRpm is not listed

    def test_input_estimate_counts_images_not_their_bytes(self):
        image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "A" * 400000}}
        self.assertLess(keys.estimate_input({"messages": [{"role": "user", "content": [image]}]}), 1700)  # not 100k
        text = {"messages": [{"role": "user", "content": "字" * 300000}]}
        self.assertGreater(keys.estimate_input(text), 200000)  # large text is never capped
        pdf = {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": "A" * 40000}}
        self.assertGreater(keys.estimate_input({"messages": [{"role": "user", "content": [pdf]}]}), 10000)
        self.assertLess(keys.estimate_input({"messages": [{"role": "user", "content": [{"type": "image_url",
                        "image_url": {"url": "data:image/jpeg;base64," + "A" * 400000}}]}]}), 1700)

    def test_protocol_handling_matches_sub2api(self):
        _, _, key = self.enabled_key()
        auth = {"Authorization": "Bearer " + key}
        # Request id on every response, stored with the call; upstream request id and Retry-After passed through,
        # rate-limit headers of our shared upstream key are not.
        FakeUpstream.reply = (200, "application/json", b'{"id":"x","choices":[{"message":{"content":"hi"}}],"usage":{"prompt_tokens":5,"completion_tokens":1}}')
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=20)
        conn.request("POST", self.service.base_path + "/v1/chat/completions", json.dumps({"model": "cheap", "messages": []}),
                     {"Content-Type": "application/json", "User-Agent": "WorkBuddy/5.8.4", **auth})
        response = conn.getresponse()
        response.read()
        request_id = response.getheader("X-Client-Request-ID")
        conn.close()
        self.assertTrue(request_id)
        self.assertEqual(self.latest()["requestId"], request_id)
        self.assertEqual(FakeUpstream.calls[-1][1]["User-Agent"], "WorkBuddy/5.8.4")  # the client's UA, not Python-urllib
        self.assertNotIn("max_tokens", FakeUpstream.calls[-1][2])  # never injected on OpenAI-compatible routes
        status, _, retry = self.raw_call("/v1/chat/completions", {"model": "cheap"}, auth)
        FakeUpstream.reply = (429, "application/json", b'{"error":{"message":"slow"}}')
        with mock.patch.object(keys, "RETRY_DELAYS", ()):
            status, _, retry = self.raw_call("/v1/chat/completions", {"model": "cheap"}, auth)
        self.assertEqual(status, 429)
        FakeUpstream.reply = None
        # Output limits: existing OpenAI fields are clamped, Anthropic gets max_tokens and a valid thinking budget.
        self.call("/v1/chat/completions", {"model": "cheap", "max_completion_tokens": 10**7}, key)
        self.assertEqual(FakeUpstream.calls[-1][2]["max_completion_tokens"], keys.OUTPUT_CAP)
        self.call("/v1/messages", {"model": "claude-x", "max_tokens": 10**7, "thinking": {"type": "enabled", "budget_tokens": 10**7}}, key)
        sent = FakeUpstream.calls[-1][2]
        self.assertEqual((sent["max_tokens"], sent["thinking"]["budget_tokens"]), (keys.OUTPUT_CAP, keys.OUTPUT_CAP - 1))
        # Bodies: gzip accepted, CJK sent unescaped, NaN refused, the limit is 32 MB.
        import gzip
        packed = gzip.compress(json.dumps({"model": "cheap", "messages": [{"role": "user", "content": "你好"}]}, ensure_ascii=False).encode())
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=20)
        conn.request("POST", self.service.base_path + "/v1/chat/completions", packed,
                     {"Content-Type": "application/json", "Content-Encoding": "gzip", **auth})
        self.assertEqual(conn.getresponse().status, 200)
        conn.close()
        self.assertEqual(FakeUpstream.calls[-1][2]["messages"][0]["content"], "你好")
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=20)
        conn.request("POST", self.service.base_path + "/v1/chat/completions", b'{"model":"cheap","temperature":NaN}',
                     {"Content-Type": "application/json", **auth})
        self.assertEqual(conn.getresponse().status, 400)
        conn.close()
        self.assertEqual(keys.BODY_LIMIT, 32 * 1024 * 1024)
        passed = keys.passed_headers({"x-request-id": "up-1", "retry-after": "3", "anthropic-ratelimit-requests-remaining": "5",
                                      "x-ratelimit-remaining-tokens": "9", "openai-processing-ms": "12"})
        self.assertEqual(passed, {"x-request-id": "up-1", "retry-after": "3", "openai-processing-ms": "12"})
        # /v1/models in both formats, and a single model.
        status, listing = self.call("/v1/models", None, key, method="GET")
        listing = json.loads(listing)
        self.assertEqual((listing["object"], listing["has_more"], listing["first_id"]), ("list", False, "cheap"))
        self.assertEqual({"id", "object", "owned_by", "type", "display_name", "created_at", "created"}, set(listing["data"][0]))
        self.assertEqual(json.loads(self.call("/v1/models/claude-x", None, key, method="GET")[1])["id"], "claude-x")
        self.assertEqual(self.call("/v1/models/nope", None, key, method="GET")[0], 404)

    def test_responses_state_isolation_and_output_estimates(self):
        team_id, client, key = self.enabled_key()
        other = self.team(number=2, name="别的队")
        other_client = self.team_client(other)
        self.assertEqual(self.admin.post("/api/admin/keys/provision", {"teamId": other})[0], 200)
        other_key = other_client.post("/api/team/keys", {"name": "o"})[1]["key"]
        FakeUpstream.reply = (200, "application/json", b'{"id":"resp_team1","status":"completed","usage":{"input_tokens":5,"output_tokens":1}}')
        self.assertEqual(self.call("/v1/responses", {"model": "cheap", "input": "hi", "store": True}, key)[0], 200)
        self.assertIs(FakeUpstream.calls[-1][2]["store"], False)  # nothing is stored on the shared upstream account
        FakeUpstream.reply = None
        # So no stored state can be referenced, by any team.
        for extra in ({"previous_response_id": "resp_team1"}, {"input": [{"type": "item_reference", "id": "msg_x"}]},
                      {"background": True}, {"conversation": "conv_1"}, {"prompt": {"id": "pmpt_1"}}):
            for team_key in (key, other_key):
                self.assertEqual(self.call("/v1/responses", {"model": "cheap", "input": "hi", **extra}, team_key)[0], 400, extra)
        # Without a client limit, OpenAI-compatible routes estimate the full output cap (nothing injected upstream).
        service = self.service.keys
        seen = []
        real = service.recheck_access
        service.recheck_access = lambda handler, call: (seen.append(call["output_estimate"]), real(handler, call))[1]
        try:
            self.call("/v1/chat/completions", {"model": "cheap"}, key)
            self.call("/v1/chat/completions", {"model": "cheap", "max_tokens": 100}, key)
        finally:
            service.recheck_access = real
        self.assertEqual(seen, [keys.OUTPUT_CAP, 100])
        self.assertNotIn("max_tokens", FakeUpstream.calls[-2][2])

    def test_admin_tools_ported_from_sub2api(self):
        team_id, client, key = self.enabled_key()
        self.call("/v1/messages", {"model": "claude-x", "max_tokens": 100}, key)
        # Live metrics and storage on the overview.
        overview = self.admin.get("/api/admin/keys")[1]
        self.assertGreater(overview["overview"]["rpm"], 0)
        self.assertIsNotNone(overview["overview"]["p95Ms"])
        self.assertGreater(overview["storage"]["rows"], 0)
        # Hourly buckets and log filters (model, requested model, request id).
        stats = self.admin.post("/api/admin/keys/stats", {"days": 1, "granularity": "hour"})[1]
        self.assertRegex(stats["daily"][0]["day"], r"^\d{4}-\d{2}-\d{2} \d{2}:00$")
        self.call("/v1/messages", {"model": "claude-latest", "max_tokens": 10}, key)
        wanted = self.latest()["requestId"]
        self.assertEqual([i["requestId"] for i in self.admin.post("/api/admin/keys/logs", {"requestId": wanted})[1]["items"]], [wanted])
        found = self.admin.post("/api/admin/keys/logs", {"requestId": wanted})[1]["items"][0]
        self.assertEqual(self.admin.post("/api/admin/keys/log", {"id": found["id"]})[1]["requestId"], wanted)
        self.assertEqual(len(self.admin.post("/api/admin/keys/logs", {"model": "claude-latest"})[1]["items"]), 1)
        self.assertGreaterEqual(len(self.admin.post("/api/admin/keys/logs", {"model": "claude-x"})[1]["items"]), 2)
        # Upstream test and model list use the saved key, which never reaches the browser.
        tested = self.admin.get("/api/admin/keys/upstreams/test/anthropic")[1]
        self.assertEqual((tested["ok"], tested["status"], tested["model"]), (True, 200, "claude-x"))
        self.assertEqual(FakeUpstream.calls[-1][2]["max_tokens"], 1)
        listed = self.admin.get("/api/admin/keys/upstreams/models/openai")[1]
        self.assertEqual(listed["models"], ["model-a", "model-b"])
        self.assertEqual(FakeUpstream.calls[-1][1]["Authorization"], "Bearer sk-upstream-openai")
        self.assertNotIn("sk-upstream", json.dumps([tested, listed]))
        FakeUpstream.status = 401
        failed = self.admin.get("/api/admin/keys/upstreams/test/openai")[1]
        self.assertEqual((failed["ok"], failed["status"]), (False, 401))
        FakeUpstream.status = 200
        self.assertEqual(client.get("/api/admin/keys/upstreams/test/openai")[0], 403)
        # Request content: retention, size cap and clearing; call metadata (usage) is never deleted.
        calls_before = len(self.admin.post("/api/admin/keys/logs", {})[1]["items"])
        with closing(sqlite3.connect(self.root / "private" / "event.sqlite3")) as db:
            db.execute("UPDATE api_usage SET at=at-? WHERE id=(SELECT MIN(id) FROM api_usage)", (100 * 3600_000,))
            db.commit()
        with self.service.db() as db:
            settings = self.service.keys.settings(db)
            self.assertGreaterEqual(self.service.keys.prune_logs(db, settings), 1)  # older than 72 h
            self.assertEqual(self.service.keys.prune_logs(db, {**settings, "logMaxMB": 0}) >= 1, True)  # over the cap
        self.assertEqual(len(self.admin.post("/api/admin/keys/logs", {})[1]["items"]), calls_before)
        self.call("/v1/messages", {"model": "claude-x", "max_tokens": 10}, key)
        cleared = self.admin.post("/api/admin/keys/logs/clear", {})[1]
        self.assertGreaterEqual(cleared["deleted"], 1)
        self.assertEqual(self.admin.get("/api/admin/keys")[1]["storage"]["rows"], 0)
        for bad in ({"logRetentionHours": 0}, {"logMaxMB": 10}):
            self.assertEqual(self.admin.post("/api/admin/keys/settings", bad)[0], 400)

    def test_responses_api_for_codex(self):
        _, _, key = self.enabled_key()
        done = {"type": "response.completed", "response": {"id": "r1", "status": "completed",
                "usage": {"input_tokens": 1000, "output_tokens": 50, "input_tokens_details": {"cached_tokens": 400}}}}
        FakeUpstream.reply = (200, "text/event-stream", self.sse({"type": "response.created", "response": {"id": "r1"}},
                              {"type": "response.output_text.delta", "delta": "你好"}, done, anthropic=False))
        status, content = self.call("/v1/responses", {"model": "cheap", "stream": True, "input": "hi",
                                                      "tools": [{"type": "local_shell"}, {"type": "function", "name": "f"}]}, key)
        self.assertEqual(status, 200)
        self.assertEqual(FakeUpstream.calls[-1][0], "/v1/responses")
        self.assertNotIn("stream_options", FakeUpstream.calls[-1][2])  # chat-completions only
        item = self.latest()
        self.assertEqual((item["status"], item["incomplete"], item["input"], item["output"], item["cacheRead"]), (200, 0, 600, 50, 400))
        FakeUpstream.reply = (200, "text/event-stream", self.sse({"type": "response.output_text.delta", "delta": "x" * 400},
                              {"type": "response.failed", "response": {"error": {"message": "server overloaded"}}}, anthropic=False))
        self.call("/v1/responses", {"model": "cheap", "stream": True, "input": "hi"}, key)
        failed = self.latest()
        self.assertEqual((failed["status"], failed["incomplete"], failed["errorType"]), (502, 1, "overloaded_error"))
        FakeUpstream.reply = (200, "application/json", json.dumps(done["response"]).encode())
        self.call("/v1/responses", {"model": "cheap", "input": "hi"}, key)
        self.assertEqual((self.latest()["status"], self.latest()["input"]), (200, 600))
        FakeUpstream.reply = None
        self.assertEqual(self.call("/v1/responses", {"model": "cheap", "input": "hi", "tools": [{"type": "web_search"}]}, key)[0], 400)

    def test_stream_idle_timeout_ends_with_an_error_event(self):
        _, _, key = self.enabled_key()
        original = FakeUpstream.do_POST

        def stalls(handler):
            handler.rfile.read(int(handler.headers["Content-Length"]))
            handler.send_response(200)
            handler.send_header("Content-Type", "text/event-stream")
            handler.end_headers()
            handler.wfile.write(b'data: {"type":"message_start","message":{"usage":{"input_tokens":300,"output_tokens":1}}}\n\n')
            handler.wfile.flush()
            threading.Event().wait(2)
        FakeUpstream.do_POST = stalls
        idle, keys.STREAM_IDLE_TIMEOUT = keys.STREAM_IDLE_TIMEOUT, 0.5
        try:
            status, content = self.call("/v1/messages", {"model": "claude-x", "stream": True, "max_tokens": 100}, key)
        finally:
            FakeUpstream.do_POST, keys.STREAM_IDLE_TIMEOUT = original, idle
        self.assertIn(b"event: error", content)
        item = self.latest()
        self.assertEqual((item["status"], item["incomplete"], item["errorPhase"]), (504, 1, "network"))
        self.assertEqual(item["input"], 300)

    def test_stream_is_billed_when_the_client_disconnects(self):
        _, client, key = self.enabled_key()
        import socket
        body = json.dumps({"model": "claude-x", "stream": True}).encode()
        sock = socket.create_connection(("127.0.0.1", self.server.server_port))
        sock.sendall(b"POST " + self.service.base_path.encode() + b"/v1/messages HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                     b"Authorization: Bearer " + key.encode() + b"\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
        sock.recv(64)  # first bytes of the stream, then hang up early
        sock.close()
        for _ in range(50):
            items = client.get("/api/team/keys/logs")[1]["items"]
            if items:
                break
            threading.Event().wait(0.05)
        self.assertEqual((items[0]["input"], items[0]["output"]), (1000, 500))  # usage at the end of the stream still billed

    def test_burst_waits_for_a_free_slot(self):
        service = self.service.keys
        delay, keys.QUEUE_WAIT = keys.QUEUE_WAIT, 0.3
        try:
            service.inflight["T1"] = keys.TEAM_CONCURRENCY
            with self.assertRaises(keys.APIError):
                service.admit("T1", 0)  # times out while the team is full
            threading.Timer(0.1, service.release, ("T1",)).start()
            service.admit("T1", 0)  # a slot frees up during the wait
            self.assertEqual(service.inflight["T1"], keys.TEAM_CONCURRENCY)
        finally:
            keys.QUEUE_WAIT = delay

    def test_upstreams_are_managed_in_the_panel(self):
        status, data, _ = self.admin.get("/api/admin/keys")
        self.assertNotIn("sk-upstream", json.dumps(data))  # upstream keys never return to the browser
        self.assertEqual(data["upstreams"]["openai"]["keyHint"], "…enai")
        self.assertEqual(list(data["upstreams"]["anthropic"]["models"]), ["claude-x", "claude-small"])
        _, _, key = self.enabled_key()

        # Gitee-style switch: new URL and model list, key left blank keeps the saved one.
        gitee = {"openai": {"url": self.upstreams["openai"]["url"], "key": "", "models": {"DeepSeek-V3": [0.3, 1.2]}}}
        status, saved, _ = self.admin.post("/api/admin/keys/upstreams", {"upstreams": gitee})
        self.assertEqual(status, 200, saved)
        self.assertEqual(saved["models"], {"openai": ["DeepSeek-V3"]})  # anthropic removed
        self.assertEqual(self.call("/v1/chat/completions", {"model": "DeepSeek-V3"}, key)[0], 200)
        self.assertEqual(FakeUpstream.calls[-1][1]["Authorization"], "Bearer sk-upstream-openai")
        self.assertEqual(self.call("/v1/chat/completions", {"model": "cheap"}, key)[0], 404)
        self.assertEqual(self.call("/v1/messages", {"model": "claude-x"}, key)[0], 404)

        # Survives a restart: a new service reads the database, not gateway.json.
        restarted = test_api.EventService(data_dir=self.root / "private", origin=self.service.origin, static_root=self.root / "public")
        self.assertEqual(restarted.keys.models(), {"openai": ["DeepSeek-V3"]})

        bad = [{"anthropic": {"url": "https://x.test/v1", "key": "", "models": {"m": [1, 1]}}},  # new upstream needs a key
               {"openai": {"url": "http://10.0.0.5/v1", "key": "k", "models": {"m": [1, 1]}}},
               {"openai": {"url": "https://x.test/v1", "key": "k", "models": {}}},
               {"other": {"url": "https://x.test/v1", "key": "k", "models": {"m": [1, 1]}}}]
        for upstreams in bad:
            self.assertEqual(self.admin.post("/api/admin/keys/upstreams", {"upstreams": upstreams})[0], 400)
        self.assertEqual(self.admin.post("/api/admin/keys/upstreams", {"upstreams": {}})[0], 200)  # turn the gateway off
        self.assertFalse(self.admin.get("/api/admin/keys")[1]["enabled"])
        changes = [c for c in self.admin.get("/api/admin/keys")[1]["changes"] if c["action"] == "api-upstreams.save"]
        self.assertNotIn("sk-upstream", json.dumps(changes))

    def test_gateway_json_seeds_the_database_once(self):
        path = self.root / "seed" / "gateway.json"
        path.parent.mkdir()
        path.write_text(json.dumps({"openai": {"url": "https://ai.gitee.com/v1", "key": "seed-key", "models": {"Qwen3-8B": [0.1, 0.2]}}}))
        service = test_api.EventService(data_dir=self.root / "seed", origin=self.service.origin, static_root=self.root / "public")
        self.assertEqual(service.keys.models(), {"openai": ["Qwen3-8B"]})
        path.write_text(json.dumps({"openai": {"url": "https://ai.gitee.com/v1", "key": "other", "models": {"Other": [1, 1]}}}))
        again = test_api.EventService(data_dir=self.root / "seed", origin=self.service.origin, static_root=self.root / "public")
        self.assertEqual(again.keys.models(), {"openai": ["Qwen3-8B"]})  # the panel's copy wins after the first start

    def test_config_validation(self):
        bad = self.root / "bad.json"
        for config in [{"openai": {"url": "http://10.0.0.5/v1", "key": "k", "models": {"m": [1, 1]}}},
                       {"openai": {"url": "https://x.test/v1", "key": "k", "models": {"m": [1]}}},
                       {"other": {}},
                       {"anthropic": {"url": "https://x.test/v1", "key": "k", "models": {"m": [1, 1]}, "aliases": {"a": "missing"}}}]:
            bad.write_text(json.dumps(config))
            with self.assertRaises(ValueError):
                load_config(bad)


if __name__ == "__main__":
    unittest.main()
