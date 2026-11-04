"""Real HTTP regressions for API-key pages outliving their browser login."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import threading
import unittest
from unittest.mock import patch

import test_api
import test_keys


class KeySessionOwnershipTests(unittest.TestCase):
    setUp = test_keys.GatewayTests.setUp
    tearDown = test_keys.GatewayTests.tearDown
    team = test_api.APITests.team
    call = test_keys.GatewayTests.call

    def login_team(self, number=1):
        team_id = self.team(number)
        status, access, _ = self.admin.post('/api/admin/team-access', {'teamId': team_id, 'action': 'issue'})
        self.assertEqual(status, 200)
        client = test_api.Client(self.server)
        status, login, _ = client.post('/api/login', {'role': 'team', 'code': access['code']})
        self.assertEqual(status, 200)
        self.assertEqual(self.admin.post('/api/admin/keys/provision', {'teamId': team_id})[0], 200)
        return team_id, client, login['loginId'], access['code']

    def key_request(self, client, method, path, body=None, login_id=None):
        headers = {} if login_id is None else {'X-GOTC-Login': login_id}
        return client.request(method, path, body, headers=headers)

    def create_key(self, client, login_id):
        status, result, _ = self.key_request(client, 'POST', '/api/team/keys', {'name': 'synthetic key'}, login_id)
        self.assertEqual(status, 200, result)
        return result

    def snapshot(self):
        with self.service.db() as db:
            return {table: [tuple(row) for row in db.execute('SELECT * FROM '+table+' ORDER BY rowid')]
                    for table in ('api_keys', 'api_usage', 'audit')}

    def test_old_tab_cannot_read_create_or_delete_after_cookie_switch(self):
        _, old, old_login, _ = self.login_team(1)
        _, current, current_login, _ = self.login_team(2)
        created = self.create_key(current, current_login)
        before = self.snapshot()
        old.cookie = current.cookie  # Cookies are shared between tabs; their in-memory login IDs are not.
        for method, path, body in [('GET', '/api/team/keys', None), ('GET', '/api/team/keys/logs', None),
                                   ('POST', '/api/team/keys', {'name': 'old tab'}),
                                   ('POST', '/api/team/keys/delete', {'id': created['item']['id']})]:
            with self.subTest(method=method, path=path):
                status, result, _ = self.key_request(old, method, path, body, old_login)
                self.assertEqual(status, 401, result)
                self.assertNotIn('key', result)
                self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.key_request(current, 'GET', '/api/team/keys', login_id=current_login)[0], 200)

    def test_cached_clients_without_login_marker_cannot_use_key_routes(self):
        _, client, login_id, _ = self.login_team()
        created = self.create_key(client, login_id)
        before = self.snapshot()
        for marker in (None, ''):
            for method, path, body in [('GET', '/api/team/keys', None), ('GET', '/api/team/keys/logs', None),
                                       ('POST', '/api/team/keys', {'name': 'old client'}),
                                       ('POST', '/api/team/keys/delete', {'id': created['item']['id']})]:
                with self.subTest(marker=marker, path=path, method=method):
                    status, result, _ = self.key_request(client, method, path, body, marker)
                    self.assertEqual((status, result.get('code')), (428, 'login_required'))
                    self.assertEqual(self.snapshot(), before)

    def test_new_login_to_same_team_also_invalidates_old_page_marker(self):
        _, client, old_login, code = self.login_team()
        status, login, _ = client.post('/api/login', {'role': 'team', 'code': code})
        self.assertEqual(status, 200)
        self.assertNotEqual(login['loginId'], old_login)
        before = self.snapshot()
        self.assertEqual(self.key_request(client, 'POST', '/api/team/keys', {'name': 'stale'}, old_login)[0], 401)
        self.assertEqual(self.snapshot(), before)
        self.create_key(client, login['loginId'])

    def test_current_browser_ownership_does_not_replace_team_authorization(self):
        _, first, first_login, _ = self.login_team(1)
        _, second, second_login, _ = self.login_team(2)
        created = self.create_key(second, second_login)
        before = self.snapshot()
        self.assertEqual(self.key_request(first, 'POST', '/api/team/keys/delete',
                                         {'id': created['item']['id']}, first_login)[0], 404)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.public.get('/api/team/keys')[0], 401)
        self.assertEqual(self.admin.get('/api/team/keys')[0], 403)
        # SDK routes use the bearer key, not the website's login marker.
        self.assertEqual(self.call('/v1/models', None, created['key'], method='GET')[0], 200)
        self.assertEqual(self.key_request(second, 'POST', '/api/team/keys/delete',
                                         {'id': created['item']['id']}, second_login)[0], 200)

    def test_revocation_while_waiting_for_write_lock_cannot_create_key(self):
        team_id, client, login_id, _ = self.login_team()
        waiting = threading.Event()
        original_db = self.service.db

        class ObservedConnection:
            def __init__(self, connection):
                self.connection = connection

            def execute(self, sql, *args):
                if sql == 'BEGIN IMMEDIATE':
                    waiting.set()
                return self.connection.execute(sql, *args)

            def __getattr__(self, name):
                return getattr(self.connection, name)

        @contextmanager
        def observed_db():
            with original_db() as connection:
                yield ObservedConnection(connection)

        with original_db() as holder, ThreadPoolExecutor(max_workers=1) as pool:
            holder.execute('BEGIN IMMEDIATE')
            with patch.object(self.service, 'db', observed_db):
                future = pool.submit(self.key_request, client, 'POST', '/api/team/keys',
                                     {'name': 'queued'}, login_id)
                try:
                    self.assertTrue(waiting.wait(3), 'request did not reach the write lock')
                    holder.execute('DELETE FROM sessions WHERE account_id=(SELECT account_id FROM team_accounts WHERE team_id=?)', (team_id,))
                finally:
                    holder.commit()
                self.assertEqual(future.result(timeout=5)[0], 401)
        with self.service.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM api_keys').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
