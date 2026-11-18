'use strict';

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
