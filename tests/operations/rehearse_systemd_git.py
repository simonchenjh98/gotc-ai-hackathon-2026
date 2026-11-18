"""Linux CI rehearsal of the production service sandbox using synthetic Git repositories."""
from pathlib import Path
import pwd
import os
import subprocess
import sys

if __name__ == '__main__':
    project = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(project))
    from ops.deploy.install import HARDENING

    properties = [f'--property={setting}' for setting in HARDENING]
    subprocess.run([
        'sudo', '-n', 'systemd-run', '--wait', '--collect', '--pipe',
        '--uid=' + pwd.getpwuid(os.getuid()).pw_name, '--working-directory=' + str(project),
        '--property=MemoryMax=256M', '--property=TasksMax=80', *properties,
        str(project / '.runtime/venv/bin/python'), '-B', '-m', 'unittest', 'discover',
        '-s', 'tests/backend', '-p', 'test_repository_backups.py',
    ], check=True)
