"""Independent revision migration, backup and rollback checks on synthetic stores."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import sqlite3
import stat
import tempfile
import unittest
from unittest.mock import patch

import app
import write_conflicts
from test_submission_records_release import snapshot


TABLES={'score_write_revisions','settings_write_revisions','write_conflicts_meta'}


class WriteConflictsReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='gotc-revisions-release-')
        self.root=Path(self.tmp.name)

    def tearDown(self):self.tmp.cleanup()

    def service(self):return app.EventService(data_dir=self.root/'private',origin='http://127.0.0.1')

    def legacy(self):
        # The preceding main already migrated admin data and immutable submissions.
        with patch.object(write_conflicts.WriteConflicts,'initialize',return_value=None):service=self.service()
        with service.db() as db:
            account,_=service.create_account(db,'judge','Synthetic existing judge')
            db.execute("INSERT INTO teams VALUES ('Tlegacy',1,'Existing team','Existing project','AI for Science',100)")
            db.execute("INSERT INTO team_details(team_id) VALUES ('Tlegacy')")
            db.execute("INSERT INTO scores VALUES (?, 'Tlegacy','[11,11,11,8.3,8.3,5.4]',550,'Existing feedback','submitted',100)",(account['id'],))
            db.execute("UPDATE settings SET value='true' WHERE key='scoringOpen'")
        with service.db() as db:db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        return service,snapshot(service.db_path)

    def test_fresh_store_and_repeat_start_are_identical_without_upgrade_backup(self):
        service=self.service();before=snapshot(service.db_path)
        self.assertTrue(TABLES.issubset(before['tables']))
        self.assertEqual(dict(before['tables']['settings_write_revisions']),{'scoring':0,'submissions':0,'voting':0})
        self.assertEqual(before['tables']['score_write_revisions'],[])
        for _ in range(2):self.service()
        self.assertEqual(snapshot(service.db_path),before)
        self.assertEqual(list(service.data_dir.glob('before-write-conflicts-*.sqlite3')),[])

    def test_existing_migrated_store_has_independent_private_complete_backup(self):
        old,before=self.legacy()
        self.assertIn('admin_data_meta',before['tables']);self.assertIn('submission_records_meta',before['tables'])
        self.assertTrue(TABLES.isdisjoint(before['tables']))
        upgraded=self.service();after=snapshot(upgraded.db_path)
        for table,rows in before['tables'].items():self.assertEqual(after['tables'][table],rows,table)
        self.assertEqual([row for row in after['schema'] if row[2] in before['tables']],before['schema'])
        self.assertEqual(after['tables']['score_write_revisions'][0][1:],('Tlegacy',1))
        self.assertEqual(dict(after['tables']['write_conflicts_meta']),{'schemaVersion':'1'})
        backups=list(old.data_dir.glob('before-write-conflicts-*.sqlite3'));self.assertEqual(len(backups),1)
        self.assertEqual(snapshot(backups[0]),before)
        self.assertEqual(stat.S_IMODE(backups[0].stat().st_mode),0o600)
        with closing(sqlite3.connect(backups[0])) as db:
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(),[])
        self.service();self.assertEqual(snapshot(upgraded.db_path),after)
        self.assertEqual(len(list(old.data_dir.glob('before-write-conflicts-*.sqlite3'))),1)

    def test_failed_schema_and_seed_roll_back_and_retry_remains_possible(self):
        old,before=self.legacy()
        for statements in [write_conflicts.DDL+['SELECT * FROM nonexistent_revision_migration_table'],
                           write_conflicts.DDL+["CREATE TRIGGER fail_seed BEFORE INSERT ON score_write_revisions BEGIN SELECT RAISE(ABORT,'synthetic seed failure'); END"]]:
            with self.subTest(statement=statements[-1]):
                with patch.object(write_conflicts,'DDL',statements):
                    with self.assertRaises(sqlite3.DatabaseError):self.service()
                self.assertEqual(snapshot(old.db_path),before)
                self.assertTrue(TABLES.isdisjoint(snapshot(old.db_path)['tables']))
        self.service()
        self.assertTrue(TABLES.issubset(snapshot(old.db_path)['tables']))
        backups=list(old.data_dir.glob('before-write-conflicts-*.sqlite3'));self.assertEqual(len(backups),3)
        for path in backups:self.assertEqual(snapshot(path),before)

    def test_backup_failure_stops_before_schema_changes(self):
        old,before=self.legacy()
        with patch('write_conflicts.os.open',side_effect=OSError('synthetic backup failure')):
            with self.assertRaises(OSError):self.service()
        self.assertEqual(snapshot(old.db_path),before)

    def test_concurrent_existing_store_upgrade_seeds_once(self):
        old,before=self.legacy()
        with ThreadPoolExecutor(max_workers=2) as pool:services=list(pool.map(lambda _:self.service(),range(2)))
        after=snapshot(services[0].db_path)
        self.assertEqual(len(after['tables']['score_write_revisions']),1)
        self.assertEqual(len(after['tables']['settings_write_revisions']),3)
        for table,rows in before['tables'].items():self.assertEqual(after['tables'][table],rows,table)
        with services[0].db() as db:self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(),[])


if __name__=='__main__':unittest.main()
