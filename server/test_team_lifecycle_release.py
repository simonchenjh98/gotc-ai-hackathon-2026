"""Release gates on synthetic stores/HTTP only; no provider or production access."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import app
import keys
import team_lifecycle
import test_keys as gateway_helpers
import test_team_lifecycle as lifecycle_helpers
import test_revoked_claims as recovery_helpers
from test_submission_records_release import snapshot


TABLES={'team_recycle_events','team_recycle_memberships'}


class LifecycleMigrationReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='gotc-lifecycle-release-')
        self.root=Path(self.tmp.name)

    def tearDown(self):self.tmp.cleanup()

    def service(self):return app.EventService(data_dir=self.root/'private',origin='http://127.0.0.1')

    def legacy(self):
        # Suppress only the additive feature initializer to reproduce its predecessor layout.
        with patch.object(team_lifecycle.TeamLifecycle,'initialize',return_value=None):service=self.service()
        with service.db() as db:
            db.execute("INSERT INTO teams VALUES ('Tlegacy',71,'Synthetic legacy','Synthetic project','AI for Science',100)")
            db.execute("INSERT INTO team_details(team_id) VALUES ('Tlegacy')")
            db.execute("INSERT INTO claim_people(id,name,school,normalized_name,normalized_school) VALUES ('Plegacy','Synthetic captain','Synthetic school','synthetic captain','synthetic school')")
            db.execute("INSERT INTO claim_requests(id,secret_hash,pin_hash,name,school,normalized_name,normalized_school,created_at,expires_at,status,team_id,captain_id) VALUES ('Rlegacy','synthetic-secret-hash','synthetic-pin-hash','Synthetic captain','Synthetic school','synthetic captain','synthetic school',1,9999999999999,'approved','Tlegacy','Plegacy')")
            db.execute("INSERT INTO claim_memberships VALUES ('Plegacy','Tlegacy','Rlegacy')")
            db.execute("CREATE TABLE synthetic_unknown_extension (id TEXT PRIMARY KEY,payload BLOB)")
            db.execute("INSERT INTO synthetic_unknown_extension VALUES ('opaque',x'000102ff')")
        return service,snapshot(service.db_path)

    def integrity(self,service):
        with service.db() as db:
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(),[])

    def mutate(self,service,operation):
        with service.db() as db:
            db.execute('BEGIN IMMEDIATE')
            actor=dict(db.execute("SELECT * FROM accounts WHERE role='admin'").fetchone())
            detail=service.team_lifecycle.detail(db,'Tlegacy')
            body={'teamId':'Tlegacy','expectedRevision':detail['team']['revision'],'reason':'Synthetic release rehearsal'}
            if operation=='recycle':body['recycleContext']=detail['recycleContext']
            else:body['name']='Synthetic restored'
            return getattr(service.team_lifecycle,operation)(db,actor,body)

    def test_existing_layout_upgrade_is_only_additive_and_repeat_initialization_is_identical(self):
        old,before=self.legacy()
        self.assertTrue(TABLES.isdisjoint(before['tables']))
        upgraded=self.service();after=snapshot(upgraded.db_path)
        self.assertEqual(set(after['tables'])-set(before['tables']),TABLES)
        for name,rows in before['tables'].items():self.assertEqual(after['tables'][name],rows,name)
        self.assertEqual([row for row in after['schema'] if row[2] in before['tables']],before['schema'])
        for _ in range(2):self.assertEqual(snapshot(self.service().db_path),after)
        self.integrity(upgraded)

    def test_recycle_restore_history_and_access_survive_independent_restarts(self):
        self.legacy();service=self.service()
        with service.db() as db:
            original={table:[tuple(row) for row in db.execute('SELECT * FROM '+table)] for table in
                      ['claim_requests','claim_people','synthetic_unknown_extension']}
        for cycle in range(2):
            self.mutate(service,'recycle');service=self.service()
            with service.db() as db:
                self.assertTrue(service.team_lifecycle.is_recycled(db,'Tlegacy'))
                self.assertEqual(db.execute('SELECT COUNT(*) FROM claim_memberships').fetchone()[0],0)
                self.assertEqual(len(service.team_lifecycle.detail(db,'Tlegacy')['roster']['members']),1)
            self.mutate(service,'restore');service=self.service()
            with service.db() as db:
                self.assertFalse(service.team_lifecycle.is_recycled(db,'Tlegacy'))
                self.assertEqual(tuple(db.execute('SELECT * FROM claim_memberships').fetchone()),('Plegacy','Tlegacy','Rlegacy'))
                self.assertEqual(db.execute("SELECT number FROM teams WHERE id='Tlegacy'").fetchone()[0],71)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM team_recycle_events WHERE restored_at IS NOT NULL').fetchone()[0],cycle+1)
                for table,rows in original.items():self.assertEqual([tuple(r) for r in db.execute('SELECT * FROM '+table)],rows,table)
            self.integrity(service)

    def test_concurrent_upgrade_creates_one_schema_without_changing_old_data(self):
        _,before=self.legacy();gate=threading.Barrier(2)
        def start(_):gate.wait();return self.service()
        with ThreadPoolExecutor(max_workers=2) as pool:services=list(pool.map(start,range(2)))
        after=snapshot(services[0].db_path)
        for table,rows in before['tables'].items():self.assertEqual(after['tables'][table],rows,table)
        self.assertEqual(after['tables']['team_recycle_events'],[])
        self.integrity(services[0])


class LifecycleRaceReleaseTests(unittest.TestCase):
    setUp=lifecycle_helpers.TeamLifecycleTests.setUp
    tearDown=lifecycle_helpers.TeamLifecycleTests.tearDown
    team=lifecycle_helpers.TeamLifecycleTests.team
    application=lifecycle_helpers.TeamLifecycleTests.application
    approval=lifecycle_helpers.TeamLifecycleTests.approval
    approved=lifecycle_helpers.TeamLifecycleTests.approved
    detail=lifecycle_helpers.TeamLifecycleTests.detail
    admin_request=lifecycle_helpers.TeamLifecycleTests.admin_request
    admin_call=lifecycle_helpers.TeamLifecycleTests.admin_call
    recycle=lifecycle_helpers.TeamLifecycleTests.recycle
    restore=lifecycle_helpers.TeamLifecycleTests.restore

    def test_restore_competes_with_same_name_and_people_approval_without_overwrite(self):
        old,_=self.approved('Synthetic competing');self.assertEqual(self.recycle(old)[0],200)
        claim=self.application('Synthetic competing');approve=self.approval(claim)
        detail=self.detail(old);restore={'teamId':old,'expectedRevision':detail['team']['revision'],
            'name':detail['team']['name'],'reason':'Synthetic competing restore'}
        gate=threading.Barrier(2)
        def invoke(path,body):gate.wait();return self.admin_request('POST',path,body)
        with ThreadPoolExecutor(max_workers=2) as pool:
            calls=[pool.submit(invoke,'/api/admin/data/team/restore',restore),pool.submit(invoke,'/api/admin/claims/approve',approve)]
            results=[call.result() for call in calls]
        self.assertEqual(sorted(r[0] for r in results),[200,409],results)
        with self.service.db() as db:
            owners=db.execute('SELECT DISTINCT team_id FROM claim_memberships').fetchall()
            self.assertEqual(len(owners),1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM claim_memberships').fetchone()[0],3)
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(),[])
            self.assertEqual(bool(owners[0][0]==old),results[0][0]==200)


class LifecycleGatewayReleaseTests(unittest.TestCase):
    setUp=gateway_helpers.GatewayTests.setUp
    tearDown=gateway_helpers.GatewayTests.tearDown
    team=gateway_helpers.GatewayTests.team
    team_client=gateway_helpers.GatewayTests.team_client
    enabled_key=gateway_helpers.GatewayTests.enabled_key
    call=gateway_helpers.GatewayTests.call
    detail=lifecycle_helpers.TeamLifecycleTests.detail
    admin_request=lifecycle_helpers.TeamLifecycleTests.admin_request
    recycle=lifecycle_helpers.TeamLifecycleTests.recycle

    def test_actual_queued_http_call_fails_after_recycle_before_fake_provider(self):
        team,_,key=self.enabled_key();service=self.service.keys
        queued=threading.Event();original_wait=service.slot_free.wait
        def observed_wait(timeout=None):queued.set();return original_wait(timeout)
        with service.lock:service.inflight[team]=keys.TEAM_CONCURRENCY
        with patch.object(service.slot_free,'wait',observed_wait),ThreadPoolExecutor(max_workers=1) as pool:
            call=pool.submit(self.call,'/v1/chat/completions',{'model':'cheap'},key)
            self.assertTrue(queued.wait(5),'HTTP request never queued')
            try:self.assertEqual(self.recycle(team)[0],200)
            finally:service.release(team)
            self.assertEqual(call.result(timeout=10)[0],401)
        self.assertEqual(gateway_helpers.FakeUpstream.calls,[])
        with service.lock:self.assertEqual(service.inflight[team],keys.TEAM_CONCURRENCY-1)

    def test_already_admitted_call_may_finish_and_remains_auditable_after_recycle(self):
        team,_,key=self.enabled_key();service=self.service.keys
        admitted=threading.Event();resume=threading.Event();forward=service.forward
        def paused_forward(*args,**kwargs):
            admitted.set()
            if not resume.wait(10):raise RuntimeError('Synthetic admission barrier timed out')
            return forward(*args,**kwargs)
        with patch.object(service,'forward',paused_forward),ThreadPoolExecutor(max_workers=1) as pool:
            call=pool.submit(self.call,'/v1/chat/completions',{'model':'cheap'},key)
            self.assertTrue(admitted.wait(5),'HTTP request never passed final recheck')
            try:self.assertEqual(self.recycle(team)[0],200)
            finally:resume.set()
            self.assertEqual(call.result(timeout=10)[0],200)
        self.assertEqual(len(gateway_helpers.FakeUpstream.calls),1)
        with self.service.db() as db:
            row=db.execute('SELECT status,cost_micro FROM api_usage WHERE team_id=?',(team,)).fetchone()
            self.assertEqual(row['status'],200)
            self.assertGreater(row['cost_micro'],0)


class LifecycleRecoveryReleaseTests(unittest.TestCase):
    setUp=recovery_helpers.RevokedClaimsTests.setUp
    tearDown=recovery_helpers.RevokedClaimsTests.tearDown
    admin_call=recovery_helpers.RevokedClaimsTests.admin_call
    bearer=recovery_helpers.RevokedClaimsTests.bearer
    application=recovery_helpers.RevokedClaimsTests.application
    registration=recovery_helpers.RevokedClaimsTests.registration
    approval=recovery_helpers.RevokedClaimsTests.approval
    access=recovery_helpers.RevokedClaimsTests.access
    target=recovery_helpers.RevokedClaimsTests.target
    five_people=recovery_helpers.RevokedClaimsTests.five_people
    reissue=recovery_helpers.RevokedClaimsTests.reissue
    detail=lifecycle_helpers.TeamLifecycleTests.detail
    admin_request=lifecycle_helpers.TeamLifecycleTests.admin_request
    recycle=lifecycle_helpers.TeamLifecycleTests.recycle

    def test_real_five_person_reissue_invalidates_old_empty_detail_without_team_revision_change(self):
        _,team,_,_=self.target();before=self.detail(team['id'])
        claim=self.five_people()
        approved=self.admin_call('/approve',self.reissue(claim,team))
        self.assertEqual(approved[0],200,approved[1])
        fresh=self.detail(team['id'])
        self.assertEqual(fresh['team']['revision'],before['team']['revision'])
        self.assertEqual(len(fresh['roster']['members']),5)
        self.assertEqual(sum(m['role']=='captain' for m in fresh['roster']['members']),1)
        with self.service.db() as db:
            saved={table:[tuple(r) for r in db.execute('SELECT * FROM '+table)] for table in
                   ['teams','team_details','claim_memberships','claim_requests','accounts','sessions','team_recycle_events']}
        failed=self.recycle(team['id'],before)
        self.assertEqual((failed[0],failed[1]['code']),(409,'team_context_conflict'))
        with self.service.db() as db:
            for table,rows in saved.items():self.assertEqual([tuple(r) for r in db.execute('SELECT * FROM '+table)],rows,table)

    def test_selected_preregistration_approval_and_recycle_serialize_without_partial_binding(self):
        _,team,_,_=self.target();detail=self.detail(team['id'])
        claim=self.five_people();body=self.reissue(claim,team);gate=threading.Barrier(2)
        def approve():gate.wait();return self.admin_call('/approve',body)
        def recycle():gate.wait();return self.recycle(team['id'],detail)
        with ThreadPoolExecutor(max_workers=2) as pool:
            pending=[pool.submit(approve),pool.submit(recycle)]
            results=[task.result() for task in pending]
        self.assertEqual(sorted(result[0] for result in results),[200,409],results)
        with self.service.db() as db:
            count=db.execute('SELECT COUNT(*) FROM claim_memberships').fetchone()[0]
            self.assertEqual(count,5 if results[0][0]==200 else 0)
            self.assertEqual(self.service.team_lifecycle.is_recycled(db,team['id']),results[1][0]==200)
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(),[])


if __name__=='__main__':unittest.main()
