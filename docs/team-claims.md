# 队伍申请与线上批准

所有参赛者都可以在 `platform.html#claim` 填写队名、队长与 2–4 名组员的姓名和学校。赛道可选已公布的三条赛道，也可留为待定。空数据库无需导入名单、预建队伍或开放 API 额度即可接受申请。提交只保存待审核申请，不创建人员、队伍或登录账号。

提交后，申请自动进入组织方后台。队长保留当前标签页的领取凭据，在本页查询状态即可，无需私信、交换核验码或到场。组织方在后台「队伍申请审核」读取申请，审核申报成员与相关私有身份参考，可修改队名或赛道，然后点击批准或拒绝。批准表示组织方基于申报信息作出的审核决定，不表示已核实本人到场。默认在同一写事务中分配 1–999 中最小的未使用队号，创建有效队伍、登记或复用人员、绑定成员并发放提交码领取权限。失败全部回滚。队长查询状态后点击领取；重复领取保持同一码。此流程不自动开通 API 网关权限或额度。

如本队已在组队登记页面创建队伍，组织方可以在「归队方式」主动选择已有队伍。此项可选，默认仍为新建，系统不会按队名自动绑定。候选只包含有效且未发提交码、未绑定领取申请、未绑定已批准成员的队伍。选择后展示登记队长、学校、成员、版本和项目；批准明确确认该登记属于本申请，并按审核通过的申报更新登记队长、学校与成员。原队伍 ID、队号、项目、来源、原始记录与管理码均保留；队长及学校不变时保留联系电话，变更时清空旧电话并将原联系信息保存到私有审计。人数同步为实际批准人数，已确认的成员与人数不能通过登记页改写。新建路径遇到规范化后相同的队名会拒绝，请主动选择已有队伍或填写可区分的队名。

私有参考记录仅用于去重，不决定报名资格。同名同校且未归队的记录会复用；已有队伍的同名同校记录会阻止重复归队。同名但不同学校且已归队时，组织方必须区分相关人员，勾选确认并填写说明。旧记录的待核实标记是参考，不要求新参赛者先导入或改写名单。原始记录与备注保留，参考管理和导入入口收在折叠区域内。

申请待审核时，队长可以修改队名、赛道和组员。冲突或保存结果不明时，页面保留草稿，必须读取已保存内容、人工核对并明确确认后才可再次提交。已批准或领码的申请不可改写。旧版无组员的申请须由原领取页面补充；已有组员但没有队名或赛道的旧申请可由组织方补齐。旧请求或更新载荷未包含队伍信息时，不会删除既存队伍信息。

管理员列表提供每份申请的 `reviewContext`，包含申请版本和相关同名参考记录的快照摘要。`POST /api/admin/claims/approve` 提交 `requestId`、`expectedRevision`、`reviewContext`、`teamName`、`track`、`verificationMethod: "online"`、`applicationReviewed: true`、`resolvedIdentity`、`note`。普通审核备注可为空，同名冲突仍须明确区分并填写说明。点击批准才会发送审核意图。批准事务重新验证上下文；相关记录有变化时要求刷新核对，无关人员的变化不会阻塞申请。选择已有队伍时另传 `teamId` 与列表中的 `teamContext`；事务内同时核对完整队伍和登记快照及资格，任何相关编辑、改名、发码或归队都要求重新读取核对。选择与其快照包含在幂等摘要内，相同批准载荷可安全重试，包括领码之后。旧版 `pin` + `inPersonVerified` 载荷仍按原流程校验，不能与线上审核字段混用；已有领取凭据不失效。审计区分 online 与 onsite，线上批准不记作到场核验。现有 `/api/admin/claims/review` 继续支持旧版名单映射客户端与拒绝操作。

公众只能通过自身 bearer 凭据读取本申请、申报信息和获批队伍，不能查询私有身份、身份匹配结果或管理员上下文。管理员列表不返回 PIN、领取凭据或提交码。现有身份、登录标记、同源写入、请求大小和速率限制继续适用。

## Trusted relay automatic approval

Automatic review defaults to disabled. Only the current authenticated administrator can import or enable a separate trusted snapshot. Public registration and ordinary `people` imports do not confer eligibility. The captain and every member (3–5 people total) must each have exactly one eligible, complete name+school match in the snapshot, using NFKC, case folding and whitespace normalization only. Duplicate pairs remain ambiguous, including when one duplicate is ineligible. No aliases or fuzzy matching are applied. Matching data does not prove physical identity.

Use the protected `POST /api/admin/claims/roster` ingress with this alternate complete replacement contract (synthetic example):

```json
{"autoReview":{"expectedRevision":0,"sourceName":"Synthetic relay","sourceSha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","entries":[{"id":"synthetic-1","name":"Example Captain","school":"Example School","eligible":true,"notes":""}]}}
```

`entries` accepts 0–500 unique IDs, each with exactly the five shown fields. Empty names/schools are retained but never qualify; duplicate normalized identity pairs are retained but never qualify. The source digest identifies the organizer's source file; it is not a computed digest of the JSON transport. Import is atomic, increments the configuration revision, and always disables automatic review. The existing `{people:[...]}` contract is unchanged. The HTTP import limit remains 2 MiB.

`POST /api/admin/claims/auto-review` accepts exactly `{"expectedRevision":1,"enabled":true}`. Every change requires the current revision and increments it; enabling requires at least one complete unique eligible entry. `GET /api/admin/claims` returns a private `autoReview` summary with revision, enabled, sourceName, sourceSha256, count, eligibleCount and ambiguousCount. No public roster lookup or individual match hints exist.

New applications, pending edits and valid pending bearer status/redemption checks may attempt automatic approval. Rejected, expired and processed receipts are unchanged. Approval uses the existing atomic team/membership/account pipeline and frozen declaration; ordinary conflicts roll back to pending manual review. Snapshot revision, source digest and matched source IDs are recorded in a distinct `claims.approve-auto` audit event. The ordinary bearer redemption and credential fingerprint checks still govern delivery. Automatic approval does not provision API access.

An existing name collision is reusable only for one active, unissued eligible registered team with the exact normalized team name, captain name/school and full member-name multiset. Registration collects only the captain school; teammate schools are established by the trusted snapshot. A name spanning different schools in the trusted snapshot leaves existing-registration reuse manual because the registration cannot distinguish those identities. Reuse preserves the team ID, project and management secret, and applies authoritative membership/contact/target-size synchronization. Other collisions remain manual.
