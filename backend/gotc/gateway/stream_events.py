"""Inspect HTTPX SSE events while retaining the original wire bytes for SDK clients."""

import json
from dataclasses import dataclass

# The public EventSource iterator omits heartbeats and raw bytes. The pinned decoders
# implement field and line handling while the relay retains and bounds each raw block.
from httpx_sse._decoders import SSEDecoder, SSELineDecoder

from .protocol_errors import GatewayError, redact_error_payload


def oversized_event(limit):
    return GatewayError("upstream", "上游流式事件过大，请联系组织方检查上游服务", upstream_status=200,
                        upstream_message=f"SSE event buffer exceeded {limit} bytes")


@dataclass
class EventBlock:
    wire: bytes
    payload: bytes
    value: object
    error: bool

    @classmethod
    def from_event(cls, wire, event):
        payload = event.data.encode() if event else b""
        try:
            value = json.loads(payload) if payload else None
        except (ValueError, RecursionError):
            value = None
        error = bool(event and event.event.strip() in ("error", "response.failed")) or isinstance(value, dict) and (
            value.get("type") in ("error", "response.failed") or isinstance(value.get("error"), (dict, str)))
        return cls(wire, payload, value, error)


async def event_blocks(chunks, limit):
    """httpx-sse decodes lines and fields. Track offsets to also return comments, BOM and original terminators."""
    lines = SSELineDecoder()
    events = SSEDecoder()
    wire = bytearray()
    cursor = 0
    first_line = True

    def consume(physical_lines):
        nonlocal cursor, first_line
        start = 0
        for line in physical_lines:
            # Latin-1 below is a reversible, one-character-per-byte view for measuring offsets.
            cursor += len(line)
            if wire[cursor:cursor + 2] == b"\r\n":
                cursor += 2
            elif cursor < len(wire):
                cursor += 1
            if cursor - start > limit:
                raise oversized_event(limit)
            field = line.encode("latin-1").decode("utf-8-sig" if first_line else "utf-8", errors="replace")
            first_line = False
            event = events.decode(field)
            if not field:
                yield EventBlock.from_event(bytes(wire[start:cursor]), event)
                start = cursor
        if start:
            del wire[:start]
            cursor -= start
        if len(wire) > limit:
            raise oversized_event(limit)

    async for chunk in chunks:
        # A provider transport may coalesce several events into one read. Bound the decoder's
        # transient input as well as the retained event, without waiting to fill a fixed-size chunk.
        for offset in range(0, len(chunk), 16384):
            part = chunk[offset:offset + 16384]
            wire.extend(part)
            for block in consume(lines.decode(part.decode("latin-1"))):
                yield block
    for block in consume(lines.flush()):
        yield block
    if wire:
        # Existing SDK compatibility: some providers terminate their final event at EOF.
        yield EventBlock.from_event(bytes(wire), events.decode(""))


def redacted_block(block, secrets):
    """Rewrite only affected error fields. Keep safe events and error metadata in their original order."""
    payload = redact_error_payload(block.payload, secrets)
    parts = iter(payload.split(b"\n")) if payload != block.payload else None
    output = bytearray()
    for line in block.wire.splitlines(keepends=True):
        field = line.rstrip(b"\r\n")
        if field == b"data" or field.startswith(b"data:"):
            if parts is None:
                output.extend(line)
            else:
                replacement = next(parts, None)
                if replacement is not None:
                    output.extend(b"data: " + replacement + line[len(field):])
        else:
            output.extend(redact_error_payload(line, secrets))
    return bytes(output), payload
