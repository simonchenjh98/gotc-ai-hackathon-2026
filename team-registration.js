'use strict';

const RegistrationUI=(()=>{
  const escape=value=>String(value ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const matches=(team,query,recruiting)=>(!recruiting || team.recruiting) && [team.name,team.captain,team.school,...team.members].join(' ').toLocaleLowerCase().includes(query.trim().toLocaleLowerCase());
  function card(team,profileUrl='https://8.141.114.201/gotc/platform.html#team-profile'){
    return `<article class="registration-card"><header><h4>${escape(team.name)}</h4><span class="registration-number">#${String(team.number).padStart(2,'0')}</span></header><p class="registration-school">${escape(team.school || '队长学校待补充')}</p><div class="registration-names">${team.members.map((name,index)=>`<span class="${index===0?'captain':''}">${escape(name)}${index===0?' · 队长':''}</span>`).join('')}</div><div class="registration-badges"><span class="registration-badge">已登记 ${team.members.length} 人${team.targetSize?` / 目标 ${team.targetSize} 人`:''}</span>${team.recruiting?'<span class="registration-badge">可继续组队</span>':''}${team.flags.map(flag=>`<span class="registration-badge">${escape(flag)}</span>`).join('')}</div>${team.project?`<p class="registration-project"><strong>${escape(team.project)}</strong>${team.summary?`<br>${escape(team.summary)}`:''}</p>`:team.summary?`<p class="registration-project">${escape(team.summary)}</p>`:''}<a class="reg-button secondary" href="${escape(profileUrl)}">用参赛码查看本队资料</a></article>`;
  }
  async function install(host){
    const endpoint=new URL(host.dataset.api,location.href).href,profileUrl=new URL('../platform.html#team-profile',endpoint).href,claimUrl=new URL('../platform.html#claim',endpoint).href;
    host.innerHTML=`<div class="registration-layout"><section class="registration-panel"><h3>本队资料与成员</h3><p>已有本队参赛码（原提交码）？使用同一个账号查看本队资料、成员、提交作品和查分。</p><a class="reg-button" href="${escape(profileUrl)}">进入本队资料与成员 ↗</a><p class="registration-note">还没有参赛码，请先提交队伍申请；申请未完成时，可继续使用原申请回执查询和补充资料。</p><a class="reg-button secondary" href="${escape(claimUrl)}">申请队伍 / 领取参赛码 ↗</a><p class="registration-note">已审核成员变更请联系组织方。下方名单可直接查看；队员学校以实际记录为准，不根据队长学校推断其他成员学校。</p>${legacyRegistrationMarkup()}</section><section class="registration-panel" aria-label="已登记队伍"><div class="registration-directory-top"><div><h3>公开队伍名单</h3><span class="registration-count" data-reg-count>正在读取…</span></div><button class="reg-button secondary" type="button" data-reg-refresh>刷新名单</button></div><label class="registration-field">搜索队名、姓名或学校<input type="search" data-reg-search placeholder="输入队名、姓名或学校"></label><label class="registration-filter"><input type="checkbox" data-reg-recruiting>只看还可组队的队伍</label><p class="registration-error" data-reg-list-error role="status"></p><div class="registration-list" data-reg-list aria-live="polite"></div></section></div>`;
    const q=selector=>host.querySelector(selector);let teams=[],generation=0;
    function showList(){
      const rows=teams.filter(team=>matches(team,q('[data-reg-search]').value,q('[data-reg-recruiting]').checked));
      q('[data-reg-count]').textContent=`共 ${teams.length} 支队伍 · 当前显示 ${rows.length} 支`;
      q('[data-reg-list]').innerHTML=rows.length?rows.map(team=>card(team,profileUrl)).join(''):'<p class="registration-empty">没有找到匹配的队伍。可换个关键词，或前往队伍申请入口。</p>';
    }
    async function refresh(){
      const serial=++generation,button=q('[data-reg-refresh]');button.disabled=true;
      try{
        const response=await fetch(endpoint,{method:'GET',credentials:'omit',cache:'no-store'}),data=await response.json();
        if(!response.ok)throw new Error(data.error || '名单暂时不可用');
        if(serial!==generation || !host.isConnected)return;
        teams=data.teams;showList();q('[data-reg-list-error]').textContent='';
      }catch(error){if(serial===generation && host.isConnected)q('[data-reg-list-error]').textContent='名单暂时未能更新：'+error.message;}
      finally{if(serial===generation && host.isConnected)button.disabled=false;}
    }
    q('[data-reg-search]').oninput=showList;q('[data-reg-recruiting]').onchange=showList;q('[data-reg-refresh]').onclick=refresh;
    const legacy=installLegacyRegistration(host,endpoint);
    await Promise.all([refresh(),legacy.ready]);
  }
  return {escape,matches,card,install};
})();
function legacyRegistrationMarkup(){
  return `<details class="registration-management" data-reg-legacy><summary>使用原队伍管理码修改资料（兼容）</summary><p class="registration-note">原有且未作废的队伍管理码继续兼容，只能读取、修改原登记资料；不能用它登录参赛工作台、提交作品、查分、使用 AI 或进入组织方后台。已有参赛码，请优先使用上方本队资料入口；新队伍请申请参赛码。</p><form data-reg-code-form><label class="registration-field">原队伍管理码<input name="code" type="password" autocomplete="off" spellcheck="false" maxlength="80" required></label><button class="reg-button secondary" type="submit" data-reg-open>读取 / 重新读取资料</button></form><div data-reg-editor hidden><p class="registration-editing" data-reg-editing></p><p class="registration-note">已审核的队长、学校、成员及人数由组织方锁定，变更请联系组织方。已正式提交的项目名称，请用参赛码在作品提交入口按原规则更新。原登记只记录队长学校，不代表其他成员学校。</p><form class="registration-form" data-reg-form><fieldset><div class="registration-fields"><label class="registration-field wide">队伍名称<input name="name" maxlength="80" required></label><label class="registration-field">队长姓名<input name="captain" maxlength="100" required></label><label class="registration-field">目标人数（含队长）<select name="targetSize"><option value="3">3 人</option><option value="4">4 人</option><option value="5">5 人</option></select></label><label class="registration-field wide">队长学校<input name="school" maxlength="200" required></label><label class="registration-field wide">队长手机号（选填）<input name="phone" type="tel" inputmode="tel" maxlength="11" pattern="1[3-9][0-9]{9}"></label></div><fieldset class="registration-members"><legend>组员姓名（队长另列，全队最多 5 人）</legend><div class="registration-fields">${[1,2,3,4].map(i=>`<label class="registration-field">队员 ${i}<input name="member${i}" maxlength="100"></label>`).join('')}</div></fieldset><label class="registration-field">项目名称<input name="project" maxlength="140"></label><label class="registration-field">一句话说明<textarea name="summary" maxlength="600" rows="3"></textarea></label><button class="reg-button" type="submit" data-reg-save>保存原登记资料</button></fieldset></form></div><p class="registration-error" data-reg-error role="alert"></p><p class="registration-note" data-reg-status role="status"></p></details>`;
}
function installLegacyRegistration(host,endpoint){
  host.legacyRegistration?.dispose();
  const q=selector=>host.querySelector(selector),codeForm=q('[data-reg-code-form]'),form=q('[data-reg-form]'),editor=q('[data-reg-editor]'),error=q('[data-reg-error]'),status=q('[data-reg-status]'),openButton=q('[data-reg-open]'),saveButton=q('[data-reg-save]'),storageKey='gotc-registration-code:v1:'+endpoint;
  const state={active:true,generation:0,editRevision:0,code:'',team:null,dirty:false,blocked:true,reading:null,saving:null,
    dispose(){this.active=false;this.generation++;},
    current(capture){return this.active && host.isConnected && host.legacyRegistration===this && this.generation===capture.generation && location.href===capture.href && codeForm.elements.code.value.trim()===capture.code;}
  };
  host.legacyRegistration=state;
  const controls=()=>{openButton.disabled=!!state.reading;saveButton.disabled=!state.team || state.blocked || !!state.reading || !!state.saving || codeForm.elements.code.value.trim()!==state.code;};
  const capture=code=>({code,generation:state.generation,href:location.href,editRevision:state.editRevision});
  function remember(code){try{sessionStorage.setItem(storageKey,code);return true;}catch{return false;}}
  async function request(action,body){
    const response=await fetch(endpoint+'/'+action,{method:'POST',credentials:'omit',cache:'no-store',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}),data=await response.json();
    if(!response.ok){const failure=new Error(data.error?.message || data.error || '资料暂时不可用');failure.status=response.status;throw failure;}
    return data;
  }
  function fill(team){
    state.team=team;state.dirty=false;state.blocked=false;state.editRevision++;
    for(const key of ['name','captain','school','phone','project','summary'])form.elements[key].value=team[key] || '';
    form.elements.targetSize.value=String(Math.max(3,team.targetSize || team.members.length,team.members.length));
    [1,2,3,4].forEach(i=>{form.elements['member'+i].value=team.members.slice(1)[i-1] || '';});
    q('[data-reg-editing]').textContent=`原登记资料 #${team.number} · ${team.name}（版本 ${team.revision}）`;
    editor.hidden=false;
  }
  function clearPrivate(){state.team=null;state.code='';state.blocked=true;state.dirty=false;form.reset();editor.hidden=true;q('[data-reg-editing]').textContent='';}
  codeForm.elements.code.addEventListener('input',()=>{
    state.generation++;state.reading=null;state.saving=null;state.blocked=true;status.textContent='管理码输入已变化，请重新读取并核对队伍资料。';controls();
  });
  form.addEventListener('input',()=>{state.editRevision++;state.dirty=true;status.textContent='原登记资料的修改尚未保存。';});
  async function open(){
    if(!state.active || !host.isConnected || host.legacyRegistration!==state)return;
    const entered=codeForm.elements.code.value.trim();
    if(!/^[A-Za-z0-9_-]{43}$/.test(entered)){error.textContent='请输入原有的完整队伍管理码。';return;}
    if(state.dirty && !confirm('原登记资料有未保存的修改。重新读取会替换当前输入，确认继续？'))return;
    const same=state.code===entered && !!state.team;state.generation++;state.saving=null;state.blocked=true;
    if(!same)clearPrivate();
    const operation=capture(entered);state.reading=operation;error.textContent='';status.textContent='正在读取原登记资料…';controls();
    try{
      const result=await request('open',{code:entered});if(!state.current(operation) || state.reading!==operation)return;
      if(state.editRevision!==operation.editRevision){state.blocked=true;status.textContent='读取期间有新输入，当前输入已保留。请复制要保留的内容，再明确重新读取。';return;}
      state.code=entered;fill(result.team);status.textContent='已读取原登记资料。旧管理码仅用于资料编辑。'+(remember(entered)?'':'本标签页未能记住管理码，请自行保留原码。');
    }catch(failure){if(state.current(operation) && state.reading===operation){if(failure.status===403)clearPrivate();error.textContent=failure.message+(same && failure.status!==403?' 当前输入已保留，请核对后重新读取。':'');status.textContent='本次读取未确认。';}}
    finally{if(state.reading===operation){state.reading=null;if(state.current(operation))controls();}}
  }
  codeForm.onsubmit=event=>{event.preventDefault();return open();};
  form.onsubmit=async event=>{
    event.preventDefault();if(!state.active || !host.isConnected || host.legacyRegistration!==state || !state.team || state.blocked || state.reading || state.saving || codeForm.elements.code.value.trim()!==state.code)return;
    const operation={...capture(state.code),teamId:state.team.id,revision:state.team.revision},team=Object.fromEntries(['name','captain','school','phone','project','summary'].map(key=>[key,form.elements[key].value.trim()]));
    team.targetSize=Number(form.elements.targetSize.value);team.members=[1,2,3,4].map(i=>form.elements['member'+i].value.trim()).filter(Boolean);
    const count=new Set([team.captain,...team.members]).size;if(count>team.targetSize){team.targetSize=count;form.elements.targetSize.value=String(count);}
    state.saving=operation;error.textContent='';status.textContent='正在保存原登记资料…';controls();
    try{
      const result=await request('save',{code:operation.code,expectedRevision:operation.revision,team});if(!state.current(operation) || state.saving!==operation)return;
      if(result.team.id!==operation.teamId){state.blocked=true;throw new Error('返回的队伍身份不一致，请重新读取并核对。');}
      if(state.editRevision===operation.editRevision){fill(result.team);status.textContent='原登记资料已保存。参赛登录及作品、查分、AI 权限保持不变。';}
      else{state.team=result.team;state.dirty=true;status.textContent='已保存发送时的内容；之后的新输入已保留，尚未保存。';}
    }catch(failure){
      if(state.current(operation) && state.saving===operation){
        if(failure.status===403)clearPrivate();else if(failure.status===409 || !failure.status || failure.status>=500)state.blocked=true;
        error.textContent=failure.message+(failure.status===403?'':' 当前输入已保留；请复制需要保留的内容，再明确重新读取并核对。');status.textContent='本次保存未确认。';
      }
    }finally{if(state.saving===operation){state.saving=null;if(state.current(operation))controls();}}
  };
  if(typeof window!=='undefined'){
    const leave=()=>{const wasReading=!!state.reading;state.generation++;state.reading=null;if(state.saving){state.saving=null;state.blocked=true;error.textContent='页面位置已变化，原保存结果尚未核对。当前输入已保留，请重新读取。';}else if(wasReading)status.textContent='本次读取已取消，请重新读取并核对资料。';controls();};
    const unload=event=>{if(state.active && host.isConnected && state.dirty){event.preventDefault();event.returnValue='';}};
    window.addEventListener('hashchange',leave);window.addEventListener('pagehide',leave);window.addEventListener('beforeunload',unload);
    const dispose=state.dispose.bind(state);state.dispose=()=>{dispose();window.removeEventListener('hashchange',leave);window.removeEventListener('pagehide',leave);window.removeEventListener('beforeunload',unload);};
  }
  controls();let stored='';try{stored=sessionStorage.getItem(storageKey) || '';}catch{/* Manual entry remains available. */}
  if(/^[A-Za-z0-9_-]{43}$/.test(stored)){codeForm.elements.code.value=stored;q('[data-reg-legacy]').open=true;state.ready=open();}else state.ready=Promise.resolve();
  return state;
}

if(typeof document!=='undefined')document.querySelectorAll('[data-team-registration]').forEach(host=>RegistrationUI.install(host));

let currentRegistrationAdmin=null;
function installRegistrationAdmin(version){
  currentRegistrationAdmin?.dispose();
  const owner=session,loginId=owner.loginId,marker=browserLoginOwner();
  root.insertAdjacentHTML('beforeend','<section class="surface" id="registration-admin"><h2>队伍登记资料</h2><p>查看联系方式及待核对项目；队伍成员资料通过原参赛码在选手工作台维护，已审核成员变更请联系组织方。</p><button class="btn secondary small" data-registration-load>读取 / 刷新登记名单</button><p data-registration-message role="status"></p><div data-registration-content></div></section>');
  const box=document.getElementById('registration-admin'),content=box.querySelector('[data-registration-content]'),message=box.querySelector('[data-registration-message]'),button=box.querySelector('[data-registration-load]');
  const state={loginId,active:true,sequence:0,
    lock(){
      this.active=false;this.sequence++;
      if(currentRegistrationAdmin!==this || !box.isConnected || document.getElementById('registration-admin')!==box)return;
      content.innerHTML='';message.textContent='当前管理员登录已失效或切换。登记资料已隐藏，请刷新并重新核对登录身份。';button.disabled=true;
    },
    dispose(){this.lock();},
    valid(){
      if(!this.active)return false;
      if(currentRegistrationAdmin!==this || !box.isConnected || document.getElementById('registration-admin')!==box || route!=='admin' || version!==routeVersion){this.dispose();return false;}
      if(session!==owner || owner.role!=='admin' || owner.operator || !loginId || owner.loginId!==loginId || browserLoginOwner()!==marker || browserLogoutOwner()===loginId || !ownsBrowserLogin(owner)){this.lock();return false;}
      return true;
    }
  };
  currentRegistrationAdmin=state;
  async function load(){
    if(!state.valid())return;
    const serial=++state.sequence;button.disabled=true;
    try{
      const data=await api('admin/registration',undefined,{loginId});if(!state.valid() || serial!==state.sequence)return;
      content.innerHTML=`<div class="table-wrap"><table class="board-table"><thead><tr><th>队伍 / 状态</th><th>队长 / 学校</th><th>联系电话</th><th>姓名 / 待核对</th></tr></thead><tbody>${data.teams.map(team=>`<tr><td>${esc(team.number)} · ${esc(team.name)}${team.recycled?'<br>回收站（已停用）':''}</td><td>${esc(team.captain)}<br>${esc(team.school || '学校待补充')}</td><td>${esc(team.phone || '未填写')}${team.phoneNeedsReview?'<br>号码格式待核对':''}</td><td>${esc(team.members.join('、'))}<br>${esc(team.flags.join('；'))}</td></tr>`).join('')}</tbody></table></div>`;
      message.textContent=`共 ${data.teams.length} 支登记队伍。`;
    }catch(error){
      if(error.status===401 || error.status===403){state.lock();return;}
      if(state.valid() && serial===state.sequence)message.textContent=error.message;
    }finally{if(state.valid() && serial===state.sequence)button.disabled=false;}
  }
  button.onclick=load;
}

let currentTeamProfile=null;
function teamProfileRosterMarkup(roster){
  const members=roster?.members || [],source={approved_membership:'已审核成员记录',registration_captain:'队长原登记',registration_member:'原登记成员'};
  if(!members.length)return '<p class="small-note">暂无成员记录；下方填写的资料为未审核草稿。</p>';
  return `<div class="table-wrap"><table class="board-table"><caption>本队成员与学校</caption><thead><tr><th scope="col">姓名 / 角色</th><th scope="col">学校</th><th scope="col">资料来源</th></tr></thead><tbody>${members.map(member=>`<tr><td>${esc(member.name)}${member.role==='captain'?'<br><small>队长</small>':''}</td><td>${esc(member.school || '学校待补充（原登记未提供）')}${member.schoolState==='needs_review'?'<br><small>学校待核对</small>':''}</td><td>${esc(source[member.source] || '已记录资料')}</td></tr>`).join('')}</tbody></table></div>`;
}
async function renderTeamProfile(version){
  const owner=session,loginId=owner.loginId,marker=browserLoginOwner();
  if(owner.role!=='team' || owner.operator){
    root.innerHTML=heading('TEAM / 本队资料与成员','请使用本队参赛码',owner.operator?'管理员代管不能进入本队资料页面；请返回代管工作台或组织方后台查看队伍资料。':'本入口仅供参赛队伍使用。组织方后台保持独立。')+`<section class="surface"><a class="btn secondary" href="${owner.operator?'#team':'#admin'}">${owner.operator?'返回代管工作台':'返回组织方后台'}</a></section>`;
    return;
  }
  currentTeamProfile?.dispose();
  root.innerHTML='<section class="surface" id="team-profile-panel"><h1>本队资料与成员</h1><p role="status">正在读取本队资料…</p></section>';
  const panel=document.getElementById('team-profile-panel');
  const state={owner,loginId,active:true,dirty:false,pending:false,reading:false,blocked:false,sequence:0,editRevision:0,model:null,form:null,
    navigationState(){return {dirty:this.dirty};},
    dispose(){this.active=false;this.model=null;this.dirty=false;},
    lock(){
      this.dispose();if(panel.isConnected && route==='team-profile' && version===routeVersion)panel.innerHTML='<h1>本队资料已隐藏</h1><p role="alert">当前登录已失效或已切换。请刷新页面并核对登录队伍后操作。</p><a class="btn secondary" href="#team">返回选手工作台</a>';
    },
    valid(){
      if(!this.active)return false;
      if(currentTeamProfile!==this || route!=='team-profile' || version!==routeVersion || !panel.isConnected){this.dispose();return false;}
      if(session!==owner || owner.role!=='team' || owner.operator || !loginId || owner.loginId!==loginId || browserLoginOwner()!==marker || browserLogoutOwner()===loginId || !ownsBrowserLogin(owner)){this.lock();return false;}
      return true;
    }
  };
  currentTeamProfile=state;
  const message=text=>{const node=panel.querySelector('[data-profile-message]');if(node)node.textContent=text;};
  const controls=()=>{if(state.form?.isConnected)state.form.querySelector('[type=submit]').disabled=state.pending || state.reading || state.blocked || !!state.model?.incomplete;};
  function render(model){
    state.model=model;state.dirty=false;state.blocked=false;state.editRevision++;
    panel.innerHTML=`<p class="eyebrow">TEAM / 本队资料与成员</p><h1>${esc(teamLabel(model.team))}</h1><p>使用现有参赛码（原提交码），本队资料、作品提交与查分共用同一个登录会话。</p><div class="form-actions"><a class="btn secondary small" href="#team">作品提交与 AI 工具</a><a class="btn secondary small" href="#team-score">本队查分</a><button type="button" class="btn secondary small" id="team-profile-refresh">重新读取本队资料</button></div><section aria-label="本队成员"><h2>本队成员与学校</h2><div id="team-profile-roster">${teamProfileRosterMarkup(model.roster)}</div><p class="small-note">${model.membersEditable?'以下为未审核组队资料；修改不会自动成为已审核成员，其他成员学校以实际记录为准。':'队长、学校、完整成员和确认人数已审核锁定。已审核成员变更请联系组织方。'}</p></section>${model.incomplete?`<p class="form-error" role="alert">${esc(model.incompleteReason)}。当前只读，请联系组织方核对。</p>`:''}<form id="team-profile-form"><fieldset${model.incomplete?' disabled':''}><div class="form-grid"><label class="field">队伍名称<input name="name" maxlength="80" required></label><label class="field">队长姓名<input name="captain" maxlength="100" required></label><label class="field">队长学校<input name="school" maxlength="200" required></label><label class="field">目标人数（含队长）<select name="targetSize"><option value="3">3 人</option><option value="4">4 人</option><option value="5">5 人</option></select></label><label class="field">队长手机号（选填）<input name="phone" type="tel" inputmode="tel" maxlength="11" pattern="1[3-9][0-9]{9}"><small>仅本队与组织方可查看。</small></label></div><fieldset><legend>组员姓名（队长另列，全队最多 5 人）</legend><div class="form-grid">${[1,2,3,4].map(i=>`<label class="field">队员 ${i}<input name="member${i}" maxlength="100"></label>`).join('')}</div></fieldset><label class="field">项目名称<input name="project" maxlength="140"></label>${model.projectEditable?'':'<p class="small-note">已有正式提交的项目名称，请前往<a href="#team">作品提交入口</a>按原窗口和版本规则更新。</p>'}<label class="field">一句话说明<textarea name="summary" maxlength="600" rows="3"></textarea></label><p class="small-note">${model.listedPublicly?'原有公开组队名单中的登记姓名和队长学校仍按原公开范围展示。':'此次保存不会将本队资料新增到公开组队名单。'}手机号仅本队及组织方可查看；成员学校不会根据队长学校推断。</p><button class="btn" type="submit">保存本队资料</button></fieldset><p class="form-error" role="alert"></p></form><p class="small-note" data-profile-message role="status"></p>`;
    const form=state.form=panel.querySelector('#team-profile-form'),values=form.elements;
    for(const key of ['name','captain','school','phone','project','summary'])values[key].value=(key==='project' && !model.projectEditable?model.team.project:model.profile[key]) || '';
    values.targetSize.value=String(model.profile.targetSize);
    [1,2,3,4].forEach(i=>{values['member'+i].value=model.profile.members.slice(1)[i-1] || '';});
    if(!model.membersEditable)for(const key of ['captain','school','targetSize','member1','member2','member3','member4'])values[key].disabled=true;
    values.project.disabled=!model.projectEditable;
    form.addEventListener('input',()=>{if(state.valid()){state.dirty=true;state.editRevision++;message('当前修改尚未保存。');}});
    panel.querySelector('#team-profile-refresh').onclick=()=>{if(form===state.form && form.isConnected)return load(true);};
    form.onsubmit=event=>{event.preventDefault();if(form===state.form && form.isConnected)return save();};controls();
  }
  async function load(explicit=false){
    if(!state.valid() || state.pending)return;
    if(explicit && state.dirty && !confirm('本队资料有未保存的修改。重新读取会替换当前输入，确认继续？'))return;
    const sequence=++state.sequence,editRevision=state.editRevision;state.reading=true;controls();message('正在重新读取本队资料…');
    try{
      const result=await api('team/profile',undefined,{loginId});if(!state.valid() || sequence!==state.sequence)return;
      if(result.loginId!==loginId || (state.model && result.team.id!==state.model.team.id)){state.lock();return;}
      if(state.editRevision!==editRevision){state.blocked=true;message('读取期间有新输入，当前输入已保留。请复制要保留的内容，再明确重新读取并核对。');return;}
      render(result);
    }catch(error){if(error.status===401 || error.status===403){state.lock();return;}if(state.valid()){if(state.form)formError(state.form,error.message+' 当前输入已保留。');else panel.innerHTML='<h1>本队资料读取失败</h1><p role="alert">'+esc(error.message)+'</p><a href="#team">返回选手工作台</a>';}}
    finally{state.reading=false;if(state.valid())controls();}
  }
  async function save(){
    if(!state.valid() || state.pending || state.reading || state.blocked || state.model?.incomplete || !state.form)return;
    const model=state.model,form=state.form,values=form.elements,editRevision=state.editRevision;
    const team=Object.fromEntries(['name','captain','school','phone','project','summary'].map(key=>[key,values[key].value.trim()]));
    team.targetSize=Number(values.targetSize.value);team.members=[1,2,3,4].map(i=>values['member'+i].value.trim()).filter(Boolean);
    if(!model.membersEditable){for(const key of ['captain','school','targetSize','members'])team[key]=model.profile[key];}
    else{const count=new Set([team.captain,...team.members]).size;if(count>team.targetSize){team.targetSize=count;values.targetSize.value=String(count);}}
    if(!model.projectEditable)team.project=model.team.project;
    state.pending=true;controls();formError(form,'');message('正在保存本队资料…');
    try{
      const result=await api('team/profile',{team,expectedRevision:model.revision,expectedTeamRevision:model.teamRevision},{loginId});
      if(!state.valid())return;
      if(result.loginId!==loginId || result.team.id!==model.team.id){state.lock();return;}
      if(state.editRevision===editRevision){render(result);message('本队资料已保存。参赛码及当前登录保持不变。');}
      else{
        state.model=result;state.dirty=true;
        const changedLocks=result.membersEditable!==model.membersEditable || result.projectEditable!==model.projectEditable || result.incomplete;
        state.blocked=!!changedLocks;
        panel.querySelector('#team-profile-roster').innerHTML=teamProfileRosterMarkup(result.roster);
        message(changedLocks?'已保存发送时的内容；成员或项目权限已变化。新输入已保留，请复制后重新读取并核对。':'已保存发送时的内容；之后的新输入已保留，尚未保存。');
      }
    }catch(error){if(error.status===401 || error.status===403){state.lock();return;}if(state.valid()){if(error.status===409)state.blocked=true;formError(form,error.message+' 当前输入已保留。'+(error.status===409?'请复制需要保留的内容，再明确重新读取并核对。':''));message('本次保存未确认。');}}
    finally{state.pending=false;if(state.valid())controls();}
  }
  await load();
}
