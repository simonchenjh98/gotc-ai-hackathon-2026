"""Startup capacity settings and bounded, round-robin admission for gateway work."""
from __future__ import annotations

import asyncio
import os
import threading
from collections import Counter, OrderedDict, deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, fields

from .protocol_errors import GatewayError


QUEUE_WAIT = 30  # seconds
CONTROL_RESERVE = 16
UPLOAD_QUEUE = 16
UPLOAD_TEAM_QUEUE = 2


@dataclass(frozen=True)
class GatewayCapacity:
    team_running: int = 15
    global_running: int = 64
    team_queue: int = 16
    global_queue: int = 64
    team_uploads: int = 8
    global_uploads: int = 96
    team_speech: int = 2
    global_speech: int = 8
    team_tasks: int = 2
    global_tasks: int = 8
    spool_mib: int = 256
    request_limit: int | None = None

    @property
    def minimum_requests(self) -> int:
        return (self.global_running + self.global_queue + self.global_uploads + UPLOAD_QUEUE
                + self.global_tasks + self.global_speech + CONTROL_RESERVE)

    @property
    def total_requests(self) -> int:
        return self.minimum_requests if self.request_limit is None else self.request_limit

    @property
    def provider_connections(self) -> int:
        # One sequential task reconciler and the platform's eight organizer workers
        # can also use the pool in the combined development process.
        return self.global_running + self.global_tasks + 1 + 8

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> GatewayCapacity:
        source = os.environ if env is None else env
        values: dict[str, int] = {}
        for field in fields(cls):
            key = "GOTC_GATEWAY_" + field.name.upper()
            raw = source.get(key)
            if raw is None:
                continue
            minimum = 0 if field.name in {"team_queue", "global_queue"} else 1
            maximum = 8192 if field.name == "request_limit" else 1024
            if not raw.isascii() or not raw.isdigit() or len(raw) > 4 or not minimum <= int(raw) <= maximum:
                raise ValueError(f"{key} must be an integer from {minimum} to {maximum}")
            values[field.name] = int(raw)
        result = cls(**values)
        for suffix in ("running", "queue", "uploads", "speech", "tasks"):
            if getattr(result, "team_" + suffix) > getattr(result, "global_" + suffix):
                raise ValueError(f"GOTC_GATEWAY_TEAM_{suffix.upper()} exceeds GOTC_GATEWAY_GLOBAL_{suffix.upper()}")
        if result.total_requests < result.minimum_requests:
            raise ValueError(f"GOTC_GATEWAY_REQUEST_LIMIT must be at least {result.minimum_requests} for these settings")
        return result


@dataclass(eq=False)
class _Waiter:
    ready: asyncio.Future[None]
    granted: bool = False


class FairSlots:
    """Serve one FIFO waiter per eligible team in rotation, reserving its slot before waking it."""

    def __init__(self, *, team_running: int, global_running: int, team_queue: int = 0, global_queue: int = 0) -> None:
        self.team_running = team_running
        self.global_running = global_running
        self.team_queue = team_queue
        self.global_queue = global_queue
        self.running: Counter[str] = Counter()
        self._queues: OrderedDict[str, deque[_Waiter]] = OrderedDict()
        self._lock = threading.Lock()

    def snapshot(self) -> tuple[Counter[str], Counter[str]]:
        with self._lock:
            return self.running.copy(), Counter({team: len(queue) for team, queue in self._queues.items()})

    def queued(self, team: str) -> int:
        with self._lock:
            return len(self._queues.get(team, ()))

    async def acquire(self, team: str, timeout: float, *,
                      wait_for_grant: Callable[[asyncio.Future[None]], Awaitable[None]] | None = None) -> None:
        with self._lock:
            if self.running[team] < self.team_running and self.running.total() < self.global_running:
                self.running[team] += 1
                return
            if (len(self._queues.get(team, ())) >= self.team_queue
                    or sum(map(len, self._queues.values())) >= self.global_queue):
                raise GatewayError("rate_limit", "排队请求已满，请稍后重试", retry_after=2)
            ticket = _Waiter(asyncio.get_running_loop().create_future())
            self._queues.setdefault(team, deque()).append(ticket)

        try:
            async with asyncio.timeout(timeout):
                if wait_for_grant is None:
                    await asyncio.shield(ticket.ready)
                else:
                    await wait_for_grant(ticket.ready)
        except BaseException as exc:
            with self._lock:
                if ticket.granted:
                    # Cancellation or expiry raced the handoff from another request.
                    self._release(team)
                else:
                    self._queues[team].remove(ticket)
                    if not self._queues[team]:
                        del self._queues[team]
            if isinstance(exc, TimeoutError):
                raise GatewayError("rate_limit", "中转请求排队超时，请稍后重试", retry_after=2) from None
            raise

    def release(self, team: str) -> None:
        with self._lock:
            self._release(team)

    def _release(self, team: str) -> None:
        self.running[team] -= 1
        if not self.running[team]:
            del self.running[team]
        # Each release frees one slot. Teams already at their own limit keep their place.
        for candidate, queue in self._queues.items():
            if self.running[candidate] < self.team_running:
                ticket = queue.popleft()
                if queue:
                    self._queues.move_to_end(candidate)
                else:
                    del self._queues[candidate]
                self.running[candidate] += 1
                ticket.granted = True
                ticket.ready.get_loop().call_soon_threadsafe(ticket.ready.set_result, None)
                return


class BodyUploads(FairSlots):
    def __init__(self, capacity: GatewayCapacity) -> None:
        super().__init__(team_running=capacity.team_uploads, global_running=capacity.global_uploads,
                         team_queue=UPLOAD_TEAM_QUEUE, global_queue=UPLOAD_QUEUE)
