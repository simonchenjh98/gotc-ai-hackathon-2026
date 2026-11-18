"""Resolve public frontend files and implement conditional GET and HEAD."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import stat

from ..errors import APIError


ENTRY_TYPES = {
    "index.html": "text/html; charset=utf-8",
    "teams.html": "text/html; charset=utf-8",
    "platform.html": "text/html; charset=utf-8",
    "favicon.svg": "image/svg+xml",
    "downloads/gotc-gitee-submission-guide.pdf": "application/pdf",
}
DIRECTORY_TYPES = {
    "static": {".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8"},
    "assets": {
        ".webp": "image/webp", ".png": "image/png", ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg", ".svg": "image/svg+xml", ".ico": "image/x-icon",
        ".woff": "font/woff", ".woff2": "font/woff2",
    },
    "downloads": {".ics": "text/calendar; charset=utf-8", ".txt": "text/plain; charset=utf-8"},
}


def public_file(root: Path, path: str) -> tuple[Path, str]:
    relative = path.removeprefix("/") or "index.html"
    parts = relative.split("/")
    if any(not part or part.startswith(".") or "\\" in part or "\x00" in part for part in parts):
        raise APIError(404, "页面不存在")
    candidate = root.joinpath(*parts)
    content_type = ENTRY_TYPES.get(relative)
    if content_type is None:
        content_type = DIRECTORY_TYPES.get(parts[0], {}).get(candidate.suffix.lower())
    if content_type is None:
        raise APIError(404, "页面不存在")
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        raise APIError(404, "页面不存在") from None
    if resolved != candidate or not resolved.is_file():
        raise APIError(404, "页面不存在")
    return resolved, content_type


def serve_public_file(handler, root: Path, path: str) -> None:
    source, content_type = public_file(root, path)
    with source.open("rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise APIError(404, "页面不存在")
        tag = f'"{info.st_ino:x}-{info.st_mtime_ns:x}-{info.st_ctime_ns:x}-{info.st_size:x}"'
        headers = {"ETag": "W/" + tag}
        validators = handler.headers.get("If-None-Match", "").split(",")
        if any(value.strip().removeprefix("W/") in {tag, "*"} for value in validators):
            handler.headers_out(304, content_type, None, extra=headers, cache_control="public, no-cache")
            return
        handler.headers_out(200, content_type, info.st_size, extra=headers, cache_control="public, no-cache")
        if handler.command != "HEAD":
            shutil.copyfileobj(stream, handler.wfile, length=64 * 1024)
