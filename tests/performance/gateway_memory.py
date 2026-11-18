"""Measure the isolated gateway process while four real SDK calls submit bulk JSON."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import platform as host
import sqlite3
import zlib
from threading import Barrier, Lock, Thread
from time import monotonic, sleep

from local_event import Client, event, seeded


@contextmanager
def provider(reply):
    stats = {"calls": 0, "active": 0, "peak": 0}
    lock = Lock()

    class Model(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            with lock:
                stats["calls"] += 1
                stats["active"] += 1
                stats["peak"] = max(stats["active"], stats["peak"])
            try:
                remaining = int(self.headers["Content-Length"])
                while remaining:
                    chunk = self.rfile.read(min(65536, remaining))
                    if not chunk:
                        raise ConnectionError("Incomplete synthetic upload")
                    remaining -= len(chunk)
                sleep(0.05)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(reply)))
                self.end_headers()
                self.wfile.write(reply)
            finally:
                with lock:
                    stats["active"] -= 1

    with ThreadingHTTPServer(("127.0.0.1", 0), Model) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server.server_port, stats
        finally:
            server.shutdown()
            thread.join()


def run(body_bytes, reply_bytes, logging, batches=1):
    root = Path(__file__).resolve().parents[2]
    prefix, suffix = b'{"model":"fixture","messages":[{"role":"user","content":"', b'"}]}'
    body = prefix + b"a" * (body_bytes - len(prefix) - len(suffix)) + suffix
    encoded = gzip.compress(body, compresslevel=1, mtime=0)
    prefix, suffix = b'{"choices":[{"message":{"content":"', b'"}}],"usage":{"prompt_tokens":1,"completion_tokens":1}}'
    reply = prefix + b"b" * (reply_bytes - len(prefix) - len(suffix)) + suffix
    measurement = {}
    with event(root, "uds", gateway_measurement=measurement) as (platform, gateway, private), provider(reply) as (port, stats):
        admin, first, team1, _ = seeded(platform, private)
        team2 = admin.json("/api/admin/teams", {"number": 2, "name": "Fixture 2", "project": "Fixture", "track": ""})["team"]["id"]
        code = admin.json("/api/admin/team-access", {"action": "issue", "teamId": team2})["code"]
        second = Client(platform)
        second.json("/api/login", {"role": "team", "code": code})
        admin.json("/api/admin/keys/upstreams", {"upstreams": {"openai": {"url": f"http://127.0.0.1:{port}/v1",
                   "key": "fixture-only", "models": {"fixture": [1, 2]}}}})
        admin.json("/api/admin/keys/settings", {"logContent": logging})
        teams = []
        for team, account in ((team1, first), (team2, second)):
            admin.json("/api/admin/keys/provision", {"teamId": team})
            teams.append(account.json("/api/team/keys", {"name": "synthetic"})["key"])
        ready = Barrier(4)
        def call(i):
            headers = {"Content-Type": "application/json", "Authorization": "Bearer " + teams[i % 2]}
            compressed = i >= 2
            if compressed:
                headers["Content-Encoding"] = "gzip"
            ready.wait(timeout=5)
            status, _, received = Client(gateway).request("POST", "/v1/chat/completions", encoded if compressed else body, headers)
            if received != reply or status != 200:
                raise RuntimeError(f"Unexpected SDK outcome: {status}, {len(received)} bytes")
            return status

        start = monotonic()
        statuses = []
        with ThreadPoolExecutor(4) as executor:
            for _ in range(batches):
                statuses.extend(executor.map(call, range(4)))
        elapsed = monotonic() - start
        with sqlite3.connect(private / "event.sqlite3") as db:
            usage = db.execute("SELECT COUNT(*) FROM api_usage WHERE status=200").fetchone()[0]
        assert usage == 4 * batches and stats["calls"] == usage
        with sqlite3.connect(private / "gateway-content.sqlite3") as db:
            captured = 0
            for request, response, req_size, reply_size in db.execute(
                "SELECT request,response,request_bytes,response_bytes FROM content"
            ):
                assert req_size == len(body) and reply_size == len(reply)
                assert zlib.decompress(request) == body
                assert zlib.decompress(response) == memoryview(reply)[:16 * 1024 * 1024]
                captured += 1
        assert captured == (usage if logging else 0)
    return {"host": host.platform(), "python": host.python_version(), "method": "getrusage(RUSAGE_SELF).ru_maxrss",
            "gateway_global_running": 4, "teams": 2, "batches": batches, "verified_content_rows": captured, "concurrent_calls": 4, "identity_calls": 2 * batches, "gzip_calls": 2 * batches,
            "json_request_bytes": len(body), "gzip_request_bytes": len(encoded), "json_reply_bytes": len(reply),
            "log_content": logging, "usage_rows": usage, "statuses": statuses, "elapsed_seconds": round(elapsed, 3),
            "provider_peak_inflight": stats["peak"], **measurement}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batches", type=int, choices=(1, 2, 3), default=1)
    parser.add_argument("--small-input", action="store_true", help="Use a 1 KiB request to exercise reply-side admission")
    parser.add_argument("--body-mib", type=int, choices=range(1, 33), default=32)
    parser.add_argument("--max-text-reply", action="store_true", help="Return exactly 50,000,000 bytes of provider JSON")
    parser.add_argument("--reply-mib", type=int, choices=range(1, 9), default=2)
    parser.add_argument("--log-content", action="store_true")
    options = parser.parse_args()
    print(json.dumps(run(1024 if options.small_input else options.body_mib << 20, 50_000_000 if options.max_text_reply else options.reply_mib << 20, options.log_content, options.batches), indent=2))
