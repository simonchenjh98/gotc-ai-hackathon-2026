"""The event API's route, actor and request-model declarations."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
from typing import Literal

from starlette.routing import compile_path

from .. import api
from ..errors import APIError
from . import request_models as models


IMPORT_BODY_LIMIT = 2 * 1024 * 1024


Actor = Literal["public", "admin", "team", "judge", "audience", "team-or-admin", "finals-access"]


@dataclass(frozen=True)
class Endpoint:
    path: str
    method: str
    actor: Actor
    controller: Callable
    snapshot: bool = False
    limit: int = 16 * 1024
    model: type | tuple[type, ...] | None = None
    unmodeled: str | None = None


@cache
def endpoints() -> tuple[Endpoint, ...]:
    routes = []

    def add(controller: Callable, method: str, actor: Actor, *paths: str, limit: int = 16384,
            model: type | tuple[type, ...] | None = None, snapshot: bool = False, unmodeled: str | None = None):
        if method == "POST" and (model is None) == (unmodeled is None):
            raise ValueError("A POST route must declare its payload model or explain why its payload is unused")
        routes.extend(Endpoint("/api/" + path, method, actor, controller, snapshot, limit, model, unmodeled)
                      for path in paths)

    add(api.admin_data, "GET", "admin", "admin/data/sync", "admin/data/template", "admin/data/runs",
        "admin/data/reviews", "admin/data/review-history", "admin/data/audit", "admin/data/export")
    add(api.admin_data, "POST", "admin", "admin/data/review-validity", model=models.ReviewValidityBody)
    add(api.admin_data, "POST", "admin", "admin/data/import/commit", model=models.ImportCommitBody)
    add(api.admin_data, "POST", "admin", "admin/data/import/preview", model=models.ImportPreviewBody,
        limit=IMPORT_BODY_LIMIT)
    add(api.audience, "GET", "audience", "audience", snapshot=True)
    add(api.backups, "GET", "team-or-admin", "submission/backups/{team}/{version}/{filename}")
    add(api.board, "GET", "admin", "board", snapshot=True)
    add(api.claims, "GET", "admin", "admin/claims")
    add(api.claims, "POST", "public", "claims/request", model=models.ClaimRequest)
    add(api.claims, "POST", "public", "claims/status", "claims/redeem", model=models.ReceiptProof)
    add(api.claims, "POST", "public", "claims/update", model=models.ClaimUpdate)
    add(api.claims, "POST", "admin", "admin/claims/person", model=models.PersonEdit)
    add(api.claims, "POST", "admin", "admin/claims/auto-review", model=models.AutoReviewSetting)
    add(api.claims, "POST", "admin", "admin/claims/approve",
        model=(models.OnlineApproval, models.OnsiteApproval))
    add(api.claims, "POST", "admin", "admin/claims/review", model=(models.ApproveClaim, models.RejectClaim),
        limit=131072)
    add(api.claims, "POST", "admin", "admin/claims/roster",
        model=(models.PeopleImport, models.AutoReviewImport), limit=IMPORT_BODY_LIMIT)
    add(api.detect_models, "POST", "admin", "admin/keys/upstreams/models", model=models.ProbeModels)
    add(api.finals, "GET", "public", "finals/login-status", snapshot=True)
    add(api.finals, "GET", "finals-access", "finals/entry", "finals/judge", snapshot=True)
    add(api.finals, "GET", "admin", "finals/board", "admin/finals", snapshot=True)
    add(api.finals, "POST", "public", "finals/access", model=models.CodeBody)
    add(api.finals, "POST", "finals-access", "finals/score", model=models.NumericBallot)
    add(api.finals, "POST", "finals-access", "finals/nominations", model=models.Nominations)
    add(api.finals, "POST", "admin", "admin/finals/roster", model=models.RosterSelection)
    add(api.finals, "POST", "admin", "admin/finals/judges", model=models.RevisionBody)
    add(api.finals, "POST", "admin", "admin/finals/settings", model=models.FinalsSettings)
    add(api.finals, "POST", "admin", "admin/finals/awards", model=models.AwardSelection)
    add(api.gateway_keys, "GET", "team", "team/keys", "team/keys/logs")
    add(api.gateway_overview, "GET", "admin", "admin/keys")
    add(api.gateway_keys, "GET", "admin", "admin/keys/export", "admin/keys/upstreams/test/{kind}",
        "admin/keys/upstreams/models/{kind}")
    add(api.gateway_keys, "POST", "team", "team/keys", model=models.NameBody)
    add(api.gateway_keys, "POST", "team", "team/keys/delete", model=models.DeleteKey)
    add(api.gateway_keys, "POST", "admin", "admin/keys/settings", model=models.GatewaySettings)
    add(api.gateway_keys, "POST", "admin", "admin/keys/logs/clear", model=models.EmptyBody)
    add(api.gateway_keys, "POST", "admin", "admin/keys/upstreams", model=models.UpstreamSettings)
    add(api.gateway_keys, "POST", "admin", "admin/keys/stats", model=models.StatsQuery)
    add(api.gateway_keys, "POST", "admin", "admin/keys/logs", model=models.LogQuery)
    add(api.gateway_keys, "POST", "admin", "admin/keys/log", model=models.LogDetails)
    add(api.gateway_keys, "POST", "admin", "admin/keys/provision", "admin/keys/revoke", model=models.TeamIdBody)
    add(api.judge, "GET", "judge", "judge", snapshot=True)
    add(api.judging, "GET", "public", "judging/{stage}/board")
    add(api.judging, "GET", "judge", "judging/{stage}", "judging/{stage}/")
    add(api.judging, "GET", "admin", "admin/judging/{stage}", "admin/judging/{stage}/")
    add(api.judging, "POST", "judge", "judging/{stage}/review", model=models.QualitativeReview)
    add(api.judging, "POST", "admin", "admin/judging/{stage}/roster", model=models.RosterSelection)
    add(api.judging, "POST", "admin", "admin/judging/{stage}/reset", model=models.ResetJudging)
    add(api.judging, "POST", "admin", "admin/judging/{stage}/publish", model=models.RunBody)
    add(api.judging, "POST", "admin", "admin/judging/{stage}/window",
        model=(models.JudgingDeadline, models.StartRoadshow))
    add(api.login, "POST", "public", "login", model=models.LoginBody)
    add(api.logout, "POST", "public", "logout", model=models.EmptyBody)
    add(api.organizer_console, "GET", "admin", "admin", snapshot=True)
    add(api.organizer_console, "GET", "admin", "admin/export", "admin/submissions/export")
    add(api.organizer_console, "POST", "admin", "admin/settings", model=models.EventSettings)
    add(api.organizer_console, "POST", "admin", "admin/teams", model=models.TeamEdit)
    add(api.organizer_console, "POST", "admin", "admin/credentials", model=models.ManageCredential)
    add(api.organizer_console, "POST", "admin", "admin/invitations", model=models.Invite)
    add(api.organizer_console, "POST", "admin", "admin/revoke", model=models.RevokeCredential)
    add(api.organizer_console, "POST", "admin", "admin/team-access", model=models.ManageTeamAccess)
    add(api.registration, "GET", "public", "registration")
    add(api.registration, "GET", "admin", "admin/registration")
    add(api.registration, "POST", "public", "registration/open", model=models.CodeBody)
    add(api.registration, "POST", "public", "registration/save", model=models.LegacyRegistrationSave)
    add(api.registration, "POST", "public", "registration/create",
        unmodeled="Retired management-code operation; the payload is never used.")
    add(api.registration, "POST", "admin", "admin/registration/import", model=models.RegistrationImport)
    add(api.registration, "POST", "admin", "admin/registration/rotate",
        unmodeled="Retired management-code operation; the payload is never used.")
    add(api.repositories, "GET", "team", "team/repository")
    add(api.repositories, "GET", "admin", "admin/repositories")
    add(api.repositories, "POST", "team", "team/repository/apply", model=models.RepositoryApplication)
    add(api.repositories, "POST", "team", "team/repository/refresh", "team/repository/preview",
        model=models.EmptyBody)
    add(api.repositories, "POST", "admin", "admin/repositories/approve", "admin/repositories/reconcile",
        model=models.RepositoryApproval)
    add(api.roadshow_access, "GET", "public", "roadshow/access")
    add(api.roadshow_access, "GET", "admin", "admin/roadshow-access")
    add(api.roadshow_access, "POST", "public", "roadshow/login", "roadshow/claim", model=models.NameBody)
    add(api.roadshow_access, "POST", "admin", "admin/roadshow-access", model=models.RoadshowNames)
    add(api.roadshow_scoring, "GET", "judge", "roadshow/scoring")
    add(api.roadshow_scoring, "GET", "admin", "roadshow/board", "admin/roadshow/scoring")
    add(api.roadshow_scoring, "POST", "judge", "roadshow/score", model=models.RoadshowBallot)
    add(api.roadshow_scoring, "POST", "admin", "admin/roadshow/publish", model=models.RoadshowPublication)
    add(api.roadshow_scoring, "POST", "admin", "admin/roadshow/teams", model=models.RoadshowTeams)
    add(api.score, "POST", "judge", "score", model=models.ScoreBody)
    add(api.session, "GET", "public", "session")
    add(api.submission, "POST", "team", "submission", model=models.SubmissionBody, limit=32768)
    add(api.submission_records, "GET", "team", "submission/history", "submission/history/{version}",
        "submission/requests/{request_id}")
    add(api.submission_records, "GET", "admin", "admin/submissions/{team}/history",
        "admin/submissions/{team}/history/{version}", "admin/submissions/{team}/checks")
    add(api.submission_records, "POST", "admin", "admin/submissions/{team}/checks", model=models.MaterialCheck)
    add(api.team, "GET", "team", "team")
    add(api.team_lifecycle, "GET", "admin", "admin/data/team", "admin/data/recycle-bin", snapshot=True)
    add(api.team_lifecycle, "POST", "admin", "admin/data/team/recycle", model=models.RecycleTeam)
    add(api.team_lifecycle, "POST", "admin", "admin/data/team/restore", model=models.RestoreTeam)
    add(api.team_profile, "GET", "team", "team/profile", snapshot=True)
    add(api.team_profile, "POST", "team", "team/profile", model=models.TeamProfileSave)
    add(api.team_score, "GET", "team", "team/score", snapshot=True)
    add(api.team_workbench, "GET", "admin", "admin/team-workbench")
    add(api.team_workbench, "POST", "team", "team/workbench/return", model=models.EmptyBody)
    add(api.team_workbench, "POST", "admin", "admin/team-workbench", model=models.TeamIdBody)
    add(api.vote, "POST", "audience", "vote", model=models.TeamIdBody)
    return tuple(routes)


@cache
def _patterns():
    return tuple((endpoint, compile_path(endpoint.path)[0]) for endpoint in endpoints())


def resolve_endpoint(method: str, path: str) -> Endpoint:
    """Use the framework's declared route patterns for direct domain callers."""
    for endpoint, pattern in _patterns():
        if method == endpoint.method and pattern.fullmatch(path):
            return endpoint
    raise APIError(404, "接口不存在")
