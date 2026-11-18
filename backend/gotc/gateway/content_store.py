"""Private, disposable gateway content. Billable usage belongs to event.sqlite3."""
from contextlib import closing, contextmanager
import json
import os
import sqlite3


class ContentStore:
    def __init__(self, directory):
        self.path = directory / "gateway-content.sqlite3"
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS content (
                    usage_id INTEGER PRIMARY KEY, request_id TEXT NOT NULL, at INTEGER NOT NULL,
                    headers TEXT NOT NULL, request BLOB NOT NULL, response BLOB NOT NULL,
                    request_bytes INTEGER NOT NULL, response_bytes INTEGER NOT NULL,
                    stored_bytes INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS content_by_time ON content(at, usage_id);
                CREATE TABLE IF NOT EXISTS storage (
                    id INTEGER PRIMARY KEY CHECK (id=1), bytes INTEGER NOT NULL
                );
                INSERT OR IGNORE INTO storage VALUES (1,0);
                CREATE TABLE IF NOT EXISTS clear_generation (id INTEGER PRIMARY KEY CHECK (id=1), value INTEGER NOT NULL);
                INSERT OR IGNORE INTO clear_generation VALUES (1,0);
                CREATE TRIGGER IF NOT EXISTS content_added AFTER INSERT ON content
                BEGIN UPDATE storage SET bytes=bytes+NEW.stored_bytes WHERE id=1; END;
                CREATE TRIGGER IF NOT EXISTS content_removed AFTER DELETE ON content
                BEGIN UPDATE storage SET bytes=bytes-OLD.stored_bytes WHERE id=1; END;
            """)

    @contextmanager
    def connection(self):
        with closing(sqlite3.connect(self.path, timeout=5)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA secure_delete=ON")
            with db:
                yield db

    @staticmethod
    def insert(db, usage_id, request_id, at, headers, request, response, request_bytes, response_bytes):
        # After restoring an event snapshot an integer usage ID may be reused.
        # Match the server-issued request ID on every read as well.
        db.execute("DELETE FROM content WHERE usage_id=?", (usage_id,))
        streamed = hasattr(request, "read")
        length = request.seek(0, os.SEEK_END) if streamed else len(request)
        sql = ("INSERT INTO content VALUES (?,?,?,?,zeroblob(?),?,?,?,?)" if streamed
               else "INSERT INTO content VALUES (?,?,?,?,?,?,?,?,?)")
        db.execute(sql, (usage_id, request_id, at, headers, length if streamed else request, response,
                         request_bytes, response_bytes, len(headers.encode("utf-8")) + length + len(response)))
        if streamed:
            request.seek(0)
            with db.blobopen("content", "request", usage_id) as destination:
                while block := request.read(64 * 1024):
                    destination.write(block)

    @staticmethod
    def prune(db, policy, now):
        cutoff = now - policy["logRetentionHours"] * 3_600_000
        deleted = db.execute("DELETE FROM content WHERE at<?", (cutoff,)).rowcount
        excess = db.execute("SELECT bytes FROM storage WHERE id=1").fetchone()[0] - policy["logMaxMB"] * 1048576
        while excess > 0:
            rows = db.execute("SELECT usage_id,stored_bytes FROM content ORDER BY at,usage_id LIMIT 128").fetchall()
            if not rows:
                break
            ids = []
            for usage_id, size in rows:
                ids.append((usage_id,))
                excess -= size
                if excess <= 0:
                    break
            db.executemany("DELETE FROM content WHERE usage_id=?", ids)
            deleted += len(ids)
        return deleted

    def summary(self):
        with self.connection() as db:
            return dict(db.execute("SELECT (SELECT count(*) FROM content) AS rows, bytes FROM storage").fetchone())

    @staticmethod
    def generation(db):
        return db.execute("SELECT value FROM clear_generation WHERE id=1").fetchone()[0]

    def clear(self):
        with self.connection() as db:
            db.execute("UPDATE clear_generation SET value=value+1 WHERE id=1")
            return db.execute("DELETE FROM content").rowcount

    def annotate(self, items):
        ids = [r["id"] for r in items]
        if not ids:
            return
        with self.connection() as db:
            rows = db.execute("SELECT usage_id,request_id,request_bytes FROM content WHERE usage_id IN (" +
                              ",".join("?" for _ in ids) + ")", ids)
            sizes = {(row[0], row[1]): row[2] for row in rows}
        for row in items:
            size = sizes.get((row["id"], row["requestId"]))
            row.update(hasContent=size is not None, requestBytes=size)

    def detail(self, db, usage_id):
        usage = db.execute("SELECT request_id FROM api_usage WHERE id=?", (usage_id,)).fetchone()
        if usage is None:
            return None
        with self.connection() as content:
            return content.execute("SELECT * FROM content WHERE usage_id=? AND request_id=?",
                                   (usage_id, usage[0])).fetchone()


def reset_legacy_capture(db):
    """Only carry forward a previous explicit organizer opt-in, never the old true default."""
    row = db.execute("""SELECT metadata FROM audit WHERE action='api-key.settings'
                        AND json_valid(metadata) AND json_type(metadata, '$.logContent')='array'
                        ORDER BY id DESC LIMIT 1""").fetchone()
    if row and json.loads(row[0]).get("logContent") == [False, True]:
        return
    db.execute("UPDATE settings SET value='false' WHERE key='keys.logContent'")
