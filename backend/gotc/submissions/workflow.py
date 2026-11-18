"""Team submission drafts, validation, transactional writes and receipts."""
from __future__ import annotations

import csv
import io
import json
import re
from urllib.parse import urlsplit

from ..errors import APIError
from ..validation import csv_safe, normalize_code_version, text_field, valid_material_host
from ..teams.admin_team_access import operator_for


class SubmissionWorkflow:
    def __init__(self, event):
        self.event = event

    def dispatch_submission(self,body,cookie,expected_login_id=None,server_received_at=None):
        try:
            proof=self.event.repositories.preflight(body,cookie,expected_login_id)
        except APIError:
            # A concurrent identical request may have committed while upstream I/O failed.
            # Let the receipt writer validate its fingerprint before returning any old result.
            with self.event.db() as db:
                actor=self.event.account(db,cookie);self.event.require(actor,'team')
                team=self.event.bound_submission_team(db,actor)
                request_id=body.get('requestId') if isinstance(body,dict) else None
                if not isinstance(request_id,str) or not db.execute('SELECT 1 FROM submission_requests WHERE team_id=? AND request_id=?',(team['id'],request_id)).fetchone():raise
            proof=None
        with self.event.db() as db:
            preliminary = self.event.write_identity(db, cookie, 'team', expected_login_id)
            if operator_for(preliminary) and not expected_login_id:
                raise APIError(428, '请刷新页面并核对当前代管队伍', 'login_required')
            db.execute('BEGIN IMMEDIATE')
            actor=self.event.account(db,cookie);self.event.require(actor,'team')
            if operator_for(actor) and not expected_login_id:raise APIError(428,'请刷新页面并核对当前代管队伍','login_required')
            if expected_login_id is not None and expected_login_id!=self.event.login_id(cookie):raise APIError(401,'登录身份已变化，请重新登录后操作')
            team=self.event.bound_submission_team(db,actor)
            if proof and proof.get('repositorySeal'):
                proof['receivedAt']=self.event.clock() if server_received_at is None else server_received_at
            result=self.event.submission_records.save(db,actor,team,body,repository_proof=proof)
        if isinstance(result,APIError):raise result
        return result,None,None

    def submission_window(self, db, current=None):
        config=self.event.config(db)
        current=self.event.clock() if current is None else current
        return {"submissionsOpen": config["submissionsOpen"], "submissionDeadline": config["submissionDeadline"],
                "canEdit": config["submissionsOpen"] and (config["submissionDeadline"] is None or current<config["submissionDeadline"]), "serverTime": current}

    def submission(self, db, team_id):
        row=db.execute("SELECT * FROM submissions WHERE team_id=?", (team_id,)).fetchone()
        if not row:
            return {"version": 0, "draft": None, "submitted": None, "submittedVersion": None,
                    "firstSubmittedAt": None, "submittedAt": None, "updatedAt": None, "hasUnsubmittedChanges": False}
        draft=json.loads(row["draft_json"]) if row["draft_json"] else None
        submitted=json.loads(row["submitted_json"]) if row["submitted_json"] else None
        # This locator is server metadata; editing state compares contestant values.
        draft_values={key:value for key,value in draft.items() if key!='repositoryMaterialsUrl'} if draft is not None else None
        submitted_values={key:value for key,value in submitted.items() if key!='repositoryMaterialsUrl'} if submitted is not None else None
        return {"version": row["version"], "draft": draft, "submitted": submitted, "submittedVersion": row["submitted_version"],
                "firstSubmittedAt": row["first_submitted_at"], "submittedAt": row["submitted_at"], "updatedAt": row["updated_at"],
                "hasUnsubmittedChanges": submitted is not None and draft_values!=submitted_values}

    def team_submission_response(self, db, team):
        submission=self.submission(db, team['id'])
        return {"team": {key: team[key] for key in ("id", "number", "name", "project", "track")},
                "repositorySeal": self.event.repositories.seals.for_version(db,team['id'],submission['submittedVersion']),
                "repositoryBackup": self.event.repository_backups.for_version(db,team['id'],submission['submittedVersion']),
                "submission": submission, "window": self.submission_window(db),
                "repository": self.event.repositories.public(self.event.repositories.get(db,team['id'])),
                "repositoryProviders": self.event.repositories.availability(),
                "judgingMaterials":self.judging_materials(db,team['id'])}

    def judging_materials(self, db, team_id):
        result={}
        for row in db.execute('SELECT s.stage,s.run_id,r.frozen_at FROM judging_stages s JOIN judging_runs r ON r.id=s.run_id'):
            roster=db.execute('SELECT snapshot_json FROM judging_roster WHERE run_id=? AND team_id=?',(row['run_id'],team_id)).fetchone()
            snapshot=json.loads(roster['snapshot_json']).get('materialSnapshot') if roster else None
            result[row['stage']]={'runId':row['run_id'],'inRoster':roster is not None,'frozenAt':row['frozen_at'],'materialSnapshot':snapshot}
        return result

    def check_materials(self, materials, submitted, lean_gitee=False):
        limits={"projectName": 140, "summary": 600, "license": 200, "demoInstructions": 2000,
                "sourcesAndLimitations": 2000, "codeVersion": 1024}
        urls=("codeUrl", "readmeUrl", "licenseUrl", "demoUrl", "slidesUrl", "videoUrl", "filesUrl")
        if not isinstance(materials, dict) or not set(materials).issubset({*limits, *urls, "confirmed"}):
            raise APIError(400, "项目材料字段不正确")
        if lean_gitee:
            materials={**materials,**{key:'' for key in ('readmeUrl','license','licenseUrl','sourcesAndLimitations')}}
        result={key: text_field(materials.get(key, ""), key, limit) for key,limit in limits.items()}
        if len(result['codeVersion'].encode('utf-8'))>1024:
            raise APIError(400,'代码版本最多1024字节')
        for key in urls:
            limit=8192 if key=='demoUrl' else 1024
            schemes=('http','https') if key=='demoUrl' else ('https',)
            value=text_field(materials.get(key, ""), key, limit)
            if value:
                try:
                    parsed=urlsplit(value)
                    valid=(parsed.scheme in schemes and valid_material_host(parsed.hostname) and parsed.username is None and parsed.password is None
                           and parsed.port!=0 and len(value.encode("utf-8"))<=limit
                           and not any(c.isspace() or ord(c)<32 or ord(c)==127 or c=="\\" for c in value)
                           and not re.search(r"%(?:0[0-9a-f]|1[0-9a-f]|7f)", value, re.I))
                except ValueError:
                    valid=False
                if not valid:
                    protocol='HTTP 或 HTTPS' if key=='demoUrl' else 'HTTPS'
                    raise APIError(400, f"{key}必须是无凭据、无空白的完整 {protocol} 链接（最多{limit}字节）")
            result[key]=value
        confirmed=materials.get("confirmed", False)
        if type(confirmed) is not bool:
            raise APIError(400, "提交确认必须为布尔值")
        result["confirmed"]=confirmed
        if submitted:
            required=("slidesUrl", "filesUrl")
            if any(not result[key] for key in required):
                raise APIError(400, "正式提交只需提供PPT和作品压缩包，请补齐这两项")
            if result['codeUrl'] or result['codeVersion']:
                result['codeVersion']=normalize_code_version(result['codeUrl'],result['codeVersion'],allow_empty=True)
        return result

    def save_submission(self, db, actor, team, body, accepted_at=None):
        if set(body)!={"expectedVersion", "status", "materials"} or (not isinstance(body.get("status"), str) or body["status"] not in {"draft", "submitted"}):
            raise APIError(400, "提交字段或状态不正确")
        if type(body["expectedVersion"]) is not int or body["expectedVersion"]<0:
            raise APIError(400, "材料版本必须是非负整数")
        submitted=body["status"]=="submitted"
        binding=self.event.repositories.binding(db,team['id'])
        lean_gitee=bool(binding and binding['provider']=='gitee')
        raw_materials=body['materials']
        if lean_gitee and isinstance(raw_materials,dict) and 'repositoryMaterialsUrl' in raw_materials:
            # Round-tripping a prior server snapshot is supported, but a client never
            # chooses the formal material reference. Drafts drop this server metadata.
            raw_materials=dict(raw_materials)
            reference=raw_materials.pop('repositoryMaterialsUrl')
            sha=raw_materials.get('codeVersion','')
            url=self.event.repositories.public(binding)['codeUrl']
            if (not isinstance(sha,str) or not re.fullmatch(r'[a-f0-9]{40}|[a-f0-9]{64}',sha)
                    or raw_materials.get('codeUrl')!=url or reference!=url+'/tree/'+sha):
                raise APIError(400,'仓库材料定位必须对应绑定仓库的完整版本','repository_override')
        materials=self.check_materials(raw_materials, submitted,lean_gitee=lean_gitee)
        self.event.material_uploads.validate_materials(db,team['id'],materials)
        if lean_gitee:
            for key in ('readmeUrl','license','licenseUrl','sourcesAndLimitations'):
                materials[key]=''
            if submitted and materials['codeVersion']:
                materials['repositoryMaterialsUrl']=self.event.repositories.public(binding)['codeUrl']+'/tree/'+materials['codeVersion']
        current=self.event.clock()
        if not self.submission_window(db, current if accepted_at is None else accepted_at)["canEdit"]:
            raise APIError(409, "作品提交尚未开放或已截止，当前材料只读", "submission_closed")
        previous=self.submission(db, team["id"])
        if body["expectedVersion"]!=previous["version"]:
            raise APIError(409, "材料已被其他页面更新。当前输入已保留，请重新读取并合并后再保存", "version_conflict")
        encoded=json.dumps(materials, ensure_ascii=False, sort_keys=True)
        version=previous["version"]+1
        db.execute("""INSERT INTO submissions VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(team_id) DO UPDATE SET
          version=excluded.version,draft_json=excluded.draft_json,submitted_json=excluded.submitted_json,
          submitted_version=excluded.submitted_version,first_submitted_at=excluded.first_submitted_at,
          submitted_at=excluded.submitted_at,updated_at=excluded.updated_at""",
          (team["id"], version, encoded, encoded if submitted else (json.dumps(previous["submitted"], ensure_ascii=False, sort_keys=True) if previous["submitted"] else None),
           version if submitted else previous["submittedVersion"], (previous["firstSubmittedAt"] or current) if submitted else previous["firstSubmittedAt"],
           current if submitted else previous["submittedAt"], current))
        if submitted:
            db.execute("UPDATE teams SET project=?,updated_at=? WHERE id=?", (materials["projectName"] or team["project"], current, team["id"]))
            db.execute('UPDATE team_details SET revision=revision+1 WHERE team_id=?',(team['id'],))
            self.event.touch(db)
        self.event.audit(db, actor, "submission."+body["status"], team["id"], {"version": version})
        updated=db.execute("SELECT * FROM teams WHERE id=?", (team["id"],)).fetchone()
        return {"ok": True, **self.team_submission_response(db, updated)}

    def judge_submissions(self, db):
        return [{"teamId": row["team_id"], "materials": json.loads(row["submitted_json"]), "version": row["submitted_version"],
                 "firstSubmittedAt": row["first_submitted_at"], "submittedAt": row["submitted_at"]}
                for row in db.execute("""SELECT s.* FROM submissions s WHERE submitted_json IS NOT NULL AND NOT EXISTS
                    (SELECT 1 FROM team_recycle_events e WHERE e.team_id=s.team_id AND e.restored_at IS NULL) ORDER BY team_id""")]

    def admin_submissions(self, db):
        rows=db.execute("""SELECT t.id AS team_id,a.id AS account_id,a.active FROM teams t
          LEFT JOIN team_accounts b ON b.team_id=t.id LEFT JOIN accounts a ON a.id=b.account_id ORDER BY t.number""")
        return [{"teamId": row["team_id"], "access": {"id": row["account_id"], "active": bool(row["active"])} if row["account_id"] else None,
                 "submission": self.submission(db, row["team_id"]),
                 "judgingMaterials":self.judging_materials(db,row['team_id'])} for row in rows]

    def submission_csv(self, db):
        fields=("projectName", "summary", "codeUrl", "readmeUrl", "license", "licenseUrl", "demoUrl", "demoInstructions", "slidesUrl", "videoUrl", "filesUrl", "sourcesAndLimitations", "codeVersion", "confirmed", "repositoryMaterialsUrl")
        output=io.StringIO(newline="")
        writer=csv.writer(output)
        writer.writerow(["队号", "队名", "赛道", "展示作品名", "提交状态", "编辑版本", "正式版本", "首次提交时间", "正式提交时间", "更新时间", "有未提交修改",
                         *["正式_"+key for key in fields], *["草稿_"+key for key in fields]])
        for team in db.execute("SELECT * FROM teams ORDER BY number"):
            record=self.submission(db, team["id"])
            state="已提交且有草稿修改" if record["hasUnsubmittedChanges"] else "已正式提交" if record["submitted"] else "草稿" if record["draft"] else "未保存"
            writer.writerow([csv_safe(value) for value in [team["number"], team["name"], team["track"], team["project"], state, record["version"], record["submittedVersion"],
                             record["firstSubmittedAt"], record["submittedAt"], record["updatedAt"], record["hasUnsubmittedChanges"],
                             *[(record["submitted"] or {}).get(key, "") for key in fields], *[(record["draft"] or {}).get(key, "") for key in fields]]])
        return ("\ufeff"+output.getvalue()).encode("utf-8")
