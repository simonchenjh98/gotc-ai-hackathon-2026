"""Receipt/history/check HTTP regressions, using disposable synthetic data only."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import sqlite3
import threading
import unittest
from unittest.mock import patch
import test_submissions


class SubmissionRecordAPITests(unittest.TestCase):
    setUp=test_submissions.SubmissionAPITests.setUp
    tearDown=test_submissions.SubmissionAPITests.tearDown
    team=test_submissions.SubmissionAPITests.team
    invitation=test_submissions.SubmissionAPITests.invitation
    access=test_submissions.SubmissionAPITests.access
    open_submissions=test_submissions.SubmissionAPITests.open_submissions
    materials=test_submissions.SubmissionAPITests.materials

    def submit(self,client,request='request-test-000001',version=0,status='submitted',materials=None):
        return client.post('/api/submission',{'requestId':request,'expectedVersion':version,'status':status,'materials':self.materials() if materials is None else materials})

    def test_same_id_concurrent_replay_and_receipt_survives_later_materials_or_closed_window(self):
        client,_=self.access(self.team());self.open_submissions()
        with ThreadPoolExecutor(max_workers=3) as pool:results=list(pool.map(lambda _:self.submit(client),range(3)))
        self.assertTrue(all(status==200 for status,_,_ in results),results);self.assertEqual(sum(not body['replayed'] for _,body,_ in results),1)
        receipt=results[0][1]['receipt'];self.assertEqual(receipt['version'],1)
        with self.service.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM submission_history').fetchone()[0],1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM audit WHERE action='submission.submitted'").fetchone()[0],1)
        self.assertEqual(self.submit(client,'request-test-000002',1,materials=self.materials(projectName='LATEST'))[0],200)
        self.admin.post('/api/admin/settings',{'submissionsOpen':False});replay=self.submit(client)[1]
        self.assertTrue(replay['replayed']);self.assertEqual(replay['receipt'],receipt);self.assertEqual(replay['submission']['submitted']['projectName'],'LATEST')
        self.assertEqual(client.get('/api/submission/requests/request-test-000001')[1],receipt)

    def test_fingerprint_preserves_raw_values_but_ignores_json_key_order(self):
        client,_=self.access(self.team());self.open_submissions();raw=self.materials(projectName='  With spaces  ')
        first=self.submit(client,materials=raw)[1];self.assertEqual(first['receipt']['materials']['projectName'],'With spaces')
        body={'materials':dict(reversed(list(raw.items()))),'status':'submitted','expectedVersion':0,'requestId':'request-test-000001'}
        self.assertTrue(client.post('/api/submission',body)[1]['replayed'])
        conflict=self.submit(client,materials=self.materials(projectName='With spaces'))
        self.assertEqual(conflict[0],409);self.assertEqual(conflict[1]['code'],'request_id_conflict')
        self.assertEqual(client.get('/api/submission/requests/request-test-000001')[1],first['receipt'])
        self.assertEqual(self.submit(client,'request-test-000002',0,materials=raw)[1]['code'],'version_conflict')
        self.assertEqual(self.submit(client,'request-test-000003',1,materials=raw)[0],200)

    def test_rejected_results_commit_and_invalid_envelopes_do_not_reserve_ids(self):
        client,_=self.access(self.team());rejected=self.submit(client)
        self.assertEqual(rejected[0],409);self.assertEqual(rejected[1]['code'],'submission_closed')
        old=client.get('/api/submission/requests/request-test-000001')[1];self.assertEqual(old['status'],'rejected');self.open_submissions()
        self.assertEqual(self.submit(client)[0],409);self.assertEqual(client.get('/api/submission/requests/request-test-000001')[1],old)
        self.assertEqual(self.submit(client,materials=self.materials(summary='DIFFERENT'))[1]['code'],'request_id_conflict')
        bad=self.submit(client,'request-test-invalid2',materials=self.materials(codeVersion='short'));self.assertEqual(bad[0],400);self.assertEqual(bad[1]['code'],'materials_invalid')
        self.assertEqual(client.get('/api/submission/requests/request-test-invalid2')[1]['status'],'rejected')
        self.assertEqual(client.post('/api/submission',{'requestId':'request-envelope-0001','expectedVersion':False,'status':'draft','materials':{}})[0],400)
        self.assertEqual(client.get('/api/submission/requests/request-envelope-0001')[1]['status'],'not_found');self.assertEqual(client.get('/api/team')[1]['submission']['version'],0)

    def test_receipt_history_permissions_and_cross_team_not_found(self):
        one=self.team(1);first,_=self.access(one);second,_=self.access(self.team(2));judge,_=self.invitation();self.open_submissions()
        self.assertEqual(self.submit(first,materials=self.materials(summary='PRIVATE_HISTORY'))[0],200)
        self.assertEqual(second.get('/api/submission/requests/request-test-000001')[1]['status'],'not_found');self.assertEqual(second.get('/api/submission/history')[1]['items'],[])
        for caller,expected in [(self.public,401),(judge,403)]:
            for path in ['/api/submission/requests/request-test-000001','/api/submission/history','/api/admin/submissions/'+one+'/history']:self.assertEqual(caller.get(path)[0],expected)
        self.assertEqual(first.get('/api/admin/submissions/'+one+'/history')[0],403)
        self.assertEqual(self.admin.get('/api/admin/submissions/'+one+'/history/1')[1]['materials']['summary'],'PRIVATE_HISTORY')
        self.assertNotIn('PRIVATE_HISTORY',json.dumps(self.public.get('/api/board')[1]))
        rotated=self.admin.post('/api/admin/team-access',{'teamId':one,'action':'rotate'})[1]
        self.assertEqual(first.get('/api/submission/requests/request-test-000001')[0],401);self.assertEqual(self.submit(first)[0],401)
        renewed=test_submissions.api_helpers.Client(self.server)
        self.assertEqual(renewed.post('/api/login',{'role':'team','code':rotated['code']})[0],200)
        self.assertEqual(renewed.get('/api/submission/requests/request-test-000001')[1]['status'],'succeeded')
        self.admin.post('/api/admin/team-access',{'teamId':one,'action':'revoke'})
        self.assertEqual(renewed.get('/api/submission/requests/request-test-000001')[0],401);self.assertEqual(self.submit(renewed)[0],401)
        with self.service.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM submission_requests').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM submission_history').fetchone()[0],1)

    def test_history_pagination_drafts_and_database_immutability(self):
        client,_=self.access(self.team());self.open_submissions()
        for version,status in enumerate(['submitted','draft','submitted','draft','submitted']):self.assertEqual(self.submit(client,'history-request-'+str(version),version,status)[0],200)
        page=client.get('/api/submission/history?limit=2')[1];self.assertEqual([item['version'] for item in page['items']],[5,3]);self.assertEqual(page['nextBeforeVersion'],3)
        tail=client.get('/api/submission/history?beforeVersion=3&limit=2')[1];self.assertEqual([item['version'] for item in tail['items']],[1]);self.assertIsNone(tail['nextBeforeVersion'])
        self.assertNotIn('materials',page['items'][0]);self.assertEqual(client.get('/api/submission/history/2')[0],404)
        for suffix in ['limit=51','limit=0','unknown=x']:self.assertEqual(client.get('/api/submission/history?'+suffix)[0],400)
        for table in ['submission_history','submission_requests']:
            for sql in ['DELETE FROM '+table,'UPDATE '+table+' SET team_id=team_id']:
                with self.assertRaises(sqlite3.IntegrityError):
                    with self.service.db() as db:db.execute(sql)

    def test_manual_check_version_url_actor_validation_and_reset(self):
        team_id=self.team();client,_=self.access(team_id);self.open_submissions();self.submit(client);path='/api/admin/submissions/'+team_id+'/checks'
        before=self.admin.get(path+'?version=1')[1];self.assertEqual(len(before['items']),4);self.assertTrue(all(row['revision']==0 and row['actor'] is None for row in before['items']))
        body={'version':1,'field':'codeUrl','expectedRevision':0,'status':'accessible','note':'人工记录'};saved=self.admin.post(path,body)[1]['items'][0]
        self.assertEqual(saved['url'],self.materials()['codeUrl']);self.assertEqual(saved['revision'],1);self.assertEqual(saved['actor']['name'],'活动管理员');self.assertIsInstance(saved['checkedAt'],int)
        self.assertEqual(self.admin.post(path,body)[1]['code'],'check_revision_conflict')
        for extra in [{'note':'x'*501},{'field':'demoInstructions'},{'url':'https://evil.invalid'},{'actor':{'id':'bad'}},{'expectedRevision':True}]:self.assertEqual(self.admin.post(path,{**body,'expectedRevision':1,**extra})[0],400)
        self.assertEqual(client.get(path+'?version=1')[0],403);self.assertEqual(self.submit(client,'next-formal-request',1)[0],200)
        self.assertEqual(self.admin.post(path,{**body,'expectedRevision':1})[1]['code'],'check_version_conflict')
        self.assertEqual(self.admin.get(path+'?version=1')[1]['items'][0]['status'],'accessible');self.assertTrue(all(row['status']=='unchecked' and row['revision']==0 for row in self.admin.get(path+'?version=2')[1]['items']))

    def test_concurrent_checks_and_formal_advance_are_serialized(self):
        team_id=self.team();client,_=self.access(team_id);self.open_submissions();self.submit(client);path='/api/admin/submissions/'+team_id+'/checks';body={'version':1,'field':'codeUrl','expectedRevision':0,'status':'unavailable','note':'合成记录'}
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda _:self.admin.post(path,body),range(2)))
        self.assertEqual(sorted(result[0] for result in results),[200,409])
        entered=threading.Event();release=threading.Event();original=self.service.save_submission
        def held(*args):
            result=original(*args);entered.set();self.assertTrue(release.wait(3));return result
        with patch.object(self.service,'save_submission',side_effect=held),ThreadPoolExecutor(max_workers=2) as pool:
            advancing=pool.submit(self.submit,client,'advance-request-0001',1);self.assertTrue(entered.wait(3));checking=pool.submit(self.admin.post,path,{**body,'expectedRevision':1});release.set();self.assertEqual(advancing.result()[0],200);self.assertEqual(checking.result()[0],409)

    def test_history_or_receipt_failure_rolls_back_materials_audit_and_all_records(self):
        client,_=self.access(self.team());self.open_submissions();original=self.service.db
        for target in ['INSERT INTO submission_history','INSERT INTO submission_requests']:
            class FailingConnection:
                def __init__(self,db):self.db=db
                def __getattr__(self,name):return getattr(self.db,name)
                def execute(self,sql,*args):
                    if sql.startswith(target):raise sqlite3.OperationalError('synthetic transaction failure')
                    return self.db.execute(sql,*args)
            @contextmanager
            def failing_db():
                with original() as db:yield FailingConnection(db)
            with patch.object(self.service,'db',failing_db):self.assertEqual(self.submit(client)[0],500)
            self.assertEqual(client.get('/api/team')[1]['submission']['version'],0)
            with original() as db:
                for table in ['submission_history','submission_requests','submissions']:self.assertEqual(db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0],0,table)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM audit WHERE action='submission.submitted'").fetchone()[0],0)
