"""Claim email, recovery and school-list behavior with synthetic local data only."""
import sqlite3
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import urlsplit

from backend.gotc.teams.claim_email import DeliveryError, MAX_ATTEMPTS, RECIPIENT_DAILY_LIMIT, RETENTION_MS
import test_auto_claims
import test_online_claims
import test_open_claims


SMTP = {'host': 'smtp.example.test', 'port': 465, 'username': 'noreply@example.test', 'password': 'synthetic-password',
        'from': 'noreply@example.test', 'security': 'tls'}


class ClaimEmailTests(unittest.TestCase):
    tearDown = test_open_claims.OpenClaimsTests.tearDown
    admin_call = test_open_claims.OpenClaimsTests.admin_call
    bearer = test_open_claims.OpenClaimsTests.bearer
    application = test_open_claims.OpenClaimsTests.application
    approval = test_online_claims.OnlineClaimsTests.approval
    source = test_auto_claims.AutoClaimsTests.source

    def setUp(self):
        test_open_claims.OpenClaimsTests.setUp(self)
        self.mail = self.service.claims.mail
        self.mail.stop()
        self.mail.__dict__['settings'] = dict(SMTP)
        self.sent = []
        self.mail.sender = lambda recipient, subject, body: self.sent.append((recipient, subject, body))

    def rows(self, claim):
        with self.service.db() as db:
            return {row['kind']: dict(row) for row in db.execute(
                'SELECT * FROM claim_email_outbox WHERE request_id=?', (claim['requestId'],))}

    def request_row(self, claim):
        return next(row for row in self.admin_call('')[1]['requests'] if row['id'] == claim['requestId'])

    def test_pending_application_queues_one_private_receipt_email_without_storing_the_receipt(self):
        claim = self.application(email=' Captain@Example.TEST ', name='Injected\tName')
        self.assertEqual(self.rows(claim)['received']['status'], 'pending')
        self.assertNotIn('email', self.bearer(claim)[1])
        with sqlite3.connect(self.service.db_path) as db:
            self.assertNotIn(claim['secret'], '\n'.join(db.iterdump()))
        self.assertEqual(self.mail.run_once(), 1)
        recipient, subject, body = self.sent[0]
        self.assertEqual((recipient, subject), ('Captain@example.test', 'GOTC 队伍申请已收到'))
        self.assertIn(claim['secret'], body)
        self.assertNotIn('Injected', body)
        self.assertNotIn('Open Team', body)
        fragment = urlsplit(body.split('platform.html', 1)[1].split()[0]).fragment
        request_id, secret, pin, expires = fragment.removeprefix('claim/recover/').split('.')
        self.assertEqual((request_id, secret, pin, int(expires)),
                         (claim['requestId'], claim['secret'], claim['pin'], claim['expiresAt']))
        self.assertEqual(self.bearer({'requestId': request_id, 'secret': secret})[1]['status'], 'pending')
        self.assertEqual(self.rows(claim)['received']['status'], 'sent')
        self.assertEqual(self.mail.run_once(), 0)

    def test_email_is_validated_and_remains_optional_for_existing_api_clients(self):
        for value in ('', 'not-an-email', 'a@b', 'a@example.test\nBcc:x@example.test', 'a b@example.test', 7):
            with self.subTest(value=value):
                status, _, _ = self.public.post('/api/claims/request', {'name': 'N', 'school': 'S', 'email': value})
                self.assertEqual(status, 400)
        self.assertEqual(self.rows(self.application()), {})

    def test_manual_decisions_notify_the_applicant_and_show_delivery_to_organizers(self):
        approved = self.application('Approved', email='approved@example.test')
        self.mail.run_once()
        self.assertEqual(self.admin_call('/approve', self.approval(approved))[0], 200)
        self.mail.run_once()
        self.assertEqual(self.sent[-1][1], 'GOTC 队伍申请已通过')
        self.assertIn(approved['secret'], self.sent[-1][2])
        row = self.request_row(approved)
        self.assertEqual(row['email'], 'approved@example.test')
        self.assertEqual([(item['kind'], item['status']) for item in row['emailDelivery']],
                         [('received', 'sent'), ('approved', 'sent')])
        self.assertTrue(self.admin_call('')[1]['emailConfigured'])

        rejected = self.application('Rejected', email='rejected@example.test')
        self.mail.run_once()
        result = self.admin_call('/review', {'requestId': rejected['requestId'], 'expectedRevision': 1,
                                             'action': 'reject', 'note': 'Private staff note'})
        self.assertEqual(result[0], 200, result[1])
        self.mail.run_once()
        self.assertEqual(self.sent[-1][1], 'GOTC 队伍申请未通过')
        self.assertNotIn(rejected['secret'], self.sent[-1][2])
        self.assertNotIn('Private staff note', self.sent[-1][2])

    def test_listed_team_is_approved_automatically_and_unlisted_team_waits_for_review(self):
        self.source()
        approved = self.application(email='listed@example.test')
        self.assertEqual(approved['status'], 'approved')
        self.assertEqual(set(self.rows(approved)), {'approved'})
        waiting = self.application(email='waiting@example.test', members=[
            {'name': 'Open One', 'school': 'School B'}, {'name': 'Not Listed', 'school': 'School C'}], teamName='Waiting Team')
        self.assertEqual(waiting['status'], 'pending')
        self.assertEqual(set(self.rows(waiting)), {'received'})
        self.assertEqual(self.request_row(waiting)['status'], 'pending')

    def test_temporary_failures_retry_and_permanent_failures_stop(self):
        claim = self.application(email='retry@example.test')
        failures = [DeliveryError('delivery_failed')]

        def fail(*_args):
            raise failures[0]

        self.mail.sender = fail
        self.mail.run_once()
        row = self.rows(claim)['received']
        self.assertEqual((row['status'], row['attempts'], row['error']), ('pending', 1, 'delivery_failed'))
        self.assertGreater(row['next_attempt_at'], self.service.clock())
        with self.service.db() as db:
            db.execute('UPDATE claim_email_outbox SET next_attempt_at=0')
        failures[0] = DeliveryError('recipient_refused', permanent=True)
        self.mail.run_once()
        self.assertEqual(self.rows(claim)['received']['status'], 'failed')

        limited = self.application('Limited', email='limited@example.test')
        with self.service.db() as db:
            db.execute('UPDATE claim_email_outbox SET attempts=?,status=\'pending\',next_attempt_at=0 WHERE request_id=?',
                       (MAX_ATTEMPTS, limited['requestId']))
        self.mail.run_once()
        self.assertEqual(self.rows(limited)['received']['status'], 'failed')

    def test_recipient_limit_expiry_and_retention(self):
        for index in range(RECIPIENT_DAILY_LIMIT):
            self.application(f'Allowed{index}', email='same@example.test')
        limited = self.application('Blocked', email='SAME@example.test')
        self.assertEqual(self.rows(limited)['received']['status'], 'suppressed')

        expired = self.application('Expired', email='expired@example.test')
        with self.service.db() as db:
            db.execute('UPDATE claim_requests SET expires_at=? WHERE id=?', (self.service.clock() - 1, expired['requestId']))
        self.mail._cycles = 0
        self.mail.run_once(0)
        self.assertEqual(self.rows(expired)['received']['status'], 'expired')
        with self.service.db() as db:
            db.execute('UPDATE claim_requests SET expires_at=? WHERE id=?',
                       (self.service.clock() - RETENTION_MS - 1, expired['requestId']))
        self.mail._cycles = 0
        self.mail.run_once(0)
        self.assertEqual(self.rows(expired), {})
        with self.service.db() as db:
            self.assertIsNone(db.execute('SELECT 1 FROM claim_contacts WHERE request_id=?', (expired['requestId'],)).fetchone())

    def test_unconfigured_email_keeps_messages_waiting(self):
        self.mail.__dict__['settings'] = None
        claim = self.application(email='later@example.test')
        self.assertEqual(self.mail.run_once(), 0)
        self.assertEqual(self.rows(claim)['received']['status'], 'pending')
        self.assertFalse(self.admin_call('')[1]['emailConfigured'])

    def test_smtp_uses_verified_tls_and_invalid_settings_are_disabled(self):
        client = MagicMock()
        with patch('backend.gotc.teams.claim_email.smtplib.SMTP_SSL', return_value=client) as smtp:
            self.mail.smtp_send('team@example.test', 'Subject', 'Body')
        self.assertEqual(smtp.call_args.args, ('smtp.example.test', 465))
        self.assertTrue(smtp.call_args.kwargs['context'].check_hostname)
        session = client.__enter__.return_value
        session.login.assert_called_once_with('noreply@example.test', 'synthetic-password')
        self.assertEqual(session.send_message.call_args.args[0]['Auto-Submitted'], 'auto-generated')
        self.mail.__dict__.pop('settings')
        (self.service.data_dir / 'smtp.json').write_text('{"host":"smtp.example.test","port":25,"security":"none"}')
        self.assertFalse(self.mail.configured)

    def test_school_options_publish_only_enabled_eligible_school_names(self):
        self.assertEqual(self.public.get('/api/claims/schools')[1], {'schools': []})
        entries = [{'id': '1', 'name': 'Private Person', 'school': 'School A', 'eligible': True, 'notes': 'private'},
                   {'id': '2', 'name': 'Second Person', 'school': ' school  a ', 'eligible': True, 'notes': ''},
                   {'id': '3', 'name': 'Third Person', 'school': 'School B', 'eligible': True, 'notes': ''},
                   {'id': '4', 'name': 'Hidden Person', 'school': 'Hidden School', 'eligible': False, 'notes': ''}]
        self.source(entries, enable=False)
        self.assertEqual(self.public.get('/api/claims/schools')[1], {'schools': []})
        self.source(entries)
        status, body, _ = self.public.get('/api/claims/schools')
        self.assertEqual((status, body), (200, {'schools': ['School A', 'School B']}))


if __name__ == '__main__':
    unittest.main()
