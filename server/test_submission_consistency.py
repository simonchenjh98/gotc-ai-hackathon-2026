"""Submission concurrency, cutoff timing and link-only snapshot contracts."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
import copy
import json
import sqlite3
import threading
import unittest
from unittest.mock import patch

import app
import test_api as api_helpers
import test_submissions as submission_helpers


class SubmissionConsistencyTests(unittest.TestCase):
    setUp = api_helpers.APITests.setUp
    tearDown = api_helpers.APITests.tearDown
    team = api_helpers.APITests.team
    invitation = api_helpers.APITests.invitation
    access = submission_helpers.SubmissionAPITests.access
    materials = submission_helpers.SubmissionAPITests.materials

    def persisted(self, team_id):
        with self.service.db() as db:
            return {
                'team': tuple(db.execute('SELECT * FROM teams WHERE id=?', (team_id,)).fetchone()),
                'submission': [tuple(row) for row in db.execute('SELECT * FROM submissions WHERE team_id=?', (team_id,))],
                'settings': [tuple(row) for row in db.execute('SELECT * FROM settings ORDER BY key')],
                'audit': [tuple(row) for row in db.execute('SELECT * FROM audit ORDER BY id')],
            }

    def test_two_legal_sessions_share_one_cas_and_old_payload_replay_is_not_idempotent(self):
        team_id = self.team()
        first, access = self.access(team_id)
        second = api_helpers.Client(self.server)
        self.assertEqual(second.post('/api/login', {'role': 'team', 'code': access['code']})[0], 200)
        self.assertNotEqual(first.cookie, second.cookie)
        self.assertEqual(self.admin.post('/api/admin/settings', {'submissionsOpen': True})[0], 200)
        payloads = [{'expectedVersion': 0, 'status': 'submitted',
                     'materials': self.materials(projectName='Concurrent ' + suffix)} for suffix in ['A', 'B']]
        barrier = threading.Barrier(2)
        def save(pair):
            client, payload = pair
            barrier.wait(timeout=3)
            return client.post('/api/submission', payload)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(save, [(first, payloads[0]), (second, payloads[1])]))
        self.assertEqual(sorted(result[0] for result in results), [200, 409])
        winner = next(index for index, result in enumerate(results) if result[0] == 200)
        state = first.get('/api/team')[1]
        self.assertEqual(state['submission']['version'], 1)
        self.assertEqual(state['submission']['submittedVersion'], 1)
        self.assertEqual(state['submission']['submitted']['projectName'], payloads[winner]['materials']['projectName'])
        self.assertEqual(state['team']['project'], payloads[winner]['materials']['projectName'])
        before = self.persisted(team_id)
        self.assertEqual(second.post('/api/submission', payloads[winner])[0], 409)
        self.assertEqual(self.persisted(team_id), before)

    def test_request_queued_before_deadline_is_rejected_after_lock_without_partial_writes(self):
        team_id = self.team()
        client, _ = self.access(team_id)
        clock = [app.now_ms()]
        deadline = clock[0] + 1000
        attempted = threading.Event()
        original_db = self.service.db
        @contextmanager
        def observed_db():
            with original_db() as db:
                class Observed:
                    def __getattr__(self, name):
                        return getattr(db, name)
                    def execute(self, sql, *values):
                        if sql.startswith('BEGIN IMMEDIATE'):
                            attempted.set()
                        return db.execute(sql, *values)
                yield Observed()
        with patch('app.now_ms', side_effect=lambda: clock[0]):
            self.assertEqual(self.admin.post('/api/admin/settings', {
                'submissionsOpen': True, 'submissionDeadline': deadline})[0], 200)
            self.assertEqual(client.post('/api/submission', {
                'expectedVersion': 0, 'status': 'draft', 'materials': {'summary': 'Retain this draft'}})[0], 200)
            before = self.persisted(team_id)
            holder = sqlite3.connect(self.service.db_path, timeout=5)
            pool = ThreadPoolExecutor(max_workers=1)
            try:
                holder.execute('BEGIN IMMEDIATE')
                clock[0] = deadline - 1
                with patch.object(self.service, 'db', observed_db):
                    future = pool.submit(client.post, '/api/submission', {
                        'expectedVersion': 1, 'status': 'submitted',
                        'materials': self.materials(projectName='Must not partially publish')})
                    self.assertTrue(attempted.wait(3), 'HTTP request must reach BEGIN before simulated cutoff')
                    self.assertFalse(future.done())
                    clock[0] = deadline
                    holder.commit()
                    status, _, _ = future.result(timeout=6)
                    self.assertEqual(status, 409)
            finally:
                holder.rollback()
                holder.close()
                pool.shutdown(wait=True)
            self.assertEqual(self.persisted(team_id), before)
            state = client.get('/api/team')[1]
            self.assertFalse(state['window']['canEdit'])
            self.assertEqual(state['submission']['version'], 1)
            self.assertIsNone(state['submission']['submitted'])

    def test_formal_link_snapshot_is_data_only_and_never_fetches_external_content(self):
        team_id = self.team()
        client, _ = self.access(team_id)
        judge, _ = self.invitation()
        self.assertEqual(self.admin.post('/api/admin/settings', {'submissionsOpen': True})[0], 200)
        materials = self.materials(codeUrl='https://github.com/synthetic/repository',
                                   readmeUrl='https://example.invalid/README?version=1')
        original = copy.deepcopy(materials)
        with patch('socket.create_connection', side_effect=AssertionError('No outbound material fetch')) as connect, \
                patch('socket.getaddrinfo', side_effect=AssertionError('No outbound DNS')) as dns, \
                patch('urllib.request.urlopen', side_effect=AssertionError('No external URL open')) as urlopen:
            result = self.service.dispatch('POST', '/api/submission', {
                'expectedVersion': 0, 'status': 'submitted', 'materials': materials}, client.cookie, '127.0.0.1')[0]
            materials['readmeUrl'] = 'https://example.invalid/README?version=2'
            view = self.service.dispatch('GET', '/api/judge', {}, judge.cookie, '127.0.0.1')[0]
            self.assertEqual(view['submissions'][0]['materials']['readmeUrl'], original['readmeUrl'])
            self.assertEqual(result['submission']['submitted']['codeUrl'], original['codeUrl'])
            connect.assert_not_called()
            dns.assert_not_called()
            urlopen.assert_not_called()
        with self.service.db() as db:
            stored = json.loads(db.execute('SELECT submitted_json FROM submissions WHERE team_id=?', (team_id,)).fetchone()[0])
            self.assertEqual(stored['readmeUrl'], original['readmeUrl'])
            self.assertEqual(stored['codeUrl'], original['codeUrl'])
            self.assertTrue(all(isinstance(value, (str, bool)) for value in stored.values()))


if __name__ == '__main__':
    unittest.main()
