# 队伍申请与自动完成

所有参赛者都可以在 `platform.html#claim` 填写队名、队长与 2–4 名组员的姓名和学校。赛道可选已公布的三条赛道，也可留为待定。空数据库无需导入名单、预建队伍或开放 API 额度即可接受申请。

完整申请提交后会在同一写事务中自动分配 1–999 中最小的未使用队号、创建有效队伍、登记或复用未归队人员、绑定成员并开放提交码领取权限，不再进入人工审核队列。队名已经存在、申报成员已归属其他队伍、同名身份需要人工区分、队号或名单容量不足等情况会直接返回冲突，并将本次申请整笔回滚，不留下新的待审申请。队长在原页点击领取；重复领取保持同一码。此流程不自动开通 API 网关权限或额度。

如本队已有历史组队登记，系统只在恰好存在一个有效、尚未发参赛码的同名队伍，且登记队长姓名、队长学校和完整成员姓名集合都与本次申报精确一致时自动绑定。绑定后保留原队伍 ID、队号、项目、来源及原始记录，并同步本次确认的成员；既有队伍管理码仍只保留原登记资料修改能力。任何不唯一或不完全匹配都直接提示冲突，不按相似名称猜测归队。

私有参考记录仅用于去重，不决定报名资格。同名同校且未归队的记录会复用；已有队伍的同名同校记录会阻止重复归队。同名但不同学校且已归队时，为避免自动冒认，本次新申请也会直接提示冲突。旧记录和原始备注继续保留。

已自动完成或已领码的申请不可改写。发布前遗留的 `pending` 回执仍可在原页面补充或修改；保存完整且无冲突的信息时会自动完成。旧版无组员的回执仍须先补充全体成员；旧请求或更新载荷未包含队伍信息时，不会删除既存队伍信息。

自动完成复用原 `approve_open` 的原子写入、版本校验、唯一约束、成员同步和账号签发，只增加内部 `automatic` 模式；公开客户端不能伪造该模式。审计写入 `verificationMethod: "automatic"`、`applicationReviewed: false` 和独立的 `claims.approve-auto-open` 事件。原 `/api/admin/claims/approve`、`/review` 仅作为历史回执、异常处理和停用身份重发的兼容接口保留，不再是正常新申请的必经步骤。

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

## 历史可信名单兼容

原可信名单导入、开关和审计接口仍保留，用于旧版本回执与回退兼容；当全量自动完成策略开启时，它不再决定新申请是否通过。普通公开接口仍不会返回私有名单、匹配细节、PIN、领取凭据或提交码。领码继续由原 bearer 回执和账号指纹校验控制，自动完成申请不会自动开通 API 权限。
