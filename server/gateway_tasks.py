"""Owned asynchronous inference tasks; only explicit per-task routes are exposed.

The upstream credential is kept only while a submitted task needs reconciliation.
It lives in the same private SQLite database as gateway_config, never in usage logs
or public responses. Network requests use that immutable submission snapshot.

Pricing callbacks are pure: ``pricing(task, payload, headers)`` returns a dict with
``cost_micro``, optional five-element ``usage``, ``estimated`` and ``billing``.
They identify the currency explicitly; this module never assumes an exchange rate.
Usage and the terminal settlement marker are committed in one transaction.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import quote

from errors import APIError


STATES = frozenset({"waiting", "in_progress", "success", "failure", "cancelled"})
TERMINAL = frozenset({"success", "failure", "cancelled"})
TASK_ID = r"tsk_[A-Za-z0-9_-]{43}"
TASK_ROUTE = re.compile(r"^/v1/task/(" + TASK_ID + r")(?P<action>/get|/status|/cancel)?$")
RESPONSE_LIMIT = 16 * 1024 * 1024
SNAPSHOT_LIMIT = 256 * 1024
POLL_INTERVAL = 10
SCHEMA = """
CREATE TABLE IF NOT EXISTS api_async_tasks (
 id TEXT PRIMARY KEY, team_id TEXT NOT NULL REFERENCES teams(id),
 key_id TEXT NOT NULL REFERENCES api_keys(id), request_id TEXT NOT NULL,
 model TEXT NOT NULL, path TEXT NOT NULL, remote_id TEXT NOT NULL,
 remote_scope TEXT NOT NULL, upstream_url TEXT NOT NULL, upstream_key TEXT NOT NULL,
 pricing_snapshot TEXT NOT NULL, request_snapshot TEXT NOT NULL, quantities_snapshot TEXT NOT NULL DEFAULT '{}',
 payload TEXT NOT NULL, response_headers TEXT NOT NULL DEFAULT '{}',
 state TEXT NOT NULL, settled INTEGER NOT NULL DEFAULT 0 CHECK(settled IN (0,1)),
 usage_id INTEGER NOT NULL UNIQUE REFERENCES api_usage(id),
 created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, next_poll_at INTEGER NOT NULL,
 attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '',
 billing TEXT NOT NULL DEFAULT '{}',
 UNIQUE(key_id, request_id), UNIQUE(remote_scope, remote_id)
);
CREATE INDEX IF NOT EXISTS api_async_tasks_pending ON api_async_tasks(settled, next_poll_at);
CREATE INDEX IF NOT EXISTS api_async_tasks_owner ON api_async_tasks(team_id, key_id, created_at);
"""


def _now():
    return int(time.time() * 1000)


def _dump(value, limit=RESPONSE_LIMIT):
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError, RecursionError):
        raise APIError(502, "异步任务返回格式不正确") from None
    if len(encoded.encode("utf-8")) > limit:
        raise APIError(502, "异步任务返回内容过大")
    return encoded


def _billing_request(request):
    """Store quantities, not prompts, image Base64, uploaded files or media URLs."""
    if not isinstance(request, dict):
        return {}
    fields = {"n", "num_images", "num_outputs", "num_inference_steps", "steps", "duration", "duration_seconds",
              "seconds", "width", "height", "size", "resolution", "aspect_ratio", "fps", "num_frames",
              "frames", "sample_rate", "sample_rate_hz", "response_format", "output_format", "format",
              "mode", "task_type", "quality", "speed", "page_count", "pages", "language", "voice",
              "max_tokens", "max_output_tokens", "model", "image_size", "video_size"}
    result = {k: v for k, v in request.items() if k in fields and
              (v is None or type(v) in (bool, int, float) or (isinstance(v, str) and len(v) <= 256))}
    for field in ("file", "ref_audio"):
        if isinstance(request.get(field), dict):
            allowed = {"size", "bytes", "duration", "duration_seconds", "sample_rate", "content_type", "contentType"}
            result[field] = {k: v for k, v in request[field].items() if k in allowed and
                            (type(v) in (int, float, bool) or (isinstance(v, str) and len(v) <= 128))}
    return result


def _quantities(request):
    """Recompute trusted counts; client-supplied private fields are never read."""
    if not isinstance(request, dict):
        return {}
    result = {}
    text = request.get("input", request.get("inputs", request.get("text")))
    if isinstance(text, str):
        result["characters"] = len(text)
    inputs = request.get("input", request.get("inputs"))
    if isinstance(inputs, (str, list)):
        result["items"] = len(inputs) if isinstance(inputs, list) else 1
    return result


def _redact(value, credential):
    from keys import redact_error_payload
    return json.loads(redact_error_payload(_dump(value).encode(), (credential,)))


class AsyncTasks:
    """The gateway authenticates each public request before calling ``request``.

    ``transport(snapshot, method, suffix) -> (status, payload, headers)`` can be
    injected for deterministic tests; suffix is constructed here, never supplied
    by a client. ``register`` must complete before the accepted response is sent.
    """

    def __init__(self, gateway, pricing=None, transport=None):
        self.gateway = gateway
        self.service = gateway.service
        self.pricing = pricing
        self.transport = transport or self._transport
        self._locks = [threading.RLock() for _ in range(64)]
        self._stop = threading.Event()
        self._worker = None

    def initialize(self, db):
        db.executescript(SCHEMA)
        columns = {row["name"] for row in db.execute("PRAGMA table_info(api_async_tasks)")}
        if "quantities_snapshot" not in columns:
            db.execute("ALTER TABLE api_async_tasks ADD COLUMN quantities_snapshot TEXT NOT NULL DEFAULT '{}'")

    @staticmethod
    def matches(path):
        return bool(TASK_ROUTE.fullmatch(path))

    def _lock(self, task_id):
        return self._locks[int(hashlib.sha256(task_id.encode()).hexdigest()[:8], 16) % len(self._locks)]

    def _task(self, row):
        task = dict(row)
        for target, source in (("request", "request_snapshot"), ("pricing_snapshot", "pricing_snapshot"),
                               ("payload", "payload"), ("response_headers", "response_headers"), ("billing", "billing"),
                               ("quantities", "quantities_snapshot")):
            task[target] = json.loads(task[source])
        task["requested_at"] = task["created_at"] / 1000
        return task

    def _get(self, task_id):
        with self.service.db() as db:
            row = db.execute("SELECT * FROM api_async_tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            raise APIError(404, "任务不存在")
        return self._task(row)

    def _owned(self, key, task_id):
        """Exact key ownership: another key in the same team also cannot read it."""
        with self.service.db() as db:
            row = db.execute("""SELECT t.* FROM api_async_tasks t
              JOIN api_keys k ON k.id=t.key_id AND k.team_id=t.team_id AND k.active=1
              JOIN team_api a ON a.team_id=t.team_id AND a.active=1
              WHERE t.id=? AND t.key_id=? AND t.team_id=?""", (task_id, key["id"], key["team_id"])).fetchone()
            if not row:
                raise APIError(404, "任务不存在")
            # Authenticate handles lifecycle and expiry too; recheck after slow polling
            # so revocation while an upstream request is in flight never reveals output.
            active = db.execute("""SELECT 1 FROM team_accounts ta JOIN accounts a
              ON a.id=ta.account_id AND a.active=1 LEFT JOIN team_details d ON d.team_id=ta.team_id
              WHERE ta.team_id=? AND COALESCE(d.status,'active')='active'""", (key["team_id"],)).fetchone()
            if not active:
                raise APIError(404, "任务不存在")
            settings = self.gateway.settings(db)
            expires = settings.get("expiresAt")
            if expires is not None and _now() >= expires:
                raise APIError(403, "本活动 API 访问权限已到期")
        return self._task(row)

    def _public(self, task):
        payload = task["payload"]
        public = {k: copy.deepcopy(payload[k]) for k in
                  ("created_at", "started_at", "completed_at", "price", "currency", "output") if k in payload}
        public.update(task_id=task["id"], status=task["state"])
        base = getattr(self.service, "base_path", "").rstrip("/") + "/v1/task/" + task["id"]
        public["urls"] = {"get": base + "/get", "cancel": base + "/cancel", "status": base + "/status"}
        if "error" in payload:
            public["error"] = _redact(payload["error"], task["upstream_key"])
        return public

    def register(self, call, upstream, payload, *, request=None, headers=None):
        """Persist one accepted upstream task and replace its remote identity.

        A repeated call with the same request_id is safe after a local database
        failure. It must never be implemented by resubmitting inference upstream.
        """
        for attempt in range(3):
            try:
                return self._register(call, upstream, payload, request=request, headers=headers)
            except sqlite3.OperationalError as error:
                if attempt == 2 or not any(word in str(error).lower() for word in ("locked", "busy")):
                    raise
                time.sleep(0.05 * (2 ** attempt))  # retry only the local transaction

    def _register(self, call, upstream, payload, *, request=None, headers=None):
        from keys import check_url
        if not isinstance(payload, dict) or not isinstance(payload.get("task_id"), str) or \
                not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,255}", payload["task_id"]) or payload.get("status") not in STATES:
            raise APIError(502, "上游未返回有效的异步任务")
        try:
            upstream_url = check_url(upstream["url"])
            credential = upstream["key"]
            if not isinstance(credential, str) or not credential or "\r" in credential or "\n" in credential:
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise APIError(500, "异步任务上游配置不正确") from None
        key = call["key"]
        request_id = call.setdefault("request_id", "async-" + secrets.token_urlsafe(24))
        task_id = "tsk_" + secrets.token_urlsafe(32)
        at = _now()
        if type(call.get("requested_at")) in (int, float) and math.isfinite(call["requested_at"]) and call["requested_at"] > 0:
            at = int(call["requested_at"] * 1000)
        scope = hashlib.sha256((upstream_url + "\0" + credential).encode()).hexdigest()
        snapshot = _dump(call.get("pricing_snapshot") or {}, SNAPSHOT_LIMIT)
        request_snapshot = _dump(_billing_request(request), SNAPSHOT_LIMIT)
        quantities = _dump(_quantities(request), SNAPSHOT_LIMIT)
        if "error" in payload:
            payload = dict(payload, error=_redact(payload["error"], credential))
        encoded = _dump(payload)
        response_headers = _dump(self._billing_headers(headers or {}), SNAPSHOT_LIMIT)
        with self.service.db() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT * FROM api_async_tasks WHERE (key_id=? AND request_id=?) OR (remote_scope=? AND remote_id=?)",
                                  (key["id"], request_id, scope, payload["task_id"])).fetchall()
            if existing:
                row = existing[0]
                if len(existing) != 1 or row["key_id"] != key["id"] or row["team_id"] != key["team_id"] \
                        or row["remote_scope"] != scope or row["remote_id"] != payload["task_id"]:
                    raise APIError(409, "异步任务登记冲突")
                task_id = row["id"]
            else:
                # One usage row per inference job; subsequent GETs and cancel calls
                # never create token estimates or extra billed requests.
                usage_id = db.execute("""INSERT INTO api_usage
                  (key_id,team_id,at,model,input_tokens,output_tokens,cache_write_tokens,cache_read_tokens,
                   cost_micro,status,path,stream,duration_ms,user_agent,incomplete,estimated,requested_model,
                   client_ip,request_id,currency,billing_status,billing_details)
                   VALUES (?,?,?,?,0,0,0,0,0,202,?,0,0,?,1,0,?,?,?,'CNY','pending','{}')""",
                   (key["id"], key["team_id"], at, call["model"], call["path"],
                    str(call.get("user_agent", ""))[:300], str(call.get("requested_model", call["model"]))[:100],
                    str(call.get("client_ip", ""))[:100], request_id)).lastrowid
                db.execute("""INSERT INTO api_async_tasks
                  (id,team_id,key_id,request_id,model,path,remote_id,remote_scope,upstream_url,upstream_key,
                   pricing_snapshot,request_snapshot,quantities_snapshot,payload,response_headers,state,usage_id,created_at,updated_at,next_poll_at)
                  VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (task_id, key["team_id"], key["id"], request_id, call["model"], call["path"], payload["task_id"],
                   scope, upstream_url, credential, snapshot, request_snapshot, quantities, encoded, response_headers, payload["status"],
                   usage_id, at, at, at + POLL_INTERVAL * 1000))
                db.execute("UPDATE api_keys SET last_used_at=? WHERE id=?", (at, key["id"]))
        # Publish the local identity only after both inserts have committed.
        call["recorded"], call["task_id"] = True, task_id
        task = self._get(task_id)
        if task["state"] in TERMINAL and not task["settled"]:
            task = self._refresh(task) if task["state"] == "success" else self._settle(task)
        return self._public(task)

    @staticmethod
    def _billing_headers(headers):
        return {str(k).lower(): str(v)[:256] for k, v in headers.items()
                if str(k).lower() in {"inference-cost", "inference-currency", "inference-cost-currency", "x-request-id", "request-id"}}

    def _quote(self, task):
        callback = self.pricing or getattr(self.gateway, "price_async_task", None)
        if callback is None:
            raise APIError(503, "异步任务计费尚未配置")
        # Callbacks cannot leak credentials accidentally into billing metadata.
        pricing_task = {k: v for k, v in task.items() if k not in {"upstream_key", "upstream_url", "remote_id", "remote_scope"}}
        result = callback(pricing_task, copy.deepcopy(task["payload"]), dict(task["response_headers"]))
        if not isinstance(result, dict) or "cost_micro" not in result or (result["cost_micro"] is not None and
                (type(result["cost_micro"]) is not int or not 0 <= result["cost_micro"] < 2 ** 63)):
            raise APIError(502, "异步任务费用无效")
        if result.get("currency", "CNY") not in {"CNY", "USD"}:
            raise APIError(502, "异步任务计费币种无效")
        usage = result.get("usage", [0] * 5)
        if not isinstance(usage, (list, tuple)) or len(usage) != 5 or \
                any(type(n) is not int or n < 0 or n >= 2 ** 62 for n in usage):
            raise APIError(502, "异步任务用量无效")
        result = dict(result, usage=list(usage))
        _dump(result.get("billing", {}), SNAPSHOT_LIMIT)
        return result

    def _settle(self, task):
        if task["settled"] or task["state"] not in TERMINAL:
            return task
        priced = self._quote(task)
        usage = priced["usage"]
        settled = priced["cost_micro"] is not None
        status = {"success": 200, "failure": 502, "cancelled": 499}[task["state"]]
        with self.service.db() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT settled FROM api_async_tasks WHERE id=?", (task["id"],)).fetchone()
            if current and not current["settled"]:
                db.execute("""UPDATE api_usage SET input_tokens=?,output_tokens=?,cache_write_tokens=?,cache_read_tokens=?,
                  cache_write_1h_tokens=?,cost_micro=?,status=?,duration_ms=?,incomplete=0,estimated=?,
                  error_type=?,error_phase=?,error_owner=?,upstream_request_id=?,currency=?,billing_status=?,billing_details=? WHERE id=?""",
                  (usage[0], usage[1], usage[2] + usage[4], usage[3], usage[4], priced["cost_micro"] or 0, status,
                   max(0, _now() - task["created_at"]), int(bool(priced.get("estimated", False))),
                   "" if status == 200 else "async_" + task["state"], "" if status == 200 else "upstream",
                   "" if status == 200 else "provider", task["response_headers"].get("x-request-id", "")[:100],
                   priced.get("currency", "CNY"), "settled" if settled else "pending",
                   _dump(priced.get("billing", {}), SNAPSHOT_LIMIT), task["usage_id"]))
                # Retain cached output for owner retrieval without retaining a live
                # credential after completion, including after config rotation.
                db.execute("UPDATE api_async_tasks SET settled=?,billing=?,upstream_key=?,last_error='',updated_at=? WHERE id=?",
                           (int(settled), _dump(priced.get("billing", {}), SNAPSHOT_LIMIT),
                            "" if settled else task["upstream_key"], _now(), task["id"]))
        return self._get(task["id"])

    def _transport(self, snapshot, method, suffix):
        from keys import make_opener
        request = urllib.request.Request(snapshot["upstream_url"] + suffix,
                  data=b"{}" if method == "POST" else None, method=method,
                  headers={"Authorization": "Bearer " + snapshot["upstream_key"], "Accept": "application/json",
                           "Content-Type": "application/json"})
        # A dedicated no-redirect opener cannot forward the stored key to a URL
        # returned in task.urls, Location, output or a caller's query string.
        try:
            with make_opener().open(request, timeout=30) as response:
                status = response.status
                raw = response.read(RESPONSE_LIMIT + 1)
                headers = self._billing_headers(response.headers)
                if len(raw) > RESPONSE_LIMIT:
                    raise APIError(502, "异步任务返回内容过大")
                try:
                    payload = json.loads(raw)
                except (ValueError, UnicodeError, RecursionError):
                    raise APIError(502, "异步任务返回格式不正确") from None
                return status, payload, headers
        except urllib.error.HTTPError as error:
            error.close()
            raise APIError(502, "上游异步任务查询失败") from None
        except (OSError, urllib.error.URLError):
            raise APIError(502, "上游异步任务暂时不可达") from None

    def _fetch(self, task, method="GET", action=""):
        if not task["upstream_key"]:
            raise APIError(502, "异步任务已结束，无法继续访问上游")
        suffix = "/task/" + quote(task["remote_id"], safe="") + action
        status, payload, headers = self.transport(task, method, suffix)
        if status < 200 or status >= 300:
            raise APIError(502, "上游异步任务查询失败")
        return payload, self._billing_headers(headers)

    def _refresh(self, task):
        if task["settled"]:
            return task
        try:
            payload, headers = self._fetch(task)
        except APIError:
            if task["state"] in TERMINAL:
                return self._settle(task)  # cached result survives a provider outage
            raise
        if not isinstance(payload, dict) or payload.get("task_id") != task["remote_id"] or payload.get("status") not in STATES:
            raise APIError(502, "上游异步任务身份或状态不正确")
        if payload["status"] == "success" and ("output" not in payload or
                (payload.get("price") is None and "inference-cost" not in headers)):
            # The documented result route supplies Inference-Cost even when the
            # task record has no price. Fetch it only after checking task identity.
            try:
                output, result_headers = self._fetch(task, "GET", "/get")
                if not isinstance(output, dict):
                    raise APIError(502, "上游异步任务结果格式不正确")
                payload = dict(payload, output=output)
                headers = {**headers, **result_headers}
            except APIError:
                if "output" not in payload:
                    raise
        # Scrub errors before caching and before removing the completed credential.
        if "error" in payload:
            payload = dict(payload, error=_redact(payload["error"], task["upstream_key"]))
        encoded = _dump(payload)
        with self.service.db() as db:
            db.execute("""UPDATE api_async_tasks SET payload=?,response_headers=?,state=?,updated_at=?,next_poll_at=?,
                       attempts=0,last_error='' WHERE id=? AND settled=0
                       AND (state NOT IN ('success','failure','cancelled') OR state=?)""",
                       (encoded, _dump(headers), payload["status"], _now(), _now() + POLL_INTERVAL * 1000, task["id"], payload["status"]))
        return self._settle(self._get(task["id"]))

    def request(self, key, method, path):
        match = TASK_ROUTE.fullmatch(path)
        if not match:
            raise APIError(404, "任务接口不存在")
        task_id, action = match.group(1), match.group("action") or ""
        if method != ("POST" if action == "/cancel" else "GET"):
            raise APIError(405, "任务接口请求方法不正确")
        with self._lock(task_id):
            task = self._owned(key, task_id)
            if action == "/cancel" and task["state"] not in TERMINAL:
                self._fetch(task, "POST", "/cancel")  # no automatic mutation retry
            task = self._refresh(task)
            task = self._owned(key, task_id)
            if action == "/status":
                payload = task["state"]
            elif action == "/get":
                if task["state"] != "success":
                    raise APIError(409, "任务尚无可用结果")
                payload = copy.deepcopy(task["payload"].get("output", {}))
            else:
                payload = self._public(task)
            return 200, payload, dict(task["response_headers"])

    def recover_once(self, limit=20):
        """Reconcile submitted jobs even after a restart or key revocation.

        Revocation stops result access, not settlement of already incurred work.
        Network, pricing and database errors leave the job recoverable. A lost
        submit response is deliberately not retried by this module.
        """
        limit = max(1, min(int(limit), 100))
        with self.service.db() as db:
            rows = db.execute("SELECT id FROM api_async_tasks WHERE settled=0 AND next_poll_at<=? ORDER BY next_poll_at LIMIT ?",
                              (_now(), limit)).fetchall()
        result = {"checked": 0, "settled": 0, "failed": 0}
        for row in rows:
            with self._lock(row["id"]):
                result["checked"] += 1
                try:
                    task = self._refresh(self._get(row["id"]))
                    result["settled"] += int(bool(task["settled"]))
                except Exception:
                    # Never persist provider errors/credentials or expose them to
                    # background logs. The pending usage row remains incomplete.
                    result["failed"] += 1
                    try:
                        with self.service.db() as db:
                            db.execute("UPDATE api_async_tasks SET attempts=attempts+1,last_error=?,next_poll_at=? WHERE id=? AND settled=0",
                                       ("reconciliation_pending", _now() + 30000, row["id"]))
                    except Exception:
                        pass
        return result

    def start(self):
        """Start one bounded daemon; initialization/migrations must finish first."""
        if self._worker and self._worker.is_alive():
            return
        self._stop.clear()

        def run():
            while not self._stop.is_set():
                try:
                    self.recover_once()
                except Exception:
                    pass  # database may be temporarily locked during restart
                self._stop.wait(POLL_INTERVAL)

        self._worker = threading.Thread(target=run, name="gateway-async-tasks", daemon=True)
        self._worker.start()

    def stop(self):
        self._stop.set()
        if self._worker and self._worker is not threading.current_thread():
            self._worker.join(timeout=1)
