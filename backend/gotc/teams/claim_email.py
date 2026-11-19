"""Durable team-application emails. Message text never includes applicant-supplied content."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from functools import cached_property
import logging
import re
import smtplib
import sqlite3
import ssl
import threading

from backend.gotc.errors import APIError

LOCAL = re.compile(r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*")
DOMAIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+")
BATCH = 3
INTERVAL_SECONDS = 5
MAX_ATTEMPTS = 5
RETRY_MINUTES = (1, 5, 30, 120)
LEASE_MS = 10 * 60 * 1000
RECIPIENT_DAILY_LIMIT = 6
RETENTION_MS = 30 * 86_400_000
BEIJING = timezone(timedelta(hours=8))
LOGGER = logging.getLogger('gotc.claim_email')


class DeliveryError(Exception):
    def __init__(self, code, permanent=False):
        super().__init__(code)
        self.code, self.permanent = code, permanent


class ClaimMail:
    def __init__(self, claims):
        self.c = claims
        self.s = claims.s
        self.sender = self.smtp_send
        self._stop = threading.Event()
        self._thread = None
        self._cycles = 0

    @staticmethod
    def clean(value):
        if not isinstance(value, str):
            raise APIError(400, '请填写有效的邮箱地址')
        value = value.strip()
        local, separator, domain = value.rpartition('@')
        if (not separator or len(value) > 254 or len(local) > 64 or not LOCAL.fullmatch(local)
                or not DOMAIN.fullmatch(domain)):
            raise APIError(400, '请填写有效的邮箱地址')
        return local + '@' + domain.lower()

    @cached_property
    def settings(self):
        try:
            value = self.s.credentials.read_json('smtp.json')
            if value is None:
                return None
            if not isinstance(value, dict) or set(value) - {'host', 'port', 'username', 'password', 'from', 'fromName', 'security'}:
                raise ValueError
            security = value.get('security', 'tls')
            valid = (isinstance(value.get('host'), str) and value['host'] and type(value.get('port')) is int
                     and 1 <= value['port'] <= 65535 and isinstance(value.get('username'), str)
                     and isinstance(value.get('password'), str) and security in {'tls', 'starttls'}
                     and isinstance(value.get('fromName', ''), str) and not re.search(r'[\r\n]', value.get('fromName', '')))
            if not valid:
                raise ValueError
            return {**value, 'from': self.clean(value.get('from')), 'security': security}
        except (ValueError, RuntimeError, APIError):
            LOGGER.error('{"event":"claim_email_configuration_invalid"}')
            return None

    @property
    def configured(self):
        return self.settings is not None

    def save_contact(self, db, request_id, email):
        db.execute('INSERT INTO claim_contacts VALUES (?,?,?,?)', (request_id, email, email.lower(), self.c.clock()))

    def notify(self, db, request_id, kind):
        contact = db.execute('SELECT normalized_email FROM claim_contacts WHERE request_id=?', (request_id,)).fetchone()
        if not contact:
            return
        now = self.c.clock()
        recent = db.execute("""SELECT COUNT(*) FROM claim_email_outbox o JOIN claim_contacts c ON c.request_id=o.request_id
            WHERE c.normalized_email=? AND o.request_id<>? AND o.created_at>? AND o.status<>'suppressed'""",
                            (contact['normalized_email'], request_id, now - 86_400_000)).fetchone()[0]
        status = 'suppressed' if recent >= RECIPIENT_DAILY_LIMIT else 'pending'
        db.execute("""INSERT OR IGNORE INTO claim_email_outbox(request_id,kind,status,next_attempt_at,created_at)
                      VALUES (?,?,?,?,?)""", (request_id, kind, status, now, now))

    def delivery(self, db):
        contacts = {row['request_id']: row['email'] for row in db.execute('SELECT request_id,email FROM claim_contacts')}
        states = {}
        for row in db.execute('SELECT request_id,kind,status,attempts,error FROM claim_email_outbox ORDER BY id'):
            states.setdefault(row['request_id'], []).append(
                {'kind': row['kind'], 'status': row['status'], 'attempts': row['attempts'], 'error': row['error']})
        return contacts, states

    def recovery_link(self, row):
        secret, pin = self.c.receipt_material(row['request_id'])
        value = '.'.join((row['request_id'], secret, pin, str(row['expires_at'])))
        return f'{self.s.origin}{self.s.base_path}/platform.html#claim/recover/{value}'

    def message(self, row):
        expires = datetime.fromtimestamp(row['expires_at'] / 1000, BEIJING).strftime('%Y-%m-%d %H:%M')
        reference = f'申请编号：{row["request_id"]}'
        notice = ('下面的链接等同领取凭据，请勿转发或公开。\n'
                  f'{self.recovery_link(row)}\n链接有效期至 {expires}（北京时间）。\n')
        ignore = '\n如果这不是你的操作，请忽略本邮件。\n'
        if row['kind'] == 'received':
            return 'GOTC 队伍申请已收到', ('我们已收到你的队伍参赛码申请，审核结果将通过邮件通知。\n'
                                       f'{reference}\n\n可通过以下链接在其他设备查看进度：\n{notice}{ignore}')
        if row['kind'] == 'approved':
            return 'GOTC 队伍申请已通过', ('你的队伍申请已通过审核，请打开链接领取本队参赛码。\n'
                                       f'{reference}\n\n{notice}{ignore}')
        return 'GOTC 队伍申请未通过', ('你的队伍申请未通过审核。如有疑问，请联系活动组织方。\n'
                                   f'{reference}\n{ignore}')

    def smtp_send(self, recipient, subject, body):
        config = self.settings
        message = EmailMessage()
        message['From'] = formataddr((config.get('fromName', 'GOTC 2026'), config['from']))
        message['To'] = recipient
        message['Subject'] = subject
        message['Date'] = formatdate(localtime=False)
        message['Message-ID'] = make_msgid(domain=config['from'].rpartition('@')[2])
        message['Auto-Submitted'] = 'auto-generated'
        message.set_content(body)
        context = ssl.create_default_context()
        try:
            if config['security'] == 'tls':
                client = smtplib.SMTP_SSL(config['host'], config['port'], timeout=20, context=context)
            else:
                client = smtplib.SMTP(config['host'], config['port'], timeout=20)
            with client as smtp:
                if config['security'] == 'starttls':
                    smtp.starttls(context=context)
                if config['username']:
                    smtp.login(config['username'], config['password'])
                smtp.send_message(message)
        except smtplib.SMTPRecipientsRefused:
            raise DeliveryError('recipient_refused', permanent=True) from None
        except smtplib.SMTPAuthenticationError:
            raise DeliveryError('authentication_failed') from None
        except (smtplib.SMTPException, OSError):
            raise DeliveryError('delivery_failed') from None

    def housekeeping(self, db, now):
        db.execute("""UPDATE claim_email_outbox SET status='expired' WHERE status IN ('pending','sending') AND kind<>'rejected'
                      AND request_id IN (SELECT id FROM claim_requests WHERE expires_at<=?)""", (now,))
        old = 'SELECT id FROM claim_requests WHERE expires_at<? LIMIT 200'
        db.execute(f'DELETE FROM claim_email_outbox WHERE request_id IN ({old})', (now - RETENTION_MS,))
        db.execute(f'DELETE FROM claim_contacts WHERE request_id IN ({old})', (now - RETENTION_MS,))

    def lease(self, now, limit):
        with self.s.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if self._cycles % 12 == 0:
                self.housekeeping(db, now)
            rows = [dict(row) for row in db.execute("""SELECT o.id,o.request_id,o.kind,o.attempts,c.email,r.status AS request_status,
                r.expires_at FROM claim_email_outbox o JOIN claim_contacts c ON c.request_id=o.request_id
                JOIN claim_requests r ON r.id=o.request_id
                WHERE o.status IN ('pending','sending') AND o.next_attempt_at<=? ORDER BY o.next_attempt_at,o.id LIMIT ?""", (now, limit))]
            ready = []
            for row in rows:
                current = {'received': {'pending'}, 'approved': {'approved', 'issued'}, 'rejected': {'rejected'}}[row['kind']]
                if row['request_status'] not in current:
                    db.execute("UPDATE claim_email_outbox SET status='suppressed' WHERE id=?", (row['id'],))
                elif row['kind'] != 'rejected' and row['expires_at'] <= now:
                    db.execute("UPDATE claim_email_outbox SET status='expired' WHERE id=?", (row['id'],))
                elif row['attempts'] >= MAX_ATTEMPTS:
                    db.execute("UPDATE claim_email_outbox SET status='failed' WHERE id=?", (row['id'],))
                else:
                    db.execute("UPDATE claim_email_outbox SET status='sending',attempts=attempts+1,next_attempt_at=? WHERE id=?",
                               (now + LEASE_MS, row['id']))
                    ready.append(row)
            return ready

    def finish(self, row, error=None):
        now = self.c.clock()
        with self.s.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if error is None:
                db.execute("UPDATE claim_email_outbox SET status='sent',sent_at=?,error='' WHERE id=?", (now, row['id']))
                return
            attempt = row['attempts'] + 1
            if error.permanent or attempt >= MAX_ATTEMPTS:
                db.execute("UPDATE claim_email_outbox SET status='failed',error=? WHERE id=?", (error.code, row['id']))
            else:
                delay = RETRY_MINUTES[min(attempt - 1, len(RETRY_MINUTES) - 1)] * 60_000
                db.execute("UPDATE claim_email_outbox SET status='pending',next_attempt_at=?,error=? WHERE id=?",
                           (now + delay, error.code, row['id']))

    def run_once(self, limit=BATCH):
        rows = self.lease(self.c.clock(), limit if self.configured else 0)
        self._cycles += 1
        for row in rows:
            subject, body = self.message(row)
            try:
                self.sender(row['email'], subject, body)
            except DeliveryError as error:
                LOGGER.warning('{"event":"claim_email_failed","reason":"%s"}', error.code)
                self.finish(row, error)
            else:
                self.finish(row)
        return len(rows)

    def _run(self):
        while not self._stop.wait(INTERVAL_SECONDS):
            try:
                self.run_once()
            except sqlite3.Error:
                LOGGER.warning('{"event":"claim_email_deferred"}')

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name='gotc-claim-email')
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join()
            self._thread = None
