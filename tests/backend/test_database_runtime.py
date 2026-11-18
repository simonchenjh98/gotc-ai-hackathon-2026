"""SQLite leases, startup ownership and cross-version private-data compatibility."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from backend.gotc import database, migrations
from backend.gotc.service import EventService

ROOT = Path(__file__).resolve().parents[2]


class DatabaseRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name) / "private"
        self.data.mkdir()
        self.path = self.data / "event.sqlite3"

    def event(self):
        event = EventService(data_dir=self.data, origin="http://127.0.0.1")
        self.addCleanup(event.connections.close)
        return event

    def test_lease_reuse_nested_rollback_and_pool_bounds(self):
        pool = database.ConnectionPool(self.path, limit=1, timeout=0.1)
        self.addCleanup(pool.close)
        with pool.connection() as db:
            db.execute("CREATE TABLE fixture (label TEXT)")
            with pool.connection() as nested:
                self.assertIs(nested, db)
                nested.execute("INSERT INTO fixture VALUES ('kept')")
            with self.assertRaises(ValueError), pool.connection() as nested:
                nested.execute("INSERT INTO fixture VALUES ('discarded')")
                raise ValueError()
            with ThreadPoolExecutor(1) as workers:
                def waiting_read():
                    with pool.connection():
                        self.fail("A second top-level lease exceeded the pool bound")
                with self.assertRaisesRegex(sqlite3.OperationalError, "pool is busy"):
                    workers.submit(waiting_read).result(2)
        with self.assertRaises(RuntimeError), pool.connection() as lease:
            self.assertIs(lease, db)
            with pool.connection() as nested:
                nested.execute("INSERT INTO fixture VALUES ('outer rollback')")
            raise RuntimeError()
        with pool.connection() as reused:
            self.assertEqual([r[0] for r in reused.execute("SELECT label FROM fixture")], ["kept"])
            self.assertFalse(reused.in_transaction)
            self.assertEqual(reused.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(reused.execute("PRAGMA synchronous").fetchone()[0], 2)
            self.assertEqual(reused.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        pool.close()
        with self.assertRaisesRegex(RuntimeError, "closed"), pool.connection():
            pass

    def test_baseline_unknown_tables_and_old_queries_survive_retry_and_reopen(self):
        # Reconstruct the full unnumbered source layout from the same historical
        # owner initializers used by the release. Include an extra operator table.
        with patch.object(migrations, "apply"):
            old = self.event()
        with old.db() as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 0)
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
            self.assertEqual(tables, migrations.v001_baseline.TABLES)
            db.execute("CREATE TABLE site_extension (label TEXT)")
            db.execute("INSERT INTO site_extension VALUES ('fixture')")
            old_triggers = {r[0] for r in db.execute("SELECT sql FROM sqlite_schema WHERE type='trigger'")}
        old.connections.close()
        # Rehearse restoration to a different private directory before touching the source.
        restore = Path(self.tmp.name) / "rollback"
        shutil.copytree(self.data, restore)
        def failed(db):
            db.execute("CREATE TABLE interrupted (id INTEGER)")
            raise RuntimeError("fixture migration aborted")
        with patch.object(migrations.v002_admin_auth, "apply", failed), self.assertRaisesRegex(RuntimeError, "aborted"):
            self.event()
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertFalse(db.execute("SELECT 1 FROM sqlite_schema WHERE name='interrupted'").fetchone())
        for _ in range(2):
            reopened = self.event()
            with reopened.db() as db:
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], migrations.LATEST)
                self.assertEqual(db.execute("SELECT label FROM site_extension").fetchone()[0], "fixture")
                self.assertEqual({r[0] for r in db.execute("SELECT sql FROM sqlite_schema WHERE type='trigger'")}, old_triggers)
            reopened.connections.close()
        with closing(sqlite3.connect(restore/"event.sqlite3")) as previous:
            self.assertEqual(previous.execute("PRAGMA user_version").fetchone()[0], 0)
            self.assertEqual(previous.execute("SELECT label FROM site_extension").fetchone()[0], "fixture")
            self.assertEqual(previous.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertFalse(previous.execute("SELECT name FROM sqlite_schema WHERE name='named_admins'").fetchone())
            self.assertEqual(previous.execute("SELECT COUNT(*) FROM accounts WHERE role='admin'").fetchone()[0], 1)

    def test_stamped_schema_with_missing_usage_column_stops_before_serving(self):
        self.event().connections.close()
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("ALTER TABLE api_usage DROP COLUMN incomplete")
            db.commit()
        with self.assertRaisesRegex(RuntimeError, "required columns in api_usage: incomplete"):
            self.event()
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], migrations.LATEST)
            self.assertNotIn("incomplete", {r[1] for r in db.execute("PRAGMA table_info(api_usage)")})

    def test_unknown_schema_or_version_fails_before_bootstrapping_accounts(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("CREATE TABLE unrelated (id INTEGER)")
        with self.assertRaisesRegex(RuntimeError, "Unrecognized event database"):
            self.event()
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT name FROM sqlite_schema WHERE type='table'").fetchall(), [("unrelated",)])
            db.execute("PRAGMA user_version=100")
        with self.assertRaisesRegex(RuntimeError, "Unsupported event schema version"):
            self.event()
        self.assertFalse((self.data/"admin-access.txt").exists())

    def test_concurrent_processes_boot_one_fresh_store(self):
        code = (
            "from backend.gotc.service import EventService; import sys; "
            "event=EventService(data_dir=sys.argv[1],origin='http://127.0.0.1'); event.connections.close()"
        )
        with ThreadPoolExecutor(2) as workers:
            results = list(workers.map(lambda _: subprocess.run(
                [sys.executable, "-B", "-c", code, str(self.data)], cwd=ROOT,
                capture_output=True, text=True, timeout=30), range(2)))
        self.assertEqual([r.returncode for r in results], [0, 0], [r.stderr for r in results])
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM accounts WHERE role='admin'").fetchone()[0], 1)
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], migrations.LATEST)
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_maintenance_removes_expired_rows_without_traffic_and_keeps_active_sessions(self):
        event = self.event()
        with event.db() as db:
            account = db.execute("SELECT id FROM accounts WHERE role='admin'").fetchone()[0]
            db.executemany("INSERT INTO sessions VALUES (?, ?, ?)",
                           [("fixture-old", account, 1), ("fixture-current", account, event.clock()+60000)])
        finished = threading.Event()
        run_once = event.database_maintenance.run_once
        def cycle():
            run_once()
            finished.set()
        event.database_maintenance.interval = 0.02
        with patch.object(event.database_maintenance, "run_once", side_effect=cycle):
            event.database_maintenance.start()
            try:
                self.assertTrue(finished.wait(3))
            finally:
                event.database_maintenance.stop()
        with event.db() as db:
            self.assertEqual([r[0] for r in db.execute("SELECT token_hash FROM sessions")], ["fixture-current"])
