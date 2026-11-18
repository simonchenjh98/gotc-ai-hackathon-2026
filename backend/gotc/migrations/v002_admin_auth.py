"""Named organizer accounts and explicit offline conversion of the development checkpoint."""
from contextlib import closing
import hmac
import json
import os
import sqlite3

from ..admin_auth import seed_binding
from ..credentials import ORGANIZER_USERNAME, TOTP_SEED
from ..database import startup_lock

CLEANUP_TABLE = 'organizer_seed_cleanup_pending'


def apply(db):
    db.execute("""CREATE TABLE IF NOT EXISTS named_admins (
        account_id TEXT PRIMARY KEY REFERENCES accounts(id),
        username TEXT NOT NULL COLLATE NOCASE UNIQUE,
        seed_binding BLOB NOT NULL CHECK(length(seed_binding)=32),
        last_step INTEGER NOT NULL,
        portal_confirmed INTEGER NOT NULL DEFAULT 1 CHECK(portal_confirmed IN (0,1))
    )""")


def ensure_portal_confirmation(db):
    # Seedless development copies from before two-phase activation already retired legacy accounts.
    if not any(row[1] == 'portal_confirmed' for row in db.execute('PRAGMA table_info(named_admins)')):
        db.execute('ALTER TABLE named_admins ADD COLUMN portal_confirmed INTEGER NOT NULL DEFAULT 1 '
                   'CHECK(portal_confirmed IN (0,1))')


def has_old_seeds(db):
    return any(row[1] == 'totp_seed' for row in db.execute('PRAGMA table_info(named_admins)'))


def cleanup_pending(db):
    return db.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name=?", (CLEANUP_TABLE,)).fetchone()


def require_clean_schema(db):
    if has_old_seeds(db) or cleanup_pending(db):
        raise RuntimeError('Development TOTP schema requires offline admin_auth migrate-checkpoint; see organizer-accounts.md')


def checkpoint_rows(db):
    if db.execute('PRAGMA user_version').fetchone()[0] not in {2, 3} or not has_old_seeds(db):
        raise ValueError('Expected an old development database containing named_admins.totp_seed')
    rows = db.execute('SELECT account_id,username,totp_seed,last_step FROM named_admins').fetchall()
    for _, login, seed, _ in rows:
        if (not isinstance(login, str) or not ORGANIZER_USERNAME.fullmatch(login)
                or not isinstance(seed, str) or not TOTP_SEED.fullmatch(seed)):
            raise ValueError('Invalid organizer row in development checkpoint')
    return rows


def export_checkpoint(directory, destination):
    """Only this explicit operator command writes seeds to an exclusively created credential file."""
    path = (directory / 'event.sqlite3').resolve()
    with startup_lock(directory), closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
        seeds = {login: seed for _, login, seed, _ in checkpoint_rows(db)}
        fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(seeds, stream)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            destination.unlink(missing_ok=True)
            raise


def convert_checkpoint(directory, credentials):
    """Refuse cutover unless every recorded seed has been independently provisioned."""
    path = (directory / 'event.sqlite3').resolve()
    with startup_lock(directory), closing(sqlite3.connect(path.as_uri() + '?mode=rw', uri=True)) as db:
        db.execute('PRAGMA secure_delete=ON')
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('BEGIN EXCLUSIVE')
        if has_old_seeds(db):
            rows = checkpoint_rows(db)
            installed = credentials.organizer_seeds()
            if any(not hmac.compare_digest(installed.get(login, ''), seed) for _, login, seed, _ in rows):
                raise ValueError('Install every matching checkpoint credential before migration')
            db.execute('ALTER TABLE named_admins RENAME TO old_named_admins')
            apply(db)
            db.executemany('INSERT INTO named_admins(account_id,username,seed_binding,last_step) VALUES (?,?,?,?)', (
                (account, login, seed_binding(seed), step) for account, login, seed, step in rows
            ))
            db.execute('DROP TABLE old_named_admins')
            db.execute(f'CREATE TABLE {CLEANUP_TABLE}(pending INTEGER)')
        elif not cleanup_pending(db):
            raise ValueError('No development checkpoint migration is pending')
        db.commit()
        # Discard old free pages and truncate the WAL before permitting startup or backup.
        # The marker survives interruption, low disk space or an unexpected reader.
        db.execute('VACUUM')
        if db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0]:
            raise RuntimeError('Stop other database users and retry development checkpoint cleanup')
        db.execute(f'DROP TABLE {CLEANUP_TABLE}')
        db.commit()
