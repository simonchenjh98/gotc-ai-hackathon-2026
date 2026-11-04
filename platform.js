'use strict';
const root = document.getElementById('platform-main');
const base = new URL('./', location.href);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const teamLabel = t => `${String(t.number).padStart(2, '0')} · ${t.name}`;
let screenStarted=0, submissionDirty=false, reviewDirty=false, scoreDirty=false, restoreSubmissionRoute=false;
let adminStage='exhibition',adminJudgingVersion=0,adminEventData=null,adminLoadPending=null,adminReadOnlyView=null;
const selectedTeams={},dirtyAdminForms=new Set();
const stageForRoute=()=>route.endsWith('-roadshow')?'roadshow':'exhibition';
const stageName=stage=>stage==='roadshow'?'路演评审':'展示评审';
const stageNotice=stage=>stage==='roadshow'?'<div class="info-banner warning"><strong>组织方人工工作名单</strong><br>晋级算法尚未实现，本页名单不代表正式晋级。展示与路演的名单、材料和评语分别保存。</div>':'';
let logoutInFlight=null;
let session={role:null}, route='', pollTimer, routeVersion=0, lastBoard, judgeData, selectedTeam, toastTimer;
const roleName = {team:'参赛队伍',judge:'专业评委',audience:'观众',admin:'组织方'};
function toast(message) { const el = document.getElementById('platform-toast'); el.textContent = message; el.classList.add('visible'); clearTimeout(toastTimer); toastTimer = setTimeout(() => el.classList.remove('visible'), 4500); }
const loginOwnerKey=`gotc-login-owner:v1:${encodeURIComponent(base.pathname)}`;
// A server-verified cookie may outlive a failed/quota-blocked owner publication.
// Keep the request-time marker snapshot; a later login publication still invalidates it.
let verifiedBrowserLogin=null;
function browserLoginOwner(){try{return localStorage.getItem(loginOwnerKey);}catch{return undefined;}}
function publishLogin(login){if(!login.loginId)return;try{localStorage.setItem(loginOwnerKey,login.loginId);}catch{/* Server saving remains available without storage. */}}
function ownsBrowserLogin(login){const owner=browserLoginOwner();return !owner || !login.loginId || owner===login.loginId || (verifiedBrowserLogin?.loginId===login.loginId && verifiedBrowserLogin.owner===owner);}
async function api(path, data, options={}) {
  const recoveryAtStart=currentTeamRecovery,sessionAtStart=session,ownerAtStart=path==='session'?browserLoginOwner():null;
  const res = await fetch(new URL(`api/${path}`, base), {credentials:'same-origin',cache:'no-store',method:data === undefined ? 'GET' : 'POST',headers:{...(data===undefined?{}:{'Content-Type':'application/json'}),...((['team','submission','logout','judge','score','admin','admin/settings'].includes(path) || path.startsWith('submission/')) && (options.loginId ?? session.loginId)?{'X-GOTC-Login':options.loginId ?? session.loginId}:{})},body:data === undefined ? undefined : JSON.stringify(data),signal:options.signal});
  let body; try { body = await res.json(); } catch { throw new Error('服务暂时不可用，请稍后重试。'); }
  if (!res.ok) { if((res.status===401 || res.status===403) && recoveryAtStart && currentTeamRecovery===recoveryAtStart)recoveryAtStart.lock();if(res.status===401 && session===sessionAtStart && (!recoveryAtStart || currentTeamRecovery===recoveryAtStart))session={role:null}; const error = new Error(body.error || '操作未成功，请重试。'); error.status = res.status; error.code=body.code; throw error; }
  if(path==='session')verifiedBrowserLogin={loginId:body.loginId,owner:ownerAtStart};
  return body;
}
function trackAdminForm(form){form.addEventListener('input',()=>{form.editRevision=(form.editRevision || 0)+1;dirtyAdminForms.add(form);});}
function stageHasUnsavedInputs(){return [...dirtyAdminForms].some(form=>form.isConnected && form.closest('#judging-admin'));}
function clearStageUnsavedInputs(){for(const form of dirtyAdminForms)if(!form.isConnected || form.closest('#judging-admin'))dirtyAdminForms.delete(form);}
function confirmLeave(){return !(submissionDirty || reviewDirty || scoreDirty || dirtyAdminForms.size) || confirm(submissionDirty?'有尚未保存服务器的作品输入。离开不会自动提交；只有成功保存的本机副本可在重新登录本队后找回，未备份输入可能丢失。确认离开？':'有尚未保存的输入，确认离开并放弃这些输入？');}
function account(){
  const el=document.getElementById('account');el.innerHTML=session.role?`<span>${esc(session.name || roleName[session.role])}</span><button id="logout">退出</button>`:'';
  document.getElementById('logout')?.addEventListener('click',logoutAccount);
}
async function logoutAccount(event){
    if(logoutInFlight?.owner===session)return;
    const operation={owner:session,context:currentTeamRecovery},button=event?.currentTarget || document.getElementById('logout');logoutInFlight=operation;button.disabled=true;
    const owned=()=>session===operation.owner;
    try{let local=null,teamAtExit=null;
      if(session.role==='team'){teamAtExit=await api('team');if(!owned())return;if(currentTeamRecovery && currentTeamRecovery.teamId!==teamAtExit.team.id)currentTeamRecovery.lock('当前登录队伍已变化，本地副本需重新授权后查看。');const store=teamDraftStore(teamAtExit,undefined,teamAtExit.loginId ?? operation.owner.loginId ?? '');const captured=store?.capturePurge();local={purge:()=>store?.purge(captured) ?? false};}
      if(teamAtExit?!confirm(`退出当前登录的${teamAtExit.team.name}会清除本浏览器内本队当前及历史恢复副本，并停止旧标签编辑；退出期间重新登录后新建的副本会保留；服务器材料不受影响。确认退出？`):!confirmLeave())return;
      await api('logout',{}, {loginId:teamAtExit?.loginId ?? operation.owner.loginId});let cleaned=true;if(local && (owned() || operation.owner.loginId))cleaned=local.purge();if(!owned())return;submissionDirty=false;reviewDirty=false;scoreDirty=false;dirtyAdminForms.clear();currentTeamRecovery?.lock('已退出。');session={role:null};await renderRoute();if(!cleaned)toast('已退出，但无法确认本队本地副本已清理，请在共享设备上清除此站点的浏览器数据。');
    }catch(error){if(owned() || (logoutInFlight===operation && !session.role))toast('退出未确认，本地副本已保留。'+error.message);}
    finally{if(logoutInFlight===operation)logoutInFlight=null;if(button.isConnected)button.disabled=false;}
}

function heading(kicker,title,description,tools='') { return `<div class="page-heading"><div><p class="eyebrow">${kicker}</p><h1>${title}</h1><p>${description}</p></div>${tools ? `<div class="heading-tools">${tools}</div>` : ''}</div>`; }
function empty(title,description,action='') { return `<div class="empty-state"><span class="empty-symbol" aria-hidden="true">▦</span><h2>${title}</h2><p>${description}</p>${action}</div>`; }
function metrics(items) { return `<div class="metrics">${items.map(i=>`<div class="metric"><strong${i[2]?` id="${esc(i[2])}"`:''}>${esc(i[0])}</strong><span>${esc(i[1])}</span></div>`).join('')}</div>`; }
function formError(form,error) { form.querySelector('.form-error').textContent = error; }
const settingsGroups={
  scoring:[['scoringOpen','开放专业评分'],['resultsPublished','公开专业均分'],['minJudges','公开均分所需评委数']],
  submissions:[['submissionsOpen','开放作品提交'],['submissionDeadline','提交截止时间（北京时间）']],
  voting:[['votingOpen','开放观众投票'],['votingDeadline','投票截止时间（北京时间）']]
};
function settingsValue(key,value){return key.endsWith('Open') || key==='resultsPublished'?(value?'开放':'关闭'):key.endsWith('Deadline')?beijingTime(value):String(value);}
// Keep an expired editor readable for copying. Late responses cannot update a new login.
function editorHasLogin(form,owner,{notify=false,error}={}){
  const sameBrowser=ownsBrowserLogin(owner);
  if(session===owner && sameBrowser)return true;
  if(notify || (error?.status===401 && !session.role && sameBrowser)){
    formError(form,'当前登录已失效或已切换。请先复制要保留的输入，再刷新页面并核对登录身份。');
  }
  return false;
}
function installSettingsForm(form,group,config){
  const owner=session,version=routeVersion,fields=settingsGroups[group];
  let revision=config.writeRevisions?.[group],blocked=false,saving=false,previewRequest=0;
  const alive=options=>version===routeVersion && route==='admin' && form.isConnected && editorHasLogin(form,owner,options);
  form.wouldLoseSettings=latest=>saving || latest?.writeRevisions?.[group]<revision;
  const validRevision=value=>Number.isSafeInteger(value) && value>=0;
  let button;
  function payload(){return Object.fromEntries(fields.map(([key])=>{const input=form.elements[key];return [key,key.endsWith('Open') || key==='resultsPublished'?input.checked:key.endsWith('Deadline')?parseBeijingInput(input.value):Number(input.value)];}));}
  function conflict(){
    blocked=true;button.disabled=true;dirtyAdminForms.add(form);
    if(!form.querySelector('[data-settings-conflict]'))form.insertAdjacentHTML('beforeend','<div data-settings-conflict aria-live="polite"></div>');
    const panel=form.querySelector('[data-settings-conflict]');
    panel.innerHTML='<div class="info-banner warning"><strong>当前输入已保留</strong><p>服务端设置已更新。请先查看最新值并与上方输入核对，复制需要保留的内容，再载入最新值重新填写。</p><button class="btn secondary small" type="button" data-settings-preview>查看服务器最新设置</button></div>';
    form.querySelector('[data-settings-preview]').onclick=async()=>{
      if(!alive({notify:true}))return;
      const request=++previewRequest;
      try{
        const latest=(await api('admin',undefined,{loginId:owner.loginId})).config;
        if(!alive() || request!==previewRequest)return;
        if(!validRevision(latest?.writeRevisions?.[group]))throw new Error('无法读取设置版本，请刷新页面后重试。');
        panel.innerHTML='<div class="info-banner warning"><strong>当前输入已保留 · 服务器最新设置</strong><p>请与上方当前输入核对；需要保留的内容请先复制。</p><dl>'+fields.map(([key,label])=>`<dt>${esc(label)}</dt><dd>${esc(settingsValue(key,latest[key]))}</dd>`).join('')+`</dl><p>设置版本 ${latest.writeRevisions[group]}</p><button class="btn secondary small" type="button" data-settings-load>载入最新设置并替换当前输入</button></div>`;
        form.querySelector('[data-settings-load]').onclick=()=>{
          if(!alive({notify:true}) || !confirm('将用上面显示的服务器最新设置替换本组当前输入。请先复制需要保留的内容，确认载入？'))return;
          for(const [key] of fields){const input=form.elements[key];if(key.endsWith('Open') || key==='resultsPublished')input.checked=latest[key];else input.value=key.endsWith('Deadline')?beijingInput(latest[key]):String(latest[key]);}
          revision=latest.writeRevisions[group];blocked=false;button.disabled=false;form.editRevision=(form.editRevision || 0)+1;dirtyAdminForms.delete(form);panel.innerHTML='';formError(form,'');
        };
      }catch(error){if(request===previewRequest && alive({error}))formError(form,error.message);}
    };
  }
  form.onsubmit=async event=>{
    event.preventDefault();if(!alive({notify:true}) || saving || blocked)return;
    button=form.querySelector('[type=submit]') || form.querySelector('button');
    if(!validRevision(revision)){formError(form,'缺少设置版本，请刷新页面升级后再保存。当前输入已保留。');return;}
    let values;try{values=payload();}catch(error){formError(form,error.message);return;}
    const editRevision=form.editRevision || 0;
    saving=true;button.disabled=true;
    formError(form,'');
    try{
      const result=await api('admin/settings',{...values,expectedRevisions:{[group]:revision}},{loginId:owner.loginId});
      if(!alive())return;
      const savedRevision=result.config?.writeRevisions?.[group];
      if(!validRevision(savedRevision)){conflict();formError(form,'保存结果缺少版本，请查看服务器最新设置核对。');return;}
      // Only this POST may advance this form's baseline. Directory/poll reads do not.
      revision=savedRevision;
      const unchanged=(form.editRevision || 0)===editRevision;
      if(unchanged)dirtyAdminForms.delete(form);
      toast(unchanged?'设置已保存。':'设置已保存。保存期间的新输入已保留，请核对后再次保存。');
    }catch(error){
      if(!alive({error}))return;
      formError(form,error.status===428?'页面版本已过期，请先复制当前输入，再刷新页面升级。':error.message);
      if(error.status===409 && error.code==='settings_revision_conflict')conflict();
      else if(error.status===409){blocked=true;dirtyAdminForms.add(form);formError(form,error.message+'。当前输入已保留，请先复制，再刷新页面核对。');}
      if(error.status===428)blocked=true;
    }finally{saving=false;if(alive())button.disabled=blocked;}

  };
}
async function withBusy(button, fn) { if (!button || button.disabled) return; button.disabled = true; try { await fn(); } catch(e) { toast(e.message); } finally { if (button.isConnected) button.disabled = false; } }
function showConnectionError(error) { root.innerHTML = heading('CONNECTION / 连接状态','暂时无法连接平台',esc(error.message)) + empty('连接暂时中断','请检查网络，稍后重试。尚未提交的操作不会显示为成功。','<button class="btn" id="retry">重新连接</button>'); document.getElementById('retry').onclick = renderRoute; }

function boardMarkup(data) {
  const teams = [...(data.teams || [])].sort((a,b) => (a.rank ?? Infinity) - (b.rank ?? Infinity) || Number(a.number) - Number(b.number)), cfg = data.config || {};
  const ready = teams.filter(t=>t.score !== null && t.score !== undefined).length;
  const count = teams.reduce((n,t)=>n + Number(t.judgeCount || 0),0);
  const votes = teams.reduce((n,t)=>n + Number(t.audienceVotes || 0),0);
  const screen = document.body.classList.contains('screen-mode');
  const pageCount = Math.max(1, Math.ceil(teams.length / 8));
  const page = screen ? Math.floor((Date.now() - screenStarted) / 12000) % pageCount : 0;
  const visibleTeams = screen ? teams.slice(page * 8, page * 8 + 8) : teams;
  const table = teams.length ? `<div class="table-wrap"><table class="board-table"><thead><tr><th scope="col">排名</th><th scope="col">队伍 / 作品</th><th scope="col" class="track-col">探索方向</th><th scope="col">专业均分</th><th scope="col">有效评审</th><th scope="col">观众票</th></tr></thead><tbody>${visibleTeams.map(t=>`<tr class="${t.rank && t.rank <= 3 ? 'top-rank' : ''}"><td class="rank">${t.rank ? String(t.rank).padStart(2,'0') : '—'}</td><td><div class="team-name"><span class="team-number">${String(t.number).padStart(2,'0')}</span>${esc(t.name)}<small>${esc(t.project || '作品名称待完善')}</small></div></td><td class="track-col">${esc(t.track || '—')}</td><td class="board-score">${t.score !== null && t.score !== undefined ? Number(t.score).toFixed(2) : `<span class="board-pending">${cfg.resultsPublished ? '等待评审' : '暂未发布'}</span>`}</td><td>${Number(t.judgeCount || 0)} <span class="board-pending">/ ${Number(cfg.minJudges || 5)}</span></td><td>${Number(t.audienceVotes || 0)}</td></tr>`).join('')}</tbody></table></div>` : empty('每一个想法，都将从这里出现','本届队伍尚未录入。主办方添加队伍后，看板会自动更新；当前没有正式成绩。');
  return metrics([[teams.length,'本届参赛队伍'],[count,'已提交专业评分'],[ready,'已显示专业均分'],[votes,'观众选择票数']]) + `<div class="info-banner">专业评分满分 100 分 · 每队至少 ${Number(cfg.minJudges || 5)} 位有效评委后显示均分 · 观众票单独展示，不计入专业分。${cfg.scoringOpen ? ' 当前评分已开放。' : ' 当前评分尚未开放或已截止。'}</div>` + table + `<div class="board-foot"><span>${screen ? `第 ${page + 1} / ${pageCount} 屏 · 每 12 秒轮播 · ` : ''}相同分数并列排名，草稿与回避不计入。</span><span id="board-updated">北京时间 ${esc(new Date(data.serverTime || Date.now()).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false}))}</span></div>`;
}
function rulePlaceholders(stage){
  const dimensions=stage.dimensions || [];
  return `<section class="surface rule-placeholders"><div class="section-caption"><h2>计分规则</h2><span class="status-chip closed">待配置 · 尚未实现</span></div><p class="small-note">本阶段可保存、正式提交评审记录；本阶段的数值评分、汇总、排名、晋级与成绩发布尚未实现。专业评分及成绩看板可从平台导航进入。</p><div class="rule-grid">${dimensions.map(d=>`<div><span>${esc(d.name)}</span><strong>待配置</strong></div>`).join('')}<div><span>有效评委数门槛</span><strong>待配置</strong></div><div><span>汇总与晋级规则</span><strong>待配置</strong></div></div></section>`;
}
function progressBoardMarkup(data){
  const teams=[...(data.teams || [])].sort((a,b)=>Number(a.number)-Number(b.number));
  const screen=document.body.classList.contains('screen-mode');
  const pageCount=Math.max(1,Math.ceil(teams.length/8));
  const page=screen?Math.floor((Date.now()-screenStarted)/12000)%pageCount:0;
  const visible=screen?teams.slice(page*8,page*8+8):teams;
  const stage=data.stage?.id || 'exhibition',name=stageName(stage);
  return stageNotice(stage)+metrics([[teams.length,stage==='roadshow'?'本轮路演队伍':'本轮展示队伍'],[teams.reduce((n,t)=>n+Number(t.reviewCount || 0),0),'正式评审记录（未计分）'],['待配置','数值评分规则'],['未实现','排名与成绩发布']])+`<div class="info-banner warning"><strong>${name}进度 · 不是成绩榜</strong><br>当前仅展示正式评审记录数量。分值、汇总、排名与晋级规则待配置，成绩发布尚未实现。${data.window?.canEdit?'本轮评审记录开放中。':'本轮评审记录尚未开放或已截止。'}</div>`+(teams.length?`<div class="table-wrap"><table class="board-table"><thead><tr><th scope="col">队伍 / 作品</th><th scope="col" class="track-col">探索方向</th><th scope="col">正式评审记录</th><th scope="col">成绩 / 排名</th></tr></thead><tbody>${visible.map(t=>`<tr><td><div class="team-name"><span class="team-number">${String(t.number).padStart(2,'0')}</span>${esc(t.name)}<small>${esc(t.project || '作品名称待完善')}</small></div></td><td class="track-col">${esc(t.track || '—')}</td><td>${Number(t.reviewCount || 0)} <span class="board-pending">份 · 未计分</span></td><td><span class="board-pending">规则待配置 / 未实现</span></td></tr>`).join('')}</tbody></table></div>`:empty(`本轮还没有${stage==='roadshow'?'路演':'展示'}队伍`,'组织方配置本轮工作名单后，评审进度会出现在这里。'))+`<div class="board-foot"><span>${screen?`第 ${page+1} / ${pageCount} 屏 · 每 12 秒轮播 · `:''}草稿与回避不计入正式评审记录数。</span><span>北京时间 ${esc(beijingTime(data.window?.serverTime || Date.now()))}</span></div>`;
}
async function loadExhibitionBoard(version,stage=stageForRoute()){
  const data=await api(`judging/${stage}/board`);if(version!==routeVersion)return;
  lastBoard=data;document.getElementById('board-content').innerHTML=progressBoardMarkup(data);
  const status=document.getElementById('live-status');status.classList.remove('offline');status.innerHTML='<span class="live-dot"></span>已连接 · 每 2 秒更新';
}
async function renderExhibitionBoard(version){
  const stage=stageForRoute(),name=stageName(stage);
  root.innerHTML=heading(`LIVE / ${name}进度`,`${name}进度`,'查看本轮正式评审记录数量。计分与成绩发布尚未实现。','<span class="live-indicator" id="live-status"><span class="live-dot"></span>连接中…</span><button class="btn secondary small" id="screen-toggle">大屏模式 ↗</button>')+'<div id="board-content"><div class="loading-state">正在读取本轮评审进度…</div></div>';
  document.getElementById('screen-toggle').onclick=()=>{document.body.classList.toggle('screen-mode');screenStarted=Date.now();if(lastBoard)document.getElementById('board-content').innerHTML=progressBoardMarkup(lastBoard);document.getElementById('screen-toggle').textContent=document.body.classList.contains('screen-mode')?'退出大屏模式':'大屏模式 ↗';};
  async function tick(){try{await loadExhibitionBoard(version,stage);}catch(e){if(version!==routeVersion)return;const status=document.getElementById('live-status');status.classList.add('offline');status.innerHTML='<span class="live-dot"></span>连接中断 · 正在重试';if(!lastBoard)document.getElementById('board-content').innerHTML=empty('暂时无法读取进度',esc(e.message));}finally{if(version===routeVersion)pollTimer=setTimeout(tick,2000);}}
  await tick();
}
async function loadBoard(version) {
  const data = await api('board'); if (version !== routeVersion) return;
  lastBoard = data;
  document.getElementById('board-content').innerHTML = boardMarkup(data);
  const status = document.getElementById('live-status'); status.classList.remove('offline'); status.innerHTML = '<span class="live-dot"></span>已连接 · 每 2 秒更新';
}
async function renderBoard(version) {
  root.innerHTML = heading('LIVE / GOTC 2026','每一格，都在发生。','百格成城 · 实时成绩看板', '<span class="live-indicator" id="live-status"><span class="live-dot"></span>连接中…</span><button class="btn secondary small" id="screen-toggle">大屏模式 ↗</button>') + '<div id="board-content"><div class="loading-state">正在读取本届活动数据…</div></div>';
  document.getElementById('screen-toggle').onclick = () => { document.body.classList.toggle('screen-mode'); screenStarted = Date.now(); if(lastBoard)document.getElementById('board-content').innerHTML=boardMarkup(lastBoard); document.getElementById('screen-toggle').textContent = document.body.classList.contains('screen-mode') ? '退出大屏模式' : '大屏模式 ↗'; };
  async function tick() { try { await loadBoard(version); } catch(e) { if(version !== routeVersion)return; const status=document.getElementById('live-status'); status.classList.add('offline');status.innerHTML='<span class="live-dot"></span>连接中断 · 正在重试';if(!lastBoard)document.getElementById('board-content').innerHTML=empty('暂时无法读取数据',esc(e.message)); } finally { if(version === routeVersion)pollTimer=setTimeout(tick,2000); } }
  await tick();
}
function renderLogin(role) {
  const stage=stageForRoute();
  const copy = {team:['把作品准备好，<br>把创造的过程留下来。','使用主办方为本队提供的独立提交码。草稿仅本队和组织方可见，正式提交后评委才能查看材料。'],judge:route==='judge'?['专业评分<br>六维 100 分','使用主办方提供的评委码，选择自己的身份。评分可保存草稿，正式提交后计入专业均分。']:[`${stageName(stage)}记录`,'使用主办方提供的评委码，选择自己的身份。可查看本轮队伍、保存草稿、提交评语或回避；本阶段记录暂不计分。'],audience:['选出让你<br>眼前一亮的作品。','观众选择票独立于专业成绩。使用观众邀请码登录，每个邀请码最多支持 3 支不同队伍。'],admin:['组织方<br>活动管理','管理队伍、评委、评审窗口和作品材料。请使用主办方管理码登录。']}[role];
  root.innerHTML = `<div class="login-layout"><div class="login-story"><p class="eyebrow">GOTC 2026 / ${role.toUpperCase()}</p><h1>${copy[0]}</h1><p>${copy[1]}</p><span class="login-pixel" aria-hidden="true"></span></div><form class="login-card" id="login-form"><h2>${roleName[role]}登录</h2><p>邀请码仅用于本届活动，请妥善保管。<br>如未收到或无法登录，请联系现场主办方。</p><label class="field">${role === 'admin' ? '组织方管理码' : (role === 'judge' ? '评委登录码' : role === 'team' ? '队伍提交码' : '观众邀请码')}<input name="code" type="password" autocomplete="one-time-code" required maxlength="160" placeholder="输入你的${role === 'admin' ? '管理码' : '邀请码'}"></label><p class="form-error" role="alert"></p><button type="submit" class="btn">进入${roleName[role]}工作台 ↗</button><p class="form-footnote">公众查看成绩和评审进度无需登录。<a href="${route==='judge'?'#board':stage==='roadshow'?'#board-roadshow':'#board-exhibition'}">查看公开看板 →</a></p></form></div>`;
  const form=document.getElementById('login-form');
  const identityField=document.createElement('label');
  identityField.className='field';identityField.hidden=true;
  identityField.innerHTML='选择自己的评委身份<select name="accountId"><option value="">请选择主办方分配的身份</option></select>';
  form.querySelector('.form-error').before(identityField);
  const identity=form.elements.accountId;
  form.elements.code.addEventListener('input',()=>{identityField.hidden=true;identity.required=false;identity.innerHTML='<option value="">请选择主办方分配的身份</option>';});
  form.onsubmit=async e=>{
    e.preventDefault();const button=form.querySelector('button');button.disabled=true;formError(form,'');
    try {
      const payload={role,code:form.elements.code.value.trim()};
      if(!identityField.hidden && identity.value)payload.accountId=identity.value;
      const result=await api('login',payload);
      if(result.selectAccount){
        identity.innerHTML='<option value="">请选择主办方分配的身份</option>'+result.accounts.map(a=>`<option value="${esc(a.id)}">${esc(a.name)}</option>`).join('');
        identityField.hidden=false;identity.required=true;identity.focus();
        formError(form,'登录码正确，请选择你自己的评委身份后继续。');
        return;
      }
      publishLogin(result);session=await api('session');await renderRoute();
    }catch(error){formError(form,error.message);}finally{if(button.isConnected)button.disabled=false;}
  };
}
function renderWrongRole(role) { root.innerHTML = heading('ACCESS / 登录身份','请使用对应的邀请码',`当前身份为${esc(roleName[session.role] || session.role)}，此入口需要${roleName[role]}身份。`) + empty('切换登录身份','退出当前账号后，使用此入口的专属邀请码登录。','<button class="btn" id="switch-role">退出并切换</button>');document.getElementById('switch-role').onclick=logoutAccount; }
function reviewState(record){return record?.invalidated?'已作废':record?.recused?'已回避':record?.submitted?(record.hasUnsubmittedChanges?'已正式提交 · 有草稿修改':'已正式提交（未计分）'):record?.draft?'草稿已保存':'待评审';}
function reviewMaterialMarkup(snapshot,stage=null){return `<section class="surface submitted-materials"><h2>本轮正式材料快照</h2>${stage?`<p class="small-note">轮次 ${esc(stage.runId)} · 冻结时间 ${esc(beijingTime(stage.frozenAt))}（北京时间）</p>`:''}${snapshot?`<p class="small-note">队伍提交版本 ${Number(snapshot.submissionVersion)} · 正式提交 ${esc(beijingTime(snapshot.submittedAt))}<br>本轮截取 ${esc(beijingTime(snapshot.capturedAt))}（北京时间）</p>${materialMarkup(snapshot.materials)}`:!stage?'<p class="small-note">本轮没有正式材料快照。</p>':stage.frozenAt===null?'<p class="small-note">尚未生成材料快照；首次开放时截取当时的正式材料。</p>':'<p class="small-note">本轮冻结时无正式材料；可根据现场演示记录观察。后续提交材料不会自动替换本轮快照。</p>'}</section>`;}
async function renderExhibitionJudge(version,expectedForm=null,stage=stageForRoute()){
  let data;
  const stale=()=>version!==routeVersion || stage!==stageForRoute() || (expectedForm && !expectedForm.isConnected);
  try{data=await api(`judging/${stage}`);}catch(error){if(stale())return;if(error.status===403){root.innerHTML=heading(`JUDGE / ${stageName(stage)}`,'本轮尚未获得评审授权',`请联系组织方，将你的评委身份加入${stageName(stage)}工作名单。`)+stageNotice(stage)+empty('尚未分配本轮队伍','获得授权后，可用当前身份重新读取。','<button class="btn secondary" id="retry-judge">重新读取</button>');document.getElementById('retry-judge').onclick=()=>renderExhibitionJudge(version,null,stage);return;}throw error;}
  if(stale())return;
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
  const stage=judgeData.stage.id || 'exhibition',viewVersion=routeVersion,viewRoute=route,runId=judgeData.stage.runId;
  const team=judgeData.teams.find(t=>String(t.id)===String(selectedTeam));
  const record=judgeData.myReviews.find(r=>String(r.teamId)===String(selectedTeam));
  const canEdit=judgeData.window.canEdit;
  const feedback=record?.draft?.feedback ?? record?.submitted?.feedback ?? '';
  document.getElementById('judge-editor').innerHTML=reviewMaterialMarkup(team.materialSnapshot,judgeData.stage)+`<form class="judge-form" id="review-form"><span class="eyebrow">TEAM ${String(team.number).padStart(2,'0')}</span><h2>${esc(team.name)}</h2><p class="project-line">${esc(team.project || '作品名称待完善')} · ${esc(team.track || '方向待完善')}</p><p class="status-chip">${reviewState(record)} · 编辑版本 ${Number(record?.version || 0)}</p><div class="info-banner">请填写观察或评语后正式提交。保存草稿不会覆盖已提交的记录。</div>${record?.invalidated?`<div class="info-banner warning"><strong>组织方已作废此记录</strong><br>原因：${esc(record.invalidatedReason || '未填写')}。继续修改仍会保留作废状态，恢复需联系组织方。</div>`:''}<fieldset ${canEdit?'':'disabled'}><label class="field">评审观察 / 给团队的评语<textarea name="feedback" maxlength="2000" rows="6" placeholder="写下作品表现、判断依据或改进建议。">${esc(feedback)}</textarea><small>最多 2000 字，仅评委本人及组织方可见。正式记录与草稿分别保存。</small></label><div class="form-actions"><button type="submit" class="btn">正式提交评语 ↗</button><button type="button" class="btn secondary" id="save-review-draft">保存草稿</button><button type="button" class="btn secondary" id="recuse-review">回避此队</button></div></fieldset><p class="form-error" role="alert"></p></form><div id="review-conflict" aria-live="polite"></div>${record?.submitted?`<section class="surface formal-review"><h2>最近正式提交的评审记录</h2><p class="small-note">${esc(beijingTime(record.submitted.submittedAt))}（北京时间） · 未计分${record.invalidated?' · 已作废':''}${record.recused?' · 当前已回避':''}</p><p class="preserve-text">${esc(record.submitted.feedback)}</p></section>`:''}`;
  const form=document.getElementById('review-form');let blocked=!canEdit;
  form.addEventListener('input',()=>{reviewDirty=true;});
  async function save(status){
    if(blocked)return;
    const value=form.elements.feedback.value.trim();
    formError(form,'');if(status==='submitted' && !value){formError(form,'请填写评审观察或评语后正式提交。');form.elements.feedback.focus();return;}
    if(Array.from(value).length>2000){formError(form,'评审记录最多 2000 字。');return;}
    const fieldset=form.querySelector('fieldset');fieldset.disabled=true;
    try{await api(`judging/${stage}/review`,{runId,teamId:team.id,expectedVersion:record?.version || 0,status,feedback:value,scores:null});if(route!==viewRoute || viewVersion!==routeVersion || stage!==stageForRoute() || !form.isConnected || String(selectedTeam)!==String(team.id))return;reviewDirty=false;toast(status==='submitted'?'评审记录已正式提交（未计分）。':status==='draft'?'草稿已保存，正式记录保持原版本。':'已回避此队，不计入正式评审记录数。');await renderExhibitionJudge(viewVersion,form,stage);}
    catch(error){if(!form.isConnected)return;formError(form,error.message);if(error.status===409){blocked=true;fieldset.disabled=false;form.elements.feedback.readOnly=true;form.querySelectorAll('button').forEach(b=>b.disabled=true);document.getElementById('review-conflict').innerHTML='<section class="surface"><h2>当前输入已保留</h2><p class="small-note">服务器版本或评审轮次已变化。请先复制需要保留的文字，再读取最新记录重新编辑。</p><button class="btn secondary" id="reload-review" type="button">读取最新记录</button></section>';document.getElementById('reload-review').onclick=()=>{if(!confirmLeave())return;reviewDirty=false;renderExhibitionJudge(viewVersion,form,stage);};}else if(error.status===401 || error.status===403){blocked=true;fieldset.disabled=true;}}
    finally{if(form.isConnected && !blocked)fieldset.disabled=false;}
  }
  form.onsubmit=e=>{e.preventDefault();save('submitted');};document.getElementById('save-review-draft').onclick=()=>save('draft');document.getElementById('recuse-review').onclick=()=>{if(confirm('确认回避此队？已有正式记录会保留，但不计入本轮正式评审记录数。'))save('recused');};
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
  document.getElementById('judge-editor').innerHTML=judgeSubmissionMarkup((judgeData.submissions || []).find(s=>s.teamId===team.id))+`<form class="judge-form" id="score-form"><span class="eyebrow">TEAM ${String(team.number).padStart(2,'0')}</span><h2>${esc(team.name)}</h2><p class="project-line">${esc(team.project || '作品名称待完善')} · ${esc(team.track || '方向待完善')}</p><p class="status-chip" id="score-status">${saved?({submitted:'已提交，可修改后重新提交；草稿不会覆盖正式评分',draft:'草稿已保存，尚未计入成绩',recused:'已回避，可在评分开放期间重新评分'}[saved.status]):'待评分'}</p>${dimensions.map((d,i)=>`<div class="dimension-row"><label for="score-${i}">${esc(d.name)}<small>满分 ${Number(d.max)} 分</small></label><input type="range" data-slider="${i}" aria-label="${esc(d.name)}滑块" min="0" max="${Number(d.max)}" step="0.1" value="${Number(saved?.scores?.[i] || 0)}" ${disabled}><input type="number" id="score-${i}" name="score${i}" data-score="${i}" aria-label="${esc(d.name)}分数" min="0" max="${Number(d.max)}" step="0.1" value="${Number(saved?.scores?.[i] || 0)}" required ${disabled}></div>`).join('')}<div class="score-preview"><span>专业评分总计</span><strong id="score-total">0.0 <small>/ 100</small></strong></div><label class="field">给团队的评语（选填）<textarea name="feedback" maxlength="2000" placeholder="记录你观察到的优点，以及一条具体的改进建议。" ${disabled}>${esc(saved?.feedback || '')}</textarea><small>评语仅供评委本人及组织方查看，不会出现在公开看板上。</small></label><p class="form-error" role="alert"></p><div class="form-actions"><button type="submit" class="btn" ${disabled}>正式提交评分 ↗</button><button type="button" class="btn secondary" id="save-draft" ${saved?.status==='submitted'?'disabled':disabled}>保存草稿</button><button type="button" class="btn secondary" id="recuse" ${disabled}>回避此队</button></div><div id="score-conflict" aria-live="polite"></div></form>`;
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
    const payload={teamId:team.id,scores:dimensions.map((_,i)=>Number(form.elements[`score${i}`].value)),feedback:form.elements.feedback.value,status,expectedRevision:revision};
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
async function renderAudience(version){const data=await api('audience');if(version!==routeVersion)return;const voted=(data.votedTeamIds || []).map(String),limit=data.voteLimit || 3;root.innerHTML=heading('AUDIENCE / 观众选择','把一票，投给打动你的作品。',`已支持 ${voted.length} / ${limit} 支队伍。观众票独立展示，不叠加到专业总分。`)+`<div class="info-banner ${data.votingOpen?'':'warning'}">${data.votingOpen?'请先看作品演示，再选择支持的队伍。每支队伍每个邀请码最多计 1 票，提交后不能撤回。':'当前观众投票尚未开放或已截止。'}</div>`+(data.teams.length?`<div class="vote-grid">${data.teams.map(t=>`<article class="vote-card ${voted.includes(String(t.id))?'selected':''}"><span class="eyebrow">TEAM ${String(t.number).padStart(2,'0')}</span><h2>${esc(t.name)}</h2><p>${esc(t.project || '作品名称待完善')}<br>${esc(t.track || '')}</p><button class="btn ${voted.includes(String(t.id))?'secondary':''}" data-vote="${esc(t.id)}" ${voted.includes(String(t.id))||voted.length>=limit||!data.votingOpen?'disabled':''}>${voted.includes(String(t.id))?'已支持 ✓':voted.length>=limit?'投票名额已用完':'支持这个作品 ↗'}</button></article>`).join('')}</div>`:empty('队伍即将出现在这里','主办方录入队伍后，你就可以了解并支持他们的作品。'));document.querySelectorAll('[data-vote]').forEach(b=>b.onclick=()=>withBusy(b,async()=>{await api('vote',{teamId:b.dataset.vote});toast('已支持这支队伍，谢谢你的参与。');if(route==='audience')await renderAudience(routeVersion);}));}

async function renderAdmin(version,expectedElement=null,discardedEdits=null){
  const owner=session,requestedStage=adminStage,request=++adminJudgingVersion;
  adminLoadPending=request;
  try{
  const [data,judging]=await Promise.all([api('admin'),api(`admin/judging/${requestedStage}`)]);
  if(session!==owner || !ownsBrowserLogin(owner) || route!=='admin' || version!==routeVersion || request!==adminJudgingVersion || requestedStage!==adminStage)return;
  if(expectedElement){
    if(!expectedElement.isConnected || [...dirtyAdminForms].some(form=>discardedEdits?.get(form)!==(form.editRevision || 0)))return;
    if([...root.querySelectorAll('form')].some(form=>form.wouldLoseSettings?.(data.config)))return;
  }
  if(typeof disposeAdminReviewSync==='function')disposeAdminReviewSync();adminReadOnlyView=null;
  adminEventData=data;dirtyAdminForms.clear();
  const cfg=data.config || {},teams=data.teams || [],judges=data.judges || [],audience=data.audience || [];
  root.innerHTML=heading('ORGANIZER / 活动控制台','活动管理','管理专业评分、成绩发布、参赛身份和独立的展示、路演评审记录。','<span class="small-note" id="admin-sync-status" role="status">所选阶段正式数与评审记录自动同步 · 每 3 秒检查</span><a class="btn secondary small" href="api/admin/export" download>导出评分明细 CSV ↓</a><button class="btn secondary small" id="refresh-admin">刷新数据</button>')+metrics([[teams.length,'本届队伍','admin-total-teams'],[judges.filter(j=>j.active).length,'有效专业评委'],[judges.reduce((n,j)=>n+Number(j.submittedCount || 0),0),'正式提交评分'],[audience.filter(a=>a.active).length,'有效观众邀请码','admin-active-audience']])+`<div class="admin-columns"><section class="surface"><h2>01 / 评分与发布</h2><form id="settings-form"><label class="toggle-line"><input type="checkbox" name="scoringOpen" ${cfg.scoringOpen?'checked':''}>开放专业评分</label><label class="toggle-line"><input type="checkbox" name="resultsPublished" ${cfg.resultsPublished?'checked':''}>公开达到门槛的专业均分</label><div class="settings-row"><label class="field">公开均分所需评委数<input name="minJudges" type="number" min="1" max="50" value="${Number(cfg.minJudges || 5)}" required><small>方案默认 5 人；修改将立即影响公开看板。</small></label><button class="btn small" type="submit">保存设置</button></div><p class="form-error" role="alert"></p></form><div class="info-banner">六维权重为 20 / 20 / 20 / 15 / 15 / 10，总分 100。评分关闭后草稿、提交与改分均锁定；成绩发布可单独控制。</div></section><section class="surface"><h2>发放身份邀请码</h2><form id="invite-form"><div class="form-grid"><label class="field">身份<select name="role"><option value="judge">专业评委</option><option value="audience">观众</option></select></label><label class="field">显示名称<input name="name" required maxlength="80" placeholder="例如：评委 01"></label></div><button class="btn small" type="submit">创建邀请码 ↗</button><p class="form-error" role="alert"></p></form><div id="new-code"></div><p class="small-note">专业评分使用评委身份即可；参与展示或路演评审记录还需加入对应阶段工作名单。统一评委码下，各评委仍选择各自身份。</p><div class="invite-list">${[...judges.map(j=>({...j,role:'judge'})),...audience.map(a=>({...a,role:'audience'}))].map(a=>`<div class="invite-row"><span>${esc(a.name)}<small>${roleName[a.role]} · ${a.active?'有效':'已停用'}</small></span>${a.active?`<button class="btn secondary small" data-revoke="${esc(a.id)}" data-role="${a.role}">停用</button>`:''}</div>`).join('') || '<p class="small-note">尚未创建身份。</p>'}</div></section></div><section class="surface"><h2>观众投票</h2><form id="audience-settings"><p class="small-note">正在读取独立投票窗口…</p></form></section><div class="admin-section-title"><h2>本届参赛队伍</h2><span class="small-note">维护队伍信息；加入各阶段工作名单后才进入对应评审。</span></div><section class="surface"><form id="team-form"><input type="hidden" name="id"><div class="form-grid"><label class="field">队伍编号<input name="number" type="number" min="1" max="999" required placeholder="例如：1"></label><label class="field">队伍名称<input name="name" required maxlength="80" placeholder="团队公开名称"></label><label class="field">作品名称<input name="project" maxlength="140" placeholder="可以稍后补充"></label><label class="field">探索方向<select name="track"><option>AI for Science</option><option>城市与社区问题</option><option>未来学习样态</option></select></label></div><div class="form-actions"><button class="btn small" type="submit" id="save-team">新增队伍 ↗</button><button class="btn secondary small" type="button" id="clear-team">清空表单</button></div><p class="form-error" role="alert"></p></form></section><div class="table-wrap admin-team-table"><table class="board-table"><thead><tr><th>编号</th><th>队伍 / 作品</th><th class="track-col">探索方向</th><th>操作</th></tr></thead><tbody>${teams.map(t=>`<tr><td>${String(t.number).padStart(2,'0')}</td><td><strong>${esc(t.name)}</strong><br><small>${esc(t.project || '待补充')}</small></td><td class="track-col">${esc(t.track || '—')}</td><td><button class="btn secondary small" data-edit="${esc(t.id)}">编辑</button></td></tr>`).join('') || '<tr><td colspan="4">尚未录入本届队伍。</td></tr>'}</tbody></table></div>`;
  document.getElementById('refresh-admin').onclick=event=>{
    if(!confirmLeave())return;
    const discarded=new Map([...dirtyAdminForms].map(form=>[form,form.editRevision || 0]));
    return renderAdmin(routeVersion,event.currentTarget,discarded);
  };
  root.insertAdjacentHTML('beforeend',metrics([[judging.roster.judgeIds.length,'所选阶段评委','admin-stage-judges'],[judging.reviews.filter(r=>r.submitted && !r.recused && !r.invalidated).length,'所选阶段正式记录（未计分）','admin-stage-reviews']])+`<div class="stage-switch" aria-label="选择评审阶段"><button type="button" class="btn secondary" data-admin-stage="exhibition" aria-pressed="${adminStage==='exhibition'}">展示评审</button><button type="button" class="btn secondary" data-admin-stage="roadshow" aria-pressed="${adminStage==='roadshow'}">路演评审</button></div><div id="judging-admin"></div>`);
  document.querySelectorAll('[data-admin-stage]').forEach(button=>button.onclick=()=>{const next=button.dataset.adminStage;if(next===adminStage)return;if(stageHasUnsavedInputs() && !confirm('本轮有尚未保存的输入，确认切换阶段并放弃这些输入？'))return;clearStageUnsavedInputs();adminStage=next;loadAdminJudging(version);});

  installJudgingAdmin(data,judging);
  const settings=document.getElementById('settings-form');
  installSettingsForm(settings,'scoring',cfg);

  const invite=document.getElementById('invite-form');invite.onsubmit=e=>{e.preventDefault();withBusy(invite.querySelector('button'),async()=>{formError(invite,'');try{const result=await api('admin/invitations',{role:invite.elements.role.value,name:invite.elements.name.value.trim()});document.getElementById('new-code').innerHTML=`<div class="code-output"><strong>${esc(result.name)} · ${roleName[invite.elements.role.value]}</strong><code>${esc(result.code)}</code><span>请立即复制并单独发放；刷新后不再显示。</span><br><button class="btn secondary small" id="copy-code" type="button">复制邀请码</button></div>`;document.getElementById('copy-code').onclick=async()=>{try{await navigator.clipboard.writeText(result.code);toast('邀请码已复制，请单独发放。');}catch{toast('请选中上方邀请码手动复制。');}};invite.elements.name.value='';dirtyAdminForms.delete(invite);toast('身份已创建。参与展示或路演评审记录时，需刷新数据后加入对应阶段名单。');}catch(error){formError(invite,error.message);}});};
  const teamForm=document.getElementById('team-form');teamForm.onsubmit=e=>{e.preventDefault();const editRevision=teamForm.editRevision || 0;withBusy(teamForm.querySelector('[type=submit]'),async()=>{formError(teamForm,'');try{const payload={number:Number(teamForm.elements.number.value),name:teamForm.elements.name.value.trim(),project:teamForm.elements.project.value.trim(),track:teamForm.elements.track.value};if(teamForm.elements.id.value)payload.id=teamForm.elements.id.value;await api('admin/teams',payload);toast('队伍信息已保存。');if(route==='admin' && teamForm.isConnected){if((teamForm.editRevision || 0)!==editRevision){toast('已保存提交时的内容，之后的新输入已保留。');return;}dirtyAdminForms.delete(teamForm);if(!dirtyAdminForms.size)await renderAdmin(routeVersion,teamForm);}}catch(error){formError(teamForm,error.message);}});};
  document.getElementById('clear-team').onclick=()=>{teamForm.reset();dirtyAdminForms.delete(teamForm);teamForm.elements.id.value='';document.getElementById('save-team').textContent='新增队伍 ↗';};
  document.querySelectorAll('[data-edit]').forEach(b=>b.onclick=()=>{const team=teams.find(t=>String(t.id)===b.dataset.edit);['id','number','name','project','track'].forEach(key=>teamForm.elements[key].value=team[key] || '');document.getElementById('save-team').textContent='保存队伍修改';teamForm.scrollIntoView({behavior:'smooth',block:'center'});teamForm.elements.name.focus();});
  document.querySelectorAll('[data-revoke]').forEach(b=>b.onclick=()=>withBusy(b,async()=>{await api('admin/revoke',{role:b.dataset.role,id:b.dataset.revoke});toast('邀请码已停用，对应登录会话已失效。');if(route==='admin' && b.isConnected && !dirtyAdminForms.size)await renderAdmin(routeVersion,b);}));
  installSubmissionAdmin(data);
  root.insertAdjacentHTML('beforeend','<div class="admin-section-title"><h2>AI API 额度</h2><span class="small-note">每队开通一次；各队自行创建的密钥共用本队额度。</span></div><section class="surface" id="key-admin"><p class="small-note">读取中…</p></section>');loadKeyAdmin(teams,version);
  root.querySelectorAll('form').forEach(form=>{if(!form.closest('#judging-admin'))trackAdminForm(form);});
  if(typeof installAdminData==='function')installAdminData(data,version);
  }finally{if(adminLoadPending===request){adminLoadPending=null;if(typeof kickAdminReviewSync==='function')kickAdminReviewSync();}}
}
async function loadAdminJudging(version,expectedForm=null){
  const stage=adminStage,request=++adminJudgingVersion,container=document.getElementById('judging-admin');
  adminLoadPending=request;
  const stale=()=>version!==routeVersion || route!=='admin' || stage!==adminStage || request!==adminJudgingVersion || !container.isConnected || (expectedForm && (!expectedForm.isConnected || stageHasUnsavedInputs()));
  document.querySelectorAll('[data-admin-stage]').forEach(button=>button.setAttribute('aria-pressed',String(button.dataset.adminStage===stage)));
  if(!expectedForm)container.innerHTML='<div class="loading-state">正在读取本轮评审…</div>';
  try{const data=await api(`admin/judging/${stage}`);if(stale())return;installJudgingAdmin(adminEventData,data);}catch(error){if(stale())return;if(expectedForm)formError(expectedForm,error.message);else container.innerHTML=empty('暂时无法读取本轮评审',esc(error.message),'<button class="btn secondary" id="retry-admin-stage">重新读取</button>');document.getElementById('retry-admin-stage')?.addEventListener('click',()=>loadAdminJudging(version));}finally{if(adminLoadPending===request){adminLoadPending=null;if(typeof kickAdminReviewSync==='function')kickAdminReviewSync();}}
}
function adminReviewKey(runId,record){return JSON.stringify([runId,record.judgeId,record.teamId]);}
function adminReviewRowsMarkup(eventData,data){
  const teams=new Map([...(eventData.teams || []),...(data.teams || [])].map(team=>[team.id,team]));
  const judges=new Map((eventData.judges || []).map(judge=>[judge.id,judge]));
  return (data.reviews || []).map(record=>`<tr data-admin-review-key="${esc(adminReviewKey(data.stage.runId,record))}" data-review-version="${Number(record.version)}"><td>${esc(teams.get(record.teamId)?.name || record.teamId)}</td><td>${esc(judges.get(record.judgeId)?.name || record.judgeId)}</td><td>${reviewState(record)}<br><small>版本 ${Number(record.version)}</small></td><td><details><summary>查看记录</summary>${record.submitted?`<h4>正式记录（未计分）</h4><p class="preserve-text">${esc(record.submitted.feedback)}</p><small>${esc(beijingTime(record.submitted.submittedAt))}（北京时间）</small>`:'<p>暂无正式记录。</p>'}${record.draft?`<h4>草稿</h4><p class="preserve-text">${esc(record.draft.feedback || '（空草稿）')}</p>`:''}</details></td></tr>`).join('') || '<tr><td colspan="4">本轮尚无评审记录。</td></tr>';
}
function patchAdminReviewData(view,data){
  if(view!==adminReadOnlyView || !view.container.isConnected || route!=='admin' || view.stage!==adminStage || data.stage.runId!==view.runId || data.stage.revision!==view.revision)return false;
  const body=view.container.querySelector('#admin-stage-records');if(!body)return false;
  const scrollBox=body.closest('.table-wrap'),scroll={x:window.scrollX,y:window.scrollY,left:scrollBox.scrollLeft,top:scrollBox.scrollTop};
  const existing=new Map([...body.children].filter(row=>row.dataset.adminReviewKey).map(row=>[row.dataset.adminReviewKey,row]));
  const wanted=new Set(data.reviews.map(record=>adminReviewKey(view.runId,record)));
  for(const row of [...body.children])if(!wanted.has(row.dataset.adminReviewKey))row.remove();
  let cursor=body.firstElementChild;
  for(const record of data.reviews){
    const key=adminReviewKey(view.runId,record);let row=existing.get(key);
    if(!row || row.dataset.reviewVersion!==String(record.version)){
      const holder=document.createElement('tbody');holder.innerHTML=adminReviewRowsMarkup(view.eventData,{...data,reviews:[record]});
      const replacement=holder.firstElementChild;
      if(row){
        const details=row.querySelector('details'),open=details?.open,focused=details?.querySelector('summary')===document.activeElement;
        row.innerHTML=replacement.innerHTML;row.dataset.reviewVersion=String(record.version);
        const updated=row.querySelector('details');if(updated)updated.open=Boolean(open);
        if(focused)updated?.querySelector('summary')?.focus({preventScroll:true});
      }else row=replacement;
    }
    if(row!==cursor)body.insertBefore(row,cursor);cursor=row.nextElementSibling;
  }
  if(!data.reviews.length)body.innerHTML='<tr><td colspan="4">本轮尚无评审记录。</td></tr>';
  document.getElementById('admin-stage-reviews').textContent=String(data.reviews.filter(record=>record.submitted && !record.recused && !record.invalidated).length);
  view.data=data;
  const note=document.getElementById('admin-sync-query-note');if(note)note.hidden=false;
  if(scrollBox.scrollLeft!==scroll.left)scrollBox.scrollLeft=scroll.left;if(scrollBox.scrollTop!==scroll.top)scrollBox.scrollTop=scroll.top;
  if(window.scrollX!==scroll.x || window.scrollY!==scroll.y)window.scrollTo({left:scroll.x,top:scroll.y,behavior:'instant'});
  return true;
}
function installJudgingAdmin(eventData,data){
  patchSubmissionStageCards(data);
  const stage=data.stage,stageId=stage.id || adminStage,viewVersion=routeVersion,window=data.window,rosterLocked=stage.rosterEditable!==true;
  const teamById=id=>(data.teams || []).find(t=>t.id===id) || eventData.teams.find(t=>t.id===id),judgeById=id=>eventData.judges.find(j=>j.id===id);
  document.getElementById('admin-stage-judges').textContent=data.roster.judgeIds.length;
  document.getElementById('admin-stage-reviews').textContent=data.reviews.filter(r=>r.submitted && !r.recused && !r.invalidated).length;
  document.getElementById('judging-admin').innerHTML=`<div class="admin-section-title"><h2>${stageName(stageId)}管理</h2><span id="admin-stage-window-status" class="status-chip ${window.canEdit?'':'closed'}">${window.canEdit?'评审记录开放中':'当前只读'}</span></div><div class="stage-strip"><strong>${esc(stage.name)}</strong><span>配置版本 ${Number(stage.revision)}</span><span>当前轮次 <code>${esc(stage.runId)}</code></span></div>${stageNotice(stageId)}${rulePlaceholders(stage)}<div class="admin-columns"><section class="surface"><h2>本轮队伍与评委授权</h2><p class="small-note">仅首次开放前可配置本轮名单。首次开放时截取正式材料；之后关闭再开放沿用相同快照。更换名单或材料需新建评审轮次。</p><form id="roster-form"><fieldset ${rosterLocked?'disabled':''}><div class="roster-columns"><div><h3>${stageId==='roadshow'?'路演':'展示'}队伍</h3><div class="roster-list">${eventData.teams.map(t=>`<label class="check-option"><input type="checkbox" name="teamIds" value="${esc(t.id)}" ${data.roster.teamIds.includes(t.id)?'checked':''}><span>${esc(teamLabel(rosterLocked?(teamById(t.id) || t):t))}<small>${esc((rosterLocked?(teamById(t.id) || t):t).track || '方向待完善')}</small></span></label>`).join('') || '<p class="small-note">先在下方录入本届队伍。</p>'}</div></div><div><h3>授权评委</h3><div class="roster-list">${eventData.judges.map(j=>`<label class="check-option"><input type="checkbox" name="judgeIds" value="${esc(j.id)}" ${data.roster.judgeIds.includes(j.id)?'checked':''} ${!j.active?'disabled':''}><span>${esc(j.name)}<small>${j.active?'有效身份':'已停用'}</small></span></label>`).join('') || '<p class="small-note">先创建专业评委身份。</p>'}</div></div></div><button class="btn small" type="submit">保存本轮工作名单</button></fieldset>${rosterLocked?'<p class="small-note">本轮工作名单已冻结或已有评审记录。需调整名单或材料时，请关闭窗口，再填写原因新建一轮评审。</p>':''}<p class="form-error" role="alert"></p></form></section><section class="surface"><h2>评审记录窗口</h2><form id="review-window-form"><label class="toggle-line"><input type="checkbox" name="open" ${window.open?'checked':''}>开放本轮评审记录</label><label class="field">截止时间（北京时间）<input name="deadline" type="datetime-local" value="${esc(beijingInput(window.deadline))}"><small>留空表示不设截止。开放前需要配置队伍和授权评委。</small></label><button class="btn small" type="submit">保存评审窗口</button><p class="form-error" role="alert"></p></form><div class="info-banner warning"><strong>本阶段成绩发布暂不可用</strong><br>${(data.blockers || ['数值规则、汇总、排名与晋级尚未实现。']).map(esc).join('<br>')}</div><button class="btn secondary small" type="button" disabled>发布成绩（尚未实现）</button></section></div><div class="admin-section-title"><h2>本轮评审记录</h2><span class="small-note">草稿与正式记录分别保留，当前不计分。</span></div><div class="table-wrap"><table class="board-table review-table"><thead><tr><th>队伍</th><th>评委</th><th>记录状态</th><th>内容与时间</th></tr></thead><tbody id="admin-stage-records">${adminReviewRowsMarkup(eventData,data)}</tbody></table></div><p class="small-note" id="admin-sync-query-note" hidden>历史筛选、修订、操作记录和其他管理数据，请按需重新查询。</p><details class="surface reset-panel"><summary>查看本轮材料快照</summary><p class="small-note">以下为本轮首次开放时保留的正式材料。队伍后续修改的材料不会替换本轮快照。</p>${(data.teams || []).map(t=>`<details class="stage-material"><summary>${esc(teamLabel(t))}</summary>${reviewMaterialMarkup(t.materialSnapshot,stage)}</details>`).join('') || '<p class="small-note">本轮尚未配置队伍。</p>'}</details><details class="surface reset-panel"><summary>新建一轮${stageName(stageId)}</summary><p class="small-note">需要更换名单或更新材料时，请先关闭窗口，再新建一轮。新一轮沿用当前名单，评审记录从空开始；旧轮记录保留，不执行晋级。</p><form id="reset-review-form"><label class="field">新建原因<input name="reason" required maxlength="500" placeholder="说明为什么需要新建一轮"></label><button class="btn danger small" type="submit" ${window.open?'disabled':''}>新建评审轮次</button><p class="form-error" role="alert"></p></form></details>`;
  async function update(form,path,payload,message){const editRevision=form.editRevision || 0;await withBusy(form.querySelector('button'),async()=>{formError(form,'');try{const saved=await api(`admin/judging/${stageId}/${path}`,payload);if(!form.isConnected || route!=='admin' || viewVersion!==routeVersion || stageId!==adminStage)return;patchSubmissionStageCards(saved);if((form.editRevision || 0)!==editRevision){toast(message+' 保存期间的新输入已保留，请刷新本轮后核对。');return;}dirtyAdminForms.delete(form);toast(message);if(!stageHasUnsavedInputs())await loadAdminJudging(viewVersion,form);else toast(message+' 其他未保存输入已保留；刷新本轮后可继续操作。');}catch(error){if(!form.isConnected)return;formError(form,error.message);if(error.status===409)form.querySelector('.form-error').textContent+=' 当前输入已保留，请刷新数据查看最新轮次或配置。';}});}
  document.getElementById('judging-admin').querySelectorAll('form').forEach(trackAdminForm);
  const roster=document.getElementById('roster-form');roster.onsubmit=e=>{e.preventDefault();update(roster,'roster',{expectedRevision:stage.revision,teamIds:[...roster.querySelectorAll('[name=teamIds]:checked')].map(i=>i.value),judgeIds:[...roster.querySelectorAll('[name=judgeIds]:checked')].map(i=>i.value)},'本轮工作名单已保存。');};
  const windowForm=document.getElementById('review-window-form');windowForm.onsubmit=e=>{e.preventDefault();try{update(windowForm,'window',{expectedRevision:stage.revision,open:windowForm.elements.open.checked,deadline:parseBeijingInput(windowForm.elements.deadline.value)},'评审记录窗口已保存。');}catch(error){formError(windowForm,error.message);}};
  const reset=document.getElementById('reset-review-form');reset.onsubmit=e=>{e.preventDefault();if(confirm(`确认新建一轮${stageName(stageId)}？新轮次沿用工作名单，评审记录从空开始，旧轮次记录保留。`))update(reset,'reset',{expectedRevision:stage.revision,reason:reset.elements.reason.value.trim()},`新一轮${stageName(stageId)}已创建，窗口保持关闭。`);};
  adminReadOnlyView={container:document.getElementById('judging-admin'),stage:stageId,runId:stage.runId,revision:stage.revision,routeVersion,eventData,data};
  if(typeof startAdminReviewSync==='function')startAdminReviewSync(adminReadOnlyView);
}
const materialFields=[
  ['projectName','作品名称',140,'text',true],['summary','项目介绍',600,'textarea',true],
  ['codeUrl','公开代码仓库主页',1024,'url',true],['codeVersion','最终 commit 链接或完整哈希',1024,'text',true],
  ['readmeUrl','README / 运行说明链接',1024,'url',true],['license','开源许可证名称或说明',200,'text',true],
  ['licenseUrl','许可证文件链接（选填）',1024,'url',false],
  ['demoUrl','原型演示链接（或填写本地说明）',1024,'url',false],['demoInstructions','本地演示说明（或填写演示链接）',2000,'textarea',false],
  ['slidesUrl','展示材料链接（建议 5–7 页）',1024,'url',true],['videoUrl','演示视频链接（建议约 2 分钟）',1024,'url',true],
  ['sourcesAndLimitations','模型、数据、图片、第三方代码来源与项目局限',2000,'textarea',true]
];
function beijingTime(value){return value===null || value===undefined?'—':new Date(value).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false});}
function beijingInput(value){if(value===null || value===undefined)return '';const parts=new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).formatToParts(new Date(value));const part=k=>parts.find(p=>p.type===k).value;return `${part('year')}-${part('month')}-${part('day')}T${part('hour')}:${part('minute')}`;}
function parseBeijingInput(value){if(!value)return null;if(!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(value))throw new Error('请填写有效的北京时间。');const time=new Date(value+':00+08:00').getTime();if(!Number.isSafeInteger(time) || time<0 || time>253402300799999 || beijingInput(time)!==value)throw new Error('请填写有效的北京时间。');return time;}
function submissionState(s){return s?.submitted?(s.hasUnsubmittedChanges?'已提交且有草稿修改':'已正式提交'):s?.draft?'草稿':'未保存';}
function safeMaterialUrl(value){if(typeof value!=='string')return '';const trimmed=value.trim();if(!trimmed || /[\s\\\u0000-\u001f\u007f]/.test(trimmed) || /%(?:0[0-9a-f]|1[0-9a-f]|7f)/i.test(trimmed) || new TextEncoder().encode(trimmed).length>1024)return '';try{const url=new URL(trimmed);return url.protocol==='https:' && url.hostname && !trimmed.match(/^https:\/\/[^/?#]*@/i) && !url.username && !url.password && url.port!=='0' && (url.hostname.startsWith('[') || url.hostname.replace(/\.$/,'').split('.').every(label=>/^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/i.test(label)))?trimmed:'';}catch{return '';}}
function fullCommit(value){return /^(?:[a-f0-9]{40}|[a-f0-9]{64})$/i.test(value);}
function repositoryParts(value,commit=false){
  if(!safeMaterialUrl(value) || !/^https:\/\/(?:github.com|gitee.com|gitlab.com)\//i.test(value) || /[%\s\\]/.test(value) || /\/(?:\.\.?)(?:\/|$)/.test(value))return null;
  try{
    const url=new URL(value),host=url.hostname;
    // Check explicit ports as well: URL() removes the default :443 spelling.
    if(!['github.com','gitee.com','gitlab.com'].includes(host) || url.search || url.hash || /:\d+$/.test(value.split('/')[2]))return null;
    let parts=url.pathname.replace(/\/+$/,'').slice(1).split('/'),sha=null;
    if(commit){const tail=host==='gitlab.com'?3:2;if(parts.length<tail+2 || parts.slice(-tail,-1).join('/')!==(host==='gitlab.com'?'-/commit':'commit') || !fullCommit(parts.at(-1)))return null;sha=parts.at(-1).toLowerCase();parts=parts.slice(0,-tail);}
    parts[parts.length-1]=parts.at(-1).replace(/\.git$/,'');
    // Match the server's GitHub-only, repository-segment allowance for names such as .github.
    if(parts.length<2 || (host!=='gitlab.com' && parts.length!==2) || parts.some((p,index)=>!(host==='github.com' && index===parts.length-1?/^[A-Za-z0-9_.][A-Za-z0-9_.-]*$/:/^[A-Za-z0-9_][A-Za-z0-9_.-]*$/).test(p) || ['.','..'].includes(p)))return null;
    const path=parts.join('/');return {host,path:host==='gitlab.com'?path:path.toLowerCase(),sha};
  }catch{return null;}
}
function validCodeRepository(value){if(!safeMaterialUrl(value))return false;const host=new URL(value).hostname;return !['github.com','gitee.com','gitlab.com'].includes(host) || !!repositoryParts(value);}
function normalizedCommit(repository,value){
  const repo=repositoryParts(repository);
  if(!validCodeRepository(repository))throw new Error('仓库请填写完整 HTTPS 仓库主页链接，不是本地路径或文件页面；GitHub、Gitee、GitLab 请使用仓库根地址。');
  if(fullCommit(value))return value.toLowerCase();
  const commit=repositoryParts(value,true);
  if(!repo || !commit || commit.host!==repo.host || commit.path!==repo.path)throw new Error('请填写同一 GitHub、Gitee 或 GitLab 仓库的完整 commit 链接，或40/64位完整哈希；其他平台请填完整哈希。不要填分支、tag或前6位；链接请去掉参数和片段。');
  return commit.sha;
}
function commitMarkup(value){return fullCommit(value)?`<code>${esc(value.slice(0,6))}</code> <button type="button" class="text-action" data-copy-value="${esc(value)}">复制完整哈希</button><details><summary>查看完整哈希</summary><code>${esc(value)}</code></details>`:esc(value);}
function materialMarkup(materials){if(!materials)return '<p class="small-note">暂无材料。</p>';return `<dl class="material-list">${materialFields.map(([key,label,,type])=>{const value=materials[key] || '';if(!value)return '';const safe=type==='url'?safeMaterialUrl(value):'';return `<div><dt>${esc(label)}</dt><dd>${key==='codeVersion'?commitMarkup(value):safe?`<a href="${esc(safe)}" target="_blank" rel="noopener noreferrer">${esc(value)} ↗</a>`:esc(value)}</dd></div>`;}).join('')}<div><dt>材料自查声明</dt><dd>${materials.confirmed===true?'已确认（队伍自行声明）':'尚未确认'}</dd></div></dl>`;}
function judgeSubmissionMarkup(record){return `<section class="surface submitted-materials"><h2>最新正式项目材料</h2><p class="small-note">原专业评分入口读取最新正式版；此处不是展示或路演的冻结快照，评分不绑定材料版本。</p>${record?`<p class="small-note">正式版本 ${Number(record.version)} · 北京时间 ${esc(beijingTime(record.submittedAt))}</p>${materialMarkup(record.materials)}`:'<p class="small-note">暂未正式提交材料；仍可根据现场演示评分。</p>'}</section>`;}
function windowMarkup(window){return `<div class="info-banner ${window.canEdit?'':'warning'}">${window.canEdit?'作品提交开放中':'当前作品提交只读：尚未开放或已截止'} · 截止时间（北京时间）：${esc(beijingTime(window.submissionDeadline))}${window.submissionDeadline===null?'（未设置）':''}<br>服务器会在实际保存前检查提交窗口和材料版本。点击提交不代表已保存，请确认成功提示和最新版本。平台只保存填写的内容和链接，不会抓取或检查外链内容；链接中的文件之后仍可能变化。</div>`;}
function stageMaterialStatus(data,latestVersion){const snapshot=data?.materialSnapshot;return !data?.inRoster?'未列入本轮名单':data.frozenAt===null?'尚未生成材料快照':snapshot?`本轮采用 v${snapshot.submissionVersion}${latestVersion && snapshot.submissionVersion!==latestVersion?`（最新正式版为 v${latestVersion}）`:''}`:'本轮冻结时无正式材料';}
function stageMaterialMarkup(stage,data,latestVersion){const snapshot=data?.materialSnapshot;return `<div class="stage-material" data-stage-material="${stage}"><strong>${stageName(stage)}：${esc(stageMaterialStatus(data,latestVersion))}</strong>${data?`<p class="small-note">轮次 ${esc(data.runId)} · 冻结时间 ${esc(beijingTime(data.frozenAt))}（北京时间）</p>`:''}${snapshot?`<details><summary>查看本轮实际材料</summary>${materialMarkup(snapshot.materials)}</details>`:''}</div>`;}

function judgingMaterialsMarkup(stages,latestVersion){return `<section class="surface submission-versions"><h2>本轮评审采用的材料</h2><p class="small-note">原专业评分读取最新正式版。展示与路演按各自首次开放时的快照；重新提交不会替换已冻结材料。</p>${['exhibition','roadshow'].map(stage=>stageMaterialMarkup(stage,stages?.[stage],latestVersion)).join('')}</section>`;}
function patchSubmissionStageCards(data){if(submissionAdminView?.container.isConnected && route==='admin'){submissionAdminView.controller.stageChanged(data);return;}for(const card of root.querySelectorAll?.('[data-submission-stage-team]') || []){const team=(data.teams || []).find(t=>t.id===card.dataset.submissionStageTeam),stage=data.stage.id;const slot=card.querySelector(`[data-stage-material="${stage}"]`);if(slot)slot.outerHTML=stageMaterialMarkup(stage,{runId:data.stage.runId,inRoster:!!team,frozenAt:data.stage.frozenAt,materialSnapshot:team?.materialSnapshot || null},Number(card.dataset.latestVersion) || null);}}

const submissionHelp={
  codeUrl:'在 GitHub、Gitee 或 GitLab 打开公开仓库主页，复制浏览器地址，例如 https://github.com/your-team/your-project。也可填写其他平台的 HTTPS 仓库主页。不是个人主页、文件页面、本地路径或上传 ZIP；只有本地代码时，请先 push 到线上仓库。',
  codeVersion:'在仓库的提交历史（Commits）选中最终一次提交，复制该 commit 页面链接，或复制40/64位完整哈希。不要填分支、tag或只填前6位；GitHub/Gitee/GitLab 链接必须属于上面的仓库；其他平台请填完整哈希。平台只检查格式，不能证明此提交存在或属于该仓库。'
};
function teamFormMarkup(data){const s=data.submission,materials=s.draft || {projectName:data.team.project || ''};const groups=[['项目说明',['projectName','summary','sourcesAndLimitations']],['代码与运行',['codeUrl','codeVersion','readmeUrl','license','licenseUrl','demoUrl','demoInstructions']],['展示材料',['slidesUrl','videoUrl']]];return `<form id="submission-form" class="surface submission-form" novalidate><h2>项目材料草稿</h2><p class="small-note">带 * 为正式提交必填；保存草稿可缺项。请确保仓库公开、材料链接无需登录即可访问。平台只保存字段和 URL，不备份代码或文件。</p><fieldset ${data.window.canEdit?'':'disabled'}>${groups.map(([title,keys])=>`<h3>${title}</h3><div class="form-grid">${keys.map(key=>{const [,label,limit,type,required]=materialFields.find(f=>f[0]===key);return `<label class="field ${type==='textarea' || submissionHelp[key]?'wide':''}" for="material-${key}">${esc(label)}${required?' *':''}${type==='textarea'?`<textarea id="material-${key}" name="${key}" maxlength="${limit}" ${required?'required':''}>${esc(materials[key] || '')}</textarea>`:`<input id="material-${key}" name="${key}" type="${type}" maxlength="${limit}" value="${esc(materials[key] || '')}" ${required?'required':''} ${type==='url'?'placeholder="https://…"':''} ${submissionHelp[key]?`aria-describedby="help-${key}"`:''}>`}${submissionHelp[key]?`<small id="help-${key}">${esc(submissionHelp[key])}</small>`:''}</label>`;}).join('')}</div>`).join('')}<label class="toggle-line" for="material-confirmed"><input id="material-confirmed" type="checkbox" name="confirmed" ${materials.confirmed?'checked':''} required><span>我已检查材料可访问，填写开源与来源信息，并移除个人隐私和密钥。此为队伍自查声明。</span></label><div class="form-actions"><button type="button" id="save-submission-draft" class="btn secondary">保存草稿</button><button type="submit" class="btn">检查并正式提交 ↗</button></div></fieldset><div id="submission-errors" role="alert" tabindex="-1"></div><p class="form-error" role="alert"></p></form>`;}
function submissionProblems(materials,formal){
  const errors=[];
  for(const [key,label,limit,type,required] of materialFields){const value=materials[key] || '';let message='';if(formal && required && !value)message='请填写'+label;else if(Array.from(value).length>limit || (key==='codeVersion' && new TextEncoder().encode(value).length>1024))message=label+'超过长度限制';else if(type==='url' && value && !safeMaterialUrl(value))message=label+'请填写完整 HTTPS 链接';if(message)errors.push({key,message});}
  if(formal){if(materials.codeUrl && !validCodeRepository(materials.codeUrl))errors.push({key:'codeUrl',message:'请填写完整 HTTPS 仓库主页链接；GitHub、Gitee、GitLab 不要填写文件或commit页面。'});if(materials.codeVersion && validCodeRepository(materials.codeUrl)){try{normalizedCommit(materials.codeUrl,materials.codeVersion);}catch(e){errors.push({key:'codeVersion',message:e.message});}}if(!materials.demoUrl && !materials.demoInstructions)errors.push({key:'demoInstructions',message:'请填写原型演示链接或本地演示说明'});if(!materials.confirmed)errors.push({key:'confirmed',message:'请完成材料自查确认'});}
  return errors;
}
function submissionRecordText(data){const s=data.submission,m=s.submitted;if(!m)return '尚无正式提交记录';return [`队伍：${teamLabel(data.team)}`,`服务器正式版：v${s.submittedVersion}`,`服务器提交时间（北京时间）：${beijingTime(s.submittedAt)}`,`完整 commit：${m.codeVersion || '旧记录未填写'}`,...materialFields.filter(([key])=>key!=='codeVersion').map(([key,label])=>`${label}：${m[key] || '未填写'}`),'材料自查：'+(m.confirmed?'已确认':'未确认'),...['exhibition','roadshow'].map(stage=>{const item=data.judgingMaterials?.[stage];return `${stageName(stage)}（${item?.runId || '未知轮次'}）：${!item?.inRoster?'未列入本轮名单':item.frozenAt===null?'尚未生成材料快照':item.materialSnapshot?'采用 v'+item.materialSnapshot.submissionVersion:'冻结时无正式材料'}`;}),'此记录仅保存字段与URL，不是代码或文件备份。'].join('\n');}
function submissionReceiptMarkup(data,received=false){const s=data.submission;if(!s.submitted)return '';return `<section class="surface submission-receipt" ${received?'id="submission-receipt"':''} tabindex="-1"><h2>${received?`正式版 v${s.submittedVersion} 已收到`:`服务器最近正式记录 · v${s.submittedVersion}`}</h2><div class="small-note">服务器提交时间（北京时间）：${esc(beijingTime(s.submittedAt))} · 首次提交 ${esc(beijingTime(s.firstSubmittedAt))}<br>commit ${commitMarkup(s.submitted.codeVersion || '旧记录未填写')}</div><button type="button" class="btn secondary small" data-copy-value="${esc(submissionRecordText(data))}">复制提交记录</button><details><summary>查看可复制的完整记录</summary><pre class="preserve-text">${esc(submissionRecordText(data))}</pre></details>${received?`<p class="small-note">${['exhibition','roadshow'].map(stage=>esc(stageName(stage)+'：'+stageMaterialStatus(data.judgingMaterials?.[stage],s.submittedVersion))).join('<br>')}<br>重提不会替换已冻结的本轮快照。</p>`:''}</section>`;}
let currentTeamRecovery=null;
const draftRecoveryWriter=globalThis.GotcDraftRecovery?.freshId();
function lockTeamRecovery(message='登录状态已变化，本地副本需重新登录本队后查看。'){currentTeamRecovery?.lock(message);}
function localRecoveryMarkup(){return '<section class="surface local-recovery" id="local-draft-recovery"><h2>本浏览器的材料副本</h2><p class="small-note">仅保存在本浏览器，保留7天，不等于已提交。共享设备请在离开时退出；成功退出会清除本浏览器内本队的恢复副本。恢复后请重新勾选材料自查。</p><p id="local-draft-status" role="status"></p><button type="button" class="btn secondary small" id="refresh-local-drafts">刷新本地副本列表</button><div id="local-draft-candidates"></div><div id="local-draft-comparison" aria-live="polite"></div></section>';}
function newSubmissionRequestId(){return globalThis.crypto?.randomUUID?.() || `request-${Date.now()}-${Math.random().toString(36).slice(2)}-${Math.random().toString(36).slice(2)}`;}
function matchingSubmissionReceipt(receipt,requestId,action,baseVersion){return receipt?.requestId===requestId && receipt.status==='succeeded' && receipt.action===action && receipt.baseVersion===baseVersion && Number.isSafeInteger(receipt.version) && receipt.materials && typeof receipt.materials==='object';}
function requestReceiptText(receipt){return [`请求编号：${receipt.requestId}`,`${receipt.action==='submitted'?'正式提交':'草稿保存'}：v${receipt.historyVersion ?? receipt.version}`,`服务器保存时间（北京时间）：${beijingTime(receipt.savedAt)}`,localMaterialsText(receipt.materials),'材料自查：'+(receipt.materials.confirmed===true?'已确认':'未确认'),'此记录只保存字段与URL，不是代码或文件备份。'].join('\n');}
function requestReceiptMarkup(receipt,requestId,focusable=false){
  if(receipt?.requestId!==requestId)return '<p class="small-note">返回编号不一致，结果仍待确认。请保留本地副本，稍后再查。</p>';
  const id=`<p class="small-note request-number">请求编号：<code>${esc(requestId)}</code></p>`;
  if(receipt.status==='not_found')return id+'<p>暂未查到这次请求，结果仍待确认。请求可能尚在处理，请稍后再查；不要据此重复提交。</p>';
  if(receipt.status==='rejected')return id+`<p>服务器已明确拒绝这次${receipt.action==='submitted'?'正式提交':'草稿保存'}，没有保存本次材料。</p><p class="small-note">${esc(receipt.error?.message || '请核对服务器最新草稿后手动处理。')} · 北京时间 ${esc(beijingTime(receipt.decidedAt))}</p>`;
  if(receipt.status!=='succeeded')return id+'<p>结果仍待确认，请稍后再查。</p>';
  return `<section class="request-receipt" ${focusable?'id="submission-receipt" tabindex="-1"':''}><h3>${receipt.action==='submitted'?`正式版 v${Number(receipt.historyVersion ?? receipt.version)} 已收到`:`草稿编辑版 v${Number(receipt.version)} 已保存`}</h3>${id}<p class="small-note">这次请求的服务器保存时间（北京时间）：${esc(beijingTime(receipt.savedAt))}。以下为这次请求保存的材料，可能不是队伍当前最新版。</p><details><summary>查看这次请求的完整材料</summary>${materialMarkup(receipt.materials)}</details><button type="button" class="btn secondary small" data-copy-value="${esc(requestReceiptText(receipt))}">复制本次保存记录</button><details><summary>查看可复制的本次记录</summary><pre class="preserve-text">${esc(requestReceiptText(receipt))}</pre></details></section>`;
}
function rawTeamMaterials(form){return Object.fromEntries(materialFields.map(([key])=>[key,form.elements[key].value]));}
function serverDraftMaterials(data){return Object.fromEntries(materialFields.map(([key])=>[key,String((data.submission.draft || {projectName:data.team.project || ''})[key] || '')]));}
function localMaterialsText(materials){return materialFields.map(([key,label])=>`${label}：\n${materials[key] || ''}`).join('\n\n');}
function teamRecoveryEpochs(){try{const target=globalThis.localStorage,prefix=`gotc-material-recovery:v1:${encodeURIComponent(base.pathname)}:`,epochs=new Map();for(let index=0;index<target.length;index++){const key=target.key(index);if(key?.startsWith(prefix) && key.endsWith(':epoch'))epochs.set(key,target.getItem(key) || '');}return (teamId,loginId)=>epochs.get(prefix+encodeURIComponent(teamId)+':'+(loginId?'auth:'+encodeURIComponent(loginId)+':':'')+'epoch') || '';}catch{return ()=>'';}}
function teamDraftStore(data,expectedEpoch,loginId=session.loginId || ""){return globalThis.GotcDraftRecovery?.create({storage:()=>globalThis.localStorage,namespace:base.pathname,teamId:data.team.id,writerId:draftRecoveryWriter,baseVersion:data.submission.version,baseMaterials:serverDraftMaterials(data),fields:materialFields.map(([key,,limit])=>[key,limit]),expectedEpoch,loginId});}
function installTeamRecovery(data,form,version,{restore,loadLatest,onWindow},expectedEpoch){
  const panel=document.getElementById('local-draft-recovery'),status=document.getElementById('local-draft-status'),candidates=document.getElementById('local-draft-candidates'),comparison=document.getElementById('local-draft-comparison');
  let active=true,edited=false,phase='editing',latest=data,loadedVersion=data.submission.version,inspectionRevision=0;
  const live=()=>active && form.isConnected && version===routeVersion && currentTeamRecovery===context;
  const store=teamDraftStore(data,expectedEpoch),sessionOwner=session;
  function hasAuthorization(){if(!active || currentTeamRecovery!==context || session!==sessionOwner)return false;if(!ownsBrowserLogin(sessionOwner)){context.lock('登录身份已变化，请重新登录后编辑。');return false;}store?.checkEpoch();if(store?.revoked){context.lock(store.error);return false;}return true;}
  function authorized(){return hasAuthorization() && live();}
  function notice(text){if(live())status.textContent=text;}
  function persist(state='editing',action=null,requestId=null){if(!authorized() || !store || (state==='editing' && ['pending','unknown'].includes(phase)))return null;const saved=store.write(rawTeamMaterials(form),state,action,requestId);notice(saved?'已保存本机副本（本机时间）：'+beijingTime(saved.record.updatedAt)+'；尚不能据此确认服务器提交结果。':store.error || '本地恢复不可用，请保存服务器草稿或复制当前输入。');return saved;}
  function list(){if(!authorized())return;if(!store){notice('本地恢复不可用，请保存服务器草稿或复制当前输入。');return;}const result=store.candidates();if(store.revoked){context.lock(store.error);return;}if(store.error)notice(store.error);else if(!edited)notice(result.ignored?'部分本地记录格式无效，已跳过；其他副本可继续查看。':'以下为本队的本地副本；请选择查看核对，不会自动保存到服务器。');
    candidates.innerHTML=result.items.map((item,index)=>{const r=item.record,uncertain=['pending','unknown'].includes(r.state);return `<details class="local-candidate"><summary>本地副本 · ${esc(beijingTime(r.updatedAt))}（本机时间） · 来源编辑版 v${r.baseVersion}${uncertain?' · 上次发送结果待确认':''}</summary><p class="small-note">${uncertain?'须先核对服务器记录，此副本只供比较和复制，不直接恢复。':'恢复只填入当前表单，仍需主动保存草稿或正式提交。'}${r.request?.requestId?'<br>请求编号：<code>'+esc(r.request.requestId)+'</code>':''}</p><div class="form-actions"><button type="button" class="btn secondary small" data-local-view="${index}">查看并核对服务器</button><button type="button" class="btn secondary small" data-copy-value="${esc(localMaterialsText(r.materials))}">复制本地材料</button><button type="button" class="btn secondary small" data-local-discard="${index}">丢弃此副本</button></div></details>`;}).join('') || '<p class="small-note">暂无其他可恢复副本。当前表单发生编辑后才建立本机副本。</p>';
    candidates.querySelectorAll('[data-local-view]').forEach(button=>button.onclick=()=>withBusy(button,()=>inspect(result.items[Number(button.dataset.localView)])));
    candidates.querySelectorAll('[data-local-discard]').forEach(button=>button.onclick=()=>{const item=result.items[Number(button.dataset.localDiscard)];notice(store.discard(item)?'已丢弃所选本地副本，服务器材料不受影响。':store.error || '副本已更新，未删除；请重新查看。');list();});
  }
  async function authorizedLatest(action){if(!authorized())return null;const next=await api('team');if(!authorized() || action!==inspectionRevision)return null;if(next.team.id!==data.team.id){context.lock('当前登录队伍已变化，请重新打开本队页面后查看本地副本。');return null;}latest=next;onWindow(next.window);return next;}
  function stillPresent(item){return store.candidates().items.some(current=>current.key===item.key && current.raw===item.raw);}
  function compare(item,next,message){const local=item.record.materials,remote=serverDraftMaterials(next),changed=materialFields.filter(([key])=>local[key]!==remote[key]);comparison.innerHTML=`<section class="local-comparison"><h3 id="local-comparison-title">${esc(message)}</h3><p class="small-note" id="local-comparison-outcome"></p><p class="small-note">本地基线 v${item.record.baseVersion}；服务器当前编辑版 v${next.submission.version}。本地副本不等于提交回执；以下是与服务器当前草稿的对照，不会自动合并。</p>${changed.length?changed.map(([key,label])=>`<div class="local-field-diff"><h4>${esc(label)}</h4><p class="small-note">本地副本</p><pre class="preserve-text">${esc(local[key])}</pre><button type="button" class="text-action" data-copy-value="${esc(local[key])}">复制此项本地内容</button><p class="small-note">服务器当前草稿</p><pre class="preserve-text">${esc(remote[key])}</pre></div>`).join(''):'<p>两份材料文字相同。文字比较不用于确认提交结果。</p>'}<details><summary>查看完整本地材料</summary><pre class="preserve-text">${esc(localMaterialsText(local))}</pre></details><div id="local-restore-actions"></div></section>`;}
  async function inspect(item){const action=++inspectionRevision;try{const next=await authorizedLatest(action);if(!next)return;if(!stillPresent(item)){notice('副本已被更新或丢弃，请刷新列表后核对。');list();return;}const unknown=['pending','unknown'].includes(item.record.state),same=item.record.baseVersion===next.submission.version && loadedVersion===next.submission.version;
      compare(item,next,unknown?'上次发送结果待确认，只能比较或复制':!next.window.canEdit?'提交窗口只读，本地副本仅供查看复制':same?'可恢复到当前表单':'服务器版本已更新，请手动合并');
      if(unknown && item.record.request?.requestId){const requestId=item.record.request.requestId;comparison.insertAdjacentHTML('afterbegin',`<div id="local-request-result"><p>正在查询请求编号 ${esc(requestId)}…</p></div>`);try{const receipt=await api('submission/requests/'+encodeURIComponent(requestId));if(!authorized() || action!==inspectionRevision || !stillPresent(item))return;if(receipt.requestId===requestId && ['succeeded','rejected'].includes(receipt.status)){document.getElementById('local-comparison-title').textContent='本地副本与服务器当前草稿对照';document.getElementById('local-comparison-outcome').textContent=receipt.status==='succeeded'?'本次结果已由上方同编号回执确认；下方仅比较材料，不用于判断请求是否成功。':'本次请求已由服务器明确拒绝；下方仅比较材料，不会重新发送。';}document.getElementById('local-request-result').innerHTML=requestReceiptMarkup(receipt,requestId)+'<p class="small-note">来源本地副本继续保留，供复制或自行丢弃；查询不会覆盖表单，也不会重发请求。</p>';}catch(error){if(!authorized() || action!==inspectionRevision)return;document.getElementById('local-request-result').textContent='本次查询未完成，结果仍待确认：'+error.message;}}
      const actions=document.getElementById('local-restore-actions');if(!next.window.canEdit)return;if(['pending','unknown'].includes(phase)){actions.innerHTML='<p class="small-note">当前页面的发送结果尚未确认，请先核对服务器记录；暂不替换表单。</p>';return;}
      if(unknown || !same){actions.innerHTML='<p class="small-note">载入会替换当前表单；如有当前新输入，将先保留独立本地副本。请复制需要合并的内容。</p><button type="button" class="btn secondary small" id="load-server-for-local-merge">载入服务器最新草稿，手动合并</button>';document.getElementById('load-server-for-local-merge').onclick=()=>{if(!authorized() || action!==inspectionRevision || ['pending','unknown'].includes(phase))return;if(submissionDirty && !persist()){notice('未替换当前输入：请先复制或保存，避免丢失。');return;}loadLatest(next);};return;}
      actions.innerHTML='<p class="small-note">恢复会替换当前表单内容；已有本地副本会保留，不会覆盖服务器或来源副本。</p><button type="button" class="btn secondary" id="confirm-local-restore">确认恢复到当前表单</button>';
      document.getElementById('confirm-local-restore').onclick=e=>withBusy(e.currentTarget,async()=>{const checked=await authorizedLatest(action);if(!checked)return;if(['pending','unknown'].includes(phase)){notice('当前发送结果尚未确认，本次未替换表单。');return;}if(checked.submission.version!==item.record.baseVersion || checked.submission.version!==loadedVersion || !checked.window.canEdit || !stillPresent(item)){await inspect(item);return;}
        if(submissionDirty && !persist()){notice('未替换当前输入：本地备份失败，请先复制或保存。');return;}store.fork();restore(item.record.materials,checked);edited=true;phase='editing';persist();comparison.innerHTML='';notice('已恢复到当前表单；尚未保存服务器，也未正式提交。请核对并重新勾选材料自查。');list();});
    }catch(error){if(live())notice(error.message);}}
  const context={teamId:data.team.id,store,sessionOwner,hasAuthorization,isAuthorized:authorized,lock(message){active=false;panel.hidden=true;form.querySelector('fieldset').disabled=true;status.textContent=message;},input(){edited=true;if(['pending','unknown'].includes(phase))return;phase='editing';persist();},flush(){if(edited && phase==='editing')persist();},pending(action,requestId){phase='pending';edited=true;return persist('pending',action,requestId);},result(sent,state){phase=state;if(store)store.result(sent,state);if(live() && store?.error)notice(store.error);},acknowledge(sent){store?.acknowledge(sent);return store?.error || '';},rebase(next){loadedVersion=next.submission.version;store?.rebase(loadedVersion,serverDraftMaterials(next));phase='editing';edited=true;persist();},purge(){return store?.purge() ?? false;},storageChanged(){if(!authorized() || !store)return;list();}};
  currentTeamRecovery=context;document.getElementById('refresh-local-drafts').onclick=()=>{inspectionRevision++;comparison.innerHTML='';list();};list();return context;
}

async function renderTeam(version,provided=null,received=false,owner=null){
  const sessionAtStart=session,authorization=owner || (currentTeamRecovery?.sessionOwner===sessionAtStart?currentTeamRecovery:null),epochs=teamRecoveryEpochs();
  if(authorization && !authorization.hasAuthorization())return;
  const data=provided || await api('team');if(version!==routeVersion || session!==sessionAtStart || !ownsBrowserLogin(sessionAtStart) || (authorization && !authorization.hasAuthorization()))return;if(authorization && authorization.teamId!==data.team.id){authorization.lock('当前登录队伍已变化，请重新登录本队后编辑。');return;}const expectedEpoch=authorization?.store?.epoch ?? epochs(data.team.id,sessionAtStart.loginId),s=data.submission;
  root.innerHTML=heading('TEAM / 作品提交',esc(teamLabel(data.team)),`${esc(data.team.track || '方向待完善')} · 队伍编号、名称及赛道由组织方维护。`)+windowMarkup(data.window)+`<section class="surface submission-overview"><h2 id="submission-status">${submissionState(s)}</h2><div>草稿编辑版 v${s.version} · 最新正式版 ${s.submittedVersion?'v'+s.submittedVersion:'尚未提交'}${s.submitted?.codeVersion?` · commit ${commitMarkup(s.submitted.codeVersion)}`:''}</div><p class="small-note" id="submission-edit-state">${s.hasUnsubmittedChanges?'草稿修改尚未正式提交；评审继续使用正式材料。':'更新时间（北京时间）：'+esc(beijingTime(s.updatedAt))}</p><a href="#submitted-summary" id="view-formal-materials">查看最近正式材料</a></section>`+(received && typeof received==='object'?requestReceiptMarkup(received,received.requestId,true):submissionReceiptMarkup(data,received))+judgingMaterialsMarkup(data.judgingMaterials,s.submittedVersion)+localRecoveryMarkup()+teamFormMarkup(data)+'<div id="submission-preview" aria-live="polite"></div><div id="submission-conflict" aria-live="polite"></div>'+`<section class="surface" id="submitted-summary"><h2>最近正式材料</h2>${s.submitted?`<p class="small-note">正式版本 ${s.submittedVersion} · 最近提交 ${esc(beijingTime(s.submittedAt))}（北京时间）</p>${materialMarkup(s.submitted)}`:'<p class="small-note">尚未正式提交；草稿不会发送给评委。</p>'}</section>`+submissionHistoryMarkup()+'<div id="new-api-key" aria-live="polite"></div><section class="surface" id="team-keys"><h2>AI API 密钥</h2><p class="small-note">读取中…</p></section>';
  document.getElementById('view-formal-materials').onclick=e=>{e.preventDefault();document.getElementById('submitted-summary').scrollIntoView({block:'start'});};
  const form=document.getElementById('submission-form'),preview=document.getElementById('submission-preview');let currentVersion=s.version,blocked=!data.window.canEdit,inputRevision=0;
  let observedInputs=JSON.stringify({...rawTeamMaterials(form),confirmed:form.elements.confirmed.checked});function changedInputs(){if(!local.isAuthorized())return;const next=JSON.stringify({...rawTeamMaterials(form),confirmed:form.elements.confirmed.checked});if(next===observedInputs)return;observedInputs=next;inputRevision++;submissionDirty=true;local.input();preview.innerHTML='';document.getElementById('submission-edit-state').textContent='当前修改尚未保存或正式提交；最近正式材料保持不变。';}form.addEventListener('input',changedInputs);form.addEventListener('change',changedInputs);
  const local=installTeamRecovery(data,form,version,{restore(materials,next){for(const [key] of materialFields)form.elements[key].value=materials[key];form.elements.confirmed.checked=false;submissionDirty=true;blocked=!next.window.canEdit;form.querySelector('fieldset').disabled=blocked;preview.innerHTML='';document.getElementById('submission-edit-state').textContent='本地副本已填入表单，尚未保存服务器或正式提交。';},loadLatest(next){if(!local.isAuthorized())return;submissionDirty=false;renderTeam(version,next,false,local);},onWindow(window){if(!window.canEdit){blocked=true;form.querySelector('fieldset').disabled=true;}}},expectedEpoch);
  installSubmissionHistory(document.getElementById('submission-history'),'submission/history',()=>local.isAuthorized());
  function values(){return Object.fromEntries([...materialFields.map(([key])=>[key,form.elements[key].value.trim()]),['confirmed',form.elements.confirmed.checked]]);}
  function validate(materials,formal){const errors=submissionProblems(materials,formal),box=document.getElementById('submission-errors');box.innerHTML=errors.length?`<p>请检查以下 ${errors.length} 项：</p><ul>${errors.map(({key,message})=>`<li><a href="#material-${key}" data-error-field="${key}">${esc(message)}</a></li>`).join('')}</ul>`:'';box.querySelectorAll('[data-error-field]').forEach(link=>link.onclick=e=>{e.preventDefault();form.elements[link.dataset.errorField].focus();form.elements[link.dataset.errorField].scrollIntoView({block:'center'});});if(errors.length)box.focus();return errors.length===0;}
  function preserve(){form.querySelector('fieldset').disabled=false;form.querySelectorAll('input,textarea').forEach(input=>{if(input.type==='checkbox')input.disabled=true;else input.readOnly=true;});form.querySelectorAll('button').forEach(button=>button.disabled=true);preview.innerHTML='';}
  function recovery(message,requestId=null){const box=document.getElementById('submission-conflict');box.innerHTML=`<section class="surface"><h2>当前输入已保留</h2><p>${esc(message)}</p>${requestId?`<p class="request-number">本次请求编号：<code>${esc(requestId)}</code></p><button type="button" class="btn secondary" id="query-submission-request">查询这次请求结果</button><div id="submission-request-result" role="status"></div>`:''}<p>读取最新记录仅供核对，不代表本次操作成功；不会自动重发。请先复制需要保留的输入。</p><button type="button" class="btn secondary" id="load-latest-submission">读取服务器最新记录</button><div id="latest-submission"></div></section>`;if(requestId)document.getElementById('query-submission-request').onclick=e=>withBusy(e.currentTarget,async()=>{if(!local.isAuthorized())return;const receipt=await api('submission/requests/'+encodeURIComponent(requestId));if(!local.isAuthorized())return;document.getElementById('submission-request-result').innerHTML=requestReceiptMarkup(receipt,requestId);if(receipt.requestId===requestId && ['succeeded','rejected'].includes(receipt.status))formError(form,'');});document.getElementById('load-latest-submission').onclick=e=>withBusy(e.currentTarget,async()=>{if(!local.isAuthorized())return;const latest=await api('team');if(!local.isAuthorized())return;document.getElementById('latest-submission').innerHTML=windowMarkup(latest.window)+submissionReceiptMarkup(latest)+judgingMaterialsMarkup(latest.judgingMaterials,latest.submission.submittedVersion)+`<h3>服务器最新草稿 · 编辑版本 ${latest.submission.version}</h3>${materialMarkup(latest.submission.draft)}<button type="button" class="btn secondary" id="restart-submission">使用最新草稿重新编辑</button>`;document.getElementById('restart-submission').onclick=()=>{if(!local.isAuthorized())return;if(confirm('确认放弃上方尚未保存的输入，并使用最新草稿重新编辑？请先复制要保留的内容。')){submissionDirty=false;renderTeam(version,latest,false,local);}};});box.scrollIntoView({block:'start'});}
  async function save(status,materials){if(blocked || !local.isAuthorized())return;blocked=true;form.querySelector('fieldset').disabled=true;preview.querySelectorAll('button').forEach(button=>button.disabled=true);formError(form,'');const requestId=newSubmissionRequestId(),sentInputRevision=inputRevision,sentBaseVersion=currentVersion,sentLocal=local.pending(status,requestId),controller=new AbortController(),timer=setTimeout(()=>controller.abort(),20000);try{const result=await api('submission',{requestId,expectedVersion:currentVersion,status,materials},{signal:controller.signal});if(!matchingSubmissionReceipt(result.receipt,requestId,status,sentBaseVersion))throw new Error('服务器回执无法对应这次请求，请按请求编号核对。');const cleanupError=local.acknowledge(sentLocal);if(!local.isAuthorized())return;currentVersion=result.submission.version;if(inputRevision!==sentInputRevision){local.rebase(result);blocked=false;preview.innerHTML='';toast('已保存发送时的材料；之后的新输入已保留，尚未保存。');return;}submissionDirty=false;toast(status==='submitted'?`正式版 v${result.receipt.historyVersion ?? result.receipt.version} 已收到。`:'草稿已保存，正式材料保持原版本。');await renderTeam(version,result,result.receipt,local);if(cleanupError)document.getElementById('local-draft-status').textContent='服务器已成功保存；本地恢复或旧副本清理未能确认。请核对本地状态，勿据旧副本重复提交。';if(status==='submitted')document.getElementById('submission-receipt')?.focus();}catch(error){local.result(sentLocal,error.status===409?'conflict':!error.status || error.status>=500?'unknown':'editing');if(!local.isAuthorized())return;if(error.status===409){preserve();formError(form,error.message);recovery(error.message.includes('截止') || error.message.includes('未开放')?'提交窗口已关闭或已截止，需联系组织方；当前输入没有写入。':'材料版本已变化，本次未保存；请核对最新草稿后手动合并。');}else if(!error.status || error.status>=500){preserve();formError(form,'结果待确认：网络中断、超时或服务异常，尚不能确定是否保存。');recovery('结果待确认。请按本次请求编号查询，再核对服务器最新记录。',requestId);}else{blocked=error.status===401 || error.status===403;formError(form,error.message);}}finally{clearTimeout(timer);if(form.isConnected && !blocked && local.isAuthorized()){form.querySelector('fieldset').disabled=false;preview.querySelectorAll('button').forEach(button=>button.disabled=false);}}
  }
  document.getElementById('save-submission-draft').onclick=()=>{const materials=values();if(validate(materials,false))return save('draft',materials);};
  form.onsubmit=e=>{e.preventDefault();if(blocked)return;const input=values();if(!validate(input,true))return;const materials={...input,codeVersion:normalizedCommit(input.codeUrl,input.codeVersion)};preview.innerHTML=`<section class="surface" tabindex="-1" id="submission-preview-panel"><h2>确认本次正式材料</h2><p class="small-note">请核对仓库、完整 commit 和材料。这是提交前预览，点击下方确认后才发送。</p><p class="preserve-text">commit 原输入：${esc(input.codeVersion)}<br>将保存的完整哈希：<code>${esc(materials.codeVersion)}</code></p>${materialMarkup(materials)}<div class="form-actions"><button type="button" class="btn" id="confirm-submission">确认正式提交</button><button type="button" class="btn secondary" id="cancel-submission-preview">返回修改</button></div></section>`;document.getElementById('confirm-submission').onclick=()=>save('submitted',materials);document.getElementById('cancel-submission-preview').onclick=()=>{preview.innerHTML='';form.scrollIntoView({block:'start'});};document.getElementById('submission-preview-panel').focus();};
  loadTeamKeys(version);
}
function submissionHistoryMarkup(id='submission-history'){return `<section class="surface submission-history" id="${id}"><h2>正式提交历史</h2><p class="small-note">每次正式提交分别保留当时的填写内容和链接；外链文件可能变化，平台不备份文件。历史不会替换评审已冻结的材料。</p><button type="button" class="btn secondary small" id="${id}-load">读取正式提交历史</button><p id="${id}-message" role="status"></p><div id="${id}-list"></div><div id="${id}-detail"></div></section>`;}
function submissionHistoryItemMarkup(item,admin=false){return `<article class="history-entry"><h3>正式版 v${Number(item.version)} · ${esc(item.projectName || '未填写作品名称')}</h3><p class="small-note">北京时间 ${esc(beijingTime(item.submittedAt))}<br>请求编号：<code>${esc(item.requestId || '旧记录无请求编号')}</code>${item.origin==='legacy_current'?'<br>旧数据迁移记录：仅保留迁移时最近一份正式材料，无法还原更早版本。':''}</p><p>完整 commit：<code>${esc(item.codeVersion || '旧记录未填写')}</code></p><button type="button" class="btn secondary small" data-history-version="${Number(item.version)}">${admin?'查看本版材料与人工检查':'查看本版完整材料'}</button></article>`;}
function installSubmissionHistory(container,path,isCurrent,{admin=false,onDetail}={}){
  const id=container.id,button=document.getElementById(id+'-load'),message=document.getElementById(id+'-message'),list=document.getElementById(id+'-list'),detail=document.getElementById(id+'-detail');
  let cursor=null,started=false,loading=false,detailRead=0,detailGeneration=0;const live=()=>container.isConnected && isCurrent();
  button.onclick=e=>withBusy(e.currentTarget,async()=>{if(loading || !live())return;loading=true;try{const result=await api(path+(cursor===null?'':'?beforeVersion='+cursor));if(!live())return;const html=(result.items || []).map(item=>submissionHistoryItemMarkup(item,admin)).join('');if(started)list.insertAdjacentHTML('beforeend',html);else list.innerHTML=html || '<p class="small-note">尚无正式提交记录。</p>';started=true;cursor=result.nextBeforeVersion;button.hidden=cursor===null;button.textContent='读取更早的正式版本';message.textContent='已读取正式历史；查看材料不会替换当前草稿。';}catch(error){if(live())message.textContent='历史读取未完成：'+error.message;}finally{loading=false;}});
  list.onclick=e=>{const selected=e.target.closest('[data-history-version]');if(!selected || !live())return;if([...detail.querySelectorAll('form')].some(form=>dirtyAdminForms.has(form)) && !confirm('切换正式版本会替换本面板尚未保存的检查输入。请先复制备注。继续？'))return;const edits=[...detail.querySelectorAll('form')].map(form=>[form,form.editRevision || 0]),read=++detailRead;return withBusy(selected,async()=>{try{const item=await api(path+'/'+Number(selected.dataset.historyVersion));if(!live() || read!==detailRead)return;if(edits.some(([form,revision])=>(form.editRevision || 0)!==revision)){message.textContent='查询期间检查输入又有修改，已保留当前面板；请保存或复制后重新选择版本。';return;}for(const form of detail.querySelectorAll('form'))dirtyAdminForms.delete(form);const generation=++detailGeneration;detail.innerHTML=`<section class="history-material"><h3>正式版 v${Number(item.version)} 的材料</h3><p class="small-note">北京时间 ${esc(beijingTime(item.submittedAt))}<br>请求编号：<code>${esc(item.requestId || '旧记录无请求编号')}</code>${item.origin==='legacy_current'?'<br>此为旧数据迁移时保留的最近一份正式材料。':''}</p>${materialMarkup(item.materials)}<div id="${id}-checks"></div></section>`;if(onDetail)await onDetail(item,document.getElementById(id+'-checks'),()=>live() && generation===detailGeneration);}catch(error){if(live() && read===detailRead)message.textContent='本版材料读取未完成：'+error.message;}});};
}
const materialCheckStates=[['unchecked','未检查'],['accessible','可访问'],['permission_required','需补权限'],['unavailable','无法访问（含链接有误）']];
function materialChecksMarkup(data){const editable=data.version===data.latestVersion;return `<h3>人工材料访问检查 · 正式版 v${Number(data.version)}</h3><p class="small-note">${editable?'组织方手动打开链接后记录结果。':'此为旧版检查，只读。当前正式版为 v'+Number(data.latestVersion)+'，新版本不会继承旧版检查。'}未检查不代表无法访问。这是人工访问记录，仅反映当时情况，不代表作品合格或链接永久可用；不影响评审材料、提交状态或成绩。</p><button type="button" class="btn secondary small" data-check-refresh>重新读取本版检查</button><p class="small-note" data-check-message role="status"></p>${data.items.length?data.items.map(item=>{const label=materialFields.find(([key])=>key===item.field)?.[1] || item.field,safe=safeMaterialUrl(item.url);return `<form class="material-check-form" data-check-field="${esc(item.field)}"><h4>${esc(label)}</h4><p class="request-number">${safe?`<a href="${esc(safe)}" target="_blank" rel="noopener noreferrer">${esc(item.url)} ↗</a>`:esc(item.url)}</p><p class="small-note" data-check-meta>记录时间：${esc(beijingTime(item.checkedAt))}（北京时间） · ${esc(item.actor?.name || '尚无人记录')} · 修订 ${Number(item.revision)}</p><label class="field">访问状态<select name="status" ${editable?'':'disabled'}>${materialCheckStates.map(([value,text])=>`<option value="${value}" ${item.status===value?'selected':''}>${text}</option>`).join('')}</select></label><label class="field">备注<textarea name="note" maxlength="500" ${editable?'':'readonly'}>${esc(item.note || '')}</textarea></label>${editable?'<button type="submit" class="btn secondary small">保存此项检查</button>':''}<p class="form-error" role="alert"></p></form>`;}).join(''):'<p class="small-note">本版没有可检查的材料链接。</p>'}`;}
async function installMaterialChecks(container,teamId,version,isCurrent){
  const path='admin/submissions/'+encodeURIComponent(teamId)+'/checks',live=()=>container.isConnected && isCurrent();let model=null,read=0,generation=0;
  async function load(){const current=++read,edits=[...container.querySelectorAll('form')].map(form=>[form,form.editRevision || 0]);try{const next=await api(path+'?version='+version);if(!live() || current!==read)return;if(edits.some(([form,revision])=>(form.editRevision || 0)!==revision)){container.querySelector('[data-check-message]').textContent='查询期间检查输入又有修改，已保留当前输入；请保存或复制后再重新读取。';return;}for(const form of container.querySelectorAll('form'))dirtyAdminForms.delete(form);model=next;generation++;container.innerHTML=materialChecksMarkup(next);bind();}catch(error){if(live() && current===read){const message=container.querySelector('[data-check-message]');if(message)message.textContent='人工检查读取未完成，已保留当前输入：'+error.message;else container.textContent='人工检查读取未完成：'+error.message;}}}
  function bind(){
    const formGeneration=generation;
    container.querySelector('[data-check-refresh]').onclick=()=>{if([...container.querySelectorAll('form')].some(form=>dirtyAdminForms.has(form)) && !confirm('重新读取会替换本面板尚未保存的检查输入。请先复制备注。继续？'))return;return load();};
    for(const form of container.querySelectorAll('[data-check-field]')){trackAdminForm(form);const item=model.items.find(item=>item.field===form.dataset.checkField);form.onsubmit=e=>{e.preventDefault();if(model.version!==model.latestVersion || form.checkPending)return;form.checkPending=true;return withBusy(form.querySelector('button'),async()=>{const editRevision=form.editRevision || 0;formError(form,'');try{const next=await api(path,{version,field:item.field,expectedRevision:item.revision,status:form.elements.status.value,note:form.elements.note.value});if(!live() || generation!==formGeneration)return;read++;const saved=next.items.find(saved=>saved.field===item.field);item.revision=saved.revision;model.latestVersion=next.latestVersion;form.querySelector('[data-check-meta]').textContent='记录时间：'+beijingTime(saved.checkedAt)+'（北京时间） · '+(saved.actor?.name || '组织方')+' · 修订 '+saved.revision;if((form.editRevision || 0)===editRevision)dirtyAdminForms.delete(form);formError(form,(form.editRevision || 0)===editRevision?'此项人工检查已保存。':'已保存发送时的检查；之后的新输入仍未保存。');}catch(error){if(live() && generation===formGeneration)formError(form,error.status===409?'本版或此项检查已有更新，本次没有保存。当前输入已保留，请复制备注后重新读取核对。':error.message);}finally{form.checkPending=false;}});};}
  }
  await load();
}
const submissionOverviewStates=[['empty','未保存'],['draft','仅草稿'],['submitted','已正式提交'],['changes','有未提交修改']];
let submissionAdminView=null;
const submissionAccessPending=new Set();
const submissionOverviewFilters={query:'',status:'all',onlyVersionDifference:false};
function submissionOverviewState(s){return s?.submitted?(s.hasUnsubmittedChanges===true?'changes':'submitted'):s?.draft?'draft':'empty';}
function submissionVersionDifferences(record){const s=record?.submission;if(!s?.submitted || !Number.isSafeInteger(s.submittedVersion))return [];return ['exhibition','roadshow'].flatMap(stage=>{const data=record.judgingMaterials?.[stage],snapshot=data?.materialSnapshot;return data?.inRoster && data.frozenAt!==null && snapshot && Number.isSafeInteger(snapshot.submissionVersion) && snapshot.submissionVersion!==s.submittedVersion?[{stage,version:snapshot.submissionVersion,latestVersion:s.submittedVersion}]:[];});}
function submissionOverviewSelection(data,filters){
  const records=new Map((data.submissions || []).map(record=>[record.teamId,record])),counts=Object.fromEntries(submissionOverviewStates.map(([key])=>[key,0]));
  const query=filters.query.trim().toLocaleLowerCase();
  const all=(data.teams || []).map(team=>{const record=records.get(team.id) || {teamId:team.id,submission:{version:0}},state=submissionOverviewState(record.submission);counts[state]++;return {team,record,state,differences:submissionVersionDifferences(record)};});
  const rows=all.filter(row=>(filters.status==='all' || row.state===filters.status) && (!filters.onlyVersionDifference || row.differences.length) && (!query || [row.team.name,String(row.team.number),String(row.team.number).padStart(2,'0'),row.team.project,row.record.submission?.submitted?.projectName,row.record.submission?.draft?.projectName].some(value=>String(value || '').toLocaleLowerCase().includes(query))));
  return {counts,total:all.length,rows};
}
function createSubmissionOverviewController({data,filters,load,render,isCurrent,onError,onLoaded}){
  const model={data,filters:{...filters},generation:0,latestRead:0,pending:null,refreshAgain:false};
  const repaint=()=>{if(isCurrent())render(model.data,model.filters);};
  const stamp=()=>({model,read:++model.latestRead});
  function replace(next,expected=null){if(!isCurrent() || (expected && (expected.model!==model || expected.read!==model.latestRead)))return false;if(!expected)model.latestRead++;model.data=next;model.generation++;repaint();onLoaded?.();return true;}
  function refresh(repeat=false){
    if(model.pending){if(repeat)model.refreshAgain=true;return model.pending;}
    const task=(async()=>{do{model.refreshAgain=false;const expected=stamp();try{const next=await load();if(!isCurrent())return false;if(model.refreshAgain)continue;replace(next,expected);}catch(error){if(isCurrent() && expected.read===model.latestRead)onError(error);return false;}}while(model.refreshAgain && isCurrent());return true;})();
    model.pending=task;task.finally(()=>{if(model.pending===task)model.pending=null;});return task;
  }
  function stageChanged(stageData){const stage=stageData.stage.id,teams=new Map((stageData.teams || []).map(team=>[team.id,team]));const known=new Set((model.data.teams || []).map(team=>team.id));const changed=!known.size || [...teams.keys()].some(id=>!known.has(id)) || (model.data.teams || []).some(team=>{const before=(model.data.submissions || []).find(record=>record.teamId===team.id)?.judgingMaterials?.[stage],after=teams.get(team.id);return !before || before.runId!==stageData.stage.runId || before.inRoster!==!!after || before.frozenAt!==stageData.stage.frozenAt || (before.materialSnapshot?.submissionVersion ?? null)!==(after?.materialSnapshot?.submissionVersion ?? null);});if(changed)return refresh(true);return Promise.resolve(false);}
  return {model,stamp,replace,refresh,stageChanged,setFilters(next){Object.assign(model.filters,next);repaint();}};
}
function submissionOverviewCard({team:t,record,state,differences}){const s=record.submission || {version:0},access=record.access;return `<section class="surface submission-admin-card"><h3>${esc(teamLabel(t))}</h3><p class="project-line">${esc(s.submitted?.projectName || s.draft?.projectName || t.project || '作品名称待完善')}</p><p class="small-note">${esc(submissionOverviewStates.find(([key])=>key===state)[1])} · 编辑版本 ${Number(s.version || 0)} · 最新正式版 ${s.submittedVersion?'v'+Number(s.submittedVersion):'尚未提交'}<br>更新时间 ${esc(beijingTime(s.updatedAt))}（北京时间） · 提交身份：${access?(access.active?'有效':'已停用'):'未发放'}</p>${differences.length?`<p class="submission-version-warning">${differences.map(item=>`${stageName(item.stage)}本轮 v${item.version}，最新正式 v${item.latestVersion}`).map(esc).join('；')}。重提不会替换本轮快照。</p>`:''}<p class="small-note">${['exhibition','roadshow'].map(stage=>esc(stageName(stage)+'：'+stageMaterialStatus(record.judgingMaterials?.[stage],s.submittedVersion))).join('<br>')}</p><div class="form-actions"><button type="button" class="btn secondary small" data-team-access="${esc(t.id)}" ${submissionAccessPending.has(t.id)?'disabled':''} data-action="${access?'rotate':'issue'}">${access?'重发提交码':'生成提交码'}</button>${access?.active?`<button type="button" class="btn danger small" data-team-access="${esc(t.id)}" ${submissionAccessPending.has(t.id)?'disabled':''} data-action="revoke">停用提交码</button>`:''}</div><button type="button" class="btn secondary small" data-submission-history="${esc(t.id)}">正式历史与人工材料检查</button><details class="submission-details"><summary>查看草稿与正式材料</summary><h4>正式材料${s.submittedVersion?` · 版本 ${Number(s.submittedVersion)}`:''}</h4><p class="small-note">首次提交 ${esc(beijingTime(s.firstSubmittedAt))} · 最近正式提交 ${esc(beijingTime(s.submittedAt))}（北京时间）</p>${materialMarkup(s.submitted)}${judgingMaterialsMarkup(record.judgingMaterials,s.submittedVersion)}<h4>草稿（未提交修改不向评委展示）</h4>${materialMarkup(s.draft)}</details></section>`;}
function submissionOverviewMarkup(){return `<section class="surface" id="submission-overview"><h3>作品提交总览</h3><div id="submission-overview-counts" class="submission-status-counts"></div><div class="submission-filters"><label class="field">搜索队伍<input id="submission-search" type="search" placeholder="队名、队号或项目名" value="${esc(submissionOverviewFilters.query)}"></label><label class="field">提交状态<select id="submission-status-filter"><option value="all">全部状态</option>${submissionOverviewStates.map(([value,label])=>`<option value="${value}" ${submissionOverviewFilters.status===value?'selected':''}>${label}</option>`).join('')}</select></label><label class="toggle-line"><input type="checkbox" id="submission-difference-filter" ${submissionOverviewFilters.onlyVersionDifference?'checked':''}><span>仅看评审材料版本不同</span></label></div><div class="form-actions"><button type="button" class="btn secondary small" id="clear-submission-filters">清除筛选</button><button type="button" class="btn secondary small" id="refresh-submission-overview">刷新总览</button></div><p class="small-note" id="submission-overview-message" role="status">统计仅反映服务器已有记录，无法看到队伍尚未保存的输入；可按需刷新，不改动其他管理表单。</p><p id="submission-overview-error" role="alert"></p><p class="small-note" id="submission-results-count" aria-live="polite"></p></section><div class="submission-admin-list" id="submission-admin-list"></div><div id="admin-submission-detail" aria-live="polite"></div>`;}
function submissionOverviewStamp(){return submissionAdminView?.controller.stamp() || null;}
function updateSubmissionOverview(data,expected=null){return submissionAdminView?.controller.replace(data,expected) || false;}
function installSubmissionOverview(data){
  const container=document.getElementById('submission-overview'),list=document.getElementById('submission-admin-list'),version=routeVersion;
  const alive=()=>submissionAdminView===view && container.isConnected && route==='admin' && routeVersion===version;
  const view={container,list,controller:null};submissionAdminView=view;
  const message=document.getElementById('submission-overview-message'),error=document.getElementById('submission-overview-error');
  function render(next,filters){const selected=submissionOverviewSelection(next,filters);document.getElementById('submission-overview-counts').innerHTML=submissionOverviewStates.map(([key,label])=>`<div><strong>${selected.counts[key]}</strong><span>${label}</span></div>`).join('');document.getElementById('submission-results-count').textContent=`显示 ${selected.rows.length} / 全部 ${selected.total} 队`;list.innerHTML=selected.rows.map(submissionOverviewCard).join('') || `<section class="surface"><p>${selected.total?'没有符合当前筛选的队伍，可清除筛选查看全部。':'尚未录入队伍，请先在队伍名录中添加。'}</p></section>`;}
  view.controller=createSubmissionOverviewController({data,filters:submissionOverviewFilters,load:()=>api('admin'),render,isCurrent:alive,onLoaded:()=>{error.textContent='';message.textContent='总览已更新（北京时间）：'+beijingTime(Date.now())+'；其他管理表单保持原输入。';},onError:e=>{error.textContent='总览未更新，以下保留上次读取结果：'+e.message+' 请点击“刷新总览”重试。';}});
  render(data,view.controller.model.filters);
  function filter(){Object.assign(submissionOverviewFilters,{query:document.getElementById('submission-search').value,status:document.getElementById('submission-status-filter').value,onlyVersionDifference:document.getElementById('submission-difference-filter').checked});view.controller.setFilters(submissionOverviewFilters);}
  document.getElementById('submission-search').oninput=filter;document.getElementById('submission-status-filter').onchange=filter;document.getElementById('submission-difference-filter').onchange=filter;
  document.getElementById('clear-submission-filters').onclick=()=>{document.getElementById('submission-search').value='';document.getElementById('submission-status-filter').value='all';document.getElementById('submission-difference-filter').checked=false;filter();};
  document.getElementById('refresh-submission-overview').onclick=e=>withBusy(e.currentTarget,()=>view.controller.refresh());
  list.onclick=e=>{const history=e.target.closest('[data-submission-history]');if(history?.dataset.submissionHistory){const panel=document.getElementById('admin-submission-detail');if([...panel.querySelectorAll('form')].some(form=>dirtyAdminForms.has(form)) && !confirm('切换队伍会替换本面板尚未保存的检查输入。请先复制备注。继续？'))return;for(const form of panel.querySelectorAll('form'))dirtyAdminForms.delete(form);const teamId=history.dataset.submissionHistory,team=view.controller.model.data.teams.find(team=>team.id===teamId);panel.innerHTML=`<h2>${esc(teamLabel(team))} · 正式材料记录</h2>`+submissionHistoryMarkup('admin-history');const historyContainer=document.getElementById('admin-history');installSubmissionHistory(historyContainer,'admin/submissions/'+encodeURIComponent(teamId)+'/history',()=>alive() && historyContainer.isConnected,{admin:true,onDetail:(item,target,isCurrent)=>installMaterialChecks(target,teamId,item.version,isCurrent)});panel.scrollIntoView({block:'start'});return;}const button=e.target.closest('[data-team-access]');if(!button || button.disabled || submissionAccessPending.has(button.dataset.teamAccess))return;const teamId=button.dataset.teamAccess;submissionAccessPending.add(teamId);withBusy(button,async()=>{const action=button.dataset.action;if(action!=='issue' && !confirm(action==='rotate'?'重发后旧提交码及该队旧登录会话立即失效，已保存材料保留。继续？':'停用后该队登录会话立即失效，已保存材料保留。继续？'))return;const result=await api('admin/team-access',{teamId:button.dataset.teamAccess,action});if(!alive())return;if(result.code){const box=document.getElementById('team-access-code');box.innerHTML=`<div class="code-output"><strong>队伍专属提交码</strong><code>${esc(result.code)}</code><p>仅显示一次，请立即复制并单独发给该队。</p><button type="button" class="btn secondary small" data-copy-value="${esc(result.code)}">复制提交码</button></div>`;box.scrollIntoView({block:'center'});}toast(action==='revoke'?'提交码已停用。':'队伍提交码已生成，请单独发放。');await view.controller.refresh(true);}).finally(()=>{submissionAccessPending.delete(teamId);const active=submissionAdminView;if(active?.container.isConnected && route==='admin')for(const current of active.list.querySelectorAll?.('[data-team-access]') || [])if(current.dataset.teamAccess===teamId)current.disabled=false;});};
}

function installSubmissionAdmin(data){const cfg=data.config || {};root.insertAdjacentHTML('beforeend',`<div class="admin-section-title"><h2>作品提交管理</h2><a class="btn secondary small" href="api/admin/submissions/export" download>导出项目提交 CSV ↓</a></div><section class="surface"><h3>提交窗口与截止</h3><form id="submission-settings"><label class="toggle-line"><input type="checkbox" name="submissionsOpen" ${cfg.submissionsOpen?'checked':''}>开放作品提交（独立于评分与投票）</label><label class="field">截止时间（北京时间）<input name="submissionDeadline" type="datetime-local" step="60" value="${esc(beijingInput(cfg.submissionDeadline))}"><small>留空表示不设截止；到点服务器自动锁定。补交时延长截止并重新开放。建议先关闭提交，再使用正式材料评审。</small></label><button class="btn small" type="submit">保存提交窗口</button><p class="form-error" role="alert"></p></form></section><div id="team-access-code" aria-live="polite"></div>${submissionOverviewMarkup()}`);
  installSettingsForm(document.getElementById('submission-settings'),'submissions',cfg);
  installSubmissionOverview(data);
}

async function renderRoute(){
  if(typeof disposeAdminReviewSync==='function')disposeAdminReviewSync();adminReadOnlyView=null;adminLoadPending=null;
  clearTimeout(pollTimer);const version=++routeVersion;
  route=['team','judge','judge-exhibition','board-exhibition','judge-roadshow','board-roadshow','audience','admin'].includes(location.hash.slice(1))?location.hash.slice(1):'board';
  lastBoard=null;document.body.classList.remove('screen-mode');
  document.querySelectorAll('[data-route]').forEach(a=>{a.classList.toggle('active',a.dataset.route===route);if(a.dataset.route===route)a.setAttribute('aria-current','page');else a.removeAttribute('aria-current');});
  document.title=`${{board:'成绩看板','board-exhibition':'展示评审进度（未计分）','board-roadshow':'路演评审进度（未计分）','judge-roadshow':'路演评审记录',team:'作品提交',judge:'专业评分（六维100分）','judge-exhibition':'展示评审记录',audience:'观众投票',admin:'组织方后台'}[route]} · GOTC 2026`;
  account();root.innerHTML='<div class="loading-state">正在读取活动数据…</div>';
  const requiredRole=route.startsWith('judge-')?'judge':route;
  try{if(route==='board')return await renderBoard(version);if(route==='board-exhibition' || route==='board-roadshow')return await renderExhibitionBoard(version);if(!session.role){renderLogin(requiredRole);return;}if(session.role!==requiredRole){renderWrongRole(requiredRole);return;}if(route==='team')await renderTeam(version);if(route==='judge')await renderJudge(version);if(route==='judge-exhibition' || route==='judge-roadshow')await renderExhibitionJudge(version);if(route==='audience')await renderAudience(version);if(route==='admin')await renderAdmin(version);}
  catch(e){if(version===routeVersion){if(e.status===401 && !['board','board-exhibition','board-roadshow'].includes(route)){account();renderLogin(requiredRole);}else showConnectionError(e);}}
}
// Cheap models cost fractions of a cent per call, so small amounts keep 4 decimals.
const usd=v=>v===null || v===undefined?'—':`$${Number(v).toFixed(Number(v)!==0 && Math.abs(v)<1?4:2)}`;
async function loadTeamKeys(version){const box=document.getElementById('team-keys');let data;try{data=await api('team/keys');}catch(e){if(version===routeVersion && box?.isConnected)box.innerHTML=`<h2>AI API 密钥</h2><p class="small-note">${esc(e.message)}</p>`;return;}if(version!==routeVersion || !box?.isConnected)return;
  if(!data.enabled){box.innerHTML='<h2>AI API 密钥</h2><p class="small-note">本队 API 额度尚未开通或已停用，请联系组织方。</p>';return;}
  teamKeyInfo=data;const b=data.budget,keys=data.keys,full=keys.length>=data.maxKeys,models=Object.entries(data.models).map(([kind,list])=>`${kind==='openai'?'OpenAI 兼容':'Anthropic'}：${list.map(esc).join('、')}`).join('<br>');
  box.innerHTML=`<h2>AI API 密钥</h2>${data.announcement?`<div class="info-banner warning"><strong>组织方公告</strong><br>${esc(data.announcement)}</div>`:''}<p class="small-note">OpenAI 兼容 SDK 的 base_url：<code>${esc(data.baseUrl)}/v1</code><br>Anthropic SDK 的 base_url：<code>${esc(data.baseUrl)}</code><br>${models}</p>${metrics([[usd(b.used24h),`近24小时全队已用 · 上限 ${usd(b.dailyLimit)}`],[usd(b.usedTotal),`累计全队已用 · 上限 ${usd(b.totalLimit)}`],[`${keys.length} / ${data.maxKeys}`,'有效密钥'],[b.expiresAt===null?'未设置':beijingTime(b.expiresAt),'额度到期（北京时间）']])}${data.logContent?'<div class="info-banner">组织方会记录每次 API 调用的请求与回复内容（不含密钥），用于额度核对与安全审查，请勿发送个人隐私或敏感信息。</div>':''}${data.clientRules.length?`<p class="small-note">客户端限制（按请求头判断）：${data.clientRules.map(r=>`${r.action==='allow'?'允许':'禁止'} ${esc(r.header)} 含“${esc(r.contains)}”`).join('；')}</p>`:''}<div class="info-banner warning">全队所有密钥共用一份额度。密钥只在创建时显示一次，请放入环境变量，切勿提交到代码仓库；疑似泄露请立即删除并重新创建。</div><div class="invite-list">${keys.map(k=>`<div class="invite-row"><span>${esc(k.name)}<small><code>${esc(k.masked)}</code> · 近24小时 ${usd(k.used24h)} · 累计 ${usd(k.usedTotal)} · ${Number(k.requests)} 次请求 · 最近使用 ${esc(beijingTime(k.lastUsedAt))}</small></span><button type="button" class="btn danger small" data-key-delete="${esc(k.id)}">删除</button></div>`).join('') || '<p class="small-note">尚未创建密钥。</p>'}</div>${setupSnippets(data,'<你的密钥>')}<h3>本队最近调用</h3><div id="team-calls"><p class="small-note">读取中…</p></div><form id="key-form"><div class="settings-row"><label class="field">新密钥名称<input name="name" required maxlength="40" placeholder="例如：alice-laptop" ${full?'disabled':''}></label><button class="btn small" type="submit" ${full?'disabled':''}>创建密钥 ↗</button></div><p class="form-error" role="alert"></p></form>`;
  const form=document.getElementById('key-form');form.onsubmit=e=>{e.preventDefault();withBusy(form.querySelector('button'),async()=>{formError(form,'');try{const result=await api('team/keys',{name:form.elements.name.value.trim()});if(version!==routeVersion)return;const out=document.getElementById('new-api-key');out.innerHTML=`<div class="code-output"><strong>新密钥 · ${esc(result.item.name)}</strong><code>${esc(result.key)}</code><p>仅显示这一次，离开或刷新页面后无法再查看。请立即复制保存。</p><button type="button" class="btn secondary small" id="copy-api-key">复制密钥</button>${setupSnippets(teamKeyInfo,result.key)}</div>`;document.getElementById('copy-api-key').onclick=async()=>{try{await navigator.clipboard.writeText(result.key);toast('密钥已复制。');}catch{toast('请选中密钥手动复制。');}};out.scrollIntoView({block:'center'});await loadTeamKeys(version);}catch(error){formError(form,error.message);}});};
  box.querySelectorAll('[data-key-delete]').forEach(button=>button.onclick=()=>withBusy(button,async()=>{if(!confirm('删除后使用该密钥的程序立即无法调用，确认删除？'))return;await api('team/keys/delete',{id:button.dataset.keyDelete});toast('密钥已删除。');await loadTeamKeys(version);}));
  api('team/keys/logs').then(logs=>{const el=document.getElementById('team-calls');if(el?.isConnected)el.innerHTML=callTable(logs.items,false);}).catch(e=>{const el=document.getElementById('team-calls');if(el)el.innerHTML=`<p class="small-note">${esc(e.message)}</p>`;});
}
async function loadKeyAdmin(teams,version){const box=document.getElementById('key-admin');let data;try{data=await api('admin/keys');}catch(e){if(version===routeVersion && box?.isConnected)box.innerHTML=`<p class="small-note">${esc(e.message)}</p>`;return;}if(version!==routeVersion || !box?.isConnected)return;
  const reload=()=>loadKeyAdmin(teams,version);data.upstreams ||= {};
  if(!data.enabled){box.innerHTML=upstreamMarkup(data.upstreams,true)+'<p class="small-note">填写并保存至少一个模型上游后，即可为队伍开通额度。</p>';installUpstreams(box,data.upstreams,reload);return;}
  const s=data.settings,o=data.overview,ranked=[...data.teams].sort((a,b)=>b.used24h-a.used24h);
  box.innerHTML=upstreamMarkup(data.upstreams,false)+`<div class="section-caption"><h3>今日概况（北京时间）</h3><a class="btn secondary small" href="api/admin/keys/export" download>导出用量 CSV ↓</a></div>${metrics([[Number(o.requests),`请求 · 失败 ${Number(o.errors)}`],[usd(o.cost),'费用'],[tokens(o.input),`输入 tokens · 输出 ${tokens(o.output)}`],[Number(o.teams),`调用的队伍 · 平均 ${o.avgMs===null?'—':Number(o.avgMs)+'ms'}`]])}<h3>近24小时消耗排行</h3><div class="table-wrap"><table class="board-table usage-table"><thead><tr><th>#</th><th>队伍</th><th>近24小时</th><th>占日额度</th><th>累计</th><th>进行中</th></tr></thead><tbody>${ranked.map((r,i)=>{const t=teams.find(x=>x.id===r.teamId);return `<tr><td>${i+1}</td><td>${esc(t?teamLabel(t):r.teamId)}${r.active?'':' <small>已停用</small>'}</td><td>${usd(r.used24h)}</td><td class="${r.used24h>=s.dailyBudget?'status-error':''}">${s.dailyBudget?Math.round(r.used24h/s.dailyBudget*100):0}%</td><td>${usd(r.usedTotal)}</td><td>${Number(r.inflight)}</td></tr>`;}).join('') || '<tr><td colspan="6">尚未开通队伍</td></tr>'}</tbody></table></div><h3>设置</h3><form id="key-settings"><div class="form-grid"><label class="field">每队近24小时额度（美元）<input name="dailyBudget" type="number" min="0" max="100000" step="0.01" value="${Number(s.dailyBudget)}" required></label><label class="field">每队累计额度（美元）<input name="totalBudget" type="number" min="0" max="100000" step="0.01" value="${Number(s.totalBudget)}" required></label><label class="field">每队密钥上限<input name="maxKeys" type="number" min="1" max="20" value="${Number(s.maxKeys)}" required></label><label class="field">额度到期（北京时间）<input name="expiresAt" type="datetime-local" step="60" value="${esc(beijingInput(s.expiresAt))}"><small>留空表示不过期；到点后所有队伍的密钥停止调用。</small></label><label class="field">每队每分钟请求上限<input name="teamRpm" type="number" min="0" max="10000" value="${Number(s.teamRpm)}" required><small>0 为不限。另有每队最多 4 个并发请求，超出时排队最多 30 秒。</small></label><label class="field">屏蔽词（每行一个）<textarea name="blockedWords" rows="3" placeholder="命中即拒绝，不区分大小写">${esc(s.blockedWords.join('\n'))}</textarea></label><label class="field wide">队伍公告<textarea name="announcement" rows="2" maxlength="500" placeholder="显示在所有队伍的密钥卡片顶部，留空不显示">${esc(s.announcement)}</textarea></label><label class="field wide">客户端规则（每行一条）<textarea name="clientRules" rows="4" placeholder="allow User-Agent: claude-cli&#10;deny User-Agent: openclaw">${esc(s.clientRules.map(r=>`${r.action} ${r.header}: ${r.contains}`).join('\n'))}</textarea><small>格式“allow|deny 请求头: 包含的文字”，不区分大小写。命中任一 deny 即拒绝；设置了 allow 时，必须命中其中一条。请求头可被伪造，只能约束常规使用。</small><span class="form-actions">${Object.keys(clientRuleTemplates).map(name=>`<button type="button" class="btn secondary small" data-rule-template="${esc(name)}">${esc(name)}</button>`).join('')}</span></label></div><label class="toggle-line"><input type="checkbox" name="logContent" ${s.logContent?'checked':''}>记录完整请求上下文与回复（压缩保存，队伍页会提示）</label><button class="btn small" type="submit">保存 API 设置</button><p class="form-error" role="alert"></p></form><div class="invite-list">${teams.map(t=>{const r=data.teams.find(x=>x.teamId===t.id);return `<div class="invite-row"><span>${esc(teamLabel(t))}<small>${r?`${r.active?'已开通':'已停用'} · ${Number(r.keyCount)} 个密钥 · 近24小时 ${usd(r.used24h)} · 累计 ${usd(r.usedTotal)}`:'未开通'}</small></span><span class="form-actions">${r?`<button type="button" class="btn secondary small" data-key-stats="${esc(t.id)}">用量详情</button><button type="button" class="btn secondary small" data-key-logs="${esc(t.id)}">调用记录</button>`:''}${r?.active?`<button type="button" class="btn danger small" data-key-revoke="${esc(t.id)}">停用额度</button>`:`<button type="button" class="btn secondary small" data-key-provision="${esc(t.id)}">${r?'重新开通':'开通额度'}</button>`}</span></div>`;}).join('') || '<p class="small-note">录入队伍后可开通。</p>'}</div><div class="form-actions"><button type="button" class="btn secondary small" data-key-stats="">全部队伍用量</button><button type="button" class="btn secondary small" data-key-logs="">全部调用记录</button></div><div id="key-detail" aria-live="polite"></div><details class="reset-panel"><summary>API 管理操作记录（最近50条）</summary><div class="invite-list">${data.changes.map(c=>{const t=teams.find(x=>x.id===c.target);return `<div class="invite-row"><span>${esc(changeName[c.action] || c.action)} ${esc(t?teamLabel(t):c.target)}<small>${esc(beijingTime(c.at))} · ${esc(JSON.stringify(c.metadata)).slice(0,300)}</small></span></div>`;}).join('') || '<p class="small-note">暂无记录。</p>'}</div></details>`;
  const label=id=>{const t=teams.find(x=>x.id===id);return t?teamLabel(t):'全部队伍';};
  box.querySelectorAll('[data-key-stats]').forEach(b=>b.onclick=()=>withBusy(b,()=>showKeyStats({teamId:b.dataset.keyStats || null},label(b.dataset.keyStats),7)));
  box.querySelectorAll('[data-key-logs]').forEach(b=>b.onclick=()=>withBusy(b,()=>showKeyLogs({teamId:b.dataset.keyLogs || null},label(b.dataset.keyLogs))));
  installUpstreams(box,data.upstreams,reload);
  box.querySelectorAll('[data-rule-template]').forEach(b=>b.onclick=()=>{const area=document.getElementById('key-settings').elements.clientRules;area.value=clientRuleTemplates[b.dataset.ruleTemplate];area.dispatchEvent(new Event('input',{bubbles:true}));toast('已填入规则模板，确认后点击“保存 API 设置”生效。');});
  const form=document.getElementById('key-settings');form.onsubmit=e=>{e.preventDefault();withBusy(form.querySelector('button'),async()=>{formError(form,'');try{await api('admin/keys/settings',{dailyBudget:Number(form.elements.dailyBudget.value),totalBudget:Number(form.elements.totalBudget.value),maxKeys:Number(form.elements.maxKeys.value),expiresAt:parseBeijingInput(form.elements.expiresAt.value),logContent:form.elements.logContent.checked,clientRules:parseClientRules(form.elements.clientRules.value),teamRpm:Number(form.elements.teamRpm.value),announcement:form.elements.announcement.value.trim(),blockedWords:form.elements.blockedWords.value.split('\n').map(w=>w.trim()).filter(Boolean)});toast('API 设置已保存。');await loadKeyAdmin(teams,version);}catch(error){formError(form,error.message);}});};
  box.querySelectorAll('[data-key-provision],[data-key-revoke]').forEach(button=>button.onclick=()=>withBusy(button,async()=>{const revoke='keyRevoke' in button.dataset;if(revoke && !confirm('停用后该队全部密钥立即无法调用，确认停用？'))return;await api(revoke?'admin/keys/revoke':'admin/keys/provision',{teamId:button.dataset.keyRevoke || button.dataset.keyProvision});toast(revoke?'该队 API 额度已停用。':'该队 API 额度已开通，队伍可自行创建密钥。');await loadKeyAdmin(teams,version);}));
}
let teamKeyInfo=null;
const changeName={'api-key.create':'创建密钥','api-key.delete':'删除密钥','api-key.provision':'开通额度','api-key.revoke':'停用额度','api-key.settings':'修改 API 设置','api-log.view':'查看调用内容','api-usage.export':'导出用量'};
// Failure classification copied from sub2api's ops error log: owner = whose fault, phase = where it failed.
const ownerName={client:'学生请求',provider:'上游',platform:'平台'};
const phaseName={request:'请求校验',auth:'鉴权',upstream:'上游',network:'网络',internal:'内部'};
function failureNote(i){if(!i.errorType)return '';const title=[i.errorPhase && `阶段：${phaseName[i.errorPhase] || i.errorPhase}`,i.upstreamStatus!==null && i.upstreamStatus!==undefined && `上游状态：${Number(i.upstreamStatus)}`,i.errorMessage].filter(Boolean).join('\n');
  return `<br><small class="failure-note" title="${esc(title)}">${esc(ownerName[i.errorOwner] || i.errorOwner || '')} · ${esc(i.errorType)}${i.upstreamStatus!==null && i.upstreamStatus!==undefined?` · 上游 ${Number(i.upstreamStatus)}`:''}</small>${i.errorMessage?`<br><small class="failure-message">${esc(String(i.errorMessage).slice(0,120))}</small>`:''}`;}
function callTable(items,admin){return `<div class="table-wrap"><table class="board-table usage-table"><thead><tr><th>时间（北京）</th>${admin?'<th>队伍 / 密钥</th>':'<th>密钥</th>'}<th>模型</th><th>状态</th><th>输入 / 输出 tokens</th><th>费用</th><th>耗时</th><th>客户端</th>${admin?'<th></th>':''}</tr></thead><tbody>${items.map(i=>`<tr><td>${esc(beijingTime(i.at))}</td><td>${admin?`${esc(String(i.teamNumber).padStart(2,'0'))} · ${esc(i.teamName)}<br><small>${esc(i.keyName)}</small>`:esc(i.keyName)}</td><td>${esc(i.model || '—')}<br><small>${esc(i.path)}${i.stream?' · 流式':''}</small></td><td class="${i.status>=400 || i.incomplete?'status-error':''}">${Number(i.status)}${i.incomplete?'<br><small>流中断</small>':''}${i.estimated?'<br><small title="上游未返回完整用量，按请求与回复大小估算计费">估算计费</small>':''}${failureNote(i)}</td><td>${tokens(i.input)} / ${tokens(i.output)}${i.cacheRead?`<br><small>缓存读取 ${tokens(i.cacheRead)}</small>`:''}</td><td>${usd(i.cost)}</td><td>${Number(i.durationMs)}ms</td><td class="ua-cell">${esc(i.userAgent || '—')}</td>${admin?`<td>${i.hasContent?`<button type="button" class="link-button" data-log-id="${Number(i.id)}">查看上下文</button><br><small>${kb(i.requestBytes || 0)}</small>`:'<small>未记录</small>'}</td>`:''}</tr>${admin?`<tr class="log-row" hidden id="log-${Number(i.id)}"><td colspan="9"></td></tr>`:''}`).join('') || `<tr><td colspan="${admin?9:8}">暂无调用记录</td></tr>`}</tbody></table></div>`;}
function setupSnippets(info,key){if(!info)return '';const base=info.baseUrl,claude=(info.models.anthropic || [])[0],openai=(info.models.openai || [])[0],tabs=[];
  if(claude){tabs.push(['Claude Code',`# 终端临时使用（macOS / Linux）\nexport ANTHROPIC_BASE_URL="${base}"\nexport ANTHROPIC_AUTH_TOKEN="${key}"\nexport ANTHROPIC_MODEL="${claude}"\nclaude\n\n# 或写入 ~/.claude/settings.json\n{\n  "env": {\n    "ANTHROPIC_BASE_URL": "${base}",\n    "ANTHROPIC_AUTH_TOKEN": "${key}",\n    "ANTHROPIC_MODEL": "${claude}"\n  }\n}`]);
    tabs.push(['Anthropic Python',`from anthropic import Anthropic\n\nclient = Anthropic(base_url="${base}", api_key="${key}")\nmessage = client.messages.create(\n    model="${claude}", max_tokens=1024,\n    messages=[{"role": "user", "content": "你好"}],\n)\nprint(message.content[0].text)`]);}
  if(openai){tabs.push(['OpenAI Python',`from openai import OpenAI\n\nclient = OpenAI(base_url="${base}/v1", api_key="${key}")\nreply = client.chat.completions.create(\n    model="${openai}",\n    messages=[{"role": "user", "content": "你好"}],\n)\nprint(reply.choices[0].message.content)`]);
    tabs.push(['curl',`curl ${base}/v1/chat/completions \\\n  -H "Authorization: Bearer ${key}" \\\n  -H "Content-Type: application/json" \\\n  -d '{"model": "${openai}", "messages": [{"role": "user", "content": "你好"}]}'`]);}
  return `<div class="setup-snippets"><h3>接入示例</h3>${tabs.map(([name,code])=>`<details><summary>${esc(name)}</summary><pre class="log-content">${esc(code)}</pre></details>`).join('')}<p class="small-note">密钥请放在环境变量或 .env 文件中，并把 .env 加入 .gitignore。</p></div>`;}
// Model upstreams (like sub2api channels): base URL, API key, model whitelist with prices, aliases.
const upstreamKinds={openai:'OpenAI 兼容（/v1/chat/completions）',anthropic:'Anthropic（/v1/messages）'};
const upstreamPresets={'Gitee AI（模力方舟）':['openai','https://ai.gitee.com/v1'],'DeepSeek':['openai','https://api.deepseek.com/v1'],'Anthropic 官方':['anthropic','https://api.anthropic.com/v1']};
const modelsText=models=>Object.entries(models || {}).map(([m,p])=>[m,...p].join(' ')).join('\n');
const aliasesText=aliases=>Object.entries(aliases || {}).map(([a,m])=>`${a} => ${m}`).join('\n');
function parseModels(text,kind){const models={};for(const line of text.split('\n').map(l=>l.trim()).filter(Boolean)){const [name,...nums]=line.split(/\s+/);const prices=nums.map(Number);if(!(prices.length===2 || prices.length===4) || prices.some(n=>!Number.isFinite(n) || n<0))throw new Error(`${upstreamKinds[kind]} 模型行格式不正确：${line}（应为“模型名 输入价 输出价”或再加“缓存写入价 缓存读取价”）`);models[name]=prices;}return models;}
function parseAliases(text,kind){const aliases={};for(const line of text.split('\n').map(l=>l.trim()).filter(Boolean)){const m=line.match(/^(\S+)\s*=>\s*(\S+)$/);if(!m)throw new Error(`${upstreamKinds[kind]} 别名格式不正确：${line}（应为“别名 => 模型名”）`);aliases[m[1]]=m[2];}return aliases;}
function upstreamMarkup(upstreams,open){const count=Object.values(upstreams).reduce((n,u)=>n+Object.keys(u.models || {}).length,0);
  return `<details class="reset-panel upstream-panel" ${open?'open':''}><summary>模型上游（${Object.keys(upstreams).length} 个上游 · ${count} 个模型）</summary><form id="upstream-form"><p class="small-note">队伍只能调用这里列出的模型。单价为每百万 tokens 的美元价（人民币价格请按汇率换算），与队伍额度同一单位；只写输入、输出价时，缓存写入按输入价 1.25 倍、缓存读取按 0.1 倍计。每类上游的第一个模型是队伍页接入示例的默认模型。</p><div class="form-actions">${Object.keys(upstreamPresets).map(n=>`<button type="button" class="btn secondary small" data-upstream-preset="${esc(n)}">填入 ${esc(n)}</button>`).join('')}</div>
  ${Object.entries(upstreamKinds).map(([kind,label])=>{const u=upstreams[kind];return `<fieldset class="upstream-block" data-kind="${kind}"><label class="toggle-line"><input type="checkbox" name="${kind}-on" ${u?'checked':''}><strong>${label}</strong></label><div class="form-grid"><label class="field">接口地址（Base URL）<input name="${kind}-url" value="${esc(u?.url || '')}" placeholder="https://…/v1"><small>只接受 HTTPS（本机地址除外）。</small></label><label class="field">API Key<input name="${kind}-key" type="password" autocomplete="off" placeholder="${u?`已保存 ${esc(u.keyHint)}，留空不修改`:'上游提供的密钥'}"><small>保存后不再显示，队伍看不到。</small></label><label class="field">开放模型（每行：模型名 输入价 输出价 [缓存写入价 缓存读取价]）<textarea name="${kind}-models" rows="5" placeholder="${kind==='openai'?'DeepSeek-V3 0.3 1.2':'claude-sonnet-5-5 3 15 3.75 0.3'}">${esc(modelsText(u?.models))}</textarea></label><label class="field">模型别名（每行：别名 => 模型名，* 结尾为前缀）<textarea name="${kind}-aliases" rows="5" placeholder="${kind==='openai'?'gpt-4o => DeepSeek-V3':'claude-* => claude-sonnet-5-5'}">${esc(aliasesText(u?.aliases))}</textarea><small>${kind==='anthropic'?'使用 Claude Code 时需要 claude-* 前缀别名。':'客户端写死模型名时可用别名映射。'}</small></label></div></fieldset>`;}).join('')}
  <button class="btn small" type="submit">保存模型上游</button><p class="form-error" role="alert"></p></form></details>`;}
function installUpstreams(box,upstreams,reload){const form=document.getElementById('upstream-form');if(!form?.elements)return;
  document.querySelectorAll('[data-upstream-preset]').forEach(b=>b.onclick=()=>{const [kind,url]=upstreamPresets[b.dataset.upstreamPreset];form.elements[`${kind}-on`].checked=true;form.elements[`${kind}-url`].value=url;form.elements[`${kind}-key`].focus();toast('已填入接口地址，请填写 API Key 和开放模型后保存。');});
  form.onsubmit=e=>{e.preventDefault();withBusy(form.querySelector('[type=submit]'),async()=>{formError(form,'');try{const result={};for(const kind of Object.keys(upstreamKinds)){if(!form.elements[`${kind}-on`].checked)continue;
      result[kind]={url:form.elements[`${kind}-url`].value.trim(),key:form.elements[`${kind}-key`].value.trim(),models:parseModels(form.elements[`${kind}-models`].value,kind),aliases:parseAliases(form.elements[`${kind}-aliases`].value,kind)};}
    const removed=Object.keys(upstreams).filter(k=>!result[k]);if(removed.length && !confirm(`将停用 ${removed.map(k=>upstreamKinds[k]).join('、')}，相关请求会立即失败。继续？`))return;
    await api('admin/keys/upstreams',{upstreams:result});toast('模型上游已保存，立即生效。');await reload();}catch(error){formError(form,error.message);}});};}
// Readable view of a stored call: the full request context and the reply rebuilt from JSON or a stream.
const kb=n=>n>=1048576?`${(n/1048576).toFixed(1)} MB`:`${Math.max(1,Math.round(n/1024))} KB`;
// Stored calls keep whatever the client sent, so every shape check is defensive (as sub2api's ops detail modal):
// parse with a raw-text fallback, check types before iterating, and never let one bad field hide the raw record.
const asList=v=>Array.isArray(v)?v:[];
const asObject=v=>v && typeof v==='object' && !Array.isArray(v)?v:null;
const asText=v=>typeof v==='string'?v:v===null || v===undefined?'':JSON.stringify(v);
function prettyJSON(raw){try{return JSON.stringify(JSON.parse(raw),null,2);}catch{return asText(raw);}}
function blockText(content){if(content===null || content===undefined)return '';if(typeof content==='string')return content;if(!Array.isArray(content))return JSON.stringify(content,null,2);
  return content.map(item=>{if(typeof item==='string')return item;const b=asObject(item);if(!b)return JSON.stringify(item);switch(b.type){case 'text':return asText(b.text);case 'thinking':return `【思考】\n${asText(b.thinking)}`;case 'redacted_thinking':return '【思考（已加密）】';case 'image':case 'image_url':case 'input_image':return '【图片】';case 'document':return '【文档】';
    case 'tool_use':return `【调用工具 ${asText(b.name)}】\n${JSON.stringify(b.input,null,2)}`;case 'tool_result':return `【工具结果${b.is_error?'（出错）':''}】\n${blockText(b.content)}`;default:return JSON.stringify(b,null,2);}}).join('\n\n');}
function openaiCalls(calls){return asList(calls).map(c=>{const f=asObject(asObject(c)?.function) || {};return `【调用工具 ${asText(f.name)}】\n${asText(f.arguments)}`;}).join('\n\n');}
function conversationOf(text){let parsed;try{parsed=JSON.parse(text);}catch{return null;}const req=asObject(parsed);if(!req)return null;const turns=[];
  if(req.system)turns.push(['system',blockText(req.system)]);
  if(req.messages!==undefined && !Array.isArray(req.messages))turns.push(['（messages 不是列表）',JSON.stringify(req.messages,null,2)]);
  for(const item of asList(req.messages)){const m=asObject(item);if(!m){turns.push(['（无法识别的消息）',JSON.stringify(item)]);continue;}const parts=[blockText(m.content),openaiCalls(m.tool_calls)].filter(Boolean);turns.push([asText(m.role) || '?',parts.join('\n\n')+(m.tool_call_id?'\n\n（工具结果）':'')]);}
  const meta=['model','max_tokens','temperature','top_p','stream','thinking','reasoning_effort','tool_choice'].filter(k=>req[k]!==undefined).map(k=>`${k}: ${typeof req[k]==='object'?JSON.stringify(req[k]):req[k]}`);
  const tools=asList(req.tools).map(t=>{const o=asObject(t) || {},f=asObject(o.function) || {};return {name:asText(o.name || f.name) || '?',description:asText(o.description || f.description)};});
  return {meta,turns,tools};}
function replyOf(text){const trimmed=asText(text).trim();if(!trimmed)return '';
  if(trimmed.startsWith('{')){try{const r=asObject(JSON.parse(trimmed));if(!r)return trimmed;if(r.content)return blockText(r.content);const m=asObject(asObject(asList(r.choices)[0])?.message);if(m)return [m.reasoning_content?`【思考】\n${asText(m.reasoning_content)}`:'',asText(m.content),openaiCalls(m.tool_calls)].filter(Boolean).join('\n\n');return JSON.stringify(r,null,2);}catch{return trimmed;}}
  if(!trimmed.split('\n').some(line=>line.startsWith('data:')))return trimmed;  // plain text (e.g. a gateway error), not a stream
  const blocks=new Map(),calls=new Map();let reasoning='',content='',stop='',finished=false;
  for(const line of trimmed.split('\n')){if(!line.startsWith('data:'))continue;if(line.slice(5).trim()==='[DONE]'){finished=true;continue;}let e;try{e=asObject(JSON.parse(line.slice(5)));}catch{continue;}if(!e)continue;
    if(e.type==='message_stop')finished=true;
    if(e.type==='content_block_start'){const c=asObject(e.content_block) || {};blocks.set(e.index,{type:c.type,name:asText(c.name),text:asText(c.text),thinking:'',json:''});}
    else if(e.type==='content_block_delta'){if(!blocks.has(e.index))blocks.set(e.index,{type:'text',text:'',thinking:'',json:''});const blk=blocks.get(e.index),dl=asObject(e.delta) || {};blk.text+=asText(dl.text);blk.thinking+=asText(dl.thinking);blk.json+=asText(dl.partial_json);}
    else if(e.type==='message_delta')stop=asText(asObject(e.delta)?.stop_reason) || stop;
    else if(e.choices!==undefined){const choice=asObject(asList(e.choices)[0]) || {},dl=asObject(choice.delta) || {};content+=asText(dl.content);reasoning+=asText(dl.reasoning_content);
      for(const item of asList(dl.tool_calls)){const c=asObject(item);if(!c)continue;const id=c.index ?? 0;if(!calls.has(id))calls.set(id,{name:'',args:''});const slot=calls.get(id),f=asObject(c.function) || {};slot.name+=asText(f.name);slot.args+=asText(f.arguments);}
      if(choice.finish_reason)stop=asText(choice.finish_reason);}}
  const out=[...blocks.values()].map(b=>b.type==='tool_use'?`【调用工具 ${b.name}】\n${b.json}`:b.type==='thinking'?`【思考】\n${b.thinking}`:b.text);
  if(reasoning)out.push(`【思考】\n${reasoning}`);if(content)out.push(content);out.push(...[...calls.values()].map(c=>`【调用工具 ${c.name}】\n${c.args}`));
  return out.join('\n\n')+(stop?`\n\n— 结束原因：${stop}`:'')+(finished?'':'\n\n— 流未正常结束（未收到结束事件）');}
function callContentMarkup(d,path){
  const turn=(role,text,i)=>`<div class="chat-turn role-${esc(String(role).split(' ')[0])}"><div class="chat-role">${i===null?'':`#${i} · `}${esc(role)} · ${Array.from(text || '').length.toLocaleString('en-US')} 字</div><pre class="chat-text">${esc(text || '（空）')}</pre></div>`;
  let chat;
  try{const conv=conversationOf(d.request),reply=replyOf(d.response);
    const conversation=conv?`<p class="small-note">${conv.meta.map(esc).join(' · ')}${conv.tools.length?` · ${conv.tools.length} 个工具`:''} · ${conv.turns.length} 条上下文消息</p>${conv.tools.length?`<details class="chat-tools"><summary>可用工具（${conv.tools.length}）</summary><ul>${conv.tools.map(t=>`<li><strong>${esc(t.name)}</strong> ${esc(t.description.split('\n')[0].slice(0,200))}</li>`).join('')}</ul></details>`:''}${conv.turns.map(([role,text],i)=>turn(role,text,i+1)).join('')}`:'<p class="small-note">请求不是常见的 JSON 对象格式，请查看原始内容。</p>';
    chat=`<h4>请求上下文</h4>${conversation}<h4>模型回复</h4>${turn('assistant · 回复',reply,null)}`;}
  catch{chat='<div class="info-banner warning">无法按对话格式解析这条记录，原始内容与请求头仍完整保存，请切换到“原始内容”查看。</div>';}
  return `<div class="form-actions call-tabs"><button type="button" class="btn small" data-view="chat" aria-pressed="true">对话视图</button><button type="button" class="btn secondary small" data-view="raw" aria-pressed="false">原始内容</button><button type="button" class="btn secondary small" data-view="headers" aria-pressed="false">请求头</button><button type="button" class="btn secondary small" data-download>下载 JSON ↓</button><span class="small-note">请求 ${kb(Number(d.requestBytes) || 0)} · 回复 ${kb(Number(d.responseBytes) || 0)}${d.responseTruncated?'（回复超过 16MB，仅保存前 16MB）':''} · ${esc(path)}</span></div>
  <div data-pane="chat">${chat}</div>
  <div data-pane="raw" hidden><h4>请求（客户端原样发送）</h4><pre class="log-content">${esc(prettyJSON(d.request))}</pre><h4>回复（上游原样返回）</h4><pre class="log-content">${esc(asText(d.response))}</pre></div>
  <div data-pane="headers" hidden><pre class="log-content">${esc(JSON.stringify(d.headers ?? {},null,2))}</pre></div>`;}
function installCallContent(el,d){el.querySelectorAll('[data-view]').forEach(b=>b.onclick=()=>{el.querySelectorAll('[data-view]').forEach(x=>{const on=x===b;x.setAttribute('aria-pressed',on);x.classList.toggle('secondary',!on);});el.querySelectorAll('[data-pane]').forEach(p=>p.hidden=p.dataset.pane!==b.dataset.view);});
  el.querySelector('[data-download]').onclick=()=>{let request=d.request;try{request=JSON.parse(d.request);}catch{}const blob=new Blob([JSON.stringify({id:d.id,headers:d.headers,request,response:d.response},null,2)],{type:'application/json'});const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=`gotc-call-${d.id}.json`;a.click();setTimeout(()=>URL.revokeObjectURL(a.href),1000);};}
// ponytail: WorkBuddy's exact header is unconfirmed; check one real call's 请求头 in the history and adjust if needed.
const clientRuleTemplates={'仅允许 WorkBuddy（官方客户端）':'allow User-Agent: workbuddy','仅允许 Claude Code':'allow User-Agent: claude-cli','不限制客户端':''};
function parseClientRules(text){return text.split('\n').map(line=>line.trim()).filter(Boolean).map(line=>{const m=line.match(/^(allow|deny)\s+([A-Za-z0-9-]+)\s*:\s*(.+)$/i);if(!m)throw new Error(`客户端规则格式不正确：${line}`);return {action:m[1].toLowerCase(),header:m[2],contains:m[3].trim()};});}
const tokens=n=>Number(n || 0).toLocaleString('en-US');
function usageTable(title,head,rows,cells){return `<h3>${title}</h3><div class="table-wrap"><table class="board-table usage-table"><thead><tr>${head.map(h=>`<th>${h}</th>`).join('')}<th>请求</th><th>输入 / 输出 tokens</th><th>费用</th></tr></thead><tbody>${rows.map(r=>`<tr>${cells(r).join('')}<td>${Number(r.requests)}</td><td>${tokens(r.input)} / ${tokens(r.output)}</td><td>${usd(r.cost)}</td></tr>`).join('') || `<tr><td colspan="${head.length+3}">暂无数据</td></tr>`}</tbody></table></div>`;}
async function showKeyStats(filter,title,days){const box=document.getElementById('key-detail');const data=await api('admin/keys/stats',{...filter,days});if(!box.isConnected)return;const t=data.totals;
  box.innerHTML=`<section class="surface key-detail"><div class="section-caption"><h2>用量详情 · ${esc(title)}</h2><label class="field">时间范围<select id="stats-days">${[1,7,30,90].map(d=>`<option value="${d}" ${d===days?'selected':''}>近 ${d} 天</option>`).join('')}</select></label></div>${metrics([[Number(t.requests),'请求'],[usd(t.cost),'费用'],[tokens(t.input),`输入 tokens · 输出 ${tokens(t.output)}`],[`${Number(t.errors || 0)} · ${t.avgMs===null?'—':Number(t.avgMs)+'ms'}`,'失败请求 · 平均耗时']])}
  ${usageTable('按密钥',['队伍','密钥','最近调用'],data.byKey,r=>[`<td>${esc(String(r.teamNumber).padStart(2,'0'))} · ${esc(r.teamName)}</td>`,`<td><button type="button" class="link-button" data-logs-key="${esc(r.keyId)}" data-logs-title="${esc(r.teamName+' / '+r.name)}">${esc(r.name)}</button><br><small>${esc(r.masked)}${r.active?'':' · 已删除'}</small></td>`,`<td>${esc(beijingTime(r.lastAt))}</td>`])}
  ${usageTable('按模型',['模型'],data.byModel,r=>[`<td>${esc(r.model || '（被拦截，未到达模型）')}</td>`])}
  ${(data.byFailure || []).length?usageTable('失败原因（责任方 · 阶段 · 类型）',['责任方','阶段','类型','上游状态'],data.byFailure,r=>[`<td>${esc(ownerName[r.owner] || r.owner || '—')}</td>`,`<td>${esc(phaseName[r.phase] || r.phase || '—')}</td>`,`<td>${esc(r.type || '—')}</td>`,`<td>${r.upstreamStatus===null || r.upstreamStatus===undefined?'—':Number(r.upstreamStatus)}</td>`]):''}
  ${usageTable('按客户端（User-Agent）',['客户端'],data.byClient,r=>[`<td class="ua-cell">${esc(r.userAgent || '（空）')}</td>`])}
  ${usageTable('按日（北京时间）',['日期'],data.daily,r=>[`<td>${esc(r.day)}</td>`])}</section>`;
  document.getElementById('stats-days').onchange=e=>showKeyStats(filter,title,Number(e.target.value)).catch(err=>toast(err.message));
  box.querySelectorAll('[data-logs-key]').forEach(b=>b.onclick=()=>withBusy(b,()=>showKeyLogs({keyId:b.dataset.logsKey},b.dataset.logsTitle)));
  box.scrollIntoView({block:'start'});}
async function showKeyLogs(filter,title,errorsOnly=false){const box=document.getElementById('key-detail');let items=[],more=false;
  async function load(){const data=await api('admin/keys/logs',{...filter,errorsOnly,beforeId:items.length?items[items.length-1].id:null});items=items.concat(data.items);more=data.more;}
  function render(){box.innerHTML=`<section class="surface key-detail"><div class="section-caption"><h2>调用记录 · ${esc(title)}</h2><label class="toggle-line"><input type="checkbox" id="logs-errors" ${errorsOnly?'checked':''}>只看失败</label></div>${callTable(items,true)}${more?'<div class="form-actions"><button type="button" class="btn secondary small" id="logs-more">加载更早的记录</button></div>':''}</section>`;
    document.getElementById('logs-errors').onchange=e=>showKeyLogs(filter,title,e.target.checked).catch(err=>toast(err.message));
    document.getElementById('logs-more')?.addEventListener('click',e=>withBusy(e.currentTarget,async()=>{await load();render();}));
    box.querySelectorAll('[data-log-id]').forEach(b=>b.onclick=()=>withBusy(b,async()=>{const row=document.getElementById('log-'+b.dataset.logId);if(!row.hidden){row.hidden=true;return;}const item=items.find(i=>String(i.id)===b.dataset.logId);const d=await api('admin/keys/log',{id:Number(b.dataset.logId)});row.firstElementChild.innerHTML=callContentMarkup(d,item.path);installCallContent(row.firstElementChild,d);row.hidden=false;}));}
  await load();if(!box.isConnected)return;render();box.scrollIntoView({block:'start'});}
window.addEventListener('hashchange',()=>{if(restoreSubmissionRoute){restoreSubmissionRoute=false;return;}if(!confirmLeave()){restoreSubmissionRoute=true;location.hash=route;return;}submissionDirty=false;reviewDirty=false;scoreDirty=false;dirtyAdminForms.clear();renderRoute();});
window.addEventListener('click',async event=>{const button=event.target.closest?.('[data-copy-value]');if(!button)return;try{await navigator.clipboard.writeText(button.dataset.copyValue);toast('已复制。');}catch{toast('复制不可用，请展开完整记录或哈希并手动复制。');}});
window.addEventListener('storage',()=>currentTeamRecovery?.storageChanged());
window.addEventListener('pagehide',()=>currentTeamRecovery?.flush());
window.addEventListener('beforeunload',event=>{currentTeamRecovery?.flush();if(submissionDirty || reviewDirty || scoreDirty || dirtyAdminForms.size){event.preventDefault();event.returnValue='';}});
(async()=>{try{session=await api('session');}catch{session={role:null};}await renderRoute();})();
