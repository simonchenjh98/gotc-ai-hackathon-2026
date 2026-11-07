"""Existing management codes retain only their historical profile capability."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import secrets
import threading
import unittest
from unittest.mock import patch

from team_registration import code_hash
import test_team_profile as profiles
import test_submissions
from test_registration_fixtures import registration_fixture_create


class LegacyRegistrationAccessTests(unittest.TestCase):
    setUp=profiles.TeamProfileTests.setUp
    tearDown=profiles.TeamProfileTests.tearDown
    owned=profiles.TeamProfileTests.owned
    value=profiles.TeamProfileTests.value
    snapshot=profiles.TeamProfileTests.snapshot
    profile=profiles.TeamProfileTests.profile
    body=profiles.TeamProfileTests.body
    application=profiles.TeamProfileTests.application
    approval=profiles.TeamProfileTests.approval
    admin_call=profiles.TeamProfileTests.admin_call

    def legacy(self,code,action='open',team=None,revision=None,client=None,**extra):
        body={'code':code,**extra}
        if action=='save':body.update(team=team,expectedRevision=revision)
        return (client or self.public).post('/api/registration/'+action,body)

    def save(self,code,current,**changes):
        return self.legacy(code,'save',{**{key:current[key] for key in self.value()},**changes},current['revision'])

    def historical(self):
        code=secrets.token_urlsafe(32)
        status,result,_=registration_fixture_create(self,{'code':code,'team':self.value()})
        self.assertEqual(status,200,result)
        return code,result['team']

    def test_existing_code_without_team_account_reads_and_saves_without_issuing_any_identity(self):
        code,team=self.historical();before=self.snapshot()
        opened=self.legacy(code);self.assertEqual(opened[0],200,opened[1]);self.assertEqual(opened[1]['team']['id'],team['id'])
        self.assertEqual(set(opened[1]),{'team'});self.assertNotIn('Set-Cookie',opened[2]);self.assertEqual(self.public.cookie,'')
        saved=self.save(code,opened[1]['team'],phone='13900000002',summary='Legacy-only edit')
        self.assertEqual(saved[0],200,saved[1]);self.assertEqual(set(saved[1]),{'ok','team'})
        self.assertEqual(saved[1]['team']['phone'],'13900000002');self.assertNotIn('Set-Cookie',saved[2])
        self.assertEqual(self.snapshot(),before)
        for key in ['roster','personId','loginId','accessToken','secret_hash']:self.assertNotIn(key,json.dumps(saved[1]))
        for path in ['/api/team','/api/team/profile','/api/team/score','/api/team/keys','/api/admin','/api/judge']:
            self.assertEqual(self.public.get(path)[0],401,path)
        self.assertEqual(self.public.post('/api/submission',{'expectedVersion':0,'status':'draft','materials':test_submissions.SubmissionAPITests.materials(self)})[0],401)
        self.assertEqual(self.public.post('/api/login',{'role':'team','code':code})[0],401)
        with self.service.db() as db:
            self.assertEqual(db.execute('SELECT secret_hash FROM team_registrations').fetchone()[0],code_hash(code))
            audit=db.execute("SELECT actor_id,role,metadata FROM audit WHERE action='registration.save'").fetchone()
            self.assertEqual(tuple(audit)[:2],(None,None));self.assertEqual(json.loads(audit['metadata'])['accessMethod'],'legacy-registration-code')
            self.assertNotIn(code,' '.join(row[0] for row in db.execute('SELECT metadata FROM audit')))

    def test_original_team_code_session_and_both_entry_revision_conflicts_are_preserved(self):
        team_id,client,access,code=self.owned(registered=True)
        current=self.profile(client)[1];opened=self.legacy(code)[1]['team'];before=self.snapshot()
        cookie,login=client.cookie,client.login_id
        saved=self.save(code,opened,summary='Saved through legacy code')
        self.assertEqual(saved[0],200,saved[1]);self.assertEqual(self.profile(client,self.body(current,summary='Stale session draft'))[0],409)
        refreshed=self.profile(client)[1];self.assertEqual(refreshed['profile']['summary'],'Saved through legacy code')
        self.assertEqual(self.profile(client,self.body(refreshed,summary='Saved through contestant session'))[0],200)
        self.assertEqual(self.save(code,saved[1]['team'],summary='Stale legacy draft')[0],409)
        self.assertEqual(self.legacy(code)[1]['team']['summary'],'Saved through contestant session')
        self.assertEqual(self.snapshot(),before);self.assertEqual(client.cookie,cookie);self.assertEqual(client.login_id,login)
        self.assertEqual(client.get('/api/team')[1]['team']['id'],team_id)
        with self.service.db() as db:
            account=dict(db.execute('SELECT * FROM accounts WHERE id=?',(access['id'],)).fetchone())
            self.assertEqual(db.execute('SELECT secret_hash FROM team_registrations WHERE team_id=?',(team_id,)).fetchone()[0],code_hash(code))
            self.assertEqual(account['active'],1)

    def test_wrong_rotated_and_inert_codes_do_not_authorize_or_disclose_profiles(self):
        code,team=self.historical()
        for invalid in ['x'*43,code+'.wrong','disabled:'+('a'*64),'',None,True]:
            self.assertEqual(self.legacy(invalid)[0],403)
        new_code=secrets.token_urlsafe(32)
        with self.service.db() as db:db.execute('UPDATE team_registrations SET secret_hash=? WHERE team_id=?',(code_hash(new_code),team['id']))
        self.assertEqual(self.legacy(code)[0],403);self.assertEqual(self.legacy(new_code)[0],200)
        with self.service.db() as db:db.execute('UPDATE team_registrations SET secret_hash=? WHERE team_id=?',('disabled:'+secrets.token_hex(32),team['id']))
        self.assertEqual(self.legacy(new_code)[0],403)

    def test_restored_legacy_bearer_checks_keep_the_original_rate_limit(self):
        for _ in range(20):self.assertEqual(self.legacy('x'*43)[0],403)
        self.assertEqual(self.legacy('x'*43)[0],429)

    def test_official_origins_only_restore_legacy_cors_not_session_profile_access(self):
        code,team=self.historical();origin='https://etownyouthai.com'
        for action,body in [('open',{'code':code}),('save',{'code':code,'expectedRevision':team['revision'],'team':self.value()})]:
            path='/api/registration/'+action
            preflight=self.public.request('OPTIONS',path,headers={'Origin':origin,'Access-Control-Request-Method':'POST','Access-Control-Request-Headers':'content-type'})
            self.assertEqual(preflight[0],204)
            result=self.public.request('POST',path,body,origin=origin,headers={'Sec-Fetch-Site':'cross-site'})
            self.assertEqual(result[0],200,result[1]);self.assertEqual(result[2]['Access-Control-Allow-Origin'],origin)
            self.assertNotIn('Access-Control-Allow-Credentials',result[2]);self.assertNotIn('Set-Cookie',result[2])
            self.assertEqual(self.public.request('POST',path,body,origin='https://evil.invalid')[0],403)
        self.assertEqual(self.public.request('POST','/api/team/profile',{},origin=origin,headers={'Sec-Fetch-Site':'cross-site'})[0],403)
        self.assertEqual(self.legacy(code,teamId='another-team')[0],400)
        self.assertEqual(self.legacy(code,'save',self.value(),team['revision'],teamId='another-team')[0],400)

    def test_cookie_target_cannot_change_the_registration_bound_to_the_supplied_code(self):
        first,one,_,first_code=self.owned(1,registered=True)
        second,two,_,_=self.owned(2,registered=True)
        second_before=self.profile(two)[1]
        current=self.legacy(first_code)[1]['team']
        saved=self.legacy(first_code,'save',{**{key:current[key] for key in self.value()},'summary':'Only legacy-code target'},current['revision'],client=two)
        self.assertEqual(saved[0],200,saved[1]);self.assertEqual(saved[1]['team']['id'],first)
        self.assertEqual(self.profile(two)[1],second_before);self.assertEqual(self.profile(one)[1]['profile']['summary'],'Only legacy-code target')
        self.assertEqual(self.profile(two,{**self.body(second_before),'code':first_code,'teamId':first})[0],400)

    def test_approved_members_and_formal_project_guards_apply_to_legacy_save_without_roster_leak(self):
        code=secrets.token_urlsafe(32)
        value=self.value(name='Legacy Team',captain='Legacy Captain',school='School A',members=['Alex','Three','Four'],targetSize=5)
        status,result,_=registration_fixture_create(self,{'code':code,'team':value});self.assertEqual(status,200,result)
        team_id=result['team']['id']
        claim=self.application('Legacy',members=[{'name':'Alex','school':'School B'},{'name':'Alex','school':'School C'},
            {'name':'Three','school':'School D'},{'name':'Four','school':'School E'}])
        target=next(team for team in self.admin_call('')[1]['teams'] if team['id']==team_id)
        approval=self.approval(claim,teamId=team_id,teamContext=target['teamContext'])
        self.assertEqual(self.admin_call('/approve',approval)[0],200)
        opened=self.legacy(code)[1]['team'];self.assertEqual(opened['members'],['Legacy Captain','Alex','Alex','Three','Four'])
        for private in ['personId','roster','School B','School C','School D','School E']:self.assertNotIn(private,json.dumps(opened))
        for changes in [{'captain':'Alex'},{'school':'Another School'},{'members':['Alex','Three','Four']},{'targetSize':4}]:
            self.assertIn(self.save(code,opened,**changes)[0],{400,409})
        before=self.snapshot(['claim_memberships','claim_people','accounts','sessions','team_accounts'])
        self.assertEqual(self.save(code,opened,summary='Five exact people preserved')[0],200)
        self.assertEqual(self.snapshot(list(before)),before)
        with self.service.db() as db:db.execute("INSERT INTO submissions VALUES (?,1,'{}','{}',1,1,1,1)",(team_id,))
        current=self.legacy(code)[1]['team']
        self.assertEqual(self.save(code,current,project='Forbidden formal replacement')[0],409)

    def test_private_visibility_and_all_credentials_survive_legacy_and_session_saves(self):
        team_id,client,_,code=self.owned(registered=True)
        with self.service.db() as db:db.execute('INSERT INTO team_profile_visibility VALUES (?,0)',(team_id,))
        before=self.public.get('/api/registration')[1]
        current=self.legacy(code)[1]['team'];self.assertEqual(self.save(code,current,phone='13900000009')[0],200)
        session=self.profile(client)[1];self.assertFalse(session['listedPublicly'])
        self.assertEqual(self.profile(client,self.body(session,summary='Still private'))[0],200)
        self.assertEqual(self.public.get('/api/registration')[1],before)
        with self.service.db() as db:self.assertEqual(db.execute('SELECT directory_visible FROM team_profile_visibility WHERE team_id=?',(team_id,)).fetchone()[0],0)

    def test_revoke_of_contestant_access_does_not_elevate_or_revoke_legacy_profile_capability(self):
        team_id,client,_,code=self.owned(registered=True)
        self.assertEqual(self.admin.post('/api/admin/team-access',{'teamId':team_id,'action':'revoke'})[0],200)
        self.assertEqual(self.profile(client)[0],401)
        current=self.legacy(code)[1]['team'];self.assertEqual(self.save(code,current,summary='Profile only, no contestant access')[0],200)
        self.assertEqual(self.profile(client)[0],401)
        with self.service.db() as db:
            self.assertEqual(db.execute('SELECT active FROM accounts WHERE id=(SELECT account_id FROM team_accounts WHERE team_id=?)',(team_id,)).fetchone()[0],0)
        self.assertIsNone(self.public.get('/api/session')[1]['role'])
        for state in ['withdrawn','disqualified']:
            with self.service.db() as db:self.service.admin_data.save_team(db,None,{'id':team_id,'status':state,'statusReason':'Synthetic status'})
            self.assertEqual(self.legacy(code)[0],409)

    def test_legacy_failures_roll_back_and_concurrent_entry_saves_serialize(self):
        _,client,_,code=self.owned(registered=True);opened=self.legacy(code)[1]['team'];session=self.profile(client)[1]
        before=self.snapshot(['teams','team_details','team_registrations','claim_people','audit','accounts','sessions'])
        with patch.object(self.service.registration,'sync_people',side_effect=RuntimeError('Synthetic legacy save failure')):
            self.assertEqual(self.save(code,opened,summary='Must roll back')[0],500)
        self.assertEqual(self.snapshot(list(before)),before)
        with ThreadPoolExecutor(max_workers=2) as pool:
            legacy=pool.submit(self.save,code,opened,summary='Legacy winner')
            modern=pool.submit(self.profile,client,self.body(session,summary='Session winner'))
            results=[legacy.result(),modern.result()]
        self.assertEqual(sorted(result[0] for result in results),[200,409])

    def test_recycle_while_legacy_save_waits_revokes_code_and_restore_does_not_revive_it(self):
        code,team=self.historical();current=self.legacy(code)[1]['team'];waiting=threading.Event();original=self.service.db
        detail=self.admin.request('GET','/api/admin/data/team?teamId='+team['id'],headers={'X-GOTC-Login':self.admin.login_id})[1]
        body={'teamId':team['id'],'expectedRevision':detail['team']['revision'],'recycleContext':detail['recycleContext'],'reason':'Synthetic recycle'}
        @contextmanager
        def observed():
            with original() as db:
                db.set_trace_callback(lambda sql:waiting.set() if sql=='BEGIN IMMEDIATE' else None)
                yield db
        with original() as holder,ThreadPoolExecutor(max_workers=1) as pool:
            holder.execute('BEGIN IMMEDIATE')
            with patch.object(self.service,'db',observed):
                future=pool.submit(self.save,code,current,summary='Must not survive recycle')
                try:
                    self.assertTrue(waiting.wait(3))
                    actor=holder.execute("SELECT * FROM accounts WHERE role='admin'").fetchone()
                    self.service.team_lifecycle.recycle(holder,actor,body)
                finally:holder.commit()
                self.assertEqual(future.result(timeout=5)[0],403)
        detail=self.admin.request('GET','/api/admin/data/team?teamId='+team['id'],headers={'X-GOTC-Login':self.admin.login_id})[1]
        restore=self.admin.request('POST','/api/admin/data/team/restore',{'teamId':team['id'],'expectedRevision':detail['team']['revision'],'name':team['name'],'reason':'Synthetic restore'},headers={'X-GOTC-Login':self.admin.login_id})
        self.assertEqual(restore[0],200,restore[1]);self.assertEqual(self.legacy(code)[0],403)


if __name__=='__main__':unittest.main()
