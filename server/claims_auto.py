"""Organizer-owned trusted snapshots. Public registration never writes this store."""
import json
import re
from collections import Counter
from errors import APIError

SCHEMA='''
CREATE TABLE IF NOT EXISTS claim_auto_config (
 id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL DEFAULT 0,
 enabled INTEGER NOT NULL DEFAULT 0, source_name TEXT NOT NULL DEFAULT '', source_sha256 TEXT NOT NULL DEFAULT ''
);
INSERT OR IGNORE INTO claim_auto_config(id) VALUES (1);
CREATE TABLE IF NOT EXISTS claim_auto_people (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, school TEXT NOT NULL,
 normalized_name TEXT NOT NULL, normalized_school TEXT NOT NULL,
 eligible INTEGER NOT NULL, notes TEXT NOT NULL
);
'''

class AutoReview:
    def __init__(self,claims,normalize):
        self.c=claims;self.normalize=normalize

    def initialize(self,db):db.executescript(SCHEMA)

    def summary(self,db):
        config=dict(db.execute('SELECT * FROM claim_auto_config WHERE id=1').fetchone())
        rows=list(db.execute('SELECT * FROM claim_auto_people'))
        pairs=Counter((p['normalized_name'],p['normalized_school']) for p in rows)
        return {'revision':config['revision'],'enabled':bool(config['enabled']),'sourceName':config['source_name'],
                'sourceSha256':config['source_sha256'],'count':len(rows),
                'eligibleCount':sum(bool(p['eligible'] and p['normalized_name'] and p['normalized_school'] and pairs[(p['normalized_name'],p['normalized_school'])]==1) for p in rows),
                'ambiguousCount':sum(pairs[(p['normalized_name'],p['normalized_school'])]>1 for p in rows)}

    def revision(self,db,value):
        if type(value) is not int or value<0:raise APIError(400,'需要自动审核名单修订号')
        if value!=self.summary(db)['revision']:raise APIError(409,'自动审核名单已变化，请刷新后核对')

    def replace(self,db,actor,body):
        if set(body)!={'autoReview'} or not isinstance(body['autoReview'],dict):raise APIError(400,'自动审核导入字段不正确')
        value=body['autoReview']
        if set(value)!={'expectedRevision','sourceName','sourceSha256','entries'}:raise APIError(400,'自动审核导入字段不正确')
        self.revision(db,value['expectedRevision'])
        source=self.c.text(value['sourceName'],'名单来源',200,True)
        if not isinstance(value['sourceSha256'],str) or not re.fullmatch('[a-f0-9]{64}',value['sourceSha256']):raise APIError(400,'需要来源文件 SHA-256')
        entries=value['entries']
        if not isinstance(entries,list) or len(entries)>500:raise APIError(400,'自动审核名单最多 500 人')
        rows=[];ids=set()
        for p in entries:
            if not isinstance(p,dict) or set(p)!={'id','name','school','eligible','notes'} or type(p['eligible']) is not bool:raise APIError(400,'自动审核名单字段不正确')
            pid=self.c.text(p['id'],'编号',100,True)
            if not re.fullmatch('[A-Za-z0-9_-]{1,100}',pid) or pid in ids:raise APIError(400,'自动审核名单编号不正确或重复')
            ids.add(pid)
            name=self.c.text(p['name'],'姓名',100);school=self.c.text(p['school'],'学校',200)
            rows.append((pid,name,school,self.normalize(name),self.normalize(school),int(p['eligible']),self.c.text(p['notes'],'备注',2000)))
        db.execute('DELETE FROM claim_auto_people')
        db.executemany('INSERT INTO claim_auto_people VALUES (?,?,?,?,?,?,?)',rows)
        # Every replacement requires a fresh explicit enable, even if the previous source was enabled.
        db.execute('UPDATE claim_auto_config SET revision=revision+1,enabled=0,source_name=?,source_sha256=? WHERE id=1',(source,value['sourceSha256']))
        summary=self.summary(db);self.c.s.audit(db,actor,'claims.auto-review.import',metadata=summary)
        return {'ok':True,'autoReview':summary}

    def configure(self,db,actor,body):
        if set(body)!={'expectedRevision','enabled'} or type(body['enabled']) is not bool:raise APIError(400,'自动审核设置字段不正确')
        self.revision(db,body['expectedRevision'])
        if body['enabled'] and not self.summary(db)['eligibleCount']:raise APIError(409,'请先导入有效的自动审核名单')
        db.execute('UPDATE claim_auto_config SET revision=revision+1,enabled=? WHERE id=1',(int(body['enabled']),))
        summary=self.summary(db);self.c.s.audit(db,actor,'claims.auto-review.configure',metadata=summary)
        return {'ok':True,'autoReview':summary}

    def approve_all(self,db):
        row=db.execute("SELECT value FROM settings WHERE key='claimsAutoApproveAll'").fetchone()
        return True if row is None else json.loads(row['value']) is True

    def registered_candidate(self,db,row,declaration):
        people=[declaration['captain']]+declaration['members']
        collisions=self.c.s.team_lifecycle.name_collisions(db,declaration['teamName'])
        if len(collisions)!=1:return None
        selected=self.c.eligible_team(db,collisions[0]['id'])
        if not selected or not selected['registration']:return None
        registration=selected['registration']
        if (self.normalize(registration['captain'])!=self.normalize(row['name']) or
            self.normalize(registration['school'])!=self.normalize(row['school']) or
            Counter(self.normalize(n) for n in registration['members'])!=Counter(self.normalize(p['name']) for p in people)):
            return None
        return selected

    def attempt(self,db,request_id,strict=False):
        if not self.approve_all(db):return self.attempt_trusted(db,request_id)
        c=self.c;row=db.execute('SELECT * FROM claim_requests WHERE id=?',(request_id,)).fetchone()
        if not row or row['status']!='pending' or row['expires_at']<=c.clock():return False
        declaration=c.declaration(db,request_id)
        if not declaration or not declaration.get('teamName'):return False
        selected=self.registered_candidate(db,row,declaration)
        context,_=c.review_context(db,request_id,row['revision'],declaration)
        body={'requestId':request_id,'expectedRevision':row['revision'],'reviewContext':context,
              'teamName':declaration['teamName'],'track':declaration.get('track',''),'resolvedIdentity':False,'note':''}
        if selected:body.update(teamId=selected['id'],teamContext=selected['teamContext'])
        db.execute('SAVEPOINT automatic_open_approval')
        try:
            c.approve_open(db,None,body,automatic=True)
            c.s.audit(db,None,'claims.approve-auto-open',request_id,{'policy':'complete-valid-application',
                **({'selectedTeamId':selected['id']} if selected else {})})
            return True
        except APIError as error:
            db.execute('ROLLBACK TO automatic_open_approval')
            if strict and error.status in {400,409}:
                raise APIError(409,error.message,'claim_conflict')
            if error.status not in {400,409}:raise
            return False
        finally:db.execute('RELEASE automatic_open_approval')

    def attempt_trusted(self,db,request_id):
        c=self.c;summary=self.summary(db)
        if not summary['enabled']:return
        row=db.execute('SELECT * FROM claim_requests WHERE id=?',(request_id,)).fetchone()
        if not row or row['status']!='pending' or row['expires_at']<=c.clock():return
        declaration=c.declaration(db,request_id)
        if not declaration or not declaration.get('teamName'):return
        people=[declaration['captain']]+declaration['members'];matched=[]
        for person in people:
            identity=(self.normalize(person['name']),self.normalize(person['school']))
            matches=list(db.execute('SELECT * FROM claim_auto_people WHERE normalized_name=? AND normalized_school=?',identity))
            if len(matches)!=1 or not matches[0]['eligible'] or not all(identity):return
            matched.append(matches[0]['id'])
        collisions=c.s.team_lifecycle.name_collisions(db,declaration['teamName'])
        selected=None
        if collisions:
            if len(collisions)!=1:return
            selected=c.eligible_team(db,collisions[0]['id'])
            if not selected or not selected['registration']:return
            registration=selected['registration']
            if (self.normalize(registration['captain'])!=self.normalize(row['name']) or
                self.normalize(registration['school'])!=self.normalize(row['school']) or
                Counter(self.normalize(n) for n in registration['members'])!=Counter(self.normalize(p['name']) for p in people)):return
        if selected:
            for person in people:
                schools={p[0] for p in db.execute('SELECT normalized_school FROM claim_auto_people WHERE normalized_name=?',(self.normalize(person['name']),))}
                if len(schools)>1:return
        context,_=c.review_context(db,request_id,row['revision'],declaration)
        body={'requestId':request_id,'expectedRevision':row['revision'],'reviewContext':context,
              'teamName':declaration['teamName'],'track':declaration.get('track',''),'resolvedIdentity':False,
              'note':'','verificationMethod':'online','applicationReviewed':True}
        if selected:body.update(teamId=selected['id'],teamContext=selected['teamContext'])
        db.execute('SAVEPOINT auto_review')
        try:
            c.approve_open(db,None,body)
            c.s.audit(db,None,'claims.approve-auto',request_id,{'snapshotRevision':summary['revision'],
                'sourceSha256':summary['sourceSha256'],'sourceName':summary['sourceName'],'matchedSourceIds':matched})
        except APIError as error:
            db.execute('ROLLBACK TO auto_review')
            if error.status not in {400,409}:raise
        finally:db.execute('RELEASE auto_review')
