"""Bounded HTTP transports for the explicitly catalogued Gitee media operations.

The catalog owns model/operation authorization and prices. This module never exposes
the upstream's account APIs, forwards client credentials, or treats media bytes as tokens.
"""
from __future__ import annotations

from email.message import Message
import json
import re
import unicodedata
from urllib.parse import parse_qsl
import wave

from python_multipart import MultipartParser
from python_multipart.exceptions import MultipartParseError

from backend.gotc.errors import APIError
from . import gateway_pricing, usage
from .gateway_pricing import PricingError
from .protocol_errors import GatewayError, upstream_message
from .requests import gateway_json, reject_stored_file_references, request_text, validate_json_depth

UPLOAD_LIMIT = 32 * 1024 * 1024
REPLY_LIMIT = 64 * 1024 * 1024
SPOOL_MEMORY = 1024 * 1024
FIELD_LIMIT = 1024 * 1024
PART_LIMIT = 200
HEADER_LIMIT = 16 * 1024
CHUNK = 64 * 1024
_TEXT_FIELDS = {"model", "prompt", "input", "text", "query", "negative_prompt", "language", "voice"}
_CHAT_PATHS = {"/v1/chat/completions", "/v1/messages", "/v1/responses"}


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
            validate_json_depth(parsed)
            gateway_json(parsed)
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


def _part_metadata(headers):
    if headers.get_content_disposition() != "form-data":
        raise APIError(400, "multipart 字段必须是 form-data")
    params = headers.get_params(header="content-disposition")[1:]
    if len({k.lower() for k, _ in params}) != len(params):
        raise APIError(400, "multipart 字段参数重复")
    name = headers.get_param("name", header="content-disposition")
    if not isinstance(name, str):
        raise APIError(400, "multipart 字段缺少名称")
    if headers.get("Content-Transfer-Encoding", "binary").strip().lower() not in ("binary", "8bit") or headers.get_content_type().startswith("multipart/"):
        raise APIError(400, "不支持嵌套或编码的 multipart 字段")
    filename = headers.get_filename()
    if filename is not None and name == "model":
        raise APIError(400, "model 必须是文本字段")
    return name, filename, headers.get_content_type()


class _MediaForm:
    """Metadata-only callbacks. All byte ranges refer to the original upload spool."""
    def __init__(self, stream, boundary):
        self.stream = stream
        self.boundary_line_size = len(boundary) + 4  # --boundary plus CRLF or --
        self.header_start = self.boundary_line_size
        self.body = {}
        self.parts = 0
        self.metadata_bytes = 0
        self.complete = False

    def begin_part(self):
        self.parts += 1
        if self.parts > PART_LIMIT:
            raise APIError(413, "multipart 字段数量超过限制")
        self.headers = Message()
        self.key = bytearray()
        self.value = bytearray()
        self.header_tail = self.header_start
        self.size = 0

    def _header_fragment(self, target, data, start, end):
        self.header_tail = self.chunk_offset + end
        if self.header_tail - self.header_start > HEADER_LIMIT:
            raise APIError(400, "multipart 字段头过长")
        target.extend(data[start:end])

    def header_field(self, data, start, end):
        if not self.headers and not self.key and self.chunk_offset + start != self.header_start:
            raise APIError(400, "multipart 字段边界不正确")
        self._header_fragment(self.key, data, start, end)

    def header_value(self, data, start, end):
        self._header_fragment(self.value, data, start, end)

    def end_header(self):
        if not re.fullmatch(rb"[A-Za-z0-9-]+", self.key) or b"\n" in self.value:
            raise APIError(400, "multipart 字段头不正确")
        key = self.key.decode("ascii")
        if key in self.headers:
            raise APIError(400, "multipart 字段头重复")
        try:
            self.headers[key] = self.value.strip().decode("utf-8")
        except UnicodeDecodeError:
            raise APIError(400, "multipart 字段头编码不正确") from None
        self.key.clear()
        self.value.clear()

    def end_headers(self):
        self.name, self.filename, self.content_type = _part_metadata(self.headers)
        # Callbacks omit the final line ending and any whitespace after an empty
        # header. Locate the payload in the spool, accounting for those wire bytes.
        self.stream.seek(self.header_tail)
        remaining = self.header_start + HEADER_LIMIT - self.header_tail - 2
        if remaining < 2 or not self.stream.readline(remaining).endswith(b"\r\n"):
            raise APIError(400, "multipart 字段头过长或不完整")
        self.data_start = self.stream.tell() + 2

    def part_data(self, data, start, end):
        self.size += end - start
        if self.filename is None and self.size > FIELD_LIMIT:
            raise APIError(413, "multipart 文本字段超过 1MB")

    def end_part(self):
        if self.filename is None:
            self.stream.seek(self.data_start)
            try:
                value = self.stream.read(self.size).decode("utf-8")
            except UnicodeError:
                raise APIError(400, "multipart 文本字段必须为 UTF-8") from None
        else:
            value = {"filename": str(self.filename).replace("\\", "/").rsplit("/", 1)[-1][:200],
                     "content_type": self.content_type, "size": self.size}
            duration = _wav_duration(self.stream, self.data_start, self.size)
            if duration is not None:
                value["duration"] = duration
        self.metadata_bytes += self.data_start - self.header_start
        if self.filename is None:
            self.metadata_bytes += self.size
        _put(self.body, self.name, value)
        self.header_start = self.data_start + self.size + 2 + self.boundary_line_size

    def end(self):
        # The library permits epilogues. The gateway requires the closing delimiter
        # to be followed by at most the optional CRLF, even within the same chunk.
        self.stream.seek(self.header_start)
        if self.stream.read(3) not in (b"", b"\r\n"):
            raise APIError(400, "multipart 结束边界后存在多余内容")
        self.complete = True


def _parse_multipart(stream, content_type):
    header = Message()
    header["content-type"] = content_type
    boundaries = [v for k, v in header.get_params()[1:] if k.lower() == "boundary"]
    if (len(boundaries) != 1 or not isinstance(boundaries[0], str) or
            not re.fullmatch(r"[0-9A-Za-z'()+_,\-./:=? ]{1,70}", boundaries[0]) or boundaries[0].endswith(" ")):
        raise APIError(400, "multipart boundary 不正确")
    boundary = boundaries[0].encode("ascii")
    stream.seek(0)
    if stream.read(len(boundary) + 4) != b"--" + boundary + b"\r\n":
        raise APIError(400, "multipart 起始边界不正确")

    form = _MediaForm(stream, boundary)
    parser = MultipartParser(boundary, {
        "on_part_begin": form.begin_part, "on_part_data": form.part_data,
        "on_part_end": form.end_part, "on_end": form.end,
        "on_header_field": form.header_field, "on_header_value": form.header_value,
        "on_header_end": form.end_header, "on_headers_finished": form.end_headers,
    }, max_header_size=HEADER_LIMIT, max_header_count=HEADER_LIMIT // 4)
    offset = 0
    try:
        while not form.complete:
            # Metadata callbacks may seek on the spool to inspect a completed part.
            stream.seek(offset)
            chunk = stream.read(CHUNK)
            if not chunk:
                break
            form.chunk_offset = offset
            parser.write(chunk)
            offset += len(chunk)
        parser.finalize()
    except MultipartParseError:
        raise APIError(400, "multipart 表单格式不正确") from None
    if not form.complete:
        raise APIError(400, "multipart 请求边界不完整")
    stream.seek(0)
    return form.body, form.metadata_bytes


def parse_multipart(stream, content_type):
    return _parse_multipart(stream, content_type)[0]


class MediaGateway:
    def __init__(self, gateway, catalog, tasks=None):
        self.gateway, self.catalog, self.tasks = gateway, catalog, tasks

    def supports(self, path):
        return path in self.catalog.paths() and path not in _CHAT_PATHS and not path.endswith("/ws")

    @staticmethod
    def parse_form(spool, mime, content_type, length):
        spool.seek(0)
        if mime == "multipart/form-data":
            return _parse_multipart(spool, content_type)
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
        return body, length

    def _quote(self, call, response=None):
        if not call.get("forwarded"):
            call["billing"] = _zero("provider_rejected_before_generation")
            return
        try:
            call["billing"] = gateway_pricing.quote(call["pricing_snapshot"], request=call["pricing_request"], response=response,
                                    usage=call.get("pricing_usage"),
                                    headers=call.get("pricing_headers"),
                                    started_at=call["requested_at"])
        except PricingError:
            call["billing"] = _pending("invalid_provider_billing_evidence")

    def prepare_request(self, handler, path, call, upstream, body, length, encoded_request=None):
        model = body.get("model")
        if not isinstance(model, str) or not model or len(model) > 100:
            raise APIError(400, "请求必须指定模型名称")
        call["model"] = call["requested_model"] = model
        if model not in upstream.get("models", {}):
            raise GatewayError("not_found", "模型不在本活动开放列表中")
        call["request_bytes"] = length
        # Multipart binary input is never persisted in request-content logs.
        if call["log"]:
            call["request"] = encoded_request if encoded_request is not None else gateway_json(body)
        reject_stored_file_references(body)
        self._check_shared_state(body)
        settings = call["settings"]
        if settings["blockedWords"]:
            text = request_text(body)
            if any(" ".join(unicodedata.normalize("NFKC", w).split()).lower() in text
                   for w in settings["blockedWords"] if w.strip()):
                raise GatewayError("forbidden", "请求内容包含本活动禁止的词语，已拦截")
        if "stream" in body and type(body["stream"]) is not bool:
            raise APIError(400, "stream 必须是布尔值")
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
            raise GatewayError("not_found", str(error)) from None
        call["pricing_snapshot"], call["pricing_request"] = snapshot, body
        call["routing_headers"] = routing_headers
        call["stream"] = body.get("stream") is True
        if path.startswith("/v1/async/") and self.tasks is None:
            raise GatewayError("internal", "异步任务服务未初始化")

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

    def response_json(self, raw, call, status):
        try:
            data = json.loads(raw, parse_constant=lambda s: (_ for _ in ()).throw(ValueError(s)))
            validate_json_depth(data)
            gateway_json(data)
        except (ValueError, RecursionError, UnicodeError):
            raise GatewayError("upstream", "上游返回了无法解析的 JSON") from None
        if isinstance(data, dict) and isinstance(data.get("error"), (dict, str)):
            call["forwarded"], call["billing"] = False, _zero("provider_rejected")
            raise GatewayError("upstream", "上游模型服务返回错误", upstream_status=status,
                                    upstream_message=upstream_message(raw, self.gateway.error_secrets(call)))
        usage.read_usage("openai", data, call["usage"], call)
        return data
