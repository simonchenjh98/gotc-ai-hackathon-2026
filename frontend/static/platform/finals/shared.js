'use strict';

// Finals are a separate contest: neither scores nor nominations use the legacy tables.
const finalsGuidance={
  creativity:'想法有新意，能清楚说明与常见方案的不同；创意服务于真实问题。',
  social:'理解使用者处境，关注弱势群体、可及性与潜在影响，并有调研或反馈支持。',
  technical:'原型能稳定演示，技术选择合理；能解释关键实现、测试结果和已知限制。',
  application:'目标用户与使用场景明确，方案有实际价值，并考虑部署、成本与持续使用。',
  crossdisciplinary:'将 AI 与其他学科或领域有效结合，能说明 AI 的作用及结合带来的价值。',
  opensource:'代码与说明便于他人理解和复用，许可证、引用来源与协作记录清楚。'
};
let activeFinalsView=null;
const finalsAccessStorageKey='gotc-finals-access-v1';
let finalsAccessGrant=null;
try{const saved=JSON.parse(sessionStorage.getItem(finalsAccessStorageKey));if(saved && typeof saved.accessToken==='string' && saved.accessToken.length>=32 && Number.isFinite(saved.expiresAt) && saved.expiresAt>Date.now())finalsAccessGrant=saved;}catch{}
function finalsHasAccess(){return !!finalsAccessGrant && finalsAccessGrant.expiresAt>Date.now();}
function finalsSetAccess(grant){
  finalsAccessGrant=grant;
  try{if(grant)sessionStorage.setItem(finalsAccessStorageKey,JSON.stringify(grant));else sessionStorage.removeItem(finalsAccessStorageKey);}catch{}
}

function makeFinalsPoller({read,apply,status,alive,isHidden,subscribe,schedule=setTimeout,cancel=clearTimeout}){
  let timer=null,deadline=null,controller=null,busy=false,stopped=false,pending=false;
  const clear=()=>{if(timer!==null)cancel(timer);timer=null;};
  function arm(delay){clear();if(!stopped && alive() && !isHidden())timer=schedule(check,delay);}
  async function check(){
    timer=null;if(stopped || busy || !alive() || isHidden())return;
    busy=true;controller=new AbortController();const request=controller;
    deadline=schedule(()=>request.abort(),8000);
    try{const data=await read(request.signal);if(!stopped && alive() && !isHidden()){apply(data);status('updated');}}
    catch(error){if(!stopped && alive()){if(error.status===401 || error.status===403){stop();status('auth');}else status('error');}}
    finally{if(deadline!==null)cancel(deadline);deadline=null;controller=null;busy=false;if(!stopped){const delay=pending?0:2000;pending=false;arm(delay);}}
  }
  function kick(){if(stopped)return;if(isHidden()){clear();controller?.abort();return;}if(busy){pending=true;return;}arm(0);}
  function stop(){stopped=true;clear();if(deadline!==null)cancel(deadline);deadline=null;controller?.abort();unsubscribe?.();}
  const unsubscribe=subscribe(kick);
  return {start(){arm(2000);},refresh(){clear();return check();},kick,stop};
}

function disposeFinalsView(){
  const view=activeFinalsView;if(!view)return;
  view.disposed=true;view.poller?.stop();for(const controller of view.controllers)controller.abort();
  view.cleanups.forEach(cleanup=>cleanup());
  for(const form of view.forms)dirtyAdminForms.delete(form);
  activeFinalsView=null;
}

function createFinalsView(version,role,judge=null){
  disposeFinalsView();const independent=['seat','entry','access'].includes(role),owner=role && !independent?session:null;
  const view={version,owner,route,role,grant:['seat','entry'].includes(role)?finalsAccessGrant:null,judgeId:judge?.id || null,judgeName:judge?.name || '',disposed:false,container:null,controllers:new Set(),cleanups:[],forms:new Set(),locked:false};
  activeFinalsView=view;
  view.current=()=>!view.disposed && activeFinalsView===view && routeVersion===version && route===view.route && (!view.container || view.container.isConnected);
  view.lock=()=>{
    if(!view.current())return;view.locked=true;view.poller?.stop();
    view.container?.querySelectorAll('button').forEach(button=>button.disabled=true);
    const notice=view.container?.querySelector('[data-finals-auth]');if(notice){notice.hidden=false;notice.textContent=independent?'决赛验证已失效或当前席位不可用。请先复制未保存的输入，再刷新并重新输入邀请码、选择评委。':'登录身份已失效或切换。请先复制未保存的输入，再刷新并核对登录身份。';}
  };
  view.valid=()=>{
    if(!view.current())return false;
    if(['seat','entry'].includes(role) && (!finalsHasAccess() || view.grant!==finalsAccessGrant)){view.lock();return false;}
    if(role && !independent && (session!==owner || owner.role!==role || !owner.loginId || !ownsBrowserLogin(owner))){view.lock();return false;}
    return !view.locked;
  };
  const recheck=()=>view.valid();window.addEventListener('storage',recheck);window.addEventListener('focus',recheck);document.addEventListener('visibilitychange',recheck);
  view.cleanups.push(()=>{window.removeEventListener('storage',recheck);window.removeEventListener('focus',recheck);document.removeEventListener('visibilitychange',recheck);});
  return view;
}

async function finalsRequest(view,path,payload,signal){
  if(!view.valid())throw new Error('当前页面已失效，请刷新核对。');
  const controller=signal?null:new AbortController();if(controller)view.controllers.add(controller);
  const timeout=controller?setTimeout(()=>controller.abort(),8000):null;
  try{
    if(['seat','entry','access'].includes(view.role)){
      const allowed=view.role==='seat'?['finals/judge','finals/score','finals/nominations']:view.role==='entry'?['finals/entry','finals/login-status']:['finals/access','finals/login-status'];
      if(!allowed.includes(path) || (view.role==='seat' && !view.judgeId) || (view.role==='entry' && payload!==undefined) || (path==='finals/login-status' && payload!==undefined))throw new Error('请先验证邀请码并选择当前评委席位。');
      // Finals authorization and attribution are independent of ordinary account cookies.
      const response=await fetch(new URL(`api/${path}`,base),{method:payload===undefined?'GET':'POST',credentials:'omit',cache:'no-store',headers:{...(view.grant?{'X-GOTC-Finals-Access':view.grant.accessToken}:{}),...(view.role==='seat'?{'X-GOTC-Finals-Judge':view.judgeId}:{}),...(payload===undefined?{}:{'Content-Type':'application/json'})},body:payload===undefined?undefined:JSON.stringify(payload),signal:signal || controller.signal});
      let body;try{body=await response.json();}catch{throw new Error('服务暂时不可用，请稍后重试。');}
      if(!response.ok)throw platformRequestError(response,body,'操作未成功，请核对当前评委席位。');
      return body;
    }
    return await api(path,payload,{loginId:view.owner?.loginId,signal:signal || controller.signal});
  }
  catch(error){
    if(view.current() && view.role!=='access' && (error.status===401 || error.status===403) && !(view.role==='entry' && error.code==='judge_login_not_open')){
      if(error.code==='finals_access_required' && view.grant===finalsAccessGrant)finalsSetAccess(null);
      view.lock();
    }
    throw error;
  }
  finally{if(timeout!==null)clearTimeout(timeout);if(controller)view.controllers.delete(controller);}
}

function finalsShell(view,kicker,title,description,content){
  root.innerHTML=heading(kicker,title,description)+`<div class="finals-root"><p class="info-banner warning" data-finals-auth role="alert" hidden></p>${content}</div>`;
  view.container=root.querySelector('.finals-root');
}
function finalsName(team){return `${String(team.number).padStart(2,'0')} · ${team.name}`;}
function finalsScore(value){return value===null || value===undefined?'—':Number(value).toFixed(2);}
function finalsStatus(record){return !record?'尚未保存':record.status==='submitted'?'已正式提交':record.status==='recused'?'已回避':'已保存草稿';}
function finalsMessage(form,text){form.querySelector('.form-error').textContent=text;}
function finalsTrack(view,form){
  view.forms.add(form);form.finalsEdit=0;
  form.addEventListener('input',()=>{form.finalsEdit++;dirtyAdminForms.add(form);});
  form.addEventListener('change',()=>{form.finalsEdit++;dirtyAdminForms.add(form);});
}
function finalsDiscard(view,form){dirtyAdminForms.delete(form);view.forms.delete(form);}
function scoringTeamChoicesMarkup(teams,selected){
  const chosen=new Set(selected),known=new Set(teams.map(team=>team.id));
  const rows=[...teams,...selected.filter(id=>!known.has(id)).map(id=>({id,number:'—',name:'已不在队伍信息中',project:id,eligible:false}))];
  return rows.map(team=>`<label class="finals-check"><input type="checkbox" name="teamIds" value="${esc(team.id)}" ${chosen.has(team.id)?'checked':''} ${team.eligible===false && !chosen.has(team.id)?'disabled':''}><span>${esc(finalsName(team))}${team.eligible===false?(chosen.has(team.id)?'（无参赛资格，请取消勾选）':'（无参赛资格）'):''}<small>${esc(team.project || '作品名称待完善')}</small></span></label>`).join('') || '<p>队伍信息中暂无队伍。</p>';
}
function scoringCheckedTeams(form){return [...form.querySelectorAll('input[name=teamIds]:checked')].map(input=>input.value);}
function scoringPatchTeamChoices(host,teams,selected){
  const markup=scoringTeamChoicesMarkup(teams,selected);if(host.scoringMarkup===markup)return;
  const focused=host.contains?.(document.activeElement)?document.activeElement?.value:null;
  host.innerHTML=markup;host.scoringMarkup=markup;
  if(focused)for(const input of host.querySelectorAll('input[name=teamIds]'))if(input.value===focused)input.focus({preventScroll:true});
}
function scoringCanEditTeam(view,teamId,roadshow=false){return !!(roadshow?view.config.canEdit:view.config.open) && (view.eligibleTeamIds?view.eligibleTeamIds.has(teamId):view.data.teams.some(team=>team.id===teamId && team.eligible!==false));}
function syncScoringJudge(view,latest,roadshow=false){
  if(!view.valid())return false;
  if(latest.config.runId!==view.config.runId){view.lock();const notice=view.container.querySelector('[data-finals-auth]');notice.hidden=false;notice.textContent='评审轮次已变化，当前输入已保留。请复制输入后刷新页面。';return false;}
  if(Number(latest.config.revision)<Number(view.config.revision))return false;
  view.config={...view.config,...latest.config};view.eligibleTeamIds=new Set(latest.teams.filter(team=>team.eligible!==false).map(team=>team.id));
  const currentAllowed=scoringCanEditTeam(view,view.selected,roadshow);
  if(view.scoreForm){const fieldset=view.scoreForm.querySelector('fieldset');if(fieldset)fieldset.disabled=!currentAllowed;view.scoreForm.finalsRefreshButtons?.();}
  if(view.nominationForm){const fieldset=view.nominationForm.querySelector('fieldset');if(fieldset)fieldset.disabled=!view.config.open;view.nominationForm.finalsRefreshButtons?.();}
  if(!roadshow){const windowNode=view.container.querySelector('[data-finals-window-state]');if(windowNode)windowNode.textContent=view.config.open?'决赛评分与提名开放中。':'决赛评分与提名已关闭，当前只读。';}
  const scores=new Map(view.scores),oldNomination=view.nominationRecord;
  let scoreChanged=false;for(const record of latest.myScores || [])if(record.revision>(scores.get(record.teamId)?.revision || 0)){scores.set(record.teamId,record);scoreChanged=true;}
  const nomination=latest.myNominations && (latest.myNominations.revision || 0)>(oldNomination?.revision || 0)?latest.myNominations:oldNomination;
  const changed=JSON.stringify(latest.teams)!==JSON.stringify(view.data.teams) || scoreChanged || nomination!==oldNomination;
  const notice=view.container.querySelector(roadshow?'[data-roadshow-changed]':'[data-finals-roster-notice]');
  const pending=[...view.forms].some(form=>form.isConnected && (form.finalsBusy || dirtyAdminForms.has(form)));
  if(changed && pending){if(notice){notice.hidden=false;notice.textContent='已确认名单、队伍信息或评分已更新。未保存输入已保留，请保存或复制后重新读取；已失去资格的队伍不能继续提交评分。';}return true;}
  if(changed){
    view.data={...latest};view.scores=scores;view.nominationRecord=nomination;
    if(!latest.teams.some(team=>team.id===view.selected))view.selected=latest.teams.find(team=>team.eligible!==false)?.id || '';
    const select=view.container.querySelector(roadshow?'[data-roadshow-team]':'[data-finals-team]');
    select.innerHTML=latest.teams.map(team=>`<option value="${esc(team.id)}" ${team.eligible===false?'disabled':''}>${esc(finalsName(team))} · ${esc(finalsStatus(scores.get(team.id)))}${team.eligible===false?'（无参赛资格）':''}</option>`).join('');select.value=view.selected;
    if(view.scoreForm)finalsDiscard(view,view.scoreForm);
    if(roadshow)roadshowRenderScore(view);else{if(view.nominationForm)finalsDiscard(view,view.nominationForm);finalsRenderScore(view);finalsRenderNominations(view,nomination);}
  }
  if(notice){notice.hidden=view.eligibleTeamIds.has(view.selected) || !view.selected;notice.textContent='当前队伍已失去评分资格，已保留记录。请选择其他已确认队伍。';}
  return true;
}
function finalsConfirmReload(view){
  if(!view.valid())return;
  const forms=new Set([...view.forms,...dirtyAdminForms,...(typeof activeRoadshowAccess!=='undefined' && activeRoadshowAccess?.form?[activeRoadshowAccess.form]:[])]);
  if([...forms].some(form=>form.isConnected && (form.finalsBusy || form.roadshowBusy))){toast('正在保存，请等待结果返回后再重新读取。');return;}
  if([...dirtyAdminForms].some(form=>form.isConnected) && !confirm('请先复制要保留的输入。重新载入会替换当前未保存内容，继续？'))return;
  for(const form of view.forms)dirtyAdminForms.delete(form);
  if(view.role==='seat')renderFinalsJudge(view.version,{id:view.judgeId,name:view.judgeName});else renderRoute();
}
function finalsConflict(view,form,message){
  form.finalsBlocked=true;dirtyAdminForms.add(form);
  finalsMessage(form,message+' 当前输入已保留，请先复制，再重新载入最新记录。');
  const box=form.querySelector('[data-finals-recovery]');
  box.innerHTML='<button class="btn secondary small" type="button">复制所需输入后，重新载入</button>';
  box.querySelector('button').onclick=()=>finalsConfirmReload(view);
}

async function finalsSave(view,form,path,payload,onSaved){
  if(!view.valid() || !form.isConnected || form.finalsBusy || form.finalsBlocked)return;
  const epoch=form.finalsEdit;form.finalsBusy=true;finalsMessage(form,'');
  form.querySelectorAll('[data-finals-save]').forEach(button=>button.disabled=true);
  try{
    const result=await finalsRequest(view,path,payload);
    if(!view.valid() || !form.isConnected)return;
    const unchanged=form.finalsEdit===epoch;onSaved(result,unchanged);
    if(unchanged)dirtyAdminForms.delete(form);
    finalsMessage(form,unchanged?'已保存到服务器。':'提交时的内容已保存；保存期间的新输入已保留，尚未提交。');
  }catch(error){
    if(!view.current() || !form.isConnected)return;
    if(error.status===409){finalsConflict(view,form,error.message);}
    else{dirtyAdminForms.add(form);finalsMessage(form,(error.name==='AbortError'?'请求超时，保存结果尚未确认。请先复制输入并重新读取，核对是否保存成功。':error.message)+' 当前输入已保留，没有自动重发。');}
  }finally{
    form.finalsBusy=false;if(view.valid() && form.isConnected)form.finalsRefreshButtons?.();
  }
}

function finalsSummaryMarkup(data,{publicView=false}={}){
  const summary=data.summary;
  if(!summary || (publicView && data.config?.published!==true))return '<section class="surface finals-empty"><h2>决赛结果尚未发布</h2><p>组织方核对评分与提名后将在这里公布。</p></section>';
  const teams=summary.teams || [],byId=new Map(teams.map(team=>[team.id,team]));
  const final=data.config?.published===true && summary.final===true;
  const state=final?'已发布 · 最终结果':'暂定结果 · 尚未发布';
  const stats=metrics([[teams.filter(t=>t.eligible!==false).length,'决赛队伍'],[`${Number(summary.submittedBallots || 0)} / ${Number(summary.expectedJudges || 7)}`,'已正式提交提名的评委'],[teams.filter(t=>t.complete && t.eligible!==false).length,'评分已收齐的队伍'],[final?'已发布':'暂定','当前结果']]);
  const scoreRows=[...teams].sort((a,b)=>(a.eligible===false)-(b.eligible===false)||(a.rank??9999)-(b.rank??9999)||a.number-b.number).map(team=>`<tr><td>${team.eligible===false?'—':esc(team.rank??'待定')}</td><td><strong>${esc(finalsName(team))}</strong><small>${esc(team.project || '作品名称待完善')}${team.eligible===false?' · 已不具备参赛资格':''}</small></td><td class="board-score">${team.eligible===false?'—':finalsScore(team.score)}</td><td>${Number(team.scoreCount || 0)} / ${Number(summary.expectedJudges || 7)}<small>${Number(team.recusedCount || 0)} 人回避 · ${team.complete?'已收齐':'尚未收齐'}</small></td></tr>`).join('');
  const awards=(summary.awards || []).map(award=>{
    const winnerIds=final?(award.confirmedWinners || award.winners || []):(award.suggestedWinners || []);
    const winners=winnerIds.map(id=>byId.get(id)).filter(Boolean);
    const maxVotes=Number(award.maxVotes || 0);
    const votes=(award.votes || []).filter(v=>Number(v.count)>0).sort((a,b)=>b.count-a.count||(byId.get(a.teamId)?.number||0)-(byId.get(b.teamId)?.number||0));
    const winnerVotes=id=>Number(votes.find(v=>v.teamId===id)?.count || 0);
    const confirmed=Array.isArray(award.confirmedWinners);
    return `<section class="surface finals-award"><h3>${esc(award.name)}</h3><p class="finals-award-state">${final?'获奖队伍':confirmed?'已确认归属 · 尚未发布':'建议获奖队伍'}${winners.length>1?' · 并列':''}</p>${winners.length?`<ul class="finals-winners">${winners.map(team=>`<li>${esc(finalsName(team))}<b>${winnerVotes(team.id)} 票</b></li>`).join('')}</ul>`:`<p class="finals-no-vote">${final?'本奖项无人获奖':maxVotes>0?'候选队伍已获前序奖项':'暂无提名票'}</p>`}<p class="small-note">共 ${Number(award.totalVotes || 0)} 票，最多 ${Number(summary.expectedJudges || 7)} 票。每队最多1奖；已获前序奖项的队伍顺延。</p>${votes.length?`<details data-finals-award="${esc(award.id)}"><summary>展开候选名单与票数 ▾</summary><ul class="finals-votes">${votes.map(v=>`<li><span>${esc(byId.has(v.teamId)?finalsName(byId.get(v.teamId)):'已退出名单的队伍')}<small>${winnerIds.includes(v.teamId)?(final?'获奖队伍':'建议获奖'):(award.rawLeaders || []).includes(v.teamId)?'原始最高票 · 已获前序奖项，顺延':'候选队伍'}</small></span><b>${Number(v.count)} 票</b></li>`).join('')}</ul></details>`:''}</section>`;
  }).join('');
  const confirmAwards=!publicView?`<section class="surface"><h2>确认专项奖归属</h2><p class="small-note">按创意 → 社会关怀 → 技术呈现 → 应用潜力 → AI跨界探索 → 开源贡献的顺序分配。每奖排除已获前序奖项的队伍，再取最高正票；并列共同获奖，每队最多1奖。</p><p class="small-note">可展开上方每个奖项核对队伍编号、队名、票数及顺延结果。确认只保存系统按票数与顺序得出的建议名单。</p><button class="btn" type="button" data-finals-confirm-awards ${data.config?.open || Number(summary.submittedBallots)!==Number(summary.expectedJudges || 7) || summary.awardsConfirmed?'disabled':''}>${summary.awardsConfirmed?'专项奖归属已确认':'确认六项专项奖归属'}</button><p class="form-error" data-finals-awards-message role="status"></p></section>`:'';
  return `<div class="info-banner ${final?'':'warning'}"><strong>${state}</strong><br>总分为有效正式评分的平均值；提名票独立统计，不加进100分。${final?'同票队伍共同获奖，由各队队长上台领奖；每队最多1个专项奖。':'未提交不作0分；请核对评分人数、回避和提名提交进度。'}</div>${stats}<section class="surface"><h2>决赛总分 · 满分100分</h2><div class="table-wrap"><table class="board-table finals-results"><thead><tr><th>排名</th><th>队伍 / 作品</th><th>平均分</th><th>有效评分人数</th></tr></thead><tbody>${scoreRows || '<tr><td colspan="4">尚未配置决赛队伍。</td></tr>'}</tbody></table></div></section><div class="finals-awards">${awards}</div>${confirmAwards}${!publicView && summary.blockers?.length?`<section class="info-banner warning"><strong>发布前还需处理</strong><ul>${summary.blockers.map(text=>`<li>${esc(text)}</li>`).join('')}</ul></section>`:''}`;
}

function finalsPatchSummary(view,data,publicView=false){
  const host=view.container.querySelector('[data-finals-summary]');
  const markup=finalsSummaryMarkup(data,{publicView});host.finalsData=data;
  // The common no-change poll must preserve element identity, pointer targets and focus.
  if(host.finalsMarkup!==markup){
    const openAwards=new Set([...host.querySelectorAll('details[data-finals-award]')].filter(node=>node.open).map(node=>node.dataset.finalsAward));
    const focused=document.activeElement?.matches?.('summary')?document.activeElement.closest('details[data-finals-award]')?.dataset.finalsAward:null;
    host.innerHTML=markup;host.finalsMarkup=markup;
    for(const node of host.querySelectorAll('details[data-finals-award]')){
      node.open=openAwards.has(node.dataset.finalsAward);
      if(node.dataset.finalsAward===focused)node.querySelector('summary')?.focus({preventScroll:true});
    }
  }
  if(publicView)return;
  const button=host.querySelector('[data-finals-confirm-awards]');if(!button)return;
  button.disabled=!!view.awardBusy || !!data.config.open || Number(data.summary?.submittedBallots)!==Number(data.summary?.expectedJudges || 7) || !!data.summary?.awardsConfirmed;
  const message=host.querySelector('[data-finals-awards-message]');message.textContent=view.awardMessage || '';
  button.onclick=async()=>{
    if(!view.valid() || button.disabled || view.awardBusy)return;
    view.awardBusy=true;view.awardMessage='';button.disabled=true;
    const latest=host.finalsData;
    const selections=Object.fromEntries((latest.summary.awards || []).map(award=>[award.id,award.suggestedWinners || []]));
    try{
      const result=await finalsRequest(view,'admin/finals/awards',{expectedRevision:latest.config.revision,selections});
      if(!view.valid())return;
      view.awardMessage='六项专项奖归属已确认。统计即将更新，请重新载入设置后发布最终结果。';
      // This action must not advance the revision of an unrelated, potentially dirty settings form.
      view.poller?.kick();
    }catch(error){if(view.current())view.awardMessage=error.name==='AbortError'?'请求超时，确认结果尚未核实。请等待统计更新后核对，没有自动重发。':error.message+' 请核对最新候选名单后再确认。';}
    finally{view.awardBusy=false;if(view.current()){const latestMessage=view.container.querySelector('[data-finals-awards-message]');if(latestMessage)latestMessage.textContent=view.awardMessage;view.poller?.kick();}}
  };
}

function finalsStartPolling(view,path,apply){
  const status=kind=>{
    if(!view.current())return;const node=view.container.querySelector('[data-finals-sync]');if(!node)return;
    node.textContent=kind==='updated'?'已同步 · 每2秒更新':kind==='auth'?'同步已停止，请重新登录':'同步暂不可用，正在重试；当前内容和输入已保留';
    node.classList.toggle('warning',kind!=='updated');
  };
  view.poller=makeFinalsPoller({read:signal=>finalsRequest(view,path,undefined,signal),apply:data=>{if(view.valid())apply(data);},status,alive:()=>view.valid(),isHidden:()=>document.hidden,subscribe:listener=>{document.addEventListener('visibilitychange',listener);return ()=>document.removeEventListener('visibilitychange',listener);}});
  view.poller.start();
}
