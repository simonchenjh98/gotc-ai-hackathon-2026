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
  disposeFinalsView();const independent=['seat','entry'].includes(role),owner=role && !independent?session:null;
  const view={version,owner,route,role,judgeId:judge?.id || null,judgeName:judge?.name || '',disposed:false,container:null,controllers:new Set(),cleanups:[],forms:new Set(),locked:false};
  activeFinalsView=view;
  view.current=()=>!view.disposed && activeFinalsView===view && routeVersion===version && route===view.route && (!view.container || view.container.isConnected);
  view.lock=()=>{
    if(!view.current())return;view.locked=true;view.poller?.stop();
    view.container?.querySelectorAll('button').forEach(button=>button.disabled=true);
    const notice=view.container?.querySelector('[data-finals-auth]');if(notice){notice.hidden=false;notice.textContent=independent?'当前评委席位已不可用。请先复制未保存的输入，再刷新并重新选择评委。':'登录身份已失效或切换。请先复制未保存的输入，再刷新并核对登录身份。';}
  };
  view.valid=()=>{
    if(!view.current())return false;
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
    if(['seat','entry'].includes(view.role)){
      const allowed=view.role==='seat'?['finals/judge','finals/score','finals/nominations']:['finals/entry','finals/login-status'];
      if(!allowed.includes(path) || (view.role==='seat' && !view.judgeId) || (view.role==='entry' && payload!==undefined))throw new Error('请先选择当前评委席位。');
      // The chosen seat is attribution only. Ordinary login cookies must never change it.
      const response=await fetch(new URL(`api/${path}`,base),{method:payload===undefined?'GET':'POST',credentials:'omit',cache:'no-store',headers:{...(view.role==='seat'?{'X-GOTC-Finals-Judge':view.judgeId}:{}),...(payload===undefined?{}:{'Content-Type':'application/json'})},body:payload===undefined?undefined:JSON.stringify(payload),signal:signal || controller.signal});
      let body;try{body=await response.json();}catch{throw new Error('服务暂时不可用，请稍后重试。');}
      if(!response.ok){const error=new Error(body.error || '操作未成功，请核对当前评委席位。');error.status=response.status;error.code=body.code;throw error;}
      return body;
    }
    return await api(path,payload,{loginId:view.owner?.loginId,signal:signal || controller.signal});
  }
  catch(error){if(view.current() && (error.status===401 || error.status===403) && !(view.role==='entry' && error.code==='judge_login_not_open'))view.lock();throw error;}
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
  const stats=metrics([[teams.filter(t=>t.eligible!==false).length,'决赛队伍'],[`${Number(summary.submittedBallots || 0)} / ${Number(summary.expectedJudges || 8)}`,'已正式提交提名的评委'],[teams.filter(t=>t.complete && t.eligible!==false).length,'评分已收齐的队伍'],[final?'已发布':'暂定','当前结果']]);
  const scoreRows=[...teams].sort((a,b)=>(a.eligible===false)-(b.eligible===false)||(a.rank??9999)-(b.rank??9999)||a.number-b.number).map(team=>`<tr><td>${team.eligible===false?'—':esc(team.rank??'待定')}</td><td><strong>${esc(finalsName(team))}</strong><small>${esc(team.project || '作品名称待完善')}${team.eligible===false?' · 已不具备参赛资格':''}</small></td><td class="board-score">${team.eligible===false?'—':finalsScore(team.score)}</td><td>${Number(team.scoreCount || 0)} / ${Number(summary.expectedJudges || 8)}<small>${Number(team.recusedCount || 0)} 人回避 · ${team.complete?'已收齐':'尚未收齐'}</small></td></tr>`).join('');
  const awards=(summary.awards || []).map(award=>{
    const winnerIds=final?(award.confirmedWinners || award.winners || []):(award.suggestedWinners || []);
    const winners=winnerIds.map(id=>byId.get(id)).filter(Boolean);
    const maxVotes=Number(award.maxVotes || 0);
    const votes=(award.votes || []).filter(v=>Number(v.count)>0).sort((a,b)=>b.count-a.count||(byId.get(a.teamId)?.number||0)-(byId.get(b.teamId)?.number||0));
    const winnerVotes=id=>Number(votes.find(v=>v.teamId===id)?.count || 0);
    const confirmed=Array.isArray(award.confirmedWinners);
    return `<section class="surface finals-award"><h3>${esc(award.name)}</h3><p class="finals-award-state">${final?'获奖队伍':confirmed?'已确认归属 · 尚未发布':'建议获奖队伍'}${winners.length>1?' · 并列':''}</p>${winners.length?`<ul class="finals-winners">${winners.map(team=>`<li>${esc(finalsName(team))}<b>${winnerVotes(team.id)} 票</b></li>`).join('')}</ul>`:`<p class="finals-no-vote">${final?'本奖项无人获奖':maxVotes>0?'候选队伍已获前序奖项':'暂无提名票'}</p>`}<p class="small-note">共 ${Number(award.totalVotes || 0)} 票，最多 ${Number(summary.expectedJudges || 8)} 票。每队最多1奖；已获前序奖项的队伍顺延。</p>${votes.length?`<details data-finals-award="${esc(award.id)}"><summary>展开候选名单与票数 ▾</summary><ul class="finals-votes">${votes.map(v=>`<li><span>${esc(byId.has(v.teamId)?finalsName(byId.get(v.teamId)):'已退出名单的队伍')}<small>${winnerIds.includes(v.teamId)?(final?'获奖队伍':'建议获奖'):(award.rawLeaders || []).includes(v.teamId)?'原始最高票 · 已获前序奖项，顺延':'候选队伍'}</small></span><b>${Number(v.count)} 票</b></li>`).join('')}</ul></details>`:''}</section>`;
  }).join('');
  const confirmAwards=!publicView?`<section class="surface"><h2>确认专项奖归属</h2><p class="small-note">按创意 → 社会关怀 → 技术呈现 → 应用潜力 → AI跨界探索 → 开源贡献的顺序分配。每奖排除已获前序奖项的队伍，再取最高正票；并列共同获奖，每队最多1奖。</p><p class="small-note">可展开上方每个奖项核对队伍编号、队名、票数及顺延结果。确认只保存系统按票数与顺序得出的建议名单。</p><button class="btn" type="button" data-finals-confirm-awards ${data.config?.open || Number(summary.submittedBallots)!==8 || summary.awardsConfirmed?'disabled':''}>${summary.awardsConfirmed?'专项奖归属已确认':'确认六项专项奖归属'}</button><p class="form-error" data-finals-awards-message role="status"></p></section>`:'';
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
  button.disabled=!!view.awardBusy || !!data.config.open || Number(data.summary?.submittedBallots)!==8 || !!data.summary?.awardsConfirmed;
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

async function renderFinalsBoard(version){
  const view=createFinalsView(version,null),data=await finalsRequest(view,'finals/board');if(!view.valid())return;
  finalsShell(view,'FINALS / 决赛结果','决赛成绩与六项专项奖','8位评委 · 六维100分 · 每奖最多8票，最高票并列共同获奖。','<p class="small-note" data-finals-sync role="status">每2秒同步</p><div data-finals-summary></div>');
  const update=latest=>finalsPatchSummary(view,latest,true);update(data);finalsStartPolling(view,'finals/board',update);
}

async function renderFinalsJudge(version,judge=null){
  const view=createFinalsView(version,judge?'seat':'entry',judge);
  if(!judge){
    let judges=[],available=false,choices='';
    finalsShell(view,'FINALS / 决赛评委','决赛评分与奖项提名','选择自己的姓名或评委席位后，即可查看队伍并评分。',`<section class="surface finals-entry"><h2>选择本次评分归属</h2><p>请按主办方分配的姓名或席位选择。每位评委的评分和提名单独保存。</p><p class="info-banner" data-finals-login-status role="status">正在读取登录开放状态…</p><form data-finals-entry><label class="field">评委姓名 / 席位<select name="judgeId" required disabled><option value="">请选择自己的姓名或席位</option></select></label><button class="btn" type="submit" disabled>进入评分与提名</button><p class="form-error" role="status"></p></form><p class="small-note" data-finals-entry-state></p><button class="btn secondary small" type="button" data-finals-login-refresh>重新读取登录状态</button><p class="small-note">每2秒自动核对开放状态。</p><a href="#board-finals">查看公开决赛结果 →</a></section>`);
    const form=view.container.querySelector('[data-finals-entry]'),select=form.elements.judgeId,submit=form.querySelector('[type=submit]');
    const notice=view.container.querySelector('[data-finals-login-status]'),state=view.container.querySelector('[data-finals-entry-state]');
    const buttons=()=>{select.disabled=submit.disabled=!available || !judges.length;};
    view.poller=makeFinalsPoller({
      read:async signal=>{
        try{const schedule=await finalsRequest(view,'finals/login-status',undefined,signal);return {schedule,entry:schedule.loginAvailable===true?await finalsRequest(view,'finals/entry',undefined,signal):null};}
        catch(error){if(error.code==='judge_login_not_open')return {schedule:{loginAvailable:false,message:error.message},entry:null};throw error;}
      },
      apply:({schedule,entry})=>{
        if(!view.valid())return;available=schedule.loginAvailable===true && !!entry;
        notice.textContent=schedule.message || (available?'评委登录已开放。':'评委登录尚未开放，请等待主办方通知。');
        if(entry){
          judges=entry.judges || [];const next=judges.map(item=>`<option value="${esc(item.id)}">${esc(item.name)}</option>`).join('');
          if(next!==choices){const selected=select.value;select.innerHTML='<option value="">请选择自己的姓名或席位</option>'+next;select.value=judges.some(item=>item.id===selected)?selected:'';choices=next;}
          state.textContent=!entry.config.rosterReady?'组织方正在准备决赛名单与8个评委席位。':entry.config.open?'决赛评分与提名已开放。':'当前尚未开放或已关闭，可进入查看已保存记录。';
        }else state.textContent='';
        buttons();
      },
      status:kind=>{if(view.current() && kind!=='updated'){available=false;buttons();notice.textContent='暂时无法读取登录开放状态，正在重试；也可点击重新读取。';}},
      alive:()=>view.valid(),isHidden:()=>document.hidden,subscribe:listener=>{document.addEventListener('visibilitychange',listener);return ()=>document.removeEventListener('visibilitychange',listener);}
    });
    view.container.querySelector('[data-finals-login-refresh]').onclick=()=>view.poller.kick();
    form.onsubmit=async event=>{
      event.preventDefault();if(!view.valid() || !available)return;
      const selected=judges.find(item=>item.id===select.value);
      if(!selected){finalsMessage(form,'请选择自己的姓名或评委席位。');return;}
      await renderFinalsJudge(version,{id:selected.id,name:selected.name});
    };
    await view.poller.refresh();
    return;
  }
  let data;
  try{data=await finalsRequest(view,'finals/judge');}
  catch(error){finalsJudgeUnavailable(view,version,error);return;}
  if(!view.valid())return;
  view.data=data;view.config=data.config;view.scores=new Map((data.myScores || []).map(score=>[score.teamId,score]));
  finalsShell(view,'FINALS / 决赛评委','决赛评分与奖项提名','六个方向合计100分。每个奖项可提名1支队伍，也可以不提名；分数和提名分别提交。',`<div class="info-banner ${data.config.open?'':'warning'}" data-finals-window><span data-finals-window-state>${data.config.open?'决赛评分与提名开放中。':'决赛评分与提名已关闭，当前只读。'}</span> <strong>当前评分归属：${esc(view.judgeName)}</strong>。<button class="btn secondary small" type="button" data-finals-switch-judge>切换评委</button></div><div class="finals-admin-tools"><button class="btn secondary small" type="button" data-finals-refresh-judge>重新读取名单与评分</button><span class="small-note" data-finals-sync role="status">每2秒同步已确认名单</span></div><p class="info-banner warning" data-finals-roster-notice hidden></p><div class="finals-judge-layout"><section class="surface"><h2>01 / 按队伍评分</h2><label class="field">选择决赛队伍<select data-finals-team>${data.teams.map(team=>`<option value="${esc(team.id)}" ${team.eligible===false?'disabled':''}>${esc(finalsName(team))} · ${esc(finalsStatus(view.scores.get(team.id)))}</option>`).join('')}</select></label><div data-finals-score-editor></div></section><section class="surface"><h2>02 / 六项专项奖提名</h2><p class="small-note">每项最多提名1队，也可弃权。同一队可以被你提名多个奖项，但最终每队最多获得1个专项奖。最多提名6项；正式提交后才计票。</p><div data-finals-nominations></div></section></div>`);
  view.container.querySelector('[data-finals-switch-judge]').onclick=()=>{
    if(!view.valid())return;
    if([...view.forms].some(form=>form.finalsBusy)){toast('正在保存，请等待结果返回后再切换评委。');return;}
    if([...view.forms].some(form=>dirtyAdminForms.has(form)) && !confirm('切换评委会放弃当前未保存的评分或提名。请先保存或复制输入，确认切换？'))return;
    renderFinalsJudge(version);
  };
  const select=view.container.querySelector('[data-finals-team]');view.selected=data.teams.find(team=>team.eligible!==false)?.id || '';
  select.value=view.selected;
  select.onchange=()=>{
    const old=view.container.querySelector('[data-finals-score-form]');
    if(old?.finalsBusy){select.value=view.selected;return;}
    if(old && dirtyAdminForms.has(old) && !confirm('切换队伍会放弃本队尚未保存的输入，请先复制或保存。继续？')){select.value=view.selected;return;}
    if(old)finalsDiscard(view,old);view.selected=select.value;finalsRenderScore(view);
  };
  finalsRenderScore(view);finalsRenderNominations(view,data.myNominations);
  view.container.querySelector('[data-finals-refresh-judge]').onclick=()=>finalsConfirmReload(view);
  finalsStartPolling(view,'finals/judge',latest=>syncScoringJudge(view,latest));
}

function finalsJudgeUnavailable(view,version,error){
  if(!view.current())return;
  if(error.code==='judge_login_not_open')return renderFinalsJudge(version);
  finalsShell(view,'FINALS / 决赛评委','暂时无法读取当前席位','',`<section class="surface"><p role="alert">${esc(error.name==='AbortError'?'读取超时，请重试。':error.message)}</p><button class="btn secondary" type="button" data-finals-reselect>重新读取评委名单</button></section>`);
  view.container.querySelector('[data-finals-reselect]').onclick=()=>renderFinalsJudge(version);
}

function finalsRenderScore(view){
  const team=view.data.teams.find(t=>t.id===view.selected),host=view.container.querySelector('[data-finals-score-editor]');
  if(!team){view.scoreForm=null;host.innerHTML='<p>尚未分配可评分的决赛队伍，请联系组织方。</p>';return;}
  let record=view.scores.get(team.id);const dimensions=view.data.dimensions;
  host.innerHTML=`<form data-finals-score-form><h3>${esc(finalsName(team))}</h3><p>${esc(team.project || '作品名称待完善')}</p><p class="small-note" data-finals-saved>${esc(finalsStatus(record))}</p><fieldset class="finals-fieldset" ${!scoringCanEditTeam(view,team.id)?'disabled':''}>${dimensions.map((dimension,i)=>`<label class="finals-dimension"><span><strong>${esc(dimension.name)}</strong><small>${esc(finalsGuidance[dimension.id] || '')}</small></span><span class="finals-number"><input name="score-${esc(dimension.id)}" type="number" inputmode="decimal" min="0" max="${Number(dimension.max)}" step="0.1" required aria-label="${esc(dimension.name)}，满分${Number(dimension.max)}分" value="${record?.scores?.[i]??''}"><small>/ ${Number(dimension.max)}</small></span></label>`).join('')}<p class="score-preview">当前合计 <strong data-finals-total>—<small> / 100</small></strong></p><label class="field">评语（选填）<textarea name="feedback" maxlength="2000" rows="3">${esc(record?.feedback || '')}</textarea></label><div class="form-actions"><button class="btn" type="submit" data-finals-save>正式提交评分</button><button class="btn secondary" type="button" data-finals-draft data-finals-save>保存草稿</button><button class="btn secondary" type="button" data-finals-recuse data-finals-save>回避此队</button></div></fieldset><p class="form-error" role="status"></p><div data-finals-recovery></div></form>`;
  const form=host.querySelector('form');view.scoreForm=form;finalsTrack(view,form);
  const draft=form.querySelector('[data-finals-draft]'),recuse=form.querySelector('[data-finals-recuse]'),submit=form.querySelector('[type=submit]');
  form.finalsRefreshButtons=()=>{const disabled=!scoringCanEditTeam(view,team.id) || form.finalsBlocked || form.finalsBusy;submit.disabled=disabled;recuse.disabled=disabled;draft.disabled=disabled || record?.status==='submitted';};
  function readScores(){return dimensions.map(dimension=>{const input=form.elements['score-'+dimension.id];if(input.value.trim()==='')throw new Error('请填写全部六个维度分数。');const score=Number(input.value);if(!Number.isFinite(score)||score<0||score>dimension.max||Math.abs(score*10-Math.round(score*10))>1e-7)throw new Error(`${dimension.name}须在0至${dimension.max}分之间，最多一位小数。`);return score;});}
  function preview(){try{form.querySelector('[data-finals-total]').innerHTML=`${readScores().reduce((a,b)=>a+b,0).toFixed(1)}<small> / 100</small>`;}catch{form.querySelector('[data-finals-total]').innerHTML='—<small> / 100</small>';}}
  form.addEventListener('input',preview);preview();
  async function save(status){
    if(!view.valid() || !scoringCanEditTeam(view,team.id))return;
    let scores=[];try{if(status!=='recused')scores=readScores();}catch(error){finalsMessage(form,error.message);return;}
    if(status==='recused' && !confirm('确认回避此队？本评委对此队的分数将不计入平均分。'))return;
    const payload={runId:view.config.runId,teamId:team.id,expectedRevision:record?.revision || 0,status,scores,feedback:form.elements.feedback.value.trim()};
    await finalsSave(view,form,'finals/score',payload,(result,unchanged)=>{
      record=result.score;view.scores.set(team.id,record);form.querySelector('[data-finals-saved]').textContent=finalsStatus(record);
      const option=[...view.container.querySelector('[data-finals-team]').options].find(option=>option.value===team.id);if(option)option.textContent=finalsName(team)+' · '+finalsStatus(record);
      if(unchanged && record.status==='recused'){for(const dimension of dimensions)form.elements['score-'+dimension.id].value='';preview();}view.poller?.kick();
    });
  }
  form.onsubmit=event=>{event.preventDefault();save('submitted');};draft.onclick=()=>save('draft');recuse.onclick=()=>save('recused');form.finalsRefreshButtons();
}

function finalsRenderNominations(view,initial){
  let record=initial;view.nominationRecord=initial;const dimensions=view.data.dimensions,teams=view.data.teams,host=view.container.querySelector('[data-finals-nominations]');
  host.innerHTML=`<form data-finals-nomination-form><p class="small-note" data-finals-saved>${esc(finalsStatus(record))}；${record?.status==='submitted'?'已计入提名统计。':'当前选择尚未计票。'}</p><fieldset class="finals-fieldset" ${!view.config.open?'disabled':''}>${dimensions.map(d=>`<label class="field">${esc(d.award || d.name)}<select name="nomination-${esc(d.id)}"><option value="">不提名（弃权）</option>${teams.map(team=>`<option value="${esc(team.id)}" ${record?.nominations?.[d.id]===team.id?'selected':''} ${team.eligible===false?'disabled':''}>${esc(finalsName(team))}${team.eligible===false?'（已失去资格，请改选或弃权）':''}</option>`).join('')}</select></label>`).join('')}<p class="small-note" data-finals-nomination-count></p><div class="form-actions"><button class="btn" type="submit" data-finals-save>正式提交六项提名</button><button class="btn secondary" type="button" data-finals-draft data-finals-save>保存提名草稿</button></div><p class="small-note">全部选择“不提名”后正式提交，也算已交提名表。每个奖项按最高正票数和固定奖项顺序分配，已获奖队伍顺延；同票共同获奖，每队最多1奖。无人提名则不产生获奖队伍。</p></fieldset><p class="form-error" role="status"></p><div data-finals-recovery></div></form>`;
  const form=host.querySelector('form'),draft=form.querySelector('[data-finals-draft]'),submit=form.querySelector('[type=submit]');view.nominationForm=form;finalsTrack(view,form);
  const values=()=>Object.fromEntries(dimensions.map(d=>[d.id,form.elements['nomination-'+d.id].value || null]));
  const updateCount=()=>{form.querySelector('[data-finals-nomination-count]').textContent=`当前选择 ${Object.values(values()).filter(Boolean).length} / 6 项提名；尚需点击保存或正式提交。`;};form.addEventListener('change',updateCount);updateCount();
  form.finalsRefreshButtons=()=>{const disabled=!view.config.open || form.finalsBlocked || form.finalsBusy;submit.disabled=disabled;draft.disabled=disabled || record?.status==='submitted';};
  const save=status=>{if(!view.valid() || !view.config.open)return;const nominations=values();if(view.eligibleTeamIds && Object.values(nominations).some(id=>id && !view.eligibleTeamIds.has(id))){finalsMessage(form,'已选队伍不在当前有效入围名单，请改选或选择不提名。');return;}return finalsSave(view,form,'finals/nominations',{runId:view.config.runId,expectedRevision:record?.revision || 0,status,nominations},(result,unchanged)=>{record=result.nominations;view.nominationRecord=record;view.poller?.kick();form.querySelector('[data-finals-saved]').textContent=finalsStatus(record)+(record.status==='submitted'?'；已计入提名统计。':'；草稿不计票。');if(unchanged)form.querySelector('[data-finals-nomination-count]').textContent=`已保存 ${Object.values(record.nominations).filter(Boolean).length} / 6 项提名，其余为弃权。`;});};
  form.onsubmit=event=>{event.preventDefault();save('submitted');};draft.onclick=()=>save('draft');form.finalsRefreshButtons();
}

async function renderFinalsAdmin(version){
  const view=createFinalsView(version,'admin'),data=await finalsRequest(view,'admin/finals');if(!view.valid())return;
  view.data=data;view.config=data.config;
  const cfg=data.config;
  finalsShell(view,'FINALS / 组织方控制台','决赛控制台','独立管理8位决赛评委、决赛队伍、六维评分和六项奖项提名。',`<div class="finals-admin-tools"><a class="btn secondary small" href="#board-finals">公开决赛看板 ↗</a><button class="btn secondary small" data-finals-reload>重新载入设置</button><span class="small-note" data-finals-sync role="status">每2秒同步统计</span></div><p class="info-banner warning" data-finals-config-notice hidden></p><div class="finals-admin-layout"><section class="surface"><h2>01 / 决赛名单</h2><p class="small-note">候选直接来自队伍信息。主办方勾选入围队伍并保存后，才会进入决赛评分与提名选项；新增登记队伍不会自动入围。首次开放后名单锁定，评委须恰好8位。</p><form data-finals-roster><fieldset class="finals-fieldset" ${cfg.rosterLocked?'disabled':''}><h3>主办方选定的决赛入围队伍</h3><div class="finals-roster" data-finals-team-list>${scoringTeamChoicesMarkup(data.availableTeams || [],cfg.teamIds || [])}</div><h3>决赛评委 · 必须恰好8位</h3><div class="finals-roster" data-finals-judge-list>${finalsJudgeChecks(data.availableJudges || [],cfg.judgeIds || [])}</div><button class="btn" type="submit" data-finals-save>保存决赛入围名单</button></fieldset><button class="btn secondary small" type="button" data-finals-refresh-teams>刷新队伍信息（保留勾选）</button><p class="form-error" role="status"></p><div data-finals-recovery></div></form><form data-finals-create-judges><p class="small-note">尚未配置决赛评委时，可建立8个评委席位。评委打开决赛入口，选择自己的姓名或席位即可评分。</p><button class="btn secondary" type="submit" data-finals-save ${cfg.rosterLocked || cfg.judgeIds?.length?'disabled':''}>建立8个决赛评委席位</button><p class="form-error" role="status"></p><div data-finals-recovery></div></form></section><section class="surface"><h2>02 / 开放与发布</h2><form data-finals-settings><label class="toggle-line"><input type="checkbox" name="open" ${cfg.open?'checked':''}>开放决赛评分与提名</label><label class="toggle-line"><input type="checkbox" name="published" ${cfg.published?'checked':''}>发布最终成绩和获奖队伍</label><p class="small-note">发布前必须关闭评分、收齐8位评委的提名表，并完成所有队伍的评分或回避。重新开放会取消发布。后台未发布时始终显示“暂定结果”。</p><button class="btn" type="submit" data-finals-save>保存开放与发布设置</button><p class="form-error" role="status"></p><div data-finals-recovery></div></form><h3>六维评分参考</h3><dl class="finals-rubrics">${data.dimensions.map(d=>`<div><dt>${esc(d.name)} · ${Number(d.max)}分</dt><dd>${esc(finalsGuidance[d.id] || '')}</dd></div>`).join('')}</dl><p class="small-note">提名票不影响100分总分；每位评委每奖最多1票，每奖最多8票，六奖合计最多48票。按固定奖项顺序排除已获奖队伍后，最高正票同票共同获奖；每队最多1奖，无有效候选则无人获奖。</p></section></div><div data-finals-summary></div>`);
  view.container.querySelector('[data-finals-reload]').onclick=()=>finalsConfirmReload(view);
  finalsInstallAdmin(view);
  const update=latest=>{
    if(Number(latest.config.revision)<Number(view.config.revision))return;
    view.latest=latest;view.updateRosterCandidates?.(latest);finalsPatchSummary(view,latest);
    const notice=view.container.querySelector('[data-finals-config-notice]');
    notice.hidden=latest.config.revision===view.config.revision;
    if(!notice.hidden)notice.textContent='决赛设置已在其他页面变化。统计已同步；当前表单输入已保留。请先复制需要保留的内容，再重新载入设置。';
  };
  update(data);finalsStartPolling(view,'admin/finals',update);
}

function finalsJudgeChecks(judges,selected){return judges.map(judge=>`<label class="finals-check"><input type="checkbox" name="judgeIds" value="${esc(judge.id)}" ${selected.includes(judge.id)?'checked':''} ${judge.active===false?'disabled':''}><span>${esc(judge.name)}${judge.active===false?'（已停用）':''}</span></label>`).join('') || '<p>暂无评委席位，可在下方建立8个决赛评委席位。</p>';}

function finalsInstallAdmin(view){
  const roster=view.container.querySelector('[data-finals-roster]'),settings=view.container.querySelector('[data-finals-settings]'),create=view.container.querySelector('[data-finals-create-judges]');
  for(const form of [roster,settings,create]){finalsTrack(view,form);form.finalsRevision=view.config.revision;}
  function buttons(){
    const locked=view.latestRosterLocked ?? view.config.rosterLocked;
    roster.querySelector('fieldset').disabled=!!locked || !!view.judgesGenerating;
    for(const form of [roster,settings,create]){const button=form.querySelector('[data-finals-save]');button.disabled=form.finalsBusy || form.finalsBlocked || (form!==settings && locked) || (form===roster && (view.judgesGenerating || view.candidateLoading)) || (form===create && !!(view.latestJudgeIds ?? view.config.judgeIds)?.length);}
  }
  for(const form of [roster,settings,create])form.finalsRefreshButtons=buttons;
  function saved(form,result){
    const cfg=result.config;form.finalsRevision=cfg.revision;view.config=cfg;view.latestRosterLocked=cfg.rosterLocked;view.latestJudgeIds=cfg.judgeIds;
    // Clean sibling controls can adopt this successful write's revision; dirty forms keep theirs.
    for(const other of [roster,settings,create])if(other!==form && !dirtyAdminForms.has(other) && !other.finalsBusy)other.finalsRevision=cfg.revision;
    buttons();view.poller?.kick();
  }
  roster.onsubmit=event=>{
    event.preventDefault();if(!view.valid() || (view.latestRosterLocked ?? view.config.rosterLocked) || view.candidateLoading)return;
    const teamIds=[...roster.querySelectorAll('input[name=teamIds]:checked')].map(input=>input.value),judgeIds=[...roster.querySelectorAll('input[name=judgeIds]:checked')].map(input=>input.value);
    if(!teamIds.length || judgeIds.length!==8){finalsMessage(roster,'请选择至少1支决赛队伍，以及恰好8位不同评委。');return;}
    return finalsSave(view,roster,'admin/finals/roster',{expectedRevision:roster.finalsRevision,teamIds,judgeIds},result=>saved(roster,result));
  };
  settings.onsubmit=event=>{
    event.preventDefault();const open=settings.elements.open.checked,published=settings.elements.published.checked;
    if(open && published){finalsMessage(settings,'请先关闭评分与提名，再发布最终结果。');return;}
    finalsSave(view,settings,'admin/finals/settings',{expectedRevision:settings.finalsRevision,open,published},result=>saved(settings,result));
  };
  create.onsubmit=event=>{
    event.preventDefault();if(!view.valid() || (view.latestRosterLocked ?? view.config.rosterLocked) || (view.latestJudgeIds ?? view.config.judgeIds)?.length || create.finalsBusy || roster.finalsBusy || view.judgesGenerating)return;
    if(dirtyAdminForms.has(roster)){finalsMessage(create,'请先保存或复制名单中的未保存输入，再重新载入后建立评委席位。');return;}
    // Replacing the judge list is safe only while its whole roster is held read-only.
    // Team selections remain in the same DOM, and the independent settings form stays editable.
    view.judgesGenerating=true;buttons();
    return finalsSave(view,create,'admin/finals/judges',{expectedRevision:create.finalsRevision},result=>{
      saved(create,result);
      const judges=result.judges || [];
      view.container.querySelector('[data-finals-judge-list]').innerHTML=finalsJudgeChecks(judges,result.config.judgeIds || []);
      roster.finalsRevision=result.config.revision;
    }).finally(()=>{view.judgesGenerating=false;if(view.valid() && roster.isConnected)buttons();});
  };
  const candidateHost=view.container.querySelector('[data-finals-team-list]'),candidateRefresh=view.container.querySelector('[data-finals-refresh-teams]');
  view.updateRosterCandidates=data=>{
    if(!view.valid() || !candidateHost || data.config.runId!==view.config.runId || Number(data.config.revision)<Number(roster.finalsRevision))return;
    view.latestRosterLocked=data.config.rosterLocked;view.latestJudgeIds=data.config.judgeIds;
    if(!roster.finalsBusy && !view.judgesGenerating){
      const dirty=dirtyAdminForms.has(roster);
      scoringPatchTeamChoices(candidateHost,data.availableTeams || [],dirty?scoringCheckedTeams(roster):(data.config.teamIds || []));
      if(!dirty){
        const host=view.container.querySelector('[data-finals-judge-list]'),markup=finalsJudgeChecks(data.availableJudges || [],data.config.judgeIds || []);
        if(host.finalsMarkup!==markup){host.innerHTML=markup;host.finalsMarkup=markup;}
        roster.finalsRevision=data.config.revision;
      }
    }
    buttons();
  };
  if(candidateRefresh)candidateRefresh.onclick=async()=>{
    if(!view.valid() || view.candidateLoading || roster.finalsBusy || view.judgesGenerating)return;
    view.candidateLoading=true;candidateRefresh.disabled=true;buttons();
    try{const data=await finalsRequest(view,'admin/finals');if(view.valid())view.updateRosterCandidates(data);}
    catch(error){if(view.current())finalsMessage(roster,error.message+' 当前勾选已保留。');}
    finally{view.candidateLoading=false;if(view.valid()){candidateRefresh.disabled=false;buttons();}}
  };
  buttons();
}
