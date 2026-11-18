"""Remote GOTC release installer. Invoked by deploy.py, never by the web app."""
from __future__ import annotations

import argparse
import base64
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request

ROOT = Path('/opt/gotc-hackathon')
UNIT_DIR = Path('/etc/systemd/system')
NGINX = Path('/etc/nginx/sites-enabled/realtalk.conf')
PUBLIC_ORIGIN_FILE = Path('/etc/gotc/public-origin')
OLD_SERVICES = ['etown-hackathon', 'nginx', 'realtalk-api', 'dapm']
SERVICES = ('gotc-platform', 'gotc-gateway')
LEGACY_SERVICE = 'gotc-hackathon'
OPERATOR_UID = 0
SIGNATURE_NAMESPACE = 'gotc-ci-release-v1'
CI_SIGNER = 'gotc-ci-main'
HARDENING = (
    'NoNewPrivileges=yes', 'CapabilityBoundingSet=', 'PrivateTmp=yes',
    'ProtectSystem=strict', 'ProtectHome=yes', 'ProtectKernelTunables=yes',
    'ProtectKernelModules=yes', 'ProtectKernelLogs=yes', 'ProtectControlGroups=yes',
    'RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6', 'RestrictNamespaces=yes',
    'LockPersonality=yes', 'SystemCallFilter=~@mount @reboot @swap @raw-io @module',
)
def unit_text(service, release, origin):
    role = service.removeprefix('gotc-')
    sock = f'/run/{service}/http.sock'
    return '\n'.join((
        '[Unit]', f'Description=GOTC {role}', 'After=network-online.target', 'Wants=network-online.target',
        '', '[Service]', 'Type=simple', 'User=gotc-hackathon', 'Group=gotc-proxy',
        f'WorkingDirectory={release}', f'Environment=GOTC_PUBLIC_ORIGIN={origin}',
        f'Environment=GOTC_DATA_DIR={ROOT}/data', 'Environment=GOTC_BASE_PATH=/gotc',
        'EnvironmentFile=-/etc/gotc/event.env',
        f'RuntimeDirectory={service}', 'RuntimeDirectoryMode=0750', 'UMask=0077',
        f'ExecStart={release}/.venv/bin/python -EsBu -m backend.gotc --service {role} --uds {sock}',
        'Restart=on-failure', 'RestartSec=4',
        'MemoryMax=256M' if role == 'platform' else 'MemoryMax=384M',
        'TimeoutStopSec=35' if role == 'platform' else 'TimeoutStopSec=55',
        'TasksMax=80' if role == 'platform' else 'TasksMax=48',
        *HARDENING, f'ReadWritePaths={ROOT}/data', '', '[Install]', 'WantedBy=multi-user.target', '',
    ))


# Release manifests are shared with the builder and embedded in this standalone
# installer, which validates downloaded members before extracting a release.
def manifest(groups):
    return {f'{folder}/{name}' for folder, names in groups.items() for name in names.split()}


PAGES_FILES = manifest({
    'frontend': 'favicon.svg index.html',
    'frontend/assets': 'hero-city.webp registration-qr.jpg schedule-20261106.jpg',
    'frontend/downloads': 'gotc-gitee-submission-guide.pdf gotc-hackathon.ics submission-checklist.txt',
    'frontend/static/registration': 'directory.js registration.css',
    'frontend/static/site': 'app.js header.css header.js styles.css',
})
FILES = PAGES_FILES | manifest({
    'frontend': 'platform.html teams.html',
    'frontend/static/platform': 'navigation.js platform.css route-assets.js routes.js shell.js workspaces.css',
    'frontend/static/platform/admin': 'api.css api.js dashboard.css directory.js event.js settings.js submissions.js',
    'frontend/static/platform/claims': 'application.js form.js receipt.js review.js',
    'frontend/static/platform/finals': 'dashboard.js finals.css judges.js organizer.js shared.js',
    'frontend/static/platform/gateway': 'keys.js logs.js usage.js',
    'frontend/static/platform/judging': 'audience.js boards.js judges.js rules.js',
    'frontend/static/platform/roadshow': 'access.css login.js roster.js scoring.js',
    'frontend/static/platform/submissions': 'history.js materials.js uploads.js',
    'frontend/static/platform/team': 'delegation.js draft-store.js recovery.js repository.js score.js submission.js',
    'frontend/static/registration': 'admin.js profile.js',
    'backend': '__init__.py requirements.txt',
    'backend/gotc': (
        '__init__.py __main__.py admin_auth.py admin_data.py api.py bootstrap.py cli.py constants.py credentials.py '
        'database.py errors.py listener.py observability.py organizer.py service.py settings.py validation.py write_conflicts.py'
    ),
    'backend/gotc/gateway': (
        '__init__.py accounting.py admission.py asgi.py asgi_context.py async_media.py async_tasks.py async_text.py config.py content_store.py gateway_catalog.json gateway_cors.py '
        'gateway_media.py gateway_pricing.py gateway_tasks.py gateway_uploads.py '
        'gateway_websocket.py keys.py memory.py protocol_errors.py requests.py '
        'stream_events.py text_policy.py transport.py usage.py'
    ),
    'backend/gotc/http': '__init__.py app.py policy.py request_models.py routes.py server.py static.py',
    'backend/gotc/migrations': '__init__.py v001_baseline.py v002_admin_auth.py v003_content_store.py',
    'backend/gotc/judging': (
        '__init__.py finals.py judge_login_schedule.py judging.py professional.py '
        'roadshow_access.py roadshow_progress.py roadshow_scoring.py standings.py'
    ),
    'backend/gotc/submissions': (
        '__init__.py material_uploads.py repositories.py repository_backups.py '
        'repository_seals.py submission_records.py workflow.py'
    ),
    'backend/gotc/teams': (
        '__init__.py admin_team_access.py claims.py claims_auto.py team_lifecycle.py '
        'team_registration.py'
    ),
    'ops': 'backup_event.py',
})


def run(*args, check=True, umask=-1, cwd=None):
    result = subprocess.run(args, text=True, capture_output=True, timeout=45, umask=umask, cwd=cwd)
    if check and result.returncode:
        # Never emit service journals or environment values in deployment logs.
        raise RuntimeError(f'Command failed ({result.returncode}): {" ".join(args)}')
    return result


def locked_wheels(requirements):
    """Map pinned, portable wheel archive names to their required hashes."""
    wheels = {}
    for line in requirements.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        match = re.fullmatch(r'([a-z0-9]+(?:[-._][a-z0-9]+)*)==([0-9]+(?:\.[0-9]+)*)\s+--hash=sha256:([0-9a-f]{64})', line)
        if not match:
            raise ValueError('Each runtime requirement needs an exact version and one universal wheel SHA-256')
        package, version, digest = match.groups()
        wheel_name = re.sub('[-.]', '_', package)
        name = f'wheels/{wheel_name}-{version}-py3-none-any.whl'
        if name in wheels:
            raise ValueError(f'Duplicate locked wheel: {name}')
        wheels[name] = digest
    if not wheels:
        raise ValueError('Empty runtime requirements')
    return wheels


def prepare_runtime(release, prefix=()):
    """Create an isolated environment at its final path before switching the unit."""
    environment = release / '.venv'
    # root owns the files, while the dedicated service user must be able to read them.
    run(*prefix, sys.executable, '-I', '-m', 'venv', str(environment), umask=0o022)
    python = str(environment / 'bin/python')
    run(*prefix, python, '-I', '-m', 'pip', '--isolated', '--disable-pip-version-check', 'install',
        '--no-index', '--no-cache-dir', '--only-binary=:all:', '--require-hashes',
        '--find-links', str(release / 'wheels'), '-r', str(release / 'backend/requirements.txt'), umask=0o022)
    run(*prefix, python, '-I', '-m', 'pip', '--isolated', '--disable-pip-version-check', 'check')


# Keep this small helper standalone: the installer runs before release extraction.
def copy_roadshow_claim_key(database, source_directory, destination_directory):
    """Copy the immutable private key only after validating the backed-up database."""
    tables = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    required = 'roadshow_access_roster' in tables and database.execute('SELECT 1 FROM roadshow_access_roster LIMIT 1').fetchone() is not None
    binding = database.execute('SELECT key_digest FROM roadshow_access_config WHERE id=1').fetchone() if 'roadshow_access_config' in tables else None
    source = Path(source_directory) / 'roadshow-claim-key'
    destination = Path(destination_directory) / 'roadshow-claim-key'
    if source.is_symlink():
        raise ValueError('Roadshow claim key must not be a symlink')
    if not source.exists():
        if required:
            raise ValueError('The original roadshow claim key is required for this database')
        return False
    key = source.read_bytes()
    if len(key) != 32 or (binding and hashlib.sha256(key).hexdigest() != binding[0]) or (required and not binding):
        raise ValueError('Roadshow claim key does not match the database')
    if destination.is_symlink():
        raise ValueError('Destination roadshow claim key must not be a symlink')
    try:
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if not destination.is_file() or destination.read_bytes() != key:
            raise ValueError('Refusing to overwrite a different destination roadshow claim key') from None
        os.chmod(destination, 0o600)
    else:
        try:
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(key)
                stream.flush()
                os.fsync(stream.fileno())
        except Exception:
            destination.unlink(missing_ok=True)
            raise
    return True


def atomic_text(path, text, mode=0o644):
    temporary = path.with_name(path.name + '.gotc-pending')
    if temporary.exists() or temporary.is_symlink():
        raise RuntimeError(f'Unexpected temporary file: {temporary}')
    with temporary.open('x', encoding='utf-8') as stream:
        stream.write(text)
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def point_current(target, release_id):
    temporary = ROOT / ('current-' + release_id + '.pending')
    temporary.symlink_to(target, target_is_directory=True)
    os.replace(temporary, ROOT / 'current')


def health(url, sock=None):
    if sock:
        reply = run('curl', '--fail', '--silent', '--max-time', '5', '--unix-socket', sock, url)
        if json.loads(reply.stdout).get('status') != 'ready':
            raise RuntimeError('Service is not ready')
        return
    with urllib.request.urlopen(url, timeout=8) as reply:
        if reply.status != 200 or reply.geturl() != url or not isinstance(json.load(reply).get('teams'), list):
            raise RuntimeError('Public team directory health check failed')


def wait_for_health(url, sock=None, attempts=20):
    for attempt in range(attempts):
        try:
            health(url, sock)
            return
        except (OSError, RuntimeError, ValueError):
            if attempt + 1 == attempts:
                raise RuntimeError('Release health check failed') from None
            time.sleep(1)


def release_statement(source_sha, digest):
    return (f'gotc-ci-release-v1\nref refs/heads/main\nsource-sha {source_sha}\n'
            f'archive-sha256 {digest}\n').encode('ascii')


def verify_ci_signature(archive_path, source_sha, digest):
    """Verify a protected-main build using the operator's independently provisioned key."""
    signers = PUBLIC_ORIGIN_FILE.parent / 'ci-release-signers'
    for path, required_type in ((signers.parent, stat.S_ISDIR), (signers, stat.S_ISREG)):
        metadata = path.lstat()
        if metadata.st_uid != OPERATOR_UID or metadata.st_mode & 0o022 or not required_type(metadata.st_mode):
            raise RuntimeError('CI allowed signers must be a protected operator-owned file and directory')
    signature_path = archive_path.with_suffix(archive_path.suffix + '.sig')
    fd = os.open(signature_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as source:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or not 0 < metadata.st_size <= 16384:
            raise RuntimeError('Expected a bounded regular CI signature')
        signature = source.read(16385)
        if len(signature) != metadata.st_size:
            raise RuntimeError('Incomplete or changing CI signature')
    with tempfile.NamedTemporaryFile(dir=ROOT) as snapshot:
        snapshot.write(signature)
        snapshot.flush()
        checked = subprocess.run(
            ['/usr/bin/ssh-keygen', '-Y', 'verify', '-f', str(signers), '-I', CI_SIGNER,
             '-n', SIGNATURE_NAMESPACE, '-s', snapshot.name],
            input=release_statement(source_sha, digest), capture_output=True, timeout=10,
        )
    if checked.returncode:
        raise RuntimeError('CI signature does not authorize this commit and archive SHA-256')


def main():
    import fcntl
    parser = argparse.ArgumentParser()
    parser.add_argument('--archive', required=True, type=Path)
    parser.add_argument('--sha256', required=True)
    parser.add_argument('--release', required=True)
    parser.add_argument('--source-sha', required=True)
    args = parser.parse_args()
    if os.geteuid() != OPERATOR_UID or ROOT.is_symlink() or not (ROOT / '.gotc-owned').is_file():
        raise RuntimeError('Expected the pre-provisioned GOTC installation and its privileged helper')
    if not re.fullmatch(r'[A-Za-z0-9-]{12,96}', args.release) or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', args.source_sha):
        raise RuntimeError('Invalid release identity')
    if not re.fullmatch(r'[0-9a-f]{64}', args.sha256) or args.archive != ROOT / 'incoming' / (args.release + '.tar.gz'):
        raise RuntimeError('Invalid staged archive or digest')
    with (ROOT / '.deploy.lock').open('a') as lock, tempfile.TemporaryFile(dir=ROOT) as snapshot:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        verify_ci_signature(args.archive, args.source_sha, args.sha256)
        # The upload directory is writable by the deploy account. Validate and install
        # the same private snapshot even if the uploader changes its file concurrently.
        fd = os.open(args.archive, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as source:
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_size > 64 * 1024 * 1024:
                raise RuntimeError('Expected one bounded regular archive')
            remaining = metadata.st_size
            while remaining and (chunk := source.read(min(remaining, 1024 * 1024))):
                snapshot.write(chunk)
                remaining -= len(chunk)
        snapshot.seek(0)
        if hashlib.file_digest(snapshot, 'sha256').hexdigest() != args.sha256:
            raise RuntimeError('CI archive SHA-256 mismatch')
        snapshot.seek(0)
        install_release(args, snapshot)


def extract_release(snapshot, release):
    with tarfile.open(fileobj=snapshot, mode='r:gz') as package:
        entries = {}
        size = 0
        for item in package:
            size += item.size
            if item.name in entries or not item.isfile() or len(entries) >= 1024 or size > 96 * 1024 * 1024:
                raise RuntimeError('Unexpected archive member, size or duplicate')
            entries[item.name] = item
        for required in ('backend/requirements.txt', 'frontend/asset-manifest.json'):
            if required not in entries:
                raise RuntimeError(f'Release is missing {required}')
        lock = package.extractfile(entries['backend/requirements.txt']).read().decode()
        wheels = locked_wheels(lock)
        assets = json.load(package.extractfile(entries['frontend/asset-manifest.json']))
        expected = validate_built_assets(package, entries, assets) | wheels.keys()
        if set(entries) != expected:
            raise RuntimeError('Release is missing required files or includes a non-allowlisted entry')
        package.extractall(release, members=entries.values(), filter='data')
    release.chmod(0o755)
    for path in release.rglob('*'):
        path.chmod(0o755 if path.is_dir() else 0o644)


def validate_built_assets(package, entries, manifest):
    source_assets = {name for name in FILES if name.startswith('frontend/') and name.endswith(('.js', '.css'))}
    expected = FILES - source_assets | {'frontend/asset-manifest.json'}
    assets = manifest.get('assets', {})
    if manifest.get('version') != 1 or set(assets) != {name.removeprefix('frontend/') for name in source_assets}:
        raise RuntimeError('Built frontend asset list differs from the public allowlist')
    for source, record in assets.items():
        stem, ext = source.rsplit('.', 1)
        path = record['path']
        if not re.fullmatch(re.escape(stem) + r'\.[a-f0-9]{16}\.' + ext, path):
            raise RuntimeError('Unexpected generated asset name')
        name = 'frontend/' + path
        data = package.extractfile(entries[name]).read()
        integrity = 'sha384-' + base64.b64encode(hashlib.sha384(data).digest()).decode()
        if path != f'{stem}.{hashlib.sha256(data).hexdigest()[:16]}.{ext}' or record['integrity'] != integrity:
            raise RuntimeError('Generated asset integrity mismatch')
        expected.add(name)
    return expected


def service_state(name):
    return (run('systemctl', 'is-active', name, check=False).returncode == 0,
            run('systemctl', 'is-enabled', name, check=False).returncode == 0)


def verify_legacy_overrides():
    directory = UNIT_DIR / (LEGACY_SERVICE + '.service.d')
    paths = sorted(directory.glob('*.conf'))
    previous = UNIT_DIR / (LEGACY_SERVICE + '.service')
    if previous.is_file():
        known = {'PYTHONDONTWRITEBYTECODE=1', 'PYTHONUNBUFFERED=1', 'GOTC_HOST=127.0.0.1',
                 'GOTC_PORT=5274', f'GOTC_DATA_DIR={ROOT}/data', 'GOTC_BASE_PATH=/gotc'}
        for line in previous.read_text().splitlines():
            key, _, value = line.strip().partition('=')
            if (key in ('EnvironmentFile', 'PassEnvironment', 'LoadCredential', 'LoadCredentialEncrypted',
                        'SetCredential', 'SetCredentialEncrypted')
                    or (key == 'Environment' and value not in known
                        and not re.fullmatch(r'GOTC_PUBLIC_ORIGIN=https://[A-Za-z0-9.-]+(?::[0-9]+)?', value))):
                paths.append(previous)
                break
    if not paths:
        return
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.name.encode() + b'\0' + hashlib.sha256(path.read_bytes()).digest())
    review = PUBLIC_ORIGIN_FILE.parent / 'legacy-dropins.sha256'
    if not review.is_file() or review.read_text().strip() != digest.hexdigest():
        raise RuntimeError('Map the legacy GOTC settings into the successor units and record their reviewed digest: ' +
                           digest.hexdigest())


def verify_runtime_accounts():
    names = {'gotc-build', 'gotc-deploy', 'gotc-hackathon'}
    output = run('getent', 'passwd', *sorted(names)).stdout
    entries = {row[0]: row for line in output.splitlines() if len(row := line.split(':')) == 7}
    if set(entries) != names or len({int(row[2]) for row in entries.values()} - {0}) != len(names):
        raise RuntimeError('Provision distinct, non-root GOTC build, upload and service identities')
    if entries['gotc-build'][-1] not in ('/sbin/nologin', '/usr/sbin/nologin', '/bin/false', '/usr/bin/false'):
        raise RuntimeError('The GOTC runtime builder must have a non-login shell')
    run('getent', 'group', 'gotc-proxy')


def install_release(args, snapshot):
    from ops.deploy.nginx import nginx_candidate

    origin = PUBLIC_ORIGIN_FILE.read_text().strip()
    if not re.fullmatch(r'https://(?:[a-zA-Z0-9.-]+|\[[a-fA-F0-9:]+\])(?::[0-9]{1,5})?', origin):
        raise RuntimeError('Configure /etc/gotc/public-origin with the public HTTPS origin')
    options = PUBLIC_ORIGIN_FILE.with_name('event.env')
    if options.exists() and re.search(r'^\s*GOTC_PUBLIC_ORIGIN\s*=', options.read_text(), re.MULTILINE):
        raise RuntimeError('Configure the origin only in /etc/gotc/public-origin')
    run('nginx', '-t')
    for service in OLD_SERVICES:
        run('systemctl', 'is-active', service)
    verify_runtime_accounts()
    nginx_path = NGINX.resolve(strict=True)
    old_nginx = nginx_path.read_text()
    new_nginx = nginx_candidate(old_nginx, origin)
    current = ROOT / 'current'
    if current.exists() and not current.is_symlink():
        raise RuntimeError('GOTC current must be a symlink')
    old_current = current.resolve() if current.is_symlink() else None
    if old_current and not old_current.is_relative_to(ROOT / 'releases'):
        raise RuntimeError('Existing GOTC release is outside its directory')
    unit_paths = {name: UNIT_DIR / f'{name}.service' for name in SERVICES}
    units = {name: path.read_text() if path.is_file() else None for name, path in unit_paths.items()}
    states = {name: service_state(name) for name in (*SERVICES, LEGACY_SERVICE)}
    # Carry an explicitly reviewed migration across repeat releases. Hash only the old
    # drop-in metadata/content, never copy private directives into a generated service.
    if states[LEGACY_SERVICE][0] or states[LEGACY_SERVICE][1]:
        verify_legacy_overrides()
    # Root-owned parent directories and the private data directory are pre-provisioned.
    data = ROOT / 'data'
    if data.is_symlink() or not data.is_dir():
        raise RuntimeError('Expected the provisioned private data directory')
    backup = ROOT / 'backups' / args.release
    backup.mkdir(mode=0o700)
    (backup / 'nginx.conf').write_text(old_nginx)
    for name, original in units.items():
        if original is not None:
            (backup / f'{name}.service').write_text(original)
    if (data / 'event.sqlite3').is_file():
        with closing(sqlite3.connect(data / 'event.sqlite3')) as source, closing(sqlite3.connect(backup / 'event.sqlite3')) as target:
            source.backup(target)
            copy_roadshow_claim_key(target, data, backup)
        os.chmod(backup / 'event.sqlite3', 0o600)
    release = ROOT / 'releases' / args.release
    release.mkdir(mode=0o755)
    extract_release(snapshot, release)
    # The SSH uploader can write only the inbox. A separate non-login identity prepares
    # signed wheels inside the root-owned release, then relinquishes the environment.
    environment = release / '.venv'
    environment.mkdir(mode=0o755)
    environment.chmod(0o755)
    run('chown', 'gotc-build', str(environment))
    prepare_runtime(release, prefix=('runuser', '-u', 'gotc-build', '--'))
    run('chown', '-hR', 'root:root', '--', str(environment))
    run('chmod', '-R', 'u-s,g-s,go-w', '--', str(environment))
    python = str(environment / 'bin/python')
    run('runuser', '-u', 'gotc-hackathon', '--', python, '-EsB', '-c', 'import backend.gotc.cli', cwd=release)
    touched = []
    switched = nginx_changed = legacy_touched = False
    try:
        for name, path in unit_paths.items():
            atomic_text(path, unit_text(name, release, origin))
            touched.append(name)
        run('systemctl', 'daemon-reload')
        # Stop the old writer first so schema baselining / workers run once per event.
        if states[LEGACY_SERVICE][0]:
            legacy_touched = True
            run('systemctl', 'stop', LEGACY_SERVICE)
        for name in SERVICES:
            if states[name][0]:
                run('systemctl', 'stop', name)
        for name in SERVICES:
            run('systemctl', 'restart', name)
            wait_for_health('http://localhost/readyz', f'/run/{name}/http.sock')
        point_current(release, args.release)
        switched = True
        if nginx_path.read_text() != old_nginx:
            raise RuntimeError('Nginx changed concurrently; refusing to overwrite it')
        atomic_text(nginx_path, new_nginx)
        nginx_changed = True
        run('nginx', '-t')
        run('systemctl', 'reload', 'nginx')
        wait_for_health(origin + '/gotc/api/registration', attempts=5)
        for name in (*OLD_SERVICES, *SERVICES):
            run('systemctl', 'is-active', name)
        run('systemctl', 'enable', *SERVICES)
        if states[LEGACY_SERVICE][1]:
            legacy_touched = True
            run('systemctl', 'disable', LEGACY_SERVICE)
        result = {'release': args.release, 'sourceSha': args.source_sha, 'archiveSha256': args.sha256,
                  'url': origin + '/gotc/', 'deployedAt': int(time.time())}
        atomic_text(ROOT / 'last-deployment.json', json.dumps(result, indent=2) + '\n', 0o600)
        print(json.dumps(result))
    except Exception:
        if nginx_changed and nginx_path.read_text() == new_nginx:
            atomic_text(nginx_path, old_nginx)
            if run('nginx', '-t', check=False).returncode == 0:
                run('systemctl', 'reload', 'nginx', check=False)
        if switched:
            if old_current:
                point_current(old_current, args.release + '-rollback')
            else:
                current.unlink()
        for name in touched:
            run('systemctl', 'stop', name, check=False)
            if units[name] is None:
                run('systemctl', 'disable', name, check=False)
                unit_paths[name].unlink(missing_ok=True)
            else:
                atomic_text(unit_paths[name], units[name])
        run('systemctl', 'daemon-reload', check=False)
        for name in (*touched, *((LEGACY_SERVICE,) if legacy_touched else ())):
            active, enabled = states[name]
            if name == LEGACY_SERVICE or units[name] is not None:
                run('systemctl', 'enable' if enabled else 'disable', name, check=False)
                run('systemctl', 'restart' if active else 'stop', name, check=False)
        raise


if __name__ == '__main__':
    main()
