"""Exercise the fixed host installer with synthetic units and a real CI release archive."""
from contextlib import ExitStack, redirect_stdout
import hashlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ops.deploy import deploy, install


def synthetic_signer(directory):
    key = directory / 'synthetic-ed25519'
    subprocess.run(['/usr/bin/ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C', 'synthetic-ci',
                    '-f', str(key)], check=True)
    return key


def signed_statement(key, source_sha, digest):
    return subprocess.run(['/usr/bin/ssh-keygen', '-q', '-Y', 'sign', '-f', str(key),
                           '-n', install.SIGNATURE_NAMESPACE], check=True, capture_output=True,
                          input=install.release_statement(source_sha, digest)).stdout


class InstallerRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        built = tempfile.TemporaryDirectory()
        cls.addClassCleanup(built.cleanup)
        path = Path(built.name) / 'ci.tar.gz'
        wheels = Path(os.environ.get('WHEELHOUSE', deploy.PROJECT / '.runtime/wheels'))
        deploy.write_archive(path, deploy.release_blobs(deploy.PROJECT, wheels))
        cls.payload = path.read_bytes()
        cls.signer = synthetic_signer(Path(built.name))
        cls.digest = hashlib.sha256(cls.payload).hexdigest()
        cls.signature = signed_statement(cls.signer, 'a' * 40, cls.digest)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        fixture = Path(temporary.name).resolve()
        self.root = fixture / 'root'
        self.release = '20261118T000000Z-abcdef123456'
        for name in ('releases/old', 'incoming', 'backups', 'data'):
            (self.root / name).mkdir(parents=True)
        (self.root / '.gotc-owned').write_text('synthetic')
        self.current = self.root / 'current'
        self.old_release = self.root / 'releases/old'
        self.current.symlink_to(self.old_release, target_is_directory=True)
        self.nginx = fixture / 'nginx.conf'
        self.old_nginx = 'server { listen 443 ssl http2; server_name event.example.test; }\n'
        self.nginx.write_text(self.old_nginx)
        self.origin = fixture / 'public-origin'
        self.origin.write_text('https://event.example.test\n')
        self.signers = self.origin.parent / 'ci-release-signers'
        self.signers.write_text(install.CI_SIGNER + ' ' + self.signer.with_suffix('.pub').read_text())
        self.units = fixture / 'units'
        self.units.mkdir()
        for service in install.SERVICES:
            (self.units / (service + '.service')).write_text('old unit ' + service)
        self.archive = self.root / 'incoming' / (self.release + '.tar.gz')
        self.archive.write_bytes(self.payload)
        self.sidecar = self.archive.with_suffix('.gz.sig')
        self.sidecar.write_bytes(self.signature)
        self.calls = []
        self.inactive = {install.LEGACY_SERVICE}
        self.disabled = {install.LEGACY_SERVICE}
        self.health = lambda url, sock=None: None
        self.users = ('gotc-build:x:3100:3100::/nonexistent:/usr/sbin/nologin\n'
                      'gotc-deploy:x:3101:3101::/home/gotc-deploy:/bin/sh\n'
                      'gotc-hackathon:x:3102:3102::/nonexistent:/usr/sbin/nologin')

    def command(self, *args, check=True, **kwargs):
        self.calls.append(args)
        status = 0
        if args[:2] == ('getent', 'passwd'):
            return SimpleNamespace(returncode=0, stdout=self.users)
        if args[:2] == ('systemctl', 'is-active'):
            status = 3 if args[2] in self.inactive else 0
        elif args[:2] == ('systemctl', 'is-enabled'):
            status = 1 if args[2] in self.disabled else 0
        if status and check:
            raise RuntimeError('Synthetic inactive service ' + args[2])
        return SimpleNamespace(returncode=status)

    def install(self, source_sha='a' * 40, digest=None):
        argv = ['install.py', '--archive', str(self.archive), '--sha256', digest or self.digest,
                '--release', self.release, '--source-sha', source_sha]
        with ExitStack() as stack:
            stack.enter_context(patch.multiple(install, ROOT=self.root, NGINX=self.nginx, UNIT_DIR=self.units,
                                               PUBLIC_ORIGIN_FILE=self.origin, OPERATOR_UID=os.getuid(),
                                               run=self.command, health=self.health))
            stack.enter_context(patch.object(install.time, 'sleep'))
            stack.enter_context(patch.object(sys, 'argv', argv))
            stack.enter_context(redirect_stdout(io.StringIO()))
            install.main()

    def assert_restored(self):
        self.assertEqual(self.current.resolve(), self.old_release)
        self.assertEqual(self.nginx.read_text(), self.old_nginx)
        for service in install.SERVICES:
            self.assertEqual((self.units / (service + '.service')).read_text(), 'old unit ' + service)
        self.assertFalse((self.root / 'last-deployment.json').exists())

    def test_normal_isolated_install_checks_both_sockets_and_individual_neighbors(self):
        checked = []
        self.health = lambda url, sock=None: checked.append((url, sock))
        self.install()
        target = self.root / 'releases' / self.release
        self.assertEqual(self.current.resolve(), target)
        memory_limits = {'gotc-platform': '256M', 'gotc-gateway': '384M'}
        for service in install.SERVICES:
            role = service.removeprefix('gotc-')
            unit = (self.units / (service + '.service')).read_text()
            sock = f'/run/{service}/http.sock'
            self.assertIn(f'ExecStart={target}/.venv/bin/python -EsBu -m backend.gotc --service {role} --uds {sock}', unit)
            self.assertIn(f'WorkingDirectory={target}', unit)
            self.assertEqual([line for line in unit.splitlines() if line.startswith('MemoryMax=')],
                             ['MemoryMax=' + memory_limits[service]])
            self.assertIn('http://localhost/readyz', [url for url, endpoint in checked if endpoint == sock])
            for directive in install.HARDENING:
                self.assertIn(directive, unit.splitlines())
        for service in install.OLD_SERVICES:
            self.assertEqual(self.calls.count(('systemctl', 'is-active', service)), 2)
        self.assertIn(('https://event.example.test/gotc/api/registration', None), checked)
        self.assertIn(('chown', 'gotc-build', str(target / '.venv')), self.calls)
        pip_install = next(call for call in self.calls if 'pip' in call and 'install' in call)
        self.assertEqual(pip_install[:4], ('runuser', '-u', 'gotc-build', '--'))

    def test_uploader_and_builder_cannot_share_a_uid_or_login_shell(self):
        original = self.users
        for bad, error in ((original.replace(':3100:', ':3101:'), 'distinct'),
                           (original.replace('/usr/sbin/nologin', '/bin/sh'), 'non-login')):
            with self.subTest(error=error):
                self.users = bad
                with self.assertRaisesRegex(RuntimeError, error):
                    self.install()
                self.assert_restored()
                self.assertFalse((self.root / 'backups' / self.release).exists())

    def test_changed_source_or_replaced_archive_is_rejected_before_host_work(self):
        with self.assertRaisesRegex(RuntimeError, 'signature does not authorize'):
            self.install(source_sha='b' * 40)
        self.archive.write_bytes(b'unreviewed synthetic archive')
        with self.assertRaisesRegex(RuntimeError, 'archive SHA-256 mismatch'):
            self.install()
        with self.assertRaisesRegex(RuntimeError, 'signature does not authorize'):
            self.install(digest=hashlib.sha256(self.archive.read_bytes()).hexdigest())
        self.assertEqual(self.calls, [])
        self.assert_restored()

    def test_untrusted_signer_cannot_authorize_even_an_unchanged_archive(self):
        impostor = synthetic_signer(self.root)
        self.sidecar.write_bytes(signed_statement(impostor, 'a' * 40, self.digest))
        with self.assertRaisesRegex(RuntimeError, 'signature does not authorize'):
            self.install()
        self.assertEqual(self.calls, [])
        self.assert_restored()

    def test_missing_symlinked_signature_or_writable_signer_policy_is_rejected(self):
        self.sidecar.unlink()
        with self.assertRaises(FileNotFoundError):
            self.install()
        self.sidecar.symlink_to(self.archive)
        with self.assertRaises(OSError):
            self.install()
        self.sidecar.unlink()
        self.sidecar.write_bytes(self.signature)
        self.signers.chmod(0o660)
        with self.assertRaisesRegex(RuntimeError, 'protected operator-owned'):
            self.install()
        self.assertEqual(self.calls, [])
        self.assert_restored()

    def test_offline_dependency_failure_keeps_previous_release_and_units(self):
        with patch.object(install, 'prepare_runtime', side_effect=RuntimeError('Synthetic wheel failure')):
            with self.assertRaisesRegex(RuntimeError, 'wheel failure'):
                self.install()
        self.assert_restored()
        self.assertNotIn(('systemctl', 'daemon-reload'), self.calls)

    def test_legacy_settings_require_review_once_per_configuration(self):
        legacy = self.units / (install.LEGACY_SERVICE + '.service')
        legacy.write_text('[Service]\nEnvironment=GOTC_REPO_BACKUP_ENABLED=1\n')
        self.inactive.remove(install.LEGACY_SERVICE)
        with self.assertRaisesRegex(RuntimeError, 'record their reviewed digest') as caught:
            self.install()
        self.assert_restored()
        review = self.origin.parent / 'legacy-dropins.sha256'
        review.write_text(str(caught.exception).split()[-1])
        self.install()
        self.assertEqual(self.current.resolve(), self.root / 'releases' / self.release)

    def test_inactive_neighbor_stops_installation_before_switch(self):
        self.inactive.add('realtalk-api')
        with self.assertRaisesRegex(RuntimeError, 'realtalk-api'):
            self.install()
        self.assert_restored()
        self.assertFalse((self.root / 'backups' / self.release).exists())

    def test_failed_gateway_restores_both_old_units_and_legacy_writer(self):
        self.inactive.clear()
        self.disabled.clear()
        def fail_gateway(url, sock):
            if sock and 'gotc-gateway' in sock:
                raise RuntimeError('gateway failed')
        self.health = fail_gateway
        with self.assertRaisesRegex(RuntimeError, 'health check failed'):
            self.install()
        self.assert_restored()
        self.assertIn(('systemctl', 'stop', install.LEGACY_SERVICE), self.calls)
        self.assertIn(('systemctl', 'restart', install.LEGACY_SERVICE), self.calls)
        for service in install.SERVICES:
            self.assertEqual(self.calls.count(('systemctl', 'restart', service)), 2)

    def test_public_health_failure_restores_prior_activation_state(self):
        self.inactive.add('gotc-gateway')
        self.disabled.add('gotc-gateway')
        def public_failure(url, sock):
            if sock is None:
                raise RuntimeError('public failure')
        self.health = public_failure
        with self.assertRaisesRegex(RuntimeError, 'health check failed'):
            self.install()
        self.assert_restored()
        self.assertIn(('systemctl', 'stop', 'gotc-gateway'), self.calls)
        self.assertIn(('systemctl', 'disable', 'gotc-gateway'), self.calls)

    def test_concurrent_nginx_edit_survives_rollback(self):
        def changed(url, sock):
            if sock is None:
                self.nginx.write_text('concurrent operator change')
                raise RuntimeError('changed')
        self.health = changed
        with self.assertRaises(RuntimeError):
            self.install()
        self.assertEqual(self.current.resolve(), self.old_release)
        self.assertEqual(self.nginx.read_text(), 'concurrent operator change')


if __name__ == '__main__':
    unittest.main()
