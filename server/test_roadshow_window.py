"""HTTP regressions for the server-owned 90-minute roadshow window."""
from concurrent.futures import ThreadPoolExecutor
import unittest

from app import EventService
from judging import ROADSHOW_DURATION_MS
import test_api as fixtures


class RoadshowWindowTests(unittest.TestCase):
    tearDown = fixtures.APITests.tearDown
    team = fixtures.APITests.team
    invitation = fixtures.APITests.invitation
    path = '/api/admin/judging/roadshow'

    def setUp(self):
        fixtures.APITests.setUp(self)
        self.current = 1800000000000
        self.service.judging.clock = lambda: self.current

    def stage(self, stage='roadshow'):
        status, data, _ = self.admin.get('/api/admin/judging/' + stage)
        self.assertEqual(status, 200, data)
        return data

    def configure(self, stage='roadshow', judge_count=3):
        self.team_id = self.team()
        self.judge, invitation = self.invitation()
        self.judge_ids = [invitation['id']] + [self.invitation(name='Extra judge ' + str(index))[1]['id']
                                             for index in range(1, judge_count)]
        status, data, _ = self.admin.post('/api/admin/judging/' + stage + '/roster', {
            'expectedRevision': self.stage(stage)['stage']['revision'],
            'teamIds': [self.team_id], 'judgeIds': self.judge_ids})
        self.assertEqual(status, 200, data)
        self.run_id = data['stage']['runId']
        return data

    def change(self, **changes):
        return self.admin.post(self.path + '/window', {
            'expectedRevision': self.stage()['stage']['revision'], **changes})

    def start(self):
        status, data, _ = self.change(open=True, start90Minutes=True)
        self.assertEqual(status, 200, data)
        return data

    def close(self):
        status, data, _ = self.change(open=False, deadline=None)
        self.assertEqual(status, 200, data)
        return data

    def score(self, revision=0):
        return self.judge.request('POST', '/api/roadshow/score', {
            'runId': self.run_id, 'teamId': self.team_id, 'expectedRevision': revision,
            'status': 'submitted', 'scores': [20, 15, 20, 20, 15, 10], 'feedback': ''},
            headers={'X-GOTC-Login': self.judge.login_id})

    def review(self, version=0):
        return self.judge.post('/api/judging/roadshow/review', {
            'runId': self.run_id, 'teamId': self.team_id, 'expectedVersion': version,
            'status': 'submitted', 'feedback': 'Synthetic qualitative review', 'scores': None})

    def test_start_is_exactly_server_90_minutes_and_survives_restart(self):
        self.configure()
        opened = self.start()
        deadline = self.current + ROADSHOW_DURATION_MS
        self.assertEqual(opened['window'], {
            'open': True, 'deadline': deadline, 'serverTime': self.current, 'canEdit': True})
        previous = self.service
        self.service = EventService(data_dir=previous.data_dir, base_path=previous.base_path,
                                    origin=previous.origin, static_root=previous.root)
        self.server.service = self.service
        self.service.judging.clock = lambda: self.current
        self.current += 120000
        self.assertEqual(self.stage()['window']['deadline'], deadline)
        self.assertFalse(self.close()['window']['canEdit'])
        reopened = self.start()
        self.assertEqual(reopened['window']['deadline'], self.current + ROADSHOW_DURATION_MS)
        self.assertEqual(reopened['stage']['frozenAt'], opened['stage']['frozenAt'])

    def test_concurrent_or_repeated_start_never_extends_deadline(self):
        before = self.configure()
        request = {'expectedRevision': before['stage']['revision'], 'open': True, 'start90Minutes': True}
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: self.admin.post(self.path + '/window', request)[0], range(2)))
        self.assertEqual(sorted(responses), [200, 409])
        deadline = self.stage()['window']['deadline']
        self.current += 60000
        self.assertEqual(self.change(open=True, start90Minutes=True)[0], 409)
        self.assertEqual(self.admin.post(self.path + '/window', request)[0], 409)
        self.assertEqual(self.stage()['window']['deadline'], deadline)

    def test_legacy_form_defaults_90_minutes_and_only_preserves_or_shortens(self):
        self.configure()
        status, opened, _ = self.change(open=True, deadline=None)
        self.assertEqual(status, 200, opened)
        deadline = opened['window']['deadline']
        self.assertEqual(deadline, self.current + ROADSHOW_DURATION_MS)
        self.current += 1000
        self.assertEqual(self.change(open=True, deadline=None)[0], 200)
        self.assertEqual(self.stage()['window']['deadline'], deadline)
        self.assertEqual(self.change(open=True, deadline=deadline + 1)[0], 409)
        self.assertEqual(self.change(open=True, deadline=deadline - 1000)[0], 200)
        self.assertEqual(self.stage()['window']['deadline'], deadline - 1000)
        self.current = deadline + 1
        self.assertEqual(self.change(open=True, deadline=None)[0], 200)
        self.assertFalse(self.stage()['window']['canEdit'])
        self.assertEqual(self.change(open=True, start90Minutes=True)[0], 409)
        self.assertEqual(self.close()['window']['open'], False)
        self.assertTrue(self.start()['window']['canEdit'])

    def test_custom_shorter_window_is_allowed_but_longer_is_rejected(self):
        self.configure()
        before = self.stage()['stage']['revision']
        self.assertEqual(self.change(open=True, deadline=self.current + ROADSHOW_DURATION_MS + 1)[0], 400)
        self.assertEqual(self.stage()['stage']['revision'], before)
        status, shorter, _ = self.change(open=True, deadline=self.current + 10000)
        self.assertEqual(status, 200, shorter)
        self.assertEqual(shorter['window']['deadline'], self.current + 10000)
        self.close()
        self.assertEqual(self.change(open=True, deadline=0)[0], 200)
        self.assertFalse(self.stage()['window']['canEdit'])

    def test_deadline_blocks_numeric_and_qualitative_writes_at_exact_boundary(self):
        self.configure()
        deadline = self.start()['window']['deadline']
        self.current = deadline - 1
        self.assertEqual(self.score()[0], 200)
        self.assertEqual(self.review()[0], 200)
        for current in [deadline, deadline + 1]:
            self.current = current
            with self.subTest(current=current):
                status, numeric, _ = self.judge.request('GET', '/api/roadshow/scoring',
                    headers={'X-GOTC-Login': self.judge.login_id})
                self.assertEqual(status, 200, numeric)
                self.assertFalse(numeric['config']['canEdit'])
                self.assertFalse(self.stage()['window']['canEdit'])
                self.assertEqual(self.score(1)[0], 409)
                self.assertEqual(self.review(1)[0], 409)
        with self.service.db() as db:
            self.assertEqual(db.execute('SELECT revision FROM roadshow_scores').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT version FROM judging_reviews').fetchone()[0], 1)
        self.assertFalse(self.close()['window']['open'])

    def test_start_requires_roster_admin_and_strict_payload(self):
        self.assertEqual(self.change(open=True, start90Minutes=True)[0], 409)
        self.assertEqual(self.stage()['stage']['revision'], 0)
        self.configure()
        revision = self.stage()['stage']['revision']
        body = {'expectedRevision': revision, 'open': True, 'start90Minutes': True}
        for client, expected in [(self.public, 401), (self.judge, 403)]:
            self.assertEqual(client.post(self.path + '/window', body)[0], expected)
        bad = [{'start90Minutes': False}, {'start90Minutes': 1}, {'open': False},
               {'open': 1}, {'deadline': None}, {'expectedRevision': True}, {'unknown': 'field'}]
        for changes in bad:
            with self.subTest(changes=changes):
                self.assertEqual(self.admin.post(self.path + '/window', {**body, **changes})[0], 400)
        self.assertEqual(self.stage()['stage']['revision'], revision)
        self.assertFalse(self.stage()['window']['open'])

    def test_window_requires_matching_page_identity_for_both_start_and_close(self):
        self.configure()
        before = self.stage()
        body = {'expectedRevision': before['stage']['revision'], 'open': True, 'start90Minutes': True}
        marker = self.admin.login_id
        no_marker = fixtures.Client(self.server)
        no_marker.cookie = self.admin.cookie
        self.assertEqual(no_marker.post(self.path + '/window', body)[0], 428)
        self.assertEqual(self.admin.request('POST', self.path + '/window', body,
                                           headers={'X-GOTC-Login': 'wrong-marker'})[0], 401)
        self.assertEqual(self.stage(), before)
        replacement = fixtures.Client(self.server)
        self.assertEqual(replacement.post('/api/login', {'role': 'admin', 'code': self.admin_code})[0], 200)
        self.assertNotEqual(replacement.login_id, marker)
        self.assertEqual(replacement.request('POST', self.path + '/window', body,
                                            headers={'X-GOTC-Login': marker})[0], 401)
        self.assertEqual(self.stage(), before)
        status, opened, _ = replacement.post(self.path + '/window', body)
        self.assertEqual(status, 200, opened)
        close = {'expectedRevision': opened['stage']['revision'], 'open': False, 'deadline': None}
        self.assertEqual(replacement.request('POST', self.path + '/window', close,
                                            headers={'X-GOTC-Login': marker})[0], 401)
        self.assertTrue(self.stage()['window']['open'])
        self.assertEqual(self.public.request('POST', self.path + '/window', close,
                                            headers={'X-GOTC-Login': replacement.login_id})[0], 401)
        self.assertEqual(replacement.post(self.path + '/window', close)[0], 200)
        self.assertFalse(self.stage()['window']['open'])

    def test_exhibition_keeps_existing_unbounded_and_custom_window_behavior(self):
        self.configure('exhibition', judge_count=1)
        path = '/api/admin/judging/exhibition/window'
        revision = self.stage('exhibition')['stage']['revision']
        self.assertEqual(self.admin.post(path, {
            'expectedRevision': revision, 'open': True, 'start90Minutes': True})[0], 400)
        for deadline in [None, self.current + ROADSHOW_DURATION_MS * 2]:
            status, data, _ = self.admin.post(path, {
                'expectedRevision': self.stage('exhibition')['stage']['revision'], 'open': True, 'deadline': deadline})
            self.assertEqual(status, 200, data)
            self.assertEqual(data['window']['deadline'], deadline)
            self.assertTrue(data['window']['canEdit'])

    def test_start_needs_three_active_granted_judges_but_close_is_always_allowed(self):
        self.configure(judge_count=2)
        self.invitation(name='Active but not granted')
        self.assertEqual(self.change(open=True, start90Minutes=True)[0], 409)
        self.assertEqual(self.change(open=True, deadline=None)[0], 409)

        self.assertFalse(self.close()['window']['open'])
        _, third = self.invitation(name='Third granted judge')
        status, data, _ = self.admin.post(self.path + '/roster', {
            'expectedRevision': self.stage()['stage']['revision'], 'teamIds': [self.team_id],
            'judgeIds': self.judge_ids + [third['id']]})
        self.assertEqual(status, 200, data)
        self.start()
        self.assertEqual(self.admin.post('/api/admin/revoke', {'role': 'judge', 'id': third['id']})[0], 200)
        self.assertFalse(self.close()['window']['canEdit'])
        self.assertEqual(self.change(open=True, start90Minutes=True)[0], 409)
        self.assertEqual(self.change(open=True, deadline=None)[0], 409)

    def test_legacy_unbounded_roadshow_cannot_accept_scores(self):
        self.configure()
        with self.service.db() as db:
            db.execute("UPDATE judging_stages SET open=1,deadline=NULL WHERE stage='roadshow'")
        self.assertFalse(self.stage()['window']['canEdit'])
        self.assertEqual(self.score()[0], 409)
        self.assertEqual(self.review()[0], 409)
        self.close()
        self.assertTrue(self.start()['window']['canEdit'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
