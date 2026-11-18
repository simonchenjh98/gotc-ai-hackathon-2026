"""Application routing, authorization and transaction boundaries."""
from __future__ import annotations

import hashlib
from http.cookies import CookieError, SimpleCookie

from . import organizer
from .constants import COOKIE_NAME
from .errors import APIError
from .judging import professional
from .teams.admin_team_access import PATHS as ADMIN_TEAM_PATHS, operator_for
from .teams.team_lifecycle import PATHS as TEAM_LIFECYCLE_PATHS
from .validation import validate_unicode


def dispatch(event, method, path, body, cookie, ip, query=None, expected_login_id=None, server_received_at=None, finals_judge_id=None, finals_access_token=None):
    """Return (JSON object or CSV bytes, optional session token, cookie action)."""
    validate_unicode(body)
    if event.roadshow_scoring.supports(path):
        return event.roadshow_scoring.dispatch(method,path,body,cookie,ip,expected_login_id,query),None,None
    if event.roadshow_access.supports(path):
        result = event.roadshow_access.dispatch(method,path,body,cookie,ip,expected_login_id,query)
        return result if method == 'POST' and path == '/api/roadshow/login' else (result,None,None)
    if path.startswith(("/api/registration", "/api/admin/registration")):
        return event.registration.dispatch(method,path,body,cookie,ip,expected_login_id,query),None,None
    if path.startswith(("/api/claims/","/api/admin/claims")):
        return event.claims.dispatch(method,path,body,cookie,ip,expected_login_id,query),None,None
    if method == "POST" and path == "/api/login":
        return event.login(body, ip)
    if method=='POST' and path=='/api/submission':return event.dispatch_submission(body,cookie,expected_login_id,server_received_at)
    if path.startswith(('/api/team/repository','/api/admin/repositories')):
        return event.repositories.dispatch(method,path,body,cookie,expected_login_id,query or {}),None,None
    if method == 'POST' and path == '/api/admin/keys/upstreams/models':
        return event.keys.detect_models(body, cookie, expected_login_id), None, None
    with event.db() as db:
        # Resolve write identities only after the lock: revocation may have committed while queued.
        if method=="POST":
            db.execute("BEGIN IMMEDIATE")
        elif method=="GET" and (event.finals.supports(path) or path in TEAM_LIFECYCLE_PATHS or path in {"/api/admin", "/api/team/profile", "/api/team/score", "/api/board", "/api/judge", "/api/audience"}):
            # Keep release settings, aggregates and editor revisions in one read snapshot.
            db.execute("BEGIN")
        actor = event.account(db, cookie)
        if path=='/api/team/profile':
            event.require(actor,'team')
            if operator_for(actor):raise APIError(403,'本队资料仅供参赛队伍登录管理；组织方请使用独立后台')
            if not expected_login_id:raise APIError(428,'请刷新页面并核对登录队伍','login_required')
            if expected_login_id!=event.login_id(cookie):raise APIError(401,'登录身份已变化，请重新登录')
            if query:raise APIError(400,'本队资料由当前登录队伍确定，不接受查询参数')
            team=event.bound_submission_team(db,actor)
            if event.admin_data.team(db,team['id'])['status']!='active':raise APIError(409,'本队资料已暂停，请联系组织方','team_ineligible')
            if method=='GET':
                if body:raise APIError(400,'读取本队资料不接受请求字段')
                return {**event.registration.profile(db,team['id']),'loginId':event.login_id(cookie)},None,None
            if method=='POST':return {**event.registration.save_profile(db,actor,team['id'],body),'loginId':event.login_id(cookie)},None,None
            raise APIError(405,'本队资料仅支持读取和保存')
        if path in TEAM_LIFECYCLE_PATHS:
            return event.team_lifecycle.dispatch(db,actor,method,path,body,query or {},cookie,expected_login_id),None,None
        if path in ADMIN_TEAM_PATHS:
            return event.admin_team_access.dispatch(db,actor,method,path,body,cookie,expected_login_id,query or {})
        if operator_for(actor) and (path in {'/api/team','/api/submission','/api/logout'} or path.startswith('/api/submission/')) and not expected_login_id:
            raise APIError(428,'请刷新页面并核对当前代管队伍','login_required')
        if actor and path in {'/api/judge', '/api/score', '/api/judging/exhibition', '/api/judging/exhibition/', '/api/judging/exhibition/review'} and event.roadshow_access.owns_account(db,actor['id']):
            raise APIError(403, '此邀请码仅用于路演评审，请从路演入口进入')
        if event.finals.supports(path):
            return event.finals.dispatch(db,actor,method,path,body,query or {},expected_login_id,cookie,finals_judge_id,finals_access_token,ip),None,None
        if path == "/api/admin/judging/roadshow/window":
            event.require(actor, "admin")
            if not expected_login_id:
                raise APIError(428, "请刷新路演窗口页面并核对登录身份", "login_required")
            if expected_login_id != event.login_id(cookie):
                raise APIError(401, "管理员登录身份已变化，请重新进入路演窗口")
        page_role = ("audience" if path in {"/api/audience", "/api/vote"} else
                     "judge" if path in {"/api/judging/exhibition", "/api/judging/exhibition/", "/api/judging/exhibition/review",
                                          "/api/judging/roadshow", "/api/judging/roadshow/", "/api/judging/roadshow/review"} else None)
        if page_role:
            event.require(actor, page_role)
            if not expected_login_id:
                raise APIError(428, "缺少页面登录信息，请刷新页面并核对登录身份后重试", "login_required")
            if expected_login_id != event.login_id(cookie):
                raise APIError(401, "登录身份已变化，请重新登录后操作")
        if path in {'/api/judging/roadshow', '/api/judging/roadshow/', '/api/judging/roadshow/review'}:
            event.judge_login_schedule.require_open(db, 'roadshow')
        if path == "/api/team/score":
            event.require(actor, "team")
            if method != "GET":
                raise APIError(405, "请使用查询入口读取成绩")
            if query:
                raise APIError(400, "查分由当前登录队伍确定")
            if not expected_login_id:
                raise APIError(428, "请刷新页面并核对登录队伍", "login_required")
            if expected_login_id != event.login_id(cookie):
                raise APIError(401, "登录身份已变化，请重新登录")
            return event.standings.own_team_score(db, actor), None, None
        team_keys_route = path == "/api/team/keys" or path.startswith("/api/team/keys/")
        admin_keys_route = path == "/api/admin/keys" or path.startswith("/api/admin/keys/")
        if admin_keys_route:
            event.require(actor, "admin")
            # Download links cannot attach a page marker; all console reads, writes and probes must.
            if not expected_login_id and not (method == "GET" and path == "/api/admin/keys/export"):
                raise APIError(428, "缺少页面登录信息，请刷新页面并核对登录身份后重试", "login_required")
            if expected_login_id is not None and expected_login_id != event.login_id(cookie):
                raise APIError(401, "登录身份已变化，请重新登录后操作")
        if team_keys_route:
            event.require(actor, "team")
            # An old tab shares the new login's cookie. Require its own page marker,
            # including for reads, before any key data or mutation is dispatched.
            if not expected_login_id:
                raise APIError(428, "缺少页面登录信息，请刷新页面并核对登录身份后重试", "login_required")
        if (team_keys_route or path in {"/api/team", "/api/submission", "/api/logout", "/api/judge", "/api/score", "/api/admin", "/api/admin/settings"} or path.startswith("/api/submission/")) and expected_login_id is not None and (not actor or expected_login_id != event.login_id(cookie)):
            raise APIError(401, "登录身份已变化，请重新登录后操作")
        if path.startswith('/api/submission/backups/'):return event.repository_backups.download(db,actor,method,path,query or {}),None,None
        if event.submission_records.supports(path):return event.submission_records.dispatch(db,actor,method,path,body,query or {}),None,None
        if path.startswith('/api/admin/data/'):
            parsed_cookie=SimpleCookie()
            try:parsed_cookie.load(cookie)
            except CookieError:pass
            session_token=parsed_cookie[COOKIE_NAME].value if COOKIE_NAME in parsed_cookie else ''
            session_hash=hashlib.sha256(session_token.encode()).hexdigest()
            return event.admin_data.dispatch(db,actor,method,path,body,query or {},session_hash),None,None
        if path.startswith(("/api/judging/","/api/admin/judging/")):
            return event.judging.dispatch(db,actor,method,path,body), None, None
        if path.startswith(("/api/team/keys", "/api/admin/keys")):
            return event.keys.dispatch(db, actor, method, path, body), None, None
        if method == "GET" and path == "/api/session":
            return event.admin_team_access.session_view(actor,cookie), None, None
        if method == "GET" and path == "/api/board":
            return professional.board(event, db, actor)
        if method == "POST" and path == "/api/logout":
            if actor:
                try:
                    parsed = SimpleCookie(cookie)
                    token = parsed[COOKIE_NAME].value
                    db.execute("DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),))
                except (KeyError, ValueError, CookieError):
                    pass
            # Revocation is authoritative. A late expiry header would erase a newer login's cookie.
            return {"ok": True}, None, None
        if method == "GET" and path == "/api/judge":
            return professional.judge_page(event, db, actor)
        if method == "POST" and path == "/api/score":
            return professional.save_score(event, db, actor, body)
        if method == "GET" and path == "/api/audience":
            return professional.audience_page(event, db, actor)
        if method == "POST" and path == "/api/vote":
            return professional.vote(event, db, actor, body)
        if path in {"/api/team", "/api/submission"}:
            event.require(actor, "team")
            bound=event.bound_submission_team(db,actor)
            if method=="GET" and path=="/api/team":
                return {**event.submissions.team_submission_response(db, bound), "loginId": event.login_id(cookie), 'operator':operator_for(actor)}, None, None
            if method=="POST" and path=="/api/submission":
                return event.save_submission(db, actor, bound, body), None, None
        if path.startswith("/api/admin"):
            return organizer.dispatch(event, db, actor, method, path, body)
        raise APIError(404, "接口不存在")
