"""Offline least-privilege enterprise membership and bound Gitee submission regressions."""
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

    def membership_fixture(self,*,error=None,grant_requires_member=True):
        original=self.remote
        self.member=None
        self.invites=0
        self.lose_invite=False
        self.grant_blocked=False
        def upstream(method,path,body=None):
            if method=='PUT' and '/collaborators/' in path and (self.grant_blocked or grant_requires_member and not (
                    isinstance(self.member,dict) and self.member.get('active') is True)):
                # Model an enterprise that refuses repository grants to non-members.
                self.remote.calls.append((method,path,body))
                raise UpstreamError(403)
            if path.startswith('/enterprises/') and '/members' in path:
                self.remote.calls.append((method,path,body))
                if method=='GET':
                    if error is not None:raise error
                    if self.member is None:raise UpstreamError(404)
                    return 200,self.member
                self.invites+=1
                if self.lose_invite:raise UpstreamError()
                self.assertEqual(body,{'username':'Developer','role':'member'})
                return 204,{}
            return original(method,path,body)
        self.repos.providers['gitee'].request=upstream

    def granted(self):
        return [c for c in self.remote.calls if c[0]=='GET' and c[1].endswith('/permission')]

    def member_data(self,**values):
        return {'user':{'id':456,'login':'Developer'},'role':'member','active':True,**values}

    def test_single_member_query_precedes_grant_without_reinviting_known_member(self):
        result=self.admin_action(self.application())[1]['repository']
        self.assertEqual(result['state'],'ready')
        membership=next(i for i,(method,path,_) in enumerate(self.remote.calls) if method=='GET' and path.endswith('/members/Developer'))
        grant=next(i for i,(method,path,_) in enumerate(self.remote.calls) if method=='PUT' and '/collaborators/' in path)
        self.assertLess(membership,grant)
        self.assertFalse(any(method=='POST' and '/members' in path for method,path,_ in self.remote.calls))

    def test_absent_member_is_invited_once_before_grant_and_acceptance_still_verifies_push(self):
        self.membership_fixture()
        row=self.admin_action(self.application())[1]['repository']
        self.assertEqual(row['state'],'invited')
        self.assertIn('待接受邀请：Developer',row['message'])
        self.assertEqual(self.granted(),[])
        self.assertEqual(self.invites,1)
        self.open_submissions()
        # The repository exists: a pending member must not block the team from submitting.
        self.assertEqual(self.preview()[0],200)
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
        grant=next(i for i,(method,path,_) in enumerate(self.remote.calls) if method=='PUT' and '/collaborators/' in path)
        self.assertTrue(any(method=='GET' and path.endswith('/members/Developer') for method,path,_ in self.remote.calls[:grant]))
        self.assertFalse(any(m in {'PATCH','DELETE'} or m=='PUT' and '/members' in p for m,p,_ in self.remote.calls))

    def test_existing_supported_roles_are_preserved_but_effective_admin_is_not_ready(self):
        self.membership_fixture()
        self.member=self.member_data(role='outsourced',active=False)
        row=self.admin_action(self.application())[1]['repository']
        self.assertEqual(row['state'],'invited')
        self.assertEqual(self.invites,0)
        self.assertEqual(self.granted(),[])
        for role in ('member','outsourced','admin'):
            with self.subTest(role=role):
                self.member=self.member_data(role=role)
                row=self.admin_action(row,'approve' if row['state']=='requested' else 'reconcile')[1]['repository']
                self.assertEqual(row['state'],'ready')
                self.assertEqual(self.member['role'],role)
                self.assertEqual(self.invites,0)
        self.remote.gitee_permission='admin'
        row=self.admin_action(row,'reconcile')[1]['repository']
        self.assertEqual(row['state'],'reconcile')
        self.assertIn('仓库管理权限',row['message'])
        self.assertFalse(any(method!='GET' and '/members' in path for method,path,_ in self.remote.calls))

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
            if method=='PUT':
                raise UpstreamError(403)
            self.assertEqual(method,'GET')
            with self.service.db() as db:
                db.execute('INSERT INTO repository_enterprise_invites VALUES (?,?,?)',('gotc-ai-hackathon','developer','attempted'))
            raise UpstreamError(404)
        with patch.object(provider,'request',side_effect=raced) as request:
            with self.assertRaises(APIError) as error:self.repos.invite(provider,row,'Developer')
            self.assertEqual(error.exception.code,'repository_membership_unknown')
            self.assertFalse(any(c.args[0]=='POST' for c in request.call_args_list))

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
        for error in [UpstreamError(401),UpstreamError(403),UpstreamError(429),UpstreamError(500),UpstreamError()]:
            with self.subTest(error=error.upstream_status):
                self.membership_fixture(error=error)
                row=self.admin_action(self.application())[1]['repository'] if self.invites==0 and not self.remote.repo else self.admin_action(row,'reconcile')[1]['repository']
                self.assertEqual(row['state'],'reconcile')
                self.assertEqual(self.invites,0)
                self.assertEqual(self.granted(),[])
                self.assertFalse(any(method=='POST' and '/members' in path for method,path,_ in self.remote.calls))
                with self.service.db() as db:
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM repository_enterprise_invites').fetchone()[0],0)

    def test_existing_membership_identity_role_and_effective_permission_fail_closed(self):
        self.membership_fixture()
        self.grant_blocked=True
        row=self.application()
        for member in [self.member_data(role='unknown'),self.member_data(role=[]),self.member_data(user={'id':456,'login':'other'}),
                       self.member_data(user={'id':789,'login':'Developer'}),self.member_data(active=None),self.member_data(enterprise={'path':'other-enterprise'}),{}]:
            with self.subTest(member=member):
                self.member=member
                row=self.admin_action(row,'approve' if row['state']=='requested' else 'reconcile')[1]['repository']
                self.assertEqual(row['state'],'reconcile')
                self.assertEqual(self.invites,0)

    def test_unavailable_member_api_falls_back_to_verified_repository_grant(self):
        # Token without enterprise scope / free-plan seat limits: still grant push on the team repo.
        for error in [UpstreamError(401),UpstreamError(403),UpstreamError(500)]:
            with self.subTest(error=error.upstream_status):
                self.membership_fixture(error=error,grant_requires_member=False)
                row=self.admin_action(self.application())[1]['repository'] if not self.remote.repo else self.admin_action(row,'reconcile')[1]['repository']
                self.assertEqual(row['state'],'ready')
                self.assertEqual(self.invites,0)
                self.assertEqual(len(self.granted()),1)
                self.remote.calls.clear()
                if error.upstream_status!=500:
                    with self.service.db() as db:
                        db.execute("UPDATE repository_bindings SET state='reconcile'")

    def test_known_enterprise_user_id_survives_grant_and_readback_failures(self):
        self.membership_fixture(grant_requires_member=False)
        row=self.application()
        for active in (True,False):
            for mismatch in ('grant','permission'):
                with self.subTest(active=active,mismatch=mismatch):
                    self.member=self.member_data(user={'id':789,'login':'Developer'},active=active)
                    self.remote.gitee_member_response=(200,{'id':789,'login':'Developer'}) if mismatch=='permission' else None
                    self.remote.calls.clear()
                    row=self.admin_action(row,'approve' if row['state']=='requested' else 'reconcile')[1]['repository']
                    self.assertEqual(row['state'],'reconcile' if active else 'invited')
                    grants=[c for c in self.remote.calls if '/collaborators/' in c[1]]
                    # The fake grants/permission readback report id 456. Do not
                    # retry without the enterprise-confirmed id 789 constraint.
                    self.assertEqual(sum(c[0]=='PUT' for c in grants),1)
                    self.assertEqual(sum(c[0]=='GET' for c in grants),int(mismatch=='permission'))
                    self.assertEqual(self.invites,0)

    def test_invalid_enterprise_identity_never_falls_back_to_repository_grant(self):
        self.membership_fixture(grant_requires_member=False)
        row=self.application()
        for member in [self.member_data(user={'id':456,'login':'other'}),
                       self.member_data(enterprise={'path':'other-enterprise'}),
                       self.member_data(role='unknown'),self.member_data(active=None),{}]:
            with self.subTest(member=member):
                self.member=member
                self.remote.calls.clear()
                row=self.admin_action(row,'approve' if row['state']=='requested' else 'reconcile')[1]['repository']
                self.assertEqual(row['state'],'reconcile')
                self.assertFalse(any('/collaborators/' in path for _,path,_ in self.remote.calls))
                self.assertEqual(self.invites,0)

    def test_failed_invitation_still_tries_repository_grant_without_reinviting(self):
        self.membership_fixture(grant_requires_member=False)
        self.lose_invite=True
        row=self.admin_action(self.application())[1]['repository']
        self.assertEqual(row['state'],'ready')
        self.assertEqual(self.invites,1)

    def test_one_bad_account_does_not_block_teammates(self):
        self.membership_fixture()
        self.member=self.member_data()
        def upstream(method,path,body=None,inner=self.repos.providers['gitee'].request):
            if '/members/Ghost' in path:
                self.remote.calls.append((method,path,body));raise UpstreamError(403)
            if method=='PUT' and path.endswith('/collaborators/Ghost'):
                self.remote.calls.append((method,path,body));raise UpstreamError(404)
            return inner(method,path,body)
        self.repos.providers['gitee'].request=upstream
        row=self.apply('gitee',['Developer','Ghost'])[1]['repository']
        row=self.admin_action(row)[1]['repository']
        self.assertEqual(row['state'],'reconcile')
        self.assertIn('已获得写权限：Developer',row['message'])
        self.assertIn('未能确认：Ghost',row['message'])
        self.assertTrue(any(c[1].endswith('/collaborators/Developer/permission') for c in self.remote.calls))
        self.open_submissions()
        self.assertEqual(self.preview()[0],200)

    def test_team_can_refresh_its_own_access_after_accepting_invitation(self):
        self.membership_fixture()
        row=self.admin_action(self.application())[1]['repository']
        self.assertEqual(row['state'],'invited')
        status,result,_=self.request(self.client,'/api/team/repository/refresh',{})
        self.assertEqual((status,result['repository']['state']),(200,'invited'))
        self.member=self.member_data()
        status,result,_=self.request(self.client,'/api/team/repository/refresh',{})
        self.assertEqual(status,200,result)
        self.assertEqual(result['repository']['state'],'ready')
        self.assertTrue(result['repository']['canSubmit'])
        self.assertEqual(self.invites,1)
        # Ready repositories are not re-checked remotely, and extra fields are rejected.
        calls=len(self.remote.calls)
        self.assertEqual(self.request(self.client,'/api/team/repository/refresh',{})[0],200)
        self.assertEqual(len(self.remote.calls),calls)
        self.assertEqual(self.request(self.client,'/api/team/repository/refresh',{'teamId':'x'})[0],400)
        self.assertEqual(self.request(self.client,'/api/team/repository/refresh',{},login=False)[0],428)

    def test_team_refresh_is_rate_limited_per_team(self):
        self.membership_fixture()
        self.admin_action(self.application())
        statuses=[self.request(self.client,'/api/team/repository/refresh',{})[0] for _ in range(7)]
        self.assertEqual(statuses[:6],[200]*6)
        self.assertEqual(statuses[6],429)

    def test_requested_application_waits_for_admin_unless_auto_approve(self):
        row=self.application()
        self.assertEqual(row['state'],'requested')
        self.assertFalse(row['canSubmit'])
        result=self.request(self.client,'/api/team/repository/refresh',{})[1]['repository']
        self.assertEqual(result['state'],'requested')
        self.assertFalse(any(c[0]=='POST' for c in self.remote.calls))
        self.repos.auto_approve=True
        result=self.request(self.client,'/api/team/repository/refresh',{})[1]['repository']
        self.assertEqual(result['state'],'ready')

    def test_auto_approve_creates_repository_when_team_applies(self):
        self.repos.auto_approve=True
        providers=self.request(self.client,'/api/team/repository')[1]['providers']
        self.assertTrue(next(p for p in providers if p['provider']=='gitee')['autoApprove'])
        row=self.application()
        self.assertEqual(row['state'],'ready')
        self.assertTrue(row['canSubmit'])
        self.assertEqual([c[1] for c in self.remote.calls if c[0]=='POST'],['/enterprises/gotc-ai-hackathon/repos'])
        with self.service.db() as db:
            events=[r[0] for r in db.execute("SELECT action FROM audit WHERE action LIKE 'repository.%' ORDER BY rowid")]
        self.assertEqual(events[0],'repository.requested')
        self.assertEqual(events[-1],'repository.ready')

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

    def test_unchanged_formal_snapshot_saved_as_draft_is_clean_but_real_edits_remain_dirty(self):
        self.admin_action(self.application())
        self.open_submissions()
        selected=self.preview()[1]
        status,formal,_=self.submit(selected)
        self.assertEqual(status,200,formal)
        saved=formal['receipt']['materials']
        self.assertIn('repositoryMaterialsUrl',saved)
        status,unchanged,_=self.request(self.client,'/api/submission',{
            'requestId':'same-formal-as-draft-0001','expectedVersion':1,'status':'draft','materials':saved})
        self.assertEqual(status,200,unchanged)
        self.assertFalse(unchanged['submission']['hasUnsubmittedChanges'])
        self.assertNotIn('repositoryMaterialsUrl',unchanged['submission']['draft'])
        self.assertEqual(unchanged['submission']['submitted'],saved)
        for version,(field,value) in enumerate((('summary','An actual edit'),('codeVersion','b'*40),
                                               ('codeUrl','https://gitee.com/other/project'),('confirmed',False)),start=2):
            with self.subTest(field=field):
                edited={key:entry for key,entry in saved.items() if key!='repositoryMaterialsUrl'}
                edited[field]=value
                status,result,_=self.request(self.client,'/api/submission',{
                    'requestId':'real-draft-edit-'+field,'expectedVersion':version,'status':'draft','materials':edited})
                self.assertEqual(status,200,result)
                self.assertTrue(result['submission']['hasUnsubmittedChanges'])
                self.assertEqual(result['submission']['submitted'],saved)
        self.assertEqual(self.request(self.client,'/api/submission/history/1')[1]['materials'],saved)

    def test_legacy_material_differences_remain_dirty_across_generated_metadata(self):
        self.open_submissions()
        original=self.materials()
        status,formal,_=self.request(self.client,'/api/submission',{
            'requestId':'legacy-before-gitee-0001','expectedVersion':0,'status':'submitted','materials':original})
        self.assertEqual(status,200,formal)
        saved=formal['submission']['submitted']
        self.admin_action(self.application())
        status,draft,_=self.request(self.client,'/api/submission',{
            'requestId':'legacy-bound-gitee-draft-0001','expectedVersion':1,'status':'draft','materials':saved})
        self.assertEqual(status,200,draft)
        self.assertTrue(draft['submission']['hasUnsubmittedChanges'])
        self.assertEqual(draft['submission']['submitted'],saved)
        self.assertEqual(self.request(self.client,'/api/submission/history/1')[1]['materials'],saved)

    def test_bound_transition_ignores_only_nonapplicable_repo_fields_before_validation(self):
        self.admin_action(self.application());self.open_submissions()
        old={**self.materials(),'readmeUrl':'unfinished-readme','licenseUrl':'http://invalid',
             'license':'x'*500,'sourcesAndLimitations':'old text to move into repo'}
        status,result,_=self.request(self.client,'/api/submission',{
            'requestId':'transition-draft-0001','expectedVersion':0,'status':'draft','materials':old})
        self.assertEqual(status,200,result)
        for field in ('readmeUrl','licenseUrl','license','sourcesAndLimitations'):self.assertEqual(result['submission']['draft'][field],'')
        self.assertEqual(result['submission']['draft']['projectName'],old['projectName'])
        for field in ('demoUrl','slidesUrl','videoUrl'):
            with self.assertRaises(APIError):self.service.check_materials({**old,field:'http://invalid'},False,lean_gitee=True)
        with self.assertRaises(APIError):self.service.check_materials(old,False,lean_gitee=False)

    def test_unbound_and_github_optional_fields_and_client_cannot_forge_reference(self):
        materials={k:v for k,v in self.materials().items() if k not in {'readmeUrl','license','sourcesAndLimitations'}}
        self.open_submissions()
        self.assertEqual(self.request(self.client,'/api/submission',{'expectedVersion':0,'status':'submitted','materials':materials})[0],200)
        self.assertEqual(self.service.check_materials(self.materials(),True)['license'],'MIT')
        self.assertEqual(self.service.check_materials(materials,True)['license'],'')
        self.enable('github')
        self.remote.provider='github'
        github=self.apply('github',['Developer'])[1]['repository']
        self.admin_action(github)
        selected=self.preview()[1]
        self.assertEqual(self.submit(selected,version=1,request_id='github-optional-fields-0001',materials={**materials,'codeUrl':'','codeVersion':''})[0],200)
        forged={**self.materials(),'repositoryMaterialsUrl':'https://gitee.com/other/project/tree/'+'a'*40}
        with self.assertRaises(APIError):self.service.check_materials(forged,True,lean_gitee=True)


class ProviderErrorRedactionTests(unittest.TestCase):
    def test_upstream_errors_discard_bodies_instead_of_inferencing_membership(self):
        provider=Provider('gitee','org','never-live','main',True)
        for status in (400,403,404,422,429):
            with self.subTest(status=status),patch('repositories.build_opener') as opener:
                error=HTTPError('https://gitee.com/redacted',status,'Forbidden',{},None)
                error.read=lambda *args: self.fail('HTTP error bodies must not be inspected')
                opener.return_value.open.side_effect=error
                with self.assertRaises(UpstreamError) as caught:
                    provider.request('GET','/enterprises/org/members/Developer')
                self.assertEqual(caught.exception.upstream_status,status)
                self.assertNotIn('redacted',caught.exception.message)
