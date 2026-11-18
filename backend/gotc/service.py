"""Event application composition, session identity and shared event configuration."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import threading
import time
from collections import OrderedDict, deque
from contextlib import contextmanager
from http.cookies import CookieError, SimpleCookie

from backend.gotc.http import request_models as inputs
from . import admin_auth, api, bootstrap, database, migrations
from .admin_data import AdminDataService
from .constants import COOKIE_NAME, DIMENSIONS, LOGIN_KDF_CONCURRENCY, SESSION_SECONDS, VOTE_LIMIT
from .errors import APIError
from .http.routes import resolve_endpoint
from .credentials import Credentials
from .gateway.keys import KeyService
from .settings import public_origin
from .judging.finals import FinalsService
from .judging.judge_login_schedule import JudgeLoginSchedule
from .judging.judging import JudgingService
from .judging.roadshow_access import RoadshowAccess
from .judging.roadshow_scoring import RoadshowScoring
from .judging.standings import Standings
from .submissions.material_uploads import MaterialUploads
from .submissions.repositories import Repositories
from .submissions.repository_backups import RepositoryBackups
from .submissions.submission_records import SubmissionRecords
from .submissions.workflow import SubmissionWorkflow
from .teams.admin_team_access import AdminTeamAccess, operator_for
from .teams.claims import Claims
from .teams.team_lifecycle import TeamLifecycle
from .teams.team_registration import TeamRegistration
from .validation import csv_safe, text_field, validate_unicode
from .write_conflicts import WriteConflicts


def now_ms():
    return int(time.time() * 1000)


def scrypt_hash(code, salt):
    return hashlib.scrypt(code.encode(), salt=salt, n=16384, r=8, p=1, dklen=32)


class EventService:
    def __init__(self, data_dir=None, base_path=None, origin=None, static_root=None, *, clock=None):
        project_root = Path(__file__).resolve().parents[2]
        self.root = Path(static_root or project_root / "frontend").resolve()
        self.data_dir = bootstrap.data_directory(data_dir, project_root)
        self.clock = clock if clock is not None else lambda: now_ms()
        self.standings = Standings(self)
        self.submissions = SubmissionWorkflow(self)
        self.base_path = (base_path if base_path is not None else os.environ.get("GOTC_BASE_PATH", "/gotc")).rstrip("/")
        if self.base_path and not re.fullmatch(r"/[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*", self.base_path):
            raise ValueError("GOTC_BASE_PATH must be a plain absolute URL path")
        self.origin = public_origin(origin)
        self.secure = self.origin.startswith("https://")
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.data_dir, 0o700)
        self.credentials = Credentials(self.data_dir)
        self.shared_login_codes = bootstrap.shared_login_codes(self.credentials)
        self.db_path = self.data_dir / "event.sqlite3"
        self.connections = database.ConnectionPool(self.db_path)
        self.database_maintenance = database.DatabaseMaintenance(self)
        self.rate_lock = threading.Lock()
        self.login_attempts = OrderedDict()
        self.login_kdf_slots = threading.BoundedSemaphore(LOGIN_KDF_CONCURRENCY)
        self.judging=JudgingService(clock=self.clock,text=text_field,audit=self.audit,touch=self.touch,
            history=lambda *args,**kwargs:self.admin_data.record_review_change(*args,**kwargs),
            enrich_team=lambda db,team:self.admin_data.enrich_team(db,team),
            is_team_recycled=lambda db,team_id:self.team_lifecycle.is_recycled(db,team_id))
        self.admin_data=AdminDataService(self)
        self.team_lifecycle=TeamLifecycle(self)
        self.admin_team_access=AdminTeamAccess(self,COOKIE_NAME)
        self.claims=Claims(self,self.clock,text_field,scrypt_hash)
        self.registration=TeamRegistration(self,self.clock,text_field)
        self.submission_records=SubmissionRecords(self)
        self.material_uploads=MaterialUploads(self)
        self.repositories=Repositories(self)
        self.repository_backups=RepositoryBackups(self)
        self.write_conflicts=WriteConflicts(self)
        self.finals=FinalsService(self,self.clock,text_field)
        # Both service processes may boot together. Serialize private-key provisioning,
        # historical snapshots and SQLite schema changes before accepting traffic.
        with database.startup_lock(self.data_dir):
            database_existed = self.db_path.exists()
            previous_version = migrations.inspect_existing(self.db_path)
            self.roadshow_access=RoadshowAccess(self,self.clock,text_field,scrypt_hash)
            self.roadshow_scoring=RoadshowScoring(self,self.clock,text_field)
            self.judge_login_schedule=JudgeLoginSchedule(self,self.clock)
            if previous_version == 0:
                self.write_conflicts.backup_before_upgrade()
                self.admin_data.backup_before_upgrade()
            self.keys=KeyService(self, self.credentials.read_json("gateway.json"), text_field)
            self.csv_safe=csv_safe
            bootstrap.initialize_state(self, database_existed=database_existed, previous_version=previous_version, hash_code=scrypt_hash)

    def db(self):
        return self.connections.connection()

    def teams(self, db, public=True):
        return self.standings.teams(db, public)

    def check_materials(self, materials, submitted, lean_gitee=False):
        return self.submissions.check_materials(materials, submitted, lean_gitee)

    def save_submission(self, db, actor, team, body, accepted_at=None):
        return self.submissions.save_submission(db, actor, team, body, accepted_at)

    def dispatch_submission(self, body, cookie, expected_login_id=None, server_received_at=None):
        return self.submissions.dispatch_submission(body, cookie, expected_login_id, server_received_at)

    def dispatch(self, method, path, body, cookie, ip, query=None, expected_login_id=None,
                 server_received_at=None, finals_judge_id=None, finals_access_token=None):
        endpoint = resolve_endpoint(method, path)
        return self.dispatch_endpoint(endpoint, path, body, cookie, ip, query, expected_login_id,
                                      server_received_at, finals_judge_id, finals_access_token)

    def dispatch_endpoint(self, endpoint, path, body, cookie, ip, query=None, expected_login_id=None,
                          server_received_at=None, finals_judge_id=None, finals_access_token=None):
        validate_unicode(body)
        if endpoint.method == "POST" and not isinstance(body, dict):
            raise APIError(400, "JSON内容必须是对象")
        command = api.Command(endpoint, path, body, cookie, ip, query or {}, expected_login_id,
                              server_received_at, finals_judge_id, finals_access_token)
        return endpoint.controller(self, command)

    def config(self, db):
        result = {r["key"]: json.loads(r["value"]) for r in db.execute("SELECT * FROM settings") if not r["key"].startswith("keys.")}
        result.update(dimensions=DIMENSIONS, voteLimit=VOTE_LIMIT, audienceIncludedInScore=False, maxScore=100)
        return result

    def voting_window(self, db):
        config=self.config(db);current=self.clock()
        return {'open':config['votingOpen'],'deadline':config['votingDeadline'],
                'canEdit':config['votingOpen'] and (config['votingDeadline'] is None or current<config['votingDeadline']),'serverTime':current}

    def credential_action(self, db, actor, body: inputs.ManageCredential):
        if not inputs.accepts_fields(body, inputs.ManageCredential) or not isinstance(body['role'],str) or body['role'] not in {'judge','audience'} \
                or not isinstance(body['action'],str) or body['action'] not in {'rotate','revoke'} or not isinstance(body['id'],str):
            raise APIError(400,'凭据操作目标不正确')
        target=db.execute('SELECT * FROM accounts WHERE id=? AND role=?',(body['id'],body['role'])).fetchone()
        if not target:raise APIError(404,'身份不存在')
        before={'active':bool(target['active']),'credentialMode':self.admin_data.credential_mode(db,target['id'],target['role'])}
        result={'ok':True,'id':target['id'],'active':body['action']=='rotate'}
        if body['action']=='rotate':
            code=target['id']+'.'+secrets.token_urlsafe(32);salt=secrets.token_bytes(16)
            db.execute('UPDATE accounts SET salt=?,code_hash=?,active=1 WHERE id=?',(salt,scrypt_hash(code,salt),target['id']))
            db.execute('INSERT INTO credential_overrides VALUES (?,?) ON CONFLICT(account_id) DO UPDATE SET rotated_at=excluded.rotated_at',(target['id'],self.clock()))
            result['code']=code
        else:db.execute('UPDATE accounts SET active=0 WHERE id=?',(target['id'],))
        db.execute('DELETE FROM sessions WHERE account_id=?',(target['id'],))
        result['credentialMode']=self.admin_data.credential_mode(db,target['id'],target['role'])
        self.audit(db,actor,'credential.'+body['action'],target['id'],{'role':target['role'],'before':before,'after':{key:result[key] for key in ('active','credentialMode')}})
        return result

    def touch(self, db):
        db.execute("UPDATE settings SET value=? WHERE key='updatedAt'", (json.dumps(self.clock()),))

    def audit(self, db, actor, action, target="", metadata=None):
        operator = operator_for(actor)
        if operator:
            metadata = {**(metadata or {}), 'operator': operator}
        db.execute("INSERT INTO audit(at,actor_id,role,action,target,metadata) VALUES (?,?,?,?,?,?)",
                   (self.clock(), actor["id"] if actor else None, 'admin' if operator else actor["role"] if actor else None,
                    action, target, json.dumps(metadata or {}, ensure_ascii=False)))

    def create_account(self, db, role, name):
        account_id = {"admin": "GA", "judge": "GJ", "audience": "GV", "team": "GT"}[role] + secrets.token_hex(8)
        code = self.shared_login_codes.get(role) or account_id + "." + secrets.token_urlsafe(32)
        salt = secrets.token_bytes(16)
        db.execute("INSERT INTO accounts(id,role,name,salt,code_hash,created_at) VALUES (?,?,?,?,?,?)",
                   (account_id, role, name, salt, scrypt_hash(code, salt), self.clock()))
        return {"id": account_id, "role": role, "name": name}, code

    def reserve_attempts(self, keys):
        now = time.monotonic()
        ticket = (now, object())
        with self.rate_lock:
            buckets = []
            for key in keys:
                bucket = self.login_attempts.get(key)
                if bucket is None:
                    bucket = deque()
                while bucket and bucket[0][0] <= now - 60:
                    bucket.popleft()
                if len(bucket) >= 20:
                    raise APIError(429, "登录尝试过于频繁，请一分钟后重试")
                buckets.append(bucket)
            for key, bucket in zip(keys, buckets):
                bucket.append(ticket)
                self.login_attempts[key] = bucket
                self.login_attempts.move_to_end(key)
            while len(self.login_attempts) > 8192:
                self.login_attempts.popitem(last=False)
        return ticket, buckets

    def rate_limit(self, ip, scope):
        # Public invitation and registration actions retain their own IP budgets.
        self.reserve_attempts([("public", scope, ip)])

    @contextmanager
    def login_attempt(self, ip, role, selector):
        # Count simultaneous attempts immediately. Only unsuccessful credentials
        # remain charged, so participants sharing an IP can sign in normally.
        digest = hashlib.sha256(selector.encode()).digest()
        ticket, buckets = self.reserve_attempts([("login-ip", ip), ("login-account", role, digest)])
        try:
            yield
        except APIError as error:
            if error.status == 401:
                raise
            self.refund_attempt(ticket, buckets)
            raise
        except BaseException:
            self.refund_attempt(ticket, buckets)
            raise
        else:
            self.refund_attempt(ticket, buckets)

    def refund_attempt(self, ticket, buckets):
        with self.rate_lock:
            for bucket in buckets:
                if ticket in bucket:
                    bucket.remove(ticket)

    @staticmethod
    def login_id(cookie):
        """Public, non-bearer identity; it cannot be used in place of the HttpOnly token."""
        try:
            token = SimpleCookie(cookie or "")[COOKIE_NAME].value
            if re.fullmatch(r"[A-Za-z0-9_-]{40,100}", token):
                return hashlib.sha256(b"gotc-public-login-id\0" + token.encode()).hexdigest()
        except (KeyError, ValueError, CookieError):
            pass
        return None

    def account(self, db, cookie):
        try:
            cookies = SimpleCookie()
            cookies.load(cookie or "")
            token = cookies[COOKIE_NAME].value
            if not re.fullmatch(r"[A-Za-z0-9_-]{40,100}", token):
                return None
        except (KeyError, ValueError, CookieError):
            return None
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        # Read identity and delegation in one SQLite snapshot. A concurrent return
        # must never make an old delegated cookie appear to be an unrestricted admin.
        row = db.execute('''SELECT a.*,c.team_id AS delegated_team_id,t.name AS delegated_team_name
            FROM sessions s JOIN accounts a ON a.id=s.account_id
            LEFT JOIN admin_team_sessions c ON c.token_hash=s.token_hash
            LEFT JOIN teams t ON t.id=c.team_id
            WHERE s.token_hash=? AND s.expires_at>? AND a.active=1''', (token_hash, self.clock())).fetchone()
        if row is None:
            return None
        actor = dict(row)
        team_id, team_name = actor.pop('delegated_team_id'), actor.pop('delegated_team_name')
        context = {'team_id':team_id,'name':team_name} if team_id else None
        return self.admin_team_access.effective_account(actor,context)

    def require(self, actor, role):
        if actor is None:
            raise APIError(401, "请先登录")
        if actor["role"] != role:
            raise APIError(403, "当前身份无权访问")

    def write_identity(self, db, cookie, role, marker, *, marker_required=False):
        actor = self.account(db, cookie)
        self.require(actor, role)
        if marker_required and not marker:
            raise APIError(428, '请刷新页面并核对登录身份', 'login_required')
        if marker is not None and marker != self.login_id(cookie):
            raise APIError(401, '登录身份已变化，请重新登录')
        return actor

    def issue_session(self, db, account):
        """Issue a session only after the caller authenticates inside its write transaction."""
        if account['role'] == 'judge' and self.roadshow_access.owns_account(db, account['id']):
            self.judge_login_schedule.require_open(db, 'roadshow')
        token = secrets.token_urlsafe(32)
        db.execute("DELETE FROM sessions WHERE expires_at<=?", (self.clock(),))
        db.execute("INSERT INTO sessions VALUES (?,?,?)", (hashlib.sha256(token.encode()).hexdigest(), account["id"], self.clock() + SESSION_SECONDS * 1000))
        self.audit(db, account, "login")
        return {"ok": True, **self.admin_team_access.session_view(account, f"{COOKIE_NAME}={token}")}, token, "set"

    def login(self, body: inputs.LoginBody, ip):
        """Verify outside the writer lock, then recheck the proof before issuing a session."""
        role = body.get("role")
        if not isinstance(role, str) or role not in {"admin", "judge", "audience", "team"}:
            raise APIError(400, "登录身份不正确")
        code = text_field(body.get("code"), "邀请码", 160, True)
        selector = body.get("accountId")
        if selector is not None and (not isinstance(selector, str) or len(selector) > 80):
            raise APIError(400, "登录身份不正确")
        named = admin_auth.username(body.get("username")) if role == "admin" else None
        claimed = named or selector or code.split(".", 1)[0]
        with self.login_attempt(ip, role, claimed):
            return self.verify_login(role, code, selector, named, body.get("totp"))

    def verify_login(self, role, code, selector, named, otp):
        with self.db() as db:
            named_mode = role == "admin" and db.execute("SELECT 1 FROM named_admins LIMIT 1").fetchone()
            if named_mode:
                account = db.execute("""SELECT a.*, n.totp_seed, n.last_step FROM named_admins n
                    JOIN accounts a ON a.id=n.account_id
                    WHERE n.username=? AND a.role='admin' AND a.active=1""", (named,)).fetchone()
            else:
                if named is not None or otp is not None:
                    raise APIError(401, "管理员账号或验证码无效")
                shared_code = self.shared_login_codes.get(role)
                if selector is None and shared_code and hmac.compare_digest(code.encode(), shared_code.encode()):
                    candidates = db.execute("""SELECT id,name FROM accounts WHERE role=? AND active=1
                        AND id NOT IN (SELECT account_id FROM credential_overrides) ORDER BY created_at,id""",
                        (role,)).fetchall()
                    if len(candidates) > 1:
                        return {"selectAccount": True, "accounts": [dict(a) for a in candidates]}, None, None
                    if candidates:
                        selector = candidates[0]["id"]
                selector = selector or code.split(".", 1)[0]
                account = db.execute("SELECT * FROM accounts WHERE id=? AND role=? AND active=1", (selector, role)).fetchone()
        salt = account["salt"] if account else b"gotc-dummy-salt!"
        if not self.login_kdf_slots.acquire(blocking=False):
            raise APIError(429, "登录繁忙，请稍后重试", "login_busy")
        try:
            hashed = scrypt_hash(code, salt)
        finally:
            self.login_kdf_slots.release()
        if not account or not hmac.compare_digest(hashed, account["code_hash"]):
            raise APIError(401, "邀请码无效或已停用")
        if named_mode and admin_auth.verified_step(account['totp_seed'], otp, self.clock(), account['last_step']) is None:
            raise APIError(401, "管理员账号或验证码无效")
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT * FROM accounts WHERE id=? AND role=? AND active=1", (account["id"], role)).fetchone()
            if not current or not hmac.compare_digest(current["salt"], account["salt"]) or not hmac.compare_digest(current["code_hash"], account["code_hash"]):
                raise APIError(401, "邀请码无效或已停用")
            if named_mode:
                credential = db.execute("SELECT totp_seed,last_step FROM named_admins WHERE account_id=? AND username=?",
                                        (current['id'], named)).fetchone()
                step = (admin_auth.verified_step(credential['totp_seed'], otp, self.clock(), credential['last_step'])
                        if credential else None)
                if step is None:
                    raise APIError(401, "管理员账号或验证码无效")
                db.execute("UPDATE named_admins SET last_step=? WHERE account_id=?", (step, current['id']))
            return self.issue_session(db, current)

    def bound_submission_team(self,db,actor):
        self.require(actor,'team')
        operator=operator_for(actor)
        if operator:
            team=db.execute('SELECT * FROM teams WHERE id=?',(operator['teamId'],)).fetchone()
            if db.execute("SELECT 1 FROM team_details WHERE team_id=? AND status!='active'",(operator['teamId'],)).fetchone():
                raise APIError(409,'该队伍已退出或被取消资格，不能继续代管','team_ineligible')
        else:
            team=db.execute('SELECT t.* FROM teams t JOIN team_accounts b ON b.team_id=t.id WHERE b.account_id=?',(actor['id'],)).fetchone()
        if not team:raise APIError(403,'本账号未绑定队伍，请联系组织方')
        self.team_lifecycle.require_available(db,team['id'])
        return team

    def team_access(self, db, actor, body: inputs.ManageTeamAccess):
        if not inputs.accepts_fields(body, inputs.ManageTeamAccess) or (not isinstance(body.get("action"), str) or body["action"] not in {"issue", "rotate", "revoke"}):
            raise APIError(400, "提交码操作不正确")
        team_id=self.team_exists(db, body["teamId"])
        action=body["action"]
        if action!='revoke':self.team_lifecycle.require_available(db,team_id)
        account=db.execute("SELECT a.* FROM accounts a JOIN team_accounts t ON t.account_id=a.id WHERE t.team_id=?", (team_id,)).fetchone()
        before={'id':account['id'],'active':bool(account['active'])} if account else None
        if action=="issue":
            if account:
                raise APIError(409, "该队已有提交身份，请重发提交码")
            name=db.execute("SELECT name FROM teams WHERE id=?", (team_id,)).fetchone()[0]
            account,code=self.create_account(db, "team", name)
            db.execute("INSERT INTO team_accounts VALUES (?,?)", (account["id"], team_id))
        else:
            if not account:
                raise APIError(404, "该队尚未创建提交身份")
            db.execute("DELETE FROM sessions WHERE account_id=?", (account["id"],))
            if action=="rotate":
                code=account["id"]+"."+secrets.token_urlsafe(32)
                salt=secrets.token_bytes(16)
                db.execute("UPDATE accounts SET salt=?,code_hash=?,active=1 WHERE id=?", (salt, scrypt_hash(code, salt), account["id"]))
            else:
                db.execute("UPDATE accounts SET active=0 WHERE id=?", (account["id"],))
        self.audit(db, actor, "team-access."+action, team_id,{'before':before,'after':{'id':account['id'],'active':action!='revoke'}})
        result={"ok": True, "teamId": team_id, "id": account["id"], "active": action!="revoke"}
        if action!="revoke":
            result["code"]=code
        return result

    def team_exists(self, db, team_id, *, require_active=False):
        if not isinstance(team_id, str) or len(team_id) > 80:
            raise APIError(400, "队伍编号不正确")
        if not db.execute("SELECT 1 FROM teams WHERE id=?", (team_id,)).fetchone():
            raise APIError(404, "队伍不存在")
        if require_active and db.execute("SELECT 1 FROM team_details WHERE team_id=? AND status!='active'", (team_id,)).fetchone():
            raise APIError(409, "该队伍已退出或被取消资格，不能继续评分或投票", "team_ineligible")
        return team_id
