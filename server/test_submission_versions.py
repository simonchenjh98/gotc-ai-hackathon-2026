"""Contract tests for submission versions and team-scoped review snapshots."""
import json
import unittest
from pathlib import Path
from app import normalize_code_version
from errors import APIError
import test_submissions


class VersionSubmissionTests(unittest.TestCase):
    setUp=test_submissions.SubmissionAPITests.setUp
    tearDown=test_submissions.SubmissionAPITests.tearDown
    team=test_submissions.SubmissionAPITests.team
    invitation=test_submissions.SubmissionAPITests.invitation
    access=test_submissions.SubmissionAPITests.access
    open_submissions=test_submissions.SubmissionAPITests.open_submissions
    materials=test_submissions.SubmissionAPITests.materials
    save=test_submissions.SubmissionAPITests.save
    def test_shared_parser_contract(self):
        cases=json.loads((Path(__file__).parents[1]/'tests/fixtures/submission-versions.json').read_text())
        for case in cases:
            with self.subTest(case=case):
                if case['expected'] is None:
                    with self.assertRaises(APIError):
                        normalize_code_version(case['repository'],case['version'])
                else:
                    self.assertEqual(normalize_code_version(case['repository'],case['version']),case['expected'])

    def test_dot_prefixed_github_repository_accepts_formal_hash_and_commit_link(self):
        self.open_submissions()
        sha='A'*40
        for index,(repository,version) in enumerate([
            ('https://github.com/github/.github',sha),
            ('https://github.com/github/.github','B'*64),
            ('https://github.com/github/.github','https://github.com/github/.github/commit/'+sha),
            ('https://github.com/github/.github.git/','https://github.com/GitHub/.github/commit/'+sha),
        ],1):
            with self.subTest(repository=repository,version=version):
                client,_=self.access(self.team(index,'Synthetic dot repository '+str(index)))
                materials=self.materials(codeUrl=repository,codeVersion=version)
                self.assertEqual(self.save(client,status='draft',materials=materials)[0],200)
                status,result,_=self.save(client,1,materials=materials)
                self.assertEqual(status,200,result)
                self.assertEqual(result['submission']['submitted']['codeVersion'],('b'*64 if version=='B'*64 else 'a'*40))
                self.assertEqual(client.get('/api/team')[1]['submission']['submittedVersion'],2)

    def test_normalized_formal_draft_partial_legacy_and_readback(self):
        client,_=self.access(self.team())
        self.open_submissions()
        self.assertEqual(self.save(client,status='draft',materials={'codeVersion':'a1b'})[0],200)
        self.assertEqual(self.save(client,1,materials=self.materials(codeVersion='a1b'))[0],400)
        sha='B'*64
        status,data,_=self.save(client,1,materials=self.materials(codeVersion='https://github.com/synthetic/repository/commit/'+sha))
        self.assertEqual(status,200,data)
        self.assertEqual(data['submission']['submitted']['codeVersion'],sha.lower())
        self.assertEqual(client.get('/api/team')[1]['submission']['submitted']['codeVersion'],sha.lower())
        self.assertEqual(self.save(client,2,status='draft',materials={'codeVersion':'unfinished'})[0],200)
        self.assertEqual(client.get('/api/team')[1]['submission']['submitted']['codeVersion'],sha.lower())
        with self.service.db() as db:
            legacy=json.dumps(self.materials(codeVersion='old-tag'))
            db.execute('UPDATE submissions SET submitted_json=?',(legacy,))
        self.assertEqual(client.get('/api/team')[1]['submission']['submitted']['codeVersion'],'old-tag')
        self.assertEqual(self.save(client,3,materials=self.materials(codeVersion='old-tag'))[0],400)
        self.assertEqual(client.get('/api/team')[1]['submission']['submitted']['codeVersion'],'old-tag')

    def test_stage_snapshots_scoped_stable_and_distinct_from_latest(self):
        one=self.team(1,'One');two=self.team(2,'Two')
        first,_=self.access(one);second,_=self.access(two)
        judge,grant=self.invitation()
        self.open_submissions()
        self.assertEqual(self.save(first,materials=self.materials(summary='FIRST_VERSION'))[0],200)
        self.assertEqual(self.save(second,materials=self.materials(summary='OTHER_TEAM_SECRET'))[0],200)
        path='/api/admin/judging/exhibition'
        def command(action,**body):
            revision=self.admin.get(path)[1]['stage']['revision']
            status,data,_=self.admin.post(path+'/'+action,{'expectedRevision':revision,**body})
            self.assertEqual(status,200,data)
            return data
        empty=first.get('/api/team')[1]['judgingMaterials']
        self.assertFalse(empty['exhibition']['inRoster'])
        command('roster',teamIds=[one,two],judgeIds=[grant['id']])
        before=first.get('/api/team')[1]['judgingMaterials']['exhibition']
        self.assertIsNone(before['frozenAt']);self.assertIsNone(before['materialSnapshot'])
        opened=command('window',open=True,deadline=None)
        self.assertIsNotNone(opened['stage']['frozenAt'])
        frozen=first.get('/api/team')[1]['judgingMaterials']['exhibition']
        self.assertEqual(frozen['materialSnapshot']['submissionVersion'],1)
        self.assertEqual(self.save(first,1,materials=self.materials(summary='LATEST_VERSION',codeVersion='b'*40))[0],200)
        data=first.get('/api/team')[1]
        self.assertEqual(data['submission']['submittedVersion'],2)
        self.assertEqual(data['judgingMaterials']['exhibition'],frozen)
        self.assertNotIn('OTHER_TEAM_SECRET',json.dumps(data))
        self.assertNotIn('myReviews',data)
        for endpoint in ['/api/board','/api/judging/exhibition/board']:
            public=self.public.get(endpoint)[1]
            self.assertNotIn('FIRST_VERSION',json.dumps(public))
            self.assertNotIn('OTHER_TEAM_SECRET',json.dumps(public))
        self.assertEqual(next(t for t in judge.get('/api/judge')[1]['submissions'] if t['teamId']==one)['version'],2)
        snapshots=judge.get('/api/judging/exhibition')[1]['teams']
        self.assertEqual(next(t for t in snapshots if t['id']==one)['materialSnapshot']['submissionVersion'],1)
        admin_record=next(t for t in self.admin.get('/api/admin')[1]['submissions'] if t['teamId']==one)
        self.assertEqual(admin_record['judgingMaterials']['exhibition'],frozen)
        command('window',open=False,deadline=None);command('window',open=True,deadline=None)
        self.assertEqual(first.get('/api/team')[1]['judgingMaterials']['exhibition'],frozen)
        command('window',open=False,deadline=None);command('reset',reason='Synthetic new round')
        new=first.get('/api/team')[1]['judgingMaterials']['exhibition']
        self.assertNotEqual(new['runId'],frozen['runId']);self.assertIsNone(new['frozenAt'])
        command('window',open=True,deadline=None)
        self.assertEqual(first.get('/api/team')[1]['judgingMaterials']['exhibition']['materialSnapshot']['submissionVersion'],2)
        third=self.team(3,'No formal materials');third_client,_=self.access(third)
        road='/api/admin/judging/roadshow'
        revision=self.admin.get(road)[1]['stage']['revision']
        self.assertEqual(self.admin.post(road+'/roster',{'expectedRevision':revision,'teamIds':[third],'judgeIds':[grant['id']]})[0],200)
        revision=self.admin.get(road)[1]['stage']['revision']
        self.assertEqual(self.admin.post(road+'/window',{'expectedRevision':revision,'open':True,'deadline':None})[0],200)
        no_material=third_client.get('/api/team')[1]['judgingMaterials']['roadshow']
        self.assertTrue(no_material['inRoster']);self.assertIsNotNone(no_material['frozenAt']);self.assertIsNone(no_material['materialSnapshot'])
