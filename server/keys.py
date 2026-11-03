"""Built-in AI API gateway: team keys, upstream proxy, metering and team budgets. Standard library only.

Organizers enable a team once; the team then creates its own keys and every key spends the same team
budget. Keys are shown once and stored only as SHA-256 hashes. Requests are forwarded with the
organizer's upstream keys from the private gateway.json, which teams never see.
"""
from __future__ import annotations

import csv
import hashlib
import io
import ipaddress
import os
import json
import re
import secrets
import socket
import threading
import time
import urllib.error
import urllib.request
import zlib
from collections import defaultdict, deque
from urllib.parse import urlsplit

from errors import APIError

SCHEMA = """
CREATE TABLE IF NOT EXISTS team_api (
 team_id TEXT PRIMARY KEY REFERENCES teams(id), active INTEGER NOT NULL CHECK(active IN (0,1)), created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS api_keys (
 id TEXT PRIMARY KEY, team_id TEXT NOT NULL REFERENCES teams(id), name TEXT NOT NULL, key_hash TEXT NOT NULL UNIQUE,
 masked TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)), created_at INTEGER NOT NULL, last_used_at INTEGER
);
CREATE TABLE IF NOT EXISTS api_usage (
 id INTEGER PRIMARY KEY AUTOINCREMENT, key_id TEXT NOT NULL REFERENCES api_keys(id), team_id TEXT NOT NULL REFERENCES teams(id),
 at INTEGER NOT NULL, model TEXT NOT NULL, input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
 cache_write_tokens INTEGER NOT NULL, cache_read_tokens INTEGER NOT NULL,
 cost_micro INTEGER NOT NULL, status INTEGER NOT NULL, path TEXT NOT NULL, stream INTEGER NOT NULL,
 duration_ms INTEGER NOT NULL, user_agent TEXT NOT NULL,
 incomplete INTEGER NOT NULL DEFAULT 0, estimated INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS api_usage_team ON api_usage(team_id, at);
CREATE INDEX IF NOT EXISTS api_usage_key ON api_usage(key_id, at);
CREATE TABLE IF NOT EXISTS gateway_config (
 id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL, updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS api_logs (
 usage_id INTEGER PRIMARY KEY REFERENCES api_usage(id), headers TEXT NOT NULL,
 request BLOB NOT NULL, response BLOB NOT NULL, request_bytes INTEGER NOT NULL, response_bytes INTEGER NOT NULL
);
"""
DEFAULTS = {"keys.dailyBudget": 5, "keys.totalBudget": 50, "keys.maxKeys": 5, "keys.expiresAt": None,
            "keys.logContent": True, "keys.clientRules": [], "keys.teamRpm": 60, "keys.announcement": "", "keys.blockedWords": []}
# Public path -> (upstream name, path appended to that upstream's base URL)
ROUTES = {"/v1/chat/completions": ("openai", "/chat/completions"), "/v1/messages": ("anthropic", "/messages")}
BODY_LIMIT = 4 * 1024 * 1024
UPSTREAM_TIMEOUT = 300
QUEUE_WAIT = 30  # seconds a request waits for a free team slot before 429 (agent tools send bursts)
RESERVE_OUTPUT_DEFAULT = 8192  # output tokens reserved when the request sets no max_tokens (as sub2api)
RESERVE_OUTPUT_CAP = 64000
RETRY_STATUSES = {429, 503, 529}  # explicit "not processed" answers; 5xx/timeouts may already be billed upstream
# Tools must run on the client. Anthropic "client tools" (bash, text editor, computer, memory) are allowed;
# anything else typed (web search, code execution, MCP, tool search, …) runs and is billed upstream.
CLIENT_TOOL_TYPES = ("custom", "function", "bash_", "text_editor_", "computer_", "memory_")
BILLED_FIELDS = {"mcp_servers", "web_search_options", "container"}  # upstream-billed extras outside token usage
PRICING_FIELDS = ("service_tier", "speed", "inference_geo")  # dropped so requests run at the configured (standard) price
# ponytail: denylist of beta prefixes that add upstream charges; extend when providers add more.
BILLED_BETAS = ("mcp-client", "code-execution", "files-api", "context-1m", "skills", "web-fetch", "web-search")
RETRY_DELAYS = (1, 2)  # seconds; retried only before any byte reaches the client
TEAM_CONCURRENCY = 4
DAY_MS = 86_400_000
RESPONSE_LOG_CAP = 16 * 1024 * 1024  # bytes of a reply kept for the content viewer; requests are already capped by BODY_LIMIT
SECRET_HEADERS = {"authorization", "x-api-key", "cookie", "proxy-authorization"}


def now_ms():
    return int(time.time() * 1000)


KEY_HASH_ITERATIONS = 1000  # keys are 256-bit random, so the server-side pepper, not iterations, carries the strength


def load_pepper(data_dir):
    """Private random secret mixed into key hashes, so a leaked database alone cannot be used to check keys."""
    path = data_dir / "api-key-pepper"
    if not path.exists():
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)  # never overwrite: that would void every key
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(secrets.token_hex(32))
        except FileExistsError:
            pass
    return bytes.fromhex(path.read_text(encoding="utf-8").strip())


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # never replay the upstream key to another location


def check_url(value):
    parts = urlsplit(value if isinstance(value, str) else "")
    try:
        loopback = parts.hostname == "localhost" or ipaddress.ip_address(parts.hostname or "").is_loopback
    except ValueError:
        loopback = False
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.query or parts.fragment or parts.username \
            or (parts.scheme == "http" and not loopback):
        raise ValueError("upstream url must be https, or http on loopback")
    return value.rstrip("/")


def load_config(path):
    """Optional private gateway.json, used only to seed the admin panel's upstream settings on first start."""
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    parse_config(data)
    return data


def parse_config(data):
    """Validate {"openai"|"anthropic": {"url", "key", "models": {name: price}, "aliases"?: {name|prefix*: model}}}.

    price is [input, output] or [input, output, cacheWrite, cacheRead] USD per million tokens;
    the short form bills cache writes at 1.25x and cache reads at 0.1x the input price.
    Returns the runtime form, or None when no upstream is configured.
    """
    if data in (None, {}):
        return None
    if not isinstance(data, dict) or not set(data).issubset({"openai", "anthropic"}):
        raise ValueError("gateway.json supports openai and anthropic upstreams")
    result = {}
    for name, item in data.items():
        if not isinstance(item, dict) or not {"url", "key", "models"} <= set(item) <= {"url", "key", "models", "aliases"} \
                or not isinstance(item["key"], str) or not item["key"]:
            raise ValueError(f"{name} upstream needs url, key and models")
        models = item["models"]
        if not isinstance(models, dict) or not models or len(models) > 200 or not all(
                isinstance(m, str) and 0 < len(m) <= 100 and isinstance(p, list) and len(p) in (2, 4) and all(type(x) in (int, float) and 0 <= x <= 10000 for x in p)
                for m, p in models.items()):
            raise ValueError(f"{name} models must map a model name to [input, output(, cacheWrite, cacheRead)] USD per million tokens")
        aliases = item.get("aliases", {})
        if not isinstance(aliases, dict) or not all(isinstance(a, str) and a and target in models for a, target in aliases.items()):
            raise ValueError(f"{name} aliases must map a name (or prefix ending in *) to one of its models")
        result[name] = {"url": check_url(item["url"]), "key": item["key"], "aliases": aliases,
                        "models": {m: p if len(p) == 4 else [p[0], p[1], p[0] * 1.25, p[0] * 0.1] for m, p in models.items()}}
    return result


def resolve(upstream, model):
    """Allowed model name for a requested one: exact, then alias, then the longest matching prefix* alias."""
    if not isinstance(model, str):
        return None
    if model in upstream["models"]:
        return model
    aliases = upstream["aliases"]
    if model in aliases:
        return aliases[model]
    prefixes = [a for a in aliases if a.endswith("*") and model.startswith(a[:-1])]
    return aliases[max(prefixes, key=len)] if prefixes else None


def read_usage(upstream, data, usage):
    """Update usage=[input, output, cacheWrite, cacheRead] tokens from one response object or stream event."""
    if not isinstance(data, dict):
        return
    try:
        if upstream == "openai":
            u = data.get("usage")
            if isinstance(u, dict):
                cached = int((u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)  # included in prompt_tokens
                usage[:] = [int(u.get("prompt_tokens") or 0) - cached, int(u.get("completion_tokens") or 0), 0, cached]
            return
        u = (data.get("message") or {}).get("usage") if data.get("type") == "message_start" else data.get("usage")
        if isinstance(u, dict):
            # message_delta repeats or omits input counts; never let a later zero erase them.
            for index, field in enumerate(("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")):
                value = int(u.get(field) or 0)
                if value or (field == "output_tokens" and field in u):
                    usage[index] = value
    except (TypeError, ValueError, AttributeError):
        pass


def stream_progress(upstream, data, call):
    """Track whether the stream reached its terminal event and how much reply text it carried (for estimates)."""
    if not isinstance(data, dict):
        return
    if upstream == "anthropic":
        if data.get("type") == "message_stop":
            call["done"] = True
        delta = data.get("delta") if isinstance(data.get("delta"), dict) else {}
        texts = [delta.get("text"), delta.get("thinking"), delta.get("partial_json")]
    else:
        choices = data.get("choices") if isinstance(data.get("choices"), list) else []
        if isinstance(data.get("usage"), dict) or any(isinstance(c, dict) and c.get("finish_reason") for c in choices):
            call["done"] = True
        texts = []
        for choice in choices:
            delta = choice.get("delta") if isinstance(choice, dict) and isinstance(choice.get("delta"), dict) else {}
            texts += [delta.get("content"), delta.get("reasoning_content")]
            calls = delta.get("tool_calls") if isinstance(delta.get("tool_calls"), list) else []
            texts += [c.get("function", {}).get("arguments") for c in calls if isinstance(c, dict) and isinstance(c.get("function"), dict)]
    call["reply_bytes"] += sum(len(t.encode()) for t in texts if isinstance(t, str))


OUTPUT_LIMIT_FIELDS = ("max_tokens", "max_completion_tokens", "max_output_tokens")


def bound_output(body):
    """Clamp the output limit the upstream will honour to what gets reserved, so the reservation is a true ceiling.

    Any limit above the cap (or a non-integer one) is lowered to the cap; with no limit at all, max_tokens is set to
    the default. Returns the number of output tokens reserved.
    """
    present = [f for f in OUTPUT_LIMIT_FIELDS if f in body]
    limit = RESERVE_OUTPUT_DEFAULT
    for field in present:
        value = body[field]
        value = value if type(value) is int and value > 0 else RESERVE_OUTPUT_CAP
        body[field] = min(value, RESERVE_OUTPUT_CAP)
    if present:
        limit = max(body[f] for f in present)
    else:
        body["max_tokens"] = limit
    return limit


def allowed_betas(value):
    """Forward client anthropic-beta flags except those that add upstream charges outside token prices."""
    flags = [f.strip() for f in (value or "").split(",") if f.strip() and not f.strip().lower().startswith(BILLED_BETAS)]
    return ",".join(flags)


def unpack(blob):
    return zlib.decompress(blob).decode("utf-8", "replace")


def check_rules(rules):
    if not isinstance(rules, list) or len(rules) > 30:
        raise APIError(400, "客户端规则最多30条")
    for rule in rules:
        if not isinstance(rule, dict) or set(rule) != {"action", "header", "contains"} or rule["action"] not in {"allow", "deny"} \
                or not isinstance(rule["header"], str) or not re.fullmatch(r"[A-Za-z0-9-]{1,64}", rule["header"]) \
                or rule["header"].lower() in SECRET_HEADERS or not isinstance(rule["contains"], str) or not 1 <= len(rule["contains"]) <= 200:
            raise APIError(400, "客户端规则格式不正确：动作 allow/deny、请求头名称、包含的文字（1–200字），不能针对密钥类请求头")


def blocked_by(rules, headers):
    """Return a reason when request headers break the rules: any deny match blocks; allow rules, if any, must match one."""
    hit = lambda r: r["contains"].lower() in (headers.get(r["header"]) or "").lower()
    for rule in rules:
        if rule["action"] == "deny" and hit(rule):
            return f"请求被组织方的客户端规则拦截（{rule['header']}）"
    allows = [r for r in rules if r["action"] == "allow"]
    if allows and not any(hit(r) for r in allows):
        return "当前应用或客户端不在本活动允许范围内：" + "；".join(f"{r['header']} 含 {r['contains']}" for r in allows)
    return None


class KeyService:
    def __init__(self, service, config, text):
        self.service, self.text = service, text
        self.pepper = load_pepper(service.data_dir)
        self.seed = config  # raw gateway.json content, copied into the database once
        self.config = parse_config(config)
        self.lock = threading.Lock()
        self.slot_free = threading.Condition(self.lock)
        # ponytail: in-memory per-team concurrency; correct because the service is a single process.
        self.inflight = defaultdict(int)
        self.recent = defaultdict(deque)  # team -> monotonic times of requests in the last minute
        self.reserved = defaultdict(int)  # team -> micro-USD held by requests in flight
        self.opener = urllib.request.build_opener(_NoRedirect)  # honours HTTPS_PROXY for reaching upstreams

    def digest(self, token):
        return hashlib.pbkdf2_hmac("sha256", token.encode(), self.pepper, KEY_HASH_ITERATIONS).hex()

    def initialize(self, db):
        db.executescript(SCHEMA)
        columns = {r["name"] for r in db.execute("PRAGMA table_info(api_usage)")}
        for column in ("incomplete", "estimated"):  # databases created before these flags existed
            if column not in columns:
                db.execute(f"ALTER TABLE api_usage ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0")
        for key, value in DEFAULTS.items():
            db.execute("INSERT OR IGNORE INTO settings VALUES (?,?)", (key, json.dumps(value)))
        if self.seed:
            db.execute("INSERT OR IGNORE INTO gateway_config VALUES (1,?,?)", (json.dumps(self.seed, ensure_ascii=False), now_ms()))
        row = db.execute("SELECT data FROM gateway_config WHERE id=1").fetchone()
        self.config = parse_config(json.loads(row["data"])) if row else None

    def raw_config(self, db):
        row = db.execute("SELECT data FROM gateway_config WHERE id=1").fetchone()
        return json.loads(row["data"]) if row else {}

    def public_config(self, db):
        """Upstream settings for the admin panel; the upstream API key is never sent back."""
        return {name: {**{k: v for k, v in item.items() if k != "key"}, "keyHint": "…" + item["key"][-4:] if len(item["key"]) > 8 else "已设置"}
                for name, item in self.raw_config(db).items()}

    def save_upstreams(self, db, actor, body):
        """Replace the upstream list. An empty or missing "key" keeps the saved key for that upstream."""
        if not isinstance(body, dict) or set(body) != {"upstreams"} or not isinstance(body["upstreams"], dict):
            raise APIError(400, "上游设置格式不正确")
        old, new = self.raw_config(db), {}
        for name, item in body["upstreams"].items():
            if name not in {"openai", "anthropic"} or not isinstance(item, dict):
                raise APIError(400, "只支持 OpenAI 兼容与 Anthropic 两类上游")
            item = dict(item)
            if not item.get("key"):
                if name not in old:
                    raise APIError(400, f"{name} 上游需要填写 API Key")
                # Never send the saved key to a different server: changing the host requires re-entering it.
                if not isinstance(item.get("url"), str) or urlsplit(item["url"]).netloc.lower() != urlsplit(old[name]["url"]).netloc.lower():
                    raise APIError(400, f"{name} 上游地址已更改，请重新填写 API Key")
                item["key"] = old[name]["key"]
            if not item.get("aliases"):
                item.pop("aliases", None)
            new[name] = item
        try:
            config = parse_config(new)
        except ValueError as error:
            raise APIError(400, "上游设置不正确：" + str(error)) from None
        db.execute("INSERT INTO gateway_config VALUES (1,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at",
                   (json.dumps(new, ensure_ascii=False), now_ms()))
        # ponytail: swapped before commit; a failed commit would leave memory ahead of the DB until restart.
        self.config = config
        self.service.audit(db, actor, "api-upstreams.save", metadata={name: {"url": item["url"], "models": list(item["models"]),
                                                                            "keyChanged": item["key"] != old.get(name, {}).get("key")}
                                                                     for name, item in new.items()})
        return {"ok": True, "upstreams": self.public_config(db), "models": self.models()}

    def settings(self, db):
        return {r["key"][5:]: json.loads(r["value"]) for r in db.execute("SELECT key,value FROM settings WHERE key LIKE 'keys.%'")}

    def models(self):
        # Config order is kept: the first model of each upstream is the default shown in setup snippets.
        return {name: list(item["models"]) for name, item in (self.config or {}).items()}

    def spend(self, db, team_id, since=0):
        return db.execute("SELECT COALESCE(SUM(cost_micro),0) FROM api_usage WHERE team_id=? AND at>=?", (team_id, since)).fetchone()[0] / 1e6

    def team_active(self, db, team_id):
        row = db.execute("SELECT active FROM team_api WHERE team_id=?", (team_id,)).fetchone()
        return bool(row and row["active"])

    # Website API (cookie sessions), runs inside the caller's SQLite transaction.
    def dispatch(self, db, actor, method, path, body):
        settings = self.settings(db)
        if path.startswith("/api/admin/"):
            self.service.require(actor, "admin")
            return self.admin(db, actor, method, path, body, settings)
        self.service.require(actor, "team")
        bound = db.execute("SELECT team_id FROM team_accounts WHERE account_id=?", (actor["id"],)).fetchone()
        if not bound:
            raise APIError(403, "队伍身份未绑定")
        team_id = bound["team_id"]
        enabled = self.config is not None and self.team_active(db, team_id)
        if method == "GET" and path == "/api/team/keys":
            if not enabled:
                return {"enabled": False, "keys": []}
            since = now_ms() - DAY_MS
            keys = [{"id": r["id"], "name": r["name"], "masked": r["masked"], "createdAt": r["created_at"], "lastUsedAt": r["last_used_at"],
                     "used24h": r["day"] / 1e6, "usedTotal": r["total"] / 1e6, "requests": r["requests"]}
                    for r in db.execute("""SELECT k.*, COALESCE(SUM(CASE WHEN u.at>=? THEN u.cost_micro END),0) AS day,
                      COALESCE(SUM(u.cost_micro),0) AS total, COUNT(u.id) AS requests
                      FROM api_keys k LEFT JOIN api_usage u ON u.key_id=k.id WHERE k.team_id=? AND k.active=1
                      GROUP BY k.id ORDER BY k.created_at""", (since, team_id))]
            return {"enabled": True, "baseUrl": self.service.origin + self.service.base_path, "models": self.models(),
                    "maxKeys": settings["maxKeys"], "keys": keys, "logContent": settings["logContent"],
                    "clientRules": settings["clientRules"], "announcement": settings["announcement"],
                    "budget": {"used24h": self.spend(db, team_id, since), "dailyLimit": settings["dailyBudget"],
                               "usedTotal": self.spend(db, team_id), "totalLimit": settings["totalBudget"], "expiresAt": settings["expiresAt"]}}
        if not enabled:
            raise APIError(409, "本队 API 额度尚未开通或已停用")
        if method == "GET" and path == "/api/team/keys/logs":
            return self.logs(db, {"teamId": team_id})  # metadata only; content stays admin-only
        if method == "POST" and path == "/api/team/keys":
            if set(body) != {"name"}:
                raise APIError(400, "密钥字段不正确")
            name = self.text(body["name"], "密钥名称", 40, True)
            if db.execute("SELECT COUNT(*) FROM api_keys WHERE team_id=? AND active=1", (team_id,)).fetchone()[0] >= settings["maxKeys"]:
                raise APIError(409, f"每队最多 {settings['maxKeys']} 个密钥，请先删除不用的密钥")
            key = "gk-" + secrets.token_urlsafe(32)
            key_id = "K" + secrets.token_hex(8)
            masked = key[:7] + "…" + key[-4:]
            db.execute("INSERT INTO api_keys(id,team_id,name,key_hash,masked,created_at) VALUES (?,?,?,?,?,?)",
                       (key_id, team_id, name, self.digest(key), masked, now_ms()))
            self.service.audit(db, actor, "api-key.create", team_id, {"keyId": key_id})
            # The only response that ever carries the full key; only its hash is stored.
            return {"ok": True, "key": key, "item": {"id": key_id, "name": name, "masked": masked}}
        if method == "POST" and path == "/api/team/keys/delete":
            if set(body) != {"id"} or not isinstance(body["id"], str):
                raise APIError(400, "密钥编号不正确")
            if not db.execute("UPDATE api_keys SET active=0 WHERE id=? AND team_id=? AND active=1", (body["id"], team_id)).rowcount:
                raise APIError(404, "密钥不存在")
            self.service.audit(db, actor, "api-key.delete", team_id, {"keyId": body["id"]})
            return {"ok": True}
        raise APIError(404, "接口不存在")

    def admin(self, db, actor, method, path, body, settings):
        if method == "GET" and path == "/api/admin/keys":
            since = now_ms() - DAY_MS
            rows = db.execute("""SELECT t.team_id, t.active,
              (SELECT COUNT(*) FROM api_keys k WHERE k.team_id=t.team_id AND k.active=1) AS key_count FROM team_api t""").fetchall()
            today = (now_ms() + 8 * 3600_000) // DAY_MS * DAY_MS - 8 * 3600_000  # Beijing midnight
            overview = dict(db.execute("""SELECT COUNT(*) AS requests, COALESCE(SUM(cost_micro),0)/1e6 AS cost,
              COALESCE(SUM(input_tokens),0) AS input, COALESCE(SUM(output_tokens),0) AS output, COALESCE(SUM(status>=400 OR incomplete=1),0) AS errors,
              COUNT(DISTINCT team_id) AS teams, CAST(AVG(CASE WHEN status<400 THEN duration_ms END) AS INTEGER) AS avgMs
              FROM api_usage WHERE at>=?""", (today,)).fetchone())
            changes = [{"at": r["at"], "action": r["action"], "target": r["target"], "metadata": json.loads(r["metadata"])}
                       for r in db.execute("SELECT at,action,target,metadata FROM audit WHERE action LIKE 'api-%' ORDER BY id DESC LIMIT 50")]
            with self.lock:
                inflight = {k: v for k, v in self.inflight.items() if v}
            return {"enabled": self.config is not None, "settings": settings, "models": self.models(), "overview": overview,
                    "upstreams": self.public_config(db),
                    "changes": changes,
                    "teams": [{"teamId": r["team_id"], "active": bool(r["active"]), "keyCount": r["key_count"], "inflight": inflight.get(r["team_id"], 0),
                               "used24h": self.spend(db, r["team_id"], since), "usedTotal": self.spend(db, r["team_id"])} for r in rows]}
        if method == "GET" and path == "/api/admin/keys/export":
            self.service.audit(db, actor, "api-usage.export")
            return self.export(db)
        if method == "POST" and path == "/api/admin/keys/settings":
            if not body or not set(body).issubset({"dailyBudget", "totalBudget", "maxKeys", "expiresAt", "logContent", "clientRules", "teamRpm", "announcement", "blockedWords"}):
                raise APIError(400, "API 设置字段不正确")
            for key, value in body.items():
                if key == "clientRules":
                    check_rules(value)
                    ok = True
                elif key == "teamRpm":
                    ok = type(value) is int and 0 <= value <= 10000
                elif key == "announcement":
                    ok = isinstance(value, str) and len(value) <= 500
                elif key == "blockedWords":
                    ok = isinstance(value, list) and len(value) <= 200 and all(isinstance(w, str) and 1 <= len(w.strip()) <= 100 for w in value)
                    value = [w.strip() for w in value] if ok else value
                elif key == "logContent":
                    ok = type(value) is bool
                elif key == "maxKeys":
                    ok = type(value) is int and 1 <= value <= 20
                elif key == "expiresAt":
                    ok = value is None or (type(value) is int and 0 <= value <= 253402300799999)
                else:
                    ok = type(value) in (int, float) and 0 <= value <= 100000
                if not ok:
                    raise APIError(400, {"maxKeys": "每队密钥上限应为1至20的整数", "expiresAt": "到期时间必须是有效的毫秒时间戳或空值", "logContent": "记录内容开关必须为布尔值",
                                               "teamRpm": "每分钟请求上限应为0至10000的整数（0为不限）", "announcement": "公告最多500字",
                                               "blockedWords": "屏蔽词最多200个，每个1至100字"}.get(key, "额度应为0至100000美元"))
                db.execute("UPDATE settings SET value=? WHERE key=?", (json.dumps(value), "keys." + key))
            self.service.audit(db, actor, "api-key.settings", metadata=body)
            return {"ok": True, "settings": self.settings(db)}
        if method == "POST" and path == "/api/admin/keys/upstreams":
            return self.save_upstreams(db, actor, body)
        if method == "POST" and path == "/api/admin/keys/stats":
            return self.stats(db, body)
        if method == "POST" and path == "/api/admin/keys/logs":
            return self.logs(db, body)
        if method == "POST" and path == "/api/admin/keys/log":
            if set(body) != {"id"} or type(body["id"]) is not int:
                raise APIError(400, "记录编号不正确")
            row = db.execute("SELECT * FROM api_logs WHERE usage_id=?", (body["id"],)).fetchone()
            if not row:
                raise APIError(404, "该请求没有记录内容")
            self.service.audit(db, actor, "api-log.view", str(body["id"]))
            return {"id": body["id"], "headers": json.loads(row["headers"]), "request": unpack(row["request"]), "response": unpack(row["response"]),
                    "requestBytes": row["request_bytes"], "responseBytes": row["response_bytes"],
                    "responseTruncated": row["response_bytes"] > RESPONSE_LOG_CAP}
        if method == "POST" and path in {"/api/admin/keys/provision", "/api/admin/keys/revoke"}:
            if set(body) != {"teamId"} or not isinstance(body["teamId"], str):
                raise APIError(400, "队伍不正确")
            team_id = self.service.team_exists(db, body["teamId"])
            if path.endswith("/revoke"):
                if not db.execute("UPDATE team_api SET active=0 WHERE team_id=? AND active=1", (team_id,)).rowcount:
                    raise APIError(409, "该队 API 额度未开通")
            else:
                if self.config is None:
                    raise APIError(409, "尚未配置模型上游，请先在“模型上游”中填写")
                if self.team_active(db, team_id):
                    raise APIError(409, "该队 API 额度已开通")
                db.execute("INSERT INTO team_api VALUES (?,1,?) ON CONFLICT(team_id) DO UPDATE SET active=1", (team_id, now_ms()))
            self.service.audit(db, actor, "api-key." + path.rsplit("/", 1)[1], team_id)
            return {"ok": True}
        raise APIError(404, "接口不存在")

    def scope(self, db, body, allowed):
        """Shared filter for stats and logs: optional teamId / keyId."""
        if not isinstance(body, dict) or not set(body).issubset(allowed):
            raise APIError(400, "筛选条件不正确")
        where, params = [], []
        if body.get("teamId") is not None:
            where.append("u.team_id=?")
            params.append(self.service.team_exists(db, body["teamId"]))
        if body.get("keyId") is not None:
            if not isinstance(body["keyId"], str):
                raise APIError(400, "密钥编号不正确")
            where.append("u.key_id=?")
            params.append(body["keyId"])
        return where, params

    def stats(self, db, body):
        where, params = self.scope(db, body, {"teamId", "keyId", "days"})
        days = body.get("days", 7)
        if type(days) is not int or not 1 <= days <= 90:
            raise APIError(400, "统计天数应为1至90")
        where.append("u.at>=?")
        params.append(now_ms() - days * DAY_MS)
        clause = " AND ".join(where)
        sums = ("COUNT(*) AS requests, COALESCE(SUM(u.input_tokens),0) AS input, COALESCE(SUM(u.output_tokens),0) AS output, "
                "COALESCE(SUM(u.cache_read_tokens),0) AS cacheRead, COALESCE(SUM(u.cost_micro),0)/1e6 AS cost")
        rows = lambda sql: [dict(r) for r in db.execute(sql, params)]
        return {
            "days": days,
            "totals": rows(f"SELECT {sums}, SUM(u.status>=400 OR u.incomplete=1) AS errors, CAST(AVG(u.duration_ms) AS INTEGER) AS avgMs FROM api_usage u WHERE {clause}")[0],
            "byKey": rows(f"""SELECT u.key_id AS keyId, k.name, k.masked, k.active, t.number AS teamNumber, t.name AS teamName, {sums},
              MAX(u.at) AS lastAt FROM api_usage u JOIN api_keys k ON k.id=u.key_id JOIN teams t ON t.id=u.team_id
              WHERE {clause} GROUP BY u.key_id ORDER BY cost DESC"""),
            "byModel": rows(f"SELECT u.model, {sums} FROM api_usage u WHERE {clause} GROUP BY u.model ORDER BY cost DESC"),
            "byClient": rows(f"SELECT u.user_agent AS userAgent, {sums} FROM api_usage u WHERE {clause} GROUP BY u.user_agent ORDER BY requests DESC LIMIT 20"),
            # Calendar days in Beijing time.
            "daily": rows(f"SELECT date(u.at/1000+28800,'unixepoch') AS day, {sums} FROM api_usage u WHERE {clause} GROUP BY day ORDER BY day"),
        }

    def logs(self, db, body):
        where, params = self.scope(db, body, {"teamId", "keyId", "beforeId", "errorsOnly"})
        if body.get("beforeId") is not None:
            if type(body["beforeId"]) is not int:
                raise APIError(400, "分页参数不正确")
            where.append("u.id<?")
            params.append(body["beforeId"])
        if body.get("errorsOnly") is True:
            where.append("(u.status>=400 OR u.incomplete=1)")
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        items = [dict(r) for r in db.execute(f"""SELECT u.id, u.at, u.key_id AS keyId, k.name AS keyName, t.number AS teamNumber, t.name AS teamName,
          u.model, u.path, u.stream, u.status, u.input_tokens AS input, u.output_tokens AS output, u.cache_write_tokens AS cacheWrite,
          u.cache_read_tokens AS cacheRead, u.cost_micro/1e6 AS cost,
          u.duration_ms AS durationMs, u.user_agent AS userAgent, u.incomplete, u.estimated, EXISTS(SELECT 1 FROM api_logs l WHERE l.usage_id=u.id) AS hasContent,
          (SELECT l.request_bytes FROM api_logs l WHERE l.usage_id=u.id) AS requestBytes
          FROM api_usage u JOIN api_keys k ON k.id=u.key_id JOIN teams t ON t.id=u.team_id {clause} ORDER BY u.id DESC LIMIT 50""", params)]
        return {"items": items, "more": len(items) == 50}

    def export(self, db):
        """Usage CSV without request content; formula-like cells are neutralised."""
        out = io.StringIO(newline="")
        writer = csv.writer(out)
        writer.writerow(["时间(UTC毫秒)", "队号", "队名", "密钥", "模型", "接口", "流式", "状态", "输入tokens", "输出tokens", "缓存写入tokens", "缓存读取tokens", "费用USD", "耗时ms", "User-Agent", "中断", "估算计费"])
        for r in db.execute("""SELECT u.*, t.number, t.name AS team_name, k.name AS key_name FROM api_usage u
          JOIN teams t ON t.id=u.team_id JOIN api_keys k ON k.id=u.key_id ORDER BY u.id"""):
            writer.writerow([self.service.csv_safe(v) for v in (r["at"], r["number"], r["team_name"], r["key_name"], r["model"], r["path"],
                             r["stream"], r["status"], r["input_tokens"], r["output_tokens"], r["cache_write_tokens"], r["cache_read_tokens"], r["cost_micro"] / 1e6, r["duration_ms"], r["user_agent"], r["incomplete"], r["estimated"])])
        return ("\ufeff" + out.getvalue()).encode("utf-8")

    def admit(self, team, rpm):
        """Per-team requests-per-minute and concurrency gate; caller must release() on success."""
        current = time.monotonic()
        with self.lock:
            window = self.recent[team]
            while window and window[0] <= current - 60:
                window.popleft()
            if rpm and len(window) >= rpm:
                raise APIError(429, f"本队每分钟最多 {rpm} 次请求，请稍后重试")
            window.append(current)
            # Agent tools fire bursts; queue briefly for a slot instead of failing at once.
            if not self.slot_free.wait_for(lambda: self.inflight[team] < TEAM_CONCURRENCY, timeout=QUEUE_WAIT):
                raise APIError(429, f"本队同时进行的请求最多 {TEAM_CONCURRENCY} 个，排队超时")
            self.inflight[team] += 1

    def reserve(self, handler, call, estimate):
        """After the queue wait: re-check the key, team and access, then atomically reserve the estimated cost.

        sub2api re-checks billing after the slot wait and reserves in-flight budget; this also re-checks the key and team,
        so a revoke or withdrawal during the wait stops the request before it reaches the upstream.
        """
        team = call["key"]["team_id"]
        with self.service.db() as db, self.lock:
            row = self.lookup(db, handler)
            if not row or row["id"] != call["key"]["id"]:
                raise APIError(401, "API 密钥在排队期间已被停用")
            settings = self.settings(db)
            current = now_ms()
            if settings["expiresAt"] is not None and current >= settings["expiresAt"]:
                raise APIError(403, "本活动 API 额度已到期")
            held = (self.reserved[team] + estimate) / 1e6
            if self.spend(db, team, current - DAY_MS) + held > settings["dailyBudget"]:
                raise APIError(429, "本队近24小时额度不足以完成本次请求")
            if self.spend(db, team) + held > settings["totalBudget"]:
                raise APIError(429, "本队总额度不足以完成本次请求")
            self.reserved[team] += estimate

    def unreserve(self, team, estimate):
        with self.lock:
            self.reserved[team] -= estimate

    def release(self, team):
        with self.lock:
            self.inflight[team] -= 1
            self.slot_free.notify_all()

    # Gateway (API keys from SDKs, no cookies or Origin).
    def gateway(self, handler, path):
        call = {"key": None, "path": path, "model": "", "stream": False, "price": None, "usage": [0, 0, 0, 0], "status": 0,
                "request": b"", "response": [], "size": 0, "started": time.monotonic(), "log": False,
                "request_bytes": 0, "reply_bytes": 0, "done": False, "forwarded": False}
        try:
            self.serve(handler, path, call)
        except APIError as error:
            call["status"] = error.status
            self.keep(call, error.message.encode())
            self.record(handler, call)
            handler.close_connection = True
            handler.respond(error.status, {"type": "error", "error": {"type": "gateway_error", "message": error.message}})
        finally:
            self.record(handler, call)  # no-op when already recorded or unauthenticated

    def keep(self, call, data):
        if call["log"]:
            if call["size"] < RESPONSE_LOG_CAP:
                call["response"].append(data)
            call["size"] += len(data)  # true size, even past the cap

    def record(self, handler, call):
        """Store the call once, before the final bytes are sent where possible, so history is never behind the client."""
        if not call["key"] or call.get("recorded"):
            return
        call["recorded"] = True
        usage, price = list(call["usage"]), call["price"] or (0, 0, 0, 0)
        # A forwarded call the upstream answered without finishing (no terminal event) is incomplete, never a plain success.
        incomplete = call["forwarded"] and call["status"] < 400 and not call["done"]
        estimated = False
        if call.get("charge_reserved"):
            usage, incomplete, estimated = [call["request_bytes"] // 4, call.get("reserve_out", 0), 0, 0], True, True
        if call["forwarded"] and call["status"] < 400:
            # sub2api bills only what it observed; a call with no usage would then be free and escape the budget.
            # Fill gaps with its reservation estimate instead: input ~ request bytes / 4, output ~ reply text bytes / 4.
            est_in, est_out = call["request_bytes"] // 4, call["reply_bytes"] // 4
            if not any(usage):
                usage[0], usage[1], estimated = est_in, est_out, True
            elif incomplete and usage[1] < est_out:
                usage[1], estimated = est_out, True
        cost = round(sum(t * p for t, p in zip(usage, price)))  # USD per 1M tokens == micro-USD per token
        at = now_ms()
        with self.service.db() as db:
            usage_id = db.execute("""INSERT INTO api_usage(key_id,team_id,at,model,input_tokens,output_tokens,cache_write_tokens,cache_read_tokens,
              cost_micro,status,path,stream,duration_ms,user_agent,incomplete,estimated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                  (call["key"]["id"], call["key"]["team_id"], at, call["model"], usage[0], usage[1], usage[2], usage[3], cost,
                                                     call["status"], call["path"], int(call["stream"]), int((time.monotonic() - call["started"]) * 1000),
                                                     (handler.headers.get("User-Agent") or "")[:300], int(incomplete), int(estimated))).lastrowid
            db.execute("UPDATE api_keys SET last_used_at=? WHERE id=?", (at, call["key"]["id"]))
            if call["log"]:
                headers = {k: v[:500] for k, v in handler.headers.items() if k.lower() not in SECRET_HEADERS}
                response = b"".join(call["response"])[:RESPONSE_LOG_CAP]
                # Full context, compressed: agent requests resend the whole conversation and compress ~5-10x.
                db.execute("INSERT INTO api_logs VALUES (?,?,?,?,?,?)", (usage_id, json.dumps(headers, ensure_ascii=False),
                           zlib.compress(call["request"], 6), zlib.compress(response, 6), len(call["request"]), call["size"]))

    def lookup(self, db, handler):
        header = handler.headers.get("Authorization", "")
        token = header[7:].strip() if header.startswith("Bearer ") else handler.headers.get("x-api-key", "").strip()
        if not token:
            raise APIError(401, "缺少 API 密钥")
        # Keys stop with the team: API access disabled, team login revoked, or team withdrawn/disqualified.
        return db.execute("""SELECT k.id, k.team_id FROM api_keys k JOIN team_api t ON t.team_id=k.team_id
          JOIN team_accounts ta ON ta.team_id=k.team_id JOIN accounts a ON a.id=ta.account_id AND a.active=1
          LEFT JOIN team_details d ON d.team_id=k.team_id
          WHERE k.key_hash=? AND k.active=1 AND t.active=1 AND COALESCE(d.status,'active')='active'""", (self.digest(token),)).fetchone()

    def authenticate(self, handler, call=None):
        with self.service.db() as db:
            row = self.lookup(db, handler)
            if not row:
                raise APIError(401, "API 密钥无效或已停用")
            settings = self.settings(db)
            if call is not None:
                call["key"], call["log"], call["settings"] = row, settings["logContent"], settings
            reason = blocked_by(settings["clientRules"], handler.headers)
            if reason:
                raise APIError(403, reason)
            if call is not None:
                current = now_ms()
                if settings["expiresAt"] is not None and current >= settings["expiresAt"]:
                    raise APIError(403, "本活动 API 额度已到期")
                # Cheap early reject; the binding check is reserve() after the queue wait.
                if self.spend(db, row["team_id"], current - DAY_MS) >= settings["dailyBudget"]:
                    raise APIError(429, "本队近24小时额度已用完")
                if self.spend(db, row["team_id"]) >= settings["totalBudget"]:
                    raise APIError(429, "本队总额度已用完")
        return row

    def read_body(self, handler):
        if handler.headers.get("Transfer-Encoding") or len(handler.headers.get_all("Content-Length", [])) != 1:
            raise APIError(411, "请求需要 Content-Length")
        try:
            length = int(handler.headers["Content-Length"])
        except ValueError:
            raise APIError(400, "请求长度不正确") from None
        if not 0 < length <= BODY_LIMIT:
            raise APIError(413, "请求内容超过4MB限制")
        if handler.headers.get_content_type() != "application/json":
            raise APIError(415, "请使用JSON请求")
        try:
            handler.raw_body = handler.rfile.read(length)
            body = json.loads(handler.raw_body)
        except (UnicodeError, ValueError):
            raise APIError(400, "JSON格式不正确") from None
        if not isinstance(body, dict):
            raise APIError(400, "JSON内容必须是对象")
        return body

    def serve(self, handler, path, call):
        if self.config is None:
            raise APIError(404, "接口不存在")
        if path == "/v1/models" and handler.command == "GET":
            self.authenticate(handler)  # client rules apply; not metered or logged
            data = [{"id": m, "object": "model", "owned_by": name} for name, models in self.models().items() for m in models]
            return handler.respond(200, {"object": "list", "data": data})
        if path == "/v1/messages/count_tokens" and handler.command == "POST" and "anthropic" in self.config:
            key = self.authenticate(handler, call)  # expiry, budget and client rules
            call["log"] = False  # metadata row only, zero cost
            self.admit(key["team_id"], call["settings"]["teamRpm"])
            try:
                self.reserve(handler, call, 0)  # free call, but a revoke during the wait still stops it
                return self.count_tokens(handler, call)
            finally:
                self.release(key["team_id"])
        name, suffix = ROUTES.get(path, (None, None))
        if handler.command != "POST" or name not in self.config:
            raise APIError(404, "接口不存在")
        upstream = self.config[name]
        key = self.authenticate(handler, call)
        body = self.read_body(handler)
        call["request"] = handler.raw_body if call["log"] else b""  # exactly what the client sent
        call["request_bytes"] = len(handler.raw_body)
        model = resolve(upstream, body.get("model"))
        call["model"] = model or (body["model"][:100] if isinstance(body.get("model"), str) else "")
        if model is None:
            raise APIError(400, "模型不在本活动开放列表中，可用模型见 /v1/models")
        body["model"] = model
        call["price"] = upstream["models"][model]
        settings = call["settings"]
        if settings["blockedWords"]:
            # Whole decoded request (ensure_ascii=False undoes \u escapes); ponytail: substring scan, fine for a short list.
            text = json.dumps(body, ensure_ascii=False).lower()
            hit = next((w for w in settings["blockedWords"] if w.lower() in text), None)
            if hit:
                raise APIError(403, "请求内容包含本活动禁止的词语，已拦截")
        if "stream" in body and type(body["stream"]) is not bool:
            raise APIError(400, "stream 必须是布尔值")
        tools = body.get("tools") or []
        if BILLED_FIELDS & set(body) or not isinstance(tools, list) or any(
                not isinstance(t, dict) or ("type" in t and not str(t["type"]).startswith(CLIENT_TOOL_TYPES)) for t in tools):
            # Server-side tools are billed by the upstream outside token usage, so they would escape team budgets.
            raise APIError(400, "本活动不开放上游服务端工具（网页搜索、代码执行、MCP 等），请在客户端实现工具")
        if body.get("n", 1) != 1 or "best_of" in body:
            # Several completions per call multiply output cost beyond the reservation; one reply per request.
            raise APIError(400, "本活动每次请求只生成一条回复（n 必须为 1，不支持 best_of）")
        for field in PRICING_FIELDS:
            body.pop(field, None)
        call["stream"] = stream = body.get("stream") is True
        if stream and name == "openai":
            options = body.get("stream_options")
            body["stream_options"] = {**(options if isinstance(options, dict) else {}), "include_usage": True}
        team = key["team_id"]
        price = call["price"]
        call["reserve_out"] = bound_output(body)
        estimate = round(call["request_bytes"] // 4 * price[0] + call["reserve_out"] * price[1])
        self.admit(team, settings["teamRpm"])
        try:
            self.reserve(handler, call, estimate)
            try:
                self.forward(handler, name, upstream, suffix, body, stream, call)
            except APIError as error:
                # record() writes once, so the final status and reason must be set before the finally below runs.
                call["status"] = error.status
                self.keep(call, error.message.encode())
                raise
            finally:
                self.record(handler, call)  # the real cost lands before the reservation is dropped
                self.unreserve(team, estimate)
        finally:
            self.release(team)

    def open_with_retry(self, request):
        """Retry upstream overload and connection errors; safe because nothing has reached the client yet."""
        for delay in (*RETRY_DELAYS, None):
            try:
                return self.opener.open(request, timeout=UPSTREAM_TIMEOUT)
            except urllib.error.HTTPError as error:
                if delay is None or error.code not in RETRY_STATUSES:
                    raise
                error.close()
            except OSError as error:
                # Only connection failures are safe: a timeout may mean the upstream is still generating (and billing).
                if delay is None or not isinstance(getattr(error, "reason", error), (ConnectionRefusedError, socket.gaierror)):
                    raise
            time.sleep(delay)

    def count_tokens(self, handler, call):
        """Claude Code calls this before requests; passed through at zero cost, recorded without content."""
        body = self.read_body(handler)
        upstream = self.config["anthropic"]
        model = resolve(upstream, body.get("model"))
        call["model"] = model or ""
        if model is None:
            raise APIError(400, "模型不在本活动开放列表中，可用模型见 /v1/models")
        body["model"] = model
        headers = {"Content-Type": "application/json", "x-api-key": upstream["key"],
                   "anthropic-version": handler.headers.get("anthropic-version", "2023-06-01")}
        beta = allowed_betas(handler.headers.get("anthropic-beta"))
        if beta:
            headers["anthropic-beta"] = beta
        request = urllib.request.Request(upstream["url"] + "/messages/count_tokens", data=json.dumps(body).encode(), headers=headers, method="POST")
        try:
            with self.open_with_retry(request) as response:
                status, payload, content_type = response.status, response.read(1_000_000), response.headers.get("Content-Type", "application/json")
        except urllib.error.HTTPError as error:
            if error.code in {401, 403} or error.code >= 500:
                raise APIError(502, "上游模型服务暂时不可用，请稍后重试") from None
            status, payload, content_type = error.code, error.read(1_000_000), error.headers.get("Content-Type", "application/json")
        except OSError:
            raise APIError(502, "上游模型服务暂时不可用，请稍后重试") from None
        call["status"] = status
        self.record(handler, call)
        handler.headers_out(status, content_type, len(payload))
        handler.wfile.write(payload)

    def upstream_failed(self, call, error):
        """Turn a network failure into the team's error response, charging the reservation when the upstream may have run."""
        reason = getattr(error, "reason", error)
        if isinstance(reason, (TimeoutError, socket.timeout)):
            # The request reached the upstream, which may still be generating and billing: charge the reserved estimate
            # rather than nothing, so timeouts cannot be used to run requests for free.
            call["charge_reserved"] = True
            self.keep(call, f"上游响应超时（{UPSTREAM_TIMEOUT} 秒）\n".encode())
            raise APIError(504, "上游模型服务响应超时，本次按预留额度计费") from None
        self.keep(call, f"上游连接失败：{type(reason).__name__}\n".encode())
        raise APIError(502, "上游模型服务暂时不可用，请稍后重试") from None

    def forward(self, handler, name, upstream, suffix, body, stream, call):
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream" if stream else "application/json"}
        if name == "openai":
            headers["Authorization"] = "Bearer " + upstream["key"]
        else:
            headers["x-api-key"] = upstream["key"]
            headers["anthropic-version"] = handler.headers.get("anthropic-version", "2023-06-01")
            beta = allowed_betas(handler.headers.get("anthropic-beta"))
            if beta:
                headers["anthropic-beta"] = beta
        request = urllib.request.Request(upstream["url"] + suffix, data=json.dumps(body).encode(), headers=headers, method="POST")
        handler.connection.settimeout(UPSTREAM_TIMEOUT)
        try:
            response = self.open_with_retry(request)
        except urllib.error.HTTPError as error:
            if error.code in {401, 403} or error.code >= 500:  # our credentials or an upstream outage
                # Hidden from the team, kept for admins; the upstream does not bill a request it rejected or failed.
                self.keep(call, f"上游返回 HTTP {error.code}\n".encode())
                raise APIError(502, "上游模型服务暂时不可用，请稍后重试") from None
            payload = error.read(1_000_000)  # client errors (bad params, 429) pass through for debugging
            call["status"] = error.code
            self.keep(call, payload)
            self.record(handler, call)
            handler.close_connection = True
            handler.headers_out(error.code, error.headers.get("Content-Type", "application/json"), len(payload))
            handler.wfile.write(payload)
            return
        except OSError as error:
            self.upstream_failed(call, error)
        call["status"], call["forwarded"] = response.status, True
        usage = call["usage"]
        with response:
            content_type = response.headers.get("Content-Type", "application/json")
            if content_type.startswith("text/event-stream"):
                handler.close_connection = True  # HTTP/1.0: the stream ends when the connection closes
                handler.headers_out(response.status, content_type, None, extra={"X-Accel-Buffering": "no"})
                client_open = True
                try:
                    for line in response:
                        if line.startswith(b"data:"):
                            if line[5:].strip() == b"[DONE]":
                                call["done"] = True
                            else:
                                try:
                                    event = json.loads(line[5:])
                                except ValueError:
                                    event = None
                                read_usage(name, event, usage)
                                stream_progress(name, event, call)
                        self.keep(call, line)
                        if client_open:
                            try:
                                handler.wfile.write(line)
                            except OSError:
                                # Keep draining so the usage event at the end is still billed (upstream timeout bounds this).
                                client_open = False
                except OSError:
                    pass  # upstream went away; bill what was produced
            else:
                try:
                    payload = response.read(50_000_000)
                except OSError as error:  # nothing sent to the client yet, so this can still become a clean error
                    self.upstream_failed(call, error)
                call["reply_bytes"] = len(payload)
                try:
                    read_usage(name, json.loads(payload), usage)
                    call["done"] = True  # a complete JSON body is a finished reply
                except ValueError:
                    pass
                self.keep(call, payload)
                self.record(handler, call)
                handler.headers_out(response.status, content_type, len(payload))
                handler.wfile.write(payload)
