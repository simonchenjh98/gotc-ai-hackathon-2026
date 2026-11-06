"""Private event data tools; no scoring or advancement calculations."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import secrets
import sqlite3
from pathlib import Path

from errors import APIError
from judging import STAGES

TRACK_OPTIONS=["AI for Science","城市与社区问题","未来学习样态"]
TEAM_FIELDS=("number","name","project","track","status","statusReason","booth","roadshowOrder")
SCHEMA="""
CREATE TABLE IF NOT EXISTS team_details (
 team_id TEXT PRIMARY KEY REFERENCES teams(id), status TEXT NOT NULL DEFAULT 'active',
 status_reason TEXT NOT NULL DEFAULT '', booth TEXT NOT NULL DEFAULT '', roadshow_order INTEGER,
 revision INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS credential_overrides (
 account_id TEXT PRIMARY KEY REFERENCES accounts(id), rotated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS import_previews (
 id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id), session_hash TEXT NOT NULL,
 kind TEXT NOT NULL, plan_json TEXT NOT NULL, created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
 consumed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS admin_data_meta (key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS judging_review_validity (
 run_id TEXT NOT NULL, judge_id TEXT NOT NULL, team_id TEXT NOT NULL,
 invalidated INTEGER NOT NULL DEFAULT 0, reason TEXT NOT NULL, updated_at INTEGER NOT NULL,
 PRIMARY KEY(run_id,judge_id,team_id),
 FOREIGN KEY(run_id,judge_id,team_id) REFERENCES judging_reviews(run_id,judge_id,team_id)
);
CREATE TABLE IF NOT EXISTS judging_review_history (
 id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, judge_id TEXT NOT NULL, team_id TEXT NOT NULL,
 version INTEGER NOT NULL, at INTEGER NOT NULL, actor_id TEXT, action TEXT NOT NULL,
 before_json TEXT, after_json TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
 UNIQUE(run_id,judge_id,team_id,version)
);
"""


class AdminDataService:
    def __init__(self, service):
        self.s=service

    def now(self):
        return self.s.judging.clock()

    def text(self, value, name, limit, required=False):
        return self.s.judging.text(value,name,limit,required)

    def initialize(self, db):
        db.executescript(SCHEMA)
        db.execute("INSERT OR IGNORE INTO admin_data_meta VALUES ('historyAvailableFrom',?)",(str(self.now()),))
        db.execute("INSERT OR IGNORE INTO team_details(team_id) SELECT id FROM teams")
        rows=db.execute("""SELECT r.* FROM judging_reviews r WHERE NOT EXISTS
          (SELECT 1 FROM judging_review_history h WHERE h.run_id=r.run_id AND h.judge_id=r.judge_id AND h.team_id=r.team_id)""").fetchall()
        for row in rows:
            after=self.record(db,row['run_id'],row['judge_id'],row['team_id'])
            self.record_review_change(db,None,row['run_id'],row['judge_id'],row['team_id'],'baseline',None,after)

    def backup_before_upgrade(self):
        if not self.s.db_path.exists():return
        source=sqlite3.connect(self.s.db_path)
        try:
            if source.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='admin_data_meta'").fetchone() \
                    or not source.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='accounts'").fetchone():return
            path=Path(self.s.data_dir)/('before-admin-data-'+str(self.now())+'-'+secrets.token_hex(4)+'.sqlite3')
            fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600);os.close(fd)
            target=sqlite3.connect(path)
            try:source.backup(target)
            finally:target.close()
        finally:source.close()

    def enrich_team(self, db, team):
        result=dict(team)
        details=db.execute("SELECT d.*,t.updated_at AS team_updated_at FROM team_details d JOIN teams t ON t.id=d.team_id WHERE d.team_id=?",(team['id'],)).fetchone()
        result.update(status=details['status'] if details else 'active',statusReason=details['status_reason'] if details else '',
                      booth=details['booth'] if details else '',roadshowOrder=details['roadshow_order'] if details else None,
                      revision=details['revision'] if details else 0,trackKnown=team.get('track','') in TRACK_OPTIONS,
                      updatedAt=details['team_updated_at'] if details else None)
        return result

    def team(self, db, team_id):
        row=db.execute("SELECT id,number,name,project,track FROM teams WHERE id=?",(team_id,)).fetchone()
        return self.enrich_team(db,dict(row)) if row else None

    def normalize_team(self, values, before=None):
        result={key:(before.get(key) if before else {'project':'','track':'','status':'active','statusReason':'','booth':'','roadshowOrder':None}.get(key)) for key in TEAM_FIELDS}
        result.update({key:value for key,value in values.items() if key in TEAM_FIELDS})
        if type(result['number']) is not int or not 1<=result['number']<=999:
            raise APIError(400,'队号应为1至999的整数')
        for field,label,limit,required in [('name','队名',80,True),('project','项目名',140,False),('track','赛道',80,False),
                                           ('statusReason','状态原因',500,False),('booth','展示展位',80,False)]:
            result[field]=self.text(result[field],label,limit,required)
        if result['track'] and result['track'] not in TRACK_OPTIONS and (not before or result['track']!=before['track']):
            raise APIError(400,'请选择已公布的赛道；旧赛道可保留原值，不能自动映射')
        if not isinstance(result['status'],str) or result['status'] not in {'active','withdrawn','disqualified'}:
            raise APIError(400,'队伍状态不正确')
        if result['status']!='active' and not result['statusReason']:
            raise APIError(400,'退出或取消资格须填写原因')
        order=result['roadshowOrder']
        if order is not None and (type(order) is not int or not 1<=order<=9999):
            raise APIError(400,'路演顺序应为1至9999的整数或空值')
        return result

    def save_team(self, db, actor, values, batch=None, *, require_revision=False):
        allowed=set(TEAM_FIELDS)|{'id','expectedRevision'}
        if not set(values).issubset(allowed):
            raise APIError(400,'队伍字段不正确')
        team_id=values.get('id')
        before=self.team(db,team_id) if isinstance(team_id,str) else None
        if team_id is not None and before is None:
            raise APIError(404,'队伍不存在')
        revision=values.get('expectedRevision')
        # Direct administrator HTTP edits must carry the displayed team revision.
        # Internal import commits verify a whole-row fingerprint in this same write
        # transaction; registration edits validate their own registration revision.
        if before and require_revision and revision is None:
            raise APIError(428,'请先读取队伍最新信息，再携带编辑版本保存','revision_required')
        if revision is not None and (type(revision) is not int or revision<0):
            raise APIError(400,'队伍版本必须是非负整数')
        if before and revision is not None and revision!=before['revision']:
            raise APIError(409,'队伍信息已更新，请保留输入并重新读取')
        after=self.normalize_team(values,before)
        duplicate=db.execute('SELECT id FROM teams WHERE number=?',(after['number'],)).fetchone()
        if duplicate and duplicate['id']!=team_id:
            raise APIError(409,'队号已存在')
        if before:
            db.execute('UPDATE teams SET number=?,name=?,project=?,track=?,updated_at=? WHERE id=?',
                       (*[after[key] for key in ('number','name','project','track')],self.now(),team_id))
        else:
            team_id='T'+secrets.token_hex(8)
            db.execute('INSERT INTO teams VALUES (?,?,?,?,?,?)',(team_id,*[after[key] for key in ('number','name','project','track')],self.now()))
        db.execute("""INSERT INTO team_details VALUES (?,?,?,?,?,?) ON CONFLICT(team_id) DO UPDATE SET
          status=excluded.status,status_reason=excluded.status_reason,booth=excluded.booth,
          roadshow_order=excluded.roadshow_order,revision=excluded.revision""",
                   (team_id,after['status'],after['statusReason'],after['booth'],after['roadshowOrder'],(before['revision']+1) if before else 1))
        saved=self.team(db,team_id)
        self.s.audit(db,actor,'team.save',team_id,{'before':before,'after':saved,**({'batch':batch} if batch else {})})
        self.s.touch(db)
        return saved

    def credential_mode(self, db, account_id, role):
        return 'individual' if role not in self.s.shared_login_codes or db.execute('SELECT 1 FROM credential_overrides WHERE account_id=?',(account_id,)).fetchone() else 'shared'

    def fingerprint(self, db, kind, key):
        if kind=='teams':
            target=db.execute('SELECT id FROM teams WHERE number=?',(int(key),)).fetchone()
            value=self.team(db,target['id']) if target else None
        else:
            target=db.execute("SELECT * FROM accounts WHERE id=? AND role='judge'",(key,)).fetchone()
            value={key:(value.hex() if isinstance(value,bytes) else value) for key,value in dict(target).items()} if target else None
        return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True).encode()).hexdigest()

    def preview_import(self, db, actor, body, session_hash):
        if set(body)!={'kind','csv'} or not isinstance(body['kind'],str) or body['kind'] not in {'teams','judges'} or not isinstance(body['csv'],str):
            raise APIError(400,'导入应包含kind和CSV文字')
        content=body['csv']
        if len(content.encode('utf-8'))>262144:
            raise APIError(413,'CSV不能超过256KiB')
        kind=body['kind']
        allowed=set(TEAM_FIELDS) if kind=='teams' else {'id','name'}
        required={'number','name'} if kind=='teams' else {'name'}
        try:
            parsed=list(csv.reader(io.StringIO(content.lstrip('\ufeff'),newline=''),strict=True))
        except csv.Error as error:
            raise APIError(400,'CSV格式不正确：'+str(error))
        if not parsed or len(parsed)>501:
            raise APIError(400,'CSV需要表头，且最多500条数据')
        headers=[header.strip() for header in parsed[0]]
        header_errors=[]
        if len(headers)!=len(set(headers)):header_errors.append('CSV表头重复')
        if not required.issubset(headers):header_errors.append('CSV表头缺少必填列')
        if not set(headers).issubset(allowed):header_errors.append('CSV表头含未知列')
        rows=[];seen=set()
        if header_errors:rows.append({'row':1,'action':'error','key':'','before':None,'after':None,'errors':header_errors})
        for line,columns in enumerate([] if header_errors else parsed[1:],2):
            row={'row':line,'action':'error','key':'','before':None,'after':None,'errors':[]}
            try:
                if len(columns)!=len(headers):
                    raise APIError(400,'列数与表头不一致')
                values={key:value.strip() for key,value in zip(headers,columns)}
                if any('\x00' in value for value in values.values()):
                    raise APIError(400,'CSV不能包含空字符')
                if kind=='teams':
                    number=values['number'];digits=number.lstrip('0') or '0'
                    if not number.isascii() or not number.isdigit() or len(digits)>3:
                        raise APIError(400,'队号必须是整数')
                    values['number']=int(digits);row['key']=str(values['number'])
                    target=db.execute('SELECT id FROM teams WHERE number=?',(values['number'],)).fetchone()
                    before=self.team(db,target['id']) if target else None
                    if values.get('status')=='':
                        values['status']=before['status'] if before else 'active'
                    if 'roadshowOrder' in values:
                        value=values['roadshowOrder']
                        digits=value.lstrip('0') or '0'
                        if value and (not value.isascii() or not value.isdigit() or len(digits)>4):
                            raise APIError(400,'路演顺序必须是整数或留空')
                        values['roadshowOrder']=int(digits) if value else None
                    after=self.normalize_team(values,before)
                    if before:
                        after['id']=before['id']
                    same=before and all(before[key]==after[key] for key in TEAM_FIELDS)
                else:
                    name=self.text(values['name'],'评委名称',80,True)
                    key=values.get('id','')
                    if key and (len(key)>80 or not db.execute("SELECT 1 FROM accounts WHERE id=? AND role='judge'",(key,)).fetchone()):
                        raise APIError(400,'评委ID不存在；不能凭同名更新')
                    row['key']=key or 'new:'+name
                    account=db.execute('SELECT id,name,active FROM accounts WHERE id=?',(key,)).fetchone() if key else None
                    before=dict(account) if account else None
                    if before:before['active']=bool(before['active'])
                    after={'name':name,**({'id':key} if key else {})}
                    same=before and before['name']==name
                if row['key'] in seen:
                    raise APIError(400,'同一CSV中队号、评委ID或新建评委名称不能重复')
                seen.add(row['key'])
                row.update(before=before,after=after,action='unchanged' if same else 'update' if before else 'create')
                row['_fingerprint']=self.fingerprint(db,kind,row['key'])
            except APIError as error:
                row['errors'].append(error.message)
            rows.append(row)
        counts={action:sum(row['action']==action for row in rows) for action in ('create','update','unchanged','error')}
        now=self.now();preview_id='IP'+secrets.token_hex(16)
        plan={'rows':rows,'canCommit':bool(rows) and counts['error']==0}
        db.execute('DELETE FROM import_previews WHERE expires_at<?',(now,))
        db.execute('INSERT INTO import_previews VALUES (?,?,?,?,?,?,?,0)',(preview_id,actor['id'],session_hash,kind,json.dumps(plan,ensure_ascii=False),now,now+900000))
        self.s.audit(db,actor,'import.preview',preview_id,{'kind':kind,'counts':counts})
        return {'previewId':preview_id,'kind':kind,'rows':[{key:value for key,value in row.items() if not key.startswith('_')} for row in rows],
                'counts':counts,'canCommit':plan['canCommit'],'expiresAt':now+900000}

    def commit_import(self, db, actor, body, session_hash):
        if set(body)!={'previewId'} or not isinstance(body['previewId'],str):
            raise APIError(400,'请提交预览编号')
        preview=db.execute('SELECT * FROM import_previews WHERE id=?',(body['previewId'],)).fetchone()
        if not preview or preview['account_id']!=actor['id'] or preview['session_hash']!=session_hash:
            raise APIError(403,'预览不存在或不属于当前管理员会话')
        if preview['consumed'] or preview['expires_at']<=self.now():
            raise APIError(409,'预览已使用或过期，请重新预览')
        plan=json.loads(preview['plan_json']);kind=preview['kind']
        if not plan['canCommit']:
            raise APIError(400,'预览仍有错误或没有数据，不能导入')
        for row in plan['rows']:
            if self.fingerprint(db,kind,row['key'])!=row['_fingerprint']:
                raise APIError(409,'预览后目标数据已变化，整批未导入，请重新预览')
        counts={'created':0,'updated':0,'unchanged':0};issued=[]
        for row in plan['rows']:
            action=row['action'];after=row['after']
            if action=='unchanged':
                counts['unchanged']+=1;continue
            if kind=='teams':
                self.save_team(db,actor,after,batch=preview['id'])
            elif action=='create':
                account,code=self.s.create_account(db,'judge',after['name'])
                issued.append({'id':account['id'],'name':account['name'],'code':code})
                self.s.audit(db,actor,'invitation.create',account['id'],{'role':'judge','after':{'name':after['name'],'active':True},'batch':preview['id']})
            else:
                db.execute('UPDATE accounts SET name=? WHERE id=?',(after['name'],after['id']))
                self.s.audit(db,actor,'judge.update',after['id'],{'before':row['before'],'after':after,'batch':preview['id']})
            counts['created' if action=='create' else 'updated']+=1
        db.execute('UPDATE import_previews SET consumed=1 WHERE id=?',(preview['id'],))
        self.s.touch(db)
        self.s.audit(db,actor,'import.commit',preview['id'],{'kind':kind,**counts})
        return {'ok':True,'kind':kind,**counts,'issuedCredentials':issued}

    def run_view(self, db, row):
        current=db.execute('SELECT 1 FROM judging_stages WHERE run_id=?',(row['id'],)).fetchone() is not None
        return {'id':row['id'],'stage':row['stage'],'createdAt':row['created_at'],'frozenAt':row['frozen_at'],'reason':row['reason'],'current':current}

    def resolve_run(self, db, query):
        stage=query.get('stage','exhibition')
        if stage not in STAGES:
            raise APIError(400,'评审阶段不正确')
        run_id=query.get('runId')
        if not run_id:
            run_id=self.s.judging.state(db,stage)['run_id']
        row=db.execute('SELECT * FROM judging_runs WHERE id=? AND stage=?',(run_id,stage)).fetchone()
        if not row:
            raise APIError(404,'评审轮次不存在或与阶段不符')
        return row

    def record(self, db, run_id, judge_id, team_id):
        row=db.execute("""SELECT r.*,COALESCE(v.invalidated,0) AS invalidated,COALESCE(v.reason,'') AS invalidated_reason
          FROM judging_reviews r LEFT JOIN judging_review_validity v
          ON v.run_id=r.run_id AND v.judge_id=r.judge_id AND v.team_id=r.team_id
          WHERE r.run_id=? AND r.judge_id=? AND r.team_id=?""",(run_id,judge_id,team_id)).fetchone()
        if row is None:return None
        result=self.s.judging.review_view(row,include_judge=True)
        result.update(invalidated=bool(row['invalidated']),invalidatedReason=row['invalidated_reason'])
        result['status']=self.review_status(result)
        return result

    def review_status(self, record):
        return 'invalidated' if record.get('invalidated') else 'recused' if record.get('recused') else 'submitted' if record.get('submitted') else 'draft' if record.get('draft') is not None else 'missing'

    def record_review_change(self, db, actor, run_id, judge_id, team_id, action, before, after, reason=''):
        # Feedback belongs only to this private history, never to the generic audit log.
        if after is None:return
        if 'invalidated' not in after or action in {'review.draft','review.submitted','review.recused'}:
            validity=db.execute('SELECT invalidated,reason FROM judging_review_validity WHERE run_id=? AND judge_id=? AND team_id=?',
                                (run_id,judge_id,team_id)).fetchone()
            for record in (before,after):
                if record is not None:
                    record.update(invalidated=bool(validity['invalidated']) if validity else False,invalidatedReason=validity['reason'] if validity else '')
        db.execute('INSERT INTO judging_review_history(run_id,judge_id,team_id,version,at,actor_id,action,before_json,after_json,reason) VALUES (?,?,?,?,?,?,?,?,?,?)',
                   (run_id,judge_id,team_id,after['version'],self.now(),actor['id'] if actor else None,action,
                    json.dumps(before,ensure_ascii=False) if before is not None else None,json.dumps(after,ensure_ascii=False),reason))

    def review_data(self, db, query):
        run=self.resolve_run(db,query);run_id=run['id']
        teams=self.s.judging.teams(db,run_id)
        judges=[dict(row) for row in db.execute("""SELECT a.id,a.name,a.active,a.created_at AS createdAt FROM accounts a JOIN judging_grants g ON g.judge_id=a.id
          WHERE g.run_id=? ORDER BY a.created_at,a.id""",(run_id,))]
        for judge in judges:judge['active']=bool(judge['active'])
        if query.get('teamId'):teams=[team for team in teams if team['id']==query['teamId']]
        if query.get('track'):teams=[team for team in teams if team['track']==query['track']]
        if query.get('judgeId'):judges=[judge for judge in judges if judge['id']==query['judgeId']]
        status=query.get('status','all')
        if status not in {'all','draft','submitted','recused','invalidated','missing'}:
            raise APIError(400,'记录状态筛选不正确')
        if len(teams)*len(judges)>10000:
            raise APIError(400,'组合超过10000条，请缩小队伍、评委或赛道筛选')
        reviews=[];coverage={'expected':len(teams)*len(judges),'recorded':0,'validSubmitted':0,'missing':0}
        for team in teams:
            for judge in judges:
                record=self.record(db,run_id,judge['id'],team['id'])
                if record:coverage['recorded']+=1
                else:
                    record={'teamId':team['id'],'judgeId':judge['id'],'version':0,'draft':None,'submitted':None,'recused':False,'updatedAt':None,
                            'hasUnsubmittedChanges':False,'invalidated':False,'invalidatedReason':'','status':'missing'}
                valid=record['submitted'] is not None and not record['recused'] and not record['invalidated']
                coverage['validSubmitted' if valid else 'missing']+=1
                include=status=='all' or (status=='missing' and not valid) or (status=='submitted' and valid) or \
                        (status=='draft' and record['draft'] is not None and (record['submitted'] is None or record['hasUnsubmittedChanges'])) or \
                        (status in {'recused','invalidated'} and record[status])
                if include:reviews.append(record)
        return {'run':self.run_view(db,run),'teams':teams,'judges':judges,'reviews':reviews,'coverage':coverage}

    def review_history(self, db, query):
        if not {'runId','teamId','judgeId'}.issubset(query):
            raise APIError(400,'请指定轮次、队伍和评委')
        if not db.execute('SELECT 1 FROM judging_runs WHERE id=?',(query['runId'],)).fetchone():
            raise APIError(404,'评审轮次不存在')
        rows=db.execute('SELECT * FROM judging_review_history WHERE run_id=? AND team_id=? AND judge_id=? ORDER BY version',
                        (query['runId'],query['teamId'],query['judgeId'])).fetchall()
        return {'history':[{'version':row['version'],'at':row['at'],'actorId':row['actor_id'],'action':row['action'],
                            'before':json.loads(row['before_json']) if row['before_json'] is not None else None,
                            'after':json.loads(row['after_json']),'reason':row['reason']} for row in rows],
                'historyAvailableFrom':int(db.execute("SELECT value FROM admin_data_meta WHERE key='historyAvailableFrom'").fetchone()[0])}

    def sync_marker(self, db):
        # A history ID catches same-millisecond changes without loading feedback or materials.
        revision = db.execute('SELECT COALESCE(MAX(id),0) FROM judging_review_history').fetchone()[0]
        current = self.now()
        stages = {}
        for row in db.execute('SELECT stage,run_id,revision,open,deadline FROM judging_stages ORDER BY stage'):
            stages[row['stage']] = {'runId':row['run_id'], 'revision':row['revision'],
                                    'canEdit':self.s.judging.window(row,current)['canEdit']}
        return {'reviewRevision':revision, 'stages':stages}

    def set_validity(self, db, actor, body):
        if set(body)!={'runId','teamId','judgeId','expectedVersion','invalidated','reason'}:
            raise APIError(400,'作废或恢复字段不正确')
        for key in ('runId','teamId','judgeId'):
            if not isinstance(body[key],str) or not 1<=len(body[key])<=80:
                raise APIError(400,'记录标识不正确')
        if type(body['invalidated']) is not bool or type(body['expectedVersion']) is not int or body['expectedVersion']<1:
            raise APIError(400,'记录状态或版本不正确')
        reason=self.text(body['reason'],'作废或恢复原因',2000,True)
        if not db.execute('SELECT 1 FROM judging_stages WHERE run_id=?',(body['runId'],)).fetchone():
            raise APIError(409,'旧轮次只读，不能改写有效状态')
        keys=(body['runId'],body['judgeId'],body['teamId'])
        before=self.record(db,*keys)
        if before is None:raise APIError(404,'评审记录不存在')
        if before['version']!=body['expectedVersion']:
            raise APIError(409,'评审记录已更新，请重新读取后核对')
        db.execute('UPDATE judging_reviews SET version=version+1,updated_at=? WHERE run_id=? AND judge_id=? AND team_id=?',(self.now(),*keys))
        db.execute("""INSERT INTO judging_review_validity VALUES (?,?,?,?,?,?) ON CONFLICT(run_id,judge_id,team_id)
          DO UPDATE SET invalidated=excluded.invalidated,reason=excluded.reason,updated_at=excluded.updated_at""",
                   (*keys,int(body['invalidated']),reason,self.now()))
        after=self.record(db,*keys)
        action='review.invalidate' if body['invalidated'] else 'review.restore'
        self.record_review_change(db,actor,*keys,action,before,after,reason)
        self.s.audit(db,actor,action,body['teamId'],{'runId':body['runId'],'judgeId':body['judgeId'],'reason':reason,
                     'before':{'invalidated':before['invalidated'],'version':before['version']},
                     'after':{'invalidated':after['invalidated'],'version':after['version']}})
        self.s.touch(db)
        return {'ok':True,'review':after}

    def audit_data(self, db, query):
        try:
            limit=int(query.get('limit','50'));before=int(query['beforeId']) if query.get('beforeId') else None
        except ValueError:
            raise APIError(400,'审计分页参数不正确')
        if not 1<=limit<=100 or before is not None and not 1<=before<=9223372036854775807:
            raise APIError(400,'审计分页范围不正确')
        clauses=[];values=[]
        for field,column in [('action','action'),('actorId','actor_id'),('target','target')]:
            if query.get(field):clauses.append(column+'=?');values.append(query[field])
        if before is not None:clauses.append('id<?');values.append(before)
        where=' WHERE '+' AND '.join(clauses) if clauses else ''
        rows=db.execute('SELECT * FROM audit'+where+' ORDER BY id DESC LIMIT ?',(*values,limit+1)).fetchall()
        return {'entries':[{'id':row['id'],'at':row['at'],'actorId':row['actor_id'],'role':row['role'],'action':row['action'],
                            'target':row['target'],'metadata':json.loads(row['metadata'])} for row in rows[:limit]],
                'nextBeforeId':rows[limit-1]['id'] if len(rows)>limit else None}

    def csv_bytes(self, headers, rows):
        output=io.StringIO(newline='');writer=csv.writer(output);writer.writerow(headers)
        for row in rows:
            clean=[]
            for value in row:
                text='' if value is None else str(value)
                if text.lstrip(' \t\r\n').startswith(('=','+','-','@')) or text.startswith(('\t','\r','\n')):
                    text="'"+text
                clean.append(text)
            writer.writerow(clean)
        return ('\ufeff'+output.getvalue()).encode('utf-8')

    def export_data(self, db, query):
        kind=query.get('kind')
        scoped=None
        if kind in {'teams','judges'} and ('stage' in query or 'runId' in query):
            scoped=self.review_data(db,{key:value for key,value in query.items() if key in {'stage','runId','teamId','judgeId','track','status'}})
        scope_fields=['stage','runId','frozenAt','recordFilter'] if scoped is not None else []
        scope_values=[scoped['run']['stage'],scoped['run']['id'],scoped['run']['frozenAt'],query.get('status','all')] if scoped is not None else []
        if kind=='teams':
            if scoped is not None:
                ids={row['teamId'] for row in scoped['reviews']}
                teams=scoped['teams'] if query.get('status','all')=='all' and not query.get('judgeId') else [team for team in scoped['teams'] if team['id'] in ids]
            else:teams=[self.team(db,row['id']) for row in db.execute('SELECT id FROM teams ORDER BY number')]
            if query.get('teamId'):teams=[team for team in teams if team['id']==query['teamId']]
            if query.get('track'):teams=[team for team in teams if team['track']==query['track']]
            return self.csv_bytes([*scope_fields,'id',*TEAM_FIELDS,'revision','updatedAt'],
                                  [[*scope_values,*[team.get(key) for key in ('id',*TEAM_FIELDS,'revision','updatedAt')]] for team in teams])
        if kind=='judges':
            if scoped is not None:
                ids={row['judgeId'] for row in scoped['reviews']}
                judges=scoped['judges'] if query.get('status','all')=='all' and not query.get('teamId') and not query.get('track') else [judge for judge in scoped['judges'] if judge['id'] in ids]
            else:judges=[dict(row) for row in db.execute("SELECT id,name,active,created_at AS createdAt FROM accounts WHERE role='judge' ORDER BY created_at,id")]
            if query.get('judgeId'):judges=[judge for judge in judges if judge['id']==query['judgeId']]
            return self.csv_bytes([*scope_fields,'id','name','active','credentialMode','createdAt'],
                                  [[*scope_values,judge['id'],judge['name'],bool(judge['active']),self.credential_mode(db,judge['id'],'judge'),judge['createdAt']] for judge in judges])
        if kind in {'reviews','rosters'}:
            data=self.review_data(db,{key:value for key,value in query.items() if key!='kind'})
            teams={team['id']:team for team in data['teams']};judges={judge['id']:judge for judge in data['judges']}
            prefix=[data['run']['stage'],data['run']['id'],data['run']['frozenAt']]
            if kind=='rosters':
                return self.csv_bytes(['stage','runId','frozenAt','rosterSource','teamId','number','name','project','track','judgeId','judgeName'],
                    [[*prefix,'manual_worklist',row['teamId'],teams[row['teamId']]['number'],teams[row['teamId']]['name'],
                      teams[row['teamId']]['project'],teams[row['teamId']]['track'],row['judgeId'],judges[row['judgeId']]['name']] for row in data['reviews']])
            return self.csv_bytes(['stage','runId','frozenAt','teamId','number','teamName','track','judgeId','judgeName','version','status','计分状态','draft','submitted','submittedAt','updatedAt','invalidated','invalidatedReason'],
                [[*prefix,row['teamId'],teams[row['teamId']]['number'],teams[row['teamId']]['name'],teams[row['teamId']]['track'],
                  row['judgeId'],judges[row['judgeId']]['name'],row['version'],row['status'],'未计分',row['draft']['feedback'] if row['draft'] else '',
                  row['submitted']['feedback'] if row['submitted'] else '',row['submitted']['submittedAt'] if row['submitted'] else None,
                  row['updatedAt'],row['invalidated'],row['invalidatedReason']] for row in data['reviews']])
        if kind=='audit':
            filters={key:value for key,value in query.items() if key in {'action','actorId','target','beforeId'}}
            rows=[]
            while True:
                page=self.audit_data(db,{**filters,'limit':'100'});rows.extend(page['entries'])
                if page['nextBeforeId'] is None:break
                filters['beforeId']=str(page['nextBeforeId'])
            return self.csv_bytes(['id','at','actorId','role','action','target','metadata'],[[row[key] if key!='metadata' else json.dumps(row[key],ensure_ascii=False) for key in ('id','at','actorId','role','action','target','metadata')] for row in rows])
        raise APIError(400,'导出分类不正确')

    def dispatch(self, db, actor, method, path, body, query, session_hash):
        self.s.require(actor,'admin')
        action=path.removeprefix('/api/admin/data/')
        if method=='POST':
            if query:raise APIError(400,'写入接口不接受查询参数')
            if action=='import/preview':return self.preview_import(db,actor,body,session_hash)
            if action=='import/commit':return self.commit_import(db,actor,body,session_hash)
            if action=='review-validity':return self.set_validity(db,actor,body)
        if method=='GET':
            allowed={'sync':set(),'template':{'kind'},'runs':{'stage'},'reviews':{'stage','runId','teamId','judgeId','track','status'},
                     'review-history':{'runId','teamId','judgeId'},'audit':{'action','actorId','target','beforeId','limit'},
                     'export':{'kind','stage','runId','teamId','judgeId','track','status','action','actorId','target','beforeId','limit'}}
            if action not in allowed or not set(query).issubset(allowed[action]):raise APIError(400,'查询字段或接口不正确')
            if action=='sync':return self.sync_marker(db)
            if action=='template':
                if query.get('kind') not in {'teams','judges'}:raise APIError(400,'导入分类不正确')
                return self.csv_bytes(TEAM_FIELDS if query['kind']=='teams' else ['id','name'],[])
            if action=='runs':
                stage=query.get('stage','exhibition')
                if stage not in STAGES:raise APIError(400,'评审阶段不正确')
                return {'runs':[self.run_view(db,row) for row in db.execute('SELECT * FROM judging_runs WHERE stage=? ORDER BY created_at DESC,id',(stage,))]}
            if action=='reviews':return self.review_data(db,query)
            if action=='review-history':return self.review_history(db,query)
            if action=='audit':return self.audit_data(db,query)
            if action=='export':return self.export_data(db,query)
        raise APIError(404,'后台数据接口不存在')
