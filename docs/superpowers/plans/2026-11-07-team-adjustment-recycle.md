# Team Adjustment and Recycle Bin Implementation Plan

## Implementation checkpoint (2026-11-07)

The manager resumed implementation after the separate urgent recovery release. References below to an operational implementation pause describe the original review checkpoint and are superseded by this note. The dated `d1dc8f7`/`6f82809` investigation evidence remains historical context, not the implementation baseline.

Actual implementation baseline: `codex/team-adjustment-recycle` branched from `a1eb41f06661efae0b071da287f0f99e0aa42e21`, preserving PR44's reviewed revoked-identity reissue behavior on top of main `6a982ab8349f2e01f14a5be4465cc857b6db5b82`. PR44 remains an explicit dependency until normally merged; no force push, rollback, or deployment is part of this implementation.

Packages A, B and C are implemented. Fourteen runtime source files were frozen after independent review closed the verified-member restoration correction and the administrator registration-history recycle marker. Exact approved person IDs and individual schools govern verified-team restoration; the older captain-school/name-only registration duplicate policy applies only to archived registration-only rosters. C also owns the narrowly authorized login-marker/owner guard adaptation for the four new routes in `platform.js`.

Package D completed additive-upgrade/restart, concurrency, privacy, browser and full regression acceptance on the frozen source. Final local results: 433 Node tests, 545 server Python tests (including eight additive-upgrade/race/gateway/recovery release gates), 11 deployment tests, site validation and diff checks passed. Real Chrome desktop and 390px viewport acceptance passed 12 checks; 28 unauthorized lifecycle requests and five shared projections passed privacy assertions. History-row byte hashes and all fourteen runtime source hashes remained unchanged. Local Python was 3.14.3; supported Python 3.11/3.12 CI remains a separate PR check.

Independent final acceptance and normal protected-branch PR checks remain required. All verification uses synthetic temporary data. This work does not authorize production deletion/restoration, credential issuance for a real team, or deployment.

---

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 组织方在队伍调整/编辑界面看到队员姓名及有来源的学校信息，能把队伍移入回收站、停用参赛并按冲突检查恢复；回收站中的队名不阻止新申请审核。

**Architecture:** Add an administrator-only `TeamLifecycle` service with additive recycle-event/member-history tables. Keep all original team IDs and historical records, remove only archived teams' current membership assignments, and mirror a recycled team as `withdrawn` in the existing eligibility field. All lifecycle mutations use the existing SQLite writer transaction and displayed revision; recycle additionally validates the private detail's opaque context to catch membership/access changes that do not increment team revision. A private detail endpoint supplies member information without changing shared/public team serialization.

**Tech Stack:** Existing Python standard library + SQLite, vanilla HTML/CSS/JavaScript, `unittest`, Node test runner. No new runtime dependency or remote service.

## Global Constraints

- Root for all paths below: `/Users/xinlu/Documents/ChatGPT/GOTC/gotc-ai-hackathon-2026/.runtime/worktrees/repository-provisioning`.
- Dated investigation baseline: `codex/five-person-teams` at `d1dc8f772f4428a718e214cd72b493b1cb2e348f`, initially clean. Current local integration baseline verified during plan revision: `6f828093fde8f10b83f223a9c8aeaf97048364eb`, including reviewed revoked-account recovery `c1172f1` and deployed-scoring source integration `58e3eed`. Preserve the existing `reissueRevoked` branch and newer scoring-roster routes; recheck HEAD before implementation.
- Product writes remain paused during the urgent operational release. Planning/review approval does not lift that pause; the manager must resume implementation. This plan stays in ignored `.superpowers/sdd/2026-11-07-team-adjustment-recycle/2026-11-07-team-adjustment-recycle.plan.md` until implementation can safely restore it to its original `docs/superpowers/plans/2026-11-07-team-adjustment-recycle.md` destination and commit it.
- 用户已经选择“移入回收站、停用参赛、可恢复”；“删除后的队伍名字不影响我通过审核”。
- 用户明确“别管自动审核了”：不改变自动审核名单、开关、匹配条件、申请补全逻辑；唯一关联改动是忽略回收站队伍的重名候选。
- 只查看队员，不添加成员/学校身份编辑，不猜学校，不通过姓名模糊合并身份。
- 不访问或复制真实名单、生产数据库、私有 `.runtime`、附件、凭据；不修改 PDF/PPT。
- 不物理删除 teams、accounts、registrations、claim_requests、claim_people、submissions、scores、votes、frozen rosters、repository bindings/seals、usage 或 audit。保留队号且不复用。
- 仅恢复队伍数据/原资格和可重新占有的成员关系。旧提交码、旧登录会话、旧 API key、旧登记管理码均不自动恢复；界面明确告知后续重发/开通步骤。
- 新接口只允许真正的管理员身份，代管身份不能调用；读写均要求正确 `X-GOTC-Login`。
- 所有验证使用合成临时数据库；本计划不授权删除真实队伍、部署或绕过分支保护。
- 重用现有 checkout；执行者负责分支隔离/集成。经理只委派、审查与验收。禁止 `--admin` 绕过或冒充审核者。

---

## 1. Investigation and selected design

Evidence: `.superpowers/sdd/2026-11-07-team-adjustment-recycle/findings.md`.

The current edit form (`platform-admin.js:164-201`) receives team metadata only. Approved members live in `claim_memberships → claim_people`, whereas a preregistration stores one captain school plus a name-only array. `Claims.approve_open`, `TeamRegistration.check_duplicates`, and automatic candidate selection currently search every team, including inactive teams. Current member assignment has a `person_id` primary key, so merely hiding a team would still reserve its people. Ordinary team binding and qualitative stage review require explicit recycle checks; numeric scoring already reads active status.

Chosen approach: independent tombstone/history plus compatibility status. Physical deletion breaks history; using only withdrawn cannot release name/people safely or support restoration. Do not add a general identity-management subsystem or broaden this into an eligibility cleanup project.

### Member display contract

1. For a current team with assigned `claim_memberships`, use only those exact person IDs joined to `claim_people`. `captain_id` on their exact linked request identifies the captain; do not require receipt issuance to display approved members. The urgent revoked-account recovery uses the existing approved/issued states; the previously proposed bound_existing state was cancelled and must not be implemented for that incident. Preserve explicit recorded school text, with `needsReview` and source labels where appropriate; “待核对” remains pending, not a verified school.
2. For an unassigned preregistration, show the captain with registration school and source `registration_captain`; show other recorded names with `school:null`, source `registration_member`, and “学校待补充（原登记未提供）”. Do not lookup same-name people to fill schools.
3. Where neither source exists, show “暂无成员资料”; do not synthesize a member from the team/account display name.
4. For a recycled team, display the immutable deletion-time roster snapshot with “删除时记录”. Display current restore conflicts separately; another team's current people never overwrite the archived roster.
5. The detail API exposes only display name, school/source/review indicator, role and necessary opaque person ID. It must not include phone, notes, raw import data, PIN/receipt material, code/key hashes, contacts, or credentials. Generic `enrich_team()` must never gain a `members` field, since it is reused in frozen/public stage data.

### Delete and restore semantics

- Deleting is a reversible lifecycle operation, confirmed with the team name and an explicit reason. It archives current member assignments and display roster, sets a tombstone, moves status to withdrawn, increments team revision, revokes ordinary/delegated sessions and all existing team/API credentials, then releases `claim_memberships` for that team. Every step is one transaction.
- Recycled teams are absent from the default organizer directory, active registration list, selectable new stage roster, participant workbench, score/vote targets and public active boards. They remain visible in an explicit administrator recycle list and existing administrator history/export endpoints.
- Keep original `teams.id` and unique `number`. Reusing a name creates a distinct team ID with a new free number; scores/submissions/claim receipts from the old team never move to it.
- Recycle and restore must invalidate existing roadshow/finals publication epochs when that team belongs to the relevant roster, including when the original status was already withdrawn. No old result is silently republished.
- Restore reacquires all archived approved person IDs. Check all conflicts first, then write all changes. Any person owned by another non-recycled team, missing/mutated identity, conflicting non-recycled name, or conflicting preregistration prevents the entire restore.
- The restore form may supply a new team name. It may not replace/reassign members. Name conflicts can therefore be resolved in that form; member conflicts instruct the organizer to retain the old team in recycle while resolving the actual team relationship outside this feature.
- Original `active`, `withdrawn`, or `disqualified` status and original status reason are restored. Thus restoring a previously disqualified team does not grant eligibility.
- Restored credentials remain inactive. The response and UI distinguish “队伍已恢复” from “参赛访问已重新开通”. Use existing submission-code rotation, API provisioning/new-key issuance, and registration-code rotation as explicit subsequent actions.
- Gateway access stops at the existing final `recheck_access` admission boundary. Requests which already passed that check may still be forwarded, finish and remain chargeable/auditable after deletion; queued calls whose final recheck occurs after deletion must fail. Preserve that boundary without holding a database lock across network I/O or adding request cancellation. Do not promise network-instant or retroactive cancellation, delete remote repositories, or erase incurred usage.

## 2. Data model and service contract

Create `server/team_lifecycle.py` and construct `self.team_lifecycle=TeamLifecycle(self)` beside the other services. At the end of `EventService.__init__`, immediately after `self.finals.initialize()` (`server/app.py:329` at baseline), initialize its schema in a fresh `with self.db() as db` block before any requests can be served. Existing bootstrap serializers must not query lifecycle tables before this point. No column order change on existing tables (legacy code uses positional INSERTs).

```sql
CREATE TABLE IF NOT EXISTS team_recycle_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 team_id TEXT NOT NULL REFERENCES teams(id),
 deleted_at INTEGER NOT NULL,
 deleted_by TEXT NOT NULL REFERENCES accounts(id),
 delete_reason TEXT NOT NULL,
 original_status TEXT NOT NULL CHECK(original_status IN ('active','withdrawn','disqualified')),
 original_status_reason TEXT NOT NULL,
 team_snapshot_json TEXT NOT NULL,
 roster_snapshot_json TEXT NOT NULL,
 restored_at INTEGER,
 restored_by TEXT REFERENCES accounts(id),
 restore_reason TEXT,
 restored_name TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS team_recycle_current
 ON team_recycle_events(team_id) WHERE restored_at IS NULL;
CREATE TABLE IF NOT EXISTS team_recycle_memberships (
 event_id INTEGER NOT NULL REFERENCES team_recycle_events(id),
 person_id TEXT NOT NULL REFERENCES claim_people(id),
 request_id TEXT NOT NULL REFERENCES claim_requests(id),
 name TEXT NOT NULL,
 school TEXT NOT NULL,
 normalized_name TEXT NOT NULL,
 normalized_school TEXT NOT NULL,
 PRIMARY KEY(event_id,person_id)
);
```

`team_snapshot_json` is an explicit allowlist: id, number, name, project, track, status, statusReason, booth, roadshowOrder, revision. `roster_snapshot_json` uses only the member-display contract below. Never serialize `SELECT *` from registrations/claims/accounts into these JSON columns.

Public Python interfaces produced by Task 1:

```python
class TeamLifecycle:
    def __init__(self, service): ...
    def initialize(self, db): ...
    def current_event(self, db, team_id): ...  # sqlite3.Row or None
    def is_recycled(self, db, team_id): ...    # bool
    def require_available(self, db, team_id): ...  # raises 409 team_recycled
    def name_collisions(self, db, name, exclude_team_id=None): ...  # list[{id,name}]
    def member_view(self, db, team_id): ...  # MemberView below
    def recycle_context(self, db, team_id): ...  # opaque 64-hex digest, same snapshot as detail
    def restore_conflicts(self, db, team_id, name): ...  # blocking list[RestoreConflict] only
    def restore_advisories(self, db, team_id): ...  # list[RestoreAdvisory], nonblocking
    def detail(self, db, team_id): ...       # DetailResponse below
    def listing(self, db): ...              # RecycleListResponse below
    def recycle(self, db, actor, body): ...  # mutation response below
    def restore(self, db, actor, body): ...  # mutation response below
```

These are interface declarations, not implementation placeholders. Implementations belong to the task owners. `name_collisions()` uses the exact existing normalization (`claims.normalize`: NFKC, casefold, whitespace collapse), imported without introducing an `admin_data ↔ claims` circular dependency. `AdminDataService` calls lifecycle through `self.s`, not a top-level import.

The shared non-recycled name candidate predicate is:

```sql
SELECT t.id,t.name FROM teams t
WHERE NOT EXISTS (
 SELECT 1 FROM team_recycle_events e
 WHERE e.team_id=t.id AND e.restored_at IS NULL
)
```

Do not replace this with `status='active'`: ordinary withdrawn/disqualified teams still follow the existing collision rules. Use this exact distinction for manual approval, registration and automatic name-candidate selection.

`claim_memberships` remains the current assignment table. Before deleting its team rows, copy every exact `(person_id,request_id)` and identity snapshot into `team_recycle_memberships`; retain prior recycle events indefinitely. Restore inserts the same assignments and never updates or deletes another team's rows.

`recycle_context()` hashes a deterministic, sorted, allowlisted snapshot under the domain `team-recycle-context-v1`: current team fields/status/revision and current recycle-event ID; membership personId/requestId mappings with corresponding person display-identity/revision and request status/revision/captainId; registration revision and roster projection; bound account ID/role/active and its credential fingerprint; current team API enablement and key IDs/active states. Return only the resulting 64-hex digest, never its sensitive input or raw account hashes. Do not include volatile usage, session lists or in-flight counters. The detail read computes roster and context in the same SQLite read snapshot. This specifically catches the approved `reissueRevoked` path assigning members while preserving the entire team row/revision; do not change that published path to manufacture a version bump.

### Transaction algorithm

Recycle, under the outer `BEGIN IMMEDIATE`:

1. Resolve and verify actual admin + login marker; validate strict JSON fields, team ID, nonnegative integer expectedRevision, 64-hex recycleContext, nonblank reason ≤500 chars. Missing recycleContext is 428 `recycle_context_required`; malformed context is 400.
2. Read team, reject nonexistent (404), stale/missing revision (409/428), already recycled (409). Recompute recycle_context under this same writer lock and compare to the submitted context; mismatch is 409 `team_context_conflict` with no writes. Only after both revision and context match may the deletion-time member snapshot be taken and mutations begin.
3. Insert recycle event and copy approved membership rows. All snapshots use allowlisted values only.
4. Set `team_details.status='withdrawn'`, status reason to the deletion reason, `revision=revision+1`; update `teams.updated_at`.
5. Set the team's ordinary account inactive; delete its `sessions`. Delete full sessions whose token_hash is in `admin_team_sessions` for this team, allowing the existing ON DELETE CASCADE to remove delegation. Never disable the underlying organizer account or delete only a delegation row.
6. Set `team_api.active=0`, `api_keys.active=0` for this team. Rotate any registration secret to `code_hash(secrets.token_urlsafe(32))` without returning or storing plaintext; increment registration revision. Delete the team's live `claim_memberships` only after snapshot insertion.
7. Explicitly invalidate affected publication epochs if status did not change; existing status triggers cover the normal active→withdrawn transition. Touch admin revision and audit `team.recycle` with event ID, before/after status/revisions and counts, not credentials/member copies.
8. Return committed lifecycle detail only after all changes succeed. Any exception rolls back every step, including session revocation.

Restore, in the same transaction model:

1. Verify admin/login, strict fields and displayed team revision; require current recycle event. `name` is required, ≤80 chars, initially the archived name; `reason` is required, ≤500 chars.
2. Recompute name conflicts against all non-recycled teams except self. Exact archived person IDs, changed/missing identities, known name+school identity already assigned elsewhere, and the existing same-registration-school/name duplicate policy remain blocking checks. For preregistration members whose school is unknown, a same-name live membership is only a nonblocking `unverified_name_overlap` advisory: “存在同名记录，学校未知，不能确定是否为同一人”. It neither adds a hard conflict nor requires a new identity editor/confirmation flow. Restore only the original unverified registration data; do not assign a person ID, create approved membership, infer a school, or alter the other team based on that advisory.
3. If blocking conflicts exist, return 409 `team_restore_conflict` with no writes. A fresh detail read supplies current identity conflicts/advisories and name conflicts for the stored old name only; the UI rules below keep that distinct from an attempted replacement name. Advisories alone do not prevent restoration.
4. Insert every archived approved membership, then mark this recycle event restored and restore original team status/reason; update name if explicitly supplied, increment team revision and updated_at. Do not touch frozen roster JSON, original claim declaration, historical scores or old events. Existing registration name trigger may also increment its revision.
5. Leave revoked account/API/key states and the rotated registration secret unchanged. Invalidate affected publication epochs, `touch`, audit `team.restore` with event ID, chosen name and before/after revision.
6. Unique/FK conflicts become 409 and roll back the full restore. Do not silently drop conflicted people or retry by replacing rows.

Regular team save/import paths reject recycled targets, including an attempted `status='active'`. Stale import fingerprints must invalidate after recycle/restore. Registration rotate, team code issue/rotate and API provision reject recycled teams; revoke remains safe/idempotent under existing behavior. This prevents bypassing the dedicated restore checks.

## 3. HTTP and frontend contract

Route the following exact paths in `server/app.py` before generic `/api/admin/data/` dispatch. Every new GET opens a read transaction; every POST uses existing `BEGIN IMMEDIATE`. Resolve actor and validate `X-GOTC-Login` inside that transaction. Existing same-origin/CSRF behavior remains. `GET` query fields must match exactly; POSTs accept no query parameters or unknown JSON fields.

| Method and path | Request | Success |
| --- | --- | --- |
| `GET /api/admin/data/team?teamId=T...` | teamId only | DetailResponse |
| `GET /api/admin/data/recycle-bin` | no query | `{teams:[RecycleSummary]}` newest deleted first, stable event ID tie-break |
| `POST /api/admin/data/team/recycle` | `{teamId,expectedRevision,recycleContext,reason}` | `{ok:true,team:DetailResponse.team,recycle:DetailResponse.recycle,access:AccessState}` |
| `POST /api/admin/data/team/restore` | `{teamId,expectedRevision,name,reason}` | same response; recycle is null |

```typescript
type Member={
  personId:string|null;
  name:string;
  school:string|null;
  role:'captain'|'member'|'unknown';
  source:'approved_membership'|'registration_captain'|'registration_member';
  schoolState:'recorded'|'needs_review'|'missing';
};
type MemberView={source:'approved_memberships'|'registration'|'none';members:Member[]};
type RestoreConflict={
  type:'name'|'member_assigned'|'identity_changed'|'identity_missing'|'registration_overlap';
  personId:string|null;
  memberName:string|null;
  teamId:string|null;
  teamName:string|null;
  message:string;
};
type RestoreAdvisory={
  type:'unverified_name_overlap';
  memberName:string;
  teamId:string;
  teamName:string;
  message:string;
};
type AccessState={submissionAccess:'inactive'|'not_issued';apiAccess:'inactive'|'not_provisioned';
  registrationAccess:'reissue_required'|'not_registered'};
type RecycleState={eventId:number;deletedAt:number;reason:string;originalStatus:string};
type DetailResponse={
  team:{id:string;number:number;name:string;project:string;track:string;status:string;
    statusReason:string;booth:string;roadshowOrder:number|null;revision:number;recycled:boolean};
  roster:MemberView;
  recycle:RecycleState|null;
  recycleContext:string|null; // 64-hex for a live team; null when already recycled
  restoreConflicts:RestoreConflict[];
  restoreAdvisories:RestoreAdvisory[];
};
type RecycleSummary={id:string;number:number;name:string;revision:number;recycle:RecycleState};
```

`AccessState` is returned only by successful recycle/restore where those inactive states are guaranteed; do not claim every live detail has inactive access. A detail response computes name conflicts against its returned `team.name` (the stored name). If the user supplies a replacement name, the POST recomputes conflicts against that submitted value. Keep the existing `{error,code}` error shape: on 409 preserve the submitted name/reason and show the error from that exact POST; a subsequent GET may refresh member conflicts/advisories but must not replace that error. Whenever the input name differs from the detail's `team.name`, hide typed `type:'name'` items from that GET instead of presenting old-name conflicts as the replacement-name result. If the input changes again after the POST, clear that attempt's name-specific error until another submit. No global API-error redesign is needed. `restoreAdvisories` is visually separate, labelled uncertain and never disables restore by itself.

Errors: 400 invalid field/type/unknown query; 401 missing/expired/mismatched session; 403 non-admin/delegated team; 404 missing team; 428 missing login marker, expectedRevision or recycleContext (`login_required` / `revision_required` / `recycle_context_required`); 409 `team_revision_conflict`, `team_context_conflict`, `team_recycled`, `team_not_recycled`, or `team_restore_conflict`. Do not expose details to unauthorized callers before the role/marker checks.

`GET /api/admin` default `teams` and submission-overview entries exclude recycled teams and provide integer `recycleCount`. Package A also filters the companion submission collection in `/api/judge` using the same current non-recycled predicate, so hidden teams do not retain exposed judge material entries. Existing current-directory count uses non-recycled teams. Existing admin historical data/export APIs continue to include preserved data; no public member fields are added. `/api/admin/registration` may keep archived entries for history, but exposes `recycled` to distinguish them and disables code rotation while archived.

Frontend changes stay in existing `platform-admin.js` and `platform.css`:

- Editing a row opens the existing team form, then loads private detail with a request sequence + `form.editSession`, selected team ID, routeVersion and login identity guard. Render “队员与学校” as a read-only accessible table. Loading/failure states must not display the previous team's roster.
- Disable delete until that selected team's detail/context has loaded successfully, and while refreshing it. Any save which changes the displayed revision invalidates the saved recycleContext until a fresh detail is reviewed. Capture teamId, expectedRevision, recycleContext and editSession together when submitting deletion. A 409 preserves the reason and requires rereading/reconfirming the current roster; never silently adopt a fresh context and automatically resubmit deletion.
- Add “删除队伍” beside save/clear only when a live existing team is selected. It opens an inline confirmation panel/dialog showing team number/name, a reason field and exact copy: “移入回收站后停止参赛并停用登录与 API；历史材料和成绩保留，同名队伍可重新申请。恢复时需检查队名和成员冲突，并重新发放访问凭据。”
- Add a “回收站（N）” directory tab/section with name, number, deletion time/reason and “查看并恢复”. Display snapshot members in the same read-only style. Restore panel offers name and reason; blocking conflicts and nonblocking unknown-school advisories are visually distinct. Apply the stored-name/attempted-name rule above to every refresh. Member conflicts do not show an overwrite/reassign button; an advisory alone never requires an identity confirmation or disables restore.
- Successful restore copy: “队伍已恢复。原提交码、API key 和登记管理码仍已停用；需要参赛访问时，请重发提交码、重新开通 API 并创建新 key；已有组队登记需重发管理码。” Adapt absent access types using AccessState and show restored inactive/disqualified status clearly.
- Reuse existing `withBusy`, `adminSaveForm` and `dirtyAdminForms` safety. Confirm before discarding unsaved team edits; mutations use displayed revision and ID captured before awaiting. A result cannot clear/overwrite another team’s later editor; route/login changes suppress late responses.
- Refresh directory and recycle count after success; clear only the successfully deleted editor if its editSession is still current. Never refresh the entire administrator page over other dirty forms. No localStorage persistence of new member details.

## 4. Work packages and ownership

One integration owner controls shared files. **Priority update:** urgent recovery code `c1172f1` is independently reviewed and included in local integration `6f82809` with `58e3eed` scoring changes; its operational publication/application is managed separately and is not verified by this planner. Preserve `reissueRevoked`, normal approved/issued states and normal claim_memberships. The earlier preserve-access/bound_existing design remains cancelled. Product implementation stays paused during that release. After the manager resumes it, Task 1 defines the recycle service; Task 2 uses its stable interfaces; Task 3 can build against the contract with a synthetic API. Task 4 follows integration. No automatic-review redesign is involved.

| Package / suggested owner | Allowed writes | Dependencies and boundary |
| --- | --- | --- |
| A — backend lifecycle + integration owner | `server/team_lifecycle.py`, `server/app.py`, `server/admin_data.py`, `server/admin_team_access.py`, `server/keys.py`, `server/test_team_lifecycle.py`, relevant existing test files for those paths | After release pause lifts, start first. Own all schema/app wiring, judging callback injection and app-level team/submission projections. No deployment. Publish interfaces before B starts callsites |
| B — approval/registration + stage executor | `server/claims.py`, `server/claims_auto.py`, `server/team_registration.py`, `server/judging.py`, `server/roadshow_scoring.py`, `server/finals.py`, `server/test_team_recycle_integrations.py` | After A interfaces fixed; may work in parallel with A once stubs/contract agreed. Auto-review change limited to candidate filtering. No writes to A-owned files |
| C — organizer UI executor | `platform-admin.js`, `platform.css`, `tests/admin-team-lifecycle.test.mjs`, `tests/admin-data.test.mjs` | After release pause lifts, can start against contract in parallel. Do not add assets or rewrite unrelated platform pages |
| D — verification/release reviewer | `server/test_team_lifecycle_release.py`, `server/README.md`, `deploy/README.md`, `.superpowers/sdd/2026-11-07-team-adjustment-recycle/verification.md` | After integrated code. Review can be read-only until defects need assigned ownership. No `deploy.py --execute`, no production mutation |

Manager chooses actual agents. A performs integration commits with explicit allowlists only after review. Planner makes no commit. If main moved, A resolves normal integration while retaining other changes; do not force merge PR #39 or mix unrelated finals/invitation work into this feature.

### Shared A/B judging contract and caller boundaries

`JudgingService` currently has no `self.s`. B adds the optional constructor keyword `is_team_recycled=None`, storing `self.is_team_recycled=is_team_recycled or (lambda db,team_id:False)` so standalone existing fixtures remain compatible. A alone wires `is_team_recycled=lambda db,team_id:self.team_lifecycle.is_recycled(db,team_id)` in `server/app.py`. The lambda is evaluated only for runtime requests after lifecycle schema initialization. B consumes this callback in `server/judging.py` and does not edit A-owned app.py. No shared `teams()` or `capture_team()` function receives a blanket filtering/throwing rule: both must continue serving administrator history and snapshot capture.

- `set_roster`: before writes, reject any explicitly selected recycled ID with 409 `team_recycled`; preserve existing freeze/version rules.
- `judge_view`, public `board` and `save_review`: filter/guard recycled IDs at these active-operation callers only; administrator `admin_view`, raw stored roster and old review history keep all rows.
- `reset`: read the old complete roster, then skip recycled IDs only when copying into the new run. Preserve the old run, roster, snapshots and review history byte-for-byte. If all old IDs are recycled, creating an empty closed new run is allowed so the organizer can choose a new roster; opening it is not allowed.
- `set_window(open=True)`: before changing deadlines, frozen_at or revisions, require at least one non-recycled roster entry in addition to existing stage/active-judge requirements. If none, return 409 `judging_no_available_teams` with no state change. This adds a recycle-specific gate, not a new policy for ordinary withdrawn/disqualified teams. On first open of a mixed stale roster, capture fresh materials only for non-recycled entries; preserve recycled entries' existing snapshot JSON as historical data and omit them from live targets. On reopening an already frozen run, update no snapshots and apply the same live-target gate. Closing remains possible even when no live targets remain. Deletion during an already-open round does not reopen, extend or automatically close its window; live targets immediately exclude the recycled team.
- `server/roadshow_scoring.py` now has `POST /api/admin/roadshow/teams` / `save_teams` and `availableTeams` through `finals.teams`, introduced by the integrated scoring changes. B excludes recycled choices and rejects direct stale selection there while preserving administrator historic summary rows. Do not globally remove those rows from the shared historical team representation.

### Task 1 — Add reversible lifecycle, private members, and protected endpoints

**Files:** package A above. **Consumes:** existing `AdminDataService.team`, `Claims.normalize`, `EventService.audit/touch/db`, account/registration/key schemas. **Produces:** TeamLifecycle and HTTP contracts in sections 2–3.

- [ ] Add synthetic tests in `server/test_team_lifecycle.py` reusing `test_api.APITests.setUp/tearDown/team` and Client, and `test_open_claims.OpenClaimsTests` request/approval helpers. Test fixtures use only strings such as “Synthetic Captain” and “Synthetic School B”. Store data under `TemporaryDirectory`, not repository `.runtime`.
- [ ] Establish these failing API assertions before implementation:

```python
def admin_request(self,method,path,body=None):
    login_id=self.admin.get('/api/session')[1]['loginId']
    return self.admin.request(method,path,body,headers={'X-GOTC-Login':login_id})

# Within a test after creating a registration-only synthetic team:
status,detail,_=self.admin_request('GET','/api/admin/data/team?teamId='+team_id)
self.assertEqual(status,200,detail)
self.assertEqual(detail['roster']['members'][1]['school'],None)
self.assertNotIn('phone',json.dumps(detail))
```

The existing Client signature is `request(method,path,payload=None,raw=None,origin=True,headers=None)`; its `get()`/`post()` convenience methods do not accept explicit headers. Use the helper above for successful lifecycle requests and explicit `request()` calls for missing/stale-marker tests.

- [ ] Run `python3 -B -m unittest discover -s server -p 'test_team_lifecycle.py' -v`; expect missing-endpoint/schema failures first, not auth fixture failures.
- [ ] Implement additive tables and `member_view` exact-ID source resolution, then protected detail/list endpoints. Initialize before exposing service; use explicit SELECT/JSON allowlists and no secret fields.
- [ ] Implement the deletion/restore algorithms in section 2. Test both revision and recycleContext preconditions under the same writer lock. Keep all history counts identical except new lifecycle/audit rows, intentional `claim_memberships` release/reinsert, and revoked sessions. Inject a late exception in restore/recycle tests and assert a full DB rollback.
- [ ] Guard regular save/import, ordinary/delegated team binding, issue/rotate credentials and API provision while recycled. Keep general withdrawn/disqualified behavior unchanged where not required for recycle.
- [ ] Exclude tombstoned teams from default admin team/submission lists and the ordinary judge's companion submissions collection. Add recycle count without dropping archived records from historical exports. Wire the agreed `is_team_recycled(db,team_id)` constructor callback for B; do not add implicit global filtering to shared history readers.
- [ ] Run focused lifecycle/admin/workbench/key checks; expect every listed suite to pass:

```sh
python3 -B -m unittest discover -s server -p 'test_team_lifecycle.py' -v
python3 -B -m unittest discover -s server -p 'test_admin_data.py' -v
python3 -B -m unittest discover -s server -p 'test_admin_team_access.py' -v
python3 -B -m unittest discover -s server -p 'test_keys.py' -v
```

- [ ] Hand B the concrete helper signatures and report schema/interface drift before changing the shared contract. Request independent review of transaction/privacy boundaries. Commit only A's explicit files when authorized by the integration workflow.

### Task 2 — Release names/people and exclude recycled competition targets

**Files:** package B above. **Consumes:** lifecycle `is_recycled`, `require_available`, `name_collisions`, and A's injected JudgingService `is_team_recycled(db,team_id)` callback. **Produces:** same-name reapproval and restoration safety across current entry points, unchanged automatic eligibility policy. Preserve the reviewed `reissueRevoked` manual recovery branch and its source/test contracts.

- [ ] Add a focused synthetic scenario: create+approve old team, record member IDs/history, recycle, submit a new application with the same normalized name and people, approve, then restore old team. Expected: approval succeeds for a distinct ID; restore gets 409 with old record unchanged. A changed restore name does not bypass member ownership.
- [ ] Add the complementary scenario with different people: same name may be approved while old team is recycled; restoring old name fails, restoring with a free replacement name succeeds with original ID/number/history. Test NFKC/casefold/whitespace equivalence and ordinary withdrawn/disqualified collision behavior.
- [ ] Run `python3 -B -m unittest discover -s server -p 'test_team_recycle_integrations.py' -v`; expect failures at current collision/qualitative-review paths before edits.
- [ ] Change manual approval to lifecycle name candidates and require non-recycled selected team. Registration duplicate names/members exclude current recycle events only; registration create's 200-team count excludes recycled rows, but free-number allocation still counts all preserved team numbers. Handle number exhaustion with 409, not StopIteration.
- [ ] In `claims_auto.py`, replace only the collision candidate source with `name_collisions`; do not change trusted source storage, summary/configure, person eligibility, declaration matching, or trigger timing. Verify trusted config/table bytes and revision remain unchanged after recycle/restore.
- [ ] Guard registration-management-code rotation while recycled. Existing public authorization is already status-gated; keep old code invalid after restore by Task 1 rotation. Approved current-member synchronization remains unchanged.
- [ ] Implement the caller-specific judging rules in the shared A/B contract: direct roster selection rejects recycled IDs; reset skips them only in the new run; mixed stale first-open roster retains historical snapshots but excludes recycled live targets; zero non-recycled targets blocks opening without state changes. Allow empty closed reset results and closing an already empty-live-target round. Keep shared teams/capture_team and all admin history complete.
- [ ] Numeric roadshow/finals already exclude status-withdrawn teams from scoring/nomination validity. Add caller-specific lifecycle filtering to current selectable/public lists, including existing `/api/admin/roadshow/teams` `save_teams`/`availableTeams`, while keeping admin historic summary rows. Ensure public filtering cannot remove history in storage. Trigger publication invalidation on recycle and restore, including already-withdrawn cases.
- [ ] Run these focused existing suites and the new suite; all must pass without changing expected automatic approval behavior except recycled-name cases:

```sh
python3 -B -m unittest discover -s server -p 'test_team_recycle_integrations.py' -v
python3 -B -m unittest discover -s server -p 'test_open_claims.py' -v
python3 -B -m unittest discover -s server -p 'test_auto_claims.py' -v
python3 -B -m unittest discover -s server -p 'test_registration_claim_consistency.py' -v
python3 -B -m unittest discover -s server -p 'test_team_eligibility.py' -v
python3 -B -m unittest discover -s server -p 'test_judging.py' -v
python3 -B -m unittest discover -s server -p 'test_roadshow_scoring.py' -v
python3 -B -m unittest discover -s server -p 'test_scoring_team_rosters.py' -v
python3 -B -m unittest discover -s server -p 'test_finals.py' -v
```

- [ ] Return a file allowlist and measured scope diff to A. Highlight any change outside name candidate filtering in `claims_auto.py` for rejection/review.

### Task 3 — Member view, delete confirmation, recycle and restore UI

**Files:** package C above. **Consumes:** exact API schema/error codes in section 3; existing `api`, `adminSaveForm`, dirty-form/request guards. **Produces:** browser actions requiring no manual API calls.

- [ ] Add VM/DOM harness tests in `tests/admin-team-lifecycle.test.mjs`, modeled on `tests/admin-data.test.mjs`, with synthetic team/member API responses and controllable deferred promises.
- [ ] Assert displayed fields and outgoing requests (no member edits in save):

```javascript
assert.match(rosterMarkup,/Synthetic Captain/);
assert.match(rosterMarkup,/学校待补充/);
assert.ok(!rosterMarkup.includes('Synthetic School A</td><td>Synthetic Member'));
assert.deepEqual(recycleRequest,{teamId:'T_SYNTHETIC',expectedRevision:3,recycleContext:'c'.repeat(64),reason:'Synthetic duplicate'});
assert.deepEqual(restoreRequest,{teamId:'T_SYNTHETIC',expectedRevision:4,name:'Synthetic Renamed',reason:'Synthetic restore'});
assert.ok(!Object.hasOwn(teamSaveRequest,'members'));
```

Use exact structural cell assertions for each member instead of depending only on substring ordering. Test `<script>`/attribute payloads in team/name/school/reason through `esc` and literal text rendering.

- [ ] Run `node --test tests/admin-team-lifecycle.test.mjs`; expect missing renderer/handler failures first.
- [ ] Add member loading/read-only table, safe empty/pending/source labels and request freshness guards. Add delete confirmation with required reason plus the same detail's revision/context. Loading/error/stale-context states disable delete; cancellation performs zero POSTs; a context-conflict 409 requires explicit fresh read and reconfirmation, never auto-retry.
- [ ] Add recycle list and restore panel, rename field and collision explanation. Prevent duplicate clicks and preserve inputs on 409. Hide GET-derived stored-name conflicts when the editor contains a replacement name; show the exact failed POST's error without replacing it on refresh, and clear that name-specific attempt message when its input changes. Render unknown-school name overlaps as nonblocking uncertainty advisories. Put the full access-restoration notice beside success; do not imply old codes resume.
- [ ] Test A→B team switching during a delayed member GET, clear form during pending deletion, newer editSession during response, route change, and cross-tab login change. The late response must neither show A's people in B nor clear B's edits.
- [ ] Run `node --test tests/admin-team-lifecycle.test.mjs tests/admin-data.test.mjs tests/admin-team-workbench.test.mjs tests/page-session-ownership.test.mjs`; expect all tests to pass. `node --check platform-admin.js` must pass.
- [ ] Provide A with only C-owned changes. Do not change `platform-claims.js`, login storage, or automatic-review UI.

### Task 4 — Integration verification, demo and release readiness

**Files:** package D above. **Consumes:** integrated A/B/C changes and matrix in section 5. **Produces:** evidence report, additive-upgrade/rollback assessment, synthetic browser demonstration.

- [ ] Recheck `git status --short`, explicit diff allowlist and active HEAD. Review migration init order against fresh and prior schema, foreign key integrity, unchanged existing column layouts, repeat initialization and restart durability.
- [ ] Create `server/test_team_lifecycle_release.py` with a synthetic old-layout DB; initialize new code twice, recycle/restore, restart, and assert membership events persist. Use `PRAGMA foreign_key_check` and `PRAGMA integrity_check`. Assert no old-table drop/rename/rebuild or unknown data changes.
- [ ] Review all matrix rows below. Reproduce atomic races with two independent DB connections/clients (not sleeps): delete versus save, restore versus new approval, double delete, double restore, approval versus deletion of a selected preregistration. For the gateway, prove queued requests rechecked after deletion fail; do not assert cancellation of a request already past final recheck. Do not call real providers or hold a database lock around provider I/O.
- [ ] Run repository-required checks once integration is stable:

```sh
node --test tests/*.test.mjs
python3 -B -m unittest discover -s server -p 'test_*.py' -v
python3 -B scripts/check_site.py
python3 -B -m unittest discover -s deploy -p 'test_*.py' -v
git diff --check
```

CI additionally covers supported Python 3.11/3.12 and repository hygiene. If loopback binding is sandbox-denied, rerun through the approved local test mechanism; report it as environment failure until tests actually run. Do not substitute source inspection for an HTTP/browser pass.

- [ ] Start an isolated local server with temporary data and synthetic admin/team credentials. Use browser automation skill to test `platform.html#admin` at desktop and narrow mobile width: select approved team with mixed schools; select registration-only team; confirm empty roster; cancel deletion; delete; find recycle; approve same-name replacement using existing claims UI; attempt blocked restore; restore a nonconflicting team with renamed name; observe disabled old credentials. Capture only synthetic screenshots in temporary/verification output, never personal data or secrets.
- [ ] Verify ordinary team, judge, audience and delegated admin cannot read lifecycle/member endpoints; unauthorized HTML/JSON must not contain synthetic member strings. Confirm member detail is absent from public board/registration/judging snapshots, apart from already-public registration fields intentionally unchanged.
- [ ] Record check commands, counts, tested HEAD and limitations in `verification.md`. Independent reviewer confirms no automatic-review expansion, identity edits, remote calls, hard delete, or unrelated changes.
- [ ] Update server/deploy documentation with recycle, restore, credential follow-up and rollback floor. Present reviewable implementation to manager; only normal protected-branch review/merge and separately authorized deployment may follow.

## 5. Acceptance matrix

| Area | Synthetic scenario | Required result |
| --- | --- | --- |
| Approved roster | 3–5 approved identities from three schools, same-name people at different schools | Exact assigned IDs/name/school; captain correctly marked; no name inference |
| Recovered-account binding | Five approved memberships attached to the original recovered account/team | All five shown with correct captain before/after receipt issuance; delete/restore preserves request history and never invokes receipt redemption or credential issuance |
| Registration roster | Captain plus 2–4 names, only captain school available | Captain school labelled registration source; every other school pending |
| Missing/flagged source | No records; a recorded “待核对” school; needsReview identity | Honest empty/pending labels; no fabricated school |
| Privacy | Public/team/judge/audience/delegated identity, correct/wrong/missing marker | 401/403/428 as contract; no member/phone/notes/secret leakage |
| Recycle | Approved team with account, API grant, active keys, delegated session, registration and stage records | Tombstone + revision; current membership released; account/grant/keys inactive; all target sessions invalid; unrelated admin sessions remain valid |
| History | Hash/snapshot team submissions, score/vote rows, rosters, claims, repositories and API usage before delete | Same stored records/IDs afterwards; only explicit lifecycle/access/assignment changes |
| Name reuse | Recycle old normalized name, approve fresh same-name application | Distinct ID/new number succeeds; ordinary withdrawn/disqualified duplicate still blocks |
| Member reuse | Recycle old approved team, approve same people in new team | Existing claim_people reused; only new current membership owns each person; old snapshot intact |
| Restore success | No collision; repeat delete/restore cycle; originally withdrawn/disqualified team | Same ID/number/members; original state preserved; history accumulates; no credential resurrection |
| Restore name conflict | Name taken by new team; submit replacement name which also conflicts, then a free name | Old/new-name POSTs report their own errors without writes; GET old-name conflicts are hidden while editing replacement; input retained; free name works if member checks pass |
| Restore member conflict | Any old member assigned elsewhere, missing identity, changed normalized identity | Whole restore 409; no overwritten assignment/identity and no partial members |
| Registration overlap | Existing same-registration-school duplicate or known exact identity assigned elsewhere | Existing policy/identity hard conflict preserved; no inferred school used to claim identity |
| Unknown-school name overlap | Archived registration member has no school and same name as a live approved person | Only uncertain nonblocking advisory; restore can proceed if no hard conflict; no new person assignment, invented school or modification to other team |
| Old credentials | Old submission/login/API/registration code after delete, after restore and after reissue | Invalid throughout; newly issued credentials work only after authorized enabling |
| Ordinary team paths | Existing/stale session hitting team, submission, score detail, keys, repository preview/history/download | No access for recycled team; read and write guards apply after lock/recheck |
| Competition paths | Professional score/vote, qualitative review, roadshow/finals score/nomination direct stale POST | Recycled team is not an active target; no history mutation |
| Stage reset/open | Frozen mixed/all-recycled roster; first open after deletion; reopen/close with zero non-recycled IDs | New reset skips recycled IDs, old snapshots remain; empty closed reset allowed; opening empty live target set returns 409 without writes; mixed first-open preserves archived snapshots; closing remains allowed |
| Existing roadshow team editor | Stale direct POST `/api/admin/roadshow/teams` and current availableTeams after recycle | Selection excludes/rejects recycled IDs; administrator historical summary remains visible; existing scoring-roster suite passes |
| Gateway boundary | Delete while a call waits for a slot or after its final access recheck | Recheck after committed deletion rejects; already-admitted call may finish/be billed; no network-instant cancellation assertion |
| Publication | Previously published roster; delete active or already-withdrawn team; restore | Publication invalidated; admin must explicitly republish; frozen data unchanged |
| Re-enable bypass | POST normal team save active, CSV import, issue/rotate code, provision API on recycled ID | Rejected; dedicated conflict-checked restore is the only lifecycle return |
| Concurrency | Two admins same revision; restore/new approval; stale import; deletion while repository preflight runs | One serialized outcome, loser 409/401; no overwrite, partial history or leaked authority |
| Recovery versus stale delete | Load empty-team detail, complete c117 reissueRevoked five-person binding without changing team revision, submit the old recycle context | 409 team_context_conflict; no deletion, credential or membership mutation; fresh members must be read and reconfirmed |
| Context-bound delete UI | Detail loading/failure/refresh, save changed revision, and context-conflict response | Delete disabled without matching detail; ID/revision/context/editSession submitted together; reason retained and no silent context swap/retry |
| UI race/XSS | Switch team/route/login during delayed GET/POST; malicious names/schools/reasons | Correct editor remains; inputs retained; escaped content; single mutation per click |
| Regression | Existing auto-review enabled/disabled trusted fixture; existing non-recycled approval/registration/login/tests | Same rules and config; only recycled candidates filtered; all existing suites pass |
| Migration/release | Existing synthetic DB initialized twice, new release package inspection, restart | Additive schema, durable histories, no credentials/data in package, integrity checks pass |

## 6. Release and migration assessment

Risk is moderate: the schema is additive, but deletion changes live membership and access. Do not treat this as a frontend-only deploy. Use the existing installer database backup and standard health checks; it already includes new `server/*.py` modules and the existing frontend assets. No production data backfill, roster export or secret import is needed.

Before the first real lifecycle mutation, the old binary can still operate with the unused additive tables. After a team has been recycled, pre-feature binaries do not understand tombstones or released membership history and may let ordinary save/reactivation bypass restore checks. Therefore **rollback after first use must retain lifecycle-aware code**; do not blindly switch to the old release or restore an old full database over newer event activity. Document this minimum release floor and use a forward fix or a reviewed compatibility rollback. The general installer health rollback is not evidence that post-use data semantics are safe.

Any new release must pass local synthetic acceptance and independent review, then current repository CI/protected-branch approvals. PR #39's parent-reported open/write-only state and earlier CI are not proof of current mergeability or of this feature's tests. Executor checks actual state before integration; no administrator bypass. Production smoke should initially be read-only. Mutating production tests require a specifically authorized designated synthetic production team, never an arbitrary real team.

## 7. Plan review checklist

- [x] Shows member names and schools with truthful source/unknown labels; no identity edit added.
- [x] Delete control is reversible and stops current participation/access.
- [x] Recycled names and live member assignments do not block new approval.
- [x] Recovery keeps history and refuses name/member conflicts without overwriting new teams.
- [x] Restore UI explicitly explains fresh credentials/API enablement.
- [x] Automatic-review scope limited to recycle-aware name candidates; old cancelled work stays cancelled.
- [x] APIs, files, owners, dependencies, synthetic tests and browser path specified.
- [x] Publication, privacy, stale form, transaction and rollback hazards identified.
- [x] Independent reviewer approved the revised contract and minimal scope on 2026-11-07; implementation still waits for the manager to lift the operational release pause.

This planner authored only this plan and `.superpowers/sdd/2026-11-07-team-adjustment-recycle/` investigation records; no product/test code or Git commit was made.
