"""SQLite usage ledger, billing reports and bounded request content logs."""

import csv
import io
import json
import time
import zlib

from backend.gotc.errors import APIError
from .config import settings
from .gateway_pricing import quote
from .requests import client_ip
from .usage import USAGE_FIELDS


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
 incomplete INTEGER NOT NULL DEFAULT 0, estimated INTEGER NOT NULL DEFAULT 0,
 error_type TEXT NOT NULL DEFAULT '', error_phase TEXT NOT NULL DEFAULT '', error_owner TEXT NOT NULL DEFAULT '',
 upstream_status INTEGER, error_message TEXT NOT NULL DEFAULT '',
 cache_write_1h_tokens INTEGER NOT NULL DEFAULT 0, requested_model TEXT NOT NULL DEFAULT '', first_token_ms INTEGER,
 client_ip TEXT NOT NULL DEFAULT '', upstream_request_id TEXT NOT NULL DEFAULT '', request_id TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS api_usage_at ON api_usage(at);
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


DAY_MS = 86_400_000
RESPONSE_LOG_CAP = 16 * 1024 * 1024  # bytes of a reply kept for the content viewer; requests are already capped by BODY_LIMIT
SECRET_HEADERS = {"authorization", "x-api-key", "cookie", "proxy-authorization", "sec-websocket-protocol"}


def initialize_schema(db):
    db.executescript(SCHEMA)
    columns = {r["name"] for r in db.execute("PRAGMA table_info(api_usage)")}
    added = {"incomplete": "INTEGER NOT NULL DEFAULT 0", "estimated": "INTEGER NOT NULL DEFAULT 0",
             "error_type": "TEXT NOT NULL DEFAULT ''", "error_phase": "TEXT NOT NULL DEFAULT ''",
             "error_owner": "TEXT NOT NULL DEFAULT ''", "upstream_status": "INTEGER", "error_message": "TEXT NOT NULL DEFAULT ''",
             "cache_write_1h_tokens": "INTEGER NOT NULL DEFAULT 0", "requested_model": "TEXT NOT NULL DEFAULT ''",
             "first_token_ms": "INTEGER", "client_ip": "TEXT NOT NULL DEFAULT ''", "upstream_request_id": "TEXT NOT NULL DEFAULT ''",
             "request_id": "TEXT NOT NULL DEFAULT ''", "currency": "TEXT NOT NULL DEFAULT 'legacy'",
             "billing_status": "TEXT NOT NULL DEFAULT 'settled'", "billing_details": "TEXT NOT NULL DEFAULT '{}'"}
    for column, kind in added.items():  # databases created before these columns existed
        if column not in columns:
            db.execute(f"ALTER TABLE api_usage ADD COLUMN {column} {kind}")


def now_ms():
    return int(time.time() * 1000)


def beijing_midnight():
    return (now_ms() + 8 * 3600_000) // DAY_MS * DAY_MS - 8 * 3600_000


def unpack(blob):
    return zlib.decompress(blob).decode("utf-8", "replace")


def task_price(task, payload, headers):
    result = quote(task["pricing_snapshot"], request=task.get("request"), response=payload,
                   headers=headers, started_at=task.get("requested_at"), trusted_quantities=task.get("quantities"))
    return {**result, "billing": result, "usage": [0] * USAGE_FIELDS,
            "billing_status": "pending" if result.get("pending") else "settled"}


class UsageLedger:
    """Write each metered call and report its costs in its original currency."""

    def __init__(self, service, currency):
        self.service = service
        self.currency = currency
        self.last_prune = 0.0

    def billing_summary(self, db, team_id=None, since=0, key_id=None):
        clause, values = "", [since]
        for column, value in (("team_id", team_id), ("key_id", key_id)):
            if value:
                clause += " AND " + column + "=?"
                values.append(value)
        rows = db.execute("SELECT currency,SUM(cost_micro)/1e6 AS cost,SUM(billing_status='pending') AS pending "
                          "FROM api_usage WHERE at>=?" + clause + " GROUP BY currency ORDER BY currency", values)
        return [dict(r) for r in rows]

    @staticmethod
    def billing_total(rows):
        """A scalar is meaningful only within a single recorded currency."""
        return None if len(rows) > 1 else (rows[0]["cost"] if rows else 0)

    def spend(self, db, team_id, since=0):
        return self.billing_total(self.billing_summary(db, team_id, since))

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
        where, params = self.scope(db, body, {"teamId", "keyId", "days", "granularity"})
        granularity = body.get("granularity", "day")
        if granularity not in ("day", "hour"):
            raise APIError(400, "统计粒度只能是 day 或 hour")
        bucket = "date(u.at/1000+28800,'unixepoch')" if granularity == "day" else "strftime('%Y-%m-%d %H:00',u.at/1000+28800,'unixepoch')"
        days = body.get("days", 7)
        if type(days) is not int or not 1 <= days <= 90:
            raise APIError(400, "统计天数应为1至90")
        where.append("u.at>=?")
        params.append(beijing_midnight() - (days - 1) * DAY_MS)  # whole Beijing calendar days, today included
        clause = " AND ".join(where)
        sums = ("COUNT(*) AS requests, COALESCE(SUM(u.input_tokens),0) AS input, COALESCE(SUM(u.output_tokens),0) AS output, "
                "COALESCE(SUM(u.cache_write_tokens),0) AS cacheWrite, COALESCE(SUM(u.cache_read_tokens),0) AS cacheRead, "
                "CASE WHEN COUNT(DISTINCT u.currency)>1 THEN NULL ELSE COALESCE(SUM(u.cost_micro),0)/1e6 END AS cost, "
                "CASE WHEN COUNT(DISTINCT u.currency)>1 THEN 'MIXED' ELSE COALESCE(MIN(u.currency),'legacy') END AS currency")
        rows = lambda sql: [dict(r) for r in db.execute(sql, params)]
        return {
            "days": days, "currency": self.currency(),
            "billing": self.billing_summary(db, body.get("teamId"), beijing_midnight() - (days - 1) * DAY_MS, body.get("keyId")),
            "totals": rows(f"SELECT {sums}, SUM(u.status>=400 OR u.incomplete=1) AS errors, "
                           f"CAST(AVG(CASE WHEN u.status<400 AND u.incomplete=0 THEN u.duration_ms END) AS INTEGER) AS avgMs, "
                           f"CAST(AVG(u.first_token_ms) AS INTEGER) AS avgFirstTokenMs FROM api_usage u WHERE {clause}")[0],
            "byKey": rows(f"""SELECT u.key_id AS keyId, k.name, k.masked, k.active, t.number AS teamNumber, t.name AS teamName, {sums},
              MAX(u.at) AS lastAt FROM api_usage u JOIN api_keys k ON k.id=u.key_id JOIN teams t ON t.id=u.team_id
              WHERE {clause} GROUP BY u.key_id ORDER BY currency, cost DESC"""),
            "byModel": rows(f"SELECT u.model, {sums} FROM api_usage u WHERE {clause} GROUP BY u.model ORDER BY currency, cost DESC"),
            "byFailure": rows(f"""SELECT u.error_owner AS owner, u.error_phase AS phase, u.error_type AS type, u.upstream_status AS upstreamStatus,
              {sums} FROM api_usage u WHERE {clause} AND (u.status>=400 OR u.incomplete=1) GROUP BY owner, phase, type, upstreamStatus
              ORDER BY requests DESC"""),
            "byClient": rows(f"SELECT u.user_agent AS userAgent, {sums} FROM api_usage u WHERE {clause} GROUP BY u.user_agent ORDER BY requests DESC LIMIT 20"),
            # Calendar days in Beijing time.
            "granularity": granularity,  # Beijing time buckets
            "daily": rows(f"SELECT {bucket} AS day, {sums} FROM api_usage u WHERE {clause} GROUP BY day ORDER BY day"),
        }

    def logs(self, db, body):
        where, params = self.scope(db, body, {"teamId", "keyId", "beforeId", "errorsOnly", "model", "requestId"})
        for field, column in (("model", "u.model"), ("requestId", "u.request_id")):
            value = body.get(field)
            if value not in (None, ""):
                if not isinstance(value, str) or len(value) > 100:
                    raise APIError(400, "筛选条件不正确")
                where.append(f"({column}=? OR u.requested_model=?)" if field == "model" else f"{column}=?")
                params.extend([value.strip(), value.strip()] if field == "model" else [value.strip()])
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
          u.cache_read_tokens AS cacheRead, u.cache_write_1h_tokens AS cacheWrite1h, u.cost_micro/1e6 AS cost,
          u.requested_model AS requestedModel, u.first_token_ms AS firstTokenMs, u.client_ip AS clientIp,
          u.currency, u.billing_status AS billingStatus, u.billing_details AS billingDetails,
          u.upstream_request_id AS upstreamRequestId, u.request_id AS requestId,
          u.duration_ms AS durationMs, u.user_agent AS userAgent, u.incomplete, u.estimated, u.error_type AS errorType,
          u.error_phase AS errorPhase, u.error_owner AS errorOwner, u.upstream_status AS upstreamStatus, u.error_message AS errorMessage, EXISTS(SELECT 1 FROM api_logs l WHERE l.usage_id=u.id) AS hasContent,
          (SELECT l.request_bytes FROM api_logs l WHERE l.usage_id=u.id) AS requestBytes
          FROM api_usage u JOIN api_keys k ON k.id=u.key_id JOIN teams t ON t.id=u.team_id {clause} ORDER BY u.id DESC LIMIT 50""", params)]
        return {"items": items, "more": len(items) == 50, "currency": self.currency()}

    def export(self, db):
        """Usage CSV without request content; formula-like cells are neutralised."""
        out = io.StringIO(newline="")
        writer = csv.writer(out)
        writer.writerow(["时间(UTC毫秒)", "队号", "队名", "密钥", "模型", "接口", "流式", "状态", "输入tokens", "输出tokens", "缓存写入tokens", "缓存读取tokens", "费用（见币种）", "耗时ms", "User-Agent", "中断", "估算计费",
                         "错误类型", "错误阶段", "责任方", "上游状态", "错误信息",
                         "其中1小时缓存写入tokens", "请求模型", "首字耗时ms", "客户端IP", "上游请求ID", "币种", "计费状态", "计费依据"])
        for r in db.execute("""SELECT u.*, t.number, t.name AS team_name, k.name AS key_name FROM api_usage u
          JOIN teams t ON t.id=u.team_id JOIN api_keys k ON k.id=u.key_id ORDER BY u.id"""):
            writer.writerow([self.service.csv_safe(v) for v in (r["at"], r["number"], r["team_name"], r["key_name"], r["model"], r["path"],
                             r["stream"], r["status"], r["input_tokens"], r["output_tokens"], r["cache_write_tokens"], r["cache_read_tokens"], r["cost_micro"] / 1e6, r["duration_ms"], r["user_agent"], r["incomplete"], r["estimated"],
                             r["error_type"], r["error_phase"], r["error_owner"], r["upstream_status"], r["error_message"],
                             r["cache_write_1h_tokens"], r["requested_model"], r["first_token_ms"], r["client_ip"], r["upstream_request_id"], r["currency"], r["billing_status"], r["billing_details"])])
        return ("\ufeff" + out.getvalue()).encode("utf-8")

    def prune_logs(self, db, settings, everything=False):
        """Delete stored request content past the retention period, then oldest first above the size cap.
        Call metadata (tokens, cost, status) is retained for usage statistics."""
        if everything:
            return db.execute("DELETE FROM api_logs").rowcount
        cutoff = now_ms() - settings["logRetentionHours"] * 3600_000
        deleted = db.execute("DELETE FROM api_logs WHERE usage_id IN (SELECT id FROM api_usage WHERE at<?)", (cutoff,)).rowcount
        cap = settings["logMaxMB"] * 1048576
        total = db.execute("SELECT COALESCE(SUM(length(request)+length(response)),0) FROM api_logs").fetchone()[0]
        while total > cap:
            rows = db.execute("SELECT usage_id, length(request)+length(response) AS size FROM api_logs ORDER BY usage_id LIMIT 500").fetchall()
            if not rows:
                break
            ids = []
            for r in rows:
                ids.append(r["usage_id"])
                total -= r["size"]
                if total <= cap:
                    break
            db.execute(f"DELETE FROM api_logs WHERE usage_id IN ({','.join('?' * len(ids))})", ids)
            deleted += len(ids)
        return deleted

    def record(self, handler, call):
        """Store the call once, before the final bytes are sent where possible, so history is never behind the client."""
        if not call["key"] or call.get("recorded"):
            return
        if call.get("billing_finalize"):
            call["billing_finalize"]()
        if not call["status"]:
            call["status"] = 500  # defensive: every stored call has a real outcome
        usage, price = list(call["usage"]), call["price"] or (0,) * USAGE_FIELDS
        # Billing follows whether the upstream generated output ("forwarded"), never the recorded status: a stream that
        # failed after output started is a 502 for the record but its tokens were still produced.
        incomplete = call["forwarded"] and not call["done"]
        estimated = False
        if call.get("metered_media"):
            estimated = bool((call.get("billing") or {}).get("estimated"))
        elif call.get("charge_estimate") and not call.get("pricing_snapshot"):
            usage, incomplete, estimated = [call.get("input_estimate", call["request_bytes"] // 4), call.get("output_estimate", 0), 0, 0, 0], True, True
        elif call["forwarded"] and not call.get("pricing_snapshot"):
            # sub2api bills only what it observed; a call with no usage would then be missing from cost statistics.
            # Fill gaps with an estimate instead: input ~ request bytes / 4, output ~ reply text bytes / 4.
            est_in, est_out = call.get("input_estimate", call["request_bytes"] // 4), call["reply_bytes"] // 4
            if not any(usage):
                usage[0], usage[1], estimated = est_in, est_out, True
            elif incomplete and usage[1] < est_out:
                usage[1], estimated = est_out, True
        cost = round(sum(t * p for t, p in zip(usage, price)))
        billing = call.get("billing")
        if not billing and not call.get("metered_media") and call.get("pricing_snapshot") and (call["forwarded"] or call.get("charge_estimate")):
            billing = quote(call["pricing_snapshot"], request=call.get("pricing_request"), usage=call.get("pricing_usage"),
                            headers=call.get("pricing_headers"), started_at=call["requested_at"])
        if not billing and call.get("metered_media"):
            pending = bool(call["forwarded"] or call.get("charge_estimate"))
            billing = {"cost_micro": None if pending else 0, "currency": "CNY", "pending": pending,
                       "estimated": False, "details": {"reason": "Awaiting authoritative upstream usage" if pending else "Request not generated"}}
        if billing:
            cost = billing.get("cost_micro") or 0
            estimated = bool(billing.get("estimated"))
            billed_tokens = billing.get("details", {}).get("tokens")
            if billed_tokens is not None:
                # Use the same validated split for statistics and price, including
                # input derived from an authoritative total minus output count.
                usage = list(billed_tokens)
        currency = (billing or {}).get("currency", call.get("currency", "USD"))
        error = call.get("error") or {"type": "", "phase": "", "owner": "", "upstream_status": None, "upstream_message": "", "message": ""}
        at = now_ms()
        with self.service.db() as db:
            usage_id = db.execute("""INSERT INTO api_usage(key_id,team_id,at,model,input_tokens,output_tokens,cache_write_tokens,cache_read_tokens,
              cost_micro,status,path,stream,duration_ms,user_agent,incomplete,estimated,error_type,error_phase,error_owner,
              upstream_status,error_message,cache_write_1h_tokens,requested_model,first_token_ms,client_ip,upstream_request_id,request_id)
              VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                  (call["key"]["id"], call["key"]["team_id"], at, call["model"], usage[0], usage[1], usage[2] + usage[4], usage[3], cost,
                                                     call["status"], call["path"], int(call["stream"]), int((time.monotonic() - call["started"]) * 1000),
                                                     (handler.headers.get("User-Agent") or "")[:300], int(incomplete), int(estimated),
                                                     error["type"], error["phase"], error["owner"], error["upstream_status"],
                                                     (error.get("detail") or error["upstream_message"] or error["message"])[:500], usage[4], call.get("requested_model", "")[:100],
                                                     call.get("stream_first_ms") if call["stream"] else call.get("first_token_ms"),
                                                     client_ip(handler), call.get("upstream_request_id", "")[:100], call["request_id"])).lastrowid
            db.execute("UPDATE api_keys SET last_used_at=? WHERE id=?", (at, call["key"]["id"]))
            db.execute("UPDATE api_usage SET currency=?,billing_status=?,billing_details=? WHERE id=?",
                       (currency, "pending" if (billing or {}).get("pending") else "settled",
                        json.dumps((billing or {}).get("details", {}), ensure_ascii=False), usage_id))
            if call["log"] and time.monotonic() - self.last_prune > 600:  # amortised: at most every 10 minutes
                self.last_prune = time.monotonic()
                self.prune_logs(db, call.get("settings") or settings(db))
            if call["log"]:
                headers = {k: v[:500] for k, v in handler.headers.items() if k.lower() not in SECRET_HEADERS}
                response = b"".join(call["response"])[:RESPONSE_LOG_CAP]
                # Full context, compressed: agent requests resend the whole conversation and compress ~5-10x.
                db.execute("INSERT INTO api_logs VALUES (?,?,?,?,?,?)", (usage_id, json.dumps(headers, ensure_ascii=False),
                           zlib.compress(call["request"], 6), zlib.compress(response, 6), len(call["request"]), call["size"]))
        # Only suppress the finalization retry after the entire transaction has committed.
        call["recorded"] = True
