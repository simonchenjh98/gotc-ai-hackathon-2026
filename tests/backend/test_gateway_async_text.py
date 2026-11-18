"""HTTPX text-provider contracts using in-process synthetic transport and real ASGI request bodies."""
import asyncio
import gzip
import json
import time
import unittest
import weakref
import zlib
from contextlib import asynccontextmanager
from email.message import Message
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from starlette.requests import Request

from backend.gotc.errors import APIError
from backend.gotc.gateway import async_text, requests
from backend.gotc.gateway.memory import BULK_BYTES
from backend.gotc.gateway.async_text import request_chunks, serve_text
from backend.gotc.gateway.protocol_errors import GatewayError
from backend.gotc.gateway.transport import AsyncProvider


class Context:
    def __init__(self, url, path, payload, result, extra_headers=None):
        self.out = []
        self.recorded = self.attempts = 0
        self.call = {"path": path, "usage": [0] * 5, "secrets": (), "stream": False, "log": True, "reply_bytes": 0,
                     "started": time.monotonic(), "forwarded": False, "done": False, "settings": {"blockedWords": []}}
        config = {kind: {"url": url, "key": "synthetic-private", "models": {"model-x": [1, 2]}, "aliases": {}}
                  for kind in ("openai", "anthropic")}
        self.config = config
        self.bulk_reservations = 0
        self.gateway = SimpleNamespace(config=config, is_gitee=lambda _: False, error_secrets=lambda call: call["secrets"],
                                       note_error=lambda call, error: call.update(status=error.status),
                                       keep=lambda call, wire: None)
        headers = Message()
        for key, value in {"Content-Type": "application/json", "Content-Length": str(len(payload)),
                           **(extra_headers or {})}.items():
            headers[key] = value
        self.handler = SimpleNamespace(command="POST", headers=headers)

        async def receive():
            return {"type": "http.request", "body": payload}
        self.request = Request({"type": "http", "headers": []}, receive)

        async def respond(request):
            self.attempts += 1
            self.provider_request = request
            return result(request)
        self.upstream = AsyncProvider(proxies={}, transport=httpx.MockTransport(respond))

    @asynccontextmanager
    async def admitted(self, count_tokens=False, defer_rpm=False):
        yield

    async def reserve_bulk_memory(self):
        self.bulk_reservations += 1

    def release_bulk_memory(self):
        pass

    async def charge_rpm(self):
        pass

    async def recheck(self):
        pass

    async def sync(self, function, *args):
        return function(*args)

    async def record(self):
        self.recorded += 1

    async def send(self, status, content_type, payload=None, length=None):
        self.out.append((status, payload))


class TextTests(unittest.IsolatedAsyncioTestCase):
    def ctx(self, path, payload, result, headers=None):
        ctx = Context("https://provider.example.invalid/v1", path, payload, result, headers)
        self.addAsyncCleanup(ctx.upstream.aclose)
        return ctx

    async def test_chat_request_usage_json_response_and_header_allowlist(self):
        raw = b'{ "model": "model-x", "messages": [], "service_tier": "priority" }'
        response = b'{ "choices": [], "usage": {"prompt_tokens":4,"completion_tokens":2} }'
        ctx = self.ctx("/v1/chat/completions", gzip.compress(raw),
                       lambda req: httpx.Response(200, content=response, headers={"X-Request-ID": "id-synthetic-private",
                                                  "Set-Cookie": "private", "Content-Type": "application/json"}),
                       {"Content-Encoding": "gzip", "Authorization": "Bearer team-private"})
        await serve_text(ctx, ctx.call["path"])
        self.assertEqual(ctx.out, [(200, response)])
        self.assertEqual(ctx.call["request"], raw)
        self.assertEqual(ctx.call["usage"][:2], [4, 2])
        self.assertEqual(ctx.call["reply_headers"], {"x-request-id": "id-***"})
        self.assertEqual(ctx.provider_request.headers["authorization"], "Bearer synthetic-private")
        self.assertEqual(json.loads(ctx.provider_request.content), {"model": "model-x", "messages": []})

    async def test_responses_cannot_reference_shared_conversation(self):
        ctx = self.ctx("/v1/responses", b'{"model":"model-x","previous_response_id":"x"}',
                       lambda req: httpx.Response(200, json={}))
        with self.assertRaises(GatewayError) as caught:
            await serve_text(ctx, ctx.call["path"])
        self.assertEqual(caught.exception.status, 400)
        self.assertEqual(ctx.attempts, 0)

    async def test_count_tokens_error_is_free_and_sdk_fallback_supported(self):
        ctx = self.ctx("/v1/messages/count_tokens", b'{"model":"model-x"}',
                       lambda req: httpx.Response(405, json={"error": {"message": "synthetic-private"}}))
        with self.assertRaises(GatewayError) as caught:
            await serve_text(ctx, ctx.call["path"])
        self.assertEqual(caught.exception.status, 404)
        self.assertEqual(caught.exception.upstream_message, "***")
        self.assertFalse(ctx.call["log"])
        self.assertFalse(ctx.call["forwarded"])

    async def test_sdk_upload_enforces_declared_size_and_silence_timeout(self):
        async def content():
            yield b"123"

        for declared in (2, 4):
            with self.subTest(declared=declared), self.assertRaises(APIError) as caught:
                _ = [part async for part in request_chunks(SimpleNamespace(stream=content), declared)]
            self.assertEqual(caught.exception.status, 400)

        async def stalls():
            yield b"1"
            await asyncio.Event().wait()

        with patch.object(async_text, "BODY_IDLE_TIMEOUT", 0.01), self.assertRaises(APIError) as caught:
            _ = [part async for part in request_chunks(SimpleNamespace(stream=stalls), 2)]
        self.assertEqual(caught.exception.status, 408)

    async def test_large_gzip_prompt_is_spooled_and_released_before_provider_wait(self):
        class Prompt(str):
            pass

        prompt = ('中\n\t"\\' + 'p' * 4000) * 300
        original = requests.gateway_json({"model": "model-x", "messages": [{"content": prompt}]})
        ctx = self.ctx("/v1/chat/completions", gzip.compress(original), lambda _: None,
                       {"Content-Encoding": "gzip"})
        ctx.call["log"] = False
        ctx.gateway.is_gitee = lambda _: True
        ctx.gateway.catalog = SimpleNamespace(resolve=lambda *args: {"operations": []})
        decode = requests.decode_json_body
        references = []

        def track(raw):
            body = decode(raw)
            tracked = Prompt(body["messages"][0]["content"])
            references.append(weakref.ref(tracked))
            body["messages"][0]["content"] = tracked
            return body

        async def open_provider(method, url, *, content, headers, length, **kwargs):
            self.assertIsNone(references[0](), "a parsed prompt is still live during provider I/O")
            self.assertNotIn("request_file", ctx.call)
            self.assertEqual(ctx.call["request_bytes"], len(original))
            self.assertTrue(content.replayable)
            for attempt in range(2):
                parts = [part async for part in content]
                self.assertLessEqual(max(map(len, parts)), 65536)
                self.assertEqual(sum(map(len, parts)), length)
                self.assertEqual(b"".join(parts), original)
            return httpx.Response(200, json={"usage": {"prompt_tokens": 4, "completion_tokens": 1}})

        with patch.object(ctx.upstream, "open", open_provider), patch.object(requests, "decode_json_body", track):
            await serve_text(ctx, ctx.call["path"])
        self.assertEqual(ctx.call["usage"][:2], [4, 1])

    async def test_unknown_length_bulk_reply_is_inspected_and_delivered_in_chunks(self):
        raw = b'{"choices":[{"message":{"content":"' + b"x" * BULK_BYTES + b'"}}],"usage":{"prompt_tokens":8,"completion_tokens":3}}'
        class Provider(httpx.AsyncByteStream):
            async def __aiter__(self):
                for start in range(0, len(raw), 40000):
                    yield raw[start:start + 40000]
        ctx = self.ctx("/v1/chat/completions", b'{"model":"model-x"}',
                       lambda _: httpx.Response(200, stream=Provider()))
        ctx.call["log"] = False
        await serve_text(ctx, ctx.call["path"])
        self.assertEqual(ctx.bulk_reservations, 1)
        self.assertEqual(ctx.out[0], (200, None))
        self.assertEqual(b"".join(part for _, part in ctx.out[1:]), raw)
        self.assertLessEqual(max(len(part) for _, part in ctx.out[1:]), 65536)
        self.assertEqual(ctx.call["usage"][:2], [8, 3])

    async def test_gzip_and_deflate_enforce_expanded_limit_and_complete_stream(self):
        raw = b'{"model":"model-x","text":"' + b"x" * 2048 + b'"}'
        for name, compress in (("gzip", gzip.compress), ("deflate", zlib.compress)):
            for data, status in ((compress(raw), 413), (compress(b'{"model":"model-x"}')[:-1], 400),
                                 (compress(b'{"model":"model-x"}') + b"junk", 400)):
                with self.subTest(encoding=name, status=status), patch.object(requests, "BODY_LIMIT", 1024):
                    ctx = self.ctx("/v1/chat/completions", data, lambda _: None, {"Content-Encoding": name})
                    with self.assertRaises(APIError) as caught:
                        await serve_text(ctx, ctx.call["path"])
                    self.assertEqual(caught.exception.status, status)
                    self.assertEqual(ctx.attempts, 0)

    async def test_provider_error_in_http_success_stays_free(self):
        ctx = self.ctx("/v1/messages", b'{"model":"model-x"}',
                       lambda req: httpx.Response(200, json={"error": {"message": "synthetic-private"}}))
        with self.assertRaises(GatewayError) as caught:
            await serve_text(ctx, ctx.call["path"])
        self.assertEqual(caught.exception.status, 502)
        self.assertEqual(caught.exception.upstream_message, "***")
        self.assertFalse(ctx.call["forwarded"])
