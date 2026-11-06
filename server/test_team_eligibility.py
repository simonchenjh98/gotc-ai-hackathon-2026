"""HTTP regressions for current eligibility without deleting historical results."""
import unittest

import test_api as fixtures


class TeamEligibilityTests(unittest.TestCase):
    setUp = fixtures.APITests.setUp
    tearDown = fixtures.APITests.tearDown
    team = fixtures.APITests.team
    invitation = fixtures.APITests.invitation

    def set_status(self, team_id, status):
        current = next(team for team in self.admin.get('/api/admin')[1]['teams'] if team['id'] == team_id)
        response = self.admin.post('/api/admin/teams', {
            'id': team_id, 'expectedRevision': current['revision'], 'status': status,
            'statusReason': '' if status == 'active' else 'Synthetic eligibility change',
        })
        self.assertEqual(response[0], 200, response[1])

    def open_windows(self):
        response = self.admin.settings({'scoringOpen': True, 'votingOpen': True, 'minJudges': 1})
        self.assertEqual(response[0], 200, response[1])

    def records(self):
        with self.service.db() as db:
            return {table: [tuple(row) for row in db.execute('SELECT * FROM ' + table + ' ORDER BY rowid')]
                    for table in ('scores', 'votes', 'score_write_revisions', 'audit')}

    def test_ineligible_teams_leave_public_lists_and_top_twelve_backfills(self):
        teams = [self.team(number=i + 1, name='Synthetic Team ' + str(i + 1)) for i in range(14)]
        judge, _ = self.invitation('judge', 'Synthetic Judge')
        audience, _ = self.invitation('audience', 'Synthetic Audience')
        self.open_windows()
        for index, team_id in enumerate(teams):
            response = judge.score({'teamId': team_id, 'scores': [20 - index, 20, 20, 15, 15, 10], 'status': 'submitted'})
            self.assertEqual(response[0], 200, response[1])
        self.set_status(teams[0], 'disqualified')
        self.set_status(teams[1], 'withdrawn')
        board = self.public.get('/api/board')[1]
        self.assertEqual([team['id'] for team in board['teams']], teams[2:])
        self.assertEqual([team['rank'] for team in board['teams']], list(range(1, 13)))
        self.assertEqual({team['id'] for team in judge.get('/api/judge')[1]['teams']}, set(teams[2:]))
        self.assertEqual({team['id'] for team in audience.get('/api/audience')[1]['teams']}, set(teams[2:]))
        # Historical data remains visible to the organizer and survives reinstatement.
        admin_teams = self.admin.get('/api/admin')[1]['teams']
        self.assertEqual(len(admin_teams), 14)
        self.assertEqual(next(team['score'] for team in admin_teams if team['id'] == teams[0]), 100)
        with self.service.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM scores').fetchone()[0], 14)
        self.set_status(teams[0], 'active')
        self.assertEqual(self.public.get('/api/board')[1]['teams'][0]['id'], teams[0])

    def test_ineligible_score_and_vote_writes_do_not_change_history(self):
        team_id = self.team()
        judge, _ = self.invitation('judge', 'Synthetic Judge')
        audience, _ = self.invitation('audience', 'Synthetic Audience')
        self.open_windows()
        payload = {'teamId': team_id, 'scores': [20, 20, 20, 15, 15, 10], 'status': 'submitted'}
        self.assertEqual(judge.score(payload)[0], 200)
        self.assertEqual(audience.post('/api/vote', {'teamId': team_id})[0], 200)
        for status in ('withdrawn', 'disqualified'):
            with self.subTest(status=status):
                self.set_status(team_id, status)
                before = self.records()
                for response in (judge.score({**payload, 'scores': [0] * 6}), audience.post('/api/vote', {'teamId': team_id})):
                    self.assertEqual((response[0], response[1].get('code')), (409, 'team_ineligible'))
                self.assertEqual(self.records(), before)
        self.set_status(team_id, 'active')
        self.assertEqual(judge.score({**payload, 'scores': [19, 20, 20, 15, 15, 10]})[0], 200)
        self.assertTrue(audience.post('/api/vote', {'teamId': team_id})[1]['alreadyVoted'])
        with self.service.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM votes').fetchone()[0], 1)

    def test_legacy_team_without_extra_metadata_remains_eligible(self):
        team_id = self.team()
        with self.service.db() as db:
            db.execute('DELETE FROM team_details WHERE team_id=?', (team_id,))
        judge, _ = self.invitation('judge', 'Synthetic Judge')
        audience, _ = self.invitation('audience', 'Synthetic Audience')
        self.open_windows()
        self.assertEqual(judge.score({'teamId': team_id, 'scores': [20, 20, 20, 15, 15, 10], 'status': 'submitted'})[0], 200)
        self.assertEqual(audience.post('/api/vote', {'teamId': team_id})[0], 200)
        self.assertEqual(self.public.get('/api/board')[1]['teams'][0]['id'], team_id)


if __name__ == '__main__':
    unittest.main()
