"""Black-box platform admission, caching, connection reuse and proxy trust."""
import asyncio
from collections import defaultdict
from contextlib import closing
import http.client
import unittest
from typing import is_typeddict
from unittest.mock import patch

import httpx
from starlette.datastructures import Headers

import test_api
from backend.gotc.http import request_models as models
from backend.gotc.http.app import create_platform_app
from backend.gotc.http.policy import client_ip
from backend.gotc.http.routes import endpoints, resolve_endpoint


class RouteDeclarationsTests(unittest.TestCase):
    def test_each_post_declares_a_payload_contract_or_a_specific_retirement(self):
        unmodeled = set()
        for endpoint in endpoints():
            if endpoint.method != "POST":
                continue
            with self.subTest(path=endpoint.path):
                if endpoint.model is None:
                    self.assertTrue(endpoint.unmodeled)
                    unmodeled.add(endpoint.path)
                    continue
                self.assertIsNone(endpoint.unmodeled)
                choices = endpoint.model if isinstance(endpoint.model, tuple) else (endpoint.model,)
                self.assertTrue(choices)
                self.assertTrue(all(is_typeddict(model) for model in choices))
        self.assertEqual(unmodeled, {"/api/registration/create", "/api/admin/registration/rotate"})

    def test_alternative_judging_window_and_claim_import_forms_keep_distinct_keys(self):
        cases = [
            ("/api/admin/judging/roadshow/window", {"expectedRevision": 1, "open": True, "start90Minutes": True}),
            ("/api/admin/judging/exhibition/window", {"expectedRevision": 2, "open": False, "deadline": None}),
            ("/api/admin/claims/roster", {"people": []}),
            ("/api/admin/claims/roster", {"autoReview": {"expectedRevision": 1, "entries": [],
                                                         "sourceName": "synthetic", "sourceSha256": "0" * 64}}),
        ]
        for path, body in cases:
            with self.subTest(path=path, fields=sorted(body)):
                alternatives = resolve_endpoint("POST", path).model
                self.assertEqual(sum(models.accepts_fields(body, model) for model in alternatives), 1)
                self.assertFalse(any(models.accepts_fields({**body, "unexpected": True}, model)
                                     for model in alternatives))

    def test_each_route_resolves_to_its_declared_controller_without_shadowing(self):
        for endpoint in endpoints():
            with self.subTest(method=endpoint.method, path=endpoint.path):
                path = endpoint.path.format_map(defaultdict(lambda: "sample"))
                self.assertIs(resolve_endpoint(endpoint.method, path), endpoint)
                self.assertTrue(callable(endpoint.controller))


class HTTPTransportTests(unittest.TestCase):
    setUp = test_api.APITests.setUp
    tearDown = test_api.APITests.tearDown

    def test_invalid_bodies_preserve_identity_and_opaque_receipt_responses(self):
        path = "/api/admin/claims/review"
        self.assertEqual(self.public.post(path, {"unexpected": True})[0], 401)
        self.assertEqual(self.admin.post(path, {"unexpected": True})[0], 428)
        self.assertEqual(self.admin.request("POST", path, {"unexpected": True},
                                            headers={"X-GOTC-Login": self.admin.login_id})[0], 400)
        status, result, _ = self.public.post("/api/claims/status", {"unexpected": True})
        self.assertEqual((status, result["status"]), (200, "invalid"))
        self.assertEqual(self.public.post("/api/registration/create", {"unexpected": True})[0], 410)

    def test_http_and_programmatic_calls_run_the_same_declared_operation(self):
        path = "/api/session"
        with patch.object(self.service, "dispatch_endpoint", wraps=self.service.dispatch_endpoint) as invoke:
            http_value = self.public.get(path)[1]
            direct_value = self.service.dispatch("GET", path, {}, "", "127.0.0.1")[0]
        self.assertEqual(http_value, direct_value)
        self.assertEqual([call.args[0] for call in invoke.call_args_list], [resolve_endpoint("GET", path)] * 2)

    def test_untrusted_tcp_forwarding_is_ignored_and_unix_proxy_must_match(self):
        path = "/run/gotc-platform/http.sock"
        template = {"headers": Headers({"x-real-ip": "192.0.2.44"}).raw}
        self.assertEqual(client_ip({**template, "server": ("127.0.0.1", 9999), "client": ("127.0.0.1", 1)}, path),
                         "127.0.0.1")
        self.assertEqual(client_ip({**template, "server": (path, None), "client": None}, path), "192.0.2.44")
        self.assertEqual(client_ip({**template, "server": ("/other", None), "client": None}, path), "unknown")
        ws_scope = {"server": (path, None), "client": None, "headers": [(b"X-Real-IP", b"192.0.2.45")]}
        self.assertEqual(client_ip(ws_scope, path), "192.0.2.45")
        ws_scope["headers"].append((b"x-real-ip", b"192.0.2.46"))
        self.assertEqual(client_ip(ws_scope, path), "unknown")

    def test_ambiguous_framing_when_present_in_asgi_request_is_rejected(self):
        async def attempt():
            app = create_platform_app(self.service)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=self.service.origin) as client:
                for extra in (("Content-Length", "2"), ("Transfer-Encoding", "chunked")):
                    with self.subTest(extra=extra[0]):
                        result = await client.post(self.service.base_path + "/api/login", content=b"{}",
                                                   headers=[("Origin", self.service.origin), ("Content-Type", "application/json"),
                                                            ("Content-Length", "2"), extra])
                        self.assertEqual((result.status_code, result.headers["Cache-Control"]), (400, "no-store"))
        asyncio.run(attempt())

    def test_unauthorized_and_oversize_bodies_are_rejected_from_headers(self):
        for path, size, expected in (("/api/admin/settings", 100, 401), ("/api/login", 16385, 413)):
            with self.subTest(path=path), closing(http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)) as client:
                client.request("POST", self.service.base_path + path, headers={"Content-Length": str(size),
                               "Content-Type": "application/json", "Origin": self.service.origin})
                response = client.getresponse()
                self.assertEqual(response.status, expected)
                response.read()

    def test_keepalive_between_private_and_public_responses(self):
        with closing(http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)) as client:
            client.request("GET", self.service.base_path + "/api/session")
            private = client.getresponse()
            self.assertEqual((private.status, private.headers["Cache-Control"]), (200, "no-store"))
            private.read()
            sock = client.sock
            self.assertIsNotNone(sock)
            client.request("GET", self.service.base_path + "/")
            public = client.getresponse()
            self.assertEqual((public.status, public.headers["Cache-Control"]), (200, "public, no-cache"))
            public.read()
            self.assertIs(client.sock, sock)
