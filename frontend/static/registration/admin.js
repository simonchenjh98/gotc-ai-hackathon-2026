'use strict';

let currentRegistrationAdmin=null;
function installRegistrationAdmin(version){
  currentRegistrationAdmin?.dispose();
  const owner=session,loginId=owner.loginId,marker=browserLoginOwner();
  root.insertAdjacentHTML('beforeend','<section class="surface" id="registration-admin"><h2>队伍登记资料</h2><p>查看联系方式及待核对项目；队伍成员资料通过本队参赛码在选手工作台维护，已审核成员变更请联系组织方。</p><button class="btn secondary small" data-registration-load>读取 / 刷新登记名单</button><p data-registration-message role="status"></p><div data-registration-content></div></section>');
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
  button.onclick=buttonAction(load);
}
