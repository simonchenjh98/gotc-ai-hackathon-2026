"""Base SQLite schema, legacy account migration and connection lifecycle."""
from __future__ import annotations

from contextlib import closing, contextmanager
import os
import secrets
import sqlite3


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
def connection(db_path):
    db = sqlite3.connect(db_path, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=10000")
    try:
        with db:
            yield db
    finally:
        db.close()
