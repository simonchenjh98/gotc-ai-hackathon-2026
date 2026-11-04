"""Login concurrency regressions against isolated real HTTP services."""
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import unittest
from unittest.mock import patch

import app
import test_api as fixtures


class LoginConcurrencyTests(unittest.TestCase):
    setUp = fixtures.APITests.setUp
    tearDown = fixtures.APITests.tearDown
    invitation = fixtures.APITests.invitation

    def login_while_hash_is_paused(self, payload, mutate):
        hashing, release_hash = threading.Event(), threading.Event()
        original_hash = app.scrypt_hash
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

        with patch.object(app, 'scrypt_hash', paused_hash):
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
        original_hash = app.scrypt_hash
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

        with patch.object(app, 'scrypt_hash', slow_hash):
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

        def restart_with(code):
            shared_path.write_text(json.dumps({'judge': code}), encoding='utf-8')
            previous = self.service
            self.service = app.EventService(data_dir=previous.data_dir, base_path=previous.base_path,
                                            origin=previous.origin, static_root=previous.root)
            self.server.service = self.service

        restart_with(old_code)
        client, (status, result, headers) = self.login_while_hash_is_paused(
            {'role': 'judge', 'code': old_code, 'accountId': invitation['id']},
            lambda: restart_with(new_code))
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
        capacity = app.LOGIN_KDF_CONCURRENCY
        attempts = capacity + 2
        release_hash, slots_full, all_attempted = (threading.Event() for _ in range(3))
        counter_lock = threading.Lock()
        active = peak = attempted = 0
        original_hash, original_limit = app.scrypt_hash, self.service.rate_limit

        def counted_rate_limit(ip, role):
            nonlocal attempted
            original_limit(ip, role)
            with counter_lock:
                attempted += 1
                if attempted == attempts:
                    all_attempted.set()

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

        with patch.object(app, 'scrypt_hash', bounded_hash), patch.object(self.service, 'rate_limit', counted_rate_limit):
            with ThreadPoolExecutor(max_workers=attempts) as pool:
                futures = [pool.submit(fixtures.Client(self.server).post, '/api/login', {
                    'role': 'judge', 'code': invitation['code']}) for _ in range(attempts)]
                try:
                    self.assertTrue(slots_full.wait(3))
                    self.assertTrue(all_attempted.wait(3))
                    # A legitimate write can finish while all hash slots and queued logins are occupied.
                    self.assertEqual(self.admin.settings({'submissionsOpen': True})[0], 200)
                finally:
                    release_hash.set()
                self.assertEqual([future.result(5)[0] for future in futures], [200] * attempts)
        self.assertEqual(peak, capacity)

    def test_rate_limit_keeps_same_ip_and_global_role_buckets_atomic(self):
        statuses = []
        lock = threading.Lock()

        def attempt(ip):
            try:
                self.service.rate_limit(ip, 'team')
                status = 200
            except app.APIError as error:
                status = error.status
            with lock:
                statuses.append(status)

        with ThreadPoolExecutor(max_workers=12) as pool:
            list(pool.map(attempt, ['192.0.2.1'] * 25))
        self.assertEqual(statuses.count(200), 20)
        self.assertEqual(statuses.count(429), 5)
        for index in range(380):
            self.service.rate_limit('198.51.100.' + str(index), 'team')
        with self.assertRaises(app.APIError) as error:
            self.service.rate_limit('203.0.113.1', 'team')
        self.assertEqual(error.exception.status, 429)


if __name__ == '__main__':
    unittest.main(verbosity=2)
