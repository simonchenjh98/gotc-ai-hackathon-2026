"""Keep the public edge and application body limits in agreement."""
import re
import unittest

from backend.gotc.http.routes import endpoints
from ops.deploy.nginx import PLATFORM, locations


class DeployConfigTests(unittest.TestCase):
    def test_public_and_organizer_request_bodies_remain_bounded(self):
        config = locations()
        post_limits = {e.path: e.limit for e in endpoints() if e.method == "POST"}
        expected = {
            "= /gotc/api/admin/data/import/preview": post_limits["/api/admin/data/import/preview"],
            "= /gotc/api/admin/claims/roster": post_limits["/api/admin/claims/roster"],
            "= /gotc/api/admin/claims/review": post_limits["/api/admin/claims/review"],
            "^~ /gotc/api/team/uploads/": 100 * 1024 * 1024,
            "^~ /gotc/api/": 32 * 1024,
        }
        for location, size in expected.items():
            with self.subTest(location=location):
                chunks = re.findall(r"location " + re.escape(location) + r" \{([^{}]+)\}", config)
                self.assertEqual(len(chunks), 1)
                self.assertIn(f"proxy_pass {PLATFORM};", chunks[0])
                match = re.search(r"client_max_body_size (\d+)([km]);", chunks[0])
                self.assertIsNotNone(match)
                self.assertEqual(int(match[1]) * {"k": 1024, "m": 1024**2}[match[2]], size)
                self.assertIn("proxy_set_header X-Forwarded-For $remote_addr;", chunks[0])
                self.assertIn("proxy_set_header X-Forwarded-Proto $scheme;", chunks[0])


if __name__ == "__main__":
    unittest.main()
