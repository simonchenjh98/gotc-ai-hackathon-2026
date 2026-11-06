"""Roadshow name claiming regressions against disposable HTTP servers only."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
import unittest

from app import EventService
import test_api as fixtures


class RoadshowAccessTests(unittest.TestCase):
    setUp = fixtures.APITests.setUp
    tearDown = fixtures.APITests.tearDown
    team = fixtures.APITests.team
    invitation = fixtures.APITests.invitation

    def admin_request(self, method='GET', body=None, client=None, marker=None):
        client = client or self.admin
        marker = client.login_id if marker is None else marker
        return client.request(method, '/api/admin/roadshow-access', body, headers={'X-GOTC-Login': marker})

    def view(self):
        status, body, _ = self.admin_request()
        self.assertEqual(status, 200, body)
        return body

    def save(self, names, previous=None, client=None):
        before = previous or self.view()
        return self.admin_request('POST', {'expectedRevision': before['revision'], 'runId': before['runId'], 'names': names}, client)

    def configure(self, names=None):
        status, body, _ = self.save(names or ['张三', '李四'])
        self.assertEqual(status, 200, body)
        return body

    def claim(self, name='张三', client=None):
        return (client or self.public).post('/api/roadshow/claim', {'name': name})

    def judge(self, name='张三'):
        status, claimed, _ = self.claim(name)
        self.assertEqual(status, 200, claimed)
        judge = fixtures.Client(self.server)
        self.assertEqual(judge.post('/api/login', {'role': 'judge', 'code': claimed['code']})[0], 200)
        return judge, claimed

    def stage(self):
        return self.admin.get('/api/admin/judging/roadshow')[1]

    def set_window(self, opened):
        before = self.stage()
        return self.admin.post('/api/admin/judging/roadshow/window', {
            'expectedRevision': before['stage']['revision'], 'open': opened, 'deadline': None})

    def add_team(self):
        team_id = self.team()
        before = self.stage()
        status, body, _ = self.admin.post('/api/admin/judging/roadshow/roster', {
            'expectedRevision': before['stage']['revision'], 'teamIds': [team_id],
            'judgeIds': before['roster']['judgeIds']})
        self.assertEqual(status, 200, body)
        return team_id

    def test_empty_roster_is_explicit_and_never_creates_arbitrary_accounts(self):
        status, public, headers = self.public.get('/api/roadshow/access')
        self.assertEqual(status, 200)
        self.assertEqual(set(public), {'configured', 'claimAvailable', 'message'})
        self.assertFalse(public['configured'])
        self.assertFalse(public['claimAvailable'])
        self.assertNotIn('Set-Cookie', headers)
        self.assertEqual(self.claim('unknown')[0], 409)
        with self.service.db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM accounts WHERE role='judge'").fetchone()[0], 0)
        key = self.service.data_dir / 'roadshow-claim-key'
        self.assertEqual(len(key.read_bytes()), 32)
        if os.name != 'nt':
            self.assertEqual(key.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.public.get('/roadshow-claim-key')[0], 404)

    def test_admin_requires_role_page_identity_and_exact_paths(self):
        self.assertEqual(self.public.get('/api/admin/roadshow-access')[0], 401)
        self.assertEqual(self.admin.get('/api/admin/roadshow-access')[0], 428)
        self.assertEqual(self.admin_request(marker='wrong')[0], 401)
        other, _ = self.invitation()
        self.assertEqual(self.admin_request(client=other)[0], 403)
        for route in ['/api/roadshow/access/', '/api/roadshow/claim/', '/api/admin/roadshow-access/']:
            self.assertEqual(self.public.get(route)[0], 404)
        self.assertEqual(self.public.get('/api/roadshow/access?names=1')[0], 400)
        self.assertEqual(self.public.request('POST', '/api/roadshow/claim', {'name': '张三'}, origin=False)[0], 403)

    def test_name_normalization_repeat_claim_and_no_public_roster_or_secret_storage(self):
        configured = self.configure([' 张三 ', 'Ａlice  Wang'])
        self.assertEqual(configured['names'], ['张三', 'Alice Wang'])
        self.assertTrue(configured['configured'])
        status, first, headers = self.claim('  张三  ')
        self.assertEqual(status, 200, first)
        self.assertNotIn('Set-Cookie', headers)
        self.assertEqual(self.claim('张三')[1], first)
        status, alice, _ = self.claim('alice   WANG')
        self.assertEqual(status, 200, alice)
        self.assertNotEqual(first['code'], alice['code'])
        public = json.dumps(self.public.get('/api/roadshow/access')[1], ensure_ascii=False)
        self.assertNotIn('张三', public)
        self.assertNotIn('Alice', public)
        self.assertNotIn(first['code'], public)
        self.assertEqual(self.public.get('/api/session')[1]['role'], None)
        self.assertTrue(all(j['claimed'] for j in self.view()['judges']))
        with self.service.db() as db:
            dump = '\n'.join(db.iterdump())
            self.assertNotIn(first['code'], dump)
            self.assertNotIn(alice['code'], dump)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM credential_overrides').fetchone()[0], 2)

    def test_claimed_code_is_roadshow_only_and_shared_code_cannot_select_it(self):
        self.service.shared_login_codes['judge'] = 'synthetic-shared-code'
        configured = self.configure()
        target = configured['judges'][0]['id']
        self.assertEqual(self.public.post('/api/login', {'role': 'judge', 'code': 'synthetic-shared-code', 'accountId': target})[0], 401)
        judge, claimed = self.judge()
        self.assertEqual(judge.get('/api/judging/roadshow')[0], 200)
        self.assertEqual(judge.get('/api/judge')[0], 403)
        self.assertEqual(judge.post('/api/score', {})[0], 403)
        self.assertEqual(judge.get('/api/judging/exhibition')[0], 403)
        self.assertEqual(judge.post('/api/judging/exhibition/review', {})[0], 403)
        self.assertEqual(judge.get('/api/admin')[0], 403)
        self.assertEqual(judge.get('/api/finals/judge')[0], 400)
        self.assertEqual(judge.get('/api/board')[0], 200)
        ordinary, _ = self.invitation(name='ordinary-existing-judge')
        self.assertEqual(ordinary.get('/api/judge')[0], 200)
        self.assertEqual(self.public.post('/api/login', {'role': 'judge', 'code': 'synthetic-shared-code', 'accountId': target})[0], 401)
        self.assertEqual(self.claim()[1]['code'], claimed['code'])

    def test_concurrent_first_claims_are_one_stable_credential_without_session_rotation(self):
        self.configure(['张三'])
        clients = [fixtures.Client(self.server) for _ in range(4)]
        with ThreadPoolExecutor(max_workers=4) as pool:
            responses = list(pool.map(lambda client: self.claim(client=client), clients))
        self.assertEqual([r[0] for r in responses], [200] * 4)
        self.assertEqual(len({r[1]['code'] for r in responses}), 1)
        judge, claimed = self.judge()
        self.assertEqual(self.claim()[1]['code'], claimed['code'])
        self.assertEqual(judge.get('/api/session')[1]['role'], 'judge')
        with self.service.db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM accounts WHERE role='judge'").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM audit WHERE action='roadshow.access.claimed'").fetchone()[0], 1)

    def test_invalid_names_are_atomic_and_roster_editors_conflict(self):
        baseline = self.view()
        for invalid in [True, '张三', ['张三', ' 张三 '], ['Ａlice', 'alice'], [''], [None], ['\u200b'], ['n'] * 101]:
            with self.subTest(invalid=invalid):
                self.assertEqual(self.save(invalid)[0], 400)
                self.assertEqual(self.view()['revision'], baseline['revision'])
        first = self.configure(['张三'])
        self.assertEqual(self.save(['李四'], baseline)[0], 409)
        self.assertEqual(self.view()['names'], ['张三'])
        self.assertEqual(self.claim('李四')[0], 404)
        team_id = self.add_team()
        self.assertTrue(team_id)
        self.assertEqual(self.save(['张三'], first)[0], 409)

    def test_admin_rotation_or_revocation_is_never_undone_by_claim(self):
        configured = self.configure(['张三', '李四'])
        judge, claimed = self.judge()
        target = claimed['code'].split('.', 1)[0]
        status, rotated, _ = self.admin.post('/api/admin/credentials', {'role': 'judge', 'id': target, 'action': 'rotate'})
        self.assertEqual(status, 200, rotated)
        self.assertEqual(self.claim()[0], 409)
        self.assertEqual(judge.get('/api/session')[1]['role'], None)
        self.assertEqual(self.public.post('/api/login', {'role': 'judge', 'code': claimed['code']})[0], 401)
        self.assertEqual(self.public.post('/api/login', {'role': 'judge', 'code': rotated['code']})[0], 200)
        self.assertEqual(self.admin.post('/api/admin/credentials', {'role': 'judge', 'id': target, 'action': 'revoke'})[0], 200)
        self.assertEqual(self.claim()[0], 403)
        second = next(j['id'] for j in configured['judges'] if j['name'] == '李四')
        self.assertEqual(self.admin.post('/api/admin/credentials', {'role': 'judge', 'id': second, 'action': 'rotate'})[0], 200)
        self.assertEqual(self.claim('李四')[0], 409)

    def test_removed_name_loses_current_grant_without_deleting_history_or_reissuing_on_restore(self):
        self.configure()
        judge, claimed = self.judge()
        self.assertEqual(self.save(['李四'])[0], 200)
        self.assertEqual(self.claim()[0], 404)
        self.assertEqual(judge.get('/api/judging/roadshow')[0], 403)
        self.assertEqual(self.save(['张三', '李四'])[0], 200)
        self.assertEqual(self.claim()[1]['code'], claimed['code'])
        self.assertEqual(judge.get('/api/judging/roadshow')[0], 200)
        with self.service.db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM accounts WHERE role='judge'").fetchone()[0], 2)

    def test_open_frozen_reviewed_rosters_lock_and_reset_rejects_old_editor(self):
        self.configure(['张三', '李四', '王五'])
        judge, _ = self.judge()
        team_id = self.add_team()
        before = self.view()
        self.assertEqual(self.set_window(True)[0], 200)
        self.assertFalse(self.view()['editable'])
        self.assertEqual(self.save(['张三', '李四'])[0], 409)
        run_id = self.stage()['stage']['runId']
        self.assertEqual(judge.post('/api/judging/roadshow/review', {
            'runId': run_id, 'teamId': team_id, 'expectedVersion': 0, 'status': 'submitted', 'feedback': 'Synthetic review', 'scores': None})[0], 200)
        self.assertEqual(self.set_window(False)[0], 200)
        self.assertEqual(self.save(['李四'])[0], 409)
        stage = self.stage()
        status, body, _ = self.admin.post('/api/admin/judging/roadshow/reset', {'expectedRevision': stage['stage']['revision'], 'reason': 'Synthetic next round'})
        self.assertEqual(status, 200, body)
        # The existing reset workflow explicitly carries roster/grants to the new run.
        self.assertTrue(self.public.get('/api/roadshow/access')[1]['configured'])
        self.assertEqual(self.claim()[0], 200)
        self.assertEqual(self.save(['张三'], before)[0], 409)
        current = self.view()
        self.assertEqual(self.admin_request('POST', {'expectedRevision': current['revision'], 'runId': run_id, 'names': ['张三']})[0], 409)
        self.assertEqual(self.save(['张三'])[0], 200)
        self.assertEqual(self.claim()[0], 200)
        with self.service.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM judging_reviews WHERE run_id=?', (run_id,)).fetchone()[0], 1)

    def test_restart_keeps_claimed_code_and_missing_or_changed_key_fails_closed(self):
        self.configure(['张三'])
        _, claimed = self.judge()
        key_path = self.service.data_dir / 'roadshow-claim-key'
        original = key_path.read_bytes()
        restarted = EventService(data_dir=self.service.data_dir, origin=self.service.origin, static_root=self.service.root)
        self.service = restarted
        self.server.service = restarted
        self.assertEqual(key_path.read_bytes(), original)
        self.assertEqual(self.claim()[1], claimed)
        key_path.write_bytes(b'x' * 32)
        with self.assertRaisesRegex(RuntimeError, 'does not match'):
            EventService(data_dir=self.service.data_dir, origin=self.service.origin, static_root=self.service.root)
        key_path.write_bytes(original)


if __name__ == '__main__':
    unittest.main()
