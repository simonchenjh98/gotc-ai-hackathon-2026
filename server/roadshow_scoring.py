"""Independent numeric roadshow scoring over the existing roadshow run and roster."""
from decimal import Decimal, InvalidOperation
from fractions import Fraction
import json

from errors import APIError
from finals import DIMENSIONS as FINALS_DIMENSIONS, MAX_REVISION

DIMENSIONS = [{key: value for key, value in item.items() if key != 'award'} for item in FINALS_DIMENSIONS]
MINIMUM_SCORES = 3
DDL = [
    """CREATE TABLE IF NOT EXISTS roadshow_scoring_config (
       id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL DEFAULT 0,
       publication_json TEXT, publication_run_id TEXT, publication_revision INTEGER, published_at INTEGER)""",
    """CREATE TABLE IF NOT EXISTS roadshow_scores (
       run_id TEXT NOT NULL, judge_id TEXT NOT NULL, team_id TEXT NOT NULL,
       revision INTEGER NOT NULL CHECK(revision>=1), status TEXT NOT NULL CHECK(status IN ('draft','submitted','recused')),
       scores_json TEXT NOT NULL, total_tenths INTEGER, feedback TEXT NOT NULL, updated_at INTEGER NOT NULL,
       PRIMARY KEY(run_id,judge_id,team_id),
       FOREIGN KEY(run_id,judge_id) REFERENCES judging_grants(run_id,judge_id),
       FOREIGN KEY(run_id,team_id) REFERENCES judging_roster(run_id,team_id))""",
]
# An epoch prevents restore/reopen from resurrecting an old publication.
TRIGGERS = [
    ('stage', 'AFTER UPDATE OF revision,run_id,open,deadline ON judging_stages',
     "NEW.stage='roadshow' AND (OLD.revision IS NOT NEW.revision OR OLD.run_id IS NOT NEW.run_id OR OLD.open IS NOT NEW.open OR OLD.deadline IS NOT NEW.deadline)"),
    ('team_status_update', 'AFTER UPDATE OF status ON team_details',
     "OLD.status IS NOT NEW.status AND EXISTS(SELECT 1 FROM judging_roster r JOIN judging_stages s ON s.run_id=r.run_id WHERE s.stage='roadshow' AND r.team_id=NEW.team_id)"),
    ('team_status_insert', 'AFTER INSERT ON team_details',
     "NEW.status!='active' AND EXISTS(SELECT 1 FROM judging_roster r JOIN judging_stages s ON s.run_id=r.run_id WHERE s.stage='roadshow' AND r.team_id=NEW.team_id)"),
    ('team_status_delete', 'AFTER DELETE ON team_details',
     "OLD.status!='active' AND EXISTS(SELECT 1 FROM judging_roster r JOIN judging_stages s ON s.run_id=r.run_id WHERE s.stage='roadshow' AND r.team_id=OLD.team_id)"),
    ('team_metadata', 'AFTER UPDATE OF number,name,project,track ON teams',
     "(OLD.number IS NOT NEW.number OR OLD.name IS NOT NEW.name OR OLD.project IS NOT NEW.project OR OLD.track IS NOT NEW.track) AND EXISTS(SELECT 1 FROM judging_roster r JOIN judging_stages s ON s.run_id=r.run_id WHERE s.stage='roadshow' AND r.team_id=NEW.id)"),
    ('judge_metadata', 'AFTER UPDATE OF active,name,role ON accounts',
     "(OLD.active IS NOT NEW.active OR OLD.name IS NOT NEW.name OR OLD.role IS NOT NEW.role) AND EXISTS(SELECT 1 FROM judging_grants g JOIN judging_stages s ON s.run_id=g.run_id WHERE s.stage='roadshow' AND g.judge_id=NEW.id)"),
]
for _table in ('judging_roster', 'judging_grants', 'roadshow_scores'):
    for _action in ('INSERT', 'UPDATE', 'DELETE'):
        _refs = ('OLD', 'NEW') if _action == 'UPDATE' else ('OLD',) if _action == 'DELETE' else ('NEW',)
        _condition = ' OR '.join(f"{ref}.run_id=(SELECT run_id FROM judging_stages WHERE stage='roadshow')" for ref in _refs)
        TRIGGERS.append((_table + '_' + _action.lower(), 'AFTER ' + _action + ' ON ' + _table, _condition))


class RoadshowScoring:
    def __init__(self, service, clock, text):
        self.s, self.clock, self.text = service, clock, text

    @staticmethod
    def supports(path):
        return any(path == endpoint or path.startswith(endpoint + '/') for endpoint in
                   ('/api/roadshow/scoring', '/api/roadshow/score', '/api/roadshow/board',
                    '/api/admin/roadshow/scoring', '/api/admin/roadshow/publish', '/api/admin/roadshow/teams'))

    def initialize(self, db):
        for statement in DDL:
            db.execute(statement)
        db.execute('INSERT OR IGNORE INTO roadshow_scoring_config(id) VALUES (1)')
        existing = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
        if any('roadshow_scoring_' + name not in existing for name, _, _ in TRIGGERS):
            db.execute('UPDATE roadshow_scoring_config SET revision=revision+1 WHERE id=1')
        for name, event, condition in TRIGGERS:
            db.execute(f'''CREATE TRIGGER IF NOT EXISTS roadshow_scoring_{name} {event} WHEN {condition}
                           BEGIN UPDATE roadshow_scoring_config SET revision=revision+1 WHERE id=1; END''')

    @staticmethod
    def publication_state(db):
        return db.execute('SELECT * FROM roadshow_scoring_config WHERE id=1').fetchone()

    def published(self, db, state):
        saved = self.publication_state(db)
        matches = bool(not self.s.judging.window(state)['canEdit'] and saved['publication_json']
                       and saved['publication_run_id'] == state['run_id']
                       and saved['publication_revision'] == saved['revision'])
        return matches and json.loads(saved['publication_json']).get('minimumScores') == MINIMUM_SCORES

    def config(self, db, state, public=False):
        saved = self.publication_state(db)
        published = self.published(db, state)
        result = {'runId': state['run_id'], 'published': published, 'minimumScores': MINIMUM_SCORES,
                  'durationMinutes': 90, 'publishedAt': saved['published_at'] if published else None}
        if not public:
            result.update(revision=saved['revision'], stageRevision=state['revision'],
                          publicationStale=bool(saved['publication_json']) and not published,
                          **self.s.judging.window(state))
        return result

    def teams(self, db, state, materials=False):
        rows = db.execute("""SELECT t.id,t.number,t.name,t.project,t.track,COALESCE(d.status,'active') AS status,
                            r.snapshot_json FROM judging_roster r JOIN teams t ON t.id=r.team_id
                            LEFT JOIN team_details d ON d.team_id=t.id WHERE r.run_id=? ORDER BY t.number,t.id""",
                          (state['run_id'],))
        result = []
        for row in rows:
            team = {key: row[key] for key in ('id', 'number', 'name', 'project', 'track')}
            team['eligible'] = row['status'] == 'active'
            if materials:
                team['materialSnapshot'] = json.loads(row['snapshot_json']).get('materialSnapshot')
            result.append(team)
        return result

    @staticmethod
    def judges(db, state):
        return [{'id': row['id'], 'name': row['name'], 'active': bool(row['active']) and row['role'] == 'judge'}
                for row in db.execute('''SELECT a.id,a.name,a.active,a.role FROM judging_grants g
                  JOIN accounts a ON a.id=g.judge_id WHERE g.run_id=? ORDER BY a.name,a.id''', (state['run_id'],))]

    @staticmethod
    def score_view(row):
        return {'teamId': row['team_id'], 'revision': row['revision'], 'status': row['status'],
                'scores': json.loads(row['scores_json']), 'feedback': row['feedback'], 'updatedAt': row['updated_at']}

    @staticmethod
    def fields(body, expected):
        if not isinstance(body, dict) or set(body) != set(expected):
            if isinstance(body, dict) and 'expectedRevision' not in body:
                raise APIError(428, '缺少编辑版本，请刷新并核对后重试', 'revision_required')
            raise APIError(400, '路演评分请求字段不正确')

    @staticmethod
    def next_revision(body, current):
        value = body.get('expectedRevision')
        if type(value) is not int or not 0 <= value <= MAX_REVISION:
            raise APIError(400, '编辑版本须为非负安全整数', 'revision_invalid')
        if value != current:
            raise APIError(409, '数据已在其他页面更新，请保留输入并核对最新记录', 'roadshow_revision_conflict')
        if current >= MAX_REVISION:
            raise APIError(409, '编辑版本已达上限', 'revision_exhausted')
        return current + 1

    def require_judge(self, db, actor, state):
        self.s.require(actor, 'judge')
        if not db.execute('SELECT 1 FROM judging_grants WHERE run_id=? AND judge_id=?',
                          (state['run_id'], actor['id'])).fetchone():
            raise APIError(403, '你尚未获得本轮路演评审授权')

    def summary(self, db, state):
        teams, judges = self.teams(db, state), self.judges(db, state)
        active_ids = {judge['id'] for judge in judges if judge['active']}
        scores = [row for row in db.execute('SELECT * FROM roadshow_scores WHERE run_id=?', (state['run_id'],))
                  if row['judge_id'] in active_ids]
        rows, exact_scores = [], {}
        for team in teams:
            submitted = [row for row in scores if row['team_id'] == team['id'] and row['status'] == 'submitted'] if team['eligible'] else []
            recused = sum(row['team_id'] == team['id'] and row['status'] == 'recused' for row in scores) if team['eligible'] else 0
            count = len(submitted)
            valid = bool(team['eligible'] and count >= MINIMUM_SCORES)
            exact = Fraction(sum(row['total_tenths'] for row in submitted), 10 * count) if count else None
            dimensions = [round(sum(json.loads(row['scores_json'])[i] for row in submitted) / count, 2) if count else None for i in range(6)]
            rows.append({**team, 'score': round(float(exact), 2) if exact is not None else None,
                         'scoreCount': count, 'recusedCount': recused, 'valid': valid, 'complete': valid,
                         'missing': max(0, MINIMUM_SCORES - count), 'rank': None, 'dimensions': dimensions})
            if valid:
                exact_scores[team['id']] = exact
        ranked = sorted((row for row in rows if row['valid']), key=lambda row: (-exact_scores[row['id']], row['number']))
        previous, rank = None, 0
        for position, row in enumerate(ranked, 1):
            value = exact_scores[row['id']]
            if value != previous:
                rank = position
            row['rank'], previous = rank, value
        qualified = len(ranked)
        can_edit = self.s.judging.window(state)['canEdit']
        blockers = []
        if can_edit:
            blockers.append('路演评分窗口尚未关闭或截止')
        if not qualified:
            blockers.append('尚无队伍获得至少3位不同评委的有效正式评分')
        can_publish = not can_edit and qualified > 0
        return {'teams': sorted(rows, key=lambda row: (not row['valid'], row['rank'] or 0, row['number'])),
                'expectedJudges': len(judges), 'activeJudges': len(active_ids), 'minimumScores': MINIMUM_SCORES,
                'qualifiedCount': qualified, 'complete': bool(rows) and all(row['valid'] for row in rows),
                'canPublish': can_publish, 'final': can_publish, 'blockers': blockers}

    @staticmethod
    def public_summary(summary):
        result = {**summary, 'teams': []}
        for row in summary['teams']:
            public = dict(row)
            if not row['valid']:
                public.update(score=None, rank=None, dimensions=[None] * len(DIMENSIONS))
            result['teams'].append(public)
        return result

    def admin_view(self, db, state):
        return {'config': self.config(db, state), 'dimensions': DIMENSIONS, 'summary': self.summary(db, state),
                'availableTeams': self.s.finals.teams(db),
                'roster': {'teamIds': [team['id'] for team in self.teams(db, state)],
                           'editable': self.s.judging.stage_view(state)['rosterEditable']}}

    def save_teams(self, db, actor, state, body):
        self.fields(body, {'runId', 'expectedRevision', 'teamIds'})
        if not isinstance(body['runId'], str) or body['runId'] != state['run_id']:
            raise APIError(409, '路演轮次已更新，请核对最新页面', 'roadshow_run_conflict')
        self.next_revision(body, state['revision'])
        team_ids = self.s.judging.identifier_list(body['teamIds'], '路演队伍名单')
        eligible = {team['id'] for team in self.s.finals.teams(db) if team['eligible']}
        if not set(team_ids) <= eligible:
            raise APIError(400, '路演名单包含不存在或已失去参赛资格的队伍')
        judge_ids = [row[0] for row in db.execute(
            'SELECT judge_id FROM judging_grants WHERE run_id=? ORDER BY judge_id', (state['run_id'],))]
        self.s.judging.set_roster(db, actor, state, {
            'expectedRevision': body['expectedRevision'], 'teamIds': team_ids, 'judgeIds': judge_ids})
        return {'ok': True, **self.admin_view(db, self.s.judging.state(db, 'roadshow'))}

    def save_score(self, db, actor, state, body):
        self.require_judge(db, actor, state)
        self.fields(body, {'runId', 'teamId', 'expectedRevision', 'status', 'scores', 'feedback'})
        if not isinstance(body['runId'], str) or body['runId'] != state['run_id']:
            raise APIError(409, '路演轮次已更新，请核对最新页面', 'roadshow_run_conflict')
        if not self.s.judging.window(state)['canEdit']:
            raise APIError(409, '路演评分尚未开放、已截止或已关闭', 'roadshow_closed')
        if not isinstance(body['teamId'], str) or body['teamId'] not in {team['id'] for team in self.teams(db, state) if team['eligible']}:
            raise APIError(403, '该队伍不在本轮有效路演名单中')
        before = db.execute('SELECT * FROM roadshow_scores WHERE run_id=? AND judge_id=? AND team_id=?',
                            (state['run_id'], actor['id'], body['teamId'])).fetchone()
        revision = self.next_revision(body, before['revision'] if before else 0)
        status = body['status']
        if not isinstance(status, str) or status not in {'draft', 'submitted', 'recused'}:
            raise APIError(400, '评分状态不正确')
        if before and before['status'] == 'submitted' and status == 'draft':
            raise APIError(409, '已有正式评分，请修改后重新正式提交')
        feedback = self.text(body['feedback'], '评语', 2000)
        values, total = [], None
        if status == 'recused':
            if body['scores'] is not None and body['scores'] != []:
                raise APIError(400, '回避评分的分值须为空列表或空值')
        else:
            if not isinstance(body['scores'], list) or len(body['scores']) != 6:
                raise APIError(400, '请填写全部六个评分维度')
            total = 0
            for raw, dimension in zip(body['scores'], DIMENSIONS):
                if type(raw) not in (int, float):
                    raise APIError(400, '分值须为数字，且最多保留一位小数')
                try:
                    value = Decimal(str(raw))
                    if not value.is_finite() or value < 0 or value > dimension['max'] or value * 10 != (value * 10).to_integral_value():
                        raise ValueError
                except (InvalidOperation, ValueError):
                    raise APIError(400, dimension['name'] + '分值须在0至' + str(dimension['max']) + '之间，最多一位小数')
                values.append(float(value))
                total += int(value * 10)
        db.execute('''INSERT INTO roadshow_scores VALUES (?,?,?,?,?,?,?,?,?)
                      ON CONFLICT(run_id,judge_id,team_id) DO UPDATE SET revision=excluded.revision,status=excluded.status,
                      scores_json=excluded.scores_json,total_tenths=excluded.total_tenths,feedback=excluded.feedback,updated_at=excluded.updated_at''',
                   (state['run_id'], actor['id'], body['teamId'], revision, status, json.dumps(values), total, feedback, self.clock()))
        self.s.audit(db, actor, 'roadshow.score.' + status, body['teamId'], {'runId': state['run_id'], 'revision': revision})
        self.s.touch(db)
        row = db.execute('SELECT * FROM roadshow_scores WHERE run_id=? AND judge_id=? AND team_id=?',
                         (state['run_id'], actor['id'], body['teamId'])).fetchone()
        return {'ok': True, 'score': self.score_view(row)}

    def publish(self, db, actor, state, body):
        self.fields(body, {'runId', 'expectedRevision', 'published'})
        if not isinstance(body['runId'], str) or body['runId'] != state['run_id']:
            raise APIError(409, '路演轮次已更新，请核对最新页面', 'roadshow_run_conflict')
        revision = self.next_revision(body, self.publication_state(db)['revision'])
        if type(body['published']) is not bool:
            raise APIError(400, '发布状态须为布尔值')
        snapshot, published_at = None, None
        if body['published']:
            if self.s.judging.window(state)['canEdit']:
                raise APIError(409, '请等待路演评分截止或先关闭窗口，再发布成绩')
            summary = self.summary(db, state)
            if not summary['qualifiedCount']:
                raise APIError(409, '至少一支队伍须有3位不同评委正式评分后才能发布')
            snapshot, published_at = json.dumps(self.public_summary(summary), ensure_ascii=False), self.clock()
        db.execute('''UPDATE roadshow_scoring_config SET revision=?,publication_json=?,publication_run_id=?,
                      publication_revision=?,published_at=? WHERE id=1''',
                   (revision, snapshot, state['run_id'] if snapshot else None, revision if snapshot else None, published_at))
        self.s.audit(db, actor, 'roadshow.publish', state['run_id'], {'published': body['published'], 'revision': revision})
        self.s.touch(db)
        return {'ok': True, **self.admin_view(db, state)}

    def dispatch(self, method, path, body, cookie, ip, expected_login_id, query=None):
        if query:
            raise APIError(400, '路演评分接口不接受查询参数')
        with self.s.db() as db:
            db.execute('BEGIN IMMEDIATE' if method == 'POST' else 'BEGIN')
            state = self.s.judging.state(db, 'roadshow')
            if method == 'GET' and path == '/api/roadshow/board':
                saved = self.publication_state(db)
                return {'config': self.config(db, state, public=True), 'dimensions': DIMENSIONS,
                        'summary': json.loads(saved['publication_json']) if self.published(db, state) else None}
            if path not in {'/api/roadshow/scoring', '/api/roadshow/score', '/api/admin/roadshow/scoring',
                            '/api/admin/roadshow/publish', '/api/admin/roadshow/teams'}:
                raise APIError(404, '路演评分接口不存在')
            actor = self.s.account(db, cookie)
            self.s.require(actor, 'admin' if path.startswith('/api/admin/') else 'judge')
            if not expected_login_id:
                raise APIError(428, '请刷新页面并核对登录身份', 'login_required')
            if expected_login_id != self.s.login_id(cookie):
                raise APIError(401, '登录身份已变化，请重新进入')
            if method == 'GET' and path == '/api/admin/roadshow/scoring':
                return self.admin_view(db, state)
            if path == '/api/admin/roadshow/teams':
                if method != 'POST':
                    raise APIError(404, '路演队伍保存接口不存在')
                return self.save_teams(db, actor, state, body)
            if method == 'POST' and path == '/api/admin/roadshow/publish':
                return self.publish(db, actor, state, body)
            self.require_judge(db, actor, state)
            if method == 'GET' and path == '/api/roadshow/scoring':
                return {'config': self.config(db, state), 'dimensions': DIMENSIONS, 'teams': self.teams(db, state, materials=True),
                        'myScores': [self.score_view(row) for row in db.execute('SELECT * FROM roadshow_scores WHERE run_id=? AND judge_id=? ORDER BY team_id', (state['run_id'], actor['id']))]}
            if method == 'POST' and path == '/api/roadshow/score':
                return self.save_score(db, actor, state, body)
            raise APIError(404, '路演评分接口不存在')
