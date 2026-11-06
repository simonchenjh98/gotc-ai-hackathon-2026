"""Roster-only name login over real HTTP with disposable databases."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
import json
import sqlite3
import threading
import unittest
from unittest.mock import patch

import test_api as fixtures
import test_roadshow_access as access_fixtures


class RoadshowNameLoginTests(unittest.TestCase):
    setUp = fixtures.APITests.setUp
    tearDown = fixtures.APITests.tearDown
    team = fixtures.APITests.team
    invitation = fixtures.APITests.invitation
    admin_request = access_fixtures.RoadshowAccessTests.admin_request
    view = access_fixtures.RoadshowAccessTests.view
    save = access_fixtures.RoadshowAccessTests.save
    configure = access_fixtures.RoadshowAccessTests.configure
    stage = access_fixtures.RoadshowAccessTests.stage
    add_team = access_fixtures.RoadshowAccessTests.add_team
    endpoint = '/api/roadshow/login'

    def login(self, name='张三', client=None):
        client = client or fixtures.Client(self.server)
        status, data, headers = client.post(self.endpoint, {'name': name})
        if status == 200:
            client.login_id = data['loginId']
        return client, status, data, headers

    def numeric(self, client, method='GET', body=None):
        path = '/api/roadshow/scoring' if method == 'GET' else '/api/roadshow/score'
        return client.request(method, path, body, headers={'X-GOTC-Login': client.login_id})

    def test_empty_unknown_and_ordinary_accounts_cannot_sign_in(self):
        before = self.public.get('/api/roadshow/access')[1]
        self.assertFalse(before['nameLoginAvailable'])
        self.assertEqual(self.login()[1], 409)
        self.invitation(name='Existing ordinary judge')
        self.configure(['张三'])
        for name in ['Existing ordinary judge', 'Administrator', '王五', '张']:
            with self.subTest(name=name):
                client, status, _, headers = self.login(name)
                self.assertEqual(status, 404)
                self.assertNotIn('Set-Cookie', headers)
                self.assertIsNone(client.get('/api/session')[1]['role'])
        with self.service.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM roadshow_access_roster').fetchone()[0], 1)

    def test_name_login_sets_private_session_without_claiming_or_changing_credentials(self):
        configured = self.configure(['张三', 'Ａlice  Wang'])
        target = next(row['id'] for row in configured['judges'] if row['name'] == 'Alice Wang')
        with self.service.db() as db:
            original = tuple(db.execute('SELECT salt,code_hash FROM accounts WHERE id=?', (target,)).fetchone())
        client, status, data, headers = self.login('  ALICE\tWang  ')
        self.assertEqual(status, 200, data)
        self.assertEqual((data['ok'], data['role'], data['name']), (True, 'judge', 'Alice Wang'))
        self.assertNotIn('code', data)
        self.assertIn('HttpOnly', headers['Set-Cookie'])
        self.assertIn('SameSite=Strict', headers['Set-Cookie'])
        self.assertEqual(headers['Cache-Control'], 'no-store')
        session = client.get('/api/session')[1]
        self.assertEqual(session['loginId'], data['loginId'])
        self.assertEqual(session['role'], 'judge')
        self.assertEqual(self.numeric(client)[0], 200)
        public = self.public.get('/api/roadshow/access')[1]
        self.assertTrue(public['nameLoginAvailable'])
        self.assertNotIn('Alice', json.dumps(public))
        admin = next(row for row in self.view()['judges'] if row['id'] == target)
        self.assertTrue(admin['canLogin'])
        self.assertFalse(admin['claimed'])
        with self.service.db() as db:
            self.assertEqual(tuple(db.execute('SELECT salt,code_hash FROM accounts WHERE id=?', (target,)).fetchone()), original)
            self.assertIsNone(db.execute('SELECT claimed_at FROM roadshow_access_roster WHERE judge_id=?', (target,)).fetchone()[0])
        self.assertEqual(self.public.post('/api/login', {'role': 'judge', 'code': 'Alice Wang', 'accountId': target})[0], 401)

    def test_csrf_exact_route_body_and_name_validation_never_create_sessions(self):
        self.configure(['张三'])
        for origin in [False, 'https://other.example']:
            response = self.public.request('POST', self.endpoint, {'name': '张三'}, origin=origin)
            self.assertEqual(response[0], 403)
            self.assertNotIn('Set-Cookie', response[2])
        for method, path in [('GET', self.endpoint), ('POST', self.endpoint + '/'), ('POST', self.endpoint + '?name=张三')]:
            # Keep query ASCII so the HTTP client itself does not reject the request.
            path = path.replace('张三', 'invalid')
            response = self.public.request(method, path, {'name': '张三'})
            self.assertEqual(response[0], 400 if '?' in path else 404)
            self.assertNotIn('Set-Cookie', response[2])
        invalid = [{}, {'name': ''}, {'name': '   '}, {'name': None}, {'name': True}, {'name': []},
                   {'name': '\u200b'}, {'name': 'x' * 101}, {'name': '张三', 'role': 'admin'},
                   {'name': '张三', 'accountId': 'other'}]
        for body in invalid:
            with self.subTest(body=body):
                response = self.public.post(self.endpoint, body)
                self.assertEqual(response[0], 400)
                self.assertNotIn('Set-Cookie', response[2])
        with self.service.db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM sessions s JOIN accounts a ON a.id=s.account_id WHERE a.role='judge'").fetchone()[0], 0)

    def test_dedicated_session_is_roadshow_only_and_generic_login_is_unchanged(self):
        self.configure(['张三'])
        client, status, _, _ = self.login()
        self.assertEqual(status, 200)
        for method, path in [('GET', '/api/judge'), ('POST', '/api/score'),
                             ('GET', '/api/judging/exhibition'), ('GET', '/api/judging/exhibition/'),
                             ('POST', '/api/judging/exhibition/review'), ('GET', '/api/admin'),
                             ('GET', '/api/admin/roadshow/scoring'), ('GET', '/api/team')]:
            with self.subTest(path=path):
                response = client.request(method, path, {} if method == 'POST' else None,
                                          headers={'X-GOTC-Login': client.login_id})
                self.assertEqual(response[0], 403, response)
        self.assertEqual(client.get('/api/finals/judge')[0], 400)
        self.assertEqual(client.get('/api/judging/roadshow')[0], 200)
        self.assertEqual(client.get('/api/board')[0], 200)
        ordinary, _ = self.invitation(name='Ordinary existing judge')
        self.assertEqual(ordinary.get('/api/judge')[0], 200)

    def test_removal_and_current_run_grant_revocation_reject_new_and_old_sessions(self):
        self.configure(['张三', '李四'])
        client, status, _, _ = self.login()
        self.assertEqual(status, 200)
        self.assertEqual(self.save(['李四'])[0], 200)
        self.assertEqual(self.login()[1], 404)
        self.assertEqual(self.numeric(client)[0], 403)
        self.assertEqual(self.save(['张三', '李四'])[0], 200)
        self.assertEqual(self.login()[1], 200)
        state = self.stage()
        target = next(row['id'] for row in self.view()['judges'] if row['name'] == '张三')
        with self.service.db() as db:
            db.execute('DELETE FROM judging_grants WHERE run_id=? AND judge_id=?', (state['stage']['runId'], target))
        self.assertEqual(self.login()[1], 404)
        self.assertEqual(self.numeric(client)[0], 403)

    def test_admin_rotation_revocation_rename_and_claim_compatibility(self):
        configured = self.configure(['张三', '李四', '王五', '赵六'])
        targets = {row['name']: row['id'] for row in configured['judges']}
        claimed = self.public.post('/api/roadshow/claim', {'name': '张三'})[1]
        client, status, _, _ = self.login()
        self.assertEqual(status, 200)
        for name in ['张三', '李四']:
            self.assertEqual(self.admin.post('/api/admin/credentials', {
                'role': 'judge', 'id': targets[name], 'action': 'rotate'})[0], 200)
            self.assertEqual(self.login(name)[1], 409)
        self.assertIsNone(client.get('/api/session')[1]['role'])
        self.assertEqual(self.public.post('/api/login', {'role': 'judge', 'code': claimed['code']})[0], 401)
        self.assertEqual(self.admin.post('/api/admin/revoke', {'role': 'judge', 'id': targets['王五']})[0], 200)
        self.assertEqual(self.login('王五')[1], 403)
        with self.service.db() as db:
            db.execute('UPDATE accounts SET name=? WHERE id=?', ('Renamed by organizer', targets['赵六']))
        self.assertEqual(self.login('赵六')[1], 409)
        self.assertEqual(self.login('Renamed by organizer')[1], 404)
        self.assertFalse(self.public.get('/api/roadshow/access')[1]['nameLoginAvailable'])
        self.assertTrue(all(not row['canLogin'] for row in self.view()['judges']))

    def test_concurrent_name_logins_reuse_one_account_and_preserve_credentials(self):
        self.configure(['张三'])
        clients = [fixtures.Client(self.server) for _ in range(4)]
        with ThreadPoolExecutor(max_workers=4) as pool:
            responses = list(pool.map(lambda client: self.login(client=client), clients))
        self.assertEqual([row[1] for row in responses], [200] * 4)
        self.assertEqual(len({row[2]['loginId'] for row in responses}), 4)
        self.assertTrue(all(self.numeric(client)[0] == 200 for client in clients))
        with self.service.db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM accounts WHERE role='judge'").fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM roadshow_access_roster WHERE claimed_at IS NOT NULL').fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM sessions s JOIN accounts a ON a.id=s.account_id WHERE a.role='judge'").fetchone()[0], 4)

    def test_name_login_can_score_and_admin_sees_saved_score_immediately(self):
        self.configure(['张三', '李四', '王五'])
        team_id = self.add_team()
        state = self.stage()
        response = self.admin.post('/api/admin/judging/roadshow/window', {
            'expectedRevision': state['stage']['revision'], 'open': True, 'start90Minutes': True})
        self.assertEqual(response[0], 200, response)
        client, status, _, _ = self.login()
        self.assertEqual(status, 200)
        response = self.numeric(client, 'POST', {'runId': state['stage']['runId'], 'teamId': team_id,
            'expectedRevision': 0, 'status': 'submitted', 'scores': [20, 15, 20, 20, 15, 10], 'feedback': ''})
        self.assertEqual(response[0], 200, response)
        status, dashboard, _ = self.admin.request('GET', '/api/admin/roadshow/scoring',
            headers={'X-GOTC-Login': self.admin.login_id})
        self.assertEqual(status, 200, dashboard)
        score = next(row for row in dashboard['summary']['teams'] if row['id'] == team_id)
        self.assertEqual((score['score'], score['scoreCount']), (100, 1))
        self.assertFalse(score['valid'])

    def test_queued_login_rechecks_all_identity_guards_after_write_lock(self):
        names = ['Judge revoke', 'Judge ungrant', 'Judge remove', 'Judge rename', 'Judge rotate', 'Still available']
        configured = self.configure(names)
        targets = {row['name']: row['id'] for row in configured['judges']}
        original_db = self.service.db
        mutations = [
            ('Judge revoke', "UPDATE accounts SET active=0 WHERE id=?", 403),
            ('Judge ungrant', "DELETE FROM judging_grants WHERE judge_id=?", 404),
            ('Judge remove', "UPDATE roadshow_access_roster SET enabled=0 WHERE judge_id=?", 404),
            ('Judge rename', "UPDATE accounts SET name='Changed name' WHERE id=?", 409),
            ('Judge rotate', "UPDATE accounts SET code_hash=X'01020304' WHERE id=?", 409)]
        for name, sql, expected in mutations:
            with self.subTest(name=name):
                queued = threading.Event()
                @contextmanager
                def traced_db():
                    with original_db() as db:
                        db.set_trace_callback(lambda query: queued.set() if query == 'BEGIN IMMEDIATE' else None)
                        yield db
                result = []
                with closing(sqlite3.connect(self.service.db_path)) as lock:
                    lock.execute('BEGIN IMMEDIATE')
                    with patch.object(self.service, 'db', traced_db):
                        worker = threading.Thread(target=lambda: result.append(self.login(name)))
                        worker.start()
                        self.assertTrue(queued.wait(3), 'Name login did not wait for the write lock')
                        lock.execute(sql, (targets[name],))
                        lock.commit()
                        worker.join(5)
                        self.assertFalse(worker.is_alive())
                self.assertEqual(result[0][1], expected, result)
                self.assertNotIn('Set-Cookie', result[0][3])
                with original_db() as db:
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM sessions WHERE account_id=?', (targets[name],)).fetchone()[0], 0)

    def test_name_login_rate_limit_has_no_session_side_effect(self):
        self.configure(['张三'])
        for _ in range(20):
            self.assertEqual(self.login('Unknown judge')[1], 404)
        client, status, _, headers = self.login()
        self.assertEqual(status, 429)
        self.assertNotIn('Set-Cookie', headers)
        self.assertIsNone(client.get('/api/session')[1]['role'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
