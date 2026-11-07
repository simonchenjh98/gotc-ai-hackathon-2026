"""Public directory, existing registration-code compatibility and no new issuance."""
import json
import secrets
import unittest
import test_api as helpers
from test_registration_fixtures import registration_fixture_create

class RegistrationTests(unittest.TestCase):
    setUp=helpers.APITests.setUp
    tearDown=helpers.APITests.tearDown
    def payload(self,**changes):
        return {'name':'Synthetic team','captain':'Captain','school':'Fixture School','phone':'13900000001',
                'targetSize':3,'members':['Student A','Student B'],'project':'','summary':'',**changes}
    def create(self,**changes):
        code=secrets.token_urlsafe(32)
        status,result,_=registration_fixture_create(self,{'code':code,'team':self.payload(**changes)})
        self.assertEqual(status,200,result)
        return code,result['team']
    def admin_call(self,suffix='',body=None,marker=None):
        return self.admin.request('GET' if body is None else 'POST','/api/admin/registration'+suffix,body,
            headers={'X-GOTC-Login':marker if marker is not None else self.admin.login_id})
    def test_public_projection_and_independent_private_admin_auth(self):
        code,team=self.create()
        status,data,_=self.public.get('/api/registration');self.assertEqual(status,200)
        for private in ['phone','secret','sourceKey','raw_json',code,'13900000001']:self.assertNotIn(private,json.dumps(data))
        self.assertEqual(data['teams'][0]['members'],['Captain','Student A','Student B'])
        self.assertEqual(self.public.get('/api/admin/registration')[0],401)
        self.assertEqual(self.admin_call(marker='')[0],428);self.assertEqual(self.admin_call(marker='old')[0],401)
        self.assertEqual(self.admin_call()[1]['teams'][0]['phone'],'13900000001')
        self.assertEqual(self.public.post('/api/login',{'role':'team','code':code})[0],401)
        with self.service.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM team_accounts').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM claim_memberships').fetchone()[0],0)
        self.assertEqual(self.public.post('/api/registration/open',{'code':code})[0],200)
    def test_cors_is_directory_only_and_never_allows_cross_site_profile_writes(self):
        origin='https://etownyouthai.com'
        headers={'Origin':origin,'Access-Control-Request-Method':'GET','Access-Control-Request-Headers':'content-type'}
        response=self.public.request('OPTIONS','/api/registration',headers=headers)
        self.assertEqual(response[0],204);self.assertEqual(response[2]['Access-Control-Allow-Origin'],origin)
        self.assertNotIn('Access-Control-Allow-Credentials',response[2])
        for path in ['/api/registration/create','/api/team/profile','/api/admin/registration']:
            self.assertEqual(self.public.request('OPTIONS',path,headers=headers)[0],403)
            self.assertEqual(self.public.request('POST',path,{'code':'x'*43},origin=origin,headers={'Sec-Fetch-Site':'cross-site'})[0],403)
        self.assertEqual(self.public.request('POST','/api/team/profile',{},origin=False)[0],403)
        for path in ['/api/registration/open','/api/registration/save']:
            self.assertEqual(self.public.request('OPTIONS',path,headers={**headers,'Access-Control-Request-Method':'POST'})[0],204)
    def test_legacy_fixture_validation_and_retired_credential_paths(self):
        for changes in [{'name':'无'},{'captain':''},{'phone':'wrong'},{'targetSize':2},{'members':['A','B','C','D','E']},{'members':'One'}]:
            result=registration_fixture_create(self,{'code':secrets.token_urlsafe(32),'team':self.payload(**changes)})
            self.assertEqual(result[0],400,result[1])
        for path in ['create']:
            result=self.public.post('/api/registration/'+path,{'code':'x'*43})
            self.assertEqual(result[0],410);self.assertEqual(result[1]['code'],'registration_code_retired')
        self.assertEqual(self.public.post('/api/registration/open',{'code':'x'*43})[0],403)
        self.assertEqual(self.public.post('/api/registration/save',{'code':'x'*43,'expectedRevision':1,'team':self.payload()})[0],403)
        self.assertEqual(self.public.post('/api/registration/create',{'code':'x'*20000})[0],413)
    def test_import_retains_raw_records_without_management_code_exports(self):
        row={'sourceKey':'synthetic-import','team':self.payload(),'raw':{'Original':'Synthetic source'}}
        result=self.admin_call('/import',{'rows':[row]})
        self.assertEqual(result[0],200,result[1]);self.assertEqual(result[1]['added'],1)
        self.assertEqual(set(result[1]['access'][0]),{'id','number','name','captain'})
        self.assertNotIn('code',json.dumps(result[1]));self.assertNotIn('secret',json.dumps(result[1]))
        self.assertEqual(self.admin_call('/import',{'rows':[row]})[1]['skipped'],1)
        with self.service.db() as db:
            saved=db.execute('SELECT raw_json,secret_hash FROM team_registrations').fetchone()
            self.assertEqual(json.loads(saved['raw_json']),row['raw']);self.assertTrue(saved['secret_hash'].startswith('disabled:'))
        self.assertEqual(len(self.public.get('/api/registration')[1]['teams']),1)
        self.assertEqual(self.admin_call('/rotate',{'teamId':result[1]['access'][0]['id'],'expectedRevision':1})[0],410)
    def test_withdrawn_registration_remains_private_and_hidden_from_directory(self):
        _,team=self.create()
        with self.service.db() as db:self.service.admin_data.save_team(db,None,{'id':team['id'],'status':'withdrawn','statusReason':'Synthetic withdrawal'})
        self.assertEqual(self.public.get('/api/registration')[1]['teams'],[])
        self.assertEqual(self.admin_call()[1]['teams'][0]['status'],'withdrawn')

if __name__=='__main__':unittest.main()
