"""Provider errors must not disclose the shared upstream credentials to teams."""
import json
import http.client
import threading
import unittest
from unittest import mock
from urllib.parse import quote

import keys
import test_keys


class GatewayErrorRedactionTests(unittest.TestCase):
    tearDown = test_keys.GatewayTests.tearDown
    team = test_keys.GatewayTests.team
    team_client = test_keys.GatewayTests.team_client
    enabled_key = test_keys.GatewayTests.enabled_key
    call = test_keys.GatewayTests.call
    latest = test_keys.GatewayTests.latest
    assertReleased = test_keys.GatewayTests.assertReleased
    sse = test_keys.GatewayTests.sse

    def setUp(self):
        test_keys.GatewayTests.setUp(self)
        # Opaque keys intentionally do not match the old sk-/Bearer heuristics.
        self.secrets = ('opaque/primary+credential-2026', 'opaque/secondary+credential-2026')
        for config, secret in zip(self.upstreams.values(), self.secrets):
            config['key'] = secret
        self.assertEqual(self.admin.post('/api/admin/keys/upstreams', {'upstreams': self.upstreams})[0], 200)

    @staticmethod
    def escaped(value):
        return ''.join(f'\\u{ord(char):04X}' if index % 2 else char for index, char in enumerate(value))

    def provider_error(self):
        return {'type': 'error', 'error': {
            'type': 'invalid_request_error', 'message': 'max_tokens is invalid; ' + self.secrets[0],
            'param': 'max_tokens', 'debug': [self.secrets[1], {'escaped': self.escaped(self.secrets[0]),
                                                         'url': quote(self.secrets[1], safe='')}],
            self.secrets[0]: 'credential appeared as an object key'}, 'request_id': 'req-safe-123'}

    def error_bytes(self):
        payload = json.dumps(self.provider_error()).encode()
        # A real JSON unicode escape is decoded by the JSON parser, unlike the nested string escape above.
        return payload.replace(self.secrets[1].encode(), self.escaped(self.secrets[1]).encode())

    def assert_redacted(self, value):
        text = value.decode() if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False)
        for secret in self.secrets:
            self.assertNotIn(secret, text)
            self.assertNotIn(self.escaped(secret), text)
            self.assertNotIn(quote(secret, safe=''), text)

    def assert_http_error_redacted(self, path, model):
        team, _, token = self.enabled_key()
        test_keys.FakeUpstream.reply = (400, 'application/json', self.error_bytes())
        status, content = self.call(path, {'model': model}, token)
        self.assertEqual(status, 400)
        self.assert_redacted(content)
        decoded = json.loads(content)
        self.assertEqual(decoded['error']['type'], 'invalid_request_error')
        self.assertEqual(decoded['error']['param'], 'max_tokens')
        self.assertEqual(decoded['request_id'], 'req-safe-123')
        self.assertIn('max_tokens is invalid', decoded['error']['message'])
        self.assertEqual(decoded['error']['debug'], ['***', {'escaped': '***', 'url': '***'}])
        self.assertEqual(decoded['error']['***'], 'credential appeared as an object key')
        item = self.latest()
        self.assertEqual((item['status'], item['cost']), (400, 0))
        self.assert_redacted(item)
        if path.endswith('count_tokens'):
            self.assertFalse(item['hasContent'])
        else:
            detail = self.admin.post('/api/admin/keys/log', {'id': item['id']})[1]
            self.assert_redacted(detail)
            self.assertEqual(json.loads(detail['response']), decoded)
        self.assertReleased(team)

    def test_chat_http_400_redacts_all_keys_and_preserves_error_fields(self):
        self.assert_http_error_redacted('/v1/chat/completions', 'cheap')

    def test_messages_http_400_redacts_all_keys_and_preserves_error_fields(self):
        self.assert_http_error_redacted('/v1/messages', 'claude-x')

    def test_count_tokens_http_400_redacts_all_keys_and_stays_free(self):
        self.assert_http_error_redacted('/v1/messages/count_tokens', 'claude-x')

    def test_midstream_error_is_redacted_before_relay_and_storage(self):
        team, _, token = self.enabled_key()
        start = {'type': 'message_start', 'message': {'usage': {'input_tokens': 10, 'output_tokens': 0}}}
        delta = {'type': 'content_block_delta', 'delta': {'type': 'text_delta', 'text': 'normal generated text'}}
        prefix = self.sse(start, delta)
        error = b'event: error\ndata: ' + self.error_bytes() + b'\n\n'
        test_keys.FakeUpstream.reply = (200, 'text/event-stream', prefix + error)
        status, content = self.call('/v1/messages', {'model': 'claude-x', 'stream': True}, token)
        self.assertEqual(status, 200)
        self.assertTrue(content.startswith(prefix))  # successful event bytes are unchanged
        self.assertEqual(content.count(b'event: error\n'), 1)
        event = json.loads(content[len(prefix):].split(b'data: ', 1)[1])
        self.assertEqual(event['error']['param'], 'max_tokens')
        self.assertEqual(event['error']['debug'], ['***', {'escaped': '***', 'url': '***'}])
        self.assert_redacted(content)
        item = self.latest()
        self.assertEqual(item['status'], 502)
        self.assertGreater(item['cost'], 0)
        self.assert_redacted(self.admin.post('/api/admin/keys/log', {'id': item['id']})[1])
        self.assertReleased(team)

    def test_key_rotation_during_upstream_call_does_not_disable_redaction(self):
        _, _, token = self.enabled_key()
        reply = test_keys.FakeUpstream.do_POST
        test_keys.FakeUpstream.reply = (400, 'application/json', self.error_bytes())

        def rotate_then_reply(handler):
            self.service.keys.config = None
            return reply(handler)

        with mock.patch.object(test_keys.FakeUpstream, 'do_POST', rotate_then_reply):
            status, content = self.call('/v1/messages', {'model': 'claude-x'}, token)
        self.assertEqual(status, 400)
        self.assert_redacted(content)
        self.assertEqual(json.loads(content)['error']['debug'][0], '***')

    def test_multiline_sse_errors_are_redacted_before_or_after_normal_output(self):
        _, _, token = self.enabled_key()
        error = self.provider_error()
        # No event name: the complete JSON object must be inspected before its earlier lines are sent.
        error = {'error': error['error'], 'request_id': error['request_id'], 'type': 'error'}
        multiline = b''.join(b'data: ' + line + b'\r\n' for line in json.dumps(error, indent=2).encode().splitlines()) + b'\r\n'
        prefix = self.sse({'type': 'content_block_delta', 'delta': {'type': 'text_delta', 'text': 'normal text'}})
        for before in (b'', prefix):
            with self.subTest(output_started=bool(before)):
                test_keys.FakeUpstream.reply = (200, 'text/event-stream', before + multiline)
                status, content = self.call('/v1/messages', {'model': 'claude-x', 'stream': True}, token)
                self.assertEqual(status, 200 if before else 502)
                self.assert_redacted(content)
                if before:
                    self.assertTrue(content.startswith(before))
                    event = json.loads(content[len(before):].split(b'data: ', 1)[1])
                    self.assertEqual(event['error']['param'], 'max_tokens')
                item = self.latest()
                self.assertEqual(item['status'], 502)
                self.assertEqual(item['cost'] > 0, bool(before))
                self.assert_redacted(self.admin.post('/api/admin/keys/log', {'id': item['id']})[1])

    def test_named_non_json_error_event_is_also_redacted(self):
        _, _, token = self.enabled_key()
        prefix = self.sse({'type': 'content_block_delta', 'delta': {'type': 'text_delta', 'text': 'normal text'}})
        error = b'event: error\ndata: provider failure: ' + self.secrets[0].encode() + b'\n\n'
        test_keys.FakeUpstream.reply = (200, 'text/event-stream', prefix + error)
        status, content = self.call('/v1/messages', {'model': 'claude-x', 'stream': True}, token)
        self.assertEqual(status, 200)
        self.assertTrue(content.startswith(prefix))
        self.assertIn(b'event: error\ndata: provider failure: ***\n\n', content)
        self.assert_redacted(content)

    def test_error_event_metadata_is_redacted_without_changing_success_events(self):
        _, _, token = self.enabled_key()
        prefix = self.sse({'type': 'content_block_delta', 'delta': {'type': 'text_delta', 'text': 'normal text'}})
        error = (b'event: error\nid: ' + self.secrets[0].encode() + b'\n: debug ' + self.escaped(self.secrets[1]).encode()
                 + b'\ndata: {"type":"error","error":{"type":"api_error","message":"upstream failed"}}\n\n')
        test_keys.FakeUpstream.reply = (200, 'text/event-stream', prefix + error)
        status, content = self.call('/v1/messages', {'model': 'claude-x', 'stream': True}, token)
        self.assertEqual(status, 200)
        self.assertTrue(content.startswith(prefix))
        self.assertIn(b'event: error\nid: ***\n: debug ***\n', content)
        self.assert_redacted(content)
        self.assert_redacted(self.admin.post('/api/admin/keys/log', {'id': self.latest()['id']})[1])

    def test_named_multiline_text_error_preserves_each_data_field(self):
        _, _, token = self.enabled_key()
        prefix = self.sse({'type': 'content_block_delta', 'delta': {'type': 'text_delta', 'text': 'normal text'}})
        for ending in (b'\n', b'\r\n'):
            with self.subTest(line_ending=ending):
                error = ending.join((b'event: error', b'data: failure ' + self.secrets[0].encode(),
                                     b'data: second error detail', b'', b''))
                expected = ending.join((b'event: error', b'data: failure ***', b'data: second error detail', b'', b''))
                test_keys.FakeUpstream.reply = (200, 'text/event-stream', prefix + error)
                status, content = self.call('/v1/messages', {'model': 'claude-x', 'stream': True}, token)
                self.assertEqual((status, content), (200, prefix + expected))
                # An SSE consumer joins the data fields; the second line must remain part of the error payload.
                data = b'\n'.join(line[6:] for line in content[len(prefix):].split(ending) if line.startswith(b'data: '))
                self.assertEqual(data, b'failure ***\nsecond error detail')
                self.assert_redacted(content)
                item = self.latest()
                self.assertEqual(item['status'], 502)
                detail = self.admin.post('/api/admin/keys/log', {'id': item['id']})[1]
                self.assertIn(expected.decode(), detail['response'])
                self.assert_redacted(detail)

    def test_oversized_event_is_rejected_without_forwarding_uninspected_bytes(self):
        _, _, token = self.enabled_key()
        prefix = self.sse({'type': 'content_block_delta', 'delta': {'type': 'text_delta', 'text': 'normal text'}})
        marker = b'UNINSPECTED-SENSITIVE-CONTENT'
        for before in (b'', prefix):
            for oversized in (b'data: ' + marker + b'x' * 1200, (b'data: ' + marker + b'\n') * 50):
                with self.subTest(output_started=bool(before), multiline=b'\n' in oversized):
                    test_keys.FakeUpstream.reply = (200, 'text/event-stream', before + oversized)
                    with mock.patch.object(keys, 'SSE_EVENT_CAP', 1024):
                        status, content = self.call('/v1/messages', {'model': 'claude-x', 'stream': True}, token)
                    self.assertEqual(status, 200 if before else 502)
                    self.assertNotIn(marker, content)
                    if before:
                        self.assertTrue(content.startswith(before))
                        self.assertIn(b'event: error\n', content)
                    item = self.latest()
                    self.assertEqual((item['status'], item['errorPhase'], item['errorOwner']), (502, 'upstream', 'provider'))
                    self.assertIn('SSE event buffer exceeded', item['errorMessage'])
                    detail = self.admin.post('/api/admin/keys/log', {'id': item['id']})[1]
                    self.assertNotIn(marker.decode(), detail['response'])
        test_keys.FakeUpstream.reply = (200, 'text/event-stream', (b': prelude\n\n' * 100))
        with mock.patch.object(keys, 'SSE_EVENT_CAP', 1024):
            status, _ = self.call('/v1/messages', {'model': 'claude-x', 'stream': True}, token)
        self.assertEqual(status, 502)

    def test_success_event_is_relayed_before_upstream_finishes(self):
        _, _, token = self.enabled_key()
        first = self.sse({'type': 'content_block_delta', 'delta': {'type': 'text_delta', 'text': 'first visible text'}})
        last = self.sse({'type': 'message_stop'})
        received = threading.Event()
        acknowledgements = []

        def upstream(handler):
            handler.rfile.read(int(handler.headers['Content-Length']))
            handler.send_response(200)
            handler.send_header('Content-Type', 'text/event-stream')
            handler.end_headers()
            handler.wfile.write(first)
            handler.wfile.flush()
            acknowledgements.append(received.wait(3))
            handler.wfile.write(last)
            handler.wfile.flush()

        with mock.patch.object(test_keys.FakeUpstream, 'do_POST', upstream):
            conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=10)
            try:
                conn.request('POST', self.service.base_path + '/v1/messages', json.dumps({'model': 'claude-x', 'stream': True}),
                             {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token})
                reply = conn.getresponse()
                self.assertEqual(reply.status, 200)
                self.assertEqual(reply.read(len(first)), first)
                received.set()
                self.assertEqual(reply.read(), last)
            finally:
                received.set()
                conn.close()
        self.assertEqual(acknowledgements, [True], 'gateway must relay complete events before upstream EOF')

    def test_success_and_non_sensitive_errors_remain_byte_identical(self):
        _, _, token = self.enabled_key()
        for status, content_type, payload in (
            (400, 'application/json', b'{ "error": {"message":"max_tokens: too large", "param":"max_tokens"} }'),
            (200, 'application/json', b'{ "choices": [{"message":{"content":"a sk-public-example in user text"}}], "usage":{"prompt_tokens":1,"completion_tokens":1}}'),
            (200, 'text/event-stream', self.sse({'choices': [{'delta': {'content': 'sk-public-example'}}]}, b'[DONE]', anthropic=False)),
        ):
            with self.subTest(status=status, content_type=content_type):
                test_keys.FakeUpstream.reply = (status, content_type, payload)
                received_status, received = self.call('/v1/chat/completions', {'model': 'cheap', 'stream': content_type == 'text/event-stream'}, token)
                self.assertEqual((received_status, received), (status, payload))

    def test_plain_text_and_mixed_json_url_escapes_are_sanitized(self):
        secret = 'ab/C+"D\\Ef123'
        mixed = ''.join(f'%{ord(char):02X}' if index % 3 == 0 else f'\\u{ord(char):04x}' if index % 3 == 1 else char
                        for index, char in enumerate(secret))
        payload = ('Provider failed: ' + mixed).encode()
        self.assertEqual(keys.redact_error_payload(payload, (secret,)), b'Provider failed: ***')


if __name__ == '__main__':
    unittest.main()
