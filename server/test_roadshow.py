"""Cross-stage HTTP regressions using isolated temporary databases."""
import json
import unittest

from app import EventService
import test_api as api_helpers
import test_submissions


class RoadshowIsolationTests(unittest.TestCase):
    setUp=api_helpers.APITests.setUp
    tearDown=api_helpers.APITests.tearDown
    team=api_helpers.APITests.team
    invitation=api_helpers.APITests.invitation

    def stage(self, stage):
        status,data,_=self.admin.get('/api/admin/judging/'+stage)
        self.assertEqual(status,200,data)
        return data

    def roster(self, stage, teams, judges):
        if stage=='roadshow':
            judges=list(judges)
            while len(judges)<3:
                judges.append(self.invitation(name='Roadshow judge '+str(len(judges)))[1]['id'])
        status,data,_=self.admin.post('/api/admin/judging/'+stage+'/roster',{
            'expectedRevision':self.stage(stage)['stage']['revision'],'teamIds':teams,'judgeIds':judges})
        self.assertEqual(status,200,data)
        return data

    def window(self, stage, opened=True, deadline=None):
        status,data,_=self.admin.post('/api/admin/judging/'+stage+'/window',{
            'expectedRevision':self.stage(stage)['stage']['revision'],'open':opened,'deadline':deadline})
        self.assertEqual(status,200,data)
        return data

    def review(self, client, stage, team_id, version=0, state='submitted', feedback='Synthetic feedback', **extra):
        return client.post('/api/judging/'+stage+'/review',{
            'runId':self.stage(stage)['stage']['runId'],'teamId':team_id,'expectedVersion':version,
            'status':state,'feedback':feedback,'scores':None,**extra})

    def judge(self, client, stage):
        status,data,_=client.get('/api/judging/'+stage)
        self.assertEqual(status,200,data)
        return data

    def test_manual_worklists_are_independent_and_never_claim_advancement_or_scores(self):
        first=self.team()
        second=self.team(2,'Only in roadshow')
        judge,invitation=self.invitation()
        initial={stage:self.stage(stage) for stage in ['exhibition','roadshow']}
        self.assertNotEqual(initial['exhibition']['stage']['runId'],initial['roadshow']['stage']['runId'])
        self.roster('exhibition',[first],[invitation['id']])
        self.window('exhibition')
        self.assertEqual(self.review(judge,'exhibition',first,feedback='EXHIBITION_ONLY')[0],200)
        self.assertEqual(self.stage('roadshow')['roster'],{'teamIds':[],'judgeIds':[]})
        # An existing team does not need to appear in exhibition to enter the manual roadshow worklist.
        self.roster('roadshow',[second],[invitation['id']])
        self.window('roadshow')
        for stage,team_id in [('exhibition',first),('roadshow',second)]:
            data=self.stage(stage)
            metadata=data['stage']
            self.assertEqual(metadata['id'],stage)
            self.assertEqual(metadata['rosterSource'],'manual_worklist')
            self.assertEqual(metadata['advancementStatus'],'unimplemented')
            for name in ['minValidJudges','aggregation','advancementQuota']:
                self.assertIsNone(metadata[name])
            self.assertTrue(all(d['max'] is None for d in metadata['dimensions']))
            for feature in ['numericScoring','aggregation','ranking','publication']:
                self.assertFalse(metadata['capabilities'][feature])
            self.assertEqual(self.review(judge,stage,team_id,scores=[1]*6)[0],409)
            self.assertEqual(self.admin.post('/api/admin/judging/'+stage+'/publish',{
                'runId':metadata['runId']})[0],409)
            status,board,_=self.public.get('/api/judging/'+stage+'/board')
            self.assertEqual(status,200)
            self.assertEqual([row['id'] for row in board['teams']],[team_id])
            self.assertTrue(all(row['score'] is None and row['rank'] is None for row in board['teams']))
            self.assertNotIn('advanced',json.dumps(board))
            self.assertNotIn('EXHIBITION_ONLY',json.dumps(board))
        self.assertEqual(self.admin.get('/api/admin/judging/unknown')[0],404)

    def test_same_judge_team_drafts_formal_recusal_and_versions_do_not_cross_stages(self):
        team_id=self.team()
        judge,invitation=self.invitation()
        for stage in ['exhibition','roadshow']:
            self.roster(stage,[team_id],[invitation['id']])
            self.window(stage)
        self.assertEqual(self.review(judge,'exhibition',team_id,state='draft',feedback='E_DRAFT')[0],200)
        self.assertEqual(self.judge(judge,'roadshow')['myReviews'],[])
        self.assertEqual(self.review(judge,'roadshow',team_id,feedback='R_FORMAL')[0],200)
        roadshow_before=self.judge(judge,'roadshow')['myReviews']
        self.assertEqual(self.review(judge,'exhibition',team_id,1,feedback='E_FORMAL')[0],200)
        self.assertEqual(self.review(judge,'exhibition',team_id,2,'recused','')[0],200)
        self.assertEqual(self.judge(judge,'roadshow')['myReviews'],roadshow_before)
        exhibition_before=self.judge(judge,'exhibition')['myReviews']
        self.assertEqual(exhibition_before[0]['version'],3)
        self.assertTrue(exhibition_before[0]['recused'])
        self.assertEqual(self.review(judge,'roadshow',team_id,1,'draft','R_LATER_DRAFT')[0],200)
        self.assertEqual(self.judge(judge,'exhibition')['myReviews'],exhibition_before)
        roadshow=self.judge(judge,'roadshow')['myReviews'][0]
        self.assertEqual(roadshow['version'],2)
        self.assertEqual(roadshow['submitted']['feedback'],'R_FORMAL')
        self.assertEqual(roadshow['draft']['feedback'],'R_LATER_DRAFT')
        self.assertTrue(roadshow['hasUnsubmittedChanges'])
        self.assertFalse(roadshow['recused'])
        for stage,other in [('exhibition','roadshow'),('roadshow','exhibition')]:
            before=self.stage(stage)['reviews']
            self.assertEqual(self.review(judge,stage,team_id,runId=self.stage(other)['stage']['runId'])[0],409)
            self.assertEqual(self.stage(stage)['reviews'],before)
        self.assertEqual(self.review(judge,'roadshow',team_id,1,feedback='STALE')[0],409)
        self.assertEqual(self.public.get('/api/judging/exhibition/board')[1]['teams'][0]['reviewCount'],0)
        self.assertEqual(self.public.get('/api/judging/roadshow/board')[1]['teams'][0]['reviewCount'],1)
        previous=self.service
        self.service=EventService(data_dir=previous.data_dir,base_path=previous.base_path,
                                  origin=previous.origin,static_root=previous.root)
        self.server.service=self.service
        self.assertEqual(self.judge(judge,'exhibition')['myReviews'],exhibition_before)
        self.assertEqual(self.judge(judge,'roadshow')['myReviews'][0],roadshow)

    def test_stage_grants_rosters_and_window_deadlines_are_independent(self):
        first=self.team()
        second=self.team(2,'Other team')
        e_judge,e_invitation=self.invitation(name='EXHIBITION_JUDGE')
        r_judge,r_invitation=self.invitation(name='ROADSHOW_JUDGE')
        self.roster('exhibition',[first],[e_invitation['id']])
        self.roster('roadshow',[second],[r_invitation['id']])
        self.window('exhibition')
        self.window('roadshow',True,0)
        for client,allowed,denied,allowed_team,denied_team in [
            (e_judge,'exhibition','roadshow',first,second),
            (r_judge,'roadshow','exhibition',second,first)]:
            self.assertEqual(client.get('/api/judging/'+denied)[0],403)
            self.assertEqual(self.review(client,denied,denied_team)[0],403)
            self.assertIn(self.review(client,allowed,denied_team)[0],[403,404])
        self.assertTrue(self.judge(e_judge,'exhibition')['window']['canEdit'])
        self.assertFalse(self.judge(r_judge,'roadshow')['window']['canEdit'])
        self.assertEqual(self.review(r_judge,'roadshow',second)[0],409)
        self.assertEqual(self.review(e_judge,'exhibition',first)[0],200)
        self.window('exhibition',False)
        self.window('roadshow',False)
        self.window('roadshow')
        self.assertEqual(self.review(e_judge,'exhibition',first,1)[0],409)
        self.assertEqual(self.review(r_judge,'roadshow',second)[0],200)

    def test_each_stage_captures_its_own_latest_formal_material_and_keeps_it_frozen(self):
        team_id=self.team()
        judge,invitation=self.invitation()
        issued=self.admin.post('/api/admin/team-access',{'teamId':team_id,'action':'issue'})[1]
        client=api_helpers.Client(self.server)
        self.assertEqual(client.post('/api/login',{'role':'team','code':issued['code']})[0],200)
        self.assertEqual(self.admin.settings({'submissionsOpen':True})[0],200)
        def submit(version,name,state='submitted'):
            self.assertEqual(client.post('/api/submission',{'expectedVersion':version,'status':state,
                'materials':test_submissions.SubmissionAPITests.materials(self,projectName=name)})[0],200)
        submit(0,'FORMAL_A')
        self.roster('exhibition',[team_id],[invitation['id']])
        self.window('exhibition')
        captured_a=self.judge(judge,'exhibition')['teams'][0]
        submit(1,'FORMAL_B')
        submit(2,'PRIVATE_DRAFT_C','draft')
        self.roster('roadshow',[team_id],[invitation['id']])
        self.window('roadshow')
        captured_b=self.judge(judge,'roadshow')['teams'][0]
        self.assertEqual(captured_a['materialSnapshot']['materials']['projectName'],'FORMAL_A')
        self.assertEqual(captured_a['materialSnapshot']['submissionVersion'],1)
        self.assertEqual(captured_b['materialSnapshot']['materials']['projectName'],'FORMAL_B')
        self.assertEqual(captured_b['materialSnapshot']['submissionVersion'],2)
        submit(3,'FORMAL_D')
        for stage,captured in [('exhibition',captured_a),('roadshow',captured_b)]:
            self.window(stage,False)
            self.window(stage)
            self.assertEqual(self.judge(judge,stage)['teams'][0],captured)
            self.assertNotIn('PRIVATE_DRAFT_C',json.dumps(self.judge(judge,stage)))

    def test_reset_of_one_stage_preserves_the_other_stage_and_archived_feedback(self):
        team_id=self.team()
        judge,invitation=self.invitation()
        for stage in ['exhibition','roadshow']:
            self.roster(stage,[team_id],[invitation['id']])
            self.window(stage)
            self.assertEqual(self.review(judge,stage,team_id,feedback=stage+'_KEPT')[0],200)
        exhibition=self.stage('exhibition')
        old_roadshow=self.stage('roadshow')
        self.window('roadshow',False)
        current=self.stage('roadshow')
        status,new,_=self.admin.post('/api/admin/judging/roadshow/reset',{
            'expectedRevision':current['stage']['revision'],'reason':'Independent roadshow reset'})
        self.assertEqual(status,200,new)
        self.assertNotEqual(new['stage']['runId'],old_roadshow['stage']['runId'])
        self.assertEqual(new['reviews'],[])
        self.assertEqual(new['roster'],old_roadshow['roster'])
        after=self.stage('exhibition')
        for key in ['stage','roster','teams','reviews']:
            self.assertEqual(after[key],exhibition[key])
        self.assertTrue(after['window']['canEdit'])
        self.window('roadshow')
        self.assertEqual(self.review(judge,'roadshow',team_id,runId=old_roadshow['stage']['runId'])[0],409)
        self.assertEqual(self.review(judge,'roadshow',team_id,feedback='NEW_ROADSHOW')[0],200)
        self.assertEqual(self.judge(judge,'roadshow')['myReviews'][0]['version'],1)
        with self.service.db() as db:
            dump='\n'.join(db.iterdump())
        self.assertIn('roadshow_KEPT',dump)
        self.assertIn('exhibition_KEPT',dump)


if __name__=='__main__':
    unittest.main(verbosity=2)
