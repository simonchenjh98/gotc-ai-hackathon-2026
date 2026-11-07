"""Live stage rosters must not admit directory additions into the finals shortlist."""
import json
import unittest

from test_api import Client
import test_finals as finals_tests


class FinalsRosterShortlistTests(unittest.TestCase):
    for name in ('setUp', 'tearDown', 'team', 'invitation', 'request',
                 'admin_view', 'settings', 'configure', 'score', 'ballot'):
        locals()[name] = getattr(finals_tests.FinalsTests, name)

    def test_live_directory_does_not_admit_teams_into_shortlist(self):
        self.configure(2)
        judge = self.judges[0]
        outsider = self.team(3, 'Synthetic late directory addition')
        before = self.request(judge, 'GET', '/api/finals/judge')[1]
        self.assertEqual({team['id'] for team in before['teams']}, set(self.teams))
        self.assertIn(outsider, {team['id'] for team in self.admin_view()['availableTeams']})
        self.assertEqual(self.admin_view()['config']['teamIds'], self.teams)
        fresh = Client(self.server)
        fresh.finals_judge_id = self.seats[0]['id']
        self.assertEqual(self.request(fresh, 'GET', '/api/finals/entry')[0], 200)
        self.assertNotIn(outsider, json.dumps(self.request(fresh, 'GET', '/api/finals/judge')[1]))
        self.assertEqual(self.score(fresh, outsider)[0], 403)
        self.assertEqual(self.score(fresh)[0], 200)
        self.assertEqual(self.ballot(fresh)[0], 200)
        self.assertEqual(fresh.cookie, '')
        self.assertIsNone(fresh.get('/api/session')[1]['role'])
        self.assertEqual(fresh.get('/api/admin/finals')[0], 401)
        self.assertEqual(fresh.get('/api/finals/board')[0], 200)


if __name__ == '__main__':
    unittest.main()
