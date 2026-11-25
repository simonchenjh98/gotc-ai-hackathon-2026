"""Resolve public frontend files and implement conditional GET and HEAD."""
from __future__ import annotations

import json
from pathlib import Path
import re

from starlette.responses import FileResponse, Response
from starlette.staticfiles import StaticFiles

from ..errors import APIError


ENTRY_TYPES = {
    "asset-manifest.json": "application/json",
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


def immutable_assets(root: Path) -> set[str]:
    manifest = root / "asset-manifest.json"
    if not manifest.is_file():
        return set()
    entries = json.loads(manifest.read_text(encoding="utf-8"))["assets"]
    return {entry["path"] for entry in entries.values()
            if re.fullmatch(r"(?:static/(?:[\w.-]+/)*[\w.-]+\.[0-9a-f]{16}\.(?:css|js)"
                            r"|assets/[\w-]+\.[0-9a-f]{16}\.(?:jpg|webp))", entry["path"])}


def public_response(root: Path, path: str, request_headers, immutable: set[str]) -> Response:
    source, content_type = public_file(root, path)
    info = source.stat()
    tag = f'"{info.st_ino:x}-{info.st_mtime_ns:x}-{info.st_ctime_ns:x}-{info.st_size:x}"'
    cache = "public, max-age=31536000, immutable" if path.lstrip("/") in immutable else "public, no-cache"
    headers = {"ETag": tag, "Cache-Control": cache}
    response = FileResponse(source, media_type=content_type, stat_result=info, headers=headers)
    # Starlette compares the opaque tag against weak and strong request validators.
    unchanged = StaticFiles().is_not_modified(response.headers, request_headers)
    response.headers["ETag"] = "W/" + tag
    if unchanged:
        return Response(status_code=304, headers={"ETag": response.headers["ETag"], "Cache-Control": cache})
    return response
