"""Monotonic SQLite event schema versions. The event database owns its version."""
from . import v001_baseline, v002_admin_auth, v003_content_store

LATEST = 3


def current(db):
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if not 0 <= version <= LATEST:
        raise RuntimeError(f"Unsupported event schema version {version}; this build supports up to {LATEST}")
    return version


def inspect_existing(path):
    """Refuse unknown or damaged sources before provisioning keys or modifying state."""
    from contextlib import closing
    import sqlite3

    if not path.exists():
        return 0
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=10)) as db:
        version = current(db)
        v001_baseline.validate_source(db)
        if version:
            v001_baseline.verify(db, check_foreign_keys=False)
        if version >= 2 and not db.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='named_admins'").fetchone():
            raise RuntimeError("Named-admin schema is missing from this versioned event database")
        return version


def apply(event):
    for version, migration in (
        (1, lambda db: v001_baseline.verify(db)),
        (2, v002_admin_auth.apply),
        (3, lambda db: v003_content_store.apply(db, event)),
    ):
        with event.db() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = current(db)
            if previous >= version:
                continue
            if previous != version - 1:
                raise RuntimeError("Missing predecessor for event schema migration")
            migration(db)
            db.execute(f"PRAGMA user_version={version}")
