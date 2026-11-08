# 本队资料与公开组队名单

已有参赛码（原提交码）的队伍在 `platform.html#team` 登录，点击“本队资料与成员”进入 `#team-profile`。本队资料、作品提交和本队查分共用同一个原会话，无需另输队伍管理码；已有参赛码、账号、队伍 ID 和会话均不因本次更新重建或轮换。主办方后台独立，代管会话不能使用新增的参赛者资料接口。

尚无参赛码的队伍继续在 `platform.html#claim` 填写含队长共 3–5 人的姓名与学校。完整且没有队名或成员占用冲突的申请会立即自动建队并开放领码；冲突在提交时返回，不进入人工审核队列。历史预登记仍保留；当队名、队长学校和完整成员名单精确一致时可自动绑定原队伍。详见 [队伍申请与领码](team-claims.md)。

## 本队资料与成员边界

`GET /api/team/profile` 按当前真实 team session 确定唯一队伍，必须携带匹配 cookie 的 `X-GOTC-Login`。返回本队 metadata、`profile`、注册 `revision`、`teamRevision`、本队实际成员及学校、`membersEditable`、`projectEditable`、`incomplete` 和只读 `listedPublicly`。不接受目标 ID、账号 ID、管理码或 query，不返回原始导入、来源编号、备注、hash 或凭据。

`POST /api/team/profile` 仅接收 `{team,expectedRevision,expectedTeamRevision}`；`team` 为原资料八个字段：`name`、`captain`、`school`、`phone`、`targetSize`、`members`、`project`、`summary`。两个版本在同一写事务中核对。未核验资料可编辑；已审核的队长、学校、完整成员及人数保持锁定，成员变化请联系组织方。同名不同校的已审核成员完整保留，不按姓名合并。缺可靠队长资料时只读提示核对，不猜身份或学校。已正式提交的项目名称必须从作品流程修改；截止、版本、成绩发布和冻结材料规则不变。

缺少登记行时，GET 不写数据库；第一次合法保存只给原队伍增加资料行，不新建队伍或账号。新资料使用不可用的唯一 `secret_hash` 占位，不显示或发放管理码。后续正常保存记录真实 team actor 的审计。

## 公开范围与已有管理码兼容

首页 `#teams` 与 `teams.html` 保留只读目录、搜索与组队状态，主入口链接到参赛码工作台及申请入口；另提供已有管理码的资料读取/修改入口。既存公开登记的展示范围不变，电话不公开；新私有资料在 `team_profile_visibility` 记为 `directory_visible=0`，不会因保存、审核同步或恢复自动加入公开名单。客户端没有改公开范围的开关，也不按 secret 值判断可见性。

已有有效的 43 字符队伍管理码继续支持 `POST /api/registration/open`（精确 `{code}`）及 `/save`（精确 `{code,expectedRevision,team}`），仅凭原 hash 授权到其既有登记行。它只保留原资料修改权限，不创建账号/会话，也不授予作品提交、查分、AI 或主办方权限；回包保持旧登记投影，不包含新 session profile 的精确人员 ID、队员个体学校或 loginId。队伍须有效且未回收；已换掉的 hash、退出/取消资格/回收队伍和新资料的 disabled 占位不能凭旧码访问。单独停用参赛码不改变原管理码的有限资料权限；回收会作废原管理码，恢复队伍不会复活它。

旧页面保存的精确 sessionStorage key 继续保留并恢复，不清除、不生成新码，也不把管理码放进 URL。两种资料入口共用注册修订；冲突返回 409 并保留输入，需显式重新读取。旧码 open/save 与目录 GET 沿用原官方来源 CORS、`credentials: omit` 和每 IP 原资料限速；新 session profile 仍仅接受同源并验证登录标记。`POST /api/registration/create` 保持 410，新队伍走 `#claim`，不从数据伪造或发行管理码。

`GET /api/admin/registration` 和 `POST /api/admin/registration/import` 保留独立管理员及页面登录标记检查。导入每项为 `{sourceKey,team,raw}`，只新增且同批失败整笔回滚，原始字段保留；响应只含新增资料标识，不含管理码。后台保留联系方式和待核对信息，取消发码列及重发按钮；旧 `/api/admin/registration/rotate` 在管理员鉴权后仍返回 410，本轮不恢复发码/重发功能。主办方管理码和参赛码的权限彼此独立。

## 升级与兼容回滚

初始化只增加幂等的 `team_profile_visibility` 元数据表；历史登记不回填身份或更改原公开范围，旧 hash 和原始记录保留。更新前正常备份，不重置数据库、队伍账号、参赛码、会话或 API。

兼容回滚必须保留私有目录过滤、原成员/项目保护及两入口的权限边界。已有管理码按上述有限能力兼容，不代表可以回滚旧二进制；旧二进制可能把新私有资料作为旧登记公开，不能仅因没有破坏性迁移就无条件回滚。优先向前修复或使用经过审核的兼容回滚；不得用旧整库备份覆盖后续活动。
