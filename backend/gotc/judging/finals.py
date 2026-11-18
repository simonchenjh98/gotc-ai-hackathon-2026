"""Independent finals: invitation access, seat selection and publication."""
from contextlib import closing
from decimal import Decimal, InvalidOperation
from fractions import Fraction
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
import re

from backend.gotc.http import request_models as inputs
from backend.gotc.errors import APIError

MAX_REVISION = 9007199254740991
EXPECTED_JUDGE_COUNT = 7
FINALS_ACCESS_TTL_MS = 12 * 3600 * 1000
FINALS_ACCESS_PATHS = {'/api/finals/entry', '/api/finals/judge', '/api/finals/score', '/api/finals/nominations'}
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
    """CREATE TABLE IF NOT EXISTS finals_access_tokens (
       token_hash TEXT PRIMARY KEY, code_tag TEXT NOT NULL, expires_at INTEGER NOT NULL)""",
    """CREATE INDEX IF NOT EXISTS finals_access_expiry ON finals_access_tokens(expires_at)""",
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
                    and db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='finals_access_tokens'").fetchone()
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

    def access_code(self):
        try:
            raw = self.s.credentials.read('finals-access-code.txt')
            code = (raw or b'').decode('ascii').rstrip('\r\n')
        except (OSError, ValueError, RuntimeError):
            raise APIError(503, '决赛邀请入口尚未配置，请联系组织方', 'finals_access_unavailable')
        if not re.fullmatch(r'[0-9]{6,32}', code):
            raise APIError(503, '决赛邀请入口尚未配置，请联系组织方', 'finals_access_unavailable')
        return code

    @staticmethod
    def access_tag(code, token):
        return hmac.new(code.encode('utf-8'), b'gotc-finals-access-v1\0'+token.encode('ascii'), hashlib.sha256).hexdigest()

    def verify_invitation(self, body):
        configured = self.access_code()
        if not inputs.accepts_fields(body, inputs.CodeBody):
            raise APIError(400, '请填写决赛邀请码')
        code = self.text(body['code'], '决赛邀请码', 160, True)
        if not hmac.compare_digest(code.encode('utf-8'), configured.encode('utf-8')):
            raise APIError(401, '决赛邀请码不正确，请核对后重试', 'finals_access_required')
        return configured

    def grant_access(self, db, body):
        configured = self.verify_invitation(body)
        token = secrets.token_urlsafe(32)
        expires = self.clock() + FINALS_ACCESS_TTL_MS
        db.execute('DELETE FROM finals_access_tokens WHERE expires_at<=?', (self.clock(),))
        db.execute('INSERT INTO finals_access_tokens VALUES (?,?,?)',
                   (hashlib.sha256(token.encode('ascii')).hexdigest(), self.access_tag(configured, token), expires))
        return {'accessToken': token, 'expiresAt': expires}

    def require_access(self, db, token):
        code = self.access_code()
        if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', token):
            raise APIError(401, '请先输入决赛邀请码', 'finals_access_required')
        row = db.execute('SELECT code_tag,expires_at FROM finals_access_tokens WHERE token_hash=?',
                         (hashlib.sha256(token.encode('ascii')).hexdigest(),)).fetchone()
        if row is None or row['expires_at'] <= self.clock() or not hmac.compare_digest(row['code_tag'], self.access_tag(code, token)):
            raise APIError(401, '决赛邀请验证已失效，请重新输入邀请码', 'finals_access_required')

    @staticmethod
    def roadshow_source_fingerprint(db):
        stage = db.execute("SELECT run_id FROM judging_stages WHERE stage='roadshow'").fetchone()
        if stage is None:
            raise ValueError('Roadshow stage is unavailable')
        run_id = stage['run_id']
        value = {
            'runId': run_id,
            'scores': [dict(row) for row in db.execute(
                'SELECT * FROM roadshow_scores WHERE run_id=? ORDER BY run_id,judge_id,team_id', (run_id,))],
            'teams': [tuple(row) for row in db.execute('''SELECT t.id,t.number,t.name,t.project,
                COALESCE(d.status,'active'),EXISTS(SELECT 1 FROM team_recycle_events e
                    WHERE e.team_id=t.id AND e.restored_at IS NULL)
                FROM judging_roster r JOIN teams t ON t.id=r.team_id
                LEFT JOIN team_details d ON d.team_id=t.id WHERE r.run_id=? ORDER BY t.id''', (run_id,))],
            'judges': [tuple(row) for row in db.execute('''SELECT g.judge_id,a.role,a.active
                FROM judging_grants g JOIN accounts a ON a.id=g.judge_id
                WHERE g.run_id=? ORDER BY g.judge_id''', (run_id,))],
        }
        return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                         separators=(',', ':')).encode('utf-8')).hexdigest()

    @staticmethod
    def confirmed_roadshow_team_ids(db, strict=False):
        row = db.execute("SELECT value FROM settings WHERE key='finalsConfirmedRoadshowRoster'").fetchone()
        if row is None:
            return None
        try:
            saved = json.loads(row['value'])
            if not isinstance(saved, dict):
                raise ValueError('Invalid confirmed roster')
            ids = saved.get('teamIds')
            if (not isinstance(ids, list) or not 1 <= len(ids) <= 999
                    or any(not isinstance(team_id, str) or not 1 <= len(team_id) <= 80 for team_id in ids)
                    or len(ids) != len(set(ids))):
                raise ValueError('Invalid team identifiers')
            fingerprint, reason = saved.get('sourceFingerprint'), saved.get('reason')
            if (not isinstance(fingerprint, str) or not re.fullmatch(r'[a-f0-9]{64}', fingerprint)
                    or not isinstance(reason, str) or not reason.strip() or len(reason) > 2000):
                raise ValueError('Missing source confirmation')
            final = db.execute('SELECT run_id FROM finals_config WHERE id=1').fetchone()
            stage = db.execute("SELECT run_id,open FROM judging_stages WHERE stage='roadshow'").fetchone()
            if (final is None or stage is None or stage['open']
                    or saved.get('finalsRunId') != final['run_id']
                    or saved.get('roadshowRunId') != stage['run_id']
                    or not hmac.compare_digest(fingerprint, FinalsService.roadshow_source_fingerprint(db))):
                raise ValueError('Source competition state changed')
            eligible = {team[0] for team in db.execute('''SELECT t.id FROM judging_roster r
                JOIN teams t ON t.id=r.team_id LEFT JOIN team_details d ON d.team_id=t.id
                WHERE r.run_id=? AND COALESCE(d.status,'active')='active'
                AND NOT EXISTS(SELECT 1 FROM team_recycle_events e
                    WHERE e.team_id=t.id AND e.restored_at IS NULL)''', (stage['run_id'],))}
            if not set(ids) <= eligible:
                raise ValueError('Confirmed team is unavailable')
            return ids
        except (ValueError, TypeError, KeyError):
            if strict:
                raise APIError(409, '组织方确认的决赛名单已失效，请重新核对并确认入围名单',
                               'finals_confirmed_roster_stale')
            return None

    @staticmethod
    def roadshow_seed_order(db):
        confirmed = FinalsService.confirmed_roadshow_team_ids(db)
        if confirmed is not None:
            return {team_id: index for index, team_id in enumerate(confirmed)}
        row = db.execute('SELECT publication_json FROM roadshow_scoring_config WHERE id=1').fetchone()
        if row is None or not row['publication_json']:
            return {}
        try:
            teams = json.loads(row['publication_json']).get('teams', [])
        except (TypeError, ValueError):
            return {}
        ordered = []
        for team in teams:
            team_id = team.get('id') if isinstance(team, dict) and team.get('valid') is True else None
            if isinstance(team_id, str) and team_id not in ordered:
                ordered.append(team_id)
                if len(ordered) == 12:
                    break
        return {team_id: index for index, team_id in enumerate(ordered)}

    def teams(self, db, run_id=None):
        sql = """SELECT t.id,t.number,t.name,t.project,COALESCE(d.status,'active') AS status
                 FROM teams t LEFT JOIN team_details d ON d.team_id=t.id"""
        params = ()
        if run_id is not None:
            sql += ' WHERE t.id IN (SELECT team_id FROM finals_teams WHERE run_id=?)'
            params = (run_id,)
        result = [{"id": r['id'], "number": r['number'], "name": r['name'], "project": r['project'],
                   "eligible": r['status'] == 'active'} for r in db.execute(sql, params)]
        seeds = self.roadshow_seed_order(db) if run_id is not None else {}
        result.sort(key=lambda team: (seeds.get(team['id'], 1000000), team['number'], team['id']))
        return result

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
        result = {'open': bool(state['open']), 'published': self.published(db, state), 'judgeCount': EXPECTED_JUDGE_COUNT,
                  'publishedAt': state['published_at'] if self.published(db, state) else None, 'awardLimit': 1}
        if not public:
            confirmed = self.confirmed_roadshow_team_ids(db)
            result.update(runId=state['run_id'], revision=state['revision'], rosterLocked=bool(state['roster_locked']),
                          publicationStale=bool(state['publication_json']) and not self.published(db, state),
                          teamIds=[t['id'] for t in self.teams(db, state['run_id'])],
                          judgeIds=[j['id'] for j in self.judges(db, state['run_id'])],
                          confirmedFinalistCount=len(confirmed) if confirmed is not None else None,
                          selectionSource='organizer-confirmed' if confirmed is not None else 'roadshow-top12')
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

    def current_roadshow_finalists(self, db):
        confirmed = self.confirmed_roadshow_team_ids(db, strict=True)
        if confirmed is not None:
            return confirmed
        stage = self.s.judging.state(db, 'roadshow')
        roster = [team for team in self.s.roadshow_scoring.teams(db, stage)
                  if team['eligible'] and not self.s.team_lifecycle.is_recycled(db, team['id'])]
        if len(roster) < 12:
            return None
        if not self.s.roadshow_scoring.published(db, stage):
            raise APIError(409, '路演前12名尚未有效发布，不能开放决赛', 'roadshow_finalists_unavailable')
        seeds = self.roadshow_seed_order(db)
        ordered = [team_id for team_id, _ in sorted(seeds.items(), key=lambda item: item[1])]
        if len(ordered) != 12:
            raise APIError(409, '路演有效成绩不足12队，不能开放决赛', 'roadshow_finalists_incomplete')
        return ordered

    def validate_roster(self, db, state):
        judges, teams = self.judges(db, state['run_id']), self.teams(db, state['run_id'])
        if len(judges) != EXPECTED_JUDGE_COUNT or any(not j['active'] for j in judges):
            raise APIError(409, f'决赛必须配置{EXPECTED_JUDGE_COUNT}位不同且有效的评委')
        if not teams or not any(t['eligible'] for t in teams):
            raise APIError(409, '请先配置有效的决赛队伍')
        expected = self.current_roadshow_finalists(db)
        if expected is not None and {team['id'] for team in teams} != set(expected):
            raise APIError(409, '决赛名单与当前路演前12名不一致，请重新确认路演成绩', 'roadshow_finalists_stale')
        return teams, judges

    def summary(self, db, state):
        teams, judges = self.teams(db, state['run_id']), self.judges(db, state['run_id'])
        active_judges = {j['id'] for j in judges if j['active']}
        eligible = {t['id'] for t in teams if t['eligible']}
        scores = [r for r in db.execute('SELECT * FROM finals_scores WHERE run_id=?', (state['run_id'],)) if r['judge_id'] in active_judges]
        blockers = []
        if len(judges) != EXPECTED_JUDGE_COUNT or len(active_judges) != EXPECTED_JUDGE_COUNT:
            blockers.append(f'需要{EXPECTED_JUDGE_COUNT}位有效决赛评委')
        if not eligible:
            blockers.append('尚无有效决赛队伍')
        rows, exact_scores = [], {}
        for team in teams:
            team_scores = [r for r in scores if r['team_id'] == team['id'] and r['status'] == 'submitted'] if team['eligible'] else []
            recused = sum(r['team_id'] == team['id'] and r['status'] == 'recused' for r in scores) if team['eligible'] else 0
            count = len(team_scores)
            complete = team['eligible'] and len(active_judges) == EXPECTED_JUDGE_COUNT and count + recused == EXPECTED_JUDGE_COUNT and count > 0
            exact = Fraction(sum(r['total_tenths'] for r in team_scores), 10 * count) if count else None
            dims = [round(sum(json.loads(r['scores_json'])[i] for r in team_scores) / count, 2) if count else None for i in range(6)]
            rows.append({**team, 'score': round(float(exact), 2) if exact is not None else None, 'scoreCount': count,
                         'recusedCount': recused, 'complete': complete, 'rank': None, 'dimensions': dims})
            if exact is not None:
                exact_scores[team['id']] = exact
            if team['eligible'] and not complete:
                blockers.append(f"队伍{team['number']}的评分尚未完成（评分{count}，回避{recused}，需合计{EXPECTED_JUDGE_COUNT}位且至少1份评分）")
        ranked = sorted((r for r in rows if r['id'] in exact_scores), key=lambda r: (-exact_scores[r['id']], r['number']))
        previous, rank = None, 0
        for position, row in enumerate(ranked, 1):
            value = exact_scores[row['id']]
            if value != previous:
                rank = position
            row['rank'], previous = rank, value
        ballots = [r for r in db.execute("SELECT * FROM finals_nominations WHERE run_id=? AND status='submitted'", (state['run_id'],)) if r['judge_id'] in active_judges]
        if len(ballots) != EXPECTED_JUDGE_COUNT:
            blockers.append(f'专项奖提名已提交{len(ballots)}/{EXPECTED_JUDGE_COUNT}位评委（可明确全部弃权）')
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
                'submittedBallots': len(ballots), 'expectedJudges': EXPECTED_JUDGE_COUNT, 'complete': complete,
                'final': complete and not state['open'] and awards_confirmed, 'blockers': blockers,
                'awardsConfirmed': awards_confirmed}

    def admin_view(self, db, state):
        return {'config': self.config(db, state), 'dimensions': DIMENSIONS,
                'teams': self.teams(db, state['run_id']), 'judges': self.judges(db, state['run_id']),
                'availableTeams': [team for team in self.teams(db) if not self.s.team_lifecycle.is_recycled(db,team['id'])], 'availableJudges': self.judges(db), 'summary': self.summary(db, state)}

    def sync_roadshow_teams(self, db, actor, ordered_team_ids, source_run_id):
        state = self.state(db)
        confirmed = self.confirmed_roadshow_team_ids(db, strict=True)
        if (not isinstance(ordered_team_ids, list) or any(not isinstance(team_id, str) for team_id in ordered_team_ids)
                or len(ordered_team_ids) != len(set(ordered_team_ids))
                or len(ordered_team_ids) > (999 if confirmed is not None else 12)):
            raise APIError(400, '路演自动入围名单格式不正确')
        if confirmed is not None:
            source = db.execute("SELECT run_id FROM judging_stages WHERE stage='roadshow'").fetchone()
            if source is None or source['run_id'] != source_run_id:
                raise APIError(409, '路演轮次已变化，请重新核对确认名单', 'finals_confirmed_roster_stale')
            # An automatic publication or withdrawal cannot replace the organizer's explicit selection.
            ordered_team_ids = confirmed
        available = {team['id'] for team in self.teams(db) if team['eligible'] and not self.s.team_lifecycle.is_recycled(db, team['id'])}
        if not set(ordered_team_ids) <= available:
            raise APIError(409, '路演前12名包含已失去资格或已回收的队伍')
        if state['revision'] >= MAX_REVISION:
            raise APIError(409, '决赛配置版本已达上限', 'revision_exhausted')
        existing = {row[0] for row in db.execute('SELECT team_id FROM finals_teams WHERE run_id=?', (state['run_id'],))}
        has_records = bool(db.execute('SELECT 1 FROM finals_scores WHERE run_id=? LIMIT 1', (state['run_id'],)).fetchone() or db.execute(
            'SELECT 1 FROM finals_nominations WHERE run_id=? LIMIT 1', (state['run_id'],)).fetchone())
        if state['open'] or state['roster_locked'] or has_records:
            if confirmed is not None and existing == set(confirmed):
                self.s.audit(db, actor, 'finals.roster.confirmed-sync-idempotent', state['run_id'],
                             {'sourceRunId': source_run_id, 'teamCount': len(confirmed)})
                return len(confirmed)
            saved = db.execute('''SELECT publication_run_id,publication_json
                                  FROM roadshow_scoring_config WHERE id=1''').fetchone()
            saved_order = []
            if saved is not None and saved['publication_json']:
                try:
                    saved_teams = json.loads(saved['publication_json']).get('teams', [])
                except (TypeError, ValueError):
                    saved_teams = []
                saved_order = [team.get('id') for team in saved_teams
                               if isinstance(team, dict) and team.get('valid') is True][:12]
            if (ordered_team_ids and existing == set(ordered_team_ids)
                    and saved is not None and saved['publication_run_id'] == source_run_id
                    and saved_order == ordered_team_ids):
                self.s.audit(db, actor, 'finals.roster.roadshow-sync-idempotent', state['run_id'],
                             {'sourceRunId': source_run_id, 'teamCount': len(ordered_team_ids)})
                return len(ordered_team_ids)
            raise APIError(409, '决赛已开放、名单已锁定或已有记录，不能覆盖路演自动入围名单', 'finals_roster_locked')
        if existing != set(ordered_team_ids):
            db.execute('DELETE FROM finals_teams WHERE run_id=?', (state['run_id'],))
            db.executemany('INSERT INTO finals_teams(run_id,team_id) VALUES (?,?)',
                           [(state['run_id'], team_id) for team_id in ordered_team_ids])
            db.execute('''UPDATE finals_config SET revision=?,publication_json=NULL,publication_fingerprint=NULL,
                          published_at=NULL,awards_json=NULL,awards_fingerprint=NULL WHERE id=1''',
                       (state['revision'] + 1,))
        self.s.audit(db, actor, 'finals.roster.roadshow-sync', state['run_id'],
                     {'sourceRunId': source_run_id, 'teamIds': ordered_team_ids, 'teamCount': len(ordered_team_ids)})
        return len(ordered_team_ids)

    def save_roster(self, db, actor, state, body: inputs.RosterSelection):
        self.fields(body, inputs.RosterSelection.__required_keys__)
        revision = self.next_revision(body, state['revision'])
        if state['roster_locked']:
            raise APIError(409, '决赛已开放过，名单已锁定，不能覆盖现有评审身份和记录')
        team_ids, judge_ids = body['teamIds'], body['judgeIds']
        for value in (team_ids, judge_ids):
            if not isinstance(value, list) or any(not isinstance(v, str) for v in value) or len(value) != len(set(value)):
                raise APIError(400, '名单必须是无重复的身份列表')
        if not team_ids or len(team_ids) > 999 or len(judge_ids) != EXPECTED_JUDGE_COUNT:
            raise APIError(400, f'请选择至少一支队伍和恰好{EXPECTED_JUDGE_COUNT}位不同评委')
        for team_id in team_ids:
            self.s.team_lifecycle.require_available(db,team_id)
        valid_teams = {t['id'] for t in self.teams(db) if t['eligible']}
        valid_judges = {j['id'] for j in self.judges(db) if j['active']}
        if not set(team_ids) <= valid_teams or not set(judge_ids) <= valid_judges:
            raise APIError(400, '名单包含不存在、停用或失去资格的身份')
        db.execute('DELETE FROM finals_teams WHERE run_id=?', (state['run_id'],))
        db.execute('DELETE FROM finals_judges WHERE run_id=?', (state['run_id'],))
        db.executemany('INSERT INTO finals_teams VALUES (?,?)', [(state['run_id'], t) for t in team_ids])
        db.executemany('INSERT INTO finals_judges VALUES (?,?)', [(state['run_id'], j) for j in judge_ids])
        db.execute('UPDATE finals_config SET revision=? WHERE id=1', (revision,))
        self.s.audit(db, actor, 'finals.roster', state['run_id'], {'teamCount': len(team_ids), 'judgeCount': EXPECTED_JUDGE_COUNT})
        return self.admin_view(db, self.state(db))

    def create_judges(self, db, actor, state, body):
        self.fields(body, inputs.RevisionBody.__required_keys__)
        revision = self.next_revision(body, state['revision'])
        if state['roster_locked'] or state['judges_created'] or self.judges(db, state['run_id']):
            raise APIError(409, '决赛评委已配置，不能重复批量创建')
        judges = []
        for number in range(1, EXPECTED_JUDGE_COUNT + 1):
            judge, _ = self.s.create_account(db, 'judge', f'决赛评委 {number:02}')
            db.execute('INSERT INTO finals_judges VALUES (?,?)', (state['run_id'], judge['id']))
            judges.append({'id': judge['id'], 'name': judge['name'], 'active': True})
        db.execute('UPDATE finals_config SET revision=?,judges_created=1 WHERE id=1', (revision,))
        self.s.audit(db, actor, 'finals.judges-created', state['run_id'], {'count': EXPECTED_JUDGE_COUNT})
        return {'config': self.config(db, self.state(db)), 'judges': judges}

    def save_settings(self, db, actor, state, body):
        self.fields(body, inputs.FinalsSettings.__required_keys__)
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
            summary={**summary,'teams':[team for team in summary['teams'] if not self.s.team_lifecycle.is_recycled(db,team['id'])]}
            summary['final'] = True
            snapshot, fingerprint, published_at = json.dumps(summary, ensure_ascii=False), self.fingerprint(db, state), self.clock()
        db.execute('''UPDATE finals_config SET revision=?,open=?,roster_locked=?,publication_json=?,
                      publication_fingerprint=?,published_at=? WHERE id=1''',
                   (revision, int(body['open']), int(state['roster_locked'] or body['open']), snapshot, fingerprint, published_at))
        if body['open']:
            db.execute('UPDATE finals_config SET awards_json=NULL,awards_fingerprint=NULL WHERE id=1')
        self.s.audit(db, actor, 'finals.settings', state['run_id'], {'open': body['open'], 'published': body['published'], 'revision': revision})
        return self.admin_view(db, self.state(db))

    def confirm_awards(self, db, actor, state, body: inputs.AwardSelection):
        self.fields(body, inputs.AwardSelection.__required_keys__)
        revision = self.next_revision(body, state['revision'])
        if state['open']:
            raise APIError(409, '请先关闭决赛评分和提名，再确认专项奖')
        self.validate_roster(db, state)
        summary = self.summary(db, state)
        if summary['submittedBallots'] != EXPECTED_JUDGE_COUNT:
            raise APIError(409, f'须收齐{EXPECTED_JUDGE_COUNT}位评委的正式提名（含明确弃权）后确认专项奖')
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

    def save_score(self, db, actor, state, body: inputs.NumericBallot):
        self.fields(body, inputs.NumericBallot.__required_keys__)
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

    def save_nominations(self, db, actor, state, body: inputs.Nominations):
        self.fields(body, inputs.Nominations.__required_keys__)
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

    @staticmethod
    def seat_account(db, state, finals_judge_id):
        if not isinstance(finals_judge_id, str) or not finals_judge_id:
            raise APIError(400, '请先选择你的决赛评委姓名或席位', 'finals_seat_required')
        actor = db.execute('''SELECT a.* FROM accounts a JOIN finals_judges j ON j.judge_id=a.id
                              WHERE j.run_id=? AND a.id=? AND a.role='judge' AND a.active=1''',
                           (state['run_id'], finals_judge_id)).fetchone()
        if actor is None:
            raise APIError(403, '所选决赛评委席位无效或已停用，请重新选择', 'finals_seat_invalid')
        return actor

    def dispatch(self, db, actor, method, path, body, query, expected_login_id, cookie, finals_judge_id=None, access_token=None, ip=''):
        if query:
            raise APIError(400, '决赛接口不接受查询参数')
        if path.endswith('/'):
            raise APIError(404, '决赛接口不存在')
        if path == '/api/finals/login-status' and method == 'GET':
            return self.s.judge_login_schedule.view(db, 'finals')
        if path == '/api/finals/access':
            if method != 'POST':
                raise APIError(404, '决赛接口不存在')
            self.s.judge_login_schedule.require_open(db, 'finals')
            return self.grant_access(db, body)
        if path in FINALS_ACCESS_PATHS:
            self.require_access(db, access_token)
            self.s.judge_login_schedule.require_open(db, 'finals')
        state = self.state(db)
        if path == '/api/finals/entry' and method == 'GET':
            judges = [j for j in self.judges(db, state['run_id']) if j['active']]
            teams = self.teams(db, state['run_id'])
            return {'config': {'runId': state['run_id'], 'open': bool(state['open']),
                               'published': self.published(db, state), 'judgeCount': EXPECTED_JUDGE_COUNT,
                               'rosterReady': len(judges) == EXPECTED_JUDGE_COUNT and any(t['eligible'] for t in teams)},
                    'judges': [{'id': j['id'], 'name': j['name']} for j in judges]}
        if path == '/api/finals/board' and method == 'GET':
            self.s.require(actor, 'admin')
            return {'config': self.config(db, state, public=True), 'dimensions': DIMENSIONS,
                    'summary': json.loads(state['publication_json']) if self.published(db, state) else None}
        if path.startswith('/api/admin/finals'):
            self.s.require(actor, 'admin')
            if not expected_login_id:
                raise APIError(428, '缺少页面登录信息，请刷新后核对身份', 'login_required')
            if expected_login_id != self.s.login_id(cookie):
                raise APIError(401, '登录身份已变化，请重新登录后操作')
        elif path in {'/api/finals/judge', '/api/finals/score', '/api/finals/nominations'}:
            # Invitation access is checked independently before seat selection. The seat
            # identifies attribution only and grants no ordinary judge/admin API access.
            actor = self.seat_account(db, state, finals_judge_id)
        else:
            raise APIError(404, '决赛接口不存在')
        if method == 'GET' and path == '/api/admin/finals':
            return self.admin_view(db, state)
        if method == 'GET' and path == '/api/finals/judge':
            self.require_judge(db, actor, state)
            return {'config': self.config(db, state), 'dimensions': DIMENSIONS,
                    'teams': [team for team in self.teams(db, state['run_id']) if not self.s.team_lifecycle.is_recycled(db,team['id'])],
                    'myScores': [self.score_view(r) for r in db.execute('SELECT * FROM finals_scores WHERE run_id=? AND judge_id=? ORDER BY team_id', (state['run_id'], actor['id'])) if not self.s.team_lifecycle.is_recycled(db,r['team_id'])],
                    'myNominations': self.nomination_view(db.execute('SELECT * FROM finals_nominations WHERE run_id=? AND judge_id=?', (state['run_id'], actor['id'])).fetchone())}
        routes = {'/api/admin/finals/roster': self.save_roster, '/api/admin/finals/settings': self.save_settings,
                  '/api/admin/finals/awards': self.confirm_awards,
                  '/api/admin/finals/judges': self.create_judges, '/api/finals/score': self.save_score,
                  '/api/finals/nominations': self.save_nominations}
        if method == 'POST' and path in routes:
            return routes[path](db, actor, state, body)
        raise APIError(404, '决赛接口不存在')
