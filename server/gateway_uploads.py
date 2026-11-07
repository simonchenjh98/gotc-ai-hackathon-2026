"""Bound simultaneous large Gitee uploads without holding slots during inference.

Place UploadLimitedConnectionMixin before HTTPConnection/HTTPSConnection in the
MRO (it can also be a base of the existing connect-timeout mixin). Only finite
bytes bodies sent to the exact Gitee host use the global upload pool.
"""
from __future__ import annotations

import errno
import http.client
import socket
import struct
import sys
import threading
import time


MIN_UPLOAD_BYTES = 256 * 1024
LARGE_BODY_THRESHOLD = MIN_UPLOAD_BYTES
UPLOAD_CONCURRENCY = 2
QUEUE_TIMEOUT = 30.0
DRAIN_TIMEOUT = 30.0
LOW_WATER_BYTES = 16 * 1024
POLL_SECONDS = 0.05
SIOCOUTQ = 0x5411
_UPLOAD_SLOTS = threading.BoundedSemaphore(UPLOAD_CONCURRENCY)


class UploadQueueFull(Exception):
    """No upload slot was obtained; no connection or request was sent."""
    request_sent = False


class UploadPostSendError(http.client.HTTPException):
    """The entire HTTP request was sent; it must never be replayed automatically.

    Deliberately not OSError: urllib wraps OSError from HTTPConnection.request in
    URLError, which existing gateway code treats as a pre-send connection error.
    The gateway must handle this exception explicitly as an uncertain submitted
    request (and account for it accordingly).
    """
    request_sent = True


class UploadDrainTimeout(UploadPostSendError):
    pass


def eligible(connection, body):
    destination = getattr(connection, "_tunnel_host", None) or getattr(connection, "host", "")
    return destination == "ai.gitee.com" and isinstance(body, (bytes, bytearray)) and len(body) >= MIN_UPLOAD_BYTES


def set_low_water(sock):
    """Linux backpressure limits how far sendall can run ahead of the network."""
    if not sys.platform.startswith("linux") or sock is None:
        return False
    option = getattr(socket, "TCP_NOTSENT_LOWAT", 25)
    try:
        sock.setsockopt(socket.IPPROTO_TCP, option, LOW_WATER_BYTES)
        return True
    except OSError:
        # Optional kernel feature: the bounded queue-drain check below still
        # prevents an upload slot being freed with megabytes queued in the socket.
        return False


def outq_bytes(sock):
    """Return Linux bytes awaiting transmission/ack, or None when unsupported."""
    if not sys.platform.startswith("linux") or sock is None:
        return None
    import fcntl
    try:
        packed = fcntl.ioctl(sock.fileno(), SIOCOUTQ, struct.pack("I", 0))
    except OSError as error:
        if error.errno in {errno.ENOTTY, errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP}:
            return None
        raise
    return struct.unpack("I", packed)[0]


def wait_for_drain(sock, timeout=None):
    """Hold a slot until the kernel's send queue is small, with a bounded wait.

    This is called only after HTTPConnection.request successfully sent all bytes.
    Every failure is consequently a post-send error, never a connection retry.
    """
    limit = DRAIN_TIMEOUT
    if isinstance(timeout, (int, float)) and timeout >= 0:
        limit = min(limit, float(timeout))
    deadline = time.monotonic() + limit
    sleeper = threading.Event()
    while True:
        try:
            queued = outq_bytes(sock)
        except (OSError, ValueError) as error:
            raise UploadPostSendError("Gitee upload completed but its send queue could not be confirmed") from error
        if queued is None or queued <= LOW_WATER_BYTES:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise UploadDrainTimeout("Gitee request sent; upload queue drain timed out")
        sleeper.wait(min(POLL_SECONDS, remaining))


class UploadLimitedConnectionMixin:
    def __init__(self, *args, upload_admitted=None, **kwargs):
        if upload_admitted is not None and not callable(upload_admitted):
            raise TypeError("upload_admitted must be callable")
        self._upload_admitted = upload_admitted
        super().__init__(*args, **kwargs)

    def connect(self):
        super().connect()
        if getattr(self, "_large_gitee_upload", False):
            set_low_water(self.sock)

    def request(self, method, url, body=None, headers=None, *, encode_chunked=False):
        headers = {} if headers is None else headers
        if not eligible(self, body):
            return super().request(method, url, body=body, headers=headers, encode_chunked=encode_chunked)
        if not _UPLOAD_SLOTS.acquire(timeout=QUEUE_TIMEOUT):
            raise UploadQueueFull("Large Gitee uploads are queued; retry later")
        previous = getattr(self, "_large_gitee_upload", False)
        self._large_gitee_upload = True
        try:
            # Access, expiry and client liveness can change during the bounded
            # global queue wait. This callback runs before connecting or sending.
            if self._upload_admitted is not None:
                self._upload_admitted()
            # Existing connections do not pass through connect() again.
            if getattr(self, "sock", None) is not None:
                set_low_water(self.sock)
            result = super().request(method, url, body=body, headers=headers, encode_chunked=encode_chunked)
            sock = getattr(self, "sock", None)
            timeout = sock.gettimeout() if sock is not None else getattr(self, "timeout", None)
            wait_for_drain(sock, timeout)
            return result
        finally:
            self._large_gitee_upload = previous
            _UPLOAD_SLOTS.release()
