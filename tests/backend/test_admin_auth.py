"""Local organizer enrollment, login boundaries and replay protection."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import unittest

import pyotp

from backend.gotc import admin_auth
from backend.gotc.errors import APIError
from backend.gotc.service import EventService


class NamedOrganizerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.directory = Path(tmp.name)
        self.now = 1_700_000_000_000
        self.service = EventService(data_dir=self.directory, clock=lambda: self.now)
        self.legacy = (self.directory / 'admin-access.txt').read_text().splitlines()[1]
        self.addCleanup(self.service.connections.close)
        self.enrollment = admin_auth.prepare_enrollment('First.Organizer', 'Synthetic organizer')

    def token(self, entry, offset=0):
        return pyotp.TOTP(entry['seed']).at(self.now // 1000, counter_offset=offset)

    def login(self, **fields):
        return self.service.login({'role': 'admin', **fields}, 'test-ip')

    def test_rfc6238_vector_truncation_and_time_window(self):
        # The public SHA-1 Appendix B vector at 59s, truncated to six digits.
        self.assertEqual(admin_auth.verified_step('GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ', '287082', 59000, -1), 1)
        self.assertIsNone(admin_auth.verified_step(self.enrollment['seed'], self.token(self.enrollment, 2), self.now, -1))

    def test_enrollment_proof_retires_legacy_access_and_preserves_named_access_on_restart(self):
        _, legacy_session, _ = self.login(code=self.legacy)
        legacy_cookie = 'gotc_session=' + legacy_session
        team = self.service.dispatch('POST', '/api/admin/teams', {'number': 1, 'name': 'Team'},
                                     legacy_cookie, 'test-ip')[0]['team']['id']
        _, delegated_token, _ = self.service.dispatch(
            'POST', '/api/admin/team-workbench', {'teamId': team}, legacy_cookie, 'test-ip',
            expected_login_id=self.service.login_id(legacy_cookie))
        self.assertIsNotNone(delegated_token)
        with self.assertRaises(ValueError):
            admin_auth.activate(self.service, self.enrollment, 'invalid')
        self.assertEqual(self.login(code=self.legacy)[0]['role'], 'admin')
        admin_auth.activate(self.service, self.enrollment, self.token(self.enrollment, -1))
        with self.service.db() as db:
            self.assertIsNone(self.service.account(db, 'gotc_session=' + delegated_token))
            self.assertEqual(db.execute('SELECT count(*) FROM admin_team_sessions').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM sessions').fetchone()[0], 0)
        with self.assertRaises(APIError):
            self.login(code=self.legacy)
        with self.assertRaises(APIError):
            self.login(code=self.enrollment['code'], username=self.enrollment['username'])
        with self.assertRaises(APIError):
            self.service.login({'role': 'team', 'code': self.legacy}, 'test-ip')
        with self.assertRaises(APIError):
            self.login(code=self.enrollment['code'], username=self.enrollment['username'], totp='invalid')
        credentials = dict(code=self.enrollment['code'], username='FIRST.ORGANIZER', totp=self.token(self.enrollment))
        view, _, _ = self.login(**credentials)
        self.assertEqual((view['role'], view['name']), ('admin', 'Synthetic organizer'))
        with self.assertRaises(APIError):
            self.login(**credentials)
        (self.directory / 'shared-login-codes.json').write_text(json.dumps({'admin': self.legacy}))
        self.service = EventService(data_dir=self.directory, clock=lambda: self.now)
        self.addCleanup(self.service.connections.close)
        with self.assertRaises(APIError):
            self.login(**credentials)
        self.now += 30_000
        self.assertEqual(self.login(**{**credentials, 'totp': self.token(self.enrollment)})[0]['role'], 'admin')

    def test_simultaneous_use_of_one_totp_can_issue_only_one_session(self):
        admin_auth.activate(self.service, self.enrollment, self.token(self.enrollment, -1))
        fields = {'username': self.enrollment['username'], 'code': self.enrollment['code'],
                  'totp': self.token(self.enrollment)}

        def sign_in(_):
            try:
                self.login(**fields)
                return 200
            except APIError as error:
                return error.status

        with ThreadPoolExecutor(max_workers=2) as workers:
            self.assertEqual(sorted(workers.map(sign_in, range(2))), [200, 401])
        with self.service.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0], 1)

    def test_second_named_account_has_its_own_seed_and_first_account_stays_active(self):
        first = admin_auth.activate(self.service, self.enrollment, self.token(self.enrollment, -1))
        second = admin_auth.prepare_enrollment('second', 'Second synthetic organizer')
        next_id = admin_auth.activate(self.service, second, self.token(second, -1))
        self.assertNotEqual(first, next_id)
        with self.assertRaises(ValueError):
            admin_auth.activate(self.service, second, self.token(second))
        for entry in [self.enrollment, second]:
            view, token, _ = self.login(code=entry['code'], username=entry['username'], totp=self.token(entry))
            with self.service.db() as db:
                actor = self.service.account(db, 'gotc_session=' + token)
                self.assertEqual(actor['name'], view['name'])
                self.assertEqual(actor['id'], first if entry is self.enrollment else next_id)
        admin_auth.revoke(self.service, 'second')
        with self.service.db() as db:
            self.assertIsNone(self.service.account(db, 'gotc_session=' + token))
        with self.assertRaisesRegex(ValueError, 'last'):
            admin_auth.revoke(self.service, self.enrollment['username'])
        self.now += 30_000
        with self.assertRaises(APIError):
            self.login(code=second['code'], username=second['username'], totp=self.token(second))
