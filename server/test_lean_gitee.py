"""Offline least-privilege enterprise membership and bound Gitee submission regressions."""
import io
import json
import os
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from errors import APIError
from repositories import Provider, UpstreamError
import test_repositories as fixtures


class LeanGiteeTests(unittest.TestCase):
    fixture=fixtures.RepositoryTests
    for _name in ('team','invitation','access','open_submissions','materials','request','apply','admin_action','preview','submit','enable'):
        locals()[_name]=getattr(fixtures.RepositoryTests,_name)

    def setUp(self):
        env=patch.dict(os.environ,{'GOTC_REPO_BACKUP_ENABLED':'0'})
        env.start()
        self.addCleanup(env.stop)
        self.fixture.setUp(self)
        self.addCleanup(self.fixture.tearDown,self)
        self.enable('gitee')
        self.remote.provider='gitee'
        self.repos.providers['gitee'].request=self.remote

    def application(self):
        return self.apply('gitee',['Developer'])[1]['repository']

    def membership_fixture(self,*,error=None):
        original=self.remote
        self.member=None
        self.invites=0
        self.allow_once=False
        self.lose_invite=False
        def upstream(method,path,body=None):
            if '/collaborators/' in path and method=='PUT':
                if self.allow_once:
                    self.allow_once=False
                else:
                    self.remote.calls.append((method,path,body))
                    raise error or UpstreamError(403,membership_required=True)
            if path.startswith('/enterprises/') and '/members' in path:
                self.remote.calls.append((method,path,body))
                if method=='GET':
                    if self.member is None:raise UpstreamError(404)
                    self.allow_once=self.member.get('active') is True
                    return 200,self.member
                self.invites+=1
                if self.lose_invite:raise UpstreamError()
                self.assertEqual(body,{'username':'Developer','role':'member'})
                return 204,{}
            return original(method,path,body)
        self.repos.providers['gitee'].request=upstream

    def member_data(self,**values):
        return {'user':{'id':456,'login':'Developer'},'role':'member','active':True,**values}

    def test_direct_push_never_requests_enterprise_membership(self):
        result=self.admin_action(self.application())[1]['repository']
        self.assertEqual(result['state'],'ready')
        self.assertFalse(any('/members' in path for _,path,_ in self.remote.calls))

    def test_explicit_requirement_sends_one_member_invite_and_acceptance_still_verifies_push(self):
        self.membership_fixture()
        row=self.admin_action(self.application())[1]['repository']
        self.assertEqual(row['state'],'invited')
        self.assertIn('普通成员邀请',row['message'])
        self.assertEqual(self.invites,1)
        self.open_submissions()
        self.assertEqual(self.preview()[0],409)
        row=self.admin_action(row,'reconcile')[1]['repository']
        self.assertEqual(row['state'],'invited')
        self.assertEqual(self.invites,1)
        self.member=self.member_data(active=False)
        row=self.admin_action(row,'reconcile')[1]['repository']
        self.assertEqual(row['state'],'invited')
        self.member=self.member_data()
        self.remote.gitee_permission='pull'
        row=self.admin_action(row,'reconcile')[1]['repository']
        self.assertEqual(row['state'],'reconcile')
        self.remote.gitee_permission='push'
        row=self.admin_action(row,'reconcile')[1]['repository']
        self.assertEqual(row['state'],'ready')
        self.assertEqual(self.invites,1)
        self.assertFalse(any(m in {'PATCH','DELETE'} or m=='PUT' and '/members' in p for m,p,_ in self.remote.calls))

    def test_enterprise_invite_intent_is_shared_across_teams_and_casing(self):
        self.membership_fixture()
        row=self.admin_action(self.application())[1]['repository']
        self.assertEqual(row['state'],'invited')
        with self.service.db() as db:
            binding=dict(self.repos.get(db,self.team_id))
        binding['team_id']='another-team'
        binding['organization']=binding['organization'].upper()
        self.assertEqual(self.repos.invite(self.repos.providers['gitee'],binding,'developer'),'invited')
        self.assertEqual(self.invites,1)

    def test_atomic_intent_loser_does_not_send_an_enterprise_invitation(self):
        provider=self.repos.providers['gitee']
        row={'organization':'gotc-ai-hackathon','slug':'synthetic'}
        def raced(method,path,body=None):
            self.assertEqual(method,'GET')
            with self.service.db() as db:
                db.execute('INSERT INTO repository_enterprise_invites VALUES (?,?,?)',('gotc-ai-hackathon','developer','attempted'))
            raise UpstreamError(404)
        with patch.object(provider,'invite',side_effect=UpstreamError(403,membership_required=True)),patch.object(provider,'request',side_effect=raced) as request:
            with self.assertRaises(APIError) as error:self.repos.invite(provider,row,'Developer')
            self.assertEqual(error.exception.code,'repository_membership_unknown')
            self.assertEqual(request.call_count,1)

    def test_lost_invitation_response_never_blindly_reinvites_even_after_service_restart(self):
        self.membership_fixture()
        self.lose_invite=True
        row=self.admin_action(self.application())[1]['repository']
        self.assertEqual(row['state'],'reconcile')
        with self.service.db() as db:
            self.repos.initialize(db)
        row=self.admin_action(row,'reconcile')[1]['repository']
        self.assertEqual(row['state'],'reconcile')
        self.assertIn('不会自动重复邀请',row['message'])
        self.assertEqual(self.invites,1)
        self.member=self.member_data()
        row=self.admin_action(row,'reconcile')[1]['repository']
        self.assertEqual(row['state'],'ready')
        self.assertEqual(self.invites,1)

    def test_generic_failures_never_invite(self):
        for error in [UpstreamError(403),UpstreamError(404),UpstreamError(429),UpstreamError()]:
            with self.subTest(error=error.upstream_status):
                self.membership_fixture(error=error)
                row=self.admin_action(self.application())[1]['repository'] if self.invites==0 and not self.remote.repo else self.admin_action(row,'reconcile')[1]['repository']
                self.assertEqual(row['state'],'reconcile')
                self.assertEqual(self.invites,0)
                self.assertFalse(any('/members' in path for _,path,_ in self.remote.calls))

    def test_existing_membership_identity_role_and_effective_permission_fail_closed(self):
        self.membership_fixture()
        row=self.application()
        for member in [self.member_data(role='admin'),self.member_data(user={'id':456,'login':'other'}),
                       self.member_data(user={'id':789,'login':'Developer'}),self.member_data(active=None),self.member_data(enterprise={'path':'other-enterprise'}),{}]:
            with self.subTest(member=member):
                self.member=member
                self.allow_once=False
                row=self.admin_action(row,'approve' if row['state']=='requested' else 'reconcile')[1]['repository']
                self.assertEqual(row['state'],'reconcile')
                self.assertEqual(self.invites,0)

    def test_bound_draft_and_formal_submit_without_duplicate_fields_has_verified_sha_reference(self):
        row=self.admin_action(self.application())[1]['repository']
        self.assertEqual(row['state'],'ready')
        self.open_submissions()
        duplicates={'readmeUrl','license','licenseUrl','sourcesAndLimitations'}
        materials={k:v for k,v in self.materials(codeUrl='',codeVersion='').items() if k not in duplicates}
        status,draft,_=self.request(self.client,'/api/submission',{'requestId':'lean-gitee-draft-0001','expectedVersion':0,'status':'draft','materials':materials})
        self.assertEqual(status,200,draft)
        self.assertNotIn('repositoryMaterialsUrl',draft['submission']['draft'])
        selected=self.preview()[1]
        status,result,_=self.submit(selected,version=1,materials=materials)
        self.assertEqual(status,200,result)
        saved=result['receipt']['materials']
        self.assertEqual(saved['repositoryMaterialsUrl'],row['codeUrl']+'/tree/'+selected['sha'])
        self.assertTrue(all(saved[key]=='' for key in duplicates))
        self.assertEqual(saved['codeVersion'],self.remote.sha)
        self.assertIsNone(result['repositoryBackup'])
        checks=self.request(self.admin,'/api/admin/submissions/'+self.team_id+'/checks?version=2')[1]
        self.assertTrue(any(item['field']=='repositoryMaterialsUrl' and item['url']==saved['repositoryMaterialsUrl'] for item in checks['items']))
        history=self.request(self.client,'/api/submission/history/2')[1]
        self.assertEqual(history['materials'],saved)
        self.remote.timeout_read=True
        replay=self.submit(selected,version=1,materials=materials)[1]
        self.assertTrue(replay['replayed'])
        self.assertEqual(replay['receipt'],result['receipt'])

    def test_unbound_and_github_still_require_duplicate_fields_and_client_cannot_forge_reference(self):
        materials={k:v for k,v in self.materials().items() if k not in {'readmeUrl','license','sourcesAndLimitations'}}
        self.open_submissions()
        self.assertEqual(self.request(self.client,'/api/submission',{'expectedVersion':0,'status':'submitted','materials':materials})[0],400)
        self.assertEqual(self.service.check_materials(self.materials(),True)['license'],'MIT')
        with self.assertRaises(APIError):self.service.check_materials(materials,True)
        self.enable('github')
        self.remote.provider='github'
        github=self.apply('github',['Developer'])[1]['repository']
        self.admin_action(github)
        selected=self.preview()[1]
        self.assertEqual(self.submit(selected,request_id='github-still-required-0001',materials=materials)[0],400)
        forged={**self.materials(),'repositoryMaterialsUrl':'https://gitee.com/other/project/tree/'+'a'*40}
        with self.assertRaises(APIError):self.service.check_materials(forged,True,lean_gitee=True)


class MembershipClassifierTests(unittest.TestCase):
    def test_bounded_explicit_prerequisite_only_on_gitee_collaborator_mutation(self):
        provider=Provider('gitee','org','never-live','main',True)
        cases=[('User must first be a member of the enterprise',True),('该用户必须先加入企业',True),
               ('Forbidden',False),('user is not a member',False),('enterprise quota exceeded',False),
               ('User must be a member of the enterprise. access_token=secret',False),('x'*9000,False)]
        for message,expected in cases:
            raw=json.dumps({'message':message}).encode()
            def fail(*args,**kwargs):raise HTTPError('https://gitee.com/redacted',403,'Forbidden',{},io.BytesIO(raw))
            with self.subTest(message=message[:60]),patch('repositories.build_opener') as opener:
                opener.return_value.open.side_effect=fail
                with self.assertRaises(UpstreamError) as caught:provider.request('PUT','/repos/org/repo/collaborators/Developer',{'permission':'push'})
                self.assertIs(caught.exception.membership_required,expected)
                self.assertNotIn('access_token',caught.exception.message)
        raw=json.dumps({'message':'User must be a member of the enterprise'}).encode()
        for method,path,status in [('GET','/repos/org/repo/collaborators/Developer/permission',403),('PUT','/repos/org/repo/collaborators/Developer',429),('POST','/enterprises/org/members',403)]:
            with patch('repositories.build_opener') as opener:
                opener.return_value.open.side_effect=HTTPError('redacted',status,'Forbidden',{},io.BytesIO(raw))
                with self.assertRaises(UpstreamError) as caught:provider.request(method,path)
                self.assertFalse(caught.exception.membership_required)
