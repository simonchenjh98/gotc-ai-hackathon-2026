"""Automatic completion for every complete, conflict-free public team application."""
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
import json
import unittest

from errors import APIError
import test_api as api_helpers
from app import EventService
from test_api import Client
import test_open_claims


class UniversalClaimsTests(unittest.TestCase):
    def setUp(self):
        api_helpers.APITests.setUp(self)
        with self.service.db() as db:db.execute("UPDATE settings SET value='true' WHERE key='claimsAutoApproveAll'")
    tearDown=api_helpers.APITests.tearDown
    admin_call=test_open_claims.OpenClaimsTests.admin_call
    bearer=test_open_claims.OpenClaimsTests.bearer
    application=test_open_claims.OpenClaimsTests.application
    counts=test_open_claims.OpenClaimsTests.counts
    registration=test_open_claims.OpenClaimsTests.registration

    def test_fresh_production_service_enables_automatic_completion_by_default(self):
        fresh=EventService(data_dir=self.root/'default-auto-private',origin='http://127.0.0.1',static_root=self.service.root)
        with fresh.db() as db:
            self.assertEqual(db.execute("SELECT value FROM settings WHERE key='claimsAutoApproveAll'").fetchone()[0],'true')

    def test_default_complete_application_is_approved_and_redeems_normally(self):
        claim=self.application()
        self.assertEqual(claim['status'],'approved')
        self.assertIsNone(claim['code'])
        self.assertEqual(self.counts()['claim_memberships'],3)
        first=self.bearer(claim,'redeem')[1]
        self.assertEqual(first,self.bearer(claim,'redeem')[1])
        team=Client(self.server)
        self.assertEqual(team.post('/api/login',{'role':'team','code':first['code']})[0],200)
        with self.service.db() as db:
            audit=json.loads(db.execute("SELECT metadata FROM audit WHERE action='claims.approve-open'").fetchone()[0])
            self.assertEqual(audit['verificationMethod'],'automatic')
            self.assertFalse(audit['applicationReviewed'])
            self.assertIsNotNone(db.execute("SELECT 1 FROM audit WHERE action='claims.approve-auto-open'").fetchone())
            self.assertEqual(db.execute('SELECT COUNT(*) FROM team_api').fetchone()[0],0)

    def test_duplicate_members_or_same_name_other_school_fail_without_queueing(self):
        self.application()
        before=self.counts()
        with self.service.db() as db:requests=db.execute('SELECT COUNT(*) FROM claim_requests').fetchone()[0]
        for body in [
            {'name':'Open Captain','school':'School A','teamName':'Another Team','track':'','members':[{'name':'Another One','school':'School B'},{'name':'Another Two','school':'School C'}]},
            {'name':'Open Captain','school':'Other School','teamName':'Distinct Team','track':'','members':[{'name':'Distinct One','school':'School B'},{'name':'Distinct Two','school':'School C'}]},
        ]:
            status,result,_=self.public.post('/api/claims/request',body)
            self.assertEqual(status,409,result)
            self.assertEqual(result['code'],'claim_conflict')
        self.assertEqual(self.counts(),before)
        with self.service.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM claim_requests').fetchone()[0],requests)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM claim_requests WHERE status='pending'").fetchone()[0],0)

    def test_exact_registered_roster_is_reused_automatically(self):
        _,registered,_=self.registration(members=['Open One','Open Two'])
        claim=self.application()
        self.assertEqual(claim['status'],'approved')
        self.assertEqual(claim['team']['id'],registered['id'])
        self.assertEqual(self.counts()['teams'],1)

    def test_existing_pending_receipt_completes_when_policy_turns_on(self):
        with self.service.db() as db:db.execute("UPDATE settings SET value='false' WHERE key='claimsAutoApproveAll'")
        claim=self.application()
        self.assertEqual(claim['status'],'pending')
        with self.service.db() as db:db.execute("UPDATE settings SET value='true' WHERE key='claimsAutoApproveAll'")
        self.assertEqual(self.bearer(claim)[1]['status'],'approved')

    def test_expiry_during_strict_update_rolls_back_approval_and_edit(self):
        with self.service.db() as db:db.execute("UPDATE settings SET value='false' WHERE key='claimsAutoApproveAll'")
        claim=self.application();before=self.counts()
        with self.service.db() as db:db.execute("UPDATE settings SET value='true' WHERE key='claimsAutoApproveAll'")
        current=[claim['expiresAt']-1];original=self.service.claims.auto.attempt
        def expires_after_approval(db,request_id,strict=False):
            result=original(db,request_id,strict=strict)
            current[0]=claim['expiresAt']
            return result
        with patch.object(self.service.claims,'clock',side_effect=lambda:current[0]), \
             patch.object(self.service.claims.auto,'attempt',side_effect=expires_after_approval):
            status,result,_=self.public.post('/api/claims/update',{'requestId':claim['requestId'],'secret':claim['secret'],
                'expectedRevision':1,'members':claim['declaration']['members'],'teamName':'Open Team','track':''})
        self.assertEqual(status,410,result);self.assertEqual(result['code'],'claim_expired')
        self.assertEqual(self.counts(),before)
        with self.service.db() as db:
            row=db.execute('SELECT status,revision,team_id,account_id FROM claim_requests WHERE id=?',(claim['requestId'],)).fetchone()
            self.assertEqual(tuple(row),('pending',1,None,None))

    def test_business_failure_rolls_back_request_instead_of_leaving_pending(self):
        before=self.counts()
        with self.service.db() as db:requests=db.execute('SELECT COUNT(*) FROM claim_requests').fetchone()[0]
        with patch.object(self.service,'team_access',side_effect=APIError(409,'Synthetic issuance conflict')):
            status,result,_=self.public.post('/api/claims/request',{'name':'Open Captain','school':'School A','teamName':'Open Team','track':'','members':[{'name':'Open One','school':'School B'},{'name':'Open Two','school':'School C'}]})
        self.assertEqual(status,409,result)
        self.assertEqual(self.counts(),before)
        with self.service.db() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM claim_requests').fetchone()[0],requests)

    def test_concurrent_duplicate_submissions_create_only_one_team(self):
        body={'name':'Race Captain','school':'School A','teamName':'Race Team','track':'','members':[{'name':'Race One','school':'School B'},{'name':'Race Two','school':'School C'}]}
        def submit(_):return Client(self.server).post('/api/claims/request',body)
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(submit,range(2)))
        self.assertEqual(sorted(result[0] for result in results),[200,409])
        self.assertEqual(self.counts()['teams'],1)
        self.assertEqual(self.counts()['claim_memberships'],3)
        with self.service.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM claim_requests').fetchone()[0],1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM claim_requests WHERE status='pending'").fetchone()[0],0)


if __name__=='__main__':unittest.main()
