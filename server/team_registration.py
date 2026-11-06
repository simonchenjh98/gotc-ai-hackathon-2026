"""Student-maintained rosters; never grants submission or model access."""
import hashlib
import json
import re
import secrets

from claims import normalize
from errors import APIError

PUBLIC_PATHS = {'/api/registration', '/api/registration/create', '/api/registration/open', '/api/registration/save'}
PUBLIC_ORIGINS = {'https://etownyouthai.com', 'https://www.etownyouthai.com', 'https://simonchenjh98.github.io'}
FIELDS = {'name', 'captain', 'school', 'phone', 'targetSize', 'members', 'project', 'summary'}
PLACEHOLDERS = {'', '欢迎', '欢迎加入', '待定', '暂无', '无'}
BAD_NAMES = PLACEHOLDERS | {'不知道', '暂时未定', '未定', '暂定'}
SCHEMA = '''
CREATE TABLE IF NOT EXISTS team_registrations (
 team_id TEXT PRIMARY KEY REFERENCES teams(id), source_key TEXT UNIQUE,
 captain TEXT NOT NULL, school TEXT NOT NULL, phone TEXT NOT NULL DEFAULT '',
 target_size INTEGER, members_json TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '',
 secret_hash TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
 flags_json TEXT NOT NULL DEFAULT '[]', raw_json TEXT, created_at INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS registration_secret_hash ON team_registrations(secret_hash);
CREATE TRIGGER IF NOT EXISTS registration_team_changed AFTER UPDATE OF name,project ON teams
WHEN OLD.name != NEW.name OR OLD.project != NEW.project BEGIN
 UPDATE team_registrations SET revision=revision+1 WHERE team_id=NEW.id;
END;
'''


def code_hash(code):
    return hashlib.sha256(('gotc-team-registration-v1\0' + code).encode()).hexdigest()


class TeamRegistration:
    def __init__(self, service, clock, text):
        self.s, self.clock, self.text = service, clock, text

    def initialize(self, db):
        db.executescript(SCHEMA)

    def clean(self, value, imported=False):
        if not isinstance(value, dict) or set(value) != FIELDS:
            raise APIError(400, '登记字段不正确')
        limits = {'name': 80, 'captain': 100, 'school': 200, 'phone': 30, 'project': 140, 'summary': 600}
        result = {key: self.text(value[key], key, limit, key in {'captain', 'school'}) for key, limit in limits.items()}
        if not imported and normalize(result['name']) in BAD_NAMES:
            raise APIError(400, '请给队伍起一个名字，不要填写“无 / 不知道 / 待定”')
        if normalize(result['captain']) in BAD_NAMES:
            raise APIError(400, '请填写队长真实姓名')
        target = value['targetSize']
        if (target is not None or not imported) and (type(target) is not int or not 3 <= target <= 5):
            raise APIError(400, '队伍目标人数请选择 3–5 人')
        result['targetSize'] = target
        if not isinstance(value['members'], list) or len(value['members']) > 5:
            raise APIError(400, '每队最多 5 人，包含队长')
        members, seen = [], set()
        for raw in [result['captain'], *value['members']]:
            name = self.text(raw, '队员姓名', 100)
            key = normalize(name)
            if key in PLACEHOLDERS:
                continue
            if key not in seen:
                members.append(name)
                seen.add(key)
        if len(members) > 5:
            raise APIError(400, '每队最多 5 人，包含队长；请核对名单')
        if not imported and len(members) > target:
            raise APIError(400, '实名人数超过目标人数，请调整人数或名单')
        if not imported and any(normalize(n) in BAD_NAMES for n in members):
            raise APIError(400, '请填写真实姓名；空缺队员直接留空')
        if not imported and result['phone'] and not re.fullmatch(r'1[3-9][0-9]{9}', result['phone']):
            raise APIError(400, '手机号应为 11 位；也可以暂时留空')
        result['members'] = members
        return result

    def flags(self, value):
        flags = []
        if normalize(value['name']) in BAD_NAMES:
            flags.append('队名待补充')
        if len(value['members']) < 3:
            flags.append('队员不足 3 人')
        if value['targetSize'] is None:
            flags.append('目标人数待确认')
        elif len(value['members']) != value['targetSize']:
            flags.append('人数待核对' if len(value['members']) > value['targetSize'] else '队员待补充')
        # Phone validity is private; do not include it in public flags.
        return flags

    def row(self, db, team_id):
        return db.execute('''SELECT r.*,t.name,t.number,t.project,t.updated_at,
            COALESCE(d.status,'active') AS status FROM team_registrations r
            JOIN teams t ON t.id=r.team_id LEFT JOIN team_details d ON d.team_id=t.id
            WHERE r.team_id=?''', (team_id,)).fetchone()

    def public(self, row):
        members = json.loads(row['members_json'])
        name = '' if row['name'] == row['captain']+'的队伍（待命名）' else row['name']
        flags = self.flags({'name': name, 'members': members, 'targetSize': row['target_size']})
        return {'id': row['team_id'], 'number': row['number'], 'name': row['name'],
                'captain': row['captain'], 'school': row['school'], 'members': members,
                'targetSize': row['target_size'], 'project': row['project'], 'summary': row['summary'],
                'revision': row['revision'], 'flags': flags,
                'recruiting': len(members) < min(row['target_size'] or 5, 5), 'updatedAt': row['updated_at']}

    def private(self, row):
        return {**self.public(row), 'phone': row['phone'], 'status': row['status'],
                'phoneNeedsReview': bool(row['phone'] and not re.fullmatch(r'1[3-9][0-9]{9}', row['phone'])),
                'sourceKey': row['source_key']}

    def listing(self, db, admin=False):
        rows = [self.row(db, r[0]) for r in db.execute('SELECT team_id FROM team_registrations')]
        return {'teams': [self.private(r) if admin else self.public(r) for r in sorted(rows, key=lambda r: r['number'])
                          if admin or r['status'] == 'active']}

    def check_duplicates(self, db, value, team_id=None):
        for row in db.execute('SELECT id,name FROM teams'):
            if row['id'] != team_id and normalize(row['name']) == normalize(value['name']):
                raise APIError(409, '这个队名已登记，请在名单中查找；如是本队，请用管理码修改')
        names = {normalize(n) for n in value['members']}
        for row in db.execute('SELECT team_id,school,members_json FROM team_registrations'):
            if row['team_id'] != team_id and normalize(row['school']) == normalize(value['school']) and names.intersection(normalize(n) for n in json.loads(row['members_json'])):
                raise APIError(409, '同校同名队员已在其他队登记，请先联系组织方核对，避免重复组队')

    def sync_people(self, db, value):
        # Registration is not identity verification. Never change verified membership.
        for name in value['members']:
            if db.execute('SELECT 1 FROM claim_people WHERE normalized_name=?', (normalize(name),)).fetchone():
                continue  # Keep the existing registration's school/identity; do not guess aliases.
            school = value['school'] if name == value['captain'] else '待现场核实'
            db.execute('''INSERT OR IGNORE INTO claim_people
                (id,name,school,normalized_name,normalized_school,notes,needs_review)
                VALUES (?,?,?,?,?,?,1)''', ('R'+secrets.token_hex(12), name, school, normalize(name),
                                         normalize(school), '来源：组队登记；队员学校未提供，须现场核实本人、学校和归属。'))

    def insert(self, db, value, secret, source_key=None, raw=None):
        if db.execute('SELECT COUNT(*) FROM teams').fetchone()[0] >= 200:
            raise APIError(409, '登记队伍已达上限，请联系组织方')
        used = {r[0] for r in db.execute('SELECT number FROM teams')}
        number = next(n for n in range(1, 1000) if n not in used)
        display_name = value['name'] or value['captain']+'的队伍（待命名）'
        team = self.s.admin_data.save_team(db, None, {'number': number, 'name': display_name, 'project': value['project'], 'track': ''})
        db.execute('''INSERT INTO team_registrations
            (team_id,source_key,captain,school,phone,target_size,members_json,summary,secret_hash,flags_json,raw_json,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''', (team['id'], source_key, value['captain'], value['school'], value['phone'],
              value['targetSize'], json.dumps(value['members'], ensure_ascii=False), value['summary'], code_hash(secret),
              json.dumps(self.flags(value), ensure_ascii=False), json.dumps(raw, ensure_ascii=False) if raw else None, self.clock()))
        self.sync_people(db, value)
        return self.row(db, team['id'])

    def authorize(self, db, code):
        if not isinstance(code, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', code):
            raise APIError(403, '管理码不正确，请向队长或组织方获取')
        row = db.execute('SELECT team_id FROM team_registrations WHERE secret_hash=?', (code_hash(code),)).fetchone()
        if not row:
            raise APIError(403, '管理码不正确，请向队长或组织方获取')
        result = self.row(db, row[0])
        if result['status'] != 'active':
            raise APIError(409, '本队登记已暂停，请联系组织方')
        return result

    def create(self, db, body):
        if set(body) != {'team', 'code'} or not isinstance(body['code'], str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', body['code']):
            raise APIError(400, '请刷新登记页面后重试')
        value = self.clean(body['team'])
        old = db.execute('SELECT team_id FROM team_registrations WHERE secret_hash=?', (code_hash(body['code']),)).fetchone()
        if old:
            return {'ok': True, 'team': self.private(self.authorize(db, body['code'])), 'replayed': True}
        self.check_duplicates(db, value)
        row = self.insert(db, value, body['code'])
        self.s.audit(db, None, 'registration.create', row['team_id'])
        return {'ok': True, 'team': self.private(row), 'replayed': False}

    def save(self, db, body):
        if set(body) != {'team', 'code', 'expectedRevision'}:
            raise APIError(400, '登记字段不正确')
        row = self.authorize(db, body['code'])
        if type(body['expectedRevision']) is not int or body['expectedRevision'] != row['revision']:
            raise APIError(409, '队伍资料已变化，本次未保存。请复制当前输入，再重新读取本队资料')
        value = self.clean(body['team'])
        if db.execute('SELECT 1 FROM claim_memberships WHERE team_id=?', (row['team_id'],)).fetchone():
            confirmed=json.loads(row['members_json'])
            # A name-only registration editor cannot collapse distinct verified people.
            # Accept its ordered tail or a compatible full-list payload, then keep the authoritative roster.
            if value['captain'] != row['captain'] or value['school'] != row['school'] or body['team']['members'] not in (confirmed,confirmed[1:]):
                raise APIError(409, '本队已完成现场身份核验，成员变化请交由组织方处理')
            if value['targetSize']<len(confirmed):
                raise APIError(400, '目标人数不能少于已现场核实的完整成员人数')
            value['members']=confirmed
        self.check_duplicates(db, value, row['team_id'])
        # A registration edit must not overwrite an already submitted project title.
        if db.execute('SELECT 1 FROM submissions WHERE team_id=? AND submitted_json IS NOT NULL', (row['team_id'],)).fetchone() and value['project'] != row['project']:
            raise APIError(409, '已正式提交的项目名称请在选手工作台更新')
        self.s.admin_data.save_team(db, None, {'id': row['team_id'], 'name': value['name'], 'project': value['project']})
        db.execute('''UPDATE team_registrations SET captain=?,school=?,phone=?,target_size=?,members_json=?,summary=?,
            flags_json=?,revision=revision+1 WHERE team_id=?''', (value['captain'],value['school'],value['phone'],value['targetSize'],
              json.dumps(value['members'],ensure_ascii=False),value['summary'],json.dumps(self.flags(value),ensure_ascii=False),row['team_id']))
        self.sync_people(db, value)
        saved = self.row(db, row['team_id'])
        self.s.audit(db, None, 'registration.save', row['team_id'], {'revision': saved['revision']})
        return {'ok': True, 'team': self.private(saved)}

    def import_rows(self, db, actor, rows):
        if not isinstance(rows, list) or not 1 <= len(rows) <= 100:
            raise APIError(400, '一次导入 1–100 队')
        added, skipped, access = 0, 0, []
        for item in rows:
            if not isinstance(item, dict) or set(item) != {'sourceKey', 'team', 'raw'} or not isinstance(item['raw'], dict):
                raise APIError(400, '导入字段不正确')
            source = self.text(item['sourceKey'], '来源编号', 120, True)
            if db.execute('SELECT 1 FROM team_registrations WHERE source_key=?', (source,)).fetchone():
                skipped += 1
                continue
            value = self.clean(item['team'], imported=True)
            secret = secrets.token_urlsafe(32)
            row = self.insert(db, value, secret, source, item['raw'])
            access.append({'id': row['team_id'], 'number': row['number'], 'name': row['name'], 'captain': row['captain'], 'code': secret})
            added += 1
        self.s.audit(db, actor, 'registration.import', metadata={'added': added, 'skipped': skipped})
        return {'ok': True, 'added': added, 'skipped': skipped, 'access': access}

    def dispatch(self, method, path, body, cookie, ip, login_id, query):
        admin = path.startswith('/api/admin/registration')
        if query:
            raise APIError(400, '此接口不接受查询参数')
        if not admin and path not in PUBLIC_PATHS:
            raise APIError(404, '接口不存在')
        if method == 'POST' and not admin:
            self.s.rate_limit(ip, 'registration')
        with self.s.db() as db:
            db.execute('BEGIN IMMEDIATE' if method == 'POST' else 'BEGIN')
            if admin:
                actor = self.s.account(db, cookie)
                self.s.require(actor, 'admin')
                if not login_id:
                    raise APIError(428, '请刷新页面并核对管理身份')
                if login_id != self.s.login_id(cookie):
                    raise APIError(401, '登录身份已变化，请重新登录')
                if method == 'GET' and path == '/api/admin/registration':
                    return self.listing(db, admin=True)
                if method == 'POST' and path == '/api/admin/registration/import' and set(body) == {'rows'}:
                    return self.import_rows(db, actor, body['rows'])
                if method == 'POST' and path == '/api/admin/registration/rotate' and set(body) == {'teamId', 'expectedRevision'}:
                    row = self.row(db, body['teamId']) if isinstance(body['teamId'], str) else None
                    if not row or type(body['expectedRevision']) is not int or row['revision'] != body['expectedRevision']:
                        raise APIError(409, '资料已变化，请刷新后重发')
                    code = secrets.token_urlsafe(32)
                    db.execute('UPDATE team_registrations SET secret_hash=?,revision=revision+1 WHERE team_id=?', (code_hash(code),row['team_id']))
                    self.s.audit(db, actor, 'registration.rotate', row['team_id'])
                    return {'ok': True, 'code': code}
            else:
                if method == 'GET' and path == '/api/registration':
                    return self.listing(db)
                if method == 'POST' and path == '/api/registration/create':
                    return self.create(db, body)
                if method == 'POST' and path == '/api/registration/open' and set(body) == {'code'}:
                    return {'team': self.private(self.authorize(db, body['code']))}
                if method == 'POST' and path == '/api/registration/save':
                    return self.save(db, body)
            raise APIError(404, '接口不存在')
