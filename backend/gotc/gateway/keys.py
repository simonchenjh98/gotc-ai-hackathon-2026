"""Team API keys, organizer configuration and gateway service entry points."""

import asyncio
import hashlib
import ipaddress
import json
import secrets
import sqlite3
import threading
import time
import logging
from collections import Counter, defaultdict, deque
from contextlib import closing
from urllib.parse import urlsplit

import anyio
import httpx

from backend.gotc.http import request_models as inputs
from backend.gotc.errors import APIError
from backend.gotc.observability import GATEWAY_RUNTIME_PATH
from . import transport
from .admission import QUEUE_WAIT, BodyUploads, FairSlots, GatewayCapacity
from .memory import BulkMemory
from .accounting import (
    CAPTURE_BLOCK, DAY_MS, RESPONSE_LOG_CAP, UsageLedger, beijing_midnight, initialize_schema, now_ms,
    task_price, unpack,
)
from .config import DEFAULTS, check_url, load_config, parse_config, settings
from .gateway_media import MediaGateway
from .gateway_pricing import Catalog
from .gateway_tasks import AsyncTasks
from .protocol_errors import GatewayError, error_info, upstream_message
from .requests import client_ip, gateway_json


__all__ = ["KeyService", "load_config"]


AUTH_FAIL_LIMIT, AUTH_FAIL_WINDOW, AUTH_BLOCK = 120, 60, 60  # per client IP, as sub2api's invalid-auth limiter


KEY_CREATE_PER_HOUR = 60  # as sub2api; deleting a key does not give the quota back


KEY_HASH_ITERATIONS = 1000  # keys are 256-bit random, so the server-side pepper, not iterations, carries the strength


def load_pepper(service):
    def no_issued_keys():
        if not service.db_path.exists():
            return True
        with closing(sqlite3.connect(service.db_path.as_uri() + "?mode=ro", uri=True, timeout=10)) as db:
            schema = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='api_keys'").fetchone()
            return not schema or not db.execute("SELECT 1 FROM api_keys LIMIT 1").fetchone()

    raw = service.credentials.stable_key("api-key-pepper", lambda: secrets.token_hex(32).encode(),
                                         can_generate=no_issued_keys)
    try:
        key = bytes.fromhex(raw.decode("ascii"))
    except ValueError:
        raise RuntimeError("Invalid API key pepper") from None
    if len(key) != 32:
        raise RuntimeError("Invalid API key pepper")
    return key


class KeyService:
    """Coordinate team-key administration, authentication and request admission."""

    def __init__(self, service, config, text):
        self.service, self.text = service, text
        self.capacity = GatewayCapacity.from_env()
        self.pepper = load_pepper(service)
        self.seed = config  # raw gateway.json content, copied into the database once
        self.config = parse_config(config)
        self.lock = threading.Lock()
        self.config_publish_lock = threading.Lock()
        capacity = self.capacity
        self.model_slots = FairSlots(team_running=capacity.team_running, global_running=capacity.global_running,
                                     team_queue=capacity.team_queue, global_queue=capacity.global_queue)
        self.task_slots = FairSlots(team_running=capacity.team_tasks, global_running=capacity.global_tasks)
        self.speech_slots = FairSlots(team_running=capacity.team_speech, global_running=capacity.global_speech)
        self.body_uploads = BodyUploads(capacity)
        self.lanes = {"model": self.model_slots, "task": self.task_slots, "speech": self.speech_slots}
        self.memory = BulkMemory(spool_mib=capacity.spool_mib)
        self.recent = defaultdict(deque)  # team -> monotonic times of requests in the last minute
        self.ledger = UsageLedger(service, self.currency)
        self.auth_failures, self.auth_blocked = defaultdict(deque), {}
        self.auth_failures_seen = deque(maxlen=100000)  # failed key attempts, for the admin overview
        self.runtime_client = None  # The combined developer app owns the live counters locally.
        self.opener = transport.make_opener(max_connections=capacity.provider_connections)
        self.catalog = Catalog()
        self.tasks = AsyncTasks(self, pricing=self.task_price)
        self.media = MediaGateway(self, self.catalog, self.tasks)

    @staticmethod
    def is_gitee(upstream):
        return upstream.get("url", "").rstrip("/") == "https://ai.gitee.com/v1"

    def currency(self):
        kinds = {"CNY" if self.is_gitee(u) else "USD" for u in (self.config or {}).values()}
        return next(iter(kinds)) if len(kinds) == 1 else "MIXED"

    def task_price(self, task, payload, headers):
        return task_price(task, payload, headers)

    def digest(self, token):
        return hashlib.pbkdf2_hmac("sha256", token.encode(), self.pepper, KEY_HASH_ITERATIONS).hex()

    def initialize(self, db):
        initialize_schema(db)
        self.tasks.initialize(db)
        self.load_runtime(db)

    def load_runtime(self, db):
        for key, value in DEFAULTS.items():
            db.execute("INSERT OR IGNORE INTO settings VALUES (?,?)", (key, json.dumps(value)))
        if self.seed:
            db.execute("INSERT OR IGNORE INTO gateway_config VALUES (1,?,?)", (json.dumps(self.seed, ensure_ascii=False), now_ms()))
        row = db.execute("SELECT data FROM gateway_config WHERE id=1").fetchone()
        self._config_raw = row["data"] if row else None
        self.config = parse_config(json.loads(self._config_raw)) if row else None

    def refresh_config(self):
        # The platform and gateway have separate processes. Read only committed state.
        with self.config_publish_lock, self.service.db() as db:
            row = db.execute("SELECT data FROM gateway_config WHERE id=1").fetchone()
            raw = row["data"] if row else None
            if raw != self._config_raw:
                self.config = parse_config(json.loads(raw)) if raw else None
                self._config_raw = raw
            return self.config

    def raw_config(self, db):
        row = db.execute("SELECT data FROM gateway_config WHERE id=1").fetchone()
        return json.loads(row["data"]) if row else {}

    def public_config(self, db):
        """Upstream settings for the admin panel; the upstream API key is never sent back."""
        return {name: {**{k: v for k, v in item.items() if k != "key"}, "keyHint": "…" + item["key"][-4:] if len(item["key"]) > 8 else "已设置"}
                for name, item in self.raw_config(db).items()}

    def save_upstreams(self, db, actor, body: inputs.UpstreamSettings):
        """Replace the upstream list. An empty or missing "key" keeps the saved key for that upstream."""
        if not inputs.accepts_fields(body, inputs.UpstreamSettings) or not isinstance(body["upstreams"], dict):
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
            for optional in ("name", "aliases", "defaultPrice"):
                if not item.get(optional):
                    item.pop(optional, None)
            new[name] = item
        try:
            config = parse_config(new)
        except ValueError as error:
            raise APIError(400, "上游设置不正确：" + str(error)) from None
        db.execute("INSERT INTO gateway_config VALUES (1,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at",
                   (json.dumps(new, ensure_ascii=False), now_ms()))
        self.service.audit(db, actor, "api-upstreams.save", metadata={name: {"url": item["url"], "models": list(item["models"]),
                                                                            "keyChanged": item["key"] != old.get(name, {}).get("key")}
                                                                     for name, item in new.items()})
        result = {"ok": True, "upstreams": self.public_config(db),
                  "models": {name: list(item["models"]) for name, item in (config or {}).items()}}
        # dispatch already holds the SQLite write transaction. Finish every DB operation before
        # publishing, and serialize commit + publication so a later save cannot publish first.
        # There must be no subsequent DB writes or fallible response building on this path.
        with self.config_publish_lock:
            db.commit()
            self.config = config
            self._config_raw = json.dumps(new, ensure_ascii=False)
        return result

    def settings(self, db):
        return settings(db)

    def models(self):
        # Config order is kept: the first model of each upstream is the default shown in setup snippets.
        return {name: list(item["models"]) for name, item in (self.config or {}).items()}

    def error_secrets(self, call=None):
        # Include the request's snapshot: an organizer can rotate or remove an upstream while a call is running.
        current = tuple(item["key"] for item in (self.config or {}).values())
        return current + tuple((call or {}).get("secrets", ()))

    def team_active(self, db, team_id):
        row = db.execute("SELECT active FROM team_api WHERE team_id=?", (team_id,)).fetchone()
        return bool(row and row["active"])

    def dispatch(self, db, actor, method, path, body):
        settings = self.settings(db)
        if path.startswith("/api/admin/"):
            self.service.require(actor, "admin")
            return self.admin(db, actor, method, path, body, settings)
        self.service.require(actor, "team")
        team_id = self.service.bound_submission_team(db, actor)['id']
        enabled = self.config is not None and self.team_active(db, team_id)
        if method == "GET" and path == "/api/team/keys":
            if not enabled:
                return {"enabled": False, "keys": []}
            since = now_ms() - DAY_MS
            keys = [{"id": r["id"], "name": r["name"], "masked": r["masked"], "createdAt": r["created_at"], "lastUsedAt": r["last_used_at"],
                     "used24h": self.ledger.billing_total(day), "usedTotal": self.ledger.billing_total(total),
                     "billing24h": day, "billing": total, "requests": r["requests"]}
                    for r in db.execute("""SELECT k.*, COUNT(u.id) AS requests
                      FROM api_keys k LEFT JOIN api_usage u ON u.key_id=k.id WHERE k.team_id=? AND k.active=1
                      GROUP BY k.id ORDER BY k.created_at""", (team_id,))
                    for day, total in [(self.ledger.billing_summary(db, team_id, since, r["id"]),
                                        self.ledger.billing_summary(db, team_id, key_id=r["id"]))]]
            return {"enabled": True, "baseUrl": self.service.origin + self.service.base_path, "models": self.models(), "currency": self.currency(),
                    "maxKeys": settings["maxKeys"], "keys": keys, "logContent": settings["logContent"],
                    "announcement": settings["announcement"],
                    "usage": {"used24h": self.ledger.spend(db, team_id, since), "usedTotal": self.ledger.spend(db, team_id)},
                    "billing": self.ledger.billing_summary(db, team_id),
                    "billing24h": self.ledger.billing_summary(db, team_id, since),
                    "expiresAt": settings["expiresAt"]}
        if not enabled:
            raise APIError(409, "本队 API 访问权限尚未开通或已停用")
        if method == "GET" and path == "/api/team/keys/logs":
            items = self.ledger.logs(db, {"teamId": team_id})  # metadata only; content stays admin-only
            for item in items["items"]:  # teams see what failed and whose fault it was, not upstream internals
                for field in ("errorMessage", "upstreamStatus", "upstreamRequestId", "clientIp"):
                    item.pop(field)
            return items
        if method == "POST" and path == "/api/team/keys":
            if not inputs.accepts_fields(body, inputs.NameBody):
                raise APIError(400, "密钥字段不正确")
            name = self.text(body["name"], "密钥名称", 40, True)
            if db.execute("SELECT COUNT(*) FROM api_keys WHERE team_id=? AND active=1", (team_id,)).fetchone()[0] >= settings["maxKeys"]:
                raise APIError(409, f"每队最多 {settings['maxKeys']} 个密钥，请先删除不用的密钥")
            if db.execute("SELECT COUNT(*) FROM api_keys WHERE team_id=? AND created_at>?", (team_id, now_ms() - 3600_000)).fetchone()[0] >= KEY_CREATE_PER_HOUR:
                # As sub2api: deleting does not give the hourly creations back, so delete-and-recreate cannot loop.
                raise APIError(429, f"每队每小时最多创建 {KEY_CREATE_PER_HOUR} 个密钥，请稍后再试")
            key = "gk-" + secrets.token_urlsafe(32)
            key_id = "K" + secrets.token_hex(8)
            masked = key[:7] + "…" + key[-4:]
            db.execute("INSERT INTO api_keys(id,team_id,name,key_hash,masked,created_at) VALUES (?,?,?,?,?,?)",
                       (key_id, team_id, name, self.digest(key), masked, now_ms()))
            self.service.audit(db, actor, "api-key.create", team_id, {"keyId": key_id})
            # The only response that ever carries the full key; only its hash is stored.
            return {"ok": True, "key": key, "item": {"id": key_id, "name": name, "masked": masked}}
        if method == "POST" and path == "/api/team/keys/delete":
            if not inputs.accepts_fields(body, inputs.DeleteKey) or not isinstance(body["id"], str):
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
            today = beijing_midnight()
            overview = dict(db.execute("""SELECT COUNT(*) AS requests, COALESCE(SUM(cost_micro),0)/1e6 AS cost,
              COALESCE(SUM(input_tokens),0) AS input, COALESCE(SUM(output_tokens),0) AS output,
              COALESCE(SUM(cache_write_tokens),0) AS cacheWrite, COALESCE(SUM(cache_read_tokens),0) AS cacheRead,
              COALESCE(SUM(status>=400 OR incomplete=1),0) AS errors,
              COUNT(DISTINCT team_id) AS teams, CAST(AVG(CASE WHEN status<400 THEN duration_ms END) AS INTEGER) AS avgMs
              FROM api_usage WHERE at>=?""", (today,)).fetchone())
            recent = db.execute("SELECT COUNT(*), COALESCE(SUM(input_tokens+output_tokens+cache_write_tokens+cache_read_tokens),0) FROM api_usage WHERE at>=?",
                                (now_ms() - 300_000,)).fetchone()
            overview["rpm"], overview["tpm"] = round(recent[0] / 5, 1), round(recent[1] / 5)  # last 5 minutes, as sub2api
            ok = db.execute("SELECT COUNT(*) FROM api_usage WHERE at>=? AND status<400 AND incomplete=0", (today,)).fetchone()[0]
            overview["p95Ms"] = db.execute("""SELECT duration_ms FROM api_usage WHERE at>=? AND status<400 AND incomplete=0
              ORDER BY duration_ms LIMIT 1 OFFSET ?""", (today, max(0, int(ok * 0.95) - 1))).fetchone()[0] if ok else None
            overview["avgFirstTokenMs"] = db.execute("SELECT CAST(AVG(first_token_ms) AS INTEGER) FROM api_usage WHERE at>=?", (today,)).fetchone()[0]
            storage = self.ledger.content.summary()
            changes = [{"at": r["at"], "action": r["action"], "target": r["target"], "metadata": json.loads(r["metadata"])}
                       for r in db.execute("SELECT at,action,target,metadata FROM audit WHERE action LIKE 'api-%' ORDER BY id DESC LIMIT 50")]
            overview["billing"] = self.ledger.billing_summary(db, since=today)
            overview["cost"] = self.ledger.billing_total(overview["billing"])
            return {"enabled": self.config is not None, "settings": settings, "models": self.models(), "overview": overview, "currency": self.currency(),
                    "upstreams": self.public_config(db), "storage": storage,
                    "changes": changes,
                    "teams": [{"teamId": r["team_id"], "active": bool(r["active"]), "keyCount": r["key_count"],
                               "used24h": self.ledger.billing_total(day), "usedTotal": self.ledger.billing_total(total),
                               "billing24h": day, "billing": total} for r in rows
                              for day, total in [(self.ledger.billing_summary(db, r["team_id"], since),
                                                  self.ledger.billing_summary(db, r["team_id"]))]]}
        if method == "GET" and path == "/api/admin/keys/export":
            self.service.audit(db, actor, "api-usage.export")
            return self.ledger.export(db)
        if method == "POST" and path == "/api/admin/keys/settings":
            if not body or not inputs.accepts_fields(body, inputs.GatewaySettings):
                raise APIError(400, "API 设置字段不正确")
            before = dict(settings)
            for key, value in body.items():
                if key == "logRetentionHours":
                    ok = type(value) is int and 1 <= value <= 8760
                elif key == "logMaxMB":
                    ok = type(value) is int and 50 <= value <= 102400
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
                if not ok:
                    raise APIError(400, {"maxKeys": "每队密钥上限应为1至20的整数", "expiresAt": "到期时间必须是有效的毫秒时间戳或空值", "logContent": "记录内容开关必须为布尔值",
                                               "teamRpm": "每分钟请求上限应为0至10000的整数（0为不限）", "announcement": "公告最多500字",
                                               "blockedWords": "屏蔽词最多200个，每个1至100字",
                                               "logRetentionHours": "调用内容保留时长应为1至8760小时", "logMaxMB": "调用内容总容量应为50至102400MB"}.get(key, "API 设置值不正确"))
                db.execute("UPDATE settings SET value=? WHERE key=?", (json.dumps(value), "keys." + key))
            if "logContent" in body and before["logContent"] != body["logContent"]:
                db.execute("INSERT OR REPLACE INTO settings VALUES ('keys.contentEpoch',json_quote(lower(hex(randomblob(16)))))")
            changed = {k: [before.get(k), v] for k, v in self.settings(db).items() if k in body and before.get(k) != v}
            self.service.audit(db, actor, "api-key.settings", metadata=changed)
            return {"ok": True, "settings": self.settings(db)}
        if method == "POST" and path == "/api/admin/keys/logs/clear":
            deleted = self.prune_logs(db, settings, everything=True)
            self.service.audit(db, actor, "api-log.clear", metadata={"deleted": deleted})
            return {"ok": True, "deleted": deleted}
        if method == "GET" and path.startswith(("/api/admin/keys/upstreams/test/", "/api/admin/keys/upstreams/models/")):
            # GET on purpose: these call the upstream, and POSTs run inside a write transaction that would block others.
            kind = path.rsplit("/", 1)[1]
            if not self.config or kind not in self.config:
                raise APIError(404, "该类上游尚未配置")
            result = self.probe_upstream(kind, models_only=path.startswith("/api/admin/keys/upstreams/models/"))
            self.service.audit(db, actor, "api-upstreams.test", kind, {"ok": result["ok"], "status": result.get("status")})
            return result
        if method == "POST" and path == "/api/admin/keys/upstreams":
            return self.save_upstreams(db, actor, body)
        if method == "POST" and path == "/api/admin/keys/stats":
            return self.ledger.stats(db, body)
        if method == "POST" and path == "/api/admin/keys/logs":
            return self.ledger.logs(db, body)
        if method == "POST" and path == "/api/admin/keys/log":
            if not inputs.accepts_fields(body, inputs.LogDetails) or type(body["id"]) is not int:
                raise APIError(400, "记录编号不正确")
            row = self.ledger.content.detail(db, body["id"])
            if not row:
                raise APIError(404, "该请求没有记录内容")
            self.service.audit(db, actor, "api-log.view", str(body["id"]))
            return {"id": body["id"], "requestId": row["request_id"], "headers": json.loads(row["headers"]), "request": unpack(row["request"]), "response": unpack(row["response"]),
                    "requestBytes": row["request_bytes"], "responseBytes": row["response_bytes"],
                    "responseTruncated": row["response_bytes"] > RESPONSE_LOG_CAP}
        if method == "POST" and path in {"/api/admin/keys/provision", "/api/admin/keys/revoke"}:
            if not inputs.accepts_fields(body, inputs.TeamIdBody) or not isinstance(body["teamId"], str):
                raise APIError(400, "队伍不正确")
            team_id = self.service.team_exists(db, body["teamId"])
            if path.endswith("/revoke"):
                if not db.execute("UPDATE team_api SET active=0 WHERE team_id=? AND active=1", (team_id,)).rowcount:
                    raise APIError(409, "该队 API 访问权限未开通")
            else:
                self.service.team_lifecycle.require_available(db,team_id)
                if self.config is None:
                    raise APIError(409, "尚未配置模型上游，请先在“模型上游”中填写")
                if self.team_active(db, team_id):
                    raise APIError(409, "该队 API 访问权限已开通")
                db.execute("INSERT INTO team_api(team_id,active,created_at) VALUES (?,1,?) ON CONFLICT(team_id) DO UPDATE SET active=1", (team_id, now_ms()))
            self.service.audit(db, actor, "api-key." + path.rsplit("/", 1)[1], team_id)
            return {"ok": True}
        raise APIError(404, "接口不存在")

    def detect_models(self, body: inputs.ProbeModels, cookie, expected_login_id):
        """Read a draft upstream's catalog without saving configuration or holding the writer lock."""
        with self.service.db() as db:
            self.service.require(self.service.account(db, cookie), "admin")
            if not expected_login_id:
                raise APIError(428, "请刷新页面并核对管理员登录身份")
            if expected_login_id != self.service.login_id(cookie):
                raise APIError(401, "登录身份已变化，请重新登录后操作")
            if not inputs.accepts_fields(body, inputs.ProbeModels) or not isinstance(body["kind"], str) or body["kind"] not in {"openai", "anthropic"}:
                raise APIError(400, "模型列表请求字段不正确")
            kind, key = body["kind"], body["key"]
            try:
                url = check_url(body["url"])
                parts = urlsplit(url)
                if parts.username is not None or parts.password is not None:
                    raise ValueError("credentials in URL")
                parts.port  # Validate explicit ports before constructing an upstream request.
            except (ValueError, TypeError):
                raise APIError(400, "接口地址应为 HTTPS Base URL（本机地址可用 HTTP），不能含查询参数或账号密码") from None
            if not isinstance(key, str) or len(key) > 4096 or any(ord(c) < 32 or ord(c) > 126 for c in key):
                raise APIError(400, "API Key 格式不正确")
            key = key.strip()
            if not key:
                saved = self.raw_config(db).get(kind)
                if not saved:
                    raise APIError(400, "请先填写 API Key")
                if urlsplit(url).netloc.lower() != urlsplit(saved["url"]).netloc.lower():
                    raise APIError(400, "上游地址已更改，请重新填写 API Key")
                key = saved["key"]
        return self.probe_upstream(kind, models_only=True, upstream={"url": url, "key": key})

    def probe_upstream(self, kind, models_only=False, upstream=None):
        """Probe a saved or draft upstream: generate one token or read its model catalog."""
        upstream = upstream if upstream is not None else self.config[kind]
        headers = {"Content-Type": "application/json", "User-Agent": "gotc-gateway/1.0"}
        if kind == "openai":
            headers["Authorization"] = "Bearer " + upstream["key"]
        else:
            headers["x-api-key"], headers["anthropic-version"] = upstream["key"], "2023-06-01"
        if models_only:
            method, url, content = "GET", upstream["url"] + "/models", None
        else:
            model = next(iter(upstream["models"]))
            body = {"model": model, "max_tokens": 1, "messages": [{"role": "user", "content": "hi"}]}
            url = upstream["url"] + ("/chat/completions" if kind == "openai" else "/messages")
            method, content = "POST", gateway_json(body)
        started = time.monotonic()
        try:
            try:
                status, payload = anyio.from_thread.run(transport.probe, self.opener, method, url, headers, content)
            except anyio.NoEventLoopError:
                async def standalone_probe():
                    try:
                        return await transport.probe(self.opener, method, url, headers, content)
                    finally:
                        await self.opener.close()
                status, payload = asyncio.run(standalone_probe())
        except OSError as error:
            return {"ok": False, "status": None, "latencyMs": int((time.monotonic() - started) * 1000), "message": transport.failure_reason(error)}
        latency = int((time.monotonic() - started) * 1000)
        if len(payload) > 2_000_000:
            return {"ok": False, "status": status, "latencyMs": latency, "message": "上游返回内容过大，请手动填写模型"}
        if not 200 <= status < 300:
            return {"ok": False, "status": status, "latencyMs": latency,
                    "message": upstream_message(payload, self.error_secrets() + (upstream["key"],))}
        if not models_only:
            return {"ok": True, "status": status, "latencyMs": latency, "model": model, "message": "连接正常，上游已返回回复"}
        try:
            listed = json.loads(payload).get("data")
        except (ValueError, AttributeError, RecursionError):
            listed = None
        ids = sorted({m["id"] for m in listed if isinstance(m, dict) and isinstance(m.get("id"), str)
                      and 0 < len(m["id"]) <= 100 and not any(c.isspace() or ord(c) < 32 for c in m["id"])}) if isinstance(listed, list) else []
        return {"ok": bool(ids), "status": status, "latencyMs": latency, "models": ids[:500],
                "message": f"上游提供 {len(ids)} 个模型" if ids else "上游未返回模型列表"}

    def connect_runtime_socket(self, path):
        limits = httpx.Limits(max_connections=3, max_keepalive_connections=0)
        self.runtime_client = httpx.Client(
            base_url="http://localhost", transport=httpx.HTTPTransport(uds=path, limits=limits, retries=0),
            trust_env=False, follow_redirects=False, timeout=httpx.Timeout(0.5, connect=0.2, pool=0.1),
        )

    def admission_snapshot(self):
        running, waiting = Counter(), Counter()
        for lane in self.lanes.values():
            active, queued = lane.snapshot()
            running.update(active)
            waiting.update(queued)
        return running, waiting

    def runtime_snapshot(self):
        now = time.monotonic()
        running, waiting = self.admission_snapshot()
        with self.lock:
            return {
                "sampledAt": now_ms(),
                "inflight": dict(running),
                "waiting": dict(waiting),
                "authFailuresHour": sum(at > now - 3600 for at in self.auth_failures_seen),
                "blockedClients": sum(until > now for until in self.auth_blocked.values()),
            }

    def read_runtime(self):
        if self.runtime_client is None:
            return self.runtime_snapshot()
        try:
            with self.runtime_client.stream("GET", GATEWAY_RUNTIME_PATH, headers={"Accept-Encoding": "identity"}) as response:
                if response.status_code != 200 or response.headers.get("content-encoding", "identity") != "identity":
                    return None
                raw = bytearray()
                for part in response.iter_raw():
                    if len(raw) + len(part) > 32768:
                        return None
                    raw.extend(part)
            data = json.loads(raw)
            counters = [data[k] for k in ("sampledAt", "authFailuresHour", "blockedClients")]
            for key in ("inflight", "waiting"):
                if not isinstance(data[key], dict):
                    return None
                counters.extend(data[key].values())
            if all(type(n) is int and n >= 0 for n in counters):
                return data
        except (httpx.HTTPError, OSError, ValueError, KeyError, TypeError, RecursionError):
            pass
        return None

    def add_runtime(self, overview):
        snapshot = self.read_runtime()
        overview["runtimeAvailable"] = snapshot is not None
        overview["runtimeSampledAt"] = snapshot["sampledAt"] if snapshot is not None else None
        for field in ("authFailuresHour", "blockedClients"):
            overview["overview"][field] = snapshot[field] if snapshot is not None else None
        for team in overview["teams"]:
            for field in ("inflight", "waiting"):
                team[field] = snapshot[field].get(team["teamId"], 0) if snapshot is not None else None

    def _window(self, team, rpm):
        now = time.monotonic()
        window = self.recent[team]
        while window and window[0] <= now - 60:
            window.popleft()
        if rpm and len(window) >= rpm:
            raise GatewayError("rate_limit", f"本队每分钟最多 {rpm} 次请求，请稍后重试", retry_after=max(1, int(window[0] + 60 - now) + 1))
        return window

    def charge_rpm(self, team, rpm):
        with self.lock:
            self._window(team, rpm).append(time.monotonic())

    async def admit_async(self, team, rpm, *, count_tokens=False, defer_rpm=False, lane="model", wait_for_grant=None):
        # Token counting is a lightweight provider operation alongside task polling.
        slots = self.lanes["task" if count_tokens else lane]
        with self.lock:
            self._window(team, rpm)
        await slots.acquire(team, QUEUE_WAIT, wait_for_grant=wait_for_grant)
        try:
            with self.lock:
                window = self._window(team, rpm)
                if not defer_rpm:
                    window.append(time.monotonic())
        except BaseException:
            slots.release(team)
            raise

    def release(self, team, *, count_tokens=False, lane="model"):
        self.lanes["task" if count_tokens else lane].release(team)

    def recheck_access(self, handler, call):
        """Re-check key, team and expiry after waiting for a concurrency slot."""
        with self.service.db() as db:
            row = self.lookup(db, handler)
            if not row or row["id"] != call["key"]["id"]:
                raise APIError(401, "API 密钥在排队期间已被停用")
            settings = self.settings(db)
            if settings["expiresAt"] is not None and now_ms() >= settings["expiresAt"]:
                raise GatewayError("billing", "本活动 API 访问权限已到期")

    def note_error(self, call, error):
        """Attach the final status and classification to a call that has not been written yet."""
        info = error_info(error)
        if not call.get("recorded"):
            call["status"], call["error"] = error.status, info
            detail = (f"上游 HTTP {info['upstream_status']}：" if info["upstream_status"] else "") + (info["detail"] or info["upstream_message"])
            detail = detail + "\n" if detail else ""
            self.keep(call, (detail + error.message).encode())
        return info

    def safe_record(self, handler, call):
        """record(), but a storage failure must not hide the original error or leave the client without a reply."""
        try:
            self.record(handler, call)
        except Exception:
            logging.getLogger(__name__).error("gateway_accounting_failed", extra={"request_id": call["request_id"]})

    def keep(self, call, data):
        if not call["log"] or not data:
            return
        available = RESPONSE_LOG_CAP - call["size"]
        call["size"] += len(data)  # count the full reply even when the stored prefix is truncated
        if available <= 0:
            return
        length = min(len(data), available)
        blocks = call["response"]
        if length >= CAPTURE_BLOCK:
            # Reuse a complete immutable chunk. A clipped view would pin its oversized owner.
            blocks.append(data if isinstance(data, bytes) and len(data) == length else bytes(memoryview(data)[:length]))
        else:
            if not blocks or not isinstance(blocks[-1], bytearray) or len(blocks[-1]) + length > CAPTURE_BLOCK:
                blocks.append(bytearray())
            blocks[-1].extend(memoryview(data)[:length])

    def lookup(self, db, handler):
        parts = handler.headers.get("Authorization", "").split(None, 1)
        token = parts[1].strip() if len(parts) == 2 and parts[0].lower() == "bearer" else handler.headers.get("x-api-key", "").strip()
        if not token:
            raise GatewayError("auth", "缺少 API 密钥")
        if len(token) > 200:
            return None  # longer than any key we issue; not worth hashing
        # Keys stop with the team: API access disabled, team login revoked, or team withdrawn/disqualified.
        return db.execute("""SELECT k.id, k.team_id FROM api_keys k JOIN team_api t ON t.team_id=k.team_id
          JOIN team_accounts ta ON ta.team_id=k.team_id JOIN accounts a ON a.id=ta.account_id AND a.active=1
          LEFT JOIN team_details d ON d.team_id=k.team_id
          WHERE k.key_hash=? AND k.active=1 AND t.active=1 AND COALESCE(d.status,'active')='active'""", (self.digest(token),)).fetchone()

    def auth_guard(self, handler):
        """sub2api's invalid-auth limiter, applied only to failures: a hackathon venue puts every team behind one NAT
        address, so a valid key is never refused because of someone else's bad ones."""
        ip = client_ip(handler)
        try:
            if ipaddress.ip_address(ip).version == 6:
                ip = str(ipaddress.ip_network(f"{ip}/64", strict=False))  # an IPv6 /64 is one client
        except ValueError:
            pass
        return ip

    def auth_blocked_error(self, ip):
        now = time.monotonic()
        with self.lock:
            blocked = self.auth_blocked.get(ip, 0)
        if blocked > now:
            return GatewayError("rate_limit", "密钥错误次数过多，请稍后再试", retry_after=int(blocked - now) + 1)
        return None

    def auth_failed(self, ip):
        now = time.monotonic()
        with self.lock:
            self.auth_failures_seen.append(now)
            failures = self.auth_failures[ip]
            while failures and failures[0] <= now - AUTH_FAIL_WINDOW:
                failures.popleft()
            failures.append(now)
            if len(failures) >= AUTH_FAIL_LIMIT:
                self.auth_blocked[ip] = now + AUTH_BLOCK
                failures.clear()
            if len(self.auth_failures) > 16384:  # bounded memory, as sub2api
                self.auth_failures = defaultdict(deque, {k: v for k, v in self.auth_failures.items() if v and v[-1] > now - AUTH_FAIL_WINDOW})
                self.auth_blocked = {k: v for k, v in self.auth_blocked.items() if v > now}

    def authenticate(self, handler, call=None):
        ip = self.auth_guard(handler)
        with self.service.db() as db:
            try:
                row = self.lookup(db, handler)
            except APIError as error:
                blocked = self.auth_blocked_error(ip)  # blocked from the attempt after the limit, as sub2api
                self.auth_failed(ip)
                raise blocked or error
            if not row:
                blocked = self.auth_blocked_error(ip)
                self.auth_failed(ip)
                raise blocked or GatewayError("auth", "API 密钥无效或已停用")
            settings = self.settings(db)
            if call is not None:
                call["key"], call["log"], call["settings"] = row, settings["logContent"], settings
            if call is not None:
                current = now_ms()
                if settings["expiresAt"] is not None and current >= settings["expiresAt"]:
                    raise GatewayError("billing", "本活动 API 访问权限已到期")
        return row

    def prune_logs(self, db, settings, everything=False):
        return self.ledger.prune_logs(db, settings, everything)

    def record(self, handler, call):
        return self.ledger.record(handler, call)
