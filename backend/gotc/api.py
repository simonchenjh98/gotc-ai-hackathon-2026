"""Event domain controllers and their authorization and SQLite transaction boundaries."""
from __future__ import annotations

from dataclasses import dataclass
from functools import wraps
import hashlib
from http.cookies import CookieError, SimpleCookie
from typing import TYPE_CHECKING

from . import organizer
from .constants import COOKIE_NAME
from .errors import APIError
from .judging import professional
from .teams.admin_team_access import operator_for

if TYPE_CHECKING:
    from .http.routes import Endpoint


@dataclass(frozen=True)
class Command:
    endpoint: Endpoint
    path: str
    body: dict
    cookie: str
    ip: str
    query: dict
    login_id: str | None
    received_at: int | None
    finals_judge: str | None
    finals_access: str | None

    @property
    def method(self):
        return self.endpoint.method


def marker(event, command, *, required=True):
    if required and not command.login_id:
        raise APIError(428, "缺少页面登录信息，请刷新后核对身份", "login_required")
    if command.login_id is not None and command.login_id != event.login_id(command.cookie):
        raise APIError(401, "登录身份已变化，请重新登录后操作")


def preauthorize_write(event, db, command):
    """Verify the declared actor before acquiring SQLite's only writer."""
    endpoint = command.endpoint
    if endpoint.controller is finals:
        if command.query:
            raise APIError(400, "决赛接口不接受查询参数")
        if command.path == "/api/finals/access":
            event.judge_login_schedule.require_open(db, "finals")
            with event.login_attempt(command.ip, "finals", "invitation"):
                event.finals.verify_invitation(command.body)
            return
        if endpoint.actor == "finals-access":
            event.finals.require_access(db, command.finals_access)
            event.judge_login_schedule.require_open(db, "finals")
            event.finals.seat_account(db, event.finals.state(db), command.finals_judge)
            return
    requires_marker = (
        endpoint.controller in {team_workbench, team_lifecycle, team_profile, vote, finals, gateway_keys}
        or command.path == "/api/admin/judging/roadshow/window"
        or (endpoint.controller is judging and endpoint.actor == "judge")
    )
    optional_marker = endpoint.controller is score or command.path == "/api/admin/settings"
    event.write_identity(db, command.cookie, endpoint.actor,
                         command.login_id if requires_marker or optional_marker else None,
                         marker_required=requires_marker)


def transaction(controller):
    """Share write preflight, revocation rechecks and selected consistent read snapshots."""
    @wraps(controller)
    def execute(event, command):
        with event.db() as db:
            if command.method == "POST":
                if command.endpoint.controller is logout:
                    previous = event.account(db, command.cookie)
                    marker(event, command, required=bool(operator_for(previous)))
                    if previous is None:
                        if command.login_id is not None:
                            raise APIError(401, "登录身份已变化，请重新登录后操作")
                        return {"ok": True}, None, None
                else:
                    preauthorize_write(event, db, command)
                db.execute("BEGIN IMMEDIATE")
            elif command.endpoint.snapshot:
                db.execute("BEGIN")
            actor = event.account(db, command.cookie)
            if command.endpoint.actor in {"admin", "team", "audience", "judge"}:
                event.require(actor, command.endpoint.actor)
            return controller(event, command, db, actor)
    return execute


# These domain controllers own their own transactions (some release SQLite
# before performing KDFs or provider I/O).
def login(event, command):
    return event.login(command.body, command.ip)


def submission(event, command):
    return event.dispatch_submission(command.body, command.cookie, command.login_id, command.received_at)


def detect_models(event, command):
    return event.keys.detect_models(command.body, command.cookie, command.login_id), None, None


def repositories(event, command):
    value = event.repositories.dispatch(command.method, command.path, command.body, command.cookie,
                                        command.login_id, command.query)
    return value, None, None


def registration(event, command):
    value = event.registration.dispatch(command.method, command.path, command.body, command.cookie,
                                        command.ip, command.login_id, command.query)
    return value, None, None


def claims(event, command):
    value = event.claims.dispatch(command.method, command.path, command.body, command.cookie,
                                  command.ip, command.login_id, command.query)
    return value, None, None


def roadshow_scoring(event, command):
    value = event.roadshow_scoring.dispatch(command.method, command.path, command.body, command.cookie,
                                            command.ip, command.login_id, command.query)
    return value, None, None


def roadshow_access(event, command):
    value = event.roadshow_access.dispatch(command.method, command.path, command.body, command.cookie,
                                           command.ip, command.login_id, command.query)
    return value if command.path == "/api/roadshow/login" else (value, None, None)


@transaction
def team_profile(event, command, db, actor):
    if operator_for(actor):
        raise APIError(403, "本队资料仅供参赛队伍登录管理；组织方请使用独立后台")
    marker(event, command)
    if command.query:
        raise APIError(400, "本队资料由当前登录队伍确定，不接受查询参数")
    bound = event.bound_submission_team(db, actor)
    if event.admin_data.team(db, bound["id"])["status"] != "active":
        raise APIError(409, "本队资料已暂停，请联系组织方", "team_ineligible")
    if command.method == "GET":
        if command.body:
            raise APIError(400, "读取本队资料不接受请求字段")
        value = event.registration.profile(db, bound["id"])
    else:
        value = event.registration.save_profile(db, actor, bound["id"], command.body)
    return {**value, "loginId": event.login_id(command.cookie)}, None, None


@transaction
def team_lifecycle(event, command, db, actor):
    value = event.team_lifecycle.dispatch(db, actor, command.method, command.path, command.body,
                                          command.query, command.cookie, command.login_id)
    return value, None, None


@transaction
def team_workbench(event, command, db, actor):
    return event.admin_team_access.dispatch(db, actor, command.method, command.path, command.body,
                                            command.cookie, command.login_id, command.query)


@transaction
def finals(event, command, db, actor):
    value = event.finals.dispatch(db, actor, command.method, command.path, command.body, command.query,
                                  command.login_id, command.cookie, command.finals_judge,
                                  command.finals_access, command.ip)
    return value, None, None


@transaction
def team_score(event, command, db, actor):
    if command.query:
        raise APIError(400, "查分由当前登录队伍确定")
    marker(event, command)
    return event.standings.own_team_score(db, actor), None, None


@transaction
def gateway_keys(event, command, db, actor):
    # A download link cannot attach the administrator's page identity marker.
    marker(event, command, required=command.path != "/api/admin/keys/export")
    return event.keys.dispatch(db, actor, command.method, command.path, command.body), None, None


def gateway_overview(event, command):
    # Complete the authenticated dashboard read before contacting the sibling process.
    value, token, action = gateway_keys(event, command)
    event.keys.add_runtime(value)
    return value, token, action


@transaction
def backups(event, command, db, actor):
    marker(event, command, required=bool(operator_for(actor)))
    return event.repository_backups.download(db, actor, command.method, command.path, command.query), None, None


@transaction
def submission_records(event, command, db, actor):
    if command.endpoint.actor == "team":
        marker(event, command, required=bool(operator_for(actor)))
    value = event.submission_records.dispatch(db, actor, command.method, command.path, command.body, command.query)
    return value, None, None


def session_token(cookie):
    parsed = SimpleCookie()
    try:
        parsed.load(cookie)
    except CookieError:
        return ""
    return parsed[COOKIE_NAME].value if COOKIE_NAME in parsed else ""


@transaction
def admin_data(event, command, db, actor):
    token_hash = hashlib.sha256(session_token(command.cookie).encode()).hexdigest()
    value = event.admin_data.dispatch(db, actor, command.method, command.path, command.body, command.query, token_hash)
    return value, None, None


def ordinary_judge(event, db, actor):
    if event.roadshow_access.owns_account(db, actor["id"]):
        raise APIError(403, "此邀请码仅用于路演评审，请从路演入口进入")


@transaction
def judging(event, command, db, actor):
    if command.endpoint.actor == "judge":
        if command.path.startswith("/api/judging/exhibition"):
            ordinary_judge(event, db, actor)
        marker(event, command)
        if command.path.startswith("/api/judging/roadshow"):
            event.judge_login_schedule.require_open(db, "roadshow")
    elif command.path == "/api/admin/judging/roadshow/window":
        marker(event, command)
    return event.judging.dispatch(db, actor, command.method, command.path, command.body), None, None


@transaction
def session(event, command, db, actor):
    return event.admin_team_access.session_view(actor, command.cookie), None, None


@transaction
def board(event, command, db, actor):
    return professional.board(event, db, actor)


@transaction
def logout(event, command, db, actor):
    marker(event, command, required=bool(operator_for(actor)))
    if command.login_id is not None and actor is None:
        raise APIError(401, "登录身份已变化，请重新登录后操作")
    if actor:
        token = session_token(command.cookie)
        db.execute("DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),))
    # An expiry header here could erase a newer login's cookie.
    return {"ok": True}, None, None


@transaction
def judge(event, command, db, actor):
    ordinary_judge(event, db, actor)
    marker(event, command, required=False)
    return professional.judge_page(event, db, actor)


@transaction
def score(event, command, db, actor):
    ordinary_judge(event, db, actor)
    marker(event, command, required=False)
    return professional.save_score(event, db, actor, command.body)


@transaction
def audience(event, command, db, actor):
    marker(event, command)
    return professional.audience_page(event, db, actor)


@transaction
def vote(event, command, db, actor):
    marker(event, command)
    return professional.vote(event, db, actor, command.body)


@transaction
def team(event, command, db, actor):
    marker(event, command, required=bool(operator_for(actor)))
    bound = event.bound_submission_team(db, actor)
    value = event.submissions.team_submission_response(db, bound)
    return {**value, "loginId": event.login_id(command.cookie), "operator": operator_for(actor)}, None, None


@transaction
def organizer_console(event, command, db, actor):
    if command.path in {"/api/admin", "/api/admin/settings"}:
        marker(event, command, required=False)
    return organizer.dispatch(event, db, actor, command.method, command.path, command.body)
