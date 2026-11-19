"""Bulk JSON reservations precede body consumption and share capacity with large replies."""
import asyncio
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, mock
import threading

import httpx
from starlette.requests import Request

from backend.gotc.errors import APIError
from backend.gotc.gateway.admission import BodyUploads, GatewayCapacity
from backend.gotc.gateway.asgi_context import GatewayContext
from backend.gotc.gateway.async_media import _spool_response
from backend.gotc.gateway.keys import KeyService
from backend.gotc.gateway.memory import BULK_BYTES, BulkMemory


class BulkAdmissionTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.gateway = object.__new__(KeyService)
        self.gateway.memory = BulkMemory()
        self.gateway.body_uploads = BodyUploads(GatewayCapacity())
        self.gateway.admit_async = mock.AsyncMock()
        self.gateway.config = {}
        self.gateway.service = SimpleNamespace(origin="https://event.invalid")
        self.gateway.recheck_access = mock.Mock()
        self.gateway.authenticate = self.authenticate

    @staticmethod
    def authenticate(handler, call):
        key = {"team_id": handler.headers["x-fixture-team"]}
        call.update(key=key, settings={"teamRpm": 0})
        return key

    def context(self, team, size, encoding="identity"):
        async def receive():
            return {"type": "http.request", "body": b"fixture"}
        headers = [(b"content-length", str(size).encode()), (b"content-type", b"application/json"),
                   (b"content-encoding", encoding.encode()), (b"x-fixture-team", team.encode())]
        request = Request({"type": "http", "method": "POST", "scheme": "http", "server": ("localhost", 80),
                           "client": ("127.0.0.1", 1), "path": "/v1/messages", "query_string": b"",
                           "headers": headers}, receive)
        return GatewayContext(self.gateway, request, "/v1/messages")

    async def test_large_and_compressed_bodies_wait_unread_while_a_small_call_runs(self):
        jobs = []
        async def submit(ctx, entered, finish):
            try:
                async with ctx.uploading():
                    if ctx.handler.headers.get("Content-Encoding") != "identity":
                        self.assertIs(self.gateway.memory.owner, ctx.bulk_lease)
                    entered.set()
                    await finish.wait()
            finally:
                ctx.release_bulk_memory()

        async def wait_for_queue():
            while not self.gateway.memory.uploads:
                await asyncio.sleep(0)

        active, waiting, small = (self.context("team-a", BULK_BYTES), self.context("team-b", 64, "gzip"),
                                  self.context("team-c", 64))
        started = [asyncio.Event() for _ in range(3)]
        release = [asyncio.Event() for _ in range(3)]
        try:
            jobs.append(asyncio.create_task(submit(active, started[0], release[0])))
            await asyncio.wait_for(started[0].wait(), 2)
            jobs.append(asyncio.create_task(submit(waiting, started[1], release[1])))
            await asyncio.wait_for(wait_for_queue(), 2)
            self.assertFalse(started[1].is_set())
            jobs.append(asyncio.create_task(submit(small, started[2], release[2])))
            await asyncio.wait_for(started[2].wait(), 2)
            self.assertIsNone(small.bulk_lease)
            release[0].set()
            await asyncio.wait_for(started[1].wait(), 2)
            self.assertEqual(self.gateway.body_uploads.running["team-b"], 1)
        finally:
            for event in release:
                event.set()
            await asyncio.gather(*jobs)
        self.gateway.admit_async.assert_not_called()
        self.assertEqual(self.gateway.body_uploads.running.total(), 0)
        self.assertIsNone(self.gateway.memory.owner)

    async def test_invalid_credential_does_not_enter_body_lane(self):
        ctx = self.context("team", BULK_BYTES)
        ctx.gateway.authenticate = mock.Mock(side_effect=APIError(401, "invalid fixture key"))
        with mock.patch.object(ctx.gateway.body_uploads, "acquire", new_callable=mock.AsyncMock) as acquire:
            with self.assertRaises(APIError):
                async with ctx.uploading():
                    self.fail("invalid credential reached body consumption")
            acquire.assert_not_called()
        self.assertIsNone(self.gateway.memory.owner)

    async def test_waiting_response_precedes_an_earlier_waiting_upload(self):
        active, upload, response = [self.gateway.memory.lease() for _ in range(3)]
        await active.acquire(upload=True)
        read = asyncio.create_task(upload.acquire(upload=True))
        send = asyncio.create_task(response.acquire())
        await asyncio.sleep(0)
        active.release()
        await asyncio.wait_for(send, 2)
        self.assertFalse(read.done())
        response.release()
        await asyncio.wait_for(read, 2)
        upload.release()
        self.assertIsNone(self.gateway.memory.owner)

    async def test_media_reply_owns_bulk_lease_before_large_spool_writes(self):
        for headers in ({}, {"content-length": str(BULK_BYTES)}):
            with self.subTest(headers=headers):
                ctx = self.context("team", 0)
                written = 0
                def write(block):
                    nonlocal written
                    written += len(block)
                    if written >= BULK_BYTES:
                        self.assertIsNotNone(self.gateway.memory.owner)
                output = SimpleNamespace(write=write, seek=lambda _: None)
                response = httpx.Response(200, stream=httpx.ByteStream(b"a" * BULK_BYTES), headers=headers)
                try:
                    self.assertEqual(await _spool_response(ctx, response, output), BULK_BYTES)
                finally:
                    await response.aclose()
                    ctx.close()

    async def test_native_cancellation_waits_before_releasing_worker_memory_and_files(self):
        started, release = threading.Event(), threading.Event()
        ctx = self.context("team", BULK_BYTES)
        def transform():
            started.set()
            release.wait(2)
        async def request():
            try:
                await ctx.reserve_bulk_memory()
                await ctx.sync(transform)
            finally:
                ctx.close()
        job = asyncio.create_task(request())
        try:
            self.assertTrue(await asyncio.to_thread(started.wait, 2))
            job.cancel()
            await asyncio.sleep(0)
            job.cancel()
            self.assertIs(self.gateway.memory.owner, ctx.bulk_lease)
            self.assertFalse(job.done())
        finally:
            release.set()
        with self.assertRaises(asyncio.CancelledError):
            await job
        self.assertIsNone(self.gateway.memory.owner)

    async def test_large_downstream_payload_uses_bounded_backpressured_writes(self):
        ctx = self.context("team", 2)
        frames = []
        async def send(event):
            frames.append(event)
        ctx.request.scope["gateway_send"] = send
        payload = b"x" * (BULK_BYTES + 1)
        await ctx.send(200, "application/json", payload)
        parts = [event["body"] for event in frames if event["type"] == "http.response.body"]
        self.assertEqual(b"".join(parts), payload)
        self.assertLessEqual(max(map(len, parts)), 65536)
        self.assertEqual(dict(frames[0]["headers"])[b"content-length"], str(len(payload)).encode())
        self.assertFalse(frames[-1].get("more_body", False))

    async def test_existing_responses_take_turn_before_another_large_upload_and_cancellation_frees_the_lane(self):
        active, cancelled, response, upload = [self.gateway.memory.lease() for _ in range(4)]
        await active.acquire()
        await active.acquire()
        cancel_job = asyncio.create_task(cancelled.acquire())
        next_job = asyncio.create_task(response.acquire())
        await asyncio.sleep(0)
        active.release()
        cancel_job.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await cancel_job
        await asyncio.wait_for(next_job, 2)
        self.assertFalse(upload.try_acquire())
        response.release()
        self.assertTrue(upload.try_acquire())
        upload.release()
        self.assertIsNone(self.gateway.memory.owner)
