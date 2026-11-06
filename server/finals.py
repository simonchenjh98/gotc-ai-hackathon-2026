"""Independent eight-seat finals: open seat selection, scores, ballots and publication."""
from contextlib import closing
from decimal import Decimal, InvalidOperation
from fractions import Fraction
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3

from errors import APIError

MAX_REVISION = 9007199254740991
DIMENSIONS = [
    {"id": "creativity", "name": "创意", "max": 20, "award": "最佳创意奖"},
    {"id": "social", "name": "社会关怀", "max": 15, "award": "最具社会关怀奖"},
    {"id": "technical", "name": "技术呈现", "max": 20, "award": "最佳技术呈现奖"},
    {"id": "application", "name": "应用潜力", "max": 20, "award": "最具应用潜力奖"},
    {"id": "crossdisciplinary", "name": "AI 跨界探索", "max": 15, "award": "最佳 AI 跨界探索奖"},
    {"id": "opensource", "name": "开源贡献", "max": 10, "award": "最佳开源贡献奖"},
]
DDL = [
    """CREATE TABLE IF NOT EXISTS finals_config (
       id INTEGER PRIMARY KEY CHECK(id=1), run_id TEXT NOT NULL UNIQUE,
       revision INTEGER NOT NULL DEFAULT 0, open INTEGER NOT NULL DEFAULT 0,
       roster_locked INTEGER NOT NULL DEFAULT 0, judges_created INTEGER NOT NULL DEFAULT 0,
       publication_json TEXT, publication_fingerprint TEXT, published_at INTEGER,
       awards_json TEXT, awards_fingerprint TEXT, eligibility_revision INTEGER NOT NULL DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS finals_teams (
       run_id TEXT NOT NULL REFERENCES finals_config(run_id), team_id TEXT NOT NULL REFERENCES teams(id),
       PRIMARY KEY(run_id,team_id))""",
    """CREATE TABLE IF NOT EXISTS finals_judges (
       run_id TEXT NOT NULL REFERENCES finals_config(run_id), judge_id TEXT NOT NULL REFERENCES accounts(id),
       PRIMARY KEY(run_id,judge_id))""",
    """CREATE TABLE IF NOT EXISTS finals_scores (
       run_id TEXT NOT NULL, judge_id TEXT NOT NULL, team_id TEXT NOT NULL,
       revision INTEGER NOT NULL CHECK(revision>=1), status TEXT NOT NULL CHECK(status IN ('draft','submitted','recused')),
       scores_json TEXT NOT NULL, total_tenths INTEGER, feedback TEXT NOT NULL, updated_at INTEGER NOT NULL,
       PRIMARY KEY(run_id,judge_id,team_id),
       FOREIGN KEY(run_id,judge_id) REFERENCES finals_judges(run_id,judge_id),
       FOREIGN KEY(run_id,team_id) REFERENCES finals_teams(run_id,team_id))""",
    """CREATE TABLE IF NOT EXISTS finals_nominations (
       run_id TEXT NOT NULL, judge_id TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision>=1),
       status TEXT NOT NULL CHECK(status IN ('draft','submitted')), nominations_json TEXT NOT NULL,
       updated_at INTEGER NOT NULL, PRIMARY KEY(run_id,judge_id),
       FOREIGN KEY(run_id,judge_id) REFERENCES finals_judges(run_id,judge_id))""",
    """CREATE TRIGGER IF NOT EXISTS finals_team_status_update AFTER UPDATE OF status ON team_details
       WHEN OLD.status IS NOT NEW.status AND EXISTS(SELECT 1 FROM finals_teams WHERE team_id=NEW.team_id)
       BEGIN UPDATE finals_config SET eligibility_revision=eligibility_revision+1 WHERE id=1; END""",
    """CREATE TRIGGER IF NOT EXISTS finals_team_status_insert AFTER INSERT ON team_details
       WHEN NEW.status!='active' AND EXISTS(SELECT 1 FROM finals_teams WHERE team_id=NEW.team_id)
       BEGIN UPDATE finals_config SET eligibility_revision=eligibility_revision+1 WHERE id=1; END""",
    """CREATE TRIGGER IF NOT EXISTS finals_team_status_delete AFTER DELETE ON team_details
       WHEN OLD.status!='active' AND EXISTS(SELECT 1 FROM finals_teams WHERE team_id=OLD.team_id)
       BEGIN UPDATE finals_config SET eligibility_revision=eligibility_revision+1 WHERE id=1; END""",
    """CREATE TRIGGER IF NOT EXISTS finals_judge_active_update AFTER UPDATE OF active ON accounts
       WHEN OLD.active IS NOT NEW.active AND EXISTS(SELECT 1 FROM finals_judges WHERE judge_id=NEW.id)
       BEGIN UPDATE finals_config SET eligibility_revision=eligibility_revision+1 WHERE id=1; END""",
    """CREATE TRIGGER IF NOT EXISTS finals_team_metadata_update AFTER UPDATE OF number,name,project ON teams
       WHEN (OLD.number IS NOT NEW.number OR OLD.name IS NOT NEW.name OR OLD.project IS NOT NEW.project)
            AND EXISTS(SELECT 1 FROM finals_teams WHERE team_id=NEW.id)
       BEGIN UPDATE finals_config SET eligibility_revision=eligibility_revision+1 WHERE id=1; END""",
    """CREATE TRIGGER IF NOT EXISTS finals_judge_name_update AFTER UPDATE OF name ON accounts
       WHEN OLD.name IS NOT NEW.name AND EXISTS(SELECT 1 FROM finals_judges WHERE judge_id=NEW.id)
       BEGIN UPDATE finals_config SET eligibility_revision=eligibility_revision+1 WHERE id=1; END""",
]


class FinalsService:
    def __init__(self, service, clock, text):
        self.s, self.clock, self.text = service, clock, text

    @staticmethod
    def supports(path):
        return path == '/api/finals' or path.startswith('/api/finals/') or path == '/api/admin/finals' or path.startswith('/api/admin/finals/')

    @staticmethod
    def initialized(db):
        return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='finals_config'").fetchone()
                    and {'awards_json', 'eligibility_revision'} <= {r[1] for r in db.execute('PRAGMA table_info(finals_config)')}
                    and {'finals_team_status_update', 'finals_team_status_insert', 'finals_team_status_delete',
                         'finals_judge_active_update', 'finals_team_metadata_update', 'finals_judge_name_update'}
                        <= {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
                    and db.execute('SELECT 1 FROM finals_config WHERE id=1').fetchone())

    def initialize(self):
        # A migration backup is separate from the other modules' upgrade markers.
        with closing(sqlite3.connect(self.s.db_path, timeout=10)) as source:
            if not self.initialized(source):
                path = Path(self.s.data_dir) / ('before-finals-' + str(self.clock()) + '-' + secrets.token_hex(4) + '.sqlite3')
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(fd)
                with closing(sqlite3.connect(path)) as target:
                    source.backup(target)
        with self.s.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if self.initialized(db):
                return
            db.execute(DDL[0])
            columns = {r[1] for r in db.execute('PRAGMA table_info(finals_config)')}
            for column in ('awards_json', 'awards_fingerprint'):
                if column not in columns:
                    db.execute('ALTER TABLE finals_config ADD COLUMN ' + column + ' TEXT')
            if 'eligibility_revision' not in columns:
                db.execute('ALTER TABLE finals_config ADD COLUMN eligibility_revision INTEGER NOT NULL DEFAULT 0')
            for statement in DDL[1:]:
                db.execute(statement)
            db.execute('INSERT OR IGNORE INTO finals_config(id,run_id) VALUES (1,?)', ('JF' + secrets.token_hex(12),))
            if db.execute('PRAGMA foreign_key_check').fetchall():
                raise RuntimeError('Finals migration failed foreign-key validation')

    def state(self, db):
        return db.execute('SELECT * FROM finals_config WHERE id=1').fetchone()

    def teams(self, db, run_id=None):
        sql = """SELECT t.id,t.number,t.name,t.project,COALESCE(d.status,'active') AS status
                 FROM teams t LEFT JOIN team_details d ON d.team_id=t.id"""
        params = ()
        if run_id is not None:
            sql += ' WHERE t.id IN (SELECT team_id FROM finals_teams WHERE run_id=?)'
            params = (run_id,)
        return [{"id": r['id'], "number": r['number'], "name": r['name'], "project": r['project'],
                 "eligible": r['status'] == 'active'} for r in db.execute(sql + ' ORDER BY t.number,t.id', params)]

    def judges(self, db, run_id=None):
        sql = "SELECT id,name,active FROM accounts WHERE role='judge'"
        params = ()
        if run_id is not None:
            sql += ' AND id IN (SELECT judge_id FROM finals_judges WHERE run_id=?)'
            params = (run_id,)
        return [{"id": r['id'], "name": r['name'], "active": bool(r['active'])}
                for r in db.execute(sql + ' ORDER BY name,id', params)]

    def fingerprint(self, db, state):
        # Names are intentionally part of the published snapshot. Eligibility changes invalidate it.
        value = {'eligibilityRevision': state['eligibility_revision'],
                 'teams': sorted((t['id'], t['eligible']) for t in self.teams(db, state['run_id'])),
                 'judges': sorted((j['id'], j['active']) for j in self.judges(db, state['run_id'])),
                 'scores': [tuple(r) for r in db.execute('SELECT judge_id,team_id,revision FROM finals_scores WHERE run_id=? ORDER BY judge_id,team_id', (state['run_id'],))],
                 'nominations': [tuple(r) for r in db.execute('SELECT judge_id,revision FROM finals_nominations WHERE run_id=? ORDER BY judge_id', (state['run_id'],))]}
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()

    def published(self, db, state):
        return bool(state['publication_json'] and not state['open']
                    and state['publication_fingerprint'] == self.fingerprint(db, state))

    def config(self, db, state, public=False):
        result = {'open': bool(state['open']), 'published': self.published(db, state), 'judgeCount': 8,
                  'publishedAt': state['published_at'] if self.published(db, state) else None, 'awardLimit': 1}
        if not public:
            result.update(runId=state['run_id'], revision=state['revision'], rosterLocked=bool(state['roster_locked']),
                          publicationStale=bool(state['publication_json']) and not self.published(db, state),
                          teamIds=[t['id'] for t in self.teams(db, state['run_id'])],
                          judgeIds=[j['id'] for j in self.judges(db, state['run_id'])])
        return result

    @staticmethod
    def score_view(row):
        return {'teamId': row['team_id'], 'revision': row['revision'], 'status': row['status'],
                'scores': json.loads(row['scores_json']), 'feedback': row['feedback'], 'updatedAt': row['updated_at']}

    @staticmethod
    def nomination_view(row):
        return None if row is None else {'revision': row['revision'], 'status': row['status'],
                                        'nominations': json.loads(row['nominations_json']), 'updatedAt': row['updated_at']}

    @staticmethod
    def fields(body, expected):
        if not isinstance(body, dict) or set(body) != set(expected):
            if isinstance(body, dict) and 'expectedRevision' not in body:
                raise APIError(428, '缺少编辑版本，请刷新并核对后重试', 'revision_required')
            raise APIError(400, '决赛请求字段不正确')

    @staticmethod
    def next_revision(body, current):
        value = body.get('expectedRevision')
        if type(value) is not int or not 0 <= value <= MAX_REVISION:
            raise APIError(400, '编辑版本须为非负安全整数', 'revision_invalid')
        if value != current:
            raise APIError(409, '数据已在其他页面更新，请保留输入并核对最新记录', 'finals_revision_conflict')
        if current == MAX_REVISION:
            raise APIError(409, '编辑版本已达上限', 'revision_exhausted')
        return current + 1

    def require_judge(self, db, actor, state, writing=False):
        if not db.execute('SELECT 1 FROM finals_judges WHERE run_id=? AND judge_id=?', (state['run_id'], actor['id'])).fetchone():
            raise APIError(403, '你尚未获得决赛评审授权')
        if writing and not state['open']:
            raise APIError(409, '决赛评分和提名尚未开放或已关闭', 'finals_closed')

    def validate_roster(self, db, state):
        judges, teams = self.judges(db, state['run_id']), self.teams(db, state['run_id'])
        if len(judges) != 8 or any(not j['active'] for j in judges):
            raise APIError(409, '决赛必须配置8位不同且有效的评委')
        if not teams or not any(t['eligible'] for t in teams):
            raise APIError(409, '请先配置有效的决赛队伍')
        return teams, judges

    def summary(self, db, state):
        teams, judges = self.teams(db, state['run_id']), self.judges(db, state['run_id'])
        active_judges = {j['id'] for j in judges if j['active']}
        eligible = {t['id'] for t in teams if t['eligible']}
        scores = [r for r in db.execute('SELECT * FROM finals_scores WHERE run_id=?', (state['run_id'],)) if r['judge_id'] in active_judges]
        blockers = []
        if len(judges) != 8 or len(active_judges) != 8:
            blockers.append('需要8位有效决赛评委')
        if not eligible:
            blockers.append('尚无有效决赛队伍')
        rows, exact_scores = [], {}
        for team in teams:
            team_scores = [r for r in scores if r['team_id'] == team['id'] and r['status'] == 'submitted'] if team['eligible'] else []
            recused = sum(r['team_id'] == team['id'] and r['status'] == 'recused' for r in scores) if team['eligible'] else 0
            count = len(team_scores)
            complete = team['eligible'] and len(active_judges) == 8 and count + recused == 8 and count > 0
            exact = Fraction(sum(r['total_tenths'] for r in team_scores), 10 * count) if count else None
            dims = [round(sum(json.loads(r['scores_json'])[i] for r in team_scores) / count, 2) if count else None for i in range(6)]
            rows.append({**team, 'score': round(float(exact), 2) if exact is not None else None, 'scoreCount': count,
                         'recusedCount': recused, 'complete': complete, 'rank': None, 'dimensions': dims})
            if exact is not None:
                exact_scores[team['id']] = exact
            if team['eligible'] and not complete:
                blockers.append(f"队伍{team['number']}的评分尚未完成（评分{count}，回避{recused}，需合计8位且至少1份评分）")
        ranked = sorted((r for r in rows if r['id'] in exact_scores), key=lambda r: (-exact_scores[r['id']], r['number']))
        previous, rank = None, 0
        for position, row in enumerate(ranked, 1):
            value = exact_scores[row['id']]
            if value != previous:
                rank = position
            row['rank'], previous = rank, value
        ballots = [r for r in db.execute("SELECT * FROM finals_nominations WHERE run_id=? AND status='submitted'", (state['run_id'],)) if r['judge_id'] in active_judges]
        if len(ballots) != 8:
            blockers.append(f'专项奖提名已提交{len(ballots)}/8位评委（可明确全部弃权）')
        awards, invalid_ballots, allocated = [], set(), set()
        awards_confirmed = bool(state['awards_json'] and not state['open']
                                and state['awards_fingerprint'] == self.fingerprint(db, state))
        confirmed = json.loads(state['awards_json']) if awards_confirmed else {}
        for dim in DIMENSIONS:
            votes = {t: 0 for t in eligible}
            for ballot in ballots:
                target = json.loads(ballot['nominations_json'])[dim['id']]
                if target is not None:
                    if target in votes:
                        votes[target] += 1
                    else:
                        invalid_ballots.add(ballot['judge_id'])
            max_votes = max(votes.values(), default=0)
            ordered = sorted(({'teamId': t['id'], 'count': votes[t['id']]} for t in teams if t['id'] in votes),
                             key=lambda v: (-v['count'], next(t['number'] for t in teams if t['id'] == v['teamId'])))
            available_votes = [v for v in ordered if v['teamId'] not in allocated and v['count'] > 0]
            next_max = max((v['count'] for v in available_votes), default=0)
            suggested = [v['teamId'] for v in available_votes if v['count'] == next_max] if next_max else []
            allocated.update(suggested)
            awards.append({'id': dim['id'], 'name': dim['award'], 'votes': ordered,
                           'rawLeaders': [v['teamId'] for v in ordered if v['count'] == max_votes] if max_votes else [],
                           'winners': confirmed.get(dim['id'], []),
                           'maxVotes': max_votes, 'totalVotes': sum(votes.values()),
                           'suggestedWinners': suggested, 'confirmedWinners': confirmed.get(dim['id'])})
        if invalid_ballots:
            blockers.append('有提名指向已失去资格的队伍，请评委重新确认提名')
        complete = not blockers
        return {'teams': sorted(rows, key=lambda t: (t['rank'] is None, t['rank'] or 0, t['number'])), 'awards': awards,
                'submittedBallots': len(ballots), 'expectedJudges': 8, 'complete': complete,
                'final': complete and not state['open'] and awards_confirmed, 'blockers': blockers,
                'awardsConfirmed': awards_confirmed}

    def admin_view(self, db, state):
        return {'config': self.config(db, state), 'dimensions': DIMENSIONS,
                'teams': self.teams(db, state['run_id']), 'judges': self.judges(db, state['run_id']),
                'availableTeams': self.teams(db), 'availableJudges': self.judges(db), 'summary': self.summary(db, state)}

    def save_roster(self, db, actor, state, body):
        self.fields(body, {'expectedRevision', 'teamIds', 'judgeIds'})
        revision = self.next_revision(body, state['revision'])
        if state['roster_locked']:
            raise APIError(409, '决赛已开放过，名单已锁定，不能覆盖现有评审身份和记录')
        team_ids, judge_ids = body['teamIds'], body['judgeIds']
        for value in (team_ids, judge_ids):
            if not isinstance(value, list) or any(not isinstance(v, str) for v in value) or len(value) != len(set(value)):
                raise APIError(400, '名单必须是无重复的身份列表')
        if not team_ids or len(team_ids) > 999 or len(judge_ids) != 8:
            raise APIError(400, '请选择至少一支队伍和恰好8位不同评委')
        valid_teams = {t['id'] for t in self.teams(db) if t['eligible']}
        valid_judges = {j['id'] for j in self.judges(db) if j['active']}
        if not set(team_ids) <= valid_teams or not set(judge_ids) <= valid_judges:
            raise APIError(400, '名单包含不存在、停用或失去资格的身份')
        db.execute('DELETE FROM finals_teams WHERE run_id=?', (state['run_id'],))
        db.execute('DELETE FROM finals_judges WHERE run_id=?', (state['run_id'],))
        db.executemany('INSERT INTO finals_teams VALUES (?,?)', [(state['run_id'], t) for t in team_ids])
        db.executemany('INSERT INTO finals_judges VALUES (?,?)', [(state['run_id'], j) for j in judge_ids])
        db.execute('UPDATE finals_config SET revision=? WHERE id=1', (revision,))
        self.s.audit(db, actor, 'finals.roster', state['run_id'], {'teamCount': len(team_ids), 'judgeCount': 8})
        return self.admin_view(db, self.state(db))

    def create_judges(self, db, actor, state, body):
        self.fields(body, {'expectedRevision'})
        revision = self.next_revision(body, state['revision'])
        if state['roster_locked'] or state['judges_created'] or self.judges(db, state['run_id']):
            raise APIError(409, '决赛评委已配置，不能重复批量创建')
        judges = []
        for number in range(1, 9):
            judge, _ = self.s.create_account(db, 'judge', f'决赛评委 {number:02}')
            db.execute('INSERT INTO finals_judges VALUES (?,?)', (state['run_id'], judge['id']))
            judges.append({'id': judge['id'], 'name': judge['name'], 'active': True})
        db.execute('UPDATE finals_config SET revision=?,judges_created=1 WHERE id=1', (revision,))
        self.s.audit(db, actor, 'finals.judges-created', state['run_id'], {'count': 8})
        return {'config': self.config(db, self.state(db)), 'judges': judges}

    def save_settings(self, db, actor, state, body):
        self.fields(body, {'expectedRevision', 'open', 'published'})
        revision = self.next_revision(body, state['revision'])
        if type(body['open']) is not bool or type(body['published']) is not bool:
            raise APIError(400, '开放和发布开关必须为布尔值')
        if body['open'] and body['published']:
            raise APIError(400, '请先关闭评分和提名，再发布最终结果')
        snapshot, fingerprint, published_at = None, None, None
        if body['open'] or body['published']:
            self.validate_roster(db, state)
        if body['published']:
            summary = self.summary(db, state)
            if not summary['complete']:
                raise APIError(409, '评审尚未完成：' + '；'.join(summary['blockers']), 'finals_incomplete')
            if not summary['awardsConfirmed']:
                raise APIError(409, '请先关闭决赛并核对、确认六项专项奖名单', 'finals_awards_unconfirmed')
            summary['final'] = True
            snapshot, fingerprint, published_at = json.dumps(summary, ensure_ascii=False), self.fingerprint(db, state), self.clock()
        db.execute('''UPDATE finals_config SET revision=?,open=?,roster_locked=?,publication_json=?,
                      publication_fingerprint=?,published_at=? WHERE id=1''',
                   (revision, int(body['open']), int(state['roster_locked'] or body['open']), snapshot, fingerprint, published_at))
        if body['open']:
            db.execute('UPDATE finals_config SET awards_json=NULL,awards_fingerprint=NULL WHERE id=1')
        self.s.audit(db, actor, 'finals.settings', state['run_id'], {'open': body['open'], 'published': body['published'], 'revision': revision})
        return self.admin_view(db, self.state(db))

    def confirm_awards(self, db, actor, state, body):
        self.fields(body, {'expectedRevision', 'selections'})
        revision = self.next_revision(body, state['revision'])
        if state['open']:
            raise APIError(409, '请先关闭决赛评分和提名，再确认专项奖')
        self.validate_roster(db, state)
        summary = self.summary(db, state)
        if summary['submittedBallots'] != 8:
            raise APIError(409, '须收齐8位评委的正式提名（含明确弃权）后确认专项奖')
        if any('提名指向' in reason for reason in summary['blockers']):
            raise APIError(409, '存在失去资格队伍的提名，请重新开放并由评委核对提名')
        selections = body['selections']
        if not isinstance(selections, dict) or set(selections) != {d['id'] for d in DIMENSIONS}:
            raise APIError(400, '请一次确认全部六个专项奖')
        for award in summary['awards']:
            choice = selections[award['id']]
            if not isinstance(choice, list) or any(not isinstance(t, str) for t in choice) or len(choice) != len(set(choice)):
                raise APIError(400, '获奖名单格式不正确')
            if set(choice) != set(award['suggestedWinners']):
                raise APIError(409, '获奖名单与当前限额顺延结果不一致，请刷新并核对票数', 'finals_awards_conflict')
        # Store server order, never trust a client-provided candidate order.
        confirmed = {a['id']: a['suggestedWinners'] for a in summary['awards']}
        db.execute('''UPDATE finals_config SET revision=?,awards_json=?,awards_fingerprint=?,
                      publication_json=NULL,publication_fingerprint=NULL,published_at=NULL WHERE id=1''',
                   (revision, json.dumps(confirmed), self.fingerprint(db, state)))
        self.s.audit(db, actor, 'finals.awards-confirmed', state['run_id'], {'revision': revision, 'awardLimit': 1})
        return self.admin_view(db, self.state(db))

    def save_score(self, db, actor, state, body):
        self.fields(body, {'runId', 'teamId', 'expectedRevision', 'status', 'scores', 'feedback'})
        self.require_judge(db, actor, state, writing=True)
        if body['runId'] != state['run_id']:
            raise APIError(409, '决赛轮次不匹配，请核对最新页面', 'finals_run_conflict')
        if not isinstance(body['teamId'], str) or body['teamId'] not in {t['id'] for t in self.teams(db, state['run_id']) if t['eligible']}:
            raise APIError(403, '该队伍不在有效决赛名单中')
        before = db.execute('SELECT * FROM finals_scores WHERE run_id=? AND judge_id=? AND team_id=?',
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
            if body['scores'] != []:
                raise APIError(400, '回避评分的分值须为空列表')
        else:
            if not isinstance(body['scores'], list) or len(body['scores']) != 6:
                raise APIError(400, '请填写六项分值')
            total = 0
            for value, dimension in zip(body['scores'], DIMENSIONS):
                if type(value) not in (int, float):
                    raise APIError(400, '分值必须为数字')
                try:
                    score = Decimal(str(value))
                    if not score.is_finite() or not 0 <= score <= dimension['max'] or score * 10 != (score * 10).to_integral_value():
                        raise APIError(400, '分值须在各项上限内，最多一位小数')
                except InvalidOperation:
                    raise APIError(400, '分值不正确')
                values.append(float(score))
                total += int(score * 10)
        now = self.clock()
        db.execute('''INSERT INTO finals_scores VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(run_id,judge_id,team_id)
                      DO UPDATE SET revision=excluded.revision,status=excluded.status,scores_json=excluded.scores_json,
                      total_tenths=excluded.total_tenths,feedback=excluded.feedback,updated_at=excluded.updated_at''',
                   (state['run_id'], actor['id'], body['teamId'], revision, status, json.dumps(values), total, feedback, now))
        self.s.audit(db, actor, 'finals.score.' + status, body['teamId'], {'runId': state['run_id'], 'revision': revision, 'total': total / 10 if total is not None else None})
        return {'score': {'teamId': body['teamId'], 'revision': revision, 'status': status, 'scores': values, 'feedback': feedback, 'updatedAt': now}}

    def save_nominations(self, db, actor, state, body):
        self.fields(body, {'runId', 'expectedRevision', 'status', 'nominations'})
        self.require_judge(db, actor, state, writing=True)
        if body['runId'] != state['run_id']:
            raise APIError(409, '决赛轮次不匹配，请核对最新页面', 'finals_run_conflict')
        before = db.execute('SELECT * FROM finals_nominations WHERE run_id=? AND judge_id=?', (state['run_id'], actor['id'])).fetchone()
        revision = self.next_revision(body, before['revision'] if before else 0)
        status, nominations = body['status'], body['nominations']
        if not isinstance(status, str) or status not in {'draft', 'submitted'}:
            raise APIError(400, '提名状态不正确')
        if before and before['status'] == 'submitted' and status == 'draft':
            raise APIError(409, '已有正式提名，请修改后重新正式提交')
        if not isinstance(nominations, dict) or set(nominations) != {d['id'] for d in DIMENSIONS}:
            raise APIError(400, '六个奖项必须逐项选择队伍或明确弃权')
        eligible = {t['id'] for t in self.teams(db, state['run_id']) if t['eligible']}
        if any(value is not None and (not isinstance(value, str) or value not in eligible) for value in nominations.values()):
            raise APIError(400, '每项提名须为一支有效决赛队伍或弃权')
        now = self.clock()
        db.execute('''INSERT INTO finals_nominations VALUES (?,?,?,?,?,?) ON CONFLICT(run_id,judge_id)
                      DO UPDATE SET revision=excluded.revision,status=excluded.status,nominations_json=excluded.nominations_json,
                      updated_at=excluded.updated_at''', (state['run_id'], actor['id'], revision, status, json.dumps(nominations), now))
        self.s.audit(db, actor, 'finals.nominations.' + status, state['run_id'], {'revision': revision, 'nominatedAwards': sum(v is not None for v in nominations.values())})
        return {'nominations': {'revision': revision, 'status': status, 'nominations': nominations, 'updatedAt': now}}

    def dispatch(self, db, actor, method, path, body, query, expected_login_id, cookie, finals_judge_id=None):
        if query:
            raise APIError(400, '决赛接口不接受查询参数')
        if path.endswith('/'):
            raise APIError(404, '决赛接口不存在')
        state = self.state(db)
        if path == '/api/finals/entry' and method == 'GET':
            judges = [j for j in self.judges(db, state['run_id']) if j['active']]
            teams = self.teams(db, state['run_id'])
            return {'config': {'runId': state['run_id'], 'open': bool(state['open']),
                               'published': self.published(db, state), 'judgeCount': 8,
                               'rosterReady': len(judges) == 8 and any(t['eligible'] for t in teams)},
                    'judges': [{'id': j['id'], 'name': j['name']} for j in judges]}
        if path == '/api/finals/board' and method == 'GET':
            return {'config': self.config(db, state, public=True), 'dimensions': DIMENSIONS,
                    'summary': json.loads(state['publication_json']) if self.published(db, state) else None}
        if path.startswith('/api/admin/finals'):
            self.s.require(actor, 'admin')
            if not expected_login_id:
                raise APIError(428, '缺少页面登录信息，请刷新后核对身份', 'login_required')
            if expected_login_id != self.s.login_id(cookie):
                raise APIError(401, '登录身份已变化，请重新登录后操作')
        elif path in {'/api/finals/judge', '/api/finals/score', '/api/finals/nominations'}:
            # A selected seat identifies attribution only. It does not authenticate a user,
            # and it never grants permissions to the ordinary judge or administrator APIs.
            if not isinstance(finals_judge_id, str) or not finals_judge_id:
                raise APIError(400, '请先选择你的决赛评委姓名或席位', 'finals_seat_required')
            actor = db.execute('''SELECT a.* FROM accounts a JOIN finals_judges j ON j.judge_id=a.id
                                  WHERE j.run_id=? AND a.id=? AND a.role='judge' AND a.active=1''',
                               (state['run_id'], finals_judge_id)).fetchone()
            if actor is None:
                raise APIError(403, '所选决赛评委席位无效或已停用，请重新选择', 'finals_seat_invalid')
        else:
            raise APIError(404, '决赛接口不存在')
        if method == 'GET' and path == '/api/admin/finals':
            return self.admin_view(db, state)
        if method == 'GET' and path == '/api/finals/judge':
            self.require_judge(db, actor, state)
            return {'config': self.config(db, state), 'dimensions': DIMENSIONS, 'teams': self.teams(db, state['run_id']),
                    'myScores': [self.score_view(r) for r in db.execute('SELECT * FROM finals_scores WHERE run_id=? AND judge_id=? ORDER BY team_id', (state['run_id'], actor['id']))],
                    'myNominations': self.nomination_view(db.execute('SELECT * FROM finals_nominations WHERE run_id=? AND judge_id=?', (state['run_id'], actor['id'])).fetchone())}
        routes = {'/api/admin/finals/roster': self.save_roster, '/api/admin/finals/settings': self.save_settings,
                  '/api/admin/finals/awards': self.confirm_awards,
                  '/api/admin/finals/judges': self.create_judges, '/api/finals/score': self.save_score,
                  '/api/finals/nominations': self.save_nominations}
        if method == 'POST' and path in routes:
            return routes[path](db, actor, state, body)
        raise APIError(404, '决赛接口不存在')
