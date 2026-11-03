"""Built-in AI gateway: team keys, metering and budgets against fake upstream providers."""
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import json
import sqlite3
import threading
import unittest

import test_api
from test_api import Client
import keys
from keys import load_config


class FakeUpstream(BaseHTTPRequestHandler):
    calls = []
    status = 200
    fail_first = 0  # answer 529 this many times before succeeding
    usage = None  # overrides the non-streaming usage object

    def log_message(self, *_):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeUpstream.calls.append((self.path, dict(self.headers), body))
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
            return
        usage = FakeUpstream.usage or ({"input_tokens": 2000, "output_tokens": 1000} if self.path.endswith("/messages") else {"prompt_tokens": 2000, "completion_tokens": 1000})
        payload = json.dumps({"id": "x", "type": "message", "role": "assistant", "model": body["model"], "content": [{"type": "text", "text": "hi"}],
                              "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}], "usage": usage}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class GatewayTests(unittest.TestCase):
    team = test_api.APITests.team  # reuse helpers without re-running APITests

    def setUp(self):
        FakeUpstream.calls, FakeUpstream.status, FakeUpstream.fail_first, FakeUpstream.usage = [], 200, 0, None
        self.delays, keys.RETRY_DELAYS = keys.RETRY_DELAYS, (0, 0)
        self.fake = ThreadingHTTPServer(("127.0.0.1", 0), FakeUpstream)
        threading.Thread(target=self.fake.serve_forever, daemon=True).start()
        test_api.APITests.setUp(self)
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
        client = Client(self.server)
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
        self.assertEqual(self.admin.post("/api/admin/keys/settings", {"maxKeys": 2, "dailyBudget": 1.5})[0], 200)
        self.assertNotIn("keys.maxKeys", self.public.get("/api/board")[1]["config"])
        self.assertEqual(self.admin.post("/api/admin/keys/provision", {"teamId": team_id})[0], 200)
        self.assertEqual(self.admin.post("/api/admin/keys/provision", {"teamId": team_id})[0], 409)

        status, created, _ = client.post("/api/team/keys", {"name": "alice"})
        self.assertEqual(status, 200)
        key = created["key"]
        self.assertTrue(key.startswith("gk-"))
        listing = client.get("/api/team/keys")[1]
        self.assertNotIn(key, json.dumps(listing))
        self.assertEqual(listing["budget"]["dailyLimit"], 1.5)
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

    def test_proxy_meters_both_upstreams_and_enforces_budget(self):
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

        status, content = self.call("/v1/messages", {"model": "claude-x", "stream": True}, headers={"x-api-key": key})
        self.assertEqual(status, 200)
        self.assertEqual(FakeUpstream.calls[-1][1]["X-Api-Key"], "sk-upstream-anthropic")
        self.assertEqual(self.call("/v1/messages", {"model": "cheap"}, key)[0], 400)  # model of the other upstream

        # 2000*1 + 1000*2 = 4000 micro-USD; 1000*1 + 500*2 = 2000; 1000*3 + 500*15 = 10500
        budget = client.get("/api/team/keys")[1]["budget"]
        self.assertAlmostEqual(budget["usedTotal"], 0.0165)
        self.assertEqual(client.get("/api/team/keys")[1]["keys"][0]["requests"], 4)  # the rejected model call counts too

        self.assertEqual(self.admin.post("/api/admin/keys/settings", {"dailyBudget": 0.01})[0], 200)
        self.assertEqual(self.call("/v1/chat/completions", {"model": "cheap"}, key)[0], 429)
        self.assertEqual(self.admin.post("/api/admin/keys/settings", {"dailyBudget": 5, "expiresAt": 1})[0], 200)
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
        self.assertEqual(self.call("/v1/chat/completions", {"model": "unknown"}, key)[0], 400)
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
        self.assertEqual([i["status"] for i in logs["items"]], [400, 200, 200])
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
        client = Client(self.server)
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

    def test_client_header_rules(self):
        _, client, key = self.enabled_key()
        rules = [{"action": "allow", "header": "User-Agent", "contains": "claude-cli"},
                 {"action": "deny", "header": "User-Agent", "contains": "openclaw"}]
        self.assertEqual(self.admin.post("/api/admin/keys/settings", {"clientRules": rules})[0], 200)
        body = {"model": "cheap"}
        self.assertEqual(self.call("/v1/chat/completions", body, key, headers={"User-Agent": "claude-cli/2.1 (external)"})[0], 200)
        status, content = self.call("/v1/chat/completions", body, key, headers={"User-Agent": "python-requests/2"})
        self.assertEqual(status, 403)
        self.assertIn("claude-cli".encode(), content)  # tells the team what is allowed
        self.assertEqual(self.call("/v1/chat/completions", body, key, headers={"User-Agent": "claude-cli openclaw"})[0], 403)
        self.assertEqual(self.call("/v1/models", None, key, method="GET", headers={"User-Agent": "curl/8"})[0], 403)
        blocked = self.admin.post("/api/admin/keys/logs", {})[1]["items"]
        self.assertEqual([i["status"] for i in blocked], [403, 403, 200])  # blocked attempts are visible to admins
        self.assertEqual(client.get("/api/team/keys")[1]["clientRules"], rules)
        for bad in [[{"action": "allow", "header": "Authorization", "contains": "x"}],
                    [{"action": "maybe", "header": "User-Agent", "contains": "x"}],
                    [{"action": "deny", "header": "User Agent", "contains": "x"}],
                    [{"action": "deny", "header": "User-Agent", "contains": ""}]]:
            self.assertEqual(self.admin.post("/api/admin/keys/settings", {"clientRules": bad})[0], 400)

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
        self.assertEqual(self.call("/v1/chat/completions", {"model": "=1+1", "messages": []}, key)[0], 400)

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
        self.assertNotIn("越狱", text)  # no request content in the export
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
        FakeUpstream.fail_first = 3
        self.assertEqual(self.call("/v1/chat/completions", {"model": "cheap"}, key)[0], 502)

        # Teams see their own call history, without content.
        status, own, _ = client.get("/api/team/keys/logs")
        self.assertEqual(status, 200)
        self.assertEqual(len(own["items"]), 8)  # count_tokens is recorded at zero cost
        counted = next(i for i in own["items"] if i["path"] == "/v1/messages/count_tokens")
        self.assertEqual((counted["cost"], counted["model"]), (0, "claude-x"))
        self.assertNotIn("request", own["items"][0])

    def test_budget_bypasses_are_closed(self):
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
        self.assertEqual(ok({"tools": [{"name": "t", "description": "越狱工具"}]}), 403)
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
        self.assertEqual((item["status"], item["incomplete"], item["estimated"]), (200, 1, 1))
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
        self.assertEqual(service.reserved[team_id], 0)

    def test_budget_reserves_in_flight_cost(self):
        team_id, _, key = self.enabled_key()
        self.assertEqual(self.admin.post("/api/admin/keys/settings", {"dailyBudget": 0.05})[0], 200)
        # claude-x output costs $15 / 1M tokens: 8192 default output tokens alone reserve ~$0.12 > $0.05.
        self.assertEqual(self.call("/v1/messages", {"model": "claude-x"}, key)[0], 429)
        self.assertEqual(self.call("/v1/messages", {"model": "claude-x", "max_tokens": 100}, key)[0], 200)
        self.assertEqual(FakeUpstream.calls[-1][2]["max_tokens"], 100)
        # The forwarded output limit never exceeds what was reserved.
        self.assertEqual(self.admin.post("/api/admin/keys/settings", {"dailyBudget": 50})[0], 200)
        self.call("/v1/messages", {"model": "claude-x"}, key)
        self.assertEqual(FakeUpstream.calls[-1][2]["max_tokens"], keys.RESERVE_OUTPUT_DEFAULT)  # injected when absent
        self.call("/v1/messages", {"model": "claude-x", "max_tokens": 10**9}, key)
        self.assertEqual(FakeUpstream.calls[-1][2]["max_tokens"], keys.RESERVE_OUTPUT_CAP)  # clamped
        self.call("/v1/chat/completions", {"model": "cheap", "max_completion_tokens": "lots"}, key)
        self.assertEqual(FakeUpstream.calls[-1][2]["max_completion_tokens"], keys.RESERVE_OUTPUT_CAP)  # non-integer
        self.assertNotIn("max_tokens", FakeUpstream.calls[-1][2])  # existing field used, none added
        before = len(FakeUpstream.calls)
        for body in [{"model": "cheap", "n": 4}, {"model": "cheap", "n": "2"}, {"model": "cheap", "best_of": 3}]:
            self.assertEqual(self.call("/v1/chat/completions", body, key)[0], 400, body)
        self.assertEqual(len(FakeUpstream.calls), before)  # multi-completion requests never reach the upstream
        self.assertEqual(self.call("/v1/chat/completions", {"model": "cheap", "n": 1}, key)[0], 200)
        self.assertEqual(self.admin.post("/api/admin/keys/settings", {"dailyBudget": 0.05})[0], 200)
        service = self.service.keys
        service.reserved[team_id] = 49_000  # $0.049 already held by another request in flight
        self.assertEqual(self.call("/v1/messages", {"model": "claude-x", "max_tokens": 100}, key)[0], 429)
        service.reserved[team_id] = 0

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
        self.assertEqual(self.call("/v1/chat/completions", {"model": "cheap"}, key)[0], 400)
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
