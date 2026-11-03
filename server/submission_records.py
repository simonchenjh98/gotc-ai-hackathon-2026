"""Atomic submission outcomes, immutable formal snapshots and manual link checks."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
from errors import APIError

URL_FIELDS=('codeUrl','readmeUrl','licenseUrl','demoUrl','slidesUrl','videoUrl')
CHECK_STATUSES={'unchecked','accessible','permission_required','unavailable'}
DDL=[
'''CREATE TABLE IF NOT EXISTS submission_records_meta (key TEXT PRIMARY KEY,value TEXT NOT NULL)''',
'''CREATE TABLE IF NOT EXISTS submission_requests (
 team_id TEXT NOT NULL REFERENCES teams(id),request_id TEXT NOT NULL,payload_sha256 TEXT NOT NULL,
 action TEXT NOT NULL CHECK(action IN ('draft','submitted')),base_version INTEGER NOT NULL,
 outcome TEXT NOT NULL CHECK(outcome IN ('succeeded','rejected')),receipt_json TEXT NOT NULL,
 http_status INTEGER NOT NULL,created_at INTEGER NOT NULL,PRIMARY KEY(team_id,request_id))''',
'''CREATE TABLE IF NOT EXISTS submission_history (
 team_id TEXT NOT NULL REFERENCES teams(id),submission_version INTEGER NOT NULL,
 materials_json TEXT NOT NULL,submitted_at INTEGER NOT NULL,request_id TEXT,
 origin TEXT NOT NULL CHECK(origin IN ('submitted','legacy_current')),
 PRIMARY KEY(team_id,submission_version),UNIQUE(team_id,request_id))''',
'''CREATE TABLE IF NOT EXISTS submission_material_checks (
 team_id TEXT NOT NULL,submission_version INTEGER NOT NULL,field TEXT NOT NULL,url TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('unchecked','accessible','permission_required','unavailable')),
 revision INTEGER NOT NULL CHECK(revision>=1),checked_at INTEGER NOT NULL,
 actor_id TEXT NOT NULL REFERENCES accounts(id),actor_name TEXT NOT NULL,note TEXT NOT NULL,
 PRIMARY KEY(team_id,submission_version,field),
 FOREIGN KEY(team_id,submission_version) REFERENCES submission_history(team_id,submission_version))''',
]
for table in ('submission_requests','submission_history'):
    for action in ('UPDATE','DELETE'):
        DDL.append(f"CREATE TRIGGER IF NOT EXISTS {table}_no_{action.lower()} BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT,'immutable submission record'); END")


def query_path(path):
    return re.fullmatch(r'/api/(?:submission/history|admin/submissions/[^/]+/(?:history|checks))',path) is not None


def stable_json(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))


class SubmissionRecords:
    def __init__(self,service):
        self.s=service

    def marked(self):
        if not self.s.db_path.exists():return False
        with closing(sqlite3.connect(self.s.db_path)) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='submission_records_meta'").fetchone():return False
            return db.execute("SELECT 1 FROM submission_records_meta WHERE key='schemaVersion' AND value='1'").fetchone() is not None

    def backup(self):
        path=Path(self.s.data_dir)/('before-submission-records-'+str(self.s.judging.clock())+'-'+secrets.token_hex(4)+'.sqlite3')
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600);os.close(fd)
        with closing(sqlite3.connect(self.s.db_path)) as source:
            with closing(sqlite3.connect(path)) as target:source.backup(target)
        return path

    def initialize(self,backup=True):
        marked=self.marked()
        if backup and not marked:self.backup()
        with self.s.db() as db:
            db.execute('BEGIN IMMEDIATE')
            for statement in DDL:db.execute(statement)
            self.seed_current(db)
            db.execute("INSERT OR IGNORE INTO submission_records_meta VALUES ('schemaVersion','1')")
            db.execute("INSERT OR IGNORE INTO submission_records_meta VALUES ('historyAvailableFrom',?)",(str(self.s.judging.clock()),))
            if db.execute('PRAGMA foreign_key_check').fetchall():raise RuntimeError('Submission record migration failed foreign-key validation')

    def seed_current(self,db):
        for row in db.execute('SELECT * FROM submissions WHERE submitted_json IS NOT NULL').fetchall():
            old=db.execute('SELECT * FROM submission_history WHERE team_id=? AND submission_version=?',(row['team_id'],row['submitted_version'])).fetchone()
            if old:
                if json.loads(old['materials_json'])!=json.loads(row['submitted_json']) or old['submitted_at']!=row['submitted_at']:
                    raise RuntimeError('Current formal submission disagrees with immutable history')
                continue
            db.execute('INSERT INTO submission_history VALUES (?,?,?,?,?,?)',(row['team_id'],row['submitted_version'],row['submitted_json'],row['submitted_at'],None,'legacy_current'))

    def request_id(self,value):
        if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,128}',value):raise APIError(400,'请求编号格式不正确','request_id_invalid')
        return value

    def error_from_receipt(self,receipt):
        error=APIError(receipt['error']['status'],receipt['error']['message'],receipt['error']['code'])
        error.request_id=receipt['requestId']
        return error

    def save(self,db,actor,team,body):
        if (not isinstance(body,dict) or set(body)-{'expectedVersion','status','materials','requestId'}
                or not {'expectedVersion','status','materials'}.issubset(body)
                or type(body['expectedVersion']) is not int or not 0<=body['expectedVersion']<=9007199254740991
                or not isinstance(body['status'],str) or body['status'] not in {'draft','submitted'} or not isinstance(body['materials'],dict)):
            raise APIError(400,'提交字段或状态不正确','submission_envelope_invalid')
        request_id=self.request_id(body['requestId']) if 'requestId' in body else 'legacy-'+secrets.token_hex(16)
        payload={key:body[key] for key in ('expectedVersion','status','materials')}
        digest=hashlib.sha256(stable_json(payload).encode()).hexdigest()
        previous=db.execute('SELECT * FROM submission_requests WHERE team_id=? AND request_id=?',(team['id'],request_id)).fetchone()
        if previous:
            if previous['payload_sha256']!=digest:raise APIError(409,'同一请求编号已用于不同内容，请为新操作使用新编号','request_id_conflict')
            receipt=json.loads(previous['receipt_json'])
            if previous['outcome']=='rejected':return self.error_from_receipt(receipt)
            current=db.execute('SELECT * FROM teams WHERE id=?',(team['id'],)).fetchone()
            return {'ok':True,**self.s.team_submission_response(db,current),'receipt':receipt,'replayed':True}
        db.execute('SAVEPOINT submission_attempt')
        try:
            result=self.s.save_submission(db,actor,team,payload)
            s=result['submission'];saved_at=s['updatedAt'];version=s['version']
            history_version=version if payload['status']=='submitted' else None
            if history_version is not None:
                db.execute('INSERT INTO submission_history VALUES (?,?,?,?,?,?)',(team['id'],version,stable_json(s['submitted']),s['submittedAt'],request_id,'submitted'))
            receipt={'requestId':request_id,'status':'succeeded','action':payload['status'],'baseVersion':payload['expectedVersion'],'version':version,'savedAt':saved_at,'materials':s['draft'],'historyVersion':history_version}
            db.execute('RELEASE submission_attempt')
        except APIError as error:
            db.execute('ROLLBACK TO submission_attempt');db.execute('RELEASE submission_attempt')
            receipt={'requestId':request_id,'status':'rejected','action':payload['status'],'baseVersion':payload['expectedVersion'],
                     'error':{'status':error.status,'code':error.code or 'materials_invalid','message':error.message},'decidedAt':self.s.judging.clock()}
            db.execute('INSERT INTO submission_requests VALUES (?,?,?,?,?,?,?,?,?)',(team['id'],request_id,digest,payload['status'],payload['expectedVersion'],'rejected',stable_json(receipt),error.status,receipt['decidedAt']))
            return self.error_from_receipt(receipt)
        db.execute('INSERT INTO submission_requests VALUES (?,?,?,?,?,?,?,?,?)',(team['id'],request_id,digest,payload['status'],payload['expectedVersion'],'succeeded',stable_json(receipt),200,saved_at))
        return {**result,'receipt':receipt,'replayed':False}

    def lookup(self,db,team_id,request_id):
        self.request_id(request_id)
        row=db.execute('SELECT receipt_json FROM submission_requests WHERE team_id=? AND request_id=?',(team_id,request_id)).fetchone()
        return json.loads(row['receipt_json']) if row else {'requestId':request_id,'status':'not_found'}

    def positive(self,value,name):
        if isinstance(value,str) and re.fullmatch(r'[1-9][0-9]{0,15}',value) and int(value)<=9007199254740991:return int(value)
        if type(value) is int and 1<=value<=9007199254740991:return value
        raise APIError(400,name+'必须是正整数')

    def history(self,db,team_id,query):
        if set(query)-{'beforeVersion','limit'}:raise APIError(400,'历史查询参数不正确')
        limit=self.positive(query.get('limit','20'),'读取条数')
        if limit>50:raise APIError(400,'历史每次最多读取50条')
        before=self.positive(query['beforeVersion'],'历史边界') if 'beforeVersion' in query else 9223372036854775807
        rows=db.execute('SELECT * FROM submission_history WHERE team_id=? AND submission_version<? ORDER BY submission_version DESC LIMIT ?',(team_id,before,limit+1)).fetchall()
        items=[]
        for row in rows[:limit]:
            materials=json.loads(row['materials_json'])
            items.append({'version':row['submission_version'],'submittedAt':row['submitted_at'],'requestId':row['request_id'],'origin':row['origin'],'codeVersion':materials.get('codeVersion',''),'projectName':materials.get('projectName','')})
        return {'items':items,'nextBeforeVersion':items[-1]['version'] if len(rows)>limit else None}

    def detail(self,db,team_id,version):
        row=db.execute('SELECT * FROM submission_history WHERE team_id=? AND submission_version=?',(team_id,version)).fetchone()
        if not row:raise APIError(404,'该正式版本不存在')
        return {'version':row['submission_version'],'submittedAt':row['submitted_at'],'requestId':row['request_id'],'origin':row['origin'],'materials':json.loads(row['materials_json'])}

    def checks(self,db,team_id,version):
        detail=self.detail(db,team_id,version);latest=self.s.submission(db,team_id)['submittedVersion']
        saved={row['field']:row for row in db.execute('SELECT * FROM submission_material_checks WHERE team_id=? AND submission_version=?',(team_id,version))}
        items=[]
        for field in URL_FIELDS:
            url=detail['materials'].get(field,'')
            if not url:continue
            row=saved.get(field)
            if row and row['url']!=url:raise RuntimeError('Manual check URL disagrees with immutable history')
            items.append({'field':field,'url':url,'status':row['status'] if row else 'unchecked','revision':row['revision'] if row else 0,
                          'checkedAt':row['checked_at'] if row else None,'actor':{'id':row['actor_id'],'name':row['actor_name']} if row else None,'note':row['note'] if row else ''})
        return {'teamId':team_id,'version':version,'latestVersion':latest,'items':items}

    def save_check(self,db,actor,team_id,body):
        if not isinstance(body,dict) or set(body)!={'version','field','expectedRevision','status','note'}:raise APIError(400,'人工访问记录字段不正确')
        version=self.positive(body['version'],'正式版本');field=body['field'];revision=body['expectedRevision']
        if type(revision) is not int or revision<0 or not isinstance(field,str) or field not in URL_FIELDS:raise APIError(400,'材料项或修订号不正确')
        if not isinstance(body['status'],str) or body['status'] not in CHECK_STATUSES:raise APIError(400,'人工访问状态不正确')
        note=self.s.judging.text(body['note'],'备注',500)
        current=self.checks(db,team_id,version)
        if current['latestVersion']!=version:raise APIError(409,'正式材料已更新，旧版本访问记录只读，请核对最新版本','check_version_conflict')
        item=next((row for row in current['items'] if row['field']==field),None)
        if not item:raise APIError(400,'该正式版本没有此链接材料项')
        if item['revision']!=revision:raise APIError(409,'此材料的访问记录已被其他管理员更新，当前输入应保留并重新核对','check_revision_conflict')
        now=self.s.judging.clock()
        db.execute('''INSERT INTO submission_material_checks VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(team_id,submission_version,field)
          DO UPDATE SET url=excluded.url,status=excluded.status,revision=excluded.revision,checked_at=excluded.checked_at,
          actor_id=excluded.actor_id,actor_name=excluded.actor_name,note=excluded.note''',(team_id,version,field,item['url'],body['status'],revision+1,now,actor['id'],actor['name'],note))
        self.s.audit(db,actor,'submission.check',team_id,{'version':version,'field':field,'before':{'status':item['status'],'revision':revision},'after':{'status':body['status'],'revision':revision+1}})
        return self.checks(db,team_id,version)

    def supports(self,path):
        return path.startswith(('/api/submission/requests/','/api/submission/history')) or re.fullmatch(r'/api/admin/submissions/[^/]+/(?:history(?:/[^/]+)?|checks)',path) is not None

    def dispatch(self,db,actor,method,path,body,query):
        admin=path.startswith('/api/admin/')
        if admin:
            self.s.require(actor,'admin');parts=path.split('/');team_id=self.s.team_exists(db,parts[4]);tail=parts[5:]
        else:
            self.s.require(actor,'team');team=self.s.bound_submission_team(db,actor);team_id=team['id'];tail=path.split('/')[3:]
        if not admin and len(tail)==2 and tail[0]=='requests' and method=='GET' and not query:return self.lookup(db,team_id,tail[1])
        if tail==['history'] and method=='GET':return self.history(db,team_id,query)
        if len(tail)==2 and tail[0]=='history' and method=='GET' and not query:return self.detail(db,team_id,self.positive(tail[1],'正式版本'))
        if admin and tail==['checks']:
            if method=='GET' and set(query)=={'version'}:return self.checks(db,team_id,self.positive(query['version'],'正式版本'))
            if method=='POST' and not query:return self.save_check(db,actor,team_id,body)
        raise APIError(400,'提交记录路径或查询参数不正确')
