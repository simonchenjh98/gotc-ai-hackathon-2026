"""Cross-module roster invariants using synthetic people and temporary databases."""
from test_registration_fixtures import registration_fixture_create, registration_fixture_open, registration_fixture_save
import json
import secrets
import sqlite3
import unittest
from unittest.mock import patch

from claims import normalize
from team_registration import FIELDS
import test_claims
import test_team_registration


class RegistrationClaimConsistencyTests(unittest.TestCase):
    setUp = test_team_registration.RegistrationTests.setUp
    tearDown = test_team_registration.RegistrationTests.tearDown
    payload = test_team_registration.RegistrationTests.payload
    create = test_team_registration.RegistrationTests.create
    admin_call = test_claims.ClaimsTests.admin_call
    roster = test_claims.ClaimsTests.roster
    claim = test_claims.ClaimsTests.claim
    approval = test_claims.ClaimsTests.approval

    def fixture(self, names=None, captain='Captain'):
        names = names or [('Captain', 'Fixture School'), ('Student A', 'Fixture School'),
                          ('Student B', 'Fixture School'), ('Student C', 'Fixture School')]
        people = [{'id': 'p' + str(i), 'name': name, 'school': school, 'notes': '', 'needsReview': False}
                  for i, (name, school) in enumerate(names)]
        self.roster(people)
        code, team = self.create()
        request = self.claim(captain, 'Fixture School')
        captain_id = next(p['id'] for p in people if p['name'] == captain)
        body = self.approval(request, team['id'], captainId=captain_id,
                             memberIds=[p['id'] for p in people], resolvedIdentity=True)
        return code, team, request, body

    def open_team(self, code):
        status, result, _ = registration_fixture_open(self, {'code': code})
        self.assertEqual(status, 200, result)
        return result['team']

    def save_team(self, code, current, **changes):
        return registration_fixture_save(self, {
            'code': code, 'expectedRevision': current['revision'],
            'team': {**{key: current[key] for key in FIELDS}, **changes}})

    def test_approval_syncs_first_claim_to_existing_registration_without_rotating_management_code(self):
        code, original, request, body = self.fixture()
        self.assertEqual(self.admin_call('/review', body)[0], 200)
        current = self.open_team(code)
        self.assertEqual(current['id'], original['id'])
        self.assertEqual(current['number'], original['number'])
        self.assertEqual(current['members'], ['Captain', 'Student A', 'Student B', 'Student C'])
        self.assertEqual(current['targetSize'], 4)
        self.assertEqual(current['phone'], '13900000001')
        self.assertGreater(current['revision'], original['revision'])
        self.assertEqual(registration_fixture_save(self, {
            'code': code, 'expectedRevision': original['revision'], 'team': self.payload()})[0], 409)
        self.assertEqual(self.save_team(code, current, summary='Verified team introduction')[0], 200)
        current = self.open_team(code)
        self.assertEqual(self.save_team(code, current, members=['Captain', 'Student A', 'New Person', 'Student C'])[0], 409)
        self.assertEqual(self.admin_call('/review', body)[0], 200)
        self.assertEqual(self.open_team(code)['revision'], current['revision'])
        with self.service.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM teams').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM team_accounts').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM claim_memberships').fetchone()[0], 4)

    def test_same_name_different_school_members_survive_contact_and_project_edits(self):
        code, original, request, body = self.fixture([
            ('Captain', 'Fixture School'), ('Student A', 'School A'), ('Student A', 'School B')])
        self.assertEqual(self.admin_call('/review', body)[0], 200)
        current = self.open_team(code)
        self.assertEqual(current['members'], ['Captain', 'Student A', 'Student A'])
        # Another registration can contain a homonym; verified membership is authoritative.
        with self.service.db() as db:
            db.execute("INSERT INTO teams(id,number,name,project,track,updated_at) VALUES ('extra',2,'Other team','','',1)")
            db.execute("INSERT INTO team_registrations(team_id,captain,school,phone,target_size,members_json,secret_hash,created_at) VALUES ('extra','Other Captain','Fixture School','',3,?, 'different-secret',1)",
                       (json.dumps(['Other Captain', 'Student A']),))
        status, result, _ = self.save_team(code, current, phone='13900000002', project='Synthetic project')
        self.assertEqual(status, 200, result)
        updated = self.open_team(code)
        self.assertEqual(updated['members'], current['members'])
        self.assertEqual(updated['phone'], '13900000002')
        self.assertEqual(self.save_team(code, updated, targetSize=4)[0], 409)

    def test_changed_verified_captain_does_not_inherit_previous_captains_private_phone(self):
        code, original, request, body = self.fixture(captain='Student A')
        self.assertEqual(self.admin_call('/review', body)[0], 200)
        current = self.open_team(code)
        self.assertEqual(current['captain'], 'Student A')
        self.assertEqual(current['phone'], '')
        with self.service.db() as db:
            audit = json.loads(db.execute("SELECT metadata FROM audit WHERE action='registration.verified-members'").fetchone()[0])
            self.assertEqual(audit['previousContact'], {'captain': 'Captain', 'school': 'Fixture School', 'phone': '13900000001'})
        self.assertNotIn('13900000001', json.dumps(self.public.get('/api/registration')[1]))

    def test_sync_failure_rolls_back_approval_membership_and_issued_account(self):
        code, original, request, body = self.fixture()
        with self.service.db() as db:
            before_accounts = db.execute('SELECT COUNT(*) FROM accounts').fetchone()[0]
        with patch.object(self.service.registration, 'sync_verified_members', side_effect=sqlite3.OperationalError('synthetic failure')):
            self.assertEqual(self.admin_call('/review', body)[0], 500)
        self.assertEqual(self.open_team(code)['revision'], original['revision'])
        with self.service.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM accounts').fetchone()[0], before_accounts)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM team_accounts').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM claim_memberships').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT status FROM claim_requests WHERE id=?', (request['requestId'],)).fetchone()[0], 'pending')
        self.assertEqual(self.admin_call('/review', body)[0], 200)

    def fill_roster(self, count):
        with self.service.db() as db:
            db.executemany('INSERT INTO claim_people(id,name,school,normalized_name,normalized_school) VALUES (?,?,?,?,?)',
                           [(f'full-{i}', f'Person {i}', 'Fixture School', normalize(f'Person {i}'), normalize('Fixture School')) for i in range(count)])

    def test_registration_cannot_exceed_roster_capacity_and_rolls_back_the_whole_team(self):
        self.fill_roster(498)
        for count in (498, 500):
            with self.subTest(count=count):
                if count == 500:
                    with self.service.db() as db:
                        db.executemany('INSERT INTO claim_people(id,name,school,normalized_name,normalized_school) VALUES (?,?,?,?,?)',
                                       [(f'full-{i}', f'Person {i}', 'Fixture School', normalize(f'Person {i}'), normalize('Fixture School')) for i in (498, 499)])
                status, result, _ = registration_fixture_create(self, {'code': secrets.token_urlsafe(32), 'team': self.payload()})
                self.assertEqual(status, 409, result)
                with self.service.db() as db:
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM claim_people').fetchone()[0], count)
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM teams').fetchone()[0], 0)
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM team_registrations').fetchone()[0], 0)
        self.create(captain='Person 0', members=['Person 1', 'Person 2'])
        with self.service.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM claim_people').fetchone()[0], 500)

    def test_legacy_over_capacity_roster_allows_idempotent_imports_but_not_new_people(self):
        self.fill_roster(501)
        person = {'id': 'full-0', 'name': 'Person 0', 'school': 'Fixture School', 'notes': '', 'needsReview': False}
        result = self.admin_call('/roster', {'people': [person]})
        self.assertEqual(result[0], 200, result[1])
        self.assertEqual(result[1]['added'], 0)
        self.assertEqual(self.admin_call('/roster', {'people': [{**person, 'id': 'new', 'name': 'New'}]})[0], 409)
        self.assertEqual(self.admin_call('/person', {'person': {**person, 'notes': 'Reviewed'}, 'expectedRevision': 1})[0], 200)
        with self.service.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM claim_people').fetchone()[0], 501)


if __name__ == '__main__':
    unittest.main()
