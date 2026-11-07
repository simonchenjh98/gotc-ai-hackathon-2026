"""Lifecycle acceptance tests. All data and credentials are synthetic and temporary."""
from test_registration_fixtures import registration_fixture_create, registration_fixture_open
import json
import secrets
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

from errors import APIError
import test_api
import test_open_claims
from test_api import Client


class TeamLifecycleTests(unittest.TestCase):
    setUp=test_api.APITests.setUp
    tearDown=test_api.APITests.tearDown
    team=test_api.APITests.team
    invitation=test_api.APITests.invitation
    application=test_open_claims.OpenClaimsTests.application
    approval=test_open_claims.OpenClaimsTests.approval

    def admin_request(self,method,path,body=None):
        return self.admin.request(method,path,body,headers={'X-GOTC-Login':self.admin.login_id})

    def admin_call(self,suffix,body=None):
        return self.admin_request('GET' if body is None else 'POST','/api/admin/claims'+suffix,body)

    def detail(self,team_id):
        status,body,_=self.admin_request('GET','/api/admin/data/team?teamId='+team_id)
        self.assertEqual(status,200,body)
        return body

    def approved(self,prefix='Synthetic'):
        claim=self.application(prefix)
        status,body,_=self.admin_call('/approve',self.approval(claim))
        self.assertEqual(status,200,body)
        with self.service.db() as db:
            team_id=db.execute('SELECT team_id FROM claim_requests WHERE id=?',(claim['requestId'],)).fetchone()[0]
        return team_id,claim

    def recycle(self,team_id,detail=None,**changes):
        detail=detail or self.detail(team_id)
        return self.admin_request('POST','/api/admin/data/team/recycle',{
            'teamId':team_id,'expectedRevision':detail['team']['revision'],
            'recycleContext':detail['recycleContext'],'reason':'Synthetic recycle reason',**changes})

    def restore(self,team_id,**changes):
        detail=self.detail(team_id)
        return self.admin_request('POST','/api/admin/data/team/restore',{
            'teamId':team_id,'expectedRevision':detail['team']['revision'],
            'name':detail['team']['name'],'reason':'Synthetic restore reason',**changes})

    def test_private_registration_and_empty_rosters(self):
        status,registered,_=registration_fixture_create(self,{'code':secrets.token_urlsafe(32),'team':{
            'name':'Synthetic Registration','captain':'Synthetic Captain','school':'Synthetic School A',
            'phone':'','targetSize':3,'members':['Synthetic Captain','Synthetic Member','Synthetic Third'],
            'project':'','summary':''}})
        self.assertEqual(status,200,registered)
        team_id=registered['team']['id']
        detail=self.detail(team_id)
        self.assertEqual(detail['roster']['source'],'registration')
        captain,member=detail['roster']['members'][:2]
        self.assertEqual(captain['school'],'Synthetic School A')
        self.assertEqual(captain['source'],'registration_captain')
        self.assertIsNone(member['school'])
        self.assertEqual(member['schoolState'],'missing')
        serialized=json.dumps(detail)
        for key in ['phone','notes','secret_hash','code_hash','salt','raw_json']:
            self.assertNotIn(key,serialized)
        empty=self.detail(self.team(2,'Synthetic Empty'))
        self.assertEqual(empty['roster'],{'source':'none','members':[]})
        self.assertRegex(empty['recycleContext'],r'^[0-9a-f]{64}$')

    def test_auth_marker_and_strict_fields(self):
        team_id=self.team(name='Synthetic Private')
        route='/api/admin/data/team?teamId='+team_id
        self.assertEqual(self.public.get(route)[0],401)
        self.assertEqual(self.admin.get(route)[0],428)
        self.assertEqual(self.admin.request('GET',route,headers={'X-GOTC-Login':'stale'})[0],401)
        judge,_=self.invitation()
        self.assertEqual(judge.request('GET',route,headers={'X-GOTC-Login':judge.login_id})[0],403)
        for suffix in ['&unknown=x','&teamId='+team_id]:
            self.assertEqual(self.admin_request('GET',route+suffix)[0],400)
        self.assertEqual(self.admin_request('GET','/api/admin/data/recycle-bin?x=1')[0],400)
        detail=self.detail(team_id)
        base={'teamId':team_id,'reason':'Synthetic reason'}
        response=self.admin_request('POST','/api/admin/data/team/recycle',base)
        self.assertEqual((response[0],response[1]['code']),(428,'revision_required'))
        response=self.admin_request('POST','/api/admin/data/team/recycle',{**base,'expectedRevision':detail['team']['revision']})
        self.assertEqual((response[0],response[1]['code']),(428,'recycle_context_required'))
        self.assertEqual(self.recycle(team_id,recycleContext='bad')[0],400)
        self.assertEqual(self.recycle(team_id,expectedRevision=True)[0],400)
        self.assertEqual(self.recycle(team_id,unknown=True)[0],400)

    def test_approved_snapshot_release_restore_and_credentials(self):
        team_id,claim=self.approved()
        detail=self.detail(team_id)
        self.assertEqual(detail['roster']['source'],'approved_memberships')
        self.assertEqual({m['school'] for m in detail['roster']['members']},{'School A','School B','School C'})
        self.assertEqual([m['name'] for m in detail['roster']['members'] if m['role']=='captain'],['Synthetic Captain'])
        issued=self.public.post('/api/claims/redeem',{'requestId':claim['requestId'],'secret':claim['secret']})[1]
        ordinary=Client(self.server)
        self.assertEqual(ordinary.post('/api/login',{'role':'team','code':issued['code']})[0],200)
        delegate=Client(self.server)
        self.assertEqual(delegate.post('/api/login',{'role':'admin','code':self.admin_code})[0],200)
        self.assertEqual(delegate.request('POST','/api/admin/team-workbench',{'teamId':team_id},headers={'X-GOTC-Login':delegate.login_id})[0],200)
        with self.service.db() as db:
            db.execute('INSERT INTO team_api VALUES (?,1,1)',(team_id,))
            db.execute("INSERT INTO api_keys(id,team_id,name,key_hash,masked,created_at,active) VALUES ('synthetic-key',?,'Synthetic key','synthetic-hash','masked',1,1)",(team_id,))
            memberships=[tuple(r) for r in db.execute('SELECT * FROM claim_memberships ORDER BY person_id')]
            request_before=dict(db.execute('SELECT * FROM claim_requests').fetchone())
        detail=self.detail(team_id)
        status,result,_=self.recycle(team_id,detail)
        self.assertEqual(status,200,result)
        self.assertEqual(result['access']['submissionAccess'],'inactive')
        self.assertEqual(self.detail(team_id)['roster'],detail['roster'])
        self.assertEqual(ordinary.get('/api/team')[0],401)
        self.assertEqual(delegate.get('/api/session')[1]['role'],None)
        self.assertEqual(self.admin.get('/api/session')[1]['role'],'admin')
        with self.service.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM claim_memberships').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT active FROM team_api').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT active FROM api_keys').fetchone()[0],0)
            self.assertEqual(dict(db.execute('SELECT * FROM claim_requests').fetchone()),request_before)
        status,restored,_=self.restore(team_id)
        self.assertEqual(status,200,restored)
        self.assertIsNone(restored['recycle'])
        self.assertEqual(restored['team']['id'],team_id)
        with self.service.db() as db:
            self.assertEqual([tuple(r) for r in db.execute('SELECT * FROM claim_memberships ORDER BY person_id')],memberships)
            self.assertEqual(db.execute('SELECT active FROM accounts WHERE role=\'team\'').fetchone()[0],0)
        self.assertEqual(ordinary.post('/api/login',{'role':'team','code':issued['code']})[0],401)

    def test_context_catches_membership_binding_without_revision_change(self):
        team_id=self.team(name='Synthetic Existing')
        stale=self.detail(team_id)
        with self.service.db() as db:
            db.execute("INSERT INTO claim_people(id,name,school,normalized_name,normalized_school) VALUES ('synthetic-person','Synthetic Captain','Synthetic School','synthetic captain','synthetic school')")
            db.execute("INSERT INTO claim_requests(id,secret_hash,pin_hash,name,school,normalized_name,normalized_school,created_at,expires_at,status,team_id,captain_id) VALUES ('synthetic-request','s','p','Synthetic Captain','Synthetic School','synthetic captain','synthetic school',1,9999999999999,'approved',?,'synthetic-person')",(team_id,))
            db.execute("INSERT INTO claim_memberships VALUES ('synthetic-person',?,'synthetic-request')",(team_id,))
        fresh=self.detail(team_id)
        self.assertEqual(fresh['team']['revision'],stale['team']['revision'])
        self.assertNotEqual(fresh['recycleContext'],stale['recycleContext'])
        status,error,_=self.recycle(team_id,stale)
        self.assertEqual((status,error['code']),(409,'team_context_conflict'))
        self.assertEqual(len(self.detail(team_id)['roster']['members']),1)

    def test_restore_conflicts_and_rollback(self):
        team_id,_=self.approved()
        self.assertEqual(self.recycle(team_id)[0],200)
        other=self.team(2,'Synthetic New Owner')
        with self.service.db() as db:
            member=db.execute('SELECT * FROM team_recycle_memberships LIMIT 1').fetchone()
            db.execute('INSERT INTO claim_memberships VALUES (?,?,?)',(member['person_id'],other,member['request_id']))
        status,error,_=self.restore(team_id,name='Synthetic Free Name')
        self.assertEqual((status,error['code']),(409,'team_restore_conflict'))
        self.assertTrue(any(c['type']=='member_assigned' for c in self.detail(team_id)['restoreConflicts']))
        with self.service.db() as db:db.execute('DELETE FROM claim_memberships')
        with patch.object(self.service,'audit',side_effect=RuntimeError('Synthetic late failure')):
            with self.assertRaises(RuntimeError):
                with self.service.db() as db:
                    db.execute('BEGIN IMMEDIATE')
                    actor=self.service.account(db,self.admin.cookie)
                    self.service.team_lifecycle.restore(db,actor,{'teamId':team_id,'expectedRevision':self.detail(team_id)['team']['revision'],'name':'Synthetic Free Name','reason':'Synthetic reason'})
        self.assertIsNotNone(self.detail(team_id)['recycle'])
        with self.service.db() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM claim_memberships').fetchone()[0],0)
        self.assertEqual(self.restore(team_id,name='Synthetic Free Name')[0],200)

    def test_recycle_hides_current_directory_and_blocks_bypasses(self):
        team_id=self.team(name='Synthetic Directory')
        self.assertEqual(self.recycle(team_id)[0],200)
        directory=self.admin.get('/api/admin')[1]
        self.assertEqual(directory['teams'],[])
        self.assertEqual(directory['submissions'],[])
        self.assertEqual(directory['recycleCount'],1)
        self.assertEqual(directory['stats']['teamCount'],0)
        self.assertEqual(self.admin_request('GET','/api/admin/data/recycle-bin')[1]['teams'][0]['id'],team_id)
        revision=self.detail(team_id)['team']['revision']
        self.assertEqual(self.admin.post('/api/admin/teams',{'id':team_id,'expectedRevision':revision,'number':1,'name':'Synthetic Directory','status':'active'})[0],409)
        self.assertEqual(self.admin.post('/api/admin/team-access',{'teamId':team_id,'action':'issue'})[0],409)
        self.assertEqual(self.admin.post('/api/admin/keys/provision',{'teamId':team_id})[0],409)

    def test_recycle_late_failure_rolls_back_memberships_sessions_and_access(self):
        team_id,_=self.approved()
        detail=self.detail(team_id)
        with self.service.db() as db:
            before={table:[tuple(row) for row in db.execute('SELECT * FROM '+table)] for table in
                ['teams','team_details','accounts','sessions','claim_memberships','team_recycle_events','team_recycle_memberships']}
        with patch.object(self.service,'audit',side_effect=RuntimeError('Synthetic late recycle failure')):
            with self.assertRaises(RuntimeError):
                with self.service.db() as db:
                    db.execute('BEGIN IMMEDIATE')
                    actor=self.service.account(db,self.admin.cookie)
                    self.service.team_lifecycle.recycle(db,actor,{'teamId':team_id,
                        'expectedRevision':detail['team']['revision'],'recycleContext':detail['recycleContext'],'reason':'Synthetic reason'})
        with self.service.db() as db:
            for table,rows in before.items():self.assertEqual([tuple(row) for row in db.execute('SELECT * FROM '+table)],rows,table)

    def test_original_status_repeat_cycles_name_conflicts_and_identity_change(self):
        team_id,_=self.approved()
        with self.service.db() as db:
            db.execute("UPDATE team_details SET status='disqualified',status_reason='Synthetic previous disqualification' WHERE team_id=?",(team_id,))
        self.assertEqual(self.recycle(team_id)[0],200)
        other=self.team(2,'Synthetic Replacement')
        self.assertEqual(self.restore(team_id,name='Ｓｙｎｔｈｅｔｉｃ   Replacement')[0],409)
        with self.service.db() as db:
            identity=db.execute('SELECT person_id FROM team_recycle_memberships LIMIT 1').fetchone()[0]
            db.execute("UPDATE claim_people SET normalized_school='synthetic changed school' WHERE id=?",(identity,))
        self.assertTrue(any(c['type']=='identity_changed' for c in self.detail(team_id)['restoreConflicts']))
        self.assertEqual(self.restore(team_id,name='Synthetic Free')[0],409)
        with self.service.db() as db:
            db.execute('UPDATE claim_people SET normalized_school=(SELECT normalized_school FROM team_recycle_memberships WHERE person_id=? LIMIT 1) WHERE id=?',(identity,identity))
        status,body,_=self.restore(team_id,name='Synthetic Free')
        self.assertEqual(status,200,body)
        self.assertEqual(body['team']['status'],'disqualified')
        self.assertEqual(body['team']['statusReason'],'Synthetic previous disqualification')
        self.assertEqual(self.recycle(team_id)[0],200)
        self.assertEqual(self.restore(team_id)[0],200)
        with self.service.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM team_recycle_events WHERE team_id=?',(team_id,)).fetchone()[0],2)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM team_recycle_memberships').fetchone()[0],6)
            self.assertEqual(db.execute('SELECT number FROM teams WHERE id=?',(other,)).fetchone()[0],2)

    def test_double_mutations_and_save_race_serialize(self):
        team_id=self.team(name='Synthetic Race')
        detail=self.detail(team_id)
        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses=list(pool.map(lambda _:self.recycle(team_id,detail)[0],range(2)))
        self.assertEqual(sorted(statuses),[200,409])
        detail=self.detail(team_id)
        body={'teamId':team_id,'expectedRevision':detail['team']['revision'],'name':detail['team']['name'],'reason':'Synthetic restore'}
        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses=list(pool.map(lambda _:self.admin_request('POST','/api/admin/data/team/restore',body)[0],range(2)))
        self.assertEqual(sorted(statuses),[200,409])
        detail=self.detail(team_id)
        save={'id':team_id,'expectedRevision':detail['team']['revision'],'number':1,'name':'Synthetic Saved'}
        with ThreadPoolExecutor(max_workers=2) as pool:
            tasks=[pool.submit(self.recycle,team_id,detail),pool.submit(self.admin.post,'/api/admin/teams',save)]
            self.assertEqual(sorted(task.result()[0] for task in tasks),[200,409])
        with self.service.db() as db:
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(),[])

    def test_context_catches_registration_account_and_key_changes(self):
        team_id=self.team(name='Synthetic Context')
        changes=[
            lambda db:db.execute("INSERT INTO team_api VALUES (?,1,1)",(team_id,)),
            lambda db:db.execute("INSERT INTO api_keys(id,team_id,name,key_hash,masked,created_at) VALUES ('context-key',?,'Synthetic','context-hash','masked',1)",(team_id,)),
            lambda db:db.execute("UPDATE api_keys SET active=0 WHERE id='context-key'")]
        for change in changes:
            stale=self.detail(team_id)
            with self.service.db() as db:change(db)
            response=self.recycle(team_id,stale)
            self.assertEqual((response[0],response[1]['code']),(409,'team_context_conflict'))
        stale=self.detail(team_id)
        self.assertEqual(self.admin.post('/api/admin/team-access',{'teamId':team_id,'action':'issue'})[0],200)
        self.assertEqual(self.recycle(team_id,stale)[1]['code'],'team_context_conflict')

    def test_private_detail_denies_delegation_and_never_enriches_public_snapshots(self):
        team_id,_=self.approved('Synthetic Secret Roster')
        detail=self.detail(team_id)
        member=detail['roster']['members'][0]['name']
        for path in ['/api/board','/api/registration']:
            self.assertNotIn(member,json.dumps(self.public.get(path)[1]))
        self.assertNotIn('roster',self.admin.get('/api/admin')[1]['teams'][0])
        with self.service.db() as db:
            self.assertNotIn('members',self.service.admin_data.team(db,team_id))
        status,delegated,_=self.admin.request('POST','/api/admin/team-workbench',{'teamId':team_id},headers={'X-GOTC-Login':self.admin.login_id})
        self.assertEqual(status,200,delegated)
        response=self.admin.request('GET','/api/admin/data/team?teamId='+team_id,headers={'X-GOTC-Login':delegated['loginId']})
        self.assertEqual(response[0],403)
        self.assertNotIn(member,json.dumps(response[1]))

    def test_withdrawn_recycle_restore_invalidates_publications_without_changing_snapshots(self):
        team_id=self.team(name='Synthetic Historical')
        snapshot='{"synthetic":"historical snapshot"}'
        with self.service.db() as db:
            db.execute("UPDATE team_details SET status='withdrawn',status_reason='Synthetic original withdrawal' WHERE team_id=?",(team_id,))
            roadshow=db.execute("SELECT run_id FROM judging_stages WHERE stage='roadshow'").fetchone()[0]
            finals=db.execute('SELECT run_id FROM finals_config').fetchone()[0]
            db.execute('INSERT INTO judging_roster VALUES (?,?,?)',(roadshow,team_id,snapshot))
            db.execute('INSERT INTO finals_teams VALUES (?,?)',(finals,team_id))
            db.execute('UPDATE roadshow_scoring_config SET publication_json=?,publication_run_id=?,publication_revision=revision',(snapshot,roadshow))
            db.execute('UPDATE finals_config SET publication_json=?,publication_fingerprint=?',(snapshot,'synthetic-old-publication'))
            before=(db.execute('SELECT revision FROM roadshow_scoring_config').fetchone()[0],db.execute('SELECT eligibility_revision FROM finals_config').fetchone()[0])
        self.assertEqual(self.recycle(team_id)[0],200)
        with self.service.db() as db:
            after=(db.execute('SELECT revision FROM roadshow_scoring_config').fetchone()[0],db.execute('SELECT eligibility_revision FROM finals_config').fetchone()[0])
            self.assertTrue(all(a>b for a,b in zip(after,before)))
            self.assertEqual(db.execute('SELECT snapshot_json FROM judging_roster').fetchone()[0],snapshot)
            self.assertEqual(db.execute('SELECT publication_json FROM roadshow_scoring_config').fetchone()[0],snapshot)
            self.assertEqual(db.execute('SELECT publication_json FROM finals_config').fetchone()[0],snapshot)
        self.assertEqual(self.restore(team_id)[0],200)
        with self.service.db() as db:
            restored=(db.execute('SELECT revision FROM roadshow_scoring_config').fetchone()[0],db.execute('SELECT eligibility_revision FROM finals_config').fetchone()[0])
            self.assertTrue(all(a>b for a,b in zip(restored,after)))
            self.assertEqual(db.execute('SELECT status FROM team_details WHERE team_id=?',(team_id,)).fetchone()[0],'withdrawn')
            self.assertEqual(db.execute('SELECT snapshot_json FROM judging_roster').fetchone()[0],snapshot)

    def test_retired_registration_code_stays_unusable_and_stale_context_rejects_profile_change(self):
        code=secrets.token_urlsafe(32)
        status,registered,_=registration_fixture_create(self,{'code':code,'team':{
            'name':'Synthetic Registered Access','captain':'Synthetic Captain','school':'Synthetic School',
            'phone':'','targetSize':3,'members':['Synthetic Captain','Synthetic One','Synthetic Two'],'project':'','summary':''}})
        self.assertEqual(status,200,registered)
        team_id=registered['team']['id'];stale=self.detail(team_id)
        rotated=self.admin_request('POST','/api/admin/registration/rotate',{'teamId':team_id,'expectedRevision':registered['team']['revision']})
        self.assertEqual(rotated[0],410,rotated[1])
        # Historical profile fixture changes still invalidate old deletion confirmations.
        with self.service.db() as db:db.execute('UPDATE team_registrations SET phone=?,revision=revision+1 WHERE team_id=?',('13900000001',team_id))
        self.assertEqual(self.recycle(team_id,stale)[1]['code'],'team_context_conflict')
        self.assertEqual(self.recycle(team_id)[0],200)
        self.assertEqual(self.public.post('/api/registration/open',{'code':code})[0],410)
        self.assertEqual(self.restore(team_id)[0],200)
        self.assertEqual(self.public.post('/api/registration/open',{'code':code})[0],410)

    def test_gateway_final_recheck_rejects_recycled_and_restored_old_key(self):
        team_id=self.team(name='Synthetic Gateway')
        self.assertEqual(self.admin.post('/api/admin/team-access',{'teamId':team_id,'action':'issue'})[0],200)
        token='synthetic-api-token';handler=SimpleNamespace(headers={'Authorization':'Bearer '+token})
        with self.service.db() as db:
            db.execute('INSERT INTO team_api VALUES (?,1,1)',(team_id,))
            db.execute("INSERT INTO api_keys(id,team_id,name,key_hash,masked,created_at) VALUES ('synthetic-admission',?,'Synthetic',?,'masked',1)",(team_id,self.service.keys.digest(token)))
            key=dict(self.service.keys.lookup(db,handler))
        self.service.keys.recheck_access(handler,{'key':key})
        self.assertEqual(self.recycle(team_id)[0],200)
        with self.assertRaises(APIError) as error:self.service.keys.recheck_access(handler,{'key':key})
        self.assertEqual(error.exception.status,401)
        self.assertEqual(self.restore(team_id)[0],200)
        with self.assertRaises(APIError) as error:self.service.keys.recheck_access(handler,{'key':key})
        self.assertEqual(error.exception.status,401)

    def test_verified_same_name_different_school_members_restore_with_same_captain_school(self):
        def register_and_approve(prefix,member_school):
            captain=prefix+' Captain';name=prefix+' Team'
            status,registration,_=registration_fixture_create(self,{'code':secrets.token_urlsafe(32),'team':{
                'name':name,'captain':captain,'school':'Synthetic Shared Captain School',
                'phone':'','targetSize':3,'members':[captain],'project':'','summary':''}})
            self.assertEqual(status,200,registration)
            team_id=registration['team']['id']
            claim=self.application(prefix,school='Synthetic Shared Captain School',members=[
                {'name':'Synthetic Shared Member','school':member_school},
                {'name':prefix+' Other','school':'Synthetic Other School'}])
            candidate=next(t for t in self.admin_call('')[1]['teams'] if t['id']==team_id)
            status,approval,_=self.admin_call('/approve',self.approval(claim,
                teamId=team_id,teamContext=candidate['teamContext']))
            self.assertEqual(status,200,approval)
            return team_id
        old=register_and_approve('Synthetic Old Verified','Synthetic School B')
        self.assertEqual(self.detail(old)['roster']['source'],'approved_memberships')
        self.assertEqual(self.recycle(old)[0],200)
        new=register_and_approve('Synthetic New Verified','Synthetic School C')
        with self.service.db() as db:
            before=[tuple(row) for row in db.execute('SELECT * FROM claim_memberships WHERE team_id=? ORDER BY person_id',(new,))]
            old_id=db.execute("SELECT person_id FROM team_recycle_memberships WHERE name='Synthetic Shared Member'").fetchone()[0]
            new_id=db.execute("SELECT p.id FROM claim_people p JOIN claim_memberships m ON m.person_id=p.id WHERE m.team_id=? AND p.name='Synthetic Shared Member'",(new,)).fetchone()[0]
        self.assertNotEqual(old_id,new_id)
        status,restored,_=self.restore(old)
        self.assertEqual(status,200,restored)
        with self.service.db() as db:
            self.assertEqual([tuple(row) for row in db.execute('SELECT * FROM claim_memberships WHERE team_id=? ORDER BY person_id',(new,))],before)
            self.assertEqual(db.execute('SELECT team_id FROM claim_memberships WHERE person_id=?',(old_id,)).fetchone()[0],old)
        self.assertEqual(next(m['school'] for m in self.detail(old)['roster']['members'] if m['personId']==old_id),'Synthetic School B')
        self.assertEqual(next(m['school'] for m in self.detail(new)['roster']['members'] if m['personId']==new_id),'Synthetic School C')

    def test_registration_only_restore_preserves_existing_same_school_duplicate_policy(self):
        def register(prefix):
            captain=prefix+' Captain'
            status,registration,_=registration_fixture_create(self,{'code':secrets.token_urlsafe(32),'team':{
                'name':prefix+' Team','captain':captain,'school':'Synthetic Registration School',
                'phone':'','targetSize':3,'members':[captain,'Synthetic Shared Member'],'project':'','summary':''}})
            self.assertEqual(status,200,registration)
            return registration['team']['id']
        old=register('Synthetic Old Registration')
        self.assertEqual(self.detail(old)['roster']['source'],'registration')
        self.assertEqual(self.recycle(old)[0],200)
        register('Synthetic New Registration')
        self.assertEqual([c['type'] for c in self.detail(old)['restoreConflicts']],['registration_overlap'])
        status,error,_=self.restore(old)
        self.assertEqual((status,error['code']),(409,'team_restore_conflict'))


if __name__=='__main__':unittest.main()
