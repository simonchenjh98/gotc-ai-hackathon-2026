"""One bounded FIFO for public credential work, shared by ASGI and direct callers."""
from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable, Coroutine, Iterator
from concurrent.futures import Future
from contextlib import contextmanager
from contextvars import ContextVar
from threading import Lock
import time
from typing import Any, TypeVar

from .constants import LOGIN_KDF_CONCURRENCY
from .errors import APIError

Result = TypeVar("Result")

PASSWORD_PATHS = frozenset({
    '/api/login', '/api/roadshow/login', '/api/roadshow/claim', '/api/claims/redeem',
    '/api/finals/access',
})


def busy() -> APIError:
    return APIError(429, '登录繁忙，请稍后重试', 'login_busy')


class PasswordCapacity:
    def __init__(self, running: int = LOGIN_KDF_CONCURRENCY, waiting: int = 40, timeout: float = 4.0):
        self.running = running
        self.waiting = waiting
        self.timeout = timeout
        self.lock = Lock()
        self.queue: deque[Future[None]] = deque()
        self.active = self.peak_active = self.peak_waiting = 0
        self.held = ContextVar('password_slot', default=False)

    def reserve(self) -> Future[None]:
        with self.lock:
            ticket: Future[None] = Future()
            if self.active < self.running:
                self.active += 1
                self.peak_active = max(self.peak_active, self.active)
                ticket.set_result(None)
            elif len(self.queue) < self.waiting:
                self.queue.append(ticket)
                self.peak_waiting = max(self.peak_waiting, len(self.queue))
            else:
                raise busy()
            return ticket

    def release(self, ticket: Future[None]) -> None:
        with self.lock:
            if not ticket.done():
                self.queue.remove(ticket)
                ticket.cancel()
            elif self.queue:
                self.queue.popleft().set_result(None)
            else:
                self.active -= 1

    @contextmanager
    def slot(self) -> Iterator[None]:
        if self.held.get():
            yield
            return
        ticket = self.reserve()
        try:
            try:
                ticket.result(timeout=self.timeout)
            except TimeoutError:
                raise busy() from None
            token = self.held.set(True)
            try:
                yield
            finally:
                self.held.reset(token)
        finally:
            self.release(ticket)

    async def run(self, work: Callable[[], Coroutine[Any, Any, Result]]) -> Result:
        ticket = self.reserve()
        try:
            if not ticket.done():
                try:
                    async with asyncio.timeout(self.timeout):
                        await asyncio.shield(asyncio.wrap_future(ticket))
                except TimeoutError:
                    raise busy() from None
        except BaseException:
            self.release(ticket)
            raise

        async def admitted_work() -> Result:
            token = self.held.set(True)
            try:
                return await work()
            finally:
                self.held.reset(token)
                self.release(ticket)

        # A cancelled HTTP request cannot stop an already running scrypt thread.
        # Give the admitted task ownership until the worker actually returns.
        task = asyncio.create_task(admitted_work())
        task.add_done_callback(self._observe_completion)
        return await asyncio.shield(task)

    @staticmethod
    def _observe_completion(task: asyncio.Task[Result]) -> None:
        # Consume the result even if the HTTP caller disconnected during the check.
        if not task.cancelled():
            task.exception()

class CoolingRequired(Exception):
    """A verified throttle decision made before any authentication side effects."""
    def __init__(self, lane: PasswordCapacity):
        super().__init__()
        self.lane = lane


class PasswordChecks:
    def __init__(self, running: int = LOGIN_KDF_CONCURRENCY):
        self.capacity = PasswordCapacity(running=running)
        # A targeted participant and an organizer do not share their cooling queues.
        self.organizer_cooling = PasswordCapacity(running=1, waiting=3, timeout=4)
        self.participant_cooling = PasswordCapacity(running=1, waiting=3, timeout=4)

    def require_cooling(self, role: str) -> None:
        lane = self.organizer_cooling if role == "admin" else self.participant_cooling
        if not lane.held.get():
            raise CoolingRequired(lane)

    def run_sync(self, work: Callable[[], Result]) -> Result:
        if self.capacity.held.get():
            return work()
        try:
            with self.capacity.slot():
                return work()
        except CoolingRequired as decision:
            lane = decision.lane
        with lane.slot():
            time.sleep(1)
            with self.capacity.slot():
                return work()

    async def run(self, work: Callable[[], Coroutine[Any, Any, Result]]) -> Result:
        try:
            return await self.capacity.run(work)
        except CoolingRequired as decision:
            lane = decision.lane

        async def after_delay() -> Result:
            await asyncio.sleep(1)
            return await self.capacity.run(work)

        return await lane.run(after_delay)
