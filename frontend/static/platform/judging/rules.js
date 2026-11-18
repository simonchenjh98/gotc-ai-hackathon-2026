'use strict';

function rulePlaceholders(stage){
  if(stage.id==='roadshow')return '<section class="surface"><h2>路演六维100分评分</h2><p>问题价值与理解20 · 方案与 AI 使用20 · 原型完成度20 · 测试与改进15 · 社会关怀与责任10 · 团队协作与表达15。每队至少3位不同评委正式评分后成绩有效；超过3位时，全部有效正式评分都纳入平均。草稿和回避不计入3人门槛，不设额外进展加权。</p><p>路演进行期间，选手和评委均看不到汇总分数或排名；组织方后台每2秒同步暂定成绩。组织方确认发布后，选手只能查看自己队伍的成绩。</p><a class="btn secondary small" href="#admin-roadshow">查看路演名单与实时成绩 →</a></section>';
  const dimensions=stage.dimensions || [];
  return `<section class="surface rule-placeholders"><div class="section-caption"><h2>计分规则</h2><span class="status-chip closed">待配置 · 尚未实现</span></div><p class="small-note">本阶段可保存、正式提交评审记录；本阶段的数值评分、汇总、排名、晋级与成绩发布尚未实现。专业评分及成绩看板可从平台导航进入。</p><div class="rule-grid">${dimensions.map(d=>`<div><span>${esc(d.name)}</span><strong>待配置</strong></div>`).join('')}<div><span>有效评委数门槛</span><strong>待配置</strong></div><div><span>汇总与晋级规则</span><strong>待配置</strong></div></div></section>`;
}

function reviewState(record){return record?.invalidated?'已作废':record?.recused?'已回避':record?.submitted?(record.hasUnsubmittedChanges?'已正式提交 · 有草稿修改':'已正式提交（未计分）'):record?.draft?'草稿已保存':'待评审';}
function reviewMaterialMarkup(snapshot,stage=null){return `<section class="surface submitted-materials"><h2>本轮正式材料快照</h2>${stage?`<p class="small-note">轮次 ${esc(stage.runId)} · 冻结时间 ${esc(beijingTime(stage.frozenAt))}（北京时间）</p>`:''}${snapshot?`<p class="small-note">队伍提交版本 ${Number(snapshot.submissionVersion)} · 正式提交 ${esc(beijingTime(snapshot.submittedAt))}<br>本轮截取 ${esc(beijingTime(snapshot.capturedAt))}（北京时间）</p>${materialMarkup(snapshot.materials)}`:!stage?'<p class="small-note">本轮没有正式材料快照。</p>':stage.frozenAt===null?'<p class="small-note">尚未生成材料快照；首次开放时截取当时的正式材料。</p>':'<p class="small-note">本轮冻结时无正式材料；可根据现场演示记录观察。后续提交材料不会自动替换本轮快照。</p>'}</section>`;}
