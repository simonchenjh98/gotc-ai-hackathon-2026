"""Record a stable, credential-free projection of normal HTTP contracts."""

import argparse
from http.cookies import SimpleCookie
from io import BytesIO
import json
import secrets
from pathlib import Path
from urllib.parse import urlsplit
from zipfile import ZipFile

from local_event import BASE, ORIGIN, Client, event, seeded


def record(source, layout):
    report = {}
    with event(source, layout) as (platform, gateway, private):
        (private / "finals-access-code.txt").write_text(
            f"{secrets.randbelow(10**12):012d}"
        )
        admin, team, _, code = seeded(platform, private)
        public = Client(platform)
        sdk = Client(gateway)

        def probe(name, client, method, path, status, body=None, headers=None, keep=()):
            actual, response, data = client.request(method, path, body, headers)
            assert actual == status, f"{name}: expected {status}, received {actual}"
            report[name] = {"status": actual, **{key: response[key] for key in keep}}
            return data, response

        body, _ = probe(
            "anonymous_session",
            public,
            "GET",
            "/api/session",
            200,
            keep=("content-type", "cache-control", "x-content-type-options"),
        )
        assert json.loads(body)["role"] is None
        probe("organizer_only", public, "GET", "/api/admin", 401)
        raw, response = probe(
            "team_cookie",
            public,
            "POST",
            "/api/login",
            200,
            {"role": "team", "code": code},
        )
        cookie = SimpleCookie(response["set-cookie"])["gotc_session"]
        report["team_cookie"].update(
            name=cookie.key,
            path=cookie["path"],
            http_only=bool(cookie["httponly"]),
            same_site=cookie["samesite"],
            secure=bool(cookie["secure"]),
        )
        assert (
            json.loads(raw)["role"] == "team"
            and cookie["path"] == BASE
            and cookie["secure"]
            and cookie["httponly"]
        )
        probe(
            "missing_identity",
            team,
            "GET",
            "/api/team/profile",
            428,
            headers={"X-GOTC-Login": ""},
        )
        probe("private_profile", team, "GET", "/api/team/profile", 200)
        probe("finals_entry_requires_token", public, "GET", "/api/finals/entry", 401)
        probe(
            "cross_site_vote",
            public,
            "POST",
            "/api/vote",
            403,
            {},
            {"Sec-Fetch-Site": "cross-site"},
        )
        probe(
            "public_registration_preflight",
            public,
            "OPTIONS",
            "/api/registration",
            204,
            headers={
                "Origin": "https://simonchenjh98.github.io",
                "Access-Control-Request-Method": "GET",
            },
            keep=("access-control-allow-origin", "access-control-allow-methods"),
        )
        probe(
            "gateway_preflight",
            sdk,
            "OPTIONS",
            "/v1/chat/completions",
            204,
            headers={
                "Origin": ORIGIN,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "Authorization, Content-Type",
            },
            keep=("access-control-allow-origin", "access-control-allow-methods"),
        )
        first = {
            "expectedVersion": 0,
            "status": "draft",
            "materials": {"summary": "First version"},
        }
        body, _ = probe("own_draft", team, "POST", "/api/submission", 200, first)
        assert json.loads(body)["submission"]["version"] == 1
        probe("stale_revision", team, "POST", "/api/submission", 409, first)
        body, _ = probe(
            "organizer_csv",
            admin,
            "GET",
            "/api/admin/export",
            200,
            keep=("content-type", "content-disposition"),
        )
        assert "Fixture team" in body.decode("utf-8-sig")
        archive = BytesIO()
        with ZipFile(archive, "w") as z:
            z.writestr("hello.txt", "Hello")
        for kind, name, payload in [
            ("files", "submission.zip", archive.getvalue()),
            ("demo", "index.html", b"<!doctype html><html><title>Test</title></html>"),
        ]:
            body, _ = probe(
                kind + "_upload",
                team,
                "POST",
                "/api/team/uploads/" + kind,
                200,
                payload,
                {
                    "Content-Type": "application/octet-stream",
                    "X-GOTC-Project": "Fixture",
                    "X-GOTC-Filename": name,
                },
            )
            upload = json.loads(body)
            path = urlsplit(upload["url"]).path.removeprefix(BASE)
            header_names = (
                "content-type",
                "x-content-type-options",
                "content-disposition",
            )
            if kind == "demo":
                header_names += ("content-security-policy",)
            stored, _ = probe(
                kind + "_download", public, "GET", path, 200, keep=header_names
            )
            assert stored == payload
            report[kind + "_upload"]["filename"] = upload["filename"]
            raw, _ = probe(kind + "_head", public, "HEAD", path, 200)
            assert raw == b""
        # HTML and ETags stay on the platform's integration route. Production Nginx
        # and immutable hashed asset responses are verified by packaging tests.
        html, hdr = probe(
            "public_html",
            public,
            "GET",
            "/platform.html",
            200,
            keep=("x-content-type-options", "content-type"),
        )
        assert b"platform-main" in html
        if "etag" in hdr:
            probe(
                "conditional_html",
                public,
                "GET",
                "/platform.html",
                304,
                headers={"If-None-Match": hdr["etag"]},
            )
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source", type=Path, default=Path(__file__).resolve().parents[2]
    )
    parser.add_argument("--layout", choices=("baseline", "uds"), default="uds")
    parser.add_argument(
        "--compare",
        type=Path,
        help="Exact expected contract JSON after normalising transport headers",
    )
    args = parser.parse_args()
    result = record(args.source.resolve(), args.layout)
    if args.compare:
        expected = json.loads(args.compare.read_text())
        if result != expected:
            different = [
                k
                for k in sorted(expected.keys() | result.keys())
                if result.get(k) != expected.get(k)
            ]
            raise SystemExit("Contract differences: " + ", ".join(different))
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
