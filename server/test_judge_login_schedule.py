"""Private judge-login schedule gates finals seat entry without touching the public board."""
import json
import unittest
from unittest.mock import patch

import test_finals as finals_tests


class JudgeLoginScheduleTests(unittest.TestCase):
    for name in ('setUp', 'tearDown', 'team', 'invitation', 'request', 'admin_view', 'settings', 'configure', 'score'):
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
        self.assertEqual(self.public.get('/api/finals/board')[0], 200)
        with patch('time.time', return_value=(opens + 1000) / 1000):
            self.assertTrue(self.public.get('/api/finals/login-status')[1]['loginAvailable'])
            self.assertEqual(self.request(self.judges[0], 'GET', '/api/finals/judge')[0], 200)

    def test_schedule_for_another_run_or_malformed_file_fails_closed(self):
        self.configure(1)
        self.write(opensAt=0, roadshowRunId='other', finalsRunId='other')
        self.assertFalse(self.public.get('/api/finals/login-status')[1]['loginAvailable'])
        self.assertEqual(self.request(self.judges[0], 'GET', '/api/finals/judge')[0], 403)
        (self.service.data_dir / 'judge-login-schedule.json').write_text('{"opensAt": 0}', encoding='utf-8')
        view = self.public.get('/api/finals/login-status')[1]
        self.assertFalse(view['loginAvailable'])
        self.assertIn('配置异常', view['message'])


if __name__ == '__main__':
    unittest.main()
