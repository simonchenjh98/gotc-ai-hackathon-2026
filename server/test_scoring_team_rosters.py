"""The shared team directory feeds candidates, never automatic stage admission."""
from concurrent.futures import ThreadPoolExecutor
import json
import unittest

import test_api as fixtures
import test_finals as finals_fixtures
import test_team_registration as registration_fixtures


class ScoringTeamRosterTests(unittest.TestCase):
    setUp = fixtures.APITests.setUp
    tearDown = fixtures.APITests.tearDown
    team = fixtures.APITests.team
    invitation = fixtures.APITests.invitation
    payload = registration_fixtures.RegistrationTests.payload
    create_registration = registration_fixtures.RegistrationTests.create

    def request(self, client, method, path, body=None, marker=None, **options):
        return client.request(method, path, body,
            headers={'X-GOTC-Login': client.login_id if marker is None else marker}, **options)

    def admin_view(self):
        status, data, _ = self.request(self.admin, 'GET', '/api/admin/roadshow/scoring')
        self.assertEqual(status, 200, data)
        return data

    def stage(self):
        status, data, _ = self.admin.get('/api/admin/judging/roadshow')
        self.assertEqual(status, 200, data)
        return data

    def select(self, team_ids, before=None, **changes):
        before = before or self.admin_view()
        body = {'runId': before['config']['runId'], 'expectedRevision': before['config']['stageRevision'],
                'teamIds': team_ids, **changes}
        return self.request(self.admin, 'POST', '/api/admin/roadshow/teams', body)

    def grants(self):
        with self.service.db() as db:
            return sorted(tuple(row) for row in db.execute('SELECT * FROM judging_grants'))

    def prepare_judges(self, count=3):
        entries = [self.invitation(name='Synthetic roadshow judge ' + str(index)) for index in range(count)]
        self.judges = [row[0] for row in entries]
        self.judge_ids = [row[1]['id'] for row in entries]
        before = self.stage()
        response = self.admin.post('/api/admin/judging/roadshow/roster', {
            'expectedRevision': before['stage']['revision'], 'teamIds': [], 'judgeIds': self.judge_ids})
        self.assertEqual(response[0], 200, response)

    def window(self, opened):
        return self.admin.post('/api/admin/judging/roadshow/window', {
            'expectedRevision': self.stage()['stage']['revision'], 'open': opened, 'deadline': None})

    def test_registration_directory_is_shared_without_automatic_stage_admission(self):
        before = self.admin_view()
        self.assertEqual(before['availableTeams'], [])
        self.assertEqual(before['roster'], {'teamIds': [], 'editable': True})
        _, registered = self.create_registration()
        manually_created = self.team(2, 'Manually added team')
        directory = self.public.get('/api/registration')[1]['teams']
        self.assertEqual([row['id'] for row in directory], [registered['id']])
        after = self.admin_view()
        self.assertEqual({row['id'] for row in after['availableTeams']}, {registered['id'], manually_created})
        self.assertEqual(after['roster']['teamIds'], [])
        self.assertEqual(after['summary']['teams'], [])
        self.assertEqual(after['config']['stageRevision'], before['config']['stageRevision'])
        for row in after['availableTeams']:
            self.assertEqual(set(row), {'id', 'number', 'name', 'project', 'eligible'})
        self.assertNotIn('13900000001', json.dumps(after))
        with self.service.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM team_accounts').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM claim_requests').fetchone()[0], 0)
        self.assertEqual(self.public.get('/api/judging/roadshow/board')[1]['teams'], [])

    def test_selected_teams_reach_judges_without_changing_grants_or_opening_window(self):
        self.prepare_judges()
        first, second = self.team(), self.team(2, 'Not selected')
        grants = self.grants()
        before = self.admin_view()
        status, selected, _ = self.select([first], before)
        self.assertEqual(status, 200, selected)
        self.assertEqual(selected['roster'], {'teamIds': [first], 'editable': True})
        self.assertEqual(selected['config']['stageRevision'], before['config']['stageRevision'] + 1)
        self.assertFalse(selected['config']['open'])
        self.assertEqual(self.grants(), grants)
        self.assertEqual(self.stage()['roster']['judgeIds'], sorted(self.judge_ids))
        judge = self.request(self.judges[0], 'GET', '/api/roadshow/scoring')[1]
        self.assertEqual([row['id'] for row in judge['teams']], [first])
        self.assertIsNone(judge['teams'][0]['materialSnapshot'])
        third = self.team(3, 'Added after selection')
        self.assertEqual({row['id'] for row in self.admin_view()['availableTeams']}, {first, second, third})
        self.assertEqual(self.admin_view()['roster']['teamIds'], [first])
        self.assertEqual(self.request(self.judges[0], 'GET', '/api/roadshow/scoring')[1]['teams'], judge['teams'])
        self.assertEqual(self.select([])[0], 200)
        self.assertEqual(self.grants(), grants)
        self.assertEqual(self.admin_view()['roster']['teamIds'], [])
        self.assertEqual(self.window(True)[0], 409)

    def test_authentication_page_identity_origin_and_exact_route(self):
        team_id = self.team()
        other, _ = self.invitation()
        audience, _ = self.invitation('audience', 'Audience')
        before = self.admin_view()
        body = {'runId': before['config']['runId'], 'expectedRevision': before['config']['stageRevision'], 'teamIds': [team_id]}
        for client, expected in [(self.public, 401), (other, 403), (audience, 403)]:
            self.assertEqual(self.request(client, 'POST', '/api/admin/roadshow/teams', body)[0], expected)
        self.assertEqual(self.request(self.admin, 'POST', '/api/admin/roadshow/teams', body, marker='')[0], 428)
        self.assertEqual(self.request(self.admin, 'POST', '/api/admin/roadshow/teams', body, marker='stale')[0], 401)
        for origin in [False, 'https://other.example']:
            self.assertEqual(self.request(self.admin, 'POST', '/api/admin/roadshow/teams', body, origin=origin)[0], 403)
        self.assertEqual(self.request(self.admin, 'POST', '/api/admin/roadshow/teams/', body)[0], 404)
        self.assertEqual(self.request(self.admin, 'POST', '/api/admin/roadshow/teams?all=1', body)[0], 400)
        self.assertEqual(self.request(self.admin, 'GET', '/api/admin/roadshow/teams')[0], 404)
        self.assertEqual(self.admin_view()['roster']['teamIds'], [])

    def test_invalid_teams_and_attempted_judge_changes_are_atomic(self):
        self.prepare_judges()
        active = self.team()
        withdrawn = self.team(2, 'Withdrawn', status='withdrawn', statusReason='Synthetic withdrawal')
        disqualified = self.team(3, 'Disqualified', status='disqualified', statusReason='Synthetic disqualification')
        before, grants = self.admin_view(), self.grants()
        eligibility = {row['id']: row['eligible'] for row in before['availableTeams']}
        self.assertEqual(eligibility, {active: True, withdrawn: False, disqualified: False})
        for values in [None, True, 'all', [active, active], [True], ['missing'], [active, withdrawn], [disqualified]]:
            with self.subTest(values=values):
                self.assertEqual(self.select(values, before)[0], 400)
        for changes in [{'judgeIds': []}, {'open': True}, {'expectedRevision': True}, {'expectedRevision': 9007199254740992}]:
            with self.subTest(changes=changes):
                self.assertEqual(self.select([active], before, **changes)[0], 400)
        self.assertEqual(self.admin_view()['config']['stageRevision'], before['config']['stageRevision'])
        self.assertEqual(self.admin_view()['roster']['teamIds'], [])
        self.assertEqual(self.grants(), grants)

    def test_run_revision_and_simultaneous_edit_conflicts_preserve_saved_selection(self):
        first, second = self.team(), self.team(2, 'Second')
        before = self.admin_view()
        self.assertEqual(self.select([first], before, runId='old-run')[0], 409)
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda ids: self.select(ids, before), [[first], [second]]))
        self.assertEqual(sorted(row[0] for row in responses), [200, 409])
        winner = next(row[1]['roster']['teamIds'] for row in responses if row[0] == 200)
        self.assertEqual(self.admin_view()['roster']['teamIds'], winner)
        self.assertEqual(self.select([], before)[0], 409)
        stage = self.stage()
        self.assertEqual(self.admin.post('/api/admin/judging/roadshow/reset', {
            'expectedRevision': stage['stage']['revision'], 'reason': 'Synthetic new run'})[0], 200)
        self.assertEqual(self.select([first], runId=before['config']['runId'])[0], 409)
        self.assertEqual(self.admin_view()['roster']['teamIds'], winner)

    def test_generic_roster_changes_invalidate_team_editor_and_preserve_latest_grants(self):
        self.prepare_judges(1)
        team_id = self.team()
        before = self.admin_view()
        _, extra = self.invitation(name='New roadshow judge')
        stage = self.stage()
        self.assertEqual(self.admin.post('/api/admin/judging/roadshow/roster', {
            'expectedRevision': stage['stage']['revision'], 'teamIds': [],
            'judgeIds': self.judge_ids + [extra['id']]})[0], 200)
        self.assertEqual(self.select([team_id], before)[0], 409)
        latest_grants = self.grants()
        self.assertEqual(self.select([team_id])[0], 200)
        self.assertEqual(self.grants(), latest_grants)

    def test_open_then_closed_roster_remains_frozen(self):
        self.prepare_judges()
        selected, outside = self.team(), self.team(2, 'Outside')
        self.assertEqual(self.select([selected])[0], 200)
        self.assertEqual(self.window(True)[0], 200)
        grants = self.grants()
        self.assertFalse(self.admin_view()['roster']['editable'])
        self.assertEqual(self.select([outside])[0], 409)
        self.assertEqual(self.window(False)[0], 200)
        self.assertFalse(self.admin_view()['roster']['editable'])
        self.assertEqual(self.select([])[0], 409)
        self.assertEqual(self.admin_view()['roster']['teamIds'], [selected])
        self.assertEqual(self.grants(), grants)

    def test_current_reviews_or_numeric_scores_lock_roster_even_without_frozen_timestamp(self):
        self.prepare_judges(1)
        team_id = self.team()
        self.assertEqual(self.select([team_id])[0], 200)
        for kind in ['qualitative', 'numeric']:
            with self.subTest(kind=kind):
                stage = self.stage()
                run_id = stage['stage']['runId']
                with self.service.db() as db:
                    if kind == 'qualitative':
                        db.execute('INSERT INTO judging_reviews VALUES (?,?,?,?,?,?,?,?,?)',
                            (run_id, self.judge_ids[0], team_id, 1, 'Saved draft', None, None, 0, 1))
                    else:
                        db.execute('INSERT INTO roadshow_scores VALUES (?,?,?,?,?,?,?,?,?)',
                            (run_id, self.judge_ids[0], team_id, 1, 'draft', '[0,0,0,0,0,0]', 0, '', 1))
                self.assertFalse(self.admin_view()['roster']['editable'])
                self.assertEqual(self.select([])[0], 409)
                self.assertEqual(self.admin_view()['roster']['teamIds'], [team_id])
                self.assertEqual(self.admin.post('/api/admin/judging/roadshow/reset', {
                    'expectedRevision': stage['stage']['revision'], 'reason': 'Preserve prior synthetic records'})[0], 200)
        with self.service.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM judging_reviews').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM roadshow_scores').fetchone()[0], 1)

    def test_finals_judge_only_sees_explicit_shortlist_even_after_directory_grows(self):
        selected = self.team()
        outside = self.team(2, 'Not a finalist')
        admin = self.request(self.admin, 'GET', '/api/admin/finals')[1]
        status, created, _ = self.request(self.admin, 'POST', '/api/admin/finals/judges', {
            'expectedRevision': admin['config']['revision']})
        self.assertEqual(status, 200, created)
        headers = {'X-GOTC-Finals-Judge': created['judges'][0]['id']}
        self.assertEqual(self.public.request('GET', '/api/finals/judge', headers=headers)[1]['teams'], [])
        status, saved, _ = self.request(self.admin, 'POST', '/api/admin/finals/roster', {
            'expectedRevision': created['config']['revision'], 'teamIds': [selected],
            'judgeIds': [judge['id'] for judge in created['judges']]})
        self.assertEqual(status, 200, saved)
        _, registered = self.create_registration(name='Late registered team')
        admin = self.request(self.admin, 'GET', '/api/admin/finals')[1]
        self.assertEqual({row['id'] for row in admin['availableTeams']}, {selected, outside, registered['id']})
        self.assertEqual(admin['config']['teamIds'], [selected])
        judge = self.public.request('GET', '/api/finals/judge', headers=headers)[1]
        self.assertEqual([row['id'] for row in judge['teams']], [selected])
        self.assertEqual(self.request(self.admin, 'POST', '/api/admin/finals/settings', {
            'expectedRevision': admin['config']['revision'], 'open': True, 'published': False})[0], 200)
        response = self.public.request('POST', '/api/finals/score', {
            'runId': judge['config']['runId'], 'teamId': outside, 'expectedRevision': 0,
            'status': 'submitted', 'scores': [0] * 6, 'feedback': ''}, headers=headers)
        self.assertEqual(response[0], 403, response)
        choices = {dimension['id']: None for dimension in finals_fixtures.DIMENSIONS}
        choices['creativity'] = outside
        response = self.public.request('POST', '/api/finals/nominations', {
            'runId': judge['config']['runId'], 'expectedRevision': 0, 'status': 'submitted',
            'nominations': choices}, headers=headers)
        self.assertEqual(response[0], 400, response)
        self.assertEqual(self.admin_view()['roster']['teamIds'], [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
