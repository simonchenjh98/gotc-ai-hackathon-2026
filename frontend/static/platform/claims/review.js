'use strict';

function makeClaimsAdminGuard(getContext){
  const owner=getContext();
  return ()=>{const current=getContext();return current.connected && current.route==='admin' && current.role==='admin' && current.version===owner.version && current.loginId===owner.loginId && current.browserOwner;};
}
function claimsPersonForm(person={id:'',name:'',school:'',notes:'',needsReview:false,revision:0}){
  return `<form id="claim-person-form"><h3>${person.revision?'修订名单':'补充已核实参赛者'}</h3><input type="hidden" name="revision" value="${Number(person.revision)}"><label class="field">稳定名单编号<input name="id" maxlength="100" pattern="[A-Za-z0-9_-]+" required value="${esc(person.id)}" ${person.revision?'readonly':''}></label><label class="field">姓名<input name="name" required maxlength="100" value="${esc(person.name)}"></label><label class="field">学校<input name="school" required maxlength="200" value="${esc(person.school)}"></label><label class="field">原始记录与核实备注<textarea name="notes" maxlength="2000">${esc(person.notes)}</textarea></label><label class="toggle-line"><input type="checkbox" name="needsReview" ${person.needsReview?'checked':''}>仍需核对</label><button class="btn secondary small">保存名单修订</button><p class="form-error" role="alert"></p></form>`;
}
function claimExistingTeamMarkup(team){
  if(!team)return '<p class="small-note">默认批准后创建新队伍；如本队已登记，请主动选择对应队伍，系统不会按队名自动绑定。</p>';
  const registration=team.registration;
  return `<section class="info-banner"><strong>将审核已有队伍：${esc(teamLabel(team))}</strong><p>项目：${esc(team.project || '未填写')}（保留）</p>${registration?`<p>登记队长：${esc(registration.captain)} · ${esc(registration.school)}<br>登记成员：${registration.members.map(esc).join('、')}<br>登记修订：${Number(registration.revision)}</p><p>批准即确认该登记属于本申请，并以本次审核通过的队长、学校和全体成员更新登记；原项目保留；队长或学校变更时不沿用旧联系电话。</p>`:'<p>批准即确认此队属于本申请；保留队号和项目，并绑定本次审核通过的全体成员。</p>'}</section>`;
}
function claimTeamSelectionMarkup(data){
  return `<label class="field">归队方式（可选已有登记）<select name="teamId"><option value="">创建新队伍（默认）</option>${(data.teams??[]).map(team=>`<option value="${esc(team.id)}">${esc(teamLabel(team))}${team.registration?' · 已登记':''}</option>`).join('')}</select></label><div id="claim-existing-summary">${claimExistingTeamMarkup(null)}</div>`;
}
function claimsReviewMarkup(request,data){
  const status={pending:'待审核',approved:'已批准',issued:'已领取',rejected:'已拒绝',expired:'已过期'};
  if(request.status!=='pending')return `<h3>${esc(request.name)} · ${esc(status[request.status] || request.status)}</h3>${claimEmailStatusMarkup(request)}${claimDeclarationMarkup(request.declaration)}<p>${esc(request.reviewNote)}</p>`;
  const declared=request.declaration;
  const references=request.references??[];
  return `<form id="claim-review-form"><h3>队伍申请审核 · ${esc(request.name)}</h3><p>申请学校：<strong>${esc(request.school)}</strong><br>申请编号：<code>${esc(request.id)}</code> · 修订 ${Number(request.revision)}</p>${claimEmailStatusMarkup(request)}${claimDeclarationMarkup(declared)}${declared?`${claimTeamSelectionMarkup(data)}${claimTeamMarkup(declared,data.trackOptions??CLAIM_TRACK_OPTIONS)}<p class="small-note">审核上述申报成员；批准后创建新队或绑定所选已有队伍、登记成员并开放本队参赛码领取。</p>${references.length?`<details open><summary>相关私有身份参考（仅用于去重）</summary><ul>${references.map(p=>`<li>${esc(p.name)} · ${esc(p.school)} · ${p.teamId?'已归队，可能重复，请区分本人':'未归队，同名同校可复用'}${p.needsReview?' · 原记录待核实（参考）':''}<br>${esc(p.notes)}</li>`).join('')}</ul></details>`:'<p>无相关参考记录；批准后将自动登记新成员。</p>'}<p class="small-note">点击批准即确认已审核申报信息及所选已有登记（如有），并同意建队或归队。此为线上申请审核。</p><label class="toggle-line"><input name="resolvedIdentity" type="checkbox">如有其他学校的同名已归队人员，已确认属于不同本人，并在下方说明</label>`:'<p class="info-banner warning">请申请人打开原领取页面，补充全体组员后再刷新审核。无需预先导入名单。</p>'}<label class="field">审核或拒绝说明（选填；同名冲突需说明）<textarea name="note" maxlength="2000">${esc(request.reviewNote)}</textarea></label><div class="actions">${declared?'<button class="btn" type="submit">确认建队或归队并批准领取</button>':''}<button class="btn danger" type="button" id="claim-reject">拒绝申请</button></div><p class="form-error" role="alert"></p></form>`;
}
async function importTrustedClaimsFile({form,file,expectedRevision,isCurrent,submit,onError}){
  if(!file || !form.isConnected || !isCurrent())return;
  if(file.size>2*1024*1024){onError('名单文件超过 2 MB');return;}
  try{
    const body=JSON.parse(await file.text());
    if(!form.isConnected || form.elements.file.files[0]!==file || !isCurrent())return;
    if(!body || Object.keys(body).length!==1 || !body.autoReview || typeof body.autoReview!=='object' || Array.isArray(body.autoReview))throw new Error('请选择独立可信名单 autoReview 文件');
    await submit({autoReview:{...body.autoReview,expectedRevision}});
  }catch(error){if(form.isConnected && isCurrent())onError(error.message);}
}
function installClaimsAdmin(version){
  root.insertAdjacentHTML('beforeend',`<section class="surface" id="claims-admin"><h2>队伍申请审核</h2><p>所有队伍均可申请，无需预置名单或队伍。组织方审核批准后创建队伍；每名选手仅归属一队。</p><button class="btn secondary small" id="claims-admin-load">读取 / 刷新申请与名单</button><p id="claims-admin-message" role="status"></p><div id="claims-admin-content"></div></section>`);
  const container=document.getElementById('claims-admin'),message=document.getElementById('claims-admin-message'),content=document.getElementById('claims-admin-content');
  const live=makeClaimsAdminGuard(()=>({connected:container.isConnected,route,role:session.role,version:routeVersion,loginId:session.loginId,browserOwner:ownsBrowserLogin(session)}));
  let model=null,read=0,edit=0,busy=false;
  container.addEventListener('input',()=>{edit++;});
  const clearForms=()=>{for(const form of content.querySelectorAll('form'))dirtyAdminForms.delete(form);};
  const hasEdits=()=>[...content.querySelectorAll('form')].some(form=>dirtyAdminForms.has(form));
  async function load(){
    if(!live() || busy)return;if(hasEdits() && !confirm('刷新会替换本面板未保存的核验输入。请先复制备注。继续？'))return;
    const operation=++read,revision=edit;message.textContent='正在读取…';
    try{const next=await api('admin/claims');if(!live() || operation!==read)return;if(revision!==edit){message.textContent='读取期间输入已修改，已保留。请保存或复制后重新读取。';return;}clearForms();model=next;draw();message.textContent='已读取。批准前请审核全体申报成员并检查同名身份参考。';}
    catch(error){if(live() && operation===read)message.textContent=error.message;}
  }
  function draw(){
    content.innerHTML=`<label class="field">搜索申请与名单<input id="claims-search" type="search" placeholder="姓名、学校、编号或状态"></label><div class="claim-columns"><section><h3>领取申请</h3><div class="claim-scroll">${model.requests.map(r=>`<p data-claim-search="${esc([r.id,r.name,r.school,r.status].join(' ').toLowerCase())}"><button type="button" class="btn secondary small" data-claim-request="${esc(r.id)}">${esc(r.name)} · ${esc(r.school)} · ${esc(({pending:'待审核',approved:'已批准',issued:'已领取',expired:'已过期',rejected:'已拒绝'})[r.status] || r.status)} · 修订 ${Number(r.revision)}</button></p>`).join('') || '<p>暂无申请。</p>'}</div></section><details><summary>私有报名参考记录（${model.people.length} 条）</summary><div class="claim-scroll">${model.people.map(p=>`<details data-claim-search="${esc([p.id,p.name,p.school].join(' ').toLowerCase())}"><summary>${esc(p.name)} · ${esc(p.school)}${p.needsReview?'（待核实）':''}${p.teamId?'（已归队）':''}</summary><p>编号：${esc(p.id)} · 修订 ${Number(p.revision)}${p.teamId?`<br>队伍：${esc(p.teamId)}`:''}</p><pre class="claim-notes">${esc(p.notes)}</pre><button class="btn secondary small" type="button" data-claim-person="${esc(p.id)}">修订此人</button></details>`).join('') || '<p>暂无参考记录，仍可接受并批准新队伍申请。</p>'}</div><button type="button" class="btn secondary small" id="claim-person-add">补充参考记录</button></details></div><div id="claim-review"></div><div id="claim-person-editor"></div><details><summary>导入私有名单</summary><form id="claim-import-form"><label class="field">名单 JSON 文件<input type="file" name="file" accept=".json,application/json" required></label><p class="small-note">格式：people 数组，包含 id、name、school、notes、needsReview；最多 500 人。重复导入不改写已有记录。</p><button class="btn secondary small">导入名单</button><p class="form-error" role="alert"></p></form></details>`;
    const auto=model.autoReview??{revision:0,enabled:false,count:0,eligibleCount:0,ambiguousCount:0,sourceName:''};
    content.insertAdjacentHTML('beforeend',`${model.emailConfigured?'':'<p class="info-banner warning" role="status">邮件服务尚未配置，申请邮件将暂存，配置后在有效期内发送。</p>'}<details><summary>可信自动审核名单（独立来源）</summary><p>状态：${auto.enabled?'已启用':'已停用'} · 修订 ${Number(auto.revision)} · ${Number(auto.count)} 条 · 可匹配 ${Number(auto.eligibleCount)} 条 · 歧义 ${Number(auto.ambiguousCount)} 条</p><p>来源：${esc(auto.sourceName)}<br>SHA-256：<code>${esc(auto.sourceSha256??'')}</code></p><p class="small-note">仅整队姓名与学校唯一匹配且无冲突时自动批准；普通参考记录及公开登记不会获得此资格。替换名单后须重新启用。不会开通 API 服务。</p><form id="claim-auto-toggle"><button class="btn secondary small">${auto.enabled?'停用':'启用'}自动审核</button><p class="form-error" role="alert"></p></form><form id="claim-auto-import"><label class="field">完整可信名单 JSON<input type="file" name="file" accept=".json,application/json" required></label><p class="small-note">autoReview：expectedRevision、sourceName、sourceSha256、entries（id、name、school、eligible、notes），最多 500 条。提交时使用当前显示的修订号，完整替换不会分批生效。</p><button class="btn secondary small">替换可信名单并停用</button><p class="form-error" role="alert"></p></form></details>`);
    const toggle=document.getElementById('claim-auto-toggle');trackAdminForm(toggle);toggle.onsubmit=event=>{event.preventDefault();mutate(toggle,'admin/claims/auto-review',{expectedRevision:auto.revision,enabled:!auto.enabled});};
    const trusted=document.getElementById('claim-auto-import');trackAdminForm(trusted);let importRead=0;
    trusted.onsubmit=async event=>{
      event.preventDefault();const file=trusted.elements.file.files[0],snapshot=model,readRevision=read,inputRevision=edit,operation=++importRead;
      await importTrustedClaimsFile({form:trusted,file,expectedRevision:auto.revision,
        isCurrent:()=>live() && !busy && model===snapshot && read===readRevision && edit===inputRevision && importRead===operation,
        submit:body=>mutate(trusted,'admin/claims/roster',body),onError:error=>formError(trusted,error)});
    };
    document.getElementById('claims-search').oninput=e=>{const q=e.target.value.trim().toLowerCase();for(const item of content.querySelectorAll('[data-claim-search]'))item.hidden=!item.dataset.claimSearch.includes(q);};
    content.querySelectorAll('[data-claim-request]').forEach(button=>button.onclick=()=>selectRequest(button.dataset.claimRequest));
    content.querySelectorAll('[data-claim-person]').forEach(button=>button.onclick=()=>editPerson(model.people.find(p=>p.id===button.dataset.claimPerson)));
    document.getElementById('claim-person-add').onclick=()=>editPerson();
    const form=document.getElementById('claim-import-form');trackAdminForm(form);form.onsubmit=async event=>{event.preventDefault();const file=form.elements.file.files[0];if(!file)return;if(file.size>2*1024*1024){formError(form,'名单文件超过 2 MB');return;}try{const body=JSON.parse(await file.text());if(live())await mutate(form,'admin/claims/roster',body);}catch(error){if(live())formError(form,error.message);}};
  }
  async function mutate(form,path,body){
    if(!live() || busy)return;busy=true;form.querySelectorAll('button').forEach(button=>button.disabled=true);
    try{await api(path,body);if(!live())return;dirtyAdminForms.delete(form);message.textContent='已保存，请刷新申请与名单后继续核验。';form.querySelectorAll('input,textarea,select,button').forEach(input=>input.disabled=true);}
    catch(error){if(live()){formError(form,error.message);if(error.status===409)form.querySelectorAll('button').forEach(button=>button.dataset.stale='true');}}
    finally{busy=false;if(live())form.querySelectorAll('button').forEach(button=>{if(!button.dataset.stale && !form.querySelector('input:disabled'))button.disabled=false;});}
  }
  function replaceEditor(panel){if([...panel.querySelectorAll('form')].some(f=>dirtyAdminForms.has(f)) && !confirm('切换会替换未保存输入。请先复制备注。继续？'))return false;for(const f of panel.querySelectorAll('form'))dirtyAdminForms.delete(f);edit++;return true;}
  function editPerson(person){
    if(!live() || busy)return;const panel=document.getElementById('claim-person-editor');if(!replaceEditor(panel))return;panel.innerHTML=claimsPersonForm(person);const form=document.getElementById('claim-person-form');trackAdminForm(form);
    form.onsubmit=e=>{e.preventDefault();const f=form.elements;mutate(form,'admin/claims/person',{expectedRevision:Number(f.revision.value),person:{id:f.id.value,name:f.name.value,school:f.school.value,notes:f.notes.value,needsReview:f.needsReview.checked}});};
  }
  function selectRequest(id){
    if(!live() || busy)return;const panel=document.getElementById('claim-review');if(!replaceEditor(panel))return;const request=model.requests.find(r=>r.id===id);panel.innerHTML=claimsReviewMarkup(request,model);const form=document.getElementById('claim-review-form');if(!form)return;trackAdminForm(form);
    const existing=form.elements.teamId;
    if(existing)existing.onchange=()=>{
      const team=model.teams.find(team=>team.id===existing.value);
      document.getElementById('claim-existing-summary').innerHTML=claimExistingTeamMarkup(team);
    };
    const common=action=>({requestId:id,expectedRevision:request.revision,action,note:form.elements.note.value});
    form.onsubmit=e=>{
      e.preventDefault();if(!request.declaration)return;const f=form.elements;
      const selectedId=f.teamId?.value??'',selected=(model.teams??[]).find(team=>team.id===selectedId);
      if(selectedId && !selected){formError(form,'所选队伍已变化，请刷新后核对');return;}
      mutate(form,'admin/claims/approve',{requestId:id,expectedRevision:request.revision,reviewContext:request.reviewContext,
        teamName:f.teamName.value,track:f.track.value,note:f.note.value,verificationMethod:'online',applicationReviewed:true,
        ...(selected?{teamId:selected.id,teamContext:selected.teamContext}:{}),resolvedIdentity:f.resolvedIdentity.checked});
    };
    document.getElementById('claim-reject').onclick=()=>{if(confirm('确认拒绝此申请？'))mutate(form,'admin/claims/review',common('reject'));};
  }
  document.getElementById('claims-admin-load').onclick=load;
}
