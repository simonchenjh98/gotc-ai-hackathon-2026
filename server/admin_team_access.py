"""Administrator-owned team sessions; no student credentials are created or changed."""
import hashlib
import secrets
from http.cookies import SimpleCookie

from errors import APIError


SCHEMA = '''
CREATE TABLE IF NOT EXISTS admin_team_sessions (
 token_hash TEXT PRIMARY KEY REFERENCES sessions(token_hash) ON DELETE CASCADE,
 team_id TEXT NOT NULL REFERENCES teams(id)
);
'''
PATHS = {'/api/admin/team-workbench', '/api/team/workbench/return'}


def operator_for(actor):
    return actor['operator'] if actor is not None and 'operator' in actor.keys() else None


class AdminTeamAccess:
    def __init__(self, service, cookie_name):
        self.s, self.cookie_name = service, cookie_name

    def initialize(self, db):
        db.executescript(SCHEMA)

    def effective_account(self, actor, context=None):
        if actor is None:
            return None
        if context is None:
            return actor
        if actor['role'] != 'admin':
            return None
        operator = {'adminId': actor['id'], 'adminName': actor['name'],
                    'teamId': context['team_id'], 'teamName': context['name']}
        return {**dict(actor), 'role': 'team', 'name': context['name'], 'operator': operator}

    def session_view(self, actor, cookie):
        return {'role': actor['role'] if actor else None, 'name': actor['name'] if actor else '',
                'loginId': self.s.login_id(cookie) if actor else None, 'operator': operator_for(actor)}

    def rotate(self, db, actor, cookie, team_id=None):
        """Called under BEGIN IMMEDIATE, after authentication and identity-marker checks."""
        old_token = SimpleCookie(cookie)[self.cookie_name].value
        old_hash = hashlib.sha256(old_token.encode()).hexdigest()
        current = db.execute('SELECT * FROM sessions WHERE token_hash=? AND account_id=?',
                             (old_hash, actor['id'])).fetchone()
        real_admin = db.execute("SELECT * FROM accounts WHERE id=? AND role='admin' AND active=1", (actor['id'],)).fetchone()
        if current is None or real_admin is None:
            raise APIError(401, '管理员登录已失效，请重新登录')
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        # Delete only this browser login; all old tabs lose its authority together.
        db.execute('DELETE FROM sessions WHERE token_hash=?', (old_hash,))
        db.execute('INSERT INTO sessions VALUES (?,?,?)', (token_hash, real_admin['id'], current['expires_at']))
        if team_id is not None:
            db.execute('INSERT INTO admin_team_sessions VALUES (?,?)', (token_hash, team_id))
        context = db.execute('SELECT id AS team_id,name FROM teams WHERE id=?', (team_id,)).fetchone() if team_id else None
        effective = self.effective_account(real_admin, context)
        action = 'team-workbench.enter' if team_id is not None else 'team-workbench.return'
        audit_actor = effective if team_id is not None else actor
        self.s.audit(db, audit_actor, action, (operator_for(audit_actor) or {}).get('teamId', ''))
        return {'ok': True, **self.session_view(effective, f'{self.cookie_name}={token}')}, token, 'set'

    def dispatch(self, db, actor, method, path, body, cookie, login_id, query):
        if path == '/api/admin/team-workbench':
            self.s.require(actor, 'admin')
        else:
            self.s.require(actor, 'team')
            if not operator_for(actor):
                raise APIError(403, '仅管理员代管会话可以返回队伍选择')
        if not login_id:
            raise APIError(428, '请刷新页面并核对管理员登录身份', 'login_required')
        if login_id != self.s.login_id(cookie):
            raise APIError(401, '登录身份已变化，请重新登录后操作')
        if query:
            raise APIError(400, '队伍代管入口不接受查询参数')
        if path == '/api/admin/team-workbench':
            if method == 'GET':
                teams = [dict(row) for row in db.execute('''SELECT t.id,t.number,t.name FROM teams t
                    LEFT JOIN team_details d ON d.team_id=t.id
                    WHERE COALESCE(d.status,'active')='active' ORDER BY t.number''')]
                return {'teams': teams, **self.session_view(actor, cookie)}, None, None
            if method != 'POST':
                raise APIError(405, '请使用队伍选择入口')
            if set(body) != {'teamId'}:
                raise APIError(400, '请选择一支队伍')
            team_id = self.s.team_exists(db, body['teamId'])
            if db.execute("SELECT 1 FROM team_details WHERE team_id=? AND status!='active'", (team_id,)).fetchone():
                raise APIError(409, '该队伍已退出或被取消资格，不能进入选手工作台', 'team_ineligible')
            return self.rotate(db, actor, cookie, team_id)
        if method != 'POST':
            raise APIError(405, '请使用返回队伍选择操作')
        if body:
            raise APIError(400, '返回队伍选择不接受额外字段')
        return self.rotate(db, actor, cookie)
