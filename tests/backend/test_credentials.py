import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend.gotc.credentials import Credentials
from backend.gotc.service import EventService
from backend.gotc.settings import hsts_allowed, public_origin


class CredentialTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.event = self.root / "private"
        self.mount = self.root / "credentials"
        self.event.mkdir()
        self.mount.mkdir()
        env = patch.dict(os.environ, {"CREDENTIALS_DIRECTORY": str(self.mount)})
        env.start()
        self.addCleanup(env.stop)

    def test_systemd_input_precedence_and_private_state_survives_restart(self):
        (self.event / "shared-login-codes.json").write_text('{"judge":"fixture-local"}')
        (self.mount / "shared-login-codes.json").write_text('{"judge":"fixture-mounted"}')
        (self.mount / "repositories.env").write_text("GOTC_REPO_GITEE_TOKEN='fixture repo'\n")
        (self.mount / "api-key-pepper").write_text("ab" * 32)
        (self.mount / "roadshow-claim-key").write_bytes(b"x" * 32)
        first = EventService(data_dir=self.event)
        self.addCleanup(first.connections.close)
        self.assertEqual(first.shared_login_codes["judge"], "fixture-mounted")
        self.assertEqual(first.repositories.providers["gitee"].token, "fixture repo")
        self.assertEqual(first.keys.pepper, bytes.fromhex("ab" * 32))
        self.assertEqual(first.roadshow_access.key, b"x" * 32)
        self.assertEqual((self.event / "roadshow-claim-key").stat().st_mode & 0o777, 0o600)
        (self.mount / "api-key-pepper").unlink()
        second = EventService(data_dir=self.event)
        self.addCleanup(second.connections.close)
        self.assertEqual(first.keys.pepper, second.keys.pepper)

    def test_mounted_organizer_mapping_never_falls_back_and_rejects_unsafe_files(self):
        name = 'organizer-totp.json'
        local = self.event / name
        local.touch(mode=0o600)
        local.write_text('{"demo":"' + 'A' * 32 + '"}')
        mounted = self.mount / name
        loader = Credentials(self.event)
        self.assertEqual(loader.organizer_seeds(), {})
        mounted.touch(mode=0o600)
        mounted.write_text('{"demo":"' + 'B' * 32 + '"}')
        self.assertTrue(loader.organizer_seeds()['demo'] == 'B' * 32)
        if os.name == 'posix':
            mounted.chmod(0o644)
            with self.assertRaisesRegex(ValueError, 'private'):
                loader.organizer_seeds()
            mounted.chmod(0o600)
        mounted.write_text('{"demo":"SYNTHETIC-INVALID"}')
        with self.assertRaises(ValueError) as failure:
            loader.organizer_seeds()
        self.assertNotIn('SYNTHETIC', str(failure.exception))
        mounted.unlink()
        mounted.symlink_to(local)
        with self.assertRaisesRegex(RuntimeError, "Cannot open"):
            loader.organizer_seeds()

    def test_mismatched_key_and_invalid_config_do_not_disclose_values(self):
        (self.event / "roadshow-claim-key").write_bytes(b"a" * 32)
        (self.mount / "roadshow-claim-key").write_bytes(b"b" * 32)
        loader = Credentials(self.event)
        with self.assertRaisesRegex(RuntimeError, "differs") as error:
            loader.stable_key("roadshow-claim-key", lambda: self.fail(), can_generate=lambda: False)
        self.assertNotIn("bbbb", str(error.exception))
        (self.mount / "gateway.json").write_text("fixture-private-invalid")
        with self.assertRaises(ValueError) as invalid:
            loader.read_json("gateway.json")
        self.assertNotIn("fixture-private", str(invalid.exception))
        self.assertIsNone(invalid.exception.__context__)

    def test_invalid_mounted_key_cannot_poison_an_empty_event(self):
        key = self.mount / "api-key-pepper"
        loader = Credentials(self.event)
        key.write_text("invalid-fixture")
        with self.assertRaises(ValueError):
            loader.stable_key(key.name, lambda: self.fail(), can_generate=lambda: False)
        self.assertFalse((self.event / key.name).exists())
        key.write_text("AB" * 32 + "\n")
        loader.stable_key(key.name, lambda: self.fail(), can_generate=lambda: False)
        self.assertEqual((self.event / key.name).read_bytes(), b"ab" * 32)

    def test_https_origin_is_explicit_for_the_private_socket_deployment(self):
        with patch.dict(os.environ, {"GOTC_PUBLIC_ORIGIN": ""}):
            with self.assertRaises(ValueError):
                public_origin(production=True)
        self.assertEqual(public_origin("https://event.example/", production=True), "https://event.example")
        self.assertTrue(hsts_allowed("https://event.example"))
        self.assertFalse(hsts_allowed("https://127.0.0.1"))
        for value in ("http://event.example", "https://event.example/path", "https://user@event.example"):
            with self.assertRaises(ValueError):
                public_origin(value, production=True)
