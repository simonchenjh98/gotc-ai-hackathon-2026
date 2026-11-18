"""GOTC single-instance event service. Python 3.11+, standard library only."""
from __future__ import annotations

import csv
from contextlib import contextmanager
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
from urllib.parse import unquote, urlsplit

DIMENSIONS = [
    {"id": "problem", "name": "问题价值与理解", "max": 20},
    {"id": "ai", "name": "方案与AI使用", "max": 20},
    {"id": "prototype", "name": "原型完成度", "max": 20},
    {"id": "testing", "name": "测试与改进", "max": 15},
    {"id": "opensource", "name": "开源与责任", "max": 15},
    {"id": "teamwork", "name": "团队协作与表达", "max": 10},
]
BODY_LIMIT = 16384
SESSION_SECONDS = 12 * 3600
VOTE_LIMIT = 3
COOKIE_NAME = "gotc_session"
STATIC_FILES = {"index.html", "styles.css", "app.js", "platform.html", "platform.js", "platform.css", "favicon.svg"}
ASSET_EXTENSIONS = {".webp", ".png", ".jpg", ".jpeg", ".svg", ".ico", ".woff", ".woff2"}
DOWNLOAD_EXTENSIONS = {".ics", ".txt"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
 id TEXT PRIMARY KEY, role TEXT NOT NULL CHECK(role IN ('admin','judge','audience')),
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


class APIError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message


def text_field(value, field, limit, required=False):
    if not isinstance(value, str):
        raise APIError(400, f"{field}必须是文字")
    value = value.strip()
    if len(value) > limit or (required and not value) or any(ord(c) < 32 and c not in "\n\t" for c in value):
        raise APIError(400, f"{field}长度或内容不正确")
    return value


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
        with self.db() as db:
            db.executescript(SCHEMA)
            for key, value in {"scoringOpen": False, "resultsPublished": True, "minJudges": 5, "updatedAt": now_ms()}.items():
                db.execute("INSERT OR IGNORE INTO settings VALUES (?,?)", (key, json.dumps(value)))
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
                for account in db.execute("SELECT * FROM accounts WHERE role=?", (role,)).fetchall():
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

    def touch(self, db):
        db.execute("UPDATE settings SET value=? WHERE key='updatedAt'", (json.dumps(now_ms()),))

    def audit(self, db, actor, action, target="", metadata=None):
        db.execute("INSERT INTO audit(at,actor_id,role,action,target,metadata) VALUES (?,?,?,?,?,?)",
                   (now_ms(), actor["id"] if actor else None, actor["role"] if actor else None,
                    action, target, json.dumps(metadata or {}, ensure_ascii=False)))

    def create_account(self, db, role, name):
        account_id = {"admin": "GA", "judge": "GJ", "audience": "GV"}[role] + secrets.token_hex(8)
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
        return result

    def require(self, actor, role):
        if actor is None:
            raise APIError(401, "请先登录")
        if actor["role"] != role:
            raise APIError(403, "当前身份无权访问")

    def dispatch(self, method, path, body, cookie, ip):
        """Return (JSON object or CSV bytes, optional session token, cookie action)."""
        with self.db() as db:
            actor = self.account(db, cookie)
            if method == "GET" and path == "/api/session":
                return {"role": actor["role"] if actor else None, "name": actor["name"] if actor else ""}, None, None
            if method == "GET" and path == "/api/board":
                config = self.config(db)
                return {"teams": self.teams(db), "config": config, "updatedAt": config["updatedAt"], "serverTime": now_ms()}, None, None
            if method == "POST":
                db.execute("BEGIN IMMEDIATE")
            if method == "POST" and path == "/api/login":
                role = body.get("role")
                if role not in {"admin", "judge", "audience"}:
                    raise APIError(400, "登录身份不正确")
                self.rate_limit(ip, role)
                code = text_field(body.get("code"), "邀请码", 160, True)
                selector = body.get("accountId")
                if selector is not None and (not isinstance(selector, str) or len(selector) > 80):
                    raise APIError(400, "登录身份不正确")
                shared_code = self.shared_login_codes.get(role)
                if selector is None and shared_code and hmac.compare_digest(code.encode(), shared_code.encode()):
                    candidates = db.execute("SELECT id,name FROM accounts WHERE role=? AND active=1 ORDER BY created_at,id", (role,)).fetchall()
                    if len(candidates) > 1:
                        return {"selectAccount": True, "accounts": [{"id": a["id"], "name": a["name"]} for a in candidates]}, None, None
                    if candidates:
                        selector = candidates[0]["id"]
                selector = selector or code.split(".", 1)[0]
                account = db.execute("SELECT * FROM accounts WHERE id=? AND role=? AND active=1", (selector, role)).fetchone()
                salt = account["salt"] if account else b"gotc-dummy-salt!"
                hashed = scrypt_hash(code, salt)
                if not account or not hmac.compare_digest(hashed, account["code_hash"]):
                    raise APIError(401, "邀请码无效或已停用")
                token = secrets.token_urlsafe(32)
                db.execute("DELETE FROM sessions WHERE expires_at<=?", (now_ms(),))
                db.execute("INSERT INTO sessions VALUES (?,?,?)", (hashlib.sha256(token.encode()).hexdigest(), account["id"], now_ms() + SESSION_SECONDS * 1000))
                self.audit(db, account, "login")
                return {"ok": True, "role": role, "name": account["name"]}, token, "set"
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
                return {"teams": self.teams(db), "myScores": scores, "dimensions": DIMENSIONS, "scoringOpen": self.config(db)["scoringOpen"], "name": actor["name"]}, None, None
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
                return {"teams": self.teams(db), "votedTeamIds": voted, "voteLimit": VOTE_LIMIT, "votingOpen": self.config(db)["scoringOpen"], "name": actor["name"]}, None, None
            if method == "POST" and path == "/api/vote":
                self.require(actor, "audience")
                if not self.config(db)["scoringOpen"]:
                    raise APIError(409, "观众投票尚未开放或已关闭")
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
            if path.startswith("/api/admin"):
                self.require(actor, "admin")
                if method == "GET" and path == "/api/admin":
                    judges = [{"id": r["id"], "name": r["name"], "active": bool(r["active"]), "submittedCount": r["submitted_count"]}
                              for r in db.execute("SELECT a.id,a.name,a.active,COUNT(s.team_id) AS submitted_count FROM accounts a LEFT JOIN scores s ON s.judge_id=a.id AND s.status='submitted' WHERE a.role='judge' GROUP BY a.id ORDER BY a.created_at")]
                    audience = [{"id": r["id"], "name": r["name"], "active": bool(r["active"])} for r in db.execute("SELECT id,name,active FROM accounts WHERE role='audience' ORDER BY created_at")]
                    stats = {"teamCount": db.execute("SELECT COUNT(*) FROM teams").fetchone()[0], "submittedScores": db.execute("SELECT COUNT(*) FROM scores WHERE status='submitted'").fetchone()[0], "audienceVotes": db.execute("SELECT COUNT(*) FROM votes").fetchone()[0]}
                    return {"teams": self.teams(db, public=False), "judges": judges, "audience": audience, "config": self.config(db), "stats": stats}, None, None
                if method == "POST" and path == "/api/admin/settings":
                    allowed = {"scoringOpen", "resultsPublished", "minJudges"}
                    if not body or not set(body).issubset(allowed):
                        raise APIError(400, "设置字段不正确")
                    for key, value in body.items():
                        if key == "minJudges":
                            if type(value) is not int or not 1 <= value <= 50:
                                raise APIError(400, "最少评委数应为1至50的整数")
                        elif type(value) is not bool:
                            raise APIError(400, "开放与公开状态必须为布尔值")
                        db.execute("UPDATE settings SET value=? WHERE key=?", (json.dumps(value), key))
                    self.touch(db)
                    self.audit(db, actor, "settings", metadata=body)
                    return {"ok": True, "config": self.config(db)}, None, None
                if method == "POST" and path == "/api/admin/teams":
                    number = body.get("number")
                    if type(number) is not int or not 1 <= number <= 999:
                        raise APIError(400, "队号应为1至999的整数")
                    name = text_field(body.get("name"), "队名", 80, True)
                    project = text_field(body.get("project", ""), "项目名", 140)
                    track = text_field(body.get("track", ""), "赛道", 80)
                    team_id = body.get("id")
                    if team_id is not None:
                        team_id = self.team_exists(db, team_id)
                        db.execute("UPDATE teams SET number=?,name=?,project=?,track=?,updated_at=? WHERE id=?", (number, name, project, track, now_ms(), team_id))
                    else:
                        team_id = "T" + secrets.token_hex(8)
                        db.execute("INSERT INTO teams VALUES (?,?,?,?,?,?)", (team_id, number, name, project, track, now_ms()))
                    self.touch(db)
                    self.audit(db, actor, "team.save", team_id)
                    return {"ok": True, "team": {"id": team_id, "number": number, "name": name, "project": project, "track": track}}, None, None
                if method == "POST" and path == "/api/admin/invitations":
                    role = body.get("role")
                    if role not in {"judge", "audience"}:
                        raise APIError(400, "只能创建评委或观众邀请")
                    name = text_field(body.get("name"), "称呼", 80, True)
                    account, code = self.create_account(db, role, name)
                    self.audit(db, actor, "invitation.create", account["id"], {"role": role})
                    return {"id": account["id"], "name": name, "code": code}, None, None
                if method == "POST" and path == "/api/admin/revoke":
                    role, account_id = body.get("role"), body.get("id")
                    if role not in {"judge", "audience"} or not isinstance(account_id, str):
                        raise APIError(400, "停用目标不正确")
                    target = db.execute("SELECT id FROM accounts WHERE id=? AND role=?", (account_id, role)).fetchone()
                    if not target:
                        raise APIError(404, "邀请不存在")
                    db.execute("UPDATE accounts SET active=0 WHERE id=?", (account_id,))
                    db.execute("DELETE FROM sessions WHERE account_id=?", (account_id,))
                    self.audit(db, actor, "invitation.revoke", account_id, {"role": role})
                    return {"ok": True}, None, None
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
            if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
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
                    if length < 0 or length > BODY_LIMIT:
                        raise APIError(413, "请求内容超过16KB限制")
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
                value, token, cookie_action = service.dispatch(self.command, path, body, self.headers.get("Cookie", ""), peer)
                cookie = None
                if cookie_action:
                    cookie = f"{COOKIE_NAME}={token or ''}; Path={service.base_path or '/'}; HttpOnly; SameSite=Strict; Max-Age={SESSION_SECONDS if cookie_action == 'set' else 0}"
                    if service.secure:
                        cookie += "; Secure"
                if isinstance(value, bytes):
                    self.headers_out(200, "text/csv; charset=utf-8", len(value), extra={"Content-Disposition": 'attachment; filename="gotc-scores.csv"'})
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
