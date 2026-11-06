"""Online organizer review uses explicit intent, never an exchanged device PIN."""
import json
import unittest
import test_claims
import test_open_claims
from test_api import Client


class OnlineClaimsTests(unittest.TestCase):
    setUp=test_open_claims.OpenClaimsTests.setUp
    tearDown=test_open_claims.OpenClaimsTests.tearDown
    admin_call=test_open_claims.OpenClaimsTests.admin_call
    bearer=test_open_claims.OpenClaimsTests.bearer
    roster=test_open_claims.OpenClaimsTests.roster
    team=test_open_claims.OpenClaimsTests.team
    application=test_open_claims.OpenClaimsTests.application
    registration=test_open_claims.OpenClaimsTests.registration
    selected_approval=test_open_claims.OpenClaimsTests.selected_approval
    counts=test_open_claims.OpenClaimsTests.counts
    invitation=test_claims.ClaimsTests.invitation

    def approval(self,claim,**changes):
        body=test_open_claims.OpenClaimsTests.approval(self,claim)
        del body['pin'];del body['inPersonVerified']
        return {**body,'verificationMethod':'online','applicationReviewed':True,**changes}

    test_empty_install=test_open_claims.OpenClaimsTests.test_empty_install_pending_then_atomic_creation_redemption_and_login
    test_stale_references=test_open_claims.OpenClaimsTests.test_reference_context_rejects_relevant_changes_but_not_unrelated_people
    test_same_name_distinction=test_open_claims.OpenClaimsTests.test_same_name_other_school_requires_distinction_and_note
    test_stale_declaration=test_open_claims.OpenClaimsTests.test_metadata_edits_legacy_preservation_and_stale_review
    test_existing_registration=test_open_claims.OpenClaimsTests.test_existing_registration_approval_binds_same_team_and_locks_reviewed_members
    test_stale_registration=test_open_claims.OpenClaimsTests.test_selected_registration_snapshot_rejects_changed_registration_and_team_fields
    test_selection_rollback=test_open_claims.OpenClaimsTests.test_selected_registration_issuance_failure_rolls_back_team_and_registration_changes
    test_same_target_race=test_open_claims.OpenClaimsTests.test_racing_different_claims_cannot_bind_the_same_existing_team
    test_duplicate_verified_names=test_open_claims.OpenClaimsTests.test_verified_same_name_people_allow_metadata_edits_without_losing_any_identity

    def test_five_person_team_registers_is_approved_and_redeems_without_losing_members(self):
        names=['Open One','Open Two','Open Three','Open Four']
        code,registered,value=self.registration(targetSize=5,members=names)
        self.assertEqual(registered['members'],['Open Captain',*names])
        self.assertFalse(registered['recruiting'])
        claim=self.application(members=[{'name':name,'school':'School A'} for name in names])
        body=self.selected_approval(claim,registered['id'])
        self.assertEqual(self.admin_call('/approve',body)[0],200)
        receipt=self.bearer(claim,'redeem')[1]
        self.assertEqual(receipt['team']['id'],registered['id'])
        self.assertEqual(len(receipt['declaration']['members']),4)
        client=Client(self.server)
        self.assertEqual(client.post('/api/login',{'role':'team','code':receipt['code']})[0],200)
        with self.service.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM claim_memberships WHERE team_id=?',(registered['id'],)).fetchone()[0],5)
        current=self.public.post('/api/registration/open',{'code':code})[1]['team']
        self.assertEqual(current['targetSize'],5)
        self.assertEqual(current['members'],['Open Captain',*names])
        changed={**value,'project':'Five-person project'}
        saved=self.public.post('/api/registration/save',{'code':code,'expectedRevision':current['revision'],'team':changed})
        self.assertEqual(saved[0],200,saved[1]);self.assertEqual(len(saved[1]['team']['members']),5)
        extra=[{'name':name,'school':'School A'} for name in [*names,'Open Five']]
        invalid=self.public.post('/api/claims/request',{'name':'Six Captain','school':'School A','teamName':'Six people','track':'','members':extra})
        self.assertEqual(invalid[0],400)

    def test_explicit_online_intent_auth_strict_modes_and_audit(self):
        claim=self.application();body=self.approval(claim)
        self.assertNotIn('pin',body);self.assertNotIn('inPersonVerified',body)
        before=self.bearer(claim,'redeem')[1];self.assertEqual(before['status'],'pending');self.assertIsNone(before['code'])
        self.assertTrue(any(r['id']==claim['requestId'] for r in self.admin_call('')[1]['requests']))
        self.assertEqual(self.public.post('/api/admin/claims/approve',body)[0],401)
        self.assertEqual(self.admin.post('/api/admin/claims/approve',body)[0],428)
        self.assertEqual(self.admin_call('/approve',body,login_id='stale')[0],401)
        judge,_=self.invitation('judge','Fixture Judge')
        self.assertEqual(self.admin_call('/approve',body,client=judge)[0],403)
        self.assertEqual(self.admin.request('POST','/api/admin/claims/approve',body,origin=False)[0],403)
        for changed in [{'verificationMethod':'onsite'},{'verificationMethod':None},{'applicationReviewed':False},
                        {'applicationReviewed':1},{'pin':claim['pin']},{'inPersonVerified':True}]:
            self.assertEqual(self.admin_call('/approve',{**body,**changed})[0],400)
        for omitted in ['verificationMethod','applicationReviewed']:
            self.assertEqual(self.admin_call('/approve',{k:v for k,v in body.items() if k!=omitted})[0],400)
        self.assertEqual(self.counts()['teams'],0)
        self.assertEqual(self.admin_call('/approve',body)[0],200)
        with self.service.db() as db:
            audit=json.loads(db.execute("SELECT metadata FROM audit WHERE action='claims.approve-open'").fetchone()[0])
            self.assertEqual(audit['verificationMethod'],'online');self.assertIs(audit['applicationReviewed'],True)
            self.assertNotIn('inPersonVerified',audit);self.assertNotIn('pin',audit)
        self.bearer(claim,'redeem');self.assertEqual(self.admin_call('/approve',body)[0],200)
        legacy=test_open_claims.OpenClaimsTests.approval(self,claim)
        self.assertEqual(self.admin_call('/approve',legacy)[0],409)

    def fill_roster_to(self,count):
        with self.service.db() as db:
            current=db.execute('SELECT COUNT(*) FROM claim_people').fetchone()[0]
            db.executemany('INSERT INTO claim_people(id,name,school,normalized_name,normalized_school) VALUES (?,?,?,?,?)',
                [(f'capacity-{i}',f'Capacity Person {i}','Reference School',f'capacity person {i}','reference school') for i in range(current,count)])

    def test_open_capacity_failure_leaves_new_and_registered_teams_unchanged(self):
        code,registered,_=self.registration();claim=self.application()
        self.fill_roster_to(499)
        body=self.selected_approval(claim,registered['id'],teamName='Would rename')
        with self.service.db() as db:original=dict(db.execute('SELECT * FROM team_registrations WHERE team_id=?',(registered['id'],)).fetchone())
        before=self.counts();self.assertEqual(self.admin_call('/approve',body)[0],409);self.assertEqual(self.counts(),before)
        with self.service.db() as db:self.assertEqual(dict(db.execute('SELECT * FROM team_registrations WHERE team_id=?',(registered['id'],)).fetchone()),original)
        newcomer=self.application('New people');before=self.counts()
        self.assertEqual(self.admin_call('/approve',self.approval(newcomer))[0],409);self.assertEqual(self.counts(),before)
        self.assertEqual(self.bearer(claim)[1]['status'],'pending');self.assertEqual(self.bearer(newcomer)[1]['status'],'pending')

    def test_open_existing_people_can_be_approved_without_growth_on_legacy_over_capacity_roster(self):
        self.roster([{'id':'known-'+str(i),'name':name,'school':school,'notes':'','needsReview':False}
            for i,(name,school) in enumerate([('Open Captain','School A'),('Open One','School B'),('Open Two','School C')])])
        self.fill_roster_to(501);claim=self.application();self.assertEqual(self.admin_call('/approve',self.approval(claim))[0],200)
        self.assertEqual(self.counts()['claim_people'],501);self.assertEqual(self.counts()['claim_memberships'],3)

    def test_online_sync_uses_assigned_members_final_count_and_allows_metadata_despite_public_homonym(self):
        code,registered,value=self.registration(targetSize=5);claim=self.application()
        self.assertEqual(self.admin_call('/approve',self.selected_approval(claim,registered['id']))[0],200)
        current=self.public.post('/api/registration/open',{'code':code})[1]['team']
        self.assertEqual(current['targetSize'],3);self.assertFalse(current['recruiting']);self.assertEqual(current['phone'],value['phone'])
        with self.service.db() as db:
            db.execute("INSERT INTO teams(id,number,name,project,track,updated_at) VALUES ('homonym',2,'Other team','','',1)")
            db.execute("INSERT INTO team_registrations(team_id,captain,school,phone,target_size,members_json,secret_hash,created_at) VALUES ('homonym','Other Captain','School A','',3,?,'homonym-secret',1)",
                (json.dumps(['Other Captain','Open One']),))
        saved={**value,'targetSize':3,'members':current['members'][1:],'phone':'13900000002','project':'Metadata edit'}
        result=self.public.post('/api/registration/save',{'code':code,'expectedRevision':current['revision'],'team':saved})
        self.assertEqual(result[0],200,result[1]);self.assertEqual(result[1]['team']['members'],current['members'])
        current=result[1]['team']
        self.assertEqual(self.public.post('/api/registration/save',{'code':code,'expectedRevision':current['revision'],'team':{**saved,'targetSize':4}})[0],409)
        self.assertEqual(self.public.post('/api/registration/save',{'code':code,'expectedRevision':current['revision'],'team':{**saved,'members':['Open One']}})[0],409)

    def test_online_changed_captain_clears_stale_contact_and_keeps_private_audit(self):
        code,registered,value=self.registration();claim=self.application(name='Replacement Captain',school='Replacement School')
        body=self.selected_approval(claim,registered['id']);self.assertEqual(self.admin_call('/approve',body)[0],200)
        current=self.public.post('/api/registration/open',{'code':code})[1]['team']
        self.assertEqual(current['captain'],'Replacement Captain');self.assertEqual(current['phone'],'')
        self.assertEqual(current['members'],['Replacement Captain','Open One','Open Two']);self.assertEqual(current['targetSize'],3)
        with self.service.db() as db:
            audit=json.loads(db.execute("SELECT metadata FROM audit WHERE action='registration.verified-members'").fetchone()[0])
            self.assertEqual(audit['previousContact'],{'captain':value['captain'],'school':value['school'],'phone':value['phone']})
        self.assertNotIn(value['phone'],json.dumps(self.public.get('/api/registration')[1]))
        before=current['revision'];self.assertEqual(self.admin_call('/approve',body)[0],200)
        self.assertEqual(self.public.post('/api/registration/open',{'code':code})[1]['team']['revision'],before)
