"""Finals HTTP regressions; every account and score lives in a temporary database."""
from concurrent.futures import ThreadPoolExecutor
import json
import unittest

from finals import DIMENSIONS, MAX_REVISION
import test_api as fixtures


class FinalsTests(unittest.TestCase):
    setUp = fixtures.APITests.setUp
    tearDown = fixtures.APITests.tearDown
    team = fixtures.APITests.team
    invitation = fixtures.APITests.invitation

    def request(self, client, method, path, body=None, marker=None):
        if marker is None:
            marker = client.get('/api/session')[1].get('loginId', '')
        return client.request(method, path, body, headers={'X-GOTC-Login': marker})

    def admin_view(self):
        response = self.request(self.admin, 'GET', '/api/admin/finals')
        self.assertEqual(response[0], 200, response[1])
        return response[1]

    def settings(self, opened, published=False):
        return self.request(self.admin, 'POST', '/api/admin/finals/settings', {
            'expectedRevision': self.admin_view()['config']['revision'], 'open': opened, 'published': published})

    def configure(self, count=1):
        self.teams = [self.team(n, f'Team {n}') for n in range(1, count + 1)]
        start = self.admin_view()
        status, created, _ = self.request(self.admin, 'POST', '/api/admin/finals/judges', {'expectedRevision': start['config']['revision']})
        self.assertEqual(status, 200, created)
        self.judges = []
        self.credentials = created['credentials']
        for credential in self.credentials:
            client = fixtures.Client(self.server)
            status, response, _ = client.post('/api/login', {'role': 'judge', 'code': credential['code']})
            self.assertEqual(status, 200, response)
            self.judges.append(client)
        status, data, _ = self.request(self.admin, 'POST', '/api/admin/finals/roster', {
            'expectedRevision': created['config']['revision'], 'teamIds': self.teams,
            'judgeIds': [c['id'] for c in self.credentials]})
        self.assertEqual(status, 200, data)
        self.run_id = data['config']['runId']
        self.assertEqual(self.settings(True)[0], 200)

    def score(self, judge, team=None, revision=0, values=None, status='submitted', **overrides):
        body = {'runId': self.run_id, 'teamId': team or self.teams[0], 'expectedRevision': revision,
                'status': status, 'scores': [20, 15, 20, 20, 15, 10] if values is None else values, 'feedback': ''}
        body.update(overrides)
        return self.request(judge, 'POST', '/api/finals/score', body)

    def ballot(self, judge, choices=None, revision=0, status='submitted', **overrides):
        body = {'runId': self.run_id, 'expectedRevision': revision, 'status': status,
                'nominations': choices if choices is not None else {d['id']: None for d in DIMENSIONS}}
        body.update(overrides)
        return self.request(judge, 'POST', '/api/finals/nominations', body)

    def confirm(self):
        data = self.admin_view()
        return self.request(self.admin, 'POST', '/api/admin/finals/awards', {
            'expectedRevision': data['config']['revision'],
            'selections': {a['id']: a['suggestedWinners'] for a in data['summary']['awards']}})

    def fill(self):
        for judge in self.judges:
            for team in self.teams:
                self.assertEqual(self.score(judge, team)[0], 200)
            self.assertEqual(self.ballot(judge)[0], 200)

    def test_migration_is_private_idempotent_and_public_empty_does_not_leak_roster(self):
        backups = list(self.service.data_dir.glob('before-finals-*.sqlite3'))
        self.assertEqual(len(backups), 1)
        self.service.finals.initialize()
        self.assertEqual(list(self.service.data_dir.glob('before-finals-*.sqlite3')), backups)
        status, public, _ = self.public.get('/api/finals/board')
        self.assertEqual(status, 200)
        self.assertIsNone(public['summary'])
        self.assertNotIn('judgeIds', public['config'])
        self.assertNotIn('teamIds', public['config'])
        self.assertEqual(sum(d['max'] for d in public['dimensions']), 100)
        self.assertEqual(public['config']['awardLimit'], 1)
        self.assertEqual(self.settings(True)[0], 409)

    def test_private_endpoints_enforce_role_login_marker_and_no_suffix_bypass(self):
        self.assertEqual(self.public.get('/api/admin/finals')[0], 401)
        self.assertEqual(self.admin.get('/api/admin/finals')[0], 428)
        self.assertEqual(self.request(self.admin, 'GET', '/api/admin/finals', marker='wrong')[0], 401)
        self.assertEqual(self.request(self.admin, 'GET', '/api/admin/finals/')[0], 404)
        self.assertEqual(self.public.get('/api/finals/board/')[0], 401)
        self.assertEqual(self.public.get('/api/finals/board?x=1')[0], 400)
        self.configure()
        other, _ = self.invitation(name='Unassigned')
        self.assertEqual(self.request(other, 'GET', '/api/finals/judge')[0], 403)
        self.assertEqual(self.request(self.judges[0], 'GET', '/api/admin/finals')[0], 403)
        first_marker = self.judges[0].get('/api/session')[1]['loginId']
        self.assertEqual(self.request(self.judges[1], 'GET', '/api/finals/judge', marker=first_marker)[0], 401)
        self.assertEqual(self.request(self.judges[0], 'GET', '/api/finals/judge/')[0], 404)
        self.assertEqual(self.judges[0].get('/api/finals/judge')[0], 428)

    def test_eight_independent_credentials_no_repeat_and_roster_locks(self):
        self.configure()
        self.assertEqual(len({c['code'] for c in self.credentials}), 8)
        data = self.admin_view()
        self.assertTrue(data['config']['rosterLocked'])
        self.assertEqual(len(data['judges']), 8)
        dumped = json.dumps(data)
        for credential in self.credentials:
            self.assertNotIn(credential['code'], dumped)
        with self.service.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM credential_overrides').fetchone()[0], 8)
            audits = '\n'.join(row[0] for row in db.execute('SELECT metadata FROM audit'))
            for credential in self.credentials:
                self.assertNotIn(credential['code'], audits)
        self.assertEqual(self.request(self.admin, 'POST', '/api/admin/finals/judges', {'expectedRevision': data['config']['revision']})[0], 409)
        self.assertEqual(self.settings(False)[0], 200)
        data = self.admin_view()
        self.assertEqual(self.request(self.admin, 'POST', '/api/admin/finals/roster', {
            'expectedRevision': data['config']['revision'], 'teamIds': self.teams,
            'judgeIds': [c['id'] for c in self.credentials]})[0], 409)

    def test_numeric_scores_are_independent_strict_and_concurrent_safe(self):
        self.configure()
        judge = self.judges[0]
        for values in ([True, 15, 20, 20, 15, 10], [21, 15, 20, 20, 15, 10],
                       [0.01, 15, 20, 20, 15, 10], [-1, 15, 20, 20, 15, 10], [1] * 5):
            self.assertEqual(self.score(judge, values=values)[0], 400)
        self.assertEqual(self.score(judge, revision=True)[0], 400)
        self.assertEqual(self.score(judge, runId='old-run')[0], 409)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.score(judge), range(2)))
        self.assertEqual(sorted(r[0] for r in results), [200, 409])
        self.assertEqual(self.score(self.judges[1], values=[0] * 6)[0], 200)
        data = self.admin_view()
        self.assertEqual(data['summary']['teams'][0]['score'], 50)
        self.assertEqual(data['summary']['teams'][0]['scoreCount'], 2)
        self.assertFalse(data['summary']['complete'])
        mine = self.request(judge, 'GET', '/api/finals/judge')[1]
        self.assertEqual(len(mine['myScores']), 1)
        self.assertEqual(mine['myScores'][0]['scores'], [20, 15, 20, 20, 15, 10])
        self.assertNotIn('summary', mine)
        self.assertEqual(self.score(judge, revision=1, status='draft')[0], 409)
        self.assertEqual(self.score(judge, revision=1, status='recused', values=[])[0], 200)
        self.assertEqual(self.admin_view()['summary']['teams'][0]['recusedCount'], 1)
        with self.service.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM scores').fetchone()[0], 0)
        self.assertEqual(self.settings(False)[0], 200)
        self.assertEqual(self.score(judge, revision=2)[0], 409)

    def test_ballot_drafts_abstentions_ties_and_replacements_do_not_inflate_votes(self):
        self.configure(2)
        a, b = self.teams
        choices = {d['id']: None for d in DIMENSIONS}
        choices['creativity'] = a
        self.assertEqual(self.ballot(self.judges[0], choices, status='draft')[0], 200)
        self.assertEqual(self.admin_view()['summary']['submittedBallots'], 0)
        self.assertEqual(self.ballot(self.judges[0], choices, revision=1)[0], 200)
        self.assertEqual(self.ballot(self.judges[0], choices, revision=1)[0], 409)
        choices['creativity'] = b
        self.assertEqual(self.ballot(self.judges[0], choices, revision=2)[0], 200)
        other = dict(choices, creativity=a)
        self.assertEqual(self.ballot(self.judges[1], other)[0], 200)
        for judge in self.judges[2:]:
            self.assertEqual(self.ballot(judge)[0], 200)
        result = self.admin_view()['summary']
        self.assertEqual(result['submittedBallots'], 8)
        self.assertEqual(result['awards'][0]['maxVotes'], 1)
        self.assertEqual(set(result['awards'][0]['rawLeaders']), {a, b})
        self.assertEqual(result['awards'][0]['winners'], [])
        self.assertEqual(result['awards'][0]['totalVotes'], 2)
        self.assertEqual(result['awards'][1]['winners'], [])
        invalid = dict(choices, social=[a, b])
        self.assertEqual(self.ballot(self.judges[0], invalid, revision=3)[0], 400)
        self.assertEqual(self.ballot(self.judges[0], choices, revision=3, status='draft')[0], 409)

    def test_award_limit_advances_in_fixed_order_and_confirmation_is_required(self):
        self.configure(3)
        a, b, c = self.teams
        for number, judge in enumerate(self.judges):
            for team in self.teams:
                self.assertEqual(self.score(judge, team)[0], 200)
            choices = {d['id']: None for d in DIMENSIONS}
            choices.update(creativity=a if number < 5 else b,
                           technical=a if number < 5 else b,
                           application=a if number < 4 else c)
            self.assertEqual(self.ballot(judge, choices)[0], 200)
        self.assertEqual(self.confirm()[0], 409)
        self.assertEqual(self.settings(False)[0], 200)
        summary = self.admin_view()['summary']
        self.assertTrue(summary['complete'])
        self.assertFalse(summary['awardsConfirmed'])
        self.assertEqual(summary['awards'][0]['suggestedWinners'], [a])
        self.assertEqual(summary['awards'][2]['rawLeaders'], [a])
        self.assertEqual(summary['awards'][2]['suggestedWinners'], [b])
        self.assertEqual(summary['awards'][3]['suggestedWinners'], [c])
        self.assertEqual(self.settings(False, True)[0], 409)
        self.assertEqual(self.confirm()[0], 200)
        self.assertTrue(self.admin_view()['summary']['awardsConfirmed'])
        self.assertEqual(self.settings(False, True)[0], 200)
        public = self.public.get('/api/finals/board')[1]
        self.assertTrue(public['config']['published'])
        self.assertTrue(public['summary']['final'])
        self.assertEqual(public['summary']['awards'][2]['confirmedWinners'], [b])
        self.assertEqual(public['summary']['awards'][2]['winners'], [b])
        self.assertEqual(public['summary']['awards'][2]['rawLeaders'], [a])
        for credential in self.credentials:
            self.assertNotIn(credential['id'], json.dumps(public))
        self.assertEqual(self.settings(True)[0], 200)
        self.assertFalse(self.admin_view()['summary']['awardsConfirmed'])
        self.assertIsNone(self.public.get('/api/finals/board')[1]['summary'])

    def test_confirmations_reject_tampered_selections_and_stale_revisions(self):
        self.configure()
        self.fill()
        self.assertEqual(self.settings(False)[0], 200)
        data = self.admin_view()
        body = {'expectedRevision': data['config']['revision'], 'selections': {d['id']: [] for d in DIMENSIONS}}
        bad = {**body, 'selections': {**body['selections'], 'creativity': self.teams}}
        self.assertEqual(self.request(self.admin, 'POST', '/api/admin/finals/awards', bad)[0], 409)
        self.assertEqual(self.request(self.admin, 'POST', '/api/admin/finals/awards', body)[0], 200)
        self.assertEqual(self.request(self.admin, 'POST', '/api/admin/finals/awards', body)[0], 409)
        summary = self.admin_view()['summary']
        self.assertTrue(all(a['confirmedWinners'] == [] for a in summary['awards']))

    def test_eligibility_and_account_changes_invalidate_publication_and_confirmations(self):
        self.configure(2)
        self.fill()
        self.assertEqual(self.settings(False)[0], 200)
        self.assertEqual(self.confirm()[0], 200)
        self.assertEqual(self.settings(False, True)[0], 200)
        with self.service.db() as db:
            db.execute("UPDATE team_details SET status='disqualified' WHERE team_id=?", (self.teams[0],))
        self.assertIsNone(self.public.get('/api/finals/board')[1]['summary'])
        data = self.admin_view()
        self.assertTrue(data['config']['publicationStale'])
        self.assertFalse(data['summary']['awardsConfirmed'])
        self.assertFalse(next(t for t in data['summary']['teams'] if t['id'] == self.teams[0])['eligible'])
        with self.service.db() as db:
            db.execute("UPDATE team_details SET status='active' WHERE team_id=?", (self.teams[0],))
        # Restoring eligibility does not resurrect an obsolete published snapshot or confirmation.
        self.assertIsNone(self.public.get('/api/finals/board')[1]['summary'])
        self.assertFalse(self.admin_view()['summary']['awardsConfirmed'])
        self.assertEqual(self.confirm()[0], 200)
        self.assertEqual(self.settings(False, True)[0], 200)
        with self.service.db() as db:
            db.execute('UPDATE accounts SET active=0 WHERE id=?', (self.credentials[0]['id'],))
        self.assertIsNone(self.public.get('/api/finals/board')[1]['summary'])
        self.assertFalse(self.admin_view()['summary']['complete'])
        self.assertEqual(self.settings(True)[0], 409)
        with self.service.db() as db:
            db.execute('UPDATE accounts SET active=1 WHERE id=?', (self.credentials[0]['id'],))
        self.assertIsNone(self.public.get('/api/finals/board')[1]['summary'])
        self.assertFalse(self.admin_view()['summary']['awardsConfirmed'])

    def test_vote_cap_and_old_page_write_identity_protection(self):
        self.configure()
        choices = {d['id']: self.teams[0] for d in DIMENSIONS}
        first_marker = self.judges[0].get('/api/session')[1]['loginId']
        body = {'runId': self.run_id, 'expectedRevision': 0, 'status': 'submitted', 'nominations': choices}
        response = self.request(self.judges[1], 'POST', '/api/finals/nominations', body, marker=first_marker)
        self.assertEqual(response[0], 401)
        self.assertEqual(self.admin_view()['summary']['submittedBallots'], 0)
        for judge in self.judges:
            self.assertEqual(self.ballot(judge, choices)[0], 200)
        summary = self.admin_view()['summary']
        self.assertEqual([award['totalVotes'] for award in summary['awards']], [8] * 6)
        self.assertEqual(sum(award['totalVotes'] for award in summary['awards']), 48)
        self.assertEqual(summary['awards'][0]['suggestedWinners'], self.teams)
        self.assertTrue(all(not award['suggestedWinners'] for award in summary['awards'][1:]))
        other, _ = self.invitation(name='Ninth non-final judge')
        self.assertEqual(self.ballot(other, choices)[0], 403)
        self.assertEqual(self.settings(False)[0], 200)
        self.assertEqual(self.ballot(self.judges[0], choices, revision=1)[0], 409)


if __name__ == '__main__':
    unittest.main()
