"""Real HTTP regressions for the separate roadshow numeric scoring tables."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import threading
from unittest.mock import patch
import json
import time
import unittest

from app import EventService
from roadshow_scoring import DIMENSIONS, TRIGGERS
import test_api as fixtures


class RoadshowScoringTests(unittest.TestCase):
    setUp = fixtures.APITests.setUp
    tearDown = fixtures.APITests.tearDown
    team = fixtures.APITests.team
    invitation = fixtures.APITests.invitation

    def request(self, client, method, path, body=None, marker=None):
        marker = client.login_id if marker is None else marker
        return client.request(method, path, body, headers={'X-GOTC-Login': marker})

    def admin_view(self):
        status, data, _ = self.request(self.admin, 'GET', '/api/admin/roadshow/scoring')
        self.assertEqual(status, 200, data)
        return data

    def stage(self):
        status, data, _ = self.admin.get('/api/admin/judging/roadshow')
        self.assertEqual(status, 200, data)
        return data

    def window(self, opened, deadline=None):
        return self.admin.post('/api/admin/judging/roadshow/window', {
            'expectedRevision': self.stage()['stage']['revision'], 'open': opened, 'deadline': deadline})

    def configure(self, team_count=1, judge_count=3):
        self.teams = [self.team(number, 'Roadshow Team ' + str(number)) for number in range(1, team_count + 1)]
        entries = [self.invitation(name='PRIVATE_ROADSHOW_JUDGE_' + str(index)) for index in range(judge_count)]
        self.judges, self.invites = [item[0] for item in entries], [item[1] for item in entries]
        stage = self.stage()
        status, data, _ = self.admin.post('/api/admin/judging/roadshow/roster', {
            'expectedRevision': stage['stage']['revision'], 'teamIds': self.teams,
            'judgeIds': [item['id'] for item in self.invites]})
        self.assertEqual(status, 200, data)
        self.run_id = data['stage']['runId']
        self.assertEqual(self.window(True)[0], 200)

    def score(self, client=None, team=None, revision=0, status='submitted', values=None, **changes):
        body = {'runId': self.run_id, 'teamId': team or self.teams[0], 'expectedRevision': revision,
                'status': status, 'scores': [20, 15, 20, 20, 15, 10] if values is None else values,
                'feedback': 'PRIVATE_SCORE_FEEDBACK'}
        body.update(changes)
        return self.request(client or self.judges[0], 'POST', '/api/roadshow/score', body)

    def fill(self):
        for client in self.judges:
            for team in self.teams:
                self.assertEqual(self.score(client, team)[0], 200)

    def publish(self, published=True, **changes):
        data = self.admin_view()
        body = {'runId': data['config']['runId'], 'expectedRevision': data['config']['revision'], 'published': published}
        body.update(changes)
        return self.request(self.admin, 'POST', '/api/admin/roadshow/publish', body)

    def board(self):
        status, data, _ = self.public.get('/api/roadshow/board')
        self.assertEqual(status, 200, data)
        return data

    def test_empty_authentication_exact_routes_and_privacy(self):
        self.assertEqual([item['max'] for item in DIMENSIONS], [20, 15, 20, 20, 15, 10])
        self.assertIsNone(self.board()['summary'])
        self.assertEqual(self.public.get('/api/admin/roadshow/scoring')[0], 401)
        self.assertEqual(self.admin.get('/api/admin/roadshow/scoring')[0], 428)
        self.assertEqual(self.request(self.admin, 'GET', '/api/admin/roadshow/scoring', marker='wrong')[0], 401)
        self.assertEqual(self.public.get('/api/roadshow/scoring')[0], 401)
        self.assertEqual(self.public.request('GET', '/api/roadshow/scoring', headers={'X-GOTC-Finals-Judge': 'GJfake'})[0], 401)
        self.assertEqual(self.public.get('/api/roadshow/board/')[0], 404)
        self.assertEqual(self.public.get('/api/roadshow/board?name=private')[0], 400)
        self.assertEqual(self.publish()[0], 409)
        self.configure()
        outsider, _ = self.invitation(name='Not granted')
        self.assertEqual(self.request(outsider, 'GET', '/api/roadshow/scoring')[0], 403)
        self.assertEqual(self.request(self.judges[0], 'GET', '/api/admin/roadshow/scoring')[0], 403)
        self.assertEqual(self.judges[0].get('/api/roadshow/scoring')[0], 428)
        self.assertEqual(self.request(self.judges[0], 'GET', '/api/roadshow/scoring', marker=self.judges[1].login_id)[0], 401)
        self.assertEqual(self.score()[0], 200)
        own = self.request(self.judges[1], 'GET', '/api/roadshow/scoring')[1]
        self.assertEqual(own['myScores'], [])
        self.assertNotIn('PRIVATE_SCORE_FEEDBACK', json.dumps(self.admin_view()))
        self.assertIsNone(self.board()['summary'])

    def test_strict_scores_and_revision_types(self):
        self.configure()
        bad = [[20, 16, 20, 20, 15, 10], [-0.1, 0, 0, 0, 0, 0], [1.11, 0, 0, 0, 0, 0],
               [True, 0, 0, 0, 0, 0], ['20', 15, 20, 20, 15, 10], [], {}, [float('inf')] * 6, [float('nan')] * 6]
        for values in bad:
            with self.subTest(values=values):
                self.assertEqual(self.score(values=values)[0], 400)
        for changes in ({'expectedRevision': True}, {'expectedRevision': -1}, {'expectedRevision': 9007199254740992},
                        {'status': []}, {'status': 'unknown'}, {'feedback': {}}, {'extra': True}):
            with self.subTest(changes=changes):
                self.assertEqual(self.score(**changes)[0], 400)
        self.assertEqual(self.score(status='recused')[0], 400)
        self.assertEqual(self.score(values=[19.9, 14.8, 19.7, 19.6, 14.5, 9.4])[0], 200)
        self.assertEqual(self.admin_view()['summary']['teams'][0]['score'], 97.9)
        self.assertEqual(self.score(revision=1, status='draft')[0], 409)
        self.assertEqual(self.score(revision=0)[0], 409)

    def test_drafts_recusal_replacement_average_and_three_distinct_judges(self):
        self.configure(judge_count=4)
        self.assertEqual(self.score(status='draft', values=[0] * 6)[0], 200)
        self.assertIsNone(self.admin_view()['summary']['teams'][0]['score'])
        self.assertEqual(self.score(revision=1)[0], 200)
        self.assertEqual(self.score(self.judges[1], values=[10, 5, 10, 10, 5, 10])[0], 200)
        row = self.admin_view()['summary']['teams'][0]
        self.assertEqual((row['score'], row['scoreCount'], row['valid'], row['missing'], row['rank']), (75, 2, False, 1, None))
        self.assertEqual(self.score(self.judges[2], values=[10, 15, 20, 20, 15, 10])[0], 200)
        self.assertEqual(self.score(self.judges[3], status='recused', values=[])[0], 200)
        row = self.admin_view()['summary']['teams'][0]
        self.assertEqual((row['score'], row['scoreCount'], row['recusedCount'], row['valid'], row['rank']), (80, 3, 1, True, 1))
        self.assertEqual(self.score(revision=2, values=[10, 15, 20, 20, 15, 10])[0], 200)
        row = self.admin_view()['summary']['teams'][0]
        self.assertEqual((row['score'], row['scoreCount']), (76.67, 3))
        self.assertEqual(self.score(self.judges[1], revision=1, status='recused', values=[])[0], 200)
        row = self.admin_view()['summary']['teams'][0]
        self.assertEqual((row['score'], row['scoreCount'], row['recusedCount'], row['valid']), (90, 2, 2, False))
        self.assertEqual(self.window(False)[0], 200)
        self.assertEqual(self.publish()[0], 409)
        with self.service.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM roadshow_scores').fetchone()[0], 4)
            for table in ('scores', 'finals_scores', 'finals_nominations', 'judging_reviews'):
                self.assertEqual(db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0], 0)

    def test_concurrent_edits_preserve_one_winner(self):
        self.configure()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda points: self.score(values=[points, 0, 0, 0, 0, 0]), [10, 20]))
        self.assertEqual(sorted(result[0] for result in results), [200, 409])
        saved = self.request(self.judges[0], 'GET', '/api/roadshow/scoring')[1]['myScores']
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]['revision'], 1)

    def test_window_deadline_eligibility_and_revocation(self):
        self.configure()
        self.assertEqual(self.window(False)[0], 200)
        self.assertEqual(self.score()[0], 409)
        self.assertEqual(self.window(True, int(time.time() * 1000) - 1)[0], 200)
        self.assertEqual(self.score()[0], 409)
        self.assertEqual(self.window(False)[0], 200)
        self.assertEqual(self.window(True)[0], 200)
        self.assertEqual(self.score(team='missing')[0], 403)
        self.assertEqual(self.score(runId='old-run')[0], 409)
        with self.service.db() as db:
            db.execute("UPDATE team_details SET status='withdrawn' WHERE team_id=?", (self.teams[0],))
        self.assertEqual(self.score()[0], 403)
        with self.service.db() as db:
            db.execute("UPDATE team_details SET status='active' WHERE team_id=?", (self.teams[0],))
        self.assertEqual(self.admin.post('/api/admin/revoke', {'role': 'judge', 'id': self.invites[0]['id']})[0], 200)
        self.assertEqual(self.score()[0], 401)
        self.assertEqual(self.request(self.judges[0], 'GET', '/api/roadshow/scoring')[0], 401)

    def test_publish_requires_three_scores_and_public_matches_valid_internal_results(self):
        self.configure(team_count=2)
        self.assertEqual(self.publish()[0], 409)
        self.assertEqual(self.window(False)[0], 200)
        self.assertEqual(self.publish()[0], 409)
        self.assertEqual(self.window(True)[0], 200)
        self.fill()
        stale = self.admin_view()['config']['revision']
        self.assertEqual(self.window(False)[0], 200)
        self.assertEqual(self.publish(expectedRevision=stale)[0], 409)
        self.assertEqual(self.publish(published='yes')[0], 400)
        self.assertEqual(self.publish()[0], 200)
        public = self.board()
        self.assertTrue(public['config']['published'])
        self.assertEqual(public['summary'], self.admin_view()['summary'])
        serialized = json.dumps(public)
        for forbidden in ('PRIVATE_SCORE_FEEDBACK', 'PRIVATE_ROADSHOW_JUDGE', 'myScores', 'materialSnapshot', 'judgeId'):
            self.assertNotIn(forbidden, serialized)
        self.assertEqual([row['rank'] for row in public['summary']['teams']], [1, 1])
        self.assertEqual(self.publish(False)[0], 200)
        self.assertIsNone(self.board()['summary'])

    def test_expired_window_publishes_qualified_teams_and_hides_insufficient_scores(self):
        self.configure(team_count=3, judge_count=5)
        for client in self.judges[:3]:
            self.assertEqual(self.score(client, self.teams[0], values=[10, 15, 20, 20, 15, 10])[0], 200)
        self.assertEqual(self.score(self.judges[3], self.teams[0], values=[0] * 6)[0], 200)
        # All four submitted scores count, not only the first three.
        self.assertEqual(self.score(self.judges[0], self.teams[1])[0], 200)
        self.assertEqual(self.score(self.judges[1], self.teams[1])[0], 200)
        self.assertEqual(self.score(self.judges[2], self.teams[1], status='draft')[0], 200)
        self.assertEqual(self.score(self.judges[3], self.teams[1], status='recused', values=[])[0], 200)
        internal = self.admin_view()
        self.assertEqual(internal['config']['minimumScores'], 3)
        self.assertEqual(internal['summary']['minimumScores'], 3)
        self.assertEqual(internal['summary']['qualifiedCount'], 1)
        self.assertFalse(internal['summary']['canPublish'])
        self.assertEqual([row['id'] for row in internal['summary']['teams']], self.teams)
        self.assertEqual(internal['summary']['teams'][0]['score'], 67.5)
        self.assertEqual(internal['summary']['teams'][1]['score'], 100)
        self.assertEqual(internal['summary']['teams'][1]['missing'], 1)
        self.assertIsNone(internal['summary']['teams'][1]['rank'])
        self.assertEqual(self.publish()[0], 409)
        deadline = self.stage()['window']['deadline']
        self.assertIsNotNone(deadline)
        with patch('app.now_ms', return_value=deadline):
            expired = self.admin_view()
            self.assertTrue(expired['config']['open'])
            self.assertFalse(expired['config']['canEdit'])
            self.assertTrue(expired['summary']['canPublish'])
            self.assertEqual(self.score(self.judges[4], self.teams[0])[0], 409)
            self.assertEqual(self.publish()[0], 200)
            public = self.board()
            self.assertTrue(public['config']['published'])
            valid, insufficient, empty = public['summary']['teams']
            self.assertEqual((valid['score'], valid['scoreCount'], valid['rank']), (67.5, 4, 1))
            self.assertEqual((insufficient['score'], insufficient['rank'], insufficient['valid']), (None, None, False))
            self.assertEqual(insufficient['dimensions'], [None] * 6)
            self.assertEqual((empty['score'], empty['rank'], empty['missing']), (None, None, 3))
            self.assertEqual(public['summary']['qualifiedCount'], 1)

    def test_restore_of_team_judge_metadata_or_status_does_not_resurrect_publication(self):
        self.configure()
        self.fill()
        self.assertEqual(self.window(False)[0], 200)
        changes = [
            ("UPDATE team_details SET status=? WHERE team_id=?", ('disqualified', self.teams[0]), ('active', self.teams[0])),
            ("UPDATE teams SET name=? WHERE id=?", ('Changed', self.teams[0]), ('Roadshow Team 1', self.teams[0])),
            ("UPDATE teams SET project=? WHERE id=?", ('Changed project', self.teams[0]), ('真实问题', self.teams[0])),
            ("UPDATE teams SET number=? WHERE id=?", (99, self.teams[0]), (1, self.teams[0])),
            ("UPDATE accounts SET active=? WHERE id=?", (0, self.invites[0]['id']), (1, self.invites[0]['id'])),
            ("UPDATE accounts SET name=? WHERE id=?", ('Changed judge', self.invites[0]['id']), ('PRIVATE_ROADSHOW_JUDGE_0', self.invites[0]['id'])),
        ]
        for sql, changed, restored in changes:
            with self.subTest(sql=sql):
                self.assertEqual(self.publish()[0], 200)
                before = self.admin_view()['config']['revision']
                with self.service.db() as db:
                    db.execute(sql, changed)
                self.assertFalse(self.board()['config']['published'])
                with self.service.db() as db:
                    db.execute(sql, restored)
                self.assertIsNone(self.board()['summary'])
                self.assertGreater(self.admin_view()['config']['revision'], before)
        self.assertEqual(self.publish()[0], 200)
        self.assertEqual(self.window(True)[0], 200)
        self.assertEqual(self.window(False)[0], 200)
        self.assertFalse(self.board()['config']['published'])

    def test_roster_grant_round_and_score_changes_invalidate_publication(self):
        self.configure()
        self.fill()
        self.assertEqual(self.window(False)[0], 200)
        outsider, invite = self.invitation(name='Extra judge')
        self.assertEqual(self.publish()[0], 200)
        with self.service.db() as db:
            db.execute('INSERT INTO judging_grants VALUES (?,?)', (self.run_id, invite['id']))
        self.assertFalse(self.board()['config']['published'])
        with self.service.db() as db:
            db.execute('DELETE FROM judging_grants WHERE run_id=? AND judge_id=?', (self.run_id, invite['id']))
        self.assertFalse(self.board()['config']['published'])
        self.assertEqual(self.publish()[0], 200)
        extra_team = self.team(3, 'Not scored')
        with self.service.db() as db:
            db.execute('INSERT INTO judging_roster VALUES (?,?,?)', (self.run_id, extra_team, '{}'))
            db.execute('DELETE FROM judging_roster WHERE run_id=? AND team_id=?', (self.run_id, extra_team))
        self.assertFalse(self.board()['config']['published'])
        self.assertEqual(self.publish()[0], 200)
        with self.service.db() as db:
            db.execute('UPDATE roadshow_scores SET revision=revision+1 WHERE run_id=?', (self.run_id,))
        self.assertFalse(self.board()['config']['published'])
        self.assertEqual(self.publish()[0], 200)
        old_run = self.run_id
        status, data, _ = self.admin.post('/api/admin/judging/roadshow/reset', {
            'expectedRevision': self.stage()['stage']['revision'], 'reason': 'Explicit next round'})
        self.assertEqual(status, 200, data)
        self.assertFalse(self.board()['config']['published'])
        self.assertEqual(self.admin_view()['summary']['teams'][0]['scoreCount'], 0)
        self.assertEqual(self.window(True)[0], 200)
        self.assertEqual(self.score(revision=1, runId=old_run)[0], 409)
        with self.service.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM roadshow_scores WHERE run_id=?', (old_run,)).fetchone()[0], 3)

    def test_numeric_records_lock_both_roster_editors_even_without_frozen_marker(self):
        self.configure()
        self.assertEqual(self.score()[0], 200)
        self.assertEqual(self.window(False)[0], 200)
        with self.service.db() as db:
            db.execute('UPDATE judging_runs SET frozen_at=NULL WHERE id=?', (self.run_id,))
        self.assertFalse(self.stage()['stage']['rosterEditable'])
        status, _, _ = self.admin.post('/api/admin/judging/roadshow/roster', {
            'expectedRevision': self.stage()['stage']['revision'], 'teamIds': [], 'judgeIds': []})
        self.assertEqual(status, 409)
        access = self.request(self.admin, 'GET', '/api/admin/roadshow-access')[1]
        self.assertFalse(access['editable'])
        status, _, _ = self.request(self.admin, 'POST', '/api/admin/roadshow-access', {
            'expectedRevision': access['revision'], 'runId': self.run_id, 'names': []})
        self.assertEqual(status, 409)

    def test_queued_write_rechecks_close_and_revocation_after_lock(self):
        self.configure()
        original_db = self.service.db
        for action, status in [('close', 409), ('revoke', 401)]:
            with self.subTest(action=action):
                if action == 'revoke':
                    self.assertEqual(self.window(True)[0], 200)
                queued = threading.Event()
                @contextmanager
                def traced_db():
                    with original_db() as db:
                        db.set_trace_callback(lambda sql: queued.set() if sql == 'BEGIN IMMEDIATE' else None)
                        yield db
                results = []
                with original_db() as lock:
                    lock.execute('BEGIN IMMEDIATE')
                    with patch.object(self.service, 'db', traced_db):
                        worker = threading.Thread(target=lambda: results.append(self.score()))
                        worker.start()
                        self.assertTrue(queued.wait(3), 'Score did not wait for the database writer lock')
                        if action == 'close':
                            lock.execute("UPDATE judging_stages SET open=0,revision=revision+1 WHERE stage='roadshow'")
                        else:
                            lock.execute('UPDATE accounts SET active=0 WHERE id=?', (self.invites[0]['id'],))
                            lock.execute('DELETE FROM sessions WHERE account_id=?', (self.invites[0]['id'],))
                        lock.commit()
                        worker.join(5)
                        self.assertFalse(worker.is_alive())
                self.assertEqual(results[0][0], status, results)
                with original_db() as db:
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM roadshow_scores').fetchone()[0], 0)

    def test_prethreshold_snapshot_requires_explicit_republication(self):
        self.configure()
        self.fill()
        self.assertEqual(self.window(False)[0], 200)
        self.assertEqual(self.publish()[0], 200)
        with self.service.db() as db:
            saved = json.loads(db.execute('SELECT publication_json FROM roadshow_scoring_config WHERE id=1').fetchone()[0])
            saved.pop('minimumScores')
            db.execute('UPDATE roadshow_scoring_config SET publication_json=? WHERE id=1', (json.dumps(saved),))
        self.assertFalse(self.board()['config']['published'])
        self.assertIsNone(self.board()['summary'])
        self.assertTrue(self.admin_view()['config']['publicationStale'])
        self.assertEqual(self.publish()[0], 200)
        self.assertTrue(self.board()['config']['published'])

    def test_restart_and_missing_invalidation_trigger_upgrade_are_idempotent(self):
        self.configure()
        self.fill()
        self.assertEqual(self.window(False)[0], 200)
        self.assertEqual(self.publish()[0], 200)
        original = self.board()
        previous = self.service
        self.service = EventService(data_dir=previous.data_dir, base_path=previous.base_path,
                                    origin=previous.origin, static_root=previous.root)
        self.server.service = self.service
        self.assertEqual(self.board(), original)
        with self.service.db() as db:
            db.execute('DROP TRIGGER roadshow_scoring_team_metadata')
        self.service = EventService(data_dir=previous.data_dir, base_path=previous.base_path,
                                    origin=previous.origin, static_root=previous.root)
        self.server.service = self.service
        self.assertFalse(self.board()['config']['published'])
        with self.service.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM roadshow_scores').fetchone()[0], 3)
            self.assertTrue({'roadshow_scoring_' + item[0] for item in TRIGGERS} <=
                            {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='trigger'")})


if __name__ == '__main__':
    unittest.main()
