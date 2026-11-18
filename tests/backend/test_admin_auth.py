"""Local organizer enrollment, login boundaries and replay protection."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import httpx
import pyotp

from backend.gotc import admin_auth
from backend.gotc.credentials import Credentials
from backend.gotc.errors import APIError
from backend.gotc.http.app import create_platform_app
from backend.gotc.migrations.v002_admin_auth import convert_checkpoint, export_checkpoint
from backend.gotc.service import EventService
from ops.backup_event import copy_database


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

    def provision(self, *entries):
        path = self.directory / 'organizer-totp.json'
        path.touch(mode=0o600)
        path.write_text(json.dumps({entry['username']: entry['seed'] for entry in entries}))
        return path

    def test_seed_never_reaches_event_files_backup_logs_or_login_response(self):
        self.provision(self.enrollment)
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            admin_auth.activate(self.service, self.enrollment, self.token(self.enrollment, -1))
            async def sign_in():
                app = create_platform_app(self.service)
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=self.service.origin) as client:
                    return await client.post(self.service.base_path + '/api/login', headers={'Origin': self.service.origin},
                                             json={'role': 'admin', 'code': self.enrollment['code'],
                                                   'username': self.enrollment['username'], 'totp': self.token(self.enrollment)})

            with self.assertLogs('gotc.requests', level='INFO') as captured:
                response = asyncio.run(sign_in())
            self.assertEqual(response.status_code, 200)
            with self.service.db() as db:
                audit = repr([tuple(row) for row in db.execute('SELECT * FROM audit')])
                db.execute('PRAGMA wal_checkpoint(FULL)')
            dest = self.directory / 'backup' / 'event.sqlite3'
            result = copy_database(self.directory / 'event.sqlite3', dest)
        seed = self.enrollment['seed'].encode()
        self.assertFalse(seed in (self.directory / 'event.sqlite3').read_bytes(),
                         'Synthetic TOTP seed present in event database')
        for path in [*self.directory.glob('event.sqlite3-*'), *dest.parent.iterdir()]:
            self.assertFalse(seed in path.read_bytes(), f'Synthetic seed present in {path.name}')
        self.assertFalse(seed in (output.getvalue() + response.text + audit + repr(result) + repr(captured.output)).encode())
        self.assertFalse((dest.parent / 'organizer-totp.json').exists())

    def test_activation_requires_matching_installed_seed(self):
        for installed in [None, admin_auth.prepare_enrollment(self.enrollment['username'], 'Wrong mapping')]:
            if installed:
                self.provision(installed)
            with self.assertRaisesRegex(ValueError, 'credential'):
                admin_auth.activate(self.service, self.enrollment, self.token(self.enrollment))
            self.assertEqual(self.login(code=self.legacy)[0]['role'], 'admin')

    def test_cli_credential_view_cannot_cut_off_the_live_platform(self):
        mount = self.directory / 'mounted'
        mount.mkdir()
        with patch.dict(os.environ, {'CREDENTIALS_DIRECTORY': str(mount)}):
            live = EventService(data_dir=self.directory, clock=lambda: self.now)
        self.addCleanup(live.connections.close)
        path = self.provision(self.enrollment)
        account = admin_auth.activate(self.service, self.enrollment, self.token(self.enrollment, -1))
        fields = {'role': 'admin', 'username': self.enrollment['username'], 'code': self.enrollment['code'],
                  'totp': self.token(self.enrollment)}
        with self.assertRaises(APIError):
            live.login(fields, 'named')
        with self.assertRaises(APIError):
            live.login({'role': 'admin', 'accountId': account, 'code': self.enrollment['code']}, 'selector')
        self.assertEqual(live.login({'role': 'admin', 'code': self.legacy}, 'legacy')[0]['role'], 'admin')
        rotated = 'synthetic-rotated-shared-code'
        (self.directory / 'shared-login-codes.json').write_text(json.dumps({'admin': rotated}))
        with patch.dict(os.environ, {'CREDENTIALS_DIRECTORY': str(mount)}):
            live.connections.close()
            live = EventService(data_dir=self.directory, clock=lambda: self.now)
        self.addCleanup(live.connections.close)
        self.assertEqual(live.login({'role': 'admin', 'code': rotated}, 'legacy')[0]['role'], 'admin')
        shutil.copy2(path, mount / path.name)
        self.assertEqual(live.login(fields, 'named')[0]['role'], 'admin')
        with self.assertRaises(APIError):
            live.login({'role': 'admin', 'code': rotated}, 'legacy')

    def test_first_portal_proof_and_legacy_cutoff_roll_back_with_failed_session(self):
        self.provision(self.enrollment)
        admin_auth.activate(self.service, self.enrollment, self.token(self.enrollment, -1))
        fields = {'username': self.enrollment['username'], 'code': self.enrollment['code'],
                  'totp': self.token(self.enrollment)}
        with patch.object(self.service, 'issue_session', side_effect=RuntimeError('fixture failure')):
            with self.assertRaisesRegex(RuntimeError, 'fixture failure'):
                self.login(**fields)
        self.assertEqual(self.login(code=self.legacy)[0]['role'], 'admin')
        self.assertEqual(self.login(**fields)[0]['role'], 'admin')

    def test_revocation_requires_live_confirmation_or_explicit_offline_recovery(self):
        self.provision(self.enrollment)
        admin_auth.activate(self.service, self.enrollment, self.token(self.enrollment, -1))
        self.login(code=self.enrollment['code'], username=self.enrollment['username'], totp=self.token(self.enrollment))
        replacement = admin_auth.prepare_enrollment('replacement', 'Recovery organizer')
        self.service.connections.close()
        self.provision(replacement)
        with self.assertRaisesRegex(RuntimeError, 'matching organizer credential'):
            EventService(data_dir=self.directory)
        self.service = EventService(data_dir=self.directory, clock=lambda: self.now, check_organizer_credentials=False)
        self.addCleanup(self.service.connections.close)
        admin_auth.activate(self.service, replacement, self.token(replacement, -1))
        with self.assertRaisesRegex(ValueError, 'portal'):
            admin_auth.revoke(self.service, self.enrollment['username'])
        admin_auth.revoke(self.service, self.enrollment['username'], offline_recovery=True)
        self.service.connections.close()
        self.provision()
        with self.assertRaisesRegex(RuntimeError, 'matching organizer credential'):
            EventService(data_dir=self.directory)
        self.provision(replacement)
        self.service = EventService(data_dir=self.directory, clock=lambda: self.now)
        self.addCleanup(self.service.connections.close)
        with self.assertRaises(APIError):
            self.login(code=self.legacy)
        with self.assertRaises(APIError):
            self.login(code=self.enrollment['code'], username=self.enrollment['username'], totp=self.token(self.enrollment))
        self.assertEqual(self.login(code=replacement['code'], username=replacement['username'],
                                   totp=self.token(replacement))[0]['role'], 'admin')

    def test_restored_database_requires_original_credential_and_replay_state(self):
        path = self.provision(self.enrollment)
        admin_auth.activate(self.service, self.enrollment, self.token(self.enrollment, -1))
        self.login(code=self.enrollment['code'], username=self.enrollment['username'], totp=self.token(self.enrollment))
        dest = self.directory / 'restore' / 'event.sqlite3'
        copy_database(self.service.db_path, dest)
        shutil.copy2(self.directory / 'api-key-pepper', dest.parent)
        with self.assertRaisesRegex(RuntimeError, 'matching organizer credential'):
            EventService(data_dir=dest.parent)
        wrong = admin_auth.prepare_enrollment(self.enrollment['username'], 'Wrong restored credential')
        dest_path = dest.parent / path.name
        dest_path.touch(mode=0o600)
        dest_path.write_text(json.dumps({wrong['username']: wrong['seed']}))
        with self.assertRaisesRegex(RuntimeError, 'matching organizer credential'):
            EventService(data_dir=dest.parent)
        shutil.copy2(path, dest_path)
        self.service = EventService(data_dir=dest.parent, clock=lambda: self.now)
        self.addCleanup(self.service.connections.close)
        fields = {'code': self.enrollment['code'], 'username': self.enrollment['username']}
        with self.assertRaises(APIError):
            self.login(code=self.legacy)
        with self.assertRaises(APIError):
            self.login(**fields, totp=self.token(self.enrollment))
        self.now += 30000
        dest_path.unlink()
        with self.assertRaises(APIError):
            self.login(**fields, totp=self.token(self.enrollment))
        shutil.copy2(path, dest_path)
        self.assertEqual(self.login(**fields, totp=self.token(self.enrollment))[0]['role'], 'admin')

    def test_earlier_seedless_checkpoint_supports_empty_and_confirmed_accounts(self):
        self.service.connections.close()
        with closing(sqlite3.connect(self.service.db_path)) as db:
            db.execute('ALTER TABLE named_admins DROP COLUMN portal_confirmed')
        self.service = EventService(data_dir=self.directory, clock=lambda: self.now)
        self.addCleanup(self.service.connections.close)
        self.assertEqual(self.login(code=self.legacy)[0]['role'], 'admin')
        self.provision(self.enrollment)
        admin_auth.activate(self.service, self.enrollment, self.token(self.enrollment, -1))
        self.login(code=self.enrollment['code'], username=self.enrollment['username'], totp=self.token(self.enrollment))
        self.service.connections.close()
        with closing(sqlite3.connect(self.service.db_path)) as db:
            db.execute('ALTER TABLE named_admins DROP COLUMN portal_confirmed')
        self.service = EventService(data_dir=self.directory, clock=lambda: self.now)
        self.addCleanup(self.service.connections.close)
        with self.assertRaises(APIError):
            self.login(code=self.legacy)
        self.now += 30000
        self.assertEqual(self.login(code=self.enrollment['code'], username=self.enrollment['username'],
                                   totp=self.token(self.enrollment))[0]['role'], 'admin')

    def test_seed_checkpoint_requires_explicit_export_and_matching_conversion(self):
        path = self.provision(self.enrollment)
        account = admin_auth.activate(self.service, self.enrollment, self.token(self.enrollment))
        self.service.connections.close()
        path.unlink()
        seed = self.enrollment['seed']
        with closing(sqlite3.connect(self.service.db_path)) as db:
            db.execute('DROP TABLE named_admins')
            db.execute("""CREATE TABLE named_admins(account_id TEXT PRIMARY KEY REFERENCES accounts(id),
                          username TEXT NOT NULL UNIQUE COLLATE NOCASE,totp_seed TEXT NOT NULL,last_step INTEGER NOT NULL)""")
            db.execute('INSERT INTO named_admins VALUES (?,?,?,?)',
                       (account, self.enrollment['username'], seed, self.now // 30000))
            db.commit()
        with self.assertRaisesRegex(RuntimeError, 'migrate-checkpoint'):
            EventService(data_dir=self.directory)
        backup = self.directory / 'backup' / 'event.sqlite3'
        with self.assertRaisesRegex(ValueError, 'checkpoint'):
            copy_database(self.service.db_path, backup)
        self.assertFalse(backup.exists())
        with self.assertRaisesRegex(ValueError, 'credential'):
            convert_checkpoint(self.directory, Credentials(self.directory))
        self.assertFalse(path.exists())
        with closing(sqlite3.connect(self.service.db_path)) as db:
            self.assertTrue(seed == db.execute('SELECT totp_seed FROM named_admins').fetchone()[0])
        export_checkpoint(self.directory, path)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(FileExistsError):
            export_checkpoint(self.directory, path)
        convert_checkpoint(self.directory, Credentials(self.directory))
        self.assertFalse(seed.encode() in self.service.db_path.read_bytes())
        copy_database(self.service.db_path, backup)
        self.assertFalse(seed.encode() in backup.read_bytes())
        self.service = EventService(data_dir=self.directory, clock=lambda: self.now)
        self.addCleanup(self.service.connections.close)
        with self.service.db() as db:
            row = db.execute('SELECT account_id,username,last_step FROM named_admins').fetchone()
            self.assertEqual(tuple(row), (account, self.enrollment['username'], self.now // 30000))
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])
        self.now += 30000
        self.assertEqual(self.login(code=self.enrollment['code'], username=self.enrollment['username'],
                                   totp=self.token(self.enrollment))[0]['role'], 'admin')

    def token(self, entry, offset=0):
        return pyotp.TOTP(entry['seed']).at(self.now // 1000, counter_offset=offset)

    def login(self, **fields):
        return self.service.login({'role': 'admin', **fields}, 'test-ip')

    def test_rfc6238_vector_truncation_and_time_window(self):
        # The public SHA-1 Appendix B vector at 59s, truncated to six digits.
        self.assertEqual(admin_auth.verified_step('GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ', '287082', 59000, -1), 1)
        self.assertIsNone(admin_auth.verified_step(self.enrollment['seed'], self.token(self.enrollment, 2), self.now, -1))

    def test_first_portal_proof_retires_legacy_access_and_preserves_named_access_on_restart(self):
        self.provision(self.enrollment)
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
        with self.assertRaises(APIError):
            self.login(code=self.enrollment['code'], username=self.enrollment['username'])
        with self.assertRaises(APIError):
            self.service.login({'role': 'team', 'code': self.legacy}, 'test-ip')
        with self.assertRaises(APIError):
            self.login(code=self.enrollment['code'], username=self.enrollment['username'], totp='invalid')
        credentials = dict(code=self.enrollment['code'], username='FIRST.ORGANIZER', totp=self.token(self.enrollment))
        view, _, _ = self.login(**credentials)
        with self.service.db() as db:
            self.assertIsNone(self.service.account(db, 'gotc_session=' + delegated_token))
            self.assertEqual(db.execute('SELECT count(*) FROM admin_team_sessions').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM sessions').fetchone()[0], 1)
        with self.assertRaises(APIError):
            self.login(code=self.legacy)
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

    def test_password_and_totp_failures_share_peer_and_organizer_budgets(self):
        self.provision(self.enrollment)
        admin_auth.activate(self.service, self.enrollment, self.token(self.enrollment, -1))
        valid = {'role':'admin', 'username':self.enrollment['username'], 'code':self.enrollment['code'],
                 'totp':self.token(self.enrollment)}
        for fields in [{'code':'invalid'}, {'totp':'invalid'}]:
            with self.assertRaises(APIError) as failed:
                self.service.login({**valid, **fields}, 'fixture-peer')
            self.assertEqual(failed.exception.status, 401)
        self.assertEqual(self.service.login(valid, 'fixture-peer')[0]['role'], 'admin')
        self.assertEqual(len(self.service.login_attempts[('login-ip', 'fixture-peer')]), 2)
        self.assertEqual(len(self.service.organizer_failures), 2)

    def test_simultaneous_use_of_one_totp_can_issue_only_one_session(self):
        self.provision(self.enrollment)
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
        self.provision(self.enrollment)
        first = admin_auth.activate(self.service, self.enrollment, self.token(self.enrollment, -1))
        second = admin_auth.prepare_enrollment('second', 'Second synthetic organizer')
        self.provision(self.enrollment, second)
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
