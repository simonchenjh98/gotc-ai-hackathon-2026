"""Original numeric scoring remains independent from the additive review framework."""
import csv
import io
import json
import unittest

import test_api as api_helpers
import test_submissions as submission_helpers


class LegacyScoringCompatibilityTests(unittest.TestCase):
    setUp = api_helpers.APITests.setUp
    tearDown = api_helpers.APITests.tearDown
    team = api_helpers.APITests.team
    invitation = api_helpers.APITests.invitation
    access = submission_helpers.SubmissionAPITests.access
    materials = submission_helpers.SubmissionAPITests.materials

    def stage(self):
        status, data, _ = self.admin.get('/api/admin/judging/exhibition')
        self.assertEqual(status, 200, data)
        return data

    def open_framework(self, teams, judges):
        current = self.stage()
        status, data, _ = self.admin.post('/api/admin/judging/exhibition/roster', {
            'expectedRevision': current['stage']['revision'], 'teamIds': teams, 'judgeIds': judges})
        self.assertEqual(status, 200, data)
        status, data, _ = self.admin.post('/api/admin/judging/exhibition/window', {
            'expectedRevision': data['stage']['revision'], 'open': True, 'deadline': None})
        self.assertEqual(status, 200, data)
        return data['stage']['runId']

    def numeric(self, judge, team, total, status='submitted'):
        values = [total * part / 100 for part in (20, 20, 20, 15, 15, 10)]
        return judge.score({
            'teamId': team, 'scores': values, 'status': status, 'feedback': 'LEGACY_NUMERIC_FEEDBACK'})

    def test_numeric_average_ties_and_windows_do_not_mix_with_framework_records(self):
        teams = [self.team(i + 1, 'Numeric team ' + str(i + 1)) for i in range(3)]
        invited = [self.invitation(name='Numeric judge ' + str(i + 1)) for i in range(5)]
        judges = [item[0] for item in invited]
        self.assertEqual(self.admin.settings({
            'scoringOpen': True, 'minJudges': 5, 'resultsPublished': True})[0], 200)
        run = self.open_framework([teams[0]], [invited[0][1]['id']])
        for index, judge in enumerate(judges):
            for team, total in zip(teams, [100 - index * 10, 80, 70]):
                self.assertEqual(self.numeric(judge, team, total)[0], 200)
            if index == 3:
                board = self.public.get('/api/board')[1]
                self.assertTrue(all(row['judgeCount'] == 4 and row['score'] is None for row in board['teams']))
        board = self.public.get('/api/board')[1]
        self.assertEqual([row['score'] for row in board['teams']], [80, 80, 70])
        self.assertEqual([row['rank'] for row in board['teams']], [1, 1, 3])
        framework = self.public.get('/api/judging/exhibition/board')[1]
        self.assertEqual(framework['teams'][0]['reviewCount'], 0)
        self.assertIsNone(framework['teams'][0]['score'])

        status, data, _ = judges[0].post('/api/judging/exhibition/review', {
            'runId': run, 'teamId': teams[0], 'expectedVersion': 0, 'status': 'submitted',
            'feedback': 'FRAMEWORK_ONLY_FEEDBACK', 'scores': None})
        self.assertEqual(status, 200, data)
        self.assertEqual(self.public.get('/api/board')[1]['teams'], board['teams'])
        self.assertEqual(self.public.get('/api/judging/exhibition/board')[1]['teams'][0]['reviewCount'], 1)
        self.assertEqual(judges[0].post('/api/judging/exhibition/review', {
            'runId': run, 'teamId': teams[0], 'expectedVersion': 1, 'status': 'submitted',
            'feedback': 'Not a numeric endpoint', 'scores': [20, 20, 20, 15, 15, 10]})[0], 409)

        self.assertEqual(self.numeric(judges[0], teams[0], 0, 'draft')[0], 409)
        self.assertEqual(self.admin.settings({'scoringOpen': False})[0], 200)
        self.assertEqual(self.numeric(judges[0], teams[0], 90)[0], 409)
        self.assertTrue(judges[0].get('/api/judging/exhibition')[1]['window']['canEdit'])
        self.assertEqual(judges[0].post('/api/judging/exhibition/review', {
            'runId': run, 'teamId': teams[0], 'expectedVersion': 1, 'status': 'draft',
            'feedback': 'PRIVATE_FRAMEWORK_DRAFT', 'scores': None})[0], 200)
        current = self.stage()
        self.assertEqual(self.admin.post('/api/admin/judging/exhibition/window', {
            'expectedRevision': current['stage']['revision'], 'open': False, 'deadline': None})[0], 200)
        self.assertEqual(self.admin.settings({'scoringOpen': True})[0], 200)
        self.assertEqual(self.numeric(judges[4], teams[0], 50)[0], 200)
        self.assertEqual(self.public.get('/api/board')[1]['teams'][0]['score'], 78)
        self.assertFalse(judges[0].get('/api/judging/exhibition')[1]['window']['canEdit'])
        self.assertEqual(self.admin.settings({'resultsPublished': False})[0], 200)
        self.assertTrue(all(row['score'] is None and row['rank'] is None for row in self.public.get('/api/board')[1]['teams']))
        self.assertEqual(self.admin.get('/api/admin')[1]['teams'][0]['score'], 78)
        self.assertEqual(self.public.get('/api/judging/exhibition/board')[1]['teams'][0]['reviewCount'], 1)
        exported = self.admin.get('/api/admin/export')
        self.assertEqual(exported[0], 200)
        rows = list(csv.reader(io.StringIO(exported[1].decode('utf-8-sig'))))
        self.assertEqual(len(rows) - 1, 15)
        self.assertIn('总分', rows[0])
        self.assertNotIn('FRAMEWORK_ONLY_FEEDBACK', exported[1].decode('utf-8-sig'))
        self.assertNotIn('PRIVATE_FRAMEWORK_DRAFT', exported[1].decode('utf-8-sig'))

    def test_original_judge_reads_latest_formal_material_while_framework_keeps_snapshot(self):
        team = self.team()
        team_client, _ = self.access(team)
        judge, invitation = self.invitation()
        audience, _ = self.invitation('audience', 'Compatibility audience')
        self.assertEqual(self.admin.settings({
            'submissionsOpen': True, 'scoringOpen': True, 'minJudges': 1})[0], 200)
        formal = self.materials(projectName='FORMAL_ORIGINAL', summary='PRIVATE_FORMAL_BODY')
        self.assertEqual(team_client.post('/api/submission', {
            'expectedVersion': 0, 'status': 'submitted', 'materials': formal})[0], 200)
        run = self.open_framework([team], [invitation['id']])
        self.assertEqual(team_client.post('/api/submission', {
            'expectedVersion': 1, 'status': 'draft',
            'materials': self.materials(projectName='PRIVATE_LATER_DRAFT')})[0], 200)
        status, original, _ = judge.get('/api/judge')
        self.assertEqual(status, 200)
        self.assertEqual([dim['max'] for dim in original['dimensions']], [20, 20, 20, 15, 15, 10])
        self.assertEqual(original['submissions'][0]['materials']['projectName'], 'FORMAL_ORIGINAL')
        self.assertNotIn('PRIVATE_LATER_DRAFT', json.dumps(original))
        self.assertEqual(self.numeric(judge, team, 90)[0], 200)
        self.assertEqual(team_client.post('/api/submission', {
            'expectedVersion': 2, 'status': 'submitted',
            'materials': self.materials(projectName='FORMAL_UPDATED')})[0], 200)
        original = judge.get('/api/judge')[1]
        self.assertEqual(original['submissions'][0]['materials']['projectName'], 'FORMAL_UPDATED')
        self.assertEqual(original['myScores'][0]['scores'], [18, 18, 18, 13.5, 13.5, 9])
        framed = judge.get('/api/judging/exhibition')[1]
        self.assertEqual(framed['stage']['runId'], run)
        self.assertEqual(framed['teams'][0]['materialSnapshot']['materials']['projectName'], 'FORMAL_ORIGINAL')
        self.assertEqual(framed['myReviews'], [])
        for client, expected in [(self.public, 401), (self.admin, 403), (audience, 403), (team_client, 403)]:
            self.assertEqual(client.get('/api/judge')[0], expected)
            self.assertEqual(client.score({
                'teamId': team, 'scores': [0] * 6, 'status': 'submitted'})[0], expected)
        config = self.admin.get('/api/admin')[1]['config']
        if 'votingOpen' in config:
            self.assertEqual(self.admin.settings({
                'votingOpen': True, 'votingDeadline': None, 'scoringOpen': False})[0], 200)
            self.assertFalse(judge.get('/api/judge')[1]['scoringOpen'])
        self.assertEqual(audience.post('/api/vote', {'teamId': team})[0], 200)
        board = self.public.get('/api/board')[1]
        self.assertEqual(board['teams'][0]['score'], 90)
        self.assertEqual(board['teams'][0]['audienceVotes'], 1)
        self.assertNotIn('PRIVATE_FORMAL_BODY', json.dumps(board))
        self.assertEqual(judge.get('/api/admin/export')[0], 403)
        with self.service.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM scores').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM judging_reviews').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
