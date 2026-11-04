"""Judging-framework HTTP regressions; all records and databases are synthetic."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
import json
import sqlite3
import threading
import unittest
from unittest.mock import patch

import app
from app import EventService
import test_api as api_helpers


class ExhibitionFrameworkTests(unittest.TestCase):
    setUp=api_helpers.APITests.setUp
    tearDown=api_helpers.APITests.tearDown
    team=api_helpers.APITests.team
    invitation=api_helpers.APITests.invitation

    stage_path='/api/admin/judging/exhibition'
    judge_path='/api/judging/exhibition'

    def admin_stage(self):
        status,data,_=self.admin.get(self.stage_path)
        self.assertEqual(status,200,data)
        return data

    def configure(self, team_ids=None, judge_ids=None):
        if team_ids is None:
            team_ids=[self.team()]
        if judge_ids is None:
            judge,invitation=self.invitation()
            self.judge=judge
            judge_ids=[invitation['id']]
        current=self.admin_stage()
        status,data,_=self.admin.post(self.stage_path+'/roster',{
            'expectedRevision':current['stage']['revision'],'teamIds':team_ids,'judgeIds':judge_ids})
        self.assertEqual(status,200,data)
        return team_ids,judge_ids,data

    def window(self, opened=True, deadline=None):
        before=self.admin_stage()
        status,data,_=self.admin.post(self.stage_path+'/window',{
            'expectedRevision':before['stage']['revision'],'open':opened,'deadline':deadline})
        self.assertEqual(status,200,data)
        return data

    def review(self, client, team_id, version=0, status='submitted', feedback='Synthetic review', **extra):
        run_id=extra.get('runId') or client.get(self.judge_path)[1]['stage']['runId']
        return client.post(self.judge_path+'/review',{
            'runId':run_id,'teamId':team_id,'expectedVersion':version,
            'status':status,'feedback':feedback,'scores':None,**extra})

    def my_review(self, client, team_id):
        status,data,_=client.get(self.judge_path)
        self.assertEqual(status,200,data)
        return next(row for row in data['myReviews'] if row['teamId']==team_id)

    def test_initial_rules_are_explicit_placeholders_and_publication_is_blocked(self):
        data=self.admin_stage()
        stage=data['stage']
        self.assertEqual(stage['id'],'exhibition')
        self.assertEqual(stage['revision'],0)
        self.assertTrue(stage['rosterEditable'])
        self.assertEqual(stage['rulesStatus'],'unconfigured')
        self.assertIsInstance(stage['runId'],str)
        for name in ['minValidJudges','aggregation','advancementQuota']:
            self.assertIsNone(stage[name],name)
        self.assertTrue(stage['dimensions'])
        self.assertTrue(all(row['max'] is None for row in stage['dimensions']))
        self.assertEqual(stage['capabilities'],{
            'reviewRecords':True,'numericScoring':False,'aggregation':False,'ranking':False,'publication':False})
        self.assertFalse(data['window']['open'])
        before={key:data[key] for key in ['stage','roster','reviews']}
        status,result,_=self.admin.post(self.stage_path+'/publish',{'runId':stage['runId']})
        self.assertEqual(status,409,result)
        self.assertIn('error',result)
        after=self.admin_stage()
        self.assertEqual({key:after[key] for key in before},before)

    def test_role_access_roster_permissions_and_revision_conflicts(self):
        team_id=self.team()
        judge,invitation=self.invitation()
        outsider,_=self.invitation(name='Unassigned judge')
        audience,_=self.invitation('audience','Synthetic audience')
        for client,expected in [(self.public,401),(judge,403),(audience,403),(self.admin,403)]:
            self.assertEqual(client.get(self.judge_path)[0],expected)
        for client,expected in [(self.public,401),(judge,403),(audience,403)]:
            self.assertEqual(client.get(self.stage_path)[0],expected)
            self.assertEqual(client.post(self.stage_path+'/roster',{'expectedRevision':0,'teamIds':[],'judgeIds':[]})[0],expected)
        _,_,configured=self.configure([team_id],[invitation['id']])
        self.assertEqual(configured['stage']['revision'],1)
        self.assertEqual(judge.get(self.judge_path)[0],200)
        self.assertEqual(outsider.get(self.judge_path)[0],403)
        self.assertEqual(self.admin.post(self.stage_path+'/roster',{
            'expectedRevision':0,'teamIds':[],'judgeIds':[]})[0],409)
        self.assertEqual(self.admin_stage()['stage']['revision'],1)
        self.assertEqual(self.review(judge,team_id)[0],409)
        self.window()
        self.assertEqual(self.review(outsider,team_id,runId=configured['stage']['runId'])[0],403)
        self.assertIn(self.review(judge,'missing')[0],[403,404])
        self.assertEqual(self.review(judge,team_id)[0],200)
        self.assertEqual(self.admin.post('/api/admin/revoke',{'role':'judge','id':invitation['id']})[0],200)
        self.assertEqual(judge.get(self.judge_path)[0],401)
        self.assertEqual(judge.post(self.judge_path+'/review',{})[0],401)

    def test_draft_formal_recusal_version_conflicts_and_restart_preserve_real_feedback(self):
        teams,_,_=self.configure()
        team_id=teams[0]
        self.window()
        self.assertEqual(self.review(self.judge,team_id,status='draft',feedback='')[0],200)
        row=self.my_review(self.judge,team_id)
        self.assertEqual(row['version'],1)
        self.assertEqual(row['draft'],{'feedback':''})
        self.assertIsNone(row['submitted'])
        self.assertFalse(row['hasUnsubmittedChanges'])
        self.assertEqual(self.review(self.judge,team_id,1,feedback='FORMAL_FIRST')[0],200)
        formal=self.my_review(self.judge,team_id)['submitted']
        self.assertEqual(formal['feedback'],'FORMAL_FIRST')
        self.assertIsInstance(formal['submittedAt'],int)
        self.assertEqual(self.review(self.judge,team_id,2,'draft','PRIVATE_DRAFT')[0],200)
        row=self.my_review(self.judge,team_id)
        self.assertEqual(row['submitted'],formal)
        self.assertEqual(row['draft'],{'feedback':'PRIVATE_DRAFT'})
        self.assertTrue(row['hasUnsubmittedChanges'])
        self.assertEqual(self.review(self.judge,team_id,2,feedback='Stale overwrite')[0],409)
        self.assertEqual(self.my_review(self.judge,team_id),row)
        self.assertEqual(self.review(self.judge,team_id,3,'recused','')[0],200)
        recused=self.my_review(self.judge,team_id)
        self.assertTrue(recused['recused'])
        self.assertEqual(recused['submitted'],formal)
        self.assertEqual(recused['draft'],row['draft'])
        self.assertEqual(self.review(self.judge,team_id,4,feedback='FORMAL_REVISED')[0],200)
        revised=self.my_review(self.judge,team_id)
        self.assertEqual(revised['version'],5)
        self.assertFalse(revised['recused'])
        self.assertFalse(revised['hasUnsubmittedChanges'])
        self.assertEqual(revised['submitted']['feedback'],'FORMAL_REVISED')
        previous=self.service
        self.service=EventService(data_dir=previous.data_dir,base_path=previous.base_path,
                                  origin=previous.origin,static_root=previous.root)
        self.server.service=self.service
        self.assertEqual(self.my_review(self.judge,team_id),revised)

    def test_validation_numeric_scores_are_rejected_without_silent_fake_results(self):
        teams,_,_=self.configure()
        team_id=teams[0]
        self.window()
        invalid=[{'feedback':''},{'feedback':'   '},{'feedback':'x'*2001},{'feedback':False},
                 {'expectedVersion':True},{'expectedVersion':-1},{'status':'other'},{'status':[]},
                 {'teamId':[]},{'unknown':'field'}]
        for change in invalid:
            with self.subTest(change=change):
                self.assertEqual(self.review(self.judge,team_id,**change)[0],400)
        for scores in [[0]*6,[],{},False,0]:
            with self.subTest(scores=scores):
                self.assertEqual(self.review(self.judge,team_id,scores=scores)[0],409)
        self.assertEqual(self.judge.get(self.judge_path)[1]['myReviews'],[])
        self.assertIn(self.review(self.judge,team_id,runId=False)[0],[400,409])
        self.assertEqual(self.review(self.judge,team_id,runId='stale-run')[0],409)

    def test_windows_and_materials_are_independent_and_public_data_stays_private(self):
        team_id=self.team()
        from test_submissions import SubmissionAPITests
        code_response=self.admin.post('/api/admin/team-access',{'teamId':team_id,'action':'issue'})[1]
        team_client=api_helpers.Client(self.server)
        self.assertEqual(team_client.post('/api/login',{'role':'team','code':code_response['code']})[0],200)
        self.assertEqual(self.admin.settings({'submissionsOpen':True})[0],200)
        materials=SubmissionAPITests.materials(self,projectName='FORMAL_CAPTURED',summary='PRIVATE_MATERIAL_SUMMARY')
        self.assertEqual(team_client.post('/api/submission',{'expectedVersion':0,'status':'submitted','materials':materials})[0],200)
        self.configure([team_id])
        self.window()
        frozen_team=self.judge.get(self.judge_path)[1]['teams'][0]
        self.assertFalse(self.admin_stage()['stage']['rosterEditable'])
        captured=frozen_team['materialSnapshot']
        self.assertEqual(captured['materials']['projectName'],'FORMAL_CAPTURED')
        self.assertEqual(captured['submissionVersion'],1)
        materials['projectName']='FORMAL_LATER'
        self.assertEqual(team_client.post('/api/submission',{'expectedVersion':1,'status':'submitted','materials':materials})[0],200)
        self.assertEqual(self.admin.post('/api/admin/teams',{
            'id':team_id,'number':1,'name':'Renamed after freeze','project':'FORMAL_LATER','track':'城市与社区问题'})[0],200)
        self.assertEqual(self.judge.get(self.judge_path)[1]['teams'][0]['materialSnapshot'],captured)
        self.window(False)
        current=self.admin_stage()
        self.assertFalse(current['stage']['rosterEditable'])
        self.assertEqual(self.admin.post(self.stage_path+'/roster',{
            'expectedRevision':current['stage']['revision'],'teamIds':[team_id],
            'judgeIds':current['roster']['judgeIds']})[0],409)
        self.window()
        self.assertEqual(self.judge.get(self.judge_path)[1]['teams'][0],frozen_team)
        self.assertEqual(self.review(self.judge,team_id,feedback='PRIVATE_REVIEW')[0],200)
        self.assertFalse(self.admin.get('/api/admin')[1]['config']['scoringOpen'])
        self.assertTrue(team_client.get('/api/team')[1]['window']['canEdit'])
        board=self.public.get(self.judge_path+'/board')[1]
        serialized=json.dumps(board)
        for secret in ['PRIVATE_REVIEW','PRIVATE_MATERIAL_SUMMARY','example.test','feedback','myReviews']:
            self.assertNotIn(secret,serialized)
        self.assertTrue(all(row.get('score') is None and row.get('rank') is None for row in board['teams']))
        self.window(False)
        for state in ['draft','submitted','recused']:
            self.assertEqual(self.review(self.judge,team_id,1,state)[0],409)
        self.assertTrue(team_client.get('/api/team')[1]['window']['canEdit'])
        self.window(True,0)
        self.assertFalse(self.judge.get(self.judge_path)[1]['window']['canEdit'])
        self.assertEqual(self.review(self.judge,team_id,1)[0],409)
        self.window(False)
        current=self.admin_stage()
        self.assertEqual(self.admin.post(self.stage_path+'/reset',{
            'expectedRevision':current['stage']['revision'],'reason':'Capture newly submitted materials for a new run'})[0],200)
        self.assertTrue(self.admin_stage()['stage']['rosterEditable'])
        self.window()
        refreshed=self.judge.get(self.judge_path)[1]['teams'][0]['materialSnapshot']
        self.assertEqual(refreshed['materials']['projectName'],'FORMAL_LATER')
        self.assertEqual(refreshed['submissionVersion'],2)
        refreshed_team=self.judge.get(self.judge_path)[1]['teams'][0]
        self.assertEqual(refreshed_team['name'],'Renamed after freeze')
        self.assertEqual(refreshed_team['track'],'城市与社区问题')

    def test_concurrent_edits_accept_exactly_one_version(self):
        teams,_,_=self.configure()
        self.window()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda feedback:self.review(self.judge,teams[0],feedback=feedback)[0],['CONCURRENT_A','CONCURRENT_B']))
        self.assertEqual(sorted(results),[200,409])
        row=self.my_review(self.judge,teams[0])
        self.assertEqual(row['version'],1)
        self.assertIn(row['submitted']['feedback'],['CONCURRENT_A','CONCURRENT_B'])

    def test_roster_validation_and_window_updates_are_atomic(self):
        team_id=self.team()
        judge,invitation=self.invitation()
        _,audience=self.invitation('audience','Wrong role')
        initial=self.admin_stage()
        self.assertEqual(self.admin.post(self.stage_path+'/window',{
            'expectedRevision':0,'open':True,'deadline':None})[0],409)
        invalid=[{'teamIds':[team_id,team_id],'judgeIds':[invitation['id']]},
                 {'teamIds':['missing'],'judgeIds':[invitation['id']]},
                 {'teamIds':[team_id],'judgeIds':[audience['id']]},
                 {'teamIds':[team_id],'judgeIds':[invitation['id'],invitation['id']]},
                 {'teamIds':'invalid','judgeIds':[]}]
        for values in invalid:
            with self.subTest(values=values):
                status=self.admin.post(self.stage_path+'/roster',{'expectedRevision':0,**values})[0]
                self.assertIn(status,[400,404])
                current=self.admin_stage()
                self.assertEqual(current['stage'],initial['stage'])
                self.assertEqual(current['roster'],initial['roster'])
        self.configure([team_id],[invitation['id']])
        for changes in [{'deadline':True},{'deadline':-1},{'deadline':1.5},
                        {'deadline':253402300800000},{'open':'true'},{'expectedRevision':True}]:
            with self.subTest(changes=changes):
                self.assertEqual(self.admin.post(self.stage_path+'/window',{
                    'expectedRevision':1,'open':True,'deadline':None,**changes})[0],400)
                self.assertEqual(self.admin_stage()['stage']['revision'],1)
        self.window()
        self.assertEqual(self.admin.post(self.stage_path+'/roster',{
            'expectedRevision':2,'teamIds':[],'judgeIds':[]})[0],409)
        self.assertEqual(self.review(judge,team_id)[0],200)
        self.window(False)
        revision=self.admin_stage()['stage']['revision']
        self.assertEqual(self.admin.post(self.stage_path+'/roster',{
            'expectedRevision':revision,'teamIds':[team_id],'judgeIds':[invitation['id']]})[0],409)

    def test_reset_retains_old_review_and_rejects_old_run_writes(self):
        teams,_,_=self.configure()
        self.window()
        old=self.judge.get(self.judge_path)[1]
        self.assertEqual(self.review(self.judge,teams[0],feedback='OLD_RUN_RETAINED')[0],200)
        before=self.admin_stage()
        self.assertEqual(self.admin.post(self.stage_path+'/reset',{
            'expectedRevision':before['stage']['revision'],'reason':'Must close before resetting'})[0],409)
        after=self.admin_stage()
        self.assertEqual(after['stage'],before['stage'])
        self.assertEqual(after['reviews'],before['reviews'])
        self.window(False)
        before=self.admin_stage()
        for reason in ['', ' ', 'x'*2001, False]:
            self.assertEqual(self.admin.post(self.stage_path+'/reset',{
                'expectedRevision':before['stage']['revision'],'reason':reason})[0],400)
        status,result,_=self.admin.post(self.stage_path+'/reset',{
            'expectedRevision':before['stage']['revision'],'reason':'Synthetic reset for reviewed correction'})
        self.assertEqual(status,200,result)
        self.assertNotEqual(result['stage']['runId'],old['stage']['runId'])
        self.assertEqual(result['stage']['revision'],before['stage']['revision']+1)
        self.assertEqual(result['roster'],before['roster'])
        self.assertFalse(result['window']['open'])
        self.assertEqual(self.judge.get(self.judge_path)[1]['myReviews'],[])
        with self.service.db() as db:
            dump='\n'.join(db.iterdump())
        self.assertIn('OLD_RUN_RETAINED',dump)
        self.assertIn(old['stage']['runId'],dump)
        self.window()
        self.assertEqual(self.review(self.judge,teams[0],1,runId=old['stage']['runId'])[0],409)
        self.assertEqual(self.review(self.judge,teams[0],feedback='NEW_RUN_REVIEW')[0],200)
        self.assertEqual(self.my_review(self.judge,teams[0])['version'],1)

    def test_legacy_scores_remain_untouched_and_are_not_exhibition_reviews(self):
        team_id=self.team()
        judge,invitation=self.invitation()
        self.assertEqual(self.admin.settings({'scoringOpen':True,'minJudges':1})[0],200)
        self.assertEqual(judge.score({'teamId':team_id,'scores':[20,20,20,15,15,10],
                                               'feedback':'LEGACY_ONLY','status':'submitted'})[0],200)
        with self.service.db() as db:
            before=[tuple(row) for row in db.execute('SELECT * FROM scores')]
        self.configure([team_id],[invitation['id']])
        self.window()
        self.assertEqual(judge.get(self.judge_path)[1]['myReviews'],[])
        self.assertEqual(self.review(judge,team_id,feedback='EXHIBITION_ONLY')[0],200)
        with self.service.db() as db:
            self.assertEqual([tuple(row) for row in db.execute('SELECT * FROM scores')],before)
        self.assertEqual(judge.get('/api/judge')[1]['myScores'][0]['feedback'],'LEGACY_ONLY')
        board=self.public.get(self.judge_path+'/board')[1]
        self.assertTrue(all(row.get('score') is None and row.get('rank') is None for row in board['teams']))

    def test_judges_only_see_own_reviews_and_public_counts_follow_formal_recusal_state(self):
        team_id=self.team()
        first,first_invite=self.invitation(name='PRIVATE_JUDGE_FIRST')
        second,second_invite=self.invitation(name='PRIVATE_JUDGE_SECOND')
        self.configure([team_id],[first_invite['id'],second_invite['id']])
        self.window()
        def count():
            status,board,_=self.public.get(self.judge_path+'/board')
            self.assertEqual(status,200)
            self.assertNotIn('PRIVATE_',json.dumps(board))
            return board['teams'][0]['reviewCount']
        self.assertEqual(self.review(first,team_id,status='draft',feedback='PRIVATE_FIRST')[0],200)
        self.assertEqual(count(),0)
        self.assertEqual(self.review(first,team_id,1,feedback='PRIVATE_FORMAL_FIRST')[0],200)
        self.assertEqual(count(),1)
        self.assertEqual(self.review(second,team_id,feedback='PRIVATE_FORMAL_SECOND')[0],200)
        self.assertEqual(count(),2)
        self.assertNotIn('PRIVATE_FORMAL_SECOND',json.dumps(first.get(self.judge_path)[1]))
        self.assertNotIn('PRIVATE_FORMAL_FIRST',json.dumps(second.get(self.judge_path)[1]))
        self.assertEqual(self.review(first,team_id,2,'recused','')[0],200)
        self.assertEqual(count(),1)
        self.assertEqual(self.review(first,team_id,3,'draft','PRIVATE_REOPENED')[0],200)
        self.assertEqual(count(),2)

    def test_deadline_is_strict_and_reopen_preserves_records(self):
        teams,_,_=self.configure()
        deadline=app.now_ms()+100000
        self.window(True,deadline)
        with patch('app.now_ms',return_value=deadline-1):
            self.assertTrue(self.judge.get(self.judge_path)[1]['window']['canEdit'])
            self.assertEqual(self.review(self.judge,teams[0],status='draft',feedback='Before deadline')[0],200)
        for now in [deadline,deadline+1]:
            with patch('app.now_ms',return_value=now):
                self.assertFalse(self.judge.get(self.judge_path)[1]['window']['canEdit'])
                self.assertEqual(self.review(self.judge,teams[0],1)[0],409)
        self.window(True,deadline+100000)
        self.assertEqual(self.my_review(self.judge,teams[0])['version'],1)
        self.assertEqual(self.review(self.judge,teams[0],1)[0],200)

    def test_queued_review_rechecks_window_or_session_after_lock(self):
        teams,judges,_=self.configure()
        self.window()
        run_id=self.judge.get(self.judge_path)[1]['stage']['runId']
        original_db=self.service.db
        for action,expected_status in [('close',409),('revoke',401)]:
            with self.subTest(action=action):
                if action=='revoke':
                    self.window()
                queued=threading.Event()
                @contextmanager
                def traced_db():
                    with original_db() as db:
                        db.set_trace_callback(lambda sql:queued.set() if sql=='BEGIN IMMEDIATE' else None)
                        yield db
                result=[]
                payload={'runId':run_id,'teamId':teams[0],'expectedVersion':0,
                         'status':'submitted','feedback':'Queued record must be rejected','scores':None}
                with closing(sqlite3.connect(self.service.db_path)) as lock:
                    lock.row_factory=sqlite3.Row
                    lock.execute('BEGIN IMMEDIATE')
                    with patch.object(self.service,'db',traced_db):
                        thread=threading.Thread(target=lambda:result.append(self.judge.post(self.judge_path+'/review',payload)))
                        thread.start()
                        self.assertTrue(queued.wait(3),'Review did not queue for the database write lock')
                        if action=='close':
                            actor=lock.execute("SELECT * FROM accounts WHERE role='admin'").fetchone()
                            state=self.service.judging.state(lock,'exhibition')
                            self.service.judging.set_window(lock,actor,state,{
                                'expectedRevision':state['revision'],'open':False,'deadline':None})
                        else:
                            lock.execute('UPDATE accounts SET active=0 WHERE id=?',(judges[0],))
                            lock.execute('DELETE FROM sessions WHERE account_id=?',(judges[0],))
                        lock.commit()
                        thread.join(5)
                        self.assertFalse(thread.is_alive())
                self.assertEqual(result[0][0],expected_status,result)
                self.assertEqual(self.admin_stage()['reviews'],[])


if __name__=='__main__':
    unittest.main(verbosity=2)
