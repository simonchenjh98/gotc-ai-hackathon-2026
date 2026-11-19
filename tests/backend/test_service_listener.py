import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from backend.gotc import cli
from backend.gotc.listener import unix_listener


class ProductionEntryTests(unittest.TestCase):
    def test_production_without_a_configured_https_origin_does_not_open_event_data(self):
        with patch.dict(os.environ, {"GOTC_PUBLIC_ORIGIN": ""}), patch.object(cli, "EventService") as service:
            with self.assertRaisesRegex(ValueError, "GOTC_PUBLIC_ORIGIN"):
                cli.main(["--service", "platform", "--uds", "/run/gotc-platform/http.sock"])
            service.assert_not_called()

    @unittest.skipUnless(os.name == "posix", "The production service runs on a Unix host")
    def test_private_listener_preserves_an_existing_file_and_owns_only_one_socket(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            path = Path(directory) / "event.sock"
            path.write_text("unrelated")
            with self.assertRaisesRegex(RuntimeError, "occupied"), unix_listener(path):
                self.fail("A regular file cannot be replaced by a socket")
            self.assertEqual(path.read_text(), "unrelated")
            path.unlink()
            with unix_listener(path) as listener:
                self.assertEqual(listener.family, socket.AF_UNIX)
                self.assertEqual(path.stat().st_mode & 0o777, 0o660)
                with self.assertRaises(BlockingIOError), unix_listener(path):
                    self.fail("Two workers cannot claim the same socket")
            self.assertFalse(path.exists())
