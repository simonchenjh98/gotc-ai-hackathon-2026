"""Private roadshow-key backup and restore regressions; synthetic data only."""
import hashlib
import importlib.util
import os
from pathlib import Path
import sqlite3
import stat
import unittest

from app import EventService
import test_api as fixtures

ROOT = Path(__file__).resolve().parents[1]


def load_backup():
    spec = importlib.util.spec_from_file_location('roadshow_backup_tool', ROOT / 'scripts' / 'backup_event.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RoadshowBackupTests(unittest.TestCase):
    setUp = fixtures.APITests.setUp
    tearDown = fixtures.APITests.tearDown
    team = fixtures.APITests.team

    def save_names(self, names):
        path = '/api/admin/roadshow-access'
        headers = {'X-GOTC-Login': self.admin.login_id}
        status, before, _ = self.admin.request('GET', path, headers=headers)
        self.assertEqual(status, 200)
        status, data, _ = self.admin.request('POST', path, {
            'expectedRevision': before['revision'], 'runId': before['runId'], 'names': names}, headers=headers)
        self.assertEqual(status, 200, data)
        return data

    def claim(self):
        status, data, _ = self.public.post('/api/roadshow/claim', {'name': 'Backup Judge'})
        self.assertEqual(status, 200, data)
        return data

    def db_only(self, destination):
        destination.parent.mkdir(parents=True)
        source = sqlite3.connect(self.service.db_path)
        target = sqlite3.connect(destination)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()

    def private(self, path):
        if os.name != 'nt':
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_backup_restore_keeps_original_key_private_and_claimed_code_stable(self):
        self.save_names(['Backup Judge'])
        claimed = self.claim()
        original = (self.service.data_dir / 'roadshow-claim-key').read_bytes()
        tool = load_backup()
        backup = self.root / 'backup' / 'event.sqlite3'
        report = tool.copy_database(self.service.db_path, backup)
        self.assertEqual(report['privateFiles'], ['roadshow-claim-key'])
        restored = self.root / 'restored' / 'event.sqlite3'
        tool.copy_database(backup, restored)
        for database in (backup, restored):
            key = database.parent / 'roadshow-claim-key'
            self.assertEqual(key.read_bytes(), original)
            self.private(key)
            self.private(database)
        previous = self.service
        self.service = EventService(data_dir=restored.parent, origin=previous.origin, static_root=previous.root)
        self.server.service = self.service
        self.assertEqual(self.claim(), claimed)
        self.assertEqual(self.public.post('/api/login', {'role': 'judge', 'code': claimed['code']})[0], 200)
        with self.assertRaises(FileExistsError):
            tool.copy_database(backup, restored)

    def test_missing_key_with_any_roster_history_fails_without_creating_replacement(self):
        self.save_names(['Backup Judge'])
        self.claim()
        for removed in (False, True):
            with self.subTest(removed=removed):
                if removed:
                    self.save_names([])
                database = self.root / ('removed' if removed else 'active') / 'event.sqlite3'
                self.db_only(database)
                key = database.parent / 'roadshow-claim-key'
                with self.assertRaisesRegex(RuntimeError, 'original roadshow claim key is missing'):
                    EventService(data_dir=database.parent, origin=self.service.origin, static_root=self.service.root)
                self.assertFalse(key.exists())
                destination = self.root / ('backup-removed' if removed else 'backup-active') / 'event.sqlite3'
                with self.assertRaisesRegex(ValueError, 'original roadshow claim key is required'):
                    load_backup().copy_database(database, destination)
                self.assertFalse(destination.exists())
                self.assertFalse((destination.parent / 'roadshow-claim-key').exists())

    def test_empty_legacy_db_only_restore_can_bind_a_new_key_without_identity_history(self):
        self.team()
        original = (self.service.data_dir / 'roadshow-claim-key').read_bytes()
        database = self.root / 'empty-roster-restore' / 'event.sqlite3'
        self.db_only(database)
        previous = self.service
        self.service = EventService(data_dir=database.parent, origin=previous.origin, static_root=previous.root)
        self.server.service = self.service
        key = (database.parent / 'roadshow-claim-key').read_bytes()
        self.assertNotEqual(key, original)
        with self.service.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM teams').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM roadshow_access_roster').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT key_digest FROM roadshow_access_config').fetchone()[0], hashlib.sha256(key).hexdigest())
        self.save_names(['Backup Judge'])
        claimed = self.claim()
        self.service = EventService(data_dir=database.parent, origin=previous.origin, static_root=previous.root)
        self.server.service = self.service
        self.assertEqual(self.claim(), claimed)

    def test_wrong_source_or_destination_key_is_not_silently_overwritten(self):
        self.save_names(['Backup Judge'])
        tool = load_backup()
        source = self.root / 'wrong-source' / 'event.sqlite3'
        self.db_only(source)
        (source.parent / 'roadshow-claim-key').write_bytes(b'x' * 32)
        target = self.root / 'failed-backup' / 'event.sqlite3'
        with self.assertRaisesRegex(ValueError, 'does not match'):
            tool.copy_database(source, target)
        self.assertFalse(target.exists())
        destination = self.root / 'existing-key' / 'event.sqlite3'
        destination.parent.mkdir()
        key = destination.parent / 'roadshow-claim-key'
        key.write_bytes(b'z' * 32)
        with self.assertRaisesRegex(ValueError, 'Refusing to overwrite'):
            tool.copy_database(self.service.db_path, destination)
        self.assertEqual(key.read_bytes(), b'z' * 32)
        self.assertFalse(destination.exists())


if __name__ == '__main__':
    unittest.main()
