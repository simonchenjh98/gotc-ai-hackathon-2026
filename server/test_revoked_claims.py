"""Explicit administrator reissue tests with synthetic people and temporary databases."""
from concurrent.futures import ThreadPoolExecutor
import json
import unittest
from unittest.mock import patch

from errors import APIError
from test_api import Client
import test_online_claims
import test_auto_claims


class RevokedClaimsTests(unittest.TestCase):
    setUp=test_online_claims.OnlineClaimsTests.setUp
    tearDown=test_online_claims.OnlineClaimsTests.tearDown
    admin_call=test_online_claims.OnlineClaimsTests.admin_call
    bearer=test_online_claims.OnlineClaimsTests.bearer
    application=test_online_claims.OnlineClaimsTests.application
    registration=test_online_claims.OnlineClaimsTests.registration
    approval=test_online_claims.OnlineClaimsTests.approval
    invitation=test_online_claims.OnlineClaimsTests.invitation
    team=test_online_claims.OnlineClaimsTests.team
    roster=test_online_claims.OnlineClaimsTests.roster
    source=test_auto_claims.AutoClaimsTests.source

    def access(self,team_id,action):
        result=self.admin.post('/api/admin/team-access',{'teamId':team_id,'action':action})
        self.assertEqual(result[0],200,result[1])
        return result[1]

    def target(self):
        code,team,_=self.registration(members=['Open One','Open Two','Open Three','Open Four'],targetSize=5)
        access=self.access(team['id'],'issue')
        old_client=Client(self.server)
        self.assertEqual(old_client.post('/api/login',{'role':'team','code':access['code']})[0],200)
        self.access(team['id'],'revoke')
        return code,team,access,old_client

    def five_people(self,**changes):
        return self.application(members=[{'name':'Open '+name,'school':'School A'} for name in ['One','Two','Three','Four']],**changes)

    def reissue(self,claim,team):
        candidate=next(t for t in self.admin_call('')[1]['revokedTeams'] if t['id']==team['id'])
        return self.approval(claim,teamId=team['id'],teamContext=candidate['teamContext'],
                             reissueRevoked=True,teamName=candidate['name'],track=candidate['track'],note='Synthetic revoked identity reviewed')

    def snapshot(self):
        with self.service.db() as db:
            return {table:[tuple(row) for row in db.execute('SELECT * FROM '+table+' ORDER BY rowid')] for table in
                    ['teams','team_details','team_registrations','claim_requests','claim_declarations','claim_people',
                     'claim_memberships','accounts','team_accounts','sessions','audit']}

    def test_five_people_reuse_same_team_account_and_redeem_new_login_code(self):
        management,team,access,old_client=self.target();claim=self.five_people();body=self.reissue(claim,team)
        listing=self.admin_call('')[1]
        self.assertFalse(any(t['id']==team['id'] for t in listing['teams']))
        before=self.snapshot();rotated=[];original=self.service.team_access

        def capture(db,actor,value):
            result=original(db,actor,value)
            if value['action']=='rotate':rotated.append(result['code'])
            return result

        with patch.object(self.service,'team_access',capture):
            approved=self.admin_call('/approve',body)
            self.assertEqual(approved[0],200,approved[1]);self.assertEqual(approved[1]['status'],'approved')
            self.assertNotIn('code',approved[1]);self.assertEqual(len(rotated),1)
            self.assertEqual(self.admin_call('/approve',body)[0],200);self.assertEqual(len(rotated),1)
        own=self.bearer(claim)[1];self.assertEqual(own['status'],'approved');self.assertIsNone(own['code'])
        self.assertEqual(own['team']['id'],team['id']);self.assertEqual(own['team']['number'],team['number'])
        self.assertEqual(old_client.get('/api/team')[0],401)
        self.assertEqual(self.public.post('/api/login',{'role':'team','code':access['code']})[0],401)
        issued=self.bearer(claim,'redeem')[1];self.assertEqual(issued['status'],'issued');self.assertNotEqual(issued['code'],access['code'])
        self.assertEqual(self.bearer(claim,'redeem')[1],issued)
        self.assertEqual(self.public.post('/api/login',{'role':'team','code':rotated[0]})[0],401)
        client=Client(self.server);self.assertEqual(client.post('/api/login',{'role':'team','code':issued['code']})[0],200)
        self.assertEqual(client.get('/api/team')[1]['team']['id'],team['id'])
        self.assertEqual(self.admin_call('/approve',body)[0],200)
        after=self.snapshot();self.assertEqual(len(after['teams']),1);self.assertEqual(len(after['team_accounts']),1)
        self.assertEqual(after['teams'],before['teams']);self.assertEqual(after['team_details'],before['team_details'])
        self.assertEqual(len(after['accounts']),len(before['accounts']))
        self.assertEqual(after['team_accounts'],before['team_accounts']);self.assertEqual(len(after['claim_memberships']),5)
        with self.service.db() as db:
            request=db.execute('SELECT * FROM claim_requests WHERE id=?',(claim['requestId'],)).fetchone()
            self.assertEqual(request['account_id'],access['id'])
            self.assertEqual(db.execute('SELECT COUNT(*) FROM team_api').fetchone()[0],0)
        saved=self.public.post('/api/registration/open',{'code':management})[1]['team']
        self.assertEqual(saved['targetSize'],5);self.assertEqual(len(saved['members']),5)
        serialized=json.dumps(self.admin_call('')[1])+' '.join(row[-1] for row in after['audit'])
        for secret in [management,access['code'],claim['secret'],claim['pin'],issued['code'],rotated[0]]:self.assertNotIn(secret,serialized)

    def test_explicit_intent_auth_and_strict_fields(self):
        _,team,_,_=self.target();claim=self.five_people();body=self.reissue(claim,team)
        self.assertEqual(self.public.post('/api/admin/claims/approve',body)[0],401)
        self.assertEqual(self.admin.post('/api/admin/claims/approve',body)[0],428)
        self.assertEqual(self.admin_call('/approve',body,login_id='stale')[0],401)
        judge,_=self.invitation('judge','Synthetic Reviewer')
        self.assertEqual(self.admin_call('/approve',body,client=judge)[0],403)
        self.assertEqual(self.admin.request('POST','/api/admin/claims/approve',body,origin=False)[0],403)
        for changes in [{'reissueRevoked':False},{'reissueRevoked':1},{'reissueRevoked':'true'},{'note':''}]:
            self.assertEqual(self.admin_call('/approve',{**body,**changes})[0],400)
        for key in ['teamId','teamContext']:
            self.assertEqual(self.admin_call('/approve',{k:v for k,v in body.items() if k!=key})[0],400)
        onsite={k:v for k,v in body.items() if k not in {'verificationMethod','applicationReviewed'}}
        self.assertEqual(self.admin_call('/approve',{**onsite,'pin':claim['pin'],'inPersonVerified':True})[0],400)
        ordinary={k:v for k,v in body.items() if k!='reissueRevoked'}
        self.assertEqual(self.admin_call('/approve',ordinary)[0],409)
        with self.service.db() as db:
            with self.assertRaises(APIError) as error:self.service.claims.approve_open(db,None,body)
            self.assertEqual(error.exception.status,401)

    def test_existing_five_roster_ids_and_team_without_registration_are_preserved(self):
        team_id=self.team(28,name='Synthetic retained team');access=self.access(team_id,'issue');self.access(team_id,'revoke')
        names=['Open Captain','Open One','Open Two','Open Three','Open Four']
        people=[{'id':'synthetic-person-'+str(i),'name':name,'school':'School A','notes':'Preserved synthetic note','needsReview':False} for i,name in enumerate(names)]
        self.roster(people);claim=self.five_people();body=self.reissue(claim,{'id':team_id});before=self.snapshot()
        self.assertEqual(self.admin_call('/approve',body)[0],200)
        after=self.snapshot()
        self.assertEqual(after['teams'],before['teams']);self.assertEqual(after['team_details'],before['team_details'])
        self.assertEqual(after['claim_people'],before['claim_people']);self.assertEqual(after['team_registrations'],[])
        self.assertEqual(after['team_accounts'],before['team_accounts'])
        with self.service.db() as db:
            members=[dict(row) for row in db.execute('SELECT * FROM claim_memberships WHERE team_id=?',(team_id,))]
            request=db.execute('SELECT * FROM claim_requests WHERE id=?',(claim['requestId'],)).fetchone()
            self.assertEqual({member['person_id'] for member in members},{person['id'] for person in people})
            self.assertEqual(request['captain_id'],people[0]['id']);self.assertEqual(request['account_id'],access['id'])
        self.assertEqual(self.bearer(claim)[1]['team']['number'],28)

    def test_metadata_changes_and_occupied_or_inactive_targets_are_rejected(self):
        _,team,access,_=self.target();claim=self.five_people();body=self.reissue(claim,team)
        for changes in [{'teamName':'Unrequested rename'},{'track':'AI for Science'}]:
            before=self.snapshot();self.assertEqual(self.admin_call('/approve',{**body,**changes})[0],409)
            self.assertEqual(self.snapshot(),before)
        other=self.application('Other')
        scenarios=[
            ("UPDATE claim_requests SET team_id=? WHERE id=?",(team['id'],other['requestId']),
             "UPDATE claim_requests SET team_id=NULL WHERE id=?",(other['requestId'],)),
            ("UPDATE claim_requests SET account_id=? WHERE id=?",(access['id'],other['requestId']),
             "UPDATE claim_requests SET account_id=NULL WHERE id=?",(other['requestId'],)),
            ("UPDATE accounts SET role='admin' WHERE id=?",(access['id'],),
             "UPDATE accounts SET role='team' WHERE id=?",(access['id'],)),
            ("UPDATE team_details SET status='withdrawn' WHERE team_id=?",(team['id'],),
             "UPDATE team_details SET status='active' WHERE team_id=?",(team['id'],)),
        ]
        for mutate,params,restore,restore_params in scenarios:
            with self.subTest(mutate=mutate):
                with self.service.db() as db:db.execute(mutate,params)
                before=self.snapshot()
                self.assertFalse(any(t['id']==team['id'] for t in self.admin_call('')[1]['revokedTeams']))
                self.assertEqual(self.admin_call('/approve',body)[0],409);self.assertEqual(self.snapshot(),before)
                with self.service.db() as db:db.execute(restore,restore_params)
        with self.service.db() as db:
            db.execute("INSERT INTO claim_people(id,name,school,normalized_name,normalized_school) VALUES ('occupied','Occupied Person','Synthetic School','occupied person','synthetic school')")
            db.execute('INSERT INTO claim_memberships VALUES (?,?,?)',('occupied',team['id'],other['requestId']))
        before=self.snapshot()
        self.assertFalse(any(t['id']==team['id'] for t in self.admin_call('')[1]['revokedTeams']))
        self.assertEqual(self.admin_call('/approve',body)[0],409);self.assertEqual(self.snapshot(),before)

    def test_stale_account_and_team_snapshots_reject_without_effects(self):
        _,team,_,_=self.target();claim=self.five_people();body=self.reissue(claim,team)
        self.access(team['id'],'rotate');before=self.snapshot()
        self.assertEqual(self.admin_call('/approve',body)[0],409);self.assertEqual(self.snapshot(),before)
        self.access(team['id'],'revoke');before=self.snapshot()
        self.assertEqual(self.admin_call('/approve',body)[0],409);self.assertEqual(self.snapshot(),before)
        body=self.reissue(claim,team)
        with self.service.db() as db:self.service.admin_data.save_team(db,None,{'id':team['id'],'project':'Synthetic changed project'})
        before=self.snapshot();self.assertEqual(self.admin_call('/approve',body)[0],409);self.assertEqual(self.snapshot(),before)

    def test_failure_after_rotation_rolls_back_every_effect_and_can_retry(self):
        _,team,_,_=self.target();claim=self.five_people();body=self.reissue(claim,team);before=self.snapshot()
        original=self.service.team_access

        def fail_after_rotate(db,actor,value):
            result=original(db,actor,value)
            self.assertEqual(value['action'],'rotate');self.assertTrue(result['active'])
            raise APIError(409,'Synthetic failure after account rotation')

        with patch.object(self.service,'team_access',fail_after_rotate):
            self.assertEqual(self.admin_call('/approve',body)[0],409)
        self.assertEqual(self.snapshot(),before);self.assertEqual(self.bearer(claim)[1]['status'],'pending')
        self.assertEqual(self.admin_call('/approve',body)[0],200)

    def test_concurrent_competing_and_identical_approvals_rotate_once(self):
        _,team,_,_=self.target();claims=[self.five_people(),self.five_people(name='Other Captain')]
        bodies=[self.reissue(claim,team) for claim in claims]
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda body:self.admin_call('/approve',body),bodies))
        self.assertEqual(sorted(result[0] for result in results),[200,409])
        winner=bodies[0] if results[0][0]==200 else bodies[1]
        before=self.snapshot()
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda body:self.admin_call('/approve',body),[winner,winner]))
        self.assertEqual([result[0] for result in results],[200,200]);self.assertEqual(self.snapshot(),before)

    def test_automatic_review_cannot_reissue_revoked_identity(self):
        _,team,_,_=self.target();self.source();before=self.snapshot()
        claim=self.application();self.assertEqual(claim['status'],'pending')
        self.assertEqual(self.bearer(claim)[1]['status'],'pending')
        after=self.snapshot()
        for table in ['teams','accounts','team_accounts','claim_memberships']:self.assertEqual(after[table],before[table])
        self.assertTrue(any(t['id']==team['id'] for t in self.admin_call('')[1]['revokedTeams']))


if __name__=='__main__':unittest.main()
