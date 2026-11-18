"""Professional score entry, organizer standings and audience voting."""
from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation

from ..constants import DIMENSIONS, VOTE_LIMIT
from ..errors import APIError
from ..validation import text_field


def board(event, db, actor):
    event.require(actor, "admin")
    config = event.config(db)
    teams = sorted((team for team in event.teams(db) if team["rank"] is not None), key=lambda team: team["rank"])
    stats = {"teamCount": db.execute("""SELECT COUNT(*) FROM teams t WHERE NOT EXISTS
        (SELECT 1 FROM team_recycle_events e WHERE e.team_id=t.id AND e.restored_at IS NULL)""").fetchone()[0],
             "submittedScores": db.execute("SELECT COUNT(*) FROM scores WHERE status='submitted'").fetchone()[0],
             "audienceVotes": db.execute("SELECT COUNT(*) FROM votes").fetchone()[0]}
    return {"teams": teams, "stats": stats, "config": config, "updatedAt": config["updatedAt"], "serverTime": event.clock()}, None, None


def judge_page(event, db, actor):
    event.require(actor, "judge")
    scores=event.write_conflicts.scores(db,actor["id"])
    return {"teams": event.teams(db), "myScores": scores, "dimensions": DIMENSIONS, "scoringOpen": event.config(db)["scoringOpen"], "name": actor["name"], "submissions": event.submissions.judge_submissions(db)}, None, None


def save_score(event, db, actor, body):
    event.require(actor, "judge")
    if not event.config(db)["scoringOpen"]:
        raise APIError(409, "评分尚未开放或已关闭")
    team_id = event.team_exists(db, body.get("teamId"), require_active=True)
    revision=event.write_conflicts.check_score(db,actor["id"],team_id,body)
    status = body.get("status")
    if not isinstance(status, str) or status not in {"draft", "submitted", "recused"}:
        raise APIError(400, "评分状态不正确")
    previous = db.execute("SELECT * FROM scores WHERE judge_id=? AND team_id=?", (actor["id"], team_id)).fetchone()
    if previous and previous["status"] == "submitted" and status == "draft":
        raise APIError(409, "已有正式评分，请修改后重新提交；草稿不会覆盖正式评分")
    feedback = text_field(body.get("feedback", ""), "反馈", 2000)
    sharing = body.get("feedbackSharing")
    if sharing is not None and sharing != "team-anonymous-v1":
        raise APIError(400, "评语分享声明不正确")
    values, total = [], None
    if status != "recused":
        values, total = check_scores(body.get("scores"))
    db.execute("""INSERT INTO scores VALUES (?,?,?,?,?,?,?) ON CONFLICT(judge_id,team_id) DO UPDATE SET
      scores_json=excluded.scores_json,total_tenths=excluded.total_tenths,feedback=excluded.feedback,status=excluded.status,updated_at=excluded.updated_at""",
               (actor["id"], team_id, json.dumps(values), total, feedback, status, event.clock()))
    score=event.write_conflicts.advance_score(db,actor["id"],team_id,revision)
    if status == "submitted" and feedback and sharing == "team-anonymous-v1":
        db.execute("INSERT INTO score_feedback_visibility VALUES (?,?,?)", (actor["id"], team_id, revision))
    event.audit(db,actor,"score."+status,team_id,metadata={
        "before":{"revision":revision-1,"status":previous["status"],"total":previous["total_tenths"]/10 if previous["total_tenths"] is not None else None} if previous else None,
        "after":{"revision":revision,"status":status,"total":total/10 if total is not None else None},
        "feedbackChanged":feedback!=(previous["feedback"] if previous else "")})
    event.touch(db)
    return {"ok": True, "status": status,"score":score}, None, None


def audience_page(event, db, actor):
    event.require(actor, "audience")
    voted = [r[0] for r in db.execute("SELECT team_id FROM votes WHERE account_id=? ORDER BY created_at,team_id", (actor["id"],))]
    window=event.voting_window(db)
    return {"teams": event.teams(db), "votedTeamIds": voted, "voteLimit": VOTE_LIMIT, "votingOpen": window['canEdit'],
            'votingDeadline':window['deadline'],'window':window,"name": actor["name"]}, None, None


def vote(event, db, actor, body):
    event.require(actor, "audience")
    if not event.voting_window(db)['canEdit']:
        raise APIError(409, "观众投票尚未开放、已关闭或已截止")
    team_id = event.team_exists(db, body.get("teamId"), require_active=True)
    already = db.execute("SELECT 1 FROM votes WHERE account_id=? AND team_id=?", (actor["id"], team_id)).fetchone()
    if not already:
        count = db.execute("SELECT COUNT(*) FROM votes WHERE account_id=?", (actor["id"],)).fetchone()[0]
        if count >= VOTE_LIMIT:
            raise APIError(409, "每个观众邀请码最多支持3个不同队伍")
        db.execute("INSERT INTO votes VALUES (?,?,?)", (actor["id"], team_id, event.clock()))
        event.audit(db, actor, "vote", team_id)
        event.touch(db)
    return {"ok": True, "alreadyVoted": bool(already)}, None, None


def check_scores(scores):
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
