"""Shared upstream files must not be readable with a team's gateway key."""
import json
import unittest

import test_keys


class GatewayFileReferenceTests(unittest.TestCase):
    setUp = test_keys.GatewayTests.setUp
    tearDown = test_keys.GatewayTests.tearDown
    team = test_keys.GatewayTests.team
    team_client = test_keys.GatewayTests.team_client
    enabled_key = test_keys.GatewayTests.enabled_key
    call = test_keys.GatewayTests.call
    latest = test_keys.GatewayTests.latest
    assertReleased = test_keys.GatewayTests.assertReleased

    def assert_blocked(self, path, body, team, key):
        before = len(test_keys.FakeUpstream.calls)
        status, payload = self.call(path, body, key)
        self.assertEqual(status, 400, payload)
        self.assertEqual(len(test_keys.FakeUpstream.calls), before)
        item = self.latest()
        self.assertEqual((item['status'], item['cost'], item['estimated']), (400, 0, 0))
        self.assertReleased(team)
        self.assertEqual(self.service.keys.counting[team], 0)

    def assert_forwarded(self, path, body, key):
        before = len(test_keys.FakeUpstream.calls)
        status, payload = self.call(path, body, key)
        self.assertEqual(status, 200, payload)
        self.assertEqual(len(test_keys.FakeUpstream.calls), before + 1)
        return test_keys.FakeUpstream.calls[-1][2]

    def test_responses_reject_shared_file_and_image_ids(self):
        team, _, key = self.enabled_key()
        for kind in ('input_file', 'input_image'):
            with self.subTest(kind=kind):
                self.assert_blocked('/v1/responses', {
                    'model': 'cheap',
                    'input': [{'role': 'user', 'content': [{'type': kind, 'file_id': 'file-another-team'}]}],
                }, team, key)

    def test_chat_completions_reject_shared_file_ids(self):
        team, _, key = self.enabled_key()
        self.assert_blocked('/v1/chat/completions', {
            'model': 'cheap',
            'messages': [{'role': 'user', 'content': [
                {'type': 'file', 'file': {'file_id': 'file-another-team'}},
            ]}],
        }, team, key)

    def test_responses_reject_shared_computer_screenshot_file(self):
        team, _, key = self.enabled_key()
        self.assert_blocked('/v1/responses', {
            'model': 'cheap',
            'input': [{'type': 'computer_call_output', 'call_id': 'call_1',
                       'output': {'type': 'computer_screenshot', 'file_id': 'file-another-team'}}],
        }, team, key)

    def test_anthropic_rejects_container_upload_file_including_count_tokens(self):
        team, _, key = self.enabled_key()
        for path in ('/v1/messages', '/v1/messages/count_tokens'):
            with self.subTest(path=path):
                self.assert_blocked(path, {
                    'model': 'claude-x', 'max_tokens': 100,
                    'messages': [{'role': 'user', 'content': [
                        {'type': 'container_upload', 'file_id': 'file-another-team'},
                    ]}],
                }, team, key)

    def test_structured_tool_output_and_document_content_cannot_hide_file_ids(self):
        team, _, key = self.enabled_key()
        self.assert_blocked('/v1/responses', {
            'model': 'cheap',
            'input': [{'type': 'function_call_output', 'call_id': 'call_1', 'output': [
                {'type': 'input_file', 'file_id': 'file-another-team'},
            ]}],
        }, team, key)
        for path in ('/v1/messages', '/v1/messages/count_tokens'):
            with self.subTest(path=path):
                self.assert_blocked(path, {
                    'model': 'claude-x', 'max_tokens': 100,
                    'messages': [{'role': 'user', 'content': [{
                        'type': 'document', 'source': {'type': 'content', 'content': [
                            {'type': 'image', 'source': {'type': 'file', 'file_id': 'file-another-team'}},
                        ]},
                    }]}],
                }, team, key)

    def test_anthropic_rejects_file_sources_including_nested_tool_results_and_count_tokens(self):
        team, _, key = self.enabled_key()
        for path in ('/v1/messages', '/v1/messages/count_tokens'):
            for kind in ('image', 'document'):
                media = {'type': kind, 'source': {'type': 'file', 'file_id': 'file-another-team'}}
                for nested in (False, True):
                    content = [{'type': 'tool_result', 'tool_use_id': 'tool_1', 'content': [media]}] if nested else [media]
                    with self.subTest(path=path, kind=kind, nested=nested):
                        self.assert_blocked(path, {
                            'model': 'claude-x', 'max_tokens': 100,
                            'messages': [{'role': 'user', 'content': content}],
                        }, team, key)

    def test_inline_media_and_public_image_urls_still_forward(self):
        _, _, key = self.enabled_key()
        cases = [
            ('/v1/responses', {'model': 'cheap', 'input': [{'role': 'user', 'content': [
                {'type': 'input_file', 'filename': 'note.pdf', 'file_data': 'data:application/pdf;base64,eA=='},
                {'type': 'input_image', 'image_url': 'https://example.org/image.png'},
            ]}]}),
            ('/v1/chat/completions', {'model': 'cheap', 'messages': [{'role': 'user', 'content': [
                {'type': 'file', 'file': {'filename': 'note.pdf', 'file_data': 'data:application/pdf;base64,eA=='}},
                {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,eA=='}},
            ]}]}),
            ('/v1/messages', {'model': 'claude-x', 'max_tokens': 100, 'messages': [{'role': 'user', 'content': [
                {'type': 'document', 'source': {'type': 'base64', 'media_type': 'application/pdf', 'data': 'eA=='}},
                {'type': 'image', 'source': {'type': 'url', 'url': 'https://example.org/image.png'}},
            ]}]}),
        ]
        for path, body in cases:
            with self.subTest(path=path):
                sent = self.assert_forwarded(path, body, key)
                field = 'input' if path == '/v1/responses' else 'messages'
                self.assertEqual(sent[field], body[field])
        self.assert_forwarded('/v1/messages/count_tokens', cases[-1][1], key)

    def test_computer_screenshot_image_url_still_forwards(self):
        _, _, key = self.enabled_key()
        body = {
            'model': 'cheap',
            'input': [{'type': 'computer_call_output', 'call_id': 'call_1',
                       'output': {'type': 'computer_screenshot', 'image_url': 'data:image/png;base64,eA=='}}],
        }
        sent = self.assert_forwarded('/v1/responses', body, key)
        self.assertEqual(sent['input'], body['input'])

    def test_file_id_in_text_tool_arguments_and_schemas_is_not_a_file_reference(self):
        _, _, key = self.enabled_key()
        example = {'type': 'input_file', 'file_id': 'file-owned-by-client'}
        schema = {'type': 'object', 'properties': {'file_id': {'type': 'string'}}, 'examples': [example]}
        cases = [
            ('/v1/responses', {
                'model': 'cheap',
                'input': [
                    {'role': 'user', 'content': [{'type': 'input_text', 'text': json.dumps(example)}]},
                    {'type': 'function_call', 'name': 'inspect', 'call_id': 'call_1', 'arguments': json.dumps(example)},
                    {'type': 'function_call_output', 'call_id': 'call_1', 'output': json.dumps(example)},
                ],
                'tools': [{'type': 'function', 'name': 'inspect', 'parameters': schema}],
            }),
            ('/v1/chat/completions', {
                'model': 'cheap',
                'messages': [{'role': 'user', 'content': json.dumps(example)},
                             {'role': 'assistant', 'tool_calls': [{'id': 'call_1', 'type': 'function',
                              'function': {'name': 'inspect', 'arguments': json.dumps(example)}}]}],
                'tools': [{'type': 'function', 'function': {'name': 'inspect', 'parameters': schema}}],
            }),
            ('/v1/messages', {
                'model': 'claude-x', 'max_tokens': 100,
                'messages': [{'role': 'user', 'content': [{'type': 'text', 'text': json.dumps(example)}]},
                             {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'tool_1',
                              'name': 'inspect', 'input': example}]}],
                'tools': [{'name': 'inspect', 'input_schema': schema}],
            }),
        ]
        for path, body in cases:
            with self.subTest(path=path):
                sent = self.assert_forwarded(path, body, key)
                self.assertEqual(sent['tools'], body['tools'])
                field = 'input' if path == '/v1/responses' else 'messages'
                self.assertEqual(sent[field], body[field])
        self.assert_forwarded('/v1/messages/count_tokens', cases[-1][1], key)


if __name__ == '__main__':
    unittest.main()
