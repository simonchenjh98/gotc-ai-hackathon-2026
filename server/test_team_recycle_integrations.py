"""Recycle-aware current entry points; every fixture uses a synthetic temporary DB."""
from test_registration_fixtures import registration_fixture_create
import json
import sqlite3
import unittest

from errors import APIError
from judging import JudgingService
import test_api as api_helpers
import test_open_claims as claim_helpers
import test_auto_claims as auto_helpers
import test_roadshow_scoring as roadshow_helpers
import test_finals as finals_helpers


class RecycledJudgingTests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:')
        self.db.row_factory=sqlite3.Row
        self.db.executescript('''
            CREATE TABLE teams(id TEXT PRIMARY KEY,number INTEGER,name TEXT,project TEXT,track TEXT);
            CREATE TABLE accounts(id TEXT PRIMARY KEY,name TEXT,role TEXT,active INTEGER,created_at INTEGER);
            CREATE TABLE submissions(team_id TEXT,submitted_json TEXT,submitted_version INTEGER,submitted_at INTEGER);
            CREATE TABLE judging_review_validity(run_id TEXT,judge_id TEXT,team_id TEXT,invalidated INTEGER,reason TEXT);
            INSERT INTO teams VALUES ('old',1,'Synthetic Old','Synthetic Project','');
            INSERT INTO teams VALUES ('live',2,'Synthetic Live','Synthetic Project','');
            INSERT INTO accounts VALUES ('judge','Synthetic Judge','judge',1,1);
        ''')
        self.recycled=set()
        self.service=JudgingService(clock=lambda:100,text=lambda value,*args:value,
            audit=lambda *args:None,touch=lambda *args:None,
            is_team_recycled=lambda db,team_id:team_id in self.recycled)
        self.service.initialize(self.db)
        self.actor={'id':'admin','role':'admin'}
        self.judge={'id':'judge','role':'judge'}
        self.service.set_roster(self.db,self.actor,self.state(),{
            'expectedRevision':0,'teamIds':['old','live'],'judgeIds':['judge']})

    def tearDown(self):
        self.db.close()

    def state(self):
        return self.service.state(self.db,'exhibition')

    def window(self,opened):
        self.service.set_window(self.db,self.actor,self.state(),{
            'expectedRevision':self.state()['revision'],'open':opened,'deadline':None})

    def snapshots(self,run_id):
        return [tuple(row) for row in self.db.execute(
            'SELECT team_id,snapshot_json FROM judging_roster WHERE run_id=? ORDER BY team_id',(run_id,))]

    def test_direct_selection_rejects_recycled_team_without_changing_roster(self):
        self.recycled.add('old');before=self.snapshots(self.state()['run_id'])
        with self.assertRaises(APIError) as failure:
            self.service.set_roster(self.db,self.actor,self.state(),{
                'expectedRevision':self.state()['revision'],'teamIds':['old'],'judgeIds':['judge']})
        self.assertEqual((failure.exception.status,failure.exception.code),(409,'team_recycled'))
        self.assertEqual(self.snapshots(self.state()['run_id']),before)

    def test_first_open_preserves_recycled_snapshot_and_hides_current_targets(self):
        run_id=self.state()['run_id'];before=dict(self.snapshots(run_id))
        self.recycled.add('old')
        self.db.execute("UPDATE teams SET name='Synthetic Later Name' WHERE id='old'")
        self.window(True)
        self.assertEqual(dict(self.snapshots(run_id))['old'],before['old'])
        self.assertEqual([t['id'] for t in self.service.teams(self.db,run_id)],['old','live'])
        self.assertEqual([t['id'] for t in self.service.admin_view(self.db,self.state())['teams']],['old','live'])
        self.assertEqual([t['id'] for t in self.service.judge_view(self.db,self.judge,self.state())['teams']],['live'])
        self.assertEqual([t['id'] for t in self.service.board(self.db,self.state())['teams']],['live'])
        with self.assertRaises(APIError) as failure:
            self.service.save_review(self.db,self.judge,self.state(),{
                'runId':run_id,'teamId':'old','expectedVersion':0,'status':'submitted','feedback':'Synthetic','scores':None})
        self.assertEqual((failure.exception.status,failure.exception.code),(409,'team_recycled'))
        self.assertEqual(self.service.reviews(self.db,run_id),[])

    def test_reset_skips_only_new_roster_and_all_recycled_reset_can_be_empty(self):
        self.window(True);self.window(False)
        old=self.state()['run_id'];before=self.snapshots(old)
        self.recycled.update(['old','live'])
        self.service.reset(self.db,self.actor,self.state(),{
            'expectedRevision':self.state()['revision'],'reason':'Synthetic reset'})
        self.assertNotEqual(self.state()['run_id'],old)
        self.assertEqual(self.snapshots(old),before)
        self.assertEqual(self.service.teams(self.db,self.state()['run_id']),[])
        self.assertFalse(self.state()['open'])
        state_before=dict(self.state())
        with self.assertRaises(APIError) as failure:self.window(True)
        self.assertEqual((failure.exception.status,failure.exception.code),(409,'judging_no_available_teams'))
        self.assertEqual(dict(self.state()),state_before)
        self.window(False)

    def test_reopening_all_recycled_run_is_atomic_and_closing_remains_allowed(self):
        self.window(True)
        self.recycled.update(['old','live'])
        self.window(False)
        state_before=dict(self.state());before=self.snapshots(self.state()['run_id'])
        with self.assertRaises(APIError) as failure:self.window(True)
        self.assertEqual(failure.exception.code,'judging_no_available_teams')
        self.assertEqual(dict(self.state()),state_before)
        self.assertEqual(self.snapshots(self.state()['run_id']),before)


class TeamRecycleIntegrationTests(unittest.TestCase):
    setUp=claim_helpers.OpenClaimsTests.setUp
    tearDown=claim_helpers.OpenClaimsTests.tearDown
    admin_call=claim_helpers.OpenClaimsTests.admin_call
    bearer=claim_helpers.OpenClaimsTests.bearer
    roster=claim_helpers.OpenClaimsTests.roster
    application=claim_helpers.OpenClaimsTests.application
    approval=claim_helpers.OpenClaimsTests.approval
    def team(self,number=1,name="Synthetic Team",**changes):
        return api_helpers.APITests.team(self,number,name,**changes)

    def lifecycle_get(self,team_id):
        status,data,_=self.admin.request('GET','/api/admin/data/team?teamId='+team_id,
            headers={'X-GOTC-Login':self.admin.login_id})
        self.assertEqual(status,200,data)
        return data

    def recycle(self,team_id):
        detail=self.lifecycle_get(team_id)
        status,data,_=self.admin.request('POST','/api/admin/data/team/recycle',{
            'teamId':team_id,'expectedRevision':detail['team']['revision'],
            'recycleContext':detail['recycleContext'],'reason':'Synthetic recycle'},
            headers={'X-GOTC-Login':self.admin.login_id})
        self.assertEqual(status,200,data)
        return data

    def restore(self,team_id,name):
        detail=self.lifecycle_get(team_id)
        return self.admin.request('POST','/api/admin/data/team/restore',{
            'teamId':team_id,'expectedRevision':detail['team']['revision'],
            'name':name,'reason':'Synthetic restore'},headers={'X-GOTC-Login':self.admin.login_id})

    def approved(self,prefix,**changes):
        claim=self.application(prefix,**changes)
        status,result,_=self.admin_call('/approve',self.approval(claim))
        self.assertEqual(status,200,result)
        return claim,self.bearer(claim)[1]['team']

    def auto_snapshot(self):
        with self.service.db() as db:
            return json.dumps({table:[tuple(row) for row in db.execute('SELECT * FROM '+table+' ORDER BY id')]
                for table in ['claim_auto_config','claim_auto_people']},sort_keys=True).encode()

    def test_auto_review_candidate_change_preserves_trusted_data_and_configuration(self):
        entries=[{'id':str(i),'name':name,'school':school,'eligible':True,'notes':'Synthetic trusted fixture'}
            for i,(name,school) in enumerate([('Synthetic Auto Captain','School A'),
                ('Synthetic Auto One','School B'),('Synthetic Auto Two','School C')])]
        auto_helpers.AutoClaimsTests.source(self,entries)
        trusted=self.auto_snapshot()
        old_claim=self.application('Synthetic Auto')
        self.assertEqual(old_claim['status'],'approved')
        old=self.bearer(old_claim)[1]['team']
        self.recycle(old['id']);self.assertEqual(self.auto_snapshot(),trusted)
        self.assertEqual(self.restore(old['id'],old['name'])[0],200)
        self.assertEqual(self.auto_snapshot(),trusted)
        self.recycle(old['id'])
        replacement=self.application('Synthetic Auto')
        self.assertEqual(replacement['status'],'approved')
        self.assertNotEqual(self.bearer(replacement)[1]['team']['id'],old['id'])
        self.assertEqual(self.auto_snapshot(),trusted)
        self.assertEqual(self.restore(old['id'],'Synthetic Free Auto')[0],409)
        self.assertEqual(self.auto_snapshot(),trusted)

    def test_registration_limit_excludes_recycled_but_numbers_remain_reserved(self):
        with self.service.db() as db:
            db.executemany('INSERT INTO teams(id,number,name,project,track,updated_at) VALUES (?,?,?,\'\',\'\',1)',
                [('synthetic-'+str(i),i,'Synthetic Capacity '+str(i)) for i in range(1,201)])
        value={'name':'Synthetic New Registration','captain':'Synthetic Capacity Captain','school':'Synthetic Capacity School',
            'phone':'','targetSize':3,'members':['Synthetic Capacity One','Synthetic Capacity Two'],'project':'','summary':''}
        self.assertEqual(registration_fixture_create(self,{'team':value,'code':'c'*43})[0],409)
        self.recycle('synthetic-1')
        status,result,_=registration_fixture_create(self,{'team':value,'code':'c'*43})
        self.assertEqual(status,200,result);self.assertEqual(result['team']['number'],201)
        with self.service.db() as db:
            db.executemany('INSERT INTO teams(id,number,name,project,track,updated_at) VALUES (?,?,?,\'\',\'\',1)',
                [('synthetic-'+str(i),i,'Synthetic Capacity '+str(i)) for i in range(202,1000)])
            actor=db.execute("SELECT id FROM accounts WHERE role='admin'").fetchone()[0]
            # A valid synthetic tombstone for every preserved original number.
            db.executemany("""INSERT INTO team_recycle_events(team_id,deleted_at,deleted_by,delete_reason,
                original_status,original_status_reason,team_snapshot_json,roster_snapshot_json)
                VALUES (?,1,?,'Synthetic capacity','active','','{}','{}')""",
                [(row[0],actor) for row in db.execute('SELECT id FROM teams WHERE id<>?',('synthetic-1',))])
        status,result,_=registration_fixture_create(self,{'team':{**value,'name':'Synthetic Exhausted'},'code':'d'*43})
        self.assertEqual(status,409,result);self.assertIn('1–999',result['error'])

    def test_reused_name_and_people_approve_distinct_team_restore_cannot_take_people(self):
        claim,old=self.approved('Synthetic Original')
        with self.service.db() as db:
            members=[tuple(r) for r in db.execute('SELECT person_id,request_id FROM claim_memberships WHERE team_id=? ORDER BY person_id',(old['id'],))]
            request_before=tuple(db.execute('SELECT * FROM claim_requests WHERE id=?',(claim['requestId'],)).fetchone())
        self.recycle(old['id'])
        _,new=self.approved('Synthetic Original',teamName='  SYNTHETIC   ORIGINAL TEAM  ')
        self.assertNotEqual(new['id'],old['id']);self.assertNotEqual(new['number'],old['number'])
        with self.service.db() as db:
            self.assertEqual([r[0] for r in db.execute('SELECT person_id FROM claim_memberships WHERE team_id=? ORDER BY person_id',(new['id'],))],[r[0] for r in members])
            self.assertEqual(tuple(db.execute('SELECT * FROM claim_requests WHERE id=?',(claim['requestId'],)).fetchone()),request_before)
        for name in [old['name'],'Synthetic Free Replacement']:
            status,result,_=self.restore(old['id'],name)
            self.assertEqual(status,409,result)
            self.assertEqual(result.get('code'),'team_restore_conflict')
        self.assertIsNotNone(self.lifecycle_get(old['id'])['recycle'])

    def test_reused_normalized_name_different_people_can_restore_with_free_name(self):
        _,old=self.approved('Synthetic Alpha',teamName='Ｓｙｎｔｈｅｔｉｃ Alpha')
        self.recycle(old['id'])
        _,new=self.approved('Synthetic Beta',teamName='  synthetic   ALPHA  ')
        self.assertNotEqual(new['id'],old['id'])
        self.assertEqual(self.restore(old['id'],old['name'])[0],409)
        status,result,_=self.restore(old['id'],'Synthetic Free Restored')
        self.assertEqual(status,200,result)
        self.assertEqual(result['team']['id'],old['id']);self.assertEqual(result['team']['number'],old['number'])
        self.assertIsNone(result['recycle'])

    def test_ordinary_withdrawn_and_disqualified_names_still_block(self):
        for number,status in enumerate(['withdrawn','disqualified'],1):
            team_id=self.team(number,name='Synthetic '+status)
            detail=self.lifecycle_get(team_id)
            response=self.admin.post('/api/admin/teams',{'id':team_id,'expectedRevision':detail['team']['revision'],'status':status,'statusReason':'Synthetic ordinary status'})
            self.assertEqual(response[0],200,response[1])
            claim=self.application('Synthetic '+status,teamName='  SYNTHETIC '+status.upper()+'  ')
            self.assertEqual(self.admin_call('/approve',self.approval(claim))[0],409)

    def test_recycled_registration_releases_name_members_and_blocks_code_rotation(self):
        body={'name':'Synthetic Registration','captain':'Synthetic Captain','school':'Synthetic School',
            'phone':'','targetSize':3,'members':['Synthetic Captain','Synthetic One','Synthetic Two'],
            'project':'Synthetic Project','summary':'Synthetic summary'}
        status,result,_=registration_fixture_create(self,{'team':body,'code':'a'*43})
        self.assertEqual(status,200,result);team_id=result['team']['id']
        self.recycle(team_id)
        row=self.service.registration.row
        with self.service.db() as db:revision=row(db,team_id)['revision']
        status,result,_=self.admin.request('POST','/api/admin/registration/rotate',{
            'teamId':team_id,'expectedRevision':revision},headers={'X-GOTC-Login':self.admin.login_id})
        self.assertEqual(status,410,result);self.assertEqual(result.get('code'),'registration_code_retired')
        status,result,_=registration_fixture_create(self,{'team':body,'code':'b'*43})
        self.assertEqual(status,200,result);self.assertNotEqual(result['team']['id'],team_id)
        self.assertEqual(len(self.public.get('/api/registration')[1]['teams']),1)

    def test_registration_history_marks_recycle_only_for_admins(self):
        body={'name':'Synthetic History Registration','captain':'Synthetic History Captain','school':'Synthetic History School',
            'phone':'','targetSize':3,'members':['Synthetic History One','Synthetic History Two'],'project':'','summary':''}
        status,result,_=registration_fixture_create(self,{'team':body,'code':'h'*43})
        self.assertEqual(status,200,result);team_id=result['team']['id']
        self.assertNotIn('recycled',result['team'])
        self.assertNotIn('recycled',self.public.get('/api/registration')[1]['teams'][0])
        def history():
            status,data,_=self.admin.request('GET','/api/admin/registration',headers={'X-GOTC-Login':self.admin.login_id})
            self.assertEqual(status,200,data)
            return next(team for team in data['teams'] if team['id']==team_id)
        self.assertIs(history()['recycled'],False)
        with self.service.db() as db:db.execute("UPDATE team_details SET status='withdrawn' WHERE team_id=?",(team_id,))
        self.assertEqual(history()['status'],'withdrawn');self.assertIs(history()['recycled'],False)
        self.recycle(team_id)
        self.assertEqual(history()['status'],'withdrawn');self.assertIs(history()['recycled'],True)
        self.assertEqual(self.public.get('/api/registration')[1]['teams'],[])
        status,result,_=self.restore(team_id,'Synthetic Restored History Registration')
        self.assertEqual(status,200,result)
        self.assertEqual(history()['status'],'withdrawn');self.assertIs(history()['recycled'],False)

    def test_scoring_available_lists_hide_recycled_but_history_keeps_rows(self):
        team_id=self.team();live=self.team(2,name='Synthetic Live')
        self.recycle(team_id)
        with self.service.db() as db:
            finals=self.service.finals
            self.assertIn(team_id,[t['id'] for t in finals.teams(db)])
            self.assertNotIn(team_id,[t['id'] for t in finals.admin_view(db,finals.state(db))['availableTeams']])
            state=self.service.judging.state(db,'roadshow')
            admin=self.service.roadshow_scoring.admin_view(db,state)
            self.assertNotIn(team_id,[t['id'] for t in admin['availableTeams']])
            self.assertIn(live,[t['id'] for t in admin['availableTeams']])
            with self.assertRaises(APIError) as failure:
                self.service.roadshow_scoring.save_teams(db,{'id':'admin'},state,{
                    'runId':state['run_id'],'expectedRevision':state['revision'],'teamIds':[team_id]})
            self.assertEqual((failure.exception.status,failure.exception.code),(409,'team_recycled'))


class NumericRoadshowRecycleTests(unittest.TestCase):
    setUp=api_helpers.APITests.setUp
    tearDown=api_helpers.APITests.tearDown
    team=TeamRecycleIntegrationTests.team
    invitation=api_helpers.APITests.invitation
    lifecycle_get=TeamRecycleIntegrationTests.lifecycle_get
    recycle=TeamRecycleIntegrationTests.recycle
    restore=TeamRecycleIntegrationTests.restore
    request=roadshow_helpers.RoadshowScoringTests.request
    admin_view=roadshow_helpers.RoadshowScoringTests.admin_view
    configure=roadshow_helpers.RoadshowScoringTests.configure
    stage=roadshow_helpers.RoadshowScoringTests.stage
    window=roadshow_helpers.RoadshowScoringTests.window
    score=roadshow_helpers.RoadshowScoringTests.score
    fill=roadshow_helpers.RoadshowScoringTests.fill
    publish=roadshow_helpers.RoadshowScoringTests.publish
    board=roadshow_helpers.RoadshowScoringTests.board

    def history(self):
        with self.service.db() as db:
            return {table:[tuple(r) for r in db.execute('SELECT * FROM '+table+' ORDER BY 1,2,3')]
                for table in ['roadshow_scores','judging_roster']}

    def test_recycled_withdrawn_team_invalidates_publication_keeps_history_and_live_targets(self):
        self.configure(2);self.fill();self.assertEqual(self.window(False)[0],200)
        old,live=self.teams
        with self.service.db() as db:db.execute("UPDATE team_details SET status='withdrawn' WHERE team_id=?",(old,))
        self.assertEqual(self.publish()[0],200)
        self.assertIn(old,[t['id'] for t in self.board()['summary']['teams']])
        history=self.history();self.recycle(old)
        self.assertIsNone(self.board()['summary']);self.assertEqual(self.history(),history)
        admin=self.admin_view()
        self.assertIn(old,[t['id'] for t in admin['summary']['teams']])
        self.assertNotIn(old,[t['id'] for t in admin['availableTeams']])
        status,judge,_=self.request(self.judges[0],'GET','/api/roadshow/scoring')
        self.assertEqual(status,200,judge)
        self.assertEqual([t['id'] for t in judge['teams']],[live])
        self.assertNotIn(old,[r['teamId'] for r in judge['myScores']])
        self.assertEqual(self.window(True)[0],200)
        self.assertEqual(self.score(team=old,revision=1)[0],403)
        self.assertEqual(self.window(False)[0],200)
        self.assertEqual(self.publish()[0],200)
        self.assertEqual([t['id'] for t in self.board()['summary']['teams']],[live])
        status,result,_=self.restore(old,'Synthetic Restored Roadshow')
        self.assertEqual(status,200,result);self.assertEqual(result['team']['status'],'withdrawn')
        self.assertIsNone(self.board()['summary']);self.assertEqual(self.history(),history)


class NumericFinalsRecycleTests(unittest.TestCase):
    setUp=finals_helpers.FinalsTests.setUp
    tearDown=api_helpers.APITests.tearDown
    team=TeamRecycleIntegrationTests.team
    invitation=api_helpers.APITests.invitation
    lifecycle_get=TeamRecycleIntegrationTests.lifecycle_get
    recycle=TeamRecycleIntegrationTests.recycle
    restore=TeamRecycleIntegrationTests.restore
    authorize=finals_helpers.FinalsTests.authorize
    request=finals_helpers.FinalsTests.request
    admin_view=finals_helpers.FinalsTests.admin_view
    settings=finals_helpers.FinalsTests.settings
    configure=finals_helpers.FinalsTests.configure
    score=finals_helpers.FinalsTests.score
    ballot=finals_helpers.FinalsTests.ballot
    confirm=finals_helpers.FinalsTests.confirm
    fill=finals_helpers.FinalsTests.fill

    def history(self):
        with self.service.db() as db:
            return {table:[tuple(r) for r in db.execute('SELECT * FROM '+table+' ORDER BY 1,2')]
                for table in ['finals_scores','finals_nominations','finals_teams']}

    def test_recycled_withdrawn_finalist_keeps_history_and_requires_explicit_republication(self):
        self.configure(2);self.fill();self.assertEqual(self.settings(False)[0],200)
        old,live=self.teams
        with self.service.db() as db:db.execute("UPDATE team_details SET status='withdrawn' WHERE team_id=?",(old,))
        self.assertEqual(self.confirm()[0],200);self.assertEqual(self.settings(False,True)[0],200)
        board=lambda:self.public.get('/api/finals/board')[1]
        self.assertIn(old,[t['id'] for t in board()['summary']['teams']])
        history=self.history();self.recycle(old)
        self.assertIsNone(board()['summary']);self.assertEqual(self.history(),history)
        admin=self.admin_view()
        self.assertIn(old,[t['id'] for t in admin['summary']['teams']])
        self.assertNotIn(old,[t['id'] for t in admin['availableTeams']])
        status,judge,_=self.request(self.judges[0],'GET','/api/finals/judge')
        self.assertEqual(status,200,judge)
        self.assertEqual([t['id'] for t in judge['teams']],[live])
        self.assertNotIn(old,[r['teamId'] for r in judge['myScores']])
        self.assertEqual(self.settings(True)[0],200)
        self.assertEqual(self.score(self.judges[0],old,revision=1)[0],403)
        nominations={dim['id']:old for dim in finals_helpers.DIMENSIONS}
        self.assertEqual(self.ballot(self.judges[0],choices=nominations,revision=1)[0],400)
        self.assertEqual(self.settings(False)[0],200)
        self.assertEqual(self.confirm()[0],200);self.assertEqual(self.settings(False,True)[0],200)
        self.assertEqual([t['id'] for t in board()['summary']['teams']],[live])
        status,result,_=self.restore(old,'Synthetic Restored Finalist')
        self.assertEqual(status,200,result);self.assertEqual(result['team']['status'],'withdrawn')
        self.assertIsNone(board()['summary']);self.assertEqual(self.history(),history)


if __name__=='__main__':unittest.main()
