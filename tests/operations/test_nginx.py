"""Exercise the generated site in isolation from host services and certificates."""
from contextlib import ExitStack, closing, contextmanager
import gzip
import http.client
from http.server import BaseHTTPRequestHandler
import json
import os
from pathlib import Path
import shutil
import socket
from socketserver import UnixStreamServer
import ssl
import subprocess
import tempfile
from threading import Thread
import time
import unittest

from ops.deploy import nginx


class SharedHostTests(unittest.TestCase):
    def test_replacement_preserves_every_byte_outside_the_markers(self):
        start = ("# shared web host\nserver {\n    listen 443 ssl http2;\n"
                 "    server_name event.invalid;\n    location /chuyi { return 204; }\n")
        end = "    location /game { return 204; }\n}\n# keep trailing whitespace\n  "
        original = start + nginx.BEGIN + "    location /gotc/ { return 418; }\n" + nginx.END + end
        candidate = nginx.nginx_candidate(original, "https://event.invalid")
        self.assertEqual(candidate, start + nginx.locations(hsts=True) + end)
        self.assertEqual(nginx.nginx_candidate(candidate, "https://event.invalid"), candidate)

    def test_validates_named_https_host_and_http2_before_changing_it(self):
        host = "server { listen 443 ssl; http2 on; server_name event.invalid; }"
        self.assertIn(nginx.BEGIN, nginx.nginx_candidate(host, "https://event.invalid"))
        other = host.replace("event.invalid", "other.invalid")
        for source in [other, host + host, host.replace("http2 on;", ""),
                       host.replace("http2 on;", "listen 80; http2 on;"),
                       host.replace("http2 on;", "location /gotc/api { return 403; } http2 on;")]:
            with self.subTest(source=source), self.assertRaises(ValueError):
                nginx.nginx_candidate(source, "https://event.invalid")
        for bad in ["http://event.invalid", "https://event.invalid/foo", "https://x@event.invalid",
                    "https://event.invalid/?id=x", "https://a.invalid;include"]:
            with self.subTest(origin=bad), self.assertRaises(ValueError):
                nginx.nginx_candidate(host, bad)

    def test_dedicated_host_and_socket_routing(self):
        config = nginx.dedicated_host("https://example.invalid", Path("/certs/event.pem"), Path("/certs/event.key"))
        self.assertEqual(nginx.nginx_candidate(config, "https://example.invalid"), config)
        self.assertIn("listen 443 ssl http2;", config)
        self.assertIn("Strict-Transport-Security", config)
        ip_config = nginx.dedicated_host("https://192.0.2.1", Path("/certs/c"), Path("/certs/k"))
        self.assertNotIn("Strict-Transport-Security", ip_config)
        self.assertIn("proxy_pass http://unix:/run/gotc-platform/http.sock;", config)
        self.assertIn("proxy_pass http://unix:/run/gotc-gateway/http.sock;", config)
        self.assertIn("proxy_set_header X-Forwarded-For $remote_addr;", config)
        self.assertNotIn("proxy_add_x_forwarded_for", config)
        self.assertNotIn("127.0.0.1:", config)
        self.assertNotIn("location /metrics", config)
        self.assertNotIn("limit_req_zone", config)
        self.assertIn("zone=gotc_login", nginx.HTTP_CONTEXT)


@contextmanager
def synthetic_upstream(path, service):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"service": service, "path": self.path, "client": self.headers["X-Forwarded-For"],
                               "proto": self.headers["X-Forwarded-Proto"], "requestId": self.headers["X-Request-ID"]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    with UnixStreamServer(str(path), Handler) as server:
        worker = Thread(target=server.serve_forever, kwargs={"poll_interval": .02}, daemon=True)
        worker.start()
        try:
            yield
        finally:
            server.shutdown()
            worker.join(timeout=3)


@unittest.skipUnless(shutil.which("nginx") and shutil.which("openssl"), "nginx and openssl are required")
class LiveNginxTests(unittest.TestCase):
    @contextmanager
    def running(self):
        with tempfile.TemporaryDirectory(prefix="gotc-nginx-", dir="/tmp") as temporary, ExitStack() as upstreams:
            root = Path(temporary).resolve()
            first = root / "first"
            first.mkdir()
            current = root / "current"
            current.symlink_to(first, target_is_directory=True)
            public = current / "frontend"
            static = public / "static/site"
            static.mkdir(parents=True)
            digest = "0123456789abcdef"
            filename = f"home.{digest}.js"
            js = b"console.log('static');\n" * 150
            (static / filename).write_bytes(js)
            (static / "legacy.js").write_text("legacy")
            (public / "private.env").write_text("synthetic private sentinel")
            (public / "index.html").write_text("<html>GOTC synthetic fixture</html>")
            (public / "assets").mkdir()
            (public / "assets/link.jpg").symlink_to(public / "private.env")
            (root / "logs").mkdir()
            key, cert = root / "server.key", root / "server.crt"
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                            "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
                            "-keyout", str(key), "-out", str(cert)], check=True, capture_output=True, timeout=20)
            with closing(socket.socket()) as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            origin = f"https://localhost:{port}"
            virtual_host = (f"server {{ listen 127.0.0.1:{port} ssl http2; server_name localhost;\n"
                            f"ssl_certificate {cert}; ssl_certificate_key {key};\n"
                            f"location /prior-validators/ {{ alias {public}/; etag on; }}\n"
                            "location = /game { return 204; }\n}\n")
            generated = nginx.nginx_candidate(virtual_host, origin, frontend=public)
            for service, endpoint in [("platform", nginx.PLATFORM), ("gateway", nginx.GATEWAY)]:
                unix_socket = root / (service + ".sock")
                upstreams.enter_context(synthetic_upstream(unix_socket, service))
                generated = generated.replace(endpoint, f"http://unix:{unix_socket}")
            config = root / "nginx.conf"
            config.write_text("daemon off; master_process off; pid nginx.pid; error_log logs/error.log;\n"
                              "events {}\nhttp {\naccess_log off; client_body_temp_path body; "
                              "proxy_temp_path proxy; fastcgi_temp_path fastcgi; scgi_temp_path scgi; "
                              "uwsgi_temp_path uwsgi;\n"
                              + nginx.HTTP_CONTEXT + generated + "}\n")
            commands = ["nginx", "-p", str(root), "-c", str(config)]
            check = subprocess.run([*commands, "-t"], text=True, capture_output=True, timeout=10)
            self.assertEqual(check.returncode, 0, check.stderr)
            context = self.loopback_context(cert)
            with subprocess.Popen(commands, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE) as process:
                try:
                    deadline = time.monotonic() + 6
                    while True:
                        try:
                            connection = http.client.HTTPSConnection("127.0.0.1", port, timeout=1,
                                                                     context=self.loopback_context(cert))
                            connection.connect()
                            connection.close()
                            break
                        except ConnectionRefusedError:
                            if time.monotonic() >= deadline or process.poll() is not None:
                                self.fail((root / "logs/error.log").read_text()[-1000:])
                            time.sleep(0.02)
                    context.set_alpn_protocols(["h2"])
                    with socket.create_connection(("127.0.0.1", port), timeout=3) as sock:
                        with context.wrap_socket(sock, server_hostname="localhost") as tls:
                            self.assertEqual(tls.selected_alpn_protocol(), "h2")
                            self.assertIn(tls.version(), ("TLSv1.2", "TLSv1.3"))
                    yield self.getter(port, cert), current, filename, js
                finally:
                    process.terminate()
                    process.wait(timeout=4)

    @staticmethod
    def loopback_context(cert):
        context = ssl.create_default_context(cafile=str(cert))
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        return context

    def getter(self, port, cert):
        def get(path, headers=None, method="GET"):
            connection = http.client.HTTPSConnection("127.0.0.1", port, context=self.loopback_context(cert), timeout=3)
            try:
                connection.request(method, path, headers=headers or {})
                response = connection.getresponse()
                return response.status, response.headers, response.read()
            finally:
                connection.close()

        return get

    def test_https_negotiation_static_headers_and_shared_host_isolation(self):
        with self.running() as (get, _, filename, js):
            for path, service in [("/gotc/api/session", "platform"), ("/gotc/v1/models", "gateway")]:
                with self.subTest(path=path):
                    status, _, response = get(path, {"X-Forwarded-For": "203.0.113.99", "X-Request-ID": "untrusted"})
                    self.assertEqual(status, 200)
                    body = json.loads(response)
                    self.assertEqual(body["service"], service)
                    self.assertEqual(body["path"], path)
                    self.assertEqual(body["client"], "127.0.0.1")
                    self.assertEqual(body["proto"], "https")
                    self.assertRegex(body["requestId"], r"^[a-f0-9]{32}$")
            self.assertEqual(get("/game")[0], 204)
            status, headers, html = get("/gotc/")
            self.assertEqual(status, 200)
            self.assertEqual(headers.get_content_type(), "text/html")
            self.assertEqual(headers["Strict-Transport-Security"], "max-age=31536000")
            self.assertIn(b"GOTC synthetic fixture", html)
            self.assertEqual(headers["Cache-Control"], "public, no-cache")
            self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
            status, headers, content = get("/gotc/static/site/" + filename, {"Accept-Encoding": "gzip"})
            self.assertEqual(status, 200)
            self.assertEqual(headers["Cache-Control"], "public, max-age=31536000, immutable")
            self.assertEqual(headers.get_content_type(), "text/javascript")
            self.assertEqual(headers["Content-Encoding"], "gzip")
            self.assertEqual(gzip.decompress(content), js)
            self.assertEqual(get("/gotc/static/site/" + filename, {"If-None-Match": headers["ETag"]})[0], 304)
            self.assertEqual(get("/gotc/static/site/" + filename, method="HEAD")[2], b"")
            for path in ("private.env", "static/site/legacy.js", "metrics", "readyz", "livez", "backend/gotc/service.py"):
                with self.subTest(path=path):
                    self.assertEqual(get("/gotc/" + path)[0], 404)
            self.assertIn(get("/gotc/assets/link.jpg")[0], (403, 404))


    def test_same_size_release_switch_returns_current_html_and_manifest(self):
        with self.running() as (get, current, _, _):
            first = current.resolve() / "frontend"
            second = current.parent / "second" / "frontend"
            second.mkdir(parents=True)
            source = {"index.html": b"<html>A</html>", "asset-manifest.json": b'{"version":"A"}'}
            validators = {}
            for name, old_content in source.items():
                for directory, content in [(first, old_content), (second, old_content.replace(b"A", b"B"))]:
                    path = directory / name
                    path.write_bytes(content)
                    os.utime(path, (0, 0))  # Deterministic release tar members have epoch mtimes.
                status, headers, previous = get("/prior-validators/" + name)
                self.assertEqual((status, previous), (200, old_content))
                validators[name] = (headers["ETag"], headers["Last-Modified"])
                self.assertTrue(all(validators[name]))

            pending = current.parent / "next"
            pending.symlink_to(second.parent, target_is_directory=True)
            pending.replace(current)
            for name, old_content in source.items():
                tag, timestamp = validators[name]
                # Confirm Nginx's legacy size/mtime validators really collide across these releases.
                _, legacy_headers, current_content = get("/prior-validators/" + name)
                self.assertEqual((legacy_headers["ETag"], legacy_headers["Last-Modified"]), (tag, timestamp))
                self.assertEqual(current_content, old_content.replace(b"A", b"B"))
                for condition in [{"If-None-Match": tag}, {"If-Modified-Since": timestamp},
                                  {"If-None-Match": tag, "If-Modified-Since": timestamp}]:
                    with self.subTest(name=name, headers=tuple(condition)):
                        status, headers, body = get("/gotc/" + name, condition)
                        self.assertEqual(status, 200)
                        self.assertEqual(headers["Cache-Control"], "public, no-cache")
                        self.assertEqual(body, current_content)


if __name__ == "__main__":
    unittest.main()
