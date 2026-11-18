"""Base SQLite schema, legacy account migration and connection lifecycle."""
from __future__ import annotations

from contextlib import closing, contextmanager
import errno
import os
import secrets
import sqlite3
import threading
import time
import weakref
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
 id TEXT PRIMARY KEY, role TEXT NOT NULL CHECK(role IN ('admin','judge','audience','team')),
 name TEXT NOT NULL, salt BLOB NOT NULL, code_hash BLOB NOT NULL,
 active INTEGER NOT NULL DEFAULT 1, created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
 token_hash TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id), expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_expiry ON sessions(expires_at);
CREATE TABLE IF NOT EXISTS teams (
 id TEXT PRIMARY KEY, number INTEGER NOT NULL UNIQUE, name TEXT NOT NULL,
 project TEXT NOT NULL DEFAULT '', track TEXT NOT NULL DEFAULT '', updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS team_accounts (
 account_id TEXT NOT NULL PRIMARY KEY REFERENCES accounts(id), team_id TEXT NOT NULL UNIQUE REFERENCES teams(id)
);
CREATE TABLE IF NOT EXISTS submissions (
 team_id TEXT PRIMARY KEY REFERENCES teams(id), version INTEGER NOT NULL CHECK(version>=1),
 draft_json TEXT NOT NULL, submitted_json TEXT, submitted_version INTEGER,
 first_submitted_at INTEGER, submitted_at INTEGER, updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS scores (
 judge_id TEXT NOT NULL REFERENCES accounts(id), team_id TEXT NOT NULL REFERENCES teams(id),
 scores_json TEXT NOT NULL, total_tenths INTEGER, feedback TEXT NOT NULL DEFAULT '',
 status TEXT NOT NULL CHECK(status IN ('draft','submitted','recused')), updated_at INTEGER NOT NULL,
 PRIMARY KEY(judge_id,team_id)
);
-- Separate from the original seven-column scores schema for rollback compatibility.
CREATE TABLE IF NOT EXISTS score_feedback_visibility (
 judge_id TEXT NOT NULL, team_id TEXT NOT NULL, revision INTEGER NOT NULL,
 PRIMARY KEY(judge_id,team_id),
 FOREIGN KEY(judge_id,team_id) REFERENCES scores(judge_id,team_id)
);
-- All writers, including rollback servers, invalidate consent before replacing a record.
CREATE TRIGGER IF NOT EXISTS score_feedback_after_insert AFTER INSERT ON scores BEGIN
 DELETE FROM score_feedback_visibility WHERE judge_id=NEW.judge_id AND team_id=NEW.team_id;
END;
CREATE TRIGGER IF NOT EXISTS score_feedback_after_update AFTER UPDATE ON scores BEGIN
 DELETE FROM score_feedback_visibility WHERE judge_id=OLD.judge_id AND team_id=OLD.team_id;
END;
CREATE TRIGGER IF NOT EXISTS score_feedback_before_delete BEFORE DELETE ON scores BEGIN
 DELETE FROM score_feedback_visibility WHERE judge_id=OLD.judge_id AND team_id=OLD.team_id;
END;
CREATE TABLE IF NOT EXISTS votes (
 account_id TEXT NOT NULL REFERENCES accounts(id), team_id TEXT NOT NULL REFERENCES teams(id), created_at INTEGER NOT NULL,
 PRIMARY KEY(account_id,team_id)
);
-- Standings aggregate by team; the original primary keys begin with the voter or judge.
CREATE INDEX IF NOT EXISTS votes_by_team ON votes(team_id);
CREATE INDEX IF NOT EXISTS submitted_scores_by_team
 ON scores(team_id, judge_id, total_tenths) WHERE status='submitted';
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit (
 id INTEGER PRIMARY KEY AUTOINCREMENT, at INTEGER NOT NULL, actor_id TEXT, role TEXT,
 action TEXT NOT NULL, target TEXT NOT NULL DEFAULT '', metadata TEXT NOT NULL DEFAULT '{}'
);
"""


def migrate_accounts(db_path, clock):
    """Rebuild only the legacy role CHECK; preserve IDs and referencing tables."""
    if not db_path.exists():
        return
    db=sqlite3.connect(db_path, timeout=10)
    try:
        table=db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='accounts'").fetchone()
        if not table or "'team'" in table[0]:
            return
        backup_path=db_path.parent/("before-team-"+str(clock())+"-"+secrets.token_hex(4)+".sqlite3")
        with closing(sqlite3.connect(backup_path)) as backup:
            db.backup(backup)
        os.chmod(backup_path, 0o600)
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("BEGIN IMMEDIATE")
        try:
            db.execute("""CREATE TABLE accounts_new (
              id TEXT PRIMARY KEY, role TEXT NOT NULL CHECK(role IN ('admin','judge','audience','team')),
              name TEXT NOT NULL, salt BLOB NOT NULL, code_hash BLOB NOT NULL,
              active INTEGER NOT NULL DEFAULT 1, created_at INTEGER NOT NULL)""")
            db.execute("INSERT INTO accounts_new SELECT id,role,name,salt,code_hash,active,created_at FROM accounts")
            db.execute("DROP TABLE accounts")
            db.execute("ALTER TABLE accounts_new RENAME TO accounts")
            if db.execute("PRAGMA foreign_key_check").fetchall():
                raise RuntimeError("Account migration failed foreign-key validation")
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.execute("PRAGMA foreign_keys=ON")
    finally:
        db.close()


@contextmanager
def startup_lock(directory):
    """Serialize schema upgrades and first-run private-file creation across processes."""
    fd = os.open(directory / ".schema.lock", os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, "rb+") as lock:
        if os.name == "nt":
            import msvcrt

            # Lock the same first byte even for a newly created, empty file.
            while True:
                try:
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as error:
                    if error.errno != errno.EACCES:
                        raise
                    time.sleep(0.1)
            try:
                yield
            finally:
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock, fcntl.LOCK_EX)
            yield


def _close_idle(handles):
    while handles:
        handles.pop().close()


class EventConnection(sqlite3.Connection):
    """Measure time spent explicitly acquiring a SQLite writer reservation."""

    def execute(self, sql, parameters=()):
        if sql.strip().upper() != "BEGIN IMMEDIATE":
            return super().execute(sql, parameters)
        from .observability import record_db_lock_wait

        start = time.monotonic()
        try:
            cursor = super().execute(sql, parameters)
        except sqlite3.OperationalError as error:
            if getattr(error, "sqlite_errorcode", 0) & 255 in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
                record_db_lock_wait(time.monotonic() - start, failed=True)
            raise
        record_db_lock_wait(time.monotonic() - start)
        return cursor


class ConnectionPool:
    """A bounded pool for one process. Each lease has one owning thread."""

    def __init__(self, path, *, limit=8, timeout=10.0):
        self.path = Path(path)
        self.limit = limit
        self.timeout = timeout
        self._idle = []
        self._count = 0
        self._closed = False
        self._condition = threading.Condition()
        self._local = threading.local()
        weakref.finalize(self, _close_idle, self._idle)

    def _open(self):
        db = sqlite3.connect(self.path, timeout=self.timeout, check_same_thread=False, factory=EventConnection)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            if db.execute("PRAGMA journal_mode=WAL").fetchone()[0] != "wal":
                raise RuntimeError("The event database requires SQLite WAL support")
            db.execute("PRAGMA synchronous=FULL")
            return db
        except BaseException:
            db.close()
            raise

    def _checkout(self):
        with self._condition:
            ready = self._condition.wait_for(
                lambda: self._closed or self._idle or self._count < self.limit, self.timeout
            )
            if self._closed:
                raise RuntimeError("Event database pool is closed")
            if not ready:
                raise sqlite3.OperationalError("Event database connection pool is busy")
            if self._idle:
                return self._idle.pop()
            self._count += 1
        try:
            return self._open()
        except BaseException:
            self._release(None)
            raise

    def _release(self, db):
        with self._condition:
            if db is None or self._closed:
                if db is not None:
                    db.close()
                self._count -= 1
            else:
                self._idle.append(db)
            self._condition.notify()

    @contextmanager
    def connection(self):
        outer = getattr(self._local, "connection", None)
        if outer is not None:
            # SAVEPOINT inside an explicit transaction keeps nested success provisional.
            # Opening a top-level savepoint alone would commit on its RELEASE.
            if not outer.in_transaction:
                outer.execute("BEGIN")
            outer.execute("SAVEPOINT gotc_nested")
            try:
                yield outer
            except BaseException:
                outer.execute("ROLLBACK TO gotc_nested")
                raise
            finally:
                outer.execute("RELEASE gotc_nested")
            return
        db = self._checkout()
        self._local.connection = db
        try:
            with db:
                yield db
        finally:
            del self._local.connection
            try:
                if db.in_transaction:
                    db.rollback()
                db.set_trace_callback(None)
                db.row_factory = sqlite3.Row
                db.execute("PRAGMA foreign_keys=ON")
                db.execute(f"PRAGMA busy_timeout={int(self.timeout * 1000)}")
            except sqlite3.Error:
                db.close()
                db = None
            self._release(db)

    def close(self):
        """Stop new leases. Active leases close as their owners return them."""
        with self._condition:
            self._closed = True
            self._count -= len(self._idle)
            _close_idle(self._idle)
            self._condition.notify_all()


class DatabaseMaintenance:
    """Platform-owned periodic cleanup, with short transactions and a passive checkpoint."""

    def __init__(self, event, interval=300):
        self.event = event
        self.interval = interval
        self._stop = threading.Event()
        self._thread = None

    def run_once(self):
        now = self.event.clock()
        with self.event.db() as db:
            db.execute("BEGIN IMMEDIATE")
            for table in ("sessions", "finals_access_tokens", "import_previews"):
                db.execute(
                    f"DELETE FROM {table} WHERE rowid IN "
                    f"(SELECT rowid FROM {table} WHERE expires_at <= ? ORDER BY expires_at LIMIT 512)",
                    (now,),
                )
        with self.event.db() as db:
            return tuple(db.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone())

    def _run(self):
        while not self._stop.wait(self.interval):
            try:
                self.run_once()
            except sqlite3.Error:
                import logging

                logging.getLogger(__name__).warning("Event database maintenance deferred")

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="gotc-db-maintenance")
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join()
