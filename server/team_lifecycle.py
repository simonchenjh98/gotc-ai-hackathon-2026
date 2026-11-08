"""Private, reversible team retirement. Call mutations under BEGIN IMMEDIATE."""
import hashlib
import hmac
import json
import re
import secrets
import sqlite3

from claims import normalize, fingerprint
from errors import APIError
from team_registration import code_hash


SCHEMA='''
CREATE TABLE IF NOT EXISTS team_recycle_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 team_id TEXT NOT NULL REFERENCES teams(id),
 deleted_at INTEGER NOT NULL,
 deleted_by TEXT NOT NULL REFERENCES accounts(id),
 delete_reason TEXT NOT NULL,
 original_status TEXT NOT NULL CHECK(original_status IN ('active','withdrawn','disqualified')),
 original_status_reason TEXT NOT NULL,
 team_snapshot_json TEXT NOT NULL,
 roster_snapshot_json TEXT NOT NULL,
 restored_at INTEGER,
 restored_by TEXT REFERENCES accounts(id),
 restore_reason TEXT,
 restored_name TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS team_recycle_current
 ON team_recycle_events(team_id) WHERE restored_at IS NULL;
CREATE TABLE IF NOT EXISTS team_recycle_memberships (
 event_id INTEGER NOT NULL REFERENCES team_recycle_events(id),
 person_id TEXT NOT NULL REFERENCES claim_people(id),
 request_id TEXT NOT NULL REFERENCES claim_requests(id),
 name TEXT NOT NULL,
 school TEXT NOT NULL,
 normalized_name TEXT NOT NULL,
 normalized_school TEXT NOT NULL,
 PRIMARY KEY(event_id,person_id)
);
'''
TEAM_FIELDS=('id','number','name','project','track','status','statusReason','booth','roadshowOrder','revision')
PATHS={'/api/admin/data/team','/api/admin/data/recycle-bin',
       '/api/admin/data/team/recycle','/api/admin/data/team/restore','/api/admin/data/team/members'}


class TeamLifecycle:
    def __init__(self,service):
        self.s=service

    def initialize(self,db):
        db.executescript(SCHEMA)

    def current_event(self,db,team_id):
        return db.execute('SELECT * FROM team_recycle_events WHERE team_id=? AND restored_at IS NULL',(team_id,)).fetchone()

    def is_recycled(self,db,team_id):
        return self.current_event(db,team_id) is not None

    def require_available(self,db,team_id):
        if self.is_recycled(db,team_id):
            raise APIError(409,'队伍已移入回收站，请先按冲突检查恢复','team_recycled')

    def name_collisions(self,db,name,exclude_team_id=None):
        return [dict(row) for row in db.execute('''SELECT t.id,t.name FROM teams t
            WHERE NOT EXISTS (SELECT 1 FROM team_recycle_events e
              WHERE e.team_id=t.id AND e.restored_at IS NULL) ORDER BY t.number''')
                if row['id']!=exclude_team_id and normalize(row['name'])==normalize(name)]

    def team(self,db,team_id):
        self.s.team_exists(db,team_id)
        team=self.s.admin_data.team(db,team_id)
        return {key:team[key] for key in TEAM_FIELDS}

    def memberships(self,db,team_id):
        return db.execute('''SELECT m.person_id,m.request_id,p.name,p.school,p.normalized_name,
            p.normalized_school,p.needs_review,p.revision AS person_revision,
            r.status AS request_status,r.revision AS request_revision,r.captain_id
            FROM claim_memberships m JOIN claim_people p ON p.id=m.person_id
            JOIN claim_requests r ON r.id=m.request_id WHERE m.team_id=?
            ORDER BY CASE WHEN m.person_id=r.captain_id THEN 0 ELSE 1 END,m.person_id''',(team_id,)).fetchall()

    @staticmethod
    def school_state(school,needs_review=False):
        if not school:return 'missing'
        return 'needs_review' if needs_review or normalize(school)=='待核对' else 'recorded'

    def member_view(self,db,team_id):
        event=self.current_event(db,team_id)
        if event:return json.loads(event['roster_snapshot_json'])
        rows=self.memberships(db,team_id)
        if rows:
            return {'source':'approved_memberships','members':[
                {'personId':r['person_id'],'name':r['name'],'school':r['school'] or None,
                 'role':('captain' if r['person_id']==r['captain_id'] else 'member') if r['captain_id'] else 'unknown',
                 'source':'approved_membership','schoolState':self.school_state(r['school'],r['needs_review'])}
                for r in rows]}
        registration=db.execute('SELECT captain,school,members_json FROM team_registrations WHERE team_id=?',(team_id,)).fetchone()
        if not registration:return {'source':'none','members':[]}
        # Only the recorded captain has school provenance; same-name identities
        # elsewhere must never be used to fill another registration member's school.
        captain=registration['captain'];members=[];seen=set()
        for name in [captain,*json.loads(registration['members_json'])]:
            key=normalize(name)
            if not key or key in seen:continue
            seen.add(key)
            is_captain=key==normalize(captain)
            school=registration['school'] if is_captain else None
            members.append({'personId':None,'name':name,'school':school or None,
                'role':'captain' if is_captain else 'member',
                'source':'registration_captain' if is_captain else 'registration_member',
                'schoolState':self.school_state(school)})
        return {'source':'registration','members':members}

    def recycle_context(self,db,team_id):
        event=self.current_event(db,team_id)
        registration=db.execute('SELECT revision,captain,school,members_json FROM team_registrations WHERE team_id=?',(team_id,)).fetchone()
        account=db.execute('''SELECT a.* FROM accounts a JOIN team_accounts b ON b.account_id=a.id
            WHERE b.team_id=?''',(team_id,)).fetchone()
        api=db.execute('SELECT active FROM team_api WHERE team_id=?',(team_id,)).fetchone()
        snapshot={'team':self.team(db,team_id),'eventId':event['id'] if event else None,
            'memberships':[dict(row) for row in self.memberships(db,team_id)],
            'registration':{'revision':registration['revision'],'captain':registration['captain'],
                'school':registration['school'],'members':json.loads(registration['members_json'])} if registration else None,
            'account':{'id':account['id'],'role':account['role'],'active':account['active'],
                'fingerprint':fingerprint(account)} if account else None,
            'api':{'active':api['active']} if api else None,
            'keys':[dict(row) for row in db.execute('SELECT id,active FROM api_keys WHERE team_id=? ORDER BY id',(team_id,))]}
        encoded=json.dumps(snapshot,sort_keys=True,ensure_ascii=False,separators=(',',':'))
        return hashlib.sha256(('team-recycle-context-v1\0'+encoded).encode()).hexdigest()

    def restore_conflicts(self,db,team_id,name):
        event=self.current_event(db,team_id)
        if not event:return []
        conflicts=[]
        def add(kind,message,person=None,other=None):
            conflicts.append({'type':kind,'personId':person['person_id'] if person else None,
                'memberName':person['name'] if person else None,'teamId':other['id'] if other else None,
                'teamName':other['name'] if other else None,'message':message})
        for other in self.name_collisions(db,name,team_id):add('name','队名已被其他队伍使用，请填写新的队名',other=other)
        for archived in db.execute('SELECT * FROM team_recycle_memberships WHERE event_id=? ORDER BY person_id',(event['id'],)):
            person=db.execute('SELECT normalized_name,normalized_school FROM claim_people WHERE id=?',(archived['person_id'],)).fetchone()
            if not person:
                add('identity_missing','删除时成员身份已不存在，须先核对身份记录',archived)
            elif (person['normalized_name'],person['normalized_school'])!=(archived['normalized_name'],archived['normalized_school']):
                add('identity_changed','删除时成员身份已变更，须先核对身份记录',archived)
            other=db.execute('''SELECT t.id,t.name FROM claim_memberships m JOIN teams t ON t.id=m.team_id
                WHERE m.person_id=? AND m.team_id!=?''',(archived['person_id'],team_id)).fetchone()
            if other:add('member_assigned','成员当前已属于其他队伍，不能覆盖其归属',archived,other)
        roster=json.loads(event['roster_snapshot_json'])
        # Exact known name+school assignments are blocking even for a captain
        # who was registered but not assigned an approved person ID at deletion.
        for member in roster['members']:
            if not member['school'] or member['schoolState']!='recorded':continue
            for other in db.execute('''SELECT t.id,t.name FROM claim_people p
                JOIN claim_memberships m ON m.person_id=p.id JOIN teams t ON t.id=m.team_id
                WHERE p.normalized_name=? AND p.normalized_school=? AND t.id!=?''',
                (normalize(member['name']),normalize(member['school']),team_id)):
                if not any(c['type']=='member_assigned' and c['memberName']==member['name'] and c['teamId']==other['id'] for c in conflicts):
                    add('registration_overlap','同名同校身份当前已属于其他队伍',{'person_id':member['personId'],'name':member['name']},other)
        registration=db.execute('SELECT school,members_json FROM team_registrations WHERE team_id=?',(team_id,)).fetchone()
        # The registration policy knows only the captain's school. Approved
        # members already have exact identities and their own recorded schools;
        # applying that name-only policy would wrongly merge distinct people.
        if registration and roster['source']=='registration':
            names={normalize(n) for n in json.loads(registration['members_json'])}
            for other in db.execute('''SELECT t.id,t.name,r.school,r.members_json FROM team_registrations r
                JOIN teams t ON t.id=r.team_id WHERE t.id!=? AND NOT EXISTS
                (SELECT 1 FROM team_recycle_events e WHERE e.team_id=t.id AND e.restored_at IS NULL)''',(team_id,)):
                if normalize(other['school'])==normalize(registration['school']) and names.intersection(normalize(n) for n in json.loads(other['members_json'])):
                    add('registration_overlap','同校同名队员已在其他队登记，请先核对组队关系',other=other)
        return conflicts

    def restore_advisories(self,db,team_id):
        event=self.current_event(db,team_id)
        if not event:return []
        result=[];seen=set()
        for member in json.loads(event['roster_snapshot_json'])['members']:
            if member['school'] is not None:continue
            for other in db.execute('''SELECT t.id,t.name FROM claim_people p JOIN claim_memberships m ON m.person_id=p.id
                JOIN teams t ON t.id=m.team_id WHERE p.normalized_name=? AND t.id!=?
                AND NOT EXISTS (SELECT 1 FROM team_recycle_events e WHERE e.team_id=t.id AND e.restored_at IS NULL)''',
                (normalize(member['name']),team_id)):
                key=(member['name'],other['id'])
                if key in seen:continue
                seen.add(key)
                result.append({'type':'unverified_name_overlap','memberName':member['name'],
                    'teamId':other['id'],'teamName':other['name'],'message':'存在同名记录，学校未知，不能确定是否为同一人'})
        return result

    @staticmethod
    def recycle_state(event):
        return {'eventId':event['id'],'deletedAt':event['deleted_at'],'reason':event['delete_reason'],
                'originalStatus':event['original_status']} if event else None

    def detail(self,db,team_id):
        team=self.team(db,team_id);event=self.current_event(db,team_id)
        return {'team':{**team,'recycled':event is not None},'roster':self.member_view(db,team_id),
            'recycle':self.recycle_state(event),'recycleContext':None if event else self.recycle_context(db,team_id),
            'restoreConflicts':self.restore_conflicts(db,team_id,team['name']) if event else [],
            'restoreAdvisories':self.restore_advisories(db,team_id) if event else [],
            **self.s.team_members.view(db,team_id)}

    def listing(self,db):
        return {'teams':[{'id':row['team_id'],'number':row['number'],'name':row['name'],
            'revision':row['revision'],'recycle':self.recycle_state(row)} for row in db.execute('''
            SELECT e.*,t.number,t.name,d.revision FROM team_recycle_events e
            JOIN teams t ON t.id=e.team_id JOIN team_details d ON d.team_id=t.id
            WHERE e.restored_at IS NULL ORDER BY e.deleted_at DESC,e.id DESC''')]}

    def validate_mutation(self,db,actor,body,restore=False):
        self.s.require(actor,'admin')
        allowed={'teamId','expectedRevision','reason','name' if restore else 'recycleContext'}
        if not isinstance(body,dict) or not set(body).issubset(allowed):raise APIError(400,'队伍操作字段不正确')
        if 'expectedRevision' not in body:raise APIError(428,'请先读取队伍最新版本','revision_required')
        revision=body['expectedRevision']
        if type(revision) is not int or revision<0:raise APIError(400,'队伍版本必须是非负整数')
        if not restore:
            if 'recycleContext' not in body:raise APIError(428,'请先读取并确认当前成员资料','recycle_context_required')
            context=body['recycleContext']
            if not isinstance(context,str) or not re.fullmatch('[0-9a-f]{64}',context):raise APIError(400,'队伍确认上下文不正确')
        reason=self.s.admin_data.text(body.get('reason'),'操作原因',500,True)
        team=self.team(db,body.get('teamId'))
        if revision!=team['revision']:raise APIError(409,'队伍信息已更新，请保留输入并重新读取','team_revision_conflict')
        name=self.s.admin_data.text(body.get('name'),'队名',80,True) if restore else None
        return team,reason,name

    def invalidate_publications(self,db,team_id):
        # Explicit epochs cover already-withdrawn teams and active->withdrawn->
        # active cycles. Frozen publication/snapshot JSON remains untouched.
        db.execute('''UPDATE roadshow_scoring_config SET revision=revision+1 WHERE id=1 AND EXISTS
            (SELECT 1 FROM judging_roster r JOIN judging_stages s ON s.run_id=r.run_id
             WHERE s.stage='roadshow' AND r.team_id=?)''',(team_id,))
        db.execute('''UPDATE finals_config SET eligibility_revision=eligibility_revision+1 WHERE id=1
            AND EXISTS (SELECT 1 FROM finals_teams WHERE team_id=?)''',(team_id,))

    def access_state(self,db,team_id):
        return {'submissionAccess':'inactive' if db.execute('SELECT 1 FROM team_accounts WHERE team_id=?',(team_id,)).fetchone() else 'not_issued',
            'apiAccess':'inactive' if db.execute('SELECT 1 FROM team_api WHERE team_id=?',(team_id,)).fetchone() else 'not_provisioned',
            'registrationAccess':'reissue_required' if db.execute('SELECT 1 FROM team_registrations WHERE team_id=?',(team_id,)).fetchone() else 'not_registered'}

    def response(self,db,team_id):
        detail=self.detail(db,team_id)
        return {'ok':True,'team':detail['team'],'recycle':detail['recycle'],'access':self.access_state(db,team_id)}

    def recycle(self,db,actor,body):
        team,reason,_=self.validate_mutation(db,actor,body)
        team_id=team['id'];self.require_available(db,team_id)
        if not hmac.compare_digest(body['recycleContext'],self.recycle_context(db,team_id)):
            raise APIError(409,'队伍成员或访问状态已更新，请重新读取并确认','team_context_conflict')
        roster=self.member_view(db,team_id);memberships=self.memberships(db,team_id);now=self.s.admin_data.now()
        event_id=db.execute('''INSERT INTO team_recycle_events
            (team_id,deleted_at,deleted_by,delete_reason,original_status,original_status_reason,team_snapshot_json,roster_snapshot_json)
            VALUES (?,?,?,?,?,?,?,?)''',(team_id,now,actor['id'],reason,team['status'],team['statusReason'],
                json.dumps(team,ensure_ascii=False),json.dumps(roster,ensure_ascii=False))).lastrowid
        for row in memberships:
            db.execute('INSERT INTO team_recycle_memberships VALUES (?,?,?,?,?,?,?)',
                (event_id,row['person_id'],row['request_id'],row['name'],row['school'],row['normalized_name'],row['normalized_school']))
        db.execute("UPDATE team_details SET status='withdrawn',status_reason=?,revision=revision+1 WHERE team_id=?",(reason,team_id))
        db.execute('UPDATE teams SET updated_at=? WHERE id=?',(now,team_id))
        db.execute('DELETE FROM sessions WHERE account_id IN (SELECT account_id FROM team_accounts WHERE team_id=?)',(team_id,))
        db.execute('DELETE FROM sessions WHERE token_hash IN (SELECT token_hash FROM admin_team_sessions WHERE team_id=?)',(team_id,))
        db.execute('UPDATE accounts SET active=0 WHERE id IN (SELECT account_id FROM team_accounts WHERE team_id=?)',(team_id,))
        db.execute('UPDATE team_api SET active=0 WHERE team_id=?',(team_id,))
        db.execute('UPDATE api_keys SET active=0 WHERE team_id=?',(team_id,))
        db.execute('UPDATE team_registrations SET secret_hash=?,revision=revision+1 WHERE team_id=?',(code_hash(secrets.token_urlsafe(32)),team_id))
        db.execute('DELETE FROM claim_memberships WHERE team_id=?',(team_id,))
        self.invalidate_publications(db,team_id);self.s.touch(db)
        self.s.audit(db,actor,'team.recycle',team_id,{'eventId':event_id,'before':{'status':team['status'],'revision':team['revision']},
            'after':{'status':'withdrawn','revision':team['revision']+1},'memberCount':len(memberships)})
        return self.response(db,team_id)

    def restore(self,db,actor,body):
        team,reason,name=self.validate_mutation(db,actor,body,restore=True)
        team_id=team['id'];event=self.current_event(db,team_id)
        if not event:raise APIError(409,'该队伍不在回收站','team_not_recycled')
        conflicts=self.restore_conflicts(db,team_id,name)
        if conflicts:raise APIError(409,'无法恢复：'+ '；'.join(dict.fromkeys(c['message'] for c in conflicts)),'team_restore_conflict')
        try:
            for row in db.execute('SELECT person_id,request_id FROM team_recycle_memberships WHERE event_id=? ORDER BY person_id',(event['id'],)).fetchall():
                db.execute('INSERT INTO claim_memberships VALUES (?,?,?)',(row['person_id'],team_id,row['request_id']))
            now=self.s.admin_data.now()
            db.execute('UPDATE team_recycle_events SET restored_at=?,restored_by=?,restore_reason=?,restored_name=? WHERE id=?',
                (now,actor['id'],reason,name,event['id']))
            db.execute('UPDATE teams SET name=?,updated_at=? WHERE id=?',(name,now,team_id))
            db.execute('UPDATE team_details SET status=?,status_reason=?,revision=revision+1 WHERE team_id=?',
                (event['original_status'],event['original_status_reason'],team_id))
        except sqlite3.IntegrityError as error:
            raise APIError(409,'成员或队伍记录冲突，请核对后重试','team_restore_conflict') from error
        self.invalidate_publications(db,team_id);self.s.touch(db)
        self.s.audit(db,actor,'team.restore',team_id,{'eventId':event['id'],'name':name,
            'before':{'status':team['status'],'revision':team['revision']},
            'after':{'status':event['original_status'],'revision':team['revision']+1}})
        return self.response(db,team_id)

    def dispatch(self,db,actor,method,path,body,query,cookie,login_id):
        self.s.require(actor,'admin')
        if not login_id:raise APIError(428,'请刷新页面并核对管理员登录身份','login_required')
        if login_id!=self.s.login_id(cookie):raise APIError(401,'管理员登录身份已变化，请重新登录')
        if method=='GET' and path=='/api/admin/data/team':
            if set(query)!={'teamId'}:raise APIError(400,'请只指定队伍编号')
            return self.detail(db,query['teamId'])
        if query:raise APIError(400,'此入口不接受查询参数')
        if method=='GET' and path=='/api/admin/data/recycle-bin':return self.listing(db)
        if method=='POST' and path=='/api/admin/data/team/recycle':return self.recycle(db,actor,body)
        if method=='POST' and path=='/api/admin/data/team/restore':return self.restore(db,actor,body)
        if method=='POST' and path=='/api/admin/data/team/members':return self.s.team_members.edit(db,actor,body)
        raise APIError(405,'队伍操作方法不正确')
