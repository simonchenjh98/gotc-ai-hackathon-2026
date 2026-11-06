"""Old browser tabs must not write votes or stage reviews as a replacement login."""
import unittest
import test_api as api_fixtures
import test_judging as stage_fixtures


class PageSessionOwnershipTests(unittest.TestCase):
    setUp = api_fixtures.APITests.setUp
    tearDown = api_fixtures.APITests.tearDown
    team = api_fixtures.APITests.team
    invitation = api_fixtures.APITests.invitation
    admin_stage = stage_fixtures.ExhibitionFrameworkTests.admin_stage
    configure = stage_fixtures.ExhibitionFrameworkTests.configure
    window = stage_fixtures.ExhibitionFrameworkTests.window

    def test_audience_reads_and_votes_require_the_original_page_login(self):
        team = self.team()
        first, _ = self.invitation('audience', 'First audience')
        second, _ = self.invitation('audience', 'Second audience')
        self.assertEqual(self.admin.settings({'votingOpen': True})[0], 200)
        for method, path, body in [('GET', '/api/audience', None), ('POST', '/api/vote', {'teamId': team})]:
            self.assertEqual(first.request(method, path, body, headers={'X-GOTC-Login': ''})[0], 428)
            self.assertEqual(second.request(method, path, body, headers={'X-GOTC-Login': first.login_id})[0], 401)
        self.assertEqual(second.get('/api/audience')[1]['votedTeamIds'], [])
        first_cookie = first.cookie
        first.cookie = second.cookie  # A tab retains its page marker while the browser cookie changes.
        self.assertEqual(first.post('/api/vote', {'teamId': team})[0], 401)
        first.cookie = first_cookie
        self.assertEqual(first.post('/api/vote', {'teamId': team})[0], 200)
        self.assertEqual(first.get('/api/audience')[1]['votedTeamIds'], [team])
        self.assertEqual(second.get('/api/audience')[1]['votedTeamIds'], [])

    def test_both_stage_reads_and_writes_reject_missing_or_replaced_login(self):
        team = self.team()
        first, first_invite = self.invitation('judge', 'First judge')
        second, second_invite = self.invitation('judge', 'Second judge')
        for stage in ('exhibition', 'roadshow'):
            with self.subTest(stage=stage):
                self.stage_path = '/api/admin/judging/' + stage
                self.judge_path = '/api/judging/' + stage
                self.configure([team], [first_invite['id'], second_invite['id']])
                self.window()
                page = first.get(self.judge_path)[1]
                body = {'runId': page['stage']['runId'], 'teamId': team, 'expectedVersion': 0,
                        'status': 'submitted', 'feedback': 'First judge input', 'scores': None}
                for method, path, payload in [('GET', self.judge_path, None), ('POST', self.judge_path + '/review', body)]:
                    self.assertEqual(first.request(method, path, payload, headers={'X-GOTC-Login': ''})[0], 428)
                    self.assertEqual(second.request(method, path, payload, headers={'X-GOTC-Login': first.login_id})[0], 401)
                self.assertEqual(second.get(self.judge_path)[1]['myReviews'], [])
                self.assertEqual(first.post(self.judge_path + '/review', body)[0], 200)
                self.assertEqual(len(first.get(self.judge_path)[1]['myReviews']), 1)
                self.assertEqual(second.get(self.judge_path)[1]['myReviews'], [])
                self.assertEqual(second.post(self.judge_path + '/review', {**body, 'feedback': 'Second judge input'})[0], 200)

    def test_stage_read_trailing_slash_aliases_enforce_page_identity(self):
        team = self.team()
        first, first_invite = self.invitation('judge', 'First judge')
        second, second_invite = self.invitation('judge', 'Second judge')
        for stage in ('exhibition', 'roadshow'):
            with self.subTest(stage=stage):
                self.stage_path = '/api/admin/judging/' + stage
                self.judge_path = '/api/judging/' + stage
                self.configure([team], [first_invite['id'], second_invite['id']])
                self.window()
                page = second.get(self.judge_path)[1]
                body = {'runId': page['stage']['runId'], 'teamId': team, 'expectedVersion': 0,
                        'status': 'submitted', 'feedback': 'Second judge private input', 'scores': None}
                self.assertEqual(second.post(self.judge_path + '/review', body)[0], 200)
                alias = self.judge_path + '/'
                for marker, expected_status in [('', 428), (first.login_id, 401)]:
                    status, response, _ = second.request('GET', alias, headers={'X-GOTC-Login': marker})
                    self.assertEqual(status, expected_status)
                    self.assertNotIn('myReviews', response)
                status, response, _ = second.request('GET', alias, headers={'X-GOTC-Login': second.login_id})
                self.assertEqual(status, 200)
                self.assertEqual(response['myReviews'], second.get(self.judge_path)[1]['myReviews'])
                # Only the existing single trailing slash read alias is accepted.
                for path in (self.judge_path + '//', self.judge_path + '/review/', self.judge_path + '/board/'):
                    self.assertEqual(second.request('GET', path, headers={'X-GOTC-Login': ''})[0], 404)
                self.assertEqual(second.request('POST', self.judge_path + '/review/', body,
                                                headers={'X-GOTC-Login': ''})[0], 404)

    def test_public_progress_and_wrong_role_behaviour_remain_unchanged(self):
        audience, _ = self.invitation('audience', 'Audience')
        judge, _ = self.invitation('judge', 'Judge')
        for stage in ('exhibition', 'roadshow'):
            self.assertEqual(self.public.get('/api/judging/' + stage + '/board')[0], 200)
            self.assertEqual(self.public.get('/api/judging/' + stage)[0], 401)
            self.assertEqual(audience.get('/api/judging/' + stage)[0], 403)
        self.assertEqual(judge.get('/api/audience')[0], 403)


if __name__ == '__main__':
    unittest.main()
