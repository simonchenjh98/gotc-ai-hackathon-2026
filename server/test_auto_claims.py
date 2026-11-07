"""Synthetic trusted snapshot regression tests; no participant records."""
from test_registration_fixtures import registration_fixture_open
import json
import unittest
from unittest.mock import patch
from errors import APIError
from test_api import Client
import test_open_claims

class AutoClaimsTests(unittest.TestCase):
    setUp=test_open_claims.OpenClaimsTests.setUp
    tearDown=test_open_claims.OpenClaimsTests.tearDown
    admin_call=test_open_claims.OpenClaimsTests.admin_call
    bearer=test_open_claims.OpenClaimsTests.bearer
    application=test_open_claims.OpenClaimsTests.application
    counts=test_open_claims.OpenClaimsTests.counts
    registration=test_open_claims.OpenClaimsTests.registration

    def source(self,entries=None,enable=True,revision=None):
        if entries is None:entries=[{'id':str(i),'name':name,'school':school,'eligible':True,'notes':''} for i,(name,school) in enumerate([('Open Captain','School A'),('Open One','School B'),('Open Two','School C')])]
        if revision is None:revision=self.admin_call('')[1]['autoReview']['revision']
        body={'autoReview':{'expectedRevision':revision,'sourceName':'Synthetic relay','sourceSha256':'a'*64,'entries':entries}}
        result=self.admin_call('/roster',body)
        self.assertEqual(result[0],200,result)
        if enable:self.assertEqual(self.admin_call('/auto-review',{'expectedRevision':result[1]['autoReview']['revision'],'enabled':True})[0],200)
        return entries,body

    def test_whole_team_automatic_private_redeem_and_no_api(self):
        self.source();claim=self.application();self.assertEqual(claim['status'],'approved');self.assertIsNone(claim['code'])
        first=self.bearer(claim,'redeem')[1];self.assertEqual(first,self.bearer(claim,'redeem')[1])
        client=Client(self.server);self.assertEqual(client.post('/api/login',{'role':'team','code':first['code']})[0],200)
        with self.service.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM team_api').fetchone()[0],0)
            audit=json.loads(db.execute("SELECT metadata FROM audit WHERE action='claims.approve-auto'").fetchone()[0])
            self.assertEqual(audit['matchedSourceIds'],['0','1','2']);self.assertEqual(audit['sourceSha256'],'a'*64)
        serialized=json.dumps(self.bearer(claim)[1]);self.assertNotIn('matchedSourceIds',serialized);self.assertNotIn('autoReview',serialized)

    def test_disabled_pending_then_status_and_five_people(self):
        names=['Open One','Open Two','Open Three','Open Four']
        entries=[{'id':str(i),'name':n,'school':'School A','eligible':True,'notes':''} for i,n in enumerate(['Open Captain',*names])]
        self.source(entries,False)
        claim=self.application(members=[{'name':n,'school':'School A'} for n in names]);self.assertEqual(claim['status'],'pending')
        revision=self.admin_call('')[1]['autoReview']['revision'];self.admin_call('/auto-review',{'expectedRevision':revision,'enabled':True})
        self.assertEqual(self.bearer(claim)[1]['status'],'approved');self.assertEqual(self.counts()['claim_memberships'],5)

    def test_ambiguity_ineligible_unknown_and_school_difference_stay_manual(self):
        entries,_=self.source(enable=False)
        for changed in [[*entries,{**entries[0],'id':'duplicate'}],[{**p,'eligible':False} if p['id']=='1' else p for p in entries],entries[:2],[{**p,'school':'Different'} if p['id']=='1' else p for p in entries]]:
            self.source(changed);claim=self.application();self.assertEqual(claim['status'],'pending');self.assertEqual(self.counts()['teams'],0)

    def test_strict_admin_revision_and_atomic_replacement(self):
        entries,body=self.source();self.assertEqual(self.admin_call('/roster',body)[0],409)
        toggle={'expectedRevision':2,'enabled':False}
        self.assertEqual(self.public.post('/api/admin/claims/auto-review',toggle)[0],401)
        self.assertEqual(self.admin.post('/api/admin/claims/auto-review',toggle)[0],428)
        self.assertEqual(self.admin_call('/auto-review',toggle,login_id='stale')[0],401)
        self.assertEqual(self.admin.request('POST','/api/admin/claims/auto-review',toggle,origin=False)[0],403)
        invalid={**body,'autoReview':{**body['autoReview'],'expectedRevision':2,'entries':[entries[0],entries[0]]}}
        self.assertEqual(self.admin_call('/roster',invalid)[0],400);self.assertTrue(self.admin_call('')[1]['autoReview']['enabled'])
        self.source(entries,False);self.assertFalse(self.admin_call('')[1]['autoReview']['enabled'])

    def test_business_failure_rolls_back_partial_creation_then_retries(self):
        self.source();before=self.counts()
        with patch.object(self.service,'team_access',side_effect=APIError(409,'Synthetic issuance conflict')):
            claim=self.application();self.assertEqual(claim['status'],'pending')
        after=self.counts()
        for table in ['teams','claim_people','claim_memberships','team_accounts','accounts']:self.assertEqual(after[table],before[table])
        self.assertEqual(self.bearer(claim)[1]['status'],'approved')

    def test_overlapping_claim_and_public_registration_do_not_grant_trust(self):
        claim=self.application();self.assertEqual(claim['status'],'pending')
        self.source();self.assertEqual(self.bearer(claim)[1]['status'],'approved')
        self.assertEqual(self.application()['status'],'pending');self.assertEqual(self.counts()['teams'],1)

    def test_exact_registered_roster_reuses_team_and_mismatch_stays_manual(self):
        code,registered,_=self.registration(members=['Open One','Open Two'])
        self.source()
        claim=self.application()
        self.assertEqual(claim['status'],'approved');self.assertEqual(claim['team']['id'],registered['id']);self.assertEqual(self.counts()['teams'],1)
        self.assertEqual(registration_fixture_open(self,{'code':code})[0],200)

    def test_update_pending_claim_and_generic_reference_separation(self):
        entries,_=self.source(enable=False)
        self.assertEqual(self.admin_call('/roster',{'people':[{'id':'generic','name':'Open Captain','school':'School A','notes':'','needsReview':False}]})[0],200)
        claim=self.application();self.assertEqual(claim['status'],'pending')
        revision=self.admin_call('')[1]['autoReview']['revision']
        self.admin_call('/auto-review',{'expectedRevision':revision,'enabled':True})
        status,result,_=self.public.post('/api/claims/update',{'requestId':claim['requestId'],'secret':claim['secret'],'expectedRevision':1,'members':claim['declaration']['members']})
        self.assertEqual(status,200);self.assertEqual(result['status'],'approved')

    def test_expired_rejected_invalid_receipts_do_not_approve(self):
        rejected=self.application();expired=self.application('Expired')
        self.assertEqual(self.admin_call('/review',{'requestId':rejected['requestId'],'expectedRevision':1,'action':'reject','note':''})[0],200)
        with self.service.db() as db:db.execute('UPDATE claim_requests SET expires_at=0 WHERE id=?',(expired['requestId'],))
        self.source()
        self.assertEqual(self.bearer(rejected)[1]['status'],'rejected');self.assertEqual(self.bearer(expired)[1]['status'],'invalid')
        status,result,_=self.public.post('/api/claims/status',{'requestId':rejected['requestId'],'secret':'z'*43})
        self.assertEqual(result['status'],'invalid');self.assertEqual(self.counts()['teams'],0)

    def test_mismatched_registered_roster_and_capacity_remain_pending(self):
        self.registration(members=['Different One','Different Two'])
        self.source();claim=self.application();self.assertEqual(claim['status'],'pending');self.assertEqual(self.counts()['team_accounts'],0)
        with self.service.db() as db:
            db.executemany('INSERT INTO claim_people(id,name,school,normalized_name,normalized_school) VALUES (?,?,?,?,?)',
                [(f'capacity-{i}',f'Capacity {i}','School',f'capacity {i}','school') for i in range(500-db.execute('SELECT COUNT(*) FROM claim_people').fetchone()[0])])
        claim=self.application(teamName='Fresh team');self.assertEqual(claim['status'],'pending');self.assertEqual(self.counts()['team_accounts'],0)

    def test_racing_overlap_yields_one_complete_team(self):
        from concurrent.futures import ThreadPoolExecutor
        first=self.application();second=self.application();self.source()
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda claim:self.bearer(claim)[1]['status'],[first,second]))
        self.assertEqual(sorted(results),['approved','pending']);self.assertEqual(self.counts()['claim_memberships'],3)

    def test_empty_incomplete_and_import_bounds(self):
        self.source([],False)
        revision=self.admin_call('')[1]['autoReview']['revision']
        self.assertEqual(self.admin_call('/auto-review',{'expectedRevision':revision,'enabled':True})[0],409)
        entries=[{'id':'incomplete','name':'','school':'School A','eligible':True,'notes':''}]
        self.source(entries,False)
        revision=self.admin_call('')[1]['autoReview']['revision']
        self.assertEqual(self.admin_call('/auto-review',{'expectedRevision':revision,'enabled':True})[0],409)
        body={'autoReview':{'expectedRevision':revision,'sourceName':'Synthetic','sourceSha256':'a'*64,'entries':entries*501}}
        self.assertEqual(self.admin_call('/roster',body)[0],400)
        body['autoReview']['entries']=[];body['autoReview']['unexpected']=True
        self.assertEqual(self.admin_call('/roster',body)[0],400)

    def test_unexpected_failure_rolls_back_entire_request(self):
        self.source()
        with self.service.db() as db:before=db.execute('SELECT COUNT(*) FROM claim_requests').fetchone()[0]
        with patch.object(self.service,'team_access',side_effect=RuntimeError('Synthetic unexpected error')):
            status,_,_=self.public.post('/api/claims/request',{'name':'Open Captain','school':'School A','teamName':'Open Team','track':'','members':[{'name':'Open One','school':'School B'},{'name':'Open Two','school':'School C'}]})
        self.assertEqual(status,500)
        with self.service.db() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM claim_requests').fetchone()[0],before)
        self.assertEqual(self.counts()['teams'],0)

    def test_expiry_during_automatic_approval_rolls_back_team_and_credentials(self):
        self.source(enable=False)
        claim=self.application()
        revision=self.admin_call('')[1]['autoReview']['revision']
        self.assertEqual(self.admin_call('/auto-review',{'expectedRevision':revision,'enabled':True})[0],200)
        before=self.counts()
        with self.service.db() as db:
            audit_count=db.execute('SELECT COUNT(*) FROM audit').fetchone()[0]
        current=[claim['expiresAt']-1]
        original=self.service.claims.auto.attempt
        def expires_after_real_approval(db,request_id):
            original(db,request_id)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM team_accounts').fetchone()[0],1)
            current[0]=claim['expiresAt']
        with patch.object(self.service.claims,'clock',side_effect=lambda:current[0]), \
             patch.object(self.service.claims.auto,'attempt',side_effect=expires_after_real_approval):
            status,result,_=self.bearer(claim)
        self.assertEqual(status,410,result)
        self.assertEqual(result['code'],'claim_expired')
        self.assertEqual(self.counts(),before)
        with self.service.db() as db:
            row=db.execute('SELECT status,revision,account_id,team_id FROM claim_requests WHERE id=?',(claim['requestId'],)).fetchone()
            self.assertEqual(tuple(row),('pending',1,None,None))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM audit').fetchone()[0],audit_count)
        with patch.object(self.service.claims,'clock',return_value=claim['expiresAt']):
            self.assertEqual(self.bearer(claim)[1]['status'],'invalid')

if __name__=='__main__':unittest.main()
