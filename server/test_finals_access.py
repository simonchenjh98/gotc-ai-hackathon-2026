"""Independent finals invitations cannot become normal login or administrator sessions."""
from contextlib import contextmanager
import hashlib
import json
import threading
import unittest
from unittest.mock import patch

from app import EventService
from finals import DIMENSIONS, FINALS_ACCESS_TTL_MS
from test_api import Client
import test_finals

FINALS_INVITATION = test_finals.FINALS_INVITATION


class FinalsAccessTests(unittest.TestCase):
    for name in ('setUp', 'tearDown', 'team', 'invitation', 'request', 'admin_view', 'settings',
                 'configure', 'score', 'ballot', 'authorize'):
        locals()[name] = getattr(test_finals.FinalsTests, name)

    def test_grant_has_no_cookie_account_or_normal_session_and_stores_no_credentials(self):
        with self.service.db() as db:
            before = {table: [tuple(row) for row in db.execute('SELECT * FROM '+table)] for table in ('accounts', 'sessions', 'audit')}
        client = Client(self.server)
        granted = self.authorize(client)
        token = granted['accessToken']
        self.assertEqual(client.cookie, '')
        self.assertRegex(token, r'^[A-Za-z0-9_-]{43}$')
        self.assertGreater(granted['expiresAt'], self.service.finals.clock())
        self.assertLessEqual(granted['expiresAt'], self.service.finals.clock()+FINALS_ACCESS_TTL_MS)
        with self.service.db() as db:
            for table, rows in before.items():
                self.assertEqual([tuple(row) for row in db.execute('SELECT * FROM '+table)], rows)
            stored = dict(db.execute('SELECT * FROM finals_access_tokens WHERE token_hash=?', (hashlib.sha256(token.encode()).hexdigest(),)).fetchone())
            self.assertNotIn(token, json.dumps(stored))
            self.assertNotIn(FINALS_INVITATION, json.dumps(stored))
            self.assertEqual(stored['expires_at'], granted['expiresAt'])
        self.assertIsNone(client.get('/api/session')[1]['role'])
        for path in ('/api/admin', '/api/admin/finals', '/api/judge', '/api/team'):
            self.assertEqual(client.request('GET', path, headers={'X-GOTC-Finals-Access': token})[0], 401)
        client.cookie = 'gotc_session='+token
        self.assertIsNone(client.get('/api/session')[1]['role'])

    def test_missing_or_forged_access_cannot_read_seats_or_score_even_with_admin_cookie(self):
        self.configure()
        score = {'runId': self.run_id, 'teamId': self.teams[0], 'expectedRevision': 0,
                 'status': 'submitted', 'scores': [10]*6, 'feedback': ''}
        ballot = {'runId': self.run_id, 'expectedRevision': 0, 'status': 'submitted',
                  'nominations': {dimension['id']: None for dimension in DIMENSIONS}}
        routes = [('GET', '/api/finals/entry', None), ('GET', '/api/finals/judge', None),
                  ('POST', '/api/finals/score', score), ('POST', '/api/finals/nominations', ballot)]
        for client in (self.public, self.admin, self.judges[0]):
            for token in ('', 'a'*43, FINALS_INVITATION):
                for method, path, body in routes:
                    status, result, _ = client.request(method, path, body, headers={
                        'X-GOTC-Finals-Judge': self.seats[0]['id'], 'X-GOTC-Finals-Access': token})
                    self.assertEqual(status, 401, (path, result))
                    self.assertEqual(result['code'], 'finals_access_required')
        self.assertEqual(self.public.get('/api/finals/board')[0], 200)
        with self.service.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM finals_scores').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM finals_nominations').fetchone()[0], 0)

    def test_unconfigured_fails_closed_and_bad_invitation_origin_and_fields_are_rejected(self):
        del self.service.shared_login_codes['admin']
        for path, body in [('/api/finals/access', {'code': FINALS_INVITATION}), ('/api/finals/entry', None)]:
            result = self.public.request('GET' if body is None else 'POST', path, body)
            self.assertEqual(result[0], 503)
            self.assertEqual(result[1]['code'], 'finals_access_unavailable')
        self.service.shared_login_codes['admin'] = FINALS_INVITATION
        self.assertEqual(self.public.post('/api/finals/access', {'code': 'wrong'})[0], 401)
        for body in ({}, {'code': []}, {'code': FINALS_INVITATION, 'role': 'admin'}, {'code': 'x'*161}):
            self.assertEqual(self.public.post('/api/finals/access', body)[0], 400)
        self.assertEqual(self.public.request('POST', '/api/finals/access', {'code': FINALS_INVITATION}, origin=False)[0], 403)
        self.assertEqual(self.public.get('/api/finals/access')[0], 404)

    def test_wrong_codes_are_rate_limited_without_issuing_tokens(self):
        with self.service.rate_lock:
            self.service.login_attempts.clear()
        with self.service.db() as db:
            before = db.execute('SELECT COUNT(*) FROM finals_access_tokens').fetchone()[0]
        for _ in range(20):
            self.assertEqual(self.public.post('/api/finals/access', {'code': 'wrong-fixture-code'})[0], 401)
        self.assertEqual(self.public.post('/api/finals/access', {'code': 'wrong-fixture-code'})[0], 429)
        with self.service.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM finals_access_tokens').fetchone()[0], before)

    def test_expiry_and_code_rotation_revoke_reads_writes_without_affecting_public_board(self):
        self.configure()
        judge = self.judges[0]
        granted = self.authorize(judge)
        with patch.object(self.service.finals, 'clock', return_value=granted['expiresAt']):
            self.assertEqual(self.request(judge, 'GET', '/api/finals/entry')[0], 401)
            self.assertEqual(self.score(judge)[0], 401)
            self.assertEqual(self.ballot(judge)[0], 401)
        self.service.shared_login_codes['admin'] = 'rotated-finals-fixture'
        self.assertEqual(self.request(judge, 'GET', '/api/finals/judge')[0], 401)
        self.assertEqual(self.score(judge)[0], 401)
        self.authorize(judge, code='rotated-finals-fixture')
        self.assertEqual(self.score(judge)[0], 200)
        self.assertEqual(self.public.get('/api/finals/board')[0], 200)

    def test_tokens_survive_same_code_restart_and_fail_after_private_code_rotation(self):
        client = Client(self.server)
        self.authorize(client)
        private_file = self.service.data_dir/'shared-login-codes.json'
        private_file.write_text(json.dumps({'admin': FINALS_INVITATION}), encoding='utf-8')
        old = self.service
        self.service = EventService(data_dir=old.data_dir, origin=old.origin, static_root=old.root, base_path=old.base_path)
        self.server.service = self.service
        self.assertEqual(self.request(client, 'GET', '/api/finals/entry')[0], 200)
        private_file.write_text(json.dumps({'admin': 'rotated-private-fixture'}), encoding='utf-8')
        old = self.service
        self.service = EventService(data_dir=old.data_dir, origin=old.origin, static_root=old.root, base_path=old.base_path)
        self.server.service = self.service
        self.assertEqual(self.request(client, 'GET', '/api/finals/entry')[0], 401)

    def test_queued_write_checks_invitation_after_acquiring_writer_lock(self):
        self.configure()
        original_db = self.service.db
        queued = threading.Event()
        @contextmanager
        def traced_db():
            with original_db() as db:
                db.set_trace_callback(lambda statement: queued.set() if statement == 'BEGIN IMMEDIATE' else None)
                yield db
        result = []
        with original_db() as lock:
            lock.execute('BEGIN IMMEDIATE')
            with patch.object(self.service, 'db', traced_db):
                worker = threading.Thread(target=lambda: result.append(self.score(self.judges[0])))
                worker.start()
                self.assertTrue(queued.wait(3), 'Finals score did not queue for the writer lock')
                self.service.shared_login_codes['admin'] = 'changed-while-queued-fixture'
                lock.commit()
                worker.join(5)
                self.assertFalse(worker.is_alive())
        self.assertEqual(result[0][0], 401, result)
        with original_db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM finals_scores').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
