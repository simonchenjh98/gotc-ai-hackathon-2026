"""Professional standings and published team results."""
from __future__ import annotations

import json
from decimal import Decimal

from ..constants import DIMENSIONS
from .finals import DIMENSIONS as FINALS_DIMENSIONS
from .roadshow_scoring import DIMENSIONS as ROADSHOW_DIMENSIONS


STANDINGS_SQL = """SELECT t.*, COUNT(s.judge_id) AS judge_count, AVG(s.total_tenths)/10.0 AS average,
(SELECT COUNT(*) FROM votes v WHERE v.team_id=t.id) AS audience_votes
FROM teams t LEFT JOIN scores s ON s.team_id=t.id AND s.status='submitted'
LEFT JOIN team_details d ON d.team_id=t.id
WHERE (?=0 OR COALESCE(d.status,'active')='active')
GROUP BY t.id ORDER BY t.number"""


class Standings:
    def __init__(self, event):
        self.event = event

    @staticmethod
    def professional_score(average, judge_count, config, public=True):
        if average is None or judge_count < config["minJudges"]:
            return None
        if public and not config["resultsPublished"]:
            return None
        return round(average, 2)

    def own_team_score(self, db, actor):
        team = self.event.bound_submission_team(db, actor)
        config = self.event.config(db)
        row = db.execute("""SELECT COUNT(judge_id) AS judge_count, AVG(total_tenths)/10.0 AS average
                            FROM scores WHERE team_id=? AND status='submitted'""", (team["id"],)).fetchone()
        score = self.professional_score(row["average"], row["judge_count"], config)
        if score is not None:
            state = "published"
        elif config["resultsPublished"]:
            state = "awaiting_scores"
        else:
            state = "unpublished"
        dimensions, feedback = [], []
        if state == "published":
            # scores_json stores point values; only total_tenths is scaled by ten.
            submitted = db.execute("SELECT scores_json FROM scores WHERE team_id=? AND status='submitted'", (team["id"],)).fetchall()
            totals = [Decimal(0) for _ in DIMENSIONS]
            for record in submitted:
                for index, value in enumerate(json.loads(record["scores_json"])):
                    totals[index] += Decimal(str(value))
            dimensions = [{**dimension, "score": round(float(total / len(submitted)), 2)}
                          for dimension, total in zip(DIMENSIONS, totals)]
            comments = db.execute("""SELECT s.feedback FROM scores s
                JOIN score_feedback_visibility v ON v.judge_id=s.judge_id AND v.team_id=s.team_id
                JOIN score_write_revisions r ON r.judge_id=s.judge_id AND r.team_id=s.team_id AND r.revision=v.revision
                WHERE s.team_id=? AND s.status='submitted'""", (team["id"],))
            # Text-only ordering cannot associate comments with judge order or scores.
            feedback = sorted(record["feedback"] for record in comments if record["feedback"].strip())
        return {"team": {"name": team["name"], "number": team["number"]}, "state": state,
                "score": score, "dimensions": dimensions, "feedback": feedback,
                "stages": self.own_stage_scores(db, team["id"])}

    def own_stage_scores(self, db, team_id):
        """Return only the logged-in team's published roadshow/finals results."""
        stages = []
        roadshow = self.event.judging.state(db, "roadshow")
        if db.execute("SELECT 1 FROM judging_roster WHERE run_id=? AND team_id=?",
                      (roadshow["run_id"], team_id)).fetchone():
            item = {"id": "roadshow", "name": "路演", "state": "unpublished", "score": None,
                    "dimensions": [], "scoreCount": 0, "minimumScores": 3, "awards": []}
            if self.event.roadshow_scoring.published(db, roadshow):
                saved = self.event.roadshow_scoring.publication_state(db)
                summary = json.loads(saved["publication_json"])
                row = next((value for value in summary.get("teams", []) if value.get("id") == team_id), None)
                if row is not None:
                    item["scoreCount"] = int(row.get("scoreCount") or 0)
                    if row.get("score") is not None:
                        item.update(state="published", score=row["score"], dimensions=[
                            {**dimension, "score": value}
                            for dimension, value in zip(ROADSHOW_DIMENSIONS, row.get("dimensions") or [])])
                    else:
                        item["state"] = "awaiting_scores"
            stages.append(item)

        finals = self.event.finals.state(db)
        if db.execute("SELECT 1 FROM finals_teams WHERE run_id=? AND team_id=?",
                      (finals["run_id"], team_id)).fetchone():
            item = {"id": "finals", "name": "决赛", "state": "unpublished", "score": None,
                    "dimensions": [], "scoreCount": 0, "minimumScores": 1, "awards": []}
            if self.event.finals.published(db, finals):
                summary = json.loads(finals["publication_json"])
                row = next((value for value in summary.get("teams", []) if value.get("id") == team_id), None)
                if row is not None:
                    item["scoreCount"] = int(row.get("scoreCount") or 0)
                    if row.get("score") is not None:
                        item.update(state="published", score=row["score"], dimensions=[
                            {"id": dimension["id"], "name": dimension["name"], "max": dimension["max"], "score": value}
                            for dimension, value in zip(FINALS_DIMENSIONS, row.get("dimensions") or [])])
                    else:
                        item["state"] = "awaiting_scores"
                item["awards"] = [award["name"] for award in summary.get("awards", [])
                                  if team_id in (award.get("winners") or [])]
            stages.append(item)
        return stages

    def teams(self, db, public=True):
        config = self.event.config(db)
        rows = db.execute(STANDINGS_SQL, (int(public),)).fetchall()
        result = [{"id": r["id"], "number": r["number"], "name": r["name"], "project": r["project"], "track": r["track"],
                   "judgeCount": r["judge_count"],
                   "score": self.professional_score(r["average"], r["judge_count"], config, public),
                   "rank": None, "audienceVotes": r["audience_votes"]} for r in rows]
        ranked = sorted((r for r in result if r["score"] is not None), key=lambda r: (-r["score"], r["number"]))
        previous = None
        rank = None
        for position, row in enumerate(ranked, 1):
            if row["score"] != previous:
                rank = position
            row["rank"] = rank
            previous = row["score"]
        if public:
            visible = {row["id"]: position for position, row in enumerate(ranked[:12], 1)}
            for row in result:
                row["rank"] = visible.get(row["id"])
                if row["rank"] is None:
                    row["score"] = None
            return result
        return [self.event.admin_data.enrich_team(db,team) for team in result]
