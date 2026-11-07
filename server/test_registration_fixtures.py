"""Historical synthetic registration setup, never an HTTP authentication fallback.

Claims/recycle regressions need preexisting registrations without issuing team
accounts. These helpers build/read those fixtures directly; current authorization
is exercised over HTTP in test_team_profile and test_legacy_registration_access.
"""
import sqlite3

from errors import APIError
from team_registration import code_hash


def fixture_response(operation):
    try:return 200,operation(),{}
    except APIError as error:return error.status,{'error':str(error),'code':error.code},{}
    except sqlite3.IntegrityError:return 409,{'error':'Synthetic fixture already exists'},{}


def registration_fixture_create(test,body):
    def create():
        with test.service.db() as db:
            db.execute('BEGIN IMMEDIATE')
            value=test.service.registration.clean(body['team'])
            test.service.registration.check_duplicates(db,value)
            row=test.service.registration.insert(db,value,body.get('code'))
            return {'team':test.service.registration.private(row)}
    return fixture_response(create)


def registration_fixture_open(test,body):
    with test.service.db() as db:
        row=db.execute('SELECT team_id FROM team_registrations WHERE secret_hash=?',(code_hash(body['code']),)).fetchone()
        if not row:raise AssertionError('Missing historical fixture')
        return 200,{'team':test.service.registration.private(test.service.registration.row(db,row[0]))},{}


def registration_fixture_save(test,body):
    def save():
        with test.service.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT team_id FROM team_registrations WHERE secret_hash=?',(code_hash(body['code']),)).fetchone()
            if not row:raise AssertionError('Missing historical fixture')
            actor=db.execute("SELECT * FROM accounts WHERE role='admin'").fetchone()
            team=test.service.admin_data.team(db,row[0])
            result=test.service.registration.save_profile(db,actor,row[0],{'team':body['team'],
                'expectedRevision':body['expectedRevision'],'expectedTeamRevision':team['revision']})
            return {'team':test.service.registration.private(test.service.registration.row(db,row[0])),**{k:v for k,v in result.items() if k!='team'}}
    return fixture_response(save)
