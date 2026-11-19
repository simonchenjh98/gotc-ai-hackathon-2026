"""Organizer console, event settings, invitations and data exports."""
from __future__ import annotations

import csv
import io
import json

from .admin_data import TRACK_OPTIONS
from .constants import DIMENSIONS
from .errors import APIError
from .validation import csv_safe, text_field


def dispatch(event, db, actor, method, path, body):
    event.require(actor, "admin")
    if method == "GET" and path == "/api/admin":
        judges = [{"id": r["id"], "name": r["name"], "active": bool(r["active"]), "submittedCount": r["submitted_count"]}
                  for r in db.execute("SELECT a.id,a.name,a.active,COUNT(s.team_id) AS submitted_count FROM accounts a LEFT JOIN scores s ON s.judge_id=a.id AND s.status='submitted' WHERE a.role='judge' GROUP BY a.id ORDER BY a.created_at")]
        audience = [{"id": r["id"], "name": r["name"], "active": bool(r["active"])} for r in db.execute("SELECT id,name,active FROM accounts WHERE role='audience' ORDER BY created_at")]
        for person in judges:person['credentialMode']=event.admin_data.credential_mode(db,person['id'],'judge')
        for person in audience:person['credentialMode']=event.admin_data.credential_mode(db,person['id'],'audience')
        teams=[team for team in event.teams(db, public=False) if not event.team_lifecycle.is_recycled(db,team['id'])]
        for team in teams:
            # Admin-only directory: show who is on each team without opening the editor.
            team['members']=[{'name':m['name'],'role':m['role'],'school':m.get('school')}
                             for m in event.team_lifecycle.member_view(db,team['id'])['members']]
        stats = {"teamCount":len(teams), "submittedScores": db.execute("SELECT COUNT(*) FROM scores WHERE status='submitted'").fetchone()[0], "audienceVotes": db.execute("SELECT COUNT(*) FROM votes").fetchone()[0]}
        submissions=[row for row in event.submissions.admin_submissions(db) if not event.team_lifecycle.is_recycled(db,row['teamId'])]
        recycle_count=db.execute('SELECT COUNT(*) FROM team_recycle_events WHERE restored_at IS NULL').fetchone()[0]
        return {"teams":teams,"recycleCount":recycle_count, "judges": judges, "audience": audience, "config": event.write_conflicts.admin_config(db), "stats": stats, "submissions":submissions,'trackOptions':TRACK_OPTIONS}, None, None
    if method == "POST" and path == "/api/admin/settings":
        changes,groups=event.write_conflicts.check_settings(db,body)
        before={key:event.config(db).get(key) for key in changes}
        for key,value in changes.items():
            db.execute("UPDATE settings SET value=? WHERE key=?",(json.dumps(value),key))
        event.write_conflicts.advance_settings(db,groups)
        event.touch(db)
        event.audit(db,actor,"settings",metadata={'before':before,'after':changes,
            'beforeRevisions':body['expectedRevisions'],'afterRevisions':{group:body['expectedRevisions'][group]+1 for group in sorted(groups)}})
        return {"ok":True,"config":event.write_conflicts.admin_config(db)},None,None
    if method == "POST" and path == "/api/admin/teams":
        return {'ok':True,'team':event.admin_data.save_team(db,actor,body,require_revision=True)},None,None
    if method=='POST' and path=='/api/admin/credentials':
        return event.credential_action(db,actor,body),None,None
    if method == "POST" and path == "/api/admin/invitations":
        role = body.get("role")
        if not isinstance(role, str) or role not in {"judge", "audience"}:
            raise APIError(400, "只能创建评委或观众邀请")
        name = text_field(body.get("name"), "称呼", 80, True)
        account, code = event.create_account(db, role, name)
        event.audit(db, actor, "invitation.create", account["id"], {"role": role,'before':None,
                   'after':{'id':account['id'],'name':name,'active':True,'credentialMode':event.admin_data.credential_mode(db,account['id'],role)}})
        return {"id": account["id"], "name": name, "code": code}, None, None
    if method == "POST" and path == "/api/admin/revoke":
        return event.credential_action(db,actor,{**body,'action':'revoke'}),None,None
    if method=="POST" and path=="/api/admin/team-access":
        return event.team_access(db, actor, body), None, None
    if method=="GET" and path=="/api/admin/submissions/export":
        event.audit(db, actor, "submissions.export")
        return event.submissions.submission_csv(db), None, None
    if method == "GET" and path == "/api/admin/export":
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(["队号", "队名", "项目", "赛道", "评委编号", "评委称呼", "状态", *[r["name"] for r in DIMENSIONS], "总分", "反馈", "更新时间"])
        rows = db.execute("SELECT t.*,s.*,s.updated_at AS score_updated_at,a.name AS judge_name FROM teams t LEFT JOIN scores s ON s.team_id=t.id LEFT JOIN accounts a ON a.id=s.judge_id ORDER BY t.number,a.id")
        for row in rows:
            values = json.loads(row["scores_json"]) if row["scores_json"] else []
            values = values + [""] * (6 - len(values))
            writer.writerow([csv_safe(v) for v in [row["number"], row["name"], row["project"], row["track"], row["judge_id"], row["judge_name"], row["status"], *values, row["total_tenths"] / 10 if row["total_tenths"] is not None else "", row["feedback"], row["score_updated_at"]]])
        event.audit(db, actor, "export")
        return ("\ufeff" + output.getvalue()).encode("utf-8"), None, None
    raise APIError(404, "接口不存在")
