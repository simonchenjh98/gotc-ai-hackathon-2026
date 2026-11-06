"""Roster-bound roadshow invitation claiming; never exposes the public roster."""
import base64
from contextlib import closing
import hashlib
import hmac
import os
import secrets
import sqlite3
import unicodedata

from errors import APIError

MAX_NAMES = 100
MAX_REVISION = 9007199254740991
SCHEMA = """
CREATE TABLE IF NOT EXISTS roadshow_access_config (
 id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL DEFAULT 0,
 key_digest TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS roadshow_access_roster (
 judge_id TEXT PRIMARY KEY REFERENCES accounts(id), normalized_name TEXT NOT NULL UNIQUE,
 enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)), nonce TEXT NOT NULL,
 credential_fingerprint TEXT NOT NULL, claimed_at INTEGER, created_at INTEGER NOT NULL
);
CREATE TRIGGER IF NOT EXISTS roadshow_access_stage_revision
 AFTER UPDATE OF revision,run_id ON judging_stages
 WHEN NEW.stage='roadshow' AND (OLD.revision IS NOT NEW.revision OR OLD.run_id IS NOT NEW.run_id)
 BEGIN UPDATE roadshow_access_config SET revision=revision+1 WHERE id=1; END;
"""


class RoadshowAccess:
    def __init__(self, service, clock, text, password_hash):
        self.s, self.clock, self.text, self.password_hash = service, clock, text, password_hash
        path = service.data_dir / 'roadshow-claim-key'
        if path.is_symlink():
            raise RuntimeError('Roadshow claim key must not be a symlink')
        if not path.exists() and service.db_path.exists():
            with closing(sqlite3.connect(service.db_path.as_uri() + '?mode=ro', uri=True)) as database:
                roster = database.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='roadshow_access_roster'").fetchone()
                if roster and database.execute('SELECT 1 FROM roadshow_access_roster LIMIT 1').fetchone():
                    raise RuntimeError('The original roadshow claim key is missing; restore it with the database')
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(secrets.token_bytes(32))
                stream.flush()
                os.fsync(stream.fileno())
        self.key = path.read_bytes()
        if len(self.key) != 32:
            raise RuntimeError('Roadshow claim key has an invalid length')
        os.chmod(path, 0o600)

    def initialize(self, db):
        db.executescript(SCHEMA)
        digest = hashlib.sha256(self.key).hexdigest()
        db.execute('INSERT OR IGNORE INTO roadshow_access_config(id,key_digest) VALUES (1,?)', (digest,))
        if not hmac.compare_digest(db.execute('SELECT key_digest FROM roadshow_access_config WHERE id=1').fetchone()[0], digest):
            if db.execute('SELECT 1 FROM roadshow_access_roster LIMIT 1').fetchone():
                raise RuntimeError('Roadshow claim key does not match the existing roster; restore the original key')
            # A DB-only legacy restore with no identity history has no code to preserve.
            db.execute('UPDATE roadshow_access_config SET key_digest=? WHERE id=1', (digest,))

    @staticmethod
    def supports(path):
        return any(path == endpoint or path.startswith(endpoint + '/') for endpoint in
                   ('/api/roadshow/access', '/api/roadshow/claim', '/api/admin/roadshow-access'))

    @staticmethod
    def normalized(name):
        return ' '.join(unicodedata.normalize('NFKC', name).split()).casefold()

    def clean_name(self, value):
        name = self.text(value, '评委完整姓名', 100, True)
        name = ' '.join(unicodedata.normalize('NFKC', name).split())
        if not name or len(name) > 100 or any(unicodedata.category(c).startswith('C') for c in name):
            raise APIError(400, '请输入名单中的评委完整姓名')
        return name

    @staticmethod
    def fingerprint(account):
        return hashlib.sha256(account['salt'] + account['code_hash']).hexdigest()

    def code(self, row):
        proof = hmac.new(self.key, b'gotc-roadshow-v1\0' + row['judge_id'].encode() + b'\0' + row['nonce'].encode(), hashlib.sha256).digest()
        return row['judge_id'] + '.' + base64.urlsafe_b64encode(proof).decode().rstrip('=')

    @staticmethod
    def owns_account(db, account_id):
        return bool(db.execute('SELECT 1 FROM roadshow_access_roster WHERE judge_id=?', (account_id,)).fetchone())

    def rows(self, db, state):
        return db.execute("""SELECT r.*,a.name,a.active,a.salt,a.code_hash,a.role,
          EXISTS(SELECT 1 FROM judging_grants g WHERE g.run_id=? AND g.judge_id=r.judge_id) AS granted
          FROM roadshow_access_roster r JOIN accounts a ON a.id=r.judge_id
          WHERE r.enabled=1 ORDER BY r.created_at,r.judge_id""", (state['run_id'],)).fetchall()

    def claimable(self, row):
        return bool(row['active'] and row['role'] == 'judge' and row['granted']
                    and self.normalized(row['name']) == row['normalized_name']
                    and hmac.compare_digest(self.fingerprint(row), row['credential_fingerprint']))

    def public_view(self, db, state):
        rows = self.rows(db, state)
        configured = any(row['granted'] for row in rows)
        available = any(self.claimable(row) for row in rows)
        message = ('名单待配置，请等待主办方录入路演评委名单。' if not configured else
                   '请输入名单中的完整姓名领取本人的专属邀请码。' if available else
                   '评委邀请码暂不可领取，请联系组织方。')
        return {'configured': configured, 'claimAvailable': available, 'message': message}

    def editable(self, db, state):
        numeric_table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='roadshow_scores'").fetchone()
        numeric_records = numeric_table and db.execute('SELECT 1 FROM roadshow_scores WHERE run_id=? LIMIT 1', (state['run_id'],)).fetchone()
        return not state['open'] and state['frozen_at'] is None and state['review_count'] == 0 and not numeric_records

    def admin_view(self, db, state):
        rows = self.rows(db, state)
        return {'configured': any(row['granted'] for row in rows),
                'revision': db.execute('SELECT revision FROM roadshow_access_config WHERE id=1').fetchone()[0],
                'runId': state['run_id'], 'editable': bool(self.editable(db, state)),
                'names': [row['name'] for row in rows],
                'judges': [{'id': row['judge_id'], 'name': row['name'], 'active': bool(row['active']),
                            'claimed': row['claimed_at'] is not None, 'canClaim': self.claimable(row)} for row in rows]}

    def save_names(self, db, actor, state, body):
        if not isinstance(body, dict) or set(body) != {'expectedRevision', 'runId', 'names'}:
            raise APIError(400, '请提交名单、轮次和编辑版本')
        if type(body['expectedRevision']) is not int or not 0 <= body['expectedRevision'] <= MAX_REVISION:
            raise APIError(400, '名单编辑版本不正确')
        revision = db.execute('SELECT revision FROM roadshow_access_config WHERE id=1').fetchone()[0]
        if body['expectedRevision'] != revision or body['runId'] != state['run_id']:
            raise APIError(409, '名单或评审轮次已更新，请保留输入并重新载入核对')
        if not self.editable(db, state):
            raise APIError(409, '路演已开放、已冻结或已有评审记录，不能替换评委名单')
        if not isinstance(body['names'], list) or len(body['names']) > MAX_NAMES:
            raise APIError(400, '评委名单必须是最多100个姓名的列表')
        names = [self.clean_name(value) for value in body['names']]
        normalized = [self.normalized(name) for name in names]
        if len(set(normalized)) != len(normalized):
            raise APIError(400, '名单存在同名；请在完整姓名中注明单位等区别后重试')
        current_ids = {row['judge_id'] for row in self.rows(db, state)}
        selected_ids = []
        for name, canonical in zip(names, normalized):
            previous = db.execute('SELECT * FROM roadshow_access_roster WHERE normalized_name=?', (canonical,)).fetchone()
            if previous:
                judge_id = previous['judge_id']
                account = db.execute('SELECT * FROM accounts WHERE id=? AND role=\'judge\'', (judge_id,)).fetchone()
                if not account or not account['active'] or self.normalized(account['name']) != canonical:
                    raise APIError(409, '已有评委身份已停用或更名，请在组织方后台核对后再保存')
                db.execute('UPDATE roadshow_access_roster SET enabled=1 WHERE judge_id=?', (judge_id,))
            else:
                account, _ = self.s.create_account(db, 'judge', name)
                judge_id = account['id']
                # Replace any shared bootstrap code before the account becomes claimable.
                self.s.credential_action(db, actor, {'role': 'judge', 'id': judge_id, 'action': 'rotate'})
                stored = db.execute('SELECT * FROM accounts WHERE id=?', (judge_id,)).fetchone()
                db.execute('INSERT INTO roadshow_access_roster VALUES (?,?,1,?,?,NULL,?)',
                           (judge_id, canonical, secrets.token_hex(16), self.fingerprint(stored), self.clock()))
            selected_ids.append(judge_id)
        for removed in current_ids - set(selected_ids):
            db.execute('UPDATE roadshow_access_roster SET enabled=0 WHERE judge_id=?', (removed,))
            db.execute('DELETE FROM judging_grants WHERE run_id=? AND judge_id=?', (state['run_id'], removed))
        for judge_id in selected_ids:
            db.execute('INSERT OR IGNORE INTO judging_grants VALUES (?,?)', (state['run_id'], judge_id))
        # Invalidate a generic roster editor that was loaded before this grant update.
        db.execute("UPDATE judging_stages SET revision=revision+1 WHERE stage='roadshow'")
        self.s.audit(db, actor, 'roadshow.access.roster', state['run_id'], {'count': len(names), 'revision': revision + 1})
        self.s.touch(db)
        return {'ok': True, **self.admin_view(db, self.s.judging.state(db, 'roadshow'))}

    def claim(self, db, state, body):
        if not isinstance(body, dict) or set(body) != {'name'}:
            raise APIError(400, '请输入评委完整姓名')
        canonical = self.normalized(self.clean_name(body['name']))
        if not self.public_view(db, state)['configured']:
            raise APIError(409, '名单待配置，请等待主办方录入路演评委名单。')
        row = next((row for row in self.rows(db, state) if row['normalized_name'] == canonical), None)
        if row is None or not row['granted']:
            raise APIError(404, '该姓名不在本轮路演评委名单中，请核对完整姓名或联系组织方')
        if not row['active'] or row['role'] != 'judge':
            raise APIError(403, '该评委身份已停用，请联系组织方')
        if not self.claimable(row):
            raise APIError(409, '该评委凭据或姓名已由组织方调整，请联系组织方领取新的登录方式')
        code = self.code(row)
        if row['claimed_at'] is None:
            salt = secrets.token_bytes(16)
            hashed = self.password_hash(code, salt)
            db.execute('UPDATE accounts SET salt=?,code_hash=? WHERE id=?', (salt, hashed, row['judge_id']))
            db.execute('DELETE FROM sessions WHERE account_id=?', (row['judge_id'],))
            db.execute('UPDATE roadshow_access_roster SET claimed_at=?,credential_fingerprint=? WHERE judge_id=?',
                       (self.clock(), hashlib.sha256(salt + hashed).hexdigest(), row['judge_id']))
            self.s.audit(db, None, 'roadshow.access.claimed', row['judge_id'])
        return {'name': row['name'], 'code': code}

    def dispatch(self, method, path, body, cookie, ip, expected_login_id, query=None):
        if query:
            raise APIError(400, '领码接口不接受查询参数')
        if method == 'POST' and path == '/api/roadshow/claim':
            self.s.rate_limit(ip, 'roadshow-claim')
        with self.s.db() as db:
            db.execute('BEGIN IMMEDIATE' if method == 'POST' else 'BEGIN')
            state = self.s.judging.state(db, 'roadshow')
            if method == 'GET' and path == '/api/roadshow/access':
                return self.public_view(db, state)
            if method == 'POST' and path == '/api/roadshow/claim':
                return self.claim(db, state, body)
            if path == '/api/admin/roadshow-access':
                actor = self.s.account(db, cookie)
                self.s.require(actor, 'admin')
                if not expected_login_id:
                    raise APIError(428, '请刷新管理页面并核对登录身份', 'login_required')
                if expected_login_id != self.s.login_id(cookie):
                    raise APIError(401, '管理员登录身份已变化，请重新进入')
                if method == 'GET':
                    return self.admin_view(db, state)
                if method == 'POST':
                    return self.save_names(db, actor, state, body)
            raise APIError(404, '路演领码接口不存在')
