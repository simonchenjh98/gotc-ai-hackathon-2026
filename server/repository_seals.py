"""Protected Gitee refs and their local, version-specific confirmation evidence."""
import os
import re
import secrets
from urllib.parse import quote

from errors import APIError
from repositories import UpstreamError


SCHEMA = """
CREATE TABLE IF NOT EXISTS repository_seal_namespaces (
 prefix TEXT PRIMARY KEY, team_id TEXT NOT NULL REFERENCES teams(id), external_id TEXT NOT NULL,
 organization TEXT NOT NULL, slug TEXT NOT NULL, prefix_rule_id INTEGER,
 UNIQUE(team_id,external_id,organization,slug));
CREATE TABLE IF NOT EXISTS repository_seals (
 id TEXT PRIMARY KEY, prefix TEXT NOT NULL REFERENCES repository_seal_namespaces(prefix),
 binding_revision INTEGER NOT NULL, sha TEXT NOT NULL, exact_rule_id INTEGER, sealed_at INTEGER,
 UNIQUE(prefix,binding_revision,sha));
CREATE TABLE IF NOT EXISTS repository_preview_seals (
 preview_id TEXT PRIMARY KEY REFERENCES repository_previews(id) ON DELETE CASCADE,
 seal_id TEXT NOT NULL REFERENCES repository_seals(id));
CREATE TABLE IF NOT EXISTS repository_submission_seals (
 team_id TEXT NOT NULL, version INTEGER NOT NULL, request_id TEXT NOT NULL, seal_id TEXT NOT NULL REFERENCES repository_seals(id),
 received_at INTEGER NOT NULL, PRIMARY KEY(team_id,version), UNIQUE(team_id,request_id),
 FOREIGN KEY(team_id,version) REFERENCES submission_history(team_id,submission_version));
CREATE TRIGGER IF NOT EXISTS repository_seal_namespace_identity BEFORE UPDATE OF prefix,team_id,external_id,organization,slug
 ON repository_seal_namespaces BEGIN SELECT RAISE(ABORT,'immutable seal namespace'); END;
CREATE TRIGGER IF NOT EXISTS repository_submission_seals_no_update BEFORE UPDATE ON repository_submission_seals
 BEGIN SELECT RAISE(ABORT,'immutable submission seal'); END;
CREATE TRIGGER IF NOT EXISTS repository_submission_seals_no_delete BEFORE DELETE ON repository_submission_seals
 BEGIN SELECT RAISE(ABORT,'immutable submission seal'); END;
CREATE TRIGGER IF NOT EXISTS repository_seals_no_rewrite BEFORE UPDATE ON repository_seals WHEN OLD.sealed_at IS NOT NULL
 BEGIN SELECT RAISE(ABORT,'immutable verified seal'); END;
"""

SEAL_SELECT = """SELECT s.*, n.team_id,n.external_id,n.organization,n.slug
 FROM repository_seals s JOIN repository_seal_namespaces n USING(prefix)"""


def proof_fields(row):
    return {'id': row['id'], 'ref': row['prefix'] + '-' + row['sha'], 'sha': row['sha'],
            'sealedAt': row['sealed_at'], 'viewUrl': f"https://gitee.com/{row['organization']}/{row['slug']}/commit/{row['sha']}"}


def invalid_proof():
    return APIError(409, '封存凭据与当前仓库或预览不一致，请重新检查材料', 'repository_seal_mismatch')


class RepositorySeals:
    def __init__(self, repositories):
        self.repos = repositories
        self.enabled = os.environ.get('GOTC_REPO_GITEE_SEAL_ENABLED') == '1'

    def applies(self, binding):
        return self.enabled and binding['provider'] == 'gitee'

    def initialize(self, db):
        db.executescript(SCHEMA)

    def check_binding(self, db, binding):
        current = self.repos.binding(db, binding['team_id'])
        if (not self.repos.can_submit(current) or
                any(current[k] != binding[k] for k in ('revision','provider','organization','slug','external_id'))):
            raise invalid_proof()

    def reserve(self, binding, sha):
        identity = (binding['team_id'], binding['external_id'], binding['organization'], binding['slug'])
        with self.repos.s.db() as db:
            db.execute('BEGIN IMMEDIATE')
            self.check_binding(db, binding)
            db.execute("""INSERT INTO repository_seal_namespaces(prefix,team_id,external_id,organization,slug)
                VALUES (?,?,?,?,?) ON CONFLICT(team_id,external_id,organization,slug) DO NOTHING""",
                       ('gotc-archive-' + secrets.token_hex(16), *identity))
            namespace = db.execute("""SELECT * FROM repository_seal_namespaces WHERE
                team_id=? AND external_id=? AND organization=? AND slug=?""", identity).fetchone()
            operation = (namespace['prefix'], binding['revision'], sha)
            db.execute("""INSERT INTO repository_seals(id,prefix,binding_revision,sha) VALUES (?,?,?,?)
                ON CONFLICT(prefix,binding_revision,sha) DO NOTHING""", (secrets.token_hex(24), *operation))
            seal = db.execute('SELECT * FROM repository_seals WHERE prefix=? AND binding_revision=? AND sha=?', operation).fetchone()
            return dict(namespace), dict(seal)

    def protect(self, provider, binding, name, role, previous_id):
        base = provider.path(binding) + '/branches/'
        settings = {'pusher': role, 'merger': role, 'mode': 'standard'}
        # Both names come exclusively from the committed, random local reservation.
        if previous_id is None:
            try:
                result = provider.request('PUT', base + 'setting/new', {'wildcard': name, **settings})
            except UpstreamError as error:
                if error.upstream_status != 400:
                    raise
                result = None
        else:
            result = None
        if result is None:
            result = provider.request('PUT', base + quote(name, safe='') + '/setting', settings)
        status, data = result
        if (status != 200 or not isinstance(data, dict) or type(data.get('id')) is not int or data['id'] <= 0
                or previous_id is not None and previous_id != data['id']
                or type(data.get('project_id')) is not int or str(data['project_id']) != binding['external_id']
                or data.get('wildcard') != name or data.get('mode') != 'standard'
                or data.get('pushers') != role or data.get('mergers') != role
                or data.get('escapse_protect_branch_list') != []):
            raise UpstreamError()
        return data['id']

    def verify_branch(self, result, status, ref, sha):
        actual_status, data = result
        if (actual_status != status or not isinstance(data, dict) or data.get('name', ref) != ref
                or data.get('protected') is not True or not isinstance(data.get('commit'), dict)
                or data['commit'].get('sha') != sha):
            raise UpstreamError()

    def ensure(self, provider, binding, sha):
        if (provider.name != 'gitee' or not self.applies(binding) or not isinstance(sha,str)
                or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', sha)):
            raise invalid_proof()
        namespace, seal = self.reserve(binding, sha)
        prefix = namespace['prefix']
        ref = prefix + '-' + sha
        base = provider.path(binding) + '/branches'
        branch = base + '/' + quote(ref, safe='')
        prefix_id = self.protect(provider, binding, prefix + '-*', 'admin', namespace['prefix_rule_id'])
        try:
            result = provider.request('GET', branch)
        except UpstreamError as error:
            if error.upstream_status != 404 or seal['sealed_at'] is not None:
                raise
            result = provider.request('POST', base, {'refs': sha, 'branch_name': ref})
            self.verify_branch(result, 201, ref, sha)
        else:
            self.verify_branch(result, 200, ref, sha)
        exact_id = self.protect(provider, binding, ref, 'none', seal['exact_rule_id'])
        self.verify_branch(provider.request('GET', branch), 200, ref, sha)
        # Confirm the numeric repository identity again after provider writes.
        self.repos.verify_repository(binding, provider.repository(binding))
        with self.repos.s.db() as db:
            db.execute('BEGIN IMMEDIATE')
            self.check_binding(db, binding)
            db.execute('UPDATE repository_seal_namespaces SET prefix_rule_id=? WHERE prefix=? AND prefix_rule_id IS NULL',
                       (prefix_id, prefix))
            stored_prefix = db.execute('SELECT prefix_rule_id FROM repository_seal_namespaces WHERE prefix=?', (prefix,)).fetchone()[0]
            db.execute('UPDATE repository_seals SET sealed_at=?, exact_rule_id=? WHERE id=? AND sealed_at IS NULL',
                       (self.repos.now(), exact_id, seal['id']))
            row = db.execute(SEAL_SELECT + ' WHERE id=?', (seal['id'],)).fetchone()
            if stored_prefix != prefix_id or row['exact_rule_id'] != exact_id:
                raise invalid_proof()
            return proof_fields(row)

    def for_preview(self, db, preview_id):
        row = db.execute(SEAL_SELECT + """ JOIN repository_preview_seals p ON p.seal_id=s.id
            WHERE p.preview_id=? AND s.sealed_at IS NOT NULL""", (preview_id,)).fetchone()
        return proof_fields(row) if row else None

    def verify_proof(self, db, binding, preview, proof):
        row = db.execute(SEAL_SELECT + """ JOIN repository_preview_seals p ON p.seal_id=s.id
            WHERE p.preview_id=? AND s.sealed_at IS NOT NULL""", (preview['id'],)).fetchone()
        if (not row or binding['provider'] != 'gitee' or any(row[k] != binding[k] for k in ('team_id','external_id','organization','slug'))
                or row['binding_revision'] != binding['revision'] or row['team_id'] != preview['team_id']
                or row['sha'] != preview['sha'] or proof.get('repositorySeal') != proof_fields(row)
                or preview['binding_revision'] != row['binding_revision'] or proof.get('sha') != row['sha']):
            raise invalid_proof()

    def record_submission(self, db, team_id, version, request_id, proof):
        if not proof or not proof.get('repositorySeal'):
            return None
        db.execute('INSERT INTO repository_submission_seals VALUES (?,?,?,?,?)',
                   (team_id, version, request_id, proof['repositorySeal']['id'], proof['receivedAt']))
        return self.for_version(db, team_id, version)

    def for_version(self, db, team_id, version):
        if version is None:
            return None
        row = db.execute("""SELECT s.*,n.organization,n.slug,v.received_at FROM repository_submission_seals v
            JOIN repository_seals s ON s.id=v.seal_id JOIN repository_seal_namespaces n USING(prefix)
            WHERE v.team_id=? AND v.version=?""", (team_id,version)).fetchone()
        return {**proof_fields(row), 'receivedAt': row['received_at']} if row else None

    def source_for_version(self, db, team_id, version):
        """Internal immutable source identity; never use the current mutable binding."""
        row=db.execute(SEAL_SELECT+""" JOIN repository_submission_seals v ON v.seal_id=s.id
            WHERE v.team_id=? AND v.version=? AND s.sealed_at IS NOT NULL""",(team_id,version)).fetchone()
        return {**{key:row[key] for key in ('team_id','external_id','organization','slug','prefix','sha')},
                'provider':'gitee','ref':row['prefix']+'-'+row['sha']} if row else None
