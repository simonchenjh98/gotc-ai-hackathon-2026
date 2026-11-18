"""Real HTTP regressions for failures while parsing an upstream response."""
from contextlib import contextmanager, redirect_stderr
import io
import json
from unittest import mock
import unittest

import test_keys


class GatewayProtocolErrorTests(unittest.TestCase):
    setUp = test_keys.GatewayTests.setUp
    tearDown = test_keys.GatewayTests.tearDown
    team = test_keys.GatewayTests.team
    team_client = test_keys.GatewayTests.team_client
    enabled_key = test_keys.GatewayTests.enabled_key
    call = test_keys.GatewayTests.call
    latest = test_keys.GatewayTests.latest
    assertReleased = test_keys.GatewayTests.assertReleased

    malformed_replies = {
        "bad status line": b"NOT HTTP\r\n\r\n",
        "overlong header": b"HTTP/1.1 200 OK\r\nX-Trace: " + b"x" * 70000 + b"\r\n\r\n",
    }

    @contextmanager
    def upstream_reply(self, payload):
        received = []

        def respond(handler):
            # The whole request reached the provider before its malformed reply.
            body = handler.rfile.read(int(handler.headers["Content-Length"]))
            received.append((handler.path, json.loads(body)))
            handler.wfile.write(payload)
            handler.wfile.flush()
            handler.close_connection = True

        with mock.patch.object(test_keys.FakeUpstream, "do_POST", respond), redirect_stderr(io.StringIO()):
            yield received

    def usage_rows(self):
        with self.service.db() as db:
            return db.execute("SELECT COUNT(*) FROM api_usage").fetchone()[0]

    def test_malformed_generation_replies_are_charged_once_and_classified_as_provider_errors(self):
        team_id, client, key = self.enabled_key()
        billed = 0
        failures = 0
        for label, reply in self.malformed_replies.items():
            for path, model in (("/v1/messages", "claude-x"), ("/v1/chat/completions", "cheap"), ("/v1/responses", "cheap")):
                for stream in (False, True):
                    with self.subTest(reply=label, path=path, stream=stream):
                        before_rows = self.usage_rows()
                        body = {"model": model, "stream": stream}
                        if path == "/v1/responses":
                            body.update(input="hello", max_output_tokens=1000)
                        else:
                            body.update(messages=[{"role": "user", "content": "hello"}], max_tokens=1000)
                        with self.upstream_reply(reply) as received:
                            status, _ = self.call(path, body, key)
                        self.assertEqual(len(received), 1, "A post-send failure must not retry a generation")
                        self.assertEqual(received[0][0], path)
                        self.assertEqual(self.usage_rows(), before_rows + 1, "One request has one final usage row")
                        item = self.latest()
                        self.assertEqual((status, item["status"], item["errorOwner"]), (502, 502, "provider"))
                        self.assertEqual((item["incomplete"], item["estimated"], item["output"]), (1, 1, 1000))
                        self.assertGreater(item["cost"], 0)
                        billed += item["cost"]
                        failures += 1
                        self.assertAlmostEqual(client.get("/api/team/keys")[1]["usage"]["used24h"], billed)
                        self.assertEqual(self.admin.get("/api/admin/keys")[1]["overview"]["errors"], failures)
                        detail = self.admin.post("/api/admin/keys/log", {"id": item["id"]})[1]["response"]
                        self.assertIn("请求已发出，按估算用量计费", detail)
                        self.assertReleased(team_id)
                        self.assertEqual(self.service.keys.inflight[team_id], 0)

    def test_malformed_count_tokens_reply_remains_free(self):
        team_id, client, key = self.enabled_key()
        for label, reply in self.malformed_replies.items():
            with self.subTest(reply=label):
                before_rows = self.usage_rows()
                with self.upstream_reply(reply) as received:
                    status, _ = self.call("/v1/messages/count_tokens", {"model": "claude-x", "messages": []}, key)
                self.assertEqual(len(received), 1)
                self.assertEqual(self.usage_rows(), before_rows + 1)
                item = self.latest()
                self.assertEqual((status, item["status"], item["errorOwner"]), (502, 502, "provider"))
                self.assertEqual((item["cost"], item["incomplete"], item["estimated"]), (0, 0, 0))
                self.assertEqual(client.get("/api/team/keys")[1]["usage"]["used24h"], 0)
                self.assertReleased(team_id)
                self.assertEqual(self.service.keys.counting[team_id], 0)

    def test_invalid_upstream_url_is_not_charged_before_any_request_is_sent(self):
        team_id, client, key = self.enabled_key()
        upstreams = {name: {**config, "url": config["url"] + "/invalid path"}
                     for name, config in self.upstreams.items()}
        self.assertEqual(self.admin.post("/api/admin/keys/upstreams", {"upstreams": upstreams})[0], 200)
        for path, model in (("/v1/messages", "claude-x"), ("/v1/chat/completions", "cheap"), ("/v1/responses", "cheap")):
            with self.subTest(path=path):
                before_rows = self.usage_rows()
                # http.client raises InvalidURL locally, before opening a connection.
                # It is an HTTPException, but unlike bad response syntax it proves no upstream work occurred.
                with redirect_stderr(io.StringIO()):
                    status, _ = self.call(path, {"model": model, "max_tokens": 1000}, key)
                self.assertEqual(test_keys.FakeUpstream.calls, [])
                self.assertEqual(self.usage_rows(), before_rows + 1)
                item = self.latest()
                self.assertEqual((item["cost"], item["estimated"], item["incomplete"]), (0, 0, 0))
                self.assertEqual((status, item["status"], item["errorOwner"]), (500, 500, "platform"))
                self.assertEqual(client.get("/api/team/keys")[1]["usage"]["used24h"], 0)
                self.assertReleased(team_id)
                self.assertEqual(self.service.keys.inflight[team_id], 0)


if __name__ == "__main__":
    unittest.main()
