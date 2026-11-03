"""Real administrator data flows against fresh temporary HTTP databases."""
import csv
from contextlib import closing,contextmanager
from concurrent.futures import ThreadPoolExecutor
import http.client
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import unittest
from urllib.parse import urlencode
from unittest.mock import patch

import app
from app import EventService
import test_api as fixtures
import test_roadshow as stage_fixtures
import test_submissions


class AdminDataTests(unittest.TestCase):
    setUp=fixtures.APITests.setUp
    tearDown=fixtures.APITests.tearDown
    team=fixtures.APITests.team
    invitation=fixtures.APITests.invitation
    stage=stage_fixtures.RoadshowIsolationTests.stage
    roster=stage_fixtures.RoadshowIsolationTests.roster
    window=stage_fixtures.RoadshowIsolationTests.window
    review=stage_fixtures.RoadshowIsolationTests.review
    judge=stage_fixtures.RoadshowIsolationTests.judge

    def get_data(self, endpoint, **params):
        path='/api/admin/data/'+endpoint+('?' + urlencode(params) if params else '')
        status,data,_=self.admin.get(path)
        self.assertEqual(status,200,data)
        return data

    def preview(self, kind, content):
        status,data,_=self.admin.post('/api/admin/data/import/preview',{'kind':kind,'csv':content})
        self.assertEqual(status,200,data)
        return data

    def commit(self, preview):
        return self.admin.post('/api/admin/data/import/commit',{'previewId':preview['previewId']})

    def entities(self):
        # Preview/audit/session rows may legitimately change; event entities must not.
        with self.service.db() as db:
            names=[row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            selected=[name for name in names if name in {'teams','accounts','submissions','scores','votes','team_accounts','team_details','credential_overrides'} or name.startswith('judging_')]
            return {name:sorted([tuple(row) for row in db.execute('SELECT * FROM '+name)],key=repr) for name in selected}

    def restart(self):
        old=self.service
        self.service=EventService(data_dir=old.data_dir,base_path=old.base_path,origin=old.origin,static_root=old.root)
        self.server.service=self.service

    def csv_data(self, kind, **params):
        status,body,headers=self.admin.get('/api/admin/data/export?'+urlencode({'kind':kind,**params}))
        self.assertEqual(status,200,body)
        self.assertTrue(headers['Content-Type'].startswith('text/csv'))
        self.assertTrue(body.startswith(b'\xef\xbb\xbf'))
        reader=csv.DictReader(io.StringIO(body.decode('utf-8-sig')))
        return reader.fieldnames,list(reader),body

    def test_preview_is_read_only_and_commit_atomically_creates_updates_and_consumes_once(self):
        existing=self.team(name='Before',track='AI for Science')
        before=self.entities()
        preview=self.preview('teams','\ufeffnumber,name,project,track,status,statusReason,booth,roadshowOrder\n'
            '1,After,Updated,AI for Science,active,,B01,2\n'
            '2,"Quoted, team",New,城市与社区问题,withdrawn,Team requested,B02,3\n')
        self.assertTrue(preview['canCommit'])
        self.assertEqual(preview['counts'],{'create':1,'update':1,'unchanged':0,'error':0})
        self.assertEqual([row['action'] for row in preview['rows']],['update','create'])
        self.assertEqual(self.entities(),before)
        self.assertNotIn('code',json.dumps(preview))
        status,result,_=self.commit(preview)
        self.assertEqual(status,200,result)
        self.assertEqual((result['created'],result['updated'],result['unchanged']),(1,1,0))
        listing=self.admin.get('/api/admin')[1]
        first=next(team for team in listing['teams'] if team['id']==existing)
        second=next(team for team in listing['teams'] if team['number']==2)
        self.assertEqual((first['name'],first['booth'],first['roadshowOrder']),('After','B01',2))
        self.assertEqual((second['name'],second['status'],second['statusReason']),('Quoted, team','withdrawn','Team requested'))
        after=self.entities()
        self.assertEqual(self.commit(preview)[0],409)
        self.assertEqual(self.entities(),after)
        unchanged=self.preview('teams','number,name,project,track,status,statusReason,booth,roadshowOrder\n'
            '1,After,Updated,AI for Science,active,,B01,2\n')
        self.assertEqual(unchanged['counts']['unchanged'],1)
        self.assertEqual(self.commit(unchanged)[0],200)
        self.assertEqual(self.entities(),after)

    def test_invalid_imports_never_partially_change_entities_or_issue_credentials(self):
        self.team(name='Retained',track='AI for Science')
        cases=[
            ('teams','number,name,project,track\n1,Would change,P,AI for Science\n2,New,P,AI for Science\n2,Duplicate,P,AI for Science\n'),
            ('teams','number,name,project,track,unknown\n2,New,P,AI for Science,bad\n'),
            ('teams','number,name,name,track\n2,A,B,AI for Science\n'),
            ('teams','number,name,project,track\n2,Missing cell,P\n'),
            ('teams','number,name,project,track,status,statusReason\n2,New,P,AI for Science,withdrawn,\n'),
            ('teams','number,name,project,track\n0,Invalid,P,AI for Science\n'),
            ('teams','number,name,project,track\n2,'+'x'*81+',P,AI for Science\n'),
            ('judges','id,name\n,Repeated name\n,Repeated name\n'),
            ('judges','id,name\nnot-an-existing-judge,Wrong target\n')]
        for kind,content in cases:
            with self.subTest(kind=kind,content=content[:100]):
                before=self.entities()
                preview=self.preview(kind,content)
                self.assertFalse(preview['canCommit'])
                self.assertGreater(preview['counts']['error'],0)
                self.assertTrue(any(row['errors'] for row in preview['rows']))
                self.assertIn(self.commit(preview)[0],[400,409])
                self.assertEqual(self.entities(),before)
        before=self.entities()
        for content in ['number,name,project,track\n'+''.join(f'{n},T{n},P,AI for Science\n' for n in range(1,502)),
                        'number,name,project,track\n2,'+'x'*(256*1024)+',P,AI for Science\n']:
            status,result,_=self.admin.post('/api/admin/data/import/preview',{'kind':'teams','csv':content})
            self.assertIn(status,[400,413],result)
            self.assertEqual(self.entities(),before)

    def test_preview_staleness_session_binding_expiry_and_revocation_are_enforced(self):
        team_id=self.team(name='Original',track='AI for Science')
        content='number,name,project,track\n1,Planned,P,AI for Science\n2,Must not partially create,P,AI for Science\n'
        preview=self.preview('teams',content)
        self.assertEqual(self.admin.post('/api/admin/teams',{'id':team_id,'number':1,'name':'Concurrent edit',
            'project':'P','track':'AI for Science'})[0],200)
        before=self.entities()
        self.assertEqual(self.commit(preview)[0],409)
        self.assertEqual(self.entities(),before)
        preview=self.preview('teams',content)
        other=fixtures.Client(self.server)
        self.assertEqual(other.post('/api/login',{'role':'admin','code':self.admin_code})[0],200)
        self.assertIn(other.post('/api/admin/data/import/commit',{'previewId':preview['previewId']})[0],[403,409])
        self.assertEqual(self.entities(),before)
        with patch('app.now_ms',return_value=preview['expiresAt']+1):
            self.assertEqual(self.commit(preview)[0],409)
        self.assertEqual(self.entities(),before)
        preview=self.preview('teams',content)
        self.assertEqual(self.admin.post('/api/logout',{})[0],200)
        self.assertEqual(self.commit(preview)[0],401)
        self.assertEqual(self.entities(),before)
        self.assertEqual(other.post('/api/admin/data/import/commit',{'csv':content,'kind':'teams'})[0],400)

    def test_judge_import_updates_only_explicit_ids_and_new_credentials_require_stage_grants(self):
        judge,old=self.invitation(name='Existing judge')
        preview=self.preview('judges','id,name\n'+old['id']+',Renamed judge\n,Existing judge\n')
        before=self.entities()
        self.assertTrue(preview['canCommit'])
        self.assertEqual(preview['counts']['create'],1)
        self.assertEqual(preview['counts']['update'],1)
        self.assertEqual(self.entities(),before)
        status,result,_=self.commit(preview)
        self.assertEqual(status,200,result)
        self.assertEqual(len(result['issuedCredentials']),1)
        issued=result['issuedCredentials'][0]
        self.assertNotEqual(issued['id'],old['id'])
        listing=self.admin.get('/api/admin')[1]
        self.assertEqual(next(row['name'] for row in listing['judges'] if row['id']==old['id']),'Renamed judge')
        self.assertEqual(judge.get('/api/session')[1]['role'],'judge')
        new=fixtures.Client(self.server)
        self.assertEqual(new.post('/api/login',{'role':'judge','code':issued['code']})[0],200)
        for stage in ['exhibition','roadshow']:
            self.assertEqual(new.get('/api/judging/'+stage)[0],403)
        self.assertNotIn(issued['code'],json.dumps(listing))
        audit=self.get_data('audit')
        self.assertNotIn(issued['code'],json.dumps(audit))

    def test_team_metadata_validation_and_concurrent_revision_keep_frozen_stage_identity(self):
        team_id=self.team(name='Frozen original',track='AI for Science')
        judge,invitation=self.invitation()
        self.roster('exhibition',[team_id],[invitation['id']])
        self.window('exhibition')
        frozen=self.judge(judge,'exhibition')['teams'][0]
        original=next(row for row in self.admin.get('/api/admin')[1]['teams'] if row['id']==team_id)
        payload={'id':team_id,'number':1,'name':'Changed directory','project':'P','track':'城市与社区问题',
            'expectedRevision':original['revision'],'status':'withdrawn','statusReason':'Requested withdrawal','booth':'B9','roadshowOrder':7}
        before=self.entities()
        for invalid in [{'statusReason':''},{'status':'invalid'},{'roadshowOrder':0},{'roadshowOrder':True},{'booth':'x'*81}]:
            self.assertEqual(self.admin.post('/api/admin/teams',{**payload,**invalid})[0],400)
            self.assertEqual(self.entities(),before)
        self.assertEqual(self.admin.post('/api/admin/teams',payload)[0],200)
        after=self.entities()
        self.assertEqual(self.admin.post('/api/admin/teams',{**payload,'name':'Stale overwrite'})[0],409)
        self.assertEqual(self.entities(),after)
        self.assertEqual(self.judge(judge,'exhibition')['teams'][0],frozen)
        listing=self.admin.get('/api/admin')[1]
        changed=next(row for row in listing['teams'] if row['id']==team_id)
        self.assertEqual((changed['status'],changed['booth'],changed['roadshowOrder']),('withdrawn','B9',7))
        self.assertTrue(changed['trackKnown'])
        unknown=self.team(2,'Historical unknown',track='AI for Science')
        with self.service.db() as db:
            db.execute('UPDATE teams SET track=? WHERE id=?',('Old custom track',unknown))
        self.assertFalse(next(row for row in self.admin.get('/api/admin')[1]['teams'] if row['id']==unknown)['trackKnown'])
        self.assertEqual(self.admin.post('/api/admin/teams',{'id':unknown,'number':2,'name':'Historical preserved',
            'project':'P','track':'Old custom track'})[0],200)
        self.assertEqual(next(row for row in self.admin.get('/api/admin')[1]['teams'] if row['id']==unknown)['track'],'Old custom track')

    def review_fixture(self):
        first=self.team(name='Frozen A',track='AI for Science')
        second=self.team(2,'Frozen B',track='城市与社区问题')
        one,one_invite=self.invitation(name='PRIVATE_JUDGE_ONE')
        two,two_invite=self.invitation(name='PRIVATE_JUDGE_TWO')
        for stage in ['exhibition','roadshow']:
            self.roster(stage,[first,second],[one_invite['id'],two_invite['id']])
            self.window(stage)
        return first,second,one,two,one_invite['id'],two_invite['id']

    def test_filtered_coverage_uses_frozen_run_roster_and_missing_valid_formal_combinations(self):
        first,second,one,two,one_id,two_id=self.review_fixture()
        self.assertEqual(self.review(one,'exhibition',first,feedback='E_FORMAL')[0],200)
        self.assertEqual(self.review(one,'exhibition',first,1,'draft','E_LATER_DRAFT')[0],200)
        self.assertEqual(self.review(two,'exhibition',first,state='recused',feedback='')[0],200)
        self.assertEqual(self.review(one,'exhibition',second,state='draft',feedback='E_ONLY_DRAFT')[0],200)
        self.assertEqual(self.review(one,'roadshow',first,feedback='R_FORMAL')[0],200)
        run=self.stage('exhibition')['stage']['runId']
        params={'stage':'exhibition','runId':run}
        all_rows=self.get_data('reviews',**params)
        self.assertEqual(all_rows['coverage'],{'expected':4,'recorded':3,'validSubmitted':1,'missing':3})
        self.assertEqual(len(all_rows['reviews']),4)
        missing=next(row for row in all_rows['reviews'] if row['teamId']==second and row['judgeId']==two_id)
        self.assertEqual((missing['version'],missing['status'],missing['draft'],missing['submitted']),(0,'missing',None,None))
        expected={'draft':2,'submitted':1,'recused':1,'invalidated':0,'missing':3}
        for status,count in expected.items():
            result=self.get_data('reviews',**params,status=status)
            self.assertEqual(len(result['reviews']),count,status)
            self.assertEqual(result['coverage'],all_rows['coverage'])
            self.assertNotIn('R_FORMAL',json.dumps(result))
        self.assertEqual(self.get_data('reviews',**params,teamId=first)['coverage']['expected'],2)
        self.assertEqual(self.get_data('reviews',**params,judgeId=one_id)['coverage']['expected'],2)
        self.assertEqual(self.get_data('reviews',**params,teamId=first,judgeId=one_id)['coverage']['expected'],1)
        self.assertEqual(self.admin.post('/api/admin/teams',{'id':first,'number':1,'name':'Current renamed',
            'project':'Current P','track':'未来学习样态'})[0],200)
        filtered=self.get_data('reviews',**params,track='AI for Science')
        self.assertEqual([row['name'] for row in filtered['teams']],['Frozen A'])
        self.assertEqual(filtered['coverage']['expected'],2)
        self.window('exhibition',False)
        old=self.stage('exhibition')
        self.assertEqual(self.admin.post('/api/admin/judging/exhibition/reset',{
            'expectedRevision':old['stage']['revision'],'reason':'Historical lookup fixture'})[0],200)
        runs=self.get_data('runs',stage='exhibition')['runs']
        self.assertEqual(len(runs),2)
        self.assertEqual(sum(row['current'] for row in runs),1)
        self.assertFalse(next(row for row in runs if row['id']==run)['current'])
        historical=self.get_data('reviews',**params)
        self.assertEqual(historical['reviews'],all_rows['reviews'])
        self.assertEqual(historical['teams'],all_rows['teams'])
        self.assertEqual(self.get_data('reviews',stage='exhibition')['coverage']['recorded'],0)
        other_run=self.stage('roadshow')['stage']['runId']
        self.assertIn(self.admin.get('/api/admin/data/reviews?'+urlencode({'stage':'exhibition','runId':other_run}))[0],[400,404])
        for query in ['unknown=1','stage=exhibition&stage=roadshow','status=unsupported']:
            self.assertEqual(self.admin.get('/api/admin/data/reviews?'+query)[0],400)

    def test_private_revision_history_and_reasoned_validity_preserve_feedback_and_counts(self):
        team_id,_,judge,_,judge_id,_=self.review_fixture()
        self.assertEqual(self.review(judge,'exhibition',team_id,state='draft',feedback='PRIVATE_DRAFT_V1')[0],200)
        self.assertEqual(self.review(judge,'exhibition',team_id,1,feedback='PRIVATE_FORMAL_V2')[0],200)
        self.assertEqual(self.review(judge,'roadshow',team_id,feedback='PRIVATE_ROADSHOW_FORMAL')[0],200)
        run=self.stage('exhibition')['stage']['runId']
        target={'runId':run,'teamId':team_id,'judgeId':judge_id}
        history=self.get_data('review-history',**target)
        self.assertIsNotNone(history['historyAvailableFrom'])
        self.assertEqual([row['version'] for row in history['history']],[1,2])
        self.assertIsNone(history['history'][0]['before'])
        self.assertEqual(history['history'][1]['before']['draft']['feedback'],'PRIVATE_DRAFT_V1')
        self.assertEqual(history['history'][1]['after']['submitted']['feedback'],'PRIVATE_FORMAL_V2')
        before=self.stage('exhibition')['reviews']
        invalid={'expectedVersion':2,'invalidated':True,'reason':'Documented administrative correction'}
        for change in [{'reason':''},{'invalidated':'true'},{'feedback':'Forged administrator feedback'}]:
            self.assertEqual(self.admin.post('/api/admin/data/review-validity',{**target,**invalid,**change})[0],400)
            self.assertEqual(self.stage('exhibition')['reviews'],before)
        status,result,_=self.admin.post('/api/admin/data/review-validity',{**target,**invalid})
        self.assertEqual(status,200,result)
        self.assertEqual(result['review']['version'],3)
        self.assertTrue(result['review']['invalidated'])
        self.assertEqual(result['review']['submitted']['feedback'],'PRIVATE_FORMAL_V2')
        self.assertEqual(self.public.get('/api/judging/exhibition/board')[1]['teams'][0]['reviewCount'],0)
        self.assertEqual(self.public.get('/api/judging/roadshow/board')[1]['teams'][0]['reviewCount'],1)
        self.assertEqual(len(self.get_data('reviews',stage='exhibition',status='invalidated')['reviews']),1)
        self.assertEqual(self.admin.post('/api/admin/data/review-validity',{**target,**invalid,'invalidated':False})[0],409)
        self.assertEqual(self.review(judge,'exhibition',team_id,3,feedback='PRIVATE_JUDGE_REVISION_V4')[0],200)
        current=self.judge(judge,'exhibition')['myReviews'][0]
        self.assertTrue(current['invalidated'],'A judge edit must not silently undo administrative invalidation')
        self.assertEqual(current['version'],4)
        self.assertEqual(self.public.get('/api/judging/exhibition/board')[1]['teams'][0]['reviewCount'],0)
        status,result,_=self.admin.post('/api/admin/data/review-validity',{
            **target,'expectedVersion':4,'invalidated':False,'reason':'Correction verified; restore this record'})
        self.assertEqual(status,200,result)
        self.assertEqual(result['review']['version'],5)
        self.assertEqual(result['review']['submitted']['feedback'],'PRIVATE_JUDGE_REVISION_V4')
        self.assertEqual(self.public.get('/api/judging/exhibition/board')[1]['teams'][0]['reviewCount'],1)
        history=self.get_data('review-history',**target)
        self.assertEqual([row['version'] for row in history['history']],[1,2,3,4,5])
        self.assertEqual(history['history'][2]['reason'],invalid['reason'])
        serialized=json.dumps(self.get_data('audit'))
        for private in ['PRIVATE_DRAFT_V1','PRIVATE_FORMAL_V2','PRIVATE_JUDGE_REVISION_V4','PRIVATE_ROADSHOW_FORMAL']:
            self.assertNotIn(private,serialized)
        self.restart()
        self.assertEqual(self.get_data('review-history',**target),history)
        self.window('exhibition',False)
        current=self.stage('exhibition')
        self.assertEqual(self.admin.post('/api/admin/judging/exhibition/reset',{
            'expectedRevision':current['stage']['revision'],'reason':'Keep old run read only'})[0],200)
        self.assertEqual(self.admin.post('/api/admin/data/review-validity',{
            **target,'expectedVersion':5,'invalidated':True,'reason':'Cannot change archived round'})[0],409)
        self.assertEqual(self.get_data('review-history',**target),history)

    def test_classified_csv_exports_follow_filters_escape_formulas_and_never_export_credentials(self):
        team_id=self.team(name='=FORMULA_TEAM',project='Line one\nLine two, "quoted"',track='AI for Science')
        other=self.team(2,'OTHER_TEAM',track='城市与社区问题')
        judge,invitation=self.invitation(name='+FORMULA_JUDGE')
        self.roster('roadshow',[team_id,other],[invitation['id']])
        self.window('roadshow')
        self.assertEqual(self.review(judge,'roadshow',team_id,feedback='=FORMULA_FEEDBACK\nSecond, "line"')[0],200)
        self.assertEqual(self.review(judge,'roadshow',other,feedback='UNRELATED_FEEDBACK')[0],200)
        run=self.stage('roadshow')['stage']['runId']
        filters={'stage':'roadshow','runId':run,'teamId':team_id,'judgeId':invitation['id']}
        for kind in ['teams','judges','reviews','rosters','audit']:
            with self.subTest(kind=kind):
                fields,rows,raw=self.csv_data(kind,**filters)
                self.assertTrue(fields)
                self.assertTrue(rows)
                serialized=raw.decode('utf-8-sig')
                for private in [invitation['code'],'code_hash','token_hash','salt']:
                    self.assertNotIn(private,serialized)
                for row in rows:
                    for cell in row.values():
                        self.assertFalse(cell.lstrip().startswith(('=','+','-','@','\t','\r')),cell)
                if kind in ['teams','reviews','rosters']:
                    self.assertNotIn('OTHER_TEAM',serialized)
                    self.assertNotIn('UNRELATED_FEEDBACK',serialized)
                if kind=='teams':
                    self.assertTrue(any("'=FORMULA_TEAM" in row.values() for row in rows))
                    self.assertTrue(any('Line one\nLine two, "quoted"' in row.values() for row in rows))
                if kind=='judges':
                    self.assertTrue(any("'+FORMULA_JUDGE" in row.values() for row in rows))
                if kind=='reviews':
                    self.assertIn(run,serialized)
                    self.assertIn('roadshow',serialized)
                    self.assertIn('未计分',serialized)
                    self.assertTrue(any("'=FORMULA_FEEDBACK\nSecond, \"line\"" in row.values() for row in rows))
                if kind=='rosters':
                    self.assertTrue('manual_worklist' in serialized or '人工' in serialized)
        for kind in ['teams','judges']:
            status,body,_=self.admin.get('/api/admin/data/template?kind='+kind)
            self.assertEqual(status,200)
            self.assertTrue(body.startswith(b'\xef\xbb\xbf'))
            self.assertTrue(list(csv.reader(io.StringIO(body.decode('utf-8-sig')))))
        for path in ['/api/admin/submissions/export','/api/admin/export']:
            status,body,_=self.admin.get(path)
            self.assertEqual(status,200)
            self.assertTrue(body.startswith(b'\xef\xbb\xbf'))
        self.assertIn(self.admin.get('/api/admin/data/export?kind=rankings')[0],[400,409])

    def test_admin_permissions_query_validation_and_non_recursive_audit_pagination(self):
        team_id=self.team(name='Audit before',track='AI for Science')
        judge,invitation=self.invitation()
        for client,status in [(self.public,401),(judge,403)]:
            for path in ['/api/admin/data/runs?stage=roadshow','/api/admin/data/reviews?stage=roadshow',
                         '/api/admin/data/audit','/api/admin/data/export?kind=teams','/api/admin/data/template?kind=teams']:
                self.assertEqual(client.get(path)[0],status,path)
            for path in ['/api/admin/data/import/preview','/api/admin/data/import/commit',
                         '/api/admin/data/review-validity','/api/admin/credentials']:
                self.assertEqual(client.post(path,{})[0],status,path)
        self.assertEqual(self.admin.post('/api/admin/teams',{'id':team_id,'number':1,'name':'Audit after',
            'project':'P','track':'AI for Science'})[0],200)
        with self.service.db() as db:
            before=db.execute('SELECT COUNT(*) FROM audit').fetchone()[0]
        first=self.get_data('audit',limit=1)
        self.assertEqual(len(first['entries']),1)
        self.assertIsNotNone(first['nextBeforeId'])
        second=self.get_data('audit',limit=1,beforeId=first['nextBeforeId'])
        self.assertEqual(len(second['entries']),1)
        self.assertLess(second['entries'][0]['id'],first['entries'][0]['id'])
        filtered=self.get_data('audit',action='team.save',target=team_id)
        self.assertTrue(filtered['entries'])
        self.assertTrue(all(row['action']=='team.save' and row['target']==team_id for row in filtered['entries']))
        change=next(row for row in filtered['entries'] if 'Audit after' in json.dumps(row['metadata']))
        self.assertIn('Audit before',json.dumps(change['metadata']))
        self.assertIsNotNone(change['actorId'])
        self.assertEqual(change['role'],'admin')
        with self.service.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM audit').fetchone()[0],before)
        for query in ['limit=0','limit=101','limit=false','beforeId=-1','beforeId=9223372036854775808','unknown=1','limit=1&limit=2']:
            self.assertEqual(self.admin.get('/api/admin/data/audit?'+query)[0],400,query)

    def test_individual_rotation_and_revocation_invalidate_sessions_and_survive_restart(self):
        team_id=self.team(track='AI for Science')
        judge,invitation=self.invitation()
        audience,audience_invitation=self.invitation('audience','Synthetic audience')
        self.roster('roadshow',[team_id],[invitation['id']])
        self.window('roadshow')
        self.assertEqual(self.review(judge,'roadshow',team_id,feedback='KEPT_AFTER_ROTATION')[0],200)
        known_codes=[]
        for role,client,old in [('judge',judge,invitation),('audience',audience,audience_invitation)]:
            status,rotated,_=self.admin.post('/api/admin/credentials',{'role':role,'id':old['id'],'action':'rotate'})
            self.assertEqual(status,200,rotated)
            self.assertTrue(rotated['active'])
            self.assertEqual(rotated['credentialMode'],'individual')
            self.assertNotEqual(rotated['code'],old['code'])
            known_codes.extend([old['code'],rotated['code']])
            self.assertIsNone(client.get('/api/session')[1]['role'])
            self.assertEqual(fixtures.Client(self.server).post('/api/login',{'role':role,'code':old['code']})[0],401)
            fresh=fixtures.Client(self.server)
            self.assertEqual(fresh.post('/api/login',{'role':role,'code':rotated['code']})[0],200)
            self.restart()
            self.assertEqual(fresh.get('/api/session')[1]['role'],role)
            self.assertEqual(fixtures.Client(self.server).post('/api/login',{'role':role,'code':rotated['code']})[0],200)
            status,revoked,_=self.admin.post('/api/admin/credentials',{'role':role,'id':old['id'],'action':'revoke'})
            self.assertEqual(status,200,revoked)
            self.assertFalse(revoked['active'])
            self.assertNotIn('code',revoked)
            self.assertIsNone(fresh.get('/api/session')[1]['role'])
            self.restart()
            self.assertEqual(fixtures.Client(self.server).post('/api/login',{'role':role,'code':rotated['code']})[0],401)
        self.assertEqual(self.stage('roadshow')['reviews'][0]['submitted']['feedback'],'KEPT_AFTER_ROTATION')
        listing=json.dumps(self.admin.get('/api/admin')[1])+json.dumps(self.get_data('audit'))
        for code in known_codes:
            self.assertNotIn(code,listing)

    def test_rotated_shared_judge_leaves_shared_login_and_is_not_reset_on_restart(self):
        _,first=self.invitation(name='Shared first')
        _,second=self.invitation(name='Shared second')
        shared='SYNTHETIC_SHARED_112233'
        (self.service.data_dir/'shared-login-codes.json').write_text(json.dumps({'judge':shared}),encoding='utf-8')
        self.restart()
        selected=fixtures.Client(self.server)
        self.assertEqual(selected.post('/api/login',{'role':'judge','code':shared,'accountId':first['id']})[0],200)
        status,rotated,_=self.admin.post('/api/admin/credentials',{'role':'judge','id':first['id'],'action':'rotate'})
        self.assertEqual(status,200,rotated)
        self.assertEqual(rotated['credentialMode'],'individual')
        self.assertIsNone(selected.get('/api/session')[1]['role'])
        for _ in range(2):
            self.restart()
            self.assertEqual(fixtures.Client(self.server).post('/api/login',{
                'role':'judge','code':shared,'accountId':first['id']})[0],401)
            self.assertEqual(fixtures.Client(self.server).post('/api/login',{
                'role':'judge','code':rotated['code']})[0],200)
            self.assertEqual(fixtures.Client(self.server).post('/api/login',{
                'role':'judge','code':shared,'accountId':second['id']})[0],200)
        listing=self.admin.get('/api/admin')[1]['judges']
        self.assertEqual(next(row for row in listing if row['id']==first['id'])['credentialMode'],'individual')
        self.assertEqual(next(row for row in listing if row['id']==second['id'])['credentialMode'],'shared')

    def test_four_windows_are_independent_and_exact_deadlines_reject_all_writes(self):
        team_id=self.team(track='AI for Science')
        judge,invitation=self.invitation()
        audience,_=self.invitation('audience','Deadline audience')
        issued=self.admin.post('/api/admin/team-access',{'teamId':team_id,'action':'issue'})[1]
        team_client=fixtures.Client(self.server)
        self.assertEqual(team_client.post('/api/login',{'role':'team','code':issued['code']})[0],200)
        for stage in ['exhibition','roadshow']:
            self.roster(stage,[team_id],[invitation['id']])
            self.window(stage)
        self.assertEqual(self.admin.settings({
            'submissionsOpen':True,'votingOpen':True,'scoringOpen':False})[0],200)
        self.assertTrue(audience.get('/api/audience')[1]['votingOpen'])
        self.assertEqual(judge.score({'teamId':team_id,'scores':[0]*6,'status':'submitted'})[0],409)
        self.window('exhibition',False)
        self.assertFalse(self.judge(judge,'exhibition')['window']['canEdit'])
        self.assertTrue(self.judge(judge,'roadshow')['window']['canEdit'])
        self.assertTrue(team_client.get('/api/team')[1]['window']['canEdit'])
        self.assertTrue(audience.get('/api/audience')[1]['votingOpen'])
        self.assertEqual(self.admin.settings({'submissionsOpen':False})[0],200)
        self.assertTrue(self.judge(judge,'roadshow')['window']['canEdit'])
        self.assertTrue(audience.get('/api/audience')[1]['votingOpen'])
        self.assertEqual(self.admin.settings({'votingOpen':False,'scoringOpen':True})[0],200)
        self.assertFalse(audience.get('/api/audience')[1]['votingOpen'])
        self.assertEqual(audience.post('/api/vote',{'teamId':team_id})[0],409)
        deadline=app.now_ms()+100000
        for stage in ['exhibition','roadshow']:
            self.window(stage,True,deadline)
        self.assertEqual(self.admin.settings({
            'submissionsOpen':True,'submissionDeadline':deadline,'votingOpen':True,'votingDeadline':deadline})[0],200)
        for now,can_edit in [(deadline-1,True),(deadline,False),(deadline+1,False)]:
            with patch('app.now_ms',return_value=now):
                self.assertEqual(team_client.get('/api/team')[1]['window']['canEdit'],can_edit)
                self.assertEqual(audience.get('/api/audience')[1]['window']['canEdit'],can_edit)
                for stage in ['exhibition','roadshow']:
                    self.assertEqual(self.judge(judge,stage)['window']['canEdit'],can_edit)
                if not can_edit:
                    before=self.entities()
                    self.assertEqual(team_client.post('/api/submission',{'expectedVersion':0,'status':'submitted',
                        'materials':test_submissions.SubmissionAPITests.materials(self)})[0],409)
                    self.assertEqual(audience.post('/api/vote',{'teamId':team_id})[0],409)
                    for stage in ['exhibition','roadshow']:
                        self.assertEqual(self.review(judge,stage,team_id)[0],409)
                    self.assertEqual(self.entities(),before)

    def test_a_queued_audience_vote_rechecks_its_own_window_after_the_write_lock(self):
        team_id=self.team(track='AI for Science')
        audience,_=self.invitation('audience','Queued audience')
        self.assertEqual(self.admin.settings({'votingOpen':True,'votingDeadline':None})[0],200)
        original_db=self.service.db
        queued=threading.Event()
        @contextmanager
        def traced_db():
            with original_db() as db:
                db.set_trace_callback(lambda sql:queued.set() if sql=='BEGIN IMMEDIATE' else None)
                yield db
        result=[]
        with closing(sqlite3.connect(self.service.db_path)) as lock:
            lock.execute('BEGIN IMMEDIATE')
            with patch.object(self.service,'db',traced_db):
                thread=threading.Thread(target=lambda:result.append(audience.post('/api/vote',{'teamId':team_id})))
                thread.start()
                self.assertTrue(queued.wait(3),'Vote did not queue behind the fixture write lock')
                lock.execute("UPDATE settings SET value=? WHERE key='votingDeadline'",(json.dumps(app.now_ms()-1),))
                lock.commit()
                thread.join(5)
                self.assertFalse(thread.is_alive())
        self.assertEqual(result[0][0],409,result)
        self.assertEqual(audience.get('/api/audience')[1]['votedTeamIds'],[])

    def test_csv_byte_limit_does_not_reject_legal_unicode_after_json_transport_escaping(self):
        rows=['number,name,project,track,status,statusReason,booth,roadshowOrder']
        rows.extend(f'{number},'+('中'*70)+','+('文'*130)+',AI for Science,active,,A,1' for number in range(1,351))
        content='\n'.join(rows)+'\n'
        self.assertLess(len(content.encode('utf-8')),262144)
        encoded=json.dumps({'kind':'teams','csv':content},ensure_ascii=True).encode()
        self.assertGreater(len(encoded),300000)
        before=self.entities()
        status,result,_=self.admin.request('POST','/api/admin/data/import/preview',raw=encoded)
        self.assertEqual(status,200,result)
        self.assertTrue(result['canCommit'])
        self.assertEqual(result['counts']['create'],350)
        self.assertEqual(self.entities(),before)

    def test_import_preview_transport_limit_stays_at_two_mib_and_rejects_larger_bodies(self):
        before=self.entities()
        conn=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=20)
        conn.putrequest('POST',self.server.service.base_path+'/api/admin/data/import/preview')
        conn.putheader('Cookie',self.admin.cookie)
        conn.putheader('Content-Type','application/json')
        conn.putheader('Origin',self.server.service.origin)
        conn.putheader('Content-Length',str(app.IMPORT_BODY_LIMIT+1))
        conn.endheaders()
        response=conn.getresponse()
        self.assertEqual(response.status,413)
        response.read()
        conn.close()
        self.assertEqual(self.entities(),before)

    def test_concurrent_import_consumption_and_competing_new_team_plans_are_atomic(self):
        judges=self.preview('judges','id,name\n,Synthetic concurrent judge\n')
        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses=list(pool.map(lambda _:self.commit(judges)[0],range(2)))
        self.assertEqual(sorted(statuses),[200,409])
        self.assertEqual(sum(row['name']=='Synthetic concurrent judge' for row in self.admin.get('/api/admin')[1]['judges']),1)
        plans=[self.preview('teams','number,name,project,track\n2,'+name+',P,AI for Science\n') for name in ['Concurrent A','Concurrent B']]
        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses=list(pool.map(lambda preview:self.commit(preview)[0],plans))
        self.assertEqual(sorted(statuses),[200,409])
        created=[row for row in self.admin.get('/api/admin')[1]['teams'] if row['number']==2]
        self.assertEqual(len(created),1)
        self.assertIn(created[0]['name'],['Concurrent A','Concurrent B'])

    def test_judge_preview_is_invalid_after_target_credentials_are_revoked_or_rotated(self):
        _,invitation=self.invitation(name='Credential import target')
        content='id,name\n'+invitation['id']+',Should not overwrite\n,Must not be partially created\n'
        for action in ['rotate','revoke']:
            preview=self.preview('judges',content)
            self.assertEqual(self.admin.post('/api/admin/credentials',{'role':'judge','id':invitation['id'],'action':action})[0],200)
            before=self.entities()
            self.assertEqual(self.commit(preview)[0],409)
            self.assertEqual(self.entities(),before)

    def test_upgraded_reviews_get_only_a_known_baseline_and_an_automatic_private_backup(self):
        team_id=self.team(track='AI for Science')
        judge,invitation=self.invitation()
        self.roster('exhibition',[team_id],[invitation['id']])
        self.window('exhibition')
        self.assertEqual(self.review(judge,'exhibition',team_id,state='draft',feedback='UNKNOWN_PREUPGRADE_DRAFT')[0],200)
        self.assertEqual(self.review(judge,'exhibition',team_id,1,feedback='KNOWN_BASELINE_ONLY')[0],200)
        run=self.stage('exhibition')['stage']['runId']
        with self.service.db() as db:
            db.execute('DROP TABLE judging_review_history')
            db.execute('DROP TABLE admin_data_meta')
        self.restart()
        target={'runId':run,'teamId':team_id,'judgeId':invitation['id']}
        history=self.get_data('review-history',**target)
        self.assertEqual(len(history['history']),1)
        row=history['history'][0]
        self.assertEqual((row['action'],row['version'],row['before']),('baseline',2,None))
        self.assertEqual(row['after']['submitted']['feedback'],'KNOWN_BASELINE_ONLY')
        self.assertNotIn('UNKNOWN_PREUPGRADE_DRAFT',json.dumps(history))
        backups=list(self.service.data_dir.glob('before-admin-data-*.sqlite3'))
        self.assertEqual(len(backups),1)
        self.assertEqual(backups[0].stat().st_mode&0o777,0o600)
        with closing(sqlite3.connect(backups[0])) as db:
            self.assertEqual(db.execute("SELECT submitted_feedback FROM judging_reviews WHERE run_id=?",(run,)).fetchone()[0],'KNOWN_BASELINE_ONLY')
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='admin_data_meta'").fetchone())
        self.restart()
        self.assertEqual(self.get_data('review-history',**target),history)
        self.assertEqual(len(list(self.service.data_dir.glob('before-admin-data-*.sqlite3'))),1)

    def test_private_backup_and_restore_preserve_all_entities_history_grants_materials_and_audit(self):
        team_id=self.team(name='Backup fixture',track='AI for Science')
        judge,invitation=self.invitation()
        issued=self.admin.post('/api/admin/team-access',{'teamId':team_id,'action':'issue'})[1]
        client=fixtures.Client(self.server)
        self.assertEqual(client.post('/api/login',{'role':'team','code':issued['code']})[0],200)
        self.assertEqual(self.admin.settings({'submissionsOpen':True})[0],200)
        self.assertEqual(client.post('/api/submission',{'expectedVersion':0,'status':'submitted',
            'materials':test_submissions.SubmissionAPITests.materials(self,projectName='BACKUP_FORMAL_MATERIAL')})[0],200)
        for stage in ['exhibition','roadshow']:
            self.roster(stage,[team_id],[invitation['id']])
            self.window(stage)
            self.assertEqual(self.review(judge,stage,team_id,feedback='BACKUP_'+stage)[0],200)
        run=self.stage('exhibition')['stage']['runId']
        self.assertEqual(self.admin.post('/api/admin/data/review-validity',{
            'runId':run,'teamId':team_id,'judgeId':invitation['id'],'expectedVersion':1,
            'invalidated':True,'reason':'Backup validity fixture'})[0],200)
        script=Path(__file__).resolve().parents[1]/'scripts'/'backup_event.py'
        backup=self.root/'private-backup.sqlite3'
        restored=self.root/'private-restored.sqlite3'
        def all_tables(path):
            with closing(sqlite3.connect(path)) as db:
                tables=[row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
                return {name:sorted([tuple(row) for row in db.execute('SELECT * FROM '+name)],key=repr) for name in tables}
        expected=all_tables(self.service.db_path)
        for action,source,destination in [('backup',self.service.db_path,backup),('restore',backup,restored)]:
            result=subprocess.run([sys.executable,'-B',str(script),action,str(source),str(destination)],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(destination.stat().st_mode&0o777,0o600)
            self.assertEqual(all_tables(destination),expected)
            with closing(sqlite3.connect(destination)) as db:
                self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
                self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(),[])
        original_bytes=restored.read_bytes()
        refused=subprocess.run([sys.executable,'-B',str(script),'restore',str(backup),str(restored)],capture_output=True,text=True)
        self.assertNotEqual(refused.returncode,0)
        self.assertEqual(restored.read_bytes(),original_bytes)
        self.assertEqual(all_tables(self.service.db_path),expected)

    def test_enormous_numeric_csv_fields_are_row_errors_and_never_server_errors(self):
        before=self.entities()
        huge='9'*5000
        cases=[f'number,name,project,track\n{huge},Invalid number,P,AI for Science\n',
               f'number,name,project,track,roadshowOrder\n1,Invalid order,P,AI for Science,{huge}\n']
        for content in cases:
            with self.subTest(column='number' if content.startswith('number,name,project,track\n') else 'roadshowOrder'):
                result=self.preview('teams',content)
                self.assertFalse(result['canCommit'])
                self.assertEqual(result['counts']['error'],1)
                self.assertTrue(result['rows'][0]['errors'])
                self.assertEqual(self.commit(result)[0],400)
                self.assertEqual(self.entities(),before)

    def test_team_and_judge_exports_project_the_selected_historical_run_and_record_status(self):
        first=self.team(name='Frozen first',track='AI for Science')
        second=self.team(2,'Frozen second',track='城市与社区问题')
        outside=self.team(3,'OUTSIDE_ROSTER',track='未来学习样态')
        one,one_invite=self.invitation(name='Authorized one')
        two,two_invite=self.invitation(name='Authorized two')
        _,outside_invite=self.invitation(name='OUTSIDE_AUTHORIZATION')
        self.roster('exhibition',[second],[outside_invite['id']])
        self.window('exhibition')
        self.roster('roadshow',[first,second],[one_invite['id'],two_invite['id']])
        self.window('roadshow')
        self.assertEqual(self.review(one,'roadshow',first,feedback='Historical formal')[0],200)
        self.assertEqual(self.review(two,'roadshow',first,state='recused',feedback='')[0],200)
        self.assertEqual(self.review(one,'roadshow',second,state='draft',feedback='Historical draft')[0],200)
        historical=self.stage('roadshow')['stage']['runId']
        with self.service.db() as db:
            frozen_at=db.execute('SELECT frozen_at FROM judging_runs WHERE id=?',(historical,)).fetchone()[0]
            original_team_times={row['id']:row['updated_at'] for row in db.execute('SELECT id,updated_at FROM teams')}
            judge_times={row['id']:row['created_at'] for row in db.execute("SELECT id,created_at FROM accounts WHERE role='judge'")}
        for team_id,number,name,track in [(first,1,'CURRENT_FIRST','未来学习样态'),(second,2,'CURRENT_SECOND','AI for Science')]:
            self.assertEqual(self.admin.post('/api/admin/teams',{'id':team_id,'number':number,'name':name,'track':track})[0],200)
        self.window('roadshow',False)
        current=self.stage('roadshow')
        self.assertEqual(self.admin.post('/api/admin/judging/roadshow/reset',{
            'expectedRevision':current['stage']['revision'],'reason':'New independent worklist'})[0],200)
        self.roster('roadshow',[outside],[outside_invite['id']])
        self.window('roadshow')
        filters={'stage':'roadshow','runId':historical}
        _,teams,_=self.csv_data('teams',**filters)
        self.assertEqual({row['id'] for row in teams},{first,second})
        self.assertEqual({row['name'] for row in teams},{'Frozen first','Frozen second'})
        self.assertEqual({row['track'] for row in teams},{'AI for Science','城市与社区问题'})
        for row in teams:
            self.assertEqual((row['stage'],row['runId'],row['frozenAt'],row['recordFilter']),('roadshow',historical,str(frozen_at),'all'))
            self.assertEqual(row['updatedAt'],str(original_team_times[row['id']]))
        _,judges,_=self.csv_data('judges',**filters)
        self.assertEqual({row['id'] for row in judges},{one_invite['id'],two_invite['id']})
        for row in judges:
            self.assertEqual((row['stage'],row['runId'],row['frozenAt'],row['recordFilter']),('roadshow',historical,str(frozen_at),'all'))
            self.assertEqual(row['createdAt'],str(judge_times[row['id']]))
        for status,expected_teams,expected_judges in [
            ('submitted',{first},{one_invite['id']}),('recused',{first},{two_invite['id']}),
            ('draft',{second},{one_invite['id']}),('missing',{first,second},{one_invite['id'],two_invite['id']})]:
            _,team_rows,_=self.csv_data('teams',**filters,status=status)
            _,judge_rows,_=self.csv_data('judges',**filters,status=status)
            self.assertEqual({row['id'] for row in team_rows},expected_teams,status)
            self.assertEqual({row['id'] for row in judge_rows},expected_judges,status)
            self.assertTrue(all(row['recordFilter']==status and row['runId']==historical for row in [*team_rows,*judge_rows]))
        _,team_rows,_=self.csv_data('teams',**filters,track='城市与社区问题',status='submitted')
        _,judge_rows,_=self.csv_data('judges',**filters,track='城市与社区问题',status='submitted')
        self.assertEqual(team_rows,[])
        self.assertEqual(judge_rows,[])
        for kind,expected in [('teams',{outside}),('judges',{outside_invite['id']})]:
            _,rows,_=self.csv_data(kind,stage='roadshow')
            self.assertEqual({row['id'] for row in rows},expected)
        _,teams,_=self.csv_data('teams',stage='exhibition')
        self.assertEqual([(row['id'],row['name']) for row in teams],[(second,'Frozen second')])
        for kind in ['reviews','rosters']:
            _,rows,_=self.csv_data(kind,**filters)
            self.assertEqual(len(rows),4)
            self.assertTrue(all(row['stage']=='roadshow' and row['runId']==historical and row['frozenAt']==str(frozen_at) for row in rows))
        with self.service.db() as db:
            current_team_times={row['id']:row['updated_at'] for row in db.execute('SELECT id,updated_at FROM teams')}
        _,global_teams,_=self.csv_data('teams')
        _,global_judges,_=self.csv_data('judges')
        self.assertEqual({row['id']:row['updatedAt'] for row in global_teams},{key:str(value) for key,value in current_team_times.items()})
        self.assertEqual({row['id']:row['createdAt'] for row in global_judges},{key:str(value) for key,value in judge_times.items()})

    def test_management_audit_links_import_batches_and_safe_before_after_credential_changes(self):
        preview=self.preview('teams','number,name,project,track\n1,Audit imported,P,AI for Science\n2,Audit second,P,AI for Science\n')
        self.assertEqual(self.commit(preview)[0],200)
        teams=self.admin.get('/api/admin')[1]['teams']
        team_id=next(row['id'] for row in teams if row['number']==1)
        audit=self.get_data('audit',action='team.save')['entries']
        batch=[row for row in audit if row['metadata'].get('batch')==preview['previewId']]
        self.assertEqual({row['target'] for row in batch},{row['id'] for row in teams})
        self.assertTrue(all(row['metadata']['before'] is None and row['metadata']['after']['name'].startswith('Audit') for row in batch))
        summary=self.get_data('audit',action='import.commit',target=preview['previewId'])['entries']
        self.assertEqual(len(summary),1)
        self.assertEqual(summary[0]['metadata']['created'],2)
        self.assertEqual(summary[0]['metadata']['kind'],'teams')
        secrets=[]
        for action in ['issue','rotate','revoke']:
            status,result,_=self.admin.post('/api/admin/team-access',{'teamId':team_id,'action':action})
            self.assertEqual(status,200,result)
            if result.get('code'):secrets.append(result['code'])
            entry=self.get_data('audit',action='team-access.'+action,target=team_id)['entries'][0]
            self.assertIn('before',entry['metadata'])
            self.assertIn('after',entry['metadata'])
            self.assertNotIn('code',entry['metadata']['after'])
        judge,invite=self.invitation(name='Audited credential')
        secrets.append(invite['code'])
        created=self.get_data('audit',action='invitation.create',target=invite['id'])['entries'][0]
        self.assertIsNone(created['metadata']['before'])
        self.assertEqual(created['metadata']['after']['id'],invite['id'])
        self.roster('exhibition',[team_id],[invite['id']])
        self.window('exhibition')
        self.assertEqual(self.review(judge,'exhibition',team_id,feedback='MUST_NOT_DUPLICATE_PRIVATE_FEEDBACK')[0],200)
        serialized=json.dumps(self.get_data('audit',limit=100))
        for private in [*secrets,'MUST_NOT_DUPLICATE_PRIVATE_FEEDBACK','code_hash','token_hash']:
            self.assertNotIn(private,serialized)

    def test_stage_exports_keep_unpaired_lists_only_when_the_opposite_side_is_unfiltered(self):
        team_id=self.team(name='UNPAIRED_TEAM',track='AI for Science')
        _,invitation=self.invitation(name='UNPAIRED_JUDGE')
        self.roster('roadshow',[team_id],[])
        self.roster('exhibition',[],[invitation['id']])
        _,teams,_=self.csv_data('teams',stage='roadshow',status='all')
        _,judges,_=self.csv_data('judges',stage='exhibition',status='all')
        self.assertEqual([row['id'] for row in teams],[team_id])
        self.assertEqual([row['id'] for row in judges],[invitation['id']])
        for kind,filters in [
            ('teams',{'stage':'roadshow','status':'missing'}),
            ('teams',{'stage':'roadshow','judgeId':invitation['id']}),
            ('judges',{'stage':'exhibition','status':'missing'}),
            ('judges',{'stage':'exhibition','teamId':team_id})]:
            _,rows,_=self.csv_data(kind,**filters)
            self.assertEqual(rows,[],(kind,filters))


if __name__=='__main__':
    unittest.main(verbosity=2)
