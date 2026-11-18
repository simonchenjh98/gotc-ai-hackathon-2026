"""Serialize memory-intensive gateway work on the service event loop."""
import asyncio
from collections import deque

BULK_BYTES = 1024 * 1024


def bulk_input(headers):
    """Compressed JSON can expand to the full decoded limit regardless of wire length."""
    if headers.get_content_type() != "application/json":
        return False
    if headers.get("Content-Encoding", "identity").strip().lower() in {"gzip", "deflate"}:
        return True
    length = headers.get("Content-Length", "")
    try:
        return int(length) >= BULK_BYTES
    except ValueError:
        return False  # The endpoint rejects malformed lengths before reading the body.


class BulkMemory:
    """A single heavy operation may coexist with the bounded ordinary SDK calls.

    Initial inputs compete for a running slot and this lease together. Already submitted
    calls requiring a large reply get FIFO priority over new large uploads.
    """
    def __init__(self, wake_admission=None):
        self.owner = None
        self.pending = deque()
        self.wake_admission = wake_admission

    def lease(self):
        return BulkLease(self)


class BulkLease:
    def __init__(self, pool):
        self.pool = pool

    def try_acquire(self):
        if self.pool.owner is None:
            self.pool.owner = self
        return self.pool.owner is self

    async def acquire(self):
        if self.try_acquire():
            return
        ready = asyncio.get_running_loop().create_future()
        entry = (self, ready)
        self.pool.pending.append(entry)
        try:
            await ready
        except BaseException:
            if entry in self.pool.pending:
                self.pool.pending.remove(entry)
            self.release()  # Cancellation can race a successful handoff.
            raise

    def release(self):
        if self.pool.owner is not self:
            return
        self.pool.owner = None
        while self.pool.pending:
            lease, ready = self.pool.pending.popleft()
            if not ready.cancelled():
                self.pool.owner = lease
                ready.set_result(None)
                return
        if self.pool.wake_admission:
            self.pool.wake_admission()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.release()
