"""Offline repository workflow through real local HTTP and immutable submission storage."""
import json
import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
import unittest
from contextlib import closing, contextmanager
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from errors import APIError
from repositories import Provider, ProviderOperations, UpstreamError
import test_submissions


class Remote:
    def __init__(self, service, provider='github'):
        self.service, self.provider = service, provider
        self.repo = None
        self.calls = []
        self.sha = 'a' * 40
        self.accepted = False
        self.timeout_create = False
        self.timeout_invite = False
        self.timeout_read = False
        self.gitee_member_response = None
        self.gitee_permission = 'push'
        self.namespace_type = None

    def __call__(self, method, path, body=None):
        # Every mocked upstream call also proves the writer lock is free.
        with closing(sqlite3.connect(self.service.db_path, timeout=.1)) as db:
            db.execute('BEGIN IMMEDIATE')
        self.calls.append((method, path, body))
        if self.timeout_read and method == 'GET':
            raise UpstreamError()
        if self.provider=='gitee' and method=='GET' and '/members/' in path:
            return 200,{'user':{'id':456,'login':path.rsplit('/',1)[-1]},'role':'member','active':True,
                        'enterprise':{'path':path.split('/')[2]}}
        if method == 'POST' and path.startswith(('/orgs/', '/enterprises/')):
            org = path.split('/')[2]
            self.repo = {'id': 123, 'full_name': org + '/' + body['name'], 'description': body['description'], 'default_branch': 'main',
                         'namespace': {'type': self.namespace_type or ('enterprise' if path.startswith('/enterprises/') else 'group')}}
            if self.timeout_create:
                raise UpstreamError()
            return 201, self.repo
        if '/collaborators/' in path:
            if self.timeout_invite:
                raise UpstreamError()
            if path.endswith('/permission'):
                return 200, {'id': 456, 'login': path.split('/')[-2], 'permission': self.gitee_permission}
            if self.provider == 'gitee':
                return self.gitee_member_response or (200, {'id': 456, 'login': path.split('/')[-1], 'permissions': 'push'})
            return (204 if self.accepted else 201), {}
        if path.endswith('/protection'):
            return 200, {}
        if '/branches/' in path:
            return 200, {'commit': {'sha': self.sha}}
        if method == 'GET' and path.startswith('/repos/'):
            if self.repo is None:
                raise UpstreamError(404)
            return 200, self.repo
        raise AssertionError((method, path, body))


class RepositoryTests(unittest.TestCase):
    team = test_submissions.SubmissionAPITests.team
    invitation = test_submissions.SubmissionAPITests.invitation
    access = test_submissions.SubmissionAPITests.access
    open_submissions = test_submissions.SubmissionAPITests.open_submissions
    materials = test_submissions.SubmissionAPITests.materials

    def setUp(self):
        self.environment = patch.dict(os.environ, {'GOTC_REPO_GITHUB_TOKEN': '', 'GOTC_REPO_GITEE_TOKEN': '', 'GOTC_REPO_GITEE_ENABLED': ''})
        self.environment.start()
        test_submissions.SubmissionAPITests.setUp(self)
        self.repos = self.service.repositories
        self.remote = Remote(self.service)
        self.repos.providers['github'].request = self.remote
        self.client, self.access_info = self.access(self.team())
        self.team_id = self.client.get('/api/team')[1]['team']['id']

    def tearDown(self):
        test_submissions.SubmissionAPITests.tearDown(self)
        self.environment.stop()

    def request(self, client, path, body=None, login=True):
        headers = {'X-GOTC-Login': client.get('/api/session')[1]['loginId']} if login else {}
        return client.request('GET' if body is None else 'POST', path, body, headers=headers)

    def enable(self, provider='github'):
        self.repos.providers[provider].enabled = True

    def apply(self, provider='github', usernames=None):
        return self.request(self.client, '/api/team/repository/apply', {'provider': provider, 'usernames': usernames or ['Synthetic-Developer']})

    def admin_action(self, row, action='approve', **changes):
        return self.request(self.admin, '/api/admin/repositories/' + action, {'teamId': row['teamId'], 'expectedRevision': row['revision'], 'usernamesVerified': True, **changes})

    def provision(self):
        self.enable()
        status, data, _ = self.apply()
        self.assertEqual(status, 200, data)
        status, data, _ = self.admin_action(data['repository'])
        self.assertEqual(status, 200, data)
        self.assertEqual(data['repository']['state'], 'invited')
        return data['repository']

    def preview(self, client=None):
        return self.request(client or self.client, '/api/team/repository/preview', {})

    def submit(self, preview=None, client=None, request_id='repo-request-0000001', version=0, **changes):
        body = {'requestId': request_id, 'expectedVersion': version, 'status': 'submitted',
                'materials': self.materials(codeUrl='', codeVersion=''), **changes}
        if preview:
            body['repoPreviewId'] = preview['previewId']
        return self.request(client or self.client, '/api/submission', body)

    def test_disabled_and_explicit_gitee_enablement(self):
        status, data, _ = self.request(self.client, '/api/team/repository')
        self.assertEqual(status, 200)
        self.assertTrue(all(not p['enabled'] for p in data['providers']))
        self.assertEqual(self.apply()[0], 503)
        self.assertEqual(self.apply('gitee')[0], 503)
        with patch.dict(os.environ, {'GOTC_REPO_GITEE_TOKEN': 'synthetic-never-live'}):
            from repositories import Repositories
            self.assertFalse(Repositories(self.service).providers['gitee'].enabled)
            with patch.dict(os.environ, {'GOTC_REPO_GITEE_ENABLED': '1'}):
                self.assertTrue(Repositories(self.service).providers['gitee'].enabled)
        self.assertEqual(self.remote.calls, [])

    def test_strict_usernames_generated_slug_and_one_application(self):
        self.enable()
        for names in [['https://github.com/u'], ['a/b'], ['../x'], ['a@b'], ['-user'], ['a--b'], ['user', 'USER'], [' x '], ['x' * 40]]:
            self.assertEqual(self.apply(usernames=names)[0], 400, names)
        status, data, _ = self.apply(usernames=['Valid-Name', 'a'])
        self.assertEqual(status, 200, data)
        self.assertRegex(data['repository']['slug'], r'^gotc-2026-team-[0-9]+-[a-f0-9]{12}$')
        self.assertEqual(self.apply()[0], 409)
        self.assertEqual(self.remote.calls, [])

    def test_team_only_application_admin_only_approval_and_login_marker(self):
        self.enable()
        self.assertEqual(self.request(self.admin, '/api/team/repository/apply', {'provider': 'github', 'usernames': ['u']})[0], 403)
        self.assertEqual(self.request(self.client, '/api/team/repository', login=False)[0], 428)
        row = self.apply()[1]['repository']
        self.assertEqual(self.request(self.client, '/api/admin/repositories/approve', {'teamId': self.team_id, 'expectedRevision': row['revision'], 'usernamesVerified': True})[0], 403)
        self.assertEqual(self.admin_action(row, usernamesVerified=False)[0], 400)
        self.assertEqual(self.admin_action(row, expectedRevision=0)[0], 409)
        self.assertEqual(self.remote.calls, [])

    def test_pending_invitation_push_only_branch_protection_and_known_identity_retry(self):
        row = self.provision()
        self.assertEqual(row['externalId'], '123')
        self.assertEqual(row['protection'], 'enabled')
        invites = [call for call in self.remote.calls if '/collaborators/' in call[1]]
        self.assertEqual(invites[0][2], {'permission': 'push'})
        protection = next(call[2] for call in self.remote.calls if call[1].endswith('/protection'))
        self.assertIs(protection['allow_force_pushes'], False)
        self.assertIs(protection['allow_deletions'], False)
        self.assertEqual(self.admin_action(row)[0], 409)
        self.remote.accepted = True
        result = self.admin_action(row, 'reconcile')
        self.assertEqual(result[1]['repository']['state'], 'ready', result)
        self.assertEqual(len([c for c in self.remote.calls if c[0] == 'POST']), 1)

    def test_lost_create_response_recovers_only_matching_operation_marker(self):
        self.enable()
        row = self.apply()[1]['repository']
        self.remote.timeout_create = True
        result = self.admin_action(row)[1]['repository']
        self.assertEqual(result['state'], 'reconcile')
        self.assertIsNone(result['externalId'])
        self.assertEqual(self.admin_action(result)[0], 409)
        recovered = self.admin_action(result, 'reconcile')[1]['repository']
        self.assertEqual(recovered['externalId'], '123')
        self.assertEqual(recovered['state'], 'invited')
        self.assertEqual(len([c for c in self.remote.calls if c[0] == 'POST']), 1)

    def test_existing_collision_and_remote_identity_replacement_are_rejected(self):
        self.enable()
        row = self.apply()[1]['repository']
        self.remote.repo = {'id': 456, 'full_name': row['organization'] + '/' + row['slug'], 'description': 'unrelated'}
        result = self.admin_action(row)[1]['repository']
        self.assertEqual(result['state'], 'reconcile')
        result = self.admin_action(result, 'reconcile')[1]['repository']
        self.assertIsNone(result['externalId'])
        self.assertFalse(any('/collaborators/' in c[1] or c[0] == 'POST' for c in self.remote.calls))

    def test_invitation_timeout_preserves_identity_and_busy_recovery_is_guarded(self):
        self.enable()
        row = self.apply()[1]['repository']
        self.remote.timeout_invite = True
        result = self.admin_action(row)[1]['repository']
        self.assertEqual(result['state'], 'reconcile')
        self.assertEqual(result['externalId'], '123')
        self.remote.timeout_invite = False
        recovered = self.admin_action(result, 'reconcile')[1]['repository']
        self.assertEqual(recovered['state'], 'invited')
        with self.service.db() as db:
            db.execute("UPDATE repository_bindings SET state='provisioning' WHERE team_id=?", (self.team_id,))
        self.assertEqual(self.admin_action(recovered, 'reconcile')[0], 409)

    def test_preview_injects_full_sha_into_atomic_receipt_and_history(self):
        row = self.provision()
        self.open_submissions()
        status, preview, _ = self.preview()
        self.assertEqual(status, 200, preview)
        self.assertEqual(preview['sha'], 'a' * 40)
        self.assertFalse(preview['archived'])
        status, result, _ = self.submit(preview)
        self.assertEqual(status, 200, result)
        for materials in [result['submission']['submitted'], result['receipt']['materials']]:
            self.assertEqual(materials['codeUrl'], row['codeUrl'])
            self.assertEqual(materials['codeVersion'], preview['sha'])
        history = self.request(self.client, '/api/submission/history/1')[1]
        self.assertEqual(history['materials']['codeVersion'], preview['sha'])
        self.assertEqual(self.request(self.client, '/api/submission/requests/repo-request-0000001')[1]['status'], 'succeeded')

    def test_gitee_complete_application_approval_sha_receipt_and_history(self):
        self.enable('gitee')
        self.remote.provider = 'gitee'
        self.repos.providers['gitee'].request = self.remote
        self.remote.accepted = True
        status, application, _ = self.apply('gitee', ['Gitee_Developer'])
        self.assertEqual(status, 200, application)
        status, approved, _ = self.admin_action(application['repository'])
        self.assertEqual(status, 200, approved)
        row = approved['repository']
        self.assertEqual(row['state'], 'ready')
        self.assertEqual(row['protection'], 'unverified')
        self.assertTrue(row['codeUrl'].startswith('https://gitee.com/gotc-ai-hackathon/'))
        self.open_submissions()
        status, preview, _ = self.preview()
        self.assertEqual(status, 200, preview)
        status, result, _ = self.submit(preview)
        self.assertEqual(status, 200, result)
        self.assertEqual(result['receipt']['materials']['codeUrl'], row['codeUrl'])
        self.assertEqual(result['receipt']['materials']['codeVersion'], self.remote.sha)
        self.assertEqual(self.request(self.client, '/api/submission/history/1')[1]['materials']['codeVersion'], self.remote.sha)
        self.assertEqual(next(c[2] for c in self.remote.calls if '/collaborators/' in c[1]), {'permission': 'push'})
        self.assertTrue(any(c[0] == 'GET' and c[1].endswith('/collaborators/Gitee_Developer/permission') for c in self.remote.calls))
        create = next(c for c in self.remote.calls if c[0] == 'POST')
        self.assertEqual(create[1], '/enterprises/gotc-ai-hackathon/repos')
        self.assertIs(type(create[2]['private']), int)
        self.assertEqual(create[2]['private'], 1)
        self.assertIs(create[2]['outsourced'], False)
        self.assertFalse(any(c[1].startswith('/orgs/') for c in self.remote.calls))

    def test_gitee_nonenterprise_namespace_never_binds_or_grants(self):
        self.enable('gitee')
        self.remote.provider = 'gitee'
        self.remote.namespace_type = 'group'
        self.repos.providers['gitee'].request = self.remote
        application = self.apply('gitee', ['Gitee_Developer'])[1]['repository']
        status, result, _ = self.admin_action(application)
        self.assertEqual(status, 200, result)
        row = result['repository']
        self.assertEqual(row['state'], 'reconcile')
        self.assertIsNone(row['externalId'])
        self.assertFalse(any('/collaborators/' in c[1] or '/members' in c[1] for c in self.remote.calls))
        row = self.admin_action(row, 'reconcile')[1]['repository']
        self.assertEqual(row['state'], 'reconcile')
        self.assertEqual(len([c for c in self.remote.calls if c[0] == 'POST']), 1)

    def test_gitee_ambiguous_enterprise_create_recovers_without_org_fallback(self):
        self.enable('gitee')
        self.remote.provider = 'gitee'
        self.remote.timeout_create = True
        self.repos.providers['gitee'].request = self.remote
        application = self.apply('gitee', ['Gitee_Developer'])[1]['repository']
        row = self.admin_action(application)[1]['repository']
        self.assertEqual(row['state'], 'reconcile')
        self.assertIsNone(row['externalId'])
        self.remote.timeout_create = False
        row = self.admin_action(row, 'reconcile')[1]['repository']
        self.assertEqual(row['state'], 'ready')
        self.assertEqual(row['externalId'], '123')
        self.assertEqual([c[1] for c in self.remote.calls if c[0] == 'POST'], ['/enterprises/gotc-ai-hackathon/repos'])

    def test_gitee_inconclusive_grant_remains_reconcilable_until_readback_confirms_push(self):
        self.enable('gitee')
        self.remote.provider = 'gitee'
        self.repos.providers['gitee'].request = self.remote
        self.remote.gitee_member_response = (204, {})
        application = self.apply('gitee', ['Gitee_Developer'])[1]['repository']
        status, result, _ = self.admin_action(application)
        self.assertEqual(status, 200, result)
        row = result['repository']
        self.assertEqual(row['state'], 'reconcile')
        self.assertEqual(row['externalId'], '123')
        self.assertNotEqual(row['message'], '仓库写权限已确认')
        self.open_submissions()
        self.assertEqual(self.preview()[0], 409)
        self.remote.gitee_member_response = None
        self.remote.gitee_permission = 'pull'
        row = self.admin_action(row, 'reconcile')[1]['repository']
        self.assertEqual(row['state'], 'reconcile')
        self.remote.gitee_permission = 'push'
        row = self.admin_action(row, 'reconcile')[1]['repository']
        self.assertEqual(row['state'], 'ready')
        self.assertEqual(len([c for c in self.remote.calls if c[0] == 'POST' and c[1].startswith('/enterprises/')]), 1)

    def test_gitee_effective_admin_stays_reconcilable_for_organizer_review(self):
        self.enable('gitee')
        self.remote.provider = 'gitee'
        self.remote.gitee_permission = 'admin'
        self.repos.providers['gitee'].request = self.remote
        application = self.apply('gitee', ['Gitee_Developer'])[1]['repository']
        status, result, _ = self.admin_action(application)
        self.assertEqual(status, 200, result)
        row = result['repository']
        self.assertEqual(row['state'], 'reconcile')
        self.assertIn('仓库管理权限', row['message'])
        self.assertIn('主办方人工核对', row['message'])
        self.assertIn('不会自动修改', row['message'])
        self.assertEqual(row['externalId'], '123')
        self.open_submissions()
        self.assertEqual(self.preview()[0], 409)
        grants = [c for c in self.remote.calls if '/collaborators/' in c[1] and c[0] == 'PUT']
        self.assertEqual(len(grants), 1)
        self.assertEqual(grants[0][2], {'permission': 'push'})
        self.assertFalse(any(c[0] in {'DELETE','PATCH'} or c[0] in {'POST','PUT'} and '/members' in c[1] for c in self.remote.calls))
        # The organizer resolves inherited privileges outside the service; only then can recovery finish.
        self.remote.gitee_permission = 'push'
        self.assertEqual(self.admin_action(row, 'reconcile')[1]['repository']['state'], 'ready')

    def test_missing_preview_and_manual_code_override_are_denied(self):
        self.provision()
        self.open_submissions()
        self.assertEqual(self.submit()[0], 400)
        preview = self.preview()[1]
        status, result, _ = self.submit(preview, materials=self.materials(codeUrl='https://github.com/other/team'))
        self.assertEqual(status, 400, result)
        self.assertEqual(result['code'], 'repository_override')
        self.assertIsNone(self.client.get('/api/team')[1]['submission']['submitted'])
        self.assertEqual(self.request(self.client, '/api/submission/requests/repo-request-0000001')[1]['status'], 'rejected')

    def test_preview_is_scoped_to_team_session_revision_and_expiry(self):
        row = self.provision()
        self.open_submissions()
        other, _ = self.access(self.team(number=2))
        self.enable()
        other_apply = self.request(other, '/api/team/repository/apply', {'provider': 'github', 'usernames': ['OtherDeveloper']})[1]['repository']
        # Other team has its own external identity; no arbitrary team selector is accepted.
        with self.service.db() as db:
            db.execute("UPDATE repository_bindings SET external_id='987',state='ready' WHERE team_id=?", (other_apply['teamId'],))
        preview = self.preview()[1]
        self.assertEqual(self.submit(preview, client=other)[0], 409)
        from test_api import Client
        fresh = Client(self.server)
        self.assertEqual(fresh.post('/api/login', {'role': 'team', 'code': self.access_info['code']})[0], 200)
        self.assertEqual(self.submit(preview, client=fresh)[0], 409)
        with self.service.db() as db:
            db.execute('UPDATE repository_bindings SET revision=revision+1 WHERE team_id=?', (self.team_id,))
        self.assertEqual(self.submit(preview)[0], 409)
        preview = self.preview()[1]
        with patch.object(self.repos, 'now', return_value=preview['expiresAt']):
            self.assertEqual(self.submit(preview)[0], 409)

    def test_changed_head_is_rejected_until_new_preview(self):
        self.provision()
        self.open_submissions()
        preview = self.preview()[1]
        self.remote.sha = 'b' * 40
        status, result, _ = self.submit(preview)
        self.assertEqual(status, 409)
        self.assertEqual(result['code'], 'repository_head_changed')
        fresh = self.preview()[1]
        self.assertEqual(self.submit(fresh)[0], 200)

    def test_successful_request_replays_without_network_after_expiry_or_closure(self):
        self.provision()
        self.open_submissions()
        preview = self.preview()[1]
        status, original, _ = self.submit(preview)
        self.assertEqual(status, 200)
        self.open_submissions(submissionsOpen=False)
        self.remote.timeout_read = True
        calls = len(self.remote.calls)
        with patch.object(self.repos, 'now', return_value=preview['expiresAt'] + 1):
            status, replay, _ = self.submit(preview)
        self.assertEqual(status, 200, replay)
        self.assertTrue(replay['replayed'])
        self.assertEqual(replay['receipt'], original['receipt'])
        self.assertEqual(len(self.remote.calls), calls)
        self.assertEqual(self.submit(preview, materials=self.materials())[0], 409)

    def test_success_committed_during_failed_upstream_check_is_replayed(self):
        self.provision()
        self.open_submissions()
        preview = self.preview()[1]
        original = self.repos.preflight
        def raced(body, cookie, login_id):
            # Model another HTTP worker committing the same request while this one's I/O is in flight.
            with patch.object(self.repos, 'preflight', side_effect=original):
                self.service.dispatch_submission(body, cookie, login_id)
            raise UpstreamError()
        with patch.object(self.repos, 'preflight', side_effect=raced):
            status, result, _ = self.submit(preview)
        self.assertEqual(status, 200, result)
        self.assertTrue(result['replayed'])
        self.assertEqual(result['receipt']['historyVersion'], 1)

    def test_deadline_and_closed_window_prevent_preview_and_confirmation(self):
        self.provision()
        self.open_submissions()
        preview = self.preview()[1]
        self.open_submissions(submissionDeadline=1)
        calls = len(self.remote.calls)
        self.assertEqual(self.preview()[0], 409)
        self.assertEqual(self.submit(preview)[0], 409)
        self.assertEqual(len(self.remote.calls), calls)

    def test_upstream_timeout_is_redacted_and_cannot_create_submission(self):
        self.provision()
        self.open_submissions()
        preview = self.preview()[1]
        self.remote.timeout_read = True
        status, result, _ = self.submit(preview)
        self.assertEqual(status, 502)
        self.assertIsNone(self.client.get('/api/team')[1]['submission']['submitted'])
        self.assertNotIn('synthetic-secret', json.dumps(result))

    def test_preview_quota_is_rolling_and_independent_of_confirmation_and_other_teams(self):
        self.provision()
        self.open_submissions()
        clock = [100.0]
        self.repos.operations.clock = lambda: clock[0]
        for _ in range(10):
            status, preview, _ = self.preview()
            self.assertEqual(status, 200, preview)
        calls = len(self.remote.calls)
        status, error, _ = self.preview()
        self.assertEqual((status, error['code']), (429, 'repository_rate_limit'))
        self.assertIn('一分钟', error['error'])
        self.assertEqual(len(self.remote.calls), calls)
        self.assertEqual(self.submit(preview)[0], 200)
        # A second authenticated team on the same provider retains its own quota.
        other, _ = self.access(self.team(number=2, name='Other synthetic quota team'))
        status, data, _ = self.request(other, '/api/team/repository/apply', {'provider': 'github', 'usernames': ['Other-Developer']})
        self.assertEqual(status, 200, data)
        saved_repo = dict(self.remote.repo)
        self.remote.repo = None
        self.assertEqual(self.admin_action(data['repository'])[0], 200)
        self.assertEqual(self.preview(other)[0], 200)
        self.remote.repo = saved_repo
        clock[0] = 159.999
        self.assertEqual(self.preview()[0], 429)
        clock[0] = 160.0
        self.assertEqual(self.preview()[0], 200)

    def test_confirmation_quota_and_replay_skip_quota_busy_provider_and_expired_preview(self):
        self.provision()
        self.open_submissions()
        clock = [100.0]
        self.repos.operations.clock = lambda: clock[0]
        preview = self.preview()[1]
        status, original, _ = self.submit(preview)
        self.assertEqual(status, 200, original)
        # An upstream error still spends one fresh confirmation verification.
        self.remote.timeout_read = True
        for i in range(19):
            self.assertEqual(self.submit(preview, request_id='quota-new-request-%03d' % i, version=1)[0], 502)
        calls = len(self.remote.calls)
        self.assertEqual(self.submit(preview, request_id='quota-over-limit-001', version=1)[0], 429)
        self.assertEqual(len(self.remote.calls), calls)
        permit = self.repos.operations.permits['github']
        for _ in range(4):
            self.assertTrue(permit.acquire(blocking=False))
        try:
            with patch.object(self.repos, 'now', return_value=preview['expiresAt'] + 1):
                status, replay, _ = self.submit(preview)
                self.assertEqual(status, 200, replay)
                self.assertEqual(replay['receipt'], original['receipt'])
                self.assertTrue(replay['replayed'])
                self.assertEqual(self.submit(preview, materials=self.materials())[0], 409)
        finally:
            for _ in range(4):
                permit.release()
        self.assertEqual(len(self.remote.calls), calls)
        clock[0] = 160.0
        self.remote.timeout_read = False
        self.assertEqual(self.submit(preview, request_id='quota-reset-request-001', version=1)[0], 200)

    def test_busy_operation_has_no_remote_or_persistent_provision_preview_or_confirmation_effect(self):
        self.enable()
        row = self.apply()[1]['repository']
        permit = self.repos.operations.permits['github']
        @contextmanager
        def saturated():
            for _ in range(4):
                self.assertTrue(permit.acquire(blocking=False))
            try:
                yield
            finally:
                for _ in range(4):
                    permit.release()
        with saturated():
            status, error, _ = self.admin_action(row)
            self.assertEqual((status, error['code']), (503, 'repository_busy'))
            self.assertEqual(self.request(self.client, '/api/team/repository')[1]['repository'], row)
            self.assertEqual(self.remote.calls, [])
        row = self.admin_action(row)[1]['repository']
        self.open_submissions()
        preview = self.preview()[1]
        calls, buckets = len(self.remote.calls), dict(self.repos.operations.buckets)
        with saturated():
            self.assertEqual(self.preview()[0], 503)
            self.assertEqual(self.submit(preview)[0], 503)
            self.assertEqual(self.admin_action(row, 'reconcile')[0], 503)
        self.assertEqual(len(self.remote.calls), calls)
        self.assertEqual(self.repos.operations.buckets, buckets)
        with self.service.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM repository_previews').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM submission_requests').fetchone()[0], 0)
        self.assertEqual(self.submit(preview)[0], 200)

    def test_permit_acquisition_and_remote_operations_never_hold_writer_lock_and_release_on_error(self):
        original = self.repos.operations.operation
        @contextmanager
        def checked(*args, **kwargs):
            with closing(sqlite3.connect(self.service.db_path, timeout=.1)) as db:
                db.execute('BEGIN IMMEDIATE')
            with original(*args, **kwargs):
                yield
        with patch.object(self.repos.operations, 'operation', side_effect=checked):
            row = self.provision()
            self.open_submissions()
            preview = self.preview()[1]
            self.remote.timeout_read = True
            for i in range(5):
                self.assertEqual(self.preview()[0], 502)
                self.assertEqual(self.submit(preview, request_id='error-release-request-%03d' % i)[0], 502)
            # Failed provisioning/reconciliation releases the same shared provider permit.
            for _ in range(5):
                row = self.admin_action(row, 'reconcile')[1]['repository']
                self.assertEqual(row['state'], 'reconcile')
            self.remote.timeout_read = False
            self.assertEqual(self.admin_action(row, 'reconcile')[1]['repository']['state'], 'invited')
        permit = self.repos.operations.permits['github']
        for _ in range(4):
            self.assertTrue(permit.acquire(blocking=False))
        self.assertFalse(permit.acquire(blocking=False))
        for _ in range(4):
            permit.release()

    def test_legacy_unbound_and_bound_drafts_remain_compatible(self):
        self.open_submissions()
        result = self.submit(materials=self.materials())
        self.assertEqual(result[0], 200, result)
        self.provision()
        status, result, _ = self.request(self.client, '/api/submission', {'expectedVersion': 1, 'status': 'draft', 'materials': {'codeUrl': '', 'codeVersion': ''}})
        self.assertEqual(status, 200, result)
        self.assertEqual(result['submission']['submitted']['codeUrl'], 'https://github.com/synthetic/repository')


class ProviderOperationTests(unittest.TestCase):
    def test_four_concurrent_operations_and_provider_isolation_release_without_sleep(self):
        operations = ProviderOperations()
        barrier = threading.Barrier(5)
        release = threading.Event()
        def worker():
            with operations.operation('github'):
                barrier.wait(timeout=5)
                if not release.wait(timeout=5):
                    raise AssertionError('release signal missing')
        with ThreadPoolExecutor(max_workers=4) as pool:
            pending = [pool.submit(worker) for _ in range(4)]
            try:
                barrier.wait(timeout=5)
                with self.assertRaises(APIError) as busy:
                    with operations.operation('github'):
                        self.fail('fifth provider operation admitted')
                self.assertEqual(busy.exception.status, 503)
                with operations.operation('gitee'):
                    pass
            finally:
                release.set()
            for future in pending:
                future.result(timeout=5)
        with self.assertRaisesRegex(RuntimeError, 'synthetic'):
            with operations.operation('github'):
                raise RuntimeError('synthetic')
        with operations.operation('github'):
            pass

    def test_quotas_are_thread_safe_bounded_and_keep_active_team_buckets(self):
        clock = [100.0]
        operations = ProviderOperations(clock=lambda: clock[0])
        def spend(_):
            try:
                with operations.operation('github', 'same-team', 'preview'):
                    pass
                return 200
            except APIError as error:
                return error.status
        # Exercise the quota lock directly: permit saturation should not obscure its limit.
        def direct_spend(_):
            try:
                operations.spend('github', 'same-team', 'preview')
                return 200
            except APIError as error:
                return error.status
        with ThreadPoolExecutor(max_workers=20) as pool:
            statuses = list(pool.map(direct_spend, range(20)))
        self.assertEqual(statuses.count(200), 10)
        self.assertEqual(statuses.count(429), 10)
        operations.spend('gitee', 'same-team', 'preview')
        operations.spend('github', 'same-team', 'confirmation')
        operations.MAX_BUCKETS = 3
        with self.assertRaises(APIError) as full:
            operations.spend('github', 'new-team', 'preview')
        self.assertEqual(full.exception.status, 503)
        self.assertEqual(len(operations.buckets), 3)
        self.assertEqual(spend(None), 429)
        clock[0] = 160.0
        operations.spend('github', 'new-team', 'preview')
        self.assertEqual(len(operations.buckets), 1)


class ProviderTransportTests(unittest.TestCase):
    def test_gitee_enterprise_create_uses_integer_external_open_and_never_falls_back(self):
        provider = Provider('gitee', 'gotc-ai-hackathon', 'never-used', 'main', True)
        row = {'organization': 'gotc-ai-hackathon', 'slug': 'generated-team-repo', 'operation_id': 'operation-marker'}
        data = {'id': 123, 'namespace': {'type': 'enterprise'}}
        with patch.object(provider, 'request', return_value=(201, data)) as request:
            self.assertEqual(provider.create(row), data)
            method, path, body = request.call_args.args
            self.assertEqual((method, path), ('POST', '/enterprises/gotc-ai-hackathon/repos'))
            self.assertIs(type(body['private']), int)
            self.assertEqual(body['private'], 1)
            self.assertEqual(body['path'], row['slug'])
            self.assertIs(body['outsourced'], False)
        for response in [(202, data), (200, data)]:
            with self.subTest(response=response), patch.object(provider, 'request', return_value=response) as request:
                with self.assertRaises(UpstreamError):
                    provider.create(row)
                self.assertEqual(request.call_count, 1)

    def test_fixed_hosts_push_permission_no_redirect_and_redacted_errors(self):
        for name, origin in [('github', 'https://api.github.com/'), ('gitee', 'https://gitee.com/api/v5/')]:
            provider = Provider(name, 'org', 'synthetic-secret', 'main', True)
            class Opener:
                def open(self, request, timeout):
                    self.request, self.timeout = request, timeout
                    raise URLError('synthetic-secret echoed upstream')
            opener = Opener()
            with patch('repositories.build_opener', return_value=opener):
                with self.assertRaises(UpstreamError) as error:
                    provider.request('GET', '/repos/org/repo')
            self.assertTrue(opener.request.full_url.startswith(origin))
            self.assertEqual(opener.timeout, 8)
            self.assertNotIn('synthetic-secret', error.exception.message)
            with self.assertRaises(ValueError):
                provider.request('GET', '//attacker.test/token')

    def test_gitee_collaborator_write_permission_uses_push_not_admin(self):
        provider = Provider('gitee', 'org', 'never-used', 'main', True)
        responses = [(200, {'id': 123, 'login': 'developer', 'permissions': 'push'}),
                     (200, {'id': 123, 'login': 'developer', 'permission': 'push'})]
        with patch.object(provider, 'request', side_effect=responses) as request:
            self.assertEqual(provider.invite({'organization': 'org', 'slug': 'repo'}, 'developer'), 'ready')
            self.assertEqual(request.call_args_list[0].args, ('PUT', '/repos/org/repo/collaborators/developer', {'permission': 'push'}))
            self.assertEqual(request.call_args_list[1].args, ('GET', '/repos/org/repo/collaborators/developer/permission'))

    def test_gitee_grant_rejects_empty_pending_mismatched_and_read_only_responses(self):
        provider = Provider('gitee', 'org', 'never-used', 'main', True)
        row = {'organization': 'org', 'slug': 'repo'}
        valid = {'id': 123, 'login': 'developer', 'permissions': 'push'}
        for response in [(202, valid), (204, {}), (200, {}), (200, {'id': 123, 'login': 'other'})]:
            with self.subTest(member=response), patch.object(provider, 'request', return_value=response) as request:
                with self.assertRaises(UpstreamError):
                    provider.invite(row, 'developer')
                self.assertEqual(request.call_count, 1)
        for permission in [{}, {'id': 123, 'login': 'developer', 'permission': 'pull'},
                           {'id': 456, 'login': 'developer', 'permission': 'push'},
                           {'id': 123, 'login': 'other', 'permission': 'push'}]:
            with self.subTest(permission=permission), patch.object(provider, 'request', side_effect=[(200, valid), (200, permission)]):
                with self.assertRaises(UpstreamError):
                    provider.invite(row, 'developer')


if __name__ == '__main__':
    unittest.main()
