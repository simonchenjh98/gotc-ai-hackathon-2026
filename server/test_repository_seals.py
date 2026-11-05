"""Gitee seals through the real HTTP/SQLite service and an isolated Gitee adapter."""
import os
import sqlite3
import threading
import unittest
from contextlib import closing, contextmanager
from unittest.mock import patch
from urllib.parse import unquote

import app
import test_repositories as fixtures
from repositories import UpstreamError
from test_api import Client


class GiteeRemote(fixtures.Remote):
    def __init__(self, service):
        super().__init__(service, 'gitee')
        self.rules, self.branches = {}, {}
        self.fail_all = False
        self.lose = None
        self.corrupt = {}
        self.exact_corrupt = {}
        self.branch_corrupt = {}

    def __call__(self, method, path, body=None):
        if self.fail_all:
            self.calls.append((method, path, body))
            raise UpstreamError()
        if ('/branches/' not in path and not path.endswith('/branches')) or path.endswith('/branches/main'):
            return super().__call__(method, path, body)
        with closing(sqlite3.connect(self.service.db_path, timeout=.1)) as db:
            db.execute('BEGIN IMMEDIATE')
        self.calls.append((method, path, body))
        if method == 'PUT' and ('/setting/' in path or path.endswith('/setting')):
            name = body['wildcard'] if path.endswith('/new') else unquote(path.split('/')[-2])
            if path.endswith('/new') and name in self.rules:
                raise UpstreamError(400)
            if not path.endswith('/new') and name not in self.rules:
                raise UpstreamError(404)
            rid = self.rules.setdefault(name, len(self.rules) + 1)
            answer = dict(id=rid, project_id=self.repo['id'], wildcard=name,
                          pushers=body['pusher'], mergers=body['merger'], mode=body['mode'], escapse_protect_branch_list=[])
            if self.lose == 'rule':
                self.lose = None
                raise UpstreamError()
            return 200, {**answer, **self.corrupt, **(self.exact_corrupt if body['pusher']=='none' else {})}
        if method == 'POST' and path.endswith('/branches'):
            name = body['branch_name']
            if name in self.branches:
                raise UpstreamError(409)
            assert any(n.endswith('-*') and name.startswith(n[:-1]) for n in self.rules)
            self.branches[name] = body['refs']
            if self.lose == 'branch':
                self.lose = None
                raise UpstreamError()
            return 201, {'name': name, 'protected': True, 'commit': {'sha': body['refs']}}
        if method == 'GET':
            name = unquote(path.rsplit('/', 1)[-1])
            if name not in self.branches:
                raise UpstreamError(404)
            return 200, {'name': name, 'protected': True, 'commit': {'sha': self.branches[name]}, **self.branch_corrupt}
        raise AssertionError((method, path, body))


class SealedTests(unittest.TestCase):
    fixture = fixtures.RepositoryTests
    team, invitation, access = fixture.team, fixture.invitation, fixture.access
    open_submissions, materials = fixture.open_submissions, fixture.materials
    request, apply, admin_action = fixture.request, fixture.apply, fixture.admin_action
    preview, formal_submit = fixture.preview, fixture.submit

    def setUp(self):
        env = patch.dict(os.environ, {'GOTC_REPO_GITEE_SEAL_ENABLED': '1'})
        env.start()
        self.addCleanup(env.stop)
        self.fixture.setUp(self)
        self.addCleanup(self.fixture.tearDown, self)
        self.remote = GiteeRemote(self.service)
        provider = self.repos.providers['gitee']
        provider.enabled, provider.request = True, self.remote
        status, row, _ = self.apply('gitee')
        self.assertEqual(status, 200, row)
        status, row, _ = self.admin_action(row['repository'])
        self.assertEqual(status, 200, row)
        self.open_submissions()

    def sealed_preview(self):
        status, preview, _ = self.preview()
        self.assertEqual(status, 200, preview)
        self.assertIn('repositorySeal', preview)
        return preview

    def test_local_confirmation_and_exact_receipt_replay(self):
        preview = self.sealed_preview()
        seal = preview['repositorySeal']
        self.assertEqual(set(seal), {'id', 'ref', 'sha', 'sealedAt', 'viewUrl'})
        self.assertEqual(seal['viewUrl'], preview['codeUrl'] + '/commit/' + preview['sha'])
        self.assertEqual(list(self.remote.branches.values()), [preview['sha']])
        rules = [payload for method,path,payload in self.remote.calls if path.endswith('/branches/setting/new')]
        self.assertEqual(rules, [
            {'wildcard': seal['ref'][:-len(seal['sha'])] + '*', 'pusher':'admin', 'merger':'admin', 'mode':'standard'},
            {'wildcard': seal['ref'], 'pusher':'none', 'merger':'none', 'mode':'standard'},
        ])
        self.remote.sha = 'b' * 40
        self.remote.fail_all = True
        count = len(self.remote.calls)
        status, result, _ = self.formal_submit(preview)
        self.assertEqual(status, 200, result)
        receipt = result['receipt']
        self.assertEqual(receipt['materials']['codeVersion'], preview['sha'])
        stable = {**seal, 'receivedAt': receipt['receivedAt']}
        self.assertEqual(receipt['repositorySeal'], stable)
        self.assertEqual(result['repositorySeal'], stable)
        self.assertEqual(self.client.get('/api/team')[1]['repositorySeal'], stable)
        self.assertEqual(self.request(self.client, '/api/submission/history/1')[1]['repositorySeal'], stable)
        self.open_submissions(submissionsOpen=False)
        replay = self.formal_submit(preview)[1]
        self.assertTrue(replay['replayed'])
        self.assertEqual(replay['receipt'], receipt)
        self.assertEqual(len(self.remote.calls), count)

    def test_same_sha_reuse_and_wrong_head_is_never_overwritten(self):
        seal = self.sealed_preview()['repositorySeal']
        self.assertEqual(self.sealed_preview()['repositorySeal'], seal)
        self.assertEqual(sum(m == 'POST' and p.endswith('/branches') for m,p,_ in self.remote.calls), 1)
        name, = self.remote.branches
        self.remote.branches[name] = 'c' * 40
        self.assertNotEqual(self.preview()[0], 200)
        self.assertEqual(self.remote.branches, {name: 'c' * 40})

    def test_lost_rule_and_branch_responses_resume_the_same_names(self):
        self.remote.lose = 'rule'
        self.assertNotEqual(self.preview()[0], 200)
        self.remote.lose = 'branch'
        self.assertNotEqual(self.preview()[0], 200)
        before = dict(self.remote.branches)
        self.assertEqual(len(before), 1)
        self.sealed_preview()
        self.assertEqual(self.remote.branches, before)
        self.assertEqual(len(self.remote.rules), 2)

    def test_mismatched_rule_responses_never_create_branch_or_preview(self):
        for field, bad in [('id', True), ('project_id', 9), ('wildcard', '*'), ('pushers', 'everyone'),
                           ('mergers', 'everyone'), ('mode', 'relaxed'), ('escapse_protect_branch_list', ['exception'])]:
            with self.subTest(field=field):
                self.remote.corrupt = {field: bad}
                self.assertNotEqual(self.preview()[0], 200)
                self.assertEqual(self.remote.branches, {})
        with self.service.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM repository_previews').fetchone()[0], 0)

    def test_exact_rule_and_independent_branch_readback(self):
        self.remote.exact_corrupt = {'pushers':'admin'}
        self.assertNotEqual(self.preview()[0], 200)
        self.assertEqual(len(self.remote.branches), 1)
        self.remote.exact_corrupt = {}
        self.remote.branch_corrupt = {'protected':False}
        self.assertNotEqual(self.preview()[0], 200)
        self.remote.branch_corrupt = {'name':'unexpected'}
        self.assertNotEqual(self.preview()[0], 200)
        with self.service.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM repository_previews').fetchone()[0], 0)

    def test_other_team_cannot_use_seal_or_override_materials(self):
        preview = self.sealed_preview()
        other, _ = self.access(self.team(number=2))
        status, data, _ = self.request(other, '/api/team/repository/apply', {'provider':'gitee','usernames':['SyntheticOther']})
        self.assertEqual(status, 200, data)
        self.repos.providers['gitee'].request = GiteeRemote(self.service)
        self.assertEqual(self.admin_action(data['repository'])[0], 200)
        self.assertEqual(self.formal_submit(preview, client=other)[0], 409)
        self.assertEqual(self.formal_submit(preview, materials=self.materials(codeVersion='c'*40))[0], 400)
        self.assertIsNone(self.client.get('/api/team')[1]['repositorySeal'])
        self.assertIsNone(other.get('/api/team')[1]['repositorySeal'])

    def test_expiry_session_binding_and_closed_window(self):
        preview = self.sealed_preview()
        fresh = Client(self.server)
        self.assertEqual(fresh.post('/api/login', {'role':'team', 'code':self.access_info['code']})[0], 200)
        self.assertEqual(self.formal_submit(preview, client=fresh)[0], 409)
        with patch('app.now_ms', return_value=preview['expiresAt']):
            self.assertEqual(self.formal_submit(preview, request_id='expired-request-0001')[0], 409)
        with self.service.db() as db:
            db.execute('UPDATE repository_bindings SET revision=revision+1 WHERE team_id=?', (self.team_id,))
        self.assertEqual(self.formal_submit(preview, request_id='changed-request-0001')[0], 409)
        preview = self.sealed_preview()
        self.open_submissions(submissionsOpen=False)
        self.assertEqual(self.formal_submit(preview, request_id='closed-request-00001')[0], 409)

    def test_rejects_client_received_at_even_without_valid_preview(self):
        self.assertEqual(self.formal_submit(receivedAt=1)[0], 400)

    def test_queued_request_still_respects_manual_closure(self):
        self.confirm_while_queued(manual_close=True)

    def test_received_before_deadline_while_waiting_for_writer(self):
        self.confirm_while_queued(manual_close=False)

    def confirm_while_queued(self, manual_close):
        preview = self.sealed_preview()
        arrival = app.now_ms()
        deadline = arrival + 1
        self.open_submissions(submissionDeadline=deadline)
        original_db = self.service.db
        queued = threading.Event()
        @contextmanager
        def traced_db():
            with original_db() as db:
                db.set_trace_callback(lambda sql: queued.set() if sql == 'BEGIN IMMEDIATE' else None)
                yield db
        with closing(sqlite3.connect(self.service.db_path)) as lock, patch('app.now_ms', return_value=arrival) as clock:
            lock.execute('BEGIN IMMEDIATE')
            results = []
            with patch.object(self.service, 'db', traced_db):
                thread = threading.Thread(target=lambda: results.append(self.formal_submit(preview)))
                thread.start()
                try:
                    self.assertTrue(queued.wait(3))
                finally:
                    clock.return_value = preview['expiresAt'] + 1
                    if manual_close:
                        lock.execute("UPDATE settings SET value='false' WHERE key='submissionsOpen'")
                    lock.commit()
                    thread.join(6)
            status, result, _ = results[0]
        if manual_close:
            self.assertEqual(status, 409, result)
            self.assertEqual(result['code'], 'submission_closed')
            self.assertIsNone(self.client.get('/api/team')[1]['submission']['submitted'])
            return
        self.assertEqual(status, 200, result)
        self.assertEqual(result['receipt']['receivedAt'], arrival)
        self.assertEqual(result['receipt']['savedAt'], preview['expiresAt'] + 1)
        self.assertEqual(result['submission']['submittedAt'], result['receipt']['savedAt'])
