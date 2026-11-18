"""Serialize memory-intensive gateway work on the service event loop."""
import asyncio
from collections import deque
from .protocol_errors import GatewayError

BULK_BYTES = 1024 * 1024


def bulk_input(headers):
    """Reserve the shared lease before consuming large forms or potentially large JSON."""
    mime = headers.get_content_type()
    if mime not in {"application/json", "application/x-www-form-urlencoded", "multipart/form-data"}:
        return False
    if mime == "application/json" and headers.get("Content-Encoding", "identity").strip().lower() in {"gzip", "deflate"}:
        return True
    length = headers.get("Content-Length", "")
    try:
        return int(length) >= BULK_BYTES
    except ValueError:
        return False  # The endpoint rejects malformed lengths before reading the body.


class BulkMemory:
    """One heavy operation at a time. Finish provider replies before starting new large uploads."""
    def __init__(self, *, spool_mib=256):
        self.owner = None
        self.pending = deque()
        self.uploads = deque()
        self.spool_limit = spool_mib * 1024 * 1024
        self.spooled = 0

    def retain_spool(self, size):
        """Account for files retained beyond the single active bulk parser."""
        if self.spooled + size > self.spool_limit:
            raise GatewayError("rate_limit", "大文件请求正在排队，请稍后重试", retry_after=2)
        self.spooled += size

    def lease(self):
        return BulkLease(self)


class BulkLease:
    def __init__(self, pool):
        self.pool = pool

    def try_acquire(self):
        if self.pool.owner is None:
            self.pool.owner = self
        return self.pool.owner is self

    async def acquire(self, *, upload=False):
        if self.try_acquire():
            return
        ready = asyncio.get_running_loop().create_future()
        entry = (self, ready)
        queue = self.pool.uploads if upload else self.pool.pending
        queue.append(entry)
        try:
            await ready
        except BaseException:
            if entry in queue:
                queue.remove(entry)
            self.release()  # Cancellation can race a successful handoff.
            raise

    def release(self):
        if self.pool.owner is not self:
            return
        self.pool.owner = None
        for queue in (self.pool.pending, self.pool.uploads):
            while queue:
                lease, ready = queue.popleft()
                if not ready.cancelled():
                    self.pool.owner = lease
                    ready.set_result(None)
                    return

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.release()
