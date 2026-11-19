"""Synthetic HTTPX byte streams exercise wire fidelity, backpressure and final accounting."""
import asyncio
import time
import unittest
from types import SimpleNamespace

import httpx

from backend.gotc.gateway.async_text import relay_stream
from backend.gotc.gateway.protocol_errors import ClientGone, GatewayError
from backend.gotc.gateway.stream_events import event_blocks


class ByteStream(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks

    async def __aiter__(self):
        async for chunk in self.chunks:
            yield chunk


async def chunks(*parts):
    for part in parts:
        yield part


class StreamTests(unittest.IsolatedAsyncioTestCase):
    def context(self, send=None):
        self.writes = []
        self.kept = []
        self.call = {"usage": [0] * 5, "forwarded": True, "reply_bytes": 0, "done": False,
                     "status": 200, "path": "/v1/chat/completions", "started": time.monotonic()}

        async def deliver(status, content_type, wire=None):
            if wire is not None:
                self.writes.append(wire)
            if send:
                await send(wire)
        gateway = SimpleNamespace(error_secrets=lambda call: ("private-fixture",),
                                  keep=lambda call, wire: self.kept.append(wire))
        return SimpleNamespace(gateway=gateway, send=deliver, call=self.call)

    async def relay(self, ctx, source):
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, stream=ByteStream(source)))) as client:
            async with client.stream("GET", "https://provider.example.invalid/stream") as response:
                await relay_stream(ctx, "openai", response, "text/event-stream")

    async def test_every_byte_boundary_preserves_bom_newlines_empty_and_multiline_fields(self):
        raw = (b"\xef\xbb\xbf: opening\r\r" + "event: delta\r\nid: 7\ndata: 北\rdata\r\ndata:  京\n\n".encode()
               + b"data: [DONE]")
        for width in (1, 2, 3, 5, len(raw)):
            with self.subTest(width=width):
                result = [block async for block in event_blocks(
                    chunks(*(raw[n:n + width] for n in range(0, len(raw), width))), 1024)]
                self.assertEqual(b"".join(block.wire for block in result), raw)
                self.assertEqual([block.payload for block in result], [b"", "北\n\n 京".encode(), b"[DONE]"])

    async def test_prior_to_data_error_stays_an_uncharged_http_failure(self):
        ctx = self.context()
        with self.assertRaises(GatewayError) as caught:
            await self.relay(ctx, chunks(b": heartbeat\r\n\r\n\xef", b"ignored: metadata\n",
                                         b'event: error\ndata: {"error":{"message":"private-fixture"}}\n\n'))
        self.assertEqual(caught.exception.status, 502)
        self.assertFalse(self.call["forwarded"])
        self.assertEqual(self.writes, [])
        self.assertNotIn("private-fixture", caught.exception.upstream_message)

    async def test_metadata_and_multiline_named_errors_kept_redacted_in_place(self):
        ctx = self.context()
        start = b'data: {"choices":[{"delta":{"content":"hello"}}]}\r\n\r\n'
        error = b"event: error\rdata: private-fixture\rid: private-fixture\rdata: second line\r\r"
        await self.relay(ctx, chunks(start, error))
        self.assertEqual(b"".join(self.writes), start + error.replace(b"private-fixture", b"***"))
        self.assertNotIn(b"private-fixture", b"".join(self.kept))
        self.assertEqual(self.call["status"], 502)

    async def test_heartbeat_is_sent_while_provider_waits_for_consumer(self):
        consumed = asyncio.Event()
        beat = b": keep-alive\r\n\r\n"

        async def observe(wire):
            if wire == beat:
                consumed.set()

        async def source():
            yield b'data: {"choices":[]}\n\n'
            yield beat
            await asyncio.wait_for(consumed.wait(), 1)
            yield b"data: [DONE]\n\n"

        await self.relay(self.context(observe), source())
        self.assertTrue(consumed.is_set())
        self.assertTrue(self.call["done"])

    async def test_disconnected_consumer_still_records_final_usage(self):
        async def gone(wire):
            if wire is not None:
                raise ClientGone()

        ctx = self.context(gone)
        await self.relay(ctx, chunks(b'data: {"choices":[{"delta":{"content":"first"}}]}\n\n',
                                     b'data: {"usage":{"prompt_tokens":9,"completion_tokens":3}}\n\n',
                                     b"data: [DONE]\n\n"))
        self.assertEqual(self.call["usage"][:2], [9, 3])
        self.assertEqual(len(self.writes), 1)
        self.assertTrue(self.call["done"])

    async def test_limit_counts_original_octets_and_unterminated_lines(self):
        for raw in (b"data: " + b"\xe5\x8c\x97" * 5 + b"\n\n", b"data: " + b"x" * 10000,
                    b": comment\n" * 10):
            with self.subTest(raw=raw[:8]), self.assertRaises(GatewayError):
                _ = [event async for event in event_blocks(chunks(raw), 16)]
        raw = b"data: 12\r\n\r\n" * 10
        result = [event async for event in event_blocks(chunks(raw), 12)]
        self.assertEqual(len(result), 10)
