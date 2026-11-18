"""The organizer dashboard reads real gateway counters from a sibling Unix socket."""
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Semaphore, Thread
from time import monotonic, sleep
from unittest import TestCase

from backend.gotc.observability import GATEWAY_RUNTIME_PATH
from tests.performance.local_event import Client, event, seeded


class RuntimeProcessTests(TestCase):
    def test_running_queued_offline_and_recovered(self):
        started, finish = Semaphore(0), Event()
        class Provider(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                started.release()
                finish.wait(6)
                reply = b'{"choices":[],"usage":{"prompt_tokens":1,"completion_tokens":1}}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(reply)))
                self.end_headers()
                self.wfile.write(reply)

        root = Path(__file__).resolve().parents[2]
        with event(root, "uds") as (platform, gateway, private), ThreadingHTTPServer(("127.0.0.1", 0), Provider) as upstream:
            server = Thread(target=upstream.serve_forever, daemon=True)
            server.start()
            try:
                admin, team, team_id, _ = seeded(platform, private)
                admin.json("/api/admin/keys/upstreams", {"upstreams": {"openai": {
                    "url": f"http://127.0.0.1:{upstream.server_port}/v1", "key": "synthetic-provider",
                    "models": {"fixture": [1, 1]},
                }}})
                admin.json("/api/admin/keys/provision", {"teamId": team_id})
                credential = team.json("/api/team/keys", {"name": "synthetic"})["key"]
                sdk = Client(gateway)
                def call():
                    return sdk.request("POST", "/v1/chat/completions", {"model": "fixture", "messages": []},
                                       headers={"Authorization": "Bearer " + credential})[0]

                self.assertEqual(team.request("GET", "/api/admin/keys")[0], 403)
                self.assertEqual(sdk.request("GET", "/v1/models", headers={"Authorization": "Bearer invalid"})[0], 401)
                with ThreadPoolExecutor(3) as workers:
                    calls = [workers.submit(call) for _ in range(3)]
                    try:
                        self.assertTrue(started.acquire(timeout=3))
                        self.assertTrue(started.acquire(timeout=3))
                        deadline = monotonic() + 2
                        while True:
                            data = admin.json("/api/admin/keys")
                            row = next(t for t in data["teams"] if t["teamId"] == team_id)
                            if row["waiting"] == 1 or monotonic() >= deadline:
                                break
                            sleep(0.02)
                        self.assertTrue(data["runtimeAvailable"])
                        self.assertIsInstance(data["runtimeSampledAt"], int)
                        self.assertEqual((row["inflight"], row["waiting"]), (2, 1))
                        self.assertEqual(data["overview"]["authFailuresHour"], 1)
                        self.assertEqual(data["overview"]["blockedClients"], 0)
                        snapshot = sdk.json(GATEWAY_RUNTIME_PATH, headers={"Host": "localhost"}, base="")
                        self.assertEqual(set(snapshot), {"sampledAt", "inflight", "waiting", "authFailuresHour", "blockedClients"})
                        self.assertEqual(snapshot["inflight"], {team_id: 2})
                        for peer in (Client(platform), sdk):
                            self.assertEqual(peer.request("GET", GATEWAY_RUNTIME_PATH, headers={
                                "Host": "localhost", "X-Real-IP": "127.0.0.1"}, base="")[0], 404)
                    finally:
                        finish.set()
                    self.assertEqual([future.result(3) for future in calls], [200] * 3)

                hidden = gateway.with_name("paused-test.sock")
                gateway.rename(hidden)
                try:
                    unavailable = admin.json("/api/admin/keys")
                    self.assertFalse(unavailable["runtimeAvailable"])
                    self.assertIsNone(unavailable["runtimeSampledAt"])
                    self.assertIsNone(unavailable["overview"]["authFailuresHour"])
                    self.assertIsNone(unavailable["teams"][0]["inflight"])
                    self.assertIsNone(unavailable["teams"][0]["waiting"])
                    self.assertEqual(unavailable["overview"]["requests"], 3)
                finally:
                    hidden.rename(gateway)
                recovered = admin.json("/api/admin/keys")
                self.assertTrue(recovered["runtimeAvailable"])
                self.assertEqual(recovered["teams"][0]["inflight"], 0)
            finally:
                finish.set()
                upstream.shutdown()
                server.join(3)
