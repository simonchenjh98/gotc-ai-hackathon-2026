'use strict';

// Roadshow numeric scores are separate from finals and from legacy qualitative records.
const roadshowProgressLevels=[[0,'无实质变化'],[2,'少量修改'],[4,'有可验证实质改进，但未贯通'],[6,'重要新增或改进可演示'],[8,'关键成果完整，可复验'],[10,'相对初始 Demo 有充分、完整的进展']];
function roadshowProgressPending(record){return record?.status==='submitted' && (record.progressPending===true || !Number.isInteger(record.progressScore));}
function roadshowScoreStatus(record){return roadshowProgressPending(record)?'已正式提交 · 待补进展评分，不计入均分':finalsStatus(record);}
function roadshowWeightedPreview(scores,progressScore){const rawTenths=scores.reduce((sum,score)=>sum+Math.round(score*10),0);return {rawTotal:rawTenths/10,multiplier:(10+progressScore)/20,weightedTotal:Math.floor((rawTenths*(10+progressScore)+1)/2)/100};}
function roadshowProgressMarkup(record){
  return `<section class="roadshow-progress"><h3>两天作品进展</h3><p class="small-note">现场对照初始 Demo 与最终成果核验进展。无法核实可先保存草稿；不按代码行数或提交次数自动评分。</p><label class="field">进展评分（正式提交必填，0—10分）<select name="progressScore" required><option value="" ${record?.progressScore===null || record?.progressScore===undefined?'selected':''}>请选择进展评分</option>${Array.from({length:11},(_,value)=>`<option value="${value}" ${record?.progressScore===value?'selected':''}>${value}分 · 系数 ${((10+value)/20).toFixed(2)}</option>`).join('')}</select></label><details class="roadshow-progress-guide"><summary>查看进展评分参考档位</summary><ul>${roadshowProgressLevels.map(([score,label])=>`<li><strong>${score}分</strong>：${esc(label)}</li>`).join('')}</ul><p class="small-note">可选择相邻档位之间的整数分。进展系数 = 0.5 + 0.05 × 进展分；每份路演加权分 = 六维原始总分 × 进展系数。</p></details><label class="field">进展依据（选填，最多1000字）<textarea name="progressNote" maxlength="1000" rows="3" placeholder="可记录本次新增或改进，以及现场核验依据">${esc(record?.progressNote || '')}</textarea></label><p class="score-preview" data-roadshow-weighted>请填写六维分数并选择进展评分，查看加权分。</p></section>`;
}
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
    const hidden=!eligible || (publicView && !valid);
    return `<tr><td>${!eligible?'—':valid?esc(team.rank??'待定'):'评分不足'}</td><td><strong>${esc(finalsName(team))}</strong><small>${esc(team.project || '作品名称待完善')}${!eligible?' · 已不具备参赛资格':''}</small></td><td class="board-score">${hidden?'—':finalsScore(team.score)}${!publicView && eligible && !valid?'<small>暂定加权均分 · 不参与排名</small>':''}</td><td>${hidden?'—':finalsScore(team.rawScore)}<small>同批有效评分</small></td><td>已收 ${Number(team.scoreCount || 0)} / 至少 ${minimum}<small>${Number(team.recusedCount || 0)} 人回避 · ${valid?'有效成绩':'评分不足'}${Number(team.pendingProgressCount || 0)>0?` · ${Number(team.pendingProgressCount)} 份待补进展`:''}</small></td></tr>`;
  }).join('');
  return `<div class="info-banner ${published?'':'warning'}"><strong>${published?'已发布 · 路演最终成绩':'暂定路演成绩 · 尚未发布'}</strong><p>路演按加权均分排名：每位评委的六维原始总分 × 进展系数，再对有效正式评分取平均。进展系数 = 0.5 + 0.05 × 两天进展分（0—10分）。每队至少${minimum}位不同评委完整正式评分后成绩有效；草稿、未交、回避与待补进展评分不计入人数或分数，不足${minimum}份不排名。成绩排名不代表晋级名单。</p></div>${metrics([[teams.length,'路演队伍'],[Number(summary.activeJudges ?? summary.expectedJudges ?? 0),'本轮有效评委'],[teams.filter(team=>roadshowTeamValid(team,minimum)).length,'有效成绩队伍'],[published?'已发布':'暂定','当前结果']])}<section class="surface"><h2>路演成绩 · 进展加权</h2><div class="table-wrap"><table class="board-table finals-results"><thead><tr><th>排名</th><th>队伍 / 作品</th><th>加权均分（排名依据）</th><th>六维原始均分</th><th>完整正式评分人数</th></tr></thead><tbody>${rows || '<tr><td colspan="5">尚未配置路演队伍。</td></tr>'}</tbody></table></div></section>${!publicView?roadshowScoreDetailsMarkup(data):''}${!publicView && summary.blockers?.length?`<div class="info-banner warning"><strong>发布前还需处理</strong><ul>${summary.blockers.map(item=>`<li>${esc(item)}</li>`).join('')}</ul></div>`:''}`;
}

function roadshowScoreDetailsMarkup(data){
  const details=Array.isArray(data.scoreDetails)?data.scoreDetails:[];if(!details.length)return '';
  const grouped=new Map();for(const record of details){if(!grouped.has(record.teamId))grouped.set(record.teamId,[]);grouped.get(record.teamId).push(record);}
  return `<section class="surface roadshow-score-details"><h2>逐队评分明细 · 仅后台可见</h2><p class="small-note">核对各评委的六维原始总分、进展评分、系数与加权分。进展依据仅供组织方复核，不在公开看板展示。</p>${[...grouped].map(([teamId,records])=>`<details data-roadshow-details="${esc(teamId)}"><summary>${esc(finalsName({number:records[0].teamNumber,name:records[0].teamName}))} · ${records.length}份记录</summary><div class="table-wrap"><table class="board-table"><thead><tr><th>评委 / 状态</th><th>原始总分</th><th>进展分</th><th>系数</th><th>加权分</th><th>进展依据</th></tr></thead><tbody>${records.map(record=>`<tr><td>${esc(record.judgeName)}<small>${esc(roadshowScoreStatus(record))}${record.judgeActive===false?' · 评委已停用':''}${record.teamEligible===false?' · 队伍无参赛资格':''}<br>${record.validForAverage?'计入当前均分':'不计入当前均分'}</small></td><td>${finalsScore(record.rawTotal)}</td><td>${record.progressScore===null || record.progressScore===undefined?'—':Number(record.progressScore)}</td><td>${finalsScore(record.progressMultiplier)}</td><td>${finalsScore(record.weightedTotal)}</td><td class="roadshow-progress-note">${esc(record.progressNote || '—')}</td></tr>`).join('')}</tbody></table></div></details>`).join('')}</section>`;
}

function roadshowPatchScoreSummary(view,data,publicView=false){
  const host=view.container.querySelector('[data-roadshow-summary]'),markup=roadshowScoreSummaryMarkup(data,publicView);
  if(host.roadshowMarkup!==markup){
    const open=new Set([...(host.querySelectorAll?.('[data-roadshow-details]') || [])].filter(node=>node.open).map(node=>node.dataset.roadshowDetails));
    const focused=document.activeElement?.matches?.('summary')?document.activeElement.closest?.('[data-roadshow-details]')?.dataset.roadshowDetails:null;
    host.innerHTML=markup;host.roadshowMarkup=markup;
    for(const node of host.querySelectorAll?.('[data-roadshow-details]') || []){node.open=open.has(node.dataset.roadshowDetails);if(node.dataset.roadshowDetails===focused)node.querySelector('summary')?.focus({preventScroll:true});}
  }
  view.latest=data;
}

async function renderRoadshowBoard(version){
  const view=createFinalsView(version,null),data=await finalsRequest(view,'roadshow/board');if(!view.valid())return;
  finalsShell(view,'ROADSHOW / 路演成绩','路演成绩看板','六维100分 × 两天作品进展系数，以各评委加权分的平均分排名。','<p class="small-note" data-finals-sync role="status">每2秒同步</p><div data-roadshow-summary></div>');
  const update=latest=>roadshowPatchScoreSummary(view,latest,true);update(data);finalsStartPolling(view,'roadshow/board',update);
}

async function renderRoadshowJudge(version){
  const view=createFinalsView(version,'judge');let data;
  try{data=await finalsRequest(view,'roadshow/scoring');}
  catch(error){
    if(!view.current())return;
    if(error.status!==403)throw error;
    finalsShell(view,'ROADSHOW / 路演评委','请使用本轮路演邀请码','当前身份尚未加入本轮路演名单。',`<section class="surface"><p>${esc(error.message)}</p><button class="btn" type="button" data-roadshow-login>返回真实姓名登录</button></section>`);
    view.container.querySelector('[data-roadshow-login]').onclick=()=>{disposeFinalsView();renderLogin('judge');};return;
  }
  if(!view.valid())return;
  view.data=data;view.config=data.config;view.scores=new Map((data.myScores || []).map(record=>[record.teamId,record]));
  finalsShell(view,'ROADSHOW / 路演评委','路演评分 · 六维与作品进展',`${esc(session.name || '评委')} · 六维100分乘以两天进展系数；专项奖提名在决赛进行。`,`<div class="finals-admin-tools"><button class="btn secondary small" type="button" data-roadshow-reload-score>重新读取评分</button><span class="small-note" data-finals-sync role="status">每2秒核对评分状态</span></div><p class="info-banner ${data.config.canEdit?'':'warning'}" data-roadshow-window>${esc(roadshowWindowText(data.config))}</p><p class="info-banner warning" data-roadshow-changed hidden></p><section class="surface"><label class="field">选择路演队伍<select data-roadshow-team>${data.teams.map(team=>`<option value="${esc(team.id)}" ${team.eligible===false?'disabled':''}>${esc(finalsName(team))} · ${esc(roadshowScoreStatus(view.scores.get(team.id)))}</option>`).join('')}</select></label><div data-roadshow-editor></div></section>`);
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
    if(view.scoreForm){view.scoreForm.querySelector('fieldset').disabled=!scoringCanEditTeam(view,view.selected,true);view.scoreForm.finalsRefreshButtons();}
  }});view.cleanups.push(()=>countdown.stop());countdown.update(data.config);
  finalsStartPolling(view,'roadshow/scoring',latest=>{if(syncScoringJudge(view,latest,true)){countdown.update(latest.config);for(const option of select.options){const team=view.data.teams.find(item=>item.id===option.value);if(team)option.textContent=finalsName(team)+' · '+roadshowScoreStatus(view.scores.get(team.id))+(team.eligible===false?'（无参赛资格）':'');}}});
}

function roadshowRenderScore(view){
  const team=view.data.teams.find(item=>item.id===view.selected),host=view.container.querySelector('[data-roadshow-editor]');
  if(!team){view.scoreForm=null;host.innerHTML='<p>本轮路演队伍尚未配置，请等待组织方准备。</p>';return;}
  let record=view.scores.get(team.id);const dimensions=view.data.dimensions;
  host.innerHTML=`<form data-roadshow-score-form><h2>${esc(finalsName(team))}</h2><p>${esc(team.project || '作品名称待完善')}</p><p class="small-note" data-finals-saved>${esc(roadshowScoreStatus(record))}</p><fieldset class="finals-fieldset" ${scoringCanEditTeam(view,team.id,true)?'':'disabled'}>${dimensions.map((dimension,index)=>`<label class="finals-dimension"><span><strong>${esc(dimension.name)}</strong><small>${esc(finalsGuidance[dimension.id] || '')}</small></span><span class="finals-number"><input name="score-${esc(dimension.id)}" type="number" min="0" max="${Number(dimension.max)}" step="0.1" inputmode="decimal" required aria-label="${esc(dimension.name)}，满分${Number(dimension.max)}分" value="${record?.scores?.[index]??''}"><small>/ ${Number(dimension.max)}</small></span></label>`).join('')}<p class="score-preview">六维原始总分 <strong data-finals-total>—<small> / 100</small></strong></p>${roadshowProgressMarkup(record)}<label class="field">评语（选填）<textarea name="feedback" maxlength="2000" rows="4">${esc(record?.feedback || '')}</textarea></label><div class="form-actions"><button class="btn" type="submit" data-finals-save>正式提交评分</button><button class="btn secondary" type="button" data-finals-draft data-finals-save>保存草稿</button><button class="btn secondary" type="button" data-finals-recuse data-finals-save>回避此队</button></div></fieldset><p class="form-error" role="status"></p><div data-finals-recovery></div></form>`;
  if(typeof reviewMaterialMarkup==='function')host.insertAdjacentHTML('beforeend',reviewMaterialMarkup(team.materialSnapshot));
  const form=host.querySelector('form');view.scoreForm=form;finalsTrack(view,form);
  const draft=form.querySelector('[data-finals-draft]'),recuse=form.querySelector('[data-finals-recuse]'),submit=form.querySelector('[type=submit]');
  form.finalsRefreshButtons=()=>{const disabled=!scoringCanEditTeam(view,team.id,true) || form.finalsBusy || form.finalsBlocked;submit.disabled=disabled;recuse.disabled=disabled;draft.disabled=disabled || record?.status==='submitted';};
  function values(){return dimensions.map(dimension=>{const input=form.elements['score-'+dimension.id],value=Number(input.value);if(!input.value.trim() || !Number.isFinite(value) || value<0 || value>dimension.max || Math.abs(value*10-Math.round(value*10))>1e-7)throw new Error(`${dimension.name}须在0至${dimension.max}分之间，最多一位小数。`);return value;});}
  function progress(required){const value=form.elements.progressScore.value;if(value==='' && !required)return null;if(!/^(?:[0-9]|10)$/.test(value))throw new Error('请选择0至10之间的整数进展评分；无法核实可先保存草稿。');return Number(value);}
  function preview(){
    const weighted=form.querySelector('[data-roadshow-weighted]');
    try{const scores=values(),value=progress(false),raw=scores.reduce((sum,score)=>sum+Math.round(score*10),0)/10;form.querySelector('[data-finals-total]').innerHTML=raw.toFixed(1)+'<small> / 100</small>';if(value===null){weighted.textContent='请选择进展评分，查看加权分。';return;}const result=roadshowWeightedPreview(scores,value);weighted.textContent=`六维原始总分 ${result.rawTotal.toFixed(1)} × 进展系数 ${result.multiplier.toFixed(2)} = 加权分 ${result.weightedTotal.toFixed(2)}`;}
    catch{form.querySelector('[data-finals-total]').innerHTML='—<small> / 100</small>';weighted.textContent='请填写六维分数并选择进展评分，查看加权分。';}
  }
  form.addEventListener('input',preview);form.addEventListener('change',preview);preview();
  async function save(status){
    if(!view.valid() || !scoringCanEditTeam(view,team.id,true))return;
    let scores=null,progressScore=null,progressNote='';try{if(status!=='recused'){scores=values();progressScore=progress(status==='submitted');progressNote=form.elements.progressNote.value.trim();if(progressNote.length>1000)throw new Error('进展依据最多1000字，也可以留空。');}}catch(error){finalsMessage(form,error.message);return;}
    if(status==='recused' && !confirm('确认回避此队？本评委对此队的分数将不计入路演平均分。'))return;
    await finalsSave(view,form,'roadshow/score',{runId:view.config.runId,teamId:team.id,expectedRevision:record?.revision || 0,status,scores,feedback:form.elements.feedback.value.trim(),progressScore,progressNote},(result,unchanged)=>{
      record=result.score;view.scores.set(team.id,record);form.querySelector('[data-finals-saved]').textContent=roadshowScoreStatus(record);
      const option=[...view.container.querySelector('[data-roadshow-team]').options].find(item=>item.value===team.id);if(option)option.textContent=finalsName(team)+' · '+roadshowScoreStatus(record);
      if(unchanged && record.status==='recused'){for(const dimension of dimensions)form.elements['score-'+dimension.id].value='';form.elements.progressScore.value='';form.elements.progressNote.value='';preview();}view.poller?.kick();
    });
  }
  form.onsubmit=event=>{event.preventDefault();return save('submitted');};draft.onclick=()=>save('draft');recuse.onclick=()=>save('recused');form.finalsRefreshButtons();
}

function roadshowTeamRosterMarkup(){
  return '<section class="surface"><h2>路演队伍名单</h2><p>候选直接来自队伍信息。勾选并保存后，评委选队菜单同步显示队伍编号、名称和作品；保存名单不会开放评分。</p><form data-roadshow-teams><fieldset class="finals-fieldset" disabled><div class="finals-roster" data-roadshow-team-choices></div><button class="btn secondary small" type="button" data-roadshow-select-all>选入全部有效队伍</button><button class="btn" type="submit" data-finals-save>保存路演队伍名单</button></fieldset><button class="btn secondary small" type="button" data-roadshow-refresh-teams>刷新队伍信息</button><p class="small-note" data-roadshow-roster-state>正在读取队伍信息…</p><p class="form-error" role="status"></p><div data-finals-recovery></div></form></section>';
}
function installRoadshowTeams(view,onSaved){
  const form=view.container.querySelector('[data-roadshow-teams]');if(!form)return null;
  const choices=form.querySelector('[data-roadshow-team-choices]'),all=form.querySelector('[data-roadshow-select-all]'),refresh=form.querySelector('[data-roadshow-refresh-teams]'),save=form.querySelector('[data-finals-save]'),state=form.querySelector('[data-roadshow-roster-state]');
  let latest=null,runId=null,revision=null,editable=false,loading=false;finalsTrack(view,form);
  const current=()=>view.valid() && form.isConnected;
  form.finalsRefreshButtons=()=>{form.querySelector('fieldset').disabled=!editable || !!form.finalsBlocked;all.disabled=save.disabled=!editable || loading || !!form.finalsBusy || !!form.finalsBlocked;refresh.disabled=loading || !!form.finalsBusy;};
  function update(data,ack=false){
    if(!current() || !data.roster || (latest && Number(data.config.revision)<Number(latest.config.revision)))return;
    latest=data;editable=data.roster.editable===true;
    if(runId!==null && runId!==data.config.runId){editable=false;form.finalsBlocked=true;finalsMessage(form,'路演轮次已变化。当前勾选已保留，请复制或记录后重新载入。');form.finalsRefreshButtons();return;}
    if(form.finalsBusy && !ack){form.finalsRefreshButtons();return;}
    const dirty=dirtyAdminForms.has(form),selected=dirty?scoringCheckedTeams(form):data.roster.teamIds;
    scoringPatchTeamChoices(choices,data.availableTeams || [],selected);
    if(!dirty || runId===null){runId=data.config.runId;revision=data.config.stageRevision;}
    state.textContent=editable?'候选自动同步；新增队伍不会自动选入。勾选后需保存路演名单。':'本轮名单已冻结或已有评审记录，当前只读；更换队伍需先创建新轮次。';
    form.finalsRefreshButtons();
  }
  all.onclick=()=>{if(!current() || all.disabled || !latest)return;scoringPatchTeamChoices(choices,latest.availableTeams || [],(latest.availableTeams || []).filter(team=>team.eligible!==false).map(team=>team.id));form.finalsEdit++;dirtyAdminForms.add(form);state.textContent='已勾选全部有效队伍，尚需保存名单。';};
  refresh.onclick=async()=>{
    if(!current() || loading || form.finalsBusy)return;loading=true;form.finalsRefreshButtons();
    try{const data=await finalsRequest(view,'admin/roadshow/scoring');if(current()){update(data);onSaved(data);}}
    catch(error){if(current())finalsMessage(form,error.message+' 当前勾选已保留。');}
    finally{loading=false;if(current())form.finalsRefreshButtons();}
  };
  form.onsubmit=event=>{
    event.preventDefault();if(!current() || !editable || loading || runId===null)return;
    return finalsSave(view,form,'admin/roadshow/teams',{runId,expectedRevision:revision,teamIds:scoringCheckedTeams(form)},(result,unchanged)=>{
      runId=result.config.runId;revision=result.config.stageRevision;if(unchanged)dirtyAdminForms.delete(form);update(result,true);onSaved(result);view.poller?.kick();
    });
  };
  form.finalsRefreshButtons();return {update,form};
}

async function mountRoadshowScoringAdmin(version,parent){
  const view=createFinalsView(version,'admin');parent.insertAdjacentHTML('beforeend','<section class="roadshow-score-admin finals-root"><p data-finals-auth class="info-banner warning" role="alert" hidden></p>'+roadshowTeamRosterMarkup()+'<h2>路演实时成绩与发布</h2><p class="info-banner" data-roadshow-countdown></p><p class="small-note" data-finals-sync role="status">每2秒同步成绩</p><div data-roadshow-summary><p>正在读取路演成绩…</p></div><section class="surface"><h3>公开路演成绩</h3><p>90分钟到时或手动关闭后，至少1队获得3位不同评委完整正式评分，且没有待补进展的正式评分，才可发布。未达3份的队伍显示评分不足，不公开均分、不参与排名；不要求所有评委交齐。可展开上方逐队评分明细，核对待补进展的记录。</p><button class="btn" type="button" data-roadshow-publish disabled>发布路演成绩</button><button class="btn secondary" type="button" data-roadshow-refresh-score>重新读取成绩</button><p class="form-error" data-roadshow-publish-message role="status"></p><a href="#board-roadshow">打开公开路演成绩看板 →</a></section></section>');
  view.container=parent.querySelector('.roadshow-score-admin');view.publishBusy=false;view.publishBlocked=false;
  const publish=view.container.querySelector('[data-roadshow-publish]'),message=view.container.querySelector('[data-roadshow-publish-message]');
  const countdown=makeRoadshowCountdown({alive:()=>view.valid(),apply:text=>{const node=view.container.querySelector('[data-roadshow-countdown]');if(node)node.textContent=text;}});view.cleanups.push(()=>countdown.stop());
  function update(data){
    if(view.latest && Number(data.config.revision)<Number(view.latest.config.revision))return;
    teamsEditor?.update(data);roadshowPatchScoreSummary(view,data);countdown.update(data.config);publish.textContent=data.config.published?'撤回公开路演成绩':'发布路演成绩';
    publish.disabled=!!view.publishBusy || !!view.publishBlocked || (!data.config.published && !(data.summary?.canPublish ?? (!data.config.canEdit && (data.summary?.teams || []).some(team=>roadshowTeamValid(team,roadshowMinimum(data))))));
  }
  const teamsEditor=installRoadshowTeams(view,update);
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
