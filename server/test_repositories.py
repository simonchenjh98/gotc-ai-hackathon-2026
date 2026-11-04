"""Offline repository workflow through real local HTTP and immutable submission storage."""
import json
import os
import sqlite3
import unittest
from contextlib import closing
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from repositories import Provider, UpstreamError
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

    def __call__(self, method, path, body=None):
        # Every mocked upstream call also proves the writer lock is free.
        with closing(sqlite3.connect(self.service.db_path, timeout=.1)) as db:
            db.execute('BEGIN IMMEDIATE')
        self.calls.append((method, path, body))
        if self.timeout_read and method == 'GET':
            raise UpstreamError()
        if method == 'POST' and path.startswith('/orgs/'):
            org = path.split('/')[2]
            self.repo = {'id': 123, 'full_name': org + '/' + body['name'], 'description': body['description'], 'default_branch': 'main'}
            if self.timeout_create:
                raise UpstreamError()
            return 201, self.repo
        if '/collaborators/' in path:
            if self.timeout_invite:
                raise UpstreamError()
            if path.endswith('/permission'):
                return 200, {'permission': 'push'}
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

    def test_legacy_unbound_and_bound_drafts_remain_compatible(self):
        self.open_submissions()
        result = self.submit(materials=self.materials())
        self.assertEqual(result[0], 200, result)
        self.provision()
        status, result, _ = self.request(self.client, '/api/submission', {'expectedVersion': 1, 'status': 'draft', 'materials': {'codeUrl': '', 'codeVersion': ''}})
        self.assertEqual(status, 200, result)
        self.assertEqual(result['submission']['submitted']['codeUrl'], 'https://github.com/synthetic/repository')


class ProviderTransportTests(unittest.TestCase):
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
        with patch.object(provider, 'request', return_value=(200, {})) as request:
            self.assertEqual(provider.invite({'organization': 'org', 'slug': 'repo'}, 'developer'), 'ready')
            request.assert_called_once_with('PUT', '/repos/org/repo/collaborators/developer', {'permission': 'push'})


if __name__ == '__main__':
    unittest.main()
