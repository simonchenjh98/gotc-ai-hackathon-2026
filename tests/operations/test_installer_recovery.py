"""Rehearse installation and rollback using a disposable tree and synthetic units."""
from contextlib import ExitStack, redirect_stdout
import hashlib
import io
from pathlib import Path
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ops.deploy import install


class InstallerRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        fixture = Path(temporary.name).resolve()
        self.root = fixture / 'root'
        self.release = '20261030T000000Z-abcdef123456'
        for name in ('releases/old', 'incoming', 'backups'):
            (self.root / name).mkdir(parents=True)
        (self.root / '.gotc-owned').write_text('owned')
        self.current = self.root / 'current'
        self.old_release = self.root / 'releases/old'
        self.current.symlink_to(self.old_release, target_is_directory=True)
        self.nginx = fixture / 'nginx'
        self.old_nginx = 'server { listen 443; server_name 8.141.114.201; }\n'
        self.nginx.write_text(self.old_nginx)
        self.unit = fixture / 'unit'
        self.unit.write_text('old unit\n')
        self.archive = self.root / 'incoming/release.tar.gz'
        with tarfile.open(self.archive, 'w:gz') as archive:
            for name in sorted(install.FILES):
                entry = tarfile.TarInfo(name)
                entry.size = 4
                archive.addfile(entry, io.BytesIO(b'x=1\n'))
        self.calls = []
        self.inactive = set()
        self.enabled = True
        self.health = lambda url: {'registeredTeams': 0}

    def command(self, *args, check=True):
        self.calls.append(args)
        status = 0
        if args[:2] == ('systemctl', 'is-active'):
            # systemctl with several units returns success when *any* is active.
            status = 0 if any(unit not in self.inactive for unit in args[2:]) else 3
        if args == ('systemctl', 'is-enabled', 'gotc-hackathon'):
            status = 0 if self.enabled else 1
        if check and status:
            raise RuntimeError('Synthetic inactive unit: ' + ' '.join(args))
        return SimpleNamespace(returncode=status)

    def install(self):
        argv = ['install.py', '--archive', str(self.archive),
                '--sha256', hashlib.sha256(self.archive.read_bytes()).hexdigest(),
                '--release', self.release, '--source-sha', 'a' * 40]
        with ExitStack() as stack:
            stack.enter_context(patch.multiple(install, ROOT=self.root, NGINX=self.nginx, UNIT=self.unit,
                                               run=self.command, health=self.health))
            stack.enter_context(patch.object(install.os, 'geteuid', return_value=0))
            stack.enter_context(patch.object(install.time, 'sleep'))
            stack.enter_context(patch.object(sys, 'argv', argv))
            stack.enter_context(redirect_stdout(io.StringIO()))
            install.main()

    def assert_restored(self):
        self.assertEqual(self.current.resolve(), self.old_release)
        self.assertEqual(self.nginx.read_text(), self.old_nginx)
        self.assertEqual(self.unit.read_text(), 'old unit\n')
        self.assertFalse((self.root / 'last-deployment.json').exists())

    def test_normal_isolated_tree(self):
        self.install()
        self.assertEqual(self.current.resolve(), self.root / 'releases' / self.release)
        self.assertIn('/gotc/', self.nginx.read_text())
        self.assertIn('ExecStart=/usr/bin/python3 -m backend.gotc', self.unit.read_text())
        for name in install.OLD_SERVICES:
            self.assertEqual(self.calls.count(('systemctl', 'is-active', name)), 2)

    def test_preflight_detects_one_inactive_neighbor_before_switch(self):
        self.inactive.add('realtalk-api')
        with self.assertRaisesRegex(RuntimeError, 'realtalk-api'):
            self.install()
        self.assert_restored()
        self.assertNotIn(('systemctl', 'daemon-reload'), self.calls)
        self.assertFalse((self.root / 'backups' / self.release).exists())

    def test_final_check_detects_one_lost_neighbor_and_restores_release(self):
        def lost_neighbor(url):
            self.inactive.add('dapm')
            return {'registeredTeams': 0}
        self.health = lost_neighbor
        with self.assertRaisesRegex(RuntimeError, 'dapm'):
            self.install()
        self.assert_restored()
        self.assertEqual(self.calls.count(('systemctl', 'restart', 'gotc-hackathon')), 2)

    def test_concurrent_nginx_change_is_not_overwritten(self):
        def concurrent_edit(url):
            self.nginx.write_text('concurrent admin change\n')
            raise RuntimeError('Synthetic health failure')
        self.health = concurrent_edit
        with self.assertRaises(RuntimeError):
            self.install()
        self.assertEqual(self.nginx.read_text(), 'concurrent admin change\n')

    def test_external_health_failure_restores_state(self):
        def external_failure(url):
            if url.startswith('https://'):
                raise RuntimeError('Synthetic external health failure')
            return {'registeredTeams': 0}
        self.health = external_failure
        with self.assertRaisesRegex(RuntimeError, 'external health failure'):
            self.install()
        self.assert_restored()

    def test_health_failure_restores_previously_inactive_and_disabled_unit(self):
        self.inactive.add('gotc-hackathon')
        self.enabled = False
        def unavailable(url):
            raise RuntimeError('Synthetic health failure')
        self.health = unavailable
        with self.assertRaisesRegex(RuntimeError, 'local health check'):
            self.install()
        self.assert_restored()
        self.assertIn(('systemctl', 'stop', 'gotc-hackathon'), self.calls)
        self.assertIn(('systemctl', 'disable', 'gotc-hackathon'), self.calls)


if __name__ == '__main__':
    unittest.main()
