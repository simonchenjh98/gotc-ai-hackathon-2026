"""HTTPX pool reuse, redirect isolation, and explicit safe generation retries."""
import asyncio
from contextlib import aclosing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import unittest
from unittest import mock

import httpx

from backend.gotc.gateway import gateway_uploads as uploads, transport


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_retries_small_overload_but_never_replays_large_context_or_redirect(self):
        for large, status, expected in ((False, 429, 2), (True, 429, 1), (False, 307, 1), (False, 500, 1)):
            calls = []
            def handle(request):
                calls.append(request)
                return httpx.Response(status if len(calls) == 1 else 200, headers={"Location": "https://elsewhere.invalid"})
            provider = transport.AsyncProvider(proxies={}, transport=httpx.MockTransport(handle))
            try:
                with self.subTest(large=large, status=status), mock.patch.object(transport, "RETRY_DELAYS", (0,)):
                    body = b"x" * uploads.LARGE_BODY_THRESHOLD if large else b"small"
                    async with aclosing(await provider.open("POST", "https://ai.gitee.com/v1", headers={}, content=body, retry=True)) as response:
                        self.assertEqual(response.status_code, 200 if expected == 2 else status)
                    self.assertEqual(len(calls), expected)
                    self.assertTrue(all(req.url.host == "ai.gitee.com" for req in calls))
            finally:
                await provider.aclose()

    async def test_completed_responses_share_a_socket_partial_reads_discard_it_and_cookies_stay_private(self):
        peers, cookies = [], []
        class Provider(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_):
                pass

            def do_GET(self):
                peers.append(self.client_address)
                cookies.append(self.headers.get("Cookie"))
                self.send_response(200)
                self.send_header("Content-Length", "5")
                self.send_header("Set-Cookie", "synthetic_session=fixture; Path=/")
                self.end_headers()
                self.wfile.write(b"reply")

        with ThreadingHTTPServer(("127.0.0.1", 0), Provider) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            provider = transport.AsyncProvider(proxies={})
            url = f"http://127.0.0.1:{server.server_port}/v1"
            try:
                for complete in (True, False, True):
                    async with aclosing(await provider.open("GET", url, headers={}, timeout=3)) as response:
                        if complete:
                            self.assertEqual(await response.aread(), b"reply")
                        else:
                            self.assertEqual(await anext(response.aiter_raw(1)), b"r")
            finally:
                self.assertEqual(len(provider.client.cookies), 0)
                await provider.aclose()
                await asyncio.to_thread(server.shutdown)
                thread.join(2)
            self.assertEqual(peers[0], peers[1])
            self.assertNotEqual(peers[1], peers[2])
            self.assertEqual(cookies, [None, None, None])
