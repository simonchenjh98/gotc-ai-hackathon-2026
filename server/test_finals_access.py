"""Public finals seats attribute scores without granting normal login authority."""
from contextlib import contextmanager
import threading
import unittest
from unittest.mock import patch

from app import EventService
import test_finals


class FinalsAccessTests(unittest.TestCase):
    for name in ('setUp', 'tearDown', 'team', 'invitation', 'request', 'admin_view', 'settings',
                 'configure', 'score', 'ballot'):
        locals()[name] = getattr(test_finals.FinalsTests, name)

    def test_entry_and_scoring_need_only_an_explicit_active_seat(self):
        self.service.shared_login_codes.clear()
        status, empty, headers = self.public.get('/api/finals/entry')
        self.assertEqual(status, 200, empty)
        self.assertEqual(empty['judges'], [])
        self.assertNotIn('Set-Cookie', headers)
        self.configure()
        entry = self.public.get('/api/finals/entry')[1]
        self.assertTrue(entry['config']['rosterReady'])
        self.assertEqual(len(entry['judges']), 8)
        judge = self.judges[0]
        self.assertEqual(judge.cookie, '')
        with self.service.db() as db:
            before_sessions = [tuple(row) for row in db.execute('SELECT * FROM sessions')]
        self.assertEqual(self.score(judge)[0], 200)
        self.assertEqual(self.ballot(judge)[0], 200)
        data = self.request(judge, 'GET', '/api/finals/judge')[1]
        self.assertEqual(len(data['myScores']), 1)
        self.assertEqual(data['myNominations']['status'], 'submitted')
        self.assertIsNone(judge.get('/api/session')[1]['role'])
        with self.service.db() as db:
            self.assertEqual([tuple(row) for row in db.execute('SELECT * FROM sessions')], before_sessions)

    def test_retired_access_route_never_accepts_or_discloses_an_admin_code(self):
        self.service.shared_login_codes['admin'] = 'synthetic-admin-only-code'
        with self.service.db() as db:
            before = {table: [tuple(row) for row in db.execute('SELECT * FROM ' + table)]
                      for table in ('accounts', 'sessions', 'audit')}
        for client in (self.public, self.admin):
            for body in ({'code': 'synthetic-admin-only-code'}, {'code': 'wrong'}, {}):
                status, result, headers = client.post('/api/finals/access', body)
                self.assertEqual(status, 404, result)
                self.assertNotIn('accessToken', result)
                self.assertNotIn('Set-Cookie', headers)
        self.assertEqual(self.public.get('/api/finals/access')[0], 404)
        self.assertEqual(self.public.get('/api/finals/entry')[0], 200)
        with self.service.db() as db:
            for table, rows in before.items():
                self.assertEqual([tuple(row) for row in db.execute('SELECT * FROM ' + table)], rows)

    def test_seat_headers_do_not_grant_admin_ordinary_judge_or_team_access(self):
        self.configure()
        headers = {'X-GOTC-Finals-Judge': self.seats[0]['id'], 'X-GOTC-Finals-Access': 'ignored-legacy-token'}
        for path in ('/api/admin', '/api/admin/finals', '/api/judge', '/api/team', '/api/admin/roadshow/scoring'):
            self.assertEqual(self.public.request('GET', path, headers=headers)[0], 401, path)
        for path in ('/api/score', '/api/admin/finals/settings', '/api/admin/teams'):
            self.assertEqual(self.public.request('POST', path, {}, headers=headers)[0], 401, path)
        self.assertIsNone(self.public.get('/api/session')[1]['role'])
        self.assertEqual(self.public.request('GET', '/api/finals/judge', headers=headers)[0], 200)
        self.assertEqual(self.public.cookie, '')

    def test_no_cookie_replaces_seat_checks_and_cross_origin_writes_stay_blocked(self):
        self.configure()
        body = {'runId': self.run_id, 'teamId': self.teams[0], 'expectedRevision': 0,
                'status': 'submitted', 'scores': [10] * 6, 'feedback': ''}
        for client in (self.public, self.admin):
            self.assertEqual(client.get('/api/finals/judge')[0], 400)
            self.assertEqual(client.post('/api/finals/score', body)[0], 400)
            self.assertEqual(client.request('GET', '/api/finals/judge', headers={
                'X-GOTC-Finals-Judge': 'not-current-seat'})[0], 403)
        for origin in (False, 'https://other.example'):
            response = self.public.request('POST', '/api/finals/score', body, origin=origin,
                headers={'X-GOTC-Finals-Judge': self.seats[0]['id']})
            self.assertEqual(response[0], 403, response)
        self.assertEqual(self.score(self.judges[0], runId='old-finals-run')[0], 409)
        with self.service.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM finals_scores').fetchone()[0], 0)

    def test_queued_score_rechecks_active_role_and_current_run_grant(self):
        self.configure()
        original_db = self.service.db
        changes = ["UPDATE accounts SET active=0 WHERE id=?", "DELETE FROM finals_judges WHERE judge_id=?",
                   "UPDATE accounts SET role='audience' WHERE id=?"]
        for index, sql in enumerate(changes):
            with self.subTest(sql=sql):
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
                        worker = threading.Thread(target=lambda: result.append(self.score(self.judges[index])))
                        worker.start()
                        self.assertTrue(queued.wait(3), 'Finals write did not wait for the database lock')
                        lock.execute(sql, (self.seats[index]['id'],))
                        lock.commit()
                        worker.join(5)
                        self.assertFalse(worker.is_alive())
                self.assertEqual(result[0][0], 403, result)
                self.assertEqual(self.request(self.judges[index], 'GET', '/api/finals/judge')[0], 403)
        self.assertFalse(self.public.get('/api/finals/entry')[1]['config']['rosterReady'])
        with original_db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM finals_scores').fetchone()[0], 0)

    def test_upgrade_keeps_obsolete_token_history_without_using_it(self):
        self.configure()
        with self.service.db() as db:
            db.execute('CREATE TABLE finals_access_tokens (token_hash TEXT PRIMARY KEY,code_tag TEXT NOT NULL,expires_at INTEGER NOT NULL)')
            db.execute('INSERT INTO finals_access_tokens VALUES (?,?,?)', ('legacy-hash', 'legacy-tag', 1))
        old = self.service
        self.service = EventService(data_dir=old.data_dir, origin=old.origin, static_root=old.root, base_path=old.base_path)
        self.server.service = self.service
        self.assertEqual(self.public.get('/api/finals/entry')[0], 200)
        self.assertEqual(self.score(self.judges[0])[0], 200)
        self.assertEqual(self.public.post('/api/finals/access', {'code': 'anything'})[0], 404)
        with self.service.db() as db:
            self.assertEqual([tuple(row) for row in db.execute('SELECT * FROM finals_access_tokens')],
                             [('legacy-hash', 'legacy-tag', 1)])


if __name__ == '__main__':
    unittest.main(verbosity=2)
