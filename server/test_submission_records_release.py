"""Isolated migration/recovery checks; exact d047 rehearsal is separate release evidence.

CI's old-schema fixture suppresses only the new initializer, then exercises the
unchanged original save_submission method. No /tmp fixture or Git history is
required. All identities, material URLs and credentials are synthetic.
"""
from contextlib import closing, redirect_stdout
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import app
from submission_records import SubmissionRecords
import test_submissions

ROOT=Path(__file__).resolve().parents[1]
NEW_TABLES={'submission_records_meta','submission_requests','submission_history','submission_material_checks'}


def load_module(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def snapshot(path):
    with closing(sqlite3.connect(path)) as db:
        names=[row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return {'schema':db.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name').fetchall(),
                'tables':{name:db.execute('SELECT * FROM "'+name.replace('"','""')+'" ORDER BY rowid').fetchall() for name in names}}


class SubmissionRecordsReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='gotc-records-release-')
        self.root=Path(self.tmp.name).resolve()
        self.public=self.root/'public'
        self.public.mkdir()
        (self.public/'index.html').write_text('<!doctype html><title>Synthetic event</title>')

    def tearDown(self):
        self.tmp.cleanup()

    def service(self,data):
        return app.EventService(data_dir=data,origin='http://127.0.0.1',static_root=self.public)

    def call(self,service,method,path,body=None,cookie='',query=None):
        return service.dispatch(method,path,body or {},cookie,'127.0.0.1',query=query)[0]

    def materials(self,**changes):
        return test_submissions.SubmissionAPITests.materials(self,**changes)

    def old_save(self,service,cookie,version,status,materials):
        # This method predates receipt routing and remains the original write path.
        with service.db() as db:
            db.execute('BEGIN IMMEDIATE')
            actor=service.account(db,cookie)
            return service.save_submission(db,actor,service.bound_submission_team(db,actor),
                {'expectedVersion':version,'status':status,'materials':materials})

    def fixture(self,name='legacy',legacy=True):
        data=self.root/name
        if legacy:
            with patch.object(SubmissionRecords,'initialize',return_value=None):service=self.service(data)
        else:service=self.service(data)
        admin_code=(data/'admin-access.txt').read_text().splitlines()[1]
        _,token,_=service.dispatch('POST','/api/login',{'role':'admin','code':admin_code},'','127.0.0.1')
        admin=app.COOKIE_NAME+'='+token
        team=self.call(service,'POST','/api/admin/teams',{'number':1,'name':'Synthetic legacy team','project':'Old project','track':'AI for Science'},admin)['team']['id']
        access=self.call(service,'POST','/api/admin/team-access',{'teamId':team,'action':'issue'},admin)
        _,token,_=service.dispatch('POST','/api/login',{'role':'team','code':access['code']},'','127.0.0.1')
        cookie=app.COOKIE_NAME+'='+token
        self.call(service,'POST','/api/admin/settings',{'submissionsOpen':True},admin)
        save=lambda version,status,materials:self.old_save(service,cookie,version,status,materials) if legacy else self.call(service,'POST','/api/submission',{'expectedVersion':version,'status':status,'materials':materials,'requestId':'synthetic-request-'+str(version)},cookie)
        save(0,'submitted',self.materials(projectName='Frozen formal v1'))
        judge=self.call(service,'POST','/api/admin/invitations',{'role':'judge','name':'Synthetic legacy judge'},admin)
        path='/api/admin/judging/exhibition'
        revision=self.call(service,'GET',path,cookie=admin)['stage']['revision']
        state=self.call(service,'POST',path+'/roster',{'expectedRevision':revision,'teamIds':[team],'judgeIds':[judge['id']]},admin)
        self.call(service,'POST',path+'/window',{'expectedRevision':state['stage']['revision'],'open':True,'deadline':None},admin)
        save(1,'submitted',self.materials(projectName='Current formal v2'))
        save(2,'draft',self.materials(projectName='Private draft v3'))
        if legacy:
            # Already-admin-migrated databases must still receive an independent backup.
            # Preserve an original short/tag value verbatim: migration cannot invent SHA.
            with service.db() as db:
                current=json.loads(db.execute('SELECT submitted_json FROM submissions WHERE team_id=?',(team,)).fetchone()[0])
                current['codeVersion']='legacy-tag-v2'
                db.execute('UPDATE submissions SET submitted_json=? WHERE team_id=?',(json.dumps(current,ensure_ascii=False),team))
        with service.db() as db:db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        return {'data':data,'service':service,'team':team,'admin':admin,'cookie':cookie,'before':snapshot(service.db_path),
                'credential':(data/'admin-access.txt').read_bytes()}

    def private_valid(self,path):
        self.assertEqual(stat.S_IMODE(path.stat().st_mode),0o600)
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(),[])

    def assert_old_unchanged(self,fixture,path):
        after=snapshot(path)
        for table,rows in fixture['before']['tables'].items():self.assertEqual(after['tables'][table],rows,table)
        old_names=set(fixture['before']['tables'])
        self.assertEqual([row for row in after['schema'] if row[2] in old_names],fixture['before']['schema'])
        self.assertEqual((fixture['data']/'admin-access.txt').read_bytes(),fixture['credential'])

    def test_new_database_and_repeat_start_are_idempotent(self):
        service=self.service(self.root/'new')
        before=snapshot(service.db_path)
        self.assertTrue(NEW_TABLES.issubset(before['tables']))
        self.assertEqual(before['tables']['submission_history'],[])
        self.assertEqual(dict(before['tables']['submission_records_meta'])['schemaVersion'],'1')
        self.assertEqual(list(service.data_dir.glob('before-submission-records-*.sqlite3')),[])
        for _ in range(2):self.service(service.data_dir)
        self.assertEqual(snapshot(service.db_path),before)
        self.assertEqual(list(service.data_dir.glob('before-submission-records-*.sqlite3')),[])
        self.private_valid(service.db_path)

    def test_nonempty_upgrade_preserves_original_state_and_only_seeds_current_formal(self):
        fixture=self.fixture()
        self.assertIn('admin_data_meta',fixture['before']['tables'])
        self.assertFalse(NEW_TABLES.intersection(fixture['before']['tables']))
        upgraded=self.service(fixture['data'])
        self.assert_old_unchanged(fixture,upgraded.db_path)
        with upgraded.db() as db:
            history=db.execute('SELECT * FROM submission_history').fetchall()
            self.assertEqual(len(history),1)
            original=db.execute('SELECT * FROM submissions WHERE team_id=?',(fixture['team'],)).fetchone()
            self.assertEqual(tuple(history[0]),(fixture['team'],2,original['submitted_json'],original['submitted_at'],None,'legacy_current'))
            self.assertEqual(json.loads(history[0]['materials_json'])['codeVersion'],'legacy-tag-v2')
            frozen=json.loads(db.execute('SELECT snapshot_json FROM judging_roster').fetchone()[0])['materialSnapshot']
            self.assertEqual(frozen['submissionVersion'],1)
            self.assertEqual(original['version'],3)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM submission_requests').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM submission_material_checks').fetchone()[0],0)
        backups=list(fixture['data'].glob('before-submission-records-*.sqlite3'))
        self.assertEqual(len(backups),1)
        self.assertEqual(snapshot(backups[0]),fixture['before'])
        self.private_valid(backups[0])
        after=snapshot(upgraded.db_path)
        for _ in range(2):self.service(fixture['data'])
        self.assertEqual(snapshot(upgraded.db_path),after)
        self.assertEqual(list(fixture['data'].glob('before-submission-records-*.sqlite3')),backups)
        self.assertEqual(self.call(upgraded,'GET','/api/session',cookie=fixture['cookie'])['role'],'team')
        self.assertEqual(self.call(upgraded,'GET','/api/session',cookie=fixture['admin'])['role'],'admin')

    def failed_main(self,data):
        # The real startup order must fail before even constructing a listener.
        mask=os.umask(0o077)
        try:
            with patch.dict(os.environ,{'GOTC_DATA_DIR':str(data),'GOTC_PUBLIC_ORIGIN':'http://127.0.0.1','GOTC_HOST':'127.0.0.1','GOTC_PORT':'0'}),patch.object(app,'EventHTTPServer') as listener:
                with self.assertRaises((OSError,sqlite3.Error)):app.main()
                listener.assert_not_called()
        finally:os.umask(mask)

    def test_backup_failure_stops_startup_without_new_tables(self):
        connect=sqlite3.connect
        for mode in ('create','sqlite_backup'):
            with self.subTest(mode=mode):
                fixture=self.fixture('backup-failed-'+mode)
                real_open=os.open
                class BadBackup(sqlite3.Connection):
                    def backup(self,*args,**kwargs):
                        self.execute('SELECT missing_sqlite_backup_function()')
                def failed_open(path,*args,**kwargs):
                    if Path(path).name.startswith('before-submission-records-'):raise PermissionError('Synthetic backup creation denial')
                    return real_open(path,*args,**kwargs)
                def failed_connect(path,*args,**kwargs):
                    if Path(path).resolve()==fixture['service'].db_path:kwargs['factory']=BadBackup
                    return connect(path,*args,**kwargs)
                if mode=='create':
                    with patch('submission_records.os.open',side_effect=failed_open):self.failed_main(fixture['data'])
                else:
                    with patch('submission_records.sqlite3.connect',side_effect=failed_connect):self.failed_main(fixture['data'])
                self.assertEqual(snapshot(fixture['service'].db_path),fixture['before'])
                self.assertFalse(NEW_TABLES.intersection(snapshot(fixture['service'].db_path)['tables']))

    def test_each_migration_failure_rolls_back_ddl_backfill_and_both_markers(self):
        connect=sqlite3.connect
        boundaries=('CREATE TABLE IF NOT EXISTS submission_requests','INSERT INTO submission_history',
                    "INSERT OR IGNORE INTO submission_records_meta VALUES ('historyAvailableFrom'",'PRAGMA foreign_key_check')
        for index,boundary in enumerate(boundaries):
            with self.subTest(boundary=boundary):
                fixture=self.fixture('migration-failed-'+str(index))
                class FaultConnection(sqlite3.Connection):
                    def execute(self,sql,*values):
                        if sql.startswith(boundary):super().execute('SELECT missing_migration_function()')
                        return super().execute(sql,*values)
                def injected(path,*args,**kwargs):
                    if Path(path).resolve()==fixture['service'].db_path:kwargs['factory']=FaultConnection
                    return connect(path,*args,**kwargs)
                with patch('submission_records.sqlite3.connect',side_effect=injected):self.failed_main(fixture['data'])
                self.assertEqual(snapshot(fixture['service'].db_path),fixture['before'])
                backups=list(fixture['data'].glob('before-submission-records-*.sqlite3'))
                self.assertEqual(len(backups),1)
                self.assertEqual(snapshot(backups[0]),fixture['before'])
                self.private_valid(backups[0])
                recovered=self.service(fixture['data'])
                self.assertEqual(len(snapshot(recovered.db_path)['tables']['submission_history']),1)

    def test_old_writer_reupgrade_only_adds_latest_known_formal_without_restoring(self):
        fixture=self.fixture()
        upgraded=self.service(fixture['data'])
        with upgraded.db() as db:before_history=[tuple(row) for row in db.execute('SELECT * FROM submission_history')]
        # The pre-receipt write method cannot maintain new tables. An exact d047
        # source execution additionally validates this premise in release evidence.
        self.old_save(upgraded,fixture['cookie'],3,'submitted',self.materials(projectName='Lost formal v4'))
        self.old_save(upgraded,fixture['cookie'],4,'submitted',self.materials(projectName='Latest legacy formal v5'))
        self.old_save(upgraded,fixture['cookie'],5,'draft',self.materials(projectName='Later private draft v6'))
        before=snapshot(upgraded.db_path)
        self.assertEqual(before['tables']['submission_history'],before_history)
        again=self.service(fixture['data'])
        after=snapshot(again.db_path)
        for name,rows in before['tables'].items():
            if name!='submission_history':self.assertEqual(after['tables'][name],rows,name)
        history=after['tables']['submission_history']
        self.assertEqual(history[0],before_history[0])
        self.assertEqual([row[1] for row in history],[2,5])
        self.assertEqual(history[-1][4:],(None,'legacy_current'))
        self.assertEqual(json.loads(history[-1][2])['projectName'],'Latest legacy formal v5')
        self.assertEqual(self.call(again,'GET','/api/team',cookie=fixture['cookie'])['submission']['draft']['projectName'],'Later private draft v6')
        self.service(fixture['data'])
        self.assertEqual(snapshot(again.db_path),after)

    def test_backup_restore_and_installer_backup_include_all_new_records(self):
        fixture=self.fixture('modern',legacy=False)
        service=fixture['service']
        check=self.call(service,'POST','/api/admin/submissions/'+fixture['team']+'/checks',
            {'version':2,'field':'codeUrl','expectedRevision':0,'status':'accessible','note':'Synthetic manual check'},fixture['admin'])
        self.assertEqual(next(item for item in check['items'] if item['field']=='codeUrl')['revision'],1)
        before=snapshot(service.db_path)
        for table in NEW_TABLES:self.assertTrue(before['tables'][table],table)
        backup_tool=load_module('gotc_records_backup_tool',ROOT/'scripts/backup_event.py')
        backup=self.root/'exports'/'backup.sqlite3'
        report=backup_tool.copy_database(service.db_path,backup)
        restored=self.root/'restored'/'event.sqlite3'
        backup_tool.copy_database(backup,restored)
        self.assertEqual(snapshot(backup),before)
        self.assertEqual(snapshot(restored),before)
        self.assertEqual(report['rows']['submission_requests'],3)
        self.private_valid(backup);self.private_valid(restored)
        with self.assertRaises(FileExistsError):backup_tool.copy_database(backup,restored)
        restored_service=self.service(restored.parent)
        self.assertEqual(snapshot(restored_service.db_path),before)
        self.assertEqual(self.call(restored_service,'GET','/api/submission/requests/synthetic-request-1',cookie=fixture['cookie'])['version'],2)
        restored_checks=self.call(restored_service,'GET','/api/admin/submissions/'+fixture['team']+'/checks',cookie=fixture['admin'],query={'version':'2'})
        self.assertEqual(next(item for item in restored_checks['items'] if item['field']=='codeUrl')['note'],'Synthetic manual check')
        installer=load_module('gotc_records_installer',ROOT/'deploy/install.py')
        tree=self.root/'installer'
        for name in ('incoming','releases','backups','data'):(tree/name).mkdir(parents=True,exist_ok=True)
        (tree/'.gotc-owned').write_text('synthetic isolated installer')
        old=tree/'releases'/'old';old.mkdir()
        (tree/'current').symlink_to(old,target_is_directory=True)
        backup_tool.copy_database(service.db_path,tree/'data'/'event.sqlite3')
        nginx=self.root/'nginx.conf';nginx.write_text('server { listen 443; server_name 8.141.114.201; }\n')
        unit=self.root/'unit';unit.write_text('synthetic old unit\n')
        package=tree/'incoming'/'test.tar.gz'
        with tarfile.open(package,'w:gz') as archive:
            for name in sorted(installer.FILES|{'server/app.py','server/submission_records.py'}):
                archive.add(ROOT/name,arcname=name)
        release='20261101T000000Z-records'
        argv=['install.py','--archive',str(package),'--sha256',hashlib.sha256(package.read_bytes()).hexdigest(),'--release',release,'--source-sha','a'*40]
        class Result:returncode=0
        with patch.object(installer,'ROOT',tree),patch.object(installer,'NGINX',nginx),patch.object(installer,'UNIT',unit),patch.object(installer,'run',return_value=Result()),patch.object(installer,'health',return_value={'teams':1,'serverTime':1}),patch.object(installer.os,'geteuid',return_value=0),patch.object(sys,'argv',argv),redirect_stdout(io.StringIO()):
            installer.main()
        installed_backup=tree/'backups'/release/'event.sqlite3'
        self.assertEqual(snapshot(installed_backup),before)
        self.private_valid(installed_backup)
        self.assertEqual(snapshot(tree/'data'/'event.sqlite3'),before)


if __name__=='__main__':unittest.main()
