"""Organization-owned repositories; upstream work never runs in a SQLite writer transaction.

All provider errors deliberately discard upstream bodies/URLs: they can contain tokens.
The operation marker permits recovery of a lost create response without adopting a name collision.
"""
import hashlib
import json
import os
import re
import secrets
import socket
import threading
import time
from collections import deque
from contextlib import contextmanager
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler

from errors import APIError

PREVIEW_MS = 5 * 60 * 1000
SCHEMA = '''
CREATE TABLE IF NOT EXISTS repository_bindings (
 team_id TEXT PRIMARY KEY REFERENCES teams(id), provider TEXT NOT NULL, organization TEXT NOT NULL,
 slug TEXT NOT NULL, branch TEXT NOT NULL, usernames_json TEXT NOT NULL, state TEXT NOT NULL,
 operation_id TEXT NOT NULL UNIQUE, external_id TEXT, revision INTEGER NOT NULL,
 protection TEXT NOT NULL, message TEXT NOT NULL, updated_at INTEGER NOT NULL,
 UNIQUE(provider,organization,slug));
CREATE TABLE IF NOT EXISTS repository_previews (
 id TEXT PRIMARY KEY, team_id TEXT NOT NULL REFERENCES teams(id), session_hash TEXT NOT NULL,
 binding_revision INTEGER NOT NULL, sha TEXT NOT NULL, expires_at INTEGER NOT NULL);
'''


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class UpstreamError(APIError):
    def __init__(self, upstream_status=None):
        super().__init__(502, '仓库服务未能确认操作结果，请由管理员核对后重试', 'repository_upstream')
        self.upstream_status = upstream_status


class ProviderOperations:
    """Single-process operation permits and bounded rolling team request quotas."""
    LIMITS = {'preview': 10, 'confirmation': 20}
    WINDOW_SECONDS = 60
    MAX_BUCKETS = 4096

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.permits = {name: threading.BoundedSemaphore(4) for name in ('github', 'gitee')}
        self.lock = threading.Lock()
        self.buckets = {}

    def spend(self, provider, team_id, kind):
        with self.lock:
            now = self.clock()
            # Expire idle teams as well as the active bucket, bounding retained state.
            for key in list(self.buckets):
                bucket = self.buckets[key]
                while bucket and bucket[0] <= now - self.WINDOW_SECONDS:
                    bucket.popleft()
                if not bucket:
                    del self.buckets[key]
            key = (provider, team_id, kind)
            bucket = self.buckets.get(key)
            if bucket is None:
                if len(self.buckets) >= self.MAX_BUCKETS:
                    raise APIError(503, '仓库查询繁忙，请等待一分钟后刷新状态再重试', 'repository_busy')
                bucket = self.buckets[key] = deque()
            if len(bucket) >= self.LIMITS[kind]:
                raise APIError(429, '本队仓库查询过于频繁，请等待一分钟后重新检查材料或查询提交记录', 'repository_rate_limit')
            bucket.append(now)

    @contextmanager
    def operation(self, provider, team_id=None, kind=None):
        permit = self.permits[provider]
        if not permit.acquire(blocking=False):
            raise APIError(503, '仓库服务繁忙，本次尚未执行远端操作；请稍后刷新状态再重试', 'repository_busy')
        try:
            if kind is not None:
                self.spend(provider, team_id, kind)
            yield
        finally:
            permit.release()


class Provider:
    """Small fixed-origin GitHub REST/Gitee API v5 adapter, with bounded responses."""
    def __init__(self, name, organization, token, branch, enabled):
        self.name, self.organization, self.token, self.branch = name, organization, token, branch
        self.enabled = enabled
        self.origin = 'https://api.github.com' if name == 'github' else 'https://gitee.com/api/v5'
        self.web = 'https://github.com' if name == 'github' else 'https://gitee.com'

    def request(self, method, path, body=None):
        if not self.enabled:
            raise APIError(503, '该组织仓库渠道尚未配置或未启用，请联系组织方', 'repository_disabled')
        if not path.startswith('/') or path.startswith('//') or '?' in path or '#' in path:
            raise ValueError('Provider paths must be fixed relative API paths')
        headers = {'Accept': 'application/vnd.github+json' if self.name == 'github' else 'application/json',
                   'User-Agent': 'GOTC-Repository-Service', 'Content-Type': 'application/json'}
        url = self.origin + path
        if self.name == 'github':
            headers.update({'Authorization': 'Bearer ' + self.token, 'X-GitHub-Api-Version': '2022-11-28'})
        elif method == 'GET':
            url += '?' + urlencode({'access_token': self.token})
        else:
            body = {**(body or {}), 'access_token': self.token}
        data = json.dumps(body).encode() if body is not None else None
        try:
            with build_opener(NoRedirect()).open(Request(url, data=data, headers=headers, method=method), timeout=8) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
                if len(raw) > 2 * 1024 * 1024:
                    raise UpstreamError()
                return response.status, json.loads(raw) if raw else {}
        except HTTPError as error:
            status = error.code
            error.close()
            raise UpstreamError(status) from None
        except (URLError, TimeoutError, socket.timeout, ValueError, OSError):
            raise UpstreamError() from None

    def path(self, row):
        return '/repos/' + quote(row['organization'], safe='') + '/' + quote(row['slug'], safe='')

    def repository(self, row):
        return self.request('GET', self.path(row))[1]

    def create(self, row):
        body = {'name': row['slug'], 'description': 'GOTC provisioning ' + row['operation_id'],
                'auto_init': True, 'private': False, 'has_wiki': False}
        if self.name == 'gitee':
            # The GOTC Gitee namespace is an enterprise, not an ordinary organization.
            # This endpoint's private field is an integer enum: 1 means externally open.
            body.update(path=row['slug'], private=1, outsourced=False)
            status, data = self.request('POST', '/enterprises/' + quote(row['organization'], safe='') + '/repos', body)
            if status != 201:
                raise UpstreamError()
            return data
        return self.request('POST', '/orgs/' + quote(row['organization'], safe='') + '/repos', body)[1]

    def head(self, row, branch=None):
        data = self.request('GET', self.path(row) + '/branches/' + quote(branch or row['branch'], safe=''))[1]
        commit = data.get('commit') if isinstance(data, dict) else None
        sha = commit.get('sha', '') if isinstance(commit, dict) else ''
        if not isinstance(sha, str) or not re.fullmatch(r'[0-9a-fA-F]{40}|[0-9a-fA-F]{64}', sha):
            raise UpstreamError()
        return sha.lower()

    def prepare_branch(self, row, repository):
        default = repository.get('default_branch')
        if not isinstance(default, str) or not default:
            raise UpstreamError()
        if default != row['branch']:
            # Never overwrite a branch, including when resuming an interrupted initialization.
            try:
                self.head(row)
            except UpstreamError as error:
                if error.upstream_status != 404:
                    raise
                sha = self.head(row, default)
                if self.name == 'github':
                    self.request('POST', self.path(row) + '/git/refs', {'ref': 'refs/heads/' + row['branch'], 'sha': sha})
                else:
                    self.request('POST', self.path(row) + '/branches', {'refs': default, 'branch_name': row['branch']})

    def protect(self, row):
        if self.name != 'github':
            return 'unverified'
        try:
            self.request('PUT', self.path(row) + '/branches/' + quote(row['branch'], safe='') + '/protection',
                         {'required_status_checks': None, 'enforce_admins': True, 'required_pull_request_reviews': None,
                          'restrictions': None, 'allow_force_pushes': False, 'allow_deletions': False})
            return 'enabled'
        except UpstreamError:
            # Paid-plan/permission failures must remain visible; a rule is not a code archive.
            return 'unverified'

    def invite(self, row, username):
        path = self.path(row) + '/collaborators/' + quote(username, safe='')
        status, member = self.request('PUT', path, {'permission': 'push'})
        if self.name == 'github':
            return 'invited' if status == 201 else 'ready'
        # Ordinary Gitee v5 documents 200 + ProjectMember. An enterprise/pending/empty
        # response must not be presented as a confirmed grant. Verify the same user's
        # effective write access independently rather than inferring it from HTTP success.
        if (status != 200 or not isinstance(member, dict) or type(member.get('id')) is not int
                or member['id'] <= 0 or not isinstance(member.get('login'), str)
                or member['login'].lower() != username.lower()):
            raise UpstreamError()
        status, permission = self.request('GET', path + '/permission')
        if (status != 200 or not isinstance(permission, dict) or type(permission.get('id')) is not int
                or permission['id'] != member['id'] or not isinstance(permission.get('login'), str)
                or permission['login'].lower() != username.lower()):
            raise UpstreamError()
        if permission.get('permission') == 'admin':
            raise APIError(409, '开发者账号仍有仓库管理权限，请主办方人工核对多余权限后恢复；平台不会自动修改组织或企业继承权限', 'repository_excess_permission')
        if permission.get('permission') != 'push':
            raise UpstreamError()
        return 'ready'


class Repositories:
    def __init__(self, service):
        self.s = service
        from repository_seals import RepositorySeals
        self.seals = RepositorySeals(self)
        self.operations = ProviderOperations()
        self.providers = {}
        for name, default in [('github', 'GOTC-AI-HACKATHON'), ('gitee', 'gotc-ai-hackathon')]:
            prefix = 'GOTC_REPO_' + name.upper()
            org = os.environ.get(prefix + '_ORG', default)
            branch = os.environ.get(prefix + '_BRANCH', 'main')
            valid = bool(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,99}', org) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,99}', branch))
            token = os.environ.get(prefix + '_TOKEN', '')
            enabled = valid and bool(token) and (name != 'gitee' or os.environ.get(prefix + '_ENABLED') == '1')
            self.providers[name] = Provider(name, org, token, branch, enabled)

    def initialize(self, db):
        db.executescript(SCHEMA)
        self.seals.initialize(db)

    def now(self):
        return self.s.judging.clock()

    def get(self, db, team_id):
        return db.execute('SELECT * FROM repository_bindings WHERE team_id=?', (team_id,)).fetchone()

    def public(self, row):
        if not row:
            return None
        provider = self.providers[row['provider']]
        return {'teamId': row['team_id'], 'provider': row['provider'], 'organization': row['organization'],
                'slug': row['slug'], 'branch': row['branch'], 'usernames': json.loads(row['usernames_json']),
                'state': row['state'], 'revision': row['revision'], 'protection': row['protection'],
                'message': row['message'], 'updatedAt': row['updated_at'], 'externalId': row['external_id'],
                'codeUrl': provider.web + '/' + row['organization'] + '/' + row['slug'] if row['external_id'] else None,
                'archived': False}

    def availability(self):
        return [{'provider': key, 'enabled': bool(p.enabled), 'organization': p.organization, 'branch': p.branch,
                 'message': '已配置；真实组织权限仍需上线验收' if p.enabled else '尚未启用，请联系组织方'} for key, p in self.providers.items()]

    def usernames(self, name, values):
        if not isinstance(values, list) or not 1 <= len(values) <= 5:
            raise APIError(400, '请填写 1 至 5 个开发者用户名', 'repository_usernames')
        pattern = r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?' if name == 'github' else r'[A-Za-z0-9][A-Za-z0-9_-]{0,38}'
        if any(not isinstance(v, str) or not re.fullmatch(pattern, v) or (name == 'github' and '--' in v) for v in values):
            raise APIError(400, '请填写平台用户名，不要填写网址、邮箱或显示名称', 'repository_usernames')
        if len({v.lower() for v in values}) != len(values):
            raise APIError(400, '开发者用户名不能重复', 'repository_usernames')
        return values

    def provider(self, row):
        p = self.providers[row['provider']]
        if not p.enabled or p.organization != row['organization']:
            raise APIError(503, '该组织仓库渠道尚未配置或组织设置已变化，请联系组织方', 'repository_disabled')
        return p

    def identity(self, cookie, expected_login_id, role, db):
        actor = self.s.account(db, cookie)
        self.s.require(actor, role)
        if not expected_login_id:
            raise APIError(428, '请刷新页面并核对登录身份后重试', 'login_required')
        if expected_login_id != self.s.login_id(cookie):
            raise APIError(401, '登录身份已变化，请重新登录后操作')
        return actor

    def update(self, db, actor, row, state, **values):
        allowed = {'external_id', 'protection', 'message'}
        if set(values) - allowed:
            raise ValueError('Invalid repository update')
        values.update(state=state, updated_at=self.now())
        db.execute('UPDATE repository_bindings SET ' + ','.join(key + '=?' for key in values) + ',revision=revision+1 WHERE team_id=?', (*values.values(), row['team_id']))
        self.s.audit(db, actor, 'repository.' + state, row['team_id'], {'provider': row['provider'], 'slug': row['slug']})
        return self.get(db, row['team_id'])

    def verify_repository(self, row, data):
        expected = row['organization'] + '/' + row['slug']
        if row['provider'] == 'gitee' and (not isinstance(data, dict) or not isinstance(data.get('namespace'), dict)
                or data['namespace'].get('type') != 'enterprise'):
            raise APIError(409, '远端仓库不属于预期 Gitee 企业命名空间，请管理员人工核对', 'repository_identity')
        if (not isinstance(data, dict) or str(data.get('full_name', '')).lower() != expected.lower()
                or type(data.get('id')) is not int or data['id'] <= 0
                or (row['external_id'] and str(data['id']) != row['external_id'])
                or (not row['external_id'] and data.get('description') != 'GOTC provisioning ' + row['operation_id'])):
            raise APIError(409, '远端仓库身份不匹配，禁止认领同名仓库；请管理员人工核对', 'repository_identity')
        return str(data['id'])

    def dispatch(self, method, path, body, cookie, expected_login_id, query):
        admin = path.startswith('/api/admin/')
        role = 'admin' if admin else 'team'
        with self.s.db() as db:
            actor = self.identity(cookie, expected_login_id, role, db)
            team = None if admin else self.s.bound_submission_team(db, actor)
            if method == 'GET' and not query and path == '/api/team/repository':
                return {'providers': self.availability(), 'repository': self.public(self.get(db, team['id']))}
            if method == 'GET' and not query and path == '/api/admin/repositories':
                return {'providers': self.availability(), 'items': [self.public(row) for row in db.execute('SELECT * FROM repository_bindings ORDER BY updated_at,team_id')]}
        if method != 'POST' or query or not isinstance(body, dict):
            raise APIError(400, '仓库请求路径或字段不正确')
        if path == '/api/team/repository/apply':
            with self.s.db() as db:
                db.execute('BEGIN IMMEDIATE')
                actor = self.identity(cookie, expected_login_id, role, db)
                team = self.s.bound_submission_team(db, actor)
                if set(body) != {'provider', 'usernames'} or not isinstance(body['provider'], str) or body['provider'] not in self.providers:
                    raise APIError(400, '仓库申请字段不正确')
                p = self.providers[body['provider']]
                if not p.enabled:
                    raise APIError(503, '该组织仓库渠道尚未配置或未启用，请联系组织方', 'repository_disabled')
                usernames = self.usernames(p.name, body['usernames'])
                if self.get(db, team['id']):
                    raise APIError(409, '本队已有仓库申请或绑定，请联系组织方核对，不能重复申请')
                slug = 'gotc-2026-team-' + str(team['number']) + '-' + hashlib.sha256(team['id'].encode()).hexdigest()[:12]
                db.execute('INSERT INTO repository_bindings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                           (team['id'], p.name, p.organization, slug, p.branch, json.dumps(usernames), 'requested', secrets.token_hex(24), None, 1, 'unverified', '等待管理员核对用户名归属并批准', self.now()))
                self.s.audit(db, actor, 'repository.requested', team['id'], {'provider': p.name, 'slug': slug})
                return {'repository': self.public(self.get(db, team['id']))}
        if path == '/api/team/repository/preview':
            if body:
                raise APIError(400, 'SHA 预览不接受仓库地址或分支输入')
            return self.preview(cookie, expected_login_id)
        if path in {'/api/admin/repositories/approve', '/api/admin/repositories/reconcile'}:
            self.s.require(actor, 'admin')
            return self.provision(path.endswith('/reconcile'), body, cookie, expected_login_id)
        raise APIError(404, '仓库路径不存在')

    def provision(self, reconcile, body, cookie, login_id):
        if set(body) != {'teamId', 'expectedRevision', 'usernamesVerified'} or body['usernamesVerified'] is not True or type(body['expectedRevision']) is not int:
            raise APIError(400, '管理员须明确确认已核对全部用户名归属，并提供当前修订号')
        # Inspect identity/provider without a writer lock, then acquire the shared permit.
        # _provision rechecks the revision and identity atomically before changing state.
        with self.s.db() as db:
            self.identity(cookie, login_id, 'admin', db)
            if not isinstance(body['teamId'], str):
                raise APIError(400, '队伍编号不正确')
            row = self.get(db, body['teamId'])
            if not row:
                raise APIError(404, '仓库申请不存在')
            p = self.provider(row)
        with self.operations.operation(p.name):
            return self._provision(reconcile, body, cookie, login_id)

    def _provision(self, reconcile, body, cookie, login_id):
        with self.s.db() as db:
            db.execute('BEGIN IMMEDIATE')
            actor = self.identity(cookie, login_id, 'admin', db)
            if not isinstance(body['teamId'], str):
                raise APIError(400, '队伍编号不正确')
            row = self.get(db, body['teamId'])
            if not row:
                raise APIError(404, '仓库申请不存在')
            if row['revision'] != body['expectedRevision']:
                raise APIError(409, '仓库状态已变化，请刷新后核对', 'repository_conflict')
            p = self.provider(row)
            if not reconcile and row['state'] != 'requested':
                raise APIError(409, '已处理的申请只能使用核对恢复，不能再次建仓')
            if reconcile and row['state'] not in {'reconcile', 'provisioning', 'invited', 'ready'}:
                raise APIError(409, '此申请尚未批准，请先批准')
            # A crashed process leaves provisioning; require a bounded quiet period before recovery.
            if row['state'] == 'provisioning' and self.now() - row['updated_at'] < 120000:
                raise APIError(409, '仓库操作仍可能进行中，请两分钟后刷新核对')
            row = dict(self.update(db, actor, row, 'provisioning', message='正在核对远端；中断后须由管理员核对恢复'))
        try:
            if reconcile:
                data = p.repository(row)
            else:
                # A pre-existing name is always a conflict, never permission to adopt it.
                try:
                    p.repository(row)
                except UpstreamError as error:
                    if error.upstream_status != 404:
                        raise
                else:
                    raise APIError(409, '远端已有同名仓库，禁止自动认领，请管理员人工核对', 'repository_identity')
                data = p.create(row)
            external_id = self.verify_repository(row, data)
            # Persist identity immediately, before branch creation or permission grants.
            with self.s.db() as db:
                db.execute('BEGIN IMMEDIATE')
                current = self.get(db, row['team_id'])
                if current['revision'] != row['revision']:
                    raise APIError(409, '仓库操作已被其他管理员接管，请刷新核对')
                row = dict(self.update(db, actor, current, 'provisioning', external_id=external_id))
            p.prepare_branch(row, data)
            protection = p.protect(row)
            state = 'ready'
            for username in json.loads(row['usernames_json']):
                if p.invite(row, username) == 'invited':
                    state = 'invited'
            message = '仓库已创建，等待开发者接受邀请；管理员可核对恢复' if state == 'invited' else '仓库写权限已确认'
        except APIError as error:
            state, protection, message = 'reconcile', row['protection'], error.message
        with self.s.db() as db:
            db.execute('BEGIN IMMEDIATE')
            current = self.get(db, row['team_id'])
            if current['revision'] != row['revision']:
                raise APIError(409, '仓库状态已变化，请刷新后核对', 'repository_conflict')
            result = self.update(db, actor, current, state, protection=protection, message=message)
            return {'repository': self.public(result)}

    def binding(self, db, team_id):
        row = self.get(db, team_id)
        return row if row and row['external_id'] else None

    def preview_row(self, db, team_id, session_hash, preview_id, reference_time=None):
        if not isinstance(preview_id, str) or not re.fullmatch(r'[0-9a-f]{48}', preview_id):
            raise APIError(400, '请先读取绑定仓库的 SHA 预览', 'repository_preview_required')
        row = self.binding(db, team_id)
        preview = db.execute('SELECT * FROM repository_previews WHERE id=? AND team_id=? AND session_hash=?', (preview_id, team_id, session_hash)).fetchone()
        if (not row or not preview or preview['expires_at'] <= (self.now() if reference_time is None else reference_time) or row['revision'] != preview['binding_revision']
                or row['state'] not in {'invited', 'ready'}):
            raise APIError(409, 'SHA 预览已失效或不属于当前登录，请重新检查材料', 'repository_preview_expired')
        return row, preview

    def preview(self, cookie, login_id):
        with self.s.db() as db:
            actor = self.identity(cookie, login_id, 'team', db)
            team = self.s.bound_submission_team(db, actor)
            row = self.binding(db, team['id'])
            if not row or row['state'] not in {'invited', 'ready'}:
                raise APIError(409, '本队仓库尚未完成绑定，请联系组织方')
            row = dict(row)
            if not self.s.submission_window(db)['canEdit']:
                raise APIError(409, '作品提交尚未开放或已截止', 'submission_closed')
        p = self.provider(row)
        with self.operations.operation(p.name, team['id'], 'preview'):
            self.verify_repository(row, p.repository(row))
            sha = p.head(row)
            seal = self.seals.ensure(p, row, sha) if self.seals.applies(row) else None
        with self.s.db() as db:
            db.execute('BEGIN IMMEDIATE')
            actor = self.identity(cookie, login_id, 'team', db)
            current_team = self.s.bound_submission_team(db, actor)
            current = self.binding(db, current_team['id'])
            if current_team['id'] != team['id'] or not current or current['state'] not in {'invited', 'ready'} or any(
                    current[k] != row[k] for k in ('revision','provider','external_id','organization','slug')):
                raise APIError(409, '仓库绑定已变化，请重新读取')
            if not self.s.submission_window(db)['canEdit']:
                raise APIError(409, '作品提交尚未开放或已截止', 'submission_closed')
            preview_id, expires = secrets.token_hex(24), self.now() + PREVIEW_MS
            # Retain expired rows through SQLite's writer queue so an on-time arrival
            # still has evidence if another preview commits first.
            db.execute('DELETE FROM repository_previews WHERE expires_at<=?', (self.now() - PREVIEW_MS,))
            db.execute('INSERT INTO repository_previews VALUES (?,?,?,?,?,?)', (preview_id, team['id'], login_id, row['revision'], sha, expires))
            if seal:
                db.execute('INSERT INTO repository_preview_seals VALUES (?,?)', (preview_id, seal['id']))
            return {**({'repositorySeal': seal} if seal else {}), 'previewId': preview_id, 'sha': sha, 'codeUrl': self.public(row)['codeUrl'], 'branch': row['branch'], 'expiresAt': expires, 'archived': False}

    def preflight(self, body, cookie, login_id):
        """Read proof and upstream head before the receipt writer lock. Existing requests skip I/O."""
        if not isinstance(body, dict) or body.get('status') != 'submitted':
            return None
        with self.s.db() as db:
            actor = self.s.account(db, cookie)
            self.s.require(actor, 'team')
            team = self.s.bound_submission_team(db, actor)
            self.s.submission_records.validate_envelope(body)
            if isinstance(body.get('requestId'), str) and db.execute('SELECT 1 FROM submission_requests WHERE team_id=? AND request_id=?', (team['id'], body['requestId'])).fetchone():
                return None
            row = self.binding(db, team['id'])
            if not row:
                return None
            self.identity(cookie, login_id, 'team', db)
            preview_id = body.get('repoPreviewId')
            seal = self.seals.for_preview(db, preview_id) if isinstance(preview_id, str) else None
            # Disabling new seals cannot change the promise made by an issued preview.
            if seal or self.seals.applies(row):
                self.provider(row)  # Check local configuration without contacting Gitee.
                if not isinstance(preview_id, str):
                    raise APIError(400, '请先封存绑定仓库的版本', 'repository_preview_required')
                if not seal:
                    raise APIError(409, '预览尚未封存或已失效，请重新检查材料', 'repository_preview_expired')
                return {'previewId': preview_id, 'sha': seal['sha'], 'sessionHash': login_id, 'repositorySeal': seal}
            if not self.s.submission_window(db)['canEdit']:
                raise APIError(409, '作品提交尚未开放或已截止', 'submission_closed')
            row, preview = self.preview_row(db, team['id'], login_id, body.get('repoPreviewId'))
            row, preview = dict(row), dict(preview)
        p = self.provider(row)
        with self.operations.operation(p.name, team['id'], 'confirmation'):
            self.verify_repository(row, p.repository(row))
            sha = p.head(row)
        if sha != preview['sha']:
            raise APIError(409, '正式分支已有新提交，请重新检查材料并预览 SHA', 'repository_head_changed')
        return {'previewId': preview['id'], 'sha': sha, 'sessionHash': login_id}

    def formal_payload(self, db, team, payload, preview_id, proof):
        row = self.binding(db, team['id'])
        if payload['status'] != 'submitted':
            if preview_id is not None:
                raise APIError(400, '草稿保存不接受 SHA 预览编号')
            return payload
        if not row:
            if preview_id is not None:
                raise APIError(400, '未绑定仓库不能使用 SHA 预览编号')
            return payload
        if not proof or proof.get('previewId') != preview_id:
            raise APIError(409, '仓库绑定已变化，请重新检查材料', 'repository_preview_required')
        row, preview = self.preview_row(db, team['id'], proof['sessionHash'], preview_id, proof.get('receivedAt'))
        if self.seals.applies(row) or proof.get('repositorySeal'):
            self.seals.verify_proof(db, row, preview, proof)
        if proof['sha'] != preview['sha']:
            raise APIError(409, '正式分支已有新提交，请重新预览', 'repository_head_changed')
        url = self.public(row)['codeUrl']
        materials = dict(payload['materials'])
        if materials.get('codeUrl', '') not in ('', url) or materials.get('codeVersion', '') not in ('', preview['sha']):
            raise APIError(400, '绑定队伍的仓库与版本必须使用服务器 SHA 预览，不能手动覆盖', 'repository_override')
        materials.update(codeUrl=url, codeVersion=preview['sha'])
        return {**payload, 'materials': materials}
