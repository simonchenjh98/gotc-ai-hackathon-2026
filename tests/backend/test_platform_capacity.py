"""Capacity regressions using disposable events and synthetic local traffic."""
from concurrent.futures import ThreadPoolExecutor
import http.client
import tempfile
import threading
import time
import unittest
from urllib.parse import quote

from backend.gotc.errors import APIError
from backend.gotc.gateway.accounting import now_ms
from backend.gotc.gateway.config import settings
from backend.gotc.service import EventService
from backend.gotc.submissions.repositories import ProviderOperations
import test_submissions


class ProviderCapacityTests(unittest.TestCase):
    def test_all_providers_share_the_running_limit(self):
        operations = ProviderOperations()
        contexts = [operations.operation(name) for name in ['github'] * 4 + ['gitee'] * 2]
        for context in contexts:
            context.__enter__()
        try:
            with self.assertRaises(APIError) as raised:
                with operations.operation('gitee'):
                    self.fail('A seventh provider operation must not start')
            self.assertEqual((raised.exception.status, raised.exception.code), (503, 'repository_busy'))
        finally:
            for context in reversed(contexts):
                context.__exit__(None, None, None)
        with operations.operation('gitee'):
            pass


class WorkerIsolationTests(unittest.TestCase):
    tearDown = test_submissions.SubmissionAPITests.tearDown
    team = test_submissions.SubmissionAPITests.team
    access = test_submissions.SubmissionAPITests.access
    open_submissions = test_submissions.SubmissionAPITests.open_submissions

    def setUp(self):
        test_submissions.SubmissionAPITests.setUp(self)
        self.open_submissions()

    def start_upload(self, client):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=10)
        connection.putrequest('POST', self.service.base_path + '/api/team/uploads/slides')
        headers = {'Cookie': client.cookie, 'Origin': self.service.origin, 'Content-Length': '1048576',
                   'Content-Type': 'application/octet-stream', 'X-GOTC-Login': client.login_id,
                   'X-GOTC-Project': quote('Synthetic Project', safe=''), 'X-GOTC-Filename': 'slides.pdf'}
        for name, value in headers.items():
            connection.putheader(name, value)
        connection.endheaders(b'%PDF-')
        return connection

    def wait_until(self, condition):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(.01)
        self.fail('The synthetic workload did not reach the expected state')

    def test_uploads_and_provider_calls_leave_api_workers_available(self):
        clients = [self.access(self.team(number=index + 1, name=f'Capacity {index}'))[0] for index in range(4)]
        uploads = [self.start_upload(client) for client in clients]
        self.wait_until(lambda: len(self.service.material_uploads.active_teams) == 4)
        waiting, _ = self.access(self.team(number=5, name='Capacity 5'))
        status, body, _headers = waiting.request('POST', '/api/team/uploads/slides', raw=b'%PDF-', headers={
            'Content-Type': 'application/octet-stream', 'X-GOTC-Login': waiting.login_id,
            'X-GOTC-Project': quote('Synthetic Project', safe=''), 'X-GOTC-Filename': 'slides.pdf'})
        self.assertEqual((status, body.get('code')), (429, 'upload_busy'))

        entered = threading.Semaphore(0)
        release = threading.Event()
        self.addCleanup(release.set)

        def blocked_dispatch(*_args):
            entered.release()
            release.wait(10)
            return {'ok': True}

        self.service.repositories.dispatch = blocked_dispatch
        provider_clients = [*clients, *clients[:2]]
        with ThreadPoolExecutor(len(provider_clients) + 1) as pool:
            provider_calls = [pool.submit(client.get, '/api/team/repository') for client in provider_clients]
            for _ in provider_clients:
                self.assertTrue(entered.acquire(timeout=5))
            status, body, _headers = pool.submit(self.public.get, '/api/session').result(timeout=3)
            self.assertEqual((status, body['role']), (200, None))
            release.set()
            self.assertEqual([call.result(timeout=5)[0] for call in provider_calls], [200] * len(provider_clients))
        for connection in uploads:
            connection.close()
        self.wait_until(lambda: not self.service.material_uploads.active_teams)


class GatewayDashboardQueryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='gotc-dashboard-')
        self.addCleanup(directory.cleanup)
        self.service = EventService(data_dir=directory.name, origin='http://127.0.0.1')
        self.addCleanup(self.service.connections.close)

    def add_team(self, db, number):
        team, key, now = f'T{number}', f'K{number}', now_ms()
        db.execute("INSERT INTO teams VALUES (?,?,?,'','',?)", (team, number, team, now))
        db.execute('INSERT INTO team_api VALUES (?,1,?)', (team, now))
        db.execute("INSERT INTO api_keys(id,team_id,name,key_hash,masked,created_at) VALUES (?,?,'k',?,'m',?)",
                   (key, team, 'hash-' + key, now))
        return team, key

    @staticmethod
    def add_usage(db, team, key, at, cost, currency='USD', pending=False):
        db.execute("""INSERT INTO api_usage(key_id,team_id,at,model,input_tokens,output_tokens,cache_write_tokens,
                      cache_read_tokens,cost_micro,status,path,stream,duration_ms,user_agent,currency,billing_status)
                      VALUES (?,?,?,'m',1,1,0,0,?,200,'/v1/chat/completions',0,10,'test',?,?)""",
                   (key, team, at, cost, currency, 'pending' if pending else 'settled'))

    def dashboard(self, db):
        statements = []
        db.set_trace_callback(statements.append)
        try:
            value = self.service.keys.admin(db, None, 'GET', '/api/admin/keys', {}, settings(db))
        finally:
            db.set_trace_callback(None)
        return value, len(statements)

    def test_team_billing_uses_a_fixed_number_of_queries(self):
        with self.service.db() as db:
            now = now_ms()
            first, first_key = self.add_team(db, 1)
            second, second_key = self.add_team(db, 2)
            idle, _ = self.add_team(db, 3)
            self.add_usage(db, first, first_key, now, 1_000_000)
            self.add_usage(db, first, first_key, now - 3 * 86_400_000, 2_000_000)
            self.add_usage(db, second, second_key, now, 3_000_000, 'CNY', pending=True)
            self.add_usage(db, second, second_key, now, 4_000_000, 'USD')
            value, statements = self.dashboard(db)
            teams = {team['teamId']: team for team in value['teams']}
            self.assertEqual((teams[first]['used24h'], teams[first]['usedTotal']), (1, 3))
            self.assertIsNone(teams[second]['usedTotal'])
            self.assertEqual(teams[second]['billing'], [{'currency': 'CNY', 'cost': 3, 'pending': 1},
                                                        {'currency': 'USD', 'cost': 4, 'pending': 0}])
            self.assertEqual((teams[idle]['used24h'], teams[idle]['usedTotal'], teams[idle]['billing']), (0, 0, []))
            for number in range(4, 16):
                self.add_team(db, number)
            self.assertEqual(self.dashboard(db)[1], statements)

    def test_dashboard_reads_use_their_indexes(self):
        with self.service.db() as db:
            usage = ' '.join(row[3] for row in db.execute("""EXPLAIN QUERY PLAN
                SELECT team_id,currency,SUM(cost_micro),SUM(billing_status='pending') FROM api_usage
                WHERE at>=0 GROUP BY team_id,currency ORDER BY team_id,currency"""))
            audit = ' '.join(row[3] for row in db.execute("""EXPLAIN QUERY PLAN
                SELECT at,action,target,metadata FROM audit WHERE action GLOB 'api-*' ORDER BY id DESC LIMIT 50"""))
        self.assertIn('COVERING INDEX api_usage_team_billing', usage)
        self.assertNotIn('TEMP B-TREE', usage)
        self.assertIn('audit_gateway_changes', audit)


if __name__ == '__main__':
    unittest.main()
