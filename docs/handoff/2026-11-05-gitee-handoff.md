# Gitee 提交功能交接 · 2026-11-05

**状态：按发起人要求暂停上线，先同步远端同事；本 PR 为草稿，不能据此视为已部署或完整验收。**

代码基于主仓 `0282853`，保留了最新 GitCode 链接支持。最后通过完整离线检查的功能提交为 `32e9a8b`。现有队伍、专业评分、API 网关和旧版手填提交流程保持兼容；GitHub 组织接入暂未启用。

## 直接从这些文件接手

- 功能、配置与恢复合同：[组织仓库说明](../repository-provisioning.md)。
- 已完成的功能设计与执行记录：[设计](../superpowers/specs/2026-11-04-repository-provisioning-design.md)、[实现计划](../superpowers/plans/2026-11-04-repository-provisioning.md)、[公开使用保护与入口计划](../superpowers/plans/2026-11-05-gitee-launch.md)。
- 离线验证：[测试报告](2026-11-05-test-report.md)，最后完整结果为服务端 208 项、Node 203 项通过；不是生产验收。报告中的“实现完成”只指已提交的功能实现，后续独立审查仍有两项待修。
- 待修问题：[独立审查](2026-11-05-review.md)。
- 主办方操作与归档：[赛前检查单](2026-11-05-organizer-checklist.md)。
- 当前选手教学稿：[Markdown](../gitee-participant-guide.md)、[PDF](../../downloads/gotc-gitee-submission-guide.pdf)。2026-11-05 已更新为 Gitee 待上线验收版，补充申请状态、5 分钟 SHA 预览、回执和异常处理，已逐页检查。真实上线后仍需复核状态和最终界面，不能当作已上线公告。
- 可复制起步文件：[README 模板](../templates/gitee-starter/README.md)、[模板许可](../templates/gitee-starter/TEMPLATE-LICENSE.txt)。

## 当前真实验证边界

官方网站为 https://etownyouthai.com/ ，由主仓 GitHub Pages 工作流发布。它的同域 `/platform.html` 返回 404；现有实际平台地址是 https://8.141.114.201/gotc/platform.html#team 。本分支已修改首页按钮为“申请 Gitee 仓库 / 提交作品”，没有把它指向不存在的同域平台页。**分支入口文案尚未发布到正式首页。**

Gitee 空间实际属于 enterprise。使用用户批准的 `projects` 加平台必选 `user_info` 令牌，已实际完成：

1. 当前账号鉴权、活动起步仓库读取、分支完整 SHA 读取均返回 200。
2. `POST /enterprises/gotc-ai-hackathon/repos` 返回 201，确认企业外部开源参数应为整数 `private=1`。
3. 建立了明确标记为测试用途的 [验收仓库](https://gitee.com/gotc-ai-hackathon/gotc-acceptance-20261105-896659)，仓库 ID 为 `51277411`，默认分支为 `master`。它不是参赛作品，也没有录入生产参赛名单。

[组织起步模板](https://gitee.com/gotc-ai-hackathon/gotc-2026-starter) 的 README 已在线。网页批量上传教程失败，原因是 Network Error；本 PR 中的 MD/PDF 和模板文件是完整本地交付，不声称已同步到 Gitee 仓库。

**尚未完成：**真实普通选手写权限确认、申请到正式回执的完整真实链路、服务器部署、公开浏览器验收、分支保护验收和独立源码归档。当前管理员账号不能被当作“普通选手只有 push 权限”的证明。Gitee 免费企业界面显示成员额度 2/5，实际参赛容量还须确认。

## 恢复工作时先处理的两项问题

独立审查定位和理由见上面的审查文档。先修复，再进行生产发布：

1. `installRepositoryAdmin` 应捕获管理员会话对象，并在渲染和操作前核对 `session === owner && ownsBrowserLogin(owner)`，避免另一标签页换号后展示旧管理员响应中的队伍与账号映射。还需要补延迟响应的跨标签登录回归。
2. 管理员收到明确的 `repository_busy` 时，不应追加“后台可能仍在处理”的不确定结果提示；该响应保证未开始远端操作。网络错误和其他不确定 5xx 则继续要求刷新并核对。

暂停发生在复现测试编写阶段，产品代码尚未进行这两项修复。已把当时未提交的测试差异完整保存为 [压缩补丁](2026-11-05-pending-admin-regressions.patch.gz)，不把尚未通过的复现测试直接混入最后通过检查的版本。应用方式：

```sh
gzip -dc docs/handoff/2026-11-05-pending-admin-regressions.patch.gz | git apply
node --test tests/repositories.test.mjs
```

该补丁包含繁忙提示的复现及不确定错误对照；身份切换回归仍需编写。修复之后运行相关测试并安排一次独立复审。

## 服务器连接与凭据

用户已授权配置活动服务器并上线，但这台电脑没有 `deploy/deploy.py` 指定的 `~/.ssh/realtalk_ecs`，SSH 代理没有身份，目标主机也不在当前已知主机记录中。没有绕过主机验证，没有连接或修改生产服务器。等待提供已授权的 SSH 密钥本地路径及可信主机信息，或云控制台连接方式，再按 [部署说明](../../deploy/README.md) 备份、配置和发布。

Gitee 凭据仅保存在操作者本机忽略的私有配置中，**本分支不包含令牌、密码、数据库、登录码、私钥或私有运行目录**。远端同事如需执行真实测试，应通过受认可的私密渠道配置服务器环境；不要把凭据填入 PR、Issue、仓库或截图。用户设置的是短期令牌，失效后服务端自动建仓和 SHA 读取会停止，需要届时更新配置。

建议下一位执行者使用 `subagent-driven-development` / `requesting-code-review` 完成修复和复审；恢复部署后用 PDF 技能从 Markdown 更新、渲染并逐页检查手册。保留这里的已验证与未验证边界，不以离线模拟替代真实验收。
