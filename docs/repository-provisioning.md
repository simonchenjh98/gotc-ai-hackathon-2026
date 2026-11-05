# 组织仓库申请与固定 SHA 提交

本功能运行在现有 Python 单实例服务与原生 JavaScript 平台中。官方网站入口为 https://etownyouthai.com/ ，使用首页实际提供的作品提交入口进入平台；本文不假定官网已经部署同域平台。代码、数据库迁移与模拟远端测试可离线验证；真实 GitHub/Gitee 组织凭据、邀请权限、分支保护及生产部署仍需单独验收。未配置时渠道显示“尚未启用”，不会伪造仓库或 SHA。

## 服务端配置

仅在服务进程的私有环境配置中设置以下值，重启进程生效。不要放入前端、静态目录、Git 仓库、日志、工单或公开截图。

| 环境变量 | 默认值 / 含义 |
| --- | --- |
| `GOTC_REPO_GITHUB_ORG` | `GOTC-AI-HACKATHON`，唯一允许的 GitHub 组织 |
| `GOTC_REPO_GITHUB_TOKEN` | 空，空值禁用 GitHub 渠道 |
| `GOTC_REPO_GITHUB_BRANCH` | `main`，每队固定的正式分支 |
| `GOTC_REPO_GITEE_ORG` | `gotc-ai-hackathon`，唯一允许的 Gitee 企业命名空间（保留 ORG 环境变量名） |
| `GOTC_REPO_GITEE_TOKEN` | 空，空值禁用 Gitee 渠道 |
| `GOTC_REPO_GITEE_BRANCH` | `main`，每队固定的正式分支 |
| `GOTC_REPO_GITEE_ENABLED` | 仅精确的 `1` 显式启用；同时需要令牌 |
| `GOTC_REPO_GITEE_SEAL_ENABLED` | 仅精确的 `1` 启用新的 Gitee 封存预览，默认关闭；已签发的封存预览仍在有效期内本地确认 |
| `GOTC_REPO_BACKUP_ENABLED` | 仅精确的 `1` 在主进程启动单个私有源码备份 worker，默认关闭 |

组织和分支只允许 1–100 位 ASCII 字母、数字、下划线与连字符，首位须为字母或数字。修改配置不会改变已有申请/绑定的组织或分支；组织不匹配时停止远端访问，由管理员人工核对。分支创建以初始化 README 的默认分支为起点，不覆盖已有分支。仓库公开，初始化只含非敏感 README，不自动选择项目许可证。

调用主机固定为 `https://api.github.com`、`https://gitee.com/api/v5`；不支持自定义 API 主机、GitHub Enterprise Server 或自建 Gitee。禁止 HTTP 重定向，单次请求 8 秒超时、响应最多 2 MiB。上游错误只保留通用提示，丢弃响应正文和含认证信息的 URL。Gitee API v5 按官方文档通过 `access_token` 参数认证，运维代理不得记录其请求 URL。服务自身不记录这些请求/令牌。

GitHub 凭据须允许创建组织仓库、读元数据/分支、添加仓库协作者并写分支保护。优先用组织批准的 GitHub App 或最小范围的 fine-grained token：仓库 Administration write（建仓、邀请、保护）及 Contents read/write（读取提交、必要时创建正式分支），仅允许活动组织和所需仓库。具体组织策略、App 安装的仓库覆盖和套餐必须实测；经典 PAT 公开仓库至少涉及 `public_repo`，权限更宽，勿授予不必要的组织管理权限。开发者通过仓库协作者接口仅获得 `permission: push`，不获得组织/企业管理员权限。

Gitee 本活动命名空间已由公开只读仓库元数据确认为 enterprise，建仓固定使用 API v5 `POST /enterprises/{enterprise}/repos`，不会尝试 `/orgs/{org}/repos`。`private` 必须是整数 `1`（外部开源），`outsourced` 为 false；不是普通组织接口的布尔 false。创建、恢复和 SHA 读取均核对仓库 `namespace.type` 精确为 enterprise。当前只支持 Gitee 企业命名空间，普通组织模式不支持，失败或结果不确定时绝不自动切换端点。协作者使用仓库级 API v5，必须传 `permission: push`（官方值为 pull / push / admin）。实际企业建仓授权、成员权限、席位和令牌 scope 仍未经过凭据验收，不能仅凭公开元数据或配置成功宣称可用。Gitee 正式开发分支的保护状态保留“尚未验证”状态，须管理员人工设置并验收。

Gitee 账户级令牌无法把“创建未来仓库”的能力限制到单一活动组织，须由专用主办方账户持有，避免其同时管理无关项目。令牌表单的必选 `user_info` 与项目范围 `projects` 是待验收的最小候选；当前官方 OpenAPI schema 没有逐端点 scope 映射，因此不能声称该组合已验证。此实现不增加组织/企业成员，不自动申请额外 scope；企业创建端点是否另需 `enterprises` 不能从当前 schema 得出，须在实际授权前由管理员核对并验收，不得自动扩大令牌权限。

## 队伍与管理员流程

队伍用原有队伍登录码进入“本队代码仓库”，选择已启用渠道并填写 1–5 个开发者的平台用户名（不是邮箱、主页网址或显示名），点击“申请本队仓库”。本队只能申请一次，不能传入组织、仓库名称、任意仓库 URL 或别队编号。仓库名由服务器按队号和内部 ID 哈希生成，避免参赛者控制名称或跨队绑定。

管理员在“组织仓库申请与恢复”中逐一在平台外核对用户名归属，勾选“我已逐一核对以上用户名归属本队开发者”，点击“核对并批准建仓”。服务器创建独立仓库、初始化正式分支、尝试禁止强推与删除，并邀请开发者写入。GitHub 返回邀请时显示“邀请待接受”；开发者到 GitHub 接受邀请后，管理员用“核对恢复 / 刷新邀请”确认状态。该按钮对已知身份的仓库重新核对并执行幂等协作者授权，不创建第二个仓库。所有开发者都已加入后显示“仓库已绑定”。Gitee 要求协作者写入返回官方的 200 成员对象，再独立读取同一用户名与成员 ID 的有效权限；只有精确的 push 权限证据才标记已绑定；若有效权限仍是 admin，则保留待核对状态并提示主办方人工处理多余权限，平台不会自动修改组织或企业继承权限。空响应、待处理、只读或身份不符均保留待核对状态，不将 HTTP 成功等同于授权已生效。

队伍点击“刷新仓库状态”后可查看仓库与固定分支。已绑定队伍的手填代码 URL / commit 字段隐藏且没有 required 校验错误；其他材料照常填写。点击“检查并正式提交 ↗”时服务器读取完整 SHA，生成有效期五分钟、仅属于本队和当前登录会话、绑定仓库修订号的预览。预览展示完整 SHA；“确认正式提交”带预览编号发送材料。对 GitHub 和未开启封存的 Gitee，服务器再次检查远端仓库 ID、分支 head、绑定修订、有效期和提交窗口，拒绝 SHA 已变化、跨队/跨登录预览或手动覆盖代码字段。此检查与写回之间仍可能有新的 push；平台保存已核对的确切 SHA，不会默默替换成更晚提交。

正式材料、精确请求回执和不可变历史使用原有原子事务。相同请求编号和相同内容重放时优先返回原回执，即使预览过期、提交截止、远端不可用或之后已更新也不改变先前成功结果。新内容须新请求编号。草稿保留旧字段兼容；没有绑定仓库的旧队伍继续原来的手填流程。

## Gitee 封存预览与本地确认

启用封存后，每次预览先核对企业仓库数字 ID，再读取正式分支完整 SHA。服务器先将该队和仓库身份对应的随机前缀写入 SQLite，然后通过 Gitee v5 执行以下操作，网络请求期间释放数据库写锁。

1. 对 `<前缀>-*` 建立 standard 保护规则，推送和合并均只允许 admin。
2. 用完整 SHA 作 `refs` 创建 `<前缀>-<完整 SHA>` 分支。同名引用已经存在时，必须匹配同一个 SHA 并具有保护标记。
3. 对确切分支设置 standard 规则，推送和合并均为 none。检查返回的规则 ID、项目 ID、名称、许可角色、模式和空豁免列表，再独立读回分支保护状态和 SHA。完成上述核对才返回带 `repositorySeal` 的预览。
4. 响应丢失后重试复用持久化的原名称。Gitee 对已存在规则返回 400 时，只允许更新本次本地预留的名称。同一 SHA 再次预览时重新核对，发现不同 SHA 即停止，不覆盖分支。

确认采用预览中固定的 SHA。后续 push 如需纳入提交，队伍须重新预览。确认只在本地验证登录会话、队伍、仓库身份、绑定修订、SHA、预览有效期与提交窗口。服务器读取完整合法 JSON 后记录 `receivedAt`（Unix 毫秒），用它判断截止和五分钟预览有效期。等待数据库期间管理员手动关闭窗口或撤销身份仍会拒绝。实际保存时间见 `savedAt` 和 `submittedAt`。

封存凭据含 `id`、`ref`、`sha`、`sealedAt` 和固定 commit 的 `viewUrl`。正式回执、版本详情、本队最新正式版的 `repositorySeal` 另含 `receivedAt`，回执也在顶层提供 `receivedAt`。版本关联与正式历史、原始请求回执在同一事务持久化，旧正式版本的详情和本队响应中 `repositorySeal` 为 null。封存预览和确认只交换仓库元数据，不下载源码、PPT 或视频。

在测试企业仓库中，精确规则拒绝了管理员 Git 推送、非快进强推和删除。管理员仍可通过 contents API 追加提交，因此正式记录与查看链接永久指向确认的原 SHA。普通协作者受平台角色规则约束是本活动接受的假设，尚未用普通账号实测。

## 异步私有源码备份

需要主机已安装 Git（生产基线为 Git 2.43.0）和 Python 3.12，无新增 Python 依赖。新封存正式版在原提交事务内排队；预览、确认和原始回执都不下载源码。即使 worker 未启用，任务也保留 queued；重启后可恢复。后台发布完成后若 SQLite 暂时无法提交，下一轮继续登记同一份结果，不重新抓取或增加尝试次数。旧版、未封存版和 GitHub/手填提交没有任务。同一仓库数字身份、封存 ref 和 SHA 的其他正式版会在后台复用已校验的私有包，避免只改 PPT/摘要时重复抓取和占用空间；队伍版本关联与下载授权仍分别校验。备份状态独立于正式提交成功状态，失败不撤销提交，也不改写历史或原始回执。

默认只运行一个 worker，每 5 秒轮询，最多尝试 3 次，失败后分别等待 60、300 秒。每个任务的 Git/生成阶段共享 180 秒墙钟上限；单任务临时文件与输出限额 268435456 字节，备份目录总限额 2147483648 字节，选定 commit 的源码内容上限 134217728 字节、最多 20000 个文件。运行期间轮询磁盘、剩余空间、停止标记和时间，必要时终止整个 Git 进程组；子进程还有文件大小限制。存储不足时重试同一个任务，不删除既有正式备份。备份文件保存在 `GOTC_DATA_DIR/repository-backups` 的私有目录，不属于静态发布目录；主机备份和容量规划也须覆盖此目录。

worker 从不可变封存关联读取原仓库数字 ID、命名空间、仓库名、ref 和 SHA，首先确认当前启用的提供方仍允许该组织，再在数据库写事务外向 Gitee 核对数字身份。只从官方 Gitee HTTPS 地址获取该 ref 的完整历史；即使管理员将 ref 推进到后代提交，仍只打包原 SHA。原 SHA 不在抓取历史内则重试/失败，不以最新 head 替代。Git 禁用凭据 helper、系统/全局配置、重定向、hooks 和非 HTTPS 协议，pack 单线程、windowMemory 为 32m。令牌通过仅 fetch 子进程可见的环境交给私有 askpass；不放进 URL、argv、归档或错误内容。`/user` 只用于验证并缓存令牌账户的 login。

归档含确切 commit 的所有已跟踪文件（包含 export-ignore 文件，不展开 export-subst）、可独立恢复的 `repo.bundle` 与私有哈希清单。ZIP 和 bundle 都校验后原子发布，路径或文件损坏时不提供下载。服务器不会 checkout、解包、执行项目、运行过滤器或拉取子模块。检测到 LFS 指针或 gitlink 时状态为 partial。私有 manifest.json 保留完整路径清单；接口和数据库仅保存路径样例 `missingAssets`（最多 16 项，JSON 转义后数组不超过 4096 字节），每项为 `{path,kind}`（kind 为 lfs 或 submodule）。`missingAssetCount` 为实际缺失项总数，`missingAssetOmitted` 为未列出的项数，样例为空仍可能是 partial；指针本身仍保留在源码 ZIP。ready 仅表示已保存这些 Git 字节，不证明依赖齐备、项目可运行，也不包含 PPT/视频备份。

本队最新响应和正式版本详情另含 `repositoryBackup`，旧版为 null；字段为 `status,sha,attempts,error,sourceZipSha256,bundleSha256,missingAssets,missingAssetCount,missingAssetOmitted,downloadUrl,bundleUrl`。状态包括 queued/running/ready/partial/failed；仅校验完毕的 ready/partial 提供同源链接。错误为固定代码，不含上游正文或本机路径。原始 receipt 不加入这些可变状态，重放仍返回原 receipt。

`GET /api/submission/backups/{team_id}/{version}/source.zip` 和 `/repo.bundle` 仅允许管理员或所属队伍的现有登录 cookie；链接无 token，下载流按 64 KiB 分块，固定 MIME、attachment 和 nosniff。匿名、其他队伍和评委不得下载。评委继续使用已有固定 Gitee commit 链接，本次没有公开归档端点。bundle 可用 `git clone repo.bundle restored` 恢复到 selected 分支；恢复应在独立审核环境进行。

## 状态与故障恢复

申请/绑定持久化在 `repository_bindings`；状态为 requested → provisioning → invited / ready。网络错误、创建冲突、身份不匹配变为 reconcile，不声称失败意味着远端没有发生操作。数据库审计保留申请、处理和恢复事件。远端请求全部在 SQLite 写事务外，处理中的修订号防止并发覆盖。进程中断留下 provisioning 时，至少两分钟后才允许管理员恢复。该静默期仅提供并发恢复缓冲；单次 8 秒是套接字无数据超时，不保证整个操作的墙钟时长。反向代理可能先超时而后台仍在处理，管理员须先刷新列表核对状态，再核对原仓库与邀请，不能把超时当作远端未发生操作。

创建前发现同名仓库时，绝不自动认领。每次创建使用持久化的随机操作标记作为仓库说明；如果创建响应丢失，恢复必须匹配组织、生成的名称和该操作标记，再持久化远端数字 ID。已知远端 ID 后，每次核对与 SHA 读取都核对同一个 ID；删除重建的同名仓库不能替代它。身份保存早于邀请，因此邀请超时不会丢失仓库身份。

管理员先查看状态和审计、再核对组织侧仓库/邀请。使用列表当前修订号执行恢复，不能重复“批准建仓”。如果不确定的创建最终查不到仓库，系统不会盲目重建；由管理员在维护窗口核对组织审计和远端不存在后，备份数据库，人工处理原申请。当前没有忽略身份校验、任意绑定、自动重置申请或远端删除入口。用户名错误也须在授权前人工核对处理；当前没有队伍自助替换已申请开发者名单的接口。

## 强推与归档规则

禁止 force push、删除正式分支、删除/改写正式提交；违规造成 SHA 不可访问的后果由队伍承担。GitHub 分支保护成功时记录“已配置”；套餐、权限或网络阻止保护时记录“尚未验证”，不能声称强推已被技术禁止。管理员须在发放仓库后核对策略，Gitee 正式开发分支的保护须人工设置。

**封存引用与源码备份分别核验。** 预览阶段的 `archived: false` 表示此步骤仅记录引用。主办方须另行核对该 SHA 的源码备份、子模块、LFS 以及可运行性，PPT 和视频外链也须单独核对。

## API 与验证边界

所有仓库路由沿用现有 API 前缀、登录 cookie 与 `X-GOTC-Login` 当前页面身份标记；仓库路由缺少身份标记返回 428。

| 方法与路径 | 内容 |
| --- | --- |
| GET `/api/team/repository` | 本队状态与渠道可用性 |
| POST `/api/team/repository/apply` | `{provider, usernames}` |
| POST `/api/team/repository/preview` | `{}`，不允许 URL/分支输入 |
| GET `/api/admin/repositories` | 管理员申请列表与渠道可用性 |
| POST `/api/admin/repositories/approve` | `{teamId, expectedRevision, usernamesVerified: true}` |
| POST `/api/admin/repositories/reconcile` | 同上，仅恢复原操作 |
| POST `/api/submission` | 现有字段加 `repoPreviewId`；仅绑定队伍正式提交需要 |

预览记录绑定队伍、会话、SHA、仓库修订和过期时间。它不是公开查询凭据，不进入代码仓库或材料字段。单服务实例中，GitHub 与 Gitee 各自最多同时处理 4 个仓库操作（批准、恢复、SHA 预览和需要访问上游的正式确认共享同一渠道配额，封存后的本地确认无需该许可），在任何 SQLite 写事务外以非等待方式获取许可，批准/恢复获取许可后才修改持久化状态。饱和返回 503 / repository_busy，并明确本次尚未执行远端操作；释放许可使用 finally，失败和异常不会占用后续容量。

每个已登录队伍、每个渠道分别限制新 SHA 预览每滚动 60 秒 10 次、新正式确认上游核对每滚动 60 秒 20 次，二者独立。仅在实际获准进入上游操作前计次，上游错误也计次；忙碌拒绝不计次。超额返回 429 / repository_rate_limit，提示等待一分钟后重新检查或查询提交记录。计时使用 monotonic，线程锁保护滚动窗口，最多保留 4096 个活跃的队伍/渠道/操作窗口；空闲窗口会清除，容量已满时返回安全 503，绝不驱逐仍受限的队伍窗口。重启会重置内存配额；多实例部署须另设共享保护。精确成功请求重放在配额和远端核对前读取不可变回执，即使配额已满、渠道繁忙或预览过期也保持原结果。

公开主页入口为 [申请 Gitee 仓库 / 提交作品](https://8.141.114.201/gotc/platform.html#team)。队伍使用独立提交码登录后申请；仅已绑定队伍自动读取 SHA，未绑定的旧队伍保留手填流程。离线测试覆盖禁用配置、权限和队伍隔离、用户名/仓库名、同名冲突、丢失响应恢复、只授予 push、超时脱敏、写锁释放、SHA 变化、预览隔离/过期、覆盖攻击、提交窗口、原子回执/历史以及成功重放。真实组织授权、邀请接受、Gitee 企业兼容、实际禁止强推、代码归档和生产可用性均须另外验收。

官方调用依据（2026-11-04 读取）：[GitHub 创建组织仓库](https://docs.github.com/en/rest/repos/repos#create-an-organization-repository)、[GitHub 协作者与邀请](https://docs.github.com/en/rest/collaborators/collaborators#add-a-repository-collaborator)、[GitHub 分支保护](https://docs.github.com/en/rest/branches/branch-protection#update-branch-protection)、[Gitee API v5 文档](https://gitee.com/api/v5/swagger)、[Gitee 官方 OpenAPI schema](https://gitee.com/api/v5/doc_json)。

## 界面与逐版本核对

队伍预览带 `repositorySeal` 时，页面显示“版本已封存”和“确认提交的是当前预览版本”。确认固定使用本次完整 SHA；之后 push 的代码需要重新预览。未启用封存、GitHub 与旧手填提交保留原流程，不新增文件上传要求。开发分支的保护状态仍单独显示，封存引用保护不等于开发分支保护或独立字节备份。

新回执分别显示服务器 `receivedAt` 和实际 `savedAt`，封存引用与固定 commit 链接来自该回执自身。按请求编号查询或重放旧回执时，不能把当前最新正式版的封存或备份套到旧回执上。“最近正式材料”下另标“最新正式版 vN · 封存与备份”；历史详情只显示所选版。已有正式 v1 和旧不可变回执不回填；`repositorySeal` / `repositoryBackup` 为 null 的旧版明确显示“本版未归档”。

| 界面状态 | 含义 |
| --- | --- |
| 版本已封存 | 已核对 Gitee 保护引用及原 SHA；不是独立源码备份 |
| 独立源码备份排队中 / 处理中 | 任务尚未完成，不提供下载 |
| 独立源码备份已就绪 | 已保存并校验该 SHA 的 Git 字节；不证明可运行、依赖齐全或 PPT/视频已保存 |
| 独立源码备份不完整 | Git 字节已保存，但列出的 LFS / 子模块外部内容未抓取；可下载现有 ZIP / bundle |
| 独立源码备份失败 | 正式提交仍有效；组织方须核对错误类别和服务条件 |

“刷新本版备份状态”只更新该版本的备份小块，不重绘材料表单、原回执或组织方人工检查表。最新正式版通过现有 `GET /api/team` 查询；队伍历史用 `GET /api/submission/history/{version}`，管理员历史用 `GET /api/admin/submissions/{team_id}/history/{version}`。没有新增备份元数据端点。刷新捕获页面会话、浏览器登录标记、路由和组件版本；换号、切页、组件断开或返回其他版本的数据时丢弃结果，保留未保存材料与备注。

固定 commit 链接仅从校验过的 GitHub / Gitee / GitCode / GitLab 仓库根地址和 40/64 位完整哈希生成；Gitee 优先使用同仓库同 SHA 的 `repositorySeal.viewUrl`。其他主机仅显示哈希。ZIP / bundle 下载只接受当前部署前缀下、同源、精确匹配队伍与正式版本的 API 地址，不含 token。Gitee 原仓库为外部开源仓库；这里私有的是新增服务器备份文件。独立源码备份仅本队和组织方可下载，评委仍走既有固定 commit 链接。本次没有自动公开归档或作品集入口；将来公开须主办方逐队逐正式版本确认。

排队长期不动时先核对 worker 开关、主机 Git 和可用容量；queued 不代表失败，失败不代表正式提交未收到。超过三次的失败任务不会无限自动重试。修复环境后按现有恢复合同处理同一 SHA，不删除旧备份、不改历史，也不让队伍重复正式提交来绕过故障。不要把运营诊断路径、上游正文或凭据带到选手界面。

本轮 UI 验证仅用合成数据运行实际 JavaScript 的 Node/vm 测试与静态检查，未访问 Gitee、浏览器或服务器。操作者已在此前固定 Task2 源码上完成实际抓取、私有 ZIP/bundle 下载、匿名拒绝、独立 bundle 恢复、原回执重放及同源码第二版本复用；这是该固定源码的实测，不能替代后续改动的离线复核或生产验收。普通账号测试仍未执行；普通开发者受平台权限规则约束是用户明确接受的假设，管理员 contents API 可追加提交的例外仍存在，正式记录始终取原 SHA。
