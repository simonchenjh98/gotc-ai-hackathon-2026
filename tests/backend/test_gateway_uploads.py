"""Large uploads consume a bounded async slot only until HTTPX finishes writing."""
import asyncio
from contextlib import aclosing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import unittest
from unittest import mock

import httpx

from backend.gotc.gateway import gateway_uploads as uploads, transport
from backend.gotc.gateway.protocol_errors import GatewayError


class UploadTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_upload_queue_rejects_before_recheck_or_connect(self):
        async def no_send(request):
            self.fail("full upload queue must not dial a provider")

        provider = transport.AsyncProvider(transport=httpx.MockTransport(no_send), proxies={})
        self.addAsyncCleanup(provider.aclose)
        provider.uploads = asyncio.Semaphore(0)
        check = mock.AsyncMock()
        with mock.patch.object(uploads, "QUEUE_TIMEOUT", 0.01), self.assertRaises(GatewayError) as error:
            await provider.open("POST", "https://ai.gitee.com/v1", headers={}, content=b"x" * uploads.LARGE_BODY_THRESHOLD,
                                recheck=check)
        self.assertEqual((error.exception.status, error.exception.retry_after), (429, 5))
        check.assert_not_awaited()

    async def test_waiting_request_rechecks_revocation_and_gives_slot_back(self):
        provider = transport.AsyncProvider(transport=httpx.MockTransport(lambda request: self.fail("revoked upload sent")),
                                           proxies={})
        self.addAsyncCleanup(provider.aclose)
        provider.uploads = asyncio.Semaphore(1)
        await provider.uploads.acquire()
        check = mock.AsyncMock(side_effect=GatewayError("auth", "revoked"))
        task = asyncio.create_task(provider.open("POST", "https://ai.gitee.com/upload", headers={},
                                                 content=b"x"*uploads.LARGE_BODY_THRESHOLD, recheck=check))
        await asyncio.sleep(0)
        check.assert_not_awaited()
        provider.uploads.release()
        with self.assertRaises(GatewayError) as error:
            await task
        self.assertEqual(error.exception.status, 401)
        check.assert_awaited_once()
        self.assertFalse(provider.uploads.locked())

    async def test_small_and_other_host_requests_bypass_full_upload_queue(self):
        async def handler(request):
            return httpx.Response(200, content=b"ok")
        provider = transport.AsyncProvider(transport=httpx.MockTransport(handler), proxies={})
        self.addAsyncCleanup(provider.aclose)
        provider.uploads = asyncio.Semaphore(0)
        for host, body in (("ai.gitee.com", b"small"), ("other.invalid", b"x"*uploads.LARGE_BODY_THRESHOLD)):
            response = await provider.open("POST", "https://" + host + "/models", headers={}, content=body)
            await response.aclose()
            self.assertEqual(response.status_code, 200)

    async def test_stream_writes_preserve_bytes_and_release_slot_before_response(self):
        received = []
        complete, finish = threading.Event(), threading.Event()

        class Provider(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                received.append(self.rfile.read(int(self.headers["Content-Length"])))
                complete.set()
                finish.wait(5)
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

        with ThreadingHTTPServer(("127.0.0.1", 0), Provider) as server:
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            provider = transport.AsyncProvider(proxies={"http": f"http://127.0.0.1:{server.server_port}"})
            provider.uploads = asyncio.Semaphore(1)
            body = b"x" * uploads.LARGE_BODY_THRESHOLD
            gate = asyncio.Event()
            started = asyncio.Event()

            async def content():
                started.set()
                yield body[:1024]
                await gate.wait()
                yield body[1024:]

            task = asyncio.create_task(provider.open("POST", "http://ai.gitee.com/v1", headers={},
                                                     content=content(), length=len(body)))
            try:
                await asyncio.wait_for(started.wait(), 2)
                self.assertTrue(provider.uploads.locked())
                gate.set()
                self.assertTrue(await asyncio.to_thread(complete.wait, 2))
                self.assertFalse(provider.uploads.locked())
                self.assertFalse(task.done())
                finish.set()
                async with aclosing(await asyncio.wait_for(task, 3)) as response:
                    self.assertEqual(await response.aread(), b"ok")
                self.assertEqual(received, [body])
            finally:
                finish.set()
                gate.set()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await provider.aclose()
                await asyncio.to_thread(server.shutdown)
                worker.join(2)
