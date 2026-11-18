"""Package the public allowlist; --execute explicitly deploys the isolated service.

The existing SSH key is referenced by path only. Admin access is fetched via SFTP
into the ignored .runtime directory and is never printed.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import gzip
import hashlib
import io
import json
import os
import re
from pathlib import Path
import shlex
import subprocess
import tarfile
import uuid

if __package__:
    from .install import FILES
else:  # Direct execution: python ops/deploy/deploy.py
    from install import FILES

PROJECT = Path(__file__).resolve().parents[2]
REMOTE_ROOT = '/opt/gotc-hackathon'
HOST = '8.141.114.201'
KNOWN_HOSTS = Path.home() / '.ssh' / 'known_hosts'
IDENTITY = Path.home() / '.ssh' / 'realtalk_ecs'


def release_sources(project, names):
    """Resolve checked-in inputs without following symlinks in any path component."""
    files = []
    for name in sorted(names):
        path = project
        for part in Path(name).parts:
            path /= part
            if path.is_symlink():
                raise RuntimeError(f'Release entry is a symlink: {name}')
        if not path.is_file():
            raise RuntimeError(f'Required release file is missing: {name}')
        files.append(path)
    return files


def public_files():
    return release_sources(PROJECT, FILES)


def write_archive(path, blobs):
    # Fix both tar entries and the gzip header so equal source bytes yield equal archives.
    with path.open('xb') as output:
        with gzip.GzipFile(filename='', mode='wb', fileobj=output, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode='w|') as package:
                for name, data in blobs:
                    entry = tarfile.TarInfo(name)
                    entry.size, entry.mode, entry.mtime = len(data), 0o644, 0
                    package.addfile(entry, io.BytesIO(data))


def run_remote(client, command):
    _, stdout, stderr = client.exec_command(command, timeout=300)
    output, error = stdout.read().decode(), stderr.read().decode()
    status = stdout.channel.recv_exit_status()
    if status:
        # Installer source never reads access codes into stdout/stderr.
        raise RuntimeError(f'Remote deployment failed ({status}): {error[-5000:]}')
    return output


def private_runtime_directory():
    runtime = PROJECT / '.runtime'
    if runtime.is_symlink() or (runtime.exists() and not runtime.is_dir()):
        raise RuntimeError('The private runtime path must be a real directory')
    runtime.mkdir(exist_ok=True, mode=0o700)
    # An existing directory may have been created with a wider process umask.
    os.chmod(runtime, 0o700)
    return runtime


def receive_admin_access(sftp, runtime):
    private_file = runtime / 'admin-access.txt'
    temporary = runtime / 'admin-access.txt.pending'
    if temporary.exists() or temporary.is_symlink() or private_file.is_symlink():
        raise RuntimeError('Unexpected local admin-access path; deployment succeeded but private retrieval needs review')
    with sftp.open(REMOTE_ROOT + '/data/admin-access.txt', 'rb') as source:
        # Restrict permissions at creation, before the first credential byte is written.
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as output:
            while True:
                block = source.read(8192)
                if not block:
                    break
                output.write(block)
    os.replace(temporary, private_file)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--package', type=Path, metavar='ARCHIVE', help='Write a reproducible source archive at a new local path')
    action.add_argument('--execute', action='store_true', help='Perform the remote deployment (default only packages locally)')
    parser.add_argument('--source-sha', help='Git commit SHA; defaults to HEAD only if this project has its own .git')
    args = parser.parse_args()
    files = public_files()
    content = hashlib.sha256()
    blobs = []
    for path in files:
        name, data = path.relative_to(PROJECT).as_posix(), path.read_bytes()
        content.update(name.encode() + b'\0' + hashlib.sha256(data).digest())
        blobs.append((name, data))
    source_sha = args.source_sha
    if source_sha is None and (PROJECT / '.git').exists():
        source_sha = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=PROJECT, text=True).strip()
        if subprocess.check_output(['git', 'status', '--porcelain'], cwd=PROJECT, text=True).strip():
            raise RuntimeError('Commit the project before deploying so the source SHA identifies this release')
    source_sha = source_sha or content.hexdigest()
    if not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', source_sha):
        raise RuntimeError('source-sha must be a 40 or 64 character lowercase hexadecimal digest')
    if args.package:
        write_archive(args.package, blobs)
        print(json.dumps({'packaged': str(args.package), 'files': len(blobs),
                          'archiveSha256': hashlib.sha256(args.package.read_bytes()).hexdigest(),
                          'sourceSha': source_sha, 'remoteChanges': False}))
        return
    release = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + source_sha[:12] + '-' + uuid.uuid4().hex[:8]
    runtime = private_runtime_directory()
    archive = runtime / ('release-' + release + '.tar.gz')
    write_archive(archive, blobs)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    manifest = {'release': release, 'sourceSha': source_sha, 'contentSha256': content.hexdigest(),
                'archiveSha256': digest, 'fileCount': len(blobs), 'files': [name for name, _ in blobs],
                'url': 'https://8.141.114.201/gotc/', 'executed': False}
    manifest_path = runtime / 'deployment.json'
    manifest_path.write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    if not args.execute:
        print(json.dumps({'packaged': str(archive), 'files': len(blobs), 'archiveSha256': digest,
                          'sourceSha': source_sha, 'remoteChanges': False}))
        return
    import paramiko
    client = paramiko.SSHClient()
    client.load_host_keys(str(KNOWN_HOSTS))
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    client.connect(HOST, username='root', key_filename=str(IDENTITY), look_for_keys=False, allow_agent=False, timeout=15)
    try:
        bootstrap = '''from pathlib import Path
import os
p=Path('/opt/gotc-hackathon')
if p.is_symlink() or (p.exists() and not (p/'.gotc-owned').is_file()):
    raise SystemExit('Unexpected GOTC directory; refusing deployment')
p.mkdir(exist_ok=True,mode=0o755)
m=p/'.gotc-owned'
if not m.exists():
    with m.open('x') as f: f.write('GOTC 2026 isolated deployment\\n')
q=p/'incoming'
if q.is_symlink(): raise SystemExit('Unexpected incoming symlink')
q.mkdir(exist_ok=True,mode=0o700)
os.chmod(q,0o700)
'''
        run_remote(client, 'python3 -c ' + shlex.quote(bootstrap))
        remote_archive = REMOTE_ROOT + '/incoming/' + archive.name
        remote_installer = REMOTE_ROOT + '/incoming/install-' + release + '.py'
        with client.open_sftp() as sftp:
            for local, remote in [(archive, remote_archive), (PROJECT / 'ops' / 'deploy' / 'install.py', remote_installer)]:
                # Exclusive create refuses symlinks and accidental reuse.
                with sftp.open(remote, 'wx') as output:
                    output.write(local.read_bytes())
                sftp.chmod(remote, 0o600)
        command = ['python3', remote_installer, '--archive', remote_archive, '--sha256', digest,
                   '--release', release, '--source-sha', source_sha]
        result = json.loads(run_remote(client, shlex.join(command)).strip())
        # This code is not an output artifact and must remain outside Git.
        with client.open_sftp() as sftp:
            receive_admin_access(sftp, runtime)
        manifest.update(result)
        manifest['executed'] = True
        manifest_path.write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
        print(json.dumps({'deployed': result['url'], 'release': release, 'sourceSha': source_sha,
                          'archiveSha256': digest, 'adminAccessSaved': True, 'oldServicesActive': True}))
    finally:
        client.close()


if __name__ == '__main__':
    main()
