"""Administrator edits of a team's roster. Call mutations under BEGIN IMMEDIATE.

Two storage shapes exist:
- approved teams keep exact identities in claim_people + claim_memberships, and
  team_registrations mirrors their names;
- registration-only teams keep names in team_registrations (only the captain has a school).
The editor writes whichever shape the team already uses and never guesses identities.
"""
import hashlib
import json
import re
import secrets
import sqlite3

from claims import normalize, ensure_roster_capacity
from errors import APIError

PATH='/api/admin/data/team/members'
MAX_MEMBERS=8
FIELDS={'teamId','rosterContext','reason','members'}
MEMBER_FIELDS={'personId','name','school'}


class TeamMembers:
    def __init__(self,service):
        self.s=service

    def mode(self,db,team_id):
        return 'approved' if db.execute('SELECT 1 FROM claim_memberships WHERE team_id=?',(team_id,)).fetchone() else 'registration'

    def context(self,db,team_id):
        lifecycle=self.s.team_lifecycle
        registration=db.execute('SELECT revision FROM team_registrations WHERE team_id=?',(team_id,)).fetchone()
        snapshot={'roster':lifecycle.member_view(db,team_id),
            'memberships':[[r['person_id'],r['request_id'],r['person_revision'],r['captain_id']] for r in lifecycle.memberships(db,team_id)],
            'registration':registration['revision'] if registration else None}
        encoded=json.dumps(snapshot,sort_keys=True,ensure_ascii=False,separators=(',',':'))
        return hashlib.sha256(('team-roster-context-v1\0'+encoded).encode()).hexdigest()

    def view(self,db,team_id):
        """Editor metadata merged into the admin team detail."""
        if self.s.team_lifecycle.is_recycled(db,team_id):
            return {'rosterContext':None,'rosterMode':None}
        return {'rosterContext':self.context(db,team_id),'rosterMode':self.mode(db,team_id)}

    def clean(self,body):
        if not isinstance(body,dict) or set(body)!=FIELDS:raise APIError(400,'成员编辑字段不正确')
        if not isinstance(body['rosterContext'],str) or not re.fullmatch('[0-9a-f]{64}',body['rosterContext']):
            raise APIError(428,'请先读取并核对当前成员资料','roster_context_required')
        reason=self.s.admin_data.text(body['reason'],'修改原因',500,True)
        members=body['members']
        if not isinstance(members,list) or not 1<=len(members)<=MAX_MEMBERS:
            raise APIError(400,f'每队成员须为 1–{MAX_MEMBERS} 人，第一位为队长')
        cleaned=[]
        for item in members:
            if not isinstance(item,dict) or set(item)!=MEMBER_FIELDS:raise APIError(400,'成员字段不正确')
            person=item['personId']
            if person is not None and (not isinstance(person,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',person)):
                raise APIError(400,'成员编号不正确')
            name=self.s.admin_data.text(item['name'],'姓名',100,True)
            school=self.s.admin_data.text(item['school'] if item['school'] is not None else '','学校',200)
            cleaned.append({'personId':person,'name':name,'school':school})
        return reason,cleaned

    def edit(self,db,actor,body):
        self.s.require(actor,'admin')
        reason,members=self.clean(body)
        team_id=self.s.team_exists(db,body.get('teamId'))
        self.s.team_lifecycle.require_available(db,team_id)
        if body['rosterContext']!=self.context(db,team_id):
            raise APIError(409,'成员资料已被更新，本次未保存。请保留输入并重新读取','roster_context_conflict')
        before=self.s.team_lifecycle.member_view(db,team_id)
        mode=self.mode(db,team_id)
        try:
            detail=self.approved(db,actor,team_id,members) if mode=='approved' else self.registration(db,actor,team_id,members)
        except sqlite3.IntegrityError as error:
            raise APIError(409,'成员身份或归队记录冲突，请重新读取后核对','roster_conflict') from error
        after=self.s.team_lifecycle.member_view(db,team_id)
        self.s.touch(db)
        self.s.audit(db,actor,'team.members.edit',team_id,{'mode':mode,'reason':reason,
            'before':before['members'],'after':after['members'],**detail})
        return {'ok':True,'mode':mode,'roster':after,**self.view(db,team_id)}

    def approved(self,db,actor,team_id,members):
        identities=[(normalize(m['name']),normalize(m['school'])) for m in members]
        if any(not m['school'] for m in members):raise APIError(400,'已审核队伍的每位成员都须填写学校')
        if len(set(identities))!=len(identities):raise APIError(400,'成员名单中有同名同校的重复记录')
        current={r['person_id']:r for r in self.s.team_lifecycle.memberships(db,team_id)}
        request=db.execute('SELECT id FROM claim_requests WHERE team_id=?',(team_id,)).fetchone()
        request_id=request['id'] if request else next(iter(current.values()))['request_id']
        resolved=[];edited=[];created=[]
        for member,identity in zip(members,identities):
            match=db.execute('SELECT * FROM claim_people WHERE normalized_name=? AND normalized_school=?',identity).fetchone()
            if member['personId'] is not None:
                if member['personId'] not in current:
                    raise APIError(409,f'{member["name"]} 已不在本队，请重新读取后核对','roster_context_conflict')
                person=db.execute('SELECT * FROM claim_people WHERE id=?',(member['personId'],)).fetchone()
                if match and match['id']!=person['id']:
                    raise APIError(409,f'名单中已有“{member["name"]}（{member["school"]}）”的身份记录，请删除这一行后直接添加该成员','identity_exists')
                if (person['name'],person['school'])!=(member['name'],member['school']):
                    db.execute('''UPDATE claim_people SET name=?,school=?,normalized_name=?,normalized_school=?,
                        needs_review=0,revision=revision+1 WHERE id=?''',(member['name'],member['school'],*identity,person['id']))
                    edited.append({'personId':person['id'],'before':{'name':person['name'],'school':person['school']},
                                   'after':{'name':member['name'],'school':member['school']}})
                resolved.append(person['id'])
                continue
            if match:
                other=db.execute('''SELECT t.name,t.number FROM claim_memberships m JOIN teams t ON t.id=m.team_id
                    WHERE m.person_id=? AND m.team_id!=?''',(match['id'],team_id)).fetchone()
                if other:raise APIError(409,f'{member["name"]}（{member["school"]}）已属于 {other["number"]} 号队「{other["name"]}」，请先从原队移出','member_assigned')
                resolved.append(match['id'])
            else:
                ensure_roster_capacity(db,1)
                person_id='P'+secrets.token_hex(16)
                db.execute('INSERT INTO claim_people(id,name,school,normalized_name,normalized_school) VALUES (?,?,?,?,?)',
                           (person_id,member['name'],member['school'],*identity))
                created.append(person_id);resolved.append(person_id)
        if len(set(resolved))!=len(resolved):raise APIError(400,'同一成员不能在名单中出现两次')
        removed=[person for person in current if person not in resolved]
        for person in removed:db.execute('DELETE FROM claim_memberships WHERE person_id=?',(person,))
        for person in resolved:
            if person in current:db.execute('UPDATE claim_memberships SET request_id=? WHERE person_id=?',(request_id,person))
            else:db.execute('INSERT INTO claim_memberships VALUES (?,?,?)',(person,team_id,request_id))
        db.execute('UPDATE claim_requests SET captain_id=? WHERE id=?',(resolved[0],request_id))
        if not self.s.registration.row(db,team_id):self.insert_registration(db,team_id,members)
        self.s.registration.sync_verified_members(db,actor,team_id,resolved[0],resolved)
        return {'memberIds':resolved,'removed':removed,'created':created,'edited':edited}

    def registration(self,db,actor,team_id,members):
        if not members[0]['school']:raise APIError(400,'请填写队长学校')
        if any(m['school'] for m in members[1:]):
            raise APIError(400,'该队尚无已审核成员，原登记只记录队长学校；请清空其他成员的学校，审核通过后再补充')
        names=[m['name'] for m in members]
        if len({normalize(n) for n in names})!=len(names):raise APIError(400,'成员名单中有重名')
        captain,school=members[0]['name'],members[0]['school']
        reg=self.s.registration
        value={'name':self.s.admin_data.team(db,team_id)['name'],'captain':captain,'school':school,'members':names}
        reg.check_duplicates(db,value,team_id)
        row=reg.row(db,team_id)
        if not row:
            self.insert_registration(db,team_id,members)
        else:
            target=len(names) if 3<=len(names)<=5 else row['target_size']
            phone=row['phone'] if (normalize(row['captain']),normalize(row['school']))==(normalize(captain),normalize(school)) else ''
            flags=reg.flags({'name':row['name'],'members':names,'targetSize':target})
            db.execute('''UPDATE team_registrations SET captain=?,school=?,phone=?,target_size=?,members_json=?,
                flags_json=?,revision=revision+1 WHERE team_id=?''',(captain,school,phone,target,
                json.dumps(names,ensure_ascii=False),json.dumps(flags,ensure_ascii=False),team_id))
        reg.sync_people(db,value)
        return {'members':names}

    def insert_registration(self,db,team_id,members):
        names=[m['name'] for m in members]
        db.execute('''INSERT INTO team_registrations(team_id,captain,school,target_size,members_json,secret_hash,created_at)
            VALUES (?,?,?,?,?,?,?)''',(team_id,members[0]['name'],members[0]['school'],len(names) if 3<=len(names)<=5 else None,
            json.dumps(names,ensure_ascii=False),'disabled:'+secrets.token_hex(32),self.s.admin_data.now()))
        db.execute('INSERT OR IGNORE INTO team_profile_visibility(team_id,directory_visible) VALUES (?,0)',(team_id,))
