"""Global upload slots, bounded kernel-drain waiting and pre/post-send distinction."""
import errno
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import socket
import threading
import time
import unittest
from unittest import mock
import urllib.error

import gateway_uploads as uploads


class FakeSocket:
    def gettimeout(self):
        return 10


class BaseConnection:
    def __init__(self, host="ai.gitee.com", tunnel=None):
        self.host, self._tunnel_host = host, tunnel
        self.sock = FakeSocket()
        self.calls = []
        self.on_request = None
        self.on_response = None

    def connect(self):
        self.sock = FakeSocket()

    def request(self, method, url, body=None, headers=None, *, encode_chunked=False):
        if self.sock is None:
            self.connect()
        self.calls.append((method, url, body, headers, encode_chunked))
        if self.on_request:
            self.on_request()
        return "sent"

    def getresponse(self):
        if self.on_response:
            self.on_response()
        return "response"


class Connection(uploads.UploadLimitedConnectionMixin, BaseConnection):
    pass


class UploadTests(unittest.TestCase):
    def setUp(self):
        self.slots = threading.BoundedSemaphore(2)
        patches = [mock.patch.object(uploads, "_UPLOAD_SLOTS", self.slots),
                   mock.patch.object(uploads, "outq_bytes", return_value=0),
                   mock.patch.object(uploads, "set_low_water", return_value=True)]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.body = b"x" * uploads.MIN_UPLOAD_BYTES

    def assert_slots_free(self):
        self.assertTrue(self.slots.acquire(blocking=False))
        self.assertTrue(self.slots.acquire(blocking=False))
        self.assertFalse(self.slots.acquire(blocking=False))
        self.slots.release()
        self.slots.release()

    def test_only_exact_gitee_bytes_or_bytearray_at_threshold_are_eligible(self):
        for host in ("evil.example", "ai.gitee.com.evil", "AI.GITEE.COM", "ai.gitee.com."):
            self.assertFalse(uploads.eligible(Connection(host), self.body))
        self.assertTrue(uploads.eligible(Connection(), self.body))
        self.assertTrue(uploads.eligible(Connection(), bytearray(self.body)))
        self.assertTrue(uploads.eligible(Connection("proxy.example", "ai.gitee.com"), self.body))
        self.assertFalse(uploads.eligible(Connection("ai.gitee.com", "other.example"), self.body))
        for body in (self.body[:-1], "x" * len(self.body), iter([self.body]), None):
            self.assertFalse(uploads.eligible(Connection(), body))

    def test_small_and_other_host_requests_bypass_a_full_pool(self):
        self.slots.acquire()
        self.slots.acquire()
        try:
            with mock.patch.object(uploads, "QUEUE_TIMEOUT", 0):
                for connection, body in ((Connection(), b"small"), (Connection("other.example"), self.body)):
                    self.assertEqual(connection.request("POST", "/v1/chat/completions", body), "sent")
                    self.assertIs(connection.calls[0][2], body)
            uploads.set_low_water.assert_not_called()
        finally:
            self.slots.release()
            self.slots.release()

    def test_full_queue_times_out_before_any_connect_or_send(self):
        self.slots.acquire()
        self.slots.acquire()
        try:
            connection = Connection()
            connection.sock = None
            with mock.patch.object(uploads, "QUEUE_TIMEOUT", 0.01), self.assertRaises(uploads.UploadQueueFull) as error:
                connection.request("POST", "/", self.body)
            self.assertFalse(error.exception.request_sent)
            self.assertEqual(connection.calls, [])
            self.assertIsNone(connection.sock)
        finally:
            self.slots.release()
            self.slots.release()

    def test_request_exception_always_releases_slot(self):
        connection = Connection()
        def fail():
            raise ConnectionResetError("send failed")
        connection.on_request = fail
        with self.assertRaises(ConnectionResetError):
            connection.request("POST", "/", self.body)
        self.assert_slots_free()

    def test_admitted_callback_rechecks_before_connect_or_send_and_releases_on_failure(self):
        checks = []
        def revoked():
            checks.append("checked")
            raise PermissionError("key revoked while queued")
        connection = Connection(upload_admitted=revoked)
        connection.sock = None
        with self.assertRaises(PermissionError):
            connection.request("POST", "/", self.body)
        self.assertEqual(checks, ["checked"])
        self.assertIsNone(connection.sock)
        self.assertEqual(connection.calls, [])
        self.assert_slots_free()

    def test_admitted_callback_runs_once_before_send_and_is_not_forwarded_to_http_init(self):
        order = []
        connection = Connection(upload_admitted=lambda: order.append("admitted"))
        connection.on_request = lambda: order.append("request")
        connection.request("POST", "/", self.body)
        self.assertEqual(order, ["admitted", "request"])

    def test_connect_sets_low_water_and_request_bytes_are_unchanged(self):
        connection = Connection()
        connection.sock = None
        headers = {"Content-Length": str(len(self.body))}
        connection.request("POST", "/", self.body, headers)
        uploads.set_low_water.assert_called_once_with(connection.sock)
        self.assertIs(connection.calls[0][2], self.body)
        self.assertIs(connection.calls[0][3], headers)
        self.assertFalse(connection._large_gitee_upload)
        self.assert_slots_free()

    def test_slots_are_released_before_getresponse_or_inference(self):
        connection = Connection()
        connection.request("POST", "/", self.body)
        connection.on_response = self.assert_slots_free
        self.assertEqual(connection.getresponse(), "response")

    def test_global_limit_two_and_release_only_after_drain(self):
        started = [threading.Event() for _ in range(3)]
        release = threading.Event()
        connections = [Connection() for _ in range(3)]
        count, peak = 0, 0
        lock, errors = threading.Lock(), []
        def drain(sock, timeout):
            nonlocal count, peak
            with lock:
                count += 1
                peak = max(peak, count)
            release.wait(2)
            with lock:
                count -= 1
        for index, connection in enumerate(connections):
            connection.on_request = started[index].set
        def run(connection):
            try:
                connection.request("POST", "/", self.body)
            except Exception as error:
                errors.append(error)
        with mock.patch.object(uploads, "wait_for_drain", side_effect=drain):
            workers = [threading.Thread(target=run, args=(c,), daemon=True) for c in connections]
            workers[0].start()
            workers[1].start()
            self.assertTrue(started[0].wait(1))
            self.assertTrue(started[1].wait(1))
            workers[2].start()
            self.assertFalse(started[2].wait(0.05), "third upload began before another queue drained")
            release.set()
            for worker in workers:
                worker.join(2)
                self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(peak, 2)
        self.assertTrue(started[2].is_set())
        self.assert_slots_free()

    def test_drain_timeout_is_post_send_not_urllib_presend_oserror(self):
        connection = Connection()
        with mock.patch.object(uploads, "outq_bytes", return_value=999999), \
                mock.patch.object(uploads, "DRAIN_TIMEOUT", 0), self.assertRaises(uploads.UploadDrainTimeout) as error:
            connection.request("POST", "/", self.body)
        self.assertTrue(error.exception.request_sent)
        self.assertIsInstance(error.exception, http.client.HTTPException)
        self.assertNotIsInstance(error.exception, OSError)
        self.assertNotIsInstance(error.exception, urllib.error.URLError)
        self.assertEqual(len(connection.calls), 1)
        self.assert_slots_free()

    def test_drain_socket_failure_is_post_send_and_releases_slot(self):
        with mock.patch.object(uploads, "outq_bytes", side_effect=OSError(errno.EBADF, "closed")), \
                self.assertRaises(uploads.UploadPostSendError):
            Connection().request("POST", "/", self.body)
        self.assert_slots_free()


class DrainTests(unittest.TestCase):
    def test_waits_until_low_water_mark(self):
        with mock.patch.object(uploads, "outq_bytes", side_effect=[999999, 32768, 16384]) as queued, \
                mock.patch.object(uploads, "POLL_SECONDS", 0.001):
            uploads.wait_for_drain(FakeSocket(), 1)
        self.assertEqual(queued.call_count, 3)

    def test_unsupported_platform_or_ioctl_is_a_bounded_noop(self):
        with mock.patch.object(uploads, "outq_bytes", return_value=None):
            uploads.wait_for_drain(FakeSocket(), 0)
        with mock.patch.object(uploads.sys, "platform", "win32"):
            self.assertIsNone(uploads.outq_bytes(FakeSocket()))
            self.assertFalse(uploads.set_low_water(FakeSocket()))

    def test_original_socket_timeout_caps_drain_wait(self):
        with mock.patch.object(uploads, "outq_bytes", return_value=999999), self.assertRaises(uploads.UploadDrainTimeout):
            uploads.wait_for_drain(FakeSocket(), 0)


class RealConnection(uploads.UploadLimitedConnectionMixin, http.client.HTTPConnection):
    pass


class WireTests(unittest.TestCase):
    def test_real_http_upload_preserves_body_and_uses_kernel_queue_on_linux(self):
        received = []
        class Provider(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                received.append(self.rfile.read(int(self.headers["Content-Length"])))
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")
        server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        connection = RealConnection("ai.gitee.com", timeout=5)
        # Keep the exact destination host for the guard, redirect only the test
        # TCP dial to loopback. No external network or upstream credential used.
        connection._create_connection = lambda address, timeout, source_address=None: socket.create_connection(
            ("127.0.0.1", server.server_port), timeout=timeout)
        raw = b'{"input":"' + b"context " * 65536 + b'"}'
        try:
            connection.request("POST", "/v1/chat/completions", raw, {"Content-Type": "application/json"})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), b"ok")
            self.assertEqual(received, [raw])
            self.assertFalse(connection._large_gitee_upload)
        finally:
            connection.close()
            server.shutdown()
            server.server_close()
            worker.join()


if __name__ == "__main__":
    unittest.main()
