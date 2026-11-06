"""Model discovery uses draft credentials without changing the live gateway."""
import json
import threading
from unittest import mock
import unittest

import test_keys


class UpstreamModelTests(unittest.TestCase):
    setUp = test_keys.GatewayTests.setUp
    tearDown = test_keys.GatewayTests.tearDown

    def detect(self, body, client=None, login_id=None):
        client = client or self.admin
        return client.request('POST', '/api/admin/keys/upstreams/models', body,
                              headers={'X-GOTC-Login': self.admin.login_id if login_id is None else login_id})

    def draft(self, kind='openai', key='fixture-draft-key'):
        return {'kind': kind, 'url': f'http://127.0.0.1:{self.fake.server_port}/v1', 'key': key}

    def test_unsaved_credentials_discover_both_protocols_without_persisting(self):
        self.assertEqual(self.admin.post('/api/admin/keys/upstreams', {'upstreams': {}})[0], 200)
        for kind, header, value in [('openai', 'Authorization', 'Bearer fixture-draft-key'),
                                    ('anthropic', 'X-Api-Key', 'fixture-draft-key')]:
            with self.subTest(kind=kind):
                status, result, _ = self.detect(self.draft(kind))
                self.assertEqual(status, 200)
                self.assertTrue(result['ok'])
                self.assertEqual(result['models'], ['model-a', 'model-b'])
                path, headers, _ = test_keys.FakeUpstream.calls[-1]
                self.assertEqual(path, '/v1/models')
                self.assertEqual(headers[header], value)
                if kind == 'anthropic':
                    self.assertEqual(headers['Anthropic-Version'], '2023-06-01')
                self.assertNotIn('fixture-draft-key', json.dumps(result))
        self.assertIsNone(self.service.keys.config)
        with self.service.db() as db:
            self.assertEqual(self.service.keys.raw_config(db), {})
            self.assertNotIn('fixture-draft-key', str(db.execute('SELECT * FROM audit').fetchall()))

    def test_saved_key_reuse_requires_the_same_host_and_port(self):
        self.assertEqual(self.detect(self.draft(key=''))[1]['models'], ['model-a', 'model-b'])
        self.assertEqual(test_keys.FakeUpstream.calls[-1][1]['Authorization'], 'Bearer sk-upstream-openai')
        before = len(test_keys.FakeUpstream.calls)
        changed = {**self.draft(key=''), 'url': 'http://localhost:1/v1'}
        self.assertEqual(self.detect(changed)[0], 400)
        self.assertEqual(len(test_keys.FakeUpstream.calls), before)

    def test_access_and_invalid_inputs_are_rejected_before_contacting_upstream(self):
        before = len(test_keys.FakeUpstream.calls)
        self.assertEqual(self.detect(self.draft(), client=self.public)[0], 401)
        self.assertEqual(self.detect(self.draft(), login_id='')[0], 428)
        self.assertEqual(self.detect(self.draft(), login_id='another-login')[0], 401)
        for bad in [{**self.draft(), 'kind': []}, {**self.draft(), 'url': 'http://remote.invalid/v1'},
                    {**self.draft(), 'url': 'https://:password@example.test/v1'},
                    {**self.draft(), 'url': 'https://example.test:bad/v1'},
                    {**self.draft(), 'key': 'bad\r\nheader'}, {**self.draft(), 'key': []}]:
            with self.subTest(body=bad):
                self.assertEqual(self.detect(bad)[0], 400)
        self.assertEqual(len(test_keys.FakeUpstream.calls), before)

    def test_error_messages_redact_draft_credentials_and_invalid_lists_fail_cleanly(self):
        def reply(handler):
            handler.send_response(401)
            handler.end_headers()
            handler.wfile.write(b'{"error":{"message":"invalid fixture-draft-key"}}')
        with mock.patch.object(test_keys.FakeUpstream, 'do_GET', reply):
            status, result, _ = self.detect(self.draft())
        self.assertEqual(status, 200)
        self.assertFalse(result['ok'])
        self.assertEqual(result['status'], 401)
        self.assertNotIn('fixture-draft-key', json.dumps(result))
        for payload in (b'not json', b'{"data":[]}', b'{"data":[{"id":3},{"id":"bad name"}]}'):
            def malformed(handler):
                handler.send_response(200)
                handler.end_headers()
                handler.wfile.write(payload)
            with mock.patch.object(test_keys.FakeUpstream, 'do_GET', malformed):
                self.assertFalse(self.detect(self.draft())[1]['ok'])

    def test_waiting_for_models_does_not_hold_the_database_writer_lock(self):
        entered, release = threading.Event(), threading.Event()
        original = test_keys.FakeUpstream.do_GET
        def wait(handler):
            entered.set()
            release.wait(5)
            original(handler)
        results = []
        with mock.patch.object(test_keys.FakeUpstream, 'do_GET', wait):
            worker = threading.Thread(target=lambda: results.append(self.detect(self.draft())))
            worker.start()
            try:
                self.assertTrue(entered.wait(3))
                with self.service.db() as db:
                    db.execute('PRAGMA busy_timeout=200')
                    db.execute('BEGIN IMMEDIATE')
                    db.execute("UPDATE settings SET value='\"catalog lookup in progress\"' WHERE key='keys.announcement'")
            finally:
                release.set()
                worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertTrue(results[0][1]['ok'])


if __name__ == '__main__':
    unittest.main()
