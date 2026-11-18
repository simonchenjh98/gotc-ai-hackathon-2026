'use strict';

async function renderExhibitionJudge(version,expectedForm=null,stage=stageForRoute()){
  let data;const owner=session;
  const stale=()=>version!==routeVersion || stage!==stageForRoute() || (expectedForm && !expectedForm.isConnected) || session!==owner || !ownsBrowserLogin(owner);
  function changedLogin(){if(version!==routeVersion || stage!==stageForRoute() || (expectedForm && !expectedForm.isConnected))return;if(expectedForm)currentIdentityPage?.valid();else root.innerHTML=empty('登录身份已失效或切换','请刷新页面并核对评委身份后继续。');}
  if(stale()){changedLogin();return;}
  try{data=await api(`judging/${stage}`,undefined,{loginId:owner.loginId});}catch(error){if(stale()){changedLogin();return;}if(error.status===403){root.innerHTML=heading(`JUDGE / ${stageName(stage)}`,'本轮尚未获得评审授权',`请联系组织方，将你的评委身份加入${stageName(stage)}工作名单。`)+stageNotice(stage)+empty('尚未分配本轮队伍','获得授权后，可用当前身份重新读取。','<button class="btn secondary" id="retry-judge">重新读取</button>');document.getElementById('retry-judge').onclick=()=>renderExhibitionJudge(version,null,stage);return;}throw error;}
  if(stale()){changedLogin();return;}
  judgeData=data;
  const submitted=judgeData.myReviews.filter(r=>r.submitted && !r.recused && !r.invalidated).length;
  root.innerHTML=heading(`JUDGE / ${stageName(stage)}`,`${stageName(stage)}记录`,`${esc(session.name || '评委')} · 已正式提交 ${submitted} 份评审记录（未计分）`,`<span class="status-chip ${judgeData.window.canEdit?'':'closed'}">${judgeData.window.canEdit?'评审记录开放中':'当前只读'}</span>`)+stageNotice(stage)+`<div class="stage-strip"><strong>${esc(judgeData.stage.name)}</strong><span>数值规则待配置</span><span>截止（北京时间）：${esc(beijingTime(judgeData.window.deadline))}${judgeData.window.deadline===null?'（未设置）':''}</span></div>`+rulePlaceholders(judgeData.stage);
  if(!judgeData.teams.length){root.innerHTML+=empty('本轮还没有可评审队伍',`请联系组织方配置${stageName(stage)}工作名单。`);return;}
  selectedTeam=selectedTeams[stage] ?? (stage==='exhibition'?selectedTeam:null);
  if(!judgeData.teams.some(t=>String(t.id)===String(selectedTeam)))selectedTeam=judgeData.teams[0].id;
  selectedTeams[stage]=selectedTeam;
  root.innerHTML+=`<div class="workspace-grid"><aside class="team-sidebar"><h2>本轮${stage==='roadshow'?'路演':'展示'}队伍</h2><div class="team-picker" id="team-picker"></div></aside><div id="judge-editor"></div></div>`;
  renderExhibitionTeamPicker();renderExhibitionJudgeEditor();
}
function renderExhibitionTeamPicker(){
  document.getElementById('team-picker').innerHTML=judgeData.teams.map(t=>{const record=judgeData.myReviews.find(r=>String(r.teamId)===String(t.id));return `<button class="team-pick ${String(t.id)===String(selectedTeam)?'selected':''}" data-team="${esc(t.id)}" aria-pressed="${String(t.id)===String(selectedTeam)}"><span class="team-number">TEAM ${String(t.number).padStart(2,'0')}</span><strong>${esc(t.name)}</strong><small class="${record?.submitted && !record.recused && !record.invalidated?'saved-status':''}">${reviewState(record)}</small></button>`;}).join('');
  document.querySelectorAll('[data-team]').forEach(b=>b.onclick=()=>{if(String(selectedTeam)===b.dataset.team)return;if(!confirmLeave())return;reviewDirty=false;selectedTeam=b.dataset.team;selectedTeams[judgeData.stage.id || 'exhibition']=selectedTeam;renderExhibitionTeamPicker();renderExhibitionJudgeEditor();});
}
function renderExhibitionJudgeEditor(){
  const owner=session,data=judgeData,stage=judgeData.stage.id || 'exhibition',viewVersion=routeVersion,viewRoute=route,runId=judgeData.stage.runId;
  const team=judgeData.teams.find(t=>String(t.id)===String(selectedTeam));
  const record=judgeData.myReviews.find(r=>String(r.teamId)===String(selectedTeam));
  const canEdit=judgeData.window.canEdit;
  const feedback=record?.draft?.feedback ?? record?.submitted?.feedback ?? '';
  document.getElementById('judge-editor').innerHTML=reviewMaterialMarkup(team.materialSnapshot,judgeData.stage)+`<form class="judge-form" id="review-form"><span class="eyebrow">TEAM ${String(team.number).padStart(2,'0')}</span><h2>${esc(team.name)}</h2><p class="project-line">${esc(team.project || '作品名称待完善')} · ${esc(team.track || '方向待完善')}</p><p class="status-chip">${reviewState(record)} · 编辑版本 ${Number(record?.version || 0)}</p><div class="info-banner">请填写观察或评语后正式提交。保存草稿不会覆盖已提交的记录。</div>${record?.invalidated?`<div class="info-banner warning"><strong>组织方已作废此记录</strong><br>原因：${esc(record.invalidatedReason || '未填写')}。继续修改仍会保留作废状态，恢复需联系组织方。</div>`:''}<fieldset ${canEdit?'':'disabled'}><label class="field">评审观察 / 给团队的评语<textarea name="feedback" maxlength="2000" rows="6" placeholder="写下作品表现、判断依据或改进建议。">${esc(feedback)}</textarea><small>最多 2000 字，仅评委本人及组织方可见。正式记录与草稿分别保存。</small></label><div class="form-actions"><button type="submit" class="btn">正式提交评语 ↗</button><button type="button" class="btn secondary" id="save-review-draft">保存草稿</button><button type="button" class="btn secondary" id="recuse-review">回避此队</button></div></fieldset><p class="form-error" role="alert"></p></form><div id="review-conflict" aria-live="polite"></div>${record?.submitted?`<section class="surface formal-review"><h2>最近正式提交的评审记录</h2><p class="small-note">${esc(beijingTime(record.submitted.submittedAt))}（北京时间） · 未计分${record.invalidated?' · 已作废':''}${record.recused?' · 当前已回避':''}</p><p class="preserve-text">${esc(record.submitted.feedback)}</p></section>`:''}`;
  const form=document.getElementById('review-form');let blocked=!canEdit,saving=false;
  function valid(){
    if(route!==viewRoute || viewVersion!==routeVersion || judgeData!==data || !form.isConnected || String(selectedTeam)!==String(team.id))return false;
    if(editorHasLogin(form,owner,{notify:true}))return true;
    blocked=true;form.querySelector('fieldset').disabled=false;form.elements.feedback.readOnly=true;form.querySelectorAll('button').forEach(button=>button.disabled=true);return false;
  }
  currentIdentityPage={valid};
  form.addEventListener('input',()=>{reviewDirty=true;});
  async function save(status){
    if(blocked || saving || !valid())return;
    const value=form.elements.feedback.value.trim();
    formError(form,'');if(status==='submitted' && !value){formError(form,'请填写评审观察或评语后正式提交。');form.elements.feedback.focus();return;}
    if(Array.from(value).length>2000){formError(form,'评审记录最多 2000 字。');return;}
    const fieldset=form.querySelector('fieldset');saving=true;fieldset.disabled=true;
    try{await api(`judging/${stage}/review`,{runId,teamId:team.id,expectedVersion:record?.version || 0,status,feedback:value,scores:null},{loginId:owner.loginId});if(!valid())return;reviewDirty=false;toast(status==='submitted'?'评审记录已正式提交（未计分）。':status==='draft'?'草稿已保存，正式记录保持原版本。':'已回避此队，不计入正式评审记录数。');await renderExhibitionJudge(viewVersion,form,stage);}
    catch(error){if(!valid())return;formError(form,error.message);if(error.status===409){blocked=true;fieldset.disabled=false;form.elements.feedback.readOnly=true;form.querySelectorAll('button').forEach(b=>b.disabled=true);document.getElementById('review-conflict').innerHTML='<section class="surface"><h2>当前输入已保留</h2><p class="small-note">服务器版本或评审轮次已变化。请先复制需要保留的文字，再读取最新记录重新编辑。</p><button class="btn secondary" id="reload-review" type="button">读取最新记录</button></section>';document.getElementById('reload-review').onclick=()=>{if(!valid() || !confirmLeave())return;reviewDirty=false;renderExhibitionJudge(viewVersion,form,stage);};}else if(error.status===401 || error.status===403 || error.status===428){blocked=true;fieldset.disabled=true;}}
    finally{saving=false;if(valid() && !blocked)fieldset.disabled=false;}
  }
  form.onsubmit=e=>{e.preventDefault();return save('submitted');};document.getElementById('save-review-draft').onclick=()=>save('draft');document.getElementById('recuse-review').onclick=()=>{if(confirm('确认回避此队？已有正式记录会保留，但不计入本轮正式评审记录数。'))return save('recused');};
}
async function renderJudge(version,expectedForm=null) {
  const owner=session;const data=await api('judge',undefined,{loginId:owner.loginId});if(session!==owner || !ownsBrowserLogin(owner) || version!==routeVersion || route!=='judge' || (expectedForm && !expectedForm.isConnected))return;judgeData=data;
  root.innerHTML=heading('JUDGE / 专业评审','专业评分（六维 100 分）',`${esc(judgeData.name || session.name)} · 已正式提交 <span id="judge-score-count"></span> 份评分`, `<span id="judge-score-window" class="status-chip"></span>`) + '<div class="info-banner">本届使用六维 100 分评分方案。请先看演示、询问过程，再据实评分；草稿不计入成绩。最终规则以现场通知为准。</div>';
  updateScoreSummary(data);
  if(!judgeData.teams.length){root.innerHTML+=empty('还没有可评审的队伍','主办方录入队伍后即可开始评审。');return;}
  if(!judgeData.teams.some(t=>String(t.id)===String(selectedTeam)))selectedTeam=judgeData.teams[0].id;
  root.innerHTML+='<div class="workspace-grid"><aside class="team-sidebar"><h2>选择参赛队伍</h2><div class="team-picker" id="team-picker"></div></aside><div id="judge-editor"></div></div>';
  renderTeamPicker();renderJudgeEditor();
}
function updateScoreSummary(data){
  const count=document.getElementById('judge-score-count'),windowChip=document.getElementById('judge-score-window');
  if(count)count.textContent=String(data.myScores.filter(item=>item.status==='submitted').length);
  if(windowChip){
    windowChip.className=data.scoringOpen?'status-chip':'status-chip closed';
    windowChip.textContent=data.scoringOpen?'评分开放中':'评分尚未开放 / 已截止';
  }
}
function renderTeamPicker(){document.getElementById('team-picker').innerHTML=judgeData.teams.map(t=>{const score=judgeData.myScores.find(s=>String(s.teamId)===String(t.id));return `<button class="team-pick ${String(t.id)===String(selectedTeam)?'selected':''}" data-team="${esc(t.id)}"><span class="team-number">TEAM ${String(t.number).padStart(2,'0')}</span><strong>${esc(t.name)}</strong><small class="${score?.status==='submitted'?'saved-status':''}">${score?({submitted:'已正式提交',draft:'草稿已保存',recused:'已回避'}[score.status] || '待评分'):'待评分'}</small></button>`;}).join('');document.querySelectorAll('[data-team]').forEach(b=>b.onclick=()=>{if(String(selectedTeam)===b.dataset.team)return;if(!confirmLeave())return;scoreDirty=false;selectedTeam=b.dataset.team;renderTeamPicker();renderJudgeEditor();});}
function renderJudgeEditor(){
  const data=judgeData,team=data.teams.find(t=>String(t.id)===String(selectedTeam));
  const saved=data.myScores.find(s=>String(s.teamId)===String(selectedTeam)),dimensions=data.dimensions,owner=session,version=routeVersion;
  const disabled=data.scoringOpen?'':'disabled';
  document.getElementById('judge-editor').innerHTML=judgeSubmissionMarkup((judgeData.submissions || []).find(s=>s.teamId===team.id))+`<form class="judge-form" id="score-form"><span class="eyebrow">TEAM ${String(team.number).padStart(2,'0')}</span><h2>${esc(team.name)}</h2><p class="project-line">${esc(team.project || '作品名称待完善')} · ${esc(team.track || '方向待完善')}</p><p class="status-chip" id="score-status">${saved?({submitted:'已提交，可修改后重新提交；草稿不会覆盖正式评分',draft:'草稿已保存，尚未计入成绩',recused:'已回避，可在评分开放期间重新评分'}[saved.status]):'待评分'}</p>${dimensions.map((d,i)=>`<div class="dimension-row"><label for="score-${i}">${esc(d.name)}<small>满分 ${Number(d.max)} 分</small></label><input type="range" data-slider="${i}" aria-label="${esc(d.name)}滑块" min="0" max="${Number(d.max)}" step="0.1" value="${Number(saved?.scores?.[i] || 0)}" ${disabled}><input type="number" id="score-${i}" name="score${i}" data-score="${i}" aria-label="${esc(d.name)}分数" min="0" max="${Number(d.max)}" step="0.1" value="${Number(saved?.scores?.[i] || 0)}" required ${disabled}></div>`).join('')}<div class="score-preview"><span>专业评分总计</span><strong id="score-total">0.0 <small>/ 100</small></strong></div><label class="field">给团队的评语（选填）<textarea name="feedback" maxlength="2000" placeholder="记录你观察到的优点，以及一条具体的改进建议。" ${disabled}>${esc(saved?.feedback || '')}</textarea><small>勾选下方声明后，本次正式提交的评语将在成绩发布且达到评委人数门槛后，匿名分享给本队。请勿署名或填写联系方式；公开看板不展示评语。旧评语需重新确认后提交才会分享。</small></label><label class="toggle-line"><input type="checkbox" name="shareFeedback" ${disabled}>我已知晓并同意将本次正式评语匿名分享给本队（每次提交需确认）</label><p class="form-error" role="alert"></p><div class="form-actions"><button type="submit" class="btn" ${disabled}>正式提交评分 ↗</button><button type="button" class="btn secondary" id="save-draft" ${saved?.status==='submitted'?'disabled':disabled}>保存草稿</button><button type="button" class="btn secondary" id="recuse" ${disabled}>回避此队</button></div><div id="score-conflict" aria-live="polite"></div></form>`;
  const form=document.getElementById('score-form');
  let saving=false,blocked=false,revision=saved?.revision ?? 0,currentStatus=saved?.status,editRevision=0,previewRequest=0;
  const alive=options=>version===routeVersion && route==='judge' && form.isConnected && judgeData===data && String(selectedTeam)===String(team.id) && editorHasLogin(form,owner,options);
  const buttons=[...form.querySelectorAll('button')];
  function updateButtons(){buttons.forEach(button=>button.disabled=saving || blocked || !data.scoringOpen);document.getElementById('save-draft').disabled=saving || blocked || !data.scoringOpen || currentStatus==='submitted';}
  form.addEventListener('input',()=>{editRevision++;scoreDirty=true;});
  function updateTotal(){let total=0;form.querySelectorAll('[data-score]').forEach(input=>{total+=Number(input.value || 0);});document.getElementById('score-total').innerHTML=`${total.toFixed(1)} <small>/ 100</small>`;}
  form.querySelectorAll('[data-slider]').forEach(slider=>slider.oninput=()=>{form.elements[`score${slider.dataset.slider}`].value=slider.value;updateTotal();});
  form.querySelectorAll('[data-score]').forEach(input=>input.oninput=()=>{form.querySelector(`[data-slider="${input.dataset.score}"]`).value=input.value;updateTotal();});updateTotal();
  function conflict(message='服务端评分已更新。'){
    blocked=true;scoreDirty=true;const panel=document.getElementById('score-conflict');
    panel.innerHTML='<section class="info-banner warning"><strong>当前输入已保留</strong><p>'+esc(message)+'先查看最新评分并与上方当前输入核对，复制需要保留的内容，再载入最新记录重新填写。</p><button class="btn secondary small" id="preview-score" type="button">查看服务器最新评分</button></section>';
    document.getElementById('preview-score').onclick=async()=>{
      if(!alive({notify:true}))return;
      const request=++previewRequest;
      try{
        const latest=await api('judge',undefined,{loginId:owner.loginId});if(!alive() || request!==previewRequest)return;
        const record=latest.myScores.find(item=>String(item.teamId)===String(team.id));
        if(record && (!Number.isSafeInteger(record.revision) || record.revision<1))throw new Error('无法读取评分版本，请刷新页面后重试。');
        panel.innerHTML='<section class="info-banner warning"><strong>当前输入已保留 · 服务器最新评分</strong><p>请与上方当前输入核对；需要保留的内容请先复制。</p>'+`<p>版本 ${record?.revision ?? 0} · ${esc({draft:'草稿',submitted:'正式提交',recused:'已回避'}[record?.status] || '尚无评分')}</p><dl>`+dimensions.map((dimension,i)=>`<dt>${esc(dimension.name)}</dt><dd>${Number(record?.scores?.[i] ?? 0)} / ${dimension.max}</dd>`).join('')+`</dl><p class="preserve-text">${esc(record?.feedback || '（无评语）')}</p><button class="btn secondary small" id="load-score" type="button">载入最新记录并替换当前输入</button></section>`;
        document.getElementById('load-score').onclick=()=>{
          if(!alive({notify:true}) || !confirm('将用上面显示的服务器最新记录替换本队当前输入。请先复制需要保留的内容，确认载入？'))return;
          data.myScores=data.myScores.filter(item=>String(item.teamId)!==String(team.id));if(record)data.myScores.push(record);
          data.scoringOpen=latest.scoringOpen;scoreDirty=false;updateScoreSummary(data);renderTeamPicker();renderJudgeEditor();
        };
      }catch(error){if(request===previewRequest && alive({error}))formError(form,error.message);}
    };
  }
  async function save(status){
    if(!alive({notify:true}) || saving || blocked || !data.scoringOpen || (status==='draft' && currentStatus==='submitted'))return;
    if(status!=='recused' && !form.reportValidity())return;
    if(status==='submitted' && form.elements.feedback.value.trim() && !form.elements.shareFeedback?.checked){formError(form,'请确认将本次正式评语匿名分享给本队后再提交。');return;}
    const payload={teamId:team.id,scores:dimensions.map((_,i)=>Number(form.elements[`score${i}`].value)),feedback:form.elements.feedback.value,status,expectedRevision:revision};
    if(status==='submitted' && form.elements.shareFeedback){
      if(form.elements.shareFeedback.checked)payload.feedbackSharing='team-anonymous-v1';
      // Confirmation belongs to this dispatched revision, including while input remains editable.
      form.elements.shareFeedback.checked=false;
    }
    const sentEditRevision=editRevision;saving=true;updateButtons();formError(form,'');
    try{
      const result=await api('score',payload,{loginId:owner.loginId});if(!alive())return;
      const record=result.score;
      if(!record || !Number.isSafeInteger(record.revision) || record.revision<1){conflict();formError(form,'保存结果缺少版本，请查看服务器最新评分核对。');return;}
      // Keep edits made during the request; adopt only this POST's authoritative revision.
      revision=record.revision;currentStatus=record.status;
      data.myScores=data.myScores.filter(item=>String(item.teamId)!==String(team.id));data.myScores.push(record);
      const unchanged=editRevision===sentEditRevision;scoreDirty=!unchanged;
      if(unchanged){
        dimensions.forEach((_,i)=>{const value=String(record.scores?.[i] ?? 0);form.elements[`score${i}`].value=value;form.querySelector(`[data-slider="${i}"]`).value=value;});
        form.elements.feedback.value=record.feedback || '';updateTotal();
      }
      const statusNode=document.getElementById('score-status');if(statusNode)statusNode.textContent={submitted:'已正式提交，可修改后重新提交',draft:'草稿已保存，尚未计入成绩',recused:'已回避，可重新评分'}[record.status];
      updateScoreSummary(data);
      renderTeamPicker();toast(unchanged?(status==='submitted'?'评分已正式提交。':status==='draft'?'草稿已保存，尚未计入成绩。':'已记录回避，不计入成绩。'):'已保存提交时的评分，之后的新输入已保留，请核对后再次提交。');
    }catch(error){
      if(!alive({error}))return;
      formError(form,error.status===428?'页面版本已过期，请先复制当前输入，再刷新页面升级。':error.message);
      if(error.status===409 && error.code==='score_revision_conflict')conflict();
      else if(error.status===409){blocked=true;scoreDirty=true;formError(form,error.message+'。当前输入已保留，请先复制，再刷新页面核对。');}
      if(error.status===428 || error.status===403)blocked=true;
    }finally{saving=false;if(alive())updateButtons();}
  }
  form.onsubmit=event=>{event.preventDefault();return save('submitted');};
  document.getElementById('save-draft').onclick=()=>save('draft');
  document.getElementById('recuse').onclick=()=>{if(confirm('确定回避此队伍？你对该队的评分将不计入专业均分。评分开放期间可重新评分。'))return save('recused');};
}
