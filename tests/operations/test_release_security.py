"""Deployment entrypoint rehearsals: temporary paths and fake SSH only."""
from contextlib import ExitStack
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
import subprocess
import tarfile
import time
import zipfile
from urllib.parse import quote, urlsplit
import shutil
import signal
import httpx
from unittest.mock import patch


from ops.deploy import deploy, install, pages


WHEELHOUSE = Path(os.environ.get('WHEELHOUSE', deploy.PROJECT / '.runtime/wheels')).absolute()


class LimitedDeploymentTests(unittest.TestCase):
    def test_only_a_prebuilt_artifact_reaches_the_restricted_account(self):
        from unittest.mock import MagicMock
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / 'release.tar.gz'
            artifact.write_bytes(b'synthetic CI artifact')
            artifact.with_suffix('.gz.sig').write_bytes(b'synthetic detached signature')
            digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
            client = MagicMock()
            stdout = MagicMock()
            stdout.read.return_value = b'{"release":"synthetic"}'
            stdout.channel.recv_exit_status.return_value = 0
            client.exec_command.return_value = (None, stdout, None)
            paramiko = types.SimpleNamespace(SSHClient=lambda: client, RejectPolicy=object)
            with patch.dict(sys.modules, {'paramiko': paramiko}):
                result = deploy.upload_release(artifact, digest, 'e' * 40, 'ci.example.test', Path('known_hosts'))
            self.assertEqual(result['release'], 'synthetic')
            self.assertEqual(client.connect.call_args.kwargs['username'], 'gotc-deploy')
            client.load_host_keys.assert_called_once_with('known_hosts')
            sftp = client.open_sftp.return_value.__enter__.return_value
            remote, mode = sftp.open.call_args_list[0].args
            self.assertRegex(remote, r'^/opt/gotc-hackathon/incoming/[A-Za-z0-9-]+\.tar\.gz$')
            self.assertEqual(mode, 'wx')
            self.assertEqual(sftp.open.call_args_list[1].args, (remote + '.sig', 'wx'))
            self.assertEqual(sftp.open.call_count, 2)
            writes = sftp.open.return_value.__enter__.return_value.write.call_args_list
            self.assertEqual([call.args[0] for call in writes], [artifact.read_bytes(), b'synthetic detached signature'])
            command, = client.exec_command.call_args.args
            self.assertTrue(command.startswith('sudo -n /usr/local/sbin/gotc-install --archive '))
            self.assertIn(digest, command)
            client.close.assert_called_once()

    def test_digest_mismatch_refuses_upload(self):
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / 'release.tar.gz'
            artifact.write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError, 'SHA-256'):
                deploy.upload_release(artifact, '0' * 64, 'e' * 40, 'ci.example.test', Path('known_hosts'))


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
            '--package', str(path), '--source-sha', '0' * 40, '--wheelhouse', str(WHEELHOUSE),
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
        env = {'PATH': os.defpath, 'XDG_DATA_HOME': str(state), 'PYTHON': sys.executable,
               'WHEELHOUSE': str(WHEELHOUSE), 'PIP_NO_INDEX': '1'}
        with subprocess.Popen([shutil.which('make'), 'dev', 'PORT=5274'], env=env, cwd=project,
                              start_new_session=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True) as process:
            try:
                _, errors = process.communicate(timeout=30)
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
        for name in install.FILES | {'ops/deploy/deploy.py', 'ops/deploy/install.py', 'ops/deploy/nginx.py', 'tools/build_frontend.py'}:
            target = project / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(deploy.PROJECT / name, target)
        first = self.package('first.tar.gz', project)
        second = self.package('second.tar.gz', project)
        self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_runtime_wheel_hashes_are_checked_before_packaging(self):
        wheelhouse = self.root / 'wheels'
        wheelhouse.mkdir()
        names = install.locked_wheels((deploy.PROJECT / 'backend/requirements.txt').read_text())
        (wheelhouse / Path(next(iter(names))).name).write_bytes(b'incorrect release wheel')
        with self.assertRaisesRegex(ValueError, 'Wrong SHA-256'):
            deploy.release_blobs(deploy.PROJECT, wheelhouse)

    def test_pages_staging_contains_only_approved_landing_page_files(self):
        staged = self.root / 'pages'
        origin = 'https://platform.example.invalid'
        pages.stage(staged, public_origin=origin)
        names = {path.relative_to(staged).as_posix() for path in staged.rglob('*') if path.is_file()}
        expected = {name.removeprefix('frontend/') for name in install.PAGES_FILES}
        manifest = json.loads((staged / 'asset-manifest.json').read_text())['assets']
        artwork = {name for name in expected if name.startswith('assets/')}
        self.assertEqual(names, (expected - manifest.keys()) | artwork | {item['path'] for item in manifest.values()} | {'.nojekyll', 'asset-manifest.json'})
        self.assertIn('index.html', names)
        self.assertIn('static/registration/directory.js', manifest)
        html = (staged / 'index.html').read_text()
        for attr, route in [('data-api', 'api/registration'), ('data-platform-base', ''),
                            ('href', 'platform.html#team'), ('href', 'teams.html')]:
            self.assertIn(f'{attr}="{origin}/gotc/{route}"', html)
        self.assertNotIn('href="platform.html', html)
        self.assertNotIn('href="teams.html', html)
        self.assertFalse(any(name.endswith('.py') or name.startswith(('backend/', 'ops/', 'tests/')) for name in names))
        with self.assertRaises(FileExistsError):
            pages.stage(staged, public_origin=origin)

    def test_source_manifest_rejects_private_files_and_symlinked_ancestors(self):
        for name in ['backend/gotc/.env', 'backend/gotc/keys.sqlite3', 'frontend/downloads/admin-access.txt',
                     'frontend/assets/unapproved.jpg', 'frontend/../frontend/index.html', 'tests/backend/fixtures/legacy_e456bd3_app.py']:
            with self.subTest(name=name):
                self.assertNotIn(name, install.FILES)
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
            wheels = install.locked_wheels(package.extractfile('backend/requirements.txt').read().decode('utf-8'))
            entries = {entry.name: entry for entry in package.getmembers()}
            assets = json.load(package.extractfile('frontend/asset-manifest.json'))
            self.assertEqual(set(names), install.validate_built_assets(package, entries, assets) | wheels.keys())
            self.assertEqual(names, sorted(set(names)))
            self.assertTrue(all(entry.isfile() for entry in package.getmembers()))
            self.assertIn('backend/gotc/__main__.py', names)
            self.assertIn('ops/backup_event.py', names)
            package.extractall(extract, filter='data')

        # A site-disabled interpreter has zero access to the builder's installed packages.
        bare = "import sys; assert not any('site-packages' in path or 'dist-packages' in path for path in sys.path)"
        subprocess.run([sys.executable, '-IS', '-c', bare], check=True, cwd=extract)
        install.prepare_runtime(extract)
        python = str(extract / '.venv/bin/python')
        isolated = """
from pathlib import Path
from importlib.metadata import distribution
import sys
assert Path(sys.prefix).resolve() == (Path.cwd() / '.venv').resolve()
for line in Path('backend/requirements.txt').read_text().splitlines():
    if line.strip() and not line.startswith('#'):
        name, version = line.split()[0].split('==')
        installed = distribution(name)
        assert installed.version == version and Path(installed.locate_file('')).is_relative_to(sys.prefix), name
"""
        subprocess.run([python, '-I', '-c', isolated], check=True, cwd=extract)
        # Seed local test accounts using only the extracted release.
        origin = 'https://example.invalid'
        env = {'PATH': os.defpath, 'PYTHONDONTWRITEBYTECODE': '1',
               'GOTC_DATA_DIR': str(self.root / 'private-data'), 'GOTC_HOST': '127.0.0.1',
               'GOTC_PORT': '0', 'GOTC_BASE_PATH': '/gotc', 'GOTC_PUBLIC_ORIGIN': origin}
        data = Path(env['GOTC_DATA_DIR'])
        data.mkdir(mode=0o700)
        upstream = data / 'gateway.json'
        upstream.write_text(json.dumps({'openai': {'url': 'https://provider.example.invalid', 'key': 'synthetic',
                                                   'models': {'synthetic-model': [0, 0]}}}))
        upstream.chmod(0o600)
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
            [python, '-EsB', '-c', seed], cwd=extract, env=env, text=True, timeout=15))
        with ExitStack() as stack:
            sockets = Path(stack.enter_context(tempfile.TemporaryDirectory(dir='/tmp', prefix='gotc-sockets-')))
            clients = {}
            for role in ('platform', 'gateway'):
                sock = sockets / (role + '.sock')
                server = subprocess.Popen([python, '-EsBu', '-m', 'backend.gotc', '--service', role, '--uds', str(sock)],
                                          cwd=extract, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
                def stop(process=server):
                    if process.poll() is None:
                        process.terminate()
                    process.wait(timeout=15)
                    process.stderr.close()
                stack.callback(stop)
                client = stack.enter_context(httpx.Client(transport=httpx.HTTPTransport(uds=str(sock)),
                                                           base_url='http://localhost', timeout=5))
                clients[role] = client
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    if server.poll() is not None:
                        self.fail(f'{role} exited: ' + server.stderr.read()[-1000:])
                    try:
                        if sock.exists() and client.get('/readyz', headers={'Host': 'localhost'}).status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    time.sleep(.1)
                else:
                    self.fail(f'{role} failed readiness')
                self.assertEqual(client.get('/livez', headers={'Host': 'localhost'}).status_code, 200)
            self.exercise_packaged_service(clients['platform'], origin, team_id, team_code, judge_code)
            self.assertEqual(clients['gateway'].get('/gotc/v1/models').status_code, 401)
            helper = assets['assets']['static/platform/team/draft-store.js']['path']
            self.assertIn(b'GotcDraftRecovery', (extract / 'frontend' / helper).read_bytes())

    def exercise_packaged_service(self, client, origin, team_id, team_code, judge_code):
        def request(route, body=None, cookie=''):
            return client.request('GET' if body is None else 'POST', '/gotc/' + route,
                                  headers={'Origin': origin, 'Cookie': cookie}, json=body)

        def api(route, body=None, cookie=''):
            response = request(route, body, cookie)
            response.raise_for_status()
            return response.json(), response.headers

        self.assertIsInstance(request('api/registration').json()['teams'], list)
        self.assertEqual(request('api/board').status_code, 401)
        self.assertEqual(request('api/team').status_code, 401)

        identity, login = api('api/login', {'role': 'team', 'code': team_code})
        cookie = login['Set-Cookie'].split(';', 1)[0]
        team, _ = api('api/team', cookie=cookie)
        self.assertEqual(team['submission']['version'], 0)
        project = 'Tar Synthetic'
        def upload(kind, original, contents):
            response = client.post(f'/gotc/api/team/uploads/{kind}', content=contents, headers={
                'Origin': origin, 'Cookie': cookie, 'X-GOTC-Login': identity['loginId'],
                'Content-Type': 'application/octet-stream',
                'X-GOTC-Filename': quote(original), 'X-GOTC-Project': quote(project),
            })
            response.raise_for_status()
            item = response.json()
            self.assertEqual(item['filename'], f'Synthetic Team+{project}{Path(original).suffix}')
            self.assertEqual(item['size'], len(contents))
            self.assertEqual(item['kind'], kind)
            self.assertTrue(item['url'].startswith(origin + '/gotc/materials/'))
            download = client.get(urlsplit(item['url']).path)
            self.assertEqual(download.status_code, 200)
            self.assertEqual(download.content, contents)
            return item['url']

        bundle = io.BytesIO()
        with zipfile.ZipFile(bundle, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('README.md', '# Packaged contestant upload\n')
        slides = upload('slides', 'presentation.pdf', b'%PDF-1.7\nSynthetic release fixture\n%%EOF')
        files = upload('files', 'project.zip', bundle.getvalue())
        materials = {'projectName': 'Tar Synthetic', 'summary': 'Tar test', 'codeUrl': 'https://example.test/code',
                     'readmeUrl': 'https://example.test/readme', 'license': 'MIT', 'licenseUrl': '',
                     'demoUrl': 'https://example.test/demo', 'demoInstructions': 'Run',
                     'slidesUrl': slides, 'filesUrl': files,
                     'videoUrl': 'https://example.test/video',
                     'sourcesAndLimitations': 'Synthetic', 'codeVersion': 'a' * 40, 'confirmed': True}
        saved, _ = api('api/submission', {'expectedVersion': 0, 'status': 'submitted', 'materials': materials}, cookie)
        self.assertEqual(saved['submission']['submitted']['projectName'], materials['projectName'])
        self.assertEqual(saved['submission']['submitted']['codeVersion'], materials['codeVersion'])
        self.assertEqual(saved['submission']['submitted']['slidesUrl'], slides)
        self.assertEqual(saved['submission']['submitted']['filesUrl'], files)
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
