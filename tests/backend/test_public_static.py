"""Packaged frontend revalidation and private data directory regressions."""
from contextlib import closing
import http.client
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from backend.gotc import bootstrap
from backend.gotc.http.server import EventHTTPServer
from backend.gotc.judging.standings import STANDINGS_SQL
from backend.gotc.service import EventService


class PackagedServiceTests(unittest.TestCase):
    def setUp(self):
        tree = tempfile.TemporaryDirectory()
        self.addCleanup(tree.cleanup)
        self.root = Path(tree.name).resolve()
        self.frontend = self.root / "frontend"
        static = self.frontend / "static" / "platform"
        static.mkdir(parents=True)
        self.module = static / "login.js"
        self.module.write_text("export const ready = true;", encoding="utf-8")
        (static / "login.css").write_text("body {}", encoding="utf-8")
        self.service = EventService(data_dir=self.root / "private", static_root=self.frontend,
                                    origin="http://127.0.0.1")

    def serve(self):
        server = EventHTTPServer(("127.0.0.1", 0), self.service)
        self.addCleanup(server.server_close)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        return server.server_port

    def request(self, port, path, method="GET", headers=None):
        with closing(http.client.HTTPConnection("127.0.0.1", port, timeout=3)) as client:
            client.request(method, "/gotc" + path, headers=headers or {})
            response = client.getresponse()
            return response.status, response.headers, response.read()

    def test_nested_modules_and_styles_revalidate(self):
        port = self.serve()
        path = "/static/platform/login.js"
        status, headers, body = self.request(port, path)
        self.assertEqual(status, 200)
        self.assertEqual(body, self.module.read_bytes())
        self.assertEqual(headers["Content-Type"], "text/javascript; charset=utf-8")
        self.assertEqual(headers["Cache-Control"], "public, no-cache")
        validator = headers["ETag"]
        status, headers, body = self.request(port, path, "HEAD")
        self.assertEqual((status, headers["ETag"], body), (200, validator, b""))
        for method in ("GET", "HEAD"):
            status, headers, body = self.request(port, path, method, {"If-None-Match": validator})
            self.assertEqual((status, headers["ETag"], body), (304, validator, b""))
        self.module.write_text("export const ready = false;", encoding="utf-8")
        status, headers, updated = self.request(port, path, headers={"If-None-Match": validator})
        self.assertEqual((status, updated), (200, self.module.read_bytes()))
        self.assertNotEqual(headers["ETag"], validator)
        status, headers, _ = self.request(port, "/static/platform/login.css")
        self.assertEqual((status, headers["Content-Type"]), (200, "text/css; charset=utf-8"))

    def test_private_files_and_directories_are_never_public_assets(self):
        port = self.serve()
        static = self.module.parent
        (static / "notes.txt").write_text("fixture")
        (static / "link.js").symlink_to(self.module)
        (static / ".hidden.js").write_text("fixture")
        for path in ("/static/platform/", "/static/platform/notes.txt", "/static/platform/link.js",
                     "/static/platform/.hidden.js", "/private/event.sqlite3", "/api/board", "/materials/unknown"):
            with self.subTest(path=path):
                status, headers, _ = self.request(port, path, headers={"If-None-Match": "*"})
                self.assertIn(status, (401, 404))
                self.assertEqual(headers["Cache-Control"], "no-store")
                self.assertIsNone(headers["ETag"])

        status, headers, _ = self.request(port, "/api/session", method="PUT")
        self.assertEqual(status, 501)
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_standings_query_uses_team_indexes_after_reopening_database(self):
        reopened = EventService(data_dir=self.service.data_dir, static_root=self.frontend)
        with reopened.db() as db:
            plan = [row["detail"] for row in db.execute("EXPLAIN QUERY PLAN " + STANDINGS_SQL, (1,))]
        self.assertTrue(any("submitted_scores_by_team" in step and "SEARCH" in step for step in plan), plan)
        self.assertTrue(any("votes_by_team" in step and "SEARCH" in step for step in plan), plan)

    def test_existing_legacy_state_requires_explicit_data_path(self):
        legacy = self.root / "server" / "data"
        legacy.mkdir(parents=True)
        (legacy / "event.sqlite3").touch()
        data_home = self.root / "user-state"
        with patch.dict(os.environ, {"GOTC_DATA_DIR": "", "XDG_DATA_HOME": str(data_home)}):
            with self.assertRaisesRegex(RuntimeError, "GOTC_DATA_DIR"):
                bootstrap.data_directory(None, self.root)
            self.assertFalse(data_home.exists())
            self.assertEqual(bootstrap.data_directory(legacy, self.root), legacy)

    def test_existing_local_development_state_requires_explicit_data_path(self):
        legacy = self.root / ".runtime" / "local-data"
        legacy.mkdir(parents=True)
        (legacy / "event.sqlite3").touch()
        data_home = self.root / "user-state"
        with patch.dict(os.environ, {"GOTC_DATA_DIR": "", "XDG_DATA_HOME": str(data_home)}):
            with self.assertRaisesRegex(RuntimeError, r"\.runtime/local-data"):
                bootstrap.data_directory(None, self.root)
            self.assertFalse(data_home.exists())
        with patch.dict(os.environ, {"GOTC_DATA_DIR": str(legacy)}):
            self.assertEqual(bootstrap.data_directory(None, self.root), legacy)
