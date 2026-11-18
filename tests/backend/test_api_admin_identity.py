"""Console requests must belong to the page login, including upstream probes."""
import unittest
from unittest.mock import patch

import test_api
import test_keys


class ApiAdminIdentityTests(unittest.TestCase):
    setUp = test_keys.GatewayTests.setUp
    tearDown = test_keys.GatewayTests.tearDown
    team = test_keys.GatewayTests.team
    team_client = test_keys.GatewayTests.team_client
    enabled_key = test_keys.GatewayTests.enabled_key

    def test_missing_or_old_page_proof_cannot_read_write_or_probe(self):
        team_id = self.team()
        previous_login = self.admin.login_id
        second = test_api.Client(self.server)
        self.assertEqual(second.post('/api/login', {'role': 'admin', 'code': self.admin_code})[0], 200)
        self.assertNotEqual(previous_login, second.login_id)
        requests = [
            ('GET', '/api/admin/keys', None),
            ('POST', '/api/admin/keys/upstreams', {'upstreams': {}}),
            ('POST', '/api/admin/keys/settings', {'maxKeys': 1}),
            ('POST', '/api/admin/keys/provision', {'teamId': team_id}),
            ('POST', '/api/admin/keys/logs/clear', {}),
            ('POST', '/api/admin/keys/stats', {}),
            ('GET', '/api/admin/keys/upstreams/test/openai', None),
            ('GET', '/api/admin/keys/upstreams/models/openai', None),
            ('POST', '/api/admin/keys/upstreams/models', {'kind': 'openai', 'url': self.upstreams['openai']['url'], 'key': ''}),
        ]
        with patch.object(self.service.keys.opener, 'open', side_effect=AssertionError('must not call upstream')) as opened:
            for marker, expected in [('', 428), (previous_login, 401)]:
                for method, path, body in requests:
                    with self.subTest(marker=bool(marker), path=path):
                        self.assertEqual(second.request(method, path, body, headers={'X-GOTC-Login': marker})[0], expected)
            opened.assert_not_called()
        with self.service.db() as db:
            self.assertEqual(self.service.keys.raw_config(db), self.upstreams)
            self.assertEqual(self.service.keys.settings(db)['maxKeys'], 5)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM team_api').fetchone()[0], 0)

    def test_current_page_can_save_probe_and_download(self):
        self.assertEqual(self.admin.get('/api/admin/keys')[0], 200)
        self.assertEqual(self.admin.post('/api/admin/keys/upstreams', {'upstreams': self.upstreams})[0], 200)
        status, result, _ = self.admin.get('/api/admin/keys/upstreams/test/openai')
        self.assertEqual(status, 200)
        self.assertTrue(result['ok'])
        self.assertEqual(len(test_keys.FakeUpstream.calls), 1)
        # Normal anchor navigation has only the cookie, while an explicitly stale marker is rejected.
        download = test_api.Client(self.server)
        download.cookie = self.admin.cookie
        self.assertEqual(download.get('/api/admin/keys/export')[0], 200)
        self.assertEqual(download.request('GET', '/api/admin/keys/export', headers={'X-GOTC-Login': 'old'})[0], 401)
        self.assertEqual(self.public.get('/api/admin/keys/export')[0], 401)


if __name__ == '__main__':
    unittest.main()
