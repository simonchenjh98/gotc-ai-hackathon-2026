"""Offline, resumable migration of optional gateway bodies out of the event ledger."""
from contextlib import closing
from dataclasses import dataclass
from heapq import merge
import os
import secrets
import shutil
import sqlite3

from ..gateway.accounting import now_ms
from ..gateway.config import DEFAULTS, settings
from ..gateway.content_store import reset_legacy_capture

MIB = 1 << 20
BATCH_BYTES = 4 * MIB
BATCH_ROWS = 128
FREE_MARGIN = 256 * MIB
OPTION = "GOTC_LEGACY_GATEWAY_CONTENT"
SIZE = "length(CAST(l.headers AS BLOB))+length(l.request)+length(l.response)"
STATE = "v003_content_migration"


def readonly_uri(path):
    # Opening a checkpointed WAL database with mode=ro alone can create -wal/-shm.
    # Keep real committed WAL frames visible when recovering an interrupted run.
    wal = path.with_name(path.name + "-wal")
    journal = path.with_name(path.name + "-journal")
    has_wal = wal.exists() and wal.stat().st_size > 0
    if has_wal and not path.with_name(path.name + "-shm").exists():
        raise RuntimeError("SQLite WAL lacks its shared-memory file; recover the stopped database before migration")
    uri = path.as_uri() + "?mode=ro"
    if not has_wal and not journal.exists():
        uri += "&immutable=1"
    return uri


def readonly(path):
    return closing(sqlite3.connect(readonly_uri(path), uri=True, timeout=10))


def table(db, name):
    return bool(db.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name=?", (name,)).fetchone())


@dataclass
class Plan:
    mode: str
    oldest: tuple | None
    token: str
    retained: int
    to_copy: int
    journals: int
    snapshots: int
    checkpoint_growth: int

    @property
    def required(self):
        return self.to_copy + self.journals + self.snapshots + self.checkpoint_growth + FREE_MARGIN


def _legacy_snapshots(db):
    """Include the actual backup predicates used by the unnumbered upgrade."""
    from ..judging.finals import FinalsService

    def marked(name):
        return table(db, name) and db.execute(
            f"SELECT 1 FROM {name} WHERE key='schemaVersion' AND value='1'"
        ).fetchone() is not None

    accounts = db.execute("SELECT sql FROM sqlite_schema WHERE name='accounts'").fetchone()[0]
    count = sum((not marked("write_conflicts_meta"), not table(db, "admin_data_meta"),
                 "'team'" not in accounts, not marked("submission_records_meta"),
                 not FinalsService.initialized(db)))
    extent = db.execute("PRAGMA page_count").fetchone()[0] * db.execute("PRAGMA page_size").fetchone()[0]
    return count * (extent + 2 * MIB)


def _checkpoint_growth(db, schema, path, page_bytes):
    # Existing WAL commits may already extend the logical database well past the
    # main file. Checkpointing must allocate those pages before releasing the WAL.
    pages = db.execute(f"PRAGMA {schema}.page_count").fetchone()[0]
    return max(0, pages * page_bytes - path.stat().st_size)


def preflight(db, directory, version, now):
    state = db.execute(f"SELECT mode,at,usage_id,token FROM {STATE}").fetchone() if table(db, STATE) else None
    if not state and (not table(db, "api_logs") or not db.execute("SELECT 1 FROM api_logs LIMIT 1").fetchone()):
        return None
    mode = os.environ.get(OPTION)
    if mode not in {"migrate", "discard"}:
        raise RuntimeError(f"Legacy gateway content requires an explicit {OPTION}=migrate or discard")
    if state and mode != state[0]:
        raise RuntimeError(f"Resume the interrupted migration with {OPTION}={state[0]}")
    if db.execute("PRAGMA foreign_key_check(api_logs)").fetchone():
        raise RuntimeError("Legacy gateway content has an invalid usage reference")
    sidecar = directory / "gateway-content.sqlite3"
    largest = db.execute(f"SELECT coalesce(max({SIZE}),0) FROM api_logs l").fetchone()[0]
    oldest = (state[1], state[2]) if state and state[1] is not None else None
    retained = needed = count = 0
    main_page = db.execute("PRAGMA main.page_size").fetchone()[0]
    growth = _checkpoint_growth(db, "main", directory / "event.sqlite3", main_page)
    side_page = 4096
    if sidecar.is_symlink():
        raise RuntimeError("Cannot migrate into a symbolic link")
    if mode == "discard" and sidecar.exists():
        # Normal service bootstrap opens this database even when no bodies are copied.
        with readonly(sidecar) as prior:
            side_page = prior.execute("PRAGMA page_size").fetchone()[0]
            growth += _checkpoint_growth(prior, "main", sidecar, side_page)
    if mode == "migrate":
        if state and not sidecar.exists():
            raise RuntimeError("Restore the missing sidecar paired with the interrupted content migration")
        attached = sidecar.exists()
        if attached:
            db.execute("ATTACH DATABASE ? AS prior", (readonly_uri(sidecar),))
        try:
            if attached:
                side_page = db.execute("PRAGMA prior.page_size").fetchone()[0]
                growth += _checkpoint_growth(db, "prior", sidecar, side_page)
            prior_tables = {row[0] for row in db.execute("SELECT name FROM prior.sqlite_schema WHERE type='table'")} if attached else set()
            if state:
                identity = (db.execute("SELECT token FROM prior.migration_origin").fetchone()
                            if "migration_origin" in prior_tables else None)
                if not identity or identity[0] != state[3]:
                    raise RuntimeError("Restore the sidecar paired with the interrupted content migration")
            elif "content" in prior_tables and db.execute("""SELECT 1 FROM prior.content c
                    JOIN main.api_logs l ON l.usage_id=c.usage_id LIMIT 1""").fetchone():
                raise RuntimeError("Existing sidecar and legacy rows claim the same usage ID; reconcile the snapshots before migrating")
            policy = {key[5:]: val for key, val in DEFAULTS.items()}
            policy.update(settings(db))
            remaining = policy["logMaxMB"] * MIB
            cutoff = now - policy["logRetentionHours"] * 3_600_000
            if state and oldest is not None:
                oldest = max(oldest, (cutoff, -(1 << 63)))
            committed = set()
            if state:
                committed.update(row[0] for row in db.execute("SELECT usage_id FROM prior.migration_batch"))
            if "content" in prior_tables:
                largest = max(largest, db.execute("SELECT coalesce(max(stored_bytes),0) FROM prior.content").fetchone()[0])
            if not state or oldest is not None:
                predicate, params = ("(u.at,u.id)>=(?,?)", oldest) if state else ("u.at>=?", (cutoff,))
                incoming = db.execute(f"""SELECT u.at,u.id,{SIZE},1 FROM api_usage u JOIN api_logs l ON l.usage_id=u.id
                                          WHERE {predicate} ORDER BY u.at DESC,u.id DESC""", params)
                existing = ()
                if not state and "content" in prior_tables:
                    existing = db.execute("""SELECT at,usage_id,stored_bytes,0 FROM prior.content WHERE at>=?
                                             ORDER BY at DESC,usage_id DESC""", (cutoff,))
                try:
                    for at, usage_id, size, incoming_row in merge(incoming, existing, key=lambda row: (row[0], row[1]), reverse=True):
                        if not state:
                            if size > remaining:
                                break
                            remaining -= size
                            oldest = (at, usage_id)
                        if incoming_row:
                            retained += size
                            if usage_id not in committed:
                                needed += size
                                count += 1
                finally:
                    incoming.close()
                    if isinstance(existing, sqlite3.Cursor):
                        existing.close()
        finally:
            if attached:
                db.execute("DETACH DATABASE prior")
    page = max(side_page, main_page)
    # A single oversized legacy record is indivisible. Include page splits, indexes,
    # freelist metadata, WAL frame headers, SHM and sidecar rollback journal.
    journals = 2 * max(BATCH_BYTES, largest) + 16 * page * BATCH_ROWS + 4 * MIB
    # Default sidecar pages are 4 KiB. Reserve a page per record, index/metadata
    # allowance, and 10% for overflow page and B-tree overhead.
    budget = (needed * 11 + 9) // 10 + count * (side_page + 256)
    plan = Plan(mode, oldest, state[3] if state else "", retained, budget, journals,
                _legacy_snapshots(db) if version == 0 else 0, growth)
    free = shutil.disk_usage(directory).free
    if free < plan.required:
        raise RuntimeError(f"Insufficient free space for {mode}: free={free} required={plan.required} bytes "
                           f"(copy={budget}, journals={journals}, historical_snapshots={plan.snapshots}, "
                           f"checkpoint_growth={growth}, safety_margin={FREE_MARGIN})")
    return plan


def _start(db, content, plan):
    token = secrets.token_hex(16)
    if plan.mode == "migrate":
        with content.connection() as side:
            # A killed writer can leave a hot rollback journal requiring a write
            # to recover. WAL lets the next read-only preflight see committed data.
            if side.execute("PRAGMA journal_mode=WAL").fetchone()[0] != "wal":
                raise RuntimeError("The private content migration requires SQLite WAL support")
            side.execute("PRAGMA synchronous=FULL")
            side.execute("BEGIN IMMEDIATE")
            side.execute("CREATE TABLE IF NOT EXISTS migration_origin (token TEXT NOT NULL)")
            side.execute("DELETE FROM migration_origin")
            side.execute("INSERT INTO migration_origin VALUES (?)", (token,))
            side.execute("CREATE TABLE IF NOT EXISTS migration_batch (usage_id INTEGER PRIMARY KEY,stored_bytes INTEGER)")
            side.execute("DELETE FROM migration_batch")
    with db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(f"CREATE TABLE {STATE} (mode TEXT NOT NULL,at INTEGER,usage_id INTEGER,token TEXT NOT NULL)")
        db.execute(f"INSERT INTO {STATE} VALUES (?,?,?,?)", (plan.mode, *(plan.oldest or (None, None)), token))


def _copy_batch(db, ids):
    if not ids:
        return
    slots = ",".join("?" for _ in ids)
    # Only the attached sidecar is written by this transaction. SQLite transfers
    # the BLOBs directly; FULL sync completes before the source transaction starts.
    with db:
        db.execute("DELETE FROM side.migration_batch")
        db.execute(f"""INSERT INTO side.content
            SELECT u.id,coalesce(nullif(u.request_id,''),lower(hex(randomblob(16)))),u.at,l.headers,
                   l.request,l.response,l.request_bytes,l.response_bytes,{SIZE}
            FROM api_logs l JOIN api_usage u ON u.id=l.usage_id
            WHERE l.usage_id IN ({slots}) AND NOT EXISTS (SELECT 1 FROM side.content c WHERE c.usage_id=u.id)""", ids)
        db.execute(f"""INSERT INTO side.migration_batch SELECT usage_id,stored_bytes FROM side.content
                       WHERE usage_id IN ({slots})""", ids)


def _delete_batch(db, ids, kept):
    with db:
        db.execute("BEGIN IMMEDIATE")
        if kept:
            slots = ",".join("?" for _ in kept)
            copied = db.execute(f"""SELECT count(*) FROM api_logs l JOIN api_usage u ON u.id=l.usage_id
                JOIN side.content c ON c.usage_id=l.usage_id AND c.at=u.at
                AND (u.request_id='' OR c.request_id=u.request_id)
                AND c.headers=l.headers AND c.request=l.request AND c.response=l.response
                AND c.request_bytes=l.request_bytes AND c.response_bytes=l.response_bytes
                WHERE l.usage_id IN ({slots})""", kept).fetchone()[0]
            if copied != len(kept):
                raise RuntimeError("Content changed between copy and deletion; stop writers and reconcile the paired databases")
            db.execute(f"""UPDATE api_usage SET request_id=(SELECT c.request_id FROM side.content c WHERE c.usage_id=id)
                           WHERE id IN ({slots}) AND request_id=''""", kept)
        db.executemany("DELETE FROM api_logs WHERE usage_id=?", ((i,) for i in ids))


def _batch(rows):
    result = []
    total = 0
    for row in rows:
        size = row[-1]
        if result and total + size > BATCH_BYTES:
            break
        result.append(row)
        total += size
    return result, total


def _prune_previous_sidecar(db, oldest):
    # A compatible rollback may have left an earlier sidecar. Apply the same
    # frozen time/quota boundary to it, also bounding its secure-delete journal.
    predicate, params = ("(at,usage_id)<(?,?)", oldest) if oldest is not None else ("1", ())
    while True:
        rows, _ = _batch(db.execute(f"""SELECT usage_id,stored_bytes FROM side.content
                         WHERE {predicate} ORDER BY at,usage_id LIMIT ?""", (*params, BATCH_ROWS)).fetchall())
        if not rows:
            return
        with db:
            db.executemany("DELETE FROM side.content WHERE usage_id=?", ((row[0],) for row in rows))
        _checkpoint(db, "side")


def _checkpoint(db, schema="main"):
    busy, _, _ = db.execute(f"PRAGMA {schema}.wal_checkpoint(TRUNCATE)").fetchone()
    if busy:
        raise RuntimeError("Content migration checkpoint blocked; stop all readers and resume with the same policy")


def apply(db, event):
    content = event.keys.ledger.content
    version = db.execute("PRAGMA user_version").fetchone()[0]
    with readonly(event.data_dir / "event.sqlite3") as reader:
        reader.row_factory = sqlite3.Row
        plan = preflight(reader, event.data_dir, version, now_ms())
    if plan:
        if not plan.token:
            _start(db, content, plan)
        db.execute("PRAGMA main.secure_delete=ON")
        attached = plan.mode == "migrate"
        if attached:
            db.execute("ATTACH DATABASE ? AS side", (str(content.path),))
        try:
            if attached:
                db.execute("PRAGMA side.synchronous=FULL")
                db.execute("PRAGMA side.secure_delete=ON")
            _checkpoint(db)
            while True:
                rows = db.execute(f"""SELECT l.usage_id,u.at,{SIZE} FROM api_logs l JOIN api_usage u ON u.id=l.usage_id
                                      ORDER BY l.usage_id LIMIT ?""", (BATCH_ROWS,)).fetchall()
                if not rows:
                    break
                batch, total = _batch(rows)
                ids = [row[0] for row in batch]
                kept = [i for i, at, _ in batch if plan.oldest is not None and (at, i) >= plan.oldest]
                if shutil.disk_usage(event.data_dir).free < FREE_MARGIN + plan.journals + total * bool(kept):
                    raise RuntimeError("Insufficient free space to continue the content migration; resume after freeing space")
                if kept:
                    _copy_batch(db, kept)
                    _checkpoint(db, "side")
                _delete_batch(db, ids, kept)
                _checkpoint(db)
            if attached:
                _prune_previous_sidecar(db, plan.oldest)
                with db:
                    db.execute("DELETE FROM side.migration_batch")
                _checkpoint(db, "side")
        finally:
            if attached:
                db.execute("DETACH DATABASE side")
    with db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT 1 FROM api_logs LIMIT 1").fetchone():
            raise RuntimeError("Legacy content remains; migration cannot finish")
        if version < 3:
            reset_legacy_capture(db)
        db.execute(f"DROP TABLE IF EXISTS {STATE}")
        db.execute("PRAGMA user_version=3")
