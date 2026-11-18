"""Local organizer enrollment and single-use TOTP verification."""
from __future__ import annotations

import argparse
import getpass
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import stat

import pyotp

USERNAME = re.compile(r'[a-z0-9][a-z0-9._-]{0,63}', re.ASCII)


def username(value):
    if isinstance(value, str) and value.isascii() and USERNAME.fullmatch(value.lower()):
        return value.lower()
    return None


def prepare_enrollment(login, name):
    login = username(login)
    if not login or not isinstance(name, str) or not 1 <= len(name.strip()) <= 100:
        raise ValueError('A valid username and display name are required')
    return {'username': login, 'name': name.strip(),
            'code': secrets.token_urlsafe(32), 'seed': pyotp.random_base32()}


def verified_step(seed, otp, now_ms, last_step):
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
            or not isinstance(seed, str) or not re.fullmatch(r'[A-Z2-7]{32}', seed)):
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
        if db.execute('SELECT 1 FROM named_admins WHERE username=?', (login,)).fetchone():
            raise ValueError('This organizer username is already enrolled')
        account_id = 'GA' + secrets.token_hex(8)
        db.execute("""INSERT INTO accounts(id,role,name,salt,code_hash,active,created_at)
                      VALUES (?,'admin',?,?,?,1,?)""",
                   (account_id, name.strip(), salt, password_hash, event.clock()))
        db.execute('INSERT INTO named_admins VALUES (?,?,?,?)', (account_id, login, seed, step))
        db.execute('INSERT INTO credential_overrides(account_id, rotated_at) VALUES (?,?)',
                   (account_id, event.clock()))
        # Retain named organizers and historic audit references. Once activated,
        # legacy credentials and already issued legacy/delegated sessions expire together.
        legacy_admins = "SELECT a.id FROM accounts a LEFT JOIN named_admins n ON n.account_id=a.id WHERE a.role='admin' AND n.account_id IS NULL"
        db.execute('DELETE FROM sessions WHERE account_id IN (' + legacy_admins + ')')
        db.execute('UPDATE accounts SET active=0 WHERE active=1 AND id IN (' + legacy_admins + ')')
        event.audit(db, {'id': account_id, 'role': 'admin'}, 'admin.enrolled', account_id)
    return account_id


def revoke(event, login):
    login = username(login)
    with event.db() as db:
        db.execute('BEGIN IMMEDIATE')
        active = db.execute("""SELECT n.account_id,n.username FROM named_admins n
                               JOIN accounts a ON a.id=n.account_id WHERE a.active=1""").fetchall()
        target = next((account for account in active if account['username'] == login), None)
        if not target:
            raise ValueError('No such active named organizer')
        if len(active) == 1:
            raise ValueError('Activate another named organizer before revoking the last one')
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
    args = parser.parse_args()
    if args.command == 'prepare':
        bundle = prepare_enrollment(args.username, args.name)
        fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(bundle, stream, ensure_ascii=False)
            stream.write('\n')
        return
    if not (args.data_dir / 'event.sqlite3').is_file():
        parser.error('Choose an existing event data directory')
    if args.command == 'revoke':
        try:
            revoke(EventService(data_dir=args.data_dir), args.username)
        except ValueError as error:
            parser.error(str(error))
        print('Named organizer revoked.')
        return
    fd = os.open(args.enrollment, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, encoding='utf-8') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.geteuid()
                or info.st_size > 4096):
            parser.error('Use a private regular enrollment file owned by the current user')
        bundle = json.load(stream)
    try:
        activate(EventService(data_dir=args.data_dir), bundle, getpass.getpass('Authenticator code: '))
    except ValueError as error:
        parser.error(str(error))
    print('Named organizer activated.')


if __name__ == '__main__':
    main()
