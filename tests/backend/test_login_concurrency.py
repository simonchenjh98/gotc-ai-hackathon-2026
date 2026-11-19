"""Login concurrency regressions against isolated real HTTP services."""
from concurrent.futures import ThreadPoolExecutor
from collections import deque
import hashlib
import json
import time
import threading
import unittest
from unittest.mock import patch

from backend.gotc import service as service_module
import test_api as fixtures


class LoginConcurrencyTests(unittest.TestCase):
    setUp = fixtures.APITests.setUp
    tearDown = fixtures.APITests.tearDown
    invitation = fixtures.APITests.invitation

    def login_while_hash_is_paused(self, payload, mutate):
        hashing, release_hash = threading.Event(), threading.Event()
        original_hash = service_module.scrypt_hash
        client = fixtures.Client(self.server)
        outcome, failures = [], []

        def paused_hash(code, salt):
            if code == payload['code']:
                hashing.set()
                if not release_hash.wait(8):
                    raise RuntimeError('Test did not release the synthetic login hash')
            return original_hash(code, salt)

        def login():
            try:
                outcome.append(client.post('/api/login', payload))
            except Exception as error:
                failures.append(error)

        with patch.object(service_module, 'scrypt_hash', paused_hash):
            thread = threading.Thread(target=login, daemon=True)
            thread.start()
            try:
                self.assertTrue(hashing.wait(3), 'Login did not enter password verification')
                mutate()
                self.assertFalse(release_hash.is_set())
            finally:
                release_hash.set()
                thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(len(outcome), 1)
        return client, outcome[0]

    def test_slow_login_hash_does_not_block_an_unrelated_authorized_write(self):
        _, invitation = self.invitation('judge', 'Synthetic login probe')
        hashing, release_hash, write_finished = (threading.Event() for _ in range(3))
        original_hash = service_module.scrypt_hash
        outcomes = {}
        failures = []

        def slow_hash(code, salt):
            if code == invitation['code']:
                hashing.set()
                if not release_hash.wait(8):
                    raise RuntimeError('Test did not release the synthetic login hash')
            return original_hash(code, salt)

        def login():
            try:
                outcomes['login'] = fixtures.Client(self.server).post('/api/login', {
                    'role': 'judge', 'code': invitation['code']})
            except Exception as error:
                failures.append(error)

        def write():
            try:
                outcomes['write'] = self.admin.settings({'submissionsOpen': True})
            except Exception as error:
                failures.append(error)
            finally:
                write_finished.set()

        with patch.object(service_module, 'scrypt_hash', slow_hash):
            login_thread = threading.Thread(target=login)
            write_thread = threading.Thread(target=write)
            login_thread.start()
            try:
                self.assertTrue(hashing.wait(3), 'Login did not enter password verification')
                write_thread.start()
                self.assertTrue(write_finished.wait(2), 'An unrelated write waited for login hashing')
                self.assertEqual(outcomes['write'][0], 200, outcomes)
                self.assertFalse(release_hash.is_set())
            finally:
                release_hash.set()
                login_thread.join(5)
                if write_thread.ident is not None:
                    write_thread.join(5)
        self.assertFalse(login_thread.is_alive())
        self.assertFalse(write_thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(outcomes['login'][0], 200, outcomes)
        self.assertTrue(self.admin.get('/api/admin')[1]['config']['submissionsOpen'])

    def test_revocation_or_rotation_committed_during_hashing_rejects_the_old_proof(self):
        for action in ('revoke', 'rotate'):
            with self.subTest(action=action):
                _, invitation = self.invitation('judge', 'Synthetic ' + action)

                def change_credential():
                    status, result, _ = self.admin.post('/api/admin/credentials', {
                        'role': 'judge', 'id': invitation['id'], 'action': action})
                    self.assertEqual(status, 200, result)
                    with self.service.db() as db:
                        self.assertEqual(db.execute('SELECT COUNT(*) FROM sessions WHERE account_id=?',
                                                    (invitation['id'],)).fetchone()[0], 0)

                client, (status, result, headers) = self.login_while_hash_is_paused(
                    {'role': 'judge', 'code': invitation['code']}, change_credential)
                self.assertEqual(status, 401, result)
                self.assertNotIn('Set-Cookie', headers)
                self.assertIsNone(client.get('/api/session')[1]['role'])
                with self.service.db() as db:
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM sessions WHERE account_id=?',
                                                (invitation['id'],)).fetchone()[0], 0)

    def test_shared_code_update_during_hashing_rejects_the_old_proof(self):
        _, invitation = self.invitation('judge', 'Synthetic shared login')
        old_code, new_code = 'SYNTHETIC_SHARED_BEFORE', 'SYNTHETIC_SHARED_AFTER'
        shared_path = self.service.data_dir / 'shared-login-codes.json'

        def service_with_shared_code(code):
            shared_path.write_text(json.dumps({'judge': code}), encoding='utf-8')
            previous = self.service
            instance = service_module.EventService(data_dir=previous.data_dir, base_path=previous.base_path,
                                                   origin=previous.origin, static_root=previous.root)
            self.addCleanup(instance.connections.close)
            return instance

        self.service = service_with_shared_code(old_code)
        self.server.service = self.service

        def rotate_credentials():
            # Startup of another instance commits the new salt/hash to the shared DB.
            # Keep the original listener alive to finish validating its in-flight proof.
            self.service = service_with_shared_code(new_code)

        client, (status, result, headers) = self.login_while_hash_is_paused(
            {'role': 'judge', 'code': old_code, 'accountId': invitation['id']}, rotate_credentials)
        self.server.service = self.service
        self.assertEqual(status, 401, result)
        self.assertNotIn('Set-Cookie', headers)
        self.assertIsNone(client.get('/api/session')[1]['role'])
        with self.service.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM sessions WHERE account_id=?',
                                        (invitation['id'],)).fetchone()[0], 0)
        fresh = fixtures.Client(self.server)
        self.assertEqual(fresh.post('/api/login', {
            'role': 'judge', 'code': new_code, 'accountId': invitation['id']})[0], 200)

    def test_concurrent_password_checks_are_bounded_and_do_not_hold_the_writer_lock(self):
        _, invitation = self.invitation('judge', 'Synthetic bounded login')
        capacity = service_module.LOGIN_KDF_CONCURRENCY
        release_hash, slots_full = threading.Event(), threading.Event()
        counter_lock = threading.Lock()
        active = peak = 0
        original_hash = service_module.scrypt_hash

        def bounded_hash(code, salt):
            nonlocal active, peak
            if code != invitation['code']:
                return original_hash(code, salt)
            with counter_lock:
                active += 1
                peak = max(peak, active)
                if active == capacity:
                    slots_full.set()
            try:
                if not release_hash.wait(8):
                    raise RuntimeError('Test did not release synthetic password checks')
                return original_hash(code, salt)
            finally:
                with counter_lock:
                    active -= 1

        with patch.object(service_module, 'scrypt_hash', bounded_hash):
            with ThreadPoolExecutor(max_workers=capacity) as pool:
                futures = [pool.submit(fixtures.Client(self.server).post, '/api/login', {
                    'role': 'judge', 'code': invitation['code']}) for _ in range(capacity)]
                try:
                    self.assertTrue(slots_full.wait(3))
                    with patch.object(self.service.password_checks.capacity, 'timeout', 0), self.assertRaises(service_module.APIError) as busy:
                        self.service.login({'role': 'judge', 'code': invitation['code']}, 'synthetic-ip')
                    self.assertEqual((busy.exception.status, busy.exception.code), (429, 'login_busy'))
                    self.assertEqual(self.admin.settings({'submissionsOpen': True})[0], 200)
                finally:
                    release_hash.set()
                self.assertEqual([future.result(5)[0] for future in futures], [200] * capacity)
        self.assertEqual(peak, capacity)

    def test_full_gate_returns_immediate_retry_after_and_preserves_normal_api(self):
        _, invitation = self.invitation('judge', 'Synthetic queued judge')
        gate = self.service.password_checks.capacity
        tickets = [gate.reserve() for _ in range(gate.running + gate.waiting)]
        try:
            with patch.object(service_module, 'scrypt_hash', side_effect=AssertionError('unexpected hash')):
                start = time.monotonic()
                status, result, headers = self.public.post('/api/login', {'role':'judge','code':invitation['code']})
                self.assertLess(time.monotonic() - start, 1.0)
                self.assertEqual((status, result['code'], headers['Retry-After']), (429, 'login_busy', '1'))
            self.assertEqual(self.admin.get('/api/admin')[0], 200)
        finally:
            for ticket in reversed(tickets):
                gate.release(ticket)
        self.assertEqual((gate.active, len(gate.queue), gate.peak_waiting), (0, 0, 40))

    def test_account_pressure_accepts_a_valid_login_from_a_fresh_peer(self):
        _, invitation = self.invitation('judge', 'Synthetic active judge')
        digest = hashlib.sha256(invitation['id'].encode()).digest()
        history = deque((time.monotonic(), object()) for _ in range(20))
        self.service.login_attempts[('login-account', 'judge', digest)] = history
        with patch('time.sleep') as delay:
            result = self.service.login({'role': 'judge', 'code': invitation['code']}, 'new-peer')
        self.assertTrue(result[0]['ok'])
        delay.assert_called_once()
        self.assertEqual(len(history), 20)

    def test_pressure_delay_releases_kdf_capacity_for_an_unrelated_valid_user(self):
        _, pressured = self.invitation('judge', 'Synthetic pressured judge')
        _, normal = self.invitation('judge', 'Synthetic unrelated judge')
        target = hashlib.sha256(pressured['id'].encode()).digest()
        self.service.login_attempts[('login-account', 'judge', target)] = deque(
            (time.monotonic(), None) for _ in range(20))
        cooling, resume = threading.Event(), threading.Event()
        def controlled_delay(_seconds):
            cooling.set()
            if not resume.wait(4):
                raise AssertionError('Synthetic delay timed out')
        with ThreadPoolExecutor(max_workers=1) as worker, patch('time.sleep', controlled_delay):
            delayed = worker.submit(self.service.login, {'role':'judge', 'code':pressured['code']}, 'new-peer')
            try:
                self.assertTrue(cooling.wait(2))
                self.assertEqual(self.service.password_checks.capacity.active, 0)
                self.assertEqual(self.public.post('/api/login', {'role':'judge', 'code':normal['code']})[0], 200)
            finally:
                resume.set()
            self.assertTrue(delayed.result(2)[0]['ok'])

    def test_peer_and_organizer_failure_controls(self):
        for key in [('login-ip', 'one-peer'),
                    ('login-peer-account', 'one-peer', 'judge', hashlib.sha256(b'target').digest())]:
            self.service.login_attempts.clear()
            self.service.login_attempts[key] = deque((time.monotonic(), object()) for _ in range(20))
            with self.assertRaises(service_module.APIError) as denied:
                with self.service.login_attempt('one-peer', 'judge', 'target'):
                    self.fail('Saturated peer budget admitted a check')
            self.assertEqual(denied.exception.status, 429)
        self.service.login_attempts.clear()
        self.service.organizer_failures.extend((time.monotonic(), None) for _ in range(20))
        def try_organizer():
            with self.service.login_attempt('fresh', 'admin', 'another-selector'):
                return True
        with patch('time.sleep') as delay:
            self.assertTrue(self.service.password_checks.run_sync(try_organizer))
            delay.assert_called_once()

if __name__ == '__main__':
    unittest.main(verbosity=2)
