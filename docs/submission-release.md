# 作品提交：最终合同、上线检查与回退

本页说明 PR1 的作品提交功能及其数据库升级。演练基线固定为父版本 `e456bd3f463f994450065d03cd3337798d1b6bd4` 的真实服务端源码；来源和文件摘要见 `server/test_fixtures/`。演练使用合成数据，不代表已在生产执行部署或回滚。

## 保存与冲突

队伍通过专属提交码绑定唯一队伍。`POST /api/submission` 只接受 `{expectedVersion,status,materials}`，不接受客户端指定另一队的 `teamId`。字段、长度和必填要求见 [服务端 API](../server/README.md#队伍提交-api)。

| 操作 | 保存结果 |
|---|---|
| 尚无记录 | `version=0`，草稿、正式材料及提交时间均为空 |
| 保存草稿 | 版本加一，只更新草稿；不改变最近正式材料及公开作品名称 |
| 首次正式提交 | 完整校验后版本加一，同时保存草稿与正式材料；设置首次及最近正式提交时间，并更新公开作品名称 |
| 再次正式提交 | 版本加一，替换最近正式材料并更新最近提交时间；首次提交时间保留 |

服务器取得写锁后检查身份、提交窗口和版本。有效提交须满足开放，且实际保存校验时的服务端时间严格早于截止；在截止前点击、但排队到截止后才校验的请求会被拒绝。客户端倒计时或点击时间不是成功依据。

同一版本的并发保存最多一项成功，其余返回 409，不做部分更新。成功后响应若丢失，原版本重发也会返回 409，不能当作幂等成功，更不能自动加一后覆盖。

409 的前端处理是：

1. 保留当前输入为可复制的只读内容，不自动重试、合并或改写版本号。
2. 用户点击“读取最新保存版本”，对照服务器草稿和当前窗口；先复制需要保留的内容。
3. 用户明确确认后，才放弃旧编辑区并用最新草稿重新编辑，再自行合并和提交。

若原因是窗口关闭或截止，须先由组织方重新开放；重新开放不会自动解除当前页面的冲突状态。若只是上次保存响应丢失，应先确认服务器是否已保存相同内容，避免无意义地再次提交。提交码停用或轮换会废除旧会话，之后须用有效身份重新登录；服务器不会因客户端仍保留表单而接受旧会话。

## “正式快照”的含义

PR1 只保存一份最新草稿和一份最近正式提交的**字段及 URL 字符串**，不提供所有正式版本的归档浏览。版本号用于并发检查，不等于保存了每个版本的外部文件。

平台不下载、抓取、校验或归档外链内容。URL 指向的分支、共享文件、访问权限或服务可用性都可能在提交后改变；关闭提交窗口只能锁住平台里的保存操作，不能冻结外部仓库或文件。新正式提交须填写完整 commit SHA 或可解析的同仓库 commit 链接，正式保存完整 SHA 供人工核对；旧 tag/短号只读保留。平台不验证其真实性或永久可用性。

评委从原专业评分接口读取最近一次正式材料，原专业评分不绑定到某次材料版本。需要评审期稳定材料时，组织方应先关闭提交，并另行确定外部文件留存办法。后续 PR 的独立评审轮次快照不是 PR1 自身能力，不能据此宣称 PR1 已做不可变内容存证。

草稿只供该队和管理员读取；评委只读正式材料，公开看板与观众接口不返回材料。撤销平台身份不撤回已经复制的 URL，也不改变外站权限。

## 升级前的准备

在获批准的维护窗口中完成以下准备。本仓库没有自动把全部入口变成只读的维护模式；只关闭评分或作品提交窗口，也不能阻止登录、审计及管理员写入。

1. 在隔离副本运行全部测试及下方演练。记录计划发布的源码版本、打包摘要、当前 `current` 指向的旧 release 和旧 unit；保留可重新运行的旧 release，不只记一个 Git SHA。
2. 由运维冻结外部写入并停止本服务，确认没有其他进程使用同一数据库。不要停止或改动无关活动服务。
3. 用 SQLite backup API 创建完整副本，检查完整性和外键。不要只复制活跃数据库的主文件而忽略 WAL。
4. 将 `admin-access.txt`、可选的 `shared-login-codes.json` 及必要的 unit/部署元数据一起保存到私有备份目录，保留权限。凭据文件必须与数据库时期相匹配；不要打印、放入 Git、Pages 或公开报告。
5. 在副本上启动候选，检查旧账号、分数、投票、专业看板和 CSV，以及新队伍提交、正式/草稿隔离、冲突和截止。生产只读健康检查不能代替这些隔离业务测试。

以下命令仅复制 SQLite 到**不存在的新文件**，可用于备份，或把已选备份恢复到新目标；它不会停止服务、切换发布或自动覆盖当前数据库。先创建一个仅授权运维/服务账号可访问的目标父目录，再替换两个参数：

```sh
python3 - /private/source/event.sqlite3 /private/new-target/event.sqlite3 <<'PY'
from contextlib import closing
from pathlib import Path
import os
import sqlite3
import sys

source = Path(sys.argv[1]).resolve(strict=True)
target = Path(sys.argv[2]).absolute()
if not source.is_file() or not target.parent.is_dir():
    raise SystemExit('Source file or target directory is missing')
if target.exists() or target.is_symlink():
    raise SystemExit('Refusing to replace an existing target')
fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
os.close(fd)
with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as reader:
    with closing(sqlite3.connect(target)) as writer:
        reader.backup(writer)
        if writer.execute('PRAGMA integrity_check').fetchone() != ('ok',):
            raise SystemExit('Restored database failed integrity check')
        if writer.execute('PRAGMA foreign_key_check').fetchall():
            raise SystemExit('Restored database failed foreign-key check')
print('New private database verified; no service or release was changed')
PY
```

校验失败时保留该失败目标供私下检查，不要启动它，也不要直接反复覆盖。这个示例没有借用后续 PR 的备份工具。

## 三种不同的回退

### 迁移过程失败

PR1 在私有数据目录生成 `before-team-*.sqlite3`，然后事务重建账号角色约束。账号 ID、哈希和已有引用保持；外键检查失败或重建中途异常会回滚，停止启动，不留下半张新账号表。备份连接在继续启动前显式关闭。

此处只保证迁移事务本身；后续启动或部署步骤失败是下一种情况。自动备份是一致性快照，不代表从生成快照起就没有其他写入。

### 代码发布失败后退回旧应用

现有安装器尝试恢复本服务原来的 release 链接、unit 和本次 Nginx 修改；**不会自动用旧快照覆盖已升级数据库**。如果 Nginx 已被其他操作同时修改，它不会擅自覆盖，会要求人工核对。

演练使用真实 `e456bd3` 服务端读取升级后的同一数据库，验证原登录、专业评分、投票、看板和 CSV 仍可使用，新增队伍身份和提交材料仍保留。旧应用没有作品提交接口，退回旧应用不等于该功能仍可用；此兼容结论也不能推广到任意更旧版本。

人工切回时，先冻结写入并停止本服务，再把 `current` 切到已核对的旧 release，恢复匹配 unit/私有配置并重新加载服务配置。核对实际 `current`、运行版本与健康结果，不只看上次部署清单。若仍使用升级数据库，不删除新增表或队伍资料；如旧应用无法运行，保持停写并采用明确批准的数据恢复方案。

### 明确恢复升级前的数据

这属于单独的数据回退：冻结写入并停止本服务，先保留一份当前新数据库及其私有配置，再将选定旧快照复制到新的私有目标，校验后用匹配的旧应用和私有配置访问。

**旧快照不包含备份之后的新增或修改。** 恢复它会舍弃之后的队伍材料、账号、分数、投票、会话和审计；不会自动合并。只有组织方明确接受这个边界后才能切换，原新数据库应保留以便人工处理。

若使用不同的数据目录，须一起调整并核查 `GOTC_DATA_DIR`、服务账号所有权、0700/0600 权限及 systemd 的 `ReadWritePaths`，不能只改一个环境变量。本服务的默认数据目录在 `/opt/gotc-hackathon/data`；不要把活动数据放入会随 release 切换的源码目录，也不要让旧 WAL 文件与恢复文件混用。

## 已接入的部署检查与边界

| 路径 | 当前约束 |
|---|---|
| `deploy/deploy.py` | 默认仅本地打包；只有明确传入 `--execute` 才连接既有 SSH 身份。使用严格 known-host 校验；不把私钥或管理码写入参数、日志或发布包 |
| 发布白名单 | 仅指定网页、公开素材/下载及直接服务端 Python 源码；数据、凭据、测试和历史测试夹具不进云包，Pages 只上传公开官网文件 |
| 本地私有产物 | `.runtime` 拒绝符号链接/普通文件，并将已存在目录收紧为 0700；取回管理码的临时文件创建时即 0600，完成前不替换旧管理码 |
| `deploy/install.py` | 私有 SQLite 备份、独立 release、应用内存编译、loopback 健康检查、Nginx 校验和本服务失败回退；不把数据库回退伪装成普通代码回退 |
| systemd / Nginx 模板 | 独立服务用户、loopback 5274、数据目录持久化、`ProtectSystem`/`ReadWritePaths`/0077；保留 `/gotc` 路径并覆盖可信代理 IP 头；当前 PR1 提交 32 KiB 与模板 32k 一致 |
| GitHub Actions | PR、main push 和手动触发均先运行只读验证；完整 Node、服务端及部署测试和站点检查纳入流程。仅 main 的 push/手动触发可在验证后运行 Pages 发布 job |

CI 的 Pages 写权限、OIDC 和 `github-pages` 环境只属于部署 job，PR 使用 `pull_request` 而不是 `pull_request_target`。[权限与依赖依据](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages)。按 PR/ref 分组校验，避免不同 PR 共用发布队列；main 发布不会被新的 PR 校验取消。[并发配置依据](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency)。

fork PR 可能需要维护者批准，冲突或平台设置也可能阻止云端检查。配置验证和本地测试不能写成“GitHub CI 已通过”；需读取实际运行结果。Pages 发布只是静态官网，不会自动部署 Python API 或生产数据库。

`/api/board` 健康检查只证明基本响应，不证明身份权限、提交、冲突、截止等业务均已在生产验证。执行过 `--execute` 后即使本地报错，也不能断言服务器没变化；私有码取回失败时应私下核对实际 release 与结果，不盲目再次发布或公开临时文件。

本轮只在本地检查拥有的脚本、模板、白名单、权限和隔离演练。没有访问生产数据库、读取真实 SSH 私钥、执行 SSH/systemd/Nginx/Pages 发布，或修改真实活动状态。

## 重跑本地演练

```sh
python3 -B -m unittest discover -s server -p 'test_submission_release.py' -v
python3 -B -m unittest discover -s deploy -p 'test_*.py' -v
node --test tests/*.test.mjs
python3 -B scripts/check_site.py
```

测试使用带来源摘要的真实父版本夹具和独立临时数据库，验证迁移成功保全、故障回滚、旧应用读取升级库及新目标恢复。部署测试使用假的 SSH/SFTP/系统命令边界；不会调用生产服务。完整回归和云端状态以本次 PR 的实际验证记录为准。

## 提交页核对（后续完善）

提交页在最新正式材料之外单独显示展示/路演本轮实际材料与冻结时间，不将重新提交描述为已替换冻结版。原专业评分仍读最新正式版，未改变评分和阶段冻结规则。集中检查后预览、确认正式提交；回执展示服务端正式版号、时间及完整commit，可复制记录。断网/超时的结果待确认，读取服务器最新记录仅供核对，不能凭内容相同认定本次成功。支持的仓库和commit链接格式见服务端API文档。

## 请求账本与历史升级

新增提交记录模块在既有初始化完成后独立原子迁移，先创建私有 `before-submission-records-*.sqlite3` 备份。成功结果、当前材料、正式历史与审计同事务；可预期拒绝通过保存点回滚业务修改并提交拒绝回执后才返回400/409。未知异常全部回滚。编号查询仅当前绑定队伍，not_found不是失败证明。

正式历史只追加，新版本人工访问记录重新开始；仅迁移当前真实旧正式记录，不从版号或阶段快照补造早期历史。回退旧代码产生的未跟踪提交，再升级只能保留当时当前一份为legacy_current。历史和回执不归档外链内容，人工检查不代验作品。数据库恢复仍需原有停服、独立目标及完整性/外键校验；本变更不自动替换任何生产数据库。
