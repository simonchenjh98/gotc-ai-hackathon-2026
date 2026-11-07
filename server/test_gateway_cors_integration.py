"""Real HTTP origin gates with temporary data and a loopback fake upstream only."""
import base64
import http.client
import json
import unittest
from unittest import mock

import test_api
import test_keys


class GatewayCorsIntegrationTests(unittest.TestCase):
    team = test_api.APITests.team
    team_client = test_keys.GatewayTests.team_client
    enabled_key = test_keys.GatewayTests.enabled_key

    def setUp(self):
        test_keys.GatewayTests.setUp(self)
        self.team_id, self.team_browser, self.gateway_key = self.enabled_key()

    def tearDown(self):
        test_keys.GatewayTests.tearDown(self)

    def usage_count(self):
        with self.service.db() as db:
            return db.execute("SELECT COUNT(*) FROM api_usage").fetchone()[0]

    def request(self, method, path, body=None, *, raw=None, headers=(), authenticated=True):
        """Keep separate header fields intact so duplicate-Origin cases are real."""
        payload = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        fields = list(headers)
        names = {name.lower() for name, _ in fields}
        if authenticated:
            fields.append(("Authorization", "Bearer " + self.gateway_key))
        if payload is not None:
            if "content-type" not in names:
                fields.append(("Content-Type", "application/json"))
            if "content-length" not in names:
                fields.append(("Content-Length", str(len(payload))))
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            connection.putrequest(method, self.service.base_path + path)
            for name, value in fields:
                connection.putheader(name, value)
            connection.endheaders(payload)
            response = connection.getresponse()
            data = response.read()
            return response.status, data, {k.lower(): v for k, v in response.getheaders()}
        finally:
            connection.close()

    def assert_no_credentials(self, headers):
        self.assertNotIn("access-control-allow-credentials", headers)
        self.assertNotIn("set-cookie", headers)
        for field in ("access-control-allow-origin", "access-control-allow-headers", "access-control-allow-methods"):
            self.assertNotIn("*", headers.get(field, ""))

    def test_untrusted_sources_are_rejected_for_every_gateway_transport_before_dispatch(self):
        speech_form = (b"--boundary\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\ncheap\r\n"
                       b"--boundary\r\nContent-Disposition: form-data; name=\"file\"; filename=\"tiny.wav\"\r\n"
                       b"Content-Type: audio/wav\r\n\r\nRIFFfake\r\n--boundary--\r\n")
        ws_headers = [("Upgrade", "websocket"), ("Connection", "Upgrade"), ("Sec-WebSocket-Version", "13"),
                      ("Sec-WebSocket-Key", base64.b64encode(b"0123456789abcdef").decode())]
        routes = [
            ("chat", "POST", "/v1/chat/completions", {"model": "cheap", "messages": []}, None, []),
            ("sse", "POST", "/v1/chat/completions", {"model": "cheap", "messages": [], "stream": True}, None, []),
            ("responses", "POST", "/v1/responses", {"model": "cheap", "input": "hello"}, None, []),
            ("anthropic", "POST", "/v1/messages", {"model": "claude-x", "messages": [], "max_tokens": 2}, None, []),
            ("count_tokens", "POST", "/v1/messages/count_tokens", {"model": "claude-x", "messages": []}, None, []),
            ("embedding", "POST", "/v1/embeddings", {"model": "cheap", "input": ["hello"]}, None, []),
            ("upload", "POST", "/v1/audio/transcriptions", None, speech_form,
             [("Content-Type", "multipart/form-data; boundary=boundary")]),
            ("async_submit", "POST", "/v1/async/images/generations", {"model": "cheap", "prompt": "test"}, None, []),
            ("models", "GET", "/v1/models", None, None, []),
            ("model_detail", "GET", "/v1/models/cheap", None, None, []),
            ("task", "GET", "/v1/task/synthetic-task", None, None, []),
            ("task_cancel", "POST", "/v1/task/synthetic-task/cancel", {}, None, []),
            ("speech_ws", "GET", "/v1/audio/speech/ws?model=cheap", None, None, ws_headers),
            ("asr_ws", "GET", "/v1/audio/transcriptions/ws?model=cheap", None, None, ws_headers),
            ("encoded_path", "POST", "/%76%31/chat/completions", {"model": "cheap", "messages": []}, None, []),
        ]
        sources = [
            ("external", [("Origin", "https://external.invalid")]),
            ("opaque", [("Origin", "null")]),
            ("empty", [("Origin", "")]),
            ("duplicate_same", [("Origin", self.service.origin), ("origin", self.service.origin)]),
            ("duplicate_external", [("Origin", self.service.origin), ("Origin", "https://external.invalid")]),
            ("cross_site", [("Origin", self.service.origin), ("Sec-Fetch-Site", "cross-site")]),
            ("cross_site_without_origin", [("Sec-Fetch-Site", "cross-site")]),
            ("same_site", [("Origin", self.service.origin), ("Sec-Fetch-Site", "same-site")]),
            ("duplicate_fetch_site", [("Sec-Fetch-Site", "same-origin"), ("sec-fetch-site", "same-origin")]),
        ]
        before = self.usage_count()
        with mock.patch.object(self.service.keys, "gateway", wraps=self.service.keys.gateway) as dispatch:
            for label, method, path, body, raw, transport_headers in routes:
                for source, source_headers in sources:
                    with self.subTest(transport=label, source=source):
                        status, data, headers = self.request(method, path, body, raw=raw,
                                                             headers=transport_headers + source_headers)
                        self.assertEqual(status, 403)
                        self.assertEqual(json.loads(data)["code"], "origin_not_allowed")
                        self.assertNotIn("access-control-allow-origin", headers)
                        self.assert_no_credentials(headers)
            dispatch.assert_not_called()
        self.assertEqual(test_keys.FakeUpstream.calls, [])
        self.assertEqual(self.usage_count(), before)

    def test_cross_site_upload_is_rejected_without_waiting_for_its_declared_body(self):
        before = self.usage_count()
        # Send only headers, deliberately withholding the promised body. A guard
        # after read_body would block until the short client timeout and fail.
        status, data, headers = self.request("POST", "/v1/audio/transcriptions", headers=[
            ("Origin", "https://external.invalid"),
            ("Content-Type", "multipart/form-data; boundary=missing"), ("Content-Length", "1048576")])
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(data)["code"], "origin_not_allowed")
        self.assertEqual(test_keys.FakeUpstream.calls, [])
        self.assertEqual(self.usage_count(), before)

    def test_same_origin_and_originless_sdk_reach_real_upstream_for_json_and_sse(self):
        for browser in (False, True):
            for stream in (False, True):
                with self.subTest(browser=browser, stream=stream):
                    fields = [("Origin", self.service.origin), ("Sec-Fetch-Site", "same-origin")] if browser else []
                    status, data, headers = self.request("POST", "/v1/chat/completions", {
                        "model": "cheap", "messages": [{"role": "user", "content": "hello"}], "stream": stream}, headers=fields)
                    self.assertEqual(status, 200)
                    if browser:
                        self.assertEqual(headers["access-control-allow-origin"], self.service.origin)
                        self.assertIn("Origin", headers["vary"])
                    else:
                        self.assertNotIn("access-control-allow-origin", headers)
                    self.assert_no_credentials(headers)
                    self.assertIn(b"[DONE]", data) if stream else self.assertEqual(json.loads(data)["choices"][0]["message"]["content"], "hi")
        self.assertEqual(len(test_keys.FakeUpstream.calls), 4)
        self.assertEqual(self.usage_count(), 4)

    def test_exact_same_origin_still_requires_key_and_cookies_cannot_replace_it(self):
        for cookie in (None, self.admin.cookie, self.team_browser.cookie):
            with self.subTest(cookie_present=cookie is not None):
                fields = [("Origin", self.service.origin)] + ([("Cookie", cookie)] if cookie else [])
                status, _, headers = self.request("POST", "/v1/chat/completions", {
                    "model": "cheap", "messages": []}, headers=fields, authenticated=False)
                self.assertEqual(status, 401)
                self.assertEqual(headers["access-control-allow-origin"], self.service.origin)
                self.assert_no_credentials(headers)
        self.assertEqual(test_keys.FakeUpstream.calls, [])
        self.assertEqual(self.usage_count(), 0)

    def test_preflight_is_keyless_finite_and_does_not_dispatch_or_bill(self):
        fields = [("Origin", self.service.origin), ("Sec-Fetch-Site", "same-origin"),
                  ("Access-Control-Request-Method", "POST"),
                  ("Access-Control-Request-Headers", "authorization, Content-Type, X-API-Key, anthropic-version")]
        with mock.patch.object(self.service.keys, "gateway", wraps=self.service.keys.gateway) as dispatch:
            for path in ("/v1/chat/completions", "/%76%31/audio/transcriptions", "/v1/async/images/generations"):
                with self.subTest(path=path):
                    status, data, headers = self.request("OPTIONS", path, headers=fields, authenticated=False)
                    self.assertEqual(status, 204)
                    self.assertEqual(data, b"")
                    self.assertEqual(headers["access-control-allow-origin"], self.service.origin)
                    methods = {v.strip() for v in headers["access-control-allow-methods"].split(",")}
                    allowed = {v.strip().lower() for v in headers["access-control-allow-headers"].split(",")}
                    self.assertIn("POST", methods)
                    self.assertLessEqual(methods, {"GET", "HEAD", "POST", "OPTIONS"})
                    self.assertLessEqual({"authorization", "content-type", "x-api-key", "anthropic-version"}, allowed)
                    self.assertNotIn("cookie", allowed)
                    self.assertNotIn("x-untrusted", allowed)
                    self.assert_no_credentials(headers)
            dispatch.assert_not_called()
        self.assertEqual(test_keys.FakeUpstream.calls, [])
        self.assertEqual(self.usage_count(), 0)

    def test_preflight_rejects_disallowed_source_method_headers_and_duplicates(self):
        base = [("Origin", self.service.origin), ("Access-Control-Request-Method", "POST")]
        cases = [
            ("external", [("Origin", "https://external.invalid"), ("Access-Control-Request-Method", "POST")], "origin_not_allowed"),
            ("opaque", [("Origin", "null"), ("Access-Control-Request-Method", "POST")], "origin_not_allowed"),
            ("no_origin", [("Access-Control-Request-Method", "POST")], "cors_preflight_not_allowed"),
            ("unknown_method", [("Origin", self.service.origin), ("Access-Control-Request-Method", "DELETE")], "cors_preflight_not_allowed"),
            ("unknown_header", base + [("Access-Control-Request-Headers", "authorization, x-untrusted")], "cors_preflight_not_allowed"),
            ("cookie_header", base + [("Access-Control-Request-Headers", "cookie")], "cors_preflight_not_allowed"),
            ("duplicate_origin", base + [("Origin", self.service.origin)], "origin_not_allowed"),
            ("duplicate_method", base + [("Access-Control-Request-Method", "GET")], "cors_preflight_not_allowed"),
            ("duplicate_headers", base + [("Access-Control-Request-Headers", "authorization"), ("access-control-request-headers", "content-type")], "cors_preflight_not_allowed"),
        ]
        with mock.patch.object(self.service.keys, "gateway", wraps=self.service.keys.gateway) as dispatch:
            for label, fields, code in cases:
                with self.subTest(case=label):
                    status, data, headers = self.request("OPTIONS", "/v1/chat/completions", headers=fields, authenticated=False)
                    self.assertEqual(status, 403)
                    self.assertEqual(json.loads(data)["code"], code)
                    self.assertNotIn("access-control-allow-methods", headers)
                    self.assertNotIn("access-control-allow-headers", headers)
                    self.assert_no_credentials(headers)
            dispatch.assert_not_called()
        self.assertEqual(test_keys.FakeUpstream.calls, [])
        self.assertEqual(self.usage_count(), 0)

    def test_allowed_encoded_chat_and_models_use_the_canonical_gateway_path(self):
        fields = [("Origin", self.service.origin)]
        status, data, headers = self.request("POST", "/%76%31/%63hat/completions", {
            "model": "cheap", "messages": [{"role": "user", "content": "hello"}]}, headers=fields)
        self.assertEqual(status, 200)
        self.assertEqual(headers["access-control-allow-origin"], self.service.origin)
        status, data, headers = self.request("GET", "/v1/models", headers=fields)
        self.assertEqual(status, 200)
        self.assertTrue(any(model["id"] == "cheap" for model in json.loads(data)["data"]))
        self.assertEqual(headers["access-control-allow-origin"], self.service.origin)
        self.assertEqual(len(test_keys.FakeUpstream.calls), 1)
        self.assertEqual(self.usage_count(), 1)


if __name__ == "__main__":
    unittest.main()
