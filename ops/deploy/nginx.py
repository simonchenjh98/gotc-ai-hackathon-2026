"""Generate the GOTC locations inside an existing HTTPS virtual host."""
from __future__ import annotations

import argparse
from collections.abc import Iterator
import ipaddress
from pathlib import Path
import re
from urllib.parse import urlsplit

BEGIN = "    # BEGIN GOTC HACKATHON MANAGED LOCATION\n"
END = "    # END GOTC HACKATHON MANAGED LOCATION\n"
FRONTEND = Path("/opt/gotc-hackathon/current/frontend")
PLATFORM = "http://unix:/run/gotc-platform/http.sock"
GATEWAY = "http://unix:/run/gotc-gateway/http.sock"

# Preprovision this separate http-context include once. No limits are applied
# to neighbors: only GOTC locations refer to these shared-memory zones.
HTTP_CONTEXT = """limit_req_zone $binary_remote_addr zone=gotc_api:1m rate=60r/s;
limit_req_zone $binary_remote_addr zone=gotc_login:1m rate=15r/s;
limit_req_zone $binary_remote_addr zone=gotc_sdk:1m rate=30r/s;
map $http_upgrade $gotc_connection_upgrade {
    default upgrade;
    '' close;
}
"""

CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; "
       "img-src 'self' data:; font-src 'self'; connect-src 'self'; object-src 'none'; "
       "frame-ancestors 'none'; base-uri 'self'; form-action 'self'")

_TOKEN = re.compile(r"""\#[^\n]*|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[{};]|[^\s{};\#"']+""")


def _origin(public_origin: str) -> tuple[str, int]:
    parts = urlsplit(public_origin)
    host = parts.hostname or ""
    if (parts.scheme != "https" or not host or parts.username is not None or parts.password is not None
            or parts.path not in {"", "/"} or "?" in public_origin or "#" in public_origin or "\\" in public_origin
            or public_origin != public_origin.strip()
            or any(ch.isspace() or ord(ch) < 32 for ch in public_origin)):
        raise ValueError("Set the public origin to an exact HTTPS origin without userinfo, path or query")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if len(host) > 253 or not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                                      for label in host.split(".")):
            raise ValueError("Invalid public hostname") from None
    port = parts.port
    if port == 0:
        raise ValueError("Invalid public port")
    return host, port or 443


def _dns_host(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return True
    return False


def _path(path: Path) -> str:
    value = str(path)
    if not path.is_absolute() or ".." in path.parts or not re.fullmatch(r"/[\w./-]+", value, re.ASCII):
        raise ValueError("Expected an absolute Nginx path without variables or special characters")
    return value


def _block(selector: str, directives: list[str], indent: str = "        ") -> str:
    return (f"{indent}location {selector} {{\n"
            + "".join(f"{indent}    {line}\n" for line in directives)
            + f"{indent}}}\n")


def _proxy(socket: str, *, size: str = "32k", read_timeout: int = 30,
           send_timeout: int = 30, zone: str = "gotc_api", burst: int = 140) -> list[str]:
    return [
        f"proxy_pass {socket};",
        "proxy_http_version 1.1;",
        "proxy_set_header Host $http_host;",
        "proxy_set_header X-Real-IP $remote_addr;",
        "proxy_set_header X-Forwarded-For $remote_addr;",
        "proxy_set_header X-Forwarded-Proto $scheme;",
        "proxy_set_header X-Request-ID $request_id;",
        f"limit_req zone={zone} burst={burst} nodelay;",
        f"client_max_body_size {size};",
        f"proxy_read_timeout {read_timeout}s;",
        f"proxy_send_timeout {send_timeout}s;",
    ]


def _static(root: str, file: str, *, hsts: bool, immutable: bool = False, content_type: str | None = None) -> list[str]:
    cache = "public, max-age=31536000, immutable" if immutable else "public, no-cache"
    lines = [
        f'alias "{root}/{file}";',
        f'disable_symlinks on from={root};',
        # Release tar members share epoch mtimes. Size and mtime cannot identify mutable bytes.
        "etag on;" if immutable else "etag off;",
        "if_modified_since exact;" if immutable else "if_modified_since off;",
        "gzip on;",
        "gzip_vary on;",
        "gzip_types text/css application/javascript text/javascript application/json image/svg+xml;",
        "log_not_found off;",
        f'add_header Cache-Control "{cache}";',  # Do not cache an error response for a year.
        'add_header X-Content-Type-Options "nosniff" always;',
        'add_header X-Frame-Options "DENY" always;',
        'add_header Referrer-Policy "strict-origin-when-cross-origin" always;',
        'add_header Permissions-Policy "camera=(), microphone=(), geolocation=()" always;',
        f'add_header Content-Security-Policy "{CSP}" always;',
    ]
    if hsts:
        lines.append('add_header Strict-Transport-Security "max-age=31536000" always;')
    if content_type:
        lines.append(f"default_type {content_type};")
    return lines


def locations(frontend: Path = FRONTEND, *, hsts: bool = False) -> str:
    root = _path(frontend)
    result = [BEGIN, _block("= /gotc", ["return 308 /gotc/;"], indent="    "),
              "    location ^~ /gotc/ {\n",
              "        types {\n",
              "            text/html html; text/javascript js; text/css css; application/json json;\n",
              "            image/svg+xml svg; image/jpeg jpg jpeg; image/png png; image/webp webp;\n",
              "            image/x-icon ico; font/woff woff; font/woff2 woff2; application/pdf pdf;\n",
              "            text/calendar ics; text/plain txt;\n",
              "        }\n",
              "        access_log off;\n",  # Route/operation logs are emitted by the applications.
              "        limit_req_status 429;\n",
              "        client_body_timeout 30s;\n",
              "        return 404;\n"]
    result.append(_block("= /gotc/", ["rewrite ^ /gotc/index.html last;"]))
    for path, file in [("index.html", "index.html"), ("teams.html", "teams.html"),
                       ("platform.html", "platform.html"), ("favicon.svg", "favicon.svg"),
                       ("asset-manifest.json", "asset-manifest.json")]:
        mime = "text/html" if file.endswith(".html") else None
        result.append(_block("= /gotc/" + path, _static(root, file, hsts=hsts, content_type=mime)))
    # Regex locations use bounded captures. An ordinary JS/CSS filename never receives immutable caching.
    for prefix, names, immutable in [
        ("static", r"(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]+\.[0-9a-f]{16}\.(?:js|css)", True),
        ("assets", r"[A-Za-z0-9_-]+\.(?:webp|png|jpg|jpeg|svg|ico|woff2?)", False),
        ("downloads", r"(?:[A-Za-z0-9_-]+\.(?:ics|txt)|gotc-gitee-submission-guide\.pdf)", False),
    ]:
        selector = f'~ "^/gotc/({prefix}/{names})$"'
        result.append(_block(selector, _static(root, "$1", hsts=hsts, immutable=immutable)))

    for route in ("api/login", "api/roadshow/login", "api/finals/access"):
        result.append(_block("= /gotc/" + route, _proxy(PLATFORM, zone="gotc_login", burst=50)))
    for route, size in [("api/admin/data/import/preview", "2m"), ("api/admin/claims/roster", "2m"),
                        ("api/admin/claims/review", "128k")]:
        result.append(_block("= /gotc/" + route, _proxy(PLATFORM, size=size)))
    result.append(_block("^~ /gotc/api/team/uploads/",
                         _proxy(PLATFORM, size="100m", read_timeout=330, send_timeout=330)
                         + ["proxy_request_buffering off;"]))
    for route in ("api/", "materials/"):
        result.append(_block("^~ /gotc/" + route, _proxy(PLATFORM)))
    result.append(_block("^~ /gotc/v1/", _proxy(GATEWAY, size="32m", read_timeout=1260, send_timeout=330,
                                                zone="gotc_sdk", burst=80) + [
        "proxy_set_header Upgrade $http_upgrade;",
        "proxy_set_header Connection $gotc_connection_upgrade;",
        "proxy_buffering off;",
        "proxy_request_buffering off;",
    ]))
    return "".join([*result, "    }\n", END])


def _servers(source: str) -> Iterator[tuple[int, int, list[list[str]]]]:
    """Locate server bodies and their direct directives, ignoring nested location directives."""
    tokens = [m for m in _TOKEN.finditer(source) if not m[0].startswith("#")]
    for i, token in enumerate(tokens[:-1]):
        if token[0] != "server" or tokens[i + 1][0] != "{":
            continue
        depth = 1
        statements: list[list[str]] = []
        words: list[str] = []
        for item in tokens[i + 2:]:
            word = item[0]
            if word == "}":
                depth -= 1
                if depth == 0:
                    yield tokens[i + 1].end(), item.start(), statements
                    break
            elif word == "{":
                words.clear()
                depth += 1
            elif depth == 1:
                if word == ";":
                    if words:
                        statements.append(words)
                    words = []
                else:
                    words.append(word.strip('"\''))
        else:
            raise ValueError("Unclosed Nginx server block")


def _tls_for(statements: list[list[str]], host: str, port: int) -> bool:
    names = {name.lower() for row in statements if row[0] == "server_name" for name in row[1:]}
    listens = [row for row in statements if row[0] == "listen" and len(row) >= 2]
    selected = [row for row in listens if row[1].rsplit(":", 1)[-1] == str(port) and "ssl" in row[2:]]
    if host not in {name.strip("[]") for name in names} or not selected:
        return False
    if any("ssl" not in row[2:] for row in listens):
        raise ValueError("The target virtual host also accepts plaintext HTTP")
    if ["http2", "on"] not in statements and not all("http2" in row[2:] for row in selected):
        raise ValueError("Enable HTTP/2 for the HTTPS listener before installing GOTC")
    return True


def nginx_candidate(source: str, public_origin: str, *, frontend: Path = FRONTEND) -> str:
    host, port = _origin(public_origin)
    matches = [(start, end) for start, end, rows in _servers(source) if _tls_for(rows, host, port)]
    if len(matches) != 1:
        raise ValueError("Expected one HTTPS server matching the configured origin")
    start, end = matches[0]
    body = source[start:end]
    generated = locations(frontend, hsts=_dns_host(host))
    if BEGIN in source or END in source:
        if source.count(BEGIN) != 1 or source.count(END) != 1 or BEGIN not in body or END not in body:
            raise ValueError("Ambiguous existing GOTC markers")
        left, right = body.index(BEGIN), body.index(END)
        if right <= left:
            raise ValueError("Reversed GOTC markers")
        before, after = body[:left], body[right + len(END):]
    else:
        before, after = body, ""
    # Never claim another operator's GOTC routes. The common closing brace and all
    # surrounding whitespace remain byte-identical on subsequent replacements.
    if re.search(r"\blocation\s+[^\n{;]*?/gotc(?:[/\s{\x22']|$)", before + after):
        raise ValueError("An unmanaged GOTC location already exists")
    if not before.endswith("\n"):
        generated = "\n" + generated
    return source[:start] + before + generated + after + source[end:]


def dedicated_host(public_origin: str, certificate: Path, certificate_key: Path) -> str:
    host, port = _origin(public_origin)
    name = f"[{host}]" if ":" in host else host
    return (f"server {{\n    listen {port} ssl http2;\n    server_name {name};\n"
            f'    ssl_certificate "{_path(certificate)}";\n'
            f'    ssl_certificate_key "{_path(certificate_key)}";\n'
            + "    location / { return 404; }\n" + locations(hsts=_dns_host(host)) + "}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    sub.add_parser("http-context", help="Print the one-time http-context include")
    host_parser = sub.add_parser("dedicated-host", help="Print an optional dedicated virtual host")
    host_parser.add_argument("--public-origin", required=True)
    host_parser.add_argument("--certificate", required=True, type=Path)
    host_parser.add_argument("--certificate-key", required=True, type=Path)
    args = parser.parse_args()
    if args.mode == "http-context":
        print(HTTP_CONTEXT, end="")
    else:
        print(dedicated_host(args.public_origin, args.certificate, args.certificate_key), end="")
