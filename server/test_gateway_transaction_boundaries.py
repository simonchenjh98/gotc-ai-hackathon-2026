"""Gateway persistence and stream completion regressions; all upstreams are local fakes."""
import json
import sqlite3
import threading
import unittest
import zlib
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from unittest.mock import patch

import keys
import test_keys
from test_keys import FakeUpstream


class ConnectionProxy:
    def __init__(self, db):
        self.db = db

    def __getattr__(self, name):
        return getattr(self.db, name)


class GatewayTransactionTests(unittest.TestCase):
    setUp = test_keys.GatewayTests.setUp
    tearDown = test_keys.GatewayTests.tearDown
    team = test_keys.GatewayTests.team
    team_client = test_keys.GatewayTests.team_client
    enabled_key = test_keys.GatewayTests.enabled_key
    call = test_keys.GatewayTests.call
    latest = test_keys.GatewayTests.latest
    assertReleased = test_keys.GatewayTests.assertReleased
    sse = test_keys.GatewayTests.sse

    def test_real_sqlite_write_lock_retries_record_once_without_recalling_upstream(self):
        team_id, _, token = self.enabled_key()
        original_db = self.service.db
        attempts, failures = [], []
        snapshots = []
        original_record = self.service.keys.record

        class LockedOnce(ConnectionProxy):
            def execute(proxy, sql, args=()):
                if sql.startswith('INSERT INTO api_usage'):
                    attempts.append(1)
                    if len(attempts) == 1:
                        # A real competing SQLite writer; shorten only the test connection's timeout.
                        blocker = sqlite3.connect(self.service.db_path)
                        try:
                            blocker.execute('BEGIN IMMEDIATE')
                            proxy.db.execute('PRAGMA busy_timeout=20')
                            try:
                                return proxy.db.execute(sql, args)
                            except sqlite3.OperationalError as error:
                                failures.append(str(error))
                                raise
                        finally:
                            blocker.rollback()
                            blocker.close()
                return proxy.db.execute(sql, args)

        @contextmanager
        def locked_db():
            with original_db() as db:
                yield LockedOnce(db)

        def observe_record(handler, call):
            snapshots.append((tuple(call['usage']), tuple(call['response']), call['size']))
            return original_record(handler, call)

        with patch.object(self.service, 'db', locked_db), patch.object(self.service.keys, 'record', observe_record), \
                patch.object(keys.traceback, 'print_exc'):
            status, content = self.call('/v1/chat/completions', {'model': 'cheap', 'messages': []}, token)
            self.assertReleased(team_id)
        with original_db() as db:
            rows = list(db.execute('SELECT * FROM api_usage'))
            logs = list(db.execute('SELECT * FROM api_logs'))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(content)['choices'][0]['message']['content'], 'hi')
        self.assertEqual(failures, ['database is locked'])
        self.assertEqual(len(attempts), 2)
        self.assertEqual(len(FakeUpstream.calls), 1)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]['status'], rows[0]['input_tokens'], rows[0]['output_tokens'], rows[0]['cost_micro']),
                         (200, 2000, 1000, 4000))
        self.assertEqual(len(logs), 1)
        self.assertEqual(zlib.decompress(logs[0]['response']), content)
        self.assertGreaterEqual(len(snapshots), 2)
        self.assertTrue(all(snapshot == snapshots[0] for snapshot in snapshots))

    def test_usage_transaction_failure_rolls_back_then_records_once(self):
        team_id, _, token = self.enabled_key()
        original_db = self.service.db
        failed, committed = threading.Event(), threading.Event()

        class UsageWrite(ConnectionProxy):
            usage_written = False

            def execute(proxy, sql, args=()):
                result = proxy.db.execute(sql, args)
                if sql.startswith('INSERT INTO api_usage'):
                    proxy.usage_written = True
                return result

        @contextmanager
        def failed_transaction():
            with original_db() as db:
                proxy = UsageWrite(db)
                yield proxy
                if proxy.usage_written and not failed.is_set():
                    failed.set()
                    # Fail at transaction exit, after usage, last_used_at and content have all been written.
                    raise sqlite3.OperationalError('injected commit failure')
            if proxy.usage_written:
                committed.set()

        with patch.object(self.service, 'db', failed_transaction), patch.object(keys.traceback, 'print_exc'):
            status, content = self.call('/v1/chat/completions', {'model': 'cheap'}, token)
            self.assertTrue(committed.wait(1), 'request finalization must retry the rolled-back usage write')
            self.assertReleased(team_id)
        with original_db() as db:
            rows = list(db.execute('SELECT status,cost_micro FROM api_usage'))
            logs = list(db.execute('SELECT response FROM api_logs'))
        self.assertTrue(failed.is_set())
        self.assertEqual(status, 200)
        self.assertEqual(len(FakeUpstream.calls), 1)
        self.assertEqual([tuple(row) for row in rows], [(200, 4000)])
        self.assertEqual([zlib.decompress(row['response']) for row in logs], [content])

    def config_state(self):
        with self.service.db() as db:
            raw = self.service.keys.raw_config(db)
            audit_count = db.execute("SELECT COUNT(*) FROM audit WHERE action='api-upstreams.save'").fetchone()[0]
        return raw, self.service.keys.models(), audit_count

    def assert_config_failed_unchanged(self, previous, token):
        self.assertEqual(self.config_state(), previous)
        panel = self.admin.get('/api/admin/keys')[1]
        self.assertTrue(panel['enabled'])
        self.assertEqual(panel['models'], previous[1])
        self.assertEqual(self.call('/v1/chat/completions', {'model': 'cheap'}, token)[0], 200)

    def test_configuration_audit_failure_keeps_active_and_saved_configuration(self):
        _, _, token = self.enabled_key()
        previous = self.config_state()
        original_audit = self.service.audit

        def fail_audit(db, actor, action, *args, **kwargs):
            if action == 'api-upstreams.save':
                raise sqlite3.OperationalError('injected audit write failure')
            return original_audit(db, actor, action, *args, **kwargs)

        with patch.object(self.service, 'audit', fail_audit), patch.object(keys.traceback, 'print_exc'):
            self.assertEqual(self.admin.post('/api/admin/keys/upstreams', {'upstreams': {}})[0], 500)
        self.assert_config_failed_unchanged(previous, token)

    def test_configuration_commit_failure_keeps_active_and_saved_configuration(self):
        _, _, token = self.enabled_key()
        previous = self.config_state()
        original_db = self.service.db
        failed = threading.Event()

        class FailedCommit(ConnectionProxy):
            config_written = False

            def execute(proxy, sql, args=()):
                result = proxy.db.execute(sql, args)
                if sql.startswith('INSERT INTO gateway_config'):
                    proxy.config_written = True
                return result

            def commit(proxy):
                if proxy.config_written:
                    failed.set()
                    raise sqlite3.OperationalError('injected configuration commit failure')
                return proxy.db.commit()

        @contextmanager
        def failed_commit():
            with original_db() as db:
                proxy = FailedCommit(db)
                yield proxy
                if proxy.config_written:
                    proxy.commit()  # also exercise the old implementation's outer commit path

        with patch.object(self.service, 'db', failed_commit), patch.object(keys.traceback, 'print_exc'):
            self.assertEqual(self.admin.post('/api/admin/keys/upstreams', {'upstreams': {}})[0], 500)
        self.assertTrue(failed.is_set())
        self.assert_config_failed_unchanged(previous, token)

    def test_concurrent_configuration_saves_publish_in_commit_order(self):
        original_db = self.service.db
        first_committed, release_first = threading.Event(), threading.Event()
        second_written, second_committed = threading.Event(), threading.Event()

        class PausedCommit(ConnectionProxy):
            model = None

            def execute(proxy, sql, args=()):
                result = proxy.db.execute(sql, args)
                if sql.startswith('INSERT INTO gateway_config'):
                    proxy.model = next(iter(json.loads(args[0])['openai']['models']))
                    if proxy.model == 'second':
                        second_written.set()
                return result

            def commit(proxy):
                result = proxy.db.commit()
                if proxy.model == 'first':
                    first_committed.set()
                    if not release_first.wait(5):
                        raise RuntimeError('test did not release first publication')
                elif proxy.model == 'second':
                    second_committed.set()
                return result

        @contextmanager
        def paused_db():
            with original_db() as db:
                yield PausedCommit(db)

        def save(model):
            upstream = {'openai': {**self.upstreams['openai'], 'models': {model: [1, 2]}}}
            return self.admin.post('/api/admin/keys/upstreams', {'upstreams': upstream})

        with patch.object(self.service, 'db', paused_db), ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(save, 'first')
            try:
                self.assertTrue(first_committed.wait(3))
                second = executor.submit(save, 'second')
                self.assertTrue(second_written.wait(3))
                self.assertFalse(second_committed.wait(0.1), 'later config cannot publish ahead of the first')
            finally:
                release_first.set()
            for future, model in ((first, 'first'), (second, 'second')):
                status, result, _ = future.result(timeout=5)
                self.assertEqual(status, 200, result)
                self.assertEqual(result['models'], {'openai': [model]})
        self.assertTrue(second_committed.is_set())
        with original_db() as db:
            saved = self.service.keys.raw_config(db)
        self.assertEqual(list(saved['openai']['models']), ['second'])
        self.assertEqual(self.service.keys.models(), {'openai': ['second']})

    def test_nonterminal_usage_is_not_a_completion_signal(self):
        _, _, token = self.enabled_key()
        for choices in ([{'index': 0, 'delta': {'content': 'hi'}, 'finish_reason': None}], []):
            with self.subTest(choices=choices):
                payload = self.sse({'choices': choices, 'usage': {'prompt_tokens': 12, 'completion_tokens': 4}})
                FakeUpstream.reply = (200, 'text/event-stream', payload)
                status, content = self.call('/v1/chat/completions', {'model': 'cheap', 'stream': True}, token)
                self.assertEqual(status, 200)  # streaming headers have already been sent
                self.assertTrue(content.startswith(payload))
                self.assertIn(b'"error"', content[len(payload):])
                item = self.latest()
                self.assertEqual((item['status'], item['incomplete'], item['input'], item['output']), (502, 1, 12, 4))

    def test_inflight_count_tokens_uses_its_configuration_snapshot(self):
        _, _, token = self.enabled_key()
        authenticate = self.service.keys.authenticate

        def remove_upstream_after_authentication(handler, call=None):
            identity = authenticate(handler, call)
            status, result, _ = self.admin.post('/api/admin/keys/upstreams', {'upstreams': {}})
            self.assertEqual(status, 200, result)
            return identity

        with patch.object(self.service.keys, 'authenticate', remove_upstream_after_authentication), \
                patch.object(keys.traceback, 'print_exc'):
            status, content = self.call('/v1/messages/count_tokens', {'model': 'claude-x'}, token)
        self.assertEqual(status, 200, content)
        self.assertEqual(json.loads(content), {'input_tokens': 42})
        self.assertEqual((self.latest()['status'], self.latest()['cost']), (200, 0))
        self.assertEqual(len(FakeUpstream.calls), 1)
        self.assertEqual(self.call('/v1/messages/count_tokens', {'model': 'claude-x'}, token)[0], 404)

    def test_deep_gateway_json_is_a_client_error_without_upstream_or_charge(self):
        _, _, token = self.enabled_key()
        raw = b'{"model":"cheap","messages":' + b'[' * 5000 + b'0' + b']' * 5000 + b'}'
        status, result, _ = self.public.request('POST', '/v1/chat/completions', raw=raw,
                                               headers={'Authorization': 'Bearer ' + token})
        self.assertEqual(status, 400, result)
        self.assertEqual(len(FakeUpstream.calls), 0)
        item = self.latest()
        self.assertEqual((item['status'], item['cost']), (400, 0))
        self.assertEqual(self.call('/v1/chat/completions', {'model': 'cheap'}, token)[0], 200)

    def test_gateway_container_depth_boundary_preserves_nested_payload_and_literal_braces(self):
        _, _, token = self.enabled_key()
        leaf = b'"literal { [ ] } and \\"quoted\\" text"'

        def payload(nested):
            return (b'{"model":"cheap","messages":[],"metadata":' + b'{"nested":' * nested +
                    leaf + b'}' * nested + b'}')

        # Both inputs parse on supported runtimes: enforce our limit rather than a parser's recursion threshold.
        raw = payload(keys.JSON_DEPTH_LIMIT)
        json.loads(raw)
        status, result, _ = self.public.request('POST', '/v1/chat/completions', raw=raw,
                                               headers={'Authorization': 'Bearer ' + token})
        self.assertEqual(status, 400, result)
        self.assertEqual(result['error']['code'], 'invalid_request')
        self.assertEqual(len(FakeUpstream.calls), 0)
        self.assertEqual((self.latest()['status'], self.latest()['cost']), (400, 0))

        raw = payload(keys.JSON_DEPTH_LIMIT - 1)  # The root object counts as one container.
        status, result, _ = self.public.request('POST', '/v1/chat/completions', raw=raw,
                                               headers={'Authorization': 'Bearer ' + token})
        self.assertEqual(status, 200, result)
        self.assertEqual(len(FakeUpstream.calls), 1)
        self.assertEqual(FakeUpstream.calls[0][2]['metadata'], json.loads(raw)['metadata'])

    def test_platform_stream_terminal_events_and_truncation(self):
        _, _, token = self.enabled_key()
        chat = {'choices': [{'delta': {'content': 'hi'}, 'finish_reason': None}],
                'usage': {'prompt_tokens': 12, 'completion_tokens': 4}}
        anthropic_start = {'type': 'message_start', 'message': {'usage': {'input_tokens': 12, 'output_tokens': 1}}}
        anthropic_usage = {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn'}, 'usage': {'output_tokens': 4}}
        response_delta = {'type': 'response.output_text.delta', 'delta': 'hi',
                          'usage': {'input_tokens': 12, 'output_tokens': 4}}
        cases = [
            ('chat DONE after usage', '/v1/chat/completions', 'cheap', [chat, b'[DONE]'], True),
            ('chat finish_reason', '/v1/chat/completions', 'cheap', [chat, {'choices': [{'finish_reason': 'stop'}]}], True),
            ('anthropic terminal', '/v1/messages', 'claude-x', [anthropic_start, anthropic_usage, {'type': 'message_stop'}], True),
            ('anthropic no terminal', '/v1/messages', 'claude-x', [anthropic_start, anthropic_usage], False),
            ('responses completed', '/v1/responses', 'cheap', [response_delta, {'type': 'response.completed'}], True),
            ('responses incomplete terminal', '/v1/responses', 'cheap', [response_delta, {'type': 'response.incomplete'}], True),
            ('responses no terminal', '/v1/responses', 'cheap', [response_delta], False),
        ]
        for label, path, model, events, complete in cases:
            with self.subTest(label=label):
                payload = self.sse(*events)
                FakeUpstream.reply = (200, 'text/event-stream', payload)
                status, content = self.call(path, {'model': model, 'stream': True}, token)
                self.assertEqual(status, 200)
                if complete:
                    self.assertEqual(content, payload)
                else:
                    self.assertTrue(content.startswith(payload))
                    self.assertIn(b'error', content[len(payload):])
                item = self.latest()
                self.assertEqual((item['status'], item['incomplete'], item['estimated'], item['input'], item['output']),
                                 (200 if complete else 502, 0 if complete else 1, 0, 12, 4))


if __name__ == '__main__':
    unittest.main()
