"""HTTP regressions for stale score editors and independently versioned settings."""
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import unittest
from unittest.mock import patch

import test_api as fixtures


class WriteConflictTests(unittest.TestCase):
    setUp=fixtures.APITests.setUp
    tearDown=fixtures.APITests.tearDown
    team=fixtures.APITests.team
    invitation=fixtures.APITests.invitation

    def settings(self,changes,revisions=...):
        if revisions is ...:
            current=self.admin.get('/api/admin')[1]['config']['writeRevisions']
            groups={'scoring' if key in {'scoringOpen','resultsPublished','minJudges'} else 'submissions' if key in {'submissionsOpen','submissionDeadline'} else 'voting' for key in changes}
            revisions={key:current[key] for key in groups}
        return self.admin.post('/api/admin/settings',{**changes,'expectedRevisions':revisions})

    def score(self,judge,team,revision,**changes):
        return judge.post('/api/score',{'teamId':team,'expectedRevision':revision,'scores':[11,11,11,8.3,8.3,5.4],'feedback':'baseline','status':'submitted',**changes})

    def snapshot(self):
        with self.service.db() as db:
            return {table:[tuple(row) for row in db.execute('SELECT * FROM '+table+' ORDER BY rowid')] for table in ('scores','settings','audit','score_write_revisions','settings_write_revisions')}

    def test_stale_score_comment_cannot_restore_old_points(self):
        team=self.team();judge,_=self.invitation()
        self.assertEqual(self.settings({'scoringOpen':True})[0],200)
        first=self.score(judge,team,0)
        self.assertEqual(first[0],200,first[1]);self.assertEqual(first[1]['score']['revision'],1)
        baseline=judge.get('/api/judge')[1]['myScores'][0]
        self.assertEqual(baseline['revision'],1)
        maximum=[20,20,20,15,15,10]
        self.assertEqual(self.score(judge,team,1,scores=maximum)[0],200)
        before=self.snapshot()
        stale=self.score(judge,team,baseline['revision'],feedback='late comment')
        self.assertEqual((stale[0],stale[1]['code']),(409,'score_revision_conflict'))
        self.assertEqual(self.snapshot(),before)
        current=judge.get('/api/judge')[1]['myScores'][0]
        self.assertEqual((current['scores'],current['revision']),(maximum,2))
        self.assertEqual(self.score(judge,team,current['revision'],scores=maximum,feedback='reviewed comment')[0],200)

    def test_score_create_race_statuses_and_independent_records(self):
        first=self.team();second=self.team(2);judge,_=self.invitation();other,_=self.invitation(name='other')
        self.settings({'scoringOpen':True})
        self.assertEqual(self.score(judge,first,0,status='draft')[1]['score']['revision'],1)
        self.assertEqual(self.score(judge,first,0)[0],409)
        for revision,status in [(1,'submitted'),(2,'recused'),(3,'draft'),(4,'submitted')]:
            response=self.score(judge,first,revision,status=status)
            self.assertEqual(response[0],200,response[1]);self.assertEqual(response[1]['score']['revision'],revision+1)
        self.assertEqual(self.score(judge,second,0)[0],200)
        self.assertEqual(self.score(other,first,0)[0],200)
        self.assertEqual(self.score(judge,first,5,status='draft')[0],409)
        self.assertEqual(next(row for row in judge.get('/api/judge')[1]['myScores'] if row['teamId']==first)['revision'],5)

    def test_score_revision_is_required_strict_safe_integer(self):
        team=self.team();judge,_=self.invitation();self.settings({'scoringOpen':True})
        body={'teamId':team,'scores':[0]*6,'status':'submitted'}
        before=self.snapshot()
        missing=judge.post('/api/score',body)
        self.assertEqual((missing[0],missing[1]['code']),(428,'revision_required'))
        self.assertEqual(missing[1]['error'],'页面版本过旧，请先复制未保存输入并刷新页面后重试')
        for value in [None,False,True,-1,1.0,'0',9007199254740992]:
            with self.subTest(value=value):self.assertEqual(judge.post('/api/score',{**body,'expectedRevision':value})[0],400)
        self.assertEqual(self.snapshot(),before)

    def test_score_simultaneous_first_writes_have_one_winner(self):
        team=self.team();judge,_=self.invitation();self.settings({'scoringOpen':True})
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses=list(pool.map(lambda _:self.score(judge,team,0),range(2)))
        self.assertEqual(sorted(row[0] for row in responses),[200,409])
        self.assertEqual(judge.get('/api/judge')[1]['myScores'][0]['revision'],1)

    def test_stale_settings_cannot_reopen_closed_window_and_groups_are_independent(self):
        original=self.admin.get('/api/admin')[1]['config']['writeRevisions']
        self.assertEqual(original,{'scoring':0,'submissions':0,'voting':0})
        self.assertEqual(self.settings({'submissionsOpen':True},{'submissions':0})[0],200)
        old=self.admin.get('/api/admin')[1]['config']['writeRevisions']
        self.assertEqual(self.settings({'submissionsOpen':False},{'submissions':old['submissions']})[0],200)
        before=self.snapshot()
        stale=self.settings({'submissionsOpen':True,'submissionDeadline':1890000000000},{'submissions':old['submissions']})
        self.assertEqual((stale[0],stale[1]['code']),(409,'settings_revision_conflict'))
        self.assertEqual(self.snapshot(),before)
        response=self.settings({'votingOpen':True},{'voting':old['voting']})
        self.assertEqual(response[0],200,response[1]);self.assertFalse(response[1]['config']['submissionsOpen'])
        response=self.settings({'minJudges':3},{'scoring':original['scoring']})
        self.assertEqual(response[0],200,response[1]);self.assertEqual(response[1]['config']['writeRevisions'],{'scoring':1,'submissions':2,'voting':1})
        self.assertNotIn('writeRevisions',self.public.get('/api/board')[1]['config'])

    def test_settings_cross_group_failure_is_atomic_and_noop_advances_once(self):
        self.settings({'votingOpen':False},{'voting':0})
        before=self.snapshot()
        response=self.settings({'scoringOpen':True,'votingOpen':True},{'scoring':0,'voting':0})
        self.assertEqual((response[0],response[1]['code']),(409,'settings_revision_conflict'))
        self.assertEqual(self.snapshot(),before)
        response=self.settings({'scoringOpen':False,'minJudges':5},{'scoring':0})
        self.assertEqual(response[0],200,response[1]);self.assertEqual(response[1]['config']['writeRevisions']['scoring'],1)
        before=self.snapshot()
        self.assertEqual(self.settings({'scoringOpen':True,'votingDeadline':-1},{'scoring':1,'voting':1})[0],400)
        self.assertEqual(self.snapshot(),before)

    def test_settings_revisions_are_required_exact_and_strict(self):
        before=self.snapshot()
        missing=self.admin.post('/api/admin/settings',{'submissionsOpen':True})
        self.assertEqual((missing[0],missing[1]['code']),(428,'revision_required'))
        self.assertEqual(missing[1]['error'],'页面版本过旧，请先复制未保存输入并刷新页面后重试')
        for revisions,status in [({},428),({'scoring':0},400),({'submissions':0,'voting':0},400),({'submissions':False},400),({'submissions':-1},400),({'submissions':1.0},400),({'submissions':9007199254740992},400),(None,400)]:
            with self.subTest(revisions=revisions):self.assertEqual(self.settings({'submissionsOpen':True},revisions)[0],status)
        self.assertEqual(self.admin.post('/api/admin/settings',{'expectedRevisions':{}})[0],400)
        self.assertEqual(self.snapshot(),before)

    def test_permissions_and_revocation_still_precede_revision_checks(self):
        team=self.team();judge,invitation=self.invitation();self.settings({'scoringOpen':True})
        self.assertEqual(judge.post('/api/admin/settings',{'scoringOpen':True,'expectedRevisions':{'scoring':0}})[0],403)
        self.assertEqual(self.public.post('/api/score',{'teamId':team})[0],401)
        self.admin.post('/api/admin/revoke',{'role':'judge','id':invitation['id']})
        self.assertEqual(self.score(judge,team,0)[0],401)

    def test_safe_revision_limit_rejects_further_score_and_setting_writes(self):
        team=self.team();judge,invitation=self.invitation();self.settings({'scoringOpen':True})
        self.score(judge,team,0)
        maximum=9007199254740991
        with self.service.db() as db:
            db.execute('UPDATE score_write_revisions SET revision=? WHERE judge_id=? AND team_id=?',(maximum-1,invitation['id'],team))
            db.execute("UPDATE settings_write_revisions SET revision=? WHERE group_id='voting'",(maximum-1,))
        self.assertEqual(self.score(judge,team,maximum-1)[1]['score']['revision'],maximum)
        self.assertEqual(self.settings({'votingOpen':False},{'voting':maximum-1})[1]['config']['writeRevisions']['voting'],maximum)
        before=self.snapshot()
        for response in [self.score(judge,team,maximum),self.settings({'votingOpen':True},{'voting':maximum})]:
            self.assertEqual((response[0],response[1]['code']),(409,'revision_exhausted'))
        self.assertEqual(self.snapshot(),before)

    def test_known_login_identity_prevents_cross_account_reads_and_writes(self):
        team=self.team();judge,_=self.invitation();other,_=self.invitation(name='other')
        self.settings({'scoringOpen':True})
        wrong={'X-GOTC-Login':other.get('/api/session')[1]['loginId']}
        before=self.snapshot()
        for method,path,body in [('GET','/api/judge',None),('POST','/api/score',{'teamId':team,'expectedRevision':0,'status':'submitted','scores':[0]*6})]:
            self.assertEqual(judge.request(method,path,body,headers=wrong)[0],401)
        for method,path,body in [('GET','/api/admin',None),('POST','/api/admin/settings',{'scoringOpen':False,'expectedRevisions':{'scoring':1}})]:
            self.assertEqual(self.admin.request(method,path,body,headers=wrong)[0],401)
        self.assertEqual(self.snapshot(),before)
        right={'X-GOTC-Login':judge.get('/api/session')[1]['loginId']}
        self.assertEqual(judge.request('POST','/api/score',{'teamId':team,'expectedRevision':0,'status':'submitted','scores':[0]*6},headers=right)[0],200)

    def test_admin_read_keeps_setting_values_and_revisions_in_one_snapshot(self):
        original=self.service.config;paused=threading.Event();release=threading.Event();calls=0
        def read_config(db):
            nonlocal calls
            result=original(db);calls+=1
            # /api/admin first reads config for teams(), then for admin_config().
            if calls==2:
                paused.set()
                if not release.wait(5):raise RuntimeError('Synthetic reader was not released')
            return result
        with patch.object(self.service,'config',side_effect=read_config):
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending=pool.submit(self.admin.get,'/api/admin')
                try:
                    self.assertTrue(paused.wait(5))
                    self.assertEqual(self.settings({'submissionsOpen':True},{'submissions':0})[0],200)
                finally:release.set()
                response=pending.result(timeout=5)
        self.assertEqual(response[0],200,response[1])
        self.assertFalse(response[1]['config']['submissionsOpen'])
        self.assertEqual(response[1]['config']['writeRevisions']['submissions'],0)
        current=self.admin.get('/api/admin')[1]['config']
        self.assertTrue(current['submissionsOpen']);self.assertEqual(current['writeRevisions']['submissions'],1)


if __name__=='__main__':unittest.main()
