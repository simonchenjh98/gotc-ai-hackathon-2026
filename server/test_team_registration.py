"""HTTP tests use synthetic people only; production rosters never enter Git."""
import json
import secrets
import unittest
from concurrent.futures import ThreadPoolExecutor
import test_api as helpers
from test_api import Client


class RegistrationTests(unittest.TestCase):
    setUp = helpers.APITests.setUp
    tearDown = helpers.APITests.tearDown

    def payload(self, **changes):
        return {'name':'Synthetic team','captain':'Captain','school':'Fixture School','phone':'13900000001',
                'targetSize':3,'members':['Student A','Student B'],'project':'','summary':'',**changes}

    def create(self, **changes):
        code=secrets.token_urlsafe(32)
        status,result,_=self.public.post('/api/registration/create',{'code':code,'team':self.payload(**changes)})
        self.assertEqual(status,200,result)
        return code,result['team']

    def admin_call(self,suffix='',body=None,marker=None):
        return self.admin.request('GET' if body is None else 'POST','/api/admin/registration'+suffix,body,
            headers={'X-GOTC-Login':marker if marker is not None else self.service.login_id(self.admin.cookie)})

    def test_public_projection_and_private_auth(self):
        code,team=self.create()
        status,data,_=self.public.get('/api/registration')
        self.assertEqual(status,200)
        for private in ['phone','secret','sourceKey','raw_json',code,'13900000001']:
            self.assertNotIn(private,json.dumps(data))
        self.assertEqual(data['teams'][0]['members'],['Captain','Student A','Student B'])
        self.assertEqual(self.public.get('/api/admin/registration')[0],401)
        self.assertEqual(self.admin_call(marker='')[0],428)
        self.assertEqual(self.admin_call(marker='old-login')[0],401)
        self.assertEqual(self.admin_call()[1]['teams'][0]['phone'],'13900000001')
        self.assertEqual(self.public.post('/api/login',{'role':'team','code':code})[0],401)
        with self.service.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM team_accounts').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM claim_people WHERE needs_review=1').fetchone()[0],3)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM claim_memberships').fetchone()[0],0)
            self.assertNotIn(code,' '.join(r[0] for r in db.execute('SELECT metadata FROM audit')))

    def test_cors_is_exact_and_does_not_apply_to_admin(self):
        origin='https://etownyouthai.com'
        h={'Origin':origin,'Access-Control-Request-Method':'POST','Access-Control-Request-Headers':'content-type'}
        status,_,headers=self.public.request('OPTIONS','/api/registration/create',headers=h)
        self.assertEqual(status,204)
        self.assertEqual(headers['Access-Control-Allow-Origin'],origin)
        self.assertNotIn('Access-Control-Allow-Credentials',headers)
        self.assertEqual(self.public.request('OPTIONS','/api/admin/registration',headers=h)[0],403)
        self.assertEqual(self.public.request('OPTIONS','/api/registration/create',headers={**h,'Origin':'https://evil.invalid'})[0],403)
        payload={'code':secrets.token_urlsafe(32),'team':self.payload()}
        self.assertEqual(self.public.request('POST','/api/registration/create',payload,origin='https://evil.invalid')[0],403)
        self.assertEqual(self.public.request('POST','/api/registration/create',payload,origin=False)[0],403)
        response=self.public.request('POST','/api/registration/create',payload,origin=origin,headers={'Sec-Fetch-Site':'cross-site'})
        self.assertEqual(response[0],200,response[1])
        self.assertEqual(response[2]['Access-Control-Allow-Origin'],origin)
        self.assertEqual(self.admin.request('POST','/api/admin/registration/rotate',{},origin=origin)[0],403)

    def test_concurrent_retry_and_duplicate_registration(self):
        body={'code':secrets.token_urlsafe(32),'team':self.payload()}
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:Client(self.server).post('/api/registration/create',body),range(2)))
        self.assertEqual([r[0] for r in results],[200,200])
        self.assertEqual(results[0][1]['team']['id'],results[1][1]['team']['id'])
        self.assertEqual(self.public.post('/api/registration/create',{**body,'code':secrets.token_urlsafe(32)})[0],409)
        self.assertEqual(len(self.public.get('/api/registration')[1]['teams']),1)

    def test_validation_and_duplicate_person(self):
        code,_=self.create()
        for changes in [{'name':'待定'},{'captain':'无'},{'targetSize':True},{'targetSize':2},{'phone':'123456789012'},
                        {'members':['One','Two','Three','Four','Five']},{'members':'One'},{'name':'x'*81}]:
            r=self.public.post('/api/registration/create',{'code':secrets.token_urlsafe(32),'team':self.payload(**changes)})
            self.assertEqual(r[0],400,r[1])
        self.assertEqual(self.public.post('/api/registration/create',{'code':secrets.token_urlsafe(32),'team':self.payload(name='Another team')})[0],409)
        self.assertEqual(self.public.post('/api/registration/open',{'code':'x'*43})[0],403)
        self.assertEqual(self.public.post('/api/registration/open',{'code':code,'teamId':'other'})[0],404)

    def test_edit_conflict_rotation_and_withdrawal(self):
        code,team=self.create(members=[])
        body={'team':self.payload(name='Updated team'),'code':code,'expectedRevision':team['revision']}
        first=self.public.post('/api/registration/save',body)
        self.assertEqual(first[0],200,first[1])
        self.assertEqual(self.public.post('/api/registration/save',body)[0],409)
        rotated=self.admin_call('/rotate',{'teamId':team['id'],'expectedRevision':first[1]['team']['revision']})
        self.assertEqual(rotated[0],200,rotated[1])
        self.assertEqual(self.public.post('/api/registration/open',{'code':code})[0],403)
        code=rotated[1]['code']
        self.assertEqual(self.public.post('/api/registration/open',{'code':code})[0],200)
        with self.service.db() as db:db.execute("UPDATE team_details SET status='withdrawn' WHERE team_id=?",(team['id'],))
        self.assertEqual(self.public.get('/api/registration')[1]['teams'],[])
        self.assertEqual(self.public.post('/api/registration/open',{'code':code})[0],409)

    def test_import_keeps_raw_deduplicates_names_and_preserves_student_edits(self):
        source={'sourceKey':'synthetic-1','team':self.payload(name='',members=['Captain','欢迎加入','Real member'],targetSize=None,phone='invalid-original'),'raw':{'original':'source preserved'}}
        imported=self.admin_call('/import',{'rows':[source]})
        self.assertEqual(imported[0],200,imported[1])
        access=imported[1]['access'][0]
        team=self.public.get('/api/registration')[1]['teams'][0]
        self.assertEqual(team['members'],['Captain','Real member'])
        self.assertIn('队名待补充',team['flags'])
        self.assertEqual(self.admin_call()[1]['teams'][0]['phone'],'invalid-original')
        with self.service.db() as db:self.assertEqual(json.loads(db.execute('SELECT raw_json FROM team_registrations').fetchone()[0]),source['raw'])
        self.assertEqual(self.public.post('/api/registration/save',{'code':access['code'],'expectedRevision':1,'team':self.payload(name='Fixed name')})[0],200)
        self.assertEqual(self.admin_call('/import',{'rows':[source]})[1]['skipped'],1)
        self.assertEqual(self.public.get('/api/registration')[1]['teams'][0]['name'],'Fixed name')

    def test_import_batch_rolls_back_and_preserves_existing_event_team(self):
        existing=helpers.APITests.team(self)
        good={'sourceKey':'good','team':self.payload(),'raw':{}}
        bad={'sourceKey':'bad','team':self.payload(targetSize=9),'raw':{}}
        self.assertEqual(self.admin_call('/import',{'rows':[good,bad]})[0],400)
        self.assertEqual(self.public.get('/api/registration')[1]['teams'],[])
        with self.service.db() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM teams').fetchone()[0],1)
        self.assertEqual(self.admin_call('/import',{'rows':[good]})[0],200)
        self.assertNotEqual(self.public.get('/api/registration')[1]['teams'][0]['id'],existing)

    def test_body_and_rate_limits(self):
        self.assertEqual(self.public.post('/api/registration/create',{'code':'a'*20000})[0],413)
        for _ in range(20):self.public.post('/api/registration/open',{'code':'x'*43})
        self.assertEqual(self.public.post('/api/registration/open',{'code':'x'*43})[0],429)

    def test_organizer_edit_invalidates_old_registration_form(self):
        code,team=self.create()
        with self.service.db() as db:
            self.service.admin_data.save_team(db,None,{'id':team['id'],'name':'Organizer corrected title'})
        response=self.public.post('/api/registration/save',{'team':self.payload(),'code':code,'expectedRevision':team['revision']})
        self.assertEqual(response[0],409)
        self.assertEqual(self.public.get('/api/registration')[1]['teams'][0]['name'],'Organizer corrected title')

    def test_existing_person_school_is_never_overwritten_or_duplicated(self):
        with self.service.db() as db:
            db.execute("INSERT INTO claim_people(id,name,school,normalized_name,normalized_school) VALUES ('known','Student A','Actual School','student a','actual school')")
        self.create()
        with self.service.db() as db:
            rows=db.execute("SELECT school FROM claim_people WHERE normalized_name='student a'").fetchall()
            self.assertEqual([r[0] for r in rows],['Actual School'])
            self.assertEqual(db.execute("SELECT school FROM claim_people WHERE normalized_name='student b'").fetchone()[0],'待现场核实')

    def test_verified_membership_is_not_changed_by_registration_code(self):
        code,team=self.create()
        with self.service.db() as db:
            request=self.service.claims.request(db,{'name':'Captain','school':'Fixture School'})
            person=db.execute("SELECT id FROM claim_people WHERE normalized_name='captain'").fetchone()[0]
            db.execute('INSERT INTO claim_memberships VALUES (?,?,?)',(person,team['id'],request['requestId']))
        body={'code':code,'expectedRevision':team['revision'],'team':self.payload(members=['Changed Member'])}
        self.assertEqual(self.public.post('/api/registration/save',body)[0],409)
        self.assertEqual(self.public.get('/api/registration')[1]['teams'][0]['members'],team['members'])

    def test_submitted_project_name_cannot_be_overwritten_from_registration(self):
        code,team=self.create()
        with self.service.db() as db:
            db.execute("INSERT INTO submissions(team_id,version,draft_json,submitted_json,updated_at) VALUES (?,1,'{}','{}',1)",(team['id'],))
        body={'code':code,'expectedRevision':team['revision'],'team':self.payload(project='Replacement title')}
        self.assertEqual(self.public.post('/api/registration/save',body)[0],409)


if __name__=='__main__':unittest.main()
