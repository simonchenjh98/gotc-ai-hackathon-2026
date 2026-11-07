"""RFC6455 frame boundaries and a real socket bridge with private upstream credentials."""
import base64
from email.message import Message
import hashlib
import http.client
import io
import json
import socket
import socketserver
import struct
import threading
import time
import types
import unittest
from unittest import mock

from errors import APIError
from gateway_websocket import GUID, ProtocolError, frame, messages, read_frame, validate_handshake, open_upstream, serve_websocket
from test_gateway_media import Gateway


def handshake_headers(auth=True):
    headers = Message()
    for k, v in {"Upgrade": "websocket", "Connection": "keep-alive, Upgrade", "Sec-WebSocket-Version": "13",
                 "Sec-WebSocket-Key": base64.b64encode(b"0123456789abcdef").decode()}.items():
        headers[k] = v
    if auth:
        headers["Authorization"] = "Bearer client-key"
    return headers


class FrameTests(unittest.TestCase):
    def test_roundtrip_short_extended_and_masked_frames(self):
        for size in (0, 1, 125, 126, 65535, 65536):
            for masked in (False, True):
                with self.subTest(size=size, masked=masked):
                    payload = b"x" * size
                    self.assertEqual(read_frame(io.BytesIO(frame(2, payload, masked)), masked), (True, 2, payload))

    def test_wrong_mask_reserved_opcode_and_noncanonical_lengths(self):
        invalid = [b"\x82\x80\0\0\0\0", b"\xc2\0", b"\x83\0", b"\x82\x7e\0\x01x",
                   b"\x82\x7f" + struct.pack("!Q", 2**63), b"\x09\x00"]
        for raw in invalid:
            with self.subTest(raw=raw), self.assertRaises(ProtocolError):
                read_frame(io.BytesIO(raw), False)

    def test_control_and_fragmented_messages(self):
        first = bytes([1, 2]) + b"he"
        continuation = bytes([128, 3]) + b"llo"
        stream = first + frame(9, b"ping") + continuation + frame(8, struct.pack("!H", 1000))
        self.assertEqual(list(messages(io.BytesIO(stream), False)), [(9, b"ping"), (1, b"hello"), (8, b"\x03\xe8")])

    def test_orphan_continuation_and_interleaved_fragments(self):
        for raw in (b"\x80\x01x", b"\x01\x01x\x81\x01y"):
            with self.assertRaises(ProtocolError):
                list(messages(io.BytesIO(raw), False))

    def test_invalid_close_codes_and_utf8_rejected(self):
        for data in (b"x", struct.pack("!H", 1005), struct.pack("!H", 1000) + b"\xff"):
            with self.assertRaises(ProtocolError):
                read_frame(io.BytesIO(frame(8, data)), False)

    def test_duplicate_handshake_fields_and_nonempty_body_rejected(self):
        h = types.SimpleNamespace(command="GET", headers=handshake_headers())
        validate_handshake(h)
        for name, value in (("Sec-WebSocket-Key", "another"), ("Content-Length", "1")):
            original = handshake_headers()
            original[name] = value
            with self.assertRaises(ProtocolError):
                validate_handshake(types.SimpleNamespace(command="GET", headers=original))


class Provider(socketserver.StreamRequestHandler):
    mode = "echo"
    calls = []
    frames = []

    def handle(self):
        self.connection.settimeout(3)
        line = self.rfile.readline().decode().rstrip()
        headers = http.client.parse_headers(self.rfile)
        type(self).calls.append((line, dict(headers)))
        if self.mode == "reject":
            self.wfile.write(b"HTTP/1.1 401 Unauthorized\r\nContent-Length: 0\r\n\r\n")
            return
        accept = base64.b64encode(hashlib.sha1((headers['Sec-WebSocket-Key'] + GUID).encode()).digest()).decode()
        response = ("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: " + accept + "\r\n")
        if self.mode == "bad-protocol":
            response += "Sec-WebSocket-Protocol: surprise\r\n"
        if not self.mode.startswith("billing-") or self.mode == "billing-error-zero":
            response += "Inference-Cost: " + ("0" if self.mode == "billing-error-zero" else "0.123") + "\r\nInference-Currency: CNY\r\n"
        response += "\r\n"
        if self.mode in ("immediate", "error-close"):
            content = frame(1, b'{"usage":{"prompt_tokens":10,"completion_tokens":2}}')
            reason = b"private-upstream-key" if self.mode == "error-close" else b"done"
            close_code = 1008 if self.mode == "error-close" else 1000
            # Early frame bytes share the handshake packet; HTTPResponse's buffered fp must retain them.
            self.wfile.write(response.encode() + content + frame(2, b"audio\0bytes") + frame(8, struct.pack("!H", close_code) + reason))
            return
        self.wfile.write(response.encode())
        self.wfile.flush()
        try:
            for opcode, payload in messages(self.rfile, True):
                type(self).frames.append((opcode, payload))
                if opcode in (1, 2):
                    if self.mode.startswith("billing-"):
                        if self.mode == "billing-disconnect":
                            return
                        event = b'{"error":{"message":"synthetic failure"}}' if self.mode in ("billing-error", "billing-error-zero") else b'{}'
                        code = 1008 if self.mode == "billing-error-close" else 1000
                        self.wfile.write(frame(1, event) + frame(8, struct.pack("!H", code)))
                        self.wfile.flush()
                        return
                    self.wfile.write(frame(1, b'{"usage":{"prompt_tokens":5,"completion_tokens":3}}') +
                                     frame(8, struct.pack("!H", 1000)))
                    self.wfile.flush()
                    return
        except (EOFError, OSError, ProtocolError):
            return


class ProviderServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class Catalog:
    def resolve(self, model, path, body):
        return {"model": model, "path": path}


class BridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ProviderServer(("127.0.0.1", 0), Provider)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        Provider.mode, Provider.calls, Provider.frames = "echo", [], []
        self.upstream = {"url": "http://127.0.0.1:" + str(self.server.server_address[1]) + "/v1",
                         "key": "private-upstream-key", "models": {"valid": [0, 0]}, "aliases": {}}
        self.gateway = Gateway()
        self.gateway.catalog = Catalog()
        self.quotes = []
        def quote(*args, **kwargs):
            self.quotes.append(kwargs)
            return {"cost_micro": 123000, "currency": "CNY", "pending": False, "estimated": False, "details": {}}
        self.patch = mock.patch("gateway_websocket.quote", quote)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def bridge(self, outgoing=None, browser_auth=False):
        server_side, client = socket.socketpair()
        server_side.settimeout(2)
        # Allow the bridge's bounded worker shutdown to finish before the test
        # reader times out on abrupt upstream disconnects.
        client.settimeout(5)
        headers = handshake_headers(not browser_auth)
        if browser_auth:
            headers["Sec-WebSocket-Protocol"] = "realtime, openai-insecure-api-key.client-key"
        self.handler = types.SimpleNamespace(command="GET", headers=headers, path="/v1/audio/speech/ws?model=valid",
                                              rfile=server_side.makefile("rb"), wfile=server_side.makefile("wb"),
                                              connection=server_side, close_connection=False)
        self.call = {"path": "/v1/audio/speech/ws", "requested_at": time.time(), "key": None,
                     "started": time.monotonic(), "model": "", "stream": False, "price": None, "usage": [0] * 5,
                     "status": 0, "request": b"", "response": [], "size": 0, "log": False,
                     "request_bytes": 0, "reply_bytes": 0, "done": False, "forwarded": False,
                     "request_id": "local-request-1", "reply_headers": {}, "secrets": ()}
        errors = []
        def run():
            try:
                serve_websocket(self.gateway, self.handler, "/v1/audio/speech/ws", self.call, self.upstream)
            except Exception as error:
                errors.append(error)
            finally:
                self.handler.wfile.close()
                self.handler.rfile.close()
                server_side.close()
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        reply = http.client.HTTPResponse(client)
        result = []
        try:
            reply.begin()
            self.assertEqual(reply.status, 101)
            if outgoing is not None:
                client.sendall(outgoing)
            try:
                result = list(messages(reply.fp, False))
            except EOFError:
                pass
        finally:
            try:
                client.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass  # macOS reports ENOTCONN once the bridge has closed its end.
            reply.close()
            client.close()
            thread.join(timeout=2)
        self.assertFalse(thread.is_alive(), "bridge or worker did not stop")
        self.assertEqual(errors, [])
        return result

    def test_early_upstream_frames_survive_http_handshake_buffer(self):
        Provider.mode = "immediate"
        result = self.bridge()
        self.assertEqual([op for op, _ in result], [1, 2, 8])
        self.assertEqual(result[1][1], b"audio\0bytes")
        self.assertEqual(self.call["usage"][:2], [10, 2])
        self.assertEqual(self.call["billing"]["cost_micro"], 123000)
        self.assertEqual(self.gateway.admitted, self.gateway.released)
        self.assertEqual(len(self.gateway.records), 1)

    def test_failed_speech_without_final_fee_is_pending_but_explicit_zero_settles(self):
        from gateway_pricing import Catalog as PriceCatalog
        self.patch.stop()  # Exercise actual pricing, rather than the fixed-fee stub.
        catalog = PriceCatalog()
        self.gateway.catalog = types.SimpleNamespace(resolve=lambda model, path, body:
            catalog.resolve("TeleTTS-Mandarin", path, body))
        for mode in ("billing-error", "billing-error-close", "billing-disconnect", "billing-error-zero", "billing-success"):
            with self.subTest(mode=mode):
                Provider.mode = mode
                self.bridge(frame(1, b'{"input":"hello world!"}', True))
                result = self.call["billing"]
                failed_without_cost = mode not in ("billing-success", "billing-error-zero")
                self.assertEqual(result["pending"], failed_without_cost)
                if failed_without_cost:
                    self.assertIsNone(result["cost_micro"])
                    self.assertFalse(result["estimated"])
                elif mode == "billing-error-zero":
                    self.assertEqual(result["cost_micro"], 0)
                    self.assertFalse(result["estimated"])
                else:
                    self.assertEqual(result["cost_micro"], 2880)
                    self.assertTrue(result["estimated"])
                self.assertEqual(self.gateway.admitted, self.gateway.released)

    def test_client_messages_masked_and_credentials_only_in_upstream_authorization(self):
        self.bridge(frame(1, b'{"model":"valid","input":"hello"}', True), browser_auth=True)
        first, headers = Provider.calls[0]
        self.assertEqual(first, "GET /v1/audio/speech/ws?model=valid HTTP/1.1")
        self.assertEqual(headers["Authorization"], "Bearer private-upstream-key")
        self.assertNotIn("client-key", json.dumps(headers))
        self.assertEqual(self.handler.headers["Sec-WebSocket-Protocol"], "realtime")
        self.assertEqual(Provider.frames[0][1], b'{"model":"valid","input":"hello"}')
        self.assertEqual(self.quotes[0]["trusted_quantities"]["characters"], 5)
        self.assertNotIn("input", self.quotes[0]["request"])

    def test_session_cannot_switch_model_or_reference_foreign_file(self):
        for payload in (b'{"model":"expensive"}', b'{"file_id":"foreign"}'):
            with self.subTest(payload=payload):
                self.bridge(frame(1, payload, True))
                self.assertEqual(Provider.frames, [])
                self.assertEqual(self.call["status"], 502)

    def test_binary_frame_rechecks_revoked_key(self):
        count = 0
        def check(handler, call):
            nonlocal count
            count += 1
            if count > 1:
                raise APIError(401, "revoked")
        self.gateway.recheck_access = check
        self.bridge(frame(2, b"audio data", True))
        self.assertEqual(count, 2)
        self.assertEqual(Provider.frames, [])

    def test_provider_close_error_reason_redacted(self):
        Provider.mode = "error-close"
        result = self.bridge()
        self.assertNotIn(b"private-upstream-key", result[-1][1])
        self.assertFalse(self.call["done"])
        self.assertEqual(self.call["status"], 502)

    def test_invalid_billing_evidence_never_leaks_a_team_concurrency_slot(self):
        from gateway_pricing import PricingError
        Provider.mode = "immediate"
        with mock.patch("gateway_websocket.quote", side_effect=PricingError("invalid provider cost")):
            self.bridge()
        self.assertEqual(self.gateway.admitted, self.gateway.released)
        self.assertTrue(self.call["billing"]["pending"])
        self.assertEqual(len(self.gateway.records), 1)

    def test_rejected_or_invalid_upstream_handshake_has_no_101(self):
        for mode in ("reject", "bad-protocol"):
            Provider.mode = mode
            with self.subTest(mode=mode), self.assertRaises(ProtocolError):
                open_upstream(self.upstream, "/v1/audio/speech/ws", "valid", [])


if __name__ == "__main__":
    unittest.main()


class UpstreamTlsTests(unittest.TestCase):
    def test_https_upstream_refuses_tls_below_1_2(self):
        import ssl
        from unittest import mock
        import gateway_websocket
        raw, context = mock.MagicMock(), mock.MagicMock()
        context.wrap_socket.side_effect = OSError('stop after TLS setup')
        with mock.patch.object(gateway_websocket.socket, 'create_connection', return_value=raw), \
                mock.patch.object(gateway_websocket.ssl, 'create_default_context', return_value=context):
            with self.assertRaises(OSError):
                gateway_websocket.open_upstream({'url': 'https://upstream.example/v1', 'key': 'synthetic'}, '/v1/realtime', 'm', [])
        self.assertEqual(context.minimum_version, ssl.TLSVersion.TLSv1_2)
        context.wrap_socket.assert_called_once_with(raw, server_hostname='upstream.example')
