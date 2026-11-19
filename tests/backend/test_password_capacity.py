"""Check the bounded FIFO using occupied slots, without generating traffic."""
import asyncio
import threading
import unittest

from backend.gotc.password_capacity import PasswordCapacity, PasswordChecks
from backend.gotc.errors import APIError


class PasswordCapacityTests(unittest.IsolatedAsyncioTestCase):
    async def test_admission_fifo_timeout_cancellation_and_recovery(self):
        gate = PasswordCapacity(running=1, waiting=2, timeout=1)
        first, second, third = [gate.reserve() for _ in range(3)]
        self.assertTrue(first.done())
        self.assertFalse(second.done())
        self.assertFalse(third.done())
        with self.assertRaises(APIError) as refused:
            gate.reserve()
        self.assertEqual((refused.exception.status, refused.exception.code), (429, "login_busy"))
        gate.release(first)
        self.assertTrue(second.done())
        self.assertFalse(third.done())
        gate.release(third)
        gate.timeout = 0
        with self.assertRaises(APIError):
            with gate.slot():
                self.fail('Timed out waiter was admitted')
        gate.timeout = 5
        async def wait():
            await gate.run(lambda: asyncio.to_thread(self.fail, 'Cancelled waiter was admitted'))
        task = asyncio.create_task(wait())
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        gate.release(second)
        def nested_check():
            with gate.slot():
                self.assertEqual((gate.active, len(gate.queue)), (1, 0))
        await gate.run(lambda: asyncio.to_thread(nested_check))
        self.assertEqual((gate.active, len(gate.queue), gate.peak_active, gate.peak_waiting), (0, 0, 1, 2))


    async def test_cancelled_request_keeps_its_admitted_worker_slot_until_completion(self):
        gate = PasswordCapacity(running=1, waiting=0)
        entered, finish = threading.Event(), threading.Event()
        def operation():
            entered.set()
            finish.wait(2)
        task = asyncio.create_task(gate.run(lambda: asyncio.to_thread(operation)))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            with self.assertRaises(APIError):
                gate.reserve()
        finally:
            finish.set()
        async with asyncio.timeout(2):
            while gate.active:
                await asyncio.sleep(0.001)

    async def test_cooling_requests_leave_ordinary_and_other_role_lanes_available(self):
        checks = PasswordChecks()
        lane = checks.organizer_cooling
        self.assertIsNot(lane, checks.participant_cooling)
        occupied = lane.reserve()

        async def pressured():
            checks.require_cooling('admin')
            return True

        async def ordinary():
            return True

        request = asyncio.create_task(checks.run(pressured))
        tickets = []
        try:
            async with asyncio.timeout(1):
                while len(lane.queue) < 1:
                    await asyncio.sleep(0)
            self.assertEqual(checks.capacity.active, 0)
            for _ in range(lane.waiting - 1):
                tickets.append(lane.reserve())
            with self.assertRaises(APIError) as full:
                await checks.run(pressured)
            self.assertEqual((full.exception.status, full.exception.code), (429, 'login_busy'))
            self.assertTrue(await checks.run(ordinary))
            self.assertEqual(checks.participant_cooling.active, 0)
        finally:
            request.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await request
            for ticket in tickets:
                lane.release(ticket)
            lane.release(occupied)
        self.assertEqual((lane.active, len(lane.queue)), (0, 0))
