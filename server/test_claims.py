"""HTTP registration tests: synthetic identities, isolated temporary databases."""
from concurrent.futures import ThreadPoolExecutor
import json
import unittest

from app import EventService
from claims import INVALID
import test_api as api_helpers
from test_api import Client


class ClaimsTests(unittest.TestCase):
    setUp=api_helpers.APITests.setUp
    tearDown=api_helpers.APITests.tearDown
    team=api_helpers.APITests.team
    invitation=api_helpers.APITests.invitation

    def admin_call(self,path,body=None,client=None,login_id=None):
        client=client or self.admin
        marker=login_id if login_id is not None else self.service.login_id(client.cookie)
        return client.request('GET' if body is None else 'POST','/api/admin/claims'+path,body,headers={'X-GOTC-Login':marker or ''})

    def roster(self,people=None):
        people=people or [{'id':'p1','name':'Synthetic Captain','school':'Fixture School','notes':'test source line 1','needsReview':False},
                          {'id':'p2','name':'Synthetic Member','school':'Fixture School','notes':'test source line 2','needsReview':False}]
        result=self.admin_call('/roster',{'people':people});self.assertEqual(result[0],200,result[1]);return people

    def claim(self,name='Synthetic Captain',school='Fixture School'):
        result=self.public.post('/api/claims/request',{'name':name,'school':school});self.assertEqual(result[0],200,result[1]);return result[1]

    def approval(self,claim,team=None,**changes):
        body={'requestId':claim['requestId'],'expectedRevision':1,'action':'approve','note':'Verified at desk',
                'teamId':team or self.team(),'captainId':'p1','memberIds':['p1','p2'],'pin':claim['pin'],
                'inPersonVerified':True,'resolvedIdentity':False,**changes}
        if 'memberRevisions' not in body:
            displayed={person['id']:person['revision'] for person in self.admin_call('')[1]['people']}
            body['memberRevisions']={person:displayed.get(person,1) for person in body['memberIds']}
        return body

    def bearer(self,claim,path='status',**changes):
        return self.public.post('/api/claims/'+path,{'requestId':claim['requestId'],'secret':claim['secret'],**changes})

    def approve(self,claim,team=None,**changes):
        body=self.approval(claim,team,**changes);result=self.admin_call('/review',body);self.assertEqual(result[0],200,result[1]);return body

    def test_private_roster_auth_marker_import_size_and_no_silent_rewrites(self):
        self.assertEqual(self.public.post('/api/claims/request',{'name':'x','school':'y'})[0],503)
        self.assertEqual(self.public.get('/api/admin/claims')[0],401)
        self.assertEqual(self.admin.get('/api/admin/claims')[0],428)
        self.assertEqual(self.admin_call('',login_id='wrong')[0],401)
        judge,_=self.invitation('judge','Synthetic Judge');self.assertEqual(self.admin_call('',client=judge)[0],403)
        people=[{'id':f'fixture-{i}','name':f'Fixture Person {i}','school':'Synthetic School','notes':'test-only '*10,'needsReview':False} for i in range(150)]
        self.assertGreater(len(json.dumps({'people':people})),16384)
        self.roster(people);self.assertEqual(self.admin_call('/roster',{'people':people})[1]['added'],0)
        changed=[{**people[0],'school':'Other School'}];self.assertEqual(self.admin_call('/roster',{'people':changed})[0],409)
        self.assertEqual(self.admin_call('/roster',{'people':[people[0],people[0]]})[0],409)
        self.assertEqual(self.admin_call('/roster',{'people':[{**people[0],'id':'other'}]})[0],409)
        self.assertEqual(self.admin_call('/roster',{'people':people*4})[0],400)
        self.assertEqual(self.public.request('POST','/api/claims/request',{'name':'a','school':'b'},origin=False)[0],403)

    def test_uniform_request_status_and_secret_redaction(self):
        self.roster();known=self.claim();unknown=self.claim('Unknown Person','Unknown School')
        self.assertEqual(set(known),set(unknown));self.assertEqual(known['status'],unknown['status'])
        self.assertEqual(len(known['secret']),43);self.assertEqual(len(known['pin']),6)
        self.assertEqual(self.bearer(known)[1]['status'],'pending')
        for body in [{'name':'Synthetic Captain','school':'Fixture School'},{'requestId':known['requestId'],'secret':'a'*43},{'requestId':'0'*32,'secret':known['secret']}]:
            status,result,_=self.public.post('/api/claims/status',body);self.assertEqual(status,200);self.assertEqual(result,INVALID)
        listing=self.admin_call('')[1];serialized=json.dumps(listing)
        for value in [known['secret'],known['pin']]:self.assertNotIn(value,serialized)
        with self.service.db() as db:
            row=db.execute('SELECT * FROM claim_requests WHERE id=?',(known['requestId'],)).fetchone()
            self.assertNotIn(known['secret'],str(dict(row)));self.assertNotIn(known['pin'],row['pin_hash'])
        self.assertEqual(self.public.get('/api/claims/status')[0],404)
        self.assertEqual(self.public.post('/api/claims/request',{'name':'x'*101,'school':'s'})[0],400)
        self.assertEqual(self.public.post('/api/claims/request',{'name':'x','school':'s','teamId':'guess'})[0],400)

    def test_success_retry_redemption_login_and_restart(self):
        self.roster();claim=self.claim();body=self.approve(claim)
        self.assertEqual(self.admin_call('/review',body)[0],200)
        before=self.bearer(claim)[1];self.assertEqual(before['status'],'approved');self.assertIsNone(before['code'])
        first=self.bearer(claim,'redeem')[1];second=self.bearer(claim,'redeem')[1];self.assertEqual(first,second)
        self.assertRegex(first['code'],r'^GT[a-f0-9]{16}\.[a-f0-9]{64}$')
        team=Client(self.server);self.assertEqual(team.post('/api/login',{'role':'team','code':first['code']})[0],200)
        self.assertEqual(team.get('/api/team')[1]['team']['id'],body['teamId'])
        self.assertEqual(self.bearer(claim)[1]['status'],'issued');self.assertIsNone(self.bearer(claim)[1]['code'])
        with self.service.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM claim_memberships').fetchone()[0],2)
            saved=tuple(db.execute('SELECT salt,code_hash FROM accounts WHERE id=?',(first['code'].split('.')[0],)).fetchone())
            audit=' '.join(row[0] for row in db.execute('SELECT metadata FROM audit'))
            for secret in [claim['secret'],claim['pin'],first['code']]:self.assertNotIn(secret,audit)
        old=self.service;self.service=EventService(old.data_dir,base_path=old.base_path,origin=old.origin,static_root=old.root);self.server.service=self.service
        self.assertEqual(self.bearer(claim,'redeem')[1]['code'],first['code'])
        with self.service.db() as db:self.assertEqual(tuple(db.execute('SELECT salt,code_hash FROM accounts WHERE id=?',(first['code'].split('.')[0],)).fetchone()),saved)

    def test_wrong_pin_has_durable_bounded_retries(self):
        self.roster();claim=self.claim();body=self.approval(claim);wrong='000000' if claim['pin']!='000000' else '111111'
        for _ in range(5):self.assertEqual(self.admin_call('/review',{**body,'pin':wrong})[0],400)
        self.assertEqual(self.admin_call('/review',body)[0],429)
        listing=self.admin_call('')[1];self.assertEqual(listing['requests'][0]['pinAttempts'],5)
        with self.service.db() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM claim_memberships').fetchone()[0],0)

    def test_identity_school_resolution_and_membership_validation(self):
        people=self.roster();claim=self.claim(school='Different School');body=self.approval(claim)
        for changes in [{'inPersonVerified':False},{'captainId':'p2'},{'memberIds':['p2']},{'memberIds':['p1','invented']},{'memberIds':['p1','p1']}]:
            self.assertIn(self.admin_call('/review',{**body,**changes})[0],{400,409})
        self.assertEqual(self.admin_call('/review',body)[0],400)
        self.assertEqual(self.admin_call('/review',{**body,'resolvedIdentity':True,'note':''})[0],400)
        self.assertEqual(self.admin_call('/review',{**body,'resolvedIdentity':True})[0],200)
        self.assertEqual(self.admin_call('/person',{'expectedRevision':1,'person':{**people[0],'name':'Changed Name'}})[0],409)
        self.assertEqual(self.admin_call('/person',{'expectedRevision':1,'person':{**people[0],'notes':'append onsite note'}})[0],200)

    def test_needs_review_and_person_edit_revision_and_alias(self):
        people=self.roster([{'id':'p1','name':'Parent Entry','school':'Fixture School','notes':'Original source preserved','needsReview':True}])
        claim=self.claim();team=self.team()
        self.assertEqual(self.admin_call('/review',self.approval(claim,team,memberIds=['p1']))[0],409)
        corrected={**people[0],'name':'Synthetic Captain'}
        self.assertEqual(self.admin_call('/person',{'expectedRevision':0,'person':corrected})[0],409)
        self.assertEqual(self.admin_call('/person',{'expectedRevision':1,'person':corrected})[0],200)
        body=self.approval(claim,team,memberIds=['p1']);self.assertEqual(self.admin_call('/review',body)[0],400)
        self.assertEqual(self.admin_call('/review',{**body,'resolvedIdentity':True})[0],200)
        duplicate={**corrected,'id':'p3'};self.assertEqual(self.admin_call('/person',{'expectedRevision':0,'person':duplicate})[0],409)
        distinct={**duplicate,'school':'Verified Different School'};self.assertEqual(self.admin_call('/person',{'expectedRevision':0,'person':distinct})[0],200)

    def test_racing_approvals_do_not_partially_assign_or_issue(self):
        self.roster();first=self.claim();second=self.claim();a=self.approval(first,self.team(1));b=self.approval(second,self.team(2))
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda body:self.admin_call('/review',body),[a,b]))
        self.assertEqual(sorted(r[0] for r in results),[200,409])
        with self.service.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM claim_memberships').fetchone()[0],2)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM team_accounts').fetchone()[0],1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM claim_requests WHERE status='approved'").fetchone()[0],1)
        winner=a if results[0][0]==200 else b;self.assertEqual(self.admin_call('/review',winner)[0],200)

    def test_same_team_conflict_rolls_back_new_members(self):
        self.roster();first=self.claim();second=self.claim('Synthetic Member');team=self.team()
        self.approve(first,team,memberIds=['p1'])
        self.assertEqual(self.admin_call('/review',self.approval(second,team,captainId='p2',memberIds=['p2']))[0],409)
        with self.service.db() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM claim_memberships').fetchone()[0],1)

    def test_existing_account_recovery_is_required(self):
        self.roster();claim=self.claim();team=self.team();issued=self.admin.post('/api/admin/team-access',{'teamId':team,'action':'issue'})[1]
        self.assertEqual(self.admin_call('/review',self.approval(claim,team))[0],409)
        self.assertEqual(self.admin_call('')[1]['teams'],[])
        with self.service.db() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM claim_memberships').fetchone()[0],0)
        fresh=Client(self.server);self.assertEqual(fresh.post('/api/login',{'role':'team','code':issued['code']})[0],200)

    def test_rotate_revoke_before_and_after_redemption_never_restore(self):
        self.roster()
        for index,(action,redeem) in enumerate([('rotate',False),('revoke',False),('rotate',True),('revoke',True)]):
            person={'id':f'extra{index}','name':f'Extra Fixture {index}','school':'Fixture School','notes':'','needsReview':False};self.roster([person]);claim=self.claim(person['name']);team=self.team(index+1)
            self.approve(claim,team,captainId=person['id'],memberIds=[person['id']])
            if redeem:self.assertIsNotNone(self.bearer(claim,'redeem')[1]['code'])
            self.assertEqual(self.admin.post('/api/admin/team-access',{'teamId':team,'action':action})[0],200)
            with self.service.db() as db:before=tuple(db.execute('SELECT salt,code_hash,active FROM accounts JOIN team_accounts ON accounts.id=team_accounts.account_id WHERE team_id=?',(team,)).fetchone())
            self.assertEqual(self.bearer(claim,'redeem')[1]['status'],'unavailable');self.assertEqual(self.bearer(claim)[1]['status'],'unavailable')
            with self.service.db() as db:self.assertEqual(tuple(db.execute('SELECT salt,code_hash,active FROM accounts JOIN team_accounts ON accounts.id=team_accounts.account_id WHERE team_id=?',(team,)).fetchone()),before)

    def test_reject_expiry_revision_and_pending_does_not_reserve(self):
        self.roster();claim=self.claim();body=self.approval(claim)
        self.assertEqual(self.admin_call('/review',{**body,'expectedRevision':0})[0],409)
        self.assertEqual(self.admin_call('/review',{'requestId':claim['requestId'],'expectedRevision':1,'action':'reject','note':'Not verified'})[0],200)
        self.assertEqual(self.bearer(claim)[1]['status'],'rejected');self.assertEqual(self.admin_call('/review',body)[0],409)
        next_claim=self.claim();self.approve(next_claim,body['teamId'])
        with self.service.db() as db:db.execute('UPDATE claim_requests SET expires_at=0 WHERE id=?',(next_claim['requestId'],))
        self.assertEqual(self.bearer(next_claim,'redeem')[1],INVALID)

    def test_unknown_identity_cannot_claim_known_team(self):
        self.roster();claim=self.claim('Unknown Person');self.assertEqual(self.admin_call('/review',self.approval(claim))[0],409)
        with self.service.db() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM team_accounts').fetchone()[0],0)

    def test_request_limits_unknown_paths_and_max_pending(self):
        self.roster();before=set(self.service.login_attempts)
        for i in range(25):self.assertEqual(self.public.post('/api/claims/not-real-'+str(i),{})[0],404)
        self.assertEqual(set(self.service.login_attempts),before)
        for _ in range(20):self.claim()
        self.assertEqual(self.public.post('/api/claims/request',{'name':'a','school':'b'})[0],429)
        self.assertEqual(self.public.post('/api/claims/status',{})[0],200)
        self.service.login_attempts.clear()
        with self.service.db() as db:
            sample=db.execute('SELECT * FROM claim_requests LIMIT 1').fetchone();values=list(sample)
            for i in range(980):
                values[0]=f'{i+10000:032x}';db.execute('INSERT INTO claim_requests VALUES ('+','.join('?' for _ in values)+')',values)
        self.assertEqual(self.public.post('/api/claims/request',{'name':'a','school':'b'})[0],429)
        with self.service.db() as db:db.execute('UPDATE claim_requests SET expires_at=0')
        self.claim()

    def test_normalized_identity_uniqueness_and_malformed_review(self):
        people=self.roster()
        duplicate={**people[0],'id':'alias','name':'  SYNTHETIC   CAPTAIN  ','school':'Ｆｉｘｔｕｒｅ School'}
        self.assertEqual(self.admin_call('/roster',{'people':[duplicate]})[0],409)
        self.assertEqual(self.admin_call('/person',{'expectedRevision':0,'person':duplicate})[0],409)
        self.assertEqual(self.admin_call('/review',{'action':[]})[0],400)
        claim=self.claim('  SYNTHETIC   CAPTAIN  ','Ｆｉｘｔｕｒｅ School')
        self.approve(claim)
        self.assertGreater(claim['expiresAt'],self.service.claims.clock()+23*3600*1000)
        self.assertLessEqual(claim['expiresAt'],self.service.claims.clock()+24*3600*1000)

    def test_full_roster_membership_has_no_implicit_team_size_cap(self):
        people=[{'id':f'fixture-{index:03}-'+'x'*88,'name':f'Fixture {index}',
                 'school':'Synthetic School','notes':'','needsReview':False} for index in range(500)]
        self.roster(people);claim=self.claim(people[0]['name'],people[0]['school'])
        body=self.approval(claim,captainId=people[0]['id'],memberIds=[p['id'] for p in people])
        self.assertEqual(len(people[0]['id']),100)
        self.assertGreater(len(json.dumps(body)),64*1024)
        self.assertLess(len(json.dumps(body)),128*1024)
        self.assertEqual(self.admin_call('/review',body)[0],200)
        with self.service.db() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM claim_memberships').fetchone()[0],500)

    def test_stale_member_snapshot_from_other_admin_and_same_panel(self):
        people=self.roster();claim=self.claim();body=self.approval(claim)
        other=Client(self.server)
        self.assertEqual(other.post('/api/login',{'role':'admin','code':self.admin_code})[0],200)
        changed={**people[1],'name':'Synthetic Charlie'}
        # A different organizer edits a non-captain identity after the selector was read.
        self.assertEqual(self.admin_call('/person',{'expectedRevision':1,'person':changed},client=other)[0],200)
        conflict=self.admin_call('/review',body)
        self.assertEqual(conflict[0],409);self.assertIn('名单已变化',conflict[1]['error'])
        with self.service.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM claim_memberships').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM team_accounts').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT status,revision FROM claim_requests WHERE id=?',(claim['requestId'],)).fetchone()[:],('pending',1))
        # The same organizer's person editor also leaves an older review selection stale.
        refreshed=self.approval(claim,body['teamId'])
        self.assertEqual(self.admin_call('/person',{'expectedRevision':2,'person':{**changed,'school':'New Verified School'}})[0],200)
        self.assertEqual(self.admin_call('/review',refreshed)[0],409)
        final=self.approval(claim,body['teamId'])
        self.assertEqual(final['memberRevisions'],{'p1':1,'p2':3})
        self.assertEqual(self.admin_call('/review',final)[0],200)
        # Post-approval notes changes must not destroy idempotency of the applied request.
        self.assertEqual(self.admin_call('/person',{'expectedRevision':3,'person':{**changed,'school':'New Verified School','notes':'After approval note'}})[0],200)
        self.assertEqual(self.admin_call('/review',final)[0],200)
        self.assertEqual(self.admin_call('/review',{**final,'memberRevisions':{'p1':1,'p2':4}})[0],409)
        with self.service.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM claim_memberships').fetchone()[0],2)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM team_accounts').fetchone()[0],1)

    def test_member_snapshot_requires_exact_positive_integer_map(self):
        self.roster();claim=self.claim();body=self.approval(claim)
        omitted={key:value for key,value in body.items() if key!='memberRevisions'}
        self.assertEqual(self.admin_call('/review',omitted)[0],400)
        for snapshot in [None,[],{}, {'p1':1}, {'p1':1,'p2':1,'extra':1},
                         {'p1':True,'p2':1},{'p1':'1','p2':1},{'p1':0,'p2':1},
                         {'p1':1.0,'p2':1},{'p1':9007199254740992,'p2':1}]:
            self.assertEqual(self.admin_call('/review',{**body,'memberRevisions':snapshot})[0],400)
        self.assertEqual(self.admin_call('/review',body)[0],200)

    def test_old_installation_initializes_empty_private_tables_idempotently(self):
        team=self.team()
        with self.service.db() as db:
            for table in ['claim_memberships','claim_requests','claim_people']:db.execute('DROP TABLE '+table)
        old=self.service
        for _ in range(2):self.service=EventService(old.data_dir,base_path=old.base_path,origin=old.origin,static_root=old.root)
        self.server.service=self.service
        self.assertEqual(self.admin_call('')[1]['people'],[]);self.assertEqual(self.admin_call('')[1]['teams'][0]['id'],team)
        self.assertEqual(self.public.post('/api/claims/request',{'name':'a','school':'b'})[0],503)


    def declared_group(self,count=3):
        people=[{'id':f'declared-{i}','name':f'Declared Person {i}','school':f'School {i%2}',
                 'notes':'Synthetic registration only','needsReview':False} for i in range(count)]
        self.roster(people)
        payload={'name':people[0]['name'],'school':people[0]['school'],
                 'members':[{k:p[k] for k in ['name','school']} for p in people[1:]]}
        status,claim,_=self.public.post('/api/claims/request',payload);self.assertEqual(status,200,claim)
        return people,payload,claim

    def declared_approval(self,claim,people,**changes):
        ids=[p['id'] for p in people]
        return self.approval(claim,captainId=ids[0],memberIds=ids,declaredMemberIds=ids,**changes)

    def test_declared_group_bounds_duplicates_validation_and_private_status(self):
        people,payload,claim=self.declared_group(5)
        self.assertEqual(claim['declaration'],{'captain':{'name':payload['name'],'school':payload['school']},'members':payload['members']})
        self.assertEqual(claim['requestRevision'],1)
        self.assertEqual(self.bearer(claim)[1]['declaration'],claim['declaration'])
        invalid=[payload['members'][:1],payload['members']+[{'name':'Sixth Person','school':'Extra School'}],
                 [{'name':' ＤＥＣＬＡＲＥＤ   ＰＥＲＳＯＮ ０ ','school':' SCHOOL 0 '},payload['members'][0]],
                 [payload['members'][0],dict(payload['members'][0])],
                 [None,payload['members'][0]],[{'name':'x','school':'y','id':'guess'},payload['members'][0]],
                 [{'name':'','school':'y'},payload['members'][0]],[{'name':'x'*101,'school':'y'},payload['members'][0]],
                 [{'name':'x','school':'y'*201},payload['members'][0]],None]
        for members in invalid:self.assertEqual(self.public.post('/api/claims/request',{**payload,'members':members})[0],400)
        other=self.claim('Other Claimant','Private Other School')
        status=self.bearer(other)[1];self.assertIsNone(status['declaration']);self.assertNotIn(people[0]['name'],json.dumps(status))
        self.assertEqual(self.bearer(claim,secret='x'*43)[1],INVALID)
        serialized=json.dumps(claim)
        for key in ['people','memberIds','notes','needsReview','teamId']:self.assertNotIn('"'+key+'"',serialized)
        listing=self.admin_call('')[1]
        self.assertEqual(next(r for r in listing['requests'] if r['id']==claim['requestId'])['declaration'],claim['declaration'])

    def test_declared_max_escaped_payload_fits_unchanged_public_body_limit(self):
        self.roster()
        payload={'name':'\\'*100,'school':'"'*200,'members':[{'name':('\\'*99)+str(i),'school':'"'*200} for i in range(4)]}
        self.assertLess(len(json.dumps(payload).encode()),16384)
        self.assertEqual(self.public.post('/api/claims/request',payload)[0],200)
        self.assertEqual(self.public.post('/api/claims/request',{**payload,'school':'x'*17000})[0],413)

    def test_pending_declaration_update_revision_snapshots_restart_and_stale_approval(self):
        people,payload,claim=self.declared_group()
        body=self.declared_approval(claim,people)
        with self.service.db() as db:before=dict(db.execute('SELECT * FROM claim_requests WHERE id=?',(claim['requestId'],)).fetchone())
        changed=[{**p,'school':'School alias'} for p in payload['members']]
        response=self.bearer(claim,'update',expectedRevision=1,members=changed)
        self.assertEqual(response[0],200,response[1]);self.assertEqual(response[1]['requestRevision'],2)
        self.assertEqual(response[1]['declaration']['members'],changed)
        self.assertEqual(self.bearer(claim,'update',expectedRevision=1,members=payload['members'])[0],409)
        self.assertEqual(self.admin_call('/review',body)[0],409)
        self.assertEqual(self.bearer(claim,'update',expectedRevision=True,members=payload['members'])[0],400)
        with self.service.db() as db:
            after=dict(db.execute('SELECT * FROM claim_requests WHERE id=?',(claim['requestId'],)).fetchone())
            for key in ['secret_hash','pin_hash','expires_at','name','school']:self.assertEqual(after[key],before[key])
            snapshots=[json.loads(r[0]) for r in db.execute('SELECT declaration_json FROM claim_declarations WHERE request_id=? ORDER BY request_revision',(claim['requestId'],))]
            self.assertEqual(len(snapshots),1);self.assertEqual(snapshots[0]['members'],changed)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM claim_memberships').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM team_accounts').fetchone()[0],0)
        old=self.service;self.service=EventService(old.data_dir,base_path=old.base_path,origin=old.origin,static_root=old.root);self.server.service=self.service
        self.assertEqual(self.bearer(claim)[1]['declaration']['members'],changed)
        self.assertEqual(self.bearer(claim)[1]['requestRevision'],2)

    def test_old_pending_can_supplement_and_old_open_admin_form_is_stale(self):
        self.roster();claim=self.claim();body=self.approval(claim)
        members=[{'name':'Synthetic Member','school':'Fixture School'},{'name':'Third Person','school':'Other School'}]
        self.roster([{'id':'p3',**members[1],'notes':'synthetic','needsReview':False}])
        self.assertEqual(self.bearer(claim,'update',expectedRevision=1,members=members)[0],200)
        self.assertEqual(self.admin_call('/review',body)[0],409)
        current={**body,'expectedRevision':2,'memberIds':['p1','p2','p3'],'memberRevisions':{'p1':1,'p2':1,'p3':1},'declaredMemberIds':['p1','p2','p3']}
        self.assertEqual(self.admin_call('/review',current)[0],200)
        self.assertEqual(self.admin_call('/review',current)[0],200)
        self.assertEqual(self.bearer(claim,'update',expectedRevision=3,members=members)[0],409)
        issued=self.bearer(claim,'redeem')[1]
        self.assertEqual(issued['requestRevision'],4);self.assertEqual(issued['declaration']['members'],members)
        self.assertEqual(self.admin_call('/review',current)[0],200)
        self.assertEqual(self.admin_call('/review',{**current,'expectedRevision':1})[0],409)
        self.assertEqual(self.bearer(claim,'update',expectedRevision=4,members=members)[0],409)
        self.assertEqual(self.bearer(claim,'redeem')[1],issued)
        login=Client(self.server);self.assertEqual(login.post('/api/login',{'role':'team','code':issued['code']})[0],200)

    def test_declared_roster_mapping_cannot_add_omit_duplicate_or_change_people(self):
        people,payload,claim=self.declared_group()
        self.roster([{'id':'unrelated','name':'Unrelated Person','school':'School 0','notes':'synthetic','needsReview':False}])
        body=self.declared_approval(claim,people)
        for change in [
            {'declaredMemberIds':['declared-0','declared-1']},
            {'declaredMemberIds':['declared-0','declared-1','declared-1']},
            {'declaredMemberIds':['declared-1','declared-0','declared-2']},
            {'memberIds':['declared-0','declared-1'],'memberRevisions':{'declared-0':1,'declared-1':1}},
            {'memberIds':body['memberIds']+['unrelated'],'memberRevisions':{**body['memberRevisions'],'unrelated':1}},
            {'memberIds':['declared-0','declared-1','unrelated'],'memberRevisions':{'declared-0':1,'declared-1':1,'unrelated':1},'declaredMemberIds':['declared-0','declared-1','unrelated'],'resolvedIdentity':True}]:
            self.assertIn(self.admin_call('/review',{**body,**change})[0],{400,409})
        self.assertEqual(self.admin_call('/review',{k:v for k,v in body.items() if k!='declaredMemberIds'})[0],400)
        with self.service.db() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM team_accounts').fetchone()[0],0)
        self.assertEqual(self.admin_call('/review',body)[0],200)

    def test_declared_school_aliases_need_explicit_resolution_and_same_name_mapping(self):
        people,payload,claim=self.declared_group()
        members=[{'name':people[1]['name'],'school':'School Alias'},payload['members'][1]]
        self.assertEqual(self.bearer(claim,'update',expectedRevision=1,members=members)[0],200)
        body=self.declared_approval(claim,people,expectedRevision=2)
        self.assertEqual(self.admin_call('/review',body)[0],400)
        self.assertEqual(self.admin_call('/review',{**body,'resolvedIdentity':True,'note':''})[0],400)
        self.assertEqual(self.admin_call('/review',{**body,'declaredMemberIds':['declared-0','declared-2','declared-1'],'resolvedIdentity':True})[0],409)
        self.assertEqual(self.admin_call('/review',{**body,'resolvedIdentity':True})[0],200)

    def test_declared_roster_revision_and_cross_team_conflicts_still_atomic(self):
        people,payload,claim=self.declared_group()
        body=self.declared_approval(claim,people)
        updated={**people[2],'notes':'Onsite verified synthetic'}
        self.assertEqual(self.admin_call('/person',{'expectedRevision':1,'person':updated})[0],200)
        self.assertEqual(self.admin_call('/review',body)[0],409)
        body['memberRevisions']['declared-2']=2
        self.assertEqual(self.admin_call('/review',body)[0],200)
        second=self.public.post('/api/claims/request',payload)[1]
        conflict=self.declared_approval(second,people,team=self.team(2))
        self.assertEqual(self.admin_call('/review',conflict)[0],409)
        with self.service.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM claim_memberships').fetchone()[0],3)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM team_accounts').fetchone()[0],1)

    def test_pending_update_and_approval_race_has_one_authoritative_result(self):
        people,payload,claim=self.declared_group()
        body=self.declared_approval(claim,people)
        with ThreadPoolExecutor(max_workers=2) as pool:
            approval=pool.submit(self.admin_call,'/review',body)
            update=pool.submit(self.bearer,claim,'update',expectedRevision=1,members=list(reversed(payload['members'])))
            responses=[approval.result(),update.result()]
        self.assertEqual(sorted(r[0] for r in responses),[200,409])
        with self.service.db() as db:
            approved=responses[0][0]==200
            self.assertEqual(db.execute('SELECT COUNT(*) FROM claim_memberships').fetchone()[0],3 if approved else 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM team_accounts').fetchone()[0],1 if approved else 0)
            self.assertEqual(db.execute('SELECT revision FROM claim_requests WHERE id=?',(claim['requestId'],)).fetchone()[0],2)

    def test_same_name_cross_school_mapping_and_unicode_normalized_names(self):
        people=[{'id':'p1','name':'Shared Name','school':'First School','notes':'synthetic','needsReview':False},
                {'id':'p2','name':'Shared Name','school':'Second School','notes':'synthetic','needsReview':False},
                {'id':'p3','name':'Third Name','school':'Third School','notes':'synthetic','needsReview':False}]
        self.roster(people)
        payload={'name':'  ＳＨＡＲＥＤ   ＮＡＭＥ ','school':'First School',
                 'members':[{'name':'Shared Name','school':'Second School'},{'name':' THIRD   NAME ','school':'Third School'}]}
        status,claim,_=self.public.post('/api/claims/request',payload);self.assertEqual(status,200,claim)
        body=self.declared_approval(claim,people)
        swapped={**body,'captainId':'p2','declaredMemberIds':['p2','p1','p3']}
        self.assertEqual(self.admin_call('/review',swapped)[0],400)
        self.assertEqual(self.admin_call('/review',body)[0],200)

    def test_repeated_pending_edits_keep_only_current_snapshot_and_freeze_at_approval(self):
        people,payload,claim=self.declared_group(5)
        for revision in range(1,11):
            members=list(reversed(payload['members'])) if revision%2 else payload['members']
            response=self.bearer(claim,'update',expectedRevision=revision,members=members)
            self.assertEqual(response[0],200,response[1])
            with self.service.db() as db:
                snapshots=db.execute('SELECT request_revision,declaration_json FROM claim_declarations WHERE request_id=?',(claim['requestId'],)).fetchall()
                self.assertEqual(len(snapshots),1);self.assertEqual(snapshots[0]['request_revision'],revision+1)
                self.assertEqual(json.loads(snapshots[0]['declaration_json'])['members'],members)
        body=self.declared_approval(claim,people,expectedRevision=11)
        self.assertEqual(self.admin_call('/review',body)[0],200)
        self.bearer(claim,'redeem')
        self.assertEqual(self.admin_call('/review',body)[0],200)
        with self.service.db() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM claim_declarations WHERE request_id=?',(claim['requestId'],)).fetchone()[0],1)

    def test_update_secret_expiry_and_fixed_scope_limits(self):
        people,payload,claim=self.declared_group()
        self.assertEqual(self.bearer(claim,'update',secret='x'*43,expectedRevision=1,members=payload['members'])[1],INVALID)
        self.assertEqual(self.bearer(claim,'update',requestId='0'*32,expectedRevision=1,members=payload['members'])[1],INVALID)
        for _ in range(18):self.assertEqual(self.bearer(claim,'update',expectedRevision=0,members=payload['members'])[0],409)
        self.assertEqual(self.bearer(claim,'update',expectedRevision=1,members=payload['members'])[0],429)
        self.assertEqual(self.bearer(claim)[0],200)
        self.service.login_attempts.clear()
        with self.service.db() as db:db.execute('UPDATE claim_requests SET expires_at=0 WHERE id=?',(claim['requestId'],))
        self.assertEqual(self.bearer(claim,'update',expectedRevision=1,members=payload['members'])[1],INVALID)


if __name__=='__main__':unittest.main()
