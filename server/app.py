"""GOTC single-instance event service. Python 3.11+, standard library only."""
from __future__ import annotations

import csv
from contextlib import closing, contextmanager
import hashlib
import hmac
import io
import ipaddress
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
from collections import defaultdict, deque
from decimal import Decimal, InvalidOperation
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit, parse_qs
from errors import APIError
from judging import JudgingService
from admin_data import AdminDataService, TRACK_OPTIONS

DIMENSIONS = [
    {"id": "problem", "name": "问题价值与理解", "max": 20},
    {"id": "ai", "name": "方案与AI使用", "max": 20},
    {"id": "prototype", "name": "原型完成度", "max": 20},
    {"id": "testing", "name": "测试与改进", "max": 15},
    {"id": "opensource", "name": "开源与责任", "max": 15},
    {"id": "teamwork", "name": "团队协作与表达", "max": 10},
]
BODY_LIMIT = 16384
SUBMISSION_BODY_LIMIT = 32768
IMPORT_BODY_LIMIT = 2 * 1024 * 1024
SESSION_SECONDS = 12 * 3600
# Bound password-check CPU/memory without holding the database writer lock.
LOGIN_KDF_CONCURRENCY = 4
VOTE_LIMIT = 3
COOKIE_NAME = "gotc_session"
STATIC_FILES = {"index.html", "styles.css", "app.js", "platform.html", "platform.js", "platform-admin.js", "platform.css", "favicon.svg"}
ASSET_EXTENSIONS = {".webp", ".png", ".jpg", ".jpeg", ".svg", ".ico", ".woff", ".woff2"}
DOWNLOAD_EXTENSIONS = {".ics", ".txt"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
 id TEXT PRIMARY KEY, role TEXT NOT NULL CHECK(role IN ('admin','judge','audience','team')),
 name TEXT NOT NULL, salt BLOB NOT NULL, code_hash BLOB NOT NULL,
 active INTEGER NOT NULL DEFAULT 1, created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
 token_hash TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id), expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_expiry ON sessions(expires_at);
CREATE TABLE IF NOT EXISTS teams (
 id TEXT PRIMARY KEY, number INTEGER NOT NULL UNIQUE, name TEXT NOT NULL,
 project TEXT NOT NULL DEFAULT '', track TEXT NOT NULL DEFAULT '', updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS team_accounts (
 account_id TEXT NOT NULL PRIMARY KEY REFERENCES accounts(id), team_id TEXT NOT NULL UNIQUE REFERENCES teams(id)
);
CREATE TABLE IF NOT EXISTS submissions (
 team_id TEXT PRIMARY KEY REFERENCES teams(id), version INTEGER NOT NULL CHECK(version>=1),
 draft_json TEXT NOT NULL, submitted_json TEXT, submitted_version INTEGER,
 first_submitted_at INTEGER, submitted_at INTEGER, updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS scores (
 judge_id TEXT NOT NULL REFERENCES accounts(id), team_id TEXT NOT NULL REFERENCES teams(id),
 scores_json TEXT NOT NULL, total_tenths INTEGER, feedback TEXT NOT NULL DEFAULT '',
 status TEXT NOT NULL CHECK(status IN ('draft','submitted','recused')), updated_at INTEGER NOT NULL,
 PRIMARY KEY(judge_id,team_id)
);
CREATE TABLE IF NOT EXISTS votes (
 account_id TEXT NOT NULL REFERENCES accounts(id), team_id TEXT NOT NULL REFERENCES teams(id), created_at INTEGER NOT NULL,
 PRIMARY KEY(account_id,team_id)
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit (
 id INTEGER PRIMARY KEY AUTOINCREMENT, at INTEGER NOT NULL, actor_id TEXT, role TEXT,
 action TEXT NOT NULL, target TEXT NOT NULL DEFAULT '', metadata TEXT NOT NULL DEFAULT '{}'
);
"""


def now_ms():
    return int(time.time() * 1000)


def text_field(value, field, limit, required=False):
    if not isinstance(value, str):
        raise APIError(400, f"{field}必须是文字")
    value = value.strip()
    if len(value) > limit or (required and not value) or any(ord(c) < 32 and c not in "\n\t" for c in value):
        raise APIError(400, f"{field}长度或内容不正确")
    return value


def valid_material_host(host):
    if not host:
        return False
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    if re.fullmatch(r"[0-9.]+", host):
        return False
    try:
        domain=host.rstrip(".").encode("idna").decode("ascii")
    except UnicodeError:
        return False
    return len(domain)<=253 and all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label, re.I) for label in domain.split("."))


def repository_parts(value, commit=False):
    """Parse supported public-host URL shapes only; never contact the host."""
    try:
        url=urlsplit(value)
        host=url.hostname
        if (url.scheme!='https' or host not in {'github.com','gitee.com','gitlab.com'} or url.username is not None
                or url.password is not None or url.netloc.lower()!=host or url.port is not None or url.query or url.fragment
                or re.search(r'[\s\\%]',value) or len(value.encode('utf-8'))>1024):
            return None
        parts=url.path.rstrip('/').split('/')[1:]
        sha=None
        if commit:
            tail=3 if host=='gitlab.com' else 2
            if len(parts)<tail+2 or parts[-tail:-1]!=(['-','commit'] if host=='gitlab.com' else ['commit']):
                return None
            sha=parts[-1]
            if not re.fullmatch(r'(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})',sha):
                return None
            parts=parts[:-tail]
        if not parts or not parts[-1]:
            return None
        parts[-1]=re.sub(r'\.git$','',parts[-1])
        # GitHub repository names may start with a dot; owners and other hosts keep their existing grammar.
        if len(parts)<2 or (host!='gitlab.com' and len(parts)!=2) or any(not re.fullmatch(r'[A-Za-z0-9_.][A-Za-z0-9_.-]*' if host=='github.com' and index==len(parts)-1 else r'[A-Za-z0-9_][A-Za-z0-9_.-]*',part) or part in {'.','..'} for index,part in enumerate(parts)):
            return None
        # GitHub/Gitee names are case-insensitive; GitLab subgroup paths are kept exact.
        path='/'.join(parts)
        return host,path if host=='gitlab.com' else path.lower(),sha.lower() if sha else None
    except (ValueError,TypeError):
        return None


def normalize_code_version(repository, version):
    try:
        url=urlsplit(repository)
        valid=(url.scheme=='https' and valid_material_host(url.hostname) and url.username is None and url.password is None
               and url.port!=0 and len(repository.encode('utf-8'))<=1024
               and not re.search(r'[\s\\\x00-\x1f\x7f]',repository)
               and not re.search(r'%(?:0[0-9a-f]|1[0-9a-f]|7f)',repository,re.I))
    except (ValueError,TypeError):
        valid=False
    repo=repository_parts(repository)
    if not valid or (url.hostname in {'github.com','gitee.com','gitlab.com'} and not repo):
        raise APIError(400,'仓库请填写完整 HTTPS 仓库主页链接，不是本地路径或文件页面；GitHub、Gitee、GitLab 请使用仓库根地址')
    if re.fullmatch(r'(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})',version):
        return version.lower()
    commit=repository_parts(version,commit=True)
    if not repo or not commit or commit[:2]!=repo[:2]:
        raise APIError(400,'commit 请填写同一 GitHub、Gitee 或 GitLab 仓库的完整提交页面链接，或40/64位完整哈希；其他平台请填完整哈希，不要填写分支、tag或前6位')
    return commit[2]


def scrypt_hash(code, salt):
    return hashlib.scrypt(code.encode(), salt=salt, n=16384, r=8, p=1, dklen=32)


def csv_safe(value):
    value = "" if value is None else str(value)
    if value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")) or value.startswith(("\t", "\r", "\n")):
        return "'" + value
    return value


class EventService:
    def __init__(self, data_dir=None, base_path=None, origin=None, static_root=None):
        self.root = Path(static_root or Path(__file__).resolve().parent.parent).resolve()
        self.data_dir = Path(data_dir or os.environ.get("GOTC_DATA_DIR", str(Path(__file__).parent / "data"))).resolve()
        self.base_path = (base_path if base_path is not None else os.environ.get("GOTC_BASE_PATH", "/gotc")).rstrip("/")
        if self.base_path and not re.fullmatch(r"/[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*", self.base_path):
            raise ValueError("GOTC_BASE_PATH must be a plain absolute URL path")
        self.origin = (origin or os.environ.get("GOTC_PUBLIC_ORIGIN", "https://8.141.114.201")).rstrip("/")
        parsed = urlsplit(self.origin)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise ValueError("GOTC_PUBLIC_ORIGIN must be an exact http(s) origin without a path")
        self.secure = parsed.scheme == "https"
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.data_dir, 0o700)
        shared_file = self.data_dir / "shared-login-codes.json"
        self.shared_login_codes = json.loads(shared_file.read_text(encoding="utf-8")) if shared_file.exists() else {}
        if not isinstance(self.shared_login_codes, dict) or not set(self.shared_login_codes).issubset({"admin", "judge"}):
            raise ValueError("Shared login configuration supports admin and judge roles only")
        for value in self.shared_login_codes.values():
            if not isinstance(value, str) or not 6 <= len(value) <= 160 or value != value.strip():
                raise ValueError("Shared login codes must contain 6 to 160 characters")
        self.db_path = self.data_dir / "event.sqlite3"
        self.rate_lock = threading.Lock()
        self.login_attempts = defaultdict(deque)
        self.login_kdf_slots = threading.BoundedSemaphore(LOGIN_KDF_CONCURRENCY)
        self.judging=JudgingService(clock=lambda:now_ms(),text=text_field,audit=self.audit,touch=self.touch,
            history=lambda *args,**kwargs:self.admin_data.record_review_change(*args,**kwargs),
            enrich_team=lambda db,team:self.admin_data.enrich_team(db,team))
        self.admin_data=AdminDataService(self)
        self.admin_data.backup_before_upgrade()
        self.migrate_accounts()
        with self.db() as db:
            db.executescript(SCHEMA)
            self.judging.initialize(db)
            self.admin_data.initialize(db)
            for key, value in {"scoringOpen": False, "resultsPublished": True, "minJudges": 5, "submissionsOpen": False, "submissionDeadline": None, "updatedAt": now_ms()}.items():
                db.execute("INSERT OR IGNORE INTO settings VALUES (?,?)", (key, json.dumps(value)))
            # Preserve an upgraded event's current audience window once; later changes are independent.
            db.execute("INSERT OR IGNORE INTO settings SELECT 'votingOpen',value FROM settings WHERE key='scoringOpen'")
            db.execute("INSERT OR IGNORE INTO settings VALUES ('votingDeadline','null')")
            if not db.execute("SELECT 1 FROM accounts WHERE role='admin'").fetchone():
                account, code = self.create_account(db, "admin", "活动管理员")
                # Exclusive creation prevents silently overwriting an earlier credential file.
                access_file = self.data_dir / "admin-access.txt"
                fd = os.open(access_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    stream.write("GOTC administrator access code (keep private):\n" + code + "\n")
                os.chmod(access_file, 0o600)
                self.audit(db, account, "bootstrap")
            for role, code in self.shared_login_codes.items():
                for account in db.execute("SELECT * FROM accounts WHERE role=? AND id NOT IN (SELECT account_id FROM credential_overrides)", (role,)).fetchall():
                    if not hmac.compare_digest(scrypt_hash(code, account["salt"]), account["code_hash"]):
                        salt = secrets.token_bytes(16)
                        db.execute("UPDATE accounts SET salt=?,code_hash=? WHERE id=?", (salt, scrypt_hash(code, salt), account["id"]))
                        db.execute("DELETE FROM sessions WHERE account_id=?", (account["id"],))
                        self.audit(db, account, "credential.shared-code-updated")
            if "judge" in self.shared_login_codes and not db.execute("SELECT 1 FROM accounts WHERE role='judge'").fetchone():
                account, _ = self.create_account(db, "judge", "评委01")
                self.audit(db, account, "bootstrap.shared-judge")
        if "admin" in self.shared_login_codes:
            access_file = self.data_dir / "admin-access.txt"
            temporary = self.data_dir / (".admin-access-" + secrets.token_hex(8))
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write("GOTC administrator access code (keep private):\n" + self.shared_login_codes["admin"] + "\n")
            os.replace(temporary, access_file)
            os.chmod(access_file, 0o600)
        os.chmod(self.db_path, 0o600)

    def migrate_accounts(self):
        """Rebuild only the legacy role CHECK; preserve IDs and referencing tables."""
        if not self.db_path.exists():
            return
        db=sqlite3.connect(self.db_path, timeout=10)
        try:
            table=db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='accounts'").fetchone()
            if not table or "'team'" in table[0]:
                return
            backup_path=self.data_dir/("before-team-"+str(now_ms())+"-"+secrets.token_hex(4)+".sqlite3")
            with closing(sqlite3.connect(backup_path)) as backup:
                db.backup(backup)
            os.chmod(backup_path, 0o600)
            db.execute("PRAGMA foreign_keys=OFF")
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute("""CREATE TABLE accounts_new (
                  id TEXT PRIMARY KEY, role TEXT NOT NULL CHECK(role IN ('admin','judge','audience','team')),
                  name TEXT NOT NULL, salt BLOB NOT NULL, code_hash BLOB NOT NULL,
                  active INTEGER NOT NULL DEFAULT 1, created_at INTEGER NOT NULL)""")
                db.execute("INSERT INTO accounts_new SELECT id,role,name,salt,code_hash,active,created_at FROM accounts")
                db.execute("DROP TABLE accounts")
                db.execute("ALTER TABLE accounts_new RENAME TO accounts")
                if db.execute("PRAGMA foreign_key_check").fetchall():
                    raise RuntimeError("Account migration failed foreign-key validation")
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                db.execute("PRAGMA foreign_keys=ON")
        finally:
            db.close()

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.db_path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=10000")
        try:
            with db:
                yield db
        finally:
            db.close()

    def config(self, db):
        result = {r["key"]: json.loads(r["value"]) for r in db.execute("SELECT * FROM settings")}
        result.update(dimensions=DIMENSIONS, voteLimit=VOTE_LIMIT, audienceIncludedInScore=False, maxScore=100)
        return result

    def voting_window(self, db):
        config=self.config(db);current=now_ms()
        return {'open':config['votingOpen'],'deadline':config['votingDeadline'],
                'canEdit':config['votingOpen'] and (config['votingDeadline'] is None or current<config['votingDeadline']),'serverTime':current}

    def credential_action(self, db, actor, body):
        if set(body)!={'role','id','action'} or not isinstance(body['role'],str) or body['role'] not in {'judge','audience'} \
                or not isinstance(body['action'],str) or body['action'] not in {'rotate','revoke'} or not isinstance(body['id'],str):
            raise APIError(400,'凭据操作目标不正确')
        target=db.execute('SELECT * FROM accounts WHERE id=? AND role=?',(body['id'],body['role'])).fetchone()
        if not target:raise APIError(404,'身份不存在')
        before={'active':bool(target['active']),'credentialMode':self.admin_data.credential_mode(db,target['id'],target['role'])}
        result={'ok':True,'id':target['id'],'active':body['action']=='rotate'}
        if body['action']=='rotate':
            code=target['id']+'.'+secrets.token_urlsafe(32);salt=secrets.token_bytes(16)
            db.execute('UPDATE accounts SET salt=?,code_hash=?,active=1 WHERE id=?',(salt,scrypt_hash(code,salt),target['id']))
            db.execute('INSERT INTO credential_overrides VALUES (?,?) ON CONFLICT(account_id) DO UPDATE SET rotated_at=excluded.rotated_at',(target['id'],now_ms()))
            result['code']=code
        else:db.execute('UPDATE accounts SET active=0 WHERE id=?',(target['id'],))
        db.execute('DELETE FROM sessions WHERE account_id=?',(target['id'],))
        result['credentialMode']=self.admin_data.credential_mode(db,target['id'],target['role'])
        self.audit(db,actor,'credential.'+body['action'],target['id'],{'role':target['role'],'before':before,'after':{key:result[key] for key in ('active','credentialMode')}})
        return result

    def touch(self, db):
        db.execute("UPDATE settings SET value=? WHERE key='updatedAt'", (json.dumps(now_ms()),))

    def audit(self, db, actor, action, target="", metadata=None):
        db.execute("INSERT INTO audit(at,actor_id,role,action,target,metadata) VALUES (?,?,?,?,?,?)",
                   (now_ms(), actor["id"] if actor else None, actor["role"] if actor else None,
                    action, target, json.dumps(metadata or {}, ensure_ascii=False)))

    def create_account(self, db, role, name):
        account_id = {"admin": "GA", "judge": "GJ", "audience": "GV", "team": "GT"}[role] + secrets.token_hex(8)
        code = self.shared_login_codes.get(role) or account_id + "." + secrets.token_urlsafe(32)
        salt = secrets.token_bytes(16)
        db.execute("INSERT INTO accounts(id,role,name,salt,code_hash,created_at) VALUES (?,?,?,?,?,?)",
                   (account_id, role, name, salt, scrypt_hash(code, salt), now_ms()))
        return {"id": account_id, "role": role, "name": name}, code

    def rate_limit(self, ip, role):
        current = time.monotonic()
        with self.rate_lock:
            # Nginx's trusted X-Real-IP is used only for loopback peers.
            for key in [("ip", ip, role), ("global", role)]:
                bucket = self.login_attempts[key]
                while bucket and bucket[0] < current - 60:
                    bucket.popleft()
                limit = 20 if key[0] == "ip" else 400
                if len(bucket) >= limit:
                    raise APIError(429, "登录尝试过于频繁，请一分钟后重试")
            for key in [("ip", ip, role), ("global", role)]:
                self.login_attempts[key].append(current)
            if len(self.login_attempts) > 5000:
                self.login_attempts = defaultdict(deque, {k: v for k, v in self.login_attempts.items() if v and v[-1] >= current - 60})

    def account(self, db, cookie):
        try:
            cookies = SimpleCookie()
            cookies.load(cookie or "")
            token = cookies[COOKIE_NAME].value
            if not re.fullmatch(r"[A-Za-z0-9_-]{40,100}", token):
                return None
        except (KeyError, ValueError, CookieError):
            return None
        return db.execute("SELECT a.* FROM sessions s JOIN accounts a ON a.id=s.account_id WHERE s.token_hash=? AND s.expires_at>? AND a.active=1",
                          (hashlib.sha256(token.encode()).hexdigest(), now_ms())).fetchone()

    def teams(self, db, public=True):
        config = self.config(db)
        rows = db.execute("""SELECT t.*, COUNT(s.judge_id) AS judge_count, AVG(s.total_tenths)/10.0 AS average,
          (SELECT COUNT(*) FROM votes v WHERE v.team_id=t.id) AS audience_votes
          FROM teams t LEFT JOIN scores s ON s.team_id=t.id AND s.status='submitted'
          GROUP BY t.id ORDER BY t.number""").fetchall()
        result = [{"id": r["id"], "number": r["number"], "name": r["name"], "project": r["project"], "track": r["track"],
                   "judgeCount": r["judge_count"],
                   "score": round(r["average"], 2) if r["average"] is not None and r["judge_count"] >= config["minJudges"] and (not public or config["resultsPublished"]) else None,
                   "rank": None, "audienceVotes": r["audience_votes"]} for r in rows]
        ranked = sorted((r for r in result if r["score"] is not None), key=lambda r: (-r["score"], r["number"]))
        previous = None
        rank = None
        for position, row in enumerate(ranked, 1):
            if row["score"] != previous:
                rank = position
            row["rank"] = rank
            previous = row["score"]
        return result if public else [self.admin_data.enrich_team(db,team) for team in result]

    def require(self, actor, role):
        if actor is None:
            raise APIError(401, "请先登录")
        if actor["role"] != role:
            raise APIError(403, "当前身份无权访问")

    def login(self, body, ip):
        """Verify outside the writer lock, then recheck the proof before issuing a session."""
        role = body.get("role")
        if not isinstance(role, str) or role not in {"admin", "judge", "audience", "team"}:
            raise APIError(400, "登录身份不正确")
        self.rate_limit(ip, role)
        code = text_field(body.get("code"), "邀请码", 160, True)
        selector = body.get("accountId")
        if selector is not None and (not isinstance(selector, str) or len(selector) > 80):
            raise APIError(400, "登录身份不正确")
        with self.db() as db:
            shared_code = self.shared_login_codes.get(role)
            if selector is None and shared_code and hmac.compare_digest(code.encode(), shared_code.encode()):
                candidates = db.execute("SELECT id,name FROM accounts WHERE role=? AND active=1 AND id NOT IN (SELECT account_id FROM credential_overrides) ORDER BY created_at,id", (role,)).fetchall()
                if len(candidates) > 1:
                    return {"selectAccount": True, "accounts": [{"id": a["id"], "name": a["name"]} for a in candidates]}, None, None
                if candidates:
                    selector = candidates[0]["id"]
            selector = selector or code.split(".", 1)[0]
            account = db.execute("SELECT * FROM accounts WHERE id=? AND role=? AND active=1", (selector, role)).fetchone()
        salt = account["salt"] if account else b"gotc-dummy-salt!"
        # Waiting for a password-check slot must not reserve a SQLite write transaction.
        with self.login_kdf_slots:
            hashed = scrypt_hash(code, salt)
        if not account or not hmac.compare_digest(hashed, account["code_hash"]):
            raise APIError(401, "邀请码无效或已停用")
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT * FROM accounts WHERE id=? AND role=? AND active=1", (account["id"], role)).fetchone()
            if not current or not hmac.compare_digest(current["salt"], account["salt"]) or not hmac.compare_digest(current["code_hash"], account["code_hash"]):
                raise APIError(401, "邀请码无效或已停用")
            token = secrets.token_urlsafe(32)
            db.execute("DELETE FROM sessions WHERE expires_at<=?", (now_ms(),))
            db.execute("INSERT INTO sessions VALUES (?,?,?)", (hashlib.sha256(token.encode()).hexdigest(), current["id"], now_ms() + SESSION_SECONDS * 1000))
            self.audit(db, current, "login")
            return {"ok": True, "role": role, "name": current["name"]}, token, "set"

    def dispatch(self, method, path, body, cookie, ip, query=None):
        """Return (JSON object or CSV bytes, optional session token, cookie action)."""
        if method == "POST" and path == "/api/login":
            return self.login(body, ip)
        with self.db() as db:
            # Resolve write identities only after the lock: revocation may have committed while queued.
            if method=="POST":
                db.execute("BEGIN IMMEDIATE")
            actor = self.account(db, cookie)
            if path.startswith('/api/admin/data/'):
                parsed_cookie=SimpleCookie()
                try:parsed_cookie.load(cookie)
                except CookieError:pass
                session_token=parsed_cookie[COOKIE_NAME].value if COOKIE_NAME in parsed_cookie else ''
                session_hash=hashlib.sha256(session_token.encode()).hexdigest()
                return self.admin_data.dispatch(db,actor,method,path,body,query or {},session_hash),None,None
            if path.startswith(("/api/judging/","/api/admin/judging/")):
                return self.judging.dispatch(db,actor,method,path,body), None, None
            if method == "GET" and path == "/api/session":
                return {"role": actor["role"] if actor else None, "name": actor["name"] if actor else ""}, None, None
            if method == "GET" and path == "/api/board":
                config = self.config(db)
                return {"teams": self.teams(db), "config": config, "updatedAt": config["updatedAt"], "serverTime": now_ms()}, None, None
            if method == "POST" and path == "/api/logout":
                if actor:
                    try:
                        parsed = SimpleCookie(cookie)
                        token = parsed[COOKIE_NAME].value
                        db.execute("DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),))
                    except (KeyError, ValueError, CookieError):
                        pass
                return {"ok": True}, None, "clear"
            if method == "GET" and path == "/api/judge":
                self.require(actor, "judge")
                scores = [{"teamId": r["team_id"], "scores": json.loads(r["scores_json"]), "feedback": r["feedback"], "status": r["status"]}
                          for r in db.execute("SELECT * FROM scores WHERE judge_id=?", (actor["id"],))]
                return {"teams": self.teams(db), "myScores": scores, "dimensions": DIMENSIONS, "scoringOpen": self.config(db)["scoringOpen"], "name": actor["name"], "submissions": self.judge_submissions(db)}, None, None
            if method == "POST" and path == "/api/score":
                self.require(actor, "judge")
                if not self.config(db)["scoringOpen"]:
                    raise APIError(409, "评分尚未开放或已关闭")
                team_id = self.team_exists(db, body.get("teamId"))
                status = body.get("status")
                if status not in {"draft", "submitted", "recused"}:
                    raise APIError(400, "评分状态不正确")
                previous = db.execute("SELECT status FROM scores WHERE judge_id=? AND team_id=?", (actor["id"], team_id)).fetchone()
                if previous and previous["status"] == "submitted" and status == "draft":
                    raise APIError(409, "已有正式评分，请修改后重新提交；草稿不会覆盖正式评分")
                feedback = text_field(body.get("feedback", ""), "反馈", 2000)
                values, total = [], None
                if status != "recused":
                    values, total = self.check_scores(body.get("scores"))
                db.execute("""INSERT INTO scores VALUES (?,?,?,?,?,?,?) ON CONFLICT(judge_id,team_id) DO UPDATE SET
                  scores_json=excluded.scores_json,total_tenths=excluded.total_tenths,feedback=excluded.feedback,status=excluded.status,updated_at=excluded.updated_at""",
                           (actor["id"], team_id, json.dumps(values), total, feedback, status, now_ms()))
                self.audit(db, actor, "score." + status, team_id)
                self.touch(db)
                return {"ok": True, "status": status}, None, None
            if method == "GET" and path == "/api/audience":
                self.require(actor, "audience")
                voted = [r[0] for r in db.execute("SELECT team_id FROM votes WHERE account_id=? ORDER BY created_at,team_id", (actor["id"],))]
                window=self.voting_window(db)
                return {"teams": self.teams(db), "votedTeamIds": voted, "voteLimit": VOTE_LIMIT, "votingOpen": window['canEdit'],
                        'votingDeadline':window['deadline'],'window':window,"name": actor["name"]}, None, None
            if method == "POST" and path == "/api/vote":
                self.require(actor, "audience")
                if not self.voting_window(db)['canEdit']:
                    raise APIError(409, "观众投票尚未开放、已关闭或已截止")
                team_id = self.team_exists(db, body.get("teamId"))
                already = db.execute("SELECT 1 FROM votes WHERE account_id=? AND team_id=?", (actor["id"], team_id)).fetchone()
                if not already:
                    count = db.execute("SELECT COUNT(*) FROM votes WHERE account_id=?", (actor["id"],)).fetchone()[0]
                    if count >= VOTE_LIMIT:
                        raise APIError(409, "每个观众邀请码最多支持3个不同队伍")
                    db.execute("INSERT INTO votes VALUES (?,?,?)", (actor["id"], team_id, now_ms()))
                    self.audit(db, actor, "vote", team_id)
                    self.touch(db)
                return {"ok": True, "alreadyVoted": bool(already)}, None, None
            if path in {"/api/team", "/api/submission"}:
                self.require(actor, "team")
                bound=db.execute("SELECT t.* FROM teams t JOIN team_accounts a ON a.team_id=t.id WHERE a.account_id=?", (actor["id"],)).fetchone()
                if not bound:
                    raise APIError(403, "队伍身份未绑定")
                if method=="GET" and path=="/api/team":
                    return self.team_submission_response(db, bound), None, None
                if method=="POST" and path=="/api/submission":
                    return self.save_submission(db, actor, bound, body), None, None
            if path.startswith("/api/admin"):
                self.require(actor, "admin")
                if method == "GET" and path == "/api/admin":
                    judges = [{"id": r["id"], "name": r["name"], "active": bool(r["active"]), "submittedCount": r["submitted_count"]}
                              for r in db.execute("SELECT a.id,a.name,a.active,COUNT(s.team_id) AS submitted_count FROM accounts a LEFT JOIN scores s ON s.judge_id=a.id AND s.status='submitted' WHERE a.role='judge' GROUP BY a.id ORDER BY a.created_at")]
                    audience = [{"id": r["id"], "name": r["name"], "active": bool(r["active"])} for r in db.execute("SELECT id,name,active FROM accounts WHERE role='audience' ORDER BY created_at")]
                    for person in judges:person['credentialMode']=self.admin_data.credential_mode(db,person['id'],'judge')
                    for person in audience:person['credentialMode']=self.admin_data.credential_mode(db,person['id'],'audience')
                    stats = {"teamCount": db.execute("SELECT COUNT(*) FROM teams").fetchone()[0], "submittedScores": db.execute("SELECT COUNT(*) FROM scores WHERE status='submitted'").fetchone()[0], "audienceVotes": db.execute("SELECT COUNT(*) FROM votes").fetchone()[0]}
                    return {"teams": self.teams(db, public=False), "judges": judges, "audience": audience, "config": self.config(db), "stats": stats, "submissions": self.admin_submissions(db),'trackOptions':TRACK_OPTIONS}, None, None
                if method == "POST" and path == "/api/admin/settings":
                    allowed = {"scoringOpen", "resultsPublished", "minJudges", "submissionsOpen", "submissionDeadline",'votingOpen','votingDeadline'}
                    if not body or not set(body).issubset(allowed):
                        raise APIError(400, "设置字段不正确")
                    before={key:self.config(db).get(key) for key in body}
                    for key, value in body.items():
                        if key == "minJudges":
                            if type(value) is not int or not 1 <= value <= 50:
                                raise APIError(400, "最少评委数应为1至50的整数")
                        elif key in {"submissionDeadline",'votingDeadline'}:
                            if value is not None and (type(value) is not int or not 0<=value<=253402300799999):
                                raise APIError(400, "窗口截止时间必须是有效的毫秒时间戳或空值")
                        elif type(value) is not bool:
                            raise APIError(400, "开放与公开状态必须为布尔值")
                        db.execute("UPDATE settings SET value=? WHERE key=?", (json.dumps(value), key))
                    self.touch(db)
                    self.audit(db, actor, "settings", metadata={'before':before,'after':body})
                    return {"ok": True, "config": self.config(db)}, None, None
                if method == "POST" and path == "/api/admin/teams":
                    return {'ok':True,'team':self.admin_data.save_team(db,actor,body)},None,None
                if method=='POST' and path=='/api/admin/credentials':
                    return self.credential_action(db,actor,body),None,None
                if method == "POST" and path == "/api/admin/invitations":
                    role = body.get("role")
                    if role not in {"judge", "audience"}:
                        raise APIError(400, "只能创建评委或观众邀请")
                    name = text_field(body.get("name"), "称呼", 80, True)
                    account, code = self.create_account(db, role, name)
                    self.audit(db, actor, "invitation.create", account["id"], {"role": role,'before':None,
                               'after':{'id':account['id'],'name':name,'active':True,'credentialMode':self.admin_data.credential_mode(db,account['id'],role)}})
                    return {"id": account["id"], "name": name, "code": code}, None, None
                if method == "POST" and path == "/api/admin/revoke":
                    return self.credential_action(db,actor,{**body,'action':'revoke'}),None,None
                if method=="POST" and path=="/api/admin/team-access":
                    return self.team_access(db, actor, body), None, None
                if method=="GET" and path=="/api/admin/submissions/export":
                    self.audit(db, actor, "submissions.export")
                    return self.submission_csv(db), None, None
                if method == "GET" and path == "/api/admin/export":
                    output = io.StringIO(newline="")
                    writer = csv.writer(output)
                    writer.writerow(["队号", "队名", "项目", "赛道", "评委编号", "评委称呼", "状态", *[r["name"] for r in DIMENSIONS], "总分", "反馈", "更新时间"])
                    rows = db.execute("SELECT t.*,s.*,s.updated_at AS score_updated_at,a.name AS judge_name FROM teams t LEFT JOIN scores s ON s.team_id=t.id LEFT JOIN accounts a ON a.id=s.judge_id ORDER BY t.number,a.id")
                    for row in rows:
                        values = json.loads(row["scores_json"]) if row["scores_json"] else []
                        values = values + [""] * (6 - len(values))
                        writer.writerow([csv_safe(v) for v in [row["number"], row["name"], row["project"], row["track"], row["judge_id"], row["judge_name"], row["status"], *values, row["total_tenths"] / 10 if row["total_tenths"] is not None else "", row["feedback"], row["score_updated_at"]]])
                    self.audit(db, actor, "export")
                    return ("\ufeff" + output.getvalue()).encode("utf-8"), None, None
            raise APIError(404, "接口不存在")

    def submission_window(self, db, current=None):
        config=self.config(db)
        current=now_ms() if current is None else current
        return {"submissionsOpen": config["submissionsOpen"], "submissionDeadline": config["submissionDeadline"],
                "canEdit": config["submissionsOpen"] and (config["submissionDeadline"] is None or current<config["submissionDeadline"]), "serverTime": current}

    def submission(self, db, team_id):
        row=db.execute("SELECT * FROM submissions WHERE team_id=?", (team_id,)).fetchone()
        if not row:
            return {"version": 0, "draft": None, "submitted": None, "submittedVersion": None,
                    "firstSubmittedAt": None, "submittedAt": None, "updatedAt": None, "hasUnsubmittedChanges": False}
        draft=json.loads(row["draft_json"]) if row["draft_json"] else None
        submitted=json.loads(row["submitted_json"]) if row["submitted_json"] else None
        return {"version": row["version"], "draft": draft, "submitted": submitted, "submittedVersion": row["submitted_version"],
                "firstSubmittedAt": row["first_submitted_at"], "submittedAt": row["submitted_at"], "updatedAt": row["updated_at"],
                "hasUnsubmittedChanges": submitted is not None and draft!=submitted}

    def team_submission_response(self, db, team):
        return {"team": {key: team[key] for key in ("id", "number", "name", "project", "track")},
                "submission": self.submission(db, team["id"]), "window": self.submission_window(db),
                "judgingMaterials":self.judging_materials(db,team['id'])}

    def judging_materials(self, db, team_id):
        result={}
        for row in db.execute('SELECT s.stage,s.run_id,r.frozen_at FROM judging_stages s JOIN judging_runs r ON r.id=s.run_id'):
            roster=db.execute('SELECT snapshot_json FROM judging_roster WHERE run_id=? AND team_id=?',(row['run_id'],team_id)).fetchone()
            snapshot=json.loads(roster['snapshot_json']).get('materialSnapshot') if roster else None
            result[row['stage']]={'runId':row['run_id'],'inRoster':roster is not None,'frozenAt':row['frozen_at'],'materialSnapshot':snapshot}
        return result

    def team_access(self, db, actor, body):
        if set(body)!={"teamId", "action"} or (not isinstance(body.get("action"), str) or body["action"] not in {"issue", "rotate", "revoke"}):
            raise APIError(400, "提交码操作不正确")
        team_id=self.team_exists(db, body["teamId"])
        action=body["action"]
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

    def check_materials(self, materials, submitted):
        limits={"projectName": 140, "summary": 600, "license": 200, "demoInstructions": 2000,
                "sourcesAndLimitations": 2000, "codeVersion": 1024}
        urls=("codeUrl", "readmeUrl", "licenseUrl", "demoUrl", "slidesUrl", "videoUrl")
        if not isinstance(materials, dict) or not set(materials).issubset({*limits, *urls, "confirmed"}):
            raise APIError(400, "项目材料字段不正确")
        result={key: text_field(materials.get(key, ""), key, limit) for key,limit in limits.items()}
        if len(result['codeVersion'].encode('utf-8'))>1024:
            raise APIError(400,'代码版本最多1024字节')
        for key in urls:
            value=text_field(materials.get(key, ""), key, 1024)
            if value:
                try:
                    parsed=urlsplit(value)
                    valid=(parsed.scheme=="https" and valid_material_host(parsed.hostname) and parsed.username is None and parsed.password is None
                           and parsed.port!=0 and len(value.encode("utf-8"))<=1024
                           and not any(c.isspace() or ord(c)<32 or ord(c)==127 or c=="\\" for c in value)
                           and not re.search(r"%(?:0[0-9a-f]|1[0-9a-f]|7f)", value, re.I))
                except ValueError:
                    valid=False
                if not valid:
                    raise APIError(400, key+"必须是无凭据、无空白的完整 HTTPS 链接（最多1024字节）")
            result[key]=value
        confirmed=materials.get("confirmed", False)
        if type(confirmed) is not bool:
            raise APIError(400, "提交确认必须为布尔值")
        result["confirmed"]=confirmed
        if submitted:
            required=("projectName", "summary", "codeUrl", "readmeUrl", "license", "slidesUrl", "videoUrl", "sourcesAndLimitations")
            if any(not result[key] for key in required):
                raise APIError(400, "正式提交请填写全部必填项目材料")
            if not result["demoUrl"] and not result["demoInstructions"]:
                raise APIError(400, "请填写原型演示链接或本地演示说明")
            if not confirmed:
                raise APIError(400, "正式提交前请完成材料自查确认")
            result['codeVersion']=normalize_code_version(result['codeUrl'],result['codeVersion'])
        return result

    def save_submission(self, db, actor, team, body):
        if set(body)!={"expectedVersion", "status", "materials"} or (not isinstance(body.get("status"), str) or body["status"] not in {"draft", "submitted"}):
            raise APIError(400, "提交字段或状态不正确")
        if type(body["expectedVersion"]) is not int or body["expectedVersion"]<0:
            raise APIError(400, "材料版本必须是非负整数")
        submitted=body["status"]=="submitted"
        materials=self.check_materials(body["materials"], submitted)
        current=now_ms()
        if not self.submission_window(db, current)["canEdit"]:
            raise APIError(409, "作品提交尚未开放或已截止，当前材料只读")
        previous=self.submission(db, team["id"])
        if body["expectedVersion"]!=previous["version"]:
            raise APIError(409, "材料已被其他页面更新。当前输入已保留，请重新读取并合并后再保存")
        encoded=json.dumps(materials, ensure_ascii=False, sort_keys=True)
        version=previous["version"]+1
        db.execute("""INSERT INTO submissions VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(team_id) DO UPDATE SET
          version=excluded.version,draft_json=excluded.draft_json,submitted_json=excluded.submitted_json,
          submitted_version=excluded.submitted_version,first_submitted_at=excluded.first_submitted_at,
          submitted_at=excluded.submitted_at,updated_at=excluded.updated_at""",
          (team["id"], version, encoded, encoded if submitted else (json.dumps(previous["submitted"], ensure_ascii=False, sort_keys=True) if previous["submitted"] else None),
           version if submitted else previous["submittedVersion"], (previous["firstSubmittedAt"] or current) if submitted else previous["firstSubmittedAt"],
           current if submitted else previous["submittedAt"], current))
        if submitted:
            db.execute("UPDATE teams SET project=?,updated_at=? WHERE id=?", (materials["projectName"], current, team["id"]))
            db.execute('UPDATE team_details SET revision=revision+1 WHERE team_id=?',(team['id'],))
            self.touch(db)
        self.audit(db, actor, "submission."+body["status"], team["id"], {"version": version})
        updated=db.execute("SELECT * FROM teams WHERE id=?", (team["id"],)).fetchone()
        return {"ok": True, **self.team_submission_response(db, updated)}

    def judge_submissions(self, db):
        return [{"teamId": row["team_id"], "materials": json.loads(row["submitted_json"]), "version": row["submitted_version"],
                 "firstSubmittedAt": row["first_submitted_at"], "submittedAt": row["submitted_at"]}
                for row in db.execute("SELECT * FROM submissions WHERE submitted_json IS NOT NULL ORDER BY team_id")]

    def admin_submissions(self, db):
        rows=db.execute("""SELECT t.id AS team_id,a.id AS account_id,a.active FROM teams t
          LEFT JOIN team_accounts b ON b.team_id=t.id LEFT JOIN accounts a ON a.id=b.account_id ORDER BY t.number""")
        return [{"teamId": row["team_id"], "access": {"id": row["account_id"], "active": bool(row["active"])} if row["account_id"] else None,
                 "submission": self.submission(db, row["team_id"]),
                 "judgingMaterials":self.judging_materials(db,row['team_id'])} for row in rows]

    def submission_csv(self, db):
        fields=("projectName", "summary", "codeUrl", "readmeUrl", "license", "licenseUrl", "demoUrl", "demoInstructions", "slidesUrl", "videoUrl", "sourcesAndLimitations", "codeVersion", "confirmed")
        output=io.StringIO(newline="")
        writer=csv.writer(output)
        writer.writerow(["队号", "队名", "赛道", "展示作品名", "提交状态", "编辑版本", "正式版本", "首次提交时间", "正式提交时间", "更新时间", "有未提交修改",
                         *["正式_"+key for key in fields], *["草稿_"+key for key in fields]])
        for team in db.execute("SELECT * FROM teams ORDER BY number"):
            record=self.submission(db, team["id"])
            state="已提交且有草稿修改" if record["hasUnsubmittedChanges"] else "已正式提交" if record["submitted"] else "草稿" if record["draft"] else "未保存"
            writer.writerow([csv_safe(value) for value in [team["number"], team["name"], team["track"], team["project"], state, record["version"], record["submittedVersion"],
                             record["firstSubmittedAt"], record["submittedAt"], record["updatedAt"], record["hasUnsubmittedChanges"],
                             *[(record["submitted"] or {}).get(key, "") for key in fields], *[(record["draft"] or {}).get(key, "") for key in fields]]])
        return ("\ufeff"+output.getvalue()).encode("utf-8")

    def team_exists(self, db, team_id):
        if not isinstance(team_id, str) or len(team_id) > 80:
            raise APIError(400, "队伍编号不正确")
        if not db.execute("SELECT 1 FROM teams WHERE id=?", (team_id,)).fetchone():
            raise APIError(404, "队伍不存在")
        return team_id

    def check_scores(self, scores):
        if not isinstance(scores, list) or len(scores) != 6:
            raise APIError(400, "请填写全部六个评分维度")
        result, total = [], 0
        for value, dim in zip(scores, DIMENSIONS):
            if type(value) not in {int, float}:
                raise APIError(400, "评分必须是数字")
            try:
                number = Decimal(str(value))
                tenths = number * 10
                if not number.is_finite() or number < 0 or number > dim["max"] or tenths != tenths.to_integral_value():
                    raise APIError(400, f"{dim['name']}应为0至{dim['max']}分，最多一位小数")
            except InvalidOperation:
                raise APIError(400, "评分数字不正确")
            result.append(float(number))
            total += int(tenths)
        return result, total


class EventHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 128

    def __init__(self, address, service):
        self.service = service
        super().__init__(address, EventHandler)


class EventHandler(BaseHTTPRequestHandler):
    server_version = "GOTC"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, *_):
        # Invitation codes, cookies, queries and request bodies never enter access logs.
        pass

    def headers_out(self, status, content_type, length, cookie=None, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
        if self.server.service.secure:
            self.send_header("Strict-Transport-Security", "max-age=31536000")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        if extra:
            for key, value in extra.items():
                self.send_header(key, value)
        self.end_headers()

    def respond(self, status, value, cookie=None):
        payload = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.headers_out(status, "application/json; charset=utf-8", len(payload), cookie)
        if self.command != "HEAD":
            self.wfile.write(payload)

    def do_GET(self):
        self.handle_route()

    def do_HEAD(self):
        self.handle_route()

    def do_POST(self):
        self.handle_route()

    def do_OPTIONS(self):
        self.respond(405, {"error": "不支持跨域请求"})

    def handle_route(self):
        service = self.server.service
        try:
            parsed = urlsplit(self.path)
            if parsed.scheme or parsed.netloc or parsed.fragment:
                raise APIError(400, "请求地址格式不正确")
            path = unquote(parsed.path, errors="strict")
            if "\\" in path or "\x00" in path or any(p in {".", ".."} for p in path.split("/")):
                raise APIError(400, "请求路径不正确")
            if path == service.base_path and self.command in {"GET", "HEAD"}:
                self.headers_out(308, "text/plain", 0, extra={"Location": service.base_path + "/"})
                return
            if service.base_path and not path.startswith(service.base_path + "/"):
                raise APIError(404, "页面不存在")
            path = path[len(service.base_path):] if service.base_path else path
            query={}
            if parsed.query:
                if self.command!='GET' or not path.startswith('/api/admin/data/'):
                    raise APIError(400,'此接口不接受查询参数')
                try:
                    params=parse_qs(parsed.query,keep_blank_values=True,strict_parsing=True,max_num_fields=20,encoding='utf-8',errors='strict')
                except (ValueError,UnicodeError):
                    raise APIError(400,'查询参数格式不正确')
                if any(len(values)!=1 or len(key)>80 or len(values[0])>2000 for key,values in params.items()):
                    raise APIError(400,'查询参数不能重复或超长')
                query={key:values[0] for key,values in params.items()}
            if path.startswith("/api/"):
                if self.command == "HEAD":
                    raise APIError(405, "接口不支持此方法")
                body = {}
                if self.command == "POST":
                    if self.headers.get("Origin") != service.origin or self.headers.get("Sec-Fetch-Site") not in {None, "same-origin", "none"}:
                        raise APIError(403, "仅允许本站发起操作，请从活动官网打开")
                    if self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length", [])) != 1:
                        raise APIError(400, "请求长度不正确")
                    try:
                        length = int(self.headers.get("Content-Length", "-1"))
                    except ValueError:
                        raise APIError(400, "请求长度不正确")
                    limit=SUBMISSION_BODY_LIMIT if path=='/api/submission' else IMPORT_BODY_LIMIT if path=='/api/admin/data/import/preview' else BODY_LIMIT
                    if length < 0 or length > limit:
                        raise APIError(413,'请求内容超过端点大小限制')
                    if self.headers.get_content_type() != "application/json":
                        raise APIError(415, "请使用JSON请求")
                    try:
                        body = json.loads(self.rfile.read(length).decode("utf-8"), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                    except (UnicodeError, ValueError):
                        raise APIError(400, "JSON格式不正确")
                    if not isinstance(body, dict):
                        raise APIError(400, "JSON内容必须是对象")
                peer = self.client_address[0]
                if ipaddress.ip_address(peer).is_loopback:
                    proxy_ip = self.headers.get("X-Real-IP", "")
                    try:
                        peer = str(ipaddress.ip_address(proxy_ip))
                    except ValueError:
                        pass
                value, token, cookie_action = service.dispatch(self.command, path, body, self.headers.get("Cookie", ""), peer,query=query)
                cookie = None
                if cookie_action:
                    cookie = f"{COOKIE_NAME}={token or ''}; Path={service.base_path or '/'}; HttpOnly; SameSite=Strict; Max-Age={SESSION_SECONDS if cookie_action == 'set' else 0}"
                    if service.secure:
                        cookie += "; Secure"
                if isinstance(value, bytes):
                    filename='gotc-submissions.csv' if path=='/api/admin/submissions/export' else 'gotc-data.csv' if path.startswith('/api/admin/data/') else 'gotc-scores.csv'
                    self.headers_out(200, "text/csv; charset=utf-8", len(value), extra={"Content-Disposition": f'attachment; filename="{filename}"'})
                    self.wfile.write(value)
                else:
                    self.respond(200, value, cookie)
            elif self.command in {"GET", "HEAD"}:
                relative = path.lstrip("/") or "index.html"
                candidate = (service.root / relative).resolve()
                if not candidate.is_relative_to(service.root):
                    raise APIError(403, "访问被拒绝")
                segments = relative.split("/")
                if any(part.startswith(".") for part in segments):
                    raise APIError(404, "页面不存在")
                allowed = relative in STATIC_FILES or (segments[0] == "assets" and candidate.suffix.lower() in ASSET_EXTENSIONS) or (segments[0] == "downloads" and candidate.suffix.lower() in DOWNLOAD_EXTENSIONS)
                if not allowed or not candidate.is_file():
                    raise APIError(404, "页面不存在")
                data = candidate.read_bytes()
                mime = mimetypes.guess_type(str(candidate))[0] or "application/octet-stream"
                self.headers_out(200, mime + ("; charset=utf-8" if mime.startswith("text/") else ""), len(data))
                if self.command != "HEAD":
                    self.wfile.write(data)
            else:
                raise APIError(405, "请求方法不支持")
        except APIError as error:
            self.close_connection = True
            self.respond(error.status, {"error": error.message})
        except sqlite3.IntegrityError:
            self.respond(409, {"error": "队号或记录已存在，请刷新后核对"})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True
        except Exception:
            self.close_connection = True
            self.respond(500, {"error": "服务暂时不可用，请稍后重试"})


def main():
    # Private files must stay private before SQLite creates its journal and WAL.
    os.umask(0o077)
    host = os.environ.get("GOTC_HOST", "127.0.0.1")
    try:
        if not ipaddress.ip_address(host).is_loopback:
            raise ValueError("Service must bind to a loopback address behind Nginx")
    except ValueError as error:
        raise SystemExit("GOTC_HOST must be a loopback IP address") from error
    service = EventService()
    server = EventHTTPServer((host, int(os.environ.get("GOTC_PORT", "5274"))), service)
    print(f"GOTC service ready on {host}:{server.server_port}{service.base_path}/ (access code is in private data directory)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
