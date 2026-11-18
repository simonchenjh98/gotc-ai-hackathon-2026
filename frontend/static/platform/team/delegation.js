'use strict';

function adminTeamWorkbenchNotice(data){
  const operator=session.operator;
  if(!operator || operator.teamId!==data.team.id)return '';
  return `<section class="info-banner warning" role="status"><strong>管理员代管 · ${esc(data.team.name)}</strong><p>你正在操作这支队伍的工作台，修改会写入该队记录，并标记为组织方代操作。可使用页头的“切换队伍”返回选择。</p></section>`;
}
async function adoptTeamWorkbenchSession(result,owner,expected,isCurrent,destination,marker){
  if(!isCurrent() || session!==owner || browserLoginOwner()!==marker)return false;
  const next=await api('session',undefined,{recordVerifiedLogin:false});
  if(!isCurrent() || session!==owner || browserLoginOwner()!==marker)return false;
  if(!result.loginId || next.loginId!==result.loginId || next.role!==expected.role ||
     (expected.teamId ? next.operator?.teamId!==expected.teamId : !!next.operator)){
    throw new Error('浏览器登录身份已变化，请刷新后重新选择队伍。');
  }
  currentTeamRecovery?.flush();currentTeamRecovery?.lock('已切换工作台，请重新核对登录队伍。');currentTeamKeys?.dispose();
  submissionDirty=false;reviewDirty=false;scoreDirty=false;dirtyAdminForms.clear();
  acceptVerifiedLogin(next,marker);
  if(location.hash.slice(1)!==destination)location.hash=destination;
  else await renderRoute();
  return true;
}
async function renderAdminTeamWorkbench(version){
  const owner=session,page=route;
  const valid=()=>session===owner && owner.role==='admin' && version===routeVersion && route===page && ownsBrowserLogin(owner);
  const data=await api('admin/team-workbench',undefined,{loginId:owner.loginId});
  if(!valid())return;
  root.innerHTML=heading('ADMIN / 队伍代管','选择要进入的队伍','管理员身份已验证。选择一支队伍进入选手工作台，操作会记入组织方记录。')+
    (data.teams.length?`<form class="surface" id="admin-team-workbench"><label class="field">选择队伍<select name="teamId" required><option value="">请选择队伍</option>${data.teams.map(team=>`<option value="${esc(team.id)}">${esc(teamLabel(team))}</option>`).join('')}</select></label><button class="btn" type="submit">进入这支队伍的工作台 ↗</button><p class="form-error" role="alert"></p></form>`:empty('还没有队伍','请先在组织方后台创建队伍，或审核队伍申请。'))+
    '<p><a class="text-link" href="#admin">返回组织方后台 →</a></p>';
  const form=document.getElementById('admin-team-workbench');if(!form)return;
  let busy=false;
  const live=()=>valid() && form.isConnected && document.getElementById('admin-team-workbench')===form;
  form.onsubmit=async event=>{
    event.preventDefault();if(busy || !live() || !confirmLeave())return;
    const teamId=form.elements.teamId.value;
    if(!data.teams.some(team=>team.id===teamId)){formError(form,'请选择需要进入的队伍。');return;}
    const button=form.querySelector('button');busy=true;button.disabled=true;formError(form,'');
    try{
      const marker=browserLoginOwner();
      const result=await api('admin/team-workbench',{teamId},{loginId:owner.loginId});
      await adoptTeamWorkbenchSession(result,owner,{role:'team',teamId},live,page,marker);
    }catch(error){if(form.isConnected && version===routeVersion && (session===owner || !session.role))formError(form,error.message);}
    finally{busy=false;if(form.isConnected)button.disabled=false;}
  };
}
async function returnAdminTeamWorkbench(event){
  const owner=session,version=routeVersion,page=route,button=event?.currentTarget;
  const live=()=>session===owner && !!owner.operator && version===routeVersion && route===page && ownsBrowserLogin(owner);
  if(!live() || !confirmLeave())return;
  return withBusy(button,async()=>{
    const marker=browserLoginOwner();
    const result=await api('team/workbench/return',{}, {loginId:owner.loginId});
    await adoptTeamWorkbenchSession(result,owner,{role:'admin'},live,page==='team-score'?'team-score':'team',marker);
  });
}
