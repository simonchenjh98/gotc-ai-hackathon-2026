"""Private registration roster and in-person team credential claims.

Receipts are bearer credentials. Only their hashes and credential fingerprints
persist; organizer reads deliberately never include receipt/PIN/code material.
"""
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import unicodedata

from errors import APIError
from admin_data import TRACK_OPTIONS

SCHEMA='''
CREATE TABLE IF NOT EXISTS claim_people (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, school TEXT NOT NULL,
 normalized_name TEXT NOT NULL, normalized_school TEXT NOT NULL,
 notes TEXT NOT NULL DEFAULT '', needs_review INTEGER NOT NULL DEFAULT 0,
 revision INTEGER NOT NULL DEFAULT 1,
 UNIQUE(normalized_name,normalized_school)
);
CREATE TABLE IF NOT EXISTS claim_requests (
 id TEXT PRIMARY KEY, secret_hash TEXT NOT NULL, pin_hash TEXT NOT NULL,
 name TEXT NOT NULL, school TEXT NOT NULL, normalized_name TEXT NOT NULL,
 normalized_school TEXT NOT NULL, created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending', revision INTEGER NOT NULL DEFAULT 1,
 pin_attempts INTEGER NOT NULL DEFAULT 0, team_id TEXT UNIQUE REFERENCES teams(id),
 captain_id TEXT REFERENCES claim_people(id), account_id TEXT REFERENCES accounts(id),
 account_fingerprint TEXT, approval_digest TEXT, review_note TEXT NOT NULL DEFAULT '',
 issued_at INTEGER
);
CREATE INDEX IF NOT EXISTS claim_requests_pending ON claim_requests(status,expires_at);
CREATE TABLE IF NOT EXISTS claim_declarations (
 request_id TEXT NOT NULL REFERENCES claim_requests(id), request_revision INTEGER NOT NULL,
 declaration_json TEXT NOT NULL, PRIMARY KEY(request_id,request_revision)
);
CREATE TABLE IF NOT EXISTS claim_memberships (
 person_id TEXT PRIMARY KEY REFERENCES claim_people(id),
 team_id TEXT NOT NULL REFERENCES teams(id), request_id TEXT NOT NULL REFERENCES claim_requests(id)
);
'''
INVALID={'status':'invalid','team':None,'code':None,'expiresAt':None}


def normalize(value):
    return ' '.join(unicodedata.normalize('NFKC',value).casefold().split())


def digest(domain,value):
    return hashlib.sha256((domain+'\0'+value).encode()).hexdigest()


def fingerprint(account):
    return hashlib.sha256(account['salt']+account['code_hash']+str(account['active']).encode()).hexdigest()


class Claims:
    def __init__(self,service,clock,text,kdf):
        self.s=service
        self.clock=clock
        self.text=text
        self.kdf=kdf

    def initialize(self,db):
        db.executescript(SCHEMA)

    def person(self,value):
        if not isinstance(value,dict) or set(value)!={'id','name','school','notes','needsReview'}:
            raise APIError(400,'名单字段不正确')
        result={key:self.text(value[key],key,limit,True) for key,limit in [('id',100),('name',100),('school',200)]}
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',result['id']):raise APIError(400,'名单编号格式不正确')
        result['notes']=self.text(value['notes'],'备注',2000)
        if type(value['needsReview']) is not bool:raise APIError(400,'需核实标记不正确')
        result['needsReview']=value['needsReview']
        return result

    def person_values(self,p):
        return (p['id'],p['name'],p['school'],normalize(p['name']),normalize(p['school']),p['notes'],int(p['needsReview']))

    def roster(self,db):
        return [dict(row) for row in db.execute('''SELECT p.id,p.name,p.school,p.notes,p.needs_review AS needsReview,
            p.revision,m.team_id AS teamId FROM claim_people p LEFT JOIN claim_memberships m ON m.person_id=p.id ORDER BY p.id''')]

    def import_roster(self,db,actor,body):
        if set(body)!={'people'} or not isinstance(body['people'],list) or not 1<=len(body['people'])<=500:
            raise APIError(400,'一次导入 1–500 名参赛者')
        people=[self.person(p) for p in body['people']]
        ids=[p['id'] for p in people];identities=[(normalize(p['name']),normalize(p['school'])) for p in people]
        if len(set(ids))!=len(ids) or len(set(identities))!=len(identities):raise APIError(409,'名单有重复编号或同名同校记录，请先人工核实')
        current={p['id']:p for p in self.roster(db)}
        if len(set(current)|set(ids))>500:raise APIError(409,'名单最多 500 人')
        added=0
        try:
            for p in people:
                old=current.get(p['id'])
                if old:
                    if any(old[k]!=p[k] for k in p):raise APIError(409,'已有名单不能通过导入覆盖，请使用逐人修订')
                    continue
                db.execute('INSERT INTO claim_people(id,name,school,normalized_name,normalized_school,notes,needs_review) VALUES (?,?,?,?,?,?,?)',self.person_values(p));added+=1
        except sqlite3.IntegrityError:raise APIError(409,'同名同校记录已存在，请人工核实')
        self.s.audit(db,actor,'claims.roster.import',metadata={'added':added,'received':len(people)})
        return {'ok':True,'added':added,'total':len(current)+added}

    def edit_person(self,db,actor,body):
        if set(body)!={'person','expectedRevision'} or type(body['expectedRevision']) is not int:raise APIError(400,'需要名单修订号')
        p=self.person(body['person']);old=db.execute('SELECT * FROM claim_people WHERE id=?',(p['id'],)).fetchone()
        if body['expectedRevision']!=(old['revision'] if old else 0):raise APIError(409,'名单已变化，请重新读取后核对')
        if not old and db.execute('SELECT COUNT(*) FROM claim_people').fetchone()[0]>=500:raise APIError(409,'名单最多 500 人')
        if old and db.execute('SELECT 1 FROM claim_memberships WHERE person_id=?',(p['id'],)).fetchone() and (p['name']!=old['name'] or p['school']!=old['school']):
            raise APIError(409,'已归队人员的身份不可改写，请联系组织方处理归队记录')
        try:
            if old:
                db.execute('UPDATE claim_people SET name=?,school=?,normalized_name=?,normalized_school=?,notes=?,needs_review=?,revision=revision+1 WHERE id=?',self.person_values(p)[1:]+(p['id'],))
            else:db.execute('INSERT INTO claim_people(id,name,school,normalized_name,normalized_school,notes,needs_review) VALUES (?,?,?,?,?,?,?)',self.person_values(p))
        except sqlite3.IntegrityError:raise APIError(409,'同名同校记录已存在，请人工核实')
        self.s.audit(db,actor,'claims.roster.edit',p['id'],{'before':dict(old) if old else None,'after':p})
        return {'ok':True,'revision':old['revision']+1 if old else 1}

    def request(self,db,body):
        if not {'name','school'}<=set(body) or not set(body)<={'name','school','members','teamName','track'}:raise APIError(400,'申请字段不正确')
        name=self.text(body['name'],'姓名',100,True);school=self.text(body['school'],'学校',200,True)
        declaration=self.validate_declaration(name,school,body['members']) if 'members' in body else None
        metadata=self.team_metadata(body)
        if metadata and not declaration:raise APIError(400,'请填写队长及全体组员')
        if declaration:declaration.update(metadata)
        now=self.clock()
        db.execute("UPDATE claim_requests SET status='expired',revision=revision+1 WHERE status='pending' AND expires_at<=?",(now,))
        if db.execute("SELECT COUNT(*) FROM claim_requests WHERE status='pending'").fetchone()[0]>=1000:raise APIError(429,'申请较多，请稍后重试')
        request_id=secrets.token_hex(16);secret=secrets.token_urlsafe(32);pin=f'{secrets.randbelow(1000000):06d}';expires=now+24*3600*1000
        db.execute('''INSERT INTO claim_requests(id,secret_hash,pin_hash,name,school,normalized_name,normalized_school,created_at,expires_at)
            VALUES (?,?,?,?,?,?,?,?,?)''',(request_id,digest('receipt',secret),digest('pin:'+request_id,pin),name,school,normalize(name),normalize(school),now,expires))
        if declaration:self.save_declaration(db,request_id,1,declaration)
        return {'requestId':request_id,'secret':secret,'pin':pin,'expiresAt':expires,'status':'pending',
                'requestRevision':1,'captain':{'name':name,'school':school},'declaration':declaration}

    def team_metadata(self,body):
        if not ({'teamName','track'} & set(body)):return {}
        name=self.text(body.get('teamName'),'队名',80,True)
        track=self.text(body.get('track',''),'赛道',80)
        if track and track not in TRACK_OPTIONS:raise APIError(400,'请选择已公布的赛道')
        return {'teamName':name,'track':track}

    def validate_declaration(self,name,school,members):
        if not isinstance(members,list) or not 2<=len(members)<=4:raise APIError(400,'每队须含队长及 2–4 名组员，共 3–5 人')
        result=[]
        for member in members:
            if not isinstance(member,dict) or set(member)!={'name','school'}:raise APIError(400,'组员须填写姓名和学校')
            result.append({key:self.text(member[key],label,limit,True) for key,label,limit in [('name','组员姓名',100),('school','组员学校',200)]})
        captain={'name':name,'school':school}
        identities=[(normalize(p['name']),normalize(p['school'])) for p in [captain]+result]
        if len(set(identities))!=len(identities):raise APIError(400,'队长和组员中有重复的同名同校记录，请核对')
        return {'captain':captain,'members':result}

    def declaration(self,db,request_id):
        row=db.execute('SELECT declaration_json FROM claim_declarations WHERE request_id=? ORDER BY request_revision DESC LIMIT 1',(request_id,)).fetchone()
        return json.loads(row['declaration_json']) if row else None

    def save_declaration(self,db,request_id,revision,declaration):
        # Store a new snapshot. Pending edits prune the superseded snapshot in the same transaction;
        # after approval no public edit can replace the frozen declaration.
        db.execute('INSERT INTO claim_declarations VALUES (?,?,?)',(request_id,revision,json.dumps(declaration,ensure_ascii=False)))
        db.execute('DELETE FROM claim_declarations WHERE request_id=? AND request_revision<?',(request_id,revision))

    def receipt_row(self,db,body):
        if not isinstance(body.get('requestId'),str) or not isinstance(body.get('secret'),str):return None
        if not re.fullmatch(r'[a-f0-9]{32}',body['requestId']) or not re.fullmatch(r'[A-Za-z0-9_-]{43}',body['secret']):return None
        row=db.execute('SELECT * FROM claim_requests WHERE id=?',(body['requestId'],)).fetchone()
        if not row or not hmac.compare_digest(row['secret_hash'],digest('receipt',body['secret'])) or row['expires_at']<=self.clock():return None
        return row

    def update(self,db,body):
        if not {'requestId','secret','expectedRevision','members'}<=set(body) or not set(body)<={'requestId','secret','expectedRevision','members','teamName','track'}:raise APIError(400,'修改申请字段不正确')
        row=self.receipt_row(db,body)
        if not row:return dict(INVALID)
        if type(body['expectedRevision']) is not int:raise APIError(400,'需要申请修订号')
        if row['revision']!=body['expectedRevision']:raise APIError(409,'申请已变化，请查询最新状态后重新核对')
        if row['status']!='pending':raise APIError(409,'申请已处理，不可修改组员')
        declaration=self.validate_declaration(row['name'],row['school'],body['members'])
        old=self.declaration(db,row['id']) or {}
        declaration.update({key:old[key] for key in ('teamName','track') if key in old})
        declaration.update(self.team_metadata(body))
        self.save_declaration(db,row['id'],row['revision']+1,declaration)
        db.execute('UPDATE claim_requests SET revision=revision+1 WHERE id=?',(row['id'],))
        self.s.audit(db,None,'claims.update',row['id'],{'requestRevision':row['revision']+1})
        return self.bearer(db,{'requestId':body['requestId'],'secret':body['secret']})

    def bearer(self,db,body,redeem=False):
        if set(body)!={'requestId','secret'}:return dict(INVALID)
        row=self.receipt_row(db,body)
        if not row:return dict(INVALID)
        result={'status':row['status'],'team':None,'code':None,'expiresAt':row['expires_at'],'requestRevision':row['revision'],
                'captain':{'name':row['name'],'school':row['school']},'declaration':self.declaration(db,row['id'])}
        if row['status'] not in {'approved','issued'}:return result
        account=db.execute('SELECT * FROM accounts WHERE id=?',(row['account_id'],)).fetchone()
        if not account or not account['active'] or not hmac.compare_digest(row['account_fingerprint'],fingerprint(account)):
            result['status']='unavailable';return result
        team=db.execute('SELECT id,number,name,track FROM teams WHERE id=?',(row['team_id'],)).fetchone();result['team']=dict(team)
        if not redeem:return result
        code=account['id']+'.'+digest('gotc-claim-code-v1',body['secret'])
        if row['status']=='approved':
            salt=secrets.token_bytes(16);hashed=self.kdf(code,salt)
            db.execute('UPDATE accounts SET salt=?,code_hash=? WHERE id=?',(salt,hashed,account['id']))
            updated=db.execute('SELECT * FROM accounts WHERE id=?',(account['id'],)).fetchone()
            db.execute("UPDATE claim_requests SET status='issued',issued_at=?,account_fingerprint=?,revision=revision+1 WHERE id=?",(self.clock(),fingerprint(updated),row['id']))
            self.s.audit(db,None,'claims.redeem',row['id'])
        elif not hmac.compare_digest(account['code_hash'],self.kdf(code,account['salt'])):
            result['status']='unavailable';result['team']=None;return result
        result.update(status='issued',code=code)
        if row['status']=='approved':result['requestRevision']=row['revision']+1
        return result

    def listing(self,db):
        requests=[dict(row) for row in db.execute('''SELECT id,name,school,status,revision,created_at AS createdAt,expires_at AS expiresAt,
            team_id AS teamId,captain_id AS captainId,review_note AS reviewNote,pin_attempts AS pinAttempts FROM claim_requests ORDER BY created_at DESC''')]
        for row in requests:
            row['declaration']=self.declaration(db,row['id'])
            row['reviewContext'],row['references']=self.review_context(db,row['id'],row['revision'],row['declaration'])
            if row['status']=='pending' and row['expiresAt']<=self.clock():row['status']='expired'
        teams=[dict(row) for row in db.execute('''SELECT t.id,t.number,t.name FROM teams t LEFT JOIN team_details d ON d.team_id=t.id
            WHERE COALESCE(d.status,'active')='active' AND NOT EXISTS(SELECT 1 FROM team_accounts a WHERE a.team_id=t.id)
            AND NOT EXISTS(SELECT 1 FROM claim_requests r WHERE r.team_id=t.id) ORDER BY t.number''')]
        return {'requests':requests,'people':self.roster(db),'teams':teams,'trackOptions':TRACK_OPTIONS}

    def review_context(self,db,request_id,revision,declaration):
        names=sorted({normalize(p['name']) for p in [declaration['captain']]+declaration['members']}) if declaration else []
        references=[]
        if names:
            references=[dict(p) for p in db.execute("""SELECT p.id,p.name,p.school,p.normalized_name,p.normalized_school,
                p.revision,p.needs_review AS needsReview,p.notes,m.team_id AS teamId
                FROM claim_people p LEFT JOIN claim_memberships m ON m.person_id=p.id
                WHERE p.normalized_name IN ("""+','.join('?' for _ in names)+') ORDER BY p.id',names)]
        context=digest('open-review-context-v1',json.dumps([request_id,revision,declaration,references],ensure_ascii=False,sort_keys=True))
        return context,references

    def verify_pin(self,db,actor,row,pin):
        if row['pin_attempts']>=5:raise APIError(429,'核验码尝试已达上限，请让申请人重新申请')
        if not hmac.compare_digest(row['pin_hash'],digest('pin:'+row['id'],pin)):
            db.execute('UPDATE claim_requests SET pin_attempts=pin_attempts+1 WHERE id=?',(row['id'],))
            self.s.audit(db,actor,'claims.pin-failed',row['id'])
            db.commit()  # Preserve failed attempts; called before any approval effects.
            raise APIError(400,'核验码不正确，请核对申请人的设备')

    def approve_open(self,db,actor,body):
        fields={'requestId','expectedRevision','reviewContext','teamName','track','pin','inPersonVerified','resolvedIdentity','note'}
        if set(body)!=fields:raise APIError(400,'开放申请审核字段不正确')
        if not isinstance(body['requestId'],str) or type(body['expectedRevision']) is not int or not isinstance(body['reviewContext'],str) or not re.fullmatch(r'[a-f0-9]{64}',body['reviewContext']):raise APIError(400,'需要申请修订号及核验上下文')
        if body['inPersonVerified'] is not True or type(body['resolvedIdentity']) is not bool:raise APIError(400,'请当面核实本人和全部组员')
        if not isinstance(body['pin'],str) or not re.fullmatch(r'\d{6}',body['pin']):raise APIError(400,'请输入申请人设备上的六位核验码')
        metadata=self.team_metadata(body);note=self.text(body['note'],'审核备注',2000)
        row=db.execute('SELECT * FROM claim_requests WHERE id=?',(body['requestId'],)).fetchone()
        if not row:raise APIError(404,'申请不存在')
        canonical={**body,**metadata,'note':note,'pin':digest('pin:'+row['id'],body['pin'])}
        approval=digest('open-approval-v1',json.dumps(canonical,ensure_ascii=False,sort_keys=True))
        # Compare applied intent before context, which necessarily changes after membership assignment.
        if row['status'] in {'approved','issued'} and row['approval_digest']==approval:
            return {'ok':True,'status':row['status'],'revision':row['revision']}
        if row['revision']!=body['expectedRevision'] or row['status']!='pending' or row['expires_at']<=self.clock():raise APIError(409,'申请已变化、处理或过期，请重新读取后核对')
        declaration=self.declaration(db,row['id'])
        if not declaration:raise APIError(409,'请申请人先补充全体组员，再进行审核')
        self.validate_declaration(row['name'],row['school'],declaration['members'])
        context,references=self.review_context(db,row['id'],row['revision'],declaration)
        if not hmac.compare_digest(context,body['reviewContext']):raise APIError(409,'相关身份或申请已变化，请保留核实备注并刷新，重新核对后批准')
        self.verify_pin(db,actor,row,body['pin'])
        people=[declaration['captain']]+declaration['members'];matches=[]
        for person in people:
            identity=(normalize(person['name']),normalize(person['school']))
            exact=next((p for p in references if (p['normalized_name'],p['normalized_school'])==identity),None)
            if exact and exact['teamId']:raise APIError(409,'申报成员已有队伍，请核对本人和组队关系')
            possible=[p for p in references if p['normalized_name']==identity[0] and p['normalized_school']!=identity[1] and p['teamId']]
            if possible and (not body['resolvedIdentity'] or not note):raise APIError(400,'存在其他学校的同名已归队人员，请明确区分本人并填写核实说明')
            matches.append(exact)
        used={r[0] for r in db.execute('SELECT number FROM teams')}
        number=next((n for n in range(1,1000) if n not in used),None)
        if number is None:raise APIError(409,'队号 1–999 已用完，请联系组织方')
        try:
            team=self.s.admin_data.save_team(db,actor,{'number':number,'name':metadata['teamName'],'track':metadata['track'],'status':'active'})
            member_ids=[]
            for person,exact in zip(people,matches):
                person_id=exact['id'] if exact else 'P'+secrets.token_hex(16)
                if not exact:
                    db.execute('INSERT INTO claim_people(id,name,school,normalized_name,normalized_school) VALUES (?,?,?,?,?)',
                        (person_id,person['name'],person['school'],normalize(person['name']),normalize(person['school'])))
                db.execute('INSERT INTO claim_memberships VALUES (?,?,?)',(person_id,team['id'],row['id']))
                member_ids.append(person_id)
            access=self.s.team_access(db,actor,{'teamId':team['id'],'action':'issue'})
            account=db.execute('SELECT * FROM accounts WHERE id=?',(access['id'],)).fetchone()
            reviewed={**declaration,**metadata}
            self.save_declaration(db,row['id'],row['revision']+1,reviewed)
            db.execute("""UPDATE claim_requests SET status='approved',revision=revision+1,team_id=?,captain_id=?,approval_digest=?,review_note=?,
                account_id=?,account_fingerprint=? WHERE id=?""",(team['id'],member_ids[0],approval,note,account['id'],fingerprint(account),row['id']))
        except sqlite3.IntegrityError:raise APIError(409,'成员或队伍已经分配，请重新读取并核对')
        self.s.audit(db,actor,'claims.approve-open',row['id'],{'teamId':team['id'],'memberIds':member_ids,'declaration':reviewed,
            'requestRevision':body['expectedRevision'],'reviewContext':body['reviewContext'],'resolvedIdentity':body['resolvedIdentity'],'note':note})
        return {'ok':True,'status':'approved','revision':row['revision']+1}

    def review(self,db,actor,body):
        common={'requestId','expectedRevision','action','note'}
        approve={'teamId','captainId','memberIds','memberRevisions','pin','inPersonVerified','resolvedIdentity'}
        allowed=(common|approve,common|approve|{'declaredMemberIds'}) if body.get('action')=='approve' else (common,)
        if not isinstance(body.get('action'),str) or body['action'] not in {'approve','reject'} or set(body) not in allowed:raise APIError(400,'审核字段不正确')
        if type(body['expectedRevision']) is not int or not isinstance(body['requestId'],str):raise APIError(400,'需要申请修订号')
        note=self.text(body['note'],'审核备注',2000)
        row=db.execute('SELECT * FROM claim_requests WHERE id=?',(body['requestId'],)).fetchone()
        if not row:raise APIError(404,'申请不存在')
        declaration=self.declaration(db,row['id'])
        approval=None
        if body['action']=='approve':
            members=body['memberIds']
            if not isinstance(members,list) or not 1<=len(members)<=500 or any(not isinstance(p,str) or len(p)>100 for p in members) or len(set(members))!=len(members):raise APIError(400,'成员名单不正确')
            member_revisions=body['memberRevisions']
            if not isinstance(member_revisions,dict) or set(member_revisions)!=set(members) or any(type(revision) is not int or not 1<=revision<=9007199254740991 for revision in member_revisions.values()):
                raise APIError(400,'请提交每位所选成员的准确名单修订号')
            if not isinstance(body['teamId'],str) or not isinstance(body['captainId'],str) or body['captainId'] not in members:raise APIError(400,'队长必须在本队名单中')
            if body['inPersonVerified'] is not True or type(body['resolvedIdentity']) is not bool:raise APIError(400,'请当面核实本人和全部组员')
            if not isinstance(body['pin'],str) or not re.fullmatch(r'\d{6}',body['pin']):raise APIError(400,'请输入申请人设备上的六位核验码')
            canonical=[body['teamId'],body['captainId'],sorted(member_revisions.items()),note,body['resolvedIdentity'],digest('pin:'+row['id'],body['pin'])]
            if declaration:
                if row['status']=='pending' and row['revision']!=body['expectedRevision']:raise APIError(409,'申请已变化，请重新读取后核对')
                mapping=body.get('declaredMemberIds')
                if not isinstance(mapping,list) or len(mapping)!=len(declaration['members'])+1 or any(not isinstance(p,str) for p in mapping) or len(set(mapping))!=len(mapping) or set(mapping)!=set(members) or mapping[0]!=body['captainId']:
                    raise APIError(400,'请为每位申报成员逐一选择唯一名单记录，不能增加或遗漏人员')
                canonical+=['declared-v1',body['expectedRevision'],declaration,mapping]
            elif 'declaredMemberIds' in body:raise APIError(400,'旧申请请使用现场人工组队核实流程')
            approval=digest('approval',json.dumps(canonical,ensure_ascii=False))
            if row['status'] in {'approved','issued'} and row['approval_digest']==approval and (declaration or body['expectedRevision'] in {row['revision'],1}):
                return {'ok':True,'status':row['status'],'revision':row['revision']}
        if row['revision']!=body['expectedRevision']:raise APIError(409,'申请已变化，请重新读取后核对')
        if row['status']!='pending' or row['expires_at']<=self.clock():raise APIError(409,'申请已处理或过期')
        if body['action']=='reject':
            db.execute("UPDATE claim_requests SET status='rejected',revision=revision+1,review_note=? WHERE id=?",(note,row['id']))
            self.s.audit(db,actor,'claims.reject',row['id'],{'note':note})
            return {'ok':True,'status':'rejected','revision':row['revision']+1}
        people={p['id']:p for p in self.roster(db)}
        if any(p not in people for p in members):raise APIError(400,'成员必须来自已核实的名单')
        if any(people[p]['revision']!=member_revisions[p] for p in members):
            raise APIError(409,'所选成员的名单已变化，请保留核实备注并刷新名单，重新核对本人后批准')
        self.verify_pin(db,actor,row,body['pin'])
        captain=people.get(body['captainId'])
        if not captain or normalize(captain['name'])!=row['normalized_name']:raise APIError(409,'队长姓名与申请不一致，请先核实名单或重新申请')
        school_difference=normalize(captain['school'])!=row['normalized_school']
        if declaration:
            for declared,person_id in zip([declaration['captain']]+declaration['members'],mapping):
                if normalize(declared['name'])!=normalize(people[person_id]['name']):raise APIError(409,'申报姓名与所选名单不一致，请逐人核对')
                school_difference|=normalize(declared['school'])!=normalize(people[person_id]['school'])
        if (school_difference or any(people[p]['needsReview'] for p in members)) and (not body['resolvedIdentity'] or not note):raise APIError(400,'学校差异或待核实条目需明确确认并填写核实说明')
        team=db.execute("SELECT t.id FROM teams t LEFT JOIN team_details d ON d.team_id=t.id WHERE t.id=? AND COALESCE(d.status,'active')='active'",(body['teamId'],)).fetchone()
        if not team:raise APIError(409,'请选择现有有效队伍')
        try:
            # UNIQUE constraints are the final authority; the outer transaction rolls everything back.
            for person in members:db.execute('INSERT INTO claim_memberships VALUES (?,?,?)',(person,body['teamId'],row['id']))
            db.execute("UPDATE claim_requests SET status='approved',revision=revision+1,team_id=?,captain_id=?,approval_digest=?,review_note=? WHERE id=?",(body['teamId'],body['captainId'],approval,note,row['id']))
            access=self.s.team_access(db,actor,{'teamId':body['teamId'],'action':'issue'})
            account=db.execute('SELECT * FROM accounts WHERE id=?',(access['id'],)).fetchone()
            db.execute('UPDATE claim_requests SET account_id=?,account_fingerprint=? WHERE id=?',(account['id'],fingerprint(account),row['id']))
        except sqlite3.IntegrityError:raise APIError(409,'成员或队伍已经分配，请重新读取并核对')
        metadata={'teamId':body['teamId'],'captainId':body['captainId'],'memberIds':sorted(members),'memberRevisions':member_revisions,'note':note,'resolvedIdentity':body['resolvedIdentity']}
        if declaration:metadata.update(declaration=declaration,declaredMemberIds=mapping,requestRevision=body['expectedRevision'])
        self.s.audit(db,actor,'claims.approve',row['id'],metadata)
        return {'ok':True,'status':'approved','revision':row['revision']+1}

    def dispatch(self,method,path,body,cookie,ip,login_id,query):
        admin=path.startswith('/api/admin/claims')
        if query:raise APIError(400,'此接口不接受查询参数')
        public_scopes={'/api/claims/request':'claims-request','/api/claims/status':'claims-status','/api/claims/redeem':'claims-redeem','/api/claims/update':'claims-update'}
        if not admin:
            if method!='POST' or path not in public_scopes:raise APIError(404,'接口不存在')
            self.s.rate_limit(ip,public_scopes[path])
        with self.s.db() as db:
            db.execute('BEGIN IMMEDIATE' if method=='POST' else 'BEGIN')
            if admin:
                actor=self.s.account(db,cookie);self.s.require(actor,'admin')
                if not login_id:raise APIError(428,'请刷新页面并核对管理身份')
                if login_id!=self.s.login_id(cookie):raise APIError(401,'登录身份已变化，请重新登录')
                if method=='GET' and path=='/api/admin/claims':return self.listing(db)
                if method=='POST' and path=='/api/admin/claims/roster':return self.import_roster(db,actor,body)
                if method=='POST' and path=='/api/admin/claims/person':return self.edit_person(db,actor,body)
                if method=='POST' and path=='/api/admin/claims/approve':return self.approve_open(db,actor,body)
                if method=='POST' and path=='/api/admin/claims/review':return self.review(db,actor,body)
            elif method=='POST':
                if path=='/api/claims/request':return self.request(db,body)
                if path=='/api/claims/update':return self.update(db,body)
                if path in {'/api/claims/status','/api/claims/redeem'}:return self.bearer(db,body,path.endswith('/redeem'))
            raise APIError(404,'接口不存在')
