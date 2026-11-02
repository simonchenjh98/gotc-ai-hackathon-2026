"""Exact-parent migration and rollback rehearsals, using disposable synthetic data."""
from contextlib import closing, contextmanager
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import app
import test_api as api_helpers


LEGACY_SHA256 = 'f9a85aec30342e95ae4e93d238242c4f5986e1eb1d15014889bfc5825e6e49a5'
LEGACY_PATH = Path(__file__).parent / 'test_fixtures' / 'legacy_e456bd3_app.py'


def legacy_module():
    if hashlib.sha256(LEGACY_PATH.read_bytes()).hexdigest() != LEGACY_SHA256:
        raise RuntimeError('The exact e456bd3 application fixture was modified')
    name = 'gotc_legacy_e456bd3_test_fixture'
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, LEGACY_PATH)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def snapshot(path):
    with closing(sqlite3.connect(path)) as db:
        names = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return {
            'schema': db.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name').fetchall(),
            'tables': {name: db.execute('SELECT * FROM "' + name.replace('"', '""') + '" ORDER BY rowid').fetchall() for name in names},
        }


def copy_to_new_private_directory(source, destination, credentials):
    """Rehearsal-only restore: an existing destination is always rejected."""
    destination.mkdir(mode=0o700, parents=False, exist_ok=False)
    target = destination / 'event.sqlite3'
    fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as src:
        with closing(sqlite3.connect(target)) as dst:
            src.backup(dst)
            if dst.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or dst.execute('PRAGMA foreign_key_check').fetchall():
                raise AssertionError('Invalid restored database')
    for name, content in credentials.items():
        with os.fdopen(os.open(destination / name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), 'wb') as stream:
            stream.write(content)
    return target


@contextmanager
def running_http(module, service):
    server = module.EventHTTPServer(('127.0.0.1', 0), service)
    service.origin = 'http://127.0.0.1:' + str(server.server_port)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)


class ExactParentReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='gotc-pr1-release-')
        self.root = Path(self.temporary.name)
        self.legacy = legacy_module()
        self.public = self.root / 'public'
        self.public.mkdir()
        (self.public / 'index.html').write_text('<!doctype html><title>Synthetic legacy service</title>')

    def tearDown(self):
        self.temporary.cleanup()

    def old_fixture(self, name='event'):
        data = self.root / name
        data.mkdir(mode=0o700)
        (data / 'shared-login-codes.json').write_text(json.dumps({'admin': 'SYNTHETIC_OLD_ADMIN_20261030'}))
        os.chmod(data / 'shared-login-codes.json', 0o600)
        service = self.legacy.EventService(data_dir=data, origin='http://127.0.0.1', static_root=self.public)
        admin_code = (data / 'admin-access.txt').read_text().splitlines()[1]
        response, token, _ = service.dispatch('POST', '/api/login', {'role': 'admin', 'code': admin_code}, '', '127.0.0.1')
        self.assertEqual(response['role'], 'admin')
        admin_cookie = self.legacy.COOKIE_NAME + '=' + token
        def post(path, body, cookie=admin_cookie):
            return service.dispatch('POST', path, body, cookie, '127.0.0.1')
        teams = []
        for number in [1, 2, 3]:
            teams.append(post('/api/admin/teams', {'number': number, 'name': 'Old team ' + str(number),
                'project': 'Old project', 'track': 'Original track'})[0]['team']['id'])
        people = []
        for role, name in [('judge', 'Old judge A'), ('judge', 'Old judge B'), ('audience', 'Old audience')]:
            invitation = post('/api/admin/invitations', {'role': role, 'name': name})[0]
            response, login_token, _ = post('/api/login', {'role': role, 'code': invitation['code']}, '')
            self.assertEqual(response['role'], role)
            people.append({**invitation, 'role': role, 'cookie': self.legacy.COOKIE_NAME + '=' + login_token})
        post('/api/admin/settings', {'scoringOpen': True, 'resultsPublished': True, 'minJudges': 1})
        for judge, scores in zip(people[:2], [[16, 16, 16, 12, 12, 8], [20, 20, 20, 15, 15, 10]]):
            post('/api/score', {'teamId': teams[0], 'scores': scores, 'status': 'submitted', 'feedback': 'OLD_PRIVATE_FEEDBACK'}, judge['cookie'])
        post('/api/score', {'teamId': teams[1], 'scores': [14, 14, 14, 10.5, 10.5, 7], 'status': 'draft'}, people[0]['cookie'])
        post('/api/score', {'teamId': teams[2], 'status': 'recused'}, people[1]['cookie'])
        post('/api/vote', {'teamId': teams[0]}, people[2]['cookie'])
        with service.db() as db:
            db.execute("INSERT INTO settings VALUES ('syntheticCustomSetting','{\"keep\":true}')")
        with service.db() as db:
            db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        credentials = {name: (data / name).read_bytes() for name in ['admin-access.txt', 'shared-login-codes.json']}
        before = snapshot(service.db_path)
        self.assertNotIn('submissions', before['tables'])
        self.assertTrue(all(before['tables'][name] for name in ['accounts', 'sessions', 'teams', 'scores', 'votes', 'settings', 'audit']))
        return {'data': data, 'service': service, 'admin_code': admin_code, 'admin_cookie': admin_cookie,
                'people': people, 'teams': teams, 'credentials': credentials, 'before': before}

    def upgraded(self, fixture):
        return app.EventService(data_dir=fixture['data'], origin='http://127.0.0.1', static_root=self.public)

    def assert_valid_private(self, path):
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])

    def add_new_submission(self, service, fixture):
        cookie = fixture['admin_cookie']
        access = service.dispatch('POST', '/api/admin/team-access', {
            'teamId': fixture['teams'][0], 'action': 'issue'}, cookie, '127.0.0.1')[0]
        response, token, _ = service.dispatch('POST', '/api/login', {
            'role': 'team', 'code': access['code']}, '', '127.0.0.1')
        self.assertEqual(response['role'], 'team')
        service.dispatch('POST', '/api/admin/settings', {'submissionsOpen': True}, cookie, '127.0.0.1')
        materials = {'projectName': 'NEW_FORMAL_PROJECT', 'summary': 'New synthetic material',
            'codeUrl': 'https://github.com/synthetic/repository', 'codeVersion': 'a'*40, 'readmeUrl': 'https://example.invalid/readme',
            'license': 'MIT', 'demoInstructions': 'Synthetic local demo',
            'slidesUrl': 'https://example.invalid/slides', 'videoUrl': 'https://example.invalid/video',
            'sourcesAndLimitations': 'Synthetic only', 'confirmed': True}
        service.dispatch('POST', '/api/submission', {'expectedVersion': 0, 'status': 'submitted', 'materials': materials},
                         app.COOKIE_NAME + '=' + token, '127.0.0.1')
        return access['id']

    def assert_old_http_works(self, data, fixture):
        service = self.legacy.EventService(data_dir=data, origin='http://127.0.0.1', static_root=self.public)
        with running_http(self.legacy, service) as server:
            old_session = api_helpers.Client(server)
            old_session.cookie = fixture['people'][0]['cookie']
            self.assertEqual(old_session.get('/api/session')[1]['role'], 'judge')
            admin = api_helpers.Client(server)
            self.assertEqual(admin.post('/api/login', {'role': 'admin', 'code': fixture['admin_code']})[0], 200)
            judge = api_helpers.Client(server)
            self.assertEqual(judge.post('/api/login', {'role': 'judge', 'code': fixture['people'][0]['code']})[0], 200)
            public = api_helpers.Client(server)
            self.assertEqual(public.get('/api/board')[1]['teams'][0]['score'], 90)
            self.assertEqual(judge.post('/api/score', {'teamId': fixture['teams'][0],
                'scores': [18, 18, 18, 13.5, 13.5, 9], 'status': 'submitted', 'feedback': 'Rollback app still scores'})[0], 200)
            self.assertEqual(public.get('/api/board')[1]['teams'][0]['score'], 95)
            audience = api_helpers.Client(server)
            self.assertEqual(audience.post('/api/login', {'role': 'audience', 'code': fixture['people'][2]['code']})[0], 200)
            self.assertEqual(audience.post('/api/vote', {'teamId': fixture['teams'][1]})[0], 200)
            self.assertEqual(sum(row['audienceVotes'] for row in public.get('/api/board')[1]['teams']), 2)
            status, content, headers = admin.get('/api/admin/export')
            self.assertEqual(status, 200)
            self.assertIn('总分', content.decode('utf-8-sig'))
            self.assertIn('attachment', headers['Content-Disposition'])
            self.assertEqual(public.get('/api/team')[0], 404)
        return service

    def test_exact_parent_migration_preserves_nonempty_data_and_restarts_idempotently(self):
        fixture = self.old_fixture()
        first = self.upgraded(fixture)
        after = snapshot(first.db_path)
        for table, rows in fixture['before']['tables'].items():
            if table != 'settings':
                self.assertEqual(after['tables'][table], rows, table)
        old_settings = dict(fixture['before']['tables']['settings'])
        new_settings = dict(after['tables']['settings'])
        self.assertEqual({key: new_settings[key] for key in old_settings}, old_settings)
        self.assertEqual(new_settings['submissionsOpen'], 'false')
        self.assertEqual(new_settings['submissionDeadline'], 'null')
        with first.db() as db:
            self.assertIn("'team'", db.execute("SELECT sql FROM sqlite_master WHERE name='accounts'").fetchone()[0])
            for table in ['scores', 'votes', 'sessions']:
                self.assertTrue(any(row['table'] == 'accounts' for row in db.execute('PRAGMA foreign_key_list(' + table + ')')))
        for name, content in fixture['credentials'].items():
            self.assertEqual((fixture['data'] / name).read_bytes(), content)
            self.assertEqual(stat.S_IMODE((fixture['data'] / name).stat().st_mode), 0o600)
        backups = list(fixture['data'].glob('before-team-*.sqlite3'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(snapshot(backups[0]), fixture['before'])
        self.assert_valid_private(backups[0])
        self.assert_valid_private(first.db_path)
        second = self.upgraded(fixture)
        self.assertEqual(snapshot(second.db_path), after)
        self.assertEqual(list(fixture['data'].glob('before-team-*.sqlite3')), backups)

    def test_automatic_backup_connection_is_closed_before_startup_returns(self):
        fixture = self.old_fixture()
        connect = sqlite3.connect
        backups = []
        def tracked(database, *values, **options):
            db = connect(database, *values, **options)
            if Path(database).name.startswith('before-team-'):
                backups.append(db)
            return db
        try:
            with patch('app.sqlite3.connect', side_effect=tracked):
                self.upgraded(fixture)
            self.assertEqual(len(backups), 1)
            with self.assertRaises(sqlite3.ProgrammingError):
                backups[0].execute('SELECT 1')
        finally:
            for db in backups:
                db.close()

    def test_failure_at_each_replacement_boundary_restores_exact_old_schema_and_backup(self):
        for fail_at in ['DROP TABLE accounts', 'ALTER TABLE accounts_new', 'PRAGMA foreign_key_check']:
            with self.subTest(fail_at=fail_at):
                fixture = self.old_fixture('failed-' + str(len(list(self.root.glob('failed-*')))))
                path = fixture['service'].db_path
                connect = sqlite3.connect
                class FailingConnection:
                    def __init__(self, db):
                        self.db = db
                    def __getattr__(self, name):
                        return getattr(self.db, name)
                    def execute(self, sql, *values):
                        if sql.startswith(fail_at):
                            raise sqlite3.OperationalError('injected replacement failure')
                        return self.db.execute(sql, *values)
                def injected(database, *values, **options):
                    db = connect(database, *values, **options)
                    return FailingConnection(db) if Path(database).resolve() == path.resolve() else db
                with patch('app.sqlite3.connect', side_effect=injected):
                    with self.assertRaisesRegex(sqlite3.OperationalError, 'injected replacement failure'):
                        self.upgraded(fixture)
                self.assertEqual(snapshot(path), fixture['before'])
                self.assertNotIn('accounts_new', snapshot(path)['tables'])
                for name, content in fixture['credentials'].items():
                    self.assertEqual((fixture['data'] / name).read_bytes(), content)
                backups = list(fixture['data'].glob('before-team-*.sqlite3'))
                self.assertEqual(len(backups), 1)
                self.assertEqual(snapshot(backups[0]), fixture['before'])
                self.assert_valid_private(backups[0])
                recovered = self.upgraded(fixture)
                self.assert_valid_private(recovered.db_path)
                self.assertEqual(recovered.dispatch('GET', '/api/session', {}, fixture['people'][0]['cookie'], '127.0.0.1')[0]['role'], 'judge')

    def test_old_code_can_run_upgraded_database_without_discarding_new_team_materials(self):
        fixture = self.old_fixture()
        upgraded = self.upgraded(fixture)
        team_account = self.add_new_submission(upgraded, fixture)
        source_state = snapshot(upgraded.db_path)
        rollback_data = self.root / 'code-rollback'
        rollback_db = copy_to_new_private_directory(upgraded.db_path, rollback_data, fixture['credentials'])
        self.assertEqual(snapshot(rollback_db), source_state)
        self.assert_old_http_works(rollback_data, fixture)
        restored = snapshot(rollback_db)
        for table in ['team_accounts', 'submissions']:
            self.assertEqual(restored['tables'][table], source_state['tables'][table], table)
        self.assertEqual([row for row in restored['tables']['accounts'] if row[0] == team_account],
                         [row for row in source_state['tables']['accounts'] if row[0] == team_account])
        self.assertEqual(snapshot(upgraded.db_path), source_state)
        self.assert_valid_private(rollback_db)

    def test_restoring_preupgrade_snapshot_to_new_target_keeps_source_and_omits_later_writes(self):
        fixture = self.old_fixture()
        upgraded = self.upgraded(fixture)
        self.add_new_submission(upgraded, fixture)
        source_after_new_writes = snapshot(upgraded.db_path)
        backup = next(fixture['data'].glob('before-team-*.sqlite3'))
        target = self.root / 'database-rollback'
        restored = copy_to_new_private_directory(backup, target, fixture['credentials'])
        self.assertEqual(snapshot(restored), fixture['before'])
        self.assertNotIn('submissions', snapshot(restored)['tables'])
        self.assertNotIn('team_accounts', snapshot(restored)['tables'])
        with self.assertRaises(FileExistsError):
            copy_to_new_private_directory(backup, target, fixture['credentials'])
        self.assertEqual(snapshot(restored), fixture['before'])
        self.assert_old_http_works(target, fixture)
        self.assertEqual(snapshot(upgraded.db_path), source_after_new_writes)
        self.assertTrue(source_after_new_writes['tables']['submissions'])
        self.assert_valid_private(restored)

    def test_main_does_not_construct_http_listener_when_actual_migration_fails(self):
        fixture = self.old_fixture()
        connect = sqlite3.connect
        path = fixture['service'].db_path
        class FailingConnection:
            def __init__(self, db):
                self.db = db
            def __getattr__(self, name):
                return getattr(self.db, name)
            def execute(self, sql, *values):
                if sql.startswith('ALTER TABLE accounts_new'):
                    raise sqlite3.OperationalError('injected startup migration failure')
                return self.db.execute(sql, *values)
        def injected(database, *values, **options):
            db = connect(database, *values, **options)
            return FailingConnection(db) if Path(database).resolve() == path.resolve() else db
        previous_umask = os.umask(0o077)
        try:
            with patch.dict(os.environ, {'GOTC_DATA_DIR': str(fixture['data']), 'GOTC_HOST': '127.0.0.1',
                    'GOTC_PORT': '0', 'GOTC_PUBLIC_ORIGIN': 'http://127.0.0.1'}), \
                    patch('app.sqlite3.connect', side_effect=injected), patch('app.EventHTTPServer') as listener:
                with self.assertRaisesRegex(sqlite3.OperationalError, 'injected startup migration failure'):
                    app.main()
                listener.assert_not_called()
        finally:
            os.umask(previous_umask)
        self.assertEqual(snapshot(path), fixture['before'])


if __name__ == '__main__':
    unittest.main()
