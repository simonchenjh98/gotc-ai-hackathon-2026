"""Deployment entrypoint rehearsals: temporary paths and fake SSH only."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import types
import unittest
import subprocess
import tarfile
import re
import selectors
import shutil
import signal
import urllib.request
import urllib.error
from unittest.mock import patch


from ops.deploy import deploy, install, pages


class LocalDeploymentPrivacyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='gotc-deploy-private-')
        self.root = Path(self.temp.name)
        self.installer = install
        self.deployer = deploy
        self.project = self.root / 'project'
        self.project.mkdir()
        for name in self.installer.FILES:
            path = self.project / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('Synthetic release file\n')
        installer_path = self.project / 'ops/deploy/install.py'
        installer_path.parent.mkdir()
        installer_path.write_text('# Synthetic installer payload, never executed\n')
        self.runtime = self.project / '.runtime'
        self.secret = b'GOTC administrator access code (keep private):\nSYNTHETIC_REMOTE_ACCESS\n'
        self.secret_reads = 0
        self.check_before_read = lambda: None
        self.fail_after_first_block = False
        self.uploaded = []
        self.remote_commands = []
        self.closed_clients = 0
        test = self

        class Channel:
            def recv_exit_status(self):
                return 0

        class RemoteRead:
            def __init__(self):
                self.sent = False
            def __enter__(self):
                return self
            def __exit__(self, *unused):
                return False
            def read(self, size):
                if self.sent:
                    if test.fail_after_first_block:
                        raise OSError('Synthetic interrupted SFTP read')
                    return b''
                test.check_before_read()
                test.secret_reads += 1
                self.sent = True
                return test.secret[:8] if test.fail_after_first_block else test.secret

        class RemoteWrite(io.BytesIO):
            def __exit__(self, *unused):
                test.uploaded.append(self.getvalue())
                self.close()

        class SFTP:
            def __enter__(self):
                return self
            def __exit__(self, *unused):
                return False
            def open(self, remote, mode):
                if mode == 'wx':
                    test.assertTrue(remote.startswith('/opt/gotc-hackathon/incoming/'))
                    return RemoteWrite()
                test.assertEqual(mode, 'rb')
                test.assertEqual(remote, '/opt/gotc-hackathon/data/admin-access.txt')
                return RemoteRead()
            def chmod(self, remote, mode):
                test.assertEqual(mode, 0o600)

        class Client:
            def load_host_keys(self, filename):
                test.assertEqual(filename, str(test.root / 'fake-known-hosts'))
            def set_missing_host_key_policy(self, policy):
                pass
            def connect(self, host, **options):
                test.assertEqual(options['key_filename'], str(test.root / 'fake-key'))
                test.assertFalse(options['look_for_keys'])
                test.assertFalse(options['allow_agent'])
            def exec_command(self, command, timeout):
                test.remote_commands.append(command)
                value = {'url': 'https://example.invalid/synthetic-gotc/', 'health': {'teams': 0}}
                stdout = io.BytesIO((json.dumps(value) + '\n').encode())
                stdout.channel = Channel()
                return None, stdout, io.BytesIO(b'')
            def open_sftp(self):
                return SFTP()
            def close(self):
                test.closed_clients += 1

        self.paramiko = types.SimpleNamespace(SSHClient=Client, RejectPolicy=object)

    def tearDown(self):
        self.temp.cleanup()

    def run_main(self, execute=True):
        arguments = ['deploy.py', '--source-sha', 'e' * 40] + (['--execute'] if execute else [])
        output = io.StringIO()
        with patch.object(self.deployer, 'PROJECT', self.project), \
                patch.object(self.deployer, 'KNOWN_HOSTS', self.root / 'fake-known-hosts'), \
                patch.object(self.deployer, 'IDENTITY', self.root / 'fake-key'), \
                patch.dict(sys.modules, {'paramiko': self.paramiko}), \
                patch.object(sys, 'argv', arguments), \
                patch('socket.socket.connect', side_effect=AssertionError('No real network permitted')), \
                patch.object(self.deployer.subprocess, 'check_output', side_effect=AssertionError('No real Git command needed')), \
                redirect_stdout(output):
            self.deployer.main()
        return output.getvalue()

    def check_private_paths(self):
        self.assertFalse(self.runtime.is_symlink())
        self.assertEqual(stat.S_IMODE(self.runtime.stat().st_mode), 0o700,
                         'Existing runtime must be private before the first secret byte is read')
        pending = self.runtime / 'admin-access.txt.pending'
        self.assertFalse(pending.is_symlink())
        self.assertEqual(stat.S_IMODE(pending.stat().st_mode), 0o600,
                         'Pending credential must be private before any remote secret is read')

    def test_execute_repairs_existing_runtime_permissions_before_sftp_reads_any_secret(self):
        self.runtime.mkdir(mode=0o755)
        os.chmod(self.runtime, 0o755)
        self.check_before_read = self.check_private_paths
        previous = os.umask(0o022)
        try:
            printed = self.run_main()
        finally:
            os.umask(previous)
        self.assertEqual(self.secret_reads, 1)
        self.assertEqual((self.runtime / 'admin-access.txt').read_bytes(), self.secret)
        self.assertEqual(stat.S_IMODE((self.runtime / 'admin-access.txt').stat().st_mode), 0o600)
        self.assertFalse((self.runtime / 'admin-access.txt.pending').exists())
        self.assertNotIn('SYNTHETIC_REMOTE_ACCESS', printed)
        self.assertNotIn('SYNTHETIC_REMOTE_ACCESS', (self.runtime / 'deployment.json').read_text())
        self.assertEqual(self.closed_clients, 1)

    def test_execute_creates_pending_credential_as_0600_before_first_secret_byte(self):
        self.runtime.mkdir(mode=0o700)
        os.chmod(self.runtime, 0o700)
        self.check_before_read = self.check_private_paths
        previous = os.umask(0o022)
        try:
            self.run_main()
        finally:
            os.umask(previous)
        self.assertEqual(self.secret_reads, 1)
        self.assertEqual(stat.S_IMODE((self.runtime / 'admin-access.txt').stat().st_mode), 0o600)

    def test_runtime_symlink_is_rejected_before_writing_to_its_target(self):
        external = self.root / 'not-owned-by-runtime'
        external.mkdir()
        sentinel = external / 'keep.txt'
        sentinel.write_text('Unchanged sentinel')
        self.runtime.symlink_to(external, target_is_directory=True)
        with self.assertRaises((RuntimeError, ValueError, FileExistsError)):
            self.run_main(execute=False)
        self.assertEqual(list(external.iterdir()), [sentinel])
        self.assertEqual(sentinel.read_text(), 'Unchanged sentinel')
        self.assertEqual(self.remote_commands, [])

    def test_runtime_regular_file_is_rejected_without_replacement(self):
        self.runtime.write_text('Do not replace this file')
        with self.assertRaises((RuntimeError, ValueError, FileExistsError)):
            self.run_main(execute=False)
        self.assertEqual(self.runtime.read_text(), 'Do not replace this file')
        self.assertEqual(self.remote_commands, [])

    def test_existing_pending_and_symlink_final_are_rejected_without_secret_reads(self):
        for existing in ['pending', 'final-symlink']:
            with self.subTest(existing=existing):
                if self.runtime.exists():
                    shutil.rmtree(self.runtime)
                self.runtime.mkdir(mode=0o700)
                final = self.runtime / 'admin-access.txt'
                if existing == 'pending':
                    final.write_bytes(b'OLD_FINAL')
                    (self.runtime / 'admin-access.txt.pending').write_bytes(b'KEEP_PENDING')
                else:
                    target = self.root / 'private-sentinel'
                    target.write_bytes(b'KEEP_EXTERNAL')
                    final.symlink_to(target)
                before_reads = self.secret_reads
                with self.assertRaises((RuntimeError, ValueError, FileExistsError)):
                    self.run_main()
                self.assertEqual(self.secret_reads, before_reads)
                self.assertEqual(final.read_bytes(), b'OLD_FINAL' if existing == 'pending' else b'KEEP_EXTERNAL')
                if existing == 'pending':
                    self.assertEqual((self.runtime / 'admin-access.txt.pending').read_bytes(), b'KEEP_PENDING')

    def test_interrupted_private_retrieval_preserves_old_final(self):
        self.runtime.mkdir(mode=0o700)
        final = self.runtime / 'admin-access.txt'
        final.write_bytes(b'PREVIOUS_FINAL')
        os.chmod(final, 0o600)
        self.check_before_read = self.check_private_paths
        self.fail_after_first_block = True
        previous = os.umask(0o022)
        try:
            with self.assertRaises(OSError):
                self.run_main()
        finally:
            os.umask(previous)
        self.assertEqual(final.read_bytes(), b'PREVIOUS_FINAL')
        pending = self.runtime / 'admin-access.txt.pending'
        if pending.exists():
            self.assertEqual(stat.S_IMODE(pending.stat().st_mode), 0o600)
        self.assertEqual(self.closed_clients, 1)


class ReleaseArtifactAndInstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='gotc-package-')
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def package(self, filename, project=deploy.PROJECT):
        path = self.root / filename
        output = subprocess.check_output([
            sys.executable, '-B', str(project / 'ops/deploy/deploy.py'),
            '--package', str(path), '--source-sha', '0' * 40,
        ], text=True, cwd=self.root)
        result = json.loads(output)
        self.assertFalse(result['remoteChanges'])
        return path

    @unittest.skipUnless(shutil.which('make'), 'make is unavailable')
    def test_make_dev_detects_existing_event_before_creating_new_data(self):
        project = self.root / 'checkout'
        data = project / 'server/data'
        data.mkdir(parents=True)
        sentinel = data / 'event.sqlite3'
        sentinel.write_bytes(b'Synthetic legacy directory sentinel')
        shutil.copyfile(deploy.PROJECT / 'Makefile', project / 'Makefile')
        shutil.copytree(deploy.PROJECT / 'backend', project / 'backend', ignore=shutil.ignore_patterns('__pycache__'))
        state = self.root / 'xdg-data'
        env = {'PATH': os.defpath, 'XDG_DATA_HOME': str(state), 'PYTHON': sys.executable}
        with subprocess.Popen([shutil.which('make'), 'dev', 'PORT=0'], env=env, cwd=project,
                              start_new_session=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True) as process:
            try:
                _, errors = process.communicate(timeout=8)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
                self.fail('make dev started a new event alongside the legacy directory')
        self.assertNotEqual(process.returncode, 0)
        self.assertIn('Set GOTC_DATA_DIR', errors)
        self.assertIn(str(data), errors)
        self.assertFalse(state.exists())
        self.assertEqual(sentinel.read_bytes(), b'Synthetic legacy directory sentinel')

    def test_release_manifest_includes_each_application_module(self):
        suffixes = {'backend/gotc': {'.py', '.json'}, 'frontend/static': {'.js', '.css'}}
        for directory, extensions in suffixes.items():
            with self.subTest(directory=directory):
                sources = {path.relative_to(deploy.PROJECT).as_posix()
                           for path in (deploy.PROJECT / directory).rglob('*')
                           if path.is_file() and path.suffix in extensions}
                self.assertSetEqual(sources, {name for name in install.FILES if name.startswith(directory + '/')})

    def test_source_archives_are_reproducible(self):
        project = self.root / 'source'
        for name in install.FILES | {'ops/deploy/deploy.py', 'ops/deploy/install.py'}:
            target = project / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(deploy.PROJECT / name, target)
        first = self.package('first.tar.gz', project)
        second = self.package('second.tar.gz', project)
        self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_pages_staging_contains_only_approved_landing_page_files(self):
        staged = self.root / 'pages'
        pages.stage(staged)
        names = {path.relative_to(staged).as_posix() for path in staged.rglob('*') if path.is_file()}
        self.assertEqual(names - {'.nojekyll'}, {name.removeprefix('frontend/') for name in install.PAGES_FILES})
        self.assertIn('index.html', names)
        self.assertIn('static/registration/directory.js', names)
        html = (staged / 'index.html').read_text()
        for attr, route in [('data-api', 'api/registration'), ('data-platform-base', ''),
                            ('href', 'platform.html#team'), ('href', 'teams.html')]:
            self.assertIn(f'{attr}="{pages.PLATFORM_BASE}{route}"', html)
        self.assertNotIn('href="platform.html', html)
        self.assertNotIn('href="teams.html', html)
        self.assertFalse(any(name.endswith('.py') or name.startswith(('backend/', 'ops/', 'tests/')) for name in names))
        with self.assertRaises(FileExistsError):
            pages.stage(staged)

    def test_source_manifest_rejects_private_files_and_symlinked_ancestors(self):
        for name in ['backend/gotc/.env', 'backend/gotc/keys.sqlite3', 'frontend/downloads/admin-access.txt',
                     'frontend/assets/unapproved.jpg', 'frontend/../frontend/index.html', 'tests/backend/fixtures/legacy_e456bd3_app.py']:
            with self.subTest(name=name):
                self.assertFalse(install.allowed(name))
        external = self.root / 'external'
        external.mkdir()
        (external / 'index.html').write_text('private sentinel')
        (self.root / 'frontend').symlink_to(external, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, 'symlink'):
            deploy.release_sources(self.root, ['frontend/index.html'])

    def test_extracted_release_serves_static_and_authenticated_workflows(self):
        archive = self.package('release.tar.gz')
        extract = self.root / 'extracted'
        with tarfile.open(archive) as package:
            names = package.getnames()
            self.assertEqual(set(names), install.FILES)
            self.assertEqual(names, sorted(set(names)))
            self.assertTrue(all(entry.isfile() for entry in package.getmembers()))
            self.assertIn('backend/gotc/__main__.py', names)
            self.assertIn('ops/backup_event.py', names)
            package.extractall(extract, filter='data')

        # Seed local test accounts using only the extracted Python modules.
        origin = 'https://example.invalid'
        env = {'PATH': os.defpath, 'PYTHONDONTWRITEBYTECODE': '1',
               'GOTC_DATA_DIR': str(self.root / 'private-data'), 'GOTC_HOST': '127.0.0.1',
               'GOTC_PORT': '0', 'GOTC_BASE_PATH': '/gotc', 'GOTC_PUBLIC_ORIGIN': origin}
        seed = """
import json, os
from backend.gotc.service import EventService
service = EventService(data_dir=os.environ['GOTC_DATA_DIR'], origin=os.environ['GOTC_PUBLIC_ORIGIN'])
with service.db() as db:
    team_id = 'T-tar-synthetic'
    db.execute("INSERT INTO teams VALUES (?,?,?,?,?,?)", (team_id, 1, 'Synthetic Team', '', '', 1))
    account, team_code = service.create_account(db, 'team', 'Synthetic Team')
    db.execute('INSERT INTO team_accounts VALUES (?,?)', (account['id'], team_id))
    _, judge_code = service.create_account(db, 'judge', 'Synthetic Judge')
    db.execute("UPDATE settings SET value='true' WHERE key IN ('submissionsOpen','scoringOpen')")
print(json.dumps([team_id, team_code, judge_code]))
"""
        team_id, team_code, judge_code = json.loads(subprocess.check_output(
            [sys.executable, '-c', seed], cwd=extract, env=env, text=True, timeout=15))
        with subprocess.Popen([sys.executable, '-m', 'backend.gotc'], cwd=extract, env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as server:
            try:
                with selectors.DefaultSelector() as selector:
                    selector.register(server.stdout, selectors.EVENT_READ)
                    self.assertTrue(selector.select(15), 'Timed out waiting for packaged app')
                ready = server.stdout.readline()
                if not ready and server.poll() is not None:
                    self.fail('Packaged app exited: ' + server.stderr.read()[-1200:])
                match = re.search(r'GOTC service ready on (127\.0\.0\.1:\d+)/gotc/', ready)
                self.assertIsNotNone(match, ready)
                base = 'http://' + match.group(1) + '/gotc/'
                self.exercise_packaged_service(base, origin, team_id, team_code, judge_code)
            finally:
                server.terminate()
                server.wait(timeout=5)

    def exercise_packaged_service(self, base, origin, team_id, team_code, judge_code):
        def request(route, body=None, cookie=''):
            payload = None if body is None else json.dumps(body).encode()
            headers = {'Origin': origin, 'Content-Type': 'application/json', 'Cookie': cookie}
            return urllib.request.urlopen(urllib.request.Request(base + route, payload, headers), timeout=5)

        def api(route, body=None, cookie=''):
            with request(route, body, cookie) as response:
                return json.load(response), response.headers

        self.assertEqual(install.health(base + 'api/registration'), {'registeredTeams': 0})
        with self.assertRaises(urllib.error.HTTPError) as private_board:
            request('api/board')
        self.assertEqual(private_board.exception.code, 401)
        private_board.exception.close()
        with request('index.html') as static:
            self.assertIn(b'<!doctype html', static.read(200).lower())
        with request('static/platform/team/draft-store.js') as helper:
            self.assertIn(b'GotcDraftRecovery', helper.read())
        with self.assertRaises(urllib.error.HTTPError) as denied:
            request('api/team')
        self.assertEqual(denied.exception.code, 401)
        denied.exception.close()

        _, login = api('api/login', {'role': 'team', 'code': team_code})
        cookie = login['Set-Cookie']
        team, _ = api('api/team', cookie=cookie)
        self.assertEqual(team['submission']['version'], 0)
        materials = {'projectName': 'Tar Synthetic', 'summary': 'Tar test', 'codeUrl': 'https://example.test/code',
                     'readmeUrl': 'https://example.test/readme', 'license': 'MIT', 'licenseUrl': '',
                     'demoUrl': 'https://example.test/demo', 'demoInstructions': 'Run',
                     'slidesUrl': 'https://example.test/slides', 'filesUrl': 'https://example.test/files.zip',
                     'videoUrl': 'https://example.test/video',
                     'sourcesAndLimitations': 'Synthetic', 'codeVersion': 'a' * 40, 'confirmed': True}
        saved, _ = api('api/submission', {'expectedVersion': 0, 'status': 'submitted', 'materials': materials}, cookie)
        self.assertEqual(saved['submission']['submitted']['projectName'], materials['projectName'])
        self.assertEqual(saved['submission']['submitted']['codeVersion'], materials['codeVersion'])
        team, _ = api('api/team', cookie=cookie)
        self.assertEqual(team['submission']['submittedVersion'], 1)

        _, login = api('api/login', {'role': 'judge', 'code': judge_code})
        cookie = login['Set-Cookie']
        judge, _ = api('api/judge', cookie=cookie)
        before = next((row for row in judge['myScores'] if row['teamId'] == team_id), {})
        scored, _ = api('api/score', {'teamId': team_id, 'scores': [16, 16, 16, 12, 12, 8],
                                     'feedback': 'Tar synthetic score', 'status': 'submitted',
                                     'expectedRevision': before.get('revision', 0)}, cookie)
        self.assertTrue(scored['ok'])
        self.assertEqual(scored['score']['revision'], 1)
        judge, _ = api('api/judge', cookie=cookie)
        self.assertIn(team_id, [row['teamId'] for row in judge['myScores']])


if __name__ == '__main__':
    unittest.main()
