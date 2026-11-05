"""Team-only professional details and the public twelve-team disclosure boundary."""
import json
import unittest

from app import DIMENSIONS, EventService
import test_api


class TeamScoreDetailsTests(unittest.TestCase):
    setUp=test_api.APITests.setUp
    tearDown=test_api.APITests.tearDown
    team=test_api.APITests.team
    invitation=test_api.APITests.invitation

    def access(self, team):
        status, access, _=self.admin.post('/api/admin/team-access', {'teamId':team,'action':'issue'})
        self.assertEqual(status,200)
        client=test_api.Client(self.server)
        status, login, _=client.post('/api/login',{'role':'team','code':access['code']})
        self.assertEqual(status,200)
        return client, {'X-GOTC-Login':login['loginId']}

    def lookup(self, access):
        status, body, _=access[0].request('GET','/api/team/score',headers=access[1])
        self.assertEqual(status,200)
        return body

    def save(self, judge, team, values, feedback='', status='submitted', shared=False):
        body={'teamId':team,'scores':values,'feedback':feedback,'status':status}
        if shared:body['feedbackSharing']='team-anonymous-v1'
        response=judge.score(body)
        self.assertEqual(response[0],200,response[1])
        return response[1]['score']

    def test_all_means_and_anonymous_text_share_only_informed_formal_revision(self):
        a,b=self.team(1),self.team(2,'OTHER_TEAM')
        access=self.access(a)
        other=self.access(b)
        self.admin.settings({'scoringOpen':True,'minJudges':2})
        first, invitation=self.invitation(name='SECRET_JUDGE')
        second,_=self.invitation(name='OTHER_SECRET_JUDGE')
        self.save(first,a,[20,19.9,18.3,14.1,13.2,9.7],'<script>\nGood advice',shared=True)
        hidden=self.lookup(access)
        self.assertEqual((hidden['state'],hidden['score'],hidden['dimensions'],hidden['feedback']),('awaiting_scores',None,[],[]))
        self.save(second,a,[0]*6,'OLD_PRIVATE_COMMENT')
        result=self.lookup(access)
        self.assertEqual(result['score'],47.6)
        self.assertEqual([d['score'] for d in result['dimensions']],[10,9.95,9.15,7.05,6.6,4.85])
        self.assertEqual([d['max'] for d in result['dimensions']],[20,20,20,15,15,10])
        self.assertEqual(result['feedback'],['<script>\nGood advice'])
        self.assertEqual(set(result),{'team','state','score','dimensions','feedback'})
        for secret in ('SECRET_JUDGE','updatedAt','judgeId','scores','OLD_PRIVATE_COMMENT'):
            self.assertNotIn(secret,json.dumps(result))
        self.assertEqual(self.lookup(other)['feedback'],[])
        for path,client in [('/api/board',self.public),('/api/audience',self.invitation('audience')[0])]:
            self.assertNotIn('Good advice',json.dumps(client.get(path)[1]))
        self.assertIn('OLD_PRIVATE_COMMENT',self.admin.get('/api/admin/export')[1].decode())
        self.assertEqual(first.get('/api/judge')[1]['myScores'][0]['feedback'],'<script>\nGood advice')
        self.admin.settings({'resultsPublished':False})
        self.assertEqual(self.lookup(access)['feedback'],[])
        self.assertEqual(self.lookup(access)['dimensions'],[])
        self.admin.settings({'resultsPublished':True})
        self.assertEqual(self.lookup(access)['feedback'],result['feedback'])
        self.save(first,a,[0]*6,'OLD_CLIENT_REVISION')
        self.assertEqual(self.lookup(access)['feedback'],[])
        self.save(first,a,[0]*6,'NEW_SHARED_REVISION',shared=True)
        self.assertEqual(self.lookup(access)['feedback'],['NEW_SHARED_REVISION'])
        self.save(first,a,[],status='recused',shared=True)
        hidden=self.lookup(access)
        self.assertEqual((hidden['score'],hidden['dimensions'],hidden['feedback']),(None,[],[]))
        self.save(first,a,[0]*6,'PRIVATE_DRAFT',status='draft',shared=True)
        self.assertEqual(self.lookup(access)['feedback'],[])
        self.save(first,a,[0]*6,'   ',shared=True)
        result=self.lookup(access)
        self.assertEqual((result['score'],result['feedback']),(0,[]))
        self.assertEqual([d['score'] for d in result['dimensions']],[0]*6)

    def test_rollback_writes_and_revision_mismatch_invalidate_sharing(self):
        team=self.team()
        access=self.access(team)
        self.admin.settings({'scoringOpen':True,'minJudges':1})
        judge, invitation=self.invitation()
        self.save(judge,team,[1]*6,'SHARED',shared=True)
        stale=judge.post('/api/score',{'teamId':team,'expectedRevision':0,'scores':[0]*6,
            'feedback':'STALE_PRIVATE','status':'submitted','feedbackSharing':'team-anonymous-v1'})
        self.assertEqual((stale[0],stale[1]['code']),(409,'score_revision_conflict'))
        self.assertEqual(self.lookup(access)['feedback'],['SHARED'])
        with self.service.db() as db:
            db.execute('UPDATE score_write_revisions SET revision=revision+1 WHERE judge_id=? AND team_id=?',(invitation['id'],team))
        self.assertEqual(self.lookup(access)['feedback'],[])
        self.save(judge,team,[1]*6,'SHARED_AGAIN',shared=True)
        with self.service.db() as db:
            db.execute("UPDATE scores SET feedback='ROLLBACK_PRIVATE' WHERE judge_id=? AND team_id=?",(invitation['id'],team))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM score_feedback_visibility').fetchone()[0],0)
        self.assertEqual(self.lookup(access)['feedback'],[])
        self.save(judge,team,[1]*6,'SHARED_THIRD',shared=True)
        with self.service.db() as db:
            row=tuple(db.execute('SELECT * FROM scores').fetchone())
            db.execute('INSERT OR REPLACE INTO scores VALUES (?,?,?,?,?,?,?)',(*row[:4],'REPLACED_PRIVATE',*row[5:]))
        self.assertEqual(self.lookup(access)['feedback'],[])
        self.service=EventService(data_dir=self.service.data_dir,origin=self.service.origin,static_root=self.service.root)
        self.server.service=self.service
        self.assertEqual(self.lookup(access)['feedback'],[])
        self.assertEqual(self.lookup(access)['score'],6)

    def test_multiple_comments_are_text_only_and_sorted_independently_of_judge(self):
        team=self.team()
        access=self.access(team)
        self.admin.settings({'scoringOpen':True,'minJudges':2})
        first,_=self.invitation(name='First identity')
        second,_=self.invitation(name='Second identity')
        self.save(first,team,[0]*6,'Zulu comment',shared=True)
        self.save(second,team,[d['max'] for d in DIMENSIONS],'Alpha comment\nSecond line',shared=True)
        result=self.lookup(access)
        self.assertEqual(result['feedback'],['Alpha comment\nSecond line','Zulu comment'])
        self.assertTrue(all(isinstance(comment,str) for comment in result['feedback']))
        self.assertEqual(result['score'],50)
        self.assertNotIn('identity',json.dumps(result))

    def test_public_twelve_tie_cutoff_private_thirteenth_and_full_rosters(self):
        # Deliberately create in reverse order: tie breaking must use numeric number.
        teams={n:self.team(n,'TEAM_'+str(n)) for n in range(15,0,-1)}
        access=self.access(teams[13])
        self.admin.settings({'scoringOpen':True,'minJudges':1})
        judge,_=self.invitation()
        audience,_=self.invitation('audience')
        for n,team in teams.items():
            if n<15:self.save(judge,team,[10]*6,'PRIVATE_'+str(n),shared=True)
            else:self.save(judge,team,[20,20,20,15,15,10],'DRAFT',status='draft',shared=True)
        board=self.public.get('/api/board')[1]
        self.assertEqual([t['number'] for t in board['teams']],list(range(1,13)))
        self.assertEqual([t['rank'] for t in board['teams']],list(range(1,13)))
        self.assertEqual(board['stats'],{'teamCount':15,'submittedScores':14,'audienceVotes':0})
        self.assertNotIn('TEAM_13',json.dumps(board))
        self.assertNotIn('PRIVATE_',json.dumps(board))
        for reader,path in [(judge,'/api/judge'),(audience,'/api/audience')]:
            roster=reader.get(path)[1]['teams']
            self.assertEqual(len(roster),15)
            for team in roster[12:]:self.assertEqual((team['score'],team['rank']),(None,None))
        private=self.lookup(access)
        self.assertEqual(private['score'],60)
        self.assertEqual(private['feedback'],['PRIVATE_13'])
        self.assertEqual([d['score'] for d in private['dimensions']],[10]*6)
        admin=self.admin.get('/api/admin')[1]['teams']
        self.assertEqual(admin[12]['score'],60)
        self.assertEqual(admin[12]['rank'],1) # Private competition ranking is intact.
        self.admin.settings({'resultsPublished':False})
        self.assertEqual(self.public.get('/api/board')[1]['teams'],[])
        self.assertEqual(self.lookup(access)['dimensions'],[])
        self.admin.settings({'resultsPublished':True,'minJudges':2})
        self.assertEqual(self.public.get('/api/board')[1]['teams'],[])
        self.assertEqual(self.lookup(access)['state'],'awaiting_scores')


if __name__=='__main__':unittest.main()
