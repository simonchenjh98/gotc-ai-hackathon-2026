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

管理员也可在选手入口填写现有统一管理码，服务端仍验证真实管理员账号，登录结果先为 `role: "admin"`。选择有效队伍后进入代管会话，可准备该队材料、申请仓库、查看该队历史及已开通的 API 工具；无需创建学生账号或改变提交码。代管不越过作品提交窗口、模型权限、密钥数量或调用限制。没有学生提交身份的队伍可先准备材料与仓库，但其 API 密钥实际调用仍遵守既有队伍账号启用条件。

代管会话的数据库 `sessions.account_id` 始终属于真实管理员，`admin_team_sessions` 将当前令牌关联到唯一队伍。对选手接口仅呈现该队权限；进入、返回和再次选队都旋转令牌，使旧标签的身份标记失效。管理员账号停用、管理码轮换时删除会话、会话到期或退出后，代管也失效。材料、仓库、密钥等审计记录实际管理员 ID、`role: "admin"` 和 `metadata.operator` 中的目标队伍。旧学生账号和会话保持原有规则。

### 原参赛码与本队资料

`platform.html#team-profile` 使用原参赛码登录后的同一队伍会话，供本队读取/保存资料及查看实际成员学校；作品与本队查分继续使用同一登录。新 `GET`/`POST /api/team/profile` 必须为真实 team 身份、带当前 `X-GOTC-Login`，不接受目标 ID、管理码或 query，拒绝组织方代管。保存严格接收 `{team,expectedRevision,expectedTeamRevision}`，保留原已审核成员/人数锁、正式项目保护及资料去重规则，不改变作品截止或冻结规则，不轮换参赛码/会话/API。

读取缺失登记不写库；首次合法保存只在原 team ID 下建立资料行。初始化新增幂等的 `team_profile_visibility` 元数据表：新资料明确 `directory_visible=0`，既存公开登记无标记维持原范围；后续审核同步/恢复不翻为公开。原 `secret_hash` 列与资料保留，新资料/导入使用不发放的惰性占位；旧 `/api/registration/create|open|save` 与管理员重发管理码均返回 410，管理员导入不再导出管理码。原 `#claim` 申请/待审编辑保留。详情见 `docs/team-claims.md`。

**兼容回滚边界：** 旧二进制会重新接受保留的历史管理码，也会忽略新私有资料标记；回滚必须保留退役路由检查和目录隐私过滤。仅恢复旧程序或整库不是安全回滚。

## 队伍编辑、回收站与恢复

组织方队伍编辑区只读显示姓名、学校和资料来源。已审核成员按实际绑定身份显示；仅有原登记时，队长显示原登记学校，其余成员显示“学校待补充（原登记未提供）”。待核对学校保留提示；没有资料时显示“暂无成员资料”，不按同名补全学校。

“删除队伍”需核对队名并填写原因，实际操作为移入回收站、停用参赛。原 ID、队号、作品、评分、投票、冻结名单、仓库和使用记录保留；当前成员绑定释放，普通及代管会话撤销，提交身份、API 授权和现有 key 停用，原登记管理码失效。普通名录与新阶段候选不再显示回收队伍；后台历史记录仍可查。同名新申请可以审核，原队号不复用。普通退出/取消资格队伍仍沿用原重名规则；自动审核名单、开关和匹配规则不变。

回收站恢复先检查现存队名、原成员身份与占用情况；可以填写空闲新队名，但不能覆盖其他队伍成员。任一明确冲突阻止整个恢复。仅原登记成员学校未知的同名提示不证明同一人，不阻止恢复，也不建立已审核身份绑定。成功恢复沿用原 ID、队号及原参赛状态；原为退出或取消资格时不会自动变成参赛中。

**恢复队伍不等于恢复访问。** 原提交码、会话及 API key 仍失效；需要访问时，使用已有后台流程重发提交码、重新开通 API 并创建新 key。资料管理统一使用参赛码，旧队伍管理码不再发放或恢复。相关阶段的已发布结果需核对后主动重新发布。网关在排队后的最终准入检查拒绝已回收队伍；已经通过该检查的请求可以完成并计费，历史用量不会撤销。

私有接口为 `GET /api/admin/data/team?teamId=…`、`GET /api/admin/data/recycle-bin`、`POST /api/admin/data/team/recycle` 和 `POST /api/admin/data/team/restore`。仅真实管理员且携带当前 `X-GOTC-Login` 可访问，代管身份不可用。回收提交显示的 `expectedRevision` 和 `recycleContext`；资料变化返回 409 后需刷新、核对并重新确认，不自动重试。恢复同样要求显示版本并在事务内重查冲突。

初始化只增加 `team_recycle_events` 和 `team_recycle_memberships` 及索引，不删除或重建已有表。**首次真实回收后，回滚必须保留理解回收站与冲突恢复规则的代码。** 更早版本不了解墓碑和已释放成员，可能从普通编辑绕过恢复检查；不可直接切回旧包，也不可用旧整库覆盖较新的活动记录。采用前向修复或经审查的兼容回滚，详见部署说明。此说明不授权真实队伍变更或部署。

## API 合同

决赛评委入口先验证统一邀请码，再选择本人席位。邀请码复用私有 `shared-login-codes.json` 的 `admin` 值；未配置时入口关闭。`POST /api/finals/access` 接收 `{code}`，成功返回 `{accessToken,expiresAt}`，有效期 12 小时，不设置 Cookie，也不创建管理员或普通评委登录会话。请求沿用同源校验，并按独立限速桶限制为每 IP 每分钟 20 次、全局每分钟 400 次。真实邀请码不可写入公开网页、源码或测试。

`GET /api/finals/entry`、`GET /api/finals/judge`、`POST /api/finals/score` 和 `POST /api/finals/nominations` 必须携带 `X-GOTC-Finals-Access`；除读取席位入口外，仍须携带已选择的 `X-GOTC-Finals-Judge`。缺少、过期或已因改码失效的授权返回 `401/finals_access_required`；未配置返回 `503/finals_access_unavailable`。原管理员或评委 Cookie 不能替代这项授权，决赛令牌也不能访问管理员、普通评分或选手接口。公开决赛看板与管理员决赛接口维持原有权限，计分和提名规则不变。

决赛授权令牌为独立随机值，数据库 `finals_access_tokens` 仅保存 SHA-256 哈希、由当前私有码计算的 HMAC 标签及有效期。每次访问重新验证标签，因此更换私有管理码并重启服务后，旧决赛授权立即失效；原码不变的正常重启可继续使用未过期授权。决赛写操作在获取 SQLite 写锁后验证授权和席位，再按原修订号规则保存。

下列路径前统一加 `/gotc`（或配置的 base path）。JSON 写入使用 `Content-Type: application/json`，携带同源 Origin 和 Cookie。成功返回 200；错误为 `{ "error": "可读错误说明" }`。错误码包括 400 输入错误、401 未登录、403 身份或来源不符、404 不存在、409 状态冲突、413 超过端点请求上限、415 内容类型错误、429 登录限速、428 缺少必须的编辑版本。版本冲突响应附具体 `code`；旧页面不能省略版本盲写。

| 方法与路径 | 身份 | 请求及响应 |
|---|---|---|
| GET `/api/board` | 公开 | `{teams,stats,config,updatedAt,serverTime}`；teams 仅为已发布且达门槛的前 12 名 |
| GET `/api/team/score` | 本队 | `{team,state,score,dimensions,feedback}`；必须带本页 `X-GOTC-Login` |
| POST `/api/login` | 公开 | `{role: "admin"\|"judge"\|"audience"\|"team",code,accountId?}` → `{ok,role,name,loginId,operator}`，设置会话 Cookie；多身份统一码先返回 `selectAccount,accounts` |
| GET `/api/session` | 公开 | `{role: null\|"admin"\|"judge"\|"audience"\|"team",name,loginId,operator}`；代管的 operator 为 `{adminId,adminName,teamId,teamName}`，其他身份为 null |
| GET `/api/admin/team-workbench` | 管理员 | 必带 `X-GOTC-Login`，返回 `{teams:[{id,number,name}],role,name,loginId,operator:null}` |
| POST `/api/admin/team-workbench` | 管理员 | `{teamId}`，必带 `X-GOTC-Login`，旋转 Cookie 并返回 `{ok,role:"team",name,loginId,operator}` |
| POST `/api/team/workbench/return` | 管理员代管 | `{}`，必带 `X-GOTC-Login`，旋转 Cookie 恢复 `{ok,role:"admin",name,loginId,operator:null}`；普通队伍无权调用 |
| POST `/api/logout` | 任意 | `{}` → `{ok}`，删除当前会话 |
| GET `/api/judge` | 评委 | `{teams,myScores,dimensions,scoringOpen,name}` |
| POST `/api/score` | 评委 | `{teamId,scores:[六项数字],feedback,status:"draft"\|"submitted"\|"recused",expectedRevision,feedbackSharing?}` → `{ok,status,score}` |
| GET `/api/audience` | 观众 | `{teams,votedTeamIds,voteLimit:3,votingOpen,name}` |
| POST `/api/vote` | 观众 | `{teamId}` → `{ok,alreadyVoted}` |
| GET `/api/admin` | 管理员 | `{teams,judges,audience,config,stats}` |
| POST `/api/admin/settings` | 管理员 | `scoringOpen`/`resultsPublished`/`submissionsOpen`/`votingOpen` 布尔值、`minJudges` 1–50 整数、`submissionDeadline`/`votingDeadline` Unix 毫秒整数或 null，均可单独更新，必带对应组 `expectedRevisions` → `{ok,config}` |
| POST `/api/admin/teams` | 管理员 | `{id?,number,name,project?,track?,status?,statusReason?,booth?,roadshowOrder?,expectedRevision?}` → `{ok,team}` |
| POST `/api/admin/invitations` | 管理员 | `{role:"judge"\|"audience",name}` → `{id,name,code}`，只有创建响应返回明文码 |
| POST `/api/admin/revoke` | 管理员 | `{role:"judge"\|"audience",id}` → `{ok}` |
| GET `/api/admin/export` | 管理员 | UTF-8 BOM CSV 下载，包含队伍、单份评分与反馈；防止表格公式注入 |

`teamId` / `id` 是随机字符串，不是队伍显示编号。`number` 是 1–999 整数且不可重复；队名必填，1–80 字；作品名 0–140 字；赛道 0–80 字。更新必须带现有 `id`，保持历史评分与投票关联；不存在的 ID 不会被自动新增。

统一码匹配多位有效评委时，登录先返回 `{selectAccount:true,accounts:[{id,name}]}`，此时不会创建会话。选择身份后，用相同的 `role`、`code` 加 `accountId` 再次提交完成登录。身份选项只在验证了该角色的正确统一码后返回。

登录密码校验在 SQLite 写锁之外进行，同一服务实例最多同时执行 4 次 scrypt，其他登录在锁外等候；这不是同时在线人数上限。scrypt 强度保持不变。校验完成后，服务取得写锁并重新检查账号角色、启用状态与被校验的凭据，再创建会话。校验期间停用、重发或更新统一码，会使旧凭据的这次登录失败。其他业务写入仍在取得写锁后检查会话、版本和截止时间。

登录限流与上述并发上限独立：同一 IP、同一角色每分钟最多 20 次，每个角色全局每分钟最多 400 次；成功、失败及统一码选择身份的两个步骤都计数。同一校园网络出口可能共用 IP 配额，429 应作为限流单独排查，不解释为数据库容量不足。

公开看板 `teams` 的每行结构（只含可公布的前 12 名，score 为数字、rank 为 1–12）：

```json
{
  "id": "T随机标识", "number": 1, "name": "正式队名",
  "project": "作品名称", "track": "探索方向", "judgeCount": 0,
  "score": 80.25, "rank": 1, "audienceVotes": 0
}
```

`config` 包含 `scoringOpen`（默认 false）、`resultsPublished`（默认 true）、`minJudges`（默认 5）、`dimensions`、`updatedAt`、`voteLimit:3`、`audienceIncludedInScore:false`、`maxScore:100`。所有时间戳为 Unix 毫秒。评委 `myScores` 仅返回本人记录：`{teamId,scores,feedback,status,revision,updatedAt}`；回避状态 scores 为空数组。

`judges` 每项为 `{id,name,submittedCount,active}`；`audience` 每项为 `{id,name,active}`；`stats` 包含 `teamCount`、`submittedScores`、`audienceVotes`。后台不返回邀请码哈希或明文。

## 原专业评分与后台设置的编辑版本

原六维权重、均分、回避、评分开关与已正式评分不能降为草稿的规则保持。`GET /api/judge` 返回每个本人评分的 `revision` 与 `updatedAt`；无记录时提交基线为 0，已有旧评分升级后从 1 开始。`POST /api/score` 必须带该评委对该队自己读到的 `expectedRevision`，草稿、正式提交、改分和回避共用同一版本。成功返回本次保存的完整 `score` 实体并加一，不能用另一次 GET 冒充本次结果。旧版本返回 `409/score_revision_conflict`，不写评分、版本或审计。

仅管理接口的 `config.writeRevisions` 包含三个独立组：

| 组 | 保护字段 |
|---|---|
| `scoring` | `scoringOpen,resultsPublished,minJudges` |
| `submissions` | `submissionsOpen,submissionDeadline` |
| `voting` | `votingOpen,votingDeadline` |

例如关闭提交为 `{submissionsOpen:false,expectedRevisions:{submissions:2}}`。原平铺设置允许部分字段更新，但 `expectedRevisions` 必须恰好覆盖本次字段所属组；任一组过期返回 `409/settings_revision_conflict`，整批设置、版本与审计均不变。成功每个涉及组只加一，同值保存也加一，未涉及组不变。修改提交窗口不会使观众窗口的编辑基线过期。成功配置含各组最新版本，但前端仅推进本次提交组的基线，不替换其他正在编辑的表单。

评分及设置的版本为非布尔 JSON 整数，范围 `0..9007199254740991`；缺版本返回 `428/revision_required`，类型、范围或多余设置组错误返回 400。当前版本已达上限时返回 `409/revision_exhausted`。请求鉴权、版本核对、字段校验、写入、版本推进与审计均在同一写事务内执行。评分审计只记录修订号、状态、总分及评语是否变动，不保存评语正文历史。

`judge`、`score`、`admin`、`admin/settings` 支持已有 `X-GOTC-Login` 登录归属检查；提供的归属与实际 Cookie 会话不同返回 401。该公开标识不能作登录凭据。省略归属的 API 调用仍按 Cookie 身份鉴权，仍必须提供编辑版本。

页面遇到冲突保留输入，先只读查看服务器最新值；只有明确确认载入后，才替换对应队伍或设置组的表单并采用新基线。查看、冲突或网络失败都不自动重发。更新前已打开的旧页面会收到 428，应先复制未保存输入，再刷新重新读取。

首次升级独立创建私有 `before-write-conflicts-*.sqlite3` 整库备份，即使已有后台、提交历史迁移标记也不会跳过。备份失败停止启动；三个新 revision/meta 表、旧评分基线与三组初始版本在单事务中建立，失败回滚，重复及并发启动在写锁内核对完成标记。原 scores 七列和值保持不变。

旧代码不维护这些版本，禁止新旧服务混跑写入；切回旧代码不代表并发保护仍有效。回退前须停止写入并核对数据。代码回退不会自动还原数据库；恢复旧备份会丢失备份后写入，仍须按现有恢复说明单独处理。

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

## 队伍 AI API 网关

上游错误正文、流式错误事件（含事件元数据）及对应内容日志会对实际上游密钥脱敏，保留错误结构与状态。正常成功回复不改写。流式转发按完整 SSE 事件检查，单事件及首个数据事件前缓冲上限为 1 MiB；超限记为上游 502，不转发未检查片段。

观众投票与展示/路演评审的网页读写也须携带页面原登录的 `X-GOTC-Login`；缺失返回 428，切换身份后旧页面返回 401。刷新并核对当前身份后继续，公开进度看板不需要此标识。

服务内置 AI API 网关（标准库实现，不需要单独部署 sub2api）：组织方在后台“AI API 管理”为每队开通 API 访问权限，队伍在作品提交页自行创建 / 删除密钥，按队伍和密钥统计用量与费用。完整密钥只在创建响应中返回一次，数据库只保存加入服务端密钥（私有数据目录 `api-key-pepper`，首次启动自动生成，0600）的 PBKDF2-HMAC-SHA256 哈希。备份或迁移时须连同 `api-key-pepper` 一起保存，丢失后所有已发放的 API 密钥都会失效。网关用组织方的上游密钥转发请求，按返回的 token 用量计费，队伍看不到上游密钥。

模型上游在后台“AI API 管理 → 模型上游”中配置（类似 sub2api 的渠道）：每类上游（OpenAI 兼容、Anthropic）填写接口地址、API Key、开放模型和别名，保存后立即生效，无需重启。内置 Gitee AI（模力方舟，`https://ai.gitee.com/v1`）、DeepSeek、Anthropic 官方地址模板。上游 API Key 保存在私有数据库，不会返回给浏览器，留空保存表示沿用原密钥；修改记录写入 API 管理操作记录（不含密钥）。

AI API 在独立控制台 `/gotc/platform.html#admin-api` 管理（布局参照 sub2api：仪表盘、运维监控、队伍管理、账号管理、使用记录、操作日志、系统设置）。在“账号管理”添加账号：填写 Base URL 与 API Key，点“同步上游支持的模型”勾选模型即可保存，每个平台一个账号。未单独定价的模型按该账号的“默认单价”（`defaultPrice`）计费，未设默认单价则不计费、队伍额度不扣减（与 sub2api 对未定价模型记 0 一致）；需要单独定价时在“按模型单独定价”中填写输入、输出价，可再加 5分钟缓存写入、缓存读取[、1小时缓存写入]价，单价为每百万 token 的美元价（人民币价格请按汇率换算）；省略缓存价时按输入价的 1.25 倍（5分钟写入）/ 0.1 倍（读取）/ 2 倍（1小时写入）计。用量解析与 sub2api 一致：区分 5 分钟与 1 小时缓存写入；OpenAI 兼容上游的缓存命中按 `prompt_tokens_details.cached_tokens`、`prompt_cache_hit_tokens` 等字段依次读取，输入 token 不会为负。调用记录另存请求的模型名（别名映射前）、首字耗时、客户端 IP 与上游请求 ID。每类上游的第一个模型是队伍页接入示例的默认模型。别名每行写“别名 => 模型名”，以 `*` 结尾表示前缀匹配。

也可以在私有数据目录放置 `gateway.json`，仅在首次启动时导入后台（之后以后台为准，修改文件不再生效）：

```json
{
  "openai":    {"url": "https://ai.gitee.com/v1", "key": "…", "models": {"DeepSeek-V3": [0.3, 1.2]}},
  "anthropic": {"url": "https://api.anthropic.com/v1", "key": "sk-ant-…",
                "models": {"claude-sonnet-5-5": [3, 15, 3.75, 0.3], "claude-haiku-5-5": [1, 5]},
                "aliases": {"claude-*": "claude-sonnet-5-5", "claude-haiku-*": "claude-haiku-5-5"}}
}
```

已用真实 Gitee AI（模力方舟）密钥验证（2026-11-04）：
- 接口地址 `https://ai.gitee.com/v1`，同一密钥同时支持 OpenAI 兼容 `/chat/completions`、Anthropic 格式 `/messages`（接受 `x-api-key`）与 `/responses`，因此两类上游都可填写此地址与同一密钥；后台“模型上游”提供两个 Gitee 模板。
- 模型名区分大小写，以后台“获取上游模型列表”为准（如 `DeepSeek-V4-Flash`、`qwen3.8-flash`、`GLM-5.3`、`kimi-k3`）；单价需按 Gitee 控制台的人民币价格换算为美元填写。
- 用量字段与网关解析一致：推理 token 计入 `completion_tokens`；Anthropic 格式流式回复在 `message_start` 中输入数为 0、在 `message_delta` 中给出实际值，网关按后者计费；缓存命中通过 `cache_read_input_tokens` 返回并按缓存读取价计费。
- 不支持 `/messages/count_tokens`（返回 404），网关原样返回 404，Claude Code 会改用本地估算。
- DeepSeek V4 等为推理模型，输出上限过小（如 20）时 token 可能全部用于推理、正文为空，接入示例中请使用较大的 `max_tokens`。
- 实测 OpenAI Python SDK（普通、流式、Responses API）、Anthropic Python SDK（普通、流式，经 `claude-*` 别名）与 Claude Code CLI（含读取文件的工具调用）均可正常使用，调用记录、计费与首字耗时正确。Claude Code 使用非 Claude 模型时会提示无法识别模型、按 200k 上下文处理，不影响使用。

Claude Code 会按自己的默认模型名请求（如 `claude-opus-…`、`claude-haiku-…`），使用 Claude Code 的活动必须配置 `claude-*` 等前缀别名，否则请求会因模型不在白名单而被拒绝。

- 队伍使用：OpenAI 兼容 SDK `base_url=https://<域名>/gotc/v1`；Anthropic SDK `base_url=https://<域名>/gotc`。支持 `Authorization: Bearer` 或 `x-api-key`，支持流式输出。
- 网页端密钥管理（`/api/team/keys` 及子路由）必须携带页面打开时的 `X-GOTC-Login`，不能从后续登录自动换取新标识继续旧操作。缺失标识返回 428，标识与当前会话不符返回 401；旧标签须刷新并核对登录身份。网页会在切换账号后清除已展示的完整密钥并锁定旧卡片；SDK `/v1/*` 仍仅使用 API Key，无需此请求头。
- 后台设置每队密钥上限、统一到期时间、每队每分钟请求上限（默认60，0为不限）、屏蔽词和队伍公告。每队最多 4 个并发请求，超出时排队最多 30 秒再返回 429。
- 每次请求在排队取得并发名额后会再次校验密钥、队伍状态与到期时间。上游调用按实际 token 用量统计费用；缺少用量时进行估算。Anthropic 请求未设置输出上限时补 `max_tokens: 8192`，显式输出上限最多 128000。
- 后台管理参照 sub2api：
  - 模型上游可“测试已保存的连接”（向第一个模型发送 1 个 token 的请求）与“自动拉取模型列表”。拉取使用当前表单的地址和密钥，无需先保存或填写模型；密钥留空时，只能在地址主机和端口未变化时沿用已保存的密钥。读取上游 `/models` 后可选择模型加入配置，已有模型和价格保留，新模型须补全价格后再保存。拉取不修改配置，也不占用数据库写锁；上游不支持列举模型时仍可手动填写。
  - 今日概况另显示近5分钟每分钟请求数与 tokens、今日成功请求 P95 耗时与平均首字耗时；用量详情可按日或按小时查看；调用记录可按模型（请求名或实际模型）与请求 ID 筛选。
  - 调用内容（请求与回复）默认保留 72 小时、总容量上限 2048MB，超出后从最早的内容开始自动删除（每 10 分钟检查一次），后台可随时一键清空；用量与费用记录始终保留。sub2api 完全不保存请求内容。
- Responses API 安全：所有队伍共用同一个上游账号，因此 `/v1/responses` 一律以 `store: false` 转发，上游不保存任何状态；`previous_response_id`、`item_reference`、`conversation`、预存 `prompt` 与 `background` 一律拒绝（后台任务会在本次调用结束后才计费，无法在当前调用中统计完整费用）。客户端需在 `input` 中提供完整上下文（Codex 使用 API 密钥时即如此）。OpenAI 兼容接口未设置输出上限时，以 128000 个输出 token 作为缺少用量时的估算值（不向上游注入字段），正常调用按实际用量统计费用。
- 上游文件隔离：网关没有上游文件的队伍归属记录，因此拒绝 Responses 的 `input_file.file_id` / `input_image.file_id` / `computer_screenshot.file_id`、Chat Completions 的 `file.file_id`、Anthropic 的 `source.type: file` / `container_upload.file_id`（含工具结果中的嵌套内容及 `count_tokens`）。返回 400，不转发、不计费；可改用内嵌文件内容或图片/文件 URL。普通文本、工具参数和工具定义中出现 `file_id` 不受影响。
- 转发与协议参照 sub2api：
  - 支持 `/v1/chat/completions`、`/v1/messages`（含 `count_tokens`）与 `/v1/responses`（Responses API，Codex CLI 使用；需上游本身支持该接口）。`/v1/models` 同时带 OpenAI 与 Anthropic 两种格式的字段，并支持 `/v1/models/{id}`。
  - 每个响应带 `X-Client-Request-ID`，与调用记录中的请求 ID 一致，便于队伍反馈问题；上游的 `x-request-id`、`request-id`、`Retry-After`、`openai-processing-ms` 透传给队伍，上游限流额度相关的头不透传（它们描述的是组织方共享的上游密钥）。
  - 向上游发送客户端自己的 `User-Agent`（无则为 `gotc-gateway/1.0`），并向 Anthropic 透传 `x-app`、`x-claude-code-session-id`、`x-stainless-*` 等少量请求头。
  - 请求体上限 32MB，支持 `Content-Encoding: gzip/deflate`（解压后同样限制 32MB）；以紧凑 UTF-8 JSON 转发，`NaN`/`Infinity` 直接拒绝。
  - OpenAI 兼容接口不再补 `max_tokens`（o 系列与 gpt-5 不接受该字段），只限制客户端已设置的输出上限；Anthropic 未设置时补 `max_tokens: 8192`，被限制后 `thinking.budget_tokens` 同步调低以保持有效。
  - 连接与 TLS 握手 10 秒超时；等待回复开始最长 300 秒；流式回复两次事件之间超过 180 秒即结束并补发错误事件。上游过载（429/503/529）最多尝试 5 次，间隔 0.3 秒起翻倍、最长 3 秒，并参考上游的 `Retry-After`。Nginx 对 `/gotc/v1/` 的读取超时为 330 秒、请求体上限 32MB。
- 鉴权与限流参照 sub2api：
  - `Authorization: Bearer`（大小写不敏感）或 `x-api-key`；网址中携带 `key` / `api_key` 参数直接拒绝（400），避免密钥留在代理日志。
  - 同一来源地址（IPv6 按 /64）每分钟无效密钥超过 120 次即封禁 60 秒，封禁期内该地址的无效密钥返回 429 与 `Retry-After`；有效密钥不受影响（比赛现场所有队伍通常共用一个出口地址）。后台概况显示近1小时无效尝试次数与被封禁地址数。
  - 每队最多 4 个并发、另可排队 20 个（排队最长 30 秒），超出立即返回 429；每分钟请求数只计入真正开始执行的请求，排队超时或被拒绝的请求不占用名额；所有 429 都带 `Retry-After`。`count_tokens` 计入每分钟请求数（未设置时默认每分钟 120 次）但不占用并发名额，另有每队 4 个的独立并发上限。
  - 每队每小时最多创建 60 个密钥，删除密钥不返还次数。
  - 屏蔽词检查模型会读取的全部文本：所有消息、系统提示、工具定义、工具参数与结果（不检查 JSON 字段名，只跳过真正的图片与文件数据），先做 NFKC 规范化、去除零宽字符、合并空白、不区分大小写，命中的屏蔽词记录在后台；sub2api 只检查最后一条用户消息，但伪造的历史消息可借此绕过，因此这里更严格。
  - 缺少用量时的输入估算：文本按字节/4 计算且不设上限，每张 base64 图片按 1600 token 计（不按其字节数），其他 base64 内容（如 PDF）按字节/4 计。
  - API 设置的修改记录保存每个改动项的旧值与新值。
- 异常请求的记录与计费参照 sub2api：
  - 每次调用记录真实状态码，失败调用另记错误类型、阶段（request 请求校验 / auth 鉴权 / upstream 上游 / network 网络 / internal 内部）、责任方（client 学生请求 / provider 上游 / platform 平台）、上游状态码与脱敏后的上游错误信息（去除上游密钥与类似密钥的字符串）。后台调用记录、失败原因统计与 CSV 导出均包含这些字段；队伍页只显示错误类型与责任方。
  - 错误响应使用各接口原生格式：`/v1/messages` 为 `{"type":"error","error":{"type","message"}}`，`/v1/chat/completions` 为 `{"error":{"message","type","code"}}`。模型不在白名单返回 404；访问权限已到期返回 403 `billing_error`。
  - 上游错误：400 原样转发；401/403 与 5xx 返回 502；429 返回 429；529 返回 503 `overloaded_error`；其他 4xx 在 `/v1/messages` 返回 502、在 `/v1/chat/completions` 保留原状态码与脱敏信息。上游返回错误时不计费。
  - 上游返回 200 但内容无法解析，或 200 中带错误对象：返回 502/503，不计费。
  - 流式回复先读取首个事件再向学生发送：首个事件即为错误时按 HTTP 错误返回、不计费；输出开始后中断或上游发出错误事件时，向学生补发一条错误事件（上游已发出的错误事件不重复补发），记录为失败（502，超时为 504），按已产生的用量计费（缺失时估算）。
  - 学生在排队期间断开：返回 499，不转发上游、不计费。学生在流式输出中途断开：继续读完上游并按完整用量计费。
  - 请求已发出后，上游返回非法 HTTP 状态行或过长响应头：返回并记录上游/网络错误 502，按估算用量统计费用一次，然后释放并发名额；不重试，避免重复调用。
  - `count_tokens` 失败按上述方式映射状态码，始终不计费。网关内部错误记录为 500，且并发名额一定释放。
- 上游调用失败按“请求是否已完整发出”计费，而不是按错误类型猜测：连接、TLS 握手（含证书校验失败）、经 `HTTPS_PROXY` 建立隧道或发送请求阶段的失败，上游不可能收到完整请求，队伍收到 502 且不计费；请求发出后的超时（504）、连接中断或回复被截断（502）因上游可能已在生成并计费，按本次估算用量计费并标记“流中断、估算计费”。上游返回 401/403/5xx 时队伍收到 502、不计费。所有失败都记录真实状态、计入失败统计，调用内容中注明原因与是否计费（如“上游 TLS 证书校验失败……（请求未发出，不计费）”）。仅在请求未发出且属于临时连接问题（连接被拒/重置、域名解析失败）时重试；证书等 TLS 配置错误与超时不重试。
- 流式回复缺少结束事件（中断）会标记为“流中断”并计入失败；上游未返回用量时按“请求字节/4、回复文本字节/4”估算计费并标记“估算计费”，不会按 0 元记账。
- 上游明确表示未处理（429/503/529）或无法建立连接时，在向客户端发送任何内容之前最多重试 2 次（间隔 1 秒、2 秒）；超时与其他 5xx 可能已在上游计费，不重试。
- 屏蔽词对 `messages` 与 `system` 做不区分大小写的包含匹配，命中返回 403 且不转发上游。
- 支持 Claude Code：网关路径接受查询参数（如 `?beta=true`，忽略不转发），并转发 `/v1/messages/count_tokens`（不计费、不记录）。
- 后台“AI API 管理”包含今日概况、近24小时消耗排行（含进行中请求数）、用量 CSV 导出（不含请求内容）和 API 管理操作记录；队伍页显示公告、接入示例（Claude Code、Anthropic / OpenAI Python、curl）和本队最近调用。
- 缓存读写 token 按输入单价计，偏保守。上游返回 401/403/5xx 时统一返回 502，不暴露上游信息；参数错误、限流等 4xx 原样返回便于调试。
- 服务器访问不到上游时，在 systemd 单元设置 `HTTPS_PROXY`。Nginx 需为 `/gotc/v1/` 关闭缓冲、放宽超时与请求体（`deploy/install.py` 已包含）。
- 网关不可用或访问权限到期只影响 API 调用，提交与评分不受影响。
- 调用记录：每次通过密钥认证的调用（包括被规则、到期时间或模型白名单拒绝的）都会记录时间、队伍、密钥、模型、状态、token、费用、耗时和 User-Agent。开启“记录请求与回复内容”（默认开启）时，另存请求头（去除 Authorization、x-api-key、Cookie）、客户端原样发送的完整请求体（即完整上下文，最大 4MB）和回复（最多 16MB），以 zlib 压缩保存；队伍页会提示正在记录。后台“查看上下文”提供对话视图（system、每条消息、工具调用与结果、由流式事件还原的回复）、原始内容、请求头和 JSON 下载；查看单条内容会写入审计日志。Agent 每次请求都会重发整段对话，存储增长较快，记录没有自动清理，活动结束后按需删除 `api_logs`。

| 方法与路径 | 身份 | 请求及响应 |
|---|---|---|
| POST `/v1/chat/completions` | API 密钥 | OpenAI 兼容请求，转发到 `openai` 上游 |
| POST `/v1/messages` | API 密钥 | Anthropic 请求，转发到 `anthropic` 上游 |
| POST `/v1/messages/count_tokens` | API 密钥 | 转发到 `anthropic` 上游，不计费 |
| GET `/v1/models` | API 密钥 | 开放模型列表 |
| GET `/api/team/keys` | 队伍 | `{enabled,baseUrl,models,maxKeys,keys:[{id,name,masked,used24h,usedTotal,requests,lastUsedAt,createdAt}],usage:{used24h,usedTotal},expiresAt}` |
| POST `/api/team/keys` | 队伍 | `{name}` → `{ok,key,item}`，`key` 仅此一次 |
| POST `/api/team/keys/delete` | 队伍 | `{id}` → `{ok}`，密钥立即失效，用量记录保留 |
| GET `/api/team/keys/logs` | 队伍 | 本队最近50次调用（不含内容） |
| GET `/api/admin/keys/export` | 管理员 | 用量 CSV（不含请求内容，防公式注入） |
| GET `/api/admin/keys` | 管理员 | `{enabled,settings,models,overview,changes,teams:[{teamId,active,keyCount,inflight,used24h,usedTotal}]}` |
| POST `/api/admin/keys/settings` | 管理员 | `maxKeys` 1–20、`expiresAt` 毫秒或 null、`logContent` 布尔、`teamRpm` 0–10000、`announcement` ≤500字、`blockedWords` ≤200个，可单独更新 |
| POST `/api/admin/keys/upstreams` | 管理员 | `{upstreams:{openai?,anthropic?:{url,key,models,aliases?}}}`，整体替换；`key` 留空沿用原值；`{}` 关闭网关 |
| POST `/api/admin/keys/upstreams/models` | 管理员 | 携带页面的 `X-GOTC-Login`，`{kind:"openai"或"anthropic",url,key}` → `{ok,status,latencyMs,models?,message}`；读取当前表单的模型列表，不保存配置；密钥留空按同主机、同端口规则沿用 |
| POST `/api/admin/keys/stats` | 管理员 | `{teamId?,keyId?,days:1–90}` → `{totals,byKey,byModel,byClient,daily}`，按北京时间分日 |
| POST `/api/admin/keys/logs` | 管理员 | `{teamId?,keyId?,errorsOnly?,beforeId?}` → `{items,more}`，每页50条，按时间倒序 |
| POST `/api/admin/keys/log` | 管理员 | `{id}` → `{headers,request,response,requestBytes,responseBytes,responseTruncated}`，记录查看操作 |
| POST `/api/admin/keys/provision` | 管理员 | `{teamId}` → `{ok}`；重新开通后原密钥恢复可用 |
| POST `/api/admin/keys/revoke` | 管理员 | `{teamId}` → `{ok}`，该队全部密钥立即停止调用 |

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

名录 `team` 增加 `status`（`active|withdrawn|disqualified`）、`statusReason`、`booth`、`roadshowOrder`（1–9999 或 null）、`revision` 和 `trackKnown`。退出或取消资格须填写原因。非 active 队伍不进入当前公开专业榜单及原专业评分、观众投票的可选名单，继续评分或投票返回 `409/team_ineligible`；历史评分和票数保留供后台查看，恢复 active 后重新参与当前名单与排名。直接调用 `POST /api/admin/teams` 更新已有队伍必须携带 `expectedRevision`；缺失或 null 返回 428，旧版本返回 409，新建队伍不需要该字段。队伍正式提交改变公开作品名也会增加名录版本。赛道选项为 AI for Science、城市与社区问题、未来学习样态；升级前未知赛道原样保留并标注，不能自动归类。状态、展位和路演顺序不改历史快照，不自动选队或排程。

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

达到最少不同专业评委数且成绩已公开，才进入公开专业看板候选。平均分涵盖全部有效正式提交，不去最高最低，保留两位小数。服务端按该可比分数降序、数值队号升序截取前 12 名，公开位置连续为 1–12，不追加同分队伍；未发布或无合格队伍时 teams 为空。公开接口不返回其他队伍、评委身份、反馈、各维分或单份分数。`stats:{teamCount,submittedScores,audienceVotes}` 独立统计全场，避免将前 12 名当作全场。评委 `/api/judge` 和观众 `/api/audience` 保留完整名单及进度/票数，非前 12 名的 score/rank 为 null。后台仍可查看达人数门槛的全部分数及原并列名次，即使尚未向公众发布。展示/路演进度接口不受此截取影响。

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

新正式提交的 `codeUrl` 应填写完整 HTTPS 仓库主页（不是代码包、文件页、个人页或本地路径）。GitHub、Gitee、GitCode、GitLab 会检查仓库根地址形状；其他平台保持原 HTTPS 格式兼容，须直接填完整 SHA。例如 `https://github.com/your-team/your-project`。队伍应保证仓库公开、代码已 push；平台不联网检查存在性或可访问性。既有材料只读保留，旧草稿可继续保存，重新正式提交时按新规则补齐。

`codeVersion` 输入40/64位十六进制完整SHA，或 GitHub、Gitee、GitCode 的 `/owner/repo/commit/<完整SHA>`、GitCode 的 `/owner/repo/commits/detail/<完整SHA>`、GitLab 的 `/group/subgroup/repo/-/commit/<完整SHA>` 链接。仓库/commit链接主机和仓库路径必须一致，GitHub/Gitee路径忽略大小写，GitCode 和 GitLab 按原路径匹配大小写，GitLab 支持多级子组；允许仓库尾部 `.git` 和尾斜杠。拒绝凭据、显式端口、查询参数、片段、百分号编码及点路径。其他平台 commit 链接无法可靠解析时提示改填完整 SHA，不限制其原有 HTTPS 仓库输入。这里只做语法和路径比较；直接填SHA不能证明其属于该仓库，commit链接也不证明提交实际存在。正式保存小写完整SHA，草稿保留输入；摘要前6位不保证唯一，应使用完整值核对。

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

队伍使用原提交码进入 `#team-score` 查看本队专业总分、六维均分及匿名逐条评语。`GET /api/team/score` 根据队伍会话绑定确定队伍，要求当前页面的 `X-GOTC-Login`，禁止查询参数指定其他队伍。返回 `{team:{number,name},state,score,dimensions,feedback}`；`state` 为 `published`、`unpublished` 或 `awaiting_scores`。已发布且达到 minJudges 门槛时，score 为百分制专业总均分，dimensions 为六项 `{id,name,max,score}`，每项为同一批全部 status=submitted 记录的该维算术均分，保留两位小数；feedback 为非空、知情确认分享的正式评语字符串数组，按文字稳定排序，保留换行。旧评分数值仍计入各均分，scores_json 本身为原分值，仅 total_tenths 按十分之一存储。是否进入公开前 12 名不影响本队查询。其他状态 score 为 null，dimensions/feedback 均为空数组；不返回个人分数、评委标识、时间或逐评委数值与评语关联。

专业评分新表单明确告知：本次正式非空评语将在发布且达到门槛后匿名分享给本队，请勿署名或留下联系方式；正式提交非空评语前必须勾选确认，每次提交重新确认。API `POST /api/score` 仅在本次显式附带 `feedbackSharing:"team-anonymous-v1"` 且正式提交非空评语时建立分享授权；省略字段的旧客户端仍可私下保存但不会分享，旧评语不自动公开。无效声明返回 400。授权使用独立 score_feedback_visibility 表绑定本次 score_write_revisions.revision；scores 的插入、更新、删除触发器先清除授权，新服务仅在本次知情提交完成后重新写入。因此旧客户端、旧代码回滚及直接旧格式 SQL 改写不能沿用先前授权。原 scores 七列不变，原评委私有查看、组织方导出不变。

### Private registration and in-person submission-code claims

`platform.html#claim` collects the captain and 2–4 additional teammates (3–5 people
total), each with a name and school; cross-school teams are supported. Duplicate
normalized name+school pairs within a group are refused. Import the private roster
before opening the desk: until at least one roster row exists, new public
applications return 503. No roster or name-based credential lookup is public.
Public applications return an opaque request ID, a 256-bit bearer receipt secret,
and a six-digit on-device verification PIN; validity is 24 hours. The receipt is
stored in this tab's sessionStorage. Storage failures remain visible. Clearing
this device or successfully logging out clears the receipt; codes are never
persisted in browser storage.

The admin section **现场报名核验** fetches requests, roster and eligible active
teams only when opened with its read button. Create teams through the existing
team directory first. New applications show the exact submitted group and require
one roster selection per declared person, including the captain; nobody can be
added or omitted. Compare every submitted name/school to the selected record,
enter the PIN shown on the applicant's
device, and confirm the in-person verification. A school discrepancy or any
`needsReview` member requires an explicit resolution checkbox and review note.
Every declared normalized name must match its mapped roster record; school
aliases require explicit resolution and a note. Old applications without a
declaration retain the existing manual member selection flow. An identity typo or
parent-filled record can be corrected with the per-person revision form before
assignment. Once assigned, the person's name/school cannot be rewritten; notes
remain editable. Preserve source lines and originals in the private notes.

Every admin claim endpoint requires the current admin cookie and `X-GOTC-Login`
marker from `/api/session`, for both reads and writes. All POSTs also require the
existing exact Origin and JSON protections. No endpoint accepts URL parameters.

| Endpoint | Input / behavior |
| --- | --- |
| `POST /api/admin/claims/roster` | `{people:[{id,name,school,notes,needsReview}]}`; 1–500 rows, total ≤500, 2 MiB import body limit. Stable IDs use ASCII letters/digits/underscore/hyphen. Exact re-import is idempotent; changed existing rows must use per-person edit. Duplicate normalized name+school is refused. |
| `POST /api/admin/claims/person` | `{person:{id,name,school,notes,needsReview},expectedRevision}`; revision 0 adds, an existing row's revision edits. |
| `GET /api/admin/claims` | Admin-only `requests`, `people`, and eligible `teams`; no receipt secret, PIN, account code, or code hashes. |
| `POST /api/claims/request` | `{name,school,members:[{name,school}]}`; 2–4 teammates; legacy clients may omit `members`. Identical response shape for known/unknown identities. Maximum 1,000 unexpired pending requests; public body limit remains 16 KiB. |
| `POST /api/claims/status` | `{requestId,secret}`; fixed invalid shape or status, `requestRevision`, this claimant's `captain` and `declaration` (`null` for legacy), and bearer-approved team; never a code. |
| `POST /api/claims/update` | `{requestId,secret,expectedRevision,members}`; pending/unexpired only. Replaces the pending declaration with a new snapshot and increments request revision; preserves receipt secret/PIN/expiry. Also supplements old pending applications. Invalid receipts use the fixed invalid response. |
| `POST /api/claims/redeem` | `{requestId,secret}`; approved receipt returns its repeatable code; exact retries preserve the credential. |
| `POST /api/admin/claims/review` | Common `{requestId,expectedRevision,action,note}`. `reject` needs only these; `approve` also needs `{teamId,captainId,memberIds,memberRevisions,pin,inPersonVerified:true,resolvedIdentity:boolean}`. Declared applications additionally require ordered `declaredMemberIds` (captain first), exactly covering the declaration and selected member IDs. |

Public request/status/redeem/update endpoints each use a fixed, separate rate-limit
scope (20 per IP / 400 global per minute). Incorrect organizer PIN attempts are
durable and limited to five per request; a fresh application is then needed.
Pending requests never reserve people. Approval transactions enforce unique
person membership and unique approved team through SQLite constraints. No
partial membership/account survives a conflict. An identical approval retry is
idempotent. Requests and per-person edits use expected revisions to reject stale
writes. Approval also requires an exact `memberRevisions` mapping from every
selected member ID to the revision displayed in the roster. The transaction
rejects changed identities or notes with 409 before assigning anyone; refresh
and re-verify the selection. Exact successful retries retain their original
snapshot, including when a later notes edit changes the current roster.
New declarations require 3–5 people; legacy applications retain their manual
roster-bound approval behavior. Approval digests for declarations bind the exact
snapshot, ordered mapping and base request revision, so a participant edit makes
an open staff form stale before any assignment. Exact approval retries still
succeed after pending edits and later redemption. Pending claimants can edit
teammates or supplement legacy requests from the receipt page; refresh recovers
the submitted group using only the bearer receipt. Browser storage contains only
request ID, receipt secret, PIN and expiry, never team declarations or login codes.

Approval creates a team account once through existing `team_access` with the
organizer actor and discards the temporary code. Redemption derives
`account_id + '.' + sha256('gotc-claim-code-v1\0' + receipt_secret).hexdigest()`
(the separator is a NUL byte), stores a fresh normal salt/scrypt hash once, and
never stores plaintext credentials. The approved request records a credential
fingerprint, checked before first issuance and every retry. Admin rotation or
revocation invalidates the old receipt's ability to issue a code. Teams with an
existing account require the existing recovery controls; claims never rotate
that account or provision API gateway keys. Rejected/expired requests release no
assignments because pending requests never had any.

Approval review has its own 128 KiB body limit so all 500 roster IDs and their revision mapping fit; the
2 MiB import limit applies only to roster import.

The private tables are `claim_people`, `claim_requests`, `claim_memberships`, and
`claim_declarations` (one current snapshot per request, keyed by request ID and revision;
superseded pending snapshots are pruned atomically and approved declarations are frozen);
initialization is additive and idempotent for old installations. Organizer
approval/rejection, roster import/edit, failed PIN checks and redemption write
the existing durable audit trail. Do not commit real roster files, release
archives, database copies, or temporary test artifacts.
