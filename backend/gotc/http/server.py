"""HTTP transport for the GOTC application and model gateway."""
from __future__ import annotations

import ipaddress
import json
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from ..constants import COOKIE_NAME, SESSION_SECONDS
from ..errors import APIError
from ..gateway.gateway_cors import origin_allowed, preflight_allowed, response_headers as gateway_cors_headers
from ..submissions.repository_backups import ArchiveDownload
from ..submissions.submission_records import query_path
from ..teams.team_registration import PUBLIC_ORIGINS, PUBLIC_PATHS
from .static import serve_public_file


BODY_LIMIT = 16384
SUBMISSION_BODY_LIMIT = 32768
IMPORT_BODY_LIMIT = 2 * 1024 * 1024
CLAIM_REVIEW_BODY_LIMIT = 128 * 1024


class EventHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 128

    def __init__(self, address, service):
        self.service = service
        super().__init__(address, EventHandler)


class EventHandler(BaseHTTPRequestHandler):
    server_version = "GOTC"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, *_):
        # Invitation codes, cookies, queries and request bodies never enter access logs.
        pass

    def end_headers(self):
        # BaseHTTPRequestHandler can send protocol errors before normal routing.
        self.send_header("Cache-Control", getattr(self, "_cache_control", "no-store"))
        self._cache_control = "no-store"
        super().end_headers()

    def headers_out(self, status, content_type, length, cookie=None, extra=None, *, cache_control="no-store"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        if length is not None:  # None: streamed until the connection closes
            self.send_header("Content-Length", str(length))
        self._cache_control = cache_control
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        request_path = urlsplit(self.path).path
        origin = self.headers.get("Origin")
        if getattr(self, "_gateway_cors", False):
            for name, value in gateway_cors_headers(self.headers, self.server.service.origin,
                                                   preflight=self.command == "OPTIONS" and status == 204).items():
                self.send_header(name, value)
        if request_path in {self.server.service.base_path + p for p in PUBLIC_PATHS} and origin in PUBLIC_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            if self.command == "OPTIONS":
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Referrer-Policy", (extra or {}).get("Referrer-Policy", "strict-origin-when-cross-origin"))
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header("Content-Security-Policy", (extra or {}).get("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; connect-src 'self' https://8.141.114.201; object-src 'none'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"))
        if self.server.service.secure:
            self.send_header("Strict-Transport-Security", "max-age=31536000")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        if extra:
            for key, value in extra.items():
                if key not in {"Content-Security-Policy", "Referrer-Policy"}:
                    self.send_header(key, value)
        self.end_headers()

    def respond(self, status, value, cookie=None):
        payload = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.headers_out(status, "application/json; charset=utf-8", len(payload), cookie)
        if self.command != "HEAD":
            self.wfile.write(payload)

    def do_GET(self):
        self.handle_route()

    def do_HEAD(self):
        self.handle_route()

    def do_OPTIONS(self):
        self._gateway_cors = False
        try:
            path = urlsplit(self.path).path
            gateway_path = unquote(path, errors="strict")
        except (UnicodeError, ValueError):
            self.respond(400, {"error": "请求路径编码不正确"})
            return
        if gateway_path.startswith(self.server.service.base_path + "/v1/"):
            self.handle_route()
            return
        if (path in {self.server.service.base_path + p for p in PUBLIC_PATHS}
                and self.headers.get("Origin") in PUBLIC_ORIGINS
                and self.headers.get("Access-Control-Request-Method") in {"GET", "POST"}
                and self.headers.get("Access-Control-Request-Headers", "").lower() in {"", "content-type"}):
            self.headers_out(204, "text/plain", 0)
        else:
            self.respond(403, {"error": "不允许跨站请求"})

    def do_POST(self):
        self.handle_route()

    def handle_route(self):
        service = self.server.service
        self._gateway_cors = False
        try:
            parsed = urlsplit(self.path)
            if parsed.scheme or parsed.netloc or parsed.fragment:
                raise APIError(400, "请求地址格式不正确")
            try:
                path = unquote(parsed.path, errors="strict")
            except UnicodeError:
                raise APIError(400, "请求路径编码不正确")
            if "\\" in path or "\x00" in path or any(p in {".", ".."} for p in path.split("/")):
                raise APIError(400, "请求路径不正确")
            if path == service.base_path and self.command in {"GET", "HEAD"}:
                self.headers_out(308, "text/plain", 0, extra={"Location": service.base_path + "/"})
                return
            if service.base_path and not path.startswith(service.base_path + "/"):
                raise APIError(404, "页面不存在")
            path = path[len(service.base_path):] if service.base_path else path
            if path.startswith("/v1/"):
                # Check browser origins before reading uploads, authorizing keys, or opening upstreams.
                # Native SDKs without Origin remain supported; every actual request still needs a key.
                self._gateway_cors = True
                if not origin_allowed(self.headers, service.origin):
                    raise APIError(403, "此浏览器来源不允许调用中转 API", code="origin_not_allowed")
                if self.command == "OPTIONS":
                    if not preflight_allowed(self.headers, service.origin):
                        raise APIError(403, "中转 API 不允许此跨域预检请求", code="cors_preflight_not_allowed")
                    self.headers_out(204, "text/plain", 0)
                    return
                service.keys.gateway(self, path)
                return
            query={}
            if parsed.query:
                if self.command!='GET' or not (path.startswith('/api/admin/data/') or query_path(path)):
                    raise APIError(400,'此接口不接受查询参数')
                try:
                    params=parse_qs(parsed.query,keep_blank_values=True,strict_parsing=True,max_num_fields=20,encoding='utf-8',errors='strict')
                except (ValueError,UnicodeError):
                    raise APIError(400,'查询参数格式不正确')
                if any(len(values)!=1 or len(key)>80 or len(values[0])>2000 for key,values in params.items()):
                    raise APIError(400,'查询参数不能重复或超长')
                query={key:values[0] for key,values in params.items()}
            if path.startswith("/api/"):
                if self.command == "HEAD":
                    raise APIError(405, "接口不支持此方法")
                body = {}
                received_at = None
                if self.command == "POST":
                    public_registration = path in PUBLIC_PATHS and self.headers.get("Origin") in PUBLIC_ORIGINS
                    if not public_registration and (self.headers.get("Origin") != service.origin or self.headers.get("Sec-Fetch-Site") not in {None, "same-origin", "none"}):
                        raise APIError(403, "仅允许本站发起操作，请从活动官网打开")
                    if self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length", [])) != 1:
                        raise APIError(400, "请求长度不正确")
                    try:
                        length = int(self.headers.get("Content-Length", "-1"))
                    except ValueError:
                        raise APIError(400, "请求长度不正确")
                    upload_kind = service.material_uploads.kind(path)
                    if upload_kind:
                        result = service.material_uploads.upload(self, upload_kind, length)
                        self.respond(200, result)
                        return
                    limit=CLAIM_REVIEW_BODY_LIMIT if path=='/api/admin/claims/review' else SUBMISSION_BODY_LIMIT if path=='/api/submission' else IMPORT_BODY_LIMIT if path in {'/api/admin/data/import/preview','/api/admin/claims/roster'} else BODY_LIMIT
                    if length < 0 or length > limit:
                        raise APIError(413,'请求内容超过端点大小限制')
                    if self.headers.get_content_type() != "application/json":
                        raise APIError(415, "请使用JSON请求")
                    try:
                        raw = self.rfile.read(length)
                        if len(raw) != length:
                            raise ValueError('incomplete request')
                        body = json.loads(raw.decode("utf-8"), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                    except (UnicodeError, ValueError, RecursionError):
                        raise APIError(400, "JSON格式不正确")
                    if not isinstance(body, dict):
                        raise APIError(400, "JSON内容必须是对象")
                    if path == '/api/submission':
                        received_at = service.clock()
                peer = self.client_address[0]
                if ipaddress.ip_address(peer).is_loopback:
                    proxy_ip = self.headers.get("X-Real-IP", "")
                    try:
                        peer = str(ipaddress.ip_address(proxy_ip))
                    except ValueError:
                        pass
                value, token, cookie_action = service.dispatch(self.command, path, body, self.headers.get("Cookie", ""), peer,query=query,expected_login_id=self.headers.get("X-GOTC-Login"),server_received_at=received_at,finals_judge_id=self.headers.get("X-GOTC-Finals-Judge"),finals_access_token=self.headers.get("X-GOTC-Finals-Access"))
                cookie = None
                if cookie_action:
                    cookie = f"{COOKIE_NAME}={token or ''}; Path={service.base_path or '/'}; HttpOnly; SameSite=Strict; Max-Age={SESSION_SECONDS if cookie_action == 'set' else 0}"
                    if service.secure:
                        cookie += "; Secure"
                if isinstance(value, ArchiveDownload):
                    with value.stream as stream:
                        self.headers_out(200,value.content_type,value.size,extra={"Content-Disposition":f'attachment; filename="{value.filename}"'})
                        while chunk:=stream.read(65536):self.wfile.write(chunk)
                elif isinstance(value, bytes):
                    filename='gotc-submissions.csv' if path=='/api/admin/submissions/export' else 'gotc-data.csv' if path.startswith('/api/admin/data/') else 'gotc-api-usage.csv' if path=='/api/admin/keys/export' else 'gotc-scores.csv'
                    self.headers_out(200, "text/csv; charset=utf-8", len(value), extra={"Content-Disposition": f'attachment; filename="{filename}"'})
                    self.wfile.write(value)
                else:
                    self.respond(200, value, cookie)
            elif self.command in {"GET", "HEAD"}:
                if path.startswith('/materials/'):
                    service.material_uploads.serve(self, path)
                    return
                serve_public_file(self, service.root, path)
            else:
                raise APIError(405, "请求方法不支持")
        except APIError as error:
            self.close_connection = True
            self.respond(error.status, {"error":error.message,**({"code":error.code} if error.code else {}),**({"requestId":error.request_id} if hasattr(error,'request_id') else {})})
        except sqlite3.IntegrityError:
            self.respond(409, {"error": "队号或记录已存在，请刷新后核对"})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True
        except Exception:
            self.close_connection = True
            self.respond(500, {"error": "服务暂时不可用，请稍后重试"})
