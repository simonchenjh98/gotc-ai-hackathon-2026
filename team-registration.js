'use strict';

const RegistrationUI=(()=>{
  const escape=value=>String(value ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const matches=(team,query,recruiting)=>(!recruiting || team.recruiting) && [team.name,team.captain,team.school,...team.members].join(' ').toLocaleLowerCase().includes(query.trim().toLocaleLowerCase());
  function card(team,profileUrl='https://8.141.114.201/gotc/platform.html#team-profile'){
    return `<article class="registration-card"><header><h4>${escape(team.name)}</h4><span class="registration-number">#${String(team.number).padStart(2,'0')}</span></header><p class="registration-school">${escape(team.school || '队长学校待补充')}</p><div class="registration-names">${team.members.map((name,index)=>`<span class="${index===0?'captain':''}">${escape(name)}${index===0?' · 队长':''}</span>`).join('')}</div><div class="registration-badges"><span class="registration-badge">已登记 ${team.members.length} 人${team.targetSize?` / 目标 ${team.targetSize} 人`:''}</span>${team.recruiting?'<span class="registration-badge">可继续组队</span>':''}${team.flags.map(flag=>`<span class="registration-badge">${escape(flag)}</span>`).join('')}</div>${team.project?`<p class="registration-project"><strong>${escape(team.project)}</strong>${team.summary?`<br>${escape(team.summary)}`:''}</p>`:team.summary?`<p class="registration-project">${escape(team.summary)}</p>`:''}<a class="reg-button secondary" href="${escape(profileUrl)}">用参赛码查看本队资料</a></article>`;
  }
  async function install(host){
    const endpoint=new URL(host.dataset.api,location.href).href,profileUrl=new URL('../platform.html#team-profile',endpoint).href,claimUrl=new URL('../platform.html#claim',endpoint).href;
    clearLegacyRegistrationStorage(endpoint);
    host.innerHTML=`<div class="registration-layout"><section class="registration-panel"><h3>本队资料与成员</h3><p>已有本队参赛码（原提交码）？使用同一个账号查看本队资料、成员、提交作品和查分。</p><a class="reg-button" href="${escape(profileUrl)}">进入本队资料与成员 ↗</a><p class="registration-note">还没有参赛码，请先提交队伍申请；申请未完成时，可继续使用原申请回执查询和补充资料。</p><a class="reg-button secondary" href="${escape(claimUrl)}">申请队伍 / 领取参赛码 ↗</a><p class="registration-note">已审核成员变更请联系组织方。下方名单可直接查看；队员学校以实际记录为准，不根据队长学校推断其他成员学校。</p></section><section class="registration-panel" aria-label="已登记队伍"><div class="registration-directory-top"><div><h3>公开队伍名单</h3><span class="registration-count" data-reg-count>正在读取…</span></div><button class="reg-button secondary" type="button" data-reg-refresh>刷新名单</button></div><label class="registration-field">搜索队名、姓名或学校<input type="search" data-reg-search placeholder="输入队名、姓名或学校"></label><label class="registration-filter"><input type="checkbox" data-reg-recruiting>只看还可组队的队伍</label><p class="registration-error" data-reg-list-error role="status"></p><div class="registration-list" data-reg-list aria-live="polite"></div></section></div>`;
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
    await refresh();
  }
  return {escape,matches,card,install};
})();
function clearLegacyRegistrationStorage(endpoint){
  try{sessionStorage.removeItem('gotc-registration-code:v1:'+endpoint);}catch{/* Only the obsolete registration credential entry is removed. */}
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
  clearLegacyRegistrationStorage(new URL('api/registration',base).href);
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
