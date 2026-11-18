'use strict';

async function renderFinalsBoard(version){
  const view=createFinalsView(version,null),data=await finalsRequest(view,'finals/board');if(!view.valid())return;
  finalsShell(view,'FINALS / 决赛结果','决赛成绩与六项专项奖','7位评委 · 六维100分 · 每奖最多7票，最高票并列共同获奖。','<p class="small-note" data-finals-sync role="status">每2秒同步</p><div data-finals-summary></div>');
  const update=latest=>finalsPatchSummary(view,latest,true);update(data);finalsStartPolling(view,'finals/board',update);
}

async function renderFinalsAccess(version){
  const view=createFinalsView(version,'access');let available=false,busy=false;
  finalsShell(view,'FINALS / 决赛评委','决赛评分与奖项提名','登录开放后输入主办方提供的邀请码，验证后选择自己的姓名或评委席位。',`<section class="surface finals-entry"><h2>决赛评委验证</h2><p class="info-banner" data-finals-login-status role="status">正在读取登录开放状态…</p><form data-finals-access><label class="field">决赛邀请码<input name="code" type="password" autocomplete="one-time-code" required maxlength="160" placeholder="输入主办方提供的邀请码" disabled></label><button class="btn" type="submit" disabled>验证并选择评委席位</button><p class="form-error" role="alert"></p></form><button class="btn secondary small" type="button" data-finals-login-refresh>重新读取登录状态</button><p class="small-note">每2秒自动核对开放状态。每位评委的评分和提名单独保存。</p></section>`);
  const form=view.container.querySelector('[data-finals-access]'),button=form.querySelector('button'),notice=view.container.querySelector('[data-finals-login-status]');
  const buttons=()=>{button.disabled=form.elements.code.disabled=busy || !available;};
  view.poller=makeFinalsPoller({
    read:signal=>finalsRequest(view,'finals/login-status',undefined,signal),
    apply:schedule=>{if(!view.valid())return;available=schedule.loginAvailable===true;notice.textContent=schedule.message || (available?'评委登录已开放，请输入决赛邀请码。':'评委登录尚未开放，请等待主办方通知。');buttons();},
    status:kind=>{if(view.current() && kind!=='updated'){available=false;buttons();notice.textContent='暂时无法读取登录开放状态，正在重试；也可点击重新读取。';}},
    alive:()=>view.valid(),isHidden:()=>document.hidden,subscribe:listener=>{document.addEventListener('visibilitychange',listener);return ()=>document.removeEventListener('visibilitychange',listener);}
  });
  view.container.querySelector('[data-finals-login-refresh]').onclick=()=>view.poller.kick();
  form.onsubmit=async event=>{
    event.preventDefault();if(busy || !available || !view.valid() || !form.isConnected)return;
    busy=true;buttons();finalsMessage(form,'');
    const code=form.elements.code.value;
    const unchanged=()=>view.valid() && form.isConnected && available && form.elements.code.value===code;
    try{
      const result=await withLoginRetry(()=>finalsRequest(view,'finals/access',{code}), unchanged,
        message=>finalsMessage(form,message));
      if(!unchanged())return;
      if(typeof result.accessToken!=='string' || result.accessToken.length<32 || !Number.isFinite(result.expiresAt) || result.expiresAt<=Date.now())throw new Error('验证结果无效，请重新输入邀请码。');
      finalsSetAccess({accessToken:result.accessToken,expiresAt:result.expiresAt});
      await renderFinalsJudge(version);
    }catch(error){
      if(view.current() && form.isConnected){
        if(error.code==='judge_login_not_open'){available=false;notice.textContent=error.message;view.poller.kick();}
        finalsMessage(form,error.name==='AbortError'?'验证超时，请重试。':error.message);
      }
    }finally{if(form.elements.code.value===code)form.elements.code.value='';busy=false;if(form.isConnected)buttons();}
  };
  await view.poller.refresh();
}
function finalsInstallAccessExit(view,version){
  const button=view.container.querySelector('[data-finals-access-exit]');if(!button)return;
  button.onclick=()=>{
    if(!view.valid())return;
    if([...view.forms].some(form=>form.finalsBusy)){toast('正在保存，请等待结果返回后再退出。');return;}
    if([...view.forms].some(form=>dirtyAdminForms.has(form)) && !confirm('退出会放弃未保存的评分或提名，请先保存或复制输入。确认退出？'))return;
    finalsSetAccess(null);return renderFinalsAccess(version);
  };
}
async function renderFinalsJudge(version,judge=null){
  if(!finalsHasAccess())return renderFinalsAccess(version);
  const view=createFinalsView(version,judge?'seat':'entry',judge);
  if(!judge){
    let judges=[],available=false,choices='';
    finalsShell(view,'FINALS / 决赛评委','决赛评分与奖项提名','邀请码已验证。登录开放后选择自己的姓名或评委席位，即可查看队伍并评分。',`<section class="surface finals-entry"><h2>选择本次评分归属</h2><p>请按主办方分配的姓名或席位选择。每位评委的评分和提名单独保存。</p><p class="info-banner" data-finals-login-status role="status">正在读取登录开放状态…</p><form data-finals-entry><label class="field">评委姓名 / 席位<select name="judgeId" required disabled><option value="">请选择自己的姓名或席位</option></select></label><button class="btn" type="submit" disabled>进入评分与提名</button><p class="form-error" role="status"></p></form><p class="small-note" data-finals-entry-state></p><button class="btn secondary small" type="button" data-finals-login-refresh>重新读取登录状态</button><p class="small-note">每2秒自动核对开放状态。</p><button class="btn secondary small" type="button" data-finals-access-exit>退出决赛验证</button></section>`);
    finalsInstallAccessExit(view,version);
    const form=view.container.querySelector('[data-finals-entry]'),select=form.elements.judgeId,submit=form.querySelector('[type=submit]');
    const notice=view.container.querySelector('[data-finals-login-status]'),state=view.container.querySelector('[data-finals-entry-state]');
    const buttons=()=>{select.disabled=submit.disabled=!available || !judges.length;};
    view.poller=makeFinalsPoller({
      read:async signal=>{
        try{const schedule=await finalsRequest(view,'finals/login-status',undefined,signal);return {schedule,entry:schedule.loginAvailable===true?await finalsRequest(view,'finals/entry',undefined,signal):null};}
        catch(error){
          if(error.code==='judge_login_not_open')return {schedule:{loginAvailable:false,message:error.message},entry:null};
          if(error.code==='finals_access_required' && view.current())void renderFinalsAccess(version);
          throw error;
        }
      },
      apply:({schedule,entry})=>{
        if(!view.valid())return;available=schedule.loginAvailable===true && !!entry;
        notice.textContent=schedule.message || (available?'评委登录已开放。':'评委登录尚未开放，请等待主办方通知。');
        if(entry){
          judges=entry.judges || [];const next=judges.map(item=>`<option value="${esc(item.id)}">${esc(item.name)}</option>`).join('');
          if(next!==choices){const selected=select.value;select.innerHTML='<option value="">请选择自己的姓名或席位</option>'+next;select.value=judges.some(item=>item.id===selected)?selected:'';choices=next;}
          state.textContent=!entry.config.rosterReady?'组织方正在准备决赛名单与7个评委席位。':entry.config.open?'决赛评分与提名已开放。':'当前尚未开放或已关闭，可进入查看已保存记录。';
        }else state.textContent='';
        buttons();
      },
      status:kind=>{if(view.current() && kind!=='updated'){available=false;buttons();if(!finalsHasAccess()){void renderFinalsAccess(version);return;}notice.textContent='暂时无法读取登录开放状态，正在重试；也可点击重新读取。';}},
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
  if(error.code==='finals_access_required')return renderFinalsAccess(version);
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
