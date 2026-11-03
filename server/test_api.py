"""End-to-end HTTP tests using fresh temporary databases; never touches event data."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import csv
import http.client
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from app import DIMENSIONS, EventHTTPServer, EventService


class Client:
    def __init__(self, server):
        self.server = server
        self.cookie = ""

    def request(self, method, path, payload=None, raw=None, origin=True, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=20)
        body = raw if raw is not None else (json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None)
        request_headers = {"Cookie": self.cookie}
        if method == "POST":
            request_headers["Content-Type"] = "application/json"
            if origin:
                request_headers["Origin"] = self.server.service.origin if origin is True else origin
        request_headers.update(headers or {})
        conn.request(method, self.server.service.base_path + path, body, request_headers)
        response = conn.getresponse()
        content = response.read()
        response_headers = dict(response.getheaders())
        if "Set-Cookie" in response_headers:
            self.cookie = response_headers["Set-Cookie"].split(";", 1)[0]
        conn.close()
        data = json.loads(content) if response_headers.get("Content-Type", "").startswith("application/json") else content
        return response.status, data, response_headers

    def get(self, path):
        return self.request("GET", path)

    def post(self, path, body):
        return self.request("POST", path, body)

    def settings(self,body):
        """Explicit fresh-read helper for pre-existing non-conflict scenarios."""
        response=self.get('/api/admin')
        if response[0]!=200:return self.post('/api/admin/settings',body)
        groups={'scoring':{'scoringOpen','resultsPublished','minJudges'},
                'submissions':{'submissionsOpen','submissionDeadline'},'voting':{'votingOpen','votingDeadline'}}
        revisions=response[1]['config']['writeRevisions']
        expected={group:revisions[group] for group,keys in groups.items() if set(body) & keys}
        return self.post('/api/admin/settings',{**body,'expectedRevisions':expected})

    def score(self,body):
        """Read a base explicitly; conflict tests use raw post with captured revisions."""
        response=self.get('/api/judge')
        if response[0]!=200:return self.post('/api/score',body)
        record=next((row for row in response[1]['myScores'] if row['teamId']==body.get('teamId')),None)
        return self.post('/api/score',{**body,'expectedRevision':record['revision'] if record else 0})


class APITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="gotc-api-test-")
        self.root = Path(self.temp.name)
        static = self.root / "public"
        static.mkdir()
        (static / "index.html").write_text("<!doctype html><title>Test event</title>", encoding="utf-8")
        (static / "platform.css").write_text("body{color:#123}", encoding="utf-8")
        (static / "secret.txt").write_text("never public", encoding="utf-8")
        (static / "assets").mkdir()
        (static / "assets" / "secret.json").write_text('{"secret":true}', encoding="utf-8")
        self.service = EventService(data_dir=self.root / "private", origin="http://127.0.0.1", static_root=static)
        self.server = EventHTTPServer(("127.0.0.1", 0), self.service)
        self.service.origin = "http://127.0.0.1:" + str(self.server.server_port)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.public = Client(self.server)
        self.admin = Client(self.server)
        self.admin_code = (self.root / "private" / "admin-access.txt").read_text().splitlines()[1]
        status, _, _ = self.admin.post("/api/login", {"role": "admin", "code": self.admin_code})
        self.assertEqual(status, 200)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.temp.cleanup()

    def team(self, number=1, name="真实队伍", **more):
        status, body, _ = self.admin.post("/api/admin/teams", {"number": number, "name": name, "project": "真实问题", "track": "AI for Science", **more})
        self.assertEqual(status, 200, body)
        return body["team"]["id"]

    def invitation(self, role="judge", name="PRIVATE_JUDGE_NAME"):
        status, invitation, _ = self.admin.post("/api/admin/invitations", {"role": role, "name": name})
        self.assertEqual(status, 200)
        client = Client(self.server)
        status, _, _ = client.post("/api/login", {"role": role, "code": invitation["code"]})
        self.assertEqual(status, 200)
        return client, invitation

    def open(self):
        self.assertEqual(self.admin.settings({"scoringOpen": True})[0], 200)

    def test_empty_start_and_bootstrap_secrets(self):
        status, board, _ = self.public.get("/api/board")
        self.assertEqual(status, 200)
        self.assertEqual(board["teams"], [])
        self.assertEqual([d["max"] for d in board["config"]["dimensions"]], [20, 20, 20, 15, 15, 10])
        self.assertEqual(board["config"]["minJudges"], 5)
        self.assertFalse(board["config"]["scoringOpen"])
        self.assertTrue(board["config"]["resultsPublished"])
        with closing(sqlite3.connect(self.service.db_path)) as db:
            dump = "\n".join(db.iterdump())
            self.assertNotIn(self.admin_code, dump)
            self.assertNotIn("GOTC administrator access code", dump)
        self.assertEqual(self.public.get("/api/session")[1]["role"], None)
        self.assertEqual(self.admin.get("/api/session")[1]["role"], "admin")
        self.assertEqual(self.public.get("/../private/admin-access.txt")[0], 400)
        self.assertEqual(self.public.get("/%2e%2e/private/admin-access.txt")[0], 400)
        self.assertEqual(self.public.get("/secret.txt")[0], 404)
        self.assertEqual(self.public.get("/assets/secret.json")[0], 404)
        self.assertEqual(self.public.get("/platform.css")[0], 200)

    def test_authorization_cookie_origin_and_revocation(self):
        judge, invitation = self.invitation()
        for path in ["/api/admin", "/api/judge", "/api/audience", "/api/admin/export"]:
            self.assertEqual(self.public.get(path)[0], 401)
        self.assertEqual(judge.get("/api/admin")[0], 403)
        self.assertEqual(judge.get("/api/audience")[0], 403)
        self.assertEqual(judge.settings({"scoringOpen": True})[0], 403)
        self.assertEqual(self.admin.request("POST", "/api/admin/settings", {"scoringOpen": True}, origin=False)[0], 403)
        self.assertEqual(self.admin.request("POST", "/api/admin/settings", {"scoringOpen": True}, origin="https://attacker.invalid")[0], 403)
        self.assertEqual(self.admin.request("POST", "/api/admin/settings", {"scoringOpen": True}, headers={"Sec-Fetch-Site": "cross-site"})[0], 403)
        self.service.secure = True
        client = Client(self.server)
        status, _, headers = client.post("/api/login", {"role": "judge", "code": invitation["code"]})
        self.assertEqual(status, 200)
        for required in ["HttpOnly", "SameSite=Strict", "Secure", "Path=/gotc"]:
            self.assertIn(required, headers["Set-Cookie"])
        self.assertEqual(self.admin.post("/api/admin/revoke", {"role": "judge", "id": invitation["id"]})[0], 200)
        self.assertEqual(judge.get("/api/judge")[0], 401)
        self.assertEqual(client.post("/api/login", {"role": "judge", "code": invitation["code"]})[0], 401)
        self.assertEqual(self.admin.post("/api/logout", {})[0], 200)
        self.assertIsNone(self.admin.get("/api/session")[1]["role"])

    def test_late_logout_response_preserves_new_login_cookie_and_identity(self):
        old, invitation = self.invitation()
        old_identity = old.get('/api/session')[1].get('loginId')
        entered, release = threading.Event(), threading.Event()
        dispatch = self.service.dispatch
        outcome = []

        def paused(method, path, *args, **kwargs):
            result = dispatch(method, path, *args, **kwargs)
            if path == '/api/logout':
                entered.set()
                if not release.wait(10):
                    raise RuntimeError('Synthetic logout was not released')
            return result

        self.service.dispatch = paused
        thread = threading.Thread(target=lambda: outcome.append(old.post('/api/logout', {})))
        thread.start()
        try:
            self.assertTrue(entered.wait(5))
            fresh = Client(self.server)
            status, login, _ = fresh.post('/api/login', {'role': 'judge', 'code': invitation['code']})
            self.assertEqual(status, 200)
            current = fresh.get('/api/session')[1]
            release.set()
            thread.join(5)
        finally:
            release.set()
            thread.join(5)
            self.service.dispatch = dispatch
        status, _, headers = outcome[0]
        self.assertEqual(status, 200)
        self.assertNotIn('Set-Cookie', headers, 'late logout must not expire the shared new login cookie')
        self.assertIsInstance(old_identity, str)
        self.assertTrue(old_identity)
        self.assertEqual(login['loginId'], current['loginId'])
        self.assertNotEqual(old_identity, current['loginId'])
        self.assertEqual(fresh.get('/api/session')[1]['loginId'], current['loginId'])
        self.assertIsNone(old.get('/api/session')[1]['role'])
        self.assertEqual(fresh.get('/api/judge')[0], 200)

    def test_score_threshold_average_recusal_edits_and_publication(self):
        team_id = self.team()
        self.open()
        judges = [self.invitation(name=f"PRIVATE_JUDGE_{i}")[0] for i in range(5)]
        full = [d["max"] for d in DIMENSIONS]
        eighty = [16, 16, 16, 12, 12, 8]
        sixty = [12, 12, 12, 9, 9, 6]
        def save(client, values, status="submitted"):
            return client.score({"teamId": team_id, "scores": values, "feedback": "PRIVATE_FEEDBACK", "status": status})
        self.assertEqual(save(judges[0], eighty, "draft")[0], 200)
        self.assertEqual(self.public.get("/api/board")[1]["teams"][0]["judgeCount"], 0)
        self.assertEqual(save(judges[0], eighty)[0], 200)
        for client in judges[1:4]:
            self.assertEqual(save(client, full)[0], 200)
        row = self.public.get("/api/board")[1]["teams"][0]
        self.assertEqual(row["judgeCount"], 4)
        self.assertIsNone(row["score"])
        self.assertIsNone(row["rank"])
        self.assertEqual(save(judges[4], full)[0], 200)
        self.assertEqual(self.public.get("/api/board")[1]["teams"][0]["score"], 96)
        self.assertEqual(save(judges[0], sixty)[0], 200)
        self.assertEqual(self.public.get("/api/board")[1]["teams"][0]["score"], 92)
        self.assertEqual(save(judges[0], full, "draft")[0], 409)
        self.assertEqual(self.public.get("/api/board")[1]["teams"][0]["score"], 92)
        self.assertEqual(judges[4].score({"teamId": team_id, "status": "recused"})[0], 200)
        self.assertIsNone(self.public.get("/api/board")[1]["teams"][0]["score"])
        self.assertEqual(save(judges[4], full, "draft")[0], 200)
        self.assertIsNone(self.public.get("/api/board")[1]["teams"][0]["score"])
        self.assertEqual(save(judges[4], full)[0], 200)
        self.assertEqual(len(judges[0].get("/api/judge")[1]["myScores"]), 1)
        self.assertEqual(len(judges[1].get("/api/judge")[1]["myScores"]), 1)
        public_serialized = json.dumps(self.public.get("/api/board")[1])
        self.assertNotIn("PRIVATE_", public_serialized)
        self.assertNotIn("feedback", public_serialized)
        self.assertNotIn("scores_json", public_serialized)
        self.assertEqual(self.admin.settings({"resultsPublished": False})[0], 200)
        self.assertIsNone(self.public.get("/api/board")[1]["teams"][0]["score"])
        self.assertEqual(self.admin.get("/api/admin")[1]["teams"][0]["score"], 92)
        self.assertEqual(self.admin.settings({"resultsPublished": True, "scoringOpen": False})[0], 200)
        for state in ["submitted", "draft", "recused"]:
            self.assertEqual(save(judges[0], full, state)[0], 409)
        self.assertEqual(self.public.get("/api/board")[1]["teams"][0]["score"], 92)

    def test_validation_and_body_limits(self):
        team_id = self.team()
        judge, _ = self.invitation()
        self.open()
        bad_scores = [[0] * 5, [21, 0, 0, 0, 0, 0], [-0.1, 0, 0, 0, 0, 0], [0.01, 0, 0, 0, 0, 0], [True, 0, 0, 0, 0, 0], ["1", 0, 0, 0, 0, 0], [float("nan"), 0, 0, 0, 0, 0]]
        for values in bad_scores:
            with self.subTest(values=values):
                self.assertEqual(judge.score({"teamId": team_id, "scores": values, "status": "submitted"})[0], 400)
        self.assertEqual(judge.score({"teamId": team_id, "scores": [0.1, 0, 0, 0, 0, 0], "status": "submitted"})[0], 200)
        self.assertEqual(judge.score({"teamId": "missing", "scores": [0] * 6, "status": "submitted"})[0], 404)
        self.assertEqual(self.admin.post("/api/admin/teams", {"number": True, "name": "bad"})[0], 400)
        self.assertEqual(self.admin.post("/api/admin/teams", {"number": 1, "name": "duplicate"})[0], 409)
        for value in [0, 51, 101, True, 1.5, "5"]:
            self.assertEqual(self.admin.settings({"minJudges": value})[0], 400)
        self.assertEqual(self.admin.settings({"scoringOpen": "true"})[0], 400)
        self.assertEqual(self.admin.request("POST", "/api/login", raw=b"{")[0], 400)
        self.assertEqual(self.admin.request("POST", "/api/login", raw=b"[]")[0], 400)
        self.assertEqual(self.admin.request("POST", "/api/login", raw=b"{}", headers={"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.admin.request("POST", "/api/login", raw=b"x" * 16385)[0], 413)

    def test_audience_votes_are_independent_idempotent_and_atomic(self):
        teams = [self.team(i + 1, f"队伍{i + 1}") for i in range(4)]
        audience, invitation = self.invitation("audience", "PRIVATE_AUDIENCE")
        self.assertEqual(audience.post("/api/vote", {"teamId": teams[0]})[0], 409)
        self.assertEqual(self.admin.settings({"votingOpen": True})[0], 200)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda team: audience.post("/api/vote", {"teamId": team})[0], teams))
        self.assertEqual(results.count(200), 3)
        self.assertEqual(results.count(409), 1)
        state = audience.get("/api/audience")[1]
        self.assertEqual(len(state["votedTeamIds"]), 3)
        self.assertEqual(state["voteLimit"], 3)
        selected = state["votedTeamIds"][0]
        self.assertTrue(audience.post("/api/vote", {"teamId": selected})[1]["alreadyVoted"])
        board = self.public.get("/api/board")[1]
        self.assertEqual(sum(t["audienceVotes"] for t in board["teams"]), 3)
        self.assertTrue(all(t["score"] is None and t["judgeCount"] == 0 for t in board["teams"]))
        self.assertFalse(board["config"]["audienceIncludedInScore"])
        self.assertEqual(audience.score({"teamId": selected, "scores": [0] * 6, "status": "submitted"})[0], 403)
        self.assertEqual(self.admin.post("/api/admin/revoke", {"role": "audience", "id": invitation["id"]})[0], 200)
        self.assertEqual(audience.get("/api/audience")[0], 401)
        self.assertEqual(sum(t["audienceVotes"] for t in self.public.get("/api/board")[1]["teams"]), 3)

    def test_tied_ranks_team_updates_csv_and_audit(self):
        ids = [self.team(i + 1, "=FORMULA" if i == 0 else f"Team{i}") for i in range(3)]
        judge, invitation = self.invitation(name="+JUDGE_FORMULA")
        self.open()
        self.assertEqual(self.admin.settings({"minJudges": 1})[0], 200)
        for i, team_id in enumerate(ids):
            scores = [20, 20, 20, 15, 15, 10 if i < 2 else 0]
            self.assertEqual(judge.score({"teamId": team_id, "scores": scores, "status": "submitted", "feedback": "@FORMULA"})[0], 200)
        self.assertEqual([t["rank"] for t in self.public.get("/api/board")[1]["teams"]], [1, 1, 3])
        status, body, _ = self.admin.post("/api/admin/teams", {"id": ids[1], "number": 2, "name": "Updated", "project": "Real", "track": "城市与社区问题"})
        self.assertEqual(status, 200)
        status, content, headers = self.admin.get("/api/admin/export")
        self.assertEqual(status, 200)
        rows = list(csv.reader(io.StringIO(content.decode("utf-8-sig"))))
        self.assertEqual(rows[1][1], "'=FORMULA")
        self.assertEqual(rows[1][5], "'+JUDGE_FORMULA")
        self.assertEqual(rows[1][-2], "'@FORMULA")
        self.assertIn("attachment", headers["Content-Disposition"])
        with self.service.db() as db:
            actions = [r[0] for r in db.execute("SELECT action FROM audit")]
            self.assertIn("score.submitted", actions)
            self.assertIn("team.save", actions)
            self.assertIn("export", actions)
        listing = self.admin.get("/api/admin")[1]
        self.assertNotIn("code", listing["judges"][0])
        self.assertEqual(listing["judges"][0]["submittedCount"], 3)
        self.assertEqual(self.admin.post("/api/admin/revoke", {"role": "judge", "id": invitation["id"]})[0], 200)
        self.assertEqual(judge.get("/api/judge")[0], 401)
        self.assertEqual([t["score"] for t in self.public.get("/api/board")[1]["teams"]], [100, 100, 90])

    def test_login_rate_limit_and_security_headers(self):
        for _ in range(20):
            self.assertEqual(self.public.post("/api/login", {"role": "judge", "code": "invalid"})[0], 401)
        status, _, _ = self.public.post("/api/login", {"role": "judge", "code": "invalid"})
        self.assertEqual(status, 429)
        status, _, headers = self.public.get("/api/board")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertNotIn("unsafe-inline", headers["Content-Security-Policy"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
