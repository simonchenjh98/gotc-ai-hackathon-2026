"""Forward text and token-count requests and relay JSON or server-sent events to the client."""

import http.client
import json
import socket
import time
import urllib.error
import urllib.request

from . import transport
from . import usage as token_usage
from .config import resolve
from .gateway_uploads import UploadDrainTimeout, UploadPostSendError
from .protocol_errors import (
    ClientGone, GatewayError, error_info, redact_error_payload, sse_error,
    upstream_http_error, upstream_message,
)
from .requests import allowed_betas, gateway_json, reject_stored_file_references
from .transport import CLIENT_HEADERS, failure_reason, passed_headers, request_was_sent, set_read_timeout


STREAM_IDLE_TIMEOUT = 360  # silence between stream events before giving up (sub2api stream_data_interval_timeout)
SSE_EVENT_CAP = 1024 * 1024  # inspect at most 1 MiB per event before any of its bytes reach the client


def count_tokens(gateway, handler, call, upstream):
    """Claude Code calls this before requests; passed through at zero cost, recorded without content."""
    body = gateway.read_body(handler)
    call["secrets"] += (upstream["key"],)
    model = resolve(upstream, body.get("model"))
    call["model"] = model or ""
    if model is None:
        raise GatewayError("not_found", "模型不在本活动开放列表中，可用模型见 /v1/models")
    body["model"] = model
    reject_stored_file_references(body)
    headers = {"Content-Type": "application/json", "x-api-key": upstream["key"],
               "anthropic-version": handler.headers.get("anthropic-version", "2023-06-01")}
    beta = allowed_betas(handler.headers.get("anthropic-beta"))
    if beta:
        headers["anthropic-beta"] = beta
    headers["User-Agent"] = (handler.headers.get("User-Agent") or "gotc-gateway/1.0")[:300]
    request = urllib.request.Request(upstream["url"] + "/messages/count_tokens", data=gateway_json(body), headers=headers, method="POST")
    request._gotc_upload_admitted = lambda: gateway.upload_ready(handler, call)
    try:
        with gateway.open_with_retry(request) as response:
            status, payload, content_type = response.status, response.read(1_000_000), response.headers.get("Content-Type", "application/json")
    except urllib.error.HTTPError as error:  # mapped like /v1/messages; count_tokens is never charged
        try:
            payload = error.read(1_000_000)
        except (OSError, http.client.HTTPException):
            payload = b"{}"
        payload = redact_error_payload(payload, gateway.error_secrets(call))
        message = upstream_message(payload, gateway.error_secrets(call))
        if error.code in (404, 405, 501):
            # The upstream has no count_tokens (Gitee AI, many compatible providers). Mirror the status, as sub2api
            # does for count_tokens, so clients such as Claude Code fall back to their own estimate.
            raise GatewayError("upstream_invalid", "上游不支持 count_tokens 接口", status=404, upstream_status=error.code,
                               upstream_message=message) from None
        mapped = upstream_http_error(call["path"], error.code, message)
        if mapped:
            raise mapped from None
        status, content_type = error.code, error.headers.get("Content-Type", "application/json")
        gateway.note_error(call, GatewayError("upstream_invalid", message, status=status, upstream_status=status, upstream_message=message))
    except (OSError, http.client.HTTPException) as error:  # count_tokens is free, so any failure is just a 502
        raise GatewayError("network", "上游模型服务暂时不可用，请稍后重试", upstream_message=failure_reason(error)) from None
    call["status"] = status
    gateway.safe_record(handler, call)
    gateway.send(handler, call, status, content_type, payload)


def upstream_failed(call, error):
    """Turn a network failure into the team's error response, estimating cost when the upstream may have run."""
    reason = getattr(error, "reason", error)
    timed_out = isinstance(reason, (TimeoutError, socket.timeout, UploadDrainTimeout))
    if not call["forwarded"] and not request_was_sent(error):
        # Failed while connecting, in the TLS handshake or while sending: the upstream never had the whole request,
        # so nothing can have been generated or billed. Decided by where the failure happened, not by its type.
        raise GatewayError("network", "上游模型服务暂时不可用，请稍后重试（本次不计费）",
                           upstream_message=f"{failure_reason(error)}（请求未发出，不计费）") from None
    # The request was sent (or the reply had started): the upstream may be generating and billing,
    # so record the estimated usage rather than nothing.
    call["charge_estimate"] = True
    reason = f"{failure_reason(error)}（请求已发出，按估算用量计费）"
    if timed_out:
        raise GatewayError("timeout", "上游模型服务响应超时，本次按估算用量计费", upstream_message=reason) from None
    raise GatewayError("network", "上游模型服务连接中断，本次按估算用量计费", upstream_message=reason) from None


def forward(gateway, handler, name, upstream, suffix, body, stream, call):
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream" if stream else "application/json"}
    if name == "openai":
        headers["Authorization"] = "Bearer " + upstream["key"]
    else:
        headers["x-api-key"] = upstream["key"]
        headers["anthropic-version"] = handler.headers.get("anthropic-version", "2023-06-01")
        beta = allowed_betas(handler.headers.get("anthropic-beta"))
        if beta:
            headers["anthropic-beta"] = beta
        for header, value in handler.headers.items():  # not `name`: that is the upstream kind used below
            if header.lower() in CLIENT_HEADERS or header.lower().startswith("x-stainless-"):
                headers[header] = value[:500]
    # The client's own User-Agent (sub2api passes it); the urllib default is blocked by Cloudflare-fronted upstreams.
    headers["User-Agent"] = (handler.headers.get("User-Agent") or "gotc-gateway/1.0")[:300]
    request = urllib.request.Request(upstream["url"] + suffix, data=gateway_json(body), headers=headers, method="POST")
    request._gotc_upload_admitted = lambda: gateway.upload_ready(handler, call)
    handler.connection.settimeout(transport.UPSTREAM_TIMEOUT)
    try:
        response = gateway.open_with_retry(request)
    except urllib.error.HTTPError as error:
        # The upstream answered with an error: nothing was generated, so it is never charged.
        try:
            payload = error.read(1_000_000)
        except (OSError, http.client.HTTPException):
            payload = b""
        call["upstream_request_id"] = error.headers.get("request-id") or error.headers.get("x-request-id") or ""
        payload = redact_error_payload(payload, gateway.error_secrets(call))
        message = upstream_message(payload, gateway.error_secrets(call)) or f"HTTP {error.code}"
        mapped = upstream_http_error(call["path"], error.code, message)
        if mapped:
            retry = error.headers.get("retry-after")
            mapped.retry_after = retry[:20] if retry and error.code in (429, 503, 529) else None
            raise mapped from None
        # 400: the team's request was wrong; pass the upstream body through so it can fix it (as sub2api does).
        gateway.note_error(call, GatewayError("upstream_invalid", message, status=400, upstream_status=400, upstream_message=message))
        call["response"], call["size"] = [], 0
        gateway.keep(call, payload)
        gateway.safe_record(handler, call)
        handler.close_connection = True
        gateway.send(handler, call, 400, error.headers.get("Content-Type", "application/json"), payload)
        return
    except (OSError, http.client.BadStatusLine, http.client.LineTooLong, UploadPostSendError) as error:
        # Response parsing failed after sending. Other HTTPExceptions (e.g. InvalidURL) can occur before sending.
        upstream_failed(call, error)
    call["status"], call["forwarded"] = response.status, True
    call["upstream_request_id"] = response.headers.get("request-id") or response.headers.get("x-request-id") or ""
    call["reply_headers"] = passed_headers(response.headers)
    call["pricing_headers"] = {k: v for k, v in response.headers.items() if k.lower() in {"inference-cost", "inference-currency", "inference-cost-currency"}}
    if not call.get("first_token_ms"):  # non-streaming: the whole reply arrives with the headers
        call["first_token_ms"] = int((time.monotonic() - call["started"]) * 1000)
    usage = call["usage"]
    with response:
        content_type = response.headers.get("Content-Type", "application/json")
        if content_type.startswith("text/event-stream"):
            gateway.relay_stream(handler, name, response, content_type, call)
            return
        try:
            payload = response.read(50_000_000)
        except (OSError, http.client.HTTPException) as error:  # nothing sent to the client yet: a clean error
            upstream_failed(call, error)
        declared = response.headers.get("Content-Length")
        if declared and declared.isdigit() and len(payload) < int(declared):
            # read(n) returns a short body instead of raising when the upstream drops mid-reply.
            upstream_failed(call, ConnectionResetError("reply cut short"))
        call["reply_bytes"] = len(payload)
        try:
            data = json.loads(payload)
        except ValueError:
            # A 200 the gateway cannot parse is not a reply (sub2api: 502, not billed); keep it for admins.
            call["forwarded"] = False
            payload = redact_error_payload(payload, gateway.error_secrets(call))
            gateway.keep(call, payload[:2000])
            preview = upstream_message(payload, gateway.error_secrets(call))
            raise GatewayError("upstream", "上游返回了无法解析的内容，请稍后重试（本次不计费）", upstream_status=response.status,
                               upstream_message=f"上游返回 HTTP {response.status} 但内容不是 JSON：{preview}") from None
        if isinstance(data, dict) and isinstance(data.get("error"), (dict, str)):
            # Some providers answer errors with HTTP 200 and an error object.
            call["forwarded"] = False
            message = upstream_message(payload, gateway.error_secrets(call))
            kind = "overloaded" if "overload" in json.dumps(data.get("error")).lower() else "upstream"
            raise GatewayError(kind, "上游模型服务返回错误，请稍后重试（本次不计费）", upstream_status=response.status,
                               upstream_message=message) from None
        token_usage.read_usage(name, data, usage, call)
        call["done"] = True  # a complete JSON body is a finished reply
        gateway.keep(call, payload)
        gateway.safe_record(handler, call)
        gateway.send(handler, call, response.status, content_type, payload)


def relay_stream(gateway, handler, name, response, content_type, call):
    """Relay an SSE stream, billing what the upstream produced however the stream or the client ends.

    As in sub2api, the first event is read before anything is sent: an upstream error there becomes a proper HTTP
    error (not billed). After output has started, a failure is reported to the client with a terminal error frame
    and recorded as a failed call (inferred 502/504), billed for what was observed or estimated.
    """
    usage, path = call["usage"], call["path"]
    handler.close_connection = True  # HTTP/1.0: the stream ends when the connection closes
    set_read_timeout(response, STREAM_IDLE_TIMEOUT)  # silence between events, not the whole reply
    def oversized_event():
        return GatewayError("upstream", "上游流式事件过大，请联系组织方检查上游服务", upstream_status=200,
                            upstream_message=f"SSE event buffer exceeded {SSE_EVENT_CAP} bytes")

    def frames():
        frame, size = [], 0
        while True:
            # Limit readline itself: checking only after an unbounded read would still allocate a giant line.
            line = response.readline(SSE_EVENT_CAP - size + 1)
            if not line:
                break
            size += len(line)
            if size > SSE_EVENT_CAP:
                raise oversized_event()
            frame.append(line)
            if line in (b"\n", b"\r\n"):
                yield frame
                frame, size = [], 0
        if frame:
            yield frame

    def inspect_frame(frame):
        # SSE joins multiple data lines with newlines; inspect the whole event before releasing an error.
        payload = b"\n".join(line[5:].removeprefix(b" ").rstrip(b"\r\n") for line in frame if line.startswith(b"data:"))
        event_name = next((line[6:].strip() for line in reversed(frame) if line.startswith(b"event:")), b"")
        try:
            event = json.loads(payload)
        except ValueError:
            event = None
        error = event_name in (b"error", b"response.failed") or isinstance(event, dict) and (
            event.get("type") in ("error", "response.failed") or isinstance(event.get("error"), (dict, str)))
        return payload, event, error

    stream = frames()
    held, held_size, payload, event, is_error = [], 0, b"", None, False
    try:
        for frame in stream:
            held_size += sum(map(len, frame))
            if held_size > SSE_EVENT_CAP:
                raise oversized_event()  # bound comment/heartbeat frames held before the first data event too
            held.append(frame)
            payload, event, is_error = inspect_frame(frame)
            if payload:
                break
    except (OSError, http.client.HTTPException) as error:
        upstream_failed(call, error)  # nothing sent to the client yet: a clean error response
    if is_error:
        call["forwarded"] = False  # the upstream refused before generating anything: not billed
        message = upstream_message(payload, gateway.error_secrets(call))
        raw = payload.decode("utf-8", "replace")
        kind = "overloaded" if "overload" in raw.lower() else "upstream_rate_limit" if "rate_limit" in raw.lower() else "upstream"
        raise GatewayError(kind, "上游模型服务返回错误，请稍后重试（本次不计费）", upstream_status=200, upstream_message=message)
    try:
        gateway.send(handler, call, 200, content_type)
        client_open = True
    except ClientGone:
        client_open = False  # still drain below: the upstream is generating and billing regardless
    failure = None

    def relay(frame):
        nonlocal client_open
        payload, parsed, is_error = inspect_frame(frame)
        if is_error:
            sanitized = redact_error_payload(payload, gateway.error_secrets(call))
            if sanitized != payload:
                replacement, written = [], False
                for line in frame:
                    if line.startswith(b"data:"):
                        if not written:
                            ending = b"\r\n" if line.endswith(b"\r\n") else b"\n" if line.endswith(b"\n") else b""
                            # Every physical payload line needs its own data field in SSE, including plain-text errors.
                            replacement.extend(b"data: " + part + ending for part in sanitized.split(b"\n"))
                            written = True
                    else:
                        replacement.append(line)
                frame = replacement
                try:
                    parsed = json.loads(sanitized)
                except ValueError:
                    parsed = None
            call["stream_error"] = parsed if isinstance(parsed, dict) else {
                "error": {"message": upstream_message(sanitized, gateway.error_secrets(call))}}
            # An error's id/event/comment lines are provider-controlled too; do not leak keys through metadata.
            frame = [line if line.startswith(b"data:") else redact_error_payload(line, gateway.error_secrets(call))
                     for line in frame]
        if payload.strip() == b"[DONE]":
            call["done"] = True
        else:
            token_usage.read_usage(name, parsed, usage, call)
            token_usage.stream_progress(name, parsed, call)
            if call["reply_bytes"] and call.get("stream_first_ms") is None:
                call["stream_first_ms"] = int((time.monotonic() - call["started"]) * 1000)
        for line in frame:
            gateway.keep(call, line)
            if client_open:
                try:
                    gateway.send(handler, call, 200, content_type, line)
                except ClientGone:
                    client_open = False  # keep draining to capture final usage, bounded by the upstream timeout

    for frame in held:
        relay(frame)
    try:
        for frame in stream:
            relay(frame)
    except (OSError, http.client.HTTPException, GatewayError) as error:
        failure = error
    if call["done"] and not call.get("stream_error") and not isinstance(failure, GatewayError):
        return  # finished (if the client left mid-way, the full drained usage is billed, as in sub2api)
    # The stream did not finish: classify, tell the client (if it is still there) and record a failed call.
    if call.get("stream_error"):
        raw = json.dumps(call["stream_error"].get("error", call["stream_error"]))
        kind = "overloaded" if "overload" in raw.lower() else "upstream"
        info = error_info(GatewayError(kind, "上游模型服务在输出过程中返回错误", status=502, upstream_status=200,
                                       upstream_message=upstream_message(raw.encode(), gateway.error_secrets(call))))
        frame = None  # the upstream's own error frame was already relayed
    elif isinstance(failure, GatewayError):
        info = error_info(failure)
        frame = sse_error(path, info)
    else:
        timed_out = failure is not None and isinstance(getattr(failure, "reason", failure), (TimeoutError, socket.timeout))
        reason = failure_reason(failure) if failure is not None else "上游未发送结束事件就关闭了连接"
        info = error_info(GatewayError("timeout" if timed_out else "network",
                                       "上游输出中断，本次按已产生的用量计费", upstream_message=reason))
        frame = sse_error(path, info)
    call["status"], call["error"] = info["kind"] == "timeout" and 504 or 502, info
    gateway.keep(call, f"{info['upstream_message']}\n".encode())
    if frame and client_open:
        try:
            gateway.send(handler, call, 200, content_type, frame)
        except ClientGone:
            pass
