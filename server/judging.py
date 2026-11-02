"""Stage-isolated qualitative review framework; numeric judging is unimplemented."""
from __future__ import annotations

import json
import secrets

from errors import APIError

STAGES={"exhibition":"展示评审","roadshow":"路演评审"}
DIMENSION_NAMES=[("problem","问题价值与理解"),("ai","方案与AI使用"),("prototype","原型完成度"),
                 ("testing","测试与改进"),("opensource","开源与责任"),("teamwork","团队协作与表达")]
BLOCKER="评分数值与计算规则尚未实现，不能发布成绩、排名或晋级"
SCHEMA="""
CREATE TABLE IF NOT EXISTS judging_runs (
 id TEXT PRIMARY KEY, stage TEXT NOT NULL, created_at INTEGER NOT NULL,
 frozen_at INTEGER, reason TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS judging_stages (
 stage TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES judging_runs(id),
 revision INTEGER NOT NULL DEFAULT 0 CHECK(revision>=0),
 open INTEGER NOT NULL DEFAULT 0 CHECK(open IN (0,1)), deadline INTEGER
);
CREATE TABLE IF NOT EXISTS judging_roster (
 run_id TEXT NOT NULL REFERENCES judging_runs(id), team_id TEXT NOT NULL REFERENCES teams(id),
 snapshot_json TEXT NOT NULL, PRIMARY KEY(run_id,team_id)
);
CREATE TABLE IF NOT EXISTS judging_grants (
 run_id TEXT NOT NULL REFERENCES judging_runs(id), judge_id TEXT NOT NULL REFERENCES accounts(id),
 PRIMARY KEY(run_id,judge_id)
);
CREATE TABLE IF NOT EXISTS judging_reviews (
 run_id TEXT NOT NULL REFERENCES judging_runs(id), judge_id TEXT NOT NULL REFERENCES accounts(id),
 team_id TEXT NOT NULL REFERENCES teams(id), version INTEGER NOT NULL CHECK(version>=1),
 draft_feedback TEXT, submitted_feedback TEXT, submitted_at INTEGER,
 recused INTEGER NOT NULL DEFAULT 0 CHECK(recused IN (0,1)), updated_at INTEGER NOT NULL,
 PRIMARY KEY(run_id,judge_id,team_id),
 FOREIGN KEY(run_id,team_id) REFERENCES judging_roster(run_id,team_id),
 FOREIGN KEY(run_id,judge_id) REFERENCES judging_grants(run_id,judge_id)
);
"""


class JudgingService:
    def __init__(self, *, clock, text, audit, touch, history=None, enrich_team=None):
        self.clock,self.text,self.audit,self.touch=clock,text,audit,touch
        self.history,self.enrich_team=history,enrich_team

    def initialize(self, db):
        db.executescript(SCHEMA)
        for stage in STAGES:
            if not db.execute("SELECT 1 FROM judging_stages WHERE stage=?",(stage,)).fetchone():
                run_id=self.new_run(db,stage)
                db.execute("INSERT INTO judging_stages(stage,run_id) VALUES (?,?)",(stage,run_id))

    def new_run(self, db, stage, reason=""):
        run_id="JR"+secrets.token_hex(12)
        db.execute("INSERT INTO judging_runs(id,stage,created_at,reason) VALUES (?,?,?,?)",(run_id,stage,self.clock(),reason))
        return run_id

    def state(self, db, stage):
        if stage not in STAGES:
            raise APIError(404,"评审阶段不存在")
        return db.execute("""SELECT s.*,r.frozen_at,
          (SELECT COUNT(*) FROM judging_reviews v WHERE v.run_id=s.run_id) AS review_count
          FROM judging_stages s JOIN judging_runs r ON r.id=s.run_id WHERE s.stage=?""",(stage,)).fetchone()

    def stage_view(self, state):
        return {"id":state["stage"],"name":STAGES[state["stage"]],"runId":state["run_id"],"revision":state["revision"],"frozenAt":state["frozen_at"],
                "rulesStatus":"unconfigured","rosterSource":"manual_worklist","advancementStatus":"unimplemented",
                "rosterEditable":not state["open"] and state["frozen_at"] is None and state["review_count"]==0,"dimensions":[{"id":key,"name":name,"max":None} for key,name in DIMENSION_NAMES],
                "minValidJudges":None,"aggregation":None,"advancementQuota":None,
                "capabilities":{"reviewRecords":True,"numericScoring":False,"aggregation":False,"ranking":False,"publication":False}}

    def window(self, state, current=None):
        current=self.clock() if current is None else current
        return {"open":bool(state["open"]),"deadline":state["deadline"],
                "canEdit":bool(state["open"]) and (state["deadline"] is None or current<state["deadline"]),"serverTime":current}

    def teams(self, db, run_id):
        records=[json.loads(row["snapshot_json"]) for row in db.execute("SELECT snapshot_json FROM judging_roster WHERE run_id=?",(run_id,))]
        return sorted(records,key=lambda team:team["number"])

    def review_view(self, row, *, include_judge=False):
        draft={"feedback":row["draft_feedback"]} if row["draft_feedback"] is not None else None
        submitted={"feedback":row["submitted_feedback"],"submittedAt":row["submitted_at"]} if row["submitted_feedback"] is not None else None
        result={"teamId":row["team_id"],"version":row["version"],"draft":draft,"submitted":submitted,
                "recused":bool(row["recused"]),"updatedAt":row["updated_at"],
                "hasUnsubmittedChanges":submitted is not None and draft is not None and draft["feedback"]!=submitted["feedback"]}
        result.update(invalidated=bool(row['invalidated']) if 'invalidated' in row.keys() else False,
                      invalidatedReason=row['invalidated_reason'] if 'invalidated_reason' in row.keys() else '')
        if include_judge:
            result["judgeId"]=row["judge_id"]
        return result

    def reviews(self, db, run_id, judge_id=None):
        sql="""SELECT r.*,COALESCE(v.invalidated,0) AS invalidated,COALESCE(v.reason,'') AS invalidated_reason
          FROM judging_reviews r LEFT JOIN judging_review_validity v
          ON v.run_id=r.run_id AND v.judge_id=r.judge_id AND v.team_id=r.team_id WHERE r.run_id=?"""
        if judge_id is None:
            rows=db.execute(sql+' ORDER BY r.team_id,r.judge_id',(run_id,))
        else:
            rows=db.execute(sql+' AND r.judge_id=? ORDER BY r.team_id',(run_id,judge_id))
        return [self.review_view(row,include_judge=judge_id is None) for row in rows]

    def require(self, actor, role):
        if actor is None:
            raise APIError(401,"请先登录")
        if actor["role"]!=role:
            raise APIError(403,"当前身份无权访问")

    def require_grant(self, db, actor, state):
        self.require(actor,"judge")
        if not db.execute("SELECT 1 FROM judging_grants WHERE run_id=? AND judge_id=?",(state["run_id"],actor["id"])).fetchone():
            raise APIError(403,"你尚未获得本轮评审权限，请联系组织方")

    def judge_view(self, db, actor, state):
        self.require_grant(db,actor,state)
        return {"stage":self.stage_view(state),"teams":self.teams(db,state["run_id"]),
                "myReviews":self.reviews(db,state["run_id"],actor["id"]),"window":self.window(state)}

    def admin_view(self, db, state):
        teams=self.teams(db,state["run_id"])
        judge_ids=[row[0] for row in db.execute("SELECT judge_id FROM judging_grants WHERE run_id=? ORDER BY judge_id",(state["run_id"],))]
        judges=[{"id":row["id"],"name":row["name"],"active":bool(row["active"])} for row in db.execute("SELECT id,name,active FROM accounts WHERE role='judge' ORDER BY created_at,id")]
        return {"stage":self.stage_view(state),"window":self.window(state),"roster":{"teamIds":[team["id"] for team in teams],"judgeIds":judge_ids},
                "teams":teams,"judges":judges,"reviews":self.reviews(db,state["run_id"]),"blockers":[BLOCKER]}

    def board(self, db, state):
        counts={row["team_id"]:row["count"] for row in db.execute("""SELECT r.team_id,COUNT(*) AS count FROM judging_reviews r
          LEFT JOIN judging_review_validity v ON v.run_id=r.run_id AND v.judge_id=r.judge_id AND v.team_id=r.team_id
          WHERE r.run_id=? AND r.submitted_feedback IS NOT NULL AND r.recused=0 AND COALESCE(v.invalidated,0)=0 GROUP BY r.team_id""",(state["run_id"],))}
        teams=[{key:team[key] for key in ("id","number","name","project","track")} |
               {"reviewCount":counts.get(team["id"],0),"score":None,"rank":None} for team in self.teams(db,state["run_id"])]
        return {"stage":self.stage_view(state),"window":self.window(state),"teams":teams,
                "publication":{"status":"unavailable","blockers":[BLOCKER]}}

    def exact_fields(self, body, fields):
        if set(body)!=set(fields):
            raise APIError(400,"评审请求字段不正确")

    def expected_revision(self, body, state):
        revision=body.get("expectedRevision")
        if type(revision) is not int or revision<0:
            raise APIError(400,"阶段版本必须是非负整数")
        if revision!=state["revision"]:
            raise APIError(409,"阶段设置已更新，请重新读取后再操作")

    def identifier_list(self, values, label):
        if not isinstance(values,list) or len(values)>1000 or any(not isinstance(value,str) or not 1<=len(value)<=80 for value in values):
            raise APIError(400,label+"必须是有效标识列表")
        if len(values)!=len(set(values)):
            raise APIError(400,label+"不能重复")
        return values

    def capture_team(self, db, team_id, *, materials=False, current=None):
        row=db.execute("SELECT id,number,name,project,track FROM teams WHERE id=?",(team_id,)).fetchone()
        if not row:
            raise APIError(404,"队伍不存在")
        result=self.enrich_team(db,dict(row)) if self.enrich_team else dict(row)
        result["materialSnapshot"]=None
        if materials:
            saved=db.execute("SELECT submitted_json,submitted_version,submitted_at FROM submissions WHERE team_id=?",(team_id,)).fetchone()
            if saved and saved["submitted_json"] is not None:
                result["materialSnapshot"]={"materials":json.loads(saved["submitted_json"]),"submissionVersion":saved["submitted_version"],
                                            "submittedAt":saved["submitted_at"],"capturedAt":current}
        return result

    def set_roster(self, db, actor, state, body):
        self.exact_fields(body,("expectedRevision","teamIds","judgeIds"))
        self.expected_revision(body,state)
        frozen=db.execute("SELECT frozen_at FROM judging_runs WHERE id=?",(state["run_id"],)).fetchone()[0]
        if state["open"] or frozen is not None or db.execute("SELECT 1 FROM judging_reviews WHERE run_id=? LIMIT 1",(state["run_id"],)).fetchone():
            raise APIError(409,"本轮已开放冻结或已有评审记录，不能改写名单；请填写理由创建新轮次")
        team_ids=self.identifier_list(body["teamIds"],"队伍名单")
        judge_ids=self.identifier_list(body["judgeIds"],"评委名单")
        teams=[self.capture_team(db,team_id) for team_id in team_ids]
        before={'teamIds':[team['id'] for team in self.teams(db,state['run_id'])],
                'judgeIds':[row[0] for row in db.execute('SELECT judge_id FROM judging_grants WHERE run_id=? ORDER BY judge_id',(state['run_id'],))]}
        for judge_id in judge_ids:
            if not db.execute("SELECT 1 FROM accounts WHERE id=? AND role='judge' AND active=1",(judge_id,)).fetchone():
                raise APIError(400,"评委不存在或已停用")
        db.execute("DELETE FROM judging_roster WHERE run_id=?",(state["run_id"],))
        db.execute("DELETE FROM judging_grants WHERE run_id=?",(state["run_id"],))
        for team in teams:
            db.execute("INSERT INTO judging_roster VALUES (?,?,?)",(state["run_id"],team["id"],json.dumps(team,ensure_ascii=False)))
        db.executemany("INSERT INTO judging_grants VALUES (?,?)",[(state["run_id"],judge_id) for judge_id in judge_ids])
        self.admin_change(db,actor,state,"roster",{"teamCount":len(teams),"judgeCount":len(judge_ids),'before':before,
                          'after':{'teamIds':team_ids,'judgeIds':judge_ids}})

    def set_window(self, db, actor, state, body):
        self.exact_fields(body,("expectedRevision","open","deadline"))
        self.expected_revision(body,state)
        if type(body["open"]) is not bool:
            raise APIError(400,"评审开放状态必须为布尔值")
        deadline=body["deadline"]
        if deadline is not None and (type(deadline) is not int or not 0<=deadline<=253402300799999):
            raise APIError(400,"评审截止时间必须是有效毫秒时间戳或空值")
        current=self.clock()
        if body["open"]:
            if not db.execute("SELECT 1 FROM judging_roster WHERE run_id=?",(state["run_id"],)).fetchone() or not db.execute("SELECT 1 FROM judging_grants WHERE run_id=?",(state["run_id"],)).fetchone():
                raise APIError(409,"请先选择本轮队伍和评委，再开放定性评审记录")
            frozen=db.execute("SELECT frozen_at FROM judging_runs WHERE id=?",(state["run_id"],)).fetchone()[0]
            if frozen is None:
                for team in self.teams(db,state["run_id"]):
                    snapshot=self.capture_team(db,team["id"],materials=True,current=current)
                    db.execute("UPDATE judging_roster SET snapshot_json=? WHERE run_id=? AND team_id=?",(json.dumps(snapshot,ensure_ascii=False),state["run_id"],team["id"]))
                db.execute("UPDATE judging_runs SET frozen_at=? WHERE id=?",(current,state["run_id"]))
        db.execute("UPDATE judging_stages SET open=?,deadline=? WHERE stage=?",(int(body["open"]),deadline,state["stage"]))
        self.admin_change(db,actor,state,"window",{"open":body["open"],"deadline":deadline,
                          'before':{'open':bool(state['open']),'deadline':state['deadline']},'after':{'open':body['open'],'deadline':deadline}})

    def reset(self, db, actor, state, body):
        self.exact_fields(body,("expectedRevision","reason"))
        self.expected_revision(body,state)
        if state["open"]:
            raise APIError(409,"请先关闭本轮评审，再填写理由创建新轮次")
        reason=self.text(body["reason"],"创建新轮次理由",2000,True)
        new_id=self.new_run(db,state["stage"],reason)
        for team in self.teams(db,state["run_id"]):
            current=self.capture_team(db,team["id"])
            db.execute("INSERT INTO judging_roster VALUES (?,?,?)",(new_id,team["id"],json.dumps(current,ensure_ascii=False)))
        db.execute("INSERT INTO judging_grants SELECT ?,judge_id FROM judging_grants WHERE run_id=?",(new_id,state["run_id"]))
        db.execute("UPDATE judging_stages SET run_id=?,open=0,deadline=NULL WHERE stage=?",(new_id,state["stage"]))
        self.admin_change(db,actor,state,"reset",{"oldRunId":state["run_id"],"newRunId":new_id,"reason":reason,
                          'before':{'runId':state['run_id'],'open':bool(state['open']),'deadline':state['deadline']},
                          'after':{'runId':new_id,'open':False,'deadline':None}})

    def admin_change(self, db, actor, state, action, metadata):
        db.execute("UPDATE judging_stages SET revision=revision+1 WHERE stage=?",(state["stage"],))
        self.audit(db,actor,"judging."+action,state["stage"],{"runId":state["run_id"],**metadata})
        self.touch(db)

    def save_review(self, db, actor, state, body):
        self.require_grant(db,actor,state)
        self.exact_fields(body,("runId","teamId","expectedVersion","status","feedback","scores"))
        if not isinstance(body["runId"],str) or body["runId"]!=state["run_id"]:
            raise APIError(409,"评审轮次已更新，请重新读取；当前输入不会覆盖新轮次")
        version=body["expectedVersion"]
        if type(version) is not int or version<0:
            raise APIError(400,"评审版本必须是非负整数")
        status=body["status"]
        if not isinstance(status,str) or status not in {"draft","submitted","recused"}:
            raise APIError(400,"评审状态不正确")
        if body["scores"] is not None:
            raise APIError(409,"数值评分尚未实现，请仅保存定性评审；scores必须为空")
        team_id=body["teamId"]
        if not isinstance(team_id,str) or len(team_id)>80:
            raise APIError(400,"队伍标识不正确")
        if not db.execute("SELECT 1 FROM judging_roster WHERE run_id=? AND team_id=?",(state["run_id"],team_id)).fetchone():
            raise APIError(403,"该队伍不在本轮评审名单中")
        feedback=self.text(body["feedback"],"评审意见",2000,status=="submitted")
        current=self.clock()
        if not self.window(state,current)["canEdit"]:
            raise APIError(409,"本轮定性评审尚未开放或已截止，当前只读")
        previous=db.execute("SELECT * FROM judging_reviews WHERE run_id=? AND judge_id=? AND team_id=?",(state["run_id"],actor["id"],team_id)).fetchone()
        if version!=(previous["version"] if previous else 0):
            raise APIError(409,"评审记录已由其他页面更新，请保留输入并重新读取后合并")
        draft=previous["draft_feedback"] if previous else None
        submitted=previous["submitted_feedback"] if previous else None
        submitted_at=previous["submitted_at"] if previous else None
        if status!="recused":
            draft=feedback
        if status=="submitted":
            submitted,submitted_at=feedback,current
        db.execute("""INSERT INTO judging_reviews VALUES (?,?,?,?,?,?,?,?,?)
          ON CONFLICT(run_id,judge_id,team_id) DO UPDATE SET version=excluded.version,draft_feedback=excluded.draft_feedback,
          submitted_feedback=excluded.submitted_feedback,submitted_at=excluded.submitted_at,recused=excluded.recused,updated_at=excluded.updated_at""",
          (state["run_id"],actor["id"],team_id,version+1,draft,submitted,submitted_at,int(status=="recused"),current))
        if self.history:
            after=db.execute('SELECT * FROM judging_reviews WHERE run_id=? AND judge_id=? AND team_id=?',(state['run_id'],actor['id'],team_id)).fetchone()
            self.history(db,actor,state['run_id'],actor['id'],team_id,'review.'+status,
                         self.review_view(previous,include_judge=True) if previous else None,self.review_view(after,include_judge=True))
        self.audit(db,actor,"judging.review."+status,team_id,{"stage":state["stage"],"runId":state["run_id"],"version":version+1})
        self.touch(db)
        return {"ok":True,**self.judge_view(db,actor,state)}

    def dispatch(self, db, actor, method, path, body):
        admin=path.startswith("/api/admin/judging/")
        prefix="/api/admin/judging/" if admin else "/api/judging/"
        route=path[len(prefix):].split("/")
        stage=route[0]
        state=self.state(db,stage)
        action=route[1] if len(route)==2 else "" if len(route)==1 else None
        if admin:
            self.require(actor,"admin")
            if method=="GET" and action=="":
                return self.admin_view(db,state)
            if method=="POST" and action in {"roster","window","reset"}:
                {"roster":self.set_roster,"window":self.set_window,"reset":self.reset}[action](db,actor,state,body)
                return {"ok":True,**self.admin_view(db,self.state(db,stage))}
            if method=="POST" and action=="publish":
                self.exact_fields(body,("runId",))
                raise APIError(409,BLOCKER)
        else:
            if method=="GET" and action=="board":
                return self.board(db,state)
            if method=="GET" and action=="":
                return self.judge_view(db,actor,state)
            if method=="POST" and action=="review":
                return self.save_review(db,actor,state,body)
        raise APIError(404,"评审接口不存在")
