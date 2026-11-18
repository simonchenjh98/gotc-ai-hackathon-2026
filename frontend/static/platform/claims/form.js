'use strict';

const CLAIM_TRACK_OPTIONS=['AI for Science','城市与社区问题','未来学习样态'];
function emptyClaimMembers(){return [{name:'',school:''},{name:'',school:''}];}

function claimTeamMarkup(value,options=CLAIM_TRACK_OPTIONS){
  return `<label class="field">队伍名称<input name="teamName" required maxlength="80" value="${esc(value.teamName??'')}" autocomplete="off"></label><label class="field">参赛赛道<select name="track"><option value="">待定</option>${options.map(track=>`<option value="${esc(track)}" ${value.track===track?'selected':''}>${esc(track)}</option>`).join('')}</select></label>`;
}
function claimMembersMarkup(members){
  return `<fieldset class="claim-team-fields"><legend>组员（不含队长，2–4 人）</legend><p class="small-note">每队共 3–5 人，包含上方的队长。5 人队请点“添加组员”，填写 4 位组员。可来自同校或不同学校。</p>${members.map((p,i)=>`<fieldset class="claim-member-row" data-claim-member-row><legend>组员 ${i+1}</legend><label class="field">姓名<input name="memberName" required maxlength="100" value="${esc(p.name)}" autocomplete="off"></label><label class="field">学校<input name="memberSchool" required maxlength="200" value="${esc(p.school)}" autocomplete="organization"></label>${members.length>2?`<button class="btn secondary small" type="button" data-claim-remove="${i}">移除组员 ${i+1}</button>`:''}</fieldset>`).join('')}${members.length<4?'<button class="btn secondary small" id="claim-member-add" type="button">添加组员</button>':''}</fieldset>`;
}
function claimDeclarationMarkup(declaration){
  if(!declaration)return '<p class="small-note">此为旧版申请，尚未申报组员；可在批准前补充。</p>';
  return `<section class="claim-declaration"><h3>已提交的队伍成员（${declaration.members.length+1} 人）</h3><p>队伍：${esc(declaration.teamName || '待组织方补充')} · 赛道：${esc(declaration.track || '待定')}</p><ol>${[declaration.captain,...declaration.members].map((p,i)=>`<li>${i===0?'队长':'组员'}：${esc(p.name)} · ${esc(p.school)}</li>`).join('')}</ol></section>`;
}
function claimUpdateMarkup(state,draft){
  if(!state.editing)return '';
  const reviewed=Number.isSafeInteger(state.reviewRevision);
  return `<form id="claim-update-form"><h3>我的待保存队伍草稿</h3><p>队长：${esc(state.captain.name)} · ${esc(state.captain.school)}</p>${claimTeamMarkup(draft)}${claimMembersMarkup(draft.members)}`+
    `${state.needsReview?`<p class="info-banner warning" role="status">${reviewed?'“已提交的队伍成员”是刚读取的名单，本表单中的草稿已保留。保存会用草稿替换已保存名单与队伍信息，请逐人核对并合并需要保留的修改，再确认继续。':'草稿已保留。请先读取最新已保存名单并核对；核对完成前不会再次提交。'}</p>`:''}`+
    `<div class="actions"><button class="btn secondary" type="button" id="claim-edit-reload" ${state.busy?'disabled':''}>读取最新已保存名单并核对</button>${reviewed?`<button class="btn secondary" type="button" id="claim-edit-confirm" ${state.busy?'disabled':''}>已逐人核对，继续编辑草稿</button>`:''}</div>`+
    `<div class="actions"><button class="btn" ${state.busy || state.needsReview?'disabled':''}>保存队伍修改</button><button class="btn secondary" type="button" id="claim-edit-cancel">取消修改</button></div></form>`;
}
function claimStateMarkup(state){
  const labels={pending:'等待审核',approved:'队伍申请已批准',issued:'提交码已领取',rejected:'申请未通过，请联系组织方',expired:'申请已过期，请重新申请',invalid:'领取凭据无效或已过期，请重新申请',unavailable:'提交码已由组织方变更或停用，请联系组织方恢复'};
  const receipt=state.receipt,draft=state.draft??{name:'',school:'',members:emptyClaimMembers()};
  let body;
  if(!receipt){
    body=`<form id="claim-request-form"><fieldset ${state.busy?'disabled':''}><label class="field">队长姓名<input name="name" required maxlength="100" autocomplete="name" value="${esc(draft.name)}"></label><label class="field">队长学校<input name="school" required maxlength="200" autocomplete="organization" value="${esc(draft.school)}"></label>${claimTeamMarkup(draft)}${claimMembersMarkup(draft.members)}<button class="btn" ${state.busy?'disabled':''}>提交领取申请</button></fieldset></form>`;
  }else{
    body=`<h2>${esc(labels[state.status] || '等待审核')}</h2><p>申请已提交至组织方后台。符合自动审核条件的队伍会自动通过，其余等待工作人员审核。请保留本页，查询审核状态，批准后领取提交码。</p><p class="small-note">申请编号：<code>${esc(receipt.requestId)}</code><br>有效期至 ${esc(beijingTime(receipt.expiresAt))}（北京时间）<br>凭据只保存在此标签页，请勿转发或共享设备。</p>${claimDeclarationMarkup(state.declaration)}${claimUpdateMarkup(state,draft)}${state.team?`<p>已批准队伍：<strong>${esc(teamLabel(state.team))}</strong></p>`:''}<div class="actions"><button class="btn secondary" id="claim-status" ${state.busy?'disabled':''}>查询审核状态</button>${state.status==='pending' && !state.editing && state.captain && Number.isSafeInteger(state.requestRevision)?`<button class="btn secondary" id="claim-edit" ${state.busy?'disabled':''}>${state.declaration?'修改队伍与组员':'补充队伍与组员'}</button>`:''}${['approved','issued'].includes(state.status)?`<button class="btn" id="claim-redeem" ${state.busy?'disabled':''}>${state.status==='issued'?'再次显示本队提交码':'领取本队提交码'}</button>`:''}</div>${state.code?`<div class="code-output"><strong>本队提交码</strong><code>${esc(state.code)}</code><p>请队长保存，只与本队成员共享。</p><button class="btn secondary small" type="button" data-copy-value="${esc(state.code)}">复制提交码</button> <a href="#team" id="claim-workbench">前往选手工作台 ↗</a><p class="small-note">进入后使用本队提交码登录；切换原登录身份会保留申请凭据，可返回申请页再次查看提交码。</p></div>`:''}<button class="btn secondary small" id="claim-clear" type="button">清除此设备上的领取凭据</button>`;
  }
  return heading('TEAM / 队伍申请','申请队伍提交码','队长填写队名、赛道，以及全体队员的姓名和学校。姓名和学校请与接龙中的写法保持一致；整队信息能唯一匹配、且没有重复归队时自动通过。其余申请由工作人员审核。每人只参加一队，每队一个提交码。')+`<section class="surface claim-panel"><p class="form-error" role="alert">${esc(state.error)}</p>${body}</section>`;
}
function claimFormMembers(form){return [...form.querySelectorAll('[data-claim-member-row]')].map(row=>({name:row.querySelector('[name="memberName"]').value,school:row.querySelector('[name="memberSchool"]').value}));}
