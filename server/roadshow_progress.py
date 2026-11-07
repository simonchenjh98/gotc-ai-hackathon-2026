"""Roadshow-only progress weighting, with exact integer arithmetic."""
from contextlib import closing
import os
import secrets
import sqlite3

from errors import APIError


VERSION = 'progress-v1'
RULE = {'min': 0, 'max': 10, 'step': 1, 'minMultiplier': 0.5, 'maxMultiplier': 1}
READ_SQL = '''SELECT s.*,p.progress_score,COALESCE(p.progress_note,'') AS progress_note
 FROM roadshow_scores s LEFT JOIN roadshow_score_progress p
 ON p.run_id=s.run_id AND p.judge_id=s.judge_id AND p.team_id=s.team_id AND p.score_revision=s.revision'''


def backup_before_upgrade(service, clock):
    if not service.db_path.exists():
        return
    with closing(sqlite3.connect(service.db_path.as_uri() + '?mode=ro', uri=True, timeout=10)) as source:
        tables = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'roadshow_scores' not in tables or 'roadshow_score_progress' in tables:
            return
        path = service.data_dir / ('before-roadshow-progress-' + str(clock()) + '-' + secrets.token_hex(4) + '.sqlite3')
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
        with closing(sqlite3.connect(path)) as target:
            source.backup(target)


def migrate(db):
    changed = not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='roadshow_score_progress'").fetchone()
    # Keep the original nine-column score table intact for older releases.
    db.execute('''CREATE TABLE IF NOT EXISTS roadshow_score_progress (
      run_id TEXT NOT NULL, judge_id TEXT NOT NULL, team_id TEXT NOT NULL,
      score_revision INTEGER NOT NULL CHECK(score_revision>=1),
      progress_score INTEGER CHECK(progress_score IS NULL OR (typeof(progress_score)='integer' AND progress_score BETWEEN 0 AND 10)),
      progress_note TEXT NOT NULL DEFAULT '', PRIMARY KEY(run_id,judge_id,team_id),
      FOREIGN KEY(run_id,judge_id,team_id) REFERENCES roadshow_scores(run_id,judge_id,team_id) ON DELETE CASCADE)''')
    existing = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    for action in ('INSERT', 'UPDATE', 'DELETE'):
        name = 'roadshow_progress_' + action.lower()
        refs = ('OLD', 'NEW') if action == 'UPDATE' else ('OLD',) if action == 'DELETE' else ('NEW',)
        condition = ' OR '.join(f"{ref}.run_id=(SELECT run_id FROM judging_stages WHERE stage='roadshow')" for ref in refs)
        if name not in existing:
            changed = True
        db.execute(f'''CREATE TRIGGER IF NOT EXISTS {name} AFTER {action} ON roadshow_score_progress
          WHEN {condition} BEGIN UPDATE roadshow_scoring_config SET revision=revision+1 WHERE id=1; END''')
    return changed


def save(db, run_id, judge_id, team_id, revision, value, note):
    db.execute('''INSERT INTO roadshow_score_progress VALUES (?,?,?,?,?,?)
      ON CONFLICT(run_id,judge_id,team_id) DO UPDATE SET score_revision=excluded.score_revision,
      progress_score=excluded.progress_score,progress_note=excluded.progress_note''',
      (run_id, judge_id, team_id, revision, value, note))


def parse(body, status, text):
    value = body.get('progressScore')
    note = text(body.get('progressNote', ''), '进展评分说明', 1000)
    if status == 'recused':
        if value is not None or note:
            raise APIError(400, '回避时进展分须为空，进展说明须清空')
        return None, ''
    if value is None:
        if status == 'submitted':
            raise APIError(400, '正式提交前请填写0至10的整数进展分', 'progress_required')
        return None, note
    if type(value) is not int or not 0 <= value <= 10:
        raise APIError(400, '进展分须为0至10的整数', 'progress_invalid')
    return value, note


def weighted_milli(row):
    progress = row['progress_score']
    raw = row['total_tenths']
    if type(progress) is not int or not 0 <= progress <= 10 or type(raw) is not int:
        return None
    # Raw points are tenths; multiplying by (10+p)/20 produces exact 0.005-point units.
    return raw * (10 + progress) * 5


def display(numerator, denominator):
    """Round nonnegative rational points to two decimals, with exact half-up ties."""
    return ((numerator * 200 + denominator) // (denominator * 2)) / 100


def view(row):
    if row['status'] == 'recused':
        return {'rawTotal': None, 'progressScore': None, 'progressNote': '', 'progressMultiplier': None,
                'weightedTotal': None, 'weightedMilli': None, 'progressPending': False}
    weighted = weighted_milli(row)
    progress = row['progress_score']
    return {'rawTotal': row['total_tenths'] / 10 if row['total_tenths'] is not None else None,
            'progressScore': progress, 'progressNote': row['progress_note'],
            'progressMultiplier': (10 + progress) / 20 if weighted is not None else None,
            'weightedTotal': display(weighted, 1000) if weighted is not None else None,
            'weightedMilli': weighted,
            'progressPending': row['status'] == 'submitted' and weighted is None}
