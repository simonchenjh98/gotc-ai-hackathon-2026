# 本队资料与公开组队名单

已有参赛码（原提交码）的队伍在 `platform.html#team` 登录，点击“本队资料与成员”进入 `#team-profile`。本队资料、作品提交和本队查分共用同一个原会话，不再需要独立的队伍管理码；已有参赛码、账号、队伍 ID 和会话均不因本次更新重建或轮换。主办方后台独立，代管会话不能使用新增的参赛者资料接口。

尚无参赛码的队伍继续在 `platform.html#claim` 填写含队长共 3–5 人的姓名与学校、保存本标签页领取凭据并等待审核；待审核申请仍可在原申请页修改。历史预登记仍保留，组织方可按原审核规则主动选择已有队伍，不按队名自动绑定。详见 [队伍申请审核](team-claims.md)。

## 本队资料与成员边界

`GET /api/team/profile` 按当前真实 team session 确定唯一队伍，必须携带匹配 cookie 的 `X-GOTC-Login`。返回本队 metadata、`profile`、注册 `revision`、`teamRevision`、本队实际成员及学校、`membersEditable`、`projectEditable`、`incomplete` 和只读 `listedPublicly`。不接受目标 ID、账号 ID、管理码或 query，不返回原始导入、来源编号、备注、hash 或凭据。

`POST /api/team/profile` 仅接收 `{team,expectedRevision,expectedTeamRevision}`；`team` 为原资料八个字段：`name`、`captain`、`school`、`phone`、`targetSize`、`members`、`project`、`summary`。两个版本在同一写事务中核对。未核验资料可编辑；已审核的队长、学校、完整成员及人数保持锁定，成员变化请联系组织方。同名不同校的已审核成员完整保留，不按姓名合并。缺可靠队长资料时只读提示核对，不猜身份或学校。已正式提交的项目名称必须从作品流程修改；截止、版本、成绩发布和冻结材料规则不变。

缺少登记行时，GET 不写数据库；第一次合法保存只给原队伍增加资料行，不新建队伍或账号。新资料使用不可用的唯一 `secret_hash` 占位，不显示或发放管理码。后续正常保存记录真实 team actor 的审计。

## 公开范围与旧入口

首页 `#teams` 与 `teams.html` 现在是只读目录，保留搜索与组队状态，链接到本队工作台及申请入口。既存公开登记的展示范围不变，电话不公开；新私有资料在 `team_profile_visibility` 记为 `directory_visible=0`，不会因保存、审核同步或恢复自动加入公开名单。客户端没有改公开范围的开关，也不按 secret 值判断可见性。

旧 `POST /api/registration/create|open|save` 一律返回 410；正确的历史管理码也不能再访问，不做 secret 查询或数据写入。页面不再生成、输入、复制、保存或自动恢复管理码，仅清理该页面精确的旧存储 key。`GET /api/registration` 仍提供原公开名单，对原受信站点只读跨域开放，不携带 Cookie；新资料写入只接受同源。

`GET /api/admin/registration` 和 `POST /api/admin/registration/import` 保留独立管理员及页面登录标记检查。导入每项为 `{sourceKey,team,raw}`，只新增且同批失败整笔回滚，原始字段保留；响应只含新增资料标识，不含管理码。后台保留联系方式和待核对信息，取消发码列及重发按钮；旧 `/api/admin/registration/rotate` 在管理员鉴权后返回 410。主办方管理码和参赛码的权限彼此独立。

## 升级与兼容回滚

初始化只增加幂等的 `team_profile_visibility` 元数据表；历史登记不回填身份或更改原公开范围，旧 hash 和原始记录保留。更新前正常备份，不重置数据库、队伍账号、参赛码、会话或 API。

兼容回滚必须保留管理码退役 guards 和私有目录过滤。旧二进制会重新接受保留的历史管理码，也可能把新私有资料作为旧登记公开，不能仅因没有破坏性迁移就无条件回滚。优先向前修复或使用经过审核的兼容回滚；不得用旧整库备份覆盖后续活动。
