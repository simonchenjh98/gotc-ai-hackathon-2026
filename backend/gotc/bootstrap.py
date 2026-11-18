"""Locate private state and apply event bootstrap and migrations in deployment order."""
from __future__ import annotations

import hmac
import json
import os
from pathlib import Path
import secrets

from . import database, migrations


def data_directory(configured, project_root):
    explicit = configured or os.environ.get("GOTC_DATA_DIR")
    if explicit:
        return Path(explicit).resolve()
    for legacy_dir in (project_root / "server" / "data", project_root / ".runtime" / "local-data"):
        if legacy_dir.is_dir() and any(legacy_dir.iterdir()):
            raise RuntimeError(
                f"Existing GOTC event files found in {legacy_dir}. Set GOTC_DATA_DIR to this "
                "directory before starting, or stop the old service and migrate the entire "
                "private directory to persistent storage, then set GOTC_DATA_DIR there."
            )
    data_home = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
    return (data_home / "gotc").resolve()


def shared_login_codes(credentials):
    codes = credentials.read_json("shared-login-codes.json", default={})
    if not isinstance(codes, dict) or not set(codes).issubset({"admin", "judge"}):
        raise ValueError("Shared login configuration supports admin and judge roles only")
    for value in codes.values():
        if not isinstance(value, str) or not 6 <= len(value) <= 160 or value != value.strip():
            raise ValueError("Shared login codes must contain 6 to 160 characters")
    return codes


def _initialize_settings(db, now):
    for key, value in {"scoringOpen": False, "resultsPublished": True, "minJudges": 5, "submissionsOpen": False, "submissionDeadline": None, "updatedAt": now}.items():
        db.execute("INSERT OR IGNORE INTO settings VALUES (?,?)", (key, json.dumps(value)))
    # Preserve an upgraded event's current audience window once; later changes are independent.
    db.execute("INSERT OR IGNORE INTO settings SELECT 'votingOpen',value FROM settings WHERE key='scoringOpen'")
    db.execute("INSERT OR IGNORE INTO settings VALUES ('votingDeadline','null')")


def _named_admins_enabled(db):
    table = db.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='named_admins'").fetchone()
    return bool(table and db.execute("SELECT 1 FROM named_admins WHERE portal_confirmed=1 LIMIT 1").fetchone())


def _initialize_accounts(db, event, hash_code):
    if not db.execute("SELECT 1 FROM accounts WHERE role='admin'").fetchone():
        account, code = event.create_account(db, "admin", "活动管理员")
        # Exclusive creation prevents silently overwriting an earlier credential file.
        access_file = event.data_dir / "admin-access.txt"
        fd = os.open(access_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write("GOTC administrator access code (keep private):\n" + code + "\n")
        os.chmod(access_file, 0o600)
        event.audit(db, account, "bootstrap")
    for role, code in event.shared_login_codes.items():
        if role == "admin" and _named_admins_enabled(db):
            continue
        for account in db.execute("SELECT * FROM accounts WHERE role=? AND id NOT IN (SELECT account_id FROM credential_overrides)", (role,)).fetchall():
            if not hmac.compare_digest(hash_code(code, account["salt"]), account["code_hash"]):
                salt = secrets.token_bytes(16)
                db.execute("UPDATE accounts SET salt=?,code_hash=? WHERE id=?", (salt, hash_code(code, salt), account["id"]))
                db.execute("DELETE FROM sessions WHERE account_id=?", (account["id"],))
                event.audit(db, account, "credential.shared-code-updated")
    if "judge" in event.shared_login_codes and not db.execute("SELECT 1 FROM accounts WHERE role='judge'").fetchone():
        account, _ = event.create_account(db, "judge", "评委01")
        event.audit(db, account, "bootstrap.shared-judge")


def legacy_schema(event, *, database_existed, hash_code):
    """Upgrade unnumbered predecessor releases in their original recovery order."""
    database.migrate_accounts(event.db_path, event.clock)
    with event.db() as db:
        db.executescript(database.SCHEMA)
        event.admin_team_access.initialize(db)
        event.judging.initialize(db)
        event.admin_data.initialize(db)
        event.roadshow_scoring.initialize(db)
        event.claims.initialize(db)
        event.roadshow_access.initialize(db)
        event.registration.initialize(db)
        event.keys.initialize(db)
        event.repositories.initialize(db)
        _initialize_settings(db, event.clock())
        _initialize_accounts(db, event, hash_code)
    os.chmod(event.db_path, 0o600)
    event.submission_records.initialize(backup=database_existed)
    with event.db() as db:event.repository_backups.initialize(db)
    with event.db() as db:event.material_uploads.initialize(db)
    event.write_conflicts.initialize()
    event.finals.initialize()
    with event.db() as db:event.team_lifecycle.initialize(db)


def initialize_state(event, *, database_existed, previous_version, hash_code):
    if previous_version >= 2:
        with event.db() as db:
            migrations.v002_admin_auth.ensure_portal_confirmation(db)
    if previous_version == 0:
        legacy_schema(event, database_existed=database_existed, hash_code=hash_code)
    else:
        # A compatible previous binary can add reviews and submissions after rollback.
        # Reconcile those rows and hydrate the per-process controller configuration.
        with event.db() as db:
            db.execute("BEGIN IMMEDIATE")
            event.admin_data.reconcile(db)
            event.roadshow_access.verify_key(db)
            event.keys.load_runtime(db)
            event.submission_records.seed_current(db)
            _initialize_accounts(db, event, hash_code)
    migrations.apply(event)
    with event.db() as db:
        write_shared_admin = "admin" in event.shared_login_codes and not _named_admins_enabled(db)
    if write_shared_admin:
        access_file = event.data_dir / "admin-access.txt"
        temporary = event.data_dir / (".admin-access-" + secrets.token_hex(8))
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write("GOTC administrator access code (keep private):\n" + event.shared_login_codes["admin"] + "\n")
        os.replace(temporary, access_file)
        os.chmod(access_file, 0o600)
