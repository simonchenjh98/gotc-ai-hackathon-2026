"""Transactional revisions for the original score editor and settings forms."""
from contextlib import closing
import json
import os
from pathlib import Path
import secrets
import sqlite3

from errors import APIError

MAX_REVISION=9007199254740991
REVISION_REQUIRED="页面版本过旧，请先复制未保存输入并刷新页面后重试"
SETTINGS_GROUPS={
    'scoring':{'scoringOpen','resultsPublished','minJudges'},
    'submissions':{'submissionsOpen','submissionDeadline'},
    'voting':{'votingOpen','votingDeadline'},
}
DDL=[
    'CREATE TABLE IF NOT EXISTS write_conflicts_meta (key TEXT PRIMARY KEY,value TEXT NOT NULL)',
    '''CREATE TABLE IF NOT EXISTS score_write_revisions (
       judge_id TEXT NOT NULL,team_id TEXT NOT NULL,revision INTEGER NOT NULL CHECK(revision>=1),
       PRIMARY KEY(judge_id,team_id),FOREIGN KEY(judge_id,team_id) REFERENCES scores(judge_id,team_id))''',
    '''CREATE TABLE IF NOT EXISTS settings_write_revisions (
       group_id TEXT PRIMARY KEY,revision INTEGER NOT NULL CHECK(revision>=0))''',
]


class WriteConflicts:
    def __init__(self,service):
        self.s=service

    def marked(self,db):
        return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='write_conflicts_meta'").fetchone()
            and db.execute("SELECT 1 FROM write_conflicts_meta WHERE key='schemaVersion' AND value='1'").fetchone())

    def backup_before_upgrade(self):
        if not self.s.db_path.exists():return
        with closing(sqlite3.connect(self.s.db_path)) as source:
            if self.marked(source):return
            path=Path(self.s.data_dir)/('before-write-conflicts-'+str(self.s.judging.clock())+'-'+secrets.token_hex(4)+'.sqlite3')
            fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600);os.close(fd)
            with closing(sqlite3.connect(path)) as target:source.backup(target)

    def initialize(self):
        with self.s.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if self.marked(db):return
            for statement in DDL:db.execute(statement)
            db.execute('INSERT INTO score_write_revisions SELECT judge_id,team_id,1 FROM scores')
            db.executemany('INSERT INTO settings_write_revisions VALUES (?,0)',[(key,) for key in SETTINGS_GROUPS])
            if db.execute('PRAGMA foreign_key_check').fetchall():raise RuntimeError('Write revision migration failed foreign-key validation')
            db.execute("INSERT INTO write_conflicts_meta VALUES ('schemaVersion','1')")

    def revision(self,value):
        if type(value) is not int or not 0<=value<=MAX_REVISION:
            raise APIError(400,'编辑版本须为有效的非负安全整数','revision_invalid')
        return value

    def compare(self,expected,current,kind):
        if self.revision(expected)!=current:
            raise APIError(409,'评分已在其他页面更新，请保留当前输入并核对最新记录' if kind=='score' else '此组设置已在其他页面更新，请保留当前输入并核对最新设置',kind+'_revision_conflict')
        if current==MAX_REVISION:
            raise APIError(409,'编辑版本已达上限，请联系组织方维护','revision_exhausted')

    def scores(self,db,judge_id):
        return [self.score(row) for row in db.execute('''SELECT s.*,r.revision FROM scores s
            JOIN score_write_revisions r ON r.judge_id=s.judge_id AND r.team_id=s.team_id WHERE s.judge_id=?''',(judge_id,))]

    def score(self,row):
        return {'teamId':row['team_id'],'scores':json.loads(row['scores_json']),'feedback':row['feedback'],
            'status':row['status'],'revision':row['revision'],'updatedAt':row['updated_at']}

    def check_score(self,db,judge_id,team_id,body):
        if 'expectedRevision' not in body:raise APIError(428,REVISION_REQUIRED,'revision_required')
        row=db.execute('SELECT revision FROM score_write_revisions WHERE judge_id=? AND team_id=?',(judge_id,team_id)).fetchone()
        current=row['revision'] if row else 0
        self.compare(body['expectedRevision'],current,'score')
        return current+1

    def advance_score(self,db,judge_id,team_id,revision):
        db.execute('''INSERT INTO score_write_revisions VALUES (?,?,?) ON CONFLICT(judge_id,team_id)
            DO UPDATE SET revision=excluded.revision''',(judge_id,team_id,revision))
        return next(row for row in self.scores(db,judge_id) if row['teamId']==team_id)

    def settings_revisions(self,db):
        return {row['group_id']:row['revision'] for row in db.execute('SELECT * FROM settings_write_revisions')}

    def admin_config(self,db):
        return {**self.s.config(db),'writeRevisions':self.settings_revisions(db)}

    def check_settings(self,db,body):
        allowed=set().union(*SETTINGS_GROUPS.values())
        fields=set(body)-{'expectedRevisions'}
        if not fields or not fields.issubset(allowed):raise APIError(400,'设置字段不正确')
        groups={group for group,keys in SETTINGS_GROUPS.items() if fields & keys}
        if 'expectedRevisions' not in body:raise APIError(428,REVISION_REQUIRED,'revision_required')
        expected=body['expectedRevisions']
        if not isinstance(expected,dict) or set(expected)-groups:raise APIError(400,'设置版本组不正确','revision_invalid')
        if groups-set(expected):raise APIError(428,REVISION_REQUIRED,'revision_required')
        # Validate every supplied token before comparing any group or writing fields.
        for value in expected.values():self.revision(value)
        current=self.settings_revisions(db)
        for group in sorted(groups):self.compare(expected[group],current[group],'settings')
        changes={key:body[key] for key in fields}
        for key,value in changes.items():
            if key=='minJudges':
                if type(value) is not int or not 1<=value<=50:raise APIError(400,'最少评委数应为1至50的整数')
            elif key in {'submissionDeadline','votingDeadline'}:
                if value is not None and (type(value) is not int or not 0<=value<=253402300799999):
                    raise APIError(400,'窗口截止时间必须是有效的毫秒时间戳或空值')
            elif type(value) is not bool:raise APIError(400,'开放与公开状态必须为布尔值')
        return changes,groups

    def advance_settings(self,db,groups):
        for group in sorted(groups):db.execute('UPDATE settings_write_revisions SET revision=revision+1 WHERE group_id=?',(group,))
