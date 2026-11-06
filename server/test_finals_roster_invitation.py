"""Live stage rosters must retain the independent finals invitation boundary."""
import json
import unittest
from unittest.mock import patch

from test_api import Client
import test_finals as finals_tests
from test_finals import DIMENSIONS, FINALS_INVITATION


class FinalsRosterInvitationTests(unittest.TestCase):
    for name in ('setUp', 'tearDown', 'team', 'invitation', 'authorize', 'request',
                 'admin_view', 'settings', 'configure', 'score', 'ballot'):
        locals()[name] = getattr(finals_tests.FinalsTests, name)

    def test_live_directory_does_not_admit_teams_or_bypass_invitation(self):
        self.configure(2)
        judge = self.judges[0]
        outsider = self.team(3, 'Synthetic late directory addition')
        before = self.request(judge, 'GET', '/api/finals/judge')[1]
        self.assertEqual({team['id'] for team in before['teams']}, set(self.teams))
        self.assertIn(outsider, {team['id'] for team in self.admin_view()['availableTeams']})
        self.assertEqual(self.admin_view()['config']['teamIds'], self.teams)
        fresh = Client(self.server)
        self.assertEqual(fresh.post('/api/finals/access', {'code': 'wrong-code'})[0], 401)
        score = {'runId': self.run_id, 'teamId': self.teams[0], 'expectedRevision': 0,
                 'status': 'submitted', 'scores': [0] * 6, 'feedback': 'Synthetic'}
        ballot = {'runId': self.run_id, 'expectedRevision': 0, 'status': 'submitted',
                  'nominations': {dimension['id']: None for dimension in DIMENSIONS}}
        for token in ('', 'a' * 43, FINALS_INVITATION):
            headers = {'X-GOTC-Finals-Judge': self.seats[0]['id'], 'X-GOTC-Finals-Access': token}
            for method, path, body in [('GET', '/api/finals/entry', None),
                                       ('GET', '/api/finals/judge', None),
                                       ('POST', '/api/finals/score', score),
                                       ('POST', '/api/finals/nominations', ballot)]:
                result = fresh.request(method, path, body, headers=headers)
                self.assertEqual(result[0], 401, (path, result))
                self.assertNotIn(self.teams[0], json.dumps(result[1]))
        grant = self.authorize(fresh)
        fresh.finals_judge_id = self.seats[0]['id']
        self.assertEqual(self.request(fresh, 'GET', '/api/finals/entry')[0], 200)
        self.assertEqual(self.score(fresh, outsider)[0], 403)
        with patch.object(self.service.finals, 'clock', return_value=grant['expiresAt']):
            self.assertEqual(self.request(fresh, 'GET', '/api/finals/judge')[0], 401)
            self.assertEqual(self.score(fresh)[0], 401)
            self.assertEqual(self.ballot(fresh)[0], 401)
        with self.service.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM finals_scores').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM finals_nominations').fetchone()[0], 0)
        self.assertEqual(self.score(fresh)[0], 200)
        self.assertEqual(self.ballot(fresh)[0], 200)
        self.assertEqual(fresh.cookie, '')
        self.assertIsNone(fresh.get('/api/session')[1]['role'])
        self.assertEqual(fresh.get('/api/admin/finals')[0], 401)
        self.assertEqual(fresh.get('/api/finals/board')[0], 200)


if __name__ == '__main__':
    unittest.main()
