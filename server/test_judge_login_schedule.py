"""Private judge-login schedule gates finals seat entry without touching the public board."""
import json
from contextlib import contextmanager
import threading
import unittest
from unittest.mock import patch

import test_finals as finals_tests
import test_roadshow_access as roadshow_access_tests
import test_roadshow_name_login as roadshow_login_tests
from test_api import Client


class JudgeLoginScheduleTests(unittest.TestCase):
    for name in ('setUp', 'tearDown', 'team', 'invitation', 'authorize', 'request', 'admin_view', 'settings', 'configure', 'score', 'ballot'):
        locals()[name] = getattr(finals_tests.FinalsTests, name)

    def write(self, **config):
        path = self.service.data_dir / 'judge-login-schedule.json'
        path.write_text(json.dumps(config), encoding='utf-8')
        return path

    def test_missing_file_keeps_login_open(self):
        status, view, _ = self.public.get('/api/finals/login-status')
        self.assertEqual((status, view['loginAvailable'], view['loginOpensAt']), (200, True, None))

    def test_future_schedule_blocks_seat_routes_until_opening_time(self):
        self.configure(1)
        run_id = self.admin_view()['config']['runId']
        with self.service.db() as db:
            roadshow_run = self.service.judging.state(db, 'roadshow')['run_id']
        opens = self.service.finals.clock() + 3600000
        self.write(opensAt=opens, roadshowRunId=roadshow_run, finalsRunId=run_id)
        status, view, _ = self.public.get('/api/finals/login-status')
        self.assertEqual(status, 200)
        self.assertFalse(view['loginAvailable'])
        self.assertIn('开放', view['message'])
        for path in ('/api/finals/entry', '/api/finals/judge'):
            result = self.request(self.judges[0], 'GET', path)
            self.assertEqual((result[0], result[1].get('code')), (403, 'judge_login_not_open'), path)
        self.assertEqual(self.score(self.judges[0])[0], 403)
        self.assertEqual(self.ballot(self.judges[0])[0], 403)
        with self.service.db() as db:
            tokens_before = db.execute('SELECT COUNT(*) FROM finals_access_tokens').fetchone()[0]
        fresh = Client(self.server)
        status, rejected, headers = fresh.post('/api/finals/access', {'code': finals_tests.FINALS_INVITATION})
        self.assertEqual((status, rejected.get('code')), (403, 'judge_login_not_open'))
        self.assertNotIn('Set-Cookie', headers)
        with self.service.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM finals_access_tokens').fetchone()[0], tokens_before)
        self.assertEqual(self.public.get('/api/finals/board')[0], 200)
        with patch('time.time', return_value=(opens + 1000) / 1000):
            self.assertTrue(self.public.get('/api/finals/login-status')[1]['loginAvailable'])
            self.assertEqual(self.request(self.judges[0], 'GET', '/api/finals/judge')[0], 200)
            self.assertEqual(fresh.post('/api/finals/access', {'code': 'wrong-fixture-code'})[0], 401)
            self.authorize(fresh)
            self.assertEqual(self.request(fresh, 'GET', '/api/finals/entry')[0], 200)
        # Moving the gate back to the future also blocks a token granted while it was open.
        self.assertEqual(self.request(fresh, 'GET', '/api/finals/entry')[0], 403)

    def test_open_schedule_never_replaces_invitation_authorization(self):
        self.configure(1)
        with self.service.db() as db:
            roadshow_run = self.service.judging.state(db, 'roadshow')['run_id']
        self.write(opensAt=0, roadshowRunId=roadshow_run, finalsRunId=self.run_id)
        self.assertTrue(self.public.get('/api/finals/login-status')[1]['loginAvailable'])
        fresh = Client(self.server)
        score = {'runId': self.run_id, 'teamId': self.teams[0], 'expectedRevision': 0,
                 'status': 'submitted', 'scores': [10] * 6, 'feedback': ''}
        ballot = {'runId': self.run_id, 'expectedRevision': 0, 'status': 'submitted',
                  'nominations': {dimension['id']: None for dimension in finals_tests.DIMENSIONS}}
        for client in (fresh, self.admin):
            for method, path, body in [('GET', '/api/finals/entry', None), ('GET', '/api/finals/judge', None),
                                       ('POST', '/api/finals/score', score), ('POST', '/api/finals/nominations', ballot)]:
                status, result, _ = client.request(method, path, body, headers={'X-GOTC-Finals-Judge': self.seats[0]['id']})
                self.assertEqual((status, result.get('code')), (401, 'finals_access_required'), path)
        self.assertEqual(fresh.post('/api/finals/access', {'code': 'wrong-fixture-code'})[0], 401)
        self.assertEqual(self.public.get('/api/finals/board')[0], 200)
        with self.service.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM finals_scores').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM finals_nominations').fetchone()[0], 0)

    def test_queued_score_checks_schedule_after_acquiring_writer_lock(self):
        self.configure(1)
        with self.service.db() as db:
            roadshow_run = self.service.judging.state(db, 'roadshow')['run_id']
        original_db, queued, result = self.service.db, threading.Event(), []
        @contextmanager
        def traced_db():
            with original_db() as db:
                db.set_trace_callback(lambda statement: queued.set() if statement == 'BEGIN IMMEDIATE' else None)
                yield db
        with original_db() as lock:
            lock.execute('BEGIN IMMEDIATE')
            with patch.object(self.service, 'db', traced_db):
                worker = threading.Thread(target=lambda: result.append(self.score(self.judges[0])))
                worker.start()
                try:
                    self.assertTrue(queued.wait(3), 'Finals write did not queue for its lock')
                    self.write(opensAt=self.service.finals.clock() + 3600000,
                               roadshowRunId=roadshow_run, finalsRunId=self.run_id)
                finally:
                    lock.commit()
                    worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual((result[0][0], result[0][1].get('code')), (403, 'judge_login_not_open'))
        with original_db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM finals_scores').fetchone()[0], 0)

    def test_schedule_for_another_run_or_malformed_file_fails_closed(self):
        self.configure(1)
        self.write(opensAt=0, roadshowRunId='other', finalsRunId='other')
        self.assertFalse(self.public.get('/api/finals/login-status')[1]['loginAvailable'])
        self.assertEqual(self.request(self.judges[0], 'GET', '/api/finals/judge')[0], 403)
        (self.service.data_dir / 'judge-login-schedule.json').write_text('{"opensAt": 0}', encoding='utf-8')
        view = self.public.get('/api/finals/login-status')[1]
        self.assertFalse(view['loginAvailable'])
        self.assertIn('配置异常', view['message'])


class RoadshowLoginScheduleTests(unittest.TestCase):
    for name in ('setUp', 'tearDown', 'team', 'invitation', 'admin_request', 'view', 'save',
                 'configure', 'stage', 'add_team', 'login', 'numeric'):
        locals()[name] = getattr(roadshow_login_tests.RoadshowNameLoginTests, name)
    endpoint = '/api/roadshow/login'

    def test_existing_cookie_and_legacy_code_follow_schedule_without_restarting_window(self):
        self.configure(['Judge Alpha', 'Judge Beta', 'Judge Gamma'])
        self.add_team()
        self.assertEqual(roadshow_access_tests.RoadshowAccessTests.set_window(self, True)[0], 200)
        status, claimed, _ = self.public.post('/api/roadshow/claim', {'name': 'Judge Alpha'})
        self.assertEqual(status, 200)
        client, status, _, _ = self.login('Judge Alpha')
        self.assertEqual(status, 200)
        with self.service.db() as db:
            stage = self.service.judging.state(db, 'roadshow')
            run_id, deadline = stage['run_id'], stage['deadline']
            finals_run = self.service.finals.state(db)['run_id']
        opens = self.service.finals.clock() + 3600000
        (self.service.data_dir / 'judge-login-schedule.json').write_text(json.dumps({
            'opensAt': opens, 'roadshowRunId': run_id, 'finalsRunId': finals_run}), encoding='utf-8')
        self.assertFalse(self.public.get('/api/roadshow/access')[1]['nameLoginAvailable'])
        self.assertEqual(self.login('Judge Alpha')[1], 403)
        self.assertEqual(Client(self.server).post('/api/login', {'role': 'judge', 'code': claimed['code']})[0], 403)
        for path in ('/api/roadshow/scoring', '/api/judging/roadshow'):
            status, result, _ = client.request('GET', path, headers={'X-GOTC-Login': client.login_id})
            self.assertEqual((status, result.get('code')), (403, 'judge_login_not_open'), path)
        self.assertEqual(self.public.get('/api/roadshow/board')[0], 200)
        with patch('time.time', return_value=opens / 1000):
            self.assertEqual(self.numeric(client)[0], 200)
            self.assertEqual(self.login('Judge Alpha')[1], 200)
        with self.service.db() as db:
            self.assertEqual(self.service.judging.state(db, 'roadshow')['deadline'], deadline)


if __name__ == '__main__':
    unittest.main()
