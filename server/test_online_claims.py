"""Online organizer review uses explicit intent, never an exchanged device PIN."""
import json
import unittest
import test_claims
import test_open_claims


class OnlineClaimsTests(unittest.TestCase):
    setUp=test_open_claims.OpenClaimsTests.setUp
    tearDown=test_open_claims.OpenClaimsTests.tearDown
    admin_call=test_open_claims.OpenClaimsTests.admin_call
    bearer=test_open_claims.OpenClaimsTests.bearer
    roster=test_open_claims.OpenClaimsTests.roster
    team=test_open_claims.OpenClaimsTests.team
    application=test_open_claims.OpenClaimsTests.application
    registration=test_open_claims.OpenClaimsTests.registration
    selected_approval=test_open_claims.OpenClaimsTests.selected_approval
    counts=test_open_claims.OpenClaimsTests.counts
    invitation=test_claims.ClaimsTests.invitation

    def approval(self,claim,**changes):
        body=test_open_claims.OpenClaimsTests.approval(self,claim)
        del body['pin'];del body['inPersonVerified']
        return {**body,'verificationMethod':'online','applicationReviewed':True,**changes}

    test_empty_install=test_open_claims.OpenClaimsTests.test_empty_install_pending_then_atomic_creation_redemption_and_login
    test_stale_references=test_open_claims.OpenClaimsTests.test_reference_context_rejects_relevant_changes_but_not_unrelated_people
    test_same_name_distinction=test_open_claims.OpenClaimsTests.test_same_name_other_school_requires_distinction_and_note
    test_stale_declaration=test_open_claims.OpenClaimsTests.test_metadata_edits_legacy_preservation_and_stale_review
    test_existing_registration=test_open_claims.OpenClaimsTests.test_existing_registration_approval_binds_same_team_and_locks_reviewed_members
    test_stale_registration=test_open_claims.OpenClaimsTests.test_selected_registration_snapshot_rejects_changed_registration_and_team_fields
    test_selection_rollback=test_open_claims.OpenClaimsTests.test_selected_registration_issuance_failure_rolls_back_team_and_registration_changes
    test_same_target_race=test_open_claims.OpenClaimsTests.test_racing_different_claims_cannot_bind_the_same_existing_team
    test_duplicate_verified_names=test_open_claims.OpenClaimsTests.test_verified_same_name_people_allow_metadata_edits_without_losing_any_identity

    def test_explicit_online_intent_auth_strict_modes_and_audit(self):
        claim=self.application();body=self.approval(claim)
        self.assertNotIn('pin',body);self.assertNotIn('inPersonVerified',body)
        before=self.bearer(claim,'redeem')[1];self.assertEqual(before['status'],'pending');self.assertIsNone(before['code'])
        self.assertTrue(any(r['id']==claim['requestId'] for r in self.admin_call('')[1]['requests']))
        self.assertEqual(self.public.post('/api/admin/claims/approve',body)[0],401)
        self.assertEqual(self.admin.post('/api/admin/claims/approve',body)[0],428)
        self.assertEqual(self.admin_call('/approve',body,login_id='stale')[0],401)
        judge,_=self.invitation('judge','Fixture Judge')
        self.assertEqual(self.admin_call('/approve',body,client=judge)[0],403)
        self.assertEqual(self.admin.request('POST','/api/admin/claims/approve',body,origin=False)[0],403)
        for changed in [{'verificationMethod':'onsite'},{'verificationMethod':None},{'applicationReviewed':False},
                        {'applicationReviewed':1},{'pin':claim['pin']},{'inPersonVerified':True}]:
            self.assertEqual(self.admin_call('/approve',{**body,**changed})[0],400)
        for omitted in ['verificationMethod','applicationReviewed']:
            self.assertEqual(self.admin_call('/approve',{k:v for k,v in body.items() if k!=omitted})[0],400)
        self.assertEqual(self.counts()['teams'],0)
        self.assertEqual(self.admin_call('/approve',body)[0],200)
        with self.service.db() as db:
            audit=json.loads(db.execute("SELECT metadata FROM audit WHERE action='claims.approve-open'").fetchone()[0])
            self.assertEqual(audit['verificationMethod'],'online');self.assertIs(audit['applicationReviewed'],True)
            self.assertNotIn('inPersonVerified',audit);self.assertNotIn('pin',audit)
        self.bearer(claim,'redeem');self.assertEqual(self.admin_call('/approve',body)[0],200)
        legacy=test_open_claims.OpenClaimsTests.approval(self,claim)
        self.assertEqual(self.admin_call('/approve',legacy)[0],409)
