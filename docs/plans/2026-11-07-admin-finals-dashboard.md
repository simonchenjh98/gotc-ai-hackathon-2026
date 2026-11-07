# 组织方首页决赛看板

用户要求在组织方后台顶部查看决赛总分排名及六项专项奖提名，实时同步并注明现有规则。

## 界面与数据

- 放在活动管理标题下面、原有管理工具上面。沿用蓝白主视觉，左侧为可纵向滚动的入围队伍排名（本次计划12队，以实际配置为准），右侧为六列专项奖提名。小屏横向查看奖项。
- 只读取现有管理员接口 `GET /api/admin/finals`；使用服务端精确排名，不对已四舍五入的分数再次排序。未收齐显示暂定，未评分与真实0分区分。
- 提名票与100分均分独立展示；所有正票提名均可查看，另外标明系统顺延建议、已确认或已发布状态，避免将提名误当获奖。
- 每2秒同步；隐藏页面暂停，单请求串行，失败标明旧数据，身份变化立即清除私有结果。只更新本面板，不覆盖其他表单及滚动位置。

## 规则

六维20/15/20/20/15/10，取有效正式评分的平均；草稿、回避、未交不计0分。每队须收齐8位评委评分或回避且至少1份评分。每位评委每奖最多1票，可弃权；草稿不计票。六奖按固定顺序分配，排除已获前序奖的队伍后最高正票获奖，同票共同获奖，每队最多1奖，无正票则空缺。须关闭评分、收齐记录、确认奖项并发布后才是最终结果。本次不改变任何计分或发布规则。

## 验证与发布

使用合成12队数据验证排名、票数、空值、并列、状态和转义；回归轮询、登录身份隔离、失败重试、草稿与滚动保留。检查桌面和窄屏布局。发布仅叠加本次前端模块和入口钩子，保护同时开发的提交材料、队伍维护和路演评分更新。

## Verification and publication

- Added 21 focused dashboard regressions, including exact backend ranks, nomination succession, unchanged forms, browser-cache races and explicit cross-tab logout markers. All 545 frontend tests pass.
- Static resource and anonymous API access smoke tests pass; all 12 deployment packaging/recovery tests pass under local Ubuntu. Windows cannot reproduce the POSIX permissions/symlink cases, so those checks run in Linux.
- Browser preview with 12 synthetic teams verifies six desktop columns, ranking scroll, automatic score updates, preserved draft input and 390 px layout without page-wide overflow.
- The repository serves the new standalone module and stylesheet through explicit static/deployment allowlists. For the active event, a scoped static update appends the same JS/CSS to already-served assets and adds only the three bootstrap hooks to the latest live script. This avoids a backend restart and preserves concurrent scoring/materials work. The update checks release/process identity and prior file hashes under the existing deployment lock, takes backups, atomically replaces three assets and verifies served hashes.
