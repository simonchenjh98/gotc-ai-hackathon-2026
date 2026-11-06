"""Administrator team workbench uses synthetic identities and isolated local databases."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app import EventService
from errors import APIError
from repositories import Provider
import test_api
import test_submissions
from test_repositories import Remote


SHARED_ADMIN_CODE = 'synthetic-admin-614829'


class AdminTeamAccessTests(unittest.TestCase):
    tearDown = test_api.APITests.tearDown
    team = test_api.APITests.team
    invitation = test_api.APITests.invitation
    access = test_submissions.SubmissionAPITests.access
    open_submissions = test_submissions.SubmissionAPITests.open_submissions
    materials = test_submissions.SubmissionAPITests.materials

    def setUp(self):
        test_api.APITests.setUp(self)
        (self.service.data_dir / 'shared-login-codes.json').write_text(json.dumps({'admin': SHARED_ADMIN_CODE}), encoding='utf-8')
        self.restart()
        self.admin = self.admin_login()
        with self.service.db() as db:
            self.admin_id = self.service.account(db, self.admin.cookie)['id']

    def restart(self):
        old = self.service
        self.service = EventService(data_dir=old.data_dir, origin=old.origin, static_root=old.root, base_path=old.base_path)
        self.server.service = self.service

    def admin_login(self, role='team', code=SHARED_ADMIN_CODE, account_id=None):
        client = test_api.Client(self.server)
        status, result, _ = client.post('/api/login', {'role': role, 'code': code, **({'accountId': account_id} if account_id else {})})
        self.assertEqual(status, 200, result)
        self.assertEqual(result['role'], 'admin')
        self.assertIsNone(result['operator'])
        return client

    def clone(self, client):
        clone = test_api.Client(self.server)
        clone.cookie, clone.login_id = client.cookie, client.login_id
        return clone

    def call(self, client, path, body=None, marker=None):
        result = client.request('GET' if body is None else 'POST', path, body,
                                headers={'X-GOTC-Login': client.login_id if marker is None else marker})
        if result[0] == 200 and 'Set-Cookie' in result[2]:
            client.login_id = result[1]['loginId']
        return result

    def enter(self, team_id, client=None):
        client = client or self.admin_login()
        result = self.call(client, '/api/admin/team-workbench', {'teamId': team_id})
        self.assertEqual(result[0], 200, result[1])
        self.assertEqual(result[1]['role'], 'team')
        self.assertEqual(result[1]['operator']['teamId'], team_id)
        return client

    def credential_snapshot(self):
        with self.service.db() as db:
            return {table: [tuple(row) for row in db.execute('SELECT * FROM '+table)] for table in ('accounts', 'team_accounts')}

    def test_shared_admin_code_at_team_login_keeps_real_admin_identity_and_selection(self):
        before = self.credential_snapshot()
        client = self.admin_login()
        self.assertEqual(client.get('/api/session')[1]['role'], 'admin')
        self.assertEqual(self.credential_snapshot(), before)
        with self.service.db() as db:
            second, _ = self.service.create_account(db, 'admin', 'Second fixture administrator')
        status, choices, _ = self.public.post('/api/login', {'role': 'team', 'code': SHARED_ADMIN_CODE})
        self.assertEqual(status, 200)
        self.assertTrue(choices['selectAccount'])
        self.assertEqual({row['id'] for row in choices['accounts']}, {self.admin_id, second['id']})
        chosen = self.admin_login(account_id=second['id'])
        self.assertEqual(chosen.get('/api/session')[1]['name'], 'Second fixture administrator')
        self.assertEqual(self.public.post('/api/login', {'role': 'team', 'code': 'wrong-code', 'accountId': second['id']})[0], 401)

    def test_selection_requires_admin_marker_origin_and_explicit_active_team(self):
        one = self.team()
        withdrawn = self.team(2, 'Withdrawn fixture', status='withdrawn', statusReason='Synthetic withdrawal')
        student, _ = self.access(one)
        judge, _ = self.invitation('judge', 'Fixture judge')
        for client, status in [(self.public, 401), (student, 403), (judge, 403)]:
            self.assertEqual(self.call(client, '/api/admin/team-workbench')[0], status)
            self.assertEqual(self.call(client, '/api/admin/team-workbench', {'teamId': one})[0], status)
            self.assertEqual(self.call(client, '/api/team/workbench/return', {})[0], 401 if client is self.public else 403)
        self.assertEqual(self.admin.get('/api/admin/team-workbench')[0], 428)
        self.assertEqual(self.call(self.admin, '/api/admin/team-workbench', marker='stale')[0], 401)
        listing = self.call(self.admin, '/api/admin/team-workbench')[1]
        self.assertEqual([team['id'] for team in listing['teams']], [one])
        for body in [{}, {'teamId': one, 'operator': {'adminId': self.admin_id}}, {'teamId': []}]:
            self.assertEqual(self.call(self.admin, '/api/admin/team-workbench', body)[0], 400)
        self.assertEqual(self.call(self.admin, '/api/admin/team-workbench', {'teamId': 'missing'})[0], 404)
        self.assertEqual(self.call(self.admin, '/api/admin/team-workbench', {'teamId': withdrawn})[0], 409)
        self.assertEqual(self.admin.request('POST', '/api/admin/team-workbench', {'teamId': one}, origin=False,
                         headers={'X-GOTC-Login': self.admin.login_id})[0], 403)

    def test_unissued_team_entry_rotation_return_and_student_credentials_stay_unchanged(self):
        one, two = self.team(), self.team(2, 'Second fixture team')
        student, access = self.access(two)
        before = self.credential_snapshot()
        operator = self.admin_login()
        old = self.clone(operator)
        self.enter(one, operator)
        self.assertNotEqual(operator.cookie, old.cookie)
        session = operator.get('/api/session')[1]
        self.assertEqual(session['operator']['adminId'], self.admin_id)
        self.assertEqual(session['operator']['teamId'], one)
        self.assertEqual(session['name'], session['operator']['teamName'])
        self.assertEqual(self.call(operator, '/api/team')[1]['team']['id'], one)
        self.assertIsNone(old.get('/api/session')[1]['role'])
        self.assertEqual(self.call(operator, '/api/team', marker=old.login_id)[0], 401)
        self.assertEqual(operator.get('/api/team')[0], 428)
        self.assertEqual(self.call(operator, '/api/admin/team-workbench', {'teamId': two})[0], 403)
        self.assertEqual(self.call(operator, '/api/team/workbench/return', {'teamId': two})[0], 400)
        self.assertEqual(self.call(operator, '/api/team/workbench/return', {}, marker='stale')[0], 401)
        former = self.clone(operator)
        result = self.call(operator, '/api/team/workbench/return', {})
        self.assertEqual(result[0], 200, result[1])
        self.assertEqual(result[1]['role'], 'admin')
        self.assertIsNone(result[1]['operator'])
        self.assertIsNone(former.get('/api/session')[1]['role'])
        self.enter(two, operator)
        self.assertEqual(self.call(operator, '/api/team')[1]['team']['id'], two)
        self.assertEqual(self.credential_snapshot(), before)
        self.assertEqual(student.get('/api/team')[1]['team']['id'], two)
        fresh = test_api.Client(self.server)
        self.assertEqual(fresh.post('/api/login', {'role': 'team', 'code': access['code']})[0], 200)
        with self.service.db() as db:
            rows = db.execute('''SELECT s.account_id,c.team_id FROM admin_team_sessions c
                JOIN sessions s ON s.token_hash=c.token_hash''').fetchall()
            self.assertEqual([tuple(row) for row in rows], [(self.admin_id, two)])

    def test_delegated_submission_history_keys_repository_and_audit_are_team_scoped(self):
        one, two = self.team(), self.team(2, 'Second fixture team')
        self.open_submissions()
        self.service.repositories.providers['github'] = Provider('github', 'synthetic-org', 'fixture-token', 'main', True)
        operator = self.enter(one)
        body = {'requestId': 'operator-formal-0001', 'expectedVersion': 0, 'status': 'submitted', 'materials': self.materials()}
        self.assertEqual(self.call(operator, '/api/submission', body)[0], 200)
        self.assertEqual(self.call(operator, '/api/submission/history')[1]['items'][0]['version'], 1)
        self.assertEqual(self.call(operator, '/api/submission/requests/operator-formal-0001')[1]['status'], 'succeeded')
        self.assertEqual(self.call(operator, '/api/submission', {**body, 'teamId': two})[0], 400)
        self.assertEqual(self.call(operator, f'/api/submission/backups/{two}/1/source.zip')[0], 403)
        self.assertEqual(self.call(operator, f'/api/submission/backups/{one}/1/source.zip')[0], 404)
        self.assertEqual(self.call(operator, '/api/team/repository/apply', {'provider': 'github', 'usernames': ['FixtureDev']})[0], 200)
        self.assertEqual(self.call(operator, '/api/team/repository')[1]['repository']['teamId'], one)
        self.assertFalse(self.call(operator, '/api/team/keys')[1]['enabled'])
        self.assertEqual(self.call(operator, '/api/team/keys', {'name': 'disabled'})[0], 409)
        self.assertEqual(self.call(operator, '/api/admin/keys')[0], 403)
        self.assertEqual(self.call(operator, '/api/team/workbench/return', {})[0], 200)
        self.enter(two, operator)
        self.assertEqual(self.call(operator, '/api/submission/history')[1]['items'], [])
        self.assertEqual(self.call(operator, '/api/submission/requests/operator-formal-0001')[1]['status'], 'not_found')
        self.assertIsNone(self.call(operator, '/api/team/repository')[1]['repository'])
        with self.service.db() as db:
            rows = db.execute("SELECT actor_id,role,metadata FROM audit WHERE action IN ('submission.submitted','repository.requested')").fetchall()
            self.assertEqual(len(rows), 2)
            for row in rows:
                self.assertEqual((row['actor_id'], row['role']), (self.admin_id, 'admin'))
                self.assertEqual(json.loads(row['metadata'])['operator']['teamId'], one)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM team_accounts').fetchone()[0], 0)

    def test_closed_submission_window_and_key_entitlements_are_not_bypassed(self):
        one, two = self.team(), self.team(2, 'Another API team')
        operator = self.enter(one)
        body = {'expectedVersion': 0, 'status': 'draft', 'materials': self.materials()}
        self.assertEqual(self.call(operator, '/api/submission', body)[0], 409)
        self.assertEqual(self.call(operator, '/api/team/keys', {'name': 'not-enabled'})[0], 409)
        upstream = {'openai': {'url': 'http://127.0.0.1:1/v1', 'key': 'synthetic-no-network-key', 'models': {'fixture-model': [1, 2]}}}
        self.assertEqual(self.admin.post('/api/admin/keys/upstreams', {'upstreams': upstream})[0], 200)
        with self.service.db() as db:
            db.executemany('INSERT INTO team_api VALUES (?,1,1)', [(one,), (two,)])
        result = self.call(operator, '/api/team/keys', {'name': 'first-team-only'})
        self.assertEqual(result[0], 200, result[1])
        key_id = result[1]['item']['id']
        with self.service.db() as db:
            handler = SimpleNamespace(headers={'Authorization': 'Bearer '+result[1]['key']})
            self.assertIsNone(self.service.keys.lookup(db, handler), 'Unissued teams cannot bypass the normal gateway login entitlement')
        self.assertEqual(self.call(operator, '/api/team/workbench/return', {})[0], 200)
        self.enter(two, operator)
        self.assertEqual(self.call(operator, '/api/team/keys')[1]['keys'], [])
        self.assertEqual(self.call(operator, '/api/team/keys/delete', {'id': key_id})[0], 404)
        with self.service.db() as db:
            audit = db.execute("SELECT actor_id,role,metadata FROM audit WHERE action='api-key.create'").fetchone()
            self.assertEqual((audit['actor_id'], audit['role']), (self.admin_id, 'admin'))
            self.assertEqual(json.loads(audit['metadata'])['operator']['teamId'], one)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM team_accounts').fetchone()[0], 0)

    def test_admin_revocation_expiry_and_code_rotation_invalidate_delegated_sessions(self):
        one = self.team()
        operator = self.enter(one)
        with self.service.db() as db:
            db.execute('UPDATE accounts SET active=0 WHERE id=?', (self.admin_id,))
        self.assertIsNone(operator.get('/api/session')[1]['role'])
        self.assertEqual(self.call(operator, '/api/team/workbench/return', {})[0], 401)
        with self.service.db() as db:
            db.execute('UPDATE accounts SET active=1 WHERE id=?', (self.admin_id,))
            db.execute('UPDATE sessions SET expires_at=0')
        self.assertIsNone(operator.get('/api/session')[1]['role'])
        operator = self.enter(one)
        (self.service.data_dir / 'shared-login-codes.json').write_text(json.dumps({'admin': 'rotated-fixture-admin'}), encoding='utf-8')
        self.restart()
        self.assertIsNone(operator.get('/api/session')[1]['role'])
        with self.service.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM admin_team_sessions').fetchone()[0], 0)
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_return_and_logout_delete_context_without_touching_student_login(self):
        one = self.team()
        student, _ = self.access(one)
        operator = self.enter(one)
        self.assertEqual(self.call(operator, '/api/logout', {})[0], 200)
        self.assertIsNone(operator.get('/api/session')[1]['role'])
        self.assertEqual(student.get('/api/team')[0], 200)
        with self.service.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM admin_team_sessions').fetchone()[0], 0)

    def test_entry_race_and_failed_rotation_are_atomic(self):
        one, two = self.team(), self.team(2, 'Second team')
        operator = self.admin_login()
        replicas = [self.clone(operator), self.clone(operator)]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda pair: self.call(pair[0], '/api/admin/team-workbench', {'teamId': pair[1]}), zip(replicas, [one, two])))
        self.assertEqual(sorted(result[0] for result in results), [200, 401])
        with self.service.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM admin_team_sessions').fetchone()[0], 1)
        fresh = self.admin_login()
        old_cookie = fresh.cookie
        with patch.object(self.service, 'audit', side_effect=APIError(409, 'synthetic audit failure')):
            self.assertEqual(self.call(fresh, '/api/admin/team-workbench', {'teamId': one})[0], 409)
        self.assertEqual(fresh.cookie, old_cookie)
        self.assertEqual(fresh.get('/api/session')[1]['role'], 'admin')

    def test_queued_entry_rechecks_administrator_after_revocation(self):
        one = self.team()
        operator = self.admin_login()
        original_db = self.service.db
        queued = threading.Event()
        @contextmanager
        def traced_db():
            with original_db() as db:
                db.set_trace_callback(lambda statement: queued.set() if statement == 'BEGIN IMMEDIATE' else None)
                yield db
        results = []
        with original_db() as lock:
            lock.execute('BEGIN IMMEDIATE')
            with patch.object(self.service, 'db', traced_db):
                thread = threading.Thread(target=lambda: results.append(self.call(operator, '/api/admin/team-workbench', {'teamId': one})))
                thread.start()
                self.assertTrue(queued.wait(3), 'Entry did not queue for the writer lock')
                lock.execute('UPDATE accounts SET active=0 WHERE id=?', (self.admin_id,))
                lock.execute('DELETE FROM sessions WHERE account_id=?', (self.admin_id,))
                lock.commit()
                thread.join(5)
                self.assertFalse(thread.is_alive())
        self.assertEqual(results[0][0], 401, results)
        with original_db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM admin_team_sessions').fetchone()[0], 0)

    def test_repository_preview_and_confirmation_follow_rotated_operator_session(self):
        one = self.team()
        self.open_submissions()
        provider = Provider('github', 'synthetic-org', 'fixture-token', 'main', True)
        remote = Remote(self.service)
        remote.accepted = True
        provider.request = remote
        self.service.repositories.providers['github'] = provider
        operator = self.enter(one)
        application = self.call(operator, '/api/team/repository/apply', {'provider': 'github', 'usernames': ['FixtureDev']})
        self.assertEqual(application[0], 200, application[1])
        row = application[1]['repository']
        approved = self.call(self.admin, '/api/admin/repositories/approve', {'teamId': one, 'expectedRevision': row['revision'], 'usernamesVerified': True})
        self.assertEqual(approved[0], 200, approved[1])
        preview = self.call(operator, '/api/team/repository/preview', {})
        self.assertEqual(preview[0], 200, preview[1])
        self.assertEqual(self.call(operator, '/api/team/workbench/return', {})[0], 200)
        self.enter(one, operator)
        body = {'requestId': 'operator-repository-0001', 'expectedVersion': 0, 'status': 'submitted',
                'materials': self.materials(codeUrl='', codeVersion=''), 'repoPreviewId': preview[1]['previewId']}
        self.assertEqual(self.call(operator, '/api/submission', body)[0], 409)
        current = self.call(operator, '/api/team/repository/preview', {})
        self.assertEqual(current[0], 200, current[1])
        result = self.call(operator, '/api/submission', {**body, 'requestId': 'operator-repository-0002', 'repoPreviewId': current[1]['previewId']})
        self.assertEqual(result[0], 200, result[1])
        self.assertEqual(result[1]['submission']['submitted']['codeVersion'], remote.sha)


if __name__ == '__main__':
    unittest.main()
