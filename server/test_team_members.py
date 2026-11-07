"""Admin team roster editor. All data is synthetic and temporary."""
import secrets
import unittest

import test_api
import test_open_claims
from test_registration_fixtures import registration_fixture_create
import test_team_lifecycle

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


if __name__=='__main__':
    unittest.main()
