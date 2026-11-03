"""Shared-code migration regressions, entirely in temporary fixture databases."""
import json
import unittest

from app import EventService
import test_api as api_helpers

SHARED_CODE = "112233"
WRONG_CODE = "112234"


class SharedLoginTests(unittest.TestCase):
    # Reuse fixture helpers without inheriting (and rerunning) the seven API tests.
    setUp = api_helpers.APITests.setUp
    tearDown = api_helpers.APITests.tearDown
    team = api_helpers.APITests.team
    invitation = api_helpers.APITests.invitation
    open = api_helpers.APITests.open

    def configure_shared_codes(self):
        path = self.service.data_dir / "shared-login-codes.json"
        path.write_text(json.dumps({"admin": SHARED_CODE, "judge": SHARED_CODE}), encoding="utf-8")
        self.restart_service()

    def restart_service(self):
        previous = self.service
        self.service = EventService(data_dir=previous.data_dir, base_path=previous.base_path,
                                    origin=previous.origin, static_root=previous.root)
        self.server.service = self.service

    def login_admin(self):
        self.admin = api_helpers.Client(self.server)
        status, result, _ = self.admin.post("/api/login", {"role": "admin", "code": SHARED_CODE})
        self.assertEqual(status, 200, result)
        self.assertEqual(self.admin.get("/api/session")[1]["role"], "admin")

    def test_admin_rotation_invalidates_old_code_sessions_but_not_audience(self):
        audience, audience_invitation = self.invitation("audience", "Existing audience")
        judge, judge_invitation = self.invitation("judge", "Existing judge")
        old_admin = self.admin
        old_admin_code = self.admin_code
        self.configure_shared_codes()

        self.assertIsNone(old_admin.get("/api/session")[1]["role"])
        self.assertEqual(old_admin.get("/api/admin")[0], 401)
        self.assertEqual(api_helpers.Client(self.server).post("/api/login", {"role": "admin", "code": old_admin_code})[0], 401)
        self.assertEqual(judge.get("/api/judge")[0], 401)
        self.assertEqual(api_helpers.Client(self.server).post("/api/login", {"role": "judge", "code": judge_invitation["code"]})[0], 401)
        self.login_admin()
        self.assertEqual((self.service.data_dir / "admin-access.txt").read_text().splitlines()[1], SHARED_CODE)

        self.assertEqual(audience.get("/api/session")[1]["role"], "audience")
        fresh_audience = api_helpers.Client(self.server)
        status, _, _ = fresh_audience.post("/api/login", {"role": "audience", "code": audience_invitation["code"]})
        self.assertEqual(status, 200)
        self.assertEqual(fresh_audience.get("/api/audience")[0], 200)
        self.assertEqual(fresh_audience.post("/api/login", {"role": "audience", "code": SHARED_CODE})[0], 401)

    def test_default_judge_and_shared_code_selection_keep_scores_separate(self):
        self.configure_shared_codes()
        self.login_admin()
        listing = self.admin.get("/api/admin")[1]
        self.assertEqual(len(listing["judges"]), 1)
        default_judge_id = listing["judges"][0]["id"]
        single = api_helpers.Client(self.server)
        status, result, _ = single.post("/api/login", {"role": "judge", "code": SHARED_CODE})
        self.assertEqual(status, 200, result)
        self.assertEqual(single.get("/api/session")[1]["role"], "judge")

        status, invitation, _ = self.admin.post("/api/admin/invitations", {"role": "judge", "name": "Judge 02"})
        self.assertEqual(status, 200)
        self.assertEqual(invitation["code"], SHARED_CODE)
        self.assertNotEqual(invitation["id"], default_judge_id)
        unselected = api_helpers.Client(self.server)
        status, selection, headers = unselected.post("/api/login", {"role": "judge", "code": SHARED_CODE})
        self.assertEqual(status, 200, selection)
        self.assertTrue(selection["selectAccount"])
        self.assertEqual({a["id"] for a in selection["accounts"]}, {default_judge_id, invitation["id"]})
        self.assertNotIn("Set-Cookie", headers)
        self.assertIsNone(unselected.get("/api/session")[1]["role"])
        self.assertEqual(unselected.get("/api/judge")[0], 401)

        first, second = api_helpers.Client(self.server), api_helpers.Client(self.server)
        for client, account_id in [(first, default_judge_id), (second, invitation["id"])]:
            status, result, _ = client.post("/api/login", {"role": "judge", "code": SHARED_CODE, "accountId": account_id})
            self.assertEqual(status, 200, result)
            self.assertEqual(client.get("/api/session")[1]["role"], "judge")
        team_id = self.team()
        self.open()
        self.assertEqual(self.admin.settings({"minJudges": 2})[0], 200)
        scores_first, scores_second = [16, 16, 16, 12, 12, 8], [20, 20, 20, 15, 15, 10]
        for client, scores, feedback in [(first, scores_first, "First judge only"), (second, scores_second, "Second judge only")]:
            self.assertEqual(client.score({"teamId": team_id, "scores": scores, "feedback": feedback, "status": "submitted"})[0], 200)
        first_scores = first.get("/api/judge")[1]["myScores"]
        second_scores = second.get("/api/judge")[1]["myScores"]
        self.assertEqual(len(first_scores), 1)
        self.assertEqual(len(second_scores), 1)
        self.assertEqual(first_scores[0]["scores"], scores_first)
        self.assertEqual(second_scores[0]["scores"], scores_second)
        self.assertEqual(first_scores[0]["feedback"], "First judge only")
        self.assertEqual(second_scores[0]["feedback"], "Second judge only")
        board_row = self.public.get("/api/board")[1]["teams"][0]
        self.assertEqual(board_row["judgeCount"], 2)
        self.assertEqual(board_row["score"], 90)

    def test_restart_is_idempotent_preserves_scores_and_rejects_wrong_code(self):
        team_id = self.team()
        judge, invitation = self.invitation("judge", "Existing scored judge")
        self.open()
        self.assertEqual(self.admin.settings({"minJudges": 1})[0], 200)
        values = [16, 16, 16, 12, 12, 8]
        self.assertEqual(judge.score({"teamId": team_id, "scores": values, "feedback": "Keep this real score", "status": "submitted"})[0], 200)
        self.configure_shared_codes()
        self.login_admin()
        self.assertEqual(self.public.get("/api/board")[1]["teams"][0]["score"], 80)
        with self.service.db() as db:
            before_accounts = [tuple(row) for row in db.execute("SELECT id,role,salt,code_hash FROM accounts ORDER BY id")]
            before_scores = [tuple(row) for row in db.execute("SELECT * FROM scores ORDER BY judge_id,team_id")]
        self.restart_service()
        self.assertEqual(self.admin.get("/api/session")[1]["role"], "admin")
        with self.service.db() as db:
            self.assertEqual([tuple(row) for row in db.execute("SELECT id,role,salt,code_hash FROM accounts ORDER BY id")], before_accounts)
            self.assertEqual([tuple(row) for row in db.execute("SELECT * FROM scores ORDER BY judge_id,team_id")], before_scores)
        self.assertEqual(len(self.admin.get("/api/admin")[1]["judges"]), 1)
        fresh_judge = api_helpers.Client(self.server)
        status, result, _ = fresh_judge.post("/api/login", {"role": "judge", "code": SHARED_CODE})
        self.assertEqual(status, 200, result)
        self.assertEqual(fresh_judge.get("/api/judge")[1]["myScores"][0]["scores"], values)
        for role in ["admin", "judge"]:
            wrong = api_helpers.Client(self.server)
            self.assertEqual(wrong.post("/api/login", {"role": role, "code": WRONG_CODE})[0], 401)
            self.assertIsNone(wrong.get("/api/session")[1]["role"])
        self.assertEqual(api_helpers.Client(self.server).post("/api/login", {"role": "judge", "code": invitation["code"]})[0], 401)


if __name__ == "__main__":
    unittest.main(verbosity=2)
