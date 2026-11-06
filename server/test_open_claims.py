"""Open applications: synthetic HTTP fixtures, no pre-imported eligibility list."""
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
import json
import unittest

from errors import APIError
from test_api import Client
import test_claims


class OpenClaimsTests(unittest.TestCase):
    setUp=test_claims.ClaimsTests.setUp
    tearDown=test_claims.ClaimsTests.tearDown
    admin_call=test_claims.ClaimsTests.admin_call
    bearer=test_claims.ClaimsTests.bearer
    roster=test_claims.ClaimsTests.roster
    team=test_claims.ClaimsTests.team

    def application(self,prefix='Open',**changes):
        body={'name':prefix+' Captain','school':'School A','teamName':prefix+' Team','track':'',
              'members':[{'name':prefix+' One','school':'School B'},{'name':prefix+' Two','school':'School C'}],**changes}
        status,result,_=self.public.post('/api/claims/request',body)
        self.assertEqual(status,200,result)
        return result

    def approval(self,claim,**changes):
        row=next(r for r in self.admin_call('')[1]['requests'] if r['id']==claim['requestId'])
        declaration=row['declaration'] or {}
        return {'requestId':row['id'],'expectedRevision':row['revision'],'reviewContext':row['reviewContext'],
                'teamName':declaration.get('teamName','Legacy Team'),'track':declaration.get('track',''),
                'pin':claim['pin'],'inPersonVerified':True,'resolvedIdentity':False,'note':'',**changes}

    def counts(self):
        with self.service.db() as db:
            return {table:db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0] for table in
                    ['teams','claim_people','claim_memberships','team_accounts','accounts','claim_declarations']}

    def test_empty_install_pending_then_atomic_creation_redemption_and_login(self):
        before=self.counts();claim=self.application();pending=self.counts()
        for table in ['teams','claim_people','claim_memberships','team_accounts','accounts']:self.assertEqual(pending[table],before[table])
        body=self.approval(claim,teamName='Organizer corrected name',track='AI for Science')
        self.assertEqual(self.admin_call('/approve',body)[0],200)
        counts=self.counts();self.assertEqual(counts['teams'],1);self.assertEqual(counts['claim_people'],3)
        self.assertEqual(counts['claim_memberships'],3);self.assertEqual(counts['team_accounts'],1)
        self.assertEqual(self.admin_call('/approve',dict(reversed(list(body.items()))))[0],200)
        own=self.bearer(claim)[1];self.assertEqual(own['team']['number'],1);self.assertEqual(own['declaration']['teamName'],'Organizer corrected name')
        self.assertEqual(own['team']['track'],'AI for Science');self.assertIsNone(own['code'])
        first=self.bearer(claim,'redeem')[1];self.assertEqual(self.bearer(claim,'redeem')[1],first)
        self.assertEqual(self.admin_call('/approve',body)[0],200);self.assertEqual(self.counts(),counts)
        team=Client(self.server);self.assertEqual(team.post('/api/login',{'role':'team','code':first['code']})[0],200)
        self.assertEqual(team.get('/api/team')[1]['team']['id'],own['team']['id'])
        private=self.admin_call('')[1];serialized=json.dumps(private)
        for secret in [claim['secret'],claim['pin'],first['code']]:self.assertNotIn(secret,serialized)
        for key in ['people','references','reviewContext','captainId','memberIds']:self.assertNotIn(key,own)
        with self.service.db() as db:
            # Team creation does not provision gateway accounts or quota entitlements.
            self.assertEqual(db.execute('SELECT COUNT(*) FROM team_api').fetchone()[0],0)
            audit=' '.join(row[0] for row in db.execute('SELECT metadata FROM audit'))
            for secret in [claim['secret'],claim['pin'],first['code']]:self.assertNotIn(secret,audit)

    def test_partial_reference_reuse_and_advisory_review_flag(self):
        self.roster([{'id':'known','name':'Open Captain','school':'School A','notes':'Original preserved','needsReview':True}])
        claim=self.application();body=self.approval(claim)
        self.assertEqual(self.admin_call('/approve',body)[0],200)
        people=self.admin_call('')[1]['people'];self.assertEqual(len(people),3)
        known=next(p for p in people if p['id']=='known');self.assertEqual(known['notes'],'Original preserved');self.assertTrue(known['needsReview'])

    def test_concurrent_exact_duplicate_approvals_leave_one_complete_team(self):
        first=self.application();second=self.application();a=self.approval(first);b=self.approval(second)
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda body:self.admin_call('/approve',body),[a,b]))
        self.assertEqual(sorted(result[0] for result in results),[200,409])
        counts=self.counts();self.assertEqual(counts['teams'],1);self.assertEqual(counts['claim_people'],3)
        self.assertEqual(counts['claim_memberships'],3);self.assertEqual(counts['team_accounts'],1)
        loser=second if results[0][0]==200 else first
        self.assertEqual(self.admin_call('/approve',self.approval(loser))[0],409)
        self.assertEqual(self.counts(),counts)

    def test_same_name_other_school_requires_distinction_and_note(self):
        first=self.application();self.assertEqual(self.admin_call('/approve',self.approval(first))[0],200)
        second=self.application('Distinct',name='Open Captain',school='Other School')
        body=self.approval(second)
        row=next(r for r in self.admin_call('')[1]['requests'] if r['id']==second['requestId'])
        self.assertTrue(any(p['name']=='Open Captain' and p['teamId'] for p in row['references']))
        for changes in [{},{'resolvedIdentity':True},{'note':'Distinct people'}]:self.assertEqual(self.admin_call('/approve',{**body,**changes})[0],400)
        self.assertEqual(self.admin_call('/approve',{**body,'resolvedIdentity':True,'note':'Different people verified in person'})[0],200)
        self.assertEqual(self.counts()['teams'],2)

    def test_reference_context_rejects_relevant_changes_but_not_unrelated_people(self):
        claim=self.application();body=self.approval(claim)
        self.roster([{'id':'unrelated','name':'Unrelated','school':'Elsewhere','notes':'','needsReview':False}])
        self.assertEqual(body['reviewContext'],self.approval(claim)['reviewContext'])
        self.roster([{'id':'related','name':'OPEN CAPTAIN','school':'School A','notes':'new reference','needsReview':False}])
        before=self.counts();self.assertEqual(self.admin_call('/approve',body)[0],409);self.assertEqual(self.counts(),before)
        body=self.approval(claim)
        person={'id':'related','name':'OPEN CAPTAIN','school':'School A','notes':'revised','needsReview':False}
        self.assertEqual(self.admin_call('/person',{'expectedRevision':1,'person':person})[0],200)
        self.assertEqual(self.admin_call('/approve',body)[0],409)
        self.assertEqual(self.admin_call('/approve',self.approval(claim))[0],200)

    def test_metadata_edits_legacy_preservation_and_stale_review(self):
        claim=self.application();old=self.approval(claim);members=claim['declaration']['members']
        changed=self.bearer(claim,'update',expectedRevision=1,members=members,teamName='Updated Team',track='未来学习样态')
        self.assertEqual(changed[0],200);self.assertEqual(changed[1]['declaration']['teamName'],'Updated Team')
        self.assertEqual(self.admin_call('/approve',old)[0],409)
        self.assertEqual(self.bearer(claim,'update',expectedRevision=1,members=members,teamName='Stale',track='')[0],409)
        legacy=self.bearer(claim,'update',expectedRevision=2,members=members)
        self.assertEqual(legacy[0],200);self.assertEqual(legacy[1]['declaration']['track'],'未来学习样态')
        self.assertEqual(legacy[1]['declaration']['teamName'],'Updated Team')
        for data in [{'teamName':'','track':''},{'teamName':'Valid','track':'invented'}]:
            self.assertEqual(self.bearer(claim,'update',expectedRevision=3,members=members,**data)[0],400)
        self.assertEqual(self.admin_call('/approve',self.approval(claim))[0],200)
        self.assertEqual(self.bearer(claim,'update',expectedRevision=4,members=members,teamName='No',track='')[0],409)

    def test_legacy_no_members_requires_supplement_and_admin_can_fill_metadata(self):
        claim=self.public.post('/api/claims/request',{'name':'Legacy','school':'School'})[1]
        self.assertEqual(self.admin_call('/approve',self.approval(claim))[0],409)
        members=[{'name':'One','school':'School'},{'name':'Two','school':'Other'}]
        self.assertEqual(self.bearer(claim,'update',expectedRevision=1,members=members)[0],200)
        self.assertEqual(self.admin_call('/approve',self.approval(claim,teamName='Filled at desk'))[0],200)
        self.assertEqual(self.bearer(claim)[1]['team']['name'],'Filled at desk')

    def test_lowest_free_number_with_existing_999_and_exhaustion(self):
        self.team(999);self.team(1);claim=self.application()
        self.assertEqual(self.admin_call('/approve',self.approval(claim))[0],200)
        self.assertEqual(self.bearer(claim)[1]['team']['number'],2)
        with self.service.db() as db:
            for number in range(3,999):db.execute('INSERT INTO teams VALUES (?,?,?,?,?,?)',('filled-'+str(number),number,'Fixture','','',0))
        next_claim=self.application('Next');before=self.counts()
        self.assertEqual(self.admin_call('/approve',self.approval(next_claim))[0],409);self.assertEqual(self.counts(),before)

    def test_failure_after_team_and_people_creation_rolls_back_all_rows(self):
        claim=self.application();body=self.approval(claim);before=self.counts()
        with patch.object(self.service,'team_access',side_effect=APIError(409,'synthetic issuance conflict')):
            self.assertEqual(self.admin_call('/approve',body)[0],409)
        self.assertEqual(self.counts(),before);self.assertEqual(self.bearer(claim)[1]['status'],'pending')
        self.assertEqual(self.admin_call('/approve',body)[0],200)

    def test_open_approval_auth_inperson_and_durable_pin_limits(self):
        claim=self.application();body=self.approval(claim)
        self.assertEqual(self.public.post('/api/admin/claims/approve',body)[0],401)
        self.assertEqual(self.admin.post('/api/admin/claims/approve',body)[0],428)
        self.assertEqual(self.admin_call('/approve',body,login_id='wrong')[0],401)
        self.assertEqual(self.admin_call('/approve',{**body,'inPersonVerified':False})[0],400)
        for context in ['非 ASCII',None,'a'*63,'g'*64]:
            self.assertEqual(self.admin_call('/approve',{**body,'reviewContext':context})[0],400)
        wrong='000000' if claim['pin']!='000000' else '111111'
        for _ in range(5):self.assertEqual(self.admin_call('/approve',{**body,'pin':wrong})[0],400)
        self.assertEqual(self.admin_call('/approve',body)[0],429);self.assertEqual(self.counts()['teams'],0)

    def registration(self,prefix='Open',**changes):
        import secrets
        value={'name':prefix+' Team','captain':prefix+' Captain','school':'School A','phone':'13900000001',
               'targetSize':3,'members':[prefix+' One'],'project':'Existing project','summary':'Retained summary',**changes}
        code=secrets.token_urlsafe(32)
        status,result,_=self.public.post('/api/registration/create',{'code':code,'team':value})
        self.assertEqual(status,200,result)
        return code,result['team'],value

    def selected_approval(self,claim,team_id,**changes):
        team=next(t for t in self.admin_call('')[1]['teams'] if t['id']==team_id)
        return self.approval(claim,teamId=team_id,teamContext=team['teamContext'],**changes)

    def test_existing_registration_approval_binds_same_team_and_locks_reviewed_members(self):
        code,registered,value=self.registration()
        self.assertEqual(self.public.post('/api/login',{'role':'team','code':code})[0],401)
        with self.service.db() as db:
            db.execute('UPDATE team_registrations SET source_key=?,raw_json=? WHERE team_id=?',('fixture-source','{"original":"unchanged"}',registered['id']))
            original=dict(db.execute('SELECT * FROM team_registrations WHERE team_id=?',(registered['id'],)).fetchone())
        claim=self.application();body=self.selected_approval(claim,registered['id'],teamName='Reviewed name',track='AI for Science')
        candidate=next(t for t in self.admin_call('')[1]['teams'] if t['id']==registered['id'])
        self.assertEqual(candidate['registration']['members'],['Open Captain','Open One'])
        for field in ['phone','raw_json','secret_hash','source_key']:self.assertNotIn(field,candidate['registration'])
        self.assertNotIn(code,json.dumps(candidate))
        self.assertEqual(self.counts()['team_accounts'],0)
        self.assertEqual(self.admin_call('/approve',body)[0],200)
        self.assertEqual(self.counts()['teams'],1);self.assertEqual(self.counts()['claim_memberships'],3)
        own=self.bearer(claim)[1];self.assertEqual(own['team']['id'],registered['id']);self.assertEqual(own['team']['number'],registered['number'])
        with self.service.db() as db:
            team=self.service.admin_data.team(db,registered['id']);updated=dict(db.execute('SELECT * FROM team_registrations WHERE team_id=?',(registered['id'],)).fetchone())
        self.assertEqual(team['project'],'Existing project');self.assertEqual(team['name'],'Reviewed name')
        self.assertEqual(json.loads(updated['members_json']),['Open Captain','Open One','Open Two'])
        self.assertGreater(updated['revision'],original['revision'])
        for field in ['phone','source_key','raw_json','secret_hash','summary','created_at']:self.assertEqual(updated[field],original[field])
        saved=self.public.post('/api/registration/open',{'code':code})[1]['team']
        self.assertEqual(saved['members'],['Open Captain','Open One','Open Two'])
        self.assertEqual(self.public.post('/api/registration/save',{'code':code,'expectedRevision':saved['revision'],
            'team':{**value,'name':'Reviewed name','members':['Open One','Different Person']}})[0],409)
        issue=self.bearer(claim,'redeem')[1];team_client=Client(self.server)
        self.assertEqual(team_client.post('/api/login',{'role':'team','code':issue['code']})[0],200)
        before=self.counts();self.assertEqual(self.admin_call('/approve',body)[0],200);self.assertEqual(self.counts(),before)
        self.assertEqual(self.admin_call('')[1]['teams'],[])

    def test_selected_registration_snapshot_rejects_changed_registration_and_team_fields(self):
        code,team,value=self.registration();claim=self.application();body=self.selected_approval(claim,team['id'])
        saved=self.public.post('/api/registration/save',{'code':code,'expectedRevision':team['revision'],'team':{**value,'phone':'13900000002'}})
        self.assertEqual(saved[0],200,saved[1]);before=self.counts()
        self.assertEqual(self.admin_call('/approve',body)[0],409);self.assertEqual(self.counts(),before)
        body=self.selected_approval(claim,team['id'])
        with self.service.db() as db:self.service.admin_data.save_team(db,None,{'id':team['id'],'name':'Renamed registration'})
        self.assertEqual(self.admin_call('/approve',body)[0],409);self.assertEqual(self.counts(),before)
        self.assertEqual(self.admin_call('/approve',self.selected_approval(claim,team['id']))[0],200)

    def test_existing_team_requires_explicit_selection_and_duplicate_name_is_normalized(self):
        _,team,_=self.registration();claim=self.application(teamName='  ＯＰＥＮ   TEAM ')
        before=self.counts();response=self.admin_call('/approve',self.approval(claim))
        self.assertEqual(response[0],409);self.assertIn('选择',response[1]['error']);self.assertEqual(self.counts(),before)
        body=self.selected_approval(claim,team['id']);self.assertEqual(self.admin_call('/approve',body)[0],200)
        second=self.application('Different',teamName='open team');before=self.counts()
        self.assertEqual(self.admin_call('/approve',self.approval(second))[0],409);self.assertEqual(self.counts(),before)
        self.assertEqual(self.admin_call('/approve',self.approval(second,teamName='Distinct new team'))[0],200)

    def test_selected_registration_issuance_failure_rolls_back_team_and_registration_changes(self):
        _,team,_=self.registration();claim=self.application();body=self.selected_approval(claim,team['id'],teamName='Changed on approval')
        with self.service.db() as db:
            before_team=dict(db.execute('SELECT * FROM teams WHERE id=?',(team['id'],)).fetchone())
            before_registration=dict(db.execute('SELECT * FROM team_registrations WHERE team_id=?',(team['id'],)).fetchone())
        before=self.counts()
        with patch.object(self.service,'team_access',side_effect=APIError(409,'synthetic failure')):
            self.assertEqual(self.admin_call('/approve',body)[0],409)
        self.assertEqual(self.counts(),before)
        with self.service.db() as db:
            self.assertEqual(dict(db.execute('SELECT * FROM teams WHERE id=?',(team['id'],)).fetchone()),before_team)
            self.assertEqual(dict(db.execute('SELECT * FROM team_registrations WHERE team_id=?',(team['id'],)).fetchone()),before_registration)
        self.assertEqual(self.admin_call('/approve',body)[0],200)

    def test_optional_team_target_is_validated_and_becomes_ineligible_after_changes(self):
        _,team,_=self.registration();claim=self.application();body=self.selected_approval(claim,team['id'])
        for changes in [{'teamContext':'非 ASCII'},{'teamContext':None},{'teamId':''}]:
            self.assertEqual(self.admin_call('/approve',{**body,**changes})[0],400)
        self.assertEqual(self.admin_call('/approve',{k:v for k,v in body.items() if k!='teamContext'})[0],400)
        with self.service.db() as db:db.execute("UPDATE team_details SET status='withdrawn' WHERE team_id=?",(team['id'],))
        before=self.counts();self.assertEqual(self.admin_call('')[1]['teams'],[])
        self.assertEqual(self.admin_call('/approve',body)[0],409);self.assertEqual(self.counts(),before)
        with self.service.db() as db:db.execute("UPDATE team_details SET status='active' WHERE team_id=?",(team['id'],))
        body=self.selected_approval(claim,team['id'])
        self.assertEqual(self.admin.post('/api/admin/team-access',{'teamId':team['id'],'action':'issue'})[0],200)
        before=self.counts();self.assertEqual(self.admin_call('')[1]['teams'],[])
        self.assertEqual(self.admin_call('/approve',body)[0],409);self.assertEqual(self.counts(),before)

    def test_racing_different_claims_cannot_bind_the_same_existing_team(self):
        _,team,_=self.registration();first=self.application();second=self.application('Different')
        bodies=[self.selected_approval(first,team['id']),self.selected_approval(second,team['id'])]
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda body:self.admin_call('/approve',body),bodies))
        self.assertEqual(sorted(result[0] for result in results),[200,409]);self.assertEqual(self.counts()['teams'],1)
        self.assertEqual(self.counts()['claim_memberships'],3);self.assertEqual(self.counts()['team_accounts'],1)
        winner=bodies[0] if results[0][0]==200 else bodies[1]
        self.assertEqual(self.admin_call('/approve',winner)[0],200)

    def test_verified_same_name_people_allow_metadata_edits_without_losing_any_identity(self):
        for prefix,captain,members in [('DuplicateMembers','Captain',['Alex','Alex']),('DuplicateCaptain','Alex',['Alex','Member'])]:
            with self.subTest(prefix=prefix):
                code,registered,value=self.registration(prefix,captain=captain,members=members[:1],school=prefix+' School')
                claim=self.application(prefix,name=captain,school=prefix+' School',members=[{'name':name,'school':prefix+' School '+str(index)} for index,name in enumerate(members)])
                body=self.selected_approval(claim,registered['id'],resolvedIdentity=True,note='Distinct people from different schools verified')
                self.assertEqual(self.admin_call('/approve',body)[0],200)
                stored=[captain,*members]
                current=self.public.post('/api/registration/open',{'code':code})[1]['team']
                self.assertEqual(current['members'],stored)
                with self.service.db() as db:
                    before=[tuple(row) for row in db.execute('SELECT person_id,team_id,request_id FROM claim_memberships WHERE team_id=? ORDER BY person_id',(registered['id'],))]
                for submitted in [members,stored]:  # Browser tail and compatible full-list clients.
                    result=self.public.post('/api/registration/save',{'code':code,'expectedRevision':current['revision'],
                        'team':{**value,'members':submitted,'phone':'13900000002','project':'Editable project after identity review'}})
                    self.assertEqual(result[0],200,result[1]);current=result[1]['team']
                    self.assertEqual(current['members'],stored);self.assertEqual(current['phone'],'13900000002')
                    self.assertEqual(current['project'],'Editable project after identity review')
                for changed in [members[:-1],['Replacement',members[1]],list(reversed(stored))]:
                    result=self.public.post('/api/registration/save',{'code':code,'expectedRevision':current['revision'],
                        'team':{**value,'members':changed}})
                    self.assertEqual(result[0],409,result[1])
                with self.service.db() as db:
                    self.assertEqual(json.loads(db.execute('SELECT members_json FROM team_registrations WHERE team_id=?',(registered['id'],)).fetchone()[0]),stored)
                    self.assertEqual([tuple(row) for row in db.execute('SELECT person_id,team_id,request_id FROM claim_memberships WHERE team_id=? ORDER BY person_id',(registered['id'],))],before)
                self.assertEqual(len(before),3)

    def test_verified_duplicate_names_reject_reordering_and_target_below_full_member_count(self):
        code,registered,value=self.registration(targetSize=4)
        members=[{'name':name,'school':'School '+str(index)} for index,name in enumerate(['Alex','Alex','Bob'])]
        claim=self.application(members=members);self.assertEqual(self.admin_call('/approve',self.selected_approval(claim,registered['id']))[0],200)
        current=self.public.post('/api/registration/open',{'code':code})[1]['team']
        for names,target,status in [(['Alex','Bob','Alex'],4,409),(['Alex','Alex','Bob'],3,400),(['Alex','Bob'],4,409)]:
            result=self.public.post('/api/registration/save',{'code':code,'expectedRevision':current['revision'],'team':{**value,'members':names,'targetSize':target}})
            self.assertEqual(result[0],status,result[1])
        result=self.public.post('/api/registration/save',{'code':code,'expectedRevision':current['revision'],
            'team':{**value,'members':['Alex','Alex','Bob'],'phone':'13900000002'}})
        self.assertEqual(result[0],200,result[1]);self.assertEqual(result[1]['team']['members'],['Open Captain','Alex','Alex','Bob'])
