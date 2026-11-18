"""Capacity and scheduling at the service admission boundary, using synthetic teams."""
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase

from backend.gotc.gateway.admission import FairSlots, GatewayCapacity
from backend.gotc.gateway.asgi_context import GatewayContext
from backend.gotc.gateway.protocol_errors import ClientGone, GatewayError
from backend.gotc.service import EventService


class GatewayAdmissionTests(IsolatedAsyncioTestCase):
    async def test_two_busy_teams_leave_room_for_a_third(self):
        with TemporaryDirectory() as directory:
            service = EventService(data_dir=Path(directory) / 'synthetic')
            self.addCleanup(service.connections.close)
            gateway = service.keys
            started = []
            for _ in range(4):
                for team in ('one', 'two'):
                    started.append(asyncio.create_task(gateway.admit_async(team, 0)))
            try:
                await asyncio.sleep(0)
                await asyncio.wait_for(gateway.admit_async('three', 0), timeout=0.5)
                gateway.release('three')
            finally:
                for job in started:
                    job.cancel()
                await asyncio.gather(*started, return_exceptions=True)
                for team, count in gateway.model_slots.running.copy().items():
                    for _ in range(count):
                        gateway.release(team)

    async def test_default_team_share_preserves_running_and_queue_space_for_other_teams(self):
        capacity = GatewayCapacity()
        slots = FairSlots(team_running=capacity.team_running, global_running=capacity.global_running,
                          team_queue=capacity.team_queue, global_queue=capacity.global_queue)
        queued = []
        try:
            for team in ('one', 'two'):
                for _ in range(capacity.team_running):
                    await slots.acquire(team, 1)
                queued.extend(asyncio.create_task(slots.acquire(team, 1)) for _ in range(capacity.team_queue))
            await asyncio.sleep(0)
            self.assertEqual(slots.snapshot(), ({'one': 15, 'two': 15}, {'one': 16, 'two': 16}))
            with self.assertRaises(GatewayError):
                await slots.acquire('one', 1)
            await slots.acquire('third', 1)
            for number in range(33):
                await slots.acquire(f'other-{number}', 1)
            next_team = asyncio.create_task(slots.acquire('new-team', 1))
            queued.append(next_team)
            await asyncio.sleep(0)
            self.assertEqual(slots.queued('new-team'), 1)
            slots.release('other-0')
            await next_team
        finally:
            for job in queued:
                job.cancel()
            await asyncio.gather(*queued, return_exceptions=True)
            for team, count in slots.snapshot()[0].items():
                for _ in range(count):
                    slots.release(team)
        self.assertEqual(slots.snapshot(), ({}, {}))

    async def test_fifo_within_team_and_round_robin_between_teams(self):
        slots = FairSlots(team_running=1, global_running=1, team_queue=3, global_queue=8)
        await slots.acquire('initial', 1)
        order = []

        async def run(team, number):
            await slots.acquire(team, 1)
            try:
                order.append((team, number))
            finally:
                slots.release(team)

        calls = []
        for team, count in (('one', 3), ('two', 3), ('three', 2)):
            calls.extend(asyncio.create_task(run(team, i)) for i in range(count))
            await asyncio.sleep(0)
            if team == 'one':
                with self.assertRaises(GatewayError) as full:
                    await slots.acquire('one', 1)
                self.assertEqual(full.exception.status, 429)
        self.assertEqual(slots.snapshot()[1], {'one': 3, 'two': 3, 'three': 2})
        slots.release('initial')
        await asyncio.gather(*calls)
        self.assertEqual(order, [('one', 0), ('two', 0), ('three', 0),
                                 ('one', 1), ('two', 1), ('three', 1), ('one', 2), ('two', 2)])
        self.assertEqual(slots.snapshot(), ({}, {}))

    async def test_timeout_and_cancellation_return_pending_and_granted_slots(self):
        slots = FairSlots(team_running=1, global_running=1, team_queue=2, global_queue=3)
        await slots.acquire('active', 1)
        with self.assertRaises(GatewayError):
            await slots.acquire('expired', 0.001)
        cancelled, next_team = [asyncio.create_task(slots.acquire(team, 1)) for team in ('cancel', 'next')]
        await asyncio.sleep(0)
        slots.release('active')
        # A request can be cancelled after it owns a reservation but before its coroutine resumes.
        cancelled.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await cancelled
        await next_team
        self.assertEqual(slots.snapshot(), ({'next': 1}, {}))
        slots.release('next')

    async def test_disconnect_at_handoff_passes_the_reserved_slot_to_the_next_team(self):
        slots = FairSlots(team_running=1, global_running=1, team_queue=1, global_queue=2)
        await slots.acquire('active', 1)
        reading, departed = asyncio.Event(), asyncio.Event()

        async def receive():
            reading.set()
            await departed.wait()
            return {"type": "http.disconnect"}

        # Use the ASGI wait implementation with an already consumed synthetic request.
        ctx = object.__new__(GatewayContext)
        ctx.request = SimpleNamespace(receive=receive)
        leaving = asyncio.create_task(slots.acquire('leaving', 1, wait_for_grant=ctx.wait_for_grant))
        waiting = asyncio.create_task(slots.acquire('next', 1))
        await asyncio.wait_for(reading.wait(), 1)
        departed.set()
        slots.release('active')
        with self.assertRaises(ClientGone):
            await leaving
        await waiting
        self.assertEqual(slots.snapshot(), ({'next': 1}, {}))
        slots.release('next')

    async def test_speech_and_task_operations_leave_all_model_slots_available(self):
        with TemporaryDirectory() as directory:
            service = EventService(data_dir=Path(directory) / 'synthetic')
            self.addCleanup(service.connections.close)
            gateway = service.keys
            for lane in ('speech', 'task'):
                slots = gateway.lanes[lane]
                for number in range(slots.global_running):
                    await gateway.admit_async(str(number), 0, lane=lane)
                with self.assertRaises(GatewayError) as overloaded:
                    await gateway.admit_async('another', 0, lane=lane)
                self.assertEqual(overloaded.exception.status, 429)
            for number in range(gateway.capacity.global_running):
                await gateway.admit_async(str(number), 0)
            self.assertEqual(gateway.model_slots.running.total(), 64)
            for lane, slots in gateway.lanes.items():
                for team in list(slots.running):
                    gateway.release(team, lane=lane)
            self.assertEqual(gateway.admission_snapshot(), ({}, {}))

    def test_capacity_settings_are_validated_at_startup(self):
        defaults = GatewayCapacity.from_env({})
        self.assertEqual((defaults.team_running, defaults.global_running, defaults.team_queue,
                          defaults.global_queue, defaults.total_requests, defaults.provider_connections),
                         (15, 64, 16, 64, 272, 81))
        valid = GatewayCapacity.from_env({'GOTC_GATEWAY_TEAM_RUNNING': '20', 'GOTC_GATEWAY_GLOBAL_RUNNING': '80'})
        self.assertEqual((valid.total_requests, valid.provider_connections), (288, 97))
        cases = [('TEAM_RUNNING', '0'), ('TEAM_QUEUE', '-1'), ('GLOBAL_QUEUE', 'no'),
                 ('TEAM_RUNNING', '65'), ('TEAM_UPLOADS', '97'), ('TEAM_SPEECH', '9'),
                 ('TEAM_TASKS', '9'), ('REQUEST_LIMIT', '271'), ('GLOBAL_RUNNING', '1025')]
        for field, value in cases:
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'GOTC_GATEWAY'):
                GatewayCapacity.from_env({'GOTC_GATEWAY_' + field: value})
