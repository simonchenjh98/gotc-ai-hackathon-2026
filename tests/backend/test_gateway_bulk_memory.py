"""Bulk JSON reservations precede body consumption and share capacity with large replies."""
import asyncio
from collections import defaultdict, deque
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, mock
import threading

from starlette.requests import Request

from backend.gotc.gateway.asgi_context import GatewayContext
from backend.gotc.gateway.keys import KeyService
from backend.gotc.gateway.memory import BULK_BYTES, BulkMemory


class BulkAdmissionTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.gateway = object.__new__(KeyService)
        self.gateway.lock = threading.Lock()
        self.gateway.memory = BulkMemory(self.gateway.wake_waiters)
        self.gateway.async_waiters = set()
        self.gateway.recent = defaultdict(deque)
        self.gateway.waiting, self.gateway.inflight, self.gateway.counting = (defaultdict(int) for _ in range(3))
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
                async with ctx.admitted():
                    if ctx.handler.headers.get("Content-Encoding") != "identity":
                        self.assertIs(self.gateway.memory.owner, ctx.bulk_lease)
                    entered.set()
                    await finish.wait()
            finally:
                ctx.release_bulk_memory()

        async def wait_for_queue(team):
            while self.gateway.waiting[team] == 0:
                await asyncio.sleep(0)

        active, waiting, small = (self.context("team-a", BULK_BYTES), self.context("team-b", 64, "gzip"),
                                  self.context("team-c", 64))
        started = [asyncio.Event() for _ in range(3)]
        release = [asyncio.Event() for _ in range(3)]
        try:
            jobs.append(asyncio.create_task(submit(active, started[0], release[0])))
            await asyncio.wait_for(started[0].wait(), 2)
            jobs.append(asyncio.create_task(submit(waiting, started[1], release[1])))
            await asyncio.wait_for(wait_for_queue("team-b"), 2)
            self.assertFalse(started[1].is_set())
            self.assertEqual(self.gateway.inflight["team-b"], 0)
            jobs.append(asyncio.create_task(submit(small, started[2], release[2])))
            await asyncio.wait_for(started[2].wait(), 2)
            self.assertIsNone(small.bulk_lease)
            release[0].set()
            await asyncio.wait_for(started[1].wait(), 2)
            self.assertEqual(self.gateway.inflight["team-b"], 1)
        finally:
            for event in release:
                event.set()
            await asyncio.gather(*jobs)
        self.assertEqual(sum(self.gateway.inflight.values()), 0)
        self.assertIsNone(self.gateway.memory.owner)

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
