"""Synthetic legacy migration, restoration and scheduled retention regressions."""
import asyncio
from contextlib import contextmanager, closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import tracemalloc
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
import zlib

from backend.gotc.gateway import accounting
from backend.gotc.gateway.asgi_context import GatewayContext
from backend.gotc.gateway.config import DEFAULTS
from backend.gotc.gateway.keys import KeyService
from backend.gotc.gateway.memory import BulkMemory
from backend.gotc.migrations import v003_content_store
from starlette.requests import Request


class CaptureCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_task_cancellation_waits_for_the_single_spooled_writer(self):
        loop = asyncio.get_running_loop()
        entered, release = asyncio.Event(), threading.Event()
        captured = []

        def record(handler, call):
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(3):
                raise AssertionError("Timed out waiting for the synthetic storage barrier")
            with accounting.compressed_request(call) as (encoded, _):
                encoded.seek(0)
                captured.append(zlib.decompress(encoded.read()))
            call["recorded"] = True

        gateway = SimpleNamespace(config={}, error_secrets=lambda: (), memory=BulkMemory(), safe_record=record)
        req = Request({"type": "http", "method": "POST", "scheme": "http", "server": ("localhost", 80),
                       "client": ("127.0.0.1", 1), "path": "/v1/messages", "query_string": b"", "headers": []})
        ctx = GatewayContext(gateway, req, "/v1/messages")
        with tempfile.TemporaryFile() as source:
            source.write(b"sample")
            ctx.call.update(key={"id": "synthetic"}, request_file=source, request_bytes=6)
            async def finalize():
                try:
                    await ctx.record()
                finally:
                    await ctx.record()
                    ctx.close()

            request_task = asyncio.create_task(finalize())
            try:
                await asyncio.wait_for(entered.wait(), 2)
                observer = asyncio.create_task(ctx.record())
                request_task.cancel()
                await asyncio.sleep(0)
                request_task.cancel()
                await asyncio.sleep(0)
                self.assertFalse(request_task.done())
                self.assertFalse(source.closed)
            finally:
                release.set()
            with self.assertRaises(asyncio.CancelledError):
                await request_task
            await observer
            self.assertTrue(source.closed)
            self.assertEqual(captured, [b"sample"])
            self.assertIsNone(gateway.memory.owner)


class RetentionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.event = SimpleNamespace(data_dir=self.root, db=self.db)
        self.ledger = accounting.UsageLedger(self.event, lambda: "CNY")
        self.event.keys = SimpleNamespace(ledger=self.ledger)
        with self.db() as db:
            db.executescript("""
                PRAGMA user_version=2;
                CREATE TABLE settings (key TEXT PRIMARY KEY,value TEXT);
                CREATE TABLE audit (id INTEGER PRIMARY KEY, action TEXT, metadata TEXT);
                CREATE TABLE api_usage (id INTEGER PRIMARY KEY, request_id TEXT, at INTEGER);
                CREATE TABLE api_logs (usage_id INTEGER PRIMARY KEY,headers TEXT,request BLOB,
                                       response BLOB,request_bytes INTEGER,response_bytes INTEGER);
            """)
            db.executemany("INSERT INTO settings VALUES (?,?)", [(k, json.dumps(v)) for k, v in DEFAULTS.items()])

    @contextmanager
    def db(self):
        with closing(sqlite3.connect(self.root / "event.sqlite3")) as db:
            db.row_factory = sqlite3.Row
            with db:
                yield db

    def legacy(self, db, usage_id, at):
        db.execute("INSERT INTO api_usage VALUES (?, ?, ?)", (usage_id, "", at))
        db.execute("INSERT INTO api_logs VALUES (?, '{}', X'01', X'02', 7, 8)", (usage_id,))

    def test_capture_bounds_large_writes_and_counts_the_full_reply(self):
        call = {"log": True, "size": 0, "response": []}
        limit = 3 * accounting.CAPTURE_BLOCK
        part = b"ab" * accounting.CAPTURE_BLOCK
        with patch("backend.gotc.gateway.keys.RESPONSE_LOG_CAP", limit):
            KeyService.keep(None, call, part)
            KeyService.keep(None, call, b"z" * (2 * limit))
            KeyService.keep(None, call, b"x")
        self.assertEqual(call["size"], len(part) + 2 * limit + 1)
        self.assertEqual(sum(map(len, call["response"])), limit)
        captured = zlib.decompress(accounting.compress_capture(call["response"]))
        self.assertEqual(captured, part + b"z" * (limit - len(part)))
        self.assertEqual(call["response"], [])

    def test_spooled_request_rewinds_and_writes_sqlite_without_an_unbounded_read(self):
        payload = b'{"messages":["' + b"synthetic " * 210000 + b'"]}'
        with tempfile.TemporaryFile() as original:
            original.write(payload)
            guarded = MagicMock(wraps=original)
            def bounded_read(size):
                self.assertGreater(size, 0)
                self.assertLessEqual(size, accounting.CAPTURE_BLOCK)
                return original.read(size)
            guarded.read.side_effect = bounded_read
            call = {"request": b"", "request_file": guarded, "request_bytes": len(payload)}
            with accounting.compressed_request(call) as (compressed, length):
                self.assertTrue(original.closed)
                self.assertEqual(length, len(payload))
                with self.ledger.content.connection() as db:
                    self.ledger.content.insert(db, 7, "sample", 10, "{}", compressed, b"", length, 0)
            self.assertTrue(compressed.closed)
            self.assertNotIn("request_file", call)
            self.assertGreater(guarded.read.call_count, 1)
        with self.ledger.content.connection() as db:
            encoded, stored_size = db.execute("SELECT request,request_bytes FROM content WHERE usage_id=7").fetchone()
        self.assertEqual(stored_size, len(payload))
        self.assertEqual(zlib.decompress(encoded), payload)

    def test_failed_optional_write_and_incomplete_spool_release_temporary_files(self):
        with tempfile.TemporaryFile() as original:
            original.write(b"sample")
            call = {"request": b"", "request_file": original, "request_bytes": 6}
            with self.assertRaises(sqlite3.OperationalError):
                with accounting.compressed_request(call) as (encoded, _):
                    raise sqlite3.OperationalError("synthetic content storage unavailable")
            self.assertTrue(original.closed)
            self.assertTrue(encoded.closed)
        with tempfile.TemporaryFile() as short:
            short.write(b"x")
            with self.assertRaisesRegex(OSError, "Incomplete"):
                with accounting.compressed_request({"request_file": short, "request_bytes": 3}):
                    self.fail("an incomplete capture cannot be published")
            self.assertTrue(short.closed)

    def test_small_frames_coalesce_and_compression_uses_bounded_workspace(self):
        call = {"log": True, "size": 0, "response": []}
        frame = b"S" * 32
        for _ in range(65536):
            KeyService.keep(None, call, frame)
        self.assertEqual(call["size"], 2 * 1024 * 1024)
        self.assertEqual(len(call["response"]), 32)
        tracing = tracemalloc.is_tracing()
        if not tracing:
            tracemalloc.start()
        try:
            baseline = tracemalloc.get_traced_memory()[0]
            tracemalloc.reset_peak()
            compressed = accounting.compress_capture(call["response"])
            _, peak = tracemalloc.get_traced_memory()
        finally:
            if not tracing:
                tracemalloc.stop()
        self.assertLess(peak - baseline, 1024 * 1024)
        self.assertEqual(zlib.decompress(compressed), frame * 65536)
        self.assertFalse(call["response"])

    def test_legacy_body_moves_durably_and_default_logging_stops(self):
        self.assertIs(DEFAULTS["keys.logContent"], False)
        with self.db() as db:
            db.execute("UPDATE settings SET value='true' WHERE key='keys.logContent'")
            self.legacy(db, 1, accounting.now_ms())
            self.legacy(db, 2, 1)
        policy = {"GOTC_LEGACY_GATEWAY_CONTENT": "migrate"}
        with patch.dict(os.environ, policy), self.db() as db:
            with patch.object(v003_content_store, "_delete_batch", side_effect=RuntimeError("fixture interruption")):
                with self.assertRaisesRegex(RuntimeError, "fixture interruption"):
                    v003_content_store.apply(db, self.event)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM api_logs").fetchone()[0], 2)
            v003_content_store.apply(db, self.event)
            self.assertEqual(db.execute("SELECT value FROM settings WHERE key='keys.logContent'").fetchone()[0], "false")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM api_usage").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM api_logs").fetchone()[0], 0)
            self.assertEqual(self.ledger.content.detail(db, 1)["response"], b"\x02")
            self.assertIsNone(self.ledger.content.detail(db, 2))
        self.assertEqual(self.ledger.content.summary()["rows"], 1)
        self.assertEqual(self.ledger.content.path.stat().st_mode & 0o777, 0o600)
        with self.db() as restored:
            restored.execute("UPDATE api_usage SET request_id='different-history' WHERE id=1")
            self.assertIsNone(self.ledger.content.detail(restored, 1))

    def test_existing_explicit_organizer_opt_in_is_preserved(self):
        with self.db() as db:
            db.execute("UPDATE settings SET value='true' WHERE key='keys.logContent'")
            db.execute("INSERT INTO audit VALUES (1,'api-key.settings',?)", (json.dumps({"logContent": [False, True]}),))
            db.commit()
            v003_content_store.apply(db, self.event)
            self.assertEqual(db.execute("SELECT value FROM settings WHERE key='keys.logContent'").fetchone()[0], "true")

    def test_migration_only_copies_the_newest_content_that_fits_the_quota(self):
        now = accounting.now_ms()
        with self.db() as db:
            db.execute("UPDATE settings SET value='1' WHERE key='keys.logMaxMB'")
            for identifier in range(1, 5):
                self.legacy(db, identifier, now + identifier)
            db.execute("UPDATE api_logs SET response=zeroblob(400000)")
            db.commit()
            with patch.dict(os.environ, {"GOTC_LEGACY_GATEWAY_CONTENT": "migrate"}):
                v003_content_store.apply(db, self.event)
            with self.ledger.content.connection() as content:
                self.assertEqual([row[0] for row in content.execute("SELECT usage_id FROM content ORDER BY usage_id")], [3, 4])
            self.assertEqual(self.ledger.content.summary()["rows"], 2)
            self.assertEqual(db.execute("SELECT count(*) FROM api_usage").fetchone()[0], 4)

    def test_worker_expires_content_while_the_gateway_is_idle(self):
        with self.db() as db:
            db.execute("INSERT INTO api_usage VALUES (1,'sample',1)")
        with self.ledger.content.connection() as db:
            self.ledger.content.insert(db, 1, "sample", accounting.now_ms(), "{}", b"sample", b"reply", 6, 5)
        with patch.object(accounting, "PRUNE_INTERVAL", 0.03):
            self.ledger.start()
            try:
                with self.ledger.content.connection() as db:
                    db.execute("UPDATE content SET at=1")
                end = time.monotonic() + 2
                while self.ledger.content.summary()["rows"] and time.monotonic() < end:
                    time.sleep(0.01)
            finally:
                self.ledger.stop()
        self.assertEqual(self.ledger.content.summary(), {"rows": 0, "bytes": 0})
        with self.db() as db:
            self.assertEqual(db.execute("SELECT request_id FROM api_usage").fetchone()[0], "sample")
