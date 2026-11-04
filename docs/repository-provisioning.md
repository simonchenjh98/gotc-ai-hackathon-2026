# 组织仓库申请与固定 SHA 提交

本功能运行在现有 Python 单实例服务与原生 JavaScript 平台中。官方网站入口为 https://etownyouthai.com/ ，使用首页实际提供的作品提交入口进入平台；本文不假定官网已经部署同域平台。代码、数据库迁移与模拟远端测试可离线验证；真实 GitHub/Gitee 组织凭据、邀请权限、分支保护及生产部署仍需单独验收。未配置时渠道显示“尚未启用”，不会伪造仓库或 SHA。

## 服务端配置

仅在服务进程的私有环境配置中设置以下值，重启进程生效。不要放入前端、静态目录、Git 仓库、日志、工单或公开截图。

| 环境变量 | 默认值 / 含义 |
| --- | --- |
| `GOTC_REPO_GITHUB_ORG` | `GOTC-AI-HACKATHON`，唯一允许的 GitHub 组织 |
| `GOTC_REPO_GITHUB_TOKEN` | 空，空值禁用 GitHub 渠道 |
| `GOTC_REPO_GITHUB_BRANCH` | `main`，每队固定的正式分支 |
| `GOTC_REPO_GITEE_ORG` | `gotc-ai-hackathon`，唯一允许的 Gitee 组织 |
| `GOTC_REPO_GITEE_TOKEN` | 空，空值禁用 Gitee 渠道 |
| `GOTC_REPO_GITEE_BRANCH` | `main`，每队固定的正式分支 |
| `GOTC_REPO_GITEE_ENABLED` | 仅精确的 `1` 显式启用；同时需要令牌 |

组织和分支只允许 1–100 位 ASCII 字母、数字、下划线与连字符，首位须为字母或数字。修改配置不会改变已有申请/绑定的组织或分支；组织不匹配时停止远端访问，由管理员人工核对。分支创建以初始化 README 的默认分支为起点，不覆盖已有分支。仓库公开，初始化只含非敏感 README，不自动选择项目许可证。

调用主机固定为 `https://api.github.com`、`https://gitee.com/api/v5`；不支持自定义 API 主机、GitHub Enterprise Server 或自建 Gitee。禁止 HTTP 重定向，单次请求 8 秒超时、响应最多 2 MiB。上游错误只保留通用提示，丢弃响应正文和含认证信息的 URL。Gitee API v5 按官方文档通过 `access_token` 参数认证，运维代理不得记录其请求 URL。服务自身不记录这些请求/令牌。

GitHub 凭据须允许创建组织仓库、读元数据/分支、添加仓库协作者并写分支保护。优先用组织批准的 GitHub App 或最小范围的 fine-grained token：仓库 Administration write（建仓、邀请、保护）及 Contents read/write（读取提交、必要时创建正式分支），仅允许活动组织和所需仓库。具体组织策略、App 安装的仓库覆盖和套餐必须实测；经典 PAT 公开仓库至少涉及 `public_repo`，权限更宽，勿授予不必要的组织管理权限。开发者通过仓库协作者接口仅获得 `permission: push`，不获得组织/企业管理员权限。

Gitee 使用普通组织 API v5 的建仓与协作者接口，协作者必须传 `permission: push`（官方值为 pull / push / admin）。Gitee 企业 API、企业席位和普通组织 API 的兼容性尚未验收。企业免费额度与人数限制由组织管理员另行核对，不能仅凭配置成功就宣称可用。未实现企业 API 自动切换。Gitee 分支保护保留“尚未验证”状态，须管理员人工设置并验收。

Gitee 账户级令牌无法把“创建未来仓库”的能力限制到单一活动组织，须由专用主办方账户持有，避免其同时管理无关项目。令牌表单的必选 `user_info` 与项目范围 `projects` 是待验收的最小候选；当前官方 OpenAPI schema 没有逐端点 scope 映射，因此不能声称该组合已验证。此实现不增加组织/企业成员，不主动要求 `groups` 或 `enterprises`；先以最小候选在活动组织验收具体端点，权限不足时由管理员核对，不能自动扩大令牌权限。

## 队伍与管理员流程

队伍用原有队伍登录码进入“本队代码仓库”，选择已启用渠道并填写 1–5 个开发者的平台用户名（不是邮箱、主页网址或显示名），点击“申请本队仓库”。本队只能申请一次，不能传入组织、仓库名称、任意仓库 URL 或别队编号。仓库名由服务器按队号和内部 ID 哈希生成，避免参赛者控制名称或跨队绑定。

管理员在“组织仓库申请与恢复”中逐一在平台外核对用户名归属，勾选“我已逐一核对以上用户名归属本队开发者”，点击“核对并批准建仓”。服务器创建独立仓库、初始化正式分支、尝试禁止强推与删除，并邀请开发者写入。GitHub 返回邀请时显示“邀请待接受”；开发者到 GitHub 接受邀请后，管理员用“核对恢复 / 刷新邀请”确认状态。该按钮对已知身份的仓库重新核对并执行幂等协作者授权，不创建第二个仓库。所有开发者都已加入后显示“仓库已绑定”。

队伍点击“刷新仓库状态”后可查看仓库与固定分支。已绑定队伍的手填代码 URL / commit 字段隐藏且没有 required 校验错误；其他材料照常填写。点击“检查并正式提交 ↗”时服务器读取完整 SHA，生成有效期五分钟、仅属于本队和当前登录会话、绑定仓库修订号的预览。预览展示完整 SHA；“确认正式提交”带预览编号发送材料。服务器再次检查远端仓库 ID、分支 head、绑定修订、有效期和提交窗口，拒绝 SHA 已变化、跨队/跨登录预览或手动覆盖代码字段。此检查与写回之间仍可能有新的 push；平台保存已核对的确切 SHA，不会默默替换成更晚提交。

正式材料、精确请求回执和不可变历史使用原有原子事务。相同请求编号和相同内容重放时优先返回原回执，即使预览过期、提交截止、远端不可用或之后已更新也不改变先前成功结果。新内容须新请求编号。草稿保留旧字段兼容；没有绑定仓库的旧队伍继续原来的手填流程。

## 状态与故障恢复

申请/绑定持久化在 `repository_bindings`；状态为 requested → provisioning → invited / ready。网络错误、创建冲突、身份不匹配变为 reconcile，不声称失败意味着远端没有发生操作。数据库审计保留申请、处理和恢复事件。远端请求全部在 SQLite 写事务外，处理中的修订号防止并发覆盖。进程中断留下 provisioning 时，至少两分钟后才允许管理员恢复，覆盖全部有界调用的正常处理时间。

创建前发现同名仓库时，绝不自动认领。每次创建使用持久化的随机操作标记作为仓库说明；如果创建响应丢失，恢复必须匹配组织、生成的名称和该操作标记，再持久化远端数字 ID。已知远端 ID 后，每次核对与 SHA 读取都核对同一个 ID；删除重建的同名仓库不能替代它。身份保存早于邀请，因此邀请超时不会丢失仓库身份。

管理员先查看状态和审计、再核对组织侧仓库/邀请。使用列表当前修订号执行恢复，不能重复“批准建仓”。如果不确定的创建最终查不到仓库，系统不会盲目重建；由管理员在维护窗口核对组织审计和远端不存在后，备份数据库，人工处理原申请。当前没有忽略身份校验、任意绑定、自动重置申请或远端删除入口。用户名错误也须在授权前人工核对处理；当前没有队伍自助替换已申请开发者名单的接口。

## 强推与归档规则

禁止 force push、删除正式分支、删除/改写正式提交；违规造成 SHA 不可访问的后果由队伍承担。GitHub 分支保护成功时记录“已配置”；套餐、权限或网络阻止保护时记录“尚未验证”，不能声称强推已被技术禁止。管理员须在发放仓库后核对策略，Gitee 须人工设置。

**记录 SHA 不等于代码已归档。** 此功能没有源码归档器。界面始终说明“代码尚未归档”，主办方仍须按完整 SHA 独立保存代码及依赖，单独处理子模块/LFS；下载一个 ZIP 不足以证明完整存证。远端权限、代码真实性、源码可运行性和外链材料可访问性不由 SHA 格式校验代替。

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

预览记录绑定队伍、会话、SHA、仓库修订和过期时间。它不是公开查询凭据，不进入代码仓库或材料字段。离线测试覆盖禁用配置、权限和队伍隔离、用户名/仓库名、同名冲突、丢失响应恢复、只授予 push、超时脱敏、写锁释放、SHA 变化、预览隔离/过期、覆盖攻击、提交窗口、原子回执/历史以及成功重放。真实组织授权、邀请接受、Gitee 企业兼容、实际禁止强推、代码归档和生产可用性均须另外验收。

官方调用依据（2026-11-04 读取）：[GitHub 创建组织仓库](https://docs.github.com/en/rest/repos/repos#create-an-organization-repository)、[GitHub 协作者与邀请](https://docs.github.com/en/rest/collaborators/collaborators#add-a-repository-collaborator)、[GitHub 分支保护](https://docs.github.com/en/rest/branches/branch-protection#update-branch-protection)、[Gitee API v5 文档](https://gitee.com/api/v5/swagger)、[Gitee 官方 OpenAPI schema](https://gitee.com/api/v5/doc_json)。
