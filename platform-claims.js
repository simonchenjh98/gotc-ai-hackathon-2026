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
const CLAIM_TRACK_OPTIONS=['AI for Science','城市与社区问题','未来学习样态'];
function emptyClaimMembers(){return [{name:'',school:''},{name:'',school:''}];}
function makeClaimController({store,request,isCurrent,onChange}){
  const saved=store.read();let storageError=saved.error,receipt=saved.receipt,generation=0,busy=false;
  let draft={name:'',school:'',teamName:'',track:'',members:emptyClaimMembers()},editRevision=null;
  let state={status:receipt?'pending':'new',team:null,code:null,error:saved.error,declaration:null,captain:null,requestRevision:null,editing:false,needsReview:false,reviewRevision:null};
  const publish=()=>{if(isCurrent())onChange({...state,draft,error:[storageError,state.error].filter((value,index,array)=>value && array.indexOf(value)===index).join(' '),receipt,busy});};
  const copyDraft=value=>({name:value.name,school:value.school,teamName:value.teamName??'',track:value.track??'',members:value.members.map(p=>({...p}))});
  async function run(action,body,reviewLatest=false){
    if(busy || !isCurrent())return;const operation=++generation;busy=true;state.error='';publish();
    try{
      const result=await request('claims/'+action,body);
      if(operation!==generation || !isCurrent())return;
      if(action==='request'){
        receipt={requestId:result.requestId,secret:result.secret,pin:result.pin,expiresAt:result.expiresAt};storageError=store.write(receipt);
        state={...state,...result,code:null,error:'',editing:false,needsReview:false,reviewRevision:null};
      }else{
        const keepEditing=result.status==='pending' && ((action==='update' && (JSON.stringify(draft.members)!==JSON.stringify(body.members) || (body.teamName!==undefined && (draft.teamName!==body.teamName || draft.track!==body.track)))) || (action==='status' && state.editing));
        if(action==='update' && keepEditing)editRevision=result.requestRevision;
        const needsReview=keepEditing && action==='status' && (reviewLatest || state.needsReview || result.requestRevision!==editRevision);
        state={...state,...result,error:'',editing:keepEditing,needsReview,
          reviewRevision:needsReview && reviewLatest && Number.isSafeInteger(result.requestRevision)?result.requestRevision:null};
      }
    }catch(error){if(operation===generation && isCurrent()){
      state.error=error.message;
      if(action==='update' || reviewLatest){state.needsReview=true;state.reviewRevision=null;}
    }}
    finally{if(operation===generation){busy=false;publish();}}
  }
  return {
    navigationState(){
      if(!isCurrent())return {pendingInitial:false,dirty:false};
      const saved=copyDraft({...state.captain,teamName:state.declaration?.teamName,track:state.declaration?.track,members:state.declaration?.members??emptyClaimMembers()});
      return {pendingInitial:busy && !receipt,dirty:receipt?state.editing && JSON.stringify(draft)!==JSON.stringify(saved):[draft.name,draft.school,draft.teamName,draft.track,...draft.members.flatMap(p=>[p.name,p.school])].some(value=>value.trim())};
    },
    start(){publish();if(receipt)return this.status();},
    setDraft(value){if(!isCurrent() || (busy && !receipt))return;draft=copyDraft(value);},
    addMember(){if(!isCurrent() || (busy && !receipt))return;if(draft.members.length<4){draft={...draft,members:[...draft.members,{name:'',school:''}]};publish();}},
    removeMember(index){if(!isCurrent() || (busy && !receipt))return;if(draft.members.length>2){draft={...draft,members:draft.members.filter((_,i)=>i!==index)};publish();}},
    apply(name,school,members,team){if(busy || !isCurrent())return;draft=copyDraft({name,school,...team,members:members??emptyClaimMembers()});return run('request',{name,school,...(members===undefined?{}:{members:draft.members}),...team});},
    edit(){if(busy || !isCurrent() || state.status!=='pending' || !state.captain || !Number.isSafeInteger(state.requestRevision))return;draft=copyDraft({...state.captain,teamName:state.declaration?.teamName,track:state.declaration?.track,members:state.declaration?.members??emptyClaimMembers()});editRevision=state.requestRevision;state.editing=true;state.error='';state.needsReview=false;state.reviewRevision=null;publish();},
    update(members,team){
      if(busy || !isCurrent() || !state.editing)return;draft=copyDraft({...draft,members,...team});
      if(state.needsReview){state.error='草稿已保留，请先读取最新已保存名单并逐人核对，再继续保存。';publish();return;}
      if(receipt)return run('update',{requestId:receipt.requestId,secret:receipt.secret,expectedRevision:editRevision,members:draft.members,...team});
    },
    reviewLatest(){if(receipt && state.editing)return run('status',{requestId:receipt.requestId,secret:receipt.secret},true);},
    confirmReview(){
      if(busy || !isCurrent() || !state.editing || state.status!=='pending' || !Number.isSafeInteger(state.reviewRevision) || state.reviewRevision!==state.requestRevision)return;
      editRevision=state.reviewRevision;state.needsReview=false;state.reviewRevision=null;state.error='';publish();
    },
    cancelEdit(){generation++;busy=false;state.editing=false;state.error='';state.needsReview=false;state.reviewRevision=null;publish();return this.status();},
    status(){if(receipt)return run('status',{requestId:receipt.requestId,secret:receipt.secret});},
    redeem(){if(receipt)return run('redeem',{requestId:receipt.requestId,secret:receipt.secret});},
    clear(){generation++;busy=false;receipt=null;storageError=store.clear();draft={name:'',school:'',teamName:'',track:'',members:emptyClaimMembers()};state={status:'new',team:null,code:null,error:'',declaration:null,captain:null,requestRevision:null,editing:false,needsReview:false,reviewRevision:null};publish();},
    dispose(){generation++;busy=false;state.code=null;}
  };
}
function claimReceiptStore(){return makeClaimReceiptStore(()=>sessionStorage,base.pathname);}
let currentClaimController=null;
function hasClaimReceipt(){return !!claimReceiptStore().read().receipt;}
function clearClaimReceipt(){currentClaimController?.clear();return claimReceiptStore().clear();}
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
  const labels={pending:'正在自动完成申请',approved:'队伍申请已自动完成',issued:'提交码已领取',rejected:'申请未通过，请联系组织方',expired:'申请已过期，请重新申请',invalid:'领取凭据无效或已过期，请重新申请',unavailable:'提交码已由组织方变更或停用，请联系组织方恢复'};
  const receipt=state.receipt,draft=state.draft??{name:'',school:'',members:emptyClaimMembers()};
  let body;
  if(!receipt){
    body=`<form id="claim-request-form"><fieldset ${state.busy?'disabled':''}><label class="field">队长姓名<input name="name" required maxlength="100" autocomplete="name" value="${esc(draft.name)}"></label><label class="field">队长学校<input name="school" required maxlength="200" autocomplete="organization" value="${esc(draft.school)}"></label>${claimTeamMarkup(draft)}${claimMembersMarkup(draft.members)}<button class="btn" ${state.busy?'disabled':''}>提交并自动完成申请</button></fieldset></form>`;
  }else{
    const progress=state.status==='pending'?'这是一份较早的申请，系统正在尝试自动完成。请保留本页并重新读取状态；如仍未完成，请联系组织方。':
      ['approved','issued'].includes(state.status)?'系统已自动完成队伍申请，无需等待工作人员审核。请立即领取并保存本队提交码。':
      state.status==='rejected'?'这份申请未完成。请核对队伍信息，联系组织方后重新申请。':
      state.status==='expired'?'领取凭据已经过期。请清除本设备上的旧凭据后重新申请。':
      state.status==='unavailable'?'本队提交码已被变更或停用，请联系组织方重新发放。':'无法使用这份领取凭据，请清除后重新申请。';
    body=`<h2>${esc(labels[state.status] || '队伍申请状态')}</h2><p>${progress}</p><p class="small-note">申请编号：<code>${esc(receipt.requestId)}</code><br>有效期至 ${esc(beijingTime(receipt.expiresAt))}（北京时间）<br>凭据只保存在此标签页，请勿转发或共享设备。</p>${claimDeclarationMarkup(state.declaration)}${claimUpdateMarkup(state,draft)}${state.team?`<p>已完成队伍：<strong>${esc(teamLabel(state.team))}</strong></p>`:''}<div class="actions">${['approved','issued'].includes(state.status)?`<button class="btn claim-redeem-primary" id="claim-redeem" ${state.busy?'disabled':''}>${state.status==='issued'?'再次显示本队提交码':'领取本队提交码'}</button>`:''}${state.status==='pending'?`<button class="btn secondary" id="claim-status" ${state.busy?'disabled':''}>重新读取状态</button>`:''}${state.status==='pending' && !state.editing && state.captain && Number.isSafeInteger(state.requestRevision)?`<button class="btn secondary" id="claim-edit" ${state.busy?'disabled':''}>${state.declaration?'修改队伍与组员':'补充队伍与组员'}</button>`:''}</div>${state.code?`<div class="code-output"><strong>本队提交码</strong><code>${esc(state.code)}</code><p>请队长保存，只与本队成员共享。</p><button class="btn secondary small" type="button" data-copy-value="${esc(state.code)}">复制提交码</button> <a href="#team" id="claim-workbench">前往选手工作台 ↗</a><p class="small-note">进入后使用本队提交码登录；切换原登录身份会保留申请凭据，可返回申请页再次查看提交码。</p></div>`:''}<button class="btn secondary small" id="claim-clear" type="button">清除此设备上的领取凭据</button>`;
  }
  return heading('TEAM / 队伍申请','申请队伍提交码','队长填写队名、赛道，以及全体队员的姓名和学校。完整且没有队名或成员占用冲突的申请会立即自动完成，无需人工审核；如有冲突会当场提示修改。每人只参加一队，每队一个提交码。')+`<section class="surface claim-panel"><p class="form-error" role="alert">${esc(state.error)}</p>${body}</section>`;
}
function claimFormMembers(form){return [...form.querySelectorAll('[data-claim-member-row]')].map(row=>({name:row.querySelector('[name="memberName"]').value,school:row.querySelector('[name="memberSchool"]').value}));}
async function renderClaim(version){
  currentClaimController?.dispose();
  const controller=makeClaimController({store:claimReceiptStore(),request:api,isCurrent:()=>route==='claim' && version===routeVersion && currentClaimController===controller,onChange:state=>{
    root.innerHTML=claimStateMarkup(state);
    const form=document.getElementById('claim-request-form')??document.getElementById('claim-update-form');
    if(form){
      const read=()=>({name:form.elements.name?.value??state.captain.name,school:form.elements.school?.value??state.captain.school,members:claimFormMembers(form),teamName:form.elements.teamName.value,track:form.elements.track.value});
      form.oninput=()=>controller.setDraft(read());
      form.onsubmit=e=>{e.preventDefault();const value=read();if(state.receipt)controller.update(value.members,{teamName:value.teamName,track:value.track});else controller.apply(value.name,value.school,value.members,{teamName:value.teamName,track:value.track});};
      const add=document.getElementById('claim-member-add');if(add)add.onclick=()=>{controller.setDraft(read());controller.addMember();};
      form.querySelectorAll('[data-claim-remove]').forEach(button=>button.onclick=()=>{controller.setDraft(read());controller.removeMember(Number(button.dataset.claimRemove));});
    }
    const workbench=document.getElementById('claim-workbench');if(workbench)workbench.onclick=event=>openClaimWorkbench(event);
    const edit=document.getElementById('claim-edit');if(edit)edit.onclick=()=>controller.edit();
    const reload=document.getElementById('claim-edit-reload');if(reload)reload.onclick=()=>controller.reviewLatest();
    const confirmEdit=document.getElementById('claim-edit-confirm');if(confirmEdit)confirmEdit.onclick=()=>controller.confirmReview();
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
  if(request.status!=='pending')return `<h3>${esc(request.name)} · ${esc(status[request.status] || request.status)}</h3>${claimDeclarationMarkup(request.declaration)}<p>${esc(request.reviewNote)}</p>`;
  const declared=request.declaration;
  const references=request.references??[];
  return `<form id="claim-review-form"><h3>队伍申请审核 · ${esc(request.name)}</h3><p>申请学校：<strong>${esc(request.school)}</strong><br>申请编号：<code>${esc(request.id)}</code> · 修订 ${Number(request.revision)}</p>${claimDeclarationMarkup(declared)}${declared?`${claimTeamSelectionMarkup(data)}${claimTeamMarkup(declared,data.trackOptions??CLAIM_TRACK_OPTIONS)}<p class="small-note">审核上述申报成员；批准后创建新队或绑定所选已有队伍、登记成员并开放本队提交码领取。</p>${references.length?`<details open><summary>相关私有身份参考（仅用于去重）</summary><ul>${references.map(p=>`<li>${esc(p.name)} · ${esc(p.school)} · ${p.teamId?'已归队，可能重复，请区分本人':'未归队，同名同校可复用'}${p.needsReview?' · 原记录待核实（参考）':''}<br>${esc(p.notes)}</li>`).join('')}</ul></details>`:'<p>无相关参考记录；批准后将自动登记新成员。</p>'}<p class="small-note">点击批准即确认已审核申报信息及所选已有登记（如有），并同意建队或归队。此为线上申请审核。</p><label class="toggle-line"><input name="resolvedIdentity" type="checkbox">如有其他学校的同名已归队人员，已确认属于不同本人，并在下方说明</label>`:'<p class="info-banner warning">请申请人打开原领取页面，补充全体组员后再刷新审核。无需预先导入名单。</p>'}<label class="field">审核或拒绝说明（选填；同名冲突需说明）<textarea name="note" maxlength="2000">${esc(request.reviewNote)}</textarea></label><div class="actions">${declared?'<button class="btn" type="submit">确认建队或归队并批准领取</button>':''}<button class="btn danger" type="button" id="claim-reject">拒绝申请</button></div><p class="form-error" role="alert"></p></form>`;
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
  root.insertAdjacentHTML('beforeend',`<section class="surface" id="claims-admin"><h2>队伍申请记录</h2><p>新申请会自动完成建队。此处保留历史记录、异常处理和停用身份重发能力；每名选手仍只能归属一队。</p><button class="btn secondary small" id="claims-admin-load">读取 / 刷新申请记录</button><p id="claims-admin-message" role="status"></p><div id="claims-admin-content"></div></section>`);
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
    content.insertAdjacentHTML('beforeend',`<details><summary>可信自动审核名单（独立来源）</summary><p>状态：${auto.enabled?'已启用':'已停用'} · 修订 ${Number(auto.revision)} · ${Number(auto.count)} 条 · 可匹配 ${Number(auto.eligibleCount)} 条 · 歧义 ${Number(auto.ambiguousCount)} 条</p><p>来源：${esc(auto.sourceName)}<br>SHA-256：<code>${esc(auto.sourceSha256??'')}</code></p><p class="small-note">仅整队姓名与学校唯一匹配且无冲突时自动批准；普通参考记录及公开登记不会获得此资格。替换名单后须重新启用。不会开通 API 服务。</p><form id="claim-auto-toggle"><button class="btn secondary small">${auto.enabled?'停用':'启用'}自动审核</button><p class="form-error" role="alert"></p></form><form id="claim-auto-import"><label class="field">完整可信名单 JSON<input type="file" name="file" accept=".json,application/json" required></label><p class="small-note">autoReview：expectedRevision、sourceName、sourceSha256、entries（id、name、school、eligible、notes），最多 500 条。提交时使用当前显示的修订号，完整替换不会分批生效。</p><button class="btn secondary small">替换可信名单并停用</button><p class="form-error" role="alert"></p></form></details>`);
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
