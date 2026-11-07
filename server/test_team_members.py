"""Admin team roster editor. All data is synthetic and temporary."""
import json
import secrets
import unittest

import test_api
import test_open_claims
from test_registration_fixtures import registration_fixture_create
import test_team_lifecycle
from test_api import Client

PATH='/api/admin/data/team/members'


class TeamMembersTests(unittest.TestCase):
    setUp=test_api.APITests.setUp
    tearDown=test_api.APITests.tearDown
    team=test_api.APITests.team
    invitation=test_api.APITests.invitation
    application=test_open_claims.OpenClaimsTests.application
    approval=test_open_claims.OpenClaimsTests.approval
    admin_request=test_team_lifecycle.TeamLifecycleTests.admin_request
    admin_call=test_team_lifecycle.TeamLifecycleTests.admin_call
    detail=test_team_lifecycle.TeamLifecycleTests.detail
    approved=test_team_lifecycle.TeamLifecycleTests.approved
    recycle=test_team_lifecycle.TeamLifecycleTests.recycle
    restore=test_team_lifecycle.TeamLifecycleTests.restore

    def members(self,detail):
        return [{'personId':m['personId'],'name':m['name'],'school':m['school'] or ''} for m in detail['roster']['members']]

    def save(self,team_id,members,detail=None,**changes):
        detail=detail or self.detail(team_id)
        return self.admin_request('POST',PATH,{'teamId':team_id,'rosterContext':detail['rosterContext'],
            'reason':'Synthetic roster fix','members':members,**changes})

    def registered(self):
        status,body,_=registration_fixture_create(self,{'code':secrets.token_urlsafe(32),'team':{
            'name':'Synthetic Registration','captain':'Synthetic Captain','school':'Synthetic School A',
            'phone':'','targetSize':3,'members':['Synthetic Captain','Synthetic Member','Synthetic Third'],
            'project':'','summary':''}})
        self.assertEqual(status,200,body)
        return body['team']['id']

    def test_detail_exposes_context_and_mode(self):
        team_id,_=self.approved()
        detail=self.detail(team_id)
        self.assertRegex(detail['rosterContext'],r'^[0-9a-f]{64}$')
        self.assertEqual(detail['rosterMode'],'approved')
        self.assertEqual(self.detail(self.registered())['rosterMode'],'registration')

    def test_approved_edit_rename_add_remove_and_captain(self):
        team_id,_=self.approved()
        detail=self.detail(team_id)
        members=self.members(detail)
        captain,second,third=members
        second['name']='Synthetic Renamed'
        status,body,_=self.save(team_id,[second,captain,{'personId':None,'name':'Synthetic New','school':'School D'}],detail)
        self.assertEqual(status,200,body)
        roster=body['roster']['members']
        self.assertEqual([m['name'] for m in roster if m['role']=='captain'],['Synthetic Renamed'])
        self.assertEqual({m['name'] for m in roster},{'Synthetic Renamed','Synthetic Captain','Synthetic New'})
        self.assertEqual(roster[0]['personId'],second['personId'])
        with self.service.db() as db:
            self.assertIsNone(db.execute('SELECT 1 FROM claim_memberships WHERE person_id=?',(third['personId'],)).fetchone())
            self.assertIsNotNone(db.execute('SELECT 1 FROM claim_people WHERE id=?',(third['personId'],)).fetchone())
            registration=db.execute('SELECT captain,members_json FROM team_registrations WHERE team_id=?',(team_id,)).fetchone()
            audit=db.execute("SELECT * FROM audit WHERE action='team.members.edit'").fetchone()
        self.assertEqual(registration['captain'],'Synthetic Renamed')
        self.assertIn('Synthetic New',registration['members_json'])
        self.assertIn('Synthetic roster fix',audit['metadata'])
        # The admin directory reflects the edit.
        teams=self.admin_request('GET','/api/admin')[1]['teams']
        listed=next(t for t in teams if t['id']==team_id)['members']
        self.assertEqual(listed[0],{'name':'Synthetic Renamed','role':'captain','school':second['school']})
        # Team profile stays consistent with the verified roster.
        with self.service.db() as db:
            self.assertFalse(self.service.registration.profile(db,team_id)['incomplete'])

    def test_stale_context_and_validation(self):
        team_id,_=self.approved()
        detail=self.detail(team_id)
        members=self.members(detail)
        self.assertEqual(self.save(team_id,members,detail)[0],200)
        status,body,_=self.save(team_id,members,detail)
        self.assertEqual((status,body['code']),(409,'roster_context_conflict'))
        detail=self.detail(team_id)
        cases=[([],400),([{**members[0],'school':''}],400),([members[0],members[0]],400),
               ([{**members[0],'extra':1}],400),([{**members[0],'personId':'bad id'}],400)]
        for value,code in cases:
            self.assertEqual(self.save(team_id,value,detail)[0],code,value)
        self.assertEqual(self.save(team_id,members,detail,reason='')[0],400)
        self.assertEqual(self.save(team_id,members,detail,rosterContext='x')[0],428)
        self.assertEqual(self.save(team_id,members,detail,unknown=1)[0],400)

    def test_cannot_take_member_of_another_team(self):
        first,_=self.approved('Synthetic')
        second,_=self.approved('Other')
        other=self.members(self.detail(second))[1]
        status,body,_=self.save(first,self.members(self.detail(first))+[{**other,'personId':None}])
        self.assertEqual((status,body['code']),(409,'member_assigned'))
        status,body,_=self.save(first,self.members(self.detail(first))[:1]+[other])
        self.assertEqual(status,409,body)

    def test_auth_and_recycled(self):
        team_id,_=self.approved()
        detail=self.detail(team_id)
        body={'teamId':team_id,'rosterContext':detail['rosterContext'],'reason':'x','members':self.members(detail)}
        self.assertEqual(self.public.post(PATH,body)[0],401)
        self.assertEqual(self.admin.post(PATH,body)[0],428)
        judge,_=self.invitation()
        self.assertEqual(judge.request('POST',PATH,body,headers={'X-GOTC-Login':judge.login_id})[0],403)
        self.assertEqual(self.recycle(team_id)[0],200)
        recycled=self.detail(team_id)
        self.assertIsNone(recycled['rosterContext'])
        status,result,_=self.save(team_id,self.members(detail),detail)
        self.assertEqual((status,result['code']),(409,'team_recycled'))

    def test_registration_only_team(self):
        team_id=self.registered()
        detail=self.detail(team_id)
        members=self.members(detail)
        # Registration-only teams record just the captain's school; refuse silent data loss.
        status,body,_=self.save(team_id,[{**members[1],'school':'Synthetic School B'},{**members[0],'school':'School X'}],detail)
        self.assertEqual(status,400,body)
        self.assertEqual(self.save(team_id,[{**members[1],'school':''},members[0]],detail)[0],400)
        status,body,_=self.save(team_id,[{**members[1],'school':'Synthetic School B'},{**members[0],'school':''},
                                         {'personId':None,'name':'Synthetic Fourth','school':''}],detail)
        self.assertEqual(status,200,body)
        self.assertEqual(body['mode'],'registration')
        self.assertEqual([m['name'] for m in body['roster']['members']],['Synthetic Member','Synthetic Captain','Synthetic Fourth'])
        self.assertEqual(body['roster']['members'][0]['school'],'Synthetic School B')
        with self.service.db() as db:
            row=db.execute('SELECT captain,school,target_size FROM team_registrations WHERE team_id=?',(team_id,)).fetchone()
        self.assertEqual(tuple(row),('Synthetic Member','Synthetic School B',3))

    def test_team_without_roster_gets_private_registration(self):
        team_id=self.team(2,'Synthetic Empty')
        detail=self.detail(team_id)
        self.assertEqual(detail['rosterMode'],'registration')
        status,body,_=self.save(team_id,[{'personId':None,'name':'Synthetic Solo','school':'School Z'}],detail)
        self.assertEqual(status,200,body)
        self.assertEqual(body['roster']['members'][0]['name'],'Synthetic Solo')
        with self.service.db() as db:
            self.assertEqual(db.execute('SELECT directory_visible FROM team_profile_visibility WHERE team_id=?',(team_id,)).fetchone()[0],0)
            self.assertEqual(db.execute('SELECT target_size FROM team_registrations WHERE team_id=?',(team_id,)).fetchone()[0],3)

    def test_six_members_are_rejected_without_partial_changes(self):
        team_id,_=self.approved()
        detail=self.detail(team_id)
        members=self.members(detail)
        extra=[{'personId':None,'name':'Synthetic Extra '+str(n),'school':'School Extra'} for n in range(3)]
        with self.service.db() as db:
            before=[tuple(r) for r in db.execute('SELECT * FROM claim_people ORDER BY id')]
        status,body,_=self.save(team_id,members+extra,detail)
        self.assertEqual(status,400,body)
        self.assertEqual(self.detail(team_id)['rosterContext'],detail['rosterContext'])
        with self.service.db() as db:
            self.assertEqual([tuple(r) for r in db.execute('SELECT * FROM claim_people ORDER BY id')],before)

    def test_partial_approved_rosters_keep_profile_access_and_history(self):
        _,judge=self.invitation()
        for count in (1,2):
            with self.subTest(count=count):
                team_id,claim=self.approved('Partial'+str(count))
                bearer={'requestId':claim['requestId'],'secret':claim['secret']}
                status,issued,_=self.public.post('/api/claims/redeem',bearer)
                self.assertEqual(status,200,issued)
                student=Client(self.server)
                self.assertEqual(student.post('/api/login',{'role':'team','code':issued['code']})[0],200)
                with self.service.db() as db:
                    db.execute("INSERT INTO submissions VALUES (?,1,'{}','{}',1,1,1,1)",(team_id,))
                    db.execute("INSERT INTO scores VALUES (?,?,'{}',0,'Synthetic score','submitted',1)",(judge['id'],team_id))
                def snapshot():
                    with self.service.db() as db:
                        return {table:[tuple(r) for r in db.execute('SELECT * FROM '+table+' ORDER BY rowid')]
                            for table in ('accounts','sessions','team_accounts','api_keys','submissions','scores')}
                before=snapshot()
                detail=self.detail(team_id)
                status,body,_=self.save(team_id,self.members(detail)[:count],detail)
                self.assertEqual(status,200,body)
                self.assertEqual(snapshot(),before)
                status,current,_=student.request('GET','/api/team/profile',headers={'X-GOTC-Login':student.login_id})
                self.assertEqual(status,200,current)
                self.assertEqual(current['profile']['targetSize'],3)
                self.assertFalse(current['incomplete'])
                status,saved,_=student.request('POST','/api/team/profile',{
                    'expectedRevision':current['revision'],'expectedTeamRevision':current['teamRevision'],
                    'team':{**current['profile'],'summary':'Still editable'}},headers={'X-GOTC-Login':student.login_id})
                self.assertEqual(status,200,saved)
                self.assertEqual(self.public.post('/api/claims/redeem',bearer)[1]['code'],issued['code'])
                self.assertEqual(snapshot(),before)

    def test_old_invalid_targets_are_repaired_without_expanding_team_size(self):
        approved,_=self.approved('Legacy')
        registered=self.registered()
        for team_id in (approved,registered):
            detail=self.detail(team_id)
            self.assertEqual(self.save(team_id,self.members(detail),detail)[0],200)
            for invalid in (None,0,8):
                with self.subTest(team_id=team_id,target=invalid):
                    with self.service.db() as db:
                        db.execute('UPDATE team_registrations SET target_size=?,revision=revision+1 WHERE team_id=?',(invalid,team_id))
                    detail=self.detail(team_id)
                    status,body,_=self.save(team_id,self.members(detail)[:1],detail)
                    self.assertEqual(status,200,body)
                    with self.service.db() as db:
                        self.assertEqual(db.execute('SELECT target_size FROM team_registrations WHERE team_id=?',(team_id,)).fetchone()[0],3)

    def test_registration_captain_cannot_duplicate_an_exact_verified_identity(self):
        first,_=self.approved('Assigned')
        member=next(m for m in self.members(self.detail(first)) if m['name']=='Assigned One')
        second=self.team(9,'Synthetic Other Registration')
        status,body,_=self.save(second,[{**member,'personId':None}])
        self.assertEqual((status,body.get('code')),(409,'member_assigned'))
        with self.service.db() as db:
            self.assertIsNone(db.execute('SELECT 1 FROM team_registrations WHERE team_id=?',(second,)).fetchone())
            self.assertEqual(db.execute('SELECT team_id FROM claim_memberships WHERE person_id=?',(member['personId'],)).fetchone()[0],first)
        # A different school is a distinct known identity; a member with no
        # recorded school must not be guessed from a matching name elsewhere.
        status,body,_=self.save(second,[{**member,'personId':None,'school':'Different School'},
                                      {'personId':None,'name':'Assigned Captain','school':''}])
        self.assertEqual(status,200,body)

    def test_edited_roster_round_trips_recycle_without_reactivating_credentials(self):
        team_id,_=self.approved('Recycle')
        detail=self.detail(team_id);members=self.members(detail)
        members[1]['name']='Revised Captain'
        status,body,_=self.save(team_id,[members[1],members[0],members[2]],detail)
        self.assertEqual(status,200,body)
        saved=body['roster']
        self.assertEqual(self.recycle(team_id)[0],200)
        self.assertEqual(self.restore(team_id)[0],200)
        self.assertEqual(self.detail(team_id)['roster'],saved)
        with self.service.db() as db:
            self.assertEqual(db.execute('SELECT a.active FROM accounts a JOIN team_accounts t ON t.account_id=a.id WHERE t.team_id=?',(team_id,)).fetchone()[0],0)

    def test_registration_captain_change_keeps_old_contact_only_in_private_audit(self):
        team_id=self.registered()
        with self.service.db() as db:
            db.execute("UPDATE team_registrations SET phone='13900000001',revision=revision+1 WHERE team_id=?",(team_id,))
        detail=self.detail(team_id);members=self.members(detail)
        status,body,_=self.save(team_id,[{**members[1],'school':'Different School'},
                                       {**members[0],'school':''},members[2]],detail)
        self.assertEqual(status,200,body)
        self.assertNotIn('13900000001',json.dumps(body))
        with self.service.db() as db:
            self.assertEqual(db.execute('SELECT phone FROM team_registrations WHERE team_id=?',(team_id,)).fetchone()[0],'')
            metadata=json.loads(db.execute("SELECT metadata FROM audit WHERE action='team.members.edit' ORDER BY id DESC LIMIT 1").fetchone()[0])
        self.assertEqual(metadata['previousContact'],{'captain':'Synthetic Captain','school':'Synthetic School A','phone':'13900000001'})
        self.assertNotIn('13900000001',json.dumps(self.public.get('/api/registration')[1]))


if __name__=='__main__':
    unittest.main()
