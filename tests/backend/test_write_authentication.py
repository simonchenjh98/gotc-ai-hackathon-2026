"""Synthetic identity rejections must be resolved using read-only SQL."""
from contextlib import contextmanager
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend.gotc.errors import APIError
from backend.gotc.service import EventService


class WriteAuthenticationTests(unittest.TestCase):
    def test_invalid_credentials_do_not_request_a_writer(self):
        with tempfile.TemporaryDirectory() as directory:
            event = EventService(data_dir=directory)
            (Path(directory) / 'finals-access-code.txt').write_text('12345678')
            statements = []
            original_db = event.db

            @contextmanager
            def traced():
                with original_db() as db:
                    db.set_trace_callback(statements.append)
                    yield db

            with patch.object(event, 'db', traced):
                for path, body in [
                    ('/api/admin/settings', {}), ('/api/team/profile', {}), ('/api/score', {}),
                    ('/api/vote', {}), ('/api/admin/claims/review', {}),
                    ('/api/admin/registration/import', {}), ('/api/registration/save', {'code': '', 'expectedRevision': 0, 'team': {}}),
                    ('/api/admin/roadshow-access', {}), ('/api/roadshow/score', {}),
                    ('/api/submission', {'status': 'draft'}), ('/api/finals/score', {}),
                    ('/api/finals/access', {'code': 'incorrect'}), ('/api/roadshow/login', {'name': 'Unknown'}),
                    ('/api/login', {'role': 'team', 'code': 'invalid'}),
                ]:
                    with self.subTest(path=path):
                        statements.clear()
                        with self.assertRaises(APIError):
                            event.dispatch('POST', path, body, '', 'test-ip')
                        self.assertFalse(any('BEGIN IMMEDIATE' in sql.upper() for sql in statements))
                statements.clear()
                result, _, _ = event.dispatch('POST', '/api/claims/redeem', {'requestId': 'absent', 'secret': 'invalid'}, '', 'test-ip')
                self.assertEqual(result['status'], 'invalid')
                self.assertFalse(any('BEGIN IMMEDIATE' in sql.upper() for sql in statements))
