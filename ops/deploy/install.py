"""Remote GOTC release installer. Invoked by deploy.py, never by the web app."""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import subprocess
import tarfile
import time
import urllib.request

ROOT = Path('/opt/gotc-hackathon')
UNIT = Path('/etc/systemd/system/gotc-hackathon.service')
NGINX = Path('/etc/nginx/sites-enabled/realtalk.conf')
OLD_SERVICES = ['etown-hackathon', 'nginx', 'realtalk-api', 'dapm']
BEGIN = '    # BEGIN GOTC HACKATHON MANAGED LOCATION\n'
END = '    # END GOTC HACKATHON MANAGED LOCATION\n'
LOCATIONS = BEGIN + '''    location = /gotc { return 308 /gotc/; }
    location = /gotc/api/admin/data/import/preview {
        proxy_pass http://127.0.0.1:5274;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 30s;
        proxy_send_timeout 30s;
        client_max_body_size 2m;
    }
    location = /gotc/api/admin/claims/roster {
        proxy_pass http://127.0.0.1:5274;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 30s;
        proxy_send_timeout 30s;
        client_max_body_size 2m;
    }
    location = /gotc/api/admin/claims/review {
        proxy_pass http://127.0.0.1:5274;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 30s;
        proxy_send_timeout 30s;
        client_max_body_size 128k;
    }
    location ^~ /gotc/api/team/uploads/ {
        proxy_pass http://127.0.0.1:5274;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_request_buffering off;
        proxy_read_timeout 330s;
        proxy_send_timeout 330s;
        client_body_timeout 30s;
        client_max_body_size 100m;
    }
    location ^~ /gotc/v1/ {
        # Realtime model sessions (gateway_websocket) need the WebSocket upgrade.
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_pass http://127.0.0.1:5274;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_buffering off;
        # Queue (30s), connection (20s), initial upstream read (1200s), plus error delivery margin.
        proxy_read_timeout 1260s;
        proxy_send_timeout 330s;
        client_max_body_size 32m;
    }
    location ^~ /gotc/ {
        proxy_pass http://127.0.0.1:5274;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 30s;
        proxy_send_timeout 30s;
        client_max_body_size 32k;
    }
''' + END
SERVICE = '''[Unit]
Description=GOTC 2026 event scoring service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=gotc-hackathon
Group=gotc-hackathon
WorkingDirectory=/opt/gotc-hackathon/current
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=PYTHONUNBUFFERED=1
Environment=GOTC_HOST=127.0.0.1
Environment=GOTC_PORT=5274
Environment=GOTC_DATA_DIR=/opt/gotc-hackathon/data
Environment=GOTC_BASE_PATH=/gotc
Environment=GOTC_PUBLIC_ORIGIN=https://8.141.114.201
ExecStart=/usr/bin/python3 -m backend.gotc
Restart=on-failure
RestartSec=4
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/opt/gotc-hackathon/data
UMask=0077
MemoryMax=512M

[Install]
WantedBy=multi-user.target
'''
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
    'frontend/static/platform': 'navigation.js platform.css routes.js shell.js workspaces.css',
    'frontend/static/platform/admin': 'api.css api.js dashboard.css directory.js event.js settings.js submissions.js',
    'frontend/static/platform/claims': 'application.js form.js receipt.js review.js',
    'frontend/static/platform/finals': 'dashboard.js finals.css judges.js organizer.js shared.js',
    'frontend/static/platform/gateway': 'keys.js logs.js usage.js',
    'frontend/static/platform/judging': 'audience.js boards.js judges.js rules.js',
    'frontend/static/platform/roadshow': 'access.css login.js roster.js scoring.js',
    'frontend/static/platform/submissions': 'history.js materials.js uploads.js',
    'frontend/static/platform/team': 'delegation.js draft-store.js recovery.js repository.js score.js submission.js',
    'frontend/static/registration': 'admin.js profile.js',
    'backend': '__init__.py',
    'backend/gotc': (
        '__init__.py __main__.py admin_data.py api.py bootstrap.py cli.py constants.py '
        'database.py errors.py organizer.py service.py validation.py write_conflicts.py'
    ),
    'backend/gotc/gateway': (
        '__init__.py accounting.py config.py gateway_catalog.json gateway_cors.py '
        'gateway_media.py gateway_pricing.py gateway_tasks.py gateway_uploads.py '
        'gateway_websocket.py keys.py protocol_errors.py relay.py requests.py sdk.py '
        'transport.py usage.py'
    ),
    'backend/gotc/http': '__init__.py server.py static.py',
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


def run(*args, check=True):
    result = subprocess.run(args, text=True, capture_output=True, timeout=45)
    if check and result.returncode:
        # Never emit service journals or environment values in deployment logs.
        raise RuntimeError(f'Command failed ({result.returncode}): {" ".join(args)}')
    return result


def allowed(name):
    # Exact canonical POSIX names only. Alternate spellings and private files fail closed.
    return name in FILES


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


def nginx_candidate(source):
    # Tokenize braces without counting braces in comments or quoted strings.
    pattern = r'''#[^\n]*|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[{};]|[^\s{};#"']+'''
    tokens = [m for m in re.finditer(pattern, source) if not m.group().startswith('#')]
    candidates = []
    for i, token in enumerate(tokens[:-1]):
        if token.group() != 'server' or tokens[i + 1].group() != '{':
            continue
        depth = 0
        for closing in tokens[i + 1:]:
            if closing.group() == '{':
                depth += 1
            elif closing.group() == '}':
                depth -= 1
                if depth == 0:
                    start, end = tokens[i + 1].end(), closing.start()
                    body = source[start:end]
                    if re.search(r'\blisten\s+(?:0\.0\.0\.0:)?443(?:\s|;)', body) and re.search(r'\bserver_name\s+[^;]*\b8\.141\.114\.201\b', body):
                        candidates.append((start, end, body))
                    break
    if len(candidates) != 1:
        raise RuntimeError('Expected exactly one IP HTTPS server block; no Nginx changes made')
    start, end, body = candidates[0]
    if BEGIN in body or END in body:
        if body.count(BEGIN) != 1 or body.count(END) != 1:
            raise RuntimeError('Ambiguous existing GOTC location markers')
        begin, finish = body.index(BEGIN), body.index(END) + len(END)
        outside = body[:begin] + body[finish:]
        if re.search(r'\blocation\s+[^\n{]*\/gotc(?:\s|/|\{)', outside):
            raise RuntimeError('Unmanaged GOTC location already exists')
        body = body[:begin] + LOCATIONS + body[finish:]
    else:
        if re.search(r'\blocation\s+[^\n{]*\/gotc(?:\s|/|\{)', source):
            raise RuntimeError('Unmanaged GOTC location already exists')
        body = body.rstrip() + '\n\n' + LOCATIONS
    return source[:start] + body + source[end:]


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


def health(url):
    with urllib.request.urlopen(url, timeout=8) as response:
        if response.status != 200:
            raise RuntimeError('Team directory HTTP health failed')
        data = json.load(response)
    if not isinstance(data, dict) or not isinstance(data.get('teams'), list):
        raise RuntimeError('Team directory response has an unexpected schema')
    return {'registeredTeams': len(data['teams'])}


def main():
    import fcntl
    parser = argparse.ArgumentParser()
    parser.add_argument('--archive', required=True)
    parser.add_argument('--sha256', required=True)
    parser.add_argument('--release', required=True)
    parser.add_argument('--source-sha', required=True)
    args = parser.parse_args()
    if os.geteuid() != 0 or ROOT.is_symlink() or not (ROOT / '.gotc-owned').is_file():
        raise RuntimeError('Expected root and the isolated GOTC ownership marker')
    if not re.fullmatch(r'[A-Za-z0-9-]{12,96}', args.release) or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', args.source_sha):
        raise RuntimeError('Invalid release identity')
    archive = Path(args.archive).resolve()
    if archive.parent != (ROOT / 'incoming').resolve() or hashlib.sha256(archive.read_bytes()).hexdigest() != args.sha256:
        raise RuntimeError('Release archive path or digest mismatch')
    with (ROOT / '.deploy.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        install_release(args, archive)


def install_release(args, archive):
    run('nginx', '-t')
    for service in OLD_SERVICES:
        run('systemctl', 'is-active', service)
    nginx_path = NGINX.resolve(strict=True)
    old_nginx = nginx_path.read_text()
    new_nginx = nginx_candidate(old_nginx)
    old_unit = UNIT.read_text() if UNIT.is_file() else None
    current = ROOT / 'current'
    if current.exists() and not current.is_symlink():
        raise RuntimeError('GOTC current must be a symlink')
    old_current = current.resolve() if current.is_symlink() else None
    if old_current and not old_current.is_relative_to(ROOT / 'releases'):
        raise RuntimeError('Existing GOTC release is outside its directory')
    old_active = run('systemctl', 'is-active', 'gotc-hackathon', check=False).returncode == 0
    old_enabled = run('systemctl', 'is-enabled', 'gotc-hackathon', check=False).returncode == 0
    if not old_current:
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 5274))
    run('getent', 'passwd', 'gotc-hackathon', check=False)
    if run('id', 'gotc-hackathon', check=False).returncode:
        run('useradd', '--system', '--home-dir', str(ROOT), '--shell', '/usr/sbin/nologin', 'gotc-hackathon')
    for directory in ['releases', 'backups']:
        (ROOT / directory).mkdir(exist_ok=True, mode=0o755)
    data = ROOT / 'data'
    data.mkdir(exist_ok=True, mode=0o700)
    if data.is_symlink():
        raise RuntimeError('GOTC data must not be a symlink')
    run('chown', 'gotc-hackathon:gotc-hackathon', str(data))
    os.chmod(data, 0o700)
    backup = ROOT / 'backups' / args.release
    backup.mkdir(mode=0o700)
    (backup / 'nginx.conf').write_text(old_nginx)
    os.chmod(backup / 'nginx.conf', 0o600)
    if old_unit:
        (backup / 'gotc-hackathon.service').write_text(old_unit)
    if (data / 'event.sqlite3').is_file():
        with closing(sqlite3.connect(data / 'event.sqlite3')) as source, closing(sqlite3.connect(backup / 'event.sqlite3')) as target:
            source.backup(target)
            copy_roadshow_claim_key(target, data, backup)
        os.chmod(backup / 'event.sqlite3', 0o600)
    release = ROOT / 'releases' / args.release
    release.mkdir(mode=0o755)
    with tarfile.open(archive, 'r:gz') as package:
        members = package.getmembers()
        if len({m.name for m in members}) != len(members) or any(not m.isfile() or not allowed(m.name) for m in members):
            raise RuntimeError('Archive contains a duplicate or non-allowlisted entry')
        if {m.name for m in members} != FILES:
            raise RuntimeError('Release is missing required application files')
        package.extractall(release, members=members, filter='data')
    for file in release.rglob('*'):
        os.chmod(file, 0o755 if file.is_dir() else 0o644)
    # Compile in memory so the immutable release needs no runtime write access.
    for file in release.rglob('*.py'):
        compile(file.read_text(encoding='utf-8'), str(file), 'exec')
    nginx_changed = False
    switched = False
    unit_changed = False
    try:
        atomic_text(UNIT, SERVICE)
        unit_changed = True
        point_current(release, args.release)
        switched = True
        run('systemctl', 'daemon-reload')
        run('systemctl', 'restart', 'gotc-hackathon')
        for attempt in range(25):
            try:
                health('http://127.0.0.1:5274/gotc/api/registration')
                break
            except Exception:
                if attempt == 24:
                    raise RuntimeError('New GOTC release failed its local health check') from None
                time.sleep(1)
        if nginx_path.read_text() != old_nginx:
            raise RuntimeError('Nginx changed concurrently; refusing to overwrite it')
        atomic_text(nginx_path, new_nginx)
        nginx_changed = True
        run('nginx', '-t')
        run('systemctl', 'reload', 'nginx')
        # systemctl reload returns before Nginx's new workers accept requests.
        # An immediate connection can still reach an old worker's routing table.
        for attempt in range(5):
            try:
                status = health('https://8.141.114.201/gotc/api/registration')
                break
            except Exception:
                if attempt == 4:
                    raise
                time.sleep(1)
        for service in [*OLD_SERVICES, 'gotc-hackathon']:
            run('systemctl', 'is-active', service)
        run('systemctl', 'enable', 'gotc-hackathon')
        metadata = {'release': args.release, 'sourceSha': args.source_sha, 'archiveSha256': args.sha256,
                    'url': 'https://8.141.114.201/gotc/', 'health': status, 'deployedAt': int(time.time())}
        atomic_text(ROOT / 'last-deployment.json', json.dumps(metadata, indent=2) + '\n', 0o600)
        print(json.dumps(metadata))
    except Exception:
        errors = []
        if nginx_changed:
            if nginx_path.read_text() == new_nginx:
                atomic_text(nginx_path, old_nginx)
                if run('nginx', '-t', check=False).returncode == 0:
                    run('systemctl', 'reload', 'nginx', check=False)
            else:
                errors.append('Nginx changed concurrently; saved backup requires review')
        if switched:
            if old_current:
                point_current(old_current, args.release + '-rollback')
            elif current.is_symlink() and current.resolve() == release:
                current.unlink()
        if unit_changed:
            if old_unit is not None:
                atomic_text(UNIT, old_unit)
            else:
                run('systemctl', 'stop', 'gotc-hackathon', check=False)
                run('systemctl', 'disable', 'gotc-hackathon', check=False)
                UNIT.unlink(missing_ok=True)
            run('systemctl', 'daemon-reload', check=False)
            if old_current and old_active:
                run('systemctl', 'restart', 'gotc-hackathon', check=False)
            elif not old_active:
                run('systemctl', 'stop', 'gotc-hackathon', check=False)
            if old_unit is not None:
                run('systemctl', 'enable' if old_enabled else 'disable', 'gotc-hackathon', check=False)
        if errors:
            raise RuntimeError('; '.join(errors)) from None
        raise


if __name__ == '__main__':
    main()
