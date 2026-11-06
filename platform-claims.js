'use strict';

// Receipt storage is scoped to this tab and deployment path. Codes stay in memory.
function makeClaimReceiptStore(storage,namespace){
  const key='gotc-claim-receipt:v1:'+encodeURIComponent(namespace);
  const valid=value=>value && /^[a-f0-9]{32}$/.test(value.requestId) && /^[A-Za-z0-9_-]{43}$/.test(value.secret) && /^\d{6}$/.test(value.pin) && Number.isSafeInteger(value.expiresAt);
  return {
    read(){try{const raw=storage().getItem(key);if(!raw)return {receipt:null,error:''};const receipt=JSON.parse(raw);if(!valid(receipt))throw new Error();return {receipt,error:''};}catch{return {receipt:null,error:'无法读取本标签页的领取凭据，请核对浏览器存储设置。'};}},
    write(receipt){try{if(!valid(receipt))throw new Error();storage().setItem(key,JSON.stringify({requestId:receipt.requestId,secret:receipt.secret,pin:receipt.pin,expiresAt:receipt.expiresAt}));return '';}catch{return '无法保存领取凭据；刷新或关闭页面后可能无法继续领取，请保持本页并联系组织方。';}},
    clear(){try{storage().removeItem(key);return '';}catch{return '无法确认领取凭据已清除；在共享设备上请清除此站点的浏览器数据。';}}
  };
}
function emptyClaimMembers(){return [{name:'',school:''},{name:'',school:''}];}
function makeClaimController({store,request,isCurrent,onChange}){
  const saved=store.read();let storageError=saved.error,receipt=saved.receipt,generation=0,busy=false;
  let draft={name:'',school:'',members:emptyClaimMembers()},editRevision=null;
  let state={status:receipt?'pending':'new',team:null,code:null,error:saved.error,declaration:null,captain:null,requestRevision:null,editing:false};
  const publish=()=>{if(isCurrent())onChange({...state,draft,error:[storageError,state.error].filter((value,index,array)=>value && array.indexOf(value)===index).join(' '),receipt,busy});};
  const copyDraft=value=>({name:value.name,school:value.school,members:value.members.map(p=>({...p}))});
  async function run(action,body){
    if(busy || !isCurrent())return;const operation=++generation;busy=true;state.error='';publish();
    try{
      const result=await request('claims/'+action,body);
      if(operation!==generation || !isCurrent())return;
      if(action==='request'){
        receipt={requestId:result.requestId,secret:result.secret,pin:result.pin,expiresAt:result.expiresAt};storageError=store.write(receipt);
        state={...state,...result,code:null,error:'',editing:false};
      }else{
        const keepEditing=result.status==='pending' && ((action==='update' && JSON.stringify(draft.members)!==JSON.stringify(body.members)) || (action==='status' && state.editing));
        if(action==='update' && keepEditing)editRevision=result.requestRevision;
        state={...state,...result,error:'',editing:keepEditing};
      }
    }catch(error){if(operation===generation && isCurrent())state.error=error.message;}
    finally{if(operation===generation){busy=false;publish();}}
  }
  return {
    start(){publish();if(receipt)return this.status();},
    setDraft(value){draft=copyDraft(value);},
    addMember(){if(draft.members.length<4){draft={...draft,members:[...draft.members,{name:'',school:''}]};publish();}},
    removeMember(index){if(draft.members.length>2){draft={...draft,members:draft.members.filter((_,i)=>i!==index)};publish();}},
    apply(name,school,members){draft=copyDraft({name,school,members:members??emptyClaimMembers()});return run('request',{name,school,...(members===undefined?{}:{members:draft.members})});},
    edit(){if(busy || state.status!=='pending' || !state.captain || !Number.isSafeInteger(state.requestRevision))return;draft=copyDraft({...state.captain,members:state.declaration?.members??emptyClaimMembers()});editRevision=state.requestRevision;state.editing=true;state.error='';publish();},
    update(members){draft=copyDraft({...draft,members});if(receipt)return run('update',{requestId:receipt.requestId,secret:receipt.secret,expectedRevision:editRevision,members:draft.members});},
    cancelEdit(){generation++;busy=false;state.editing=false;state.error='';publish();return this.status();},
    status(){if(receipt)return run('status',{requestId:receipt.requestId,secret:receipt.secret});},
    redeem(){if(receipt)return run('redeem',{requestId:receipt.requestId,secret:receipt.secret});},
    clear(){generation++;busy=false;receipt=null;storageError=store.clear();draft={name:'',school:'',members:emptyClaimMembers()};state={status:'new',team:null,code:null,error:'',declaration:null,captain:null,requestRevision:null,editing:false};publish();},
    dispose(){generation++;busy=false;state.code=null;}
  };
}
function claimReceiptStore(){return makeClaimReceiptStore(()=>sessionStorage,base.pathname);}
let currentClaimController=null;
function clearClaimReceipt(){currentClaimController?.clear();return claimReceiptStore().clear();}
function claimMembersMarkup(members){
  return `<fieldset class="claim-team-fields"><legend>组员（不含队长，2–4 人）</legend><p class="small-note">每队共 3–5 人。可来自同校或不同学校，请逐人填写。</p>${members.map((p,i)=>`<fieldset class="claim-member-row" data-claim-member-row><legend>组员 ${i+1}</legend><label class="field">姓名<input name="memberName" required maxlength="100" value="${esc(p.name)}" autocomplete="off"></label><label class="field">学校<input name="memberSchool" required maxlength="200" value="${esc(p.school)}" autocomplete="organization"></label>${members.length>2?`<button class="btn secondary small" type="button" data-claim-remove="${i}">移除组员 ${i+1}</button>`:''}</fieldset>`).join('')}${members.length<4?'<button class="btn secondary small" id="claim-member-add" type="button">添加组员</button>':''}</fieldset>`;
}
function claimDeclarationMarkup(declaration){
  if(!declaration)return '<p class="small-note">此为旧版申请，尚未申报组员；可在批准前补充。</p>';
  return `<section class="claim-declaration"><h3>已提交的队伍成员（${declaration.members.length+1} 人）</h3><ol>${[declaration.captain,...declaration.members].map((p,i)=>`<li>${i===0?'队长':'组员'}：${esc(p.name)} · ${esc(p.school)}</li>`).join('')}</ol></section>`;
}
function claimStateMarkup(state){
  const labels={pending:'等待现场核验',approved:'现场核验已通过',issued:'提交码已领取',rejected:'申请未通过，请联系现场组织方',expired:'申请已过期，请重新申请',invalid:'领取凭据无效或已过期，请重新申请',unavailable:'提交码已由组织方变更或停用，请联系现场组织方恢复'};
  const receipt=state.receipt,draft=state.draft??{name:'',school:'',members:emptyClaimMembers()};
  let body;
  if(!receipt){
    body=`<form id="claim-request-form"><label class="field">队长姓名<input name="name" required maxlength="100" autocomplete="name" value="${esc(draft.name)}"></label><label class="field">队长学校<input name="school" required maxlength="200" autocomplete="organization" value="${esc(draft.school)}"></label>${claimMembersMarkup(draft.members)}<button class="btn" ${state.busy?'disabled':''}>提交领取申请</button></form>`;
  }else{
    body=`<h2>${esc(labels[state.status] || '等待核验')}</h2><p>请在现场向组织方出示本页核验码。</p><p class="claim-pin" aria-label="现场核验码">${esc(receipt.pin)}</p><p class="small-note">申请编号：<code>${esc(receipt.requestId)}</code><br>有效期至 ${esc(beijingTime(receipt.expiresAt))}（北京时间）<br>凭据只保存在此标签页，请勿转发或共享设备。</p>${claimDeclarationMarkup(state.declaration)}${state.editing?`<form id="claim-update-form"><h3>修改待核验组员</h3><p>队长：${esc(state.captain.name)} · ${esc(state.captain.school)}</p>${claimMembersMarkup(draft.members)}<div class="actions"><button class="btn" ${state.busy?'disabled':''}>保存组员修改</button><button class="btn secondary" type="button" id="claim-edit-cancel">取消修改</button></div></form>`:''}${state.team?`<p>已核实队伍：<strong>${esc(teamLabel(state.team))}</strong></p>`:''}<div class="actions"><button class="btn secondary" id="claim-status" ${state.busy?'disabled':''}>查询审核状态</button>${state.status==='pending' && !state.editing && state.captain && Number.isSafeInteger(state.requestRevision)?`<button class="btn secondary" id="claim-edit" ${state.busy?'disabled':''}>${state.declaration?'修改组员':'补充组员'}</button>`:''}${['approved','issued'].includes(state.status)?`<button class="btn" id="claim-redeem" ${state.busy?'disabled':''}>${state.status==='issued'?'再次显示本队提交码':'领取本队提交码'}</button>`:''}</div>${state.code?`<div class="code-output"><strong>本队提交码</strong><code>${esc(state.code)}</code><p>请队长保存，只与本队成员共享。</p><button class="btn secondary small" type="button" data-copy-value="${esc(state.code)}">复制提交码</button> <a href="#team">前往作品提交 ↗</a></div>`:''}<button class="btn secondary small" id="claim-clear" type="button">清除此设备上的领取凭据</button>`;
  }
  return heading('TEAM / 现场领取','领取队伍提交码','填写队长及全体组员的姓名和学校后，请队长携全体组员到组织方处核实身份。每人只参加一个队，每队一个提交码。')+`<section class="surface claim-panel"><p class="form-error" role="alert">${esc(state.error)}</p>${body}</section>`;
}
function claimFormMembers(form){return [...form.querySelectorAll('[data-claim-member-row]')].map(row=>({name:row.querySelector('[name="memberName"]').value,school:row.querySelector('[name="memberSchool"]').value}));}
async function renderClaim(version){
  currentClaimController?.dispose();
  const controller=makeClaimController({store:claimReceiptStore(),request:api,isCurrent:()=>route==='claim' && version===routeVersion && currentClaimController===controller,onChange:state=>{
    root.innerHTML=claimStateMarkup(state);
    const form=document.getElementById('claim-request-form')??document.getElementById('claim-update-form');
    if(form){
      const read=()=>({name:form.elements.name?.value??state.captain.name,school:form.elements.school?.value??state.captain.school,members:claimFormMembers(form)});
      form.oninput=()=>controller.setDraft(read());
      form.onsubmit=e=>{e.preventDefault();const value=read();if(state.receipt)controller.update(value.members);else controller.apply(value.name,value.school,value.members);};
      const add=document.getElementById('claim-member-add');if(add)add.onclick=()=>{controller.setDraft(read());controller.addMember();};
      form.querySelectorAll('[data-claim-remove]').forEach(button=>button.onclick=()=>{controller.setDraft(read());controller.removeMember(Number(button.dataset.claimRemove));});
    }
    const edit=document.getElementById('claim-edit');if(edit)edit.onclick=()=>controller.edit();
    const cancel=document.getElementById('claim-edit-cancel');if(cancel)cancel.onclick=()=>controller.cancelEdit();
    const refresh=document.getElementById('claim-status');if(refresh)refresh.onclick=()=>controller.status();
    const redeem=document.getElementById('claim-redeem');if(redeem)redeem.onclick=()=>controller.redeem();
    const clear=document.getElementById('claim-clear');if(clear)clear.onclick=()=>{if(confirm('清除后本标签页无法继续领取。已领取的提交码请先自行保存。确认清除？'))controller.clear();};
  }});
  currentClaimController=controller;await controller.start();
}

function makeClaimsAdminGuard(getContext){
  const owner=getContext();
  return ()=>{const current=getContext();return current.connected && current.route==='admin' && current.role==='admin' && current.version===owner.version && current.loginId===owner.loginId && current.browserOwner;};
}
function claimsPersonForm(person={id:'',name:'',school:'',notes:'',needsReview:false,revision:0}){
  return `<form id="claim-person-form"><h3>${person.revision?'修订名单':'补充已核实参赛者'}</h3><input type="hidden" name="revision" value="${Number(person.revision)}"><label class="field">稳定名单编号<input name="id" maxlength="100" pattern="[A-Za-z0-9_-]+" required value="${esc(person.id)}" ${person.revision?'readonly':''}></label><label class="field">姓名<input name="name" required maxlength="100" value="${esc(person.name)}"></label><label class="field">学校<input name="school" required maxlength="200" value="${esc(person.school)}"></label><label class="field">原始记录与核实备注<textarea name="notes" maxlength="2000">${esc(person.notes)}</textarea></label><label class="toggle-line"><input type="checkbox" name="needsReview" ${person.needsReview?'checked':''}>仍需现场核实</label><button class="btn secondary small">保存名单修订</button><p class="form-error" role="alert"></p></form>`;
}
function claimMemberSnapshot(memberIds,people){
  const revisions=new Map(people.map(person=>[person.id,person.revision]));
  return {memberIds:[...memberIds],memberRevisions:Object.fromEntries(memberIds.map(id=>[id,revisions.get(id)]))};
}
function claimsReviewMarkup(request,data){
  const status={pending:'待核验',approved:'已批准',issued:'已领取',rejected:'已拒绝',expired:'已过期'};
  if(request.status!=='pending')return `<h3>${esc(request.name)} · ${esc(status[request.status] || request.status)}</h3>${claimDeclarationMarkup(request.declaration)}<p>${esc(request.reviewNote)}</p>`;
  const eligible=data.people.filter(person=>!person.teamId);
  return `<form id="claim-review-form"><h3>现场核验 · ${esc(request.name)}</h3><p>申请学校：<strong>${esc(request.school)}</strong><br>申请编号：<code>${esc(request.id)}</code> · 修订 ${Number(request.revision)}</p><label class="field">查找有效队伍<input id="claim-team-search" type="search" placeholder="队号或队名"></label><label class="field">已有有效队伍<select name="teamId" required><option value="">请选择（创建队伍请使用队伍管理）</option>${data.teams.map(t=>`<option value="${esc(t.id)}">${esc(teamLabel(t))}</option>`).join('')}</select></label><p class="small-note">已发提交码的队伍不能再次领取，请在作品提交管理中恢复。</p>${request.declaration?`<fieldset class="claim-team-fields"><legend>按申报逐人核对名单（${request.declaration.members.length+1} 人）</legend>${[request.declaration.captain,...request.declaration.members].map((declared,index)=>`<label class="field">${index===0?'队长':'组员 '+index}：${esc(declared.name)} · ${esc(declared.school)}<select name="${index===0?'captainId':'declaredMemberId'}" data-claim-map="${index}" required><option value="">选择此人的名单记录</option>${eligible.map(p=>`<option value="${esc(p.id)}">${esc(p.name)} · ${esc(p.school)} · ${esc(p.id)}${p.needsReview?'（待核实）':''}</option>`).join('')}</select><span class="small-note" data-claim-map-note="${index}"></span></label>`).join('')}</fieldset>`:`<label class="field">队长名单记录<select name="captainId" required><option value="">请选择与申请人同名的本人记录</option>${eligible.map(p=>`<option value="${esc(p.id)}">${esc(p.name)} · ${esc(p.school)}${p.needsReview?'（待核实）':''}</option>`).join('')}</select></label><p id="claim-captain-school" role="status"></p><label class="field">查找组员<input type="search" id="claim-member-search" placeholder="姓名、学校或名单编号"></label><fieldset class="claim-members"><legend>当面核实全部组员（须勾选队长）</legend>${eligible.map(p=>`<label class="toggle-line" data-claim-member="${esc([p.id,p.name,p.school].join(' ').toLowerCase())}"><input type="checkbox" name="memberIds" value="${esc(p.id)}">${esc(p.name)} · ${esc(p.school)} · ${esc(p.id)}${p.needsReview?'（待核实）':''}</label>`).join('')}</fieldset>` }<label class="field">申请人设备上的六位核验码<input name="pin" inputmode="numeric" pattern="[0-9]{6}" minlength="6" maxlength="6" autocomplete="off" required></label><label class="toggle-line"><input name="inPersonVerified" type="checkbox" required>已当面核实申请人及全体组员身份和组队关系</label><label class="toggle-line"><input name="resolvedIdentity" type="checkbox">已逐人解决申报学校差异及所选成员的待核实项</label><label class="field">核实或拒绝说明<textarea name="note" maxlength="2000">${esc(request.reviewNote)}</textarea></label><div class="actions"><button class="btn" type="submit">确认归队并批准领取</button><button class="btn danger" type="button" id="claim-reject">拒绝申请</button></div><p class="form-error" role="alert"></p></form>`;
}
function installClaimsAdmin(version){
  root.insertAdjacentHTML('beforeend',`<section class="surface" id="claims-admin"><h2>现场报名核验与提交码领取</h2><p>仅组织方可读取名单。每名选手仅归属一队；申请本身不占用身份。</p><button class="btn secondary small" id="claims-admin-load">读取 / 刷新申请与名单</button><p id="claims-admin-message" role="status"></p><div id="claims-admin-content"></div></section>`);
  const container=document.getElementById('claims-admin'),message=document.getElementById('claims-admin-message'),content=document.getElementById('claims-admin-content');
  const live=makeClaimsAdminGuard(()=>({connected:container.isConnected,route,role:session.role,version:routeVersion,loginId:session.loginId,browserOwner:ownsBrowserLogin(session)}));
  let model=null,read=0,edit=0,busy=false;
  container.addEventListener('input',()=>{edit++;});
  const clearForms=()=>{for(const form of content.querySelectorAll('form'))dirtyAdminForms.delete(form);};
  const hasEdits=()=>[...content.querySelectorAll('form')].some(form=>dirtyAdminForms.has(form));
  async function load(){
    if(!live() || busy)return;if(hasEdits() && !confirm('刷新会替换本面板未保存的核验输入。请先复制备注。继续？'))return;
    const operation=++read,revision=edit;message.textContent='正在读取…';
    try{const next=await api('admin/claims');if(!live() || operation!==read)return;if(revision!==edit){message.textContent='读取期间输入已修改，已保留。请保存或复制后重新读取。';return;}clearForms();model=next;draw();message.textContent='已读取。批准前请核对申请学校、名单学校及全部组员。';}
    catch(error){if(live() && operation===read)message.textContent=error.message;}
  }
  function draw(){
    content.innerHTML=`<label class="field">搜索申请与名单<input id="claims-search" type="search" placeholder="姓名、学校、编号或状态"></label><div class="claim-columns"><section><h3>领取申请</h3><div class="claim-scroll">${model.requests.map(r=>`<p data-claim-search="${esc([r.id,r.name,r.school,r.status].join(' ').toLowerCase())}"><button type="button" class="btn secondary small" data-claim-request="${esc(r.id)}">${esc(r.name)} · ${esc(r.school)} · ${esc(({pending:'待核验',approved:'已批准',issued:'已领取',expired:'已过期',rejected:'已拒绝'})[r.status] || r.status)} · 修订 ${Number(r.revision)}</button></p>`).join('') || '<p>暂无申请。</p>'}</div></section><section><h3>私有报名核对记录（${model.people.length} 条）</h3><div class="claim-scroll">${model.people.map(p=>`<details data-claim-search="${esc([p.id,p.name,p.school].join(' ').toLowerCase())}"><summary>${esc(p.name)} · ${esc(p.school)}${p.needsReview?'（待核实）':''}${p.teamId?'（已归队）':''}</summary><p>编号：${esc(p.id)} · 修订 ${Number(p.revision)}${p.teamId?`<br>队伍：${esc(p.teamId)}`:''}</p><pre class="claim-notes">${esc(p.notes)}</pre><button class="btn secondary small" type="button" data-claim-person="${esc(p.id)}">修订此人</button></details>`).join('') || '<p>请先导入名单；导入前公众申请关闭。</p>'}</div><button type="button" class="btn secondary small" id="claim-person-add">补充参赛者</button></section></div><div id="claim-review"></div><div id="claim-person-editor"></div><details><summary>导入私有名单</summary><form id="claim-import-form"><label class="field">名单 JSON 文件<input type="file" name="file" accept=".json,application/json" required></label><p class="small-note">格式：people 数组，包含 id、name、school、notes、needsReview；最多 500 人。重复导入不改写已有记录。</p><button class="btn secondary small">导入名单</button><p class="form-error" role="alert"></p></form></details>`;
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
    const memberSearch=document.getElementById('claim-member-search');if(memberSearch)memberSearch.oninput=e=>{const q=e.target.value.trim().toLowerCase();for(const item of form.querySelectorAll('[data-claim-member]'))item.hidden=!item.dataset.claimMember.includes(q);};
    document.getElementById('claim-team-search').oninput=e=>{const q=e.target.value.trim().toLowerCase();for(const option of form.elements.teamId.options)option.hidden=Boolean(option.value && !option.textContent.toLowerCase().includes(q));};
    if(!request.declaration)form.elements.captainId.onchange=()=>{const captain=model.people.find(p=>p.id===form.elements.captainId.value);document.getElementById('claim-captain-school').textContent=captain?`申请学校：${request.school}；名单学校：${captain.school}。${captain.notes}`:'';};
    if(request.declaration)form.querySelectorAll('[data-claim-map]').forEach(select=>select.onchange=()=>{
      const person=model.people.find(p=>p.id===select.value);
      form.querySelector(`[data-claim-map-note="${select.dataset.claimMap}"]`).textContent=person?`名单学校：${person.school}；名单修订：${person.revision}。${person.notes}`:'';
    });
    const common=action=>({requestId:id,expectedRevision:request.revision,action,note:form.elements.note.value});
    form.onsubmit=e=>{
      e.preventDefault();const f=form.elements;
      const mapping=request.declaration?[...form.querySelectorAll('[data-claim-map]')].map(select=>select.value):null;
      const memberIds=mapping??[...form.querySelectorAll('[name="memberIds"]:checked')].map(input=>input.value);
      mutate(form,'admin/claims/review',{...common('approve'),teamId:f.teamId.value,captainId:f.captainId.value,...claimMemberSnapshot(memberIds,model.people),...(mapping?{declaredMemberIds:mapping}:{}),pin:f.pin.value,inPersonVerified:f.inPersonVerified.checked,resolvedIdentity:f.resolvedIdentity.checked});
    };
    document.getElementById('claim-reject').onclick=()=>{if(confirm('确认拒绝此申请？'))mutate(form,'admin/claims/review',common('reject'));};
  }
  document.getElementById('claims-admin-load').onclick=load;
}
