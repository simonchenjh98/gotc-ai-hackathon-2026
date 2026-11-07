"""Bounded HTTP transports for the explicitly catalogued Gitee media operations.

The catalog owns model/operation authorization and prices. This module never exposes
the upstream's account APIs, forwards client credentials, or treats media bytes as tokens.
"""
from __future__ import annotations

from contextlib import closing
from email.message import Message
import http.client
import io
import json
import re
import tempfile
import time
import unicodedata
import urllib.error
import urllib.request
from urllib.parse import parse_qsl, urlsplit
import wave

from errors import APIError

UPLOAD_LIMIT = 32 * 1024 * 1024
REPLY_LIMIT = 64 * 1024 * 1024
SPOOL_MEMORY = 1024 * 1024
FIELD_LIMIT = 1024 * 1024
PART_LIMIT = 200
HEADER_LIMIT = 16 * 1024
CHUNK = 64 * 1024
_TEXT_FIELDS = {"model", "prompt", "input", "text", "query", "negative_prompt", "language", "voice"}
_CHAT_PATHS = {"/v1/chat/completions", "/v1/messages", "/v1/responses"}


def _core():
    # Lazy import: KeyService imports this adapter during initialization.
    import keys
    return keys


def _pending(reason):
    return {"cost_micro": None, "currency": "CNY", "estimated": False, "pending": True,
            "details": {"reason": reason}}


def _zero(reason):
    return {"cost_micro": 0, "currency": "CNY", "estimated": False, "pending": False,
            "details": {"reason": reason}}


def _length(handler):
    if handler.headers.get("Transfer-Encoding") or len(handler.headers.get_all("Content-Length", [])) != 1:
        raise APIError(411, "请求需要单一 Content-Length")
    value = handler.headers.get("Content-Length", "")
    if not re.fullmatch(r"[0-9]+", value):
        raise APIError(400, "请求长度不正确")
    length = int(value)
    if not 0 < length <= UPLOAD_LIMIT:
        raise APIError(413, "媒体请求超过 32MB 限制")
    return length


def _find(stream, marker):
    """Find a MIME delimiter without reading an entire binary part into memory."""
    start = stream.tell()
    overlap = b""
    cursor = start
    while True:
        chunk = stream.read(CHUNK)
        if not chunk:
            raise APIError(400, "multipart 请求边界不完整")
        block = overlap + chunk
        base = cursor - len(overlap)
        pos = block.find(marker)
        while pos >= 0:
            found = base + pos
            saved = stream.tell()
            stream.seek(found + len(marker))
            suffix = stream.read(2)
            stream.seek(saved)
            if suffix in (b"\r\n", b"--"):
                stream.seek(found + len(marker))
                return found
            pos = block.find(marker, pos + 1)
        overlap = block[-(len(marker) - 1):]
        cursor += len(chunk)


class _Slice:
    """A bounded, seekable view, used only to inspect an uploaded WAV's actual frames."""
    def __init__(self, stream, start, length):
        self.stream, self.start, self.length, self.pos = stream, start, length, 0

    def read(self, count=-1):
        count = self.length - self.pos if count < 0 else min(count, self.length - self.pos)
        self.stream.seek(self.start + self.pos)
        data = self.stream.read(count)
        self.pos += len(data)
        return data

    def tell(self):
        return self.pos

    def seek(self, offset, whence=0):
        value = offset if whence == 0 else self.pos + offset if whence == 1 else self.length + offset
        if not 0 <= value <= self.length:
            raise ValueError("out of bounds")
        self.pos = value
        return value


def _wav_duration(stream, start, length):
    saved = stream.tell()
    try:
        view = _Slice(stream, start, length)
        with wave.open(view, "rb") as audio:
            # A header claiming more frames than the actual upload is not a duration measurement.
            frames = audio.getnframes()
            frame_size = audio.getnchannels() * audio.getsampwidth()
            data_start = view.tell()
            if audio.getframerate() > 0 and frames * frame_size <= length - data_start:
                return frames / audio.getframerate()
    except (EOFError, ValueError, wave.Error, OSError):
        pass
    finally:
        stream.seek(saved)
    return None


def _field(name, value):
    if not name or len(name) > 100 or name.startswith("_") or any(ord(c) < 32 for c in name):
        raise APIError(400, "表单字段名称不正确")
    # Bracket-encoded file IDs must not hide from the recursive shared-state guard.
    if re.search(r"(?:^|[\[.])(?:file_id|file_ids|task_id|taskId|previous_response_id|conversation)(?:\]|$)", name):
        raise APIError(400, "不支持引用共享上游账户的文件或任务")
    if isinstance(value, str) and name not in _TEXT_FIELDS:
        try:
            parsed = json.loads(value, parse_constant=lambda s: (_ for _ in ()).throw(ValueError(s)))
            _core().validate_json_depth(parsed)
            _core().gateway_json(parsed)
            if isinstance(parsed, (dict, list, int, float, bool)) or parsed is None:
                return parsed
        except (ValueError, RecursionError):
            if value.lstrip().startswith(("{", "[")):
                raise APIError(400, "表单中的 JSON 字段不正确") from None
    return value


def _put(body, name, value):
    value = _field(name, value)
    if name in body:
        # File arrays have a documented repeated-field encoding; scalar duplicates are ambiguous.
        if not name.endswith("[]"):
            raise APIError(400, "表单中包含重复字段")
        if not isinstance(body[name], list):
            body[name] = [body[name]]
        body[name].append(value)
    else:
        body[name] = value


def parse_multipart(stream, content_type):
    header = Message()
    header["content-type"] = content_type
    boundaries = [v for k, v in header.get_params()[1:] if k.lower() == "boundary"]
    if len(boundaries) != 1 or not re.fullmatch(r"[0-9A-Za-z'()+_,\-./:=? ]{1,70}", boundaries[0]) or boundaries[0].endswith(" "):
        raise APIError(400, "multipart boundary 不正确")
    boundary = boundaries[0].encode("ascii")
    stream.seek(0)
    if stream.read(len(boundary) + 4) != b"--" + boundary + b"\r\n":
        raise APIError(400, "multipart 起始边界不正确")
    body = {}
    for _ in range(PART_LIMIT):
        headers, total = Message(), 0
        while True:
            line = stream.readline(HEADER_LIMIT - total + 1)
            total += len(line)
            if total > HEADER_LIMIT or not line.endswith(b"\r\n"):
                raise APIError(400, "multipart 字段头过长或不完整")
            if line == b"\r\n":
                break
            if b":" not in line or line[:1] in b" \t":
                raise APIError(400, "multipart 字段头不正确")
            key, value = line[:-2].split(b":", 1)
            try:
                key, value = key.decode("ascii"), value.strip().decode("utf-8")
            except UnicodeError:
                raise APIError(400, "multipart 字段头编码不正确") from None
            if not re.fullmatch(r"[A-Za-z0-9-]+", key) or headers.get(key) is not None:
                raise APIError(400, "multipart 字段头重复或不正确")
            headers[key] = value
        if headers.get_content_disposition() != "form-data":
            raise APIError(400, "multipart 字段必须是 form-data")
        params = headers.get_params(header="content-disposition")[1:]
        if len({k.lower() for k, _ in params}) != len(params):
            raise APIError(400, "multipart 字段参数重复")
        name = headers.get_param("name", header="content-disposition")
        if not isinstance(name, str):
            raise APIError(400, "multipart 字段缺少名称")
        if headers.get("Content-Transfer-Encoding", "binary").lower() not in ("binary", "8bit") or headers.get_content_type().startswith("multipart/"):
            raise APIError(400, "不支持嵌套或编码的 multipart 字段")
        start = stream.tell()
        end = _find(stream, b"\r\n--" + boundary)
        suffix = stream.read(2)
        next_pos = stream.tell()
        filename = headers.get_filename()
        if filename is not None:
            if name == "model":
                raise APIError(400, "model 必须是文本字段")
            value = {"filename": str(filename).replace("\\", "/").rsplit("/", 1)[-1][:200],
                     "content_type": headers.get_content_type(), "size": end - start}
            duration = _wav_duration(stream, start, end - start)
            if duration is not None:
                value["duration"] = duration
        else:
            if end - start > FIELD_LIMIT:
                raise APIError(413, "multipart 文本字段超过 1MB")
            stream.seek(start)
            try:
                value = stream.read(end - start).decode("utf-8")
            except UnicodeError:
                raise APIError(400, "multipart 文本字段必须为 UTF-8") from None
        _put(body, name, value)
        stream.seek(next_pos)
        if suffix == b"--":
            if stream.read() not in (b"", b"\r\n"):
                raise APIError(400, "multipart 结束边界后存在多余内容")
            stream.seek(0)
            return body
    raise APIError(413, "multipart 字段数量超过限制")


class MediaGateway:
    def __init__(self, gateway, catalog, tasks=None):
        self.gateway, self.catalog, self.tasks = gateway, catalog, tasks

    def supports(self, path):
        return path in self.catalog.paths() and path not in _CHAT_PATHS and not path.endswith("/ws")

    def read_request(self, handler):
        """Return (pricing metadata, exact replayable payload, content type, byte length)."""
        core = _core()
        content_type = handler.headers.get("Content-Type", "")
        mime = handler.headers.get_content_type()
        if mime == "application/json":
            body = self.gateway.read_body(handler)
            payload = io.BytesIO(core.gateway_json(body))
            return body, payload, "application/json", len(payload.getbuffer())
        length = _length(handler)
        if handler.headers.get("Content-Encoding", "identity").lower() != "identity":
            raise APIError(415, "媒体表单不支持 Content-Encoding")
        if mime not in ("multipart/form-data", "application/x-www-form-urlencoded"):
            raise APIError(415, "请使用 JSON、multipart/form-data 或表单请求")
        spool = tempfile.SpooledTemporaryFile(max_size=SPOOL_MEMORY, mode="w+b")
        try:
            remaining = length
            while remaining:
                block = handler.rfile.read(min(CHUNK, remaining))
                if not block:
                    raise APIError(400, "请求内容未完整发送")
                spool.write(block)
                remaining -= len(block)
            spool.seek(0)
            if mime == "multipart/form-data":
                body = parse_multipart(spool, content_type)
            else:
                if length > FIELD_LIMIT:
                    raise APIError(413, "URL 编码表单超过 1MB")
                try:
                    pairs = parse_qsl(spool.read().decode("utf-8"), keep_blank_values=True,
                                      strict_parsing=True, max_num_fields=PART_LIMIT, errors="strict")
                except (UnicodeError, ValueError):
                    raise APIError(400, "表单编码不正确") from None
                body = {}
                for name, value in pairs:
                    _put(body, name, value)
            spool.seek(0)
            return body, spool, content_type, length
        except BaseException:
            spool.close()
            raise

    def _quote(self, call, response=None):
        if not call.get("forwarded"):
            call["billing"] = _zero("provider_rejected_before_generation")
            return
        from gateway_pricing import PricingError, quote
        try:
            call["billing"] = quote(call["pricing_snapshot"], request=call["pricing_request"], response=response,
                                    usage=call["usage"] if any(call["usage"]) else None,
                                    headers=call.get("pricing_headers"),
                                    started_at=call["requested_at"])
        except PricingError:
            call["billing"] = _pending("invalid_provider_billing_evidence")

    def serve(self, handler, path, call, upstream):
        core, gateway = _core(), self.gateway
        if handler.command != "POST" or not self.supports(path):
            raise APIError(404, "接口不存在")
        if urlsplit(handler.path).query:
            raise APIError(400, "模型请求参数请放在请求体中")
        call["metered_media"], call["requested_at"] = True, time.time()
        call["billing"] = _zero("not_forwarded")
        call["secrets"] += (upstream["key"],)
        key = gateway.authenticate(handler, call)
        call["user_agent"] = (handler.headers.get("User-Agent") or "")[:300]
        call["client_ip"] = core.client_ip(handler)
        body, payload, content_type, length = self.read_request(handler)
        with closing(payload):
            model = body.get("model")
            if not isinstance(model, str) or not model or len(model) > 100:
                raise APIError(400, "请求必须指定模型名称")
            call["model"] = call["requested_model"] = model
            if model not in upstream.get("models", {}):
                raise core.GatewayError("not_found", "模型不在本活动开放列表中")
            call["request_bytes"] = length
            # Multipart binary input is never persisted in request-content logs.
            call["request"] = core.gateway_json(body) if call["log"] else b""
            core.reject_stored_file_references(body)
            self._check_shared_state(body)
            settings = call["settings"]
            if settings["blockedWords"]:
                text = core.request_text(body)
                if any(" ".join(unicodedata.normalize("NFKC", w).split()).lower() in text
                       for w in settings["blockedWords"] if w.strip()):
                    raise core.GatewayError("forbidden", "请求内容包含本活动禁止的词语，已拦截")
            if "stream" in body and type(body["stream"]) is not bool:
                raise APIError(400, "stream 必须是布尔值")
            from gateway_pricing import PricingError
            routing_headers = {}
            for name in ("X-Compute-Id", "X-Pipeline-Tag"):
                values = handler.headers.get_all(name, [])
                if len(values) > 1:
                    raise APIError(400, "模型路由请求头不能重复")
                if values:
                    value = values[0].strip()
                    if not value or len(value) > 200 or any(ord(c) < 32 or ord(c) > 126 for c in value):
                        raise APIError(400, "模型路由请求头不正确")
                    routing_headers[name] = value
            try:
                snapshot = self.catalog.resolve(model, path, body, headers=routing_headers)
            except PricingError as error:
                raise core.GatewayError("not_found", str(error)) from None
            call["pricing_snapshot"], call["pricing_request"] = snapshot, body
            call["routing_headers"] = routing_headers
            call["stream"] = body.get("stream") is True
            if path.startswith("/v1/async/") and self.tasks is None:
                raise core.GatewayError("internal", "异步任务服务未初始化")
            gateway.admit(key["team_id"], settings["teamRpm"])
            try:
                gateway.recheck_access(handler, call)
                if gateway.client_gone(handler):
                    raise core.GatewayError("client_closed", "客户端已断开，请求未发出，不计费")
                self.forward(handler, path, call, upstream, body, payload, content_type, length)
            finally:
                gateway.release(key["team_id"])

    @staticmethod
    def _check_shared_state(value):
        if isinstance(value, dict):
            forbidden = {"file_id", "file_ids", "task_id", "taskId", "previous_response_id", "conversation", "container", "mcp_servers"}
            if forbidden.intersection(value):
                raise APIError(400, "不支持引用共享上游账户的持久状态")
            for item in value.values():
                MediaGateway._check_shared_state(item)
        elif isinstance(value, list):
            for item in value:
                MediaGateway._check_shared_state(item)

    def forward(self, handler, path, call, upstream, body, payload, content_type, length):
        core, gateway = _core(), self.gateway
        headers = {"Authorization": "Bearer " + upstream["key"], "Content-Type": content_type,
                   "Content-Length": str(length), "Accept-Encoding": "identity",
                   "User-Agent": (handler.headers.get("User-Agent") or "gotc-gateway/1.0")[:300]}
        headers.update(call.get("routing_headers", {}))
        if "X-Compute-Id" in headers:
            headers["X-Failover-Enabled"] = "false"
        request = urllib.request.Request(upstream["url"].rstrip("/") + path[len("/v1"):],
                                         data=payload, headers=headers, method="POST")
        handler.connection.settimeout(core.UPSTREAM_TIMEOUT)
        try:
            # Generations can have side effects. A submitted request is never automatically replayed.
            response = gateway.opener.open(request, timeout=core.UPSTREAM_TIMEOUT)
        except urllib.error.HTTPError as error:
            with error:
                raw = error.read(FIELD_LIMIT)
                call["upstream_request_id"] = error.headers.get("request-id") or error.headers.get("x-request-id") or ""
                raw = core.redact_error_payload(raw, gateway.error_secrets(call))
                message = core.upstream_message(raw, gateway.error_secrets(call)) or f"HTTP {error.code}"
                mapped = core.upstream_http_error(path, error.code, message)
                if mapped:
                    raise mapped from None
                raise core.GatewayError("upstream_invalid", message, status=400, upstream_status=error.code,
                                        upstream_message=message) from None
        except (OSError, http.client.HTTPException) as error:
            if core.request_was_sent(error):
                call["billing"] = _pending("submission_outcome_unknown")
            raise core.GatewayError("network", "上游连接失败；已提交请求的费用等待核对",
                                    upstream_message=core.failure_reason(error)) from None
        call["forwarded"], call["status"] = True, response.status
        call["billing"] = _pending("provider_usage_not_received")
        call["upstream_request_id"] = response.headers.get("request-id") or response.headers.get("x-request-id") or ""
        call["reply_headers"] = core.passed_headers(response.headers)
        call["pricing_headers"] = {k: v for k, v in response.headers.items()
                                   if k.lower() in {"inference-cost", "inference-currency", "inference-cost-currency"}}
        call["first_token_ms"] = int((time.monotonic() - call["started"]) * 1000)
        with response:
            mime = response.headers.get("Content-Type", "application/json").split(";", 1)[0].strip().lower()
            if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                raise core.GatewayError("upstream", "上游返回了不支持的媒体压缩编码")
            if mime == "text/event-stream":
                if path.startswith("/v1/async/"):
                    raise core.GatewayError("upstream", "异步任务提交未返回任务 JSON")
                call["stream"] = True
                call["billing_finalize"] = lambda: self._quote(call)
                try:
                    gateway.relay_stream(handler, "openai", response, "text/event-stream", call)
                finally:
                    self._quote(call)
                return
            with tempfile.SpooledTemporaryFile(max_size=SPOOL_MEMORY, mode="w+b") as output:
                size = self._read_response(response, output)
                call["reply_bytes"] = size
                output.seek(0)
                if mime == "application/json" or mime.endswith("+json"):
                    raw = output.read()
                    try:
                        data = json.loads(raw, parse_constant=lambda s: (_ for _ in ()).throw(ValueError(s)))
                        core.validate_json_depth(data)
                        core.gateway_json(data)
                    except (ValueError, RecursionError, UnicodeError):
                        raise core.GatewayError("upstream", "上游返回了无法解析的 JSON") from None
                    if isinstance(data, dict) and isinstance(data.get("error"), (dict, str)):
                        call["forwarded"], call["billing"] = False, _zero("provider_rejected")
                        raise core.GatewayError("upstream", "上游模型服务返回错误", upstream_status=response.status,
                                                upstream_message=core.upstream_message(raw, gateway.error_secrets(call)))
                    core.read_usage("openai", data, call["usage"])
                    if path.startswith("/v1/async/"):
                        data = self.tasks.register(call, upstream, data, request=body, headers=dict(response.headers))
                        raw = core.gateway_json(data)
                    else:
                        self._quote(call, data)
                    call["done"] = True
                    gateway.keep(call, raw)
                    gateway.safe_record(handler, call)
                    gateway.send(handler, call, response.status, "application/json", raw)
                    return
                if path.startswith("/v1/async/") or not (mime.startswith(("audio/", "image/")) or mime == "application/octet-stream"):
                    raise core.GatewayError("upstream", "上游返回了不支持的媒体类型")
                metadata = {"_binary": {"bytes": size, "content_type": mime}}
                duration = _wav_duration(output, 0, size)
                if duration is not None:
                    metadata["duration"] = duration
                self._quote(call, metadata)
                call["done"] = True
                gateway.keep(call, core.gateway_json(metadata))
                gateway.safe_record(handler, call)
                gateway.send(handler, call, response.status, mime, length=size)
                output.seek(0)
                while True:
                    block = output.read(CHUNK)
                    if not block:
                        break
                    gateway.send(handler, call, response.status, mime, block)

    @staticmethod
    def _read_response(response, output):
        core = _core()
        declared = response.headers.get("Content-Length")
        if declared is not None and (not declared.isdigit() or int(declared) > REPLY_LIMIT):
            raise core.GatewayError("upstream", "上游媒体响应长度不正确或超过 64MB 限制")
        total = 0
        try:
            while True:
                block = response.read(min(CHUNK, REPLY_LIMIT - total + 1))
                if not block:
                    break
                total += len(block)
                if total > REPLY_LIMIT:
                    raise core.GatewayError("upstream", "上游媒体响应超过 64MB 限制")
                output.write(block)
        except (OSError, http.client.HTTPException) as error:
            raise core.GatewayError("network", "上游媒体响应中断，费用等待核对",
                                    upstream_message=core.failure_reason(error)) from None
        if declared is not None and total != int(declared):
            raise core.GatewayError("network", "上游媒体响应未完整接收，费用等待核对")
        return total
