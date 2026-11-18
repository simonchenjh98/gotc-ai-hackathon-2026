"""Release gate for legacy content. All database paths are fresh temporary fixtures."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import random
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from backend.gotc.migrations import v003_content_store as migration
from backend.gotc.service import EventService
from ops.backup_event import copy_database

MIB = 1 << 20
ROW_BYTES = 2 * MIB
OPTION = "GOTC_LEGACY_GATEWAY_CONTENT"
USAGE = """INSERT INTO api_usage(id,key_id,team_id,at,model,input_tokens,output_tokens,
    cache_write_tokens,cache_read_tokens,cost_micro,status,path,stream,duration_ms,user_agent,request_id)
    VALUES (?,'key','team',?,'synthetic',11,7,0,0,23,200,'/fixture',0,100,'fixture',?)"""


def start(directory):
    return EventService(directory, origin="http://127.0.0.1")


def seed(directory, count, version=2):
    event = start(directory)
    now = event.clock()
    data = random.Random(20261118).randbytes(ROW_BYTES - 8)
    with event.db() as db:
        db.execute("INSERT INTO teams VALUES ('team',1,'Synthetic','','',0)")
        db.execute("INSERT INTO api_keys VALUES ('key','team','fixture','test','mask',1,0,NULL)")
        db.execute("UPDATE settings SET value='true' WHERE key='keys.logContent'")
        for i in range(1, count + 1):
            db.execute(USAGE, (i, now, f"fixture-{i}" if i % 2 else ""))
            db.execute("INSERT INTO api_logs VALUES (?,'{}',?,?,?,8)",
                       (i, data, i.to_bytes(8, "big"), len(data)))
        if version == 0:
            db.execute("DROP TABLE named_admins")
        db.execute(f"PRAGMA user_version={version}")
    event.connections.close()
    (directory / "gateway-content.sqlite3").unlink()
    return hashlib.sha256(data).digest()


def files(directory):
    return [path for path in directory.iterdir() if path.is_file()]


def snapshot(directory):
    return {path.name: (path.stat().st_mode, hashlib.sha256(path.read_bytes()).digest())
            for path in files(directory)}


def assert_result(test, event, count, mode, digest):
    with event.db() as db:
        test.assertEqual(tuple(db.execute("SELECT count(*),sum(cost_micro),sum(input_tokens),sum(output_tokens) FROM api_usage").fetchone()),
                         (count, 23 * count, 11 * count, 7 * count))
        test.assertEqual(db.execute("SELECT count(*) FROM api_logs").fetchone()[0], 0)
        test.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 3)
        test.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
        ids = dict(db.execute("SELECT id,request_id FROM api_usage"))
        test.assertEqual(db.execute("SELECT value FROM settings WHERE key='keys.logContent'").fetchone()[0], 'false')
    with event.keys.ledger.content.connection() as db:
        rows = 0
        for index, request_id, headers, body, reply in db.execute(
                "SELECT usage_id,request_id,headers,request,response FROM content"):
            rows += 1
            test.assertEqual(headers, '{}')
            test.assertEqual(hashlib.sha256(body).digest(), digest)
            test.assertEqual(reply, index.to_bytes(8, "big"))
            test.assertTrue(request_id)
            test.assertEqual(request_id, ids[index])
        test.assertEqual(rows, count if mode == "migrate" else 0)


class ContentMigrationDiskTests(unittest.TestCase):
    def test_operator_and_space_refusals_preserve_every_file(self):
        for choice, free, reason in [(None, 500 * MIB, OPTION), ("invalid", 500 * MIB, OPTION),
                                     ("discard", 0, "free"), ("migrate", 0, "free")]:
            with self.subTest(choice=choice), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp)
                seed(directory, 2)
                (directory / ".schema.lock").unlink()
                (directory / "material-uploads").rmdir()
                directory.chmod(0o750)
                before = snapshot(directory)
                with patch.dict(os.environ, {OPTION: choice or ""}), patch(
                    "shutil.disk_usage", return_value=shutil._ntuple_diskusage(free, 0, free)
                ), self.assertRaisesRegex(RuntimeError, reason):
                    start(directory)
                self.assertEqual(snapshot(directory), before)
                self.assertFalse((directory / "gateway-content.sqlite3").exists())
                self.assertFalse((directory / "material-uploads").exists())
                self.assertEqual(directory.stat().st_mode & 0o777, 0o750)

    def test_one_byte_short_of_reserve_fails_and_exact_estimate_passes(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {OPTION: "migrate"}):
            directory = Path(tmp)
            seed(directory, 3)
            with migration.readonly(directory / "event.sqlite3") as reader:
                reader.row_factory = sqlite3.Row
                amount = migration.preflight(reader, directory, 2, migration.now_ms()).required
            before = snapshot(directory)
            with patch("shutil.disk_usage", return_value=shutil._ntuple_diskusage(amount, 1, amount - 1)):
                with self.assertRaisesRegex(RuntimeError, f"required={amount}"):
                    start(directory)
            self.assertEqual(snapshot(directory), before)
            with patch("shutil.disk_usage", return_value=shutil._ntuple_diskusage(amount, 0, amount)):
                start(directory).connections.close()

    def test_existing_wal_requires_space_to_extend_main_file_before_truncation(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {OPTION: "discard"}):
            directory = Path(tmp)
            seed(directory, 1)
            path = directory / "event.sqlite3"
            with closing(sqlite3.connect(path)) as writer:
                writer.execute("PRAGMA wal_autocheckpoint=0")
                for i in range(2, 35):
                    writer.execute(USAGE, (i, migration.now_ms(), ""))
                    writer.execute("INSERT INTO api_logs VALUES (?,'{}',zeroblob(?),X'',?,0)", (i, ROW_BYTES, ROW_BYTES))
                writer.commit()
                extent = writer.execute("PRAGMA page_count").fetchone()[0] * writer.execute("PRAGMA page_size").fetchone()[0]
                growth = extent - path.stat().st_size
                self.assertGreater(growth, 64 * MIB)
                original_budget = migration.FREE_MARGIN + 20 * MIB
                free = original_budget + growth - 1
                before = snapshot(directory)
                with patch("shutil.disk_usage", return_value=shutil._ntuple_diskusage(free, 0, free)):
                    with self.assertRaisesRegex(RuntimeError, f"checkpoint_growth={growth}"):
                        start(directory)
                self.assertEqual(snapshot(directory), before)

    def test_discard_budgets_preexisting_sidecar_wal_before_opening_it(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {OPTION: "discard"}):
            directory = Path(tmp)
            seed(directory, 1)
            path = directory / "gateway-content.sqlite3"
            with closing(sqlite3.connect(path)) as writer:
                writer.execute("PRAGMA journal_mode=WAL")
                writer.execute("PRAGMA wal_autocheckpoint=0")
                writer.execute("CREATE TABLE fixture (payload BLOB)")
                writer.execute("INSERT INTO fixture VALUES (zeroblob(?))", (32 * MIB,))
                writer.commit()
                growth = writer.execute("PRAGMA page_count").fetchone()[0] * writer.execute("PRAGMA page_size").fetchone()[0] - path.stat().st_size
                self.assertGreater(growth, 31 * MIB)
                free = migration.FREE_MARGIN + 20 * MIB + growth - 1
                before = snapshot(directory)
                with patch("shutil.disk_usage", return_value=shutil._ntuple_diskusage(free, 0, free)):
                    with self.assertRaisesRegex(RuntimeError, f"checkpoint_growth={growth}"):
                        start(directory)
                self.assertEqual(snapshot(directory), before)

    def test_200_mib_peak_including_journals(self):
        for mode in ("migrate", "discard"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp)
                self.assertGreater(shutil.disk_usage(directory).free, 800 * MIB, "fixture needs 800 MiB temporary space")
                digest = seed(directory, 100, version=0)
                with patch.dict(os.environ, {OPTION: mode}), migration.readonly(directory / "event.sqlite3") as db:
                    db.row_factory = sqlite3.Row
                    estimate = migration.preflight(db, directory, 0, migration.now_ms())
                initial = sum(p.stat().st_size for p in files(directory))
                measure_blocks = hasattr(directory.stat(), "st_blocks")
                allocated = sum(getattr(p.stat(), "st_blocks", 0) * 512 for p in files(directory))
                peak = {"total": initial, "allocated": allocated, "wal": 0, "rollback_journal": 0}
                done = threading.Event()

                def sample():
                    total = blocks = wal = 0
                    for path in files(directory):
                        try:
                            info = path.stat()
                            size = info.st_size
                            blocks += getattr(info, "st_blocks", 0) * 512
                        except FileNotFoundError:
                            continue
                        total += size
                        if path.name.endswith("-wal"):
                            wal += size
                        if path.name.endswith("-journal"):
                            peak["rollback_journal"] = max(peak["rollback_journal"], size)
                    peak["total"] = max(peak["total"], total)
                    peak["allocated"] = max(peak["allocated"], blocks)
                    peak["wal"] = max(peak["wal"], wal)

                def monitor():
                    while not done.wait(.001):
                        sample()

                watcher = threading.Thread(target=monitor)
                watcher.start()
                try:
                    with patch.dict(os.environ, {OPTION: mode}):
                        event = start(directory)
                    sample()
                finally:
                    done.set()
                    watcher.join()
                try:
                    assert_result(self, event, 100, mode, digest)
                finally:
                    event.connections.close()
                extra = peak["total"] - initial
                print(json.dumps(dict(mode=mode, content_bytes=100 * ROW_BYTES + 200,
                                      initial_bytes=initial, peak_extra_bytes=extra,
                                      allocated_peak_extra_bytes=peak["allocated"] - allocated if measure_blocks else None,
                                      required_free_bytes=estimate.required, copy_budget_bytes=estimate.to_copy,
                                      journal_budget_bytes=estimate.journals, snapshot_budget_bytes=estimate.snapshots,
                                      peak_wal_bytes=peak["wal"], peak_rollback_journal_bytes=peak["rollback_journal"])))
                bound = (220 * MIB if mode == "migrate" else 0) + 24 * MIB
                self.assertLessEqual(extra, bound)
                self.assertLessEqual(peak["allocated"] - allocated, bound)

    def test_existing_sidecar_and_replayed_old_binary_share_one_quota(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {OPTION: "migrate"}):
            directory = Path(tmp)
            seed(directory, 2)
            event = start(directory)
            now = event.clock()
            with event.db() as db:
                db.execute("UPDATE settings SET value='3' WHERE key='keys.logMaxMB'")
                db.execute("UPDATE settings SET value='true' WHERE key='keys.logContent'")
                db.execute(USAGE, (3, now + 1, "new"))
                db.execute("INSERT INTO api_logs VALUES (3,'{}',zeroblob(?),X'',?,0)", (ROW_BYTES, ROW_BYTES))
            event.connections.close()
            reopened = start(directory)
            with reopened.keys.ledger.content.connection() as side:
                self.assertEqual([row[0] for row in side.execute("SELECT usage_id FROM content")], [3])
            with reopened.db() as db:
                self.assertEqual(db.execute("SELECT count(*) FROM api_usage").fetchone()[0], 3)
                self.assertEqual(db.execute("SELECT value FROM settings WHERE key='keys.logContent'").fetchone()[0], "true")
            reopened.connections.close()

    def test_missing_paired_sidecar_refuses_resume_before_new_files(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {OPTION: "migrate"}):
            directory = Path(tmp)
            seed(directory, 9)
            with patch.object(migration, "_delete_batch", side_effect=RuntimeError("test interruption")):
                with self.assertRaisesRegex(RuntimeError, "test interruption"):
                    start(directory)
            portable = directory / "portable" / "event.sqlite3"
            with self.assertRaisesRegex(ValueError, "content migration"):
                copy_database(directory / "event.sqlite3", portable)
            self.assertFalse(portable.exists())
            (directory / "gateway-content.sqlite3").rename(directory / "misplaced-fixture")
            with self.assertRaisesRegex(RuntimeError, "missing sidecar"):
                start(directory)
            self.assertFalse((directory / "gateway-content.sqlite3").exists())

    def test_process_interruptions_recover_with_the_same_policy(self):
        for mode, stage in (("migrate", "_copy_batch"), ("migrate", "_delete_batch"), ("migrate", "_checkpoint"),
                            ("migrate", "_prune_previous_sidecar"), ("migrate", "mid_copy"), ("discard", "mid_delete")):
            with self.subTest(mode=mode, stage=stage), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp)
                digest = seed(directory, 9)
                env = {**os.environ, OPTION: mode}
                child = subprocess.run([sys.executable, "-B", "-m", "tests.backend.test_content_migration_disk",
                                        stage, str(directory)], env=env, capture_output=True, timeout=30)
                self.assertEqual(child.returncode, 73, child.stderr.decode())
                with migration.readonly(directory / "event.sqlite3") as db:
                    self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
                with patch.dict(os.environ, {OPTION: "discard" if mode == "migrate" else "migrate"}), self.assertRaisesRegex(RuntimeError, mode):
                    start(directory)
                with patch.dict(os.environ, {OPTION: mode}):
                    event = start(directory)
                    assert_result(self, event, 9, mode, digest)
                    event.connections.close()


if __name__ == "__main__":
    stage = sys.argv[1]
    target = {"mid_copy": "_copy_batch", "mid_delete": "_delete_batch"}.get(stage, stage)
    operation = getattr(migration, target)

    def interrupt(db, *args):
        if stage.startswith("mid_"):
            def kill_on_commit(sql):
                if sql == "COMMIT":
                    os._exit(73)
            db.set_trace_callback(kill_on_commit)
        operation(db, *args)
        os._exit(73)

    with patch.object(migration, target, interrupt):
        start(Path(sys.argv[2]))
