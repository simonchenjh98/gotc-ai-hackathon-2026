"""Bounded RFC 6455 bridge for the two documented Gitee speech routes."""
from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import socket
import ssl
import struct
import threading
import time
from urllib.parse import parse_qs, urlencode, urlsplit

from gateway_pricing import PricingError, quote

GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
FRAME_LIMIT = 1024 * 1024
SESSION_LIMIT = 64 * 1024 * 1024
SESSION_SECONDS = 300
IDLE_SECONDS = 60


class ProtocolError(ValueError):
    pass


def exact(stream, count):
    chunks = bytearray()
    while len(chunks) < count:
        part = stream.read(count - len(chunks))
        if not part:
            raise EOFError('WebSocket peer closed')
        chunks.extend(part)
    return bytes(chunks)


def read_frame(stream, masked):
    a, b = exact(stream, 2)
    final, opcode, has_mask, length = bool(a & 128), a & 15, bool(b & 128), b & 127
    if a & 112 or has_mask != masked or opcode not in (0, 1, 2, 8, 9, 10):
        raise ProtocolError('Invalid WebSocket frame')
    if length == 126:
        length = struct.unpack('!H', exact(stream, 2))[0]
        if length < 126:
            raise ProtocolError('Noncanonical frame length')
    elif length == 127:
        length = struct.unpack('!Q', exact(stream, 8))[0]
        if length < 65536 or length >> 63:
            raise ProtocolError('Invalid frame length')
    if length > FRAME_LIMIT or (opcode >= 8 and (not final or length > 125)):
        raise ProtocolError('WebSocket frame too large')
    mask = exact(stream, 4) if has_mask else b''
    data = exact(stream, length)
    if mask:
        data = bytes(c ^ mask[i % 4] for i, c in enumerate(data))
    if opcode == 8 and len(data) == 1:
        raise ProtocolError('Invalid close frame')
    if opcode == 8 and data:
        code = struct.unpack('!H', data[:2])[0]
        if code not in {1000, 1001, 1002, 1003, 1007, 1008, 1009, 1010, 1011, 1012, 1013, 1014} and not 3000 <= code <= 4999:
            raise ProtocolError('Invalid WebSocket close code')
        try:
            data[2:].decode('utf-8', errors='strict')
        except UnicodeError:
            raise ProtocolError('Invalid WebSocket close reason') from None
    return final, opcode, data


def frame(opcode, payload, masked=False):
    n = len(payload)
    prefix = bytes([128 | opcode, (128 if masked else 0) | (n if n < 126 else 126 if n < 65536 else 127)])
    if n >= 126:
        prefix += struct.pack('!H' if n < 65536 else '!Q', n)
    if masked:
        mask = os.urandom(4)
        return prefix + mask + bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
    return prefix + payload


def messages(stream, masked):
    """Reassemble a bounded message while preserving control frames."""
    pending, kind, total = bytearray(), None, 0
    while True:
        final, opcode, payload = read_frame(stream, masked)
        total += len(payload)
        if total > SESSION_LIMIT:
            raise ProtocolError('WebSocket session byte limit exceeded')
        if opcode >= 8:
            yield opcode, payload
            if opcode == 8:
                return
            continue
        if opcode:
            if kind is not None:
                raise ProtocolError('Interleaved fragmented messages')
            kind = opcode
        elif kind is None:
            raise ProtocolError('Unexpected continuation')
        pending.extend(payload)
        if len(pending) > FRAME_LIMIT:
            raise ProtocolError('WebSocket message too large')
        if final:
            payload = bytes(pending)
            if kind == 1:
                payload.decode('utf-8', errors='strict')
            yield kind, payload
            pending, kind = bytearray(), None


def validate_handshake(handler):
    for name in ('Upgrade', 'Connection', 'Sec-WebSocket-Version', 'Sec-WebSocket-Key',
                 'Content-Length', 'Authorization', 'x-api-key', 'Sec-WebSocket-Protocol'):
        if len(handler.headers.get_all(name, [])) > 1:
            raise ProtocolError('Duplicate WebSocket handshake field')
    if handler.command != 'GET' or handler.headers.get('Upgrade', '').lower() != 'websocket':
        raise ProtocolError('WebSocket Upgrade required')
    if 'upgrade' not in {v.strip().lower() for v in handler.headers.get('Connection', '').split(',')}:
        raise ProtocolError('Connection Upgrade required')
    if handler.headers.get('Sec-WebSocket-Version') != '13':
        raise ProtocolError('WebSocket version 13 required')
    if handler.headers.get('Transfer-Encoding') or handler.headers.get('Content-Length', '0') != '0':
        raise ProtocolError('WebSocket handshake must not contain a body')
    nonce = handler.headers.get('Sec-WebSocket-Key', '')
    try:
        if len(base64.b64decode(nonce, validate=True)) != 16:
            raise ValueError()
    except ValueError:
        raise ProtocolError('Invalid WebSocket key') from None
    return nonce


def open_upstream(upstream, path, model, protocols):
    """No redirects, cookie forwarding or caller-selected host/query parameters."""
    parts = urlsplit(upstream['url'])
    sock = socket.create_connection((parts.hostname, parts.port or (443 if parts.scheme == 'https' else 80)), timeout=10)
    try:
        if parts.scheme == 'https':
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=parts.hostname)
        sock.settimeout(IDLE_SECONDS)
        nonce = base64.b64encode(os.urandom(16)).decode()
        target = parts.path.rstrip('/') + path.removeprefix('/v1') + '?' + urlencode({'model': model})
        lines = [f'GET {target} HTTP/1.1', f'Host: {parts.netloc}', 'Upgrade: websocket', 'Connection: Upgrade',
                 'Sec-WebSocket-Version: 13', f'Sec-WebSocket-Key: {nonce}',
                 'Authorization: Bearer ' + upstream['key'], 'User-Agent: gotc-gateway/1.0']
        if protocols:
            lines.append('Sec-WebSocket-Protocol: ' + ', '.join(protocols))
        sock.sendall(('\r\n'.join(lines) + '\r\n\r\n').encode('ascii'))
        response = http.client.HTTPResponse(sock)
        response.begin()
        expected = base64.b64encode(hashlib.sha1((nonce + GUID).encode()).digest()).decode()
        if response.status != 101 or response.headers.get('Sec-WebSocket-Accept') != expected:
            raise ProtocolError('Upstream rejected WebSocket handshake')
        if response.headers.get('Upgrade', '').lower() != 'websocket' or response.headers.get('Sec-WebSocket-Extensions') \
                or 'upgrade' not in {v.strip().lower() for v in response.headers.get('Connection', '').split(',')}:
            raise ProtocolError('Unsupported upstream WebSocket negotiation')
        if any(len(response.headers.get_all(name, [])) > 1 for name in
               ('Upgrade', 'Sec-WebSocket-Accept', 'Sec-WebSocket-Protocol')):
            raise ProtocolError('Duplicate upstream WebSocket negotiation field')
        selected = response.headers.get('Sec-WebSocket-Protocol')
        if selected and selected not in protocols:
            raise ProtocolError('Unexpected upstream WebSocket subprotocol')
        return sock, response, selected
    except Exception:
        sock.close()
        raise


def serve_websocket(gateway, handler, path, call, upstream):
    from keys import GatewayError, resolve, reject_stored_file_references, redact_error_payload, read_usage
    from gateway_media import MediaGateway
    call.update(metered_media=True, stream=True, currency='CNY')
    try:
        nonce = validate_handshake(handler)
        query = parse_qs(urlsplit(handler.path).query, keep_blank_values=True, max_num_fields=8)
        if set(query) != {'model'} or len(query['model']) != 1:
            raise ProtocolError('Exactly one model query parameter is required')
        protocols = [p.strip() for p in handler.headers.get('Sec-WebSocket-Protocol', '').split(',') if p.strip()]
        tokens = [p.removeprefix('openai-insecure-api-key.') for p in protocols if p.startswith('openai-insecure-api-key.')]
        if len(tokens) > 1:
            raise ProtocolError('Ambiguous WebSocket authentication')
        if tokens:
            if handler.headers.get('Authorization') or handler.headers.get('x-api-key'):
                raise ProtocolError('Use one authentication mechanism')
            handler.headers['Authorization'] = 'Bearer ' + tokens[0]
            call['secrets'] += tuple(tokens)
        protocols = [p for p in protocols if p in {'realtime', 'openai-beta.realtime-v1'}]
        # The browser's credential subprotocol is authentication, never content-log metadata.
        if handler.headers.get('Sec-WebSocket-Protocol') is not None:
            del handler.headers['Sec-WebSocket-Protocol']
        if protocols:
            handler.headers['Sec-WebSocket-Protocol'] = ', '.join(protocols)
        key = gateway.authenticate(handler, call)
        model = resolve(upstream, query['model'][0])
        if model is None:
            raise GatewayError('not_found', '模型不在本活动开放列表中')
        snapshot = gateway.catalog.resolve(model, path, {})
    except (ProtocolError, PricingError, ValueError) as error:
        raise GatewayError('invalid', str(error)) from None
    call.update(model=model, requested_model=query['model'][0], pricing_snapshot=snapshot, pricing_request={'model': model})
    call['secrets'] += (upstream['key'],)
    gateway.admit(key['team_id'], call['settings']['teamRpm'])
    remote, response, timer, worker = None, None, None, None
    stopped = threading.Event()
    problems, provider_data = [], {}
    request_characters = 0
    write_lock = threading.Lock()

    def shutdown_remote():
        stopped.set()
        if remote:
            try:
                remote.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def check_model(value):
        if isinstance(value, dict):
            if 'model' in value and value['model'] != model:
                raise ProtocolError('Changing model within a WebSocket session is not supported')
            for item in value.values():
                check_model(item)
        elif isinstance(value, list):
            for item in value:
                check_model(item)

    def client_loop():
        nonlocal request_characters
        try:
            for opcode, payload in messages(handler.rfile, True):
                if stopped.is_set():
                    break
                characters = 0
                if opcode in (1, 2):
                    # Audio bytes are billable too: revocation must stop binary-only clients.
                    gateway.recheck_access(handler, call)
                if opcode == 1:
                    data = json.loads(payload)
                    if not isinstance(data, dict):
                        raise ProtocolError('Speech events must be JSON objects')
                    from keys import validate_json_depth, gateway_json
                    validate_json_depth(data)
                    gateway_json(data)
                    reject_stored_file_references(data)
                    MediaGateway._check_shared_state(data)
                    check_model(data)
                    from keys import request_text
                    import unicodedata
                    text = request_text(data)
                    for word in call['settings']['blockedWords']:
                        if word.strip() and ' '.join(unicodedata.normalize('NFKC', word).split()).lower() in text:
                            raise ProtocolError('Request contains a blocked word')
                    value = data.get('text', data.get('input', ''))
                    if isinstance(value, str):
                        characters = len(value)
                if stopped.is_set():
                    break
                with write_lock:
                    remote.sendall(frame(opcode, payload, True))
                request_characters += characters
                call['request_bytes'] += len(payload)
                if opcode == 8:
                    return
        except (EOFError, OSError):
            shutdown_remote()
        except Exception as error:
            problems.append(type(error).__name__)
            shutdown_remote()

    try:
        gateway.recheck_access(handler, call)
        try:
            remote, response, protocol = open_upstream(upstream, path, model, protocols)
        except (OSError, http.client.HTTPException, ProtocolError):
            raise GatewayError('upstream', '上游实时语音连接失败，请稍后重试') from None
        accept = base64.b64encode(hashlib.sha1((nonce + GUID).encode()).digest()).decode()
        lines = ['HTTP/1.1 101 Switching Protocols', 'Upgrade: websocket', 'Connection: Upgrade',
                 'Sec-WebSocket-Accept: ' + accept, 'X-Client-Request-ID: ' + call['request_id']]
        if protocol:
            lines.append('Sec-WebSocket-Protocol: ' + protocol)
        handler.wfile.write(('\r\n'.join(lines) + '\r\n\r\n').encode('ascii'))
        handler.wfile.flush()
        call.update(headers_sent=True, forwarded=True, status=101)
        handler.close_connection = True
        handler.connection.settimeout(IDLE_SECONDS)
        timer = threading.Timer(SESSION_SECONDS, shutdown_remote)
        timer.daemon = True
        timer.start()
        worker = threading.Thread(target=client_loop, daemon=True, name='gotc-speech-client')
        worker.start()
        for opcode, payload in messages(response.fp, False):
            if opcode == 1:
                try:
                    event = json.loads(payload)
                except ValueError:
                    raise ProtocolError('Invalid upstream speech event') from None
                if isinstance(event, dict):
                    if event.get('error') or 'error' in str(event.get('type', '')).lower():
                        payload = redact_error_payload(payload, gateway.error_secrets(call))
                        problems.append('upstream_error')
                    else:
                        read_usage('openai', event, call['usage'])
                        if isinstance(event.get('usage'), dict):
                            provider_data['usage'] = event['usage']
            elif opcode == 8:
                code = struct.unpack('!H', payload[:2])[0] if payload else 1000
                if code not in (1000, 1001):
                    problems.append('upstream_closed_with_error')
                if payload:
                    reason = redact_error_payload(payload[2:], gateway.error_secrets(call))
                    reason = reason[:123].decode('utf-8', 'ignore').encode('utf-8')
                    payload = payload[:2] + reason
            handler.wfile.write(frame(opcode, payload))
            handler.wfile.flush()
            call['reply_bytes'] += len(payload)
            if opcode == 8:
                call['done'] = not problems
                break
    except (OSError, EOFError, ProtocolError, ValueError):
        problems.append('connection_interrupted')
    finally:
        shutdown_remote()
        if timer:
            timer.cancel()
        if remote:
            remote.close()
        if response:
            response.close()
        if worker and worker.is_alive():
            try:
                handler.connection.shutdown(socket.SHUT_RD)
            except OSError:
                pass
            worker.join(timeout=1)
        try:
            if call['forwarded']:
                call['pricing_quantities'] = {'characters': request_characters}
                try:
                    call['billing'] = quote(snapshot, request=call['pricing_request'], response=provider_data,
                                            headers=dict(response.headers) if response else {}, started_at=call['requested_at'],
                                            trusted_quantities=call['pricing_quantities'])
                except PricingError:
                    call['billing'] = {'cost_micro': None, 'currency': 'CNY', 'estimated': False, 'pending': True,
                                       'details': {'reason': 'invalid_provider_billing_evidence'}}
                if problems and not call['done']:
                    call['status'] = 502
                gateway.safe_record(handler, call)
        finally:
            gateway.release(key['team_id'])
