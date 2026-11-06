"""Malformed HTTP payloads and direct administrator edits use disposable event data."""
import json
import unittest

from app import text_field
from errors import APIError
import test_api as fixtures


class BackendInputValidationTests(unittest.TestCase):
    setUp = fixtures.APITests.setUp
    tearDown = fixtures.APITests.tearDown
    team = fixtures.APITests.team
    invitation = fixtures.APITests.invitation

    def snapshot(self):
        with self.service.db() as db:
            return {table: [tuple(row) for row in db.execute('SELECT * FROM ' + table + ' ORDER BY rowid')]
                    for table in ('teams', 'team_details', 'scores', 'score_write_revisions', 'audit')}

    def current_team(self, team_id):
        return next(team for team in self.admin.get('/api/admin')[1]['teams'] if team['id'] == team_id)

    def test_existing_admin_team_edits_require_a_revision_and_cannot_restore_stale_eligibility(self):
        team_id = self.team(name='Original team')
        original = self.current_team(team_id)
        status, changed, _ = self.admin.post('/api/admin/teams', {
            'id': team_id, 'expectedRevision': original['revision'], 'name': 'Organizer correction',
            'status': 'disqualified', 'statusReason': 'Synthetic eligibility decision'})
        self.assertEqual(status, 200)
        before = self.snapshot()
        stale = {'id': team_id, 'name': original['name'], 'status': 'active', 'statusReason': ''}
        for extra, expected in [({}, 428), ({'expectedRevision': None}, 428),
                                ({'expectedRevision': original['revision']}, 409),
                                ({'expectedRevision': True}, 400), ({'expectedRevision': []}, 400)]:
            with self.subTest(extra=extra):
                self.assertEqual(self.admin.post('/api/admin/teams', {**stale, **extra})[0], expected)
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(self.current_team(team_id)['status'], 'disqualified')
        self.assertEqual(self.admin.post('/api/admin/teams', {
            **stale, 'expectedRevision': changed['team']['revision']})[0], 200)
        self.assertEqual(self.current_team(team_id)['status'], 'active')
        self.assertEqual(self.admin.post('/api/admin/teams', {'number': 2, 'name': 'New without revision'})[0], 200)

    def test_non_string_score_status_and_invitation_role_are_client_errors_without_writes(self):
        team_id = self.team()
        judge, _ = self.invitation('judge', 'Synthetic judge')
        self.assertEqual(self.admin.settings({'scoringOpen': True})[0], 200)
        before = self.snapshot()
        for value in ([], {}, None, True, 7):
            with self.subTest(value=value):
                self.assertEqual(judge.post('/api/score', {
                    'teamId': team_id, 'expectedRevision': 0, 'status': value, 'scores': [0] * 6})[0], 400)
                self.assertEqual(self.admin.post('/api/admin/invitations', {'role': value, 'name': 'Bad request'})[0], 400)
                self.assertEqual(self.snapshot(), before)

    def test_unpaired_unicode_is_rejected_in_all_json_keys_values_and_nested_arrays(self):
        before = self.snapshot()
        samples = [
            (self.public, '/api/login', {'role': 'team', 'code': '\ud800'}),
            (self.public, '/api/login', {'role': 'team', 'code': 'valid', 'accountId': '\udfff'}),
            (self.admin, '/api/admin/teams', {'number': 999, 'name': 'A\ud83dB'}),
            (self.admin, '/api/admin/data/import/preview', {'kind': 'teams', 'csv': 'number,name\n999,\ud800\n'}),
            (self.public, '/api/login', {'role': 'team', 'code': 'valid', '\ud800': 'value'}),
            (self.public, '/api/login', {'role': 'team', 'code': 'valid', 'extra': [{'nested': ['\udfff']}]}),
        ]
        for client, path, body in samples:
            with self.subTest(path=path, body=repr(body)):
                status, _, _ = client.request('POST', path, raw=json.dumps(body).encode())
                self.assertEqual(status, 400)
                self.assertEqual(self.snapshot(), before)

    def test_valid_emoji_surrogate_pairs_and_literal_utf8_remain_supported(self):
        for number, raw in [(1, b'{"number":1,"name":"Team \\ud83d\\ude80"}'),
                            (2, json.dumps({'number': 2, 'name': 'Team \U0001f680'}, ensure_ascii=False).encode())]:
            with self.subTest(number=number):
                status, result, _ = self.admin.request('POST', '/api/admin/teams', raw=raw)
                self.assertEqual(status, 200)
                self.assertEqual(result['team']['name'], 'Team \U0001f680')
        # A syntactically valid but wrong credential remains an ordinary authentication failure.
        self.assertEqual(self.public.request('POST', '/api/login', raw=b'{"role":"team","code":"\\ud83d\\ude80"}')[0], 401)

    def test_shared_text_validator_rejects_lone_surrogates_before_internal_persistence(self):
        for value in ('\ud800', 'prefix\udfff', '\ud83dX\ude80'):
            with self.subTest(value=repr(value)):
                with self.assertRaises(APIError) as raised:
                    text_field(value, 'Test field', 100)
                self.assertEqual(raised.exception.status, 400)
        self.assertEqual(text_field(json.loads('"\\ud83d\\ude80"'), 'Test field', 100), '\U0001f680')

    def test_invalid_percent_encoded_utf8_paths_are_client_errors_and_do_not_affect_health(self):
        before = self.snapshot()
        for path in ('/api/%FF', '/api/%ED%A0%80'):
            with self.subTest(path=path):
                self.assertEqual(self.public.get(path)[0], 400)
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(self.public.get('/api/session')[0], 200)

    def test_deep_json_is_rejected_without_writes_and_next_request_still_succeeds(self):
        before = self.snapshot()
        nested = b'{"x":' + b'[' * 5000 + b'0' + b']' * 5000 + b'}'
        self.assertLess(len(nested), 16384)
        self.assertEqual(self.public.request('POST', '/api/login', raw=nested)[0], 400)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.public.get('/api/session')[0], 200)


if __name__ == '__main__':
    unittest.main()
