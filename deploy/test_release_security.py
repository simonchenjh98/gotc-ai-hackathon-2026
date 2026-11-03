"""Deployment entrypoint rehearsals: temporary paths and fake SSH only."""
from contextlib import redirect_stdout
import importlib.util
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
import time
import urllib.request
import urllib.error
import urllib.parse
import shutil
from unittest.mock import patch


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class LocalDeploymentPrivacyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='gotc-deploy-private-')
        self.root = Path(self.temp.name)
        here = Path(__file__).resolve().parent
        self.installer = load_module('gotc_test_installer', here / 'install.py')
        self.deployer = load_module('gotc_test_deployer', here / 'deploy.py')
        self.project = self.root / 'project'
        self.project.mkdir()
        for name in self.installer.FILES:
            (self.project / name).write_text('Synthetic public release file\n')
        (self.project / 'server').mkdir()
        (self.project / 'server' / 'app.py').write_text('# Synthetic package entrypoint\n')
        (self.project / 'deploy').mkdir()
        (self.project / 'deploy' / 'install.py').write_text('# Synthetic installer payload, never executed\n')
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
                patch.dict(sys.modules, {'paramiko': self.paramiko, 'install': self.installer}), \
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
                    import shutil
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

    def test_default_package_extracts_and_real_app_starts_in_new_directory(self):
        project = Path(__file__).resolve().parent.parent
        output = subprocess.check_output([sys.executable, str(project / 'deploy' / 'deploy.py'), '--source-sha', '0' * 64], text=True)
        package_path = Path(json.loads(output)['packaged'])
        extract = self.root / 'empty-release'; extract.mkdir()
        with tarfile.open(package_path) as package:
            names = package.getnames()
            self.assertNotIn('server/data/event.sqlite3', names)
            self.assertFalse(any('test_fixture' in name or '/test_' in name for name in names))
            package.extractall(extract)
        self.assertTrue((extract / 'index.html').is_file())
        self.assertTrue((extract / 'platform-draft-recovery.js').is_file())
        with urllib.request.urlopen('file://' + str(extract / 'index.html')) as page:
            self.assertIn(b'<!doctype html', page.read(200).lower())
        env = os.environ | {'GOTC_DATA_DIR': str(self.root / 'data'), 'GOTC_HOST': '127.0.0.1', 'GOTC_PORT': '55991', 'GOTC_BASE_PATH': '/gotc', 'GOTC_PUBLIC_ORIGIN': 'https://example.invalid'}
        process = subprocess.Popen([sys.executable, '-u', '-B', str(extract / 'server' / 'app.py')], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            ready = process.stdout.readline().strip()
            self.assertIn('GOTC service ready on 127.0.0.1:', ready)
            port = '55991'
            deadline = time.time() + 8
            last_error = None
            while time.time() < deadline:
                try:
                    with urllib.request.urlopen(f'http://127.0.0.1:{port}/gotc/api/board', timeout=0.5) as response:
                        payload = json.load(response)
                        self.assertEqual(response.status, 200)
                        self.assertIn('teams', payload); self.assertIn('config', payload)
                        with urllib.request.urlopen(f'http://127.0.0.1:{port}/gotc/index.html') as static:
                            self.assertEqual(static.status, 200); self.assertIn(b'<!doctype html', static.read(200).lower())
                        with urllib.request.urlopen(f'http://127.0.0.1:{port}/gotc/platform-draft-recovery.js') as helper:
                            self.assertEqual(helper.status, 200); self.assertIn(b'GotcDraftRecovery', helper.read())
                        try: urllib.request.urlopen(f'http://127.0.0.1:{port}/gotc/api/team')
                        except urllib.error.HTTPError as denied: self.assertEqual(denied.code, 401)
                        else: self.fail('unauthenticated team API unexpectedly succeeded')
                        # Exercise authenticated team and legacy judge score paths using synthetic local DB data.
                        # Import the extracted service with the same sibling-module
                        # lookup as its real server/app.py process, not the source tree.
                        with patch.object(sys, 'path', [str(extract / 'server'), *sys.path]):
                            source_app = load_module('gotc_test_extracted_app', extract / 'server' / 'app.py')
                        data = self.root / 'data'
                        service = source_app.EventService(data_dir=data, origin='https://example.invalid')
                        with service.db() as db:
                            team_id = 'T-tar-synthetic'
                            db.execute("INSERT INTO teams VALUES (?,?,?,?,?,?)", (team_id, 1, 'Synthetic Team', '', '', 1))
                            account, team_code = service.create_account(db, 'team', 'Synthetic Team')
                            db.execute('INSERT OR IGNORE INTO team_accounts VALUES (?,?)', (account['id'], team_id))
                            judge, judge_code = service.create_account(db, 'judge', 'Synthetic Judge')
                            db.execute("UPDATE settings SET value='true' WHERE key='submissionsOpen'")
                            db.execute("UPDATE settings SET value='true' WHERE key='scoringOpen'")
                        cookie = urllib.request.urlopen(urllib.request.Request(f'http://127.0.0.1:{port}/gotc/api/login', data=json.dumps({'role':'team','code':team_code}).encode(), headers={'Content-Type':'application/json','Origin':'https://example.invalid'})).headers['Set-Cookie']
                        team_req = urllib.request.Request(f'http://127.0.0.1:{port}/gotc/api/team', headers={'Cookie':cookie})
                        with urllib.request.urlopen(team_req) as team_response:
                            team_payload = json.load(team_response); self.assertEqual(team_response.status, 200)
                            self.assertEqual(team_payload['submission']['version'], 0)
                        def http_json(path, payload, cookie):
                            request = urllib.request.Request(f'http://127.0.0.1:{port}/gotc{path}', data=json.dumps(payload).encode(), headers={'Content-Type':'application/json','Origin':'https://example.invalid','Cookie':cookie})
                            with urllib.request.urlopen(request) as result: return json.load(result), result.headers
                        materials = {'projectName':'Tar Synthetic','summary':'Tar test','codeUrl':'https://example.test/code','readmeUrl':'https://example.test/readme','license':'MIT','licenseUrl':'','demoUrl':'','demoInstructions':'Run','slidesUrl':'https://example.test/slides','videoUrl':'https://example.test/video','sourcesAndLimitations':'Synthetic','codeVersion':'a'*40,'confirmed':True}
                        saved, _ = http_json('/api/submission', {'expectedVersion':0,'status':'submitted','materials':materials}, cookie)
                        self.assertEqual(saved['submission']['submitted']['projectName'], 'Tar Synthetic')
                        self.assertEqual(saved['submission']['submitted']['codeVersion'], 'a'*40)
                        team_req = urllib.request.Request(f'http://127.0.0.1:{port}/gotc/api/team', headers={'Cookie':cookie})
                        with urllib.request.urlopen(team_req) as team_response: self.assertEqual(json.load(team_response)['submission']['submittedVersion'], 1)
                        judge_cookie = urllib.request.urlopen(urllib.request.Request(f'http://127.0.0.1:{port}/gotc/api/login', data=json.dumps({'role':'judge','code':judge_code}).encode(), headers={'Content-Type':'application/json','Origin':'https://example.invalid'})).headers['Set-Cookie']
                        scored, _ = http_json('/api/score', {'teamId':team_id,'scores':[16,16,16,12,12,8],'feedback':'Tar synthetic score','status':'submitted'}, judge_cookie)
                        self.assertEqual(scored['ok'], True)
                        judge_req = urllib.request.Request(f'http://127.0.0.1:{port}/gotc/api/judge', headers={'Cookie':judge_cookie})
                        with urllib.request.urlopen(judge_req) as judge_response:
                            judge_payload = json.load(judge_response); self.assertTrue(any(row['teamId'] == team_id for row in judge_payload['myScores']))
                        break
                except Exception as error:
                    last_error = repr(error); time.sleep(0.1)
            else:
                process.terminate(); process.wait(timeout=5)
                stderr = process.stderr.read()
                self.fail('extracted release did not start a reachable HTTP service: ' + str(last_error) + ' ' + stderr[-1000:])
        finally:
            process.terminate(); process.wait(timeout=5)
            process.stdout.close(); process.stderr.close()


if __name__ == '__main__':
    unittest.main()
