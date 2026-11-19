"""Read-only admin sync markers against isolated real HTTP services."""
import json
import unittest
from unittest.mock import patch

from backend.gotc import service
import test_api as fixtures
import test_roadshow as stage_fixtures


class AdminSyncTests(unittest.TestCase):
    setUp = fixtures.APITests.setUp
    tearDown = fixtures.APITests.tearDown
    team = fixtures.APITests.team
    invitation = fixtures.APITests.invitation
    stage = stage_fixtures.RoadshowIsolationTests.stage
    roster = stage_fixtures.RoadshowIsolationTests.roster
    window = stage_fixtures.RoadshowIsolationTests.window
    review = stage_fixtures.RoadshowIsolationTests.review

    def marker(self):
        status, result, _ = self.admin.get('/api/admin/data/sync')
        self.assertEqual(status, 200, result)
        return result

    def audit_count(self):
        with self.service.db() as db:
            return db.execute('SELECT COUNT(*) FROM audit').fetchone()[0]

    def test_admin_only_marker_is_small_read_only_and_rejects_query(self):
        judge, _ = self.invitation(name='PRIVATE_SYNC_JUDGE')
        self.assertEqual(self.public.get('/api/admin/data/sync')[0], 401)
        self.assertEqual(judge.get('/api/admin/data/sync')[0], 403)
        before_audit = self.audit_count()
        marker = self.marker()
        self.assertEqual(set(marker), {'reviewRevision', 'stages'})
        self.assertEqual(marker['reviewRevision'], 0)
        self.assertEqual(set(marker['stages']), {'exhibition', 'roadshow'})
        for stage, metadata in marker['stages'].items():
            expected = self.stage(stage)['stage']
            self.assertEqual(metadata, {'runId': expected['runId'], 'revision': expected['revision'], 'canEdit': False})
        for _ in range(3):
            self.assertEqual(self.marker(), marker)
        self.assertEqual(self.admin.get('/api/admin/data/sync?stage=exhibition')[0], 400)
        self.assertEqual(self.admin.post('/api/admin/data/sync', {})[0], 404)
        self.assertEqual(self.audit_count(), before_audit)
        serialized = json.dumps(marker, ensure_ascii=False)
        self.assertLess(len(serialized.encode()), 512)
        self.assertNotIn('PRIVATE_SYNC_JUDGE', serialized)
        self.assertNotIn('feedback', serialized)
        self.assertNotIn('code', serialized)

    def test_review_markers_advance_with_same_millisecond_changes_but_not_logins(self):
        team_id = self.team()
        judge, invitation = self.invitation(name='PRIVATE_SYNC_JUDGE')
        for stage in ('exhibition', 'roadshow'):
            self.roster(stage, [team_id], [invitation['id']])
            self.window(stage)
        before = self.marker()
        instant = service.now_ms()
        markers = []
        with patch.object(service, 'now_ms', return_value=instant):
            for version, status, feedback in [
                    (0, 'draft', 'PRIVATE_SYNC_DRAFT'),
                    (1, 'submitted', 'PRIVATE_SYNC_FORMAL'),
                    (2, 'recused', '')]:
                self.assertEqual(self.review(judge, 'exhibition', team_id, version, status, feedback)[0], 200)
                markers.append(self.marker())
            for version, invalidated in [(3, True), (4, False)]:
                status, result, _ = self.admin.post('/api/admin/data/review-validity', {
                    'runId': before['stages']['exhibition']['runId'], 'teamId': team_id,
                    'judgeId': invitation['id'], 'expectedVersion': version,
                    'invalidated': invalidated, 'reason': 'PRIVATE_SYNC_REASON'})
                self.assertEqual(status, 200, result)
                markers.append(self.marker())
            self.assertEqual(self.review(judge, 'roadshow', team_id, feedback='PRIVATE_ROADSHOW')[0], 200)
            markers.append(self.marker())
        previous = before['reviewRevision']
        for marker in markers:
            self.assertGreater(marker['reviewRevision'], previous)
            self.assertEqual(marker['stages'], before['stages'])
            self.assertNotIn('PRIVATE_', json.dumps(marker))
            previous = marker['reviewRevision']
        with self.service.db() as db:
            self.assertEqual({row[0] for row in db.execute('SELECT at FROM judging_review_history')}, {instant})
        fresh = fixtures.Client(self.server)
        self.assertEqual(fresh.post('/api/login', {'role': 'judge', 'code': invitation['code']})[0], 200)
        self.assertEqual(self.marker(), markers[-1])

    def test_run_and_configuration_changes_remain_separate_for_each_stage(self):
        team_id = self.team()
        _, invitation = self.invitation()
        before = self.marker()
        self.roster('exhibition', [team_id], [invitation['id']])
        roster_changed = self.marker()
        self.assertGreater(roster_changed['stages']['exhibition']['revision'],
                           before['stages']['exhibition']['revision'])
        self.window('exhibition')
        opened = self.marker()
        self.assertTrue(opened['stages']['exhibition']['canEdit'])
        self.window('exhibition', False)
        closed = self.marker()
        status, result, _ = self.admin.post('/api/admin/judging/exhibition/reset', {
            'expectedRevision': closed['stages']['exhibition']['revision'], 'reason': 'Synthetic next run'})
        self.assertEqual(status, 200, result)
        reset = self.marker()
        self.assertNotEqual(reset['stages']['exhibition']['runId'], before['stages']['exhibition']['runId'])
        for marker in (roster_changed, opened, closed, reset):
            self.assertEqual(marker['reviewRevision'], before['reviewRevision'])
            self.assertEqual(marker['stages']['roadshow'], before['stages']['roadshow'])

    def test_exact_deadline_changes_effective_window_without_database_writes(self):
        team_id = self.team()
        _, invitation = self.invitation()
        self.roster('exhibition', [team_id], [invitation['id']])
        deadline = service.now_ms() + 10_000
        self.window('exhibition', True, deadline)
        audit_before = self.audit_count()
        with patch.object(service, 'now_ms', return_value=deadline - 1):
            before = self.marker()
        with patch.object(service, 'now_ms', return_value=deadline):
            at_deadline = self.marker()
        self.assertTrue(before['stages']['exhibition']['canEdit'])
        self.assertFalse(at_deadline['stages']['exhibition']['canEdit'])
        self.assertEqual(before['reviewRevision'], at_deadline['reviewRevision'])
        for field in ('runId', 'revision'):
            self.assertEqual(before['stages']['exhibition'][field], at_deadline['stages']['exhibition'][field])
        self.assertEqual(before['stages']['roadshow'], at_deadline['stages']['roadshow'])
        self.assertEqual(self.audit_count(), audit_before)


if __name__ == '__main__':
    unittest.main(verbosity=2)
