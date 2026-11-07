"""Real HTTP media transport and ambiguous/malicious multipart boundary regressions."""
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import socket
import sys
import threading
import time
import types
import unittest
from unittest import mock
import wave

from errors import APIError
import keys
from gateway_media import MediaGateway, parse_multipart, _wav_duration


def multipart(fields, boundary="test-boundary"):
    parts = []
    for name, value, filename in fields:
        disposition = 'Content-Disposition: form-data; name="' + name + '"'
        if filename is not None:
            disposition += '; filename="' + filename + '"'
        parts.append(b"--" + boundary.encode() + b"\r\n" + disposition.encode() + b"\r\n\r\n" +
                     (value.encode() if isinstance(value, str) else value) + b"\r\n")
    return b"".join(parts) + b"--" + boundary.encode() + b"--\r\n"


def wav():
    stream = io.BytesIO()
    with wave.open(stream, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\0\0" * 8000)
    return stream.getvalue()


class UploadTests(unittest.TestCase):
    def parse(self, fields):
        return parse_multipart(io.BytesIO(multipart(fields)), "multipart/form-data; boundary=test-boundary")

    def test_file_is_metadata_and_scalar_types_are_preserved(self):
        body = self.parse([("model", "speech", None), ("file", wav(), "C:\\private\\sound.wav"),
                           ("n", "2", None), ("stream", "false", None)])
        self.assertEqual(body["model"], "speech")
        self.assertEqual(body["file"]["filename"], "sound.wav")
        self.assertEqual(body["file"]["duration"], 1)
        self.assertEqual(body["n"], 2)
        self.assertIs(body["stream"], False)
        self.assertNotIn("RIFF", json.dumps(body))

    def test_wav_header_cannot_claim_frames_not_present(self):
        data = wav()[:-100]
        self.assertIsNone(_wav_duration(io.BytesIO(data), 0, len(data)))

    def test_duplicate_scalar_model_rejected(self):
        with self.assertRaises(APIError):
            self.parse([("model", "first", None), ("model", "second", None)])

    def test_repeated_image_array_supported(self):
        body = self.parse([("model", "edit", None), ("image[]", b"a", "a.png"), ("image[]", b"b", "b.png")])
        self.assertEqual(len(body["image[]"]), 2)

    def test_binary_containing_false_boundary_survives(self):
        data = b"a\r\n--test-boundaryNO\x00b" + b"z" * 150000
        body = self.parse([("model", "speech", None), ("file", data, "a.bin")])
        self.assertEqual(body["file"]["size"], len(data))

    def test_nested_file_reference_can_be_inspected(self):
        body = self.parse([("model", "edit", None), ("image", '{"file_id":"someone-else"}', None)])
        with self.assertRaises(APIError):
            MediaGateway._check_shared_state(body)

    def test_bracket_encoded_reference_rejected(self):
        with self.assertRaises(APIError):
            self.parse([("model", "edit", None), ("image[file_id]", "someone-else", None)])

    def test_malformed_json_field_rejected(self):
        with self.assertRaises(APIError):
            self.parse([("model", "edit", None), ("image", '{"file_id":', None)])

    def test_truncated_and_trailing_parts_rejected(self):
        raw = multipart([("model", "x", None)])
        for altered in (raw[:-9], raw + b"hidden", raw.replace(b'name="model"', b'name="model"; name="evil"')):
            with self.subTest(altered=altered[-15:]), self.assertRaises(APIError):
                parse_multipart(io.BytesIO(altered), "multipart/form-data; boundary=test-boundary")

    def test_multiple_boundaries_and_nested_mime_rejected(self):
        raw = multipart([("model", "x", None)])
        with self.assertRaises(APIError):
            parse_multipart(io.BytesIO(raw), "multipart/form-data; boundary=test-boundary; boundary=other")
        raw = raw.replace(b"\r\n\r\n", b"\r\nContent-Type: multipart/mixed; boundary=x\r\n\r\n", 1)
        with self.assertRaises(APIError):
            parse_multipart(io.BytesIO(raw), "multipart/form-data; boundary=test-boundary")


class Provider(BaseHTTPRequestHandler):
    calls = []
    reply = (200, "application/json", b'{"data":[],"usage":{"prompt_tokens":12,"total_tokens":12}}')
    drop = False

    def log_message(self, *_):
        pass

    def do_POST(self):
        raw = self.rfile.read(int(self.headers["Content-Length"]))
        type(self).calls.append((self.path, dict(self.headers), raw))
        if self.drop:
            self.connection.shutdown(socket.SHUT_RDWR)
            self.close_connection = True
            return
        status, mime, data = self.reply
        self.send_response(status)
        self.send_header("Content-Type", mime)
        if mime != "text/event-stream":
            self.send_header("Content-Length", str(len(data)))
        self.send_header("Set-Cookie", "upstream-private=hidden")
        self.end_headers()
        self.wfile.write(data)


class PricingError(ValueError):
    pass


class Catalog:
    def paths(self):
        return {"/v1/embeddings", "/v1/audio/speech", "/v1/audio/transcriptions", "/v1/async/images/generations",
                "/v1/completions", "/v1/chat/completions", "/v1/audio/speech/ws"}

    def resolve(self, model, path, body=None, headers=None):
        if model != "valid":
            raise PricingError("unknown model")
        return {"model": model, "path": path}


class Gateway(keys.KeyService):
    def __init__(self):
        self.opener = keys.make_opener({})
        self.records = []
        self.admitted = self.released = self.rechecked = 0
        self.config = {}

    def authenticate(self, handler, call=None):
        if handler.headers.get("Authorization") != "Bearer client-key":
            raise APIError(401, "invalid key")
        call["key"] = {"id": "key-1", "team_id": "team-1"}
        call["settings"] = {"teamRpm": 60, "blockedWords": []}
        call["log"] = True
        return call["key"]

    def admit(self, team, rpm, slot=True):
        self.admitted += 1

    def release(self, team):
        self.released += 1

    def recheck_access(self, handler, call):
        self.rechecked += 1

    def client_gone(self, handler):
        return False

    def safe_record(self, handler, call):
        if not call.get("recorded"):
            self.records.append(dict(call))
            call["recorded"] = True


class TransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.provider = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
        cls.thread = threading.Thread(target=cls.provider.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.provider.shutdown()
        cls.provider.server_close()
        cls.thread.join()

    def setUp(self):
        Provider.calls, Provider.drop = [], False
        Provider.reply = (200, "application/json", b'{"data":[],"usage":{"prompt_tokens":12,"total_tokens":12}}')
        self.gw = Gateway()
        self.media = MediaGateway(self.gw, Catalog())
        self.upstream = {"url": f"http://127.0.0.1:{self.provider.server_port}/v1", "key": "private-upstream-key", "models": {"valid": []}}
        self.quotes = []
        def quote(snapshot, **kwargs):
            self.quotes.append((snapshot, kwargs))
            return {"cost_micro": 70, "currency": "CNY", "estimated": False, "pending": False, "details": {}}
        self.pricing = mock.patch.dict(sys.modules, {"gateway_pricing": types.SimpleNamespace(quote=quote, PricingError=PricingError)})
        self.pricing.start()
        self.addCleanup(self.pricing.stop)

    def request(self, path="/v1/embeddings", data=None, mime="application/json", raw=None, auth="Bearer client-key", extra_headers=()):
        if raw is None:
            raw = json.dumps(data or {"model": "valid", "input": "hello"}).encode()
        headers = Message()
        for key, value in {"Content-Type": mime, "Content-Length": str(len(raw)), "Authorization": auth,
                           "Cookie": "client-cookie=do-not-send", "x-api-key": "do-not-send"}.items():
            headers[key] = value
        for key, value in extra_headers:
            headers[key] = value
        handler = types.SimpleNamespace(headers=headers, rfile=io.BytesIO(raw), wfile=io.BytesIO(),
                                        path=path, command="POST", connection=mock.Mock(), close_connection=False,
                                        client_address=("127.0.0.1", 12345))
        sent = []
        handler.headers_out = lambda *args, **kwargs: sent.append((args, kwargs))
        call = {"key": None, "path": path, "model": "", "stream": False, "price": None, "usage": [0] * 5,
                "status": 0, "request": b"", "response": [], "size": 0, "started": time.monotonic(), "log": False,
                "request_bytes": 0, "reply_bytes": 0, "done": False, "forwarded": False,
                "request_id": "local-request-1", "reply_headers": {}, "secrets": ()}
        self.last_call = call
        self.media.serve(handler, path.split("?", 1)[0], call, self.upstream)
        return call, handler.wfile.getvalue(), sent

    def test_json_model_route_and_credentials(self):
        call, raw, sent = self.request()
        self.assertEqual(call["usage"], [12, 0, 0, 0, 0])
        self.assertEqual(call["billing"]["cost_micro"], 70)
        self.assertEqual(Provider.calls[0][0], "/v1/embeddings")
        upstream_headers = {k.lower(): v for k, v in Provider.calls[0][1].items()}
        self.assertEqual(upstream_headers["authorization"], "Bearer private-upstream-key")
        self.assertNotIn("cookie", upstream_headers)
        self.assertNotIn("x-api-key", upstream_headers)
        self.assertNotIn("Set-Cookie", sent[0][1]["extra"])
        self.assertEqual(self.gw.admitted, self.gw.released)
        self.assertEqual(self.gw.rechecked, 1)

    def test_unknown_model_route_key_and_query_never_forward(self):
        cases = [{"data": {"model": "not-allowed"}}, {"path": "/v1/files"}, {"auth": "Bearer wrong"},
                 {"path": "/v1/embeddings?model=other"}]
        for kwargs in cases:
            with self.subTest(kwargs=kwargs), self.assertRaises(APIError):
                self.request(**kwargs)
        self.assertEqual(Provider.calls, [])

    def test_chat_and_websocket_are_not_media_http_routes(self):
        self.assertFalse(self.media.supports("/v1/chat/completions"))
        self.assertFalse(self.media.supports("/v1/audio/speech/ws"))

    def test_catalog_model_disabled_in_upstream_is_not_forwarded(self):
        self.upstream["models"] = {}
        with self.assertRaises(APIError):
            self.request()
        self.assertEqual(Provider.calls, [])

    def test_priced_routing_headers_match_wire_and_forbid_duplicate_selection(self):
        self.request(extra_headers=[("X-Compute-Id", "validated-compute"), ("X-Pipeline-Tag", "embeddings")])
        sent = {k.lower(): v for k, v in Provider.calls[0][1].items()}
        self.assertEqual(sent["x-compute-id"], "validated-compute")
        self.assertEqual(sent["x-pipeline-tag"], "embeddings")
        self.assertEqual(sent["x-failover-enabled"], "false")
        with self.assertRaises(APIError):
            self.request(extra_headers=[("X-Compute-Id", "cheap"), ("X-Compute-Id", "expensive")])
        self.assertEqual(len(Provider.calls), 1)

    def test_multipart_forwarded_byte_for_byte_with_metadata_only_log(self):
        raw = multipart([("model", "valid", None), ("file", wav(), "audio.wav")])
        call, _, _ = self.request("/v1/audio/transcriptions", raw=raw, mime="multipart/form-data; boundary=test-boundary")
        self.assertEqual(Provider.calls[0][2], raw)
        self.assertEqual(json.loads(call["request"])["file"]["duration"], 1)
        self.assertLess(len(call["request"]), 1000)

    def test_form_urlencoded_preserves_wire_and_metadata(self):
        raw = b"model=valid&input=hello%20world&stream=false"
        call, _, _ = self.request("/v1/audio/speech", raw=raw, mime="application/x-www-form-urlencoded")
        self.assertEqual(Provider.calls[0][2], raw)
        self.assertEqual(call["pricing_request"]["input"], "hello world")
        self.assertIs(call["stream"], False)

    def test_binary_is_billed_before_client_delivery_without_token_estimate(self):
        audio = wav()
        Provider.reply = (200, "audio/wav", audio)
        call, raw, _ = self.request("/v1/audio/speech")
        self.assertEqual(raw, audio)
        self.assertTrue(call["metered_media"])
        self.assertEqual(call["usage"], [0] * 5)
        self.assertEqual(self.quotes[-1][1]["response"]["duration"], 1)
        self.assertNotIn("RIFF", b"".join(call["response"]).decode())
        self.assertTrue(call["done"])

    def test_invalid_billing_evidence_preserves_output_with_pending_charge(self):
        import gateway_pricing
        with mock.patch.object(gateway_pricing, "quote", side_effect=PricingError("invalid provider billing")):
            call, raw, _ = self.request()
        self.assertEqual(json.loads(raw)["data"], [])
        self.assertTrue(call["billing"]["pending"])
        self.assertEqual(self.gw.admitted, self.gw.released)

    def test_sse_passes_through_and_collects_usage_before_quote(self):
        Provider.reply = (200, "text/event-stream", b'data: {"choices":[],"usage":{"prompt_tokens":9,"completion_tokens":3}}\n\ndata: [DONE]\n\n')
        call, raw, _ = self.request("/v1/completions", data={"model": "valid", "prompt": "hi", "stream": True})
        self.assertIn(b"[DONE]", raw)
        self.assertEqual(call["usage"][:2], [9, 3])
        self.assertEqual(self.quotes[-1][1]["usage"][:2], [9, 3])

    def test_provider_error_secret_redacted_and_never_retried(self):
        Provider.reply = (400, "application/json", b'{"error":{"message":"bad private-upstream-key"}}')
        with self.assertRaises(APIError) as error:
            self.request()
        self.assertNotIn("private-upstream-key", error.exception.message)
        self.assertEqual(len(Provider.calls), 1)
        self.assertEqual(self.last_call["billing"]["cost_micro"], 0)

    def test_submitted_connection_drop_is_pending_not_zero_or_retry(self):
        Provider.drop = True
        with self.assertRaises(APIError):
            self.request()
        self.assertEqual(len(Provider.calls), 1)
        self.assertTrue(self.last_call["billing"]["pending"])
        self.assertIsNone(self.last_call["billing"]["cost_micro"])
        self.assertEqual(self.gw.released, 1)

    def test_http_200_error_is_not_a_success(self):
        Provider.reply = (200, "application/json", b'{"error":{"message":"private-upstream-key"}}')
        with self.assertRaises(APIError) as error:
            self.request()
        self.assertNotIn("private-upstream-key", str(error.exception))
        self.assertFalse(self.last_call["forwarded"])

    def test_shared_file_and_task_references_never_forward(self):
        for body in ({"model": "valid", "image": {"file_id": "foreign"}},
                     {"model": "valid", "task_id": "foreign"}):
            with self.assertRaises(APIError):
                self.request(data=body)
        self.assertEqual(Provider.calls, [])

    def test_async_registration_precedes_client_response(self):
        Provider.reply = (200, "application/json", b'{"task_id":"upstream-secret-task","status":"waiting"}')
        def register(call, upstream, payload, **kwargs):
            self.assertEqual(payload["task_id"], "upstream-secret-task")
            call["recorded"] = True
            return {"task_id": "local-opaque-task", "status": "waiting"}
        self.media.tasks = types.SimpleNamespace(register=register)
        call, raw, _ = self.request("/v1/async/images/generations")
        self.assertEqual(json.loads(raw)["task_id"], "local-opaque-task")
        self.assertNotIn(b"upstream-secret-task", raw)
        self.assertEqual(self.gw.records, [])

    def test_async_storage_failure_does_not_leak_task_or_resubmit(self):
        Provider.reply = (200, "application/json", b'{"task_id":"upstream-secret-task","status":"waiting"}')
        def register(*args, **kwargs):
            raise APIError(503, "task storage temporarily unavailable")
        self.media.tasks = types.SimpleNamespace(register=register)
        with self.assertRaises(APIError):
            self.request("/v1/async/images/generations")
        self.assertEqual(len(Provider.calls), 1)
        self.assertEqual(self.last_call["response"], [])


class GatewayIntegrationTests(unittest.TestCase):
    """Exercise the real auth, route dispatcher, pricing and SQLite ledger together."""
    from test_keys import GatewayTests as _Base
    setUp = _Base.setUp
    tearDown = _Base.tearDown
    team = _Base.team
    team_client = _Base.team_client
    enabled_key = _Base.enabled_key
    call = _Base.call

    def enable_catalog(self):
        team_id, client, key = self.enabled_key()
        # The fake provider is local; only the real official host enables this in production.
        self.service.keys.is_gitee = lambda upstream: True
        self.service.keys.config["openai"]["models"].update({m: [0, 0] for m in ("ChatTTS", "FLUX.1-dev", "bge-m3", "glm-4-9b-chat")})
        return team_id, client, key

    def test_audio_charge_uses_cny_call_price_and_zero_tokens(self):
        from test_keys import FakeUpstream
        _, _, key = self.enable_catalog()
        FakeUpstream.reply = (200, "audio/wav", wav())
        status, data = self.call("/v1/audio/speech", {"model": "ChatTTS", "input": "hello"}, key)
        self.assertEqual(status, 200, data)
        self.assertEqual(data, wav())
        with self.service.db() as db:
            rows = db.execute("SELECT * FROM api_usage").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["currency"], "CNY")
        self.assertEqual(rows[0]["cost_micro"], 20000)
        self.assertEqual(rows[0]["input_tokens"] + rows[0]["output_tokens"], 0)
        self.assertEqual(rows[0]["billing_status"], "settled")

    def test_unknown_operation_and_foreign_task_are_rejected_before_upstream(self):
        from test_keys import FakeUpstream
        _, _, key = self.enable_catalog()
        status, data = self.call("/v1/audio/speech", {"model": "bge-m3", "input": "hello"}, key)
        self.assertEqual(status, 404, data)
        status, data = self.call("/v1/audio/speech", {"model": "ChatTTS", "input": "hello", "task_id": "foreign"}, key)
        self.assertEqual(status, 400, data)
        self.assertEqual(FakeUpstream.calls, [])

    def test_missing_embedding_usage_stays_pending_not_free(self):
        from test_keys import FakeUpstream
        _, _, key = self.enable_catalog()
        FakeUpstream.reply = (200, "application/json", b'{"object":"list","data":[]}')
        status, raw = self.call("/v1/embeddings", {"model": "bge-m3", "input": ["hello"] * 30}, key)
        self.assertEqual(status, 200, raw)
        with self.service.db() as db:
            row = db.execute("SELECT * FROM api_usage").fetchone()
        self.assertEqual(row["billing_status"], "pending")

    def test_image_count_forwarded_and_provider_actual_cost_wins(self):
        from test_keys import FakeUpstream
        _, _, key = self.enable_catalog()
        FakeUpstream.reply = (200, "application/json", json.dumps({"data": [{"url": "https://example.org/image"}] * 3}).encode())
        original = FakeUpstream.end_headers
        def actual_cost(provider):
            provider.send_header("Inference-Cost", "0.15")
            provider.send_header("Inference-Currency", "CNY")
            original(provider)
        with mock.patch.object(FakeUpstream, "end_headers", actual_cost):
            status, raw = self.call("/v1/images/generations", {"model": "FLUX.1-dev", "prompt": "blue", "n": 3,
                                                             "price": 0, "cost": 0}, key)
        self.assertEqual(status, 200, raw)
        self.assertEqual(FakeUpstream.calls[-1][2]["n"], 3)
        with self.service.db() as db:
            row = db.execute("SELECT * FROM api_usage").fetchone()
        self.assertEqual(row["cost_micro"], 150000)
        self.assertEqual(row["billing_status"], "settled")

    def test_sse_early_record_finalizes_actual_provider_cost_once(self):
        from test_keys import FakeUpstream
        _, _, key = self.enable_catalog()
        FakeUpstream.reply = (200, "text/event-stream", b'data: {"usage":{"prompt_tokens":100,"completion_tokens":50}}\n\ndata: [DONE]\n\n')
        original_headers = FakeUpstream.end_headers
        def actual_cost(provider):
            provider.send_header("Inference-Cost", "0.321")
            provider.send_header("Inference-Currency", "CNY")
            original_headers(provider)
        gateway = self.service.keys
        original_relay = gateway.relay_stream
        def record_inside_relay(handler, name, response, content_type, call):
            original_relay(handler, name, response, content_type, call)
            gateway.safe_record(handler, call)
        with mock.patch.object(FakeUpstream, "end_headers", actual_cost), mock.patch.object(gateway, "relay_stream", record_inside_relay):
            status, raw = self.call("/v1/completions", {"model": "glm-4-9b-chat", "prompt": "hi", "stream": True}, key)
        self.assertEqual(status, 200, raw)
        with self.service.db() as db:
            rows = db.execute("SELECT * FROM api_usage").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["cost_micro"], 321000)
        self.assertEqual(rows[0]["billing_status"], "settled")

    def test_async_submission_one_record_actual_price_and_local_identity(self):
        from test_keys import FakeUpstream
        _, _, key = self.enable_catalog()
        snapshot = json.dumps({"task_id": "remote-task-123", "status": "success",
                               "price": 0.25, "currency": "CNY", "output": {}}).encode()
        FakeUpstream.reply = (200, "application/json", snapshot)
        original_get = FakeUpstream.do_GET
        def get_task(provider):
            if provider.path != "/v1/task/remote-task-123":
                return original_get(provider)
            FakeUpstream.calls.append((provider.path, dict(provider.headers), None))
            provider.send_response(200)
            provider.send_header("Content-Type", "application/json")
            provider.send_header("Content-Length", str(len(snapshot)))
            provider.end_headers()
            provider.wfile.write(snapshot)
        # A successful submission is refreshed once before settling, so its
        # remote identity and final result use the same official task schema.
        with mock.patch.object(FakeUpstream, "do_GET", get_task):
            status, raw = self.call("/v1/async/images/generations", {"model": "FLUX.1-dev", "prompt": "blue square"}, key)
        self.assertEqual(status, 200, raw)
        task_id = json.loads(raw)["task_id"]
        self.assertTrue(task_id.startswith("tsk_"))
        self.assertNotIn(b"remote-task-123", raw)
        with self.service.db() as db:
            rows = db.execute("SELECT * FROM api_usage").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["cost_micro"], 250000)
        self.assertEqual(rows[0]["billing_status"], "settled")
        status, raw = self.call("/v1/task/" + task_id, None, key, method="GET")
        self.assertEqual(status, 200, raw)
        with self.service.db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM api_usage").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
