"""Unified contestant access using fresh synthetic HTTP services only."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import secrets
import threading
import unittest
from unittest.mock import patch

from test_api import Client
import test_api
import test_online_claims
import test_submissions
from test_registration_fixtures import registration_fixture_create


class TeamProfileTests(unittest.TestCase):
    setUp=test_api.APITests.setUp
    tearDown=test_api.APITests.tearDown
    team=test_api.APITests.team
    invitation=test_api.APITests.invitation
    admin_call=test_online_claims.OnlineClaimsTests.admin_call
    application=test_online_claims.OnlineClaimsTests.application
    approval=test_online_claims.OnlineClaimsTests.approval
    bearer=test_online_claims.OnlineClaimsTests.bearer

    def value(self,**changes):
        return {'name':'Synthetic Profile','captain':'Synthetic Captain','school':'Synthetic School','phone':'13900000001',
                'targetSize':3,'members':['Synthetic One','Synthetic Two'],'project':'','summary':'Synthetic summary',**changes}

    def owned(self,number=1,registered=False):
        legacy=secrets.token_urlsafe(32)
        if registered:
            value=self.value(name='Synthetic Profile '+str(number),captain='Synthetic Captain '+str(number),school='Synthetic School '+str(number),
                             members=['Synthetic One '+str(number),'Synthetic Two '+str(number)])
            status,data,_=registration_fixture_create(self,{'team':value,'code':legacy})
            self.assertEqual(status,200,data);team_id=data['team']['id']
        else:team_id=self.team(number,name='Synthetic Profile '+str(number))
        status,access,_=self.admin.post('/api/admin/team-access',{'teamId':team_id,'action':'issue'})
        self.assertEqual(status,200,access)
        client=Client(self.server);self.assertEqual(client.post('/api/login',{'role':'team','code':access['code']})[0],200)
        return team_id,client,access,legacy

    def profile(self,client,body=None,marker=None,path='/api/team/profile'):
        return client.request('GET' if body is None else 'POST',path,body,
                              headers={'X-GOTC-Login':client.login_id if marker is None else marker})

    def body(self,current,**changes):
        return {'expectedRevision':current['revision'],'expectedTeamRevision':current['teamRevision'],
                'team':{**current['profile'],**changes}}

    def snapshot(self,tables=None):
        tables=tables or ['accounts','sessions','team_accounts','team_api','api_keys']
        with self.service.db() as db:
            return {table:[tuple(row) for row in db.execute('SELECT * FROM '+table+' ORDER BY rowid')] for table in tables}

    def test_original_code_and_session_manage_profile_submission_and_score_without_credential_changes(self):
        team_id,client,access,_=self.owned(registered=True)
        with self.service.db() as db:
            db.execute('INSERT INTO team_api VALUES (?,1,1)',(team_id,))
            db.execute("INSERT INTO api_keys(id,team_id,name,key_hash,masked,created_at) VALUES ('synthetic-key',?,'Synthetic','not-a-real-key','masked',1)",(team_id,))
        original_cookie=client.cookie;original_login=client.login_id;before=self.snapshot()
        current=self.profile(client)[1];result=self.profile(client,self.body(current,phone='13900000002',summary='Edited with original session'))
        self.assertEqual(result[0],200,result[1]);self.assertEqual(result[1]['team']['id'],team_id)
        self.assertEqual(result[1]['profile']['phone'],'13900000002')
        self.assertTrue(result[1]['listedPublicly'])
        self.assertTrue(any(team['id']==team_id for team in self.public.get('/api/registration')[1]['teams']))
        self.assertEqual(client.get('/api/team')[1]['team']['id'],team_id)
        self.assertEqual(self.admin.settings({'submissionsOpen':True})[0],200)
        draft=client.request('POST','/api/submission',{'expectedVersion':0,'status':'draft',
            'materials':test_submissions.SubmissionAPITests.materials(self)},headers={'X-GOTC-Login':original_login})
        self.assertEqual(draft[0],200,draft[1]);self.assertEqual(draft[1]['submission']['version'],1)
        score=client.request('GET','/api/team/score',headers={'X-GOTC-Login':original_login})
        self.assertEqual(score[0],200,score[1]);self.assertEqual(score[1]['team']['name'],current['team']['name'])
        self.assertEqual(client.cookie,original_cookie);self.assertEqual(client.login_id,original_login)
        self.assertEqual(self.snapshot(),before)
        same_code=Client(self.server);self.assertEqual(same_code.post('/api/login',{'role':'team','code':access['code']})[0],200)
        self.assertEqual(same_code.get('/api/team')[1]['team']['id'],team_id)
        with self.service.db() as db:
            audit=db.execute("SELECT actor_id,role,metadata FROM audit WHERE action='team.profile.save'").fetchone()
            self.assertEqual(tuple(audit)[:2],(access['id'],'team'));self.assertNotIn(access['code'],audit['metadata'])

    def test_missing_profile_get_is_read_only_and_first_save_only_creates_original_team_profile(self):
        team_id,client,_,_=self.owned();before=self.snapshot(['accounts','sessions','team_accounts','teams','team_details','team_registrations','claim_people','claim_memberships','audit'])
        current=self.profile(client)[1]
        self.assertEqual(current['revision'],0);self.assertTrue(current['membersEditable']);self.assertEqual(current['profile']['captain'],'')
        self.assertEqual(self.snapshot(list(before)),before)
        body=self.body(current,**self.value(name=current['team']['name'],project=current['team']['project']))
        result=self.profile(client,body);self.assertEqual(result[0],200,result[1]);self.assertEqual(result[1]['revision'],1)
        after=self.snapshot(list(before))
        for table in ['accounts','sessions','team_accounts','claim_memberships']:self.assertEqual(after[table],before[table])
        self.assertEqual(len(after['teams']),1);self.assertEqual(len(after['team_registrations']),1)
        reloaded=self.profile(client)[1];self.assertFalse(reloaded['listedPublicly'])
        self.assertEqual(self.profile(client,self.body(reloaded,summary='Still private'))[0],200)
        self.assertFalse(self.profile(client)[1]['listedPublicly'])
        self.assertFalse(any(team['id']==team_id for team in self.public.get('/api/registration')[1]['teams']))
        with self.service.db() as db:
            row=db.execute('SELECT team_id,secret_hash FROM team_registrations').fetchone()
            self.assertEqual(row['team_id'],team_id);self.assertTrue(row['secret_hash'].startswith('disabled:'))
        self.assertEqual(self.profile(client,body)[0],409)

    def test_roles_marker_query_and_forged_target_are_rejected_without_leaking_other_profile(self):
        first,one,_,_=self.owned(1,registered=True)
        second,two,_,_=self.owned(2,registered=True)
        current=self.profile(one)[1];body=self.body(current,summary='Unauthorized attempt');before=self.snapshot(['team_registrations','teams','audit'])
        for client,status in [(self.public,401),(self.admin,403),(self.invitation('judge','Synthetic Judge')[0],403),(self.invitation('audience','Synthetic Audience')[0],403)]:
            self.assertEqual(self.profile(client)[0],status);self.assertEqual(self.profile(client,body)[0],status)
        for marker,status in [('',428),(two.login_id,401)]:
            self.assertEqual(self.profile(one,marker=marker)[0],status);self.assertEqual(self.profile(one,body,marker=marker)[0],status)
        for extra in [{'teamId':second},{'accountId':'forged'},{'code':'old-management-code'}]:self.assertEqual(self.profile(one,{**body,**extra})[0],400)
        self.assertEqual(self.profile(one,{**body,'team':{**body['team'],'teamId':second}})[0],400)
        self.assertEqual(self.profile(one,path='/api/team/profile?teamId='+second)[0],400)
        one.cookie=two.cookie
        self.assertEqual(self.profile(one)[0],401);self.assertEqual(self.profile(one,body)[0],401)
        self.assertEqual(self.profile(two)[1]['team']['id'],second)
        # Invitations add audit rows; no attempted profile operation may do so.
        self.assertEqual(self.snapshot(['team_registrations','teams']),{key:before[key] for key in ['team_registrations','teams']})

    def test_operator_is_denied_profile_while_existing_delegated_submission_access_remains(self):
        team_id,_,_,_=self.owned()
        result=self.admin.request('POST','/api/admin/team-workbench',{'teamId':team_id},headers={'X-GOTC-Login':self.admin.login_id})
        self.assertEqual(result[0],200,result[1]);self.admin.login_id=result[1]['loginId']
        self.assertEqual(self.profile(self.admin)[0],403)
        self.assertEqual(self.admin.request('GET','/api/team',headers={'X-GOTC-Login':self.admin.login_id})[0],200)

    def test_both_registration_and_team_revisions_are_required(self):
        team_id,client,_,_=self.owned(registered=True);current=self.profile(client)[1];body=self.body(current)
        for changed in [{'expectedRevision':True},{'expectedTeamRevision':False},{'expectedRevision':0},{'expectedTeamRevision':-1}]:
            self.assertEqual(self.profile(client,{**body,**changed})[0],409)
        with self.service.db() as db:self.service.admin_data.save_team(db,None,{'id':team_id,'booth':'Synthetic new booth'})
        self.assertEqual(self.profile(client,body)[0],409)
        current=self.profile(client)[1];self.assertEqual(self.profile(client,self.body(current))[0],200)
        self.assertEqual(self.profile(client,self.body(current))[0],409)

    def test_approved_exact_identities_and_unknown_captain_are_never_guessed(self):
        claim=self.application(members=[{'name':'Alex','school':'School A'},{'name':'Alex','school':'School B'},{'name':'Three','school':'School C'},{'name':'Four','school':'School D'}])
        self.assertEqual(self.admin_call('/approve',self.approval(claim))[0],200)
        issued=self.bearer(claim,'redeem')[1];client=Client(self.server)
        self.assertEqual(client.post('/api/login',{'role':'team','code':issued['code']})[0],200)
        current=self.profile(client)[1];self.assertEqual(current['revision'],0);self.assertFalse(current['membersEditable'])
        self.assertEqual(current['profile']['members'],['Open Captain','Alex','Alex','Three','Four']);self.assertEqual(current['profile']['targetSize'],5)
        public_before=self.public.get('/api/registration')[1]
        self.assertEqual({(m['name'],m['school']) for m in current['roster']['members'] if m['name']=='Alex'},{('Alex','School A'),('Alex','School B')})
        for changes in [{'captain':'Alex'},{'school':'Different School'},{'members':['Alex','Three','Four']},{'targetSize':4}]:
            self.assertIn(self.profile(client,self.body(current,**changes))[0],{400,409})
        before=self.snapshot(['claim_people','claim_memberships','accounts','sessions','team_accounts'])
        saved=self.profile(client,self.body(current,summary='Original five identities retained'))
        self.assertEqual(saved[0],200,saved[1]);self.assertEqual(self.snapshot(list(before)),before)
        self.assertFalse(saved[1]['listedPublicly']);self.assertEqual(self.public.get('/api/registration')[1],public_before)
        with self.service.db() as db:
            row=db.execute('SELECT team_id,captain_id FROM claim_requests WHERE id=?',(claim['requestId'],)).fetchone()
            ids=[member['person_id'] for member in db.execute('SELECT person_id FROM claim_memberships WHERE team_id=?',(row['team_id'],))]
            self.service.registration.sync_verified_members(db,None,row['team_id'],row['captain_id'],[row['captain_id'],*[person for person in ids if person!=row['captain_id']]])
            self.assertEqual(db.execute('SELECT directory_visible FROM team_profile_visibility WHERE team_id=?',(row['team_id'],)).fetchone()[0],0)
        self.assertEqual(self.public.get('/api/registration')[1],public_before)
        with self.service.db() as db:db.execute('UPDATE claim_requests SET captain_id=NULL WHERE id=?',(claim['requestId'],))
        incomplete=self.profile(client)[1];self.assertTrue(incomplete['incomplete']);self.assertEqual(incomplete['profile']['captain'],'')
        self.assertEqual(self.profile(client,self.body(incomplete))[0],409)

    def test_first_save_race_creates_one_profile_and_stale_body_does_not_overwrite(self):
        _,client,_,_=self.owned();current=self.profile(client)[1]
        bodies=[self.body(current,**self.value(summary=summary)) for summary in ['First draft','Second draft']]
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda body:self.profile(client,body),bodies))
        self.assertEqual(sorted(result[0] for result in results),[200,409])
        with self.service.db() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM team_registrations').fetchone()[0],1)

    def test_legacy_two_approved_members_keep_existing_metadata_edit_policy(self):
        team_id,client,access,_=self.owned(registered=True)
        current=self.profile(client)[1];captain=current['profile']['captain'];school=current['profile']['school'];member=current['profile']['members'][1]
        claim=self.public.post('/api/claims/request',{'name':captain,'school':school})[1]
        with self.service.db() as db:
            rows=[db.execute('SELECT id FROM claim_people WHERE name=?',(name,)).fetchone()[0] for name in [captain,member]]
            db.execute("UPDATE claim_requests SET status='approved',team_id=?,captain_id=?,account_id=? WHERE id=?",(team_id,rows[0],access['id'],claim['requestId']))
            db.executemany('INSERT INTO claim_memberships VALUES (?,?,?)',[(person,team_id,claim['requestId']) for person in rows])
            db.execute('UPDATE team_registrations SET members_json=? WHERE team_id=?',(json.dumps([captain,member]),team_id))
        current=self.profile(client)[1];self.assertFalse(current['incomplete']);self.assertFalse(current['membersEditable'])
        before=self.snapshot(['claim_memberships','claim_people','accounts','sessions'])
        saved=self.profile(client,self.body(current,summary='Legacy metadata update'))
        self.assertEqual(saved[0],200,saved[1]);self.assertEqual(saved[1]['profile']['members'],[captain,member])
        self.assertEqual(saved[1]['profile']['targetSize'],3);self.assertEqual(self.snapshot(list(before)),before)

    def test_profile_does_not_bypass_formal_project_or_submission_window(self):
        team_id,client,_,_=self.owned(registered=True)
        with self.service.db() as db:
            db.execute("INSERT INTO submissions VALUES (?,1,'{}','{}',1,1,1,1)",(team_id,))
        current=self.profile(client)[1];self.assertFalse(current['projectEditable'])
        before=self.snapshot(['submissions','settings','judging_roster','accounts','sessions'])
        before['settings']=[row for row in before['settings'] if row[0]!='updatedAt']
        self.assertEqual(self.profile(client,self.body(current,project='Forbidden replacement'))[0],409)
        self.assertEqual(self.profile(client,self.body(current,phone='13900000003'))[0],200)
        after=self.snapshot(list(before));after['settings']=[row for row in after['settings'] if row[0]!='updatedAt']
        self.assertEqual(after,before)

    def test_new_management_code_creation_is_retired_and_admin_import_never_issues_one(self):
        _,_,_,legacy=self.owned(registered=True);before=self.snapshot(['accounts','sessions','teams','team_registrations','audit'])
        for path in ['create']:
            for code in [legacy,'invalid']:
                result=self.public.post('/api/registration/'+path,{'code':code,'team':self.value()})
                self.assertEqual(result[0],410,result[1]);self.assertEqual(result[1]['code'],'registration_code_retired')
        self.assertEqual(self.admin.post('/api/admin/registration/rotate',{})[0],428)
        retired=self.admin.request('POST','/api/admin/registration/rotate',{},headers={'X-GOTC-Login':self.admin.login_id})
        self.assertEqual(retired[0],410);self.assertEqual(self.snapshot(list(before)),before)
        imported=self.admin.request('POST','/api/admin/registration/import',{'rows':[{'sourceKey':'synthetic-import',
            'team':self.value(name='Imported profile',captain='Imported Captain',school='Imported School',members=['Imported One','Imported Two']),
            'raw':{'source':'Synthetic source'}}]},headers={'X-GOTC-Login':self.admin.login_id})
        self.assertEqual(imported[0],200,imported[1]);self.assertNotIn('code',json.dumps(imported[1]));self.assertNotIn('secret',json.dumps(imported[1]))
        with self.service.db() as db:self.assertTrue(db.execute("SELECT secret_hash FROM team_registrations WHERE source_key='synthetic-import'").fetchone()[0].startswith('disabled:'))

    def test_revoked_session_waiting_for_profile_write_lock_is_rejected(self):
        team_id,client,_,_=self.owned(registered=True);current=self.profile(client)[1];waiting=threading.Event();original=self.service.db
        @contextmanager
        def observed():
            with original() as db:
                db.set_trace_callback(lambda sql:waiting.set() if sql=='BEGIN IMMEDIATE' else None)
                yield db
        with original() as holder,ThreadPoolExecutor(max_workers=1) as pool:
            holder.execute('BEGIN IMMEDIATE')
            with patch.object(self.service,'db',observed):
                future=pool.submit(self.profile,client,self.body(current,summary='Must not save'))
                try:
                    self.assertTrue(waiting.wait(3))
                    holder.execute('DELETE FROM sessions WHERE account_id=(SELECT account_id FROM team_accounts WHERE team_id=?)',(team_id,))
                finally:holder.commit()
                self.assertEqual(future.result(timeout=5)[0],401)
        with self.service.db() as db:self.assertEqual(db.execute('SELECT summary FROM team_registrations WHERE team_id=?',(team_id,)).fetchone()[0],'Synthetic summary')

    def test_visibility_upgrade_is_idempotent_and_lazy_creation_failure_rolls_back(self):
        _,legacy,_,_=self.owned(registered=True)
        before=self.snapshot(['team_registrations','accounts','sessions','team_accounts'])
        with self.service.db() as db:
            db.execute('DROP TABLE team_profile_visibility')
            self.service.registration.initialize(db)
            self.service.registration.initialize(db)
        self.assertEqual(self.snapshot(list(before)),before)
        self.assertTrue(self.profile(legacy)[1]['listedPublicly'])
        team_id,client,_,_=self.owned(2);current=self.profile(client)[1]
        before=self.snapshot(['teams','team_details','team_registrations','team_profile_visibility','accounts','sessions','claim_people','audit','settings'])
        with patch.object(self.service.registration,'sync_people',side_effect=RuntimeError('Synthetic profile failure after insert')):
            failed=self.profile(client,self.body(current,**self.value(name='Private new profile',captain='Private Captain',school='Private School',members=['Private One','Private Two'])))
        self.assertEqual(failed[0],500,failed[1]);self.assertEqual(self.snapshot(list(before)),before)
        self.assertEqual(self.profile(client)[1]['revision'],0)

    def test_withdrawn_or_recycled_team_cannot_read_or_save_profile(self):
        team_id,client,_,_=self.owned(registered=True);current=self.profile(client)[1]
        with self.service.db() as db:self.service.admin_data.save_team(db,None,{'id':team_id,'status':'withdrawn','statusReason':'Synthetic pause'})
        self.assertEqual(self.profile(client)[0],409);self.assertEqual(self.profile(client,self.body(current))[0],409)
        with self.service.db() as db:self.service.admin_data.save_team(db,None,{'id':team_id,'status':'active','statusReason':''})
        detail=self.admin.request('GET','/api/admin/data/team?teamId='+team_id,headers={'X-GOTC-Login':self.admin.login_id})[1]
        result=self.admin.request('POST','/api/admin/data/team/recycle',{'teamId':team_id,'expectedRevision':detail['team']['revision'],
            'recycleContext':detail['recycleContext'],'reason':'Synthetic recycle'},headers={'X-GOTC-Login':self.admin.login_id})
        self.assertEqual(result[0],200,result[1]);self.assertEqual(self.profile(client)[0],401)


if __name__=='__main__':unittest.main()
