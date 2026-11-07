"""Pure security regressions for the API-key gateway's browser-source boundary."""
from email.message import Message
import unittest

from gateway_cors import ALLOWED_HEADERS, origin_allowed, preflight_allowed, response_headers


TRUSTED = "https://8.141.114.201"


def headers(*items):
    value = Message()
    for name, item in items:
        value[name] = item
    return value


def preflight(origin=TRUSTED, method="POST", requested="authorization,content-type"):
    return headers(("Origin", origin), ("Access-Control-Request-Method", method),
                   ("Access-Control-Request-Headers", requested), ("Sec-Fetch-Site", "same-origin"))


class OriginTests(unittest.TestCase):
    def test_exact_same_origin_and_originless_sdk_allowed(self):
        for request in (headers(), headers(("Origin", TRUSTED)),
                        headers(("Sec-Fetch-Site", "none")), headers(("Sec-Fetch-Site", "same-origin")),
                        headers(("Origin", TRUSTED), ("Sec-Fetch-Site", "same-origin"))):
            self.assertTrue(origin_allowed(request, TRUSTED))

    def test_null_empty_external_and_lookalike_origins_rejected(self):
        values = ["", "null", "NULL", "https://evil.example", "https://8.141.114.201.evil.example",
                  "https://8.141.114.201@evil.example", "https://evil.example@8.141.114.201", "http://8.141.114.201",
                  "https://8.141.114.201:443", "https://8.141.114.201/", "https://8.141.114.201?x=1",
                  "https://8.141.114.201#x", "https://8.141.114.201,https://evil.example",
                  " " + TRUSTED, TRUSTED + " ", TRUSTED + "\r\nX-Injected: yes", TRUSTED + "\x00"]
        for value in values:
            with self.subTest(origin=value):
                request = headers(("Origin", value))
                self.assertFalse(origin_allowed(request, TRUSTED))
                self.assertEqual(response_headers(request, TRUSTED), {})

    def test_duplicate_origins_rejected_even_if_identical_or_different_case(self):
        for second in (TRUSTED, "null", "", "https://evil.example"):
            request = headers(("Origin", TRUSTED), ("origin", second))
            self.assertFalse(origin_allowed(request, TRUSTED))
            self.assertEqual(response_headers(request, TRUSTED), {})

    def test_cross_site_fetch_metadata_rejected_even_without_origin(self):
        for source in ("cross-site", "same-site", "", "SAME-ORIGIN", "same-origin, none", "same-origin\r\n"):
            for with_origin in (False, True):
                request = headers(("Sec-Fetch-Site", source))
                if with_origin:
                    request["Origin"] = TRUSTED
                with self.subTest(source=source, with_origin=with_origin):
                    self.assertFalse(origin_allowed(request, TRUSTED))

    def test_duplicate_fetch_metadata_rejected(self):
        request = headers(("Origin", TRUSTED), ("Sec-Fetch-Site", "same-origin"), ("sec-fetch-site", "same-origin"))
        self.assertFalse(origin_allowed(request, TRUSTED))

    def test_normal_mapping_uses_case_insensitive_names_without_flattening_values(self):
        self.assertTrue(origin_allowed({"origin": TRUSTED, "sec-fetch-site": "same-origin"}, TRUSTED))
        self.assertFalse(origin_allowed({"Origin": TRUSTED, "origin": TRUSTED}, TRUSTED))
        self.assertFalse(origin_allowed({"Origin": [TRUSTED]}, TRUSTED))

    def test_invalid_trusted_configuration_cannot_be_reflected(self):
        for origin in ("*", "null", "file://local", "https://user@host", "https://host/path", "https://host:0",
                       "https://host\r\nInjected: true", "https://host:bad", ""):
            with self.subTest(origin=origin):
                self.assertEqual(response_headers(headers(("Origin", origin)), origin), {})

    def test_local_http_origin_supported_only_when_exactly_configured(self):
        origin = "http://localhost:5274"
        self.assertTrue(origin_allowed(headers(("Origin", origin)), origin))
        self.assertFalse(origin_allowed(headers(("Origin", "http://127.0.0.1:5274")), origin))


class PreflightTests(unittest.TestCase):
    def test_supported_methods_and_all_explicit_headers(self):
        for method in ("GET", "HEAD", "POST"):
            request = preflight(method=method, requested=", ".join(ALLOWED_HEADERS))
            self.assertTrue(preflight_allowed(request, TRUSTED))

    def test_optional_or_empty_requested_header_list_supported(self):
        request = preflight()
        del request["Access-Control-Request-Headers"]
        self.assertTrue(preflight_allowed(request, TRUSTED))
        for value in ("", " , , ", "AUTHORIZATION, , Content-TYPE, "):
            with self.subTest(value=value):
                self.assertTrue(preflight_allowed(preflight(requested=value), TRUSTED))

    def test_method_required_exact_and_not_duplicated(self):
        for method in ("", "DELETE", "PATCH", "PUT", "OPTIONS", "get", "POST,GET", " POST", "POST\r\n"):
            with self.subTest(method=method):
                self.assertFalse(preflight_allowed(preflight(method=method), TRUSTED))
        request = preflight()
        del request["Access-Control-Request-Method"]
        self.assertFalse(preflight_allowed(request, TRUSTED))
        request["Access-Control-Request-Method"] = "POST"
        request["access-control-request-method"] = "POST"
        self.assertFalse(preflight_allowed(request, TRUSTED))

    def test_preflight_requires_explicit_allowed_origin(self):
        for value in ("", "null", "https://evil.example"):
            self.assertFalse(preflight_allowed(preflight(origin=value), TRUSTED))
        request = preflight()
        del request["Origin"]
        self.assertFalse(preflight_allowed(request, TRUSTED))

    def test_cookie_unknown_and_invalid_requested_names_rejected(self):
        for requested in ("Cookie", "authorization, cookie", "X-Evil", "Origin", "Sec-Fetch-Site", "Host",
                          "Content-Length", "Proxy-Authorization", "*", "Authorization: abc", "Authorization\r\nX-Evil",
                          "X-API-Key", "Authorization,authorization"):
            with self.subTest(requested=requested):
                request = preflight(requested=requested)
                self.assertFalse(preflight_allowed(request, TRUSTED))
                self.assertEqual(response_headers(request, TRUSTED, preflight=True), {})

    def test_duplicate_acr_header_fields_rejected(self):
        request = preflight()
        request["access-control-request-headers"] = "authorization"
        self.assertFalse(preflight_allowed(request, TRUSTED))

    def test_preflight_also_rejects_cross_site_metadata(self):
        request = preflight()
        request.replace_header("Sec-Fetch-Site", "cross-site")
        self.assertFalse(preflight_allowed(request, TRUSTED))


class ResponseTests(unittest.TestCase):
    def assert_private_origin_only(self, result):
        self.assertEqual(result["Access-Control-Allow-Origin"], TRUSTED)
        self.assertEqual(result["Vary"], "Origin")
        self.assertNotIn("Access-Control-Allow-Credentials", result)
        self.assertFalse(any("*" in value for value in result.values()))

    def test_normal_response_exposes_only_request_id_and_retry_after(self):
        result = response_headers(headers(("Origin", TRUSTED)), TRUSTED)
        self.assert_private_origin_only(result)
        self.assertEqual(result["Access-Control-Expose-Headers"], "X-Client-Request-ID, Retry-After")
        self.assertNotIn("Access-Control-Allow-Headers", result)

    def test_preflight_response_uses_static_allowlist_not_reflection(self):
        result = response_headers(preflight(requested="authorization"), TRUSTED, preflight=True)
        self.assert_private_origin_only(result)
        self.assertEqual(result["Access-Control-Allow-Methods"], "GET, HEAD, POST, OPTIONS")
        self.assertEqual(result["Access-Control-Allow-Headers"], ", ".join(ALLOWED_HEADERS))
        self.assertNotIn("Cookie", result["Access-Control-Allow-Headers"])

    def test_sdk_response_has_no_browser_cors_headers(self):
        self.assertEqual(response_headers(headers(), TRUSTED), {})
        self.assertEqual(response_headers(headers(), TRUSTED, preflight=True), {})

    def test_bad_preflight_never_gets_partial_allow_headers(self):
        self.assertEqual(response_headers(preflight(requested="Cookie"), TRUSTED, preflight=True), {})

    def test_helper_never_reads_body_or_requires_credentials(self):
        request = preflight()
        before = list(request.items())
        self.assertTrue(preflight_allowed(request, TRUSTED))
        response_headers(request, TRUSTED, preflight=True)
        self.assertEqual(list(request.items()), before)
        self.assertIsNone(request.get("Authorization"))


if __name__ == "__main__":
    unittest.main()
