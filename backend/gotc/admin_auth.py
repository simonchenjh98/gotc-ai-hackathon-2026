"""Local organizer enrollment and single-use TOTP verification."""
from __future__ import annotations

import argparse
import getpass
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys

import pyotp

from .credentials import Credentials, ORGANIZER_USERNAME, TOTP_SEED


def username(value: object) -> str | None:
    if isinstance(value, str) and value.isascii() and ORGANIZER_USERNAME.fullmatch(value.lower()):
        return value.lower()
    return None


def prepare_enrollment(login, name):
    login = username(login)
    if not login or not isinstance(name, str) or not 1 <= len(name.strip()) <= 100:
        raise ValueError('A valid username and display name are required')
    return {'username': login, 'name': name.strip(),
            'code': secrets.token_urlsafe(32), 'seed': pyotp.random_base32()}


def seed_binding(seed: str) -> bytes:
    # This public fingerprint binds a high-entropy seed to its enrollment on restore.
    return hashlib.sha256(seed.encode("ascii")).digest()


def require_organizer_credentials(db, credentials):
    # Older seedless checkpoints already cut over all enrolled accounts.
    has_state = any(row[1] == 'portal_confirmed' for row in db.execute('PRAGMA table_info(named_admins)'))
    # After offline recovery revokes the last confirmed account, the replacement
    # must be usable before the named-only platform can reopen.
    named_only = db.execute('SELECT 1 FROM named_admins'
                            + (' WHERE portal_confirmed=1' if has_state else '') + ' LIMIT 1').fetchone()
    if not named_only:
        return
    confirmed_clause = ' AND n.portal_confirmed=1' if has_state else ''
    active = "SELECT n.username,n.seed_binding FROM named_admins n JOIN accounts a ON a.id=n.account_id WHERE a.active=1"
    confirmed = db.execute(active + confirmed_clause).fetchall()
    entries = credentials.organizer_seeds()
    if confirmed:
        valid = all(login in entries and hmac.compare_digest(seed_binding(entries[login]), binding)
                    for login, binding in confirmed)
    else:
        valid = any(login in entries and hmac.compare_digest(seed_binding(entries[login]), binding)
                    for login, binding in db.execute(active))
    if not valid:
        raise RuntimeError("Restore the matching organizer credential before starting the platform")


def login_seed(credentials: Credentials, login: str, binding: bytes) -> str | None:
    try:
        seed = credentials.organizer_seeds().get(login)
    except (ValueError, RuntimeError, OSError):
        return None
    return seed if seed and hmac.compare_digest(seed_binding(seed), binding) else None


def verified_step(seed: str, otp: object, now_ms: int, last_step: int) -> int | None:
    if not isinstance(otp, str) or not re.fullmatch(r'[0-9]{6}', otp):
        return None
    current = now_ms // 30000
    generator = pyotp.TOTP(seed)
    for step in (current, current - 1, current + 1):
        if step >= 0 and step > last_step and hmac.compare_digest(generator.generate_otp(step), otp):
            return step
    return None


def activate(event, enrollment, otp):
    """Called locally by someone with filesystem access. No HTTP management route."""
    from .service import scrypt_hash

    if not isinstance(enrollment, dict):
        raise ValueError('Invalid enrollment file')
    login = username(enrollment.get('username'))
    name, code, seed = (enrollment.get(field) for field in ('name', 'code', 'seed'))
    if (not login or not isinstance(name, str) or not 1 <= len(name.strip()) <= 100
            or not isinstance(code, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', code)
            or not isinstance(seed, str) or not TOTP_SEED.fullmatch(seed)):
        raise ValueError('Invalid enrollment file')
    if verified_step(seed, otp, event.clock(), -1) is None:
        raise ValueError('Authenticator verification failed')
    salt = secrets.token_bytes(16)
    password_hash = scrypt_hash(code, salt)
    with event.db() as db:
        db.execute('BEGIN IMMEDIATE')
        step = verified_step(seed, otp, event.clock(), -1)
        if step is None:
            raise ValueError('Authenticator verification expired')
        installed = event.credentials.organizer_seeds().get(login)
        if not installed or not hmac.compare_digest(seed, installed):
            raise ValueError('Install the matching organizer credential before activation')
        if db.execute('SELECT 1 FROM named_admins WHERE username=?', (login,)).fetchone():
            raise ValueError('This organizer username is already enrolled')
        account_id = 'GA' + secrets.token_hex(8)
        db.execute("""INSERT INTO accounts(id,role,name,salt,code_hash,active,created_at)
                      VALUES (?,'admin',?,?,?,1,?)""",
                   (account_id, name.strip(), salt, password_hash, event.clock()))
        db.execute('INSERT INTO named_admins(account_id,username,seed_binding,last_step,portal_confirmed) VALUES (?,?,?,?,0)',
                   (account_id, login, seed_binding(seed), step))
        db.execute('INSERT INTO credential_overrides(account_id, rotated_at) VALUES (?,?)',
                   (account_id, event.clock()))
        event.audit(db, {'id': account_id, 'role': 'admin'}, 'admin.enrolled', account_id)
    return account_id


def confirm_portal_login(event, db, account, step):
    """Called inside the session-issuance transaction after the platform verified the proof."""
    first_login = db.execute("UPDATE named_admins SET portal_confirmed=1 WHERE account_id=? AND portal_confirmed=0",
                             (account['id'],)).rowcount
    db.execute("UPDATE named_admins SET last_step=? WHERE account_id=?", (step, account['id']))
    if first_login:
        legacy = "SELECT a.id FROM accounts a LEFT JOIN named_admins n ON n.account_id=a.id WHERE a.role='admin' AND n.account_id IS NULL"
        db.execute('DELETE FROM sessions WHERE account_id IN (' + legacy + ')')
        db.execute('UPDATE accounts SET active=0 WHERE active=1 AND id IN (' + legacy + ')')
        event.audit(db, account, 'admin.portal-confirmed', account['id'])


def revoke(event, login, *, offline_recovery=False):
    login = username(login)
    with event.db() as db:
        db.execute('BEGIN IMMEDIATE')
        active = db.execute("""SELECT n.account_id,n.username,n.seed_binding,n.portal_confirmed FROM named_admins n
                               JOIN accounts a ON a.id=n.account_id WHERE a.active=1""").fetchall()
        target = next((account for account in active if account['username'] == login), None)
        if not target:
            raise ValueError('No such active named organizer')
        entries = event.credentials.organizer_seeds()
        usable = {a['username'] for a in active if a['username'] in entries
                  and hmac.compare_digest(a['seed_binding'], seed_binding(entries[a['username']]))}
        alternatives = usable - {login}
        confirmed_alternative = any(a['portal_confirmed'] and a['username'] in alternatives for a in active)
        recovering_lost_credential = offline_recovery and login not in usable and bool(alternatives)
        if not confirmed_alternative and not recovering_lost_credential:
            raise ValueError('Confirm another organizer through the portal before revoking the last usable account')
        db.execute('UPDATE accounts SET active=0 WHERE id=?', (target['account_id'],))
        db.execute('DELETE FROM sessions WHERE account_id=?', (target['account_id'],))
        event.audit(db, None, 'admin.revoked-locally', target['account_id'])


def main():
    from .service import EventService

    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    prepare = commands.add_parser('prepare')
    prepare.add_argument('--username', required=True)
    prepare.add_argument('--name', required=True)
    prepare.add_argument('--out', type=Path, required=True)
    enable = commands.add_parser('activate')
    enable.add_argument('--data-dir', type=Path, required=True)
    enable.add_argument('--enrollment', type=Path, required=True)
    disable = commands.add_parser('revoke')
    disable.add_argument('--data-dir', type=Path, required=True)
    disable.add_argument('--username', required=True)
    disable.add_argument('--offline-recovery', action='store_true',
                         help='Allow a staged replacement when recovering from a lost operator credential')
    export = commands.add_parser('export-checkpoint', help='Explicitly export an old development seed map')
    export.add_argument('--data-dir', required=True, type=Path)
    export.add_argument('--out', required=True, type=Path)
    migrate = commands.add_parser('migrate-checkpoint', help='Convert an offline development database')
    migrate.add_argument('--data-dir', required=True, type=Path)
    args = parser.parse_args()
    if args.command == 'prepare':
        bundle = prepare_enrollment(args.username, args.name)
        fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(bundle, stream, ensure_ascii=False)
            stream.write('\n')
        print('Private enrollment file created. Remove it after provisioning and activation.', file=sys.stderr)
        return
    if not (args.data_dir / 'event.sqlite3').is_file():
        parser.error('Choose an existing event data directory')
    if args.command in {'export-checkpoint', 'migrate-checkpoint'}:
        from .migrations.v002_admin_auth import convert_checkpoint, export_checkpoint
        try:
            if args.command == 'export-checkpoint':
                export_checkpoint(args.data_dir, args.out)
            else:
                convert_checkpoint(args.data_dir, Credentials(args.data_dir))
        except (RuntimeError, ValueError, OSError) as error:
            parser.error(str(error))
        return
    if args.command == 'revoke':
        try:
            revoke(EventService(data_dir=args.data_dir, check_organizer_credentials=False), args.username,
                   offline_recovery=args.offline_recovery)
        except ValueError as error:
            parser.error(str(error))
        print('Named organizer revoked.')
        return
    print('Remove the private enrollment file after delivering the access code and completing activation.',
          file=sys.stderr)
    fd = os.open(args.enrollment, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, encoding='utf-8') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.geteuid()
                or info.st_size > 4096):
            parser.error('Use a private regular enrollment file owned by the current user')
        bundle = json.load(stream)
    try:
        activate(EventService(data_dir=args.data_dir, check_organizer_credentials=False), bundle, getpass.getpass('Authenticator code: '))
    except ValueError as error:
        parser.error(str(error))
    print('Enrollment accepted. Complete activation by signing in to the organizer portal with a fresh code.')


if __name__ == '__main__':
    main()
