"""Build a reproducible release or explicitly upload an existing CI artifact."""
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
import shutil
import subprocess
import sys
import tempfile
import tarfile
import uuid

if __package__:
    from .install import FILES, locked_wheels
else:  # Direct execution: python ops/deploy/deploy.py
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from ops.deploy.install import FILES, locked_wheels

PROJECT = Path(__file__).resolve().parents[2]
REMOTE_ROOT = '/opt/gotc-hackathon'
KNOWN_HOSTS = Path.home() / '.ssh' / 'known_hosts'


def release_sources(project, names):
    """Resolve checked-in inputs without following symlinks in any path component."""
    if project.is_symlink():
        raise RuntimeError(f'Release source directory is a symlink: {project}')
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


def release_blobs(project, wheelhouse):
    # Validate all source paths first, including source asset ancestors.
    sources = release_sources(project, FILES)
    blobs = {path.relative_to(project).as_posix(): path.read_bytes() for path in sources
             if not path.relative_to(project).as_posix().startswith('frontend/')}
    from tools.build_frontend import build_frontend
    public = [path.relative_to(project / 'frontend').as_posix() for path in sources if path.is_relative_to(project / 'frontend')]
    with tempfile.TemporaryDirectory(prefix='gotc-release-') as temporary:
        destination = Path(temporary) / 'frontend'
        build_frontend(project / 'frontend', destination, public)
        for path in sorted(destination.rglob('*')):
            if path.is_file():
                blobs['frontend/' + path.relative_to(destination).as_posix()] = path.read_bytes()
    for name, digest in locked_wheels(blobs['backend/requirements.txt'].decode('utf-8')).items():
        filename = Path(name).name
        wheel, = release_sources(wheelhouse, [filename])
        data = wheel.read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError(f'Wrong SHA-256 for release wheel: {filename}')
        blobs[name] = data
    return sorted(blobs.items())


def write_archive(path, blobs):
    # Fix both tar entries and the gzip header so equal source bytes yield equal archives.
    with path.open('xb') as output:
        with gzip.GzipFile(filename='', mode='wb', fileobj=output, mtime=0, compresslevel=9) as compressed:
            with tarfile.open(fileobj=compressed, mode='w|') as package:
                for name, data in blobs:
                    entry = tarfile.TarInfo(name)
                    entry.size, entry.mode, entry.mtime = len(data), 0o644, 0
                    package.addfile(entry, io.BytesIO(data))


def upload_release(archive, digest, source_sha, host, known_hosts, identity=None):
    """A pre-provisioned non-root account can stage an artifact and invoke one trusted helper."""
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9.:-]*', host):
        raise ValueError('Expected a deployment hostname or IP address')
    if not re.fullmatch(r'[0-9a-f]{64}', digest) or hashlib.sha256(archive.read_bytes()).hexdigest() != digest:
        raise ValueError('Archive does not match the expected CI SHA-256')
    signature = archive.with_suffix(archive.suffix + '.sig')
    if not signature.is_file():
        raise ValueError('A detached signature from the protected CI release job is required beside the archive')
    release = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + source_sha[:12] + '-' + uuid.uuid4().hex[:8]
    remote = f'{REMOTE_ROOT}/incoming/{release}.tar.gz'
    import paramiko
    client = paramiko.SSHClient()
    client.load_host_keys(str(known_hosts))
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    try:
        client.connect(host, username='gotc-deploy', key_filename=str(identity) if identity else None,
                       allow_agent=identity is None, look_for_keys=False, timeout=15)
        with client.open_sftp() as sftp:
            for local, destination in ((archive, remote), (signature, remote + '.sig')):
                with local.open('rb') as source, sftp.open(destination, 'wx') as output:
                    shutil.copyfileobj(source, output)
        command = ['sudo', '-n', '/usr/local/sbin/gotc-install', '--archive', remote, '--sha256', digest,
                   '--release', release, '--source-sha', source_sha]
        _, stdout, _ = client.exec_command(shlex.join(command), timeout=300)
        result = stdout.read()
        if stdout.channel.recv_exit_status():
            raise RuntimeError('Release installer failed; consult the host operator')
        return json.loads(result)
    finally:
        client.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--package', type=Path, metavar='ARCHIVE', help='Build a new local CI artifact')
    action.add_argument('--execute', type=Path, metavar='CI_ARCHIVE', help='Upload this previously built CI archive')
    parser.add_argument('--source-sha', help='Exact source commit (defaults to clean HEAD when building)')
    parser.add_argument('--sha256', help='Expected archive SHA-256 from the CI build')
    parser.add_argument('--host', help='Owner-configured limited deployment host')
    parser.add_argument('--identity', type=Path, help='SSH identity for gotc-deploy (default: SSH agent)')
    parser.add_argument('--known-hosts', type=Path, default=KNOWN_HOSTS)
    parser.add_argument('--wheelhouse', type=Path, default=os.environ.get('WHEELHOUSE'),
                        help='Directory of locked universal wheels (default: .runtime/wheels; run make wheels)')
    args = parser.parse_args()
    source_sha = args.source_sha
    if args.package and source_sha is None:
        source_sha = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=PROJECT, text=True).strip()
        if subprocess.check_output(['git', 'status', '--porcelain'], cwd=PROJECT, text=True).strip():
            parser.error('Commit the source or explicitly identify the source revision for a local rehearsal')
    if not source_sha or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', source_sha):
        parser.error('A full source SHA is required')
    if args.execute:
        if not args.sha256 or not args.host:
            parser.error('--execute needs the CI archive SHA-256 and the deployment hostname')
        print(json.dumps(upload_release(args.execute, args.sha256, source_sha, args.host, args.known_hosts, args.identity)))
        return
    wheelhouse = args.wheelhouse or PROJECT / '.runtime/wheels'
    if args.wheelhouse is None and (PROJECT / '.runtime').is_symlink():
        raise RuntimeError('The wheelhouse must not use a symlinked .runtime directory')
    blobs = release_blobs(PROJECT, wheelhouse)
    write_archive(args.package, blobs)
    print(json.dumps({'packaged': args.package.name, 'files': len(blobs),
                      'archiveSha256': hashlib.sha256(args.package.read_bytes()).hexdigest(),
                      'sourceSha': source_sha, 'remoteChanges': False}))


if __name__ == '__main__':
    main()
