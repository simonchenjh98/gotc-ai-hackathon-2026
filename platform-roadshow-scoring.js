'use strict';

// Roadshow numeric scores are separate from finals and from legacy qualitative records.
function roadshowMinimum(data){return Number(data.summary?.minimumScores || data.config?.minimumScores || 3);}
function roadshowTeamValid(team,minimum=3){return team.eligible!==false && team.valid!==false && Number(team.scoreCount || 0)>=minimum;}
function roadshowWindowText(config,now=config.serverTime??Date.now()){
  const deadline=config.deadline===null || config.deadline===undefined?null:Number(config.deadline);
  const ended=deadline!==null && now>=deadline;
  const state=!config.open?'路演评分未开放或已关闭，当前只读。':ended?'本轮90分钟路演已截止，当前只读。':config.canEdit?'路演评分开放中。':'路演评分当前只读。';
  const seconds=deadline===null?null:Math.max(0,Math.ceil((deadline-now)/1000));
  const remaining=config.open && config.canEdit && !ended && seconds!==null?` 剩余 ${String(Math.floor(seconds/60)).padStart(2,'0')}:${String(seconds%60).padStart(2,'0')}。`:'';
  return state+remaining+(deadline===null?'':` 截止（北京时间）：${new Date(deadline).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false})}。`);
}
function makeRoadshowCountdown({alive,apply,schedule=setTimeout,cancel=clearTimeout,clock=()=>Date.now()}){
  let timer=null,config=null,syncedAt=0,stopped=false;
  function tick(){
    timer=null;if(stopped || !alive() || !config)return;
    const now=Number(config.serverTime ?? syncedAt)+Math.max(0,clock()-syncedAt);
    const canEdit=!!config.canEdit && (config.deadline===null || config.deadline===undefined || now<Number(config.deadline));
    apply(roadshowWindowText(config,now),canEdit);
    if(config.open && canEdit && config.deadline!==null && config.deadline!==undefined)timer=schedule(tick,1000);
  }
  return {update(value){if(timer!==null)cancel(timer);config={...value};syncedAt=clock();tick();},stop(){stopped=true;if(timer!==null)cancel(timer);timer=null;}};
}

let activeRoadshowWindow=null;
function disposeRoadshowWindow(){activeRoadshowWindow?.dispose();activeRoadshowWindow=null;}
function roadshowWindowFormMarkup(window){
  return `<p class="info-banner" data-roadshow-window-countdown>${esc(roadshowWindowText(window))}</p><p class="small-note">开始后计时90分钟，到时自动停止评分；也可提前关闭。开始前请保存队伍名单并授权至少3位已启用评委。已开始的窗口不能续时。</p><div class="form-actions"><button class="btn small" type="button" data-roadshow-start ${window.open?'disabled':''}>开始90分钟路演</button><button class="btn secondary small" type="button" data-roadshow-close ${window.open?'':'disabled'}>关闭本轮评分窗口</button></div><p class="form-error" role="alert"></p>`;
}
function installRoadshowWindowControls(form,stage,initialWindow,version){
  disposeRoadshowWindow();const owner=session,start=form.querySelector('[data-roadshow-start]'),close=form.querySelector('[data-roadshow-close]'),error=form.querySelector('.form-error');
  let disposed=false,busy=false,blocked=false,revision=stage.revision,currentWindow={...initialWindow},request=null;
  const current=()=>!disposed && form.isConnected && route==='admin' && adminStage==='roadshow' && routeVersion===version;
  const valid=()=>{if(!current())return false;if(session!==owner || !ownsBrowserLogin(owner)){blocked=true;start.disabled=true;close.disabled=true;error.textContent='管理员身份已失效或切换，请刷新并核对身份。';return false;}return true;};
  const buttons=()=>{start.disabled=busy || blocked || !!currentWindow.open;close.disabled=busy || blocked || !currentWindow.open;};
  const countdown=makeRoadshowCountdown({alive:valid,apply:text=>{form.querySelector('[data-roadshow-window-countdown]').textContent=text;}});
  function applyWindow(window){currentWindow={...window};countdown.update(currentWindow);buttons();}
  const poller=makeFinalsPoller({read:signal=>api('admin/roadshow/scoring',undefined,{loginId:owner.loginId,signal}),alive:valid,isHidden:()=>document.hidden,
    subscribe:listener=>{document.addEventListener('visibilitychange',listener);return ()=>document.removeEventListener('visibilitychange',listener);},status:kind=>{if(current() && kind==='auth'){blocked=true;buttons();error.textContent='登录已失效，请刷新后重新登录。';}},apply:data=>{
      if(!valid() || busy || Number(data.config.stageRevision)<Number(revision))return;
      if(data.config.runId!==stage.runId || Number(data.config.stageRevision)!==Number(revision)){blocked=true;buttons();error.textContent='路演名单、窗口或轮次已更新。请刷新数据核对后再操作。';return;}
      applyWindow(data.config);
    }});
  const controller={dispose(){disposed=true;countdown.stop();poller.stop();request?.abort();}};activeRoadshowWindow=controller;
  async function save(open){
    if(!valid() || busy || blocked || (open?start.disabled:close.disabled))return;
    if(open && stageHasUnsavedInputs()){error.textContent='请先保存本轮名单及其他未保存输入，再开始90分钟路演。';return;}
    busy=true;buttons();error.textContent='';
    const payload=open?{expectedRevision:revision,open:true,start90Minutes:true}:{expectedRevision:revision,open:false,deadline:null};
    request=new AbortController();const deadline=setTimeout(()=>request?.abort(),8000);
    try{
      const saved=await api('admin/judging/roadshow/window',payload,{loginId:owner.loginId,signal:request.signal});if(!valid())return;
      revision=saved.stage.revision;applyWindow(saved.window);patchSubmissionStageCards(saved);dirtyAdminForms.delete(form);
      toast(open?'路演评分已开始，90分钟后自动截止。':'路演评分窗口已关闭。');
      if(!stageHasUnsavedInputs())await loadAdminJudging(version,form);else error.textContent='窗口已保存，其他未保存输入已保留。刷新数据后可核对最新设置。';
    }catch(failure){if(valid()){blocked=true;error.textContent=failure.message+' 请刷新数据核对当前窗口后再操作，没有自动重发。';}}
    finally{clearTimeout(deadline);request=null;busy=false;if(valid()){buttons();poller.kick();}}
  }
  start.onclick=()=>save(true);close.onclick=()=>save(false);form.onsubmit=event=>event.preventDefault();applyWindow(initialWindow);poller.start();return controller;
}

function roadshowScoreSummaryMarkup(data,publicView=false){
  if(!data.summary || (publicView && data.config?.published!==true))return '<section class="surface"><h2>路演成绩尚未发布</h2><p>评分窗口结束后，组织方核对有效成绩并发布。</p></section>';
  const summary=data.summary,teams=summary.teams || [],minimum=roadshowMinimum(data),published=data.config?.published===true;
  const rows=[...teams].sort((a,b)=>Number(roadshowTeamValid(b,minimum))-Number(roadshowTeamValid(a,minimum))||(a.rank??9999)-(b.rank??9999)||a.number-b.number).map(team=>{
    const valid=roadshowTeamValid(team,minimum),eligible=team.eligible!==false;
    return `<tr><td>${!eligible?'—':valid?esc(team.rank??'待定'):'评分不足'}</td><td><strong>${esc(finalsName(team))}</strong><small>${esc(team.project || '作品名称待完善')}${!eligible?' · 已不具备参赛资格':''}</small></td><td class="board-score">${!eligible || (publicView && !valid)?'—':finalsScore(team.score)}${!publicView && eligible && !valid?'<small>暂定均分 · 不参与排名</small>':''}</td><td>已收 ${Number(team.scoreCount || 0)} / 至少 ${minimum}<small>${Number(team.recusedCount || 0)} 人回避 · ${valid?'有效成绩':'评分不足'}</small></td></tr>`;
  }).join('');
  return `<div class="info-banner ${published?'':'warning'}"><strong>${published?'已发布 · 路演最终成绩':'暂定路演成绩 · 尚未发布'}</strong><p>每队至少${minimum}位不同评委正式评分后成绩有效，平均分纳入所有已交正式评分。草稿、未交与回避不计入人数或分数；不足${minimum}份不排名。成绩排名不代表晋级名单。</p></div>${metrics([[teams.length,'路演队伍'],[Number(summary.activeJudges ?? summary.expectedJudges ?? 0),'本轮有效评委'],[teams.filter(team=>roadshowTeamValid(team,minimum)).length,'有效成绩队伍'],[published?'已发布':'暂定','当前结果']])}<section class="surface"><h2>路演成绩 · 六维100分</h2><div class="table-wrap"><table class="board-table finals-results"><thead><tr><th>排名</th><th>队伍 / 作品</th><th>平均分</th><th>正式评分人数</th></tr></thead><tbody>${rows || '<tr><td colspan="4">尚未配置路演队伍。</td></tr>'}</tbody></table></div></section>${!publicView && summary.blockers?.length?`<div class="info-banner warning"><strong>发布前还需处理</strong><ul>${summary.blockers.map(item=>`<li>${esc(item)}</li>`).join('')}</ul></div>`:''}`;
}

function roadshowPatchScoreSummary(view,data,publicView=false){
  const host=view.container.querySelector('[data-roadshow-summary]'),markup=roadshowScoreSummaryMarkup(data,publicView);
  if(host.roadshowMarkup!==markup){host.innerHTML=markup;host.roadshowMarkup=markup;}
  view.latest=data;
}

async function renderRoadshowBoard(version){
  const view=createFinalsView(version,null),data=await finalsRequest(view,'roadshow/board');if(!view.valid())return;
  finalsShell(view,'ROADSHOW / 路演成绩','路演成绩看板','创意20 · 社会关怀15 · 技术呈现20 · 应用潜力20 · AI跨界探索15 · 开源贡献10','<p class="small-note" data-finals-sync role="status">每2秒同步</p><div data-roadshow-summary></div>');
  const update=latest=>roadshowPatchScoreSummary(view,latest,true);update(data);finalsStartPolling(view,'roadshow/board',update);
}

async function renderRoadshowJudge(version){
  const view=createFinalsView(version,'judge');let data;
  try{data=await finalsRequest(view,'roadshow/scoring');}
  catch(error){
    if(!view.current())return;
    if(error.status!==403)throw error;
    finalsShell(view,'ROADSHOW / 路演评委','请使用本轮路演邀请码','当前身份尚未加入本轮路演名单。',`<section class="surface"><p>${esc(error.message)}</p><button class="btn" type="button" data-roadshow-login>返回姓名领码与登录</button></section>`);
    view.container.querySelector('[data-roadshow-login]').onclick=()=>{disposeFinalsView();renderLogin('judge');};return;
  }
  if(!view.valid())return;
  view.data=data;view.config=data.config;view.scores=new Map((data.myScores || []).map(record=>[record.teamId,record]));
  finalsShell(view,'ROADSHOW / 路演评委','路演评分 · 六维100分',`${esc(session.name || '评委')} · 路演成绩独立统计；专项奖提名在决赛进行。`,`<div class="finals-admin-tools"><button class="btn secondary small" type="button" data-roadshow-reload-score>重新读取评分</button><span class="small-note" data-finals-sync role="status">每2秒核对评分状态</span></div><p class="info-banner ${data.config.canEdit?'':'warning'}" data-roadshow-window>${esc(roadshowWindowText(data.config))}</p><p class="info-banner warning" data-roadshow-changed hidden></p><section class="surface"><label class="field">选择路演队伍<select data-roadshow-team>${data.teams.map(team=>`<option value="${esc(team.id)}" ${team.eligible===false?'disabled':''}>${esc(finalsName(team))} · ${esc(finalsStatus(view.scores.get(team.id)))}</option>`).join('')}</select></label><div data-roadshow-editor></div></section>`);
  const select=view.container.querySelector('[data-roadshow-team]');view.selected=data.teams.find(team=>team.eligible!==false)?.id || '';select.value=view.selected;
  view.container.querySelector('[data-roadshow-reload-score]').onclick=()=>finalsConfirmReload(view);
  select.onchange=()=>{
    const previous=view.scoreForm;
    if(previous?.finalsBusy){select.value=view.selected;finalsMessage(previous,'评分正在保存，请等待完成后再切换队伍。');return;}
    if(previous && dirtyAdminForms.has(previous) && !confirm('切换队伍会放弃当前未保存的评分。请先保存或复制输入，确认切换？')){select.value=view.selected;return;}
    if(previous)finalsDiscard(view,previous);view.selected=select.value;roadshowRenderScore(view);
  };
  roadshowRenderScore(view);
  const countdown=makeRoadshowCountdown({alive:()=>view.valid(),apply:(text,canEdit)=>{
    view.config.canEdit=canEdit;const notice=view.container.querySelector('[data-roadshow-window]');notice.textContent=text;notice.classList.toggle('warning',!canEdit);
    if(view.scoreForm){view.scoreForm.querySelector('fieldset').disabled=!canEdit;view.scoreForm.finalsRefreshButtons();}
  }});view.cleanups.push(()=>countdown.stop());countdown.update(data.config);
  finalsStartPolling(view,'roadshow/scoring',latest=>{
    if(latest.config.runId!==view.config.runId){view.lock();const notice=view.container.querySelector('[data-finals-auth]');notice.hidden=false;notice.textContent='路演轮次已变化，当前输入已保留。请复制输入后刷新页面，重新读取本轮评分。';return;}
    view.config={...view.config,...latest.config};countdown.update(latest.config);
    const newer=(latest.myScores || []).some(record=>record.revision>(view.scores.get(record.teamId)?.revision || 0));
    const changed=view.container.querySelector('[data-roadshow-changed]');changed.hidden=!newer;if(newer)changed.textContent='本评委的评分已在其他页面更新。当前输入已保留，请复制后重新读取评分并核对。';
  });
}

function roadshowRenderScore(view){
  const team=view.data.teams.find(item=>item.id===view.selected),host=view.container.querySelector('[data-roadshow-editor]');
  if(!team){host.innerHTML='<p>本轮路演队伍尚未配置，请等待组织方准备。</p>';return;}
  let record=view.scores.get(team.id);const dimensions=view.data.dimensions;
  host.innerHTML=`<form data-roadshow-score-form><h2>${esc(finalsName(team))}</h2><p>${esc(team.project || '作品名称待完善')}</p><p class="small-note" data-finals-saved>${esc(finalsStatus(record))}</p><fieldset class="finals-fieldset" ${view.config.canEdit?'':'disabled'}>${dimensions.map((dimension,index)=>`<label class="finals-dimension"><span><strong>${esc(dimension.name)}</strong><small>${esc(finalsGuidance[dimension.id] || '')}</small></span><span class="finals-number"><input name="score-${esc(dimension.id)}" type="number" min="0" max="${Number(dimension.max)}" step="0.1" inputmode="decimal" required aria-label="${esc(dimension.name)}，满分${Number(dimension.max)}分" value="${record?.scores?.[index]??''}"><small>/ ${Number(dimension.max)}</small></span></label>`).join('')}<p class="score-preview">当前合计 <strong data-finals-total>—<small> / 100</small></strong></p><label class="field">评语（选填）<textarea name="feedback" maxlength="2000" rows="4">${esc(record?.feedback || '')}</textarea></label><div class="form-actions"><button class="btn" type="submit" data-finals-save>正式提交评分</button><button class="btn secondary" type="button" data-finals-draft data-finals-save>保存草稿</button><button class="btn secondary" type="button" data-finals-recuse data-finals-save>回避此队</button></div></fieldset><p class="form-error" role="status"></p><div data-finals-recovery></div></form>`;
  if(typeof reviewMaterialMarkup==='function')host.insertAdjacentHTML('beforeend',reviewMaterialMarkup(team.materialSnapshot));
  const form=host.querySelector('form');view.scoreForm=form;finalsTrack(view,form);
  const draft=form.querySelector('[data-finals-draft]'),recuse=form.querySelector('[data-finals-recuse]'),submit=form.querySelector('[type=submit]');
  form.finalsRefreshButtons=()=>{const disabled=!view.config.canEdit || form.finalsBusy || form.finalsBlocked;submit.disabled=disabled;recuse.disabled=disabled;draft.disabled=disabled || record?.status==='submitted';};
  function values(){return dimensions.map(dimension=>{const input=form.elements['score-'+dimension.id],value=Number(input.value);if(!input.value.trim() || !Number.isFinite(value) || value<0 || value>dimension.max || Math.abs(value*10-Math.round(value*10))>1e-7)throw new Error(`${dimension.name}须在0至${dimension.max}分之间，最多一位小数。`);return value;});}
  function preview(){try{form.querySelector('[data-finals-total]').innerHTML=values().reduce((sum,value)=>sum+value,0).toFixed(1)+'<small> / 100</small>';}catch{form.querySelector('[data-finals-total]').innerHTML='—<small> / 100</small>';}}
  form.addEventListener('input',preview);preview();
  async function save(status){
    if(!view.valid() || !view.config.canEdit)return;
    let scores=null;try{if(status!=='recused')scores=values();}catch(error){finalsMessage(form,error.message);return;}
    if(status==='recused' && !confirm('确认回避此队？本评委对此队的分数将不计入路演平均分。'))return;
    await finalsSave(view,form,'roadshow/score',{runId:view.config.runId,teamId:team.id,expectedRevision:record?.revision || 0,status,scores,feedback:form.elements.feedback.value.trim()},(result,unchanged)=>{
      record=result.score;view.scores.set(team.id,record);form.querySelector('[data-finals-saved]').textContent=finalsStatus(record);
      const option=[...view.container.querySelector('[data-roadshow-team]').options].find(item=>item.value===team.id);if(option)option.textContent=finalsName(team)+' · '+finalsStatus(record);
      if(unchanged && record.status==='recused'){for(const dimension of dimensions)form.elements['score-'+dimension.id].value='';preview();}view.poller?.kick();
    });
  }
  form.onsubmit=event=>{event.preventDefault();return save('submitted');};draft.onclick=()=>save('draft');recuse.onclick=()=>save('recused');form.finalsRefreshButtons();
}

async function mountRoadshowScoringAdmin(version,parent){
  const view=createFinalsView(version,'admin');parent.insertAdjacentHTML('beforeend','<section class="roadshow-score-admin finals-root"><p data-finals-auth class="info-banner warning" role="alert" hidden></p><h2>路演实时成绩与发布</h2><p class="info-banner" data-roadshow-countdown></p><p class="small-note" data-finals-sync role="status">每2秒同步成绩</p><div data-roadshow-summary><p>正在读取路演成绩…</p></div><section class="surface"><h3>公开路演成绩</h3><p>90分钟到时或手动关闭后，只要至少1队获得3位不同评委正式评分，即可发布。未达3份的队伍显示评分不足，不公开均分、不参与排名；不要求所有评委交齐。</p><button class="btn" type="button" data-roadshow-publish disabled>发布路演成绩</button><button class="btn secondary" type="button" data-roadshow-refresh-score>重新读取成绩</button><p class="form-error" data-roadshow-publish-message role="status"></p><a href="#board-roadshow">打开公开路演成绩看板 →</a></section></section>');
  view.container=parent.querySelector('.roadshow-score-admin');view.publishBusy=false;view.publishBlocked=false;
  const publish=view.container.querySelector('[data-roadshow-publish]'),message=view.container.querySelector('[data-roadshow-publish-message]');
  const countdown=makeRoadshowCountdown({alive:()=>view.valid(),apply:text=>{const node=view.container.querySelector('[data-roadshow-countdown]');if(node)node.textContent=text;}});view.cleanups.push(()=>countdown.stop());
  function update(data){
    if(view.latest && Number(data.config.revision)<Number(view.latest.config.revision))return;
    roadshowPatchScoreSummary(view,data);countdown.update(data.config);publish.textContent=data.config.published?'撤回公开路演成绩':'发布路演成绩';
    publish.disabled=!!view.publishBusy || !!view.publishBlocked || (!data.config.published && !(data.summary?.canPublish ?? (!data.config.canEdit && (data.summary?.teams || []).some(team=>roadshowTeamValid(team,roadshowMinimum(data))))));
  }
  async function refresh(){
    if(!view.valid() || view.publishBusy)return;
    try{const data=await finalsRequest(view,'admin/roadshow/scoring');if(!view.valid())return;view.publishBlocked=false;message.textContent='';update(data);}
    catch(error){if(view.current())message.textContent=error.message+' 当前页面输入已保留，请重新读取。';}
  }
  view.container.querySelector('[data-roadshow-refresh-score]').onclick=refresh;
  publish.onclick=async()=>{
    if(!view.valid() || view.publishBusy || view.publishBlocked || publish.disabled || !view.latest)return;
    const config=view.latest.config;view.publishBusy=true;publish.disabled=true;message.textContent='';
    try{
      const data=await finalsRequest(view,'admin/roadshow/publish',{runId:config.runId,expectedRevision:config.revision,published:!config.published});
      if(!view.valid())return;update(data);message.textContent=data.config.published?'路演成绩已发布，公开看板将自动同步。':'路演成绩已撤回公开。';
    }catch(error){if(view.current()){view.publishBlocked=true;message.textContent=error.message+' 请重新读取最新成绩核对后，再手动操作；没有自动重发。';}}
    finally{view.publishBusy=false;if(view.valid() && view.latest)update(view.latest);}
  };
  await refresh();if(view.valid())finalsStartPolling(view,'admin/roadshow/scoring',update);
}
