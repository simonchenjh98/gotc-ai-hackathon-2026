"""Monotonic SQLite event schema versions. The event database owns its version."""
from . import v001_baseline, v002_admin_auth, v003_content_store, v004_claim_email

LATEST = 4


def current(db):
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if not 0 <= version <= LATEST:
        raise RuntimeError(f"Unsupported event schema version {version}; this build supports up to {LATEST}")
    return version


def inspect_existing(path, organizer_credentials=None):
    """Refuse unknown or damaged sources before provisioning keys or modifying state."""
    import sqlite3

    if not path.exists():
        return 0
    with v003_content_store.readonly(path) as db:
        db.row_factory = sqlite3.Row
        version = current(db)
        v002_admin_auth.require_clean_schema(db)
        v001_baseline.validate_source(db)
        if version:
            v001_baseline.verify(db, check_foreign_keys=False)
        if version >= 2 and not db.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='named_admins'").fetchone():
            raise RuntimeError("Named-admin schema is missing from this versioned event database")
        if version >= 2 and organizer_credentials is not None:
            from ..admin_auth import require_organizer_credentials

            require_organizer_credentials(db, organizer_credentials)
        v003_content_store.preflight(db, path.parent, version, v003_content_store.now_ms())
        return version


def apply(event):
    for version, migration in (
        (1, lambda db: v001_baseline.verify(db)),
        (2, v002_admin_auth.apply),
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
    with event.db() as db:
        if (current(db) == 2 or db.execute("SELECT 1 FROM api_logs LIMIT 1").fetchone()
                or v003_content_store.table(db, v003_content_store.STATE)):
            v003_content_store.apply(db, event)
    with event.db() as db:
        db.execute("BEGIN IMMEDIATE")
        previous = current(db)
        if previous < 4:
            if previous != 3:
                raise RuntimeError("Missing predecessor for event schema migration")
            v004_claim_email.apply(db)
            db.execute("PRAGMA user_version=4")
