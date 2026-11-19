"""Boot the release units and exercise Git on a disposable GitHub-hosted Linux VM."""
from pathlib import Path
import os
import subprocess
import sys
import tempfile


def main():
    project = Path(__file__).resolve().parents[2]
    if os.environ.get('RUNNER_ENVIRONMENT') != 'github-hosted' or project != Path('/opt/gotc-ci'):
        raise SystemExit('This fixture installs temporary units on the disposable GitHub-hosted runner.')
    sys.path.insert(0, str(project))
    from ops.deploy.install import HARDENING, SERVICES, unit_text

    def sudo(*command):
        subprocess.run(['sudo', '-n', *command], check=True)

    platform = unit_text('gotc-platform', project, 'https://event.example.invalid')
    with tempfile.TemporaryDirectory() as temporary:
        units = []
        for service in SERVICES:
            unit = Path(temporary, service + '.service')
            unit.write_text(unit_text(service, project, 'https://event.example.invalid'))
            units.append(str(unit))
        sudo('systemd-analyze', 'verify', *units)
        sudo('install', '-m', '0644', '-t', '/run/systemd/system', *units)
        sudo('systemctl', 'daemon-reload')
        try:
            sudo('systemctl', 'start', *SERVICES)
            for service in SERVICES:
                # Probe as the proxy group to cover traversal and socket permissions.
                sudo('systemd-run', '--wait', '--collect', '--pipe', '--uid=nobody', '--gid=gotc-proxy',
                     '--working-directory=' + str(project), str(project / '.venv/bin/python'),
                     '-Bc', 'from ops.deploy.install import wait_for_health; import sys; '
                     'wait_for_health("http://localhost/readyz", sys.argv[1])',
                     f'/run/{service}/http.sock')
                sudo('systemctl', 'show', service, '-p', 'ActiveState', '-p', 'MemoryMax',
                     '-p', 'TasksMax', '-p', 'LimitNOFILE')
        finally:
            sudo('systemctl', 'stop', *SERVICES)

    limits = [line for line in platform.splitlines() if line.startswith(('MemoryMax=', 'TasksMax=', 'LimitNOFILE='))]
    sudo('systemd-run', '--wait', '--collect', '--pipe', '--uid=gotc-hackathon', '--gid=gotc-proxy',
         '--working-directory=' + str(project), *(f'--property={p}' for p in (*HARDENING, *limits)),
         str(project / '.venv/bin/python'), '-Bm', 'unittest', 'discover', '-s', 'tests/backend',
         '-p', 'test_repository_backups.py')


if __name__ == '__main__':
    main()
