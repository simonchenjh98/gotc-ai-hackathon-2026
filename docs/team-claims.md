# 队伍申请与线上批准

所有参赛者都可以在 `platform.html#claim` 填写队名、队长与 2–4 名组员的姓名和学校。赛道可选已公布的三条赛道，也可留为待定。空数据库无需导入名单、预建队伍或开放 API 额度即可接受申请。提交只保存待审核申请，不创建人员、队伍或登录账号。

提交后，申请自动进入组织方后台。队长保留当前标签页的领取凭据，在本页查询状态即可，无需私信、交换核验码或到场。组织方在后台「队伍申请审核」读取申请，审核申报成员与相关私有身份参考，可修改队名或赛道，然后点击批准或拒绝。批准表示组织方基于申报信息作出的审核决定，不表示已核实本人到场。默认在同一写事务中分配 1–999 中最小的未使用队号，创建有效队伍、登记或复用人员、绑定成员并发放提交码领取权限。失败全部回滚。队长查询状态后点击领取；重复领取保持同一码。此流程不自动开通 API 网关权限或额度。

如本队已有历史组队登记，组织方可以在「归队方式」主动选择已有队伍。此项可选，默认仍为新建，系统不会按队名自动绑定。候选只包含有效且未发提交码、未绑定领取申请、未绑定已批准成员的队伍。选择后展示登记队长、学校、成员、版本和项目；批准明确确认该登记属于本申请，并按审核通过的申报更新登记队长、学校与成员。原队伍 ID、队号、项目、来源及原始记录保留；原参赛码可统一本队资料与参赛功能，已有有效队伍管理码仍仅保留原登记资料修改能力。队长及学校不变时保留联系电话，变更时清空旧电话并将原联系信息保存到私有审计。人数同步为实际批准人数，已确认的成员与人数不能由参赛者自行改写。新建路径遇到规范化后相同的队名会拒绝，请主动选择已有队伍或填写可区分的队名。

私有参考记录仅用于去重，不决定报名资格。同名同校且未归队的记录会复用；已有队伍的同名同校记录会阻止重复归队。同名但不同学校且已归队时，组织方必须区分相关人员，勾选确认并填写说明。旧记录的待核实标记是参考，不要求新参赛者先导入或改写名单。原始记录与备注保留，参考管理和导入入口收在折叠区域内。

申请待审核时，队长可以修改队名、赛道和组员。冲突或保存结果不明时，页面保留草稿，必须读取已保存内容、人工核对并明确确认后才可再次提交。已批准或领码的申请不可改写。旧版无组员的申请须由原领取页面补充；已有组员但没有队名或赛道的旧申请可由组织方补齐。旧请求或更新载荷未包含队伍信息时，不会删除既存队伍信息。

管理员列表提供每份申请的 `reviewContext`，包含申请版本和相关同名参考记录的快照摘要。`POST /api/admin/claims/approve` 提交 `requestId`、`expectedRevision`、`reviewContext`、`teamName`、`track`、`verificationMethod: "online"`、`applicationReviewed: true`、`resolvedIdentity`、`note`。普通审核备注可为空，同名冲突仍须明确区分并填写说明。点击批准才会发送审核意图。批准事务重新验证上下文；相关记录有变化时要求刷新核对，无关人员的变化不会阻塞申请。选择已有队伍时另传 `teamId` 与列表中的 `teamContext`；事务内同时核对完整队伍和登记快照及资格，任何相关编辑、改名、发码或归队都要求重新读取核对。选择与其快照包含在幂等摘要内，相同批准载荷可安全重试，包括领码之后。旧版 `pin` + `inPersonVerified` 载荷仍按原流程校验，不能与线上审核字段混用；已有领取凭据不失效。审计区分 online 与 onsite，线上批准不记作到场核验。现有 `/api/admin/claims/review` 继续支持旧版名单映射客户端与拒绝操作。

公众只能通过自身 bearer 凭据读取本申请、申报信息和获批队伍，不能查询私有身份、身份匹配结果或管理员上下文。管理员列表不返回 PIN、领取凭据或提交码。现有身份、登录标记、同源写入、请求大小和速率限制继续适用。

## 原参赛码统一本队资料

已有参赛码（原提交码）的队伍直接登录 `platform.html#team`，进入「本队资料与成员」（`#team-profile`）；作品提交和 `#team-score` 共用同一个原登录。原账号、参赛码、队伍和会话不会因这次功能更新重建或轮换。主办方后台仍独立；管理员代管会话不能使用此新增的参赛者资料接口。

`GET /api/team/profile` 只读返回当前登录队伍的 metadata、`profile`（原登记八个字段）、本队成员与实际学校、`revision`、`teamRevision`、`membersEditable`、`projectEditable`、`incomplete` 和只读 `listedPublicly`。不接受目标队伍 ID、账号 ID、管理码或查询参数。`POST` 仅接受 `{team: profile, expectedRevision, expectedTeamRevision}`，须带匹配当前 cookie 的 `X-GOTC-Login`；两个版本都在同一事务中核对。已审核的队长、学校、全体成员与人数保持锁定；成员变更请联系组织方。资料页不能改写已正式提交的项目名称，作品修改仍须遵守原作品流程、版本与截止规则。学校未知时明确显示待补充或待核对，不推断。

没有登记行的队伍，读取资料不写数据库；首次合法保存仅为原队伍建立资料行，不另建队伍或账号。已审核人员来自原精确成员记录和关联申请；缺少可靠队长资料时只读提示核对。新增私有资料在 `team_profile_visibility` 中明确记为不公开，保存、审核同步和恢复不会将其自动加入公开组队名单；既存公开登记范围保持不变，手机号码不公开。

已有有效管理码仍可通过原 `/api/registration/open|save` 读取和修改其既有登记行，保持原 43 字符 hash 校验、官方来源 CORS、无 Cookie 和限速规则。回包不包含新 profile 的私有人员 ID 或队友个体学校，不建立参赛会话，不授予作品、查分、AI 或后台权限；已作废/回收队伍的旧码不会自动恢复。两入口共用资料修订和原审核锁、正式项目保护及私有目录标记。旧码存储保留，冲突需显式重读；详情见 `team-registration.md`。

公开创建新码 `/api/registration/create` 和管理员重发入口仍为 410；导入仍不发放管理码。新申请继续通过 `#claim`。兼容旧码不代表可以回滚旧二进制：回滚必须保留私有目录过滤和现有权限/冻结保护，旧版本可能把新增私有资料公开；不得仅凭无破坏性迁移就回滚旧程序或还原整库。

## 管理员审核已停用提交身份并重发

`GET /api/admin/claims` 的 `revokedTeams` 单独列出可审核重发的队伍；普通 `teams` 和自动审核的候选规则保持原样。候选须为有效队伍，已有唯一且停用的 team 账号，没有关联申请或已批准成员。其 `teamContext` 同时核对队伍、登记和停用账号快照；账号曾重发后再停用时，旧上下文也不能使用。

管理员读取最新申请与候选后，向原 `POST /api/admin/claims/approve` 提交以下结构（标识及摘要均为合成示例）。仍须使用当前管理员 cookie、同源请求与 `X-GOTC-Login`：

```json
{"requestId":"00000000000000000000000000000000","expectedRevision":1,"reviewContext":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","teamId":"synthetic-team","teamContext":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","teamName":"Synthetic retained team","track":"","verificationMethod":"online","applicationReviewed":true,"resolvedIdentity":false,"note":"已核对原停用身份及全部申报成员，审核重新发码","reissueRevoked":true}
```

`reissueRevoked` 必须为布尔 `true`，必须明确选择队伍并提交上下文，只接受线上管理员审核且备注非空；`teamName`、`track` 必须与候选原值完全相同。此路径保留队伍 ID、队号及项目等元数据，按原审核校验绑定申报成员，在同一事务中重新生成原账号凭据、清除旧会话，并将申请设为 `approved`。响应仅返回 `ok`、`status`、`revision`，不返回临时码。审核审计仍为 `claims.approve-open`，增加 `reissueRevoked: true` 与 `accountId`，同时保留 `team-access.rotate` 审计。失败整笔回滚；相同批准载荷重试不会再次重发。

队长继续使用原领取凭据调用原 `/api/claims/status` 和 `/api/claims/redeem`，由正常领取流程得到新的可登录提交码。旧码保持失效；重复领取返回同一新码。上下文、身份或占用冲突返回 409，需重新读取核对；错误的审核字段返回 400。管理员列表和审计均不含领取凭据、PIN 或提交码。此入口不会更改自动审核开关、名单或规则，也不会开通 API 额度。

## Trusted relay automatic approval

Automatic review defaults to disabled. Only the current authenticated administrator can import or enable a separate trusted snapshot. Public registration and ordinary `people` imports do not confer eligibility. The captain and every member (3–5 people total) must each have exactly one eligible, complete name+school match in the snapshot, using NFKC, case folding and whitespace normalization only. Duplicate pairs remain ambiguous, including when one duplicate is ineligible. No aliases or fuzzy matching are applied. Matching data does not prove physical identity.

Use the protected `POST /api/admin/claims/roster` ingress with this alternate complete replacement contract (synthetic example):

```json
{"autoReview":{"expectedRevision":0,"sourceName":"Synthetic relay","sourceSha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","entries":[{"id":"synthetic-1","name":"Example Captain","school":"Example School","eligible":true,"notes":""}]}}
```

`entries` accepts 0–500 unique IDs, each with exactly the five shown fields. Empty names/schools are retained but never qualify; duplicate normalized identity pairs are retained but never qualify. The source digest identifies the organizer's source file; it is not a computed digest of the JSON transport. Import is atomic, increments the configuration revision, and always disables automatic review. The existing `{people:[...]}` contract is unchanged. The HTTP import limit remains 2 MiB.

`POST /api/admin/claims/auto-review` accepts exactly `{"expectedRevision":1,"enabled":true}`. Every change requires the current revision and increments it; enabling requires at least one complete unique eligible entry. `GET /api/admin/claims` returns a private `autoReview` summary with revision, enabled, sourceName, sourceSha256, count, eligibleCount and ambiguousCount. No public roster lookup or individual match hints exist.

New applications, pending edits and valid pending bearer status/redemption checks may attempt automatic approval. Rejected, expired and processed receipts are unchanged. Approval uses the existing atomic team/membership/account pipeline and frozen declaration; ordinary conflicts roll back to pending manual review. Snapshot revision, source digest and matched source IDs are recorded in a distinct `claims.approve-auto` audit event. The ordinary bearer redemption and credential fingerprint checks still govern delivery. Automatic approval does not provision API access.

An existing name collision is reusable only for one active, unissued eligible registered team with the exact normalized team name, captain name/school and full member-name multiset. Registration collects only the captain school; teammate schools are established by the trusted snapshot. A name spanning different schools in the trusted snapshot leaves existing-registration reuse manual because the registration cannot distinguish those identities. Reuse preserves the team ID, project and original records, and applies authoritative membership/contact/target-size synchronization. Existing valid registration credentials retain only their original profile-edit capability. Other collisions remain manual.
