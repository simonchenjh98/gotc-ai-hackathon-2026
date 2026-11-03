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
ExecStart=/usr/bin/python3 server/app.py
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
FILES = {'index.html', 'styles.css', 'app.js', 'platform.html', 'platform.css', 'platform.js', 'platform-admin.js', 'platform-draft-recovery.js', 'favicon.svg'}
ASSETS = {'.webp', '.png', '.jpg', '.jpeg', '.svg', '.ico', '.woff', '.woff2'}


def run(*args, check=True):
    result = subprocess.run(args, text=True, capture_output=True, timeout=45)
    if check and result.returncode:
        # Never emit service journals or environment values in deployment logs.
        raise RuntimeError(f'Command failed ({result.returncode}): {" ".join(args)}')
    return result


def allowed(name):
    parts = Path(name).parts
    if not parts or Path(name).is_absolute() or '..' in parts or any(p.startswith('.') for p in parts):
        return False
    if any(re.search(r'(?:secret|credential|admin-access|production\.env)', p, re.I) for p in parts):
        return False
    suffix = Path(name).suffix.lower()
    return (name in FILES or name == 'scripts/backup_event.py' or
            (parts[0] == 'server' and suffix == '.py' and len(parts) == 2 and not parts[-1].startswith('test')) or
            (parts[0] == 'assets' and suffix in ASSETS) or
            (parts[0] == 'downloads' and suffix in {'.ics', '.txt'}))


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
            raise RuntimeError('Board HTTP health failed')
        data = json.load(response)
    if not isinstance(data.get('teams'), list) or not isinstance(data.get('config'), dict) or not isinstance(data.get('serverTime'), int):
        raise RuntimeError('Board response has an unexpected schema')
    return {'teams': len(data['teams']), 'serverTime': data['serverTime']}


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
    lock = (ROOT / '.deploy.lock').open('a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    run('nginx', '-t')
    run('systemctl', 'is-active', *OLD_SERVICES)
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
        os.chmod(backup / 'event.sqlite3', 0o600)
    release = ROOT / 'releases' / args.release
    release.mkdir(mode=0o755)
    with tarfile.open(archive, 'r:gz') as package:
        members = package.getmembers()
        if len({m.name for m in members}) != len(members) or any(not m.isfile() or not allowed(m.name) for m in members):
            raise RuntimeError('Archive contains a duplicate or non-allowlisted entry')
        if not (FILES | {'server/app.py'}).issubset({m.name for m in members}):
            raise RuntimeError('Release is missing required application files')
        package.extractall(release, members=members, filter='data')
    for file in release.rglob('*'):
        os.chmod(file, 0o755 if file.is_dir() else 0o644)
    # Compile in memory so the immutable release needs no runtime write access.
    for file in (release / 'server').glob('*.py'):
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
                health('http://127.0.0.1:5274/gotc/api/board')
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
                status = health('https://8.141.114.201/gotc/api/board')
                break
            except Exception:
                if attempt == 4:
                    raise
                time.sleep(1)
        run('systemctl', 'is-active', *OLD_SERVICES, 'gotc-hackathon')
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
            if old_enabled:
                run('systemctl', 'enable', 'gotc-hackathon', check=False)
        if errors:
            raise RuntimeError('; '.join(errors)) from None
        raise
    finally:
        lock.close()


if __name__ == '__main__':
    main()
