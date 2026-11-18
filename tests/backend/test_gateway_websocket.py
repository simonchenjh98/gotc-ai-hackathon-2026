"""Speech sessions through a real Uvicorn Unix socket and a local synthetic provider."""
import asyncio
from http import HTTPStatus
from pathlib import Path
import ssl
import tempfile
import types
import unittest
from unittest import mock

import uvicorn
from websockets.asyncio.client import unix_connect
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed, InvalidStatus

from backend.gotc.errors import APIError
from backend.gotc.gateway import gateway_websocket as speech
from backend.gotc.gateway.asgi import create_gateway_app
from backend.gotc.gateway.gateway_pricing import Catalog, PricingError
from backend.gotc.gateway.protocol_errors import error_info


class SpeechGateway:
    def __init__(self):
        self.records = []
        self.refreshed = 0
        self.admitted = self.released = 0

    def refresh_config(self):
        self.refreshed += 1
        return self.config

    def authenticate(self, handler, call):
        if handler.headers.get('Authorization') != 'Bearer client-key':
            raise APIError(401, 'invalid key')
        call.update(key={'id': 'key', 'team_id': 'team'}, settings={'teamRpm': 60, 'blockedWords': []})
        return call['key']

    async def admit_async(self, team, rpm, count_tokens=False):
        self.admitted += 1

    def release(self, team, count_tokens=False):
        self.released += 1

    def recheck_access(self, handler, call):
        self.authenticate(handler, call)

    def safe_record(self, handler, call):
        if call['key'] and not call.get('recorded'):
            self.records.append(dict(call))
            call['recorded'] = True

    def error_secrets(self, call=None):
        return tuple((call or {}).get('secrets', ()))

    def note_error(self, call, error):
        call['status'] = error.status
        return error_info(error)


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.mode = 'echo'
        self.provider_requests = []
        self.provider_opening = asyncio.Event()
        self.resume_provider = asyncio.Event()
        self.provider_messages = []
        self.gateway = SpeechGateway()
        self.gateway.service = types.SimpleNamespace(origin='https://event.test', base_path='/gotc', keys=self.gateway)
        self.gateway.catalog = Catalog()
        self.gateway.is_gitee = lambda _: True
        self.provider = await serve(self.provider_session, '127.0.0.1', 0, compression=None,
                                    subprotocols=['realtime'], select_subprotocol=self.subprotocol,
                                    process_request=self.process_request, process_response=self.process_response)
        self.addAsyncCleanup(self.stop_provider)
        port = self.provider.sockets[0].getsockname()[1]
        self.gateway.config = {'openai': {'url': f'http://127.0.0.1:{port}/v1', 'key': 'private-upstream-key',
                                          'models': {'TeleTTS-Mandarin': [0, 0], 'TeleASR-MultiDialect': [0, 0]}, 'aliases': {}}}
        self.finished = asyncio.Event()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.socket_path = str(Path(self.directory.name, 'gateway.sock'))
        self.app = create_gateway_app(self.gateway.service, trusted_proxy_socket=self.socket_path)
        config = uvicorn.Config(self.observe_gateway, interface='asgi3', uds=self.socket_path, ws='websockets-sansio', ws_max_size=speech.MESSAGE_LIMIT,
                                ws_per_message_deflate=False, lifespan='off', access_log=False, log_level='critical')
        self.server = uvicorn.Server(config)
        self.server_task = asyncio.create_task(self.server.serve())
        self.addAsyncCleanup(self.stop_gateway)
        async with asyncio.timeout(3):
            while not self.server.started:
                if self.server_task.done():
                    self.server_task.result()
                await asyncio.sleep(0.01)

    async def stop_provider(self):
        self.provider.close()
        await self.provider.wait_closed()

    async def stop_gateway(self):
        self.server.should_exit = True
        await self.server_task

    def subprotocol(self, connection, offered):
        return 'realtime' if 'realtime' in offered else None

    async def process_request(self, connection, request):
        self.provider_requests.append(request)
        if self.mode == 'hold-handshake':
            self.provider_opening.set()
            await self.resume_provider.wait()
        if self.mode == 'reject':
            return connection.respond(HTTPStatus.UNAUTHORIZED, 'provider rejected')
        if self.mode == 'redirect':
            response = connection.respond(HTTPStatus.FOUND, 'redirect')
            response.headers['Location'] = '/other-endpoint'
            return response

    def process_response(self, connection, request, response):
        if self.mode == 'duplicate-negotiation':
            response.headers['Upgrade'] = 'websocket'
        if self.mode == 'zero':
            response.headers['Inference-Cost'] = '0'
        elif self.mode == 'immediate':
            response.headers['Inference-Cost'] = '0.123'
        return response

    async def provider_session(self, remote):
        if self.mode == 'immediate':
            await remote.send('{"usage":{"prompt_tokens":10,"completion_tokens":2}}')
            await remote.send(b'audio\x00bytes')
            await remote.close(1000, 'done')
            return
        if self.mode == 'error-close':
            await remote.close(1014, 'private-upstream-key')
            return
        if self.mode == 'hold':
            await remote.wait_closed()
            return
        try:
            message = await remote.recv()
        except ConnectionClosed:
            return
        self.provider_messages.append(message)
        if self.mode in ('error', 'zero'):
            await remote.send('{"error":{"message":"private-upstream-key"}}')
        elif self.mode == 'provider-close':
            await remote.close(1008, 'denied')
        else:
            await remote.send('{"usage":{"prompt_tokens":5,"completion_tokens":3}}')
        await remote.close()

    async def observe_gateway(self, scope, receive, send):
        try:
            await self.app(scope, receive, send)
        finally:
            self.finished.set()

    def connect(self, *, browser=False, headers=(), query='model=TeleTTS-Mandarin', path='/v1/audio/speech/ws'):
        protocols = ['realtime', 'openai-insecure-api-key.client-key'] if browser else []
        fields = list(headers) if browser else [('Authorization', 'Bearer client-key'), *headers]
        uri = f'ws://localhost/gotc{path}?{query}'
        return unix_connect(self.socket_path, uri=uri, subprotocols=protocols or None, additional_headers=fields,
                            compression=None, open_timeout=3, close_timeout=1)

    async def bridge(self, outgoing=None, **kwargs):
        self.finished.clear()
        replies = []
        async with self.connect(**kwargs) as client:
            self.assertEqual(client.response.headers['x-client-request-id'], client.response.headers['x-request-id'])
            self.assertEqual(client.subprotocol, 'realtime' if kwargs.get('browser') else None)
            if outgoing is not None:
                await client.send(outgoing)
            async with asyncio.timeout(3):
                try:
                    while True:
                        replies.append(await client.recv())
                except ConnectionClosed as closed:
                    self.close_code, self.close_reason = client.close_code, client.close_reason
                    self.assertIsNotNone(closed.rcvd)
        await asyncio.wait_for(self.finished.wait(), timeout=3)
        self.call = self.gateway.records[-1]
        self.assertEqual(self.gateway.admitted, self.gateway.released)
        return replies

    async def test_early_provider_messages_and_cost(self):
        self.mode = 'immediate'
        replies = await self.bridge()
        self.assertEqual(replies, ['{"usage":{"prompt_tokens":10,"completion_tokens":2}}', b'audio\x00bytes'])
        self.assertEqual(self.call['billing']['cost_micro'], 123000)
        self.assertEqual(self.call['usage'][:2], [10, 2])
        self.assertTrue(self.call['done'])
        self.assertEqual(len(self.gateway.records), 1)

    async def test_browser_credential_is_only_used_for_gateway_authentication(self):
        text = '{"input":"你好"}'
        await self.bridge([text[:5], text[5:]], browser=True, headers=[('Cookie', 'participant=session')])
        request = self.provider_requests[0]
        self.assertEqual(request.path, '/v1/audio/speech/ws?model=TeleTTS-Mandarin')
        self.assertEqual(request.headers['Authorization'], 'Bearer private-upstream-key')
        self.assertEqual(request.headers['Sec-WebSocket-Protocol'], 'realtime')
        self.assertNotIn('Cookie', request.headers)
        self.assertNotIn('client-key', repr(list(request.headers.raw_items())))
        self.assertEqual(self.provider_messages, [text])
        self.assertEqual(self.call['request_bytes'], len(text.encode()))
        self.assertEqual(self.call['pricing_quantities'], {'characters': 2})

    async def test_transcription_forwards_binary_and_requires_provider_duration_for_pricing(self):
        await self.bridge(b'audio-bytes', path='/v1/audio/transcriptions/ws', query='model=TeleASR-MultiDialect')
        self.assertEqual(self.provider_messages, [b'audio-bytes'])
        self.assertEqual(self.provider_requests[0].path, '/v1/audio/transcriptions/ws?model=TeleASR-MultiDialect')
        self.assertTrue(self.call['done'])
        self.assertTrue(self.call['billing']['pending'])

    async def test_failed_and_successful_billing(self):
        for mode in ('error', 'provider-close', 'zero', 'echo'):
            with self.subTest(mode=mode):
                self.mode = mode
                replies = await self.bridge('{"input":"hello world!"}')
                self.assertNotIn('private-upstream-key', repr(replies))
                fee = self.call['billing']
                if mode in ('error', 'provider-close'):
                    self.assertTrue(fee['pending'])
                    self.assertIsNone(fee['cost_micro'])
                else:
                    self.assertEqual(fee['cost_micro'], 0 if mode == 'zero' else 2880)
                    self.assertEqual(fee['estimated'], mode == 'echo')

    async def test_registered_1014_close_reason_is_redacted(self):
        self.mode = 'error-close'
        await self.bridge()
        self.assertEqual(self.close_code, 1014)
        self.assertNotIn('private-upstream-key', self.close_reason)
        self.assertEqual(self.call['status'], 502)
        self.assertFalse(self.call['done'])

    async def test_client_abort_and_session_deadline_release_admission(self):
        self.mode = 'hold'
        async with self.connect() as client:
            pong = await client.ping(b'client-ping')
            await asyncio.wait_for(pong, timeout=1)
        await asyncio.wait_for(self.finished.wait(), timeout=2)
        self.assertEqual(self.gateway.records[-1]['status'], 499)
        self.assertTrue(self.gateway.records[-1]['billing']['pending'])
        with mock.patch.object(speech, 'SESSION_SECONDS', 0.1):
            await self.bridge()
        self.assertFalse(self.call['done'])
        self.assertEqual(self.call['status'], 502)

    async def test_caller_disappearing_during_provider_handshake_releases_capacity(self):
        self.mode = 'hold-handshake'
        connecting = asyncio.ensure_future(self.connect())
        try:
            await asyncio.wait_for(self.provider_opening.wait(), timeout=2)
            connecting.cancel()
            await asyncio.gather(connecting, return_exceptions=True)
            await asyncio.wait_for(self.finished.wait(), timeout=1)
            self.assertEqual(self.gateway.admitted, self.gateway.released)
            self.assertEqual(self.gateway.records[-1]['status'], 499)
            self.assertFalse(self.gateway.records[-1]['forwarded'])
        finally:
            self.resume_provider.set()

    async def test_text_policy_revocation_and_byte_limits(self):
        for message in ('{"model":"other"}', '{"file_id":"foreign"}', b'abc'):
            with self.subTest(message=message), mock.patch.object(speech, 'SESSION_LIMIT', 2 if isinstance(message, bytes) else 65536):
                await self.bridge(message)
                self.assertEqual(self.provider_messages, [])
                self.assertFalse(self.call['done'])
        original = self.gateway.recheck_access
        count = 0
        def revoke(handler, call):
            nonlocal count
            count += 1
            if count == 2:
                raise APIError(401, 'revoked')
            original(handler, call)
        self.gateway.recheck_access = revoke
        await self.bridge(b'audio')
        self.assertEqual(count, 2)
        self.assertEqual(self.provider_messages, [])

    async def test_failed_negotiation_is_http_502_without_redirects(self):
        for i, mode in enumerate(('reject', 'redirect', 'duplicate-negotiation'), start=1):
            with self.subTest(mode=mode):
                self.mode = mode
                self.finished.clear()
                with self.assertRaises(InvalidStatus) as failed:
                    await self.connect()
                self.assertEqual(failed.exception.response.status_code, 502)
                await asyncio.wait_for(self.finished.wait(), timeout=2)
                self.assertEqual(len(self.provider_requests), i)
                self.assertFalse(self.gateway.records[-1]['forwarded'])
                self.assertEqual(self.gateway.admitted, self.gateway.released)

    async def test_denied_origin_and_ambiguous_query_never_open_provider(self):
        for kwargs, status in (({'headers': [('Origin', 'https://other.test')]}, 403),
                               ({'query': 'model=a&model=b'}, 400),
                               ({'browser': True, 'headers': [('x-api-key', 'other-team')]}, 400)):
            with self.subTest(status=status), self.assertRaises(InvalidStatus) as failed:
                await self.connect(**kwargs)
            self.assertEqual(failed.exception.response.status_code, status)
        self.assertEqual(self.provider_requests, [])
        self.assertEqual(self.gateway.refreshed, 2)  # Origin refusal precedes the DB configuration lookup.

    async def test_invalid_billing_evidence_releases_team_slot(self):
        self.mode = 'immediate'
        with mock.patch.object(speech, 'quote', side_effect=PricingError('invalid fee')):
            await self.bridge()
        self.assertTrue(self.call['billing']['pending'])

    def test_tls_policy_and_private_headers(self):
        with mock.patch.object(speech, 'ProviderConnect') as connector:
            speech.provider_connection({'url': 'https://provider.test/v1', 'key': 'synthetic'},
                                       '/v1/audio/transcriptions/ws', 'model', [])
        args, kwargs = connector.call_args
        self.assertEqual(args, ('wss://provider.test/v1/audio/transcriptions/ws?model=model',))
        self.assertGreaterEqual(kwargs['ssl'].minimum_version, ssl.TLSVersion.TLSv1_2)
        self.assertEqual(kwargs['additional_headers'], {'Authorization': 'Bearer synthetic'})
        self.assertIsNone(kwargs['proxy'])
