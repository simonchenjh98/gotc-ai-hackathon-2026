"""ASGI speech sessions with Gitee's realtime synthesis and transcription services."""
from __future__ import annotations

import asyncio
import json
import ssl
import unicodedata
from urllib.parse import urlencode, urlsplit, urlunsplit

from starlette.responses import JSONResponse
from starlette.websockets import WebSocket, WebSocketDisconnect, WebSocketState
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, WebSocketException

from backend.gotc.errors import APIError
from . import usage
from .asgi_context import GatewayContext
from .config import resolve
from .gateway_media import MediaGateway
from .gateway_pricing import PricingError, quote
from .protocol_errors import ClientGone, GatewayError, error_body, redact_error_payload
from .requests import gateway_json, reject_stored_file_references, request_text, validate_json_depth

MESSAGE_LIMIT = 1024 * 1024
SESSION_LIMIT = 64 * 1024 * 1024
SESSION_SECONDS = 300
IDLE_SECONDS = 60
SPEECH_PATHS = frozenset(('/v1/audio/speech/ws', '/v1/audio/transcriptions/ws'))
PROVIDER_TLS = ssl.create_default_context()
PROVIDER_TLS.minimum_version = ssl.TLSVersion.TLSv1_2


class ProviderConnect(connect):
    def process_redirect(self, exc):
        # A credential is scoped to the configured provider endpoint, including its path.
        return exc


def provider_connection(upstream, path, model, protocols):
    parts = urlsplit(upstream['url'])
    secure = parts.scheme == 'https'
    provider_path = parts.path.rstrip('/') + path.removeprefix('/v1')
    uri = urlunsplit(('wss' if secure else 'ws', parts.netloc, provider_path, urlencode({'model': model}), ''))
    return ProviderConnect(uri, ssl=PROVIDER_TLS if secure else None, subprotocols=protocols or None, proxy=None, compression=None,
                           additional_headers={'Authorization': 'Bearer ' + upstream['key']},
                           user_agent_header='gotc-gateway/1.0', max_size=MESSAGE_LIMIT, max_queue=4,
                           open_timeout=10, close_timeout=2, ping_interval=None)


def browser_credentials(websocket, ctx):
    headers = ctx.handler.headers
    for name in ('Authorization', 'x-api-key', 'Sec-WebSocket-Protocol', 'Content-Length'):
        if len(headers.get_all(name, [])) > 1:
            raise GatewayError('invalid', 'Duplicate WebSocket request field')
    if headers.get('Transfer-Encoding') is not None or headers.get('Content-Length', '0') != '0':
        raise GatewayError('invalid', 'WebSocket handshake must not contain a body')
    offered = websocket.scope.get('subprotocols', [])
    tokens = [p.removeprefix('openai-insecure-api-key.') for p in offered if p.startswith('openai-insecure-api-key.')]
    if len(tokens) > 1 or (tokens and (headers.get('Authorization') or headers.get('x-api-key'))):
        raise GatewayError('invalid', 'Use one authentication mechanism')
    if tokens:
        headers['Authorization'] = 'Bearer ' + tokens[0]
        ctx.call['secrets'] += tuple(tokens)
    # Only approved offers can reach the provider or the metering record.
    protocols = [p for p in offered if p in {'realtime', 'openai-beta.realtime-v1'}]
    if 'Sec-WebSocket-Protocol' in headers:
        del headers['Sec-WebSocket-Protocol']
    if protocols:
        headers['Sec-WebSocket-Protocol'] = ', '.join(protocols)
    return protocols


def validate_model(data, model):
    if isinstance(data, dict):
        if 'model' in data and data['model'] != model:
            raise ValueError('Changing model within a WebSocket session is not supported')
        for value in data.values():
            validate_model(value, model)
    elif isinstance(data, list):
        for value in data:
            validate_model(value, model)


def request_characters(message, call):
    data = json.loads(message)
    if not isinstance(data, dict):
        raise ValueError('Speech events must be JSON objects')
    validate_json_depth(data)
    gateway_json(data)
    reject_stored_file_references(data)
    MediaGateway._check_shared_state(data)
    validate_model(data, call['model'])
    text = request_text(data)
    for word in call['settings']['blockedWords']:
        if word.strip() and ' '.join(unicodedata.normalize('NFKC', word).split()).lower() in text:
            raise ValueError('Request contains a blocked word')
    value = data.get('text', data.get('input', ''))
    return len(value) if isinstance(value, str) else 0


def checked_size(message, bytes_so_far):
    size = len(message.encode('utf-8')) if isinstance(message, str) else len(message)
    if size > MESSAGE_LIMIT or size + bytes_so_far > SESSION_LIMIT:
        raise ValueError('WebSocket data limit exceeded')
    return size


async def relay(websocket, remote, gateway, ctx, evidence, first_event):
    call = ctx.call

    async def from_client():
        pending_first = first_event
        while True:
            async with asyncio.timeout(IDLE_SECONDS):
                event = await (asyncio.shield(pending_first) if pending_first is not None else websocket.receive())
            pending_first = None
            if event['type'] == 'websocket.disconnect':
                raise ClientGone
            message = event.get('text') if event.get('text') is not None else event['bytes']
            size = checked_size(message, call['request_bytes'])
            await ctx.sync(gateway.recheck_access, ctx.handler, call)
            characters = request_characters(message, call) if isinstance(message, str) else 0
            await remote.send(message)
            call['request_bytes'] += size
            call['pricing_quantities']['characters'] += characters

    async def from_provider():
        received_bytes = 0
        while True:
            try:
                async with asyncio.timeout(IDLE_SECONDS):
                    message = await remote.recv()
            except ConnectionClosed as closed:
                if closed.rcvd is None:
                    raise GatewayError('upstream', '上游实时语音连接中断') from None
                code = closed.rcvd.code
                reason = redact_error_payload(closed.rcvd.reason.encode('utf-8'), gateway.error_secrets(call))
                await websocket.close(code=1000 if code == 1005 else code, reason=reason[:123].decode('utf-8', 'ignore'))
                call['done'] = code in (1000, 1001, 1005) and not evidence.get('error')
                call['status'] = 101 if call['done'] else 502
                return
            size = checked_size(message, received_bytes)
            received_bytes += size
            if isinstance(message, bytes):
                await websocket.send_bytes(message)
            else:
                data = json.loads(message)
                if isinstance(data, dict):
                    if data.get('error') or 'error' in str(data.get('type', '')).lower():
                        evidence['error'] = True
                        message = redact_error_payload(message.encode('utf-8'), gateway.error_secrets(call)).decode('utf-8')
                    else:
                        usage.read_usage('openai', data, call['usage'])
                        if isinstance(data.get('usage'), dict):
                            evidence['usage'] = data['usage']
                await websocket.send_text(message)
                size = len(message.encode('utf-8'))
            call['reply_bytes'] += size

    incoming = asyncio.create_task(from_client())
    outgoing = asyncio.create_task(from_provider())
    try:
        async with asyncio.timeout(SESSION_SECONDS):
            done, _ = await asyncio.wait((incoming, outgoing), return_when=asyncio.FIRST_COMPLETED)
            # A provider's terminal event may coincide with the caller closing its end.
            if outgoing in done:
                outgoing.result()
            else:
                incoming.result()
    finally:
        for task in (incoming, outgoing):
            task.cancel()
        await asyncio.gather(incoming, outgoing, return_exceptions=True)


def settle(call, evidence, headers):
    if not call['done']:
        evidence['error'] = True
    try:
        call['billing'] = quote(call['pricing_snapshot'], request=call['pricing_request'], response=evidence,
                                headers=headers, started_at=call['requested_at'], trusted_quantities=call['pricing_quantities'])
    except PricingError:
        call['billing'] = {'cost_micro': None, 'currency': 'CNY', 'estimated': False, 'pending': True,
                           'details': {'reason': 'invalid_provider_billing_evidence'}}


async def speech_session(websocket, ctx, path, first_event):
    gateway = ctx.gateway
    call = ctx.call
    call.update(metered_media=True, stream=True, currency='CNY', pricing_quantities={'characters': 0})
    evidence, billing_headers = {}, {}
    try:
        upstream = (ctx.config or {}).get('openai', {})
        if path not in SPEECH_PATHS or not gateway.is_gitee(upstream):
            raise GatewayError('not_found', '接口不存在')
        query = websocket.query_params
        if set(query) != {'model'} or len(query.getlist('model')) != 1:
            raise GatewayError('invalid', 'Exactly one model query parameter is required')
        protocols = browser_credentials(websocket, ctx)
        async with ctx.admitted():
            model = resolve(upstream, query['model'])
            if model is None:
                raise GatewayError('not_found', '模型不在本活动开放列表中')
            try:
                snapshot = gateway.catalog.resolve(model, path, {})
            except PricingError:
                raise GatewayError('invalid', '实时语音模型定价不可用') from None
            call.update(model=model, requested_model=query['model'], pricing_snapshot=snapshot, pricing_request={'model': model})
            call['secrets'] += (upstream['key'],)
            try:
                remote = await provider_connection(upstream, path, model, protocols)
            except (OSError, WebSocketException, ValueError):
                raise GatewayError('upstream', '上游实时语音连接失败，请稍后重试') from None
            try:
                billing_headers = dict(remote.response.headers.raw_items())
                await websocket.accept(subprotocol=remote.subprotocol, headers=[
                    (b'x-client-request-id', call['request_id'].encode('ascii')), (b'cache-control', b'no-store'),
                ])
                call.update(headers_sent=True, forwarded=True, status=101)
                await relay(websocket, remote, gateway, ctx, evidence, first_event)
            finally:
                await remote.close()
    except asyncio.CancelledError:
        call['status'] = 499
        raise
    except (ClientGone, WebSocketDisconnect):
        call['status'] = 499
    except (APIError, WebSocketException, OSError, ValueError, RecursionError) as exc:
        if call['forwarded']:
            call['status'] = 502
            if websocket.application_state is WebSocketState.CONNECTED:
                try:
                    await websocket.close(code=1011)
                except WebSocketDisconnect:
                    pass
        else:
            error = exc if isinstance(exc, APIError) else GatewayError('upstream', '上游实时语音连接失败，请稍后重试')
            info = gateway.note_error(call, error)
            headers = {'X-Client-Request-ID': call['request_id'], 'Cache-Control': 'no-store'}
            if info.get('retry_after'):
                headers['Retry-After'] = str(info['retry_after'])
            await websocket.send_denial_response(JSONResponse(error_body(path, info), status_code=error.status, headers=headers))
    finally:
        if call['forwarded']:
            settle(call, evidence, billing_headers)
        await ctx.record()


async def serve_websocket(websocket: WebSocket, ctx: GatewayContext, path: str) -> None:
    # Watch for a caller leaving even while admission or the provider handshake is pending.
    # The same read supplies the first application message after acceptance.
    await websocket.receive()  # ASGI websocket.connect
    first = asyncio.create_task(websocket.receive())
    session = asyncio.create_task(speech_session(websocket, ctx, path, first))
    try:
        done, _ = await asyncio.wait((first, session), return_when=asyncio.FIRST_COMPLETED)
        if session not in done and first.result()['type'] == 'websocket.disconnect' and not ctx.call['forwarded']:
            return
        await session
    finally:
        first.cancel()
        session.cancel()
        await asyncio.gather(first, session, return_exceptions=True)
