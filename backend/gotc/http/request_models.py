"""Accepted platform JSON bodies. Domain controllers validate credentials, revisions and values.

Keep these types non-coercing: the original JSON also feeds submission fingerprints.
Variants represent alternative valid request shapes, not optional authorization fields.
"""
from typing import Literal, NotRequired, Protocol, Required, TypedDict, cast, is_typeddict


class EmptyBody(TypedDict):
    pass


class LoginBody(TypedDict, total=False):
    role: Required[Literal["admin", "team", "judge", "audience"]]
    code: Required[str]
    accountId: str | None
    username: str | None
    totp: str | None


class CodeBody(TypedDict):
    code: str


class NameBody(TypedDict):
    name: str


class TeamIdBody(TypedDict):
    teamId: str


class RevisionBody(TypedDict):
    expectedRevision: int


class RunBody(TypedDict):
    runId: str


class ScoreBody(TypedDict, total=False):
    teamId: Required[str]
    status: Required[Literal["draft", "submitted", "recused"]]
    expectedRevision: Required[int]
    scores: list[int | float] | None
    feedback: str
    feedbackSharing: str | None


class SubmissionMaterials(TypedDict, total=False):
    projectName: str
    summary: str
    slidesUrl: str
    filesUrl: str
    demoUrl: str
    videoUrl: str
    codeUrl: str
    readmeUrl: str
    license: str
    licenseUrl: str
    sourcesAndLimitations: str
    codeVersion: str
    demoInstructions: str
    confirmed: bool


class SubmissionBody(TypedDict):
    status: str
    expectedVersion: int
    materials: SubmissionMaterials
    requestId: NotRequired[str]
    repoPreviewId: NotRequired[str | None]


class ReviewValidityBody(TypedDict):
    runId: str
    teamId: str
    judgeId: str
    expectedVersion: int
    invalidated: bool
    reason: str


class ImportPreviewBody(TypedDict):
    kind: Literal["teams", "judges"]
    csv: str


class ImportCommitBody(TypedDict):
    previewId: str


class ClaimMember(TypedDict):
    name: str
    school: str


class ClaimRequest(ClaimMember):
    members: NotRequired[list[ClaimMember]]
    teamName: NotRequired[str]
    track: NotRequired[str]


class ReceiptProof(TypedDict):
    requestId: str
    secret: str


class ClaimUpdate(ReceiptProof, RevisionBody):
    members: list[ClaimMember]
    teamName: NotRequired[str]
    track: NotRequired[str]


class RosterPerson(ClaimMember):
    id: str
    notes: str
    needsReview: bool


class PersonEdit(RevisionBody):
    person: RosterPerson


class AutoReviewSetting(RevisionBody):
    enabled: bool


class PeopleImport(TypedDict):
    people: list[RosterPerson]


class AutoReviewPerson(ClaimMember):
    id: str
    notes: str
    eligible: bool


class AutoReviewRoster(RevisionBody):
    sourceName: str
    sourceSha256: str
    entries: list[AutoReviewPerson]


class AutoReviewImport(TypedDict):
    autoReview: AutoReviewRoster


class ClaimApproval(RevisionBody):
    requestId: str
    reviewContext: str
    teamName: str
    track: str
    resolvedIdentity: bool
    note: str
    teamId: NotRequired[str]
    teamContext: NotRequired[str]


class OnlineApproval(ClaimApproval):
    verificationMethod: Literal["online"]
    applicationReviewed: Literal[True]
    reissueRevoked: NotRequired[Literal[True]]


class OnsiteApproval(ClaimApproval):
    pin: str
    inPersonVerified: Literal[True]


class RejectClaim(RevisionBody):
    requestId: str
    action: Literal["reject"]
    note: str


class ApproveClaim(RevisionBody):
    requestId: str
    action: Literal["approve"]
    note: str
    teamId: str
    captainId: str
    memberIds: list[str]
    memberRevisions: dict[str, int]
    pin: str
    inPersonVerified: Literal[True]
    resolvedIdentity: bool
    declaredMemberIds: NotRequired[list[str]]


class ProbeModels(TypedDict):
    kind: Literal["openai", "anthropic"]
    url: str
    key: str


class NumericBallot(RunBody, TeamIdBody, RevisionBody):
    status: Literal["draft", "submitted", "recused"]
    scores: list[int | float]
    feedback: str


class RoadshowBallot(RunBody, TeamIdBody, RevisionBody):
    status: Literal["draft", "submitted", "recused"]
    scores: list[int | float] | None
    feedback: str


class Nominations(RunBody, RevisionBody):
    status: Literal["draft", "submitted"]
    nominations: dict[str, str | None]


class RosterSelection(RevisionBody):
    teamIds: list[str]
    judgeIds: list[str]


class FinalsSettings(RevisionBody):
    open: bool
    published: bool


class AwardSelection(RevisionBody):
    selections: dict[str, list[str]]


class DeleteKey(TypedDict):
    id: str


class GatewaySettings(TypedDict, total=False):
    maxKeys: int
    expiresAt: int | None
    logContent: bool
    teamRpm: int
    announcement: str
    blockedWords: list[str]
    logRetentionHours: int
    logMaxMB: int


class ProviderConfig(TypedDict):
    url: str
    models: dict[str, list[int | float]]
    key: NotRequired[str]
    name: NotRequired[str]
    aliases: NotRequired[dict[str, str]]
    defaultPrice: NotRequired[list[int | float]]


class UpstreamSettings(TypedDict):
    upstreams: dict[str, ProviderConfig]


class UsageFilters(TypedDict, total=False):
    teamId: str | None
    keyId: str | None


class StatsQuery(UsageFilters, total=False):
    days: int
    granularity: Literal["day", "hour"]


class LogQuery(UsageFilters, total=False):
    beforeId: int | None
    model: str | None
    requestId: str | None
    errorsOnly: bool


class LogDetails(TypedDict):
    id: int


class QualitativeReview(RunBody, TeamIdBody):
    expectedVersion: int
    status: Literal["draft", "submitted", "recused"]
    feedback: str
    scores: None


class ResetJudging(RevisionBody):
    reason: str


class JudgingDeadline(RevisionBody):
    open: bool
    deadline: int | None


class StartRoadshow(RevisionBody):
    open: Literal[True]
    start90Minutes: Literal[True]


class EventSettings(TypedDict, total=False):
    expectedRevisions: Required[dict[str, int]]
    scoringOpen: bool
    resultsPublished: bool
    minJudges: int
    submissionsOpen: bool
    submissionDeadline: int | None
    votingOpen: bool
    votingDeadline: int | None


class TeamEdit(TypedDict, total=False):
    id: str
    expectedRevision: int
    number: int
    name: str
    project: str
    track: str
    status: Literal["active", "withdrawn", "disqualified"]
    statusReason: str
    booth: str
    roadshowOrder: int | None


class Invite(TypedDict):
    role: Literal["judge", "audience"]
    name: str


class RevokeCredential(TypedDict):
    role: Literal["judge", "audience"]
    id: str


class ManageCredential(RevokeCredential):
    action: Literal["rotate", "revoke"]


class ManageTeamAccess(TeamIdBody):
    action: Literal["issue", "rotate", "revoke"]


class TeamRegistration(TypedDict):
    name: str
    captain: str
    school: str
    phone: str
    targetSize: int | None
    members: list[str]
    project: str
    summary: str


class LegacyRegistrationSave(CodeBody, RevisionBody):
    team: TeamRegistration


class RegistrationRow(TypedDict):
    sourceKey: str
    team: TeamRegistration
    raw: dict[str, object]


class RegistrationImport(TypedDict):
    rows: list[RegistrationRow]


class RepositoryApplication(TypedDict):
    provider: str
    usernames: list[str]


class RepositoryApproval(TeamIdBody, RevisionBody):
    usernamesVerified: Literal[True]


class RoadshowNames(RevisionBody, RunBody):
    names: list[str]


class RoadshowPublication(RevisionBody, RunBody):
    published: bool


class RoadshowTeams(RevisionBody, RunBody):
    teamIds: list[str]


class MaterialCheck(RevisionBody):
    version: int | str
    field: str
    status: Literal["unchecked", "accessible", "permission_required", "unavailable"]
    note: str


class RecycleTeam(TeamIdBody, RevisionBody):
    reason: str
    recycleContext: str


class RestoreTeam(TeamIdBody, RevisionBody):
    reason: str
    name: str


class TeamProfileSave(RevisionBody):
    expectedTeamRevision: int
    team: TeamRegistration


class _TypedDictFields(Protocol):
    __required_keys__: frozenset[str]
    __optional_keys__: frozenset[str]


def accepts_fields(value: object, model: type) -> bool:
    """Check keys without coercing values or changing the domain's error handling."""
    if not isinstance(value, dict) or not is_typeddict(model):
        return False
    fields = cast(_TypedDictFields, model)
    return fields.__required_keys__ <= value.keys() <= fields.__required_keys__ | fields.__optional_keys__
