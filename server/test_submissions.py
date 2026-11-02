"""Submission regressions using synthetic data and disposable databases only."""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from app import EventService, SCHEMA, scrypt_hash
import hashlib


class SubmissionMigrationTests(unittest.TestCase):
    def test_legacy_accounts_migration_preserves_all_relations_and_credentials(self):
        with tempfile.TemporaryDirectory(prefix="gotc-migration-") as directory:
            data=Path(directory)
            legacy=SCHEMA.replace("'admin','judge','audience','team'", "'admin','judge','audience'")
            with closing(sqlite3.connect(data/"event.sqlite3")) as db:
                db.executescript(legacy)
                db.execute("INSERT INTO accounts VALUES (?,?,?,?,?,?,?)", ("admin-old", "admin", "Synthetic admin", b"salt", b"hash", 1, 1))
                db.execute("INSERT INTO accounts VALUES (?,?,?,?,?,?,?)", ("judge-old", "judge", "Synthetic judge", b"salt2", scrypt_hash("judge-old.fixture", b"salt2"), 1, 2))
                db.execute("INSERT INTO accounts VALUES (?,?,?,?,?,?,?)", ("audience-old", "audience", "Synthetic audience", b"salt3", b"hash3", 1, 3))
                db.execute("INSERT INTO teams VALUES ('Told',1,'Synthetic team','Old project','Test track',1)")
                db.execute("INSERT INTO sessions VALUES (?,'judge-old',9999999999999)", (hashlib.sha256(("x"*43).encode()).hexdigest(),))
                db.execute("INSERT INTO scores VALUES ('judge-old','Told','[1,2,3,4,5,6]',210,'Old feedback','submitted',4)")
                db.execute("INSERT INTO votes VALUES ('audience-old','Told',5)")
                db.execute("INSERT INTO audit VALUES (1,6,'admin-old','admin','fixture','Told','{}')")
                db.execute("INSERT INTO settings VALUES ('scoringOpen','true')")
                db.commit()
                before={table:db.execute("SELECT * FROM "+table).fetchall() for table in ("accounts","teams","sessions","scores","votes","audit")}
            for _ in range(2):
                service=EventService(data_dir=data, origin="http://127.0.0.1")
                with service.db() as db:
                    for table,rows in before.items():
                        self.assertEqual([tuple(row) for row in db.execute("SELECT * FROM "+table)], rows, table)
                    self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
                    self.assertEqual(db.execute("PRAGMA foreign_keys").fetchone()[0], 1)
                    self.assertTrue(service.config(db)["scoringOpen"])
                    self.assertFalse(service.config(db)["submissionsOpen"])
                    self.assertIsNone(service.config(db)["submissionDeadline"])
                    self.assertIn("'team'", db.execute("SELECT sql FROM sqlite_master WHERE name='accounts'").fetchone()[0])
                    for table in ("sessions","scores","votes"):
                        self.assertIn("accounts", [row[2] for row in db.execute("PRAGMA foreign_key_list("+table+")")])
            self.assertEqual(service.dispatch("GET", "/api/judge", {}, "gotc_session="+"x"*43, "127.0.0.1")[0]["myScores"][0]["teamId"], "Told")
            self.assertEqual(service.dispatch("POST", "/api/login", {"role":"judge","code":"judge-old.fixture"}, "", "127.0.0.1")[0]["role"], "judge")
            backups=list(data.glob("*before-team*.sqlite3"))
            self.assertEqual(len(backups), 1)
            with closing(sqlite3.connect(backups[0])) as backup:
                self.assertNotIn("'team'", backup.execute("SELECT sql FROM sqlite_master WHERE name='accounts'").fetchone()[0])
                self.assertEqual(backup.execute("SELECT * FROM accounts").fetchall(), before["accounts"])

    def test_failed_rebuild_after_drop_rolls_back_and_keeps_credentials(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory(prefix="gotc-migration-failure-") as directory:
            data=Path(directory)
            path=data/"event.sqlite3"
            access=data/"admin-access.txt"
            access.write_text("Synthetic credential unchanged",encoding="utf-8")
            with closing(sqlite3.connect(path)) as db:
                db.executescript(SCHEMA.replace("'admin','judge','audience','team'", "'admin','judge','audience'"))
                db.execute("INSERT INTO accounts VALUES ('A','admin','Synthetic',X'01',X'02',1,1)")
                db.execute("INSERT INTO sessions VALUES ('S','A',9999999999999)")
                db.commit()
                original=list(db.iterdump())
            connect=sqlite3.connect
            class FailingConnection:
                def __init__(self, db): self.db=db
                def __getattr__(self, key): return getattr(self.db,key)
                def execute(self, sql, *args):
                    if sql.startswith("ALTER TABLE accounts_new"):
                        raise sqlite3.OperationalError("synthetic failure after DROP")
                    return self.db.execute(sql,*args)
            def injected_connect(database, *args, **kwargs):
                db=connect(database,*args,**kwargs)
                return FailingConnection(db) if Path(database).resolve()==path.resolve() else db
            with patch('app.sqlite3.connect',side_effect=injected_connect):
                with self.assertRaisesRegex(sqlite3.OperationalError,'synthetic failure'):
                    EventService(data_dir=data,origin="http://127.0.0.1")
            self.assertEqual(access.read_text(),"Synthetic credential unchanged")
            with closing(connect(path)) as db:
                self.assertEqual(list(db.iterdump()),original)
                self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(),[])
                self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='accounts_new'").fetchone())
            self.assertEqual(len(list(data.glob('*before-team*.sqlite3'))),1)

import csv
import io
import json
from unittest.mock import patch
import app
import test_api as api_helpers


class SubmissionAPITests(unittest.TestCase):
    setUp=api_helpers.APITests.setUp
    tearDown=api_helpers.APITests.tearDown
    team=api_helpers.APITests.team
    invitation=api_helpers.APITests.invitation

    def access(self, team_id):
        status,access,_=self.admin.post('/api/admin/team-access', {'teamId':team_id,'action':'issue'})
        self.assertEqual(status,200,access)
        client=api_helpers.Client(self.server)
        self.assertEqual(client.post('/api/login',{'role':'team','code':access['code']})[0],200)
        return client,access

    def open_submissions(self, **settings):
        result=self.admin.post('/api/admin/settings',{'submissionsOpen':True,**settings})
        self.assertEqual(result[0],200,result[1])

    def materials(self, **changes):
        return {'projectName':'Synthetic Project','summary':'A synthetic prototype', 'codeUrl':'https://github.com/synthetic/repository',
                'readmeUrl':'https://example.test/README','license':'MIT','licenseUrl':'', 'demoUrl':'',
                'demoInstructions':'Run locally following the README','slidesUrl':'https://example.test/slides',
                'videoUrl':'https://example.test/video','sourcesAndLimitations':'No third-party assets. Synthetic demo only.',
                'codeVersion':'a'*40,'confirmed':True,**changes}

    def save(self, client, version=0, status='submitted', materials=None, **extra):
        return client.post('/api/submission',{'expectedVersion':version,'status':status,'materials':self.materials() if materials is None else materials,**extra})

    def test_team_access_isolated_rotated_and_revoked_without_losing_materials(self):
        first=self.team(1,'Synthetic One')
        second=self.team(2,'Synthetic Two')
        one,access=self.access(first)
        two,_=self.access(second)
        self.open_submissions()
        self.assertEqual(self.save(one)[0],200)
        self.assertEqual(two.get('/api/team')[1]['submission']['version'],0)
        for path in ['/api/admin','/api/judge','/api/audience','/api/admin/export','/api/admin/submissions/export']:
            self.assertEqual(one.get(path)[0],403,path)
            self.assertEqual(self.public.get(path)[0],401,path)
        self.assertEqual(self.public.get('/api/team')[0],401)
        self.assertEqual(self.admin.get('/api/team')[0],403)
        self.assertEqual(self.save(one,1,teamId=second)[0],400)
        self.assertEqual(two.get('/api/team')[1]['submission']['version'],0)
        self.assertEqual(self.admin.post('/api/admin/team-access',{'teamId':first,'action':'issue'})[0],409)
        status,rotated,_=self.admin.post('/api/admin/team-access',{'teamId':first,'action':'rotate'})
        self.assertEqual(status,200)
        self.assertEqual(rotated['id'],access['id'])
        self.assertNotEqual(rotated['code'],access['code'])
        self.assertEqual(one.get('/api/team')[0],401)
        self.assertEqual(one.post('/api/login',{'role':'team','code':access['code']})[0],401)
        self.assertEqual(one.post('/api/login',{'role':'team','code':rotated['code']})[0],200)
        self.assertEqual(one.get('/api/team')[1]['submission']['submitted']['projectName'],'Synthetic Project')
        self.assertEqual(self.admin.post('/api/admin/team-access',{'teamId':first,'action':'revoke'})[0],200)
        self.assertEqual(one.get('/api/team')[0],401)
        self.assertEqual(one.post('/api/login',{'role':'team','code':rotated['code']})[0],401)
        with self.service.db() as db:
            dump='\n'.join(db.iterdump())
            self.assertNotIn(access['code'],dump)
            self.assertNotIn(rotated['code'],dump)

    def test_draft_formal_snapshot_conflicts_restart_and_public_privacy(self):
        team_id=self.team()
        client,_=self.access(team_id)
        judge,_=self.invitation()
        audience,_=self.invitation('audience','Synthetic audience')
        self.open_submissions()
        self.assertEqual(self.save(client,status='draft',materials={'summary':'PRIVATE_DRAFT'})[0],200)
        self.assertEqual(judge.get('/api/judge')[1]['submissions'],[])
        self.assertEqual(self.save(client,1,materials={'summary':'Incomplete'})[0],400)
        self.assertEqual(client.get('/api/team')[1]['submission']['version'],1)
        status,result,_=self.save(client,1)
        self.assertEqual(status,200,result)
        self.assertEqual(result['submission']['submittedVersion'],2)
        first_time=result['submission']['firstSubmittedAt']
        self.assertEqual(self.public.get('/api/board')[1]['teams'][0]['project'],'Synthetic Project')
        changed=self.materials(projectName='PRIVATE_DRAFT_NEW',summary='PRIVATE_DRAFT_NEW')
        self.assertEqual(self.save(client,2,'draft',changed)[0],200)
        current=client.get('/api/team')[1]['submission']
        self.assertTrue(current['hasUnsubmittedChanges'])
        self.assertEqual(current['submitted']['projectName'],'Synthetic Project')
        self.assertEqual(judge.get('/api/judge')[1]['submissions'][0]['materials']['projectName'],'Synthetic Project')
        self.assertEqual(self.save(client,2,materials=self.materials(projectName='Stale'))[0],409)
        self.assertEqual(client.get('/api/team')[1]['submission'],current)
        self.assertEqual(self.public.get('/api/board')[1]['teams'][0]['project'],'Synthetic Project')
        self.service=EventService(data_dir=self.service.data_dir,origin=self.service.origin,static_root=self.service.root)
        self.server.service=self.service
        self.assertEqual(client.get('/api/team')[1]['submission'],current)
        for path,reader in [('/api/board',self.public),('/api/audience',audience),('/api/judge',judge)]:
            response=json.dumps(reader.get(path)[1])
            self.assertNotIn('PRIVATE_DRAFT',response)
            if path!='/api/judge':
                self.assertNotIn('example.test',response)
        status,result,_=self.save(client,3,materials=self.materials(projectName='Final revised'))
        self.assertEqual(status,200)
        self.assertEqual(result['submission']['submittedVersion'],4)
        self.assertEqual(result['submission']['firstSubmittedAt'],first_time)
        self.assertFalse(result['submission']['hasUnsubmittedChanges'])
        self.assertEqual(self.public.get('/api/board')[1]['teams'][0]['project'],'Final revised')

    def test_window_default_close_exact_deadline_and_reopen_are_independent(self):
        client,_=self.access(self.team())
        self.assertFalse(client.get('/api/team')[1]['window']['canEdit'])
        self.assertEqual(self.save(client)[0],409)
        deadline=app.now_ms()+100000
        self.open_submissions(submissionDeadline=deadline)
        self.assertFalse(self.admin.get('/api/admin')[1]['config']['scoringOpen'])
        with patch('app.now_ms',return_value=deadline-1):
            self.assertEqual(self.save(client,status='draft',materials={})[0],200)
        for current in [deadline,deadline+1]:
            with patch('app.now_ms',return_value=current):
                self.assertFalse(client.get('/api/team')[1]['window']['canEdit'])
                self.assertEqual(self.save(client,1)[0],409)
        self.assertEqual(self.admin.post('/api/admin/settings',{'scoringOpen':True})[0],200)
        self.assertEqual(self.admin.post('/api/admin/settings',{'submissionsOpen':False})[0],200)
        self.assertEqual(self.save(client,1)[0],409)
        self.open_submissions(submissionDeadline=deadline+200000)
        self.assertEqual(self.save(client,1)[0],200)
        before=self.admin.get('/api/admin')[1]['config']
        for value in [True,'tomorrow',-1,1.5,253402300800000]:
            self.assertEqual(self.admin.post('/api/admin/settings',{'scoringOpen':False,'submissionDeadline':value})[0],400)
            self.assertEqual(self.admin.get('/api/admin')[1]['config'],before)

    def test_material_validation_origin_and_body_limit(self):
        client,_=self.access(self.team())
        self.open_submissions()
        invalid=[{'confirmed':'true'},{'summary':3},{'summary':'x'*601},{'projectName':'x'*141},
                 {'unexpected':'field'},{'codeVersion':'x'*1025},{'license':'x'*201},
                 *[{'codeUrl':url} for url in ['javascript:alert(1)','data:text/html,abc','http://example.test','//example.test','https://u:p@example.test','https://@example.test','https://exa<mple.test','https://exa|mple.test','https://%40example.test','https://under_score.test',
                                                'https://example.test/a\\b','https://example.test/a b','https://example.test/%0aheader','https://[bad','https://example.test:bad',
                                                'https://example.test/'+('中'*400)]]]
        for materials in invalid:
            self.assertEqual(self.save(client,status='draft',materials=materials)[0],400,materials)
        for invalid_type in [[],{}]:
            self.assertEqual(client.post('/api/submission',{'expectedVersion':0,'status':invalid_type,'materials':{}})[0],400)
            self.assertEqual(self.admin.post('/api/admin/team-access',{'teamId':client.get('/api/team')[1]['team']['id'],'action':invalid_type})[0],400)
        self.assertEqual(client.get('/api/team')[1]['submission']['version'],0)
        for changes in [{'confirmed':False},{'demoUrl':'','demoInstructions':''},{'slidesUrl':''},{'videoUrl':''},{'sourcesAndLimitations':''}]:
            self.assertEqual(self.save(client,materials=self.materials(**changes))[0],400,changes)
        self.assertEqual(client.request('POST','/api/submission',{'expectedVersion':0,'status':'draft','materials':{}},origin='https://attacker.invalid')[0],403)
        large=self.materials(summary='中'*600,demoInstructions='中'*2000,sourcesAndLimitations='中'*2000,
                             codeUrl='https://github.com/synthetic/'+('a'*920),readmeUrl='https://example.test/'+('b'*950),slidesUrl='https://example.test/'+('c'*950))
        encoded=json.dumps({'expectedVersion':0,'status':'submitted','materials':large},ensure_ascii=False).encode()
        self.assertGreater(len(encoded),16384)
        self.assertLess(len(encoded),32768)
        self.assertEqual(self.save(client,materials=large)[0],200)
        self.assertEqual(client.request('POST','/api/submission',raw=b'x'*32769)[0],413)
        self.assertEqual(self.admin.request('POST','/api/admin/settings',raw=b'x'*16385)[0],413)
        self.assertEqual(self.save(client,True)[0],400)

    def test_admin_csv_labels_formula_safety_no_credentials_and_existing_export(self):
        team_id=self.team(name='=Synthetic formula')
        client,access=self.access(team_id)
        self.open_submissions()
        self.assertEqual(self.save(client,materials=self.materials(summary='=HYPERLINK("unsafe")'))[0],200)
        self.assertEqual(self.save(client,1,'draft',self.materials(summary='PRIVATE_DRAFT'))[0],200)
        data=self.admin.get('/api/admin')[1]
        record=data['submissions'][0]
        self.assertEqual(record['access'],{'id':access['id'],'active':True})
        self.assertTrue(record['submission']['hasUnsubmittedChanges'])
        self.assertNotIn(access['code'],json.dumps(data))
        status,raw,headers=self.admin.get('/api/admin/submissions/export')
        self.assertEqual(status,200)
        self.assertIn('gotc-submissions.csv',headers['Content-Disposition'])
        self.assertTrue(raw.startswith(b'\xef\xbb\xbf'))
        rows=list(csv.reader(io.StringIO(raw.decode('utf-8-sig'))))
        self.assertEqual(len(rows),2)
        self.assertEqual(rows[1][1],"'=Synthetic formula")
        self.assertEqual(rows[1][rows[0].index('正式_summary')],"'=HYPERLINK(\"unsafe\")")
        self.assertEqual(rows[1][rows[0].index('草稿_summary')],'PRIVATE_DRAFT')
        self.assertNotIn(access['code'],raw.decode())
        scores=self.admin.get('/api/admin/export')
        self.assertIn('gotc-scores.csv',scores[2]['Content-Disposition'])
        self.assertNotIn('草稿_',scores[1].decode())

    def test_simultaneous_edits_accept_only_one_version(self):
        from concurrent.futures import ThreadPoolExecutor
        team_id=self.team()
        client,_=self.access(team_id)
        other=api_helpers.Client(self.server)
        other.cookie=client.cookie
        self.open_submissions()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda pair:self.save(pair[0],status='draft',materials={'summary':pair[1]}),[(client,'A'),(other,'B')]))
        self.assertEqual(sorted(result[0] for result in results),[200,409])
        winner=next(result[1]['submission']['draft']['summary'] for result in results if result[0]==200)
        self.assertEqual(client.get('/api/team')[1]['submission']['draft']['summary'],winner)

    def test_queued_write_rechecks_session_after_rotation_or_revocation(self):
        from contextlib import contextmanager
        import threading
        for action,status in [('rotate','draft'),('revoke','submitted')]:
            with self.subTest(action=action):
                team_id=self.team(11 if action=='rotate' else 12)
                client,_=self.access(team_id)
                self.open_submissions()
                original_db=self.service.db
                queued=threading.Event()
                @contextmanager
                def traced_db():
                    with original_db() as db:
                        db.set_trace_callback(lambda statement:queued.set() if statement=='BEGIN IMMEDIATE' else None)
                        yield db
                result=[]
                with closing(sqlite3.connect(self.service.db_path)) as lock:
                    lock.row_factory=sqlite3.Row
                    lock.execute('BEGIN IMMEDIATE')
                    with patch.object(self.service,'db',traced_db):
                        thread=threading.Thread(target=lambda:result.append(self.save(client,status=status)))
                        thread.start()
                        self.assertTrue(queued.wait(3),'Submission did not queue for the write lock')
                        actor=lock.execute("SELECT * FROM accounts WHERE role='admin'").fetchone()
                        self.service.team_access(lock,actor,{'teamId':team_id,'action':action})
                        lock.commit()
                        thread.join(5)
                        self.assertFalse(thread.is_alive())
                self.assertEqual(result[0][0],401,result)
                with original_db() as db:
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM submissions WHERE team_id=?',(team_id,)).fetchone()[0],0)


if __name__=='__main__':
    unittest.main()
