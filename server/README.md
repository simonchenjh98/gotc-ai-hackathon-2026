# GOTC 活动评分服务

Python 3.11+ 标准库 HTTP + SQLite 服务，无 pip 依赖。只管理本届活动；首次启动没有队伍、评委、观众、评分或投票数据。

## 启动

在项目根目录运行：

```sh
GOTC_DATA_DIR=/opt/gotc-hackathon/data \
GOTC_BASE_PATH=/gotc \
GOTC_HOST=127.0.0.1 \
GOTC_PORT=5274 \
GOTC_PUBLIC_ORIGIN=https://8.141.114.201 \
python3 server/app.py
```

环境变量均为可选，默认值依次为 `server/data`、`/gotc`、`127.0.0.1`、`5274`、`https://8.141.114.201`。生产必须使用独立、持久化的数据目录，以及非 root 专用服务用户。程序拒绝绑定非 loopback IP。

首次启动在数据目录生成 `admin-access.txt`，包含随机管理码，只创建一次。它不会打印到日志、返回给公开接口或写入仓库。Linux 上数据目录为 0700，管理码文件、数据库为 0600，进程 umask 为 0077。Windows 的 chmod 不替代 ACL，正式部署以 Linux 私有数据目录为准。不要删除或覆盖数据库来更新网页。备份时使用 SQLite backup API 或 `sqlite3 .backup`，避免单独复制正在写入的主数据库文件。

Nginx 应将 `/gotc/` **保留完整路径**代理到 `http://127.0.0.1:5274`，覆盖 `X-Real-IP` 为 `$remote_addr`，保留 Host，使用 HTTPS。服务只接受 Origin 与 `GOTC_PUBLIC_ORIGIN` 完全一致的 POST；不要将 `Origin` 改写成可信值。管理页面为 `/gotc/platform.html#admin`，在浏览器填写管理码。首次进入后台后录入正式队伍、创建评委/观众邀请码，并在活动开始时开放评分。

本地浏览器测试可将 `GOTC_PUBLIC_ORIGIN` 设为 `http://127.0.0.1:5274`。此时 Cookie 不带 Secure；HTTPS 正式地址自动启用 Secure。身份 Cookie 为 HttpOnly、SameSite=Strict，12 小时过期；邀请码和会话令牌的明文均不存入数据库。

组织方可以在私有数据目录放置 `shared-login-codes.json`，用 `admin` 和/或 `judge` 字段配置统一登录码。真实值仅放在该私有文件，不进入 Git 或公开网页。启动时同步仍使用统一码的账号哈希，使已更改密码账号的旧会话失效，保留账号 ID、评分和投票。若配置了评委统一码且还没有任何评委身份，自动创建“评委01”；后续新建评委身份沿用统一码。多位评委共用登录码时，正确输入登录码后选择自己的身份，各自记录分开保存。单独重发的评委会退出统一码，使用自己的新码；启动会保留该独立码，不再恢复统一码，也不出现在统一码的身份选择中。观众邀请码不受统一码影响。

## API 合同

下列路径前统一加 `/gotc`（或配置的 base path）。JSON 写入使用 `Content-Type: application/json`，携带同源 Origin 和 Cookie。成功返回 200；错误为 `{ "error": "可读错误说明" }`。错误码包括 400 输入错误、401 未登录、403 身份或来源不符、404 不存在、409 状态冲突、413 超过端点请求上限、415 内容类型错误、429 登录限速。

| 方法与路径 | 身份 | 请求及响应 |
|---|---|---|
| GET `/api/board` | 公开 | `{teams,config,updatedAt,serverTime}` |
| POST `/api/login` | 公开 | `{role: "admin"\|"judge"\|"audience"\|"team",code}` → `{ok,role,name}`，设置会话 Cookie |
| GET `/api/session` | 公开 | `{role: null\|"admin"\|"judge"\|"audience"\|"team",name}` |
| POST `/api/logout` | 任意 | `{}` → `{ok}`，删除当前会话 |
| GET `/api/judge` | 评委 | `{teams,myScores,dimensions,scoringOpen,name}` |
| POST `/api/score` | 评委 | `{teamId,scores:[六项数字],feedback,status:"draft"\|"submitted"\|"recused"}` → `{ok,status}` |
| GET `/api/audience` | 观众 | `{teams,votedTeamIds,voteLimit:3,votingOpen,name}` |
| POST `/api/vote` | 观众 | `{teamId}` → `{ok,alreadyVoted}` |
| GET `/api/admin` | 管理员 | `{teams,judges,audience,config,stats}` |
| POST `/api/admin/settings` | 管理员 | `scoringOpen`/`resultsPublished`/`submissionsOpen`/`votingOpen` 布尔值、`minJudges` 1–50 整数、`submissionDeadline`/`votingDeadline` Unix 毫秒整数或 null，均可单独更新 → `{ok,config}` |
| POST `/api/admin/teams` | 管理员 | `{id?,number,name,project?,track?,status?,statusReason?,booth?,roadshowOrder?,expectedRevision?}` → `{ok,team}` |
| POST `/api/admin/invitations` | 管理员 | `{role:"judge"\|"audience",name}` → `{id,name,code}`，只有创建响应返回明文码 |
| POST `/api/admin/revoke` | 管理员 | `{role:"judge"\|"audience",id}` → `{ok}` |
| GET `/api/admin/export` | 管理员 | UTF-8 BOM CSV 下载，包含队伍、单份评分与反馈；防止表格公式注入 |

`teamId` / `id` 是随机字符串，不是队伍显示编号。`number` 是 1–999 整数且不可重复；队名必填，1–80 字；作品名 0–140 字；赛道 0–80 字。更新必须带现有 `id`，保持历史评分与投票关联；不存在的 ID 不会被自动新增。

统一码匹配多位有效评委时，登录先返回 `{selectAccount:true,accounts:[{id,name}]}`，此时不会创建会话。选择身份后，用相同的 `role`、`code` 加 `accountId` 再次提交完成登录。身份选项只在验证了该角色的正确统一码后返回。

登录密码校验在 SQLite 写锁之外进行，同一服务实例最多同时执行 4 次 scrypt，其他登录在锁外等候；这不是同时在线人数上限。scrypt 强度保持不变。校验完成后，服务取得写锁并重新检查账号角色、启用状态与被校验的凭据，再创建会话。校验期间停用、重发或更新统一码，会使旧凭据的这次登录失败。其他业务写入仍在取得写锁后检查会话、版本和截止时间。

登录限流与上述并发上限独立：同一 IP、同一角色每分钟最多 20 次，每个角色全局每分钟最多 400 次；成功、失败及统一码选择身份的两个步骤都计数。同一校园网络出口可能共用 IP 配额，429 应作为限流单独排查，不解释为数据库容量不足。

公开 `teams` 的每行结构：

```json
{
  "id": "T随机标识", "number": 1, "name": "正式队名",
  "project": "作品名称", "track": "探索方向", "judgeCount": 0,
  "score": null, "rank": null, "audienceVotes": 0
}
```

`config` 包含 `scoringOpen`（默认 false）、`resultsPublished`（默认 true）、`minJudges`（默认 5）、`dimensions`、`updatedAt`、`voteLimit:3`、`audienceIncludedInScore:false`、`maxScore:100`。所有时间戳为 Unix 毫秒。评委 `myScores` 仅返回本人记录：`{teamId,scores,feedback,status}`；回避状态 scores 为空数组。

`judges` 每项为 `{id,name,submittedCount,active}`；`audience` 每项为 `{id,name,active}`；`stats` 包含 `teamCount`、`submittedScores`、`audienceVotes`。后台不返回邀请码哈希或明文。

## 队伍提交 API

| 方法与路径 | 身份 | 请求及响应 |
|---|---|---|
| GET `/api/team` | 队伍 | `{team,submission,window,judgingMaterials}`，仅绑定队伍 |
| POST `/api/submission` | 队伍 | `{expectedVersion,status:"draft"\|"submitted",materials}` → `{ok,team,submission,window,judgingMaterials}` |
| POST `/api/admin/team-access` | 管理员 | `{teamId,action:"issue"\|"rotate"\|"revoke"}` → `{ok,teamId,id,active,code?}`；仅发码与重发返回明文 |
| GET `/api/admin/submissions/export` | 管理员 | UTF-8 BOM CSV，一队一行，正式与草稿列明确分开，含版本、时间和未提交修改状态，无凭据 |

队伍以 `role:"team"` 使用独立提交码登录，不使用统一评委码。每队绑定一个账号。`issue` 仅首次创建；存在身份返回 409。`rotate` 保留账号 ID、重发随机码并重新启用；`revoke` 停用，可重复调用。二者立即删除旧会话，已保存材料保留。所有写操作先获得 SQLite 写锁再验证会话，排队中的旧会话不能绕过重发或停用。

`materials` 只接受下列字段，字符串省略时默认为空并去除首尾空白，`confirmed` 省略为 false：

| 字段 | 长度上限 | 正式提交 |
|---|---|---|
| `projectName` | 140 字 | 必填 |
| `summary` | 600 字 | 必填 |
| `codeUrl`, `readmeUrl`, `slidesUrl`, `videoUrl` | 各 1024 UTF-8 字节 | 必填 |
| `license` | 200 字 | 必填，许可证名称或说明 |
| `licenseUrl` | 1024 UTF-8 字节 | 选填 |
| `demoUrl`, `demoInstructions` | 链接 1024 字节、说明 2000 字 | 至少一项 |
| `sourcesAndLimitations` | 2000 字 | 必填 |
| `codeVersion` | 输入最多1024 UTF-8字节 | 新正式提交必填：完整40/64位SHA或同仓库commit链接；正式保存为小写完整SHA，草稿可不完整 |
| `confirmed` | 严格布尔值 | 必须 true，队伍自查声明 |

只接受无凭据、无空白或控制字符的完整 HTTPS 链接；不抓取外部资源，不自动验证许可、可访问性、页数或视频长度。提交端点最多 32768 字节，其他 JSON 请求仍为 16384 字节；未知字段、错误类型、超长内容为 400，超请求上限为 413。请求不接受 `teamId`，身份与队伍由服务器绑定。

`submission` 为 `{version,draft,submitted,submittedVersion,firstSubmittedAt,submittedAt,updatedAt,hasUnsubmittedChanges}`。初始 version=0，其余快照、正式编号和时间为 null，changes=false。每次成功写入 version 加一，正式提交把当前材料同时保存为草稿与正式快照，submittedVersion 等于新 version。首次提交时间保留；只有正式提交更新 `teams.project`，草稿不会影响公开作品名或评委资料。保存与版本检查处于同一事务，旧 expectedVersion 返回 409，不发生部分更新。

`window` 为 `{submissionsOpen,submissionDeadline,canEdit,serverTime}`。默认关闭、截止 null；非空截止为 0–253402300799999 的整数 Unix 毫秒时间戳，布尔值不接受。写入要求开放且服务器时间严格早于截止；到点为只读，时间和开关独立于评分。后台以北京时间输入；补交通过管理员延长截止并重新开放。建议先关闭提交再评审，已有评分不与材料版本绑定。快照保存字段和链接，外链内容仍可能改变。

`GET /api/admin` 保留原合同并新增 `submissions:[{teamId,access:null|{id,active},submission}]`，含每支队伍和两份快照。`GET /api/judge` 新增 `submissions:[{teamId,materials,version,firstSubmittedAt,submittedAt}]`，仅正式材料，不含草稿、草稿更新时间或凭据。公开看板和观众响应不返回项目材料。没有提交材料的队伍仍可按现场演示评分。原评分 CSV 格式与文件名不变。

旧版数据库会先经 SQLite backup API 备份到私有数据目录的 `before-team-*.sqlite3`，再事务重建 `accounts` 的角色约束；保留账号 ID、密码哈希、会话、评分、投票、队伍和审计，通过外键检查后提交，失败回滚并停止启动。重复启动跳过已完成迁移。升级前仍建议进行常规备份，保留整个持久化目录；备份不会被静态服务公开。

完整的保存/冲突处理、URL 字段快照边界、部署检查及三类回退步骤见 [作品提交上线与回退说明](../docs/submission-release.md)。作品提交模块保留最新草稿与最近正式字段，不归档外链内容；独立评审轮次的冻结能力见下文。保存是否成功以服务端锁后校验与成功响应为准，不能以点击时间判断；409 需要读取最新版本并由用户明确合并，不自动重试。

## 展示与路演评审框架

新记录入口 `#judge-exhibition`、`#judge-roadshow` 使用 `/api/judging/{stage}`，stage 允许 `exhibition`（展示）或 `roadshow`（路演），未知阶段返回 404；相应进度页为 `#board-exhibition`、`#board-roadshow`。它保存定性评审记录，**仅这些新阶段的数值评分、满分、有效评委门槛、汇总公式、排名、晋级名额和成绩发布尚未实现**：相应字段为 `null`，能力标记为 false，不生成成绩或晋级结果。六个维度名称仅作为后续量表的占位，`max` 全为 null。

两阶段的 run、版本、工作名单、评委授权、窗口、记录和材料快照分别保存。stage 另含 `rosterSource:"manual_worklist"`、`advancementStatus:"unimplemented"`。路演名单由组织方人工配置，可选择任意现有队伍，不要求该队先参加展示；这不构成正式晋级结果。展示关闭、重开或新建轮次不会修改路演，反向也一样，没有自动下游计算。

现有 `#judge`、`#board` 继续提供原六维评分与成绩看板，使用 `/api/score`、`/api/judge`、`/api/board`；后台保留 `scoringOpen`、`minJudges`、`resultsPublished` 及原评分 CSV。原分数只保存到 `scores`，既不迁入新阶段，也不参与 `#board-exhibition` 或 `#board-roadshow` 的记录进度。新阶段的 `scores:null` 和发布 409 不适用于原专业评分接口。

| 方法与路径 | 身份 | 合同 |
|---|---|---|
| GET `/api/judging/{stage}` | 本轮获权评委 | `{stage,teams,myReviews,window}` |
| POST `/api/judging/{stage}/review` | 本轮获权评委 | `{runId,teamId,expectedVersion,status,feedback,scores:null}` → `{ok,stage,teams,myReviews,window}` |
| GET `/api/judging/{stage}/board` | 公开 | `{stage,window,teams,publication}`；仅完成进度，score/rank 全为 null |
| GET `/api/admin/judging/{stage}` | 管理员 | `{stage,window,roster,teams,judges,reviews,blockers}` |
| POST `/api/admin/judging/{stage}/roster` | 管理员 | `{expectedRevision,teamIds,judgeIds}` |
| POST `/api/admin/judging/{stage}/window` | 管理员 | `{expectedRevision,open,deadline}` |
| POST `/api/admin/judging/{stage}/reset` | 管理员 | `{expectedRevision,reason}`，新建关闭的轮次，保留旧记录 |
| POST `/api/admin/judging/{stage}/publish` | 管理员 | `{runId}`，返回 409，明确尚未实现 |

管理员写入成功均返回 `{ok:true,...完整管理员状态}`，stage.revision 加一；旧 expectedRevision 返回 409，防止覆盖其他页面设置。首次启动分别建立关闭、无队伍和评委的展示与路演轮次，不导入旧成绩。名单必须使用现有队伍与有效评委，不能重复；只有窗口关闭、从未开放冻结且没有任何评审记录时才能修改。首次开放后即使尚无评审记录，也需填写理由创建新轮次才能更换名单或重新采集材料。创建新轮次前必须先关闭本轮；新轮次复制工作名单，旧轮次、材料快照与评审记录留在数据库。stage.rosterEditable 明确表示名单当前是否可改，供前端显示，服务端仍在事务中强制校验。

首次开放时复制队伍信息与最近正式材料作为本轮快照，之后提交页面的改动不替换本轮材料。路演独立截取当时的正式材料，不借用展示快照。没有正式材料也可记录现场观察；公开进度页不返回材料、评语或评委身份。同一轮次关闭后重新开放只修改窗口，不重新采集或覆盖冻结材料。开放需要非空队伍和评委名单。`window={open,deadline,canEdit,serverTime}`，截止使用 Unix 毫秒或 null，后台按北京时间输入；到点只读。此窗口与作品提交、旧评分和观众投票开关独立。

评审请求明确包含 `scores:null`；任何非空分数（包括空数组）返回 409，因为数值评分未实现。status 只接受 `draft`、`submitted`、`recused`。反馈最多 2000 字，正式定性记录必填，草稿可留空。记录形状为 `{teamId,version,draft,submitted,recused,updatedAt,hasUnsubmittedChanges}`；draft 为 `{feedback}`，submitted 为 `{feedback,submittedAt}`。每次成功写入 version 加一；草稿保留最近正式内容，回避保留内容并从完成计数中排除，后续保存草稿或正式记录解除回避。旧轮次或旧版本请求返回 409，不静默覆盖。

身份、授权、轮次、窗口与记录版本均在同一 SQLite 写事务中核对。新数据使用独立的 `judging_*` 表和阶段标识，不修改原 `scores` 数据。审计记录操作、轮次、版本和管理员重置理由，不复制评审反馈正文。公开 `publication.status` 始终为 `unavailable` 并附阻断原因；配置规则或自动发布的通用规则引擎不在此框架中实现。

## 后台数据 API

以下接口仅管理员可用。所有写入沿用同一 SQLite 事务，获得写锁后才校验会话。仅 `/api/admin/data/` 下的 GET 允许查询参数；未知、重复或过长参数返回 400。

| 方法与路径 | 请求或查询 | 响应 |
|---|---|---|
| GET `/api/admin/data/sync` | 不接受查询参数 | `{reviewRevision,stages}`，只读的评审变化标记 |
| GET `/api/admin/data/template` | `kind=teams\|judges` | UTF-8 BOM CSV 模板 |
| POST `/api/admin/data/import/preview` | `{kind:"teams"\|"judges",csv}` | `{previewId,kind,rows,counts,canCommit,expiresAt}` |
| POST `/api/admin/data/import/commit` | `{previewId}` | `{ok,kind,created,updated,unchanged,issuedCredentials}` |
| POST `/api/admin/credentials` | `{role:"judge"\|"audience",id,action:"rotate"\|"revoke"}` | `{ok,id,active,credentialMode,code?}` |
| GET `/api/admin/data/runs` | `stage=exhibition\|roadshow` | `{runs:[{id,stage,createdAt,frozenAt,reason,current}]}` |
| GET `/api/admin/data/reviews` | `stage,runId,teamId,judgeId,track,status` 均可选 | `{run,teams,judges,reviews,coverage}` |
| GET `/api/admin/data/review-history` | `runId,teamId,judgeId` 必填 | `{history,historyAvailableFrom}` |
| POST `/api/admin/data/review-validity` | `{runId,teamId,judgeId,expectedVersion,invalidated,reason}` | `{ok,review}` |
| GET `/api/admin/data/export` | `kind=teams\|judges\|reviews\|rosters\|audit` 及对应筛选 | UTF-8 BOM CSV |
| GET `/api/admin/data/audit` | `action,actorId,target,beforeId,limit` 可选 | `{entries,nextBeforeId}` |

`sync` 只追踪独立展示/路演记录，不追踪原 `scores` 表的专业评分。它不返回评语、材料、名单或凭据，不写数据库或审计。`reviewRevision` 取私有评审修订表的最大 ID，无记录时为 0；同一毫秒内的多次修改仍会改变标记，普通登录不会改变它。`stages` 按 `exhibition`、`roadshow` 分别返回 `{runId,revision,canEdit}`；`canEdit` 使用同一次服务端时刻计算，到截止时间会变为 false，即使没有新的写入。

后台页面可见时，每次检查结束后等待 3 秒再读取标记。新阶段评审记录发生变化且轮次、配置仍一致时，只更新该阶段的正式记录数量和只读记录表；页面隐藏时暂停。轮次或配置变更时提示刷新核对，截止时间到达时更新只读标识，保留未保存输入。原专业评分设置与统计、历史筛选、审计、名录、材料和一次性登录码区域不自动重绘，需要重新查询或手动刷新；原专业成绩看板仍通过 `/api/board` 定时刷新。

名录 `team` 增加 `status`（`active|withdrawn|disqualified`）、`statusReason`、`booth`、`roadshowOrder`（1–9999 或 null）、`revision` 和 `trackKnown`。退出或取消资格须填写原因。更新带 `expectedRevision` 时，旧版本返回 409；队伍正式提交改变公开作品名也会增加名录版本。赛道选项为 AI for Science、城市与社区问题、未来学习样态；升级前未知赛道原样保留并标注，不能自动归类。状态、展位和路演顺序不改历史快照，不自动选队或排程。

CSV 队伍列为 `number,name,project,track`，另可含 `status,statusReason,booth,roadshowOrder`；number/name 必填，按 number 匹配。评委列 `id,name`，空 id 为新建，非空 id 必须匹配已有评委，不按同名更新。缺失、重复或未知表头会返回第 1 行的错误预览；数据行的重复键、列数、类型和长度错误逐行列出。最多 500 数据行，CSV 本体最多 256 KiB。专用预览端点允许 2 MiB JSON 封装，以容纳合法转义；其余端点上限不变。

预览行 `{row,action,key,before,after,errors}`，action 为 `create|update|unchanged|error`；counts 使用同名四个键。预览保存于私有数据库，绑定原管理员会话，15 分钟过期，既不创建实体也不生成凭据。确认时比较所有目标前值，任一变化则整批 409；有错误预览不能提交，成功后只能消费一次。`issuedCredentials` 只在成功响应返回明文，不进预览、审计或导出。导入评委不自动授权任何评审阶段。

记录查询默认展示当前轮。指定 runId 须属于所选 stage；teams 来自该轮冻结快照。status 可为 `all|draft|submitted|recused|invalidated|missing`：draft 包括正式后的未提交修改，submitted 只含未回避且未作废的正式记录，missing 是名单×授权中没有有效正式记录的组合。无记录组合返回 version=0、空草稿/正式内容。coverage 为 `{expected,recorded,validSubmitted,missing}`，按队伍/评委/赛道筛选统计，不代表达到计分门槛。组合超过 10000 条须缩小筛选。

作废或恢复只允许当前轮，原因必填，递增记录版本但不改评语。作废记录不计公开数量，评委后续修改仍保留作废，须管理员明确恢复。旧轮只读。私有 history 保存每次修改的前后内容、版本、操作者与原因；首次升级仅保存已知当前基线，不伪造过去版本，`historyAvailableFrom` 标明开始记录的时间。通用 audit 保存名录、窗口、授权、导入、凭据和有效性变更前后状态，不复制评语全文或明文凭据。

分类 CSV 沿用当前筛选，包含阶段/轮次/版本/状态；定性记录写“未计分”，不填 0 分；rosters 写 `manual_worklist`，不叫晋级名单。审计页面每次读取 1–100 条，默认 50，使用 nextBeforeId 继续；审计 CSV 导出全部匹配记录。所有 CSV 处理引号、换行与表格公式注入，空结果仍有表头。材料继续用原提交 CSV，旧分数导出仍是独立旧版接口。

观众投票现使用独立 `votingOpen` 和 `votingDeadline`。首次升级只把旧 scoringOpen 的当前值复制一次，此后旧评分开关与投票互不联动。`GET /api/audience` 的 votingOpen 是当前可投状态，另含 votingDeadline 与 `{open,deadline,canEdit,serverTime}` window；服务器在写锁后执行严格截止检查。作品提交、展示、路演和观众四窗口独立。

## 私有备份与恢复

首次升级后台数据结构前，服务会在私有数据目录生成 `before-admin-data-*.sqlite3`。日常备份可在服务运行时用 SQLite backup API 获取一致副本：

```sh
python3 -B scripts/backup_event.py backup /private/event.sqlite3 /private/backups/event-copy.sqlite3
```

恢复时先停止目标服务，恢复到一个不存在的新目标，再把服务的数据目录指向该恢复目录；脚本拒绝覆盖已有文件：

```sh
python3 -B scripts/backup_event.py restore /private/backups/event-copy.sqlite3 /private/restored/event.sqlite3
```

脚本校验完整性和外键，文件权限为 0600。数据库包含账号哈希、会话、队伍、材料、轮次、授权、反馈历史与审计；数据目录中的统一码配置、管理码文件须另行保存在私有备份中，不进入仓库或公开下载。不要只复制运行中的主 SQLite 文件而遗漏 WAL。

## 专业评分语义

六维依次为：

1. `problem` 问题价值与理解，20 分。
2. `ai` 方案与AI使用，20 分。
3. `prototype` 原型完成度，20 分。
4. `testing` 测试与改进，15 分。
5. `opensource` 开源与责任，15 分。
6. `teamwork` 团队协作与表达，10 分。

评分必须填齐六项数字，范围受各项上限约束，最小步长 0.1；拒绝布尔值、字符串、NaN、Infinity 和多余小数位。反馈选填，最多 2000 字。所有评分修改都必须在评分开放时进行。草稿和回避不计入均分；提交后可直接修改并重新正式提交。**已提交记录不能被草稿覆盖**，返回 409 并保留原成绩。提交后可以明确回避；回避后可以恢复草稿或正式提交。

达到最少不同专业评委数且成绩已公开，才返回专业平均分和排名。平均分涵盖全部有效提交，不去最高最低，公开保留两位小数；相同显示分数并列，采用 1、1、3 排名。公开接口只展示聚合值，不展示评委身份、反馈或单份分数。后台可查看达到人数门槛的分数，即使尚未向公众发布。

观众每个邀请码最多支持 3 个不同队伍，每队限一票，重复请求幂等；并发请求受事务保护。观众票单独统计，**不加入专业分，不计入评委人数门槛**。此版本由 `votingOpen`、`votingDeadline` 独立控制投票，专业评分仍由 `scoringOpen` 控制；投票提交后不能撤回。

停用邀请会立即废除该账号的全部会话、阻止后续登录；**既有正式评分和观众票保留且继续计入**。系统保留基础审计记录，不在审计中保存邀请码、会话令牌或评分反馈正文。

## 安全与静态资源

生产仅 loopback + Nginx。静态根目录为项目根，但只允许 `index.html`、`styles.css`、`app.js`、`platform.html`、`platform.js`、`platform.css`、`favicon.svg`、`assets/` 中的图片/字体、`downloads/` 中的 txt/ics。服务拒绝隐藏文件、路径穿越、越界符号链接和目录列表；不会公开 `server/`、文档、数据库或凭据。

接口禁止跨域写入，JSON 上限为 CSV 导入预览端点 2 MiB、提交材料端点 32KB、其余端点 16KB，登录按真实来源 IP/身份限速为每分钟 20 次，另有每身份每分钟 400 次全局上限。Nginx 对导入预览路径单独放行 2 MiB，其余 `/gotc/` 请求保持 32KB，应用端点限额仍然强制执行。Nginx 必须覆盖可信代理 IP 头。CSP 仅允许本站脚本、样式、字体和请求，不允许内联脚本、外部框架或嵌入页面。所有响应禁用缓存。

## 验证

从项目根目录执行：

```sh
python3 -B -m unittest discover -s server -p 'test_*.py' -v
python3 -B -m unittest discover -s deploy -p 'test_*.py' -v
python3 -m py_compile server/app.py server/test_api.py
```

测试启动真实本地 HTTP 服务，使用各自独立的临时数据库并清理，不写入生产。覆盖：空初始状态、管理码不落入数据库、身份隔离、Cookie、安全头、同源检查、登录限速、输入校验、16KB 上限、静态白名单与穿越保护、草稿/提交/回避、5 位门槛、算术均分、改分、提交不降为草稿、关闭评分、成绩发布开关、观众三票并发限制与幂等、票数独立、停用保留历史、同分排名、CSV 公式注入保护及审计。

## 仓库与 commit 核对

新正式提交的 `codeUrl` 应填写完整 HTTPS 仓库主页（不是代码包、文件页、个人页或本地路径）。GitHub、Gitee、GitLab 会检查仓库根地址形状；其他平台保持原 HTTPS 格式兼容，须直接填完整 SHA。例如 `https://github.com/your-team/your-project`。队伍应保证仓库公开、代码已 push；平台不联网检查存在性或可访问性。既有材料只读保留，旧草稿可继续保存，重新正式提交时按新规则补齐。

`codeVersion` 输入40/64位十六进制完整SHA，或 GitHub/Gitee 的 `/owner/repo/commit/<完整SHA>`、GitLab 的 `/group/subgroup/repo/-/commit/<完整SHA>` 链接。仓库/commit链接主机和仓库路径必须一致，GitHub/Gitee路径忽略大小写，GitLab保持大小写并支持多级子组；允许仓库尾部 `.git` 和尾斜杠。拒绝凭据、显式端口、查询参数、片段、百分号编码及点路径。上述是三家已支持的 commit 链接解析范围；其他平台 commit 链接无法可靠解析时提示改填完整 SHA，不限制其原有 HTTPS 仓库输入。这里只做语法和路径比较；直接填SHA不能证明其属于该仓库，commit链接也不证明提交实际存在。正式保存小写完整SHA，草稿保留输入；摘要前6位不保证唯一，应使用完整值核对。

本队响应及管理员各队记录新增 `judgingMaterials:{exhibition,roadshow}`。每阶段为 `{runId,inRoster,frozenAt,materialSnapshot}`，后者为空或既有 `{materials,submissionVersion,submittedAt,capturedAt}` 快照。区分未入名单、未冻结、冻结时无材料和已有冻结版；不会拿最新正式版补空。队伍只能得到本队材料，不带评委或评语。原专业入口仍读最新正式版，展示/路演同轮重开保持快照，新轮首次开放才重新截取。

前端集中列缺项，预览原commit输入及规范化完整值后再次确认。成功回执取服务端返回的正式版号/时间；刷新只展示服务器最近记录。20秒超时、网络/5xx异常标记结果待确认，保留可复制输入、主动读取最新记录供核对，不自动重试或断言本次成功。409仍不覆盖、不自动合并；版本冲突与关闭/截止分别提示。回执只是字段与URL记录，不备份外部文件。

## 后台提交总览

管理员在提交窗口设置下方查看全队四类数量（未保存、仅草稿、已正式提交、有未提交修改）。统计仅反映服务器已有记录；同内容草稿重复保存不算未提交修改。队名／队号／项目名搜索、状态和真实冻结版本差异取交集，筛选不改变全队统计。仅有实际冻结快照且与最新正式版不同才标记差异；未入名单、未冻结或冻结时无材料均不误报。

总览刷新复用现有管理员接口，只更新总览区，保留筛选、其他未保存表单和一次性码；阶段材料变化后重新读取数据再计算。读取失败保留上次结果并提示未更新，可手动重试。本功能不新增轮询、数据表或权限，也不包含队伍草稿恢复。

## 队伍本浏览器材料副本

队伍输入或主动发送时，前端按赛事路径与服务端确认的队伍ID，在本浏览器 localStorage 同步保存材料原始文本；不同文档使用独立槽，保留7天。不会保存邀请码、会话凭据或自查勾选，不会自动发送草稿或正式提交。无实际文本变化的离页补存不延长最后编辑时间。

只有授权读取本队后才列候选。恢复前再次读取服务器；同版且窗口开放时可主动填回，仍需核对和手动保存。服务器变版、窗口关闭或发送结果不明时只比较／复制；来源副本保留，不能把相同内容当成功回执。新输入或另一标签的副本不随旧请求成功一起删除。读取／配额／清理失败会提示，手动服务器保存仍可进行；离页提醒保持。

成功退出前重新确认当前队伍，退出成功后切换本队本地清理代次并清理旧副本，旧标签停止写入；会话到期、关页、刷新不等同退出，不自动删除。共享设备请显式退出；如浏览器阻止本地清理，按提示清除此站点数据。此功能不改服务器数据库、正式材料或评审快照。

## 精确请求回执、正式历史与人工访问记录

`POST /api/submission` 新增可选 `requestId`（16–128位 ASCII 字母、数字、下划线或连字符）。新操作生成新编号，不自动重发。原始 `{expectedVersion,status,materials}` 以排序JSON指纹绑定编号；同队同ID同指纹回放原结果，不增版、不追加审计或历史；同ID不同内容为409 `request_id_conflict`。旧客户端省略编号时服务端生成，保留原保存流程，但丢响应后不知道编号。

成功响应在原投影外增加 `receipt` 与 `replayed`。精确回执为 `{requestId,status:"succeeded",action,baseVersion,version,savedAt,materials,historyVersion}`；材料、版号、时间是那一次的不可变结果，外层队伍投影仍可能已推进。草稿 historyVersion 为 null，正式提交等于此次编辑版。

队伍 `GET /api/submission/requests/{id}` 仅查询本队，返回成功回执，或 `{requestId,status:"rejected",action,baseVersion,error:{status,code,message},decidedAt}`，或 `{requestId,status:"not_found"}`，查询本身为200。未找到不证明失败，可能仍在途，不自动重发。材料、窗口、版号拒绝分别记录 `materials_invalid/submission_closed/version_conflict` 终态后返回原400/409；修正后用新编号。401/403、非法ID或顶层信封不占编号。写锁后才鉴权；停用或轮换前的旧会话不能借编号读取或重放。

历史列表 `GET /api/submission/history?beforeVersion=N&limit=20` 返回 `{items,nextBeforeVersion}`，版号倒序且不含边界，默认20、最多50。列表项为 `{version,submittedAt,requestId,origin,codeVersion,projectName}`，不含全文；详情 `/api/submission/history/{version}` 返回版本元数据和完整 `materials`。管理员同形路径 `/api/admin/submissions/{teamId}/history[/version]`；不扩大评委或公开权限。只记录成功正式提交；旧库每队只补当前真实一份为 `legacy_current/requestId=null`，不推造早期版本。历史保存字段和URL，不归档外部文件。

管理员 `GET /api/admin/submissions/{teamId}/checks?version=N` 返回 `{teamId,version,latestVersion,items}`；每项 `{field,url,status,revision,checkedAt,actor,note}`。仅生成非空代码/README/许可证/演示/幻灯片/视频链接，地址来自不可变历史。POST同一路径接收 `{version,field,expectedRevision,status,note}`；备注最多500字，状态 `unchecked/accessible/permission_required/unavailable`。仅当前正式版可写，每项独立并发修订；版号推进或修订冲突均409。操作者与记录时间由服务器产生。新正式版默认未检查，旧版只读，即使地址相同也不复制。人工记录仅反映当时观察，不证明作品合格或永久可访问，不自动抓取或通知。

新增请求账本、正式历史、人工检查和迁移元数据表。非空升级先创建私有 `before-submission-records-*.sqlite3` SQLite整库备份；备份失败停止启动。DDL、真实当前正式回填与标记独立原子提交，失败全部回滚，重复启动幂等。旧代码回退期间不维护新记录，再升级只补当时仍保有的当前正式一份并保留缺口，不虚构请求回执、不覆盖已有历史。代码回退不恢复数据库，不自动覆盖现库；整库SQLite备份恢复包含新表。私有临时库演练不代表生产迁移或部署。
