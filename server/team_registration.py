"""Own-team profiles via contestant sessions or existing registration-code capability."""
import hashlib
import json
import re
import secrets
from collections import Counter

from claims import normalize, ensure_roster_capacity
from errors import APIError

PUBLIC_PATHS = {'/api/registration','/api/registration/open','/api/registration/save'}
RETIRED_PATHS = {'/api/registration/create'}
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
CREATE TABLE IF NOT EXISTS team_profile_visibility (
 team_id TEXT PRIMARY KEY REFERENCES team_registrations(team_id),
 directory_visible INTEGER NOT NULL CHECK(directory_visible IN (0,1))
);
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
        return {'teams': [{**self.private(r),'recycled':self.s.team_lifecycle.is_recycled(db,r['team_id'])} if admin else self.public(r)
                          for r in sorted(rows, key=lambda r: r['number'])
                          if admin or (r['status'] == 'active' and not self.s.team_lifecycle.is_recycled(db,r['team_id']) and
                              not db.execute('SELECT 1 FROM team_profile_visibility WHERE team_id=? AND directory_visible=0',(r['team_id'],)).fetchone())]}

    def check_duplicates(self, db, value, team_id=None, *, check_members=True):
        if self.s.team_lifecycle.name_collisions(db,value['name'],team_id):
            raise APIError(409, '这个队名已登记，请核对队伍；本队资料请用参赛码登录选手工作台修改')
        if not check_members:
            return
        names = {normalize(n) for n in value['members']}
        for row in db.execute('SELECT team_id,school,members_json FROM team_registrations'):
            if row['team_id'] != team_id and not self.s.team_lifecycle.is_recycled(db,row['team_id']) and normalize(row['school']) == normalize(value['school']) and names.intersection(normalize(n) for n in json.loads(row['members_json'])):
                raise APIError(409, '同校同名队员已在其他队登记，请先联系组织方核对，避免重复组队')

    def sync_people(self, db, value):
        # Registration is not identity verification. Never change verified membership.
        pending = []
        for name in value['members']:
            if db.execute('SELECT 1 FROM claim_people WHERE normalized_name=?', (normalize(name),)).fetchone():
                continue  # Keep the existing registration's school/identity; do not guess aliases.
            school = value['school'] if name == value['captain'] else '待核对'
            pending.append((name, school))
        ensure_roster_capacity(db, len(pending))
        for name, school in pending:
            db.execute('''INSERT OR IGNORE INTO claim_people
                (id,name,school,normalized_name,normalized_school,notes,needs_review)
                VALUES (?,?,?,?,?,?,1)''', ('R'+secrets.token_hex(12), name, school, normalize(name),
                                         normalize(school), '来源：组队登记；队员学校未提供，须核对本人、学校和归属。'))

    def sync_verified_members(self, db, actor, team_id, captain_id, member_ids):
        row = self.row(db, team_id)
        if not row:
            return
        # Read the identities that were actually assigned, never infer people from names.
        people = {person['id']: person for person in db.execute('''SELECT p.id,p.name,p.school
            FROM claim_memberships m JOIN claim_people p ON p.id=m.person_id WHERE m.team_id=?''', (team_id,))}
        if set(member_ids) != set(people) or captain_id not in people:
            raise APIError(409, '已核验成员已变化，请重新核对')
        captain = people[captain_id]
        members = [people[person_id]['name'] for person_id in member_ids]
        same_captain = (normalize(row['captain']), normalize(row['school'])) == (normalize(captain['name']), normalize(captain['school']))
        phone = row['phone'] if same_captain else ''
        target = len(members) if 3 <= len(members) <= 5 else row['target_size']
        flags = self.flags({'name': row['name'], 'members': members, 'targetSize': target})
        db.execute('''UPDATE team_registrations SET captain=?,school=?,phone=?,target_size=?,members_json=?,
            flags_json=?,revision=revision+1 WHERE team_id=?''',
            (captain['name'],captain['school'],phone,target,json.dumps(members,ensure_ascii=False),
             json.dumps(flags,ensure_ascii=False),team_id))
        metadata = {'captainId': captain_id, 'memberIds': member_ids, 'revision': row['revision']+1}
        if not same_captain and row['phone']:
            metadata['previousContact'] = {key: row[key] for key in ('captain','school','phone')}
        self.s.audit(db, actor, 'registration.verified-members', team_id, metadata)

    def insert(self, db, value, secret, source_key=None, raw=None):
        if sum(not self.s.team_lifecycle.is_recycled(db,row[0]) for row in db.execute('SELECT id FROM teams')) >= 200:
            raise APIError(409, '登记队伍已达上限，请联系组织方')
        used = {r[0] for r in db.execute('SELECT number FROM teams')}
        number = next((n for n in range(1, 1000) if n not in used),None)
        if number is None:
            raise APIError(409, '队号 1–999 已用完，请联系组织方')
        display_name = value['name'] or value['captain']+'的队伍（待命名）'
        team = self.s.admin_data.save_team(db, None, {'number': number, 'name': display_name, 'project': value['project'], 'track': ''})
        db.execute('''INSERT INTO team_registrations
            (team_id,source_key,captain,school,phone,target_size,members_json,summary,secret_hash,flags_json,raw_json,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''', (team['id'], source_key, value['captain'], value['school'], value['phone'],
              value['targetSize'], json.dumps(value['members'], ensure_ascii=False), value['summary'], code_hash(secret) if secret else 'disabled:'+secrets.token_hex(32),
              json.dumps(self.flags(value), ensure_ascii=False), json.dumps(raw, ensure_ascii=False) if raw else None, self.clock()))
        self.sync_people(db, value)
        return self.row(db, team['id'])

    def profile(self,db,team_id):
        team=self.s.admin_data.team(db,team_id)
        row=self.row(db,team_id)
        members=list(self.s.team_lifecycle.memberships(db,team_id))
        stored=self.private(row) if row else None
        value=({key:stored[key] for key in FIELDS} if row else
               {'name':team['name'],'project':team['project'],'captain':'','school':'','phone':'',
                'members':[],'targetSize':3,'summary':''})
        incomplete=''
        if members:
            captain_ids={member['captain_id'] for member in members if member['captain_id']}
            captain=next((member for member in members if len(captain_ids)==1 and member['person_id'] in captain_ids),None)
            ordered=([captain]+[member for member in members if member['person_id']!=captain['person_id']]) if captain else members
            if captain and not row:
                declaration=self.s.claims.declaration(db,captain['request_id'])
                declared=[declaration['captain'],*declaration['members']] if declaration else []
                exact={(normalize(member['name']),normalize(member['school'])):member for member in members}
                identities=[(normalize(member['name']),normalize(member['school'])) for member in declared]
                if len(identities)==len(exact) and set(identities)==set(exact):ordered=[exact[identity] for identity in identities]
            value.update(captain=captain['name'] if captain else '',school=captain['school'] if captain else '',
                         members=stored['members'] if row else [member['name'] for member in ordered],targetSize=row['target_size'] if row else len(members))
            if not captain or not captain['school'] or (not row and not 3<=len(members)<=5):
                incomplete='已审核成员的队长或学校资料不完整，请联系组织方核对；此处不会推断身份'
            if row and (row['captain']!=value['captain'] or row['school']!=value['school'] or
                        Counter(stored['members'])!=Counter(member['name'] for member in members) or
                        not stored['members'] or stored['members'][0]!=value['captain']):
                incomplete='登记与已审核成员记录不一致，请联系组织方核对；此处不会改写已审核身份'
        submitted=db.execute('SELECT 1 FROM submissions WHERE team_id=? AND submitted_json IS NOT NULL',(team_id,)).fetchone()
        return {'team':{key:team[key] for key in ('id','number','name','project','track','status')},'profile':value,
                'revision':row['revision'] if row else 0,'teamRevision':team['revision'],
                'roster':self.s.team_lifecycle.member_view(db,team_id),'membersEditable':not bool(members),
                'incomplete':bool(incomplete),'incompleteReason':incomplete,'projectEditable':not bool(submitted),
                'listedPublicly':bool(row and not db.execute('SELECT 1 FROM team_profile_visibility WHERE team_id=? AND directory_visible=0',(team_id,)).fetchone())}

    def legacy_registration(self,db,code):
        if not isinstance(code,str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}',code):
            raise APIError(403,'队伍管理码不正确，请核对原有管理码；参赛码请在选手工作台登录')
        found=db.execute('SELECT team_id FROM team_registrations WHERE secret_hash=?',(code_hash(code),)).fetchone()
        if not found:raise APIError(403,'队伍管理码不正确或已作废，请联系组织方核对')
        self.s.team_lifecycle.require_available(db,found['team_id'])
        row=self.row(db,found['team_id'])
        if row['status']!='active':raise APIError(409,'本队登记已暂停，请联系组织方')
        return row

    def legacy_save(self,db,body):
        if set(body)!={'code','expectedRevision','team'}:raise APIError(400,'登记字段不正确')
        row=self.legacy_registration(db,body['code'])
        team=self.s.admin_data.team(db,row['team_id'])
        self.save_profile(db,None,row['team_id'],{'team':body['team'],'expectedRevision':body['expectedRevision'],
            'expectedTeamRevision':team['revision']},legacy=True)
        return {'ok':True,'team':self.private(self.row(db,row['team_id']))}

    def save_profile(self,db,actor,team_id,body,*,legacy=False):
        if set(body)!={'team','expectedRevision','expectedTeamRevision'}:raise APIError(400,'本队资料字段不正确')
        current=self.profile(db,team_id)
        if type(body['expectedRevision']) is not int or body['expectedRevision']!=current['revision'] or type(body['expectedTeamRevision']) is not int or body['expectedTeamRevision']!=current['teamRevision']:
            raise APIError(409, '队伍资料已变化，本次未保存。请复制当前输入，再重新读取本队资料')
        if current['incomplete']:raise APIError(409,current['incompleteReason'])
        team=current['team'];baseline=current['profile'];row=self.row(db,team_id)
        value = self.clean(body['team'])
        verified=not current['membersEditable']
        if verified:
            confirmed=baseline['members']
            # Keep every approved identity; name-only edits accept the exact full list or ordered tail.
            if value['captain'] != baseline['captain'] or value['school'] != baseline['school'] or body['team']['members'] not in (confirmed,confirmed[1:]):
                raise APIError(409, '本队成员已获组织方批准，成员变化请交由组织方处理')
            if value['targetSize']<len(confirmed):
                raise APIError(400, '目标人数不能少于已批准的完整成员人数')
            if value['targetSize'] != baseline['targetSize']:
                raise APIError(409, '本队人数已由组织方确认，人数变化请交由组织方处理')
            value['members']=confirmed
        self.check_duplicates(db, value, team_id, check_members=not verified)
        # A registration edit must not overwrite an already submitted project title.
        if not current['projectEditable'] and value['project']!=team['project']:
            raise APIError(409, '已正式提交的项目名称请在选手工作台更新')
        self.s.admin_data.save_team(db, actor, {'id':team_id,'name':value['name'],'project':value['project']})
        if not row:
            db.execute('''INSERT INTO team_registrations(team_id,captain,school,phone,target_size,members_json,summary,secret_hash,created_at)
                VALUES (?,?,?,?,?,?,?,?,?)''',(team_id,value['captain'],value['school'],value['phone'],value['targetSize'],
                json.dumps(value['members'],ensure_ascii=False),value['summary'],'disabled:'+secrets.token_hex(32),self.clock()))
            db.execute('INSERT INTO team_profile_visibility(team_id,directory_visible) VALUES (?,0)',(team_id,))
        else:
            db.execute('''UPDATE team_registrations SET captain=?,school=?,phone=?,target_size=?,members_json=?,summary=?,
                flags_json=?,revision=revision+1 WHERE team_id=?''',(value['captain'],value['school'],value['phone'],value['targetSize'],
                json.dumps(value['members'],ensure_ascii=False),value['summary'],json.dumps(self.flags(value),ensure_ascii=False),team_id))
        if not verified:
            self.sync_people(db, value)
        saved=self.profile(db,team_id)
        self.s.audit(db,actor,'registration.save' if legacy else 'team.profile.save',team_id,
                     {'revision':saved['revision'],'teamRevision':saved['teamRevision'],
                      **({'accessMethod':'legacy-registration-code'} if legacy else {})})
        return {'ok':True,**saved}

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
            row = self.insert(db, value, None, source, item['raw'])
            access.append({'id': row['team_id'], 'number': row['number'], 'name': row['name'], 'captain': row['captain']})
            added += 1
        self.s.audit(db, actor, 'registration.import', metadata={'added': added, 'skipped': skipped})
        return {'ok': True, 'added': added, 'skipped': skipped, 'access': access}

    def dispatch(self, method, path, body, cookie, ip, login_id, query):
        admin = path.startswith('/api/admin/registration')
        if query:
            raise APIError(400, '此接口不接受查询参数')
        if not admin and path not in PUBLIC_PATHS|RETIRED_PATHS:
            raise APIError(404, '接口不存在')
        if method=='POST' and path in RETIRED_PATHS:
            raise APIError(410,'队伍管理码功能已停用。请使用原参赛码登录选手工作台；尚无参赛码请前往队伍申请入口','registration_code_retired')
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
                if method == 'POST' and path == '/api/admin/registration/rotate':
                    raise APIError(410,'队伍管理码已停用，不再重发；队伍资料使用原参赛码登录选手工作台管理','registration_code_retired')
            else:
                if method == 'GET' and path == '/api/registration':
                    return self.listing(db)
                if method=='POST' and path=='/api/registration/open':
                    if set(body)!={'code'}:raise APIError(400,'读取登记只接受原队伍管理码')
                    return {'team':self.private(self.legacy_registration(db,body['code']))}
                if method=='POST' and path=='/api/registration/save':
                    return self.legacy_save(db,body)
            raise APIError(404, '接口不存在')
