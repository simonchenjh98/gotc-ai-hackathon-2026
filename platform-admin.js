'use strict';

// A small single-flight cursor check; only an applied read advances the cursor.
function makeAdminReviewSync(options){
  const {getContext,readMarker,readStage,apply,applyWindow,status,isHidden,subscribe,schedule,cancel}=options;
  let started=false,stopped=false,busy=false,pendingKick=false,timer=null,request=null,timeout=null,applied=null,failures=0;
  const clear=()=>{if(timer!==null)cancel(timer);timer=null;};
  const current=context=>!stopped && !isHidden() && getContext()?.key===context.key;
  const arm=delay=>{clear();if(started && !stopped && !isHidden())timer=schedule(check,delay);};
  async function fetchOne(fn){
    const controller=new AbortController();request=controller;
    timeout=schedule(()=>controller.abort(),8000);
    try{return await fn(controller.signal);}
    finally{cancel(timeout);timeout=null;if(request===controller)request=null;}
  }
  async function check(){
    timer=null;if(stopped || isHidden() || busy)return;
    const context=getContext();if(!context){arm(3000);return;}
    busy=true;let delay=3000;
    try{
      const marker=await fetchOne(readMarker);if(!current(context))return;
      const object=value=>value!==null && typeof value==='object' && !Array.isArray(value);
      if(!object(marker) || !Number.isSafeInteger(marker.reviewRevision) || marker.reviewRevision<0 || !object(marker.stages))throw new Error('Invalid review revision');
      for(const name of ['exhibition','roadshow']){
        const value=marker.stages[name];
        if(!object(value) || typeof value.runId!=='string' || !value.runId || !Number.isSafeInteger(value.revision) || value.revision<0 || typeof value.canEdit!=='boolean')throw new Error('Invalid stage revision');
      }
      const config=marker.stages[context.stage];
      if(!config || config.runId!==context.runId || config.revision!==context.revision){status('configuration');return;}
      applyWindow(config.canEdit,context);
      if(applied===null || applied!==marker.reviewRevision){
        const data=await fetchOne(signal=>readStage(context.stage,signal));if(!current(context))return;
        if(data.stage?.id!==context.stage || data.stage?.runId!==context.runId || data.stage?.revision!==context.revision){status('configuration');return;}
        if(apply(data,context)!==true)return;
        applied=marker.reviewRevision;status('updated');
      }else status('idle');
      failures=0;
    }catch(error){
      if(!current(context))return;
      if(error.status===401 || error.status===403){stop();status('auth');return;}
      failures++;delay=Math.min(30000,3000*2**Math.min(failures,4));status('error');
    }finally{
      busy=false;
      if(!stopped && !isHidden()){const next=pendingKick?0:delay;pendingKick=false;arm(next);}
    }
  }
  function kick(){if(!started || stopped)return;if(isHidden()){clear();return;}if(busy){pendingKick=true;return;}arm(0);}
  const unsubscribe=subscribe(()=>{if(isHidden())clear();else kick();});
  function stop(){stopped=true;clear();if(timeout!==null)cancel(timeout);timeout=null;request?.abort();request=null;unsubscribe?.();}
  return {start(){if(started || stopped)return;started=true;kick();},kick,stop};
}

let adminReviewSync=null;
function disposeAdminReviewSync(){adminReviewSync?.stop();adminReviewSync=null;}
function kickAdminReviewSync(){adminReviewSync?.kick();}
function startAdminReviewSync(view){
  disposeAdminReviewSync();
  const context=()=>{
    if(route!=='admin' || view.routeVersion!==routeVersion || !view.container.isConnected || adminReadOnlyView!==view || view.stage!==adminStage || adminLoadPending!==null)return null;
    return {key:[routeVersion,adminJudgingVersion,view.stage,view.runId,view.revision].join(':'),stage:view.stage,runId:view.runId,revision:view.revision,view};
  };
  const messages={idle:'所选阶段正式数与评审记录自动同步 · 每 3 秒检查',updated:'所选阶段评审记录已同步 · 每 3 秒检查',
    configuration:'轮次或设置已变化，请刷新数据核对。当前输入已保留。',
    error:'自动同步暂不可用，稍后重试。当前内容和输入已保留。',
    auth:'自动同步已停止：登录已失效或无权访问。请重新登录，当前输入已保留。'};
  const status=kind=>{
    if(route!=='admin' || adminReadOnlyView!==view || !view.container.isConnected)return;
    const node=document.getElementById('admin-sync-status');if(!node)return;
    const text=messages[kind];if(node.textContent!==text)node.textContent=text;
    node.classList.toggle('warning',kind==='configuration' || kind==='error' || kind==='auth');
  };
  adminReviewSync=makeAdminReviewSync({
    getContext:context,isHidden:()=>document.hidden,
    readMarker:signal=>api('admin/data/sync',undefined,{signal}),
    readStage:(stage,signal)=>api(`admin/judging/${stage}`,undefined,{signal}),
    apply:(data,current)=>patchAdminReviewData(current.view,data),
    applyWindow:(canEdit,current)=>{
      if(adminReadOnlyView!==current.view)return;
      const node=document.getElementById('admin-stage-window-status');
      if(node){node.textContent=canEdit?'评审记录开放中':'当前只读';node.classList.toggle('closed',!canEdit);}
    },status,
    subscribe:listener=>{document.addEventListener('visibilitychange',listener);return ()=>document.removeEventListener('visibilitychange',listener);},
    schedule:(fn,delay)=>setTimeout(fn,delay),cancel:id=>clearTimeout(id)
  });
  adminReviewSync.start();
}

// Admin-only tools share the existing API, session and edit tracking.
function adminQuery(path,values={}){
  const query=new URLSearchParams();
  for(const [key,value] of Object.entries(values))if(value!=='' && value!==null && value!==undefined)query.set(key,String(value));
  return path+(query.size?'?'+query.toString():'');
}
function adminOptions(items,valueKey,label,emptyLabel='全部'){
  return `<option value="">${esc(emptyLabel)}</option>`+items.map(item=>`<option value="${esc(item[valueKey])}">${esc(label(item))}</option>`).join('');
}
function adminJson(value){return `<pre class="admin-json">${esc(JSON.stringify(value,null,2))}</pre>`;}
function adminRecordStatus(record){
  if(record.invalidated)return '已作废';
  if(record.recused)return '已回避';
  if(record.submitted)return record.hasUnsubmittedChanges?'已提交 · 有草稿修改':'已正式提交';
  return record.draft?'草稿':'缺少有效正式记录';
}
function adminCodeOutput(container,credentials){
  const markup=credentials.map(item=>`<div class="code-output"><strong>${esc(item.name || item.id)}</strong><code>${esc(item.code)}</code><p>仅显示本次，请立即复制并单独发放。</p><button type="button" class="btn secondary small" data-copy-secret>复制登录码</button></div>`).join('');
  if(markup)container.insertAdjacentHTML('beforeend',markup);
  container.querySelectorAll('[data-copy-secret]').forEach(button=>button.onclick=async()=>{
    try{await navigator.clipboard.writeText(button.parentElement.querySelector('code').textContent);toast('登录码已复制。');}
    catch{toast('请选中登录码手动复制。');}
  });
}
async function adminSaveForm(form,path,payload,onSaved){
  const version=routeVersion,editRevision=form.editRevision || 0;
  await withBusy(form.querySelector('[type=submit]') || form.querySelector('button'),async()=>{
    formError(form,'');
    try{
      const result=await api(path,payload);
      if(version!==routeVersion || route!=='admin' || !form.isConnected)return;
      const unchanged=(form.editRevision || 0)===editRevision;
      if(unchanged)dirtyAdminForms.delete(form);
      await onSaved?.(result,unchanged);
      if(!unchanged)toast('已保存提交时的内容，之后的新输入已保留。');
    }catch(error){if(version===routeVersion && form.isConnected)formError(form,error.message+(error.status===409?' 当前输入已保留，请重新查询后核对。':''));}
  });
}
function installAdminData(data,version){
  const ctx={data,version,requests:{},reviewQuery:null,reviewData:null,auditQuery:null};
  const mount=document.getElementById('team-form');
  const alive=()=>route==='admin' && version===routeVersion && mount.isConnected;
  const next=key=>ctx.requests[key]=(ctx.requests[key] || 0)+1;
  const fresh=(key,id,element)=>alive() && ctx.requests[key]===id && element.isConnected;
  const track=form=>{if(!form.adminDataTracked){trackAdminForm(form);form.adminDataTracked=true;}};
  const formFrom=id=>document.getElementById(id);

  root.insertAdjacentHTML('afterbegin',`<nav class="admin-subnav" aria-label="后台区域">
    <button type="button" data-admin-jump="settings-form">专业评分与发布</button>
    <button type="button" data-admin-jump="audience-settings">观众投票</button>
    <button type="button" data-admin-jump="judging-admin">评审窗口与名单</button>
    <button type="button" data-admin-jump="team-form">队伍名录</button>
    <button type="button" data-admin-jump="admin-import">CSV 导入</button>
    <button type="button" data-admin-jump="invite-form">身份与凭据</button>
    <button type="button" data-admin-jump="submission-settings">作品提交</button>
    <button type="button" data-admin-jump="admin-records">历史与记录</button>
    <button type="button" data-admin-jump="admin-audit">操作记录</button>
  </nav>`);
  root.insertAdjacentHTML('beforeend',adminImportMarkup()+adminRecordsMarkup()+adminAuditMarkup()+`<section class="surface admin-unavailable"><h2>新阶段待配置功能</h2><p class="small-note">展示和路演阶段的数值评分、汇总算法、晋级与综合排名、奖项及 GOTC 资格、正式成绩发布尚未实现。这两个阶段当前只保存工作名单、材料和定性评审记录。现有六维专业评分与成绩发布可在上方使用。</p><button class="btn secondary small" disabled>配置阶段计分与汇总</button> <button class="btn secondary small" disabled>发布阶段结果</button></section>`);
  document.querySelectorAll('[data-admin-jump]').forEach(button=>button.onclick=()=>document.getElementById(button.dataset.adminJump)?.scrollIntoView({behavior:'smooth',block:'start'}));

  async function refreshDirectory(){
    const id=next('directory'),table=document.querySelector('.admin-team-table'),submissionStamp=typeof submissionOverviewStamp==='function'?submissionOverviewStamp():null;
    try{
      const result=await api('admin');if(!fresh('directory',id,table))return;
      ctx.data=result;adminEventData=result;if(typeof updateSubmissionOverview==='function')updateSubmissionOverview(result,submissionStamp);
      const teamTotal=document.getElementById('admin-total-teams');
      const activeAudience=document.getElementById('admin-active-audience');
      if(teamTotal)teamTotal.textContent=String(result.teams.length);
      if(activeAudience)activeAudience.textContent=String(result.audience.filter(person=>person.active).length);
      renderDirectory();renderCredentials();
      return result;
    }catch(error){if(alive())toast(error.message);}
  }
  function renderDirectory(){
    const table=document.querySelector('.admin-team-table');
    const status={active:'参赛中',withdrawn:'已退出',disqualified:'取消资格'};
    table.innerHTML=`<table class="board-table"><thead><tr><th>编号</th><th>队伍 / 作品</th><th>赛道</th><th>状态</th><th>展位 / 路演顺序</th><th>操作</th></tr></thead><tbody>${ctx.data.teams.map(team=>`<tr><td>${esc(team.number)}</td><td><strong>${esc(team.name)}</strong><br><small>${esc(team.project || '待补充')}</small></td><td>${esc(team.track || '未填写')}${team.trackKnown===false?'<br><span class="status-chip closed">历史赛道，尚未归类</span>':''}</td><td>${esc(status[team.status] || team.status || '参赛中')}<br><small>${esc(team.statusReason || '')}</small></td><td>${esc(team.booth || '未安排')} / ${esc(team.roadshowOrder ?? '未安排')}</td><td><button class="btn secondary small" type="button" data-edit-team="${esc(team.id)}">编辑</button></td></tr>`).join('') || '<tr><td colspan="6">尚未录入队伍。</td></tr>'}</tbody></table>`;
    table.querySelectorAll('[data-edit-team]').forEach(button=>button.onclick=()=>{
      const form=formFrom('team-form');
      if(dirtyAdminForms.has(form) && !confirm('表单有未保存的修改，确认载入另一支队伍？'))return;
      form.editSession=(form.editSession || 0)+1;
      const team=ctx.data.teams.find(item=>item.id===button.dataset.editTeam);
      const track=form.elements.track;
      track.innerHTML=adminOptions((ctx.data.trackOptions || []).map(name=>({name})),'name',item=>item.name,'请选择赛道');
      if(team.track && ![...track.options].some(option=>option.value===team.track))track.insertAdjacentHTML('beforeend',`<option value="${esc(team.track)}">${esc(team.track)}（历史赛道）</option>`);
      for(const key of ['id','number','name','project','track','status','statusReason','booth','roadshowOrder'])form.elements[key].value=team[key] ?? (key==='status'?'active':'');
      form.elements.expectedRevision.value=String(team.revision ?? 0);
      form.editRevision=(form.editRevision || 0)+1;dirtyAdminForms.delete(form);
      form.querySelector('[type=submit]').textContent='保存队伍修改';form.scrollIntoView({behavior:'smooth',block:'center'});form.elements.name.focus();
    });
  }
  const teamForm=formFrom('team-form');
  teamForm.querySelector('.form-grid').insertAdjacentHTML('beforeend',`<input type="hidden" name="expectedRevision"><label class="field">参赛状态<select name="status"><option value="active">参赛中</option><option value="withdrawn">退出</option><option value="disqualified">取消资格</option></select></label><label class="field">状态变更原因<input name="statusReason" maxlength="500" placeholder="退出或取消资格时必填"></label><label class="field">展示展位<input name="booth" maxlength="80" placeholder="例如：A-01"></label><label class="field">路演顺序<input name="roadshowOrder" type="number" min="1" max="9999" placeholder="留空表示未安排"></label>`);
  teamForm.insertAdjacentHTML('afterbegin','<p class="small-note">名录修改不会改写已冻结的评审轮次。状态和路演顺序不代表晋级结果。</p>');
  teamForm.onsubmit=event=>{
    event.preventDefault();const values=teamForm.elements,session=teamForm.editSession || 0,editingId=values.id.value;
    if(values.status.value!=='active' && !values.statusReason.value.trim()){formError(teamForm,'请填写退出或取消资格的原因。');return;}
    const payload={number:Number(values.number.value),name:values.name.value.trim(),project:values.project.value.trim(),track:values.track.value,status:values.status.value,statusReason:values.statusReason.value.trim(),booth:values.booth.value.trim(),roadshowOrder:values.roadshowOrder.value?Number(values.roadshowOrder.value):null};
    if(values.id.value){payload.id=values.id.value;payload.expectedRevision=Number(values.expectedRevision.value);}
    adminSaveForm(teamForm,'admin/teams',payload,async result=>{
      const saved=result?.team;
      // Only adopt the version from this POST. A later directory read may include
      // another administrator's newer version, which must not be skipped locally.
      if(saved && (teamForm.editSession || 0)===session && values.id.value===editingId){
        values.id.value=saved.id;values.expectedRevision.value=String(saved.revision);
        teamForm.querySelector('[type=submit]').textContent='保存队伍修改';
      }
      toast('队伍信息已保存。');await refreshDirectory();
    });
  };
  document.getElementById('clear-team').onclick=()=>{teamForm.reset();teamForm.elements.id.value='';teamForm.elements.expectedRevision.value='';teamForm.editRevision=(teamForm.editRevision || 0)+1;teamForm.editSession=(teamForm.editSession || 0)+1;dirtyAdminForms.delete(teamForm);teamForm.querySelector('[type=submit]').textContent='新增队伍';};
  renderDirectory();

  function renderCredentials(){
    const list=document.querySelector('.invite-list');
    list.innerHTML=[...ctx.data.judges.map(person=>({...person,role:'judge'})),...ctx.data.audience.map(person=>({...person,role:'audience'}))].map(person=>`<div class="invite-row"><span><strong>${esc(person.name)}</strong><small>${roleName[person.role]} · ${person.active?'有效':'已停用'} · ${person.credentialMode==='shared'?'使用统一码':'独立登录码'}<br>ID: ${esc(person.id)}</small></span><div class="form-actions"><button type="button" class="btn secondary small" data-credential="rotate" data-id="${esc(person.id)}" data-role="${person.role}">重发登录码</button>${person.active?`<button type="button" class="btn danger small" data-credential="revoke" data-id="${esc(person.id)}" data-role="${person.role}">停用</button>`:''}</div></div>`).join('') || '<p class="small-note">尚未创建身份。</p>';
    list.querySelectorAll('[data-credential]').forEach(button=>button.onclick=()=>withBusy(button,async()=>{
      const action=button.dataset.credential,name=button.closest('.invite-row').querySelector('strong').textContent;
      if(!confirm(action==='rotate'?'重发后旧登录码和旧会话立即失效。该身份将改用独立登录码，继续？':'停用后该身份的登录会话立即失效，继续？'))return;
      const result=await api('admin/credentials',{role:button.dataset.role,id:button.dataset.id,action});
      if(!alive())return;
      if(result.code)adminCodeOutput(formFrom('new-code'),[{name,code:result.code}]);
      await refreshDirectory();toast(action==='rotate'?'已重发登录码，请单独发放。':'该身份已停用。');
    }));
  }
  renderCredentials();
  const invite=formFrom('invite-form');
  invite.onsubmit=event=>{event.preventDefault();const payload={role:invite.elements.role.value,name:invite.elements.name.value.trim()};adminSaveForm(invite,'admin/invitations',payload,async(result,unchanged)=>{adminCodeOutput(formFrom('new-code'),[{name:result.name || payload.name,code:result.code}]);if(unchanged)invite.elements.name.value='';await refreshDirectory();toast('身份已创建。专业评分可直接使用；参与展示或路演评审记录时，需加入对应阶段工作名单。');});};

  const voting=formFrom('audience-settings');
  voting.innerHTML=`<h3>观众投票窗口</h3><label class="toggle-line"><input type="checkbox" name="votingOpen" ${data.config.votingOpen?'checked':''}>开放观众投票</label><label class="field">截止时间（北京时间）<input name="votingDeadline" type="datetime-local" value="${esc(beijingInput(data.config.votingDeadline))}"><small>留空表示不设截止。此窗口独立于专业评分、作品提交、展示和路演评审。</small></label><button class="btn small" type="submit">保存投票窗口</button><p class="form-error" role="alert"></p>`;
  voting.onsubmit=event=>{event.preventDefault();try{adminSaveForm(voting,'admin/settings',{votingOpen:voting.elements.votingOpen.checked,votingDeadline:parseBeijingInput(voting.elements.votingDeadline.value)},()=>toast('观众投票窗口已保存。'));}catch(error){formError(voting,error.message);}};
  const submissions=formFrom('submission-settings');
  submissions.onsubmit=event=>{event.preventDefault();try{adminSaveForm(submissions,'admin/settings',{submissionsOpen:submissions.elements.submissionsOpen.checked,submissionDeadline:parseBeijingInput(submissions.elements.submissionDeadline.value)},()=>toast('作品提交窗口已保存。'));}catch(error){formError(submissions,error.message);}};

  installImportTools(ctx,{alive,next,fresh,refreshDirectory,track});
  installRecordTools(ctx,{alive,next,fresh,track});
  installAuditTools(ctx,{alive,next,fresh,track});
}

function adminImportMarkup(){return `<section class="surface" id="admin-import"><h2>批量导入</h2><p class="small-note">先预览，再确认。队伍按编号匹配；更新评委须填写 ID，留空会新建。预览不会写入名录或生成登录码。</p><form id="import-form"><div class="form-grid"><label class="field">导入类型<select name="kind"><option value="teams">队伍</option><option value="judges">评委</option></select></label><label class="field">上传 CSV（UTF-8）<input type="file" name="file" accept=".csv,text/csv"><small>最多 500 行、256 KiB，也可直接粘贴。</small></label></div><a class="btn secondary small" id="import-template" href="api/admin/data/template?kind=teams" download>下载队伍模板</a><label class="field">CSV 内容<textarea name="csv" rows="8" placeholder="粘贴包含表头的 CSV"></textarea></label><button class="btn small" type="submit">预览导入</button><p class="form-error" role="alert"></p></form><div id="import-preview" aria-live="polite"></div><div id="import-credentials" aria-live="polite"></div></section>`;}
function installImportTools(ctx,{alive,next,fresh,refreshDirectory,track}){
  const form=document.getElementById('import-form'),preview=document.getElementById('import-preview');track(form);
  let prepared=null;
  const invalidate=()=>{prepared=null;const button=document.getElementById('commit-import');if(button)button.disabled=true;};
  form.addEventListener('input',invalidate);
  form.elements.kind.onchange=()=>{
    invalidate();const link=document.getElementById('import-template'),judges=form.elements.kind.value==='judges';
    // Choose fixed destinations rather than interpolating DOM input into a URL.
    link.href=judges?'api/admin/data/template?kind=judges':'api/admin/data/template?kind=teams';
    link.textContent=judges?'下载评委模板':'下载队伍模板';
  };
  form.elements.file.onchange=async()=>{
    invalidate();const file=form.elements.file.files[0];if(!file)return;
    if(file.size>262144){formError(form,'CSV 文件不能超过 256 KiB。');return;}
    const revision=form.editRevision || 0;
    try{const content=new TextDecoder('utf-8',{fatal:true}).decode(await file.arrayBuffer());if(!alive() || !form.isConnected || form.elements.file.files[0]!==file || (form.editRevision || 0)!==revision)return;form.elements.csv.value=content;form.dispatchEvent(new Event('input',{bubbles:true}));}catch{formError(form,'无法读取 UTF-8 CSV，请确认文件编码，或改为粘贴内容。');}
  };
  form.onsubmit=event=>{
    event.preventDefault();invalidate();const csv=form.elements.csv.value;
    if(new TextEncoder().encode(csv).length>262144){formError(form,'CSV 内容不能超过 256 KiB。');return;}
    const revision=form.editRevision || 0,id=next('import');
    adminSaveForm(form,'admin/data/import/preview',{kind:form.elements.kind.value,csv},(result,unchanged)=>{
      if(!unchanged || !fresh('import',id,preview))return;
      // A preview is still pending work until the batch is committed or discarded.
      dirtyAdminForms.add(form);prepared={id:result.previewId,revision};
      preview.innerHTML=`<div class="info-banner">新增 ${Number(result.counts.create)} · 更新 ${Number(result.counts.update)} · 无变更 ${Number(result.counts.unchanged)} · 错误 ${Number(result.counts.error)}<br>预览有效至 ${esc(beijingTime(result.expiresAt))}（北京时间）</div><div class="table-wrap"><table class="board-table"><thead><tr><th>行</th><th>操作</th><th>标识</th><th>变更 / 错误</th></tr></thead><tbody>${result.rows.map(row=>`<tr><td>${Number(row.row)}</td><td>${esc({create:'新增',update:'更新',unchanged:'无变更',error:'错误'}[row.action] || row.action)}</td><td>${esc(row.key)}</td><td>${(row.errors || []).length?`<p class="form-error">${row.errors.map(esc).join('<br>')}</p>`:`<details><summary>查看前后内容</summary><h4>原内容</h4>${adminJson(row.before)}<h4>导入后</h4>${adminJson(row.after)}</details>`}</td></tr>`).join('')}</tbody></table></div><div class="form-actions"><button type="button" class="btn" id="commit-import" ${result.canCommit?'':'disabled'}>确认导入本批次</button><button type="button" class="btn secondary" id="discard-import">放弃预览</button></div><p class="form-error" id="import-commit-error" role="alert"></p>`;
      document.getElementById('discard-import').onclick=()=>{invalidate();preview.innerHTML='';dirtyAdminForms.delete(form);};
      document.getElementById('commit-import').onclick=event=>{const button=event.currentTarget;return withBusy(button,async()=>{
        if(!prepared || prepared.revision!==(form.editRevision || 0))return;
        const committing=prepared;document.getElementById('import-commit-error').textContent='';
        try{
          const result=await api('admin/data/import/commit',{previewId:committing.id});if(!alive() || !form.isConnected)return;
          if(prepared===committing){prepared=null;button.disabled=true;}
          if((form.editRevision || 0)===committing.revision)dirtyAdminForms.delete(form);
          if(!prepared)preview.innerHTML=`<div class="info-banner">导入完成：新增 ${Number(result.created)}，更新 ${Number(result.updated)}，无变更 ${Number(result.unchanged)}。</div>`;
          adminCodeOutput(document.getElementById('import-credentials'),result.issuedCredentials || []);await refreshDirectory();toast('导入已完成，名录已更新。');
        }catch(error){if(alive() && document.getElementById('import-commit-error'))document.getElementById('import-commit-error').textContent=error.message+' 请重新预览后核对。';}
      });};
    });
  };
}

function adminRecordsMarkup(){return `<section class="surface" id="admin-records"><h2>历史轮次与评审记录</h2><p class="small-note">查看冻结材料、草稿、正式记录和缺评。缺评指没有有效正式记录的队伍与评委组合，不表示计分门槛。</p><form id="record-filter"><div class="filter-grid"><label class="field">阶段<select name="stage"><option value="exhibition">展示评审</option><option value="roadshow">路演评审</option></select></label><label class="field">轮次<select name="runId"><option value="">当前轮次</option></select></label><label class="field">队伍<select name="teamId"><option value="">全部队伍</option></select></label><label class="field">评委<select name="judgeId"><option value="">全部评委</option></select></label><label class="field">赛道<select name="track"><option value="">全部赛道</option></select></label><label class="field">记录状态<select name="status"><option value="all">全部</option><option value="draft">有草稿</option><option value="submitted">有效正式记录</option><option value="recused">已回避</option><option value="invalidated">已作废</option><option value="missing">缺少有效正式记录</option></select></label></div><button class="btn small" type="submit">查询记录</button><p class="form-error" role="alert"></p></form><div class="admin-exports" id="record-exports"></div><div id="record-results" aria-live="polite"><p class="small-note">正在读取轮次…</p></div></section>`;}
function installRecordTools(ctx,{alive,next,fresh,track}){
  const filter=document.getElementById('record-filter'),results=document.getElementById('record-results');
  const elements=filter.elements;
  const query=()=>Object.fromEntries(['stage','runId','teamId','judgeId','track','status'].map(key=>[key,elements[key].value]));
  const hasRecordEdits=()=>[...dirtyAdminForms].some(form=>form.isConnected && results.contains(form));
  const discardRecordEdits=()=>{for(const form of dirtyAdminForms)if(!form.isConnected || results.contains(form))dirtyAdminForms.delete(form);};
  filter.addEventListener('input',()=>{filter.editRevision=(filter.editRevision || 0)+1;});
  function selectedOptions(select,items,key,label,emptyLabel){const selected=select.value;select.innerHTML=adminOptions(items,key,label,emptyLabel);if([...select.options].some(option=>option.value===selected))select.value=selected;}
  async function loadRuns(){
    const id=next('runs'),stage=elements.stage.value;
    try{
      const result=await api(adminQuery('admin/data/runs',{stage}));
      if(!fresh('runs',id,filter) || elements.stage.value!==stage)return false;
      ctx.runs=result.runs;
      elements.runId.innerHTML=result.runs.map(run=>`<option value="${esc(run.id)}">${run.current?'当前轮次':'历史轮次'} · ${esc(beijingTime(run.createdAt))}${run.reason?' · '+esc(run.reason):''}</option>`).join('') || '<option value="">暂无轮次</option>';
      const current=result.runs.find(run=>run.current);if(current)elements.runId.value=current.id;
      elements.teamId.innerHTML='<option value="">全部队伍</option>';elements.judgeId.innerHTML='<option value="">全部评委</option>';elements.track.innerHTML='<option value="">全部赛道</option>';
      return true;
    }catch(error){if(alive())formError(filter,error.message);return false;}
  }
  async function readRecords(){
    const values=query(),id=next('records'),revision=filter.editRevision || 0;
    formError(filter,'');
    try{
      const result=await api(adminQuery('admin/data/reviews',values));
      if(!fresh('records',id,results) || (filter.editRevision || 0)!==revision || hasRecordEdits())return;
      ctx.reviewData=result;ctx.reviewQuery={...values,runId:result.run.id};
      if(ctx.choiceRunId!==result.run.id){ctx.choiceRunId=result.run.id;ctx.choiceTeams=result.teams;ctx.choiceJudges=result.judges;}
      selectedOptions(elements.teamId,ctx.choiceTeams,'id',team=>teamLabel(team),'全部队伍');
      selectedOptions(elements.judgeId,ctx.choiceJudges,'id',judge=>judge.name,'全部评委');
      selectedOptions(elements.track,[...new Set(ctx.choiceTeams.map(team=>team.track).filter(Boolean))].map(name=>({name})),'name',track=>track.name,'全部赛道');
      const coverage=result.coverage || {};
      const run=(ctx.runs || []).find(item=>item.id===result.run.id) || result.run;
      results.innerHTML=`<div class="stage-strip"><strong>${esc(stageName(values.stage))} · ${run.current?'当前轮次':'历史轮次，只读'}</strong><span>创建于 ${esc(beijingTime(run.createdAt))}</span><span>冻结于 ${esc(beijingTime(run.frozenAt))}</span></div><p class="small-note">应有组合 ${Number(coverage.expected || 0)} · 已有记录 ${Number(coverage.recorded || 0)} · 有效正式记录 ${Number(coverage.validSubmitted || 0)} · 缺少有效正式记录 ${Number(coverage.missing || 0)}</p><div class="table-wrap"><table class="board-table review-table"><thead><tr><th>队伍</th><th>评委</th><th>状态 / 版本</th><th>内容与操作</th></tr></thead><tbody>${result.reviews.map((record,index)=>{
        const team=result.teams.find(item=>item.id===record.teamId),judge=result.judges.find(item=>item.id===record.judgeId);
        return `<tr><td>${esc(team?teamLabel(team):record.teamId)}<br><small>${esc(team?.track || '')}</small></td><td>${esc(judge?.name || record.judgeId)}</td><td>${esc(adminRecordStatus(record))}<br><small>版本 ${Number(record.version)} · 未计分</small>${record.invalidatedReason?`<p class="small-note">原因：${esc(record.invalidatedReason)}</p>`:''}</td><td><details><summary>查看草稿与正式内容</summary>${record.submitted?`<h4>正式记录</h4><p class="preserve-text">${esc(record.submitted.feedback)}</p><small>${esc(beijingTime(record.submitted.submittedAt))}</small>`:'<p class="small-note">暂无正式记录。</p>'}${record.draft?`<h4>草稿</h4><p class="preserve-text">${esc(record.draft.feedback || '（空草稿）')}</p>`:''}</details>${record.version?`<button class="btn secondary small" type="button" data-record-history="${index}">查看修订历史</button><div data-history-output="${index}" class="record-history"></div>${run.current?`<details><summary>${record.invalidated?'恢复这份记录':'作废这份记录'}</summary><form data-validity="${index}"><label class="field">${record.invalidated?'恢复':'作废'}原因<input name="reason" required maxlength="500"></label><button class="btn ${record.invalidated?'secondary':'danger'} small" type="submit">${record.invalidated?'恢复记录':'作废记录'}</button><p class="form-error" role="alert"></p></form></details>`:''}`:''}</td></tr>`;
      }).join('') || '<tr><td colspan="4">没有符合筛选条件的记录。</td></tr>'}</tbody></table></div><details class="stage-material"><summary>查看本轮冻结队伍与材料</summary>${result.teams.map(team=>`<details class="stage-material"><summary>${esc(teamLabel(team))} · ${esc(team.track || '未填写赛道')}</summary>${reviewMaterialMarkup(team.materialSnapshot)}</details>`).join('') || '<p class="small-note">本轮没有队伍。</p>'}</details>`;
      document.getElementById('record-exports').innerHTML=[['teams','队伍名录'],['judges','评委名录'],['reviews','当前筛选记录'],['rosters','人工工作名单']].map(([kind,label])=>`<a class="btn secondary small" download href="api/${esc(adminQuery('admin/data/export',{kind,...ctx.reviewQuery}))}">导出${label} CSV</a>`).join('')+'<a class="btn secondary small" download href="api/admin/submissions/export">导出作品材料 CSV</a><a class="btn secondary small" download href="api/admin/export">导出专业评分 CSV</a>';
      results.querySelectorAll('[data-record-history]').forEach(button=>button.onclick=()=>withBusy(button,async()=>{
        const record=result.reviews[Number(button.dataset.recordHistory)],output=results.querySelector(`[data-history-output="${button.dataset.recordHistory}"]`),request=next('history-'+button.dataset.recordHistory);
        try{
          const history=await api(adminQuery('admin/data/review-history',{runId:result.run.id,teamId:record.teamId,judgeId:record.judgeId}));
          if(!fresh('history-'+button.dataset.recordHistory,request,output))return;
          output.innerHTML=`<p class="small-note">历史记录从 ${esc(beijingTime(history.historyAvailableFrom))} 起可查；此前未知的修订未补写。</p>${history.history.map(item=>`<details><summary>版本 ${Number(item.version)} · ${esc(beijingTime(item.at))} · ${esc(item.action)}</summary><p class="small-note">操作者 ${esc(item.actorId)}${item.reason?' · 原因 '+esc(item.reason):''}</p><h4>修改前</h4>${adminJson(item.before)}<h4>修改后</h4>${adminJson(item.after)}</details>`).join('') || '<p class="small-note">暂无可查修订。</p>'}`;
        }catch(error){if(output.isConnected)output.textContent=error.message;}
      }));
      results.querySelectorAll('[data-validity]').forEach(form=>{
        track(form);const record=result.reviews[Number(form.dataset.validity)];
        form.onsubmit=event=>{event.preventDefault();adminSaveForm(form,'admin/data/review-validity',{runId:result.run.id,teamId:record.teamId,judgeId:record.judgeId,expectedVersion:record.version,invalidated:!record.invalidated,reason:form.elements.reason.value.trim()},async(_saved,unchanged)=>{toast(record.invalidated?'记录已恢复。':'记录已作废，原评语保留。');if(unchanged && !hasRecordEdits()){await readRecords();if(!stageHasUnsavedInputs())await loadAdminJudging(ctx.version,results);}});};
      });
    }catch(error){if(fresh('records',id,results))formError(filter,error.message);}
  }
  filter.onsubmit=event=>{event.preventDefault();if(hasRecordEdits() && !confirm('有未保存的作废或恢复原因，确认重新查询并放弃输入？'))return;discardRecordEdits();readRecords();};
  elements.stage.onchange=async()=>{if(hasRecordEdits() && !confirm('有未保存的原因，确认切换阶段并放弃输入？')){elements.stage.value=ctx.reviewQuery?.stage || 'exhibition';return;}discardRecordEdits();if(await loadRuns())readRecords();};
  elements.runId.onchange=()=>{elements.teamId.value='';elements.judgeId.value='';elements.track.value='';};
  loadRuns().then(loaded=>{if(loaded && alive())readRecords();});
}
function adminAuditMarkup(){return `<section class="surface" id="admin-audit"><h2>操作记录</h2><p class="small-note">每次读取 50 条，可继续查看更早记录。</p><form id="audit-filter"><div class="filter-grid"><label class="field">操作类型<input name="action" placeholder="留空查看全部"></label><label class="field">操作者 ID<input name="actorId" placeholder="留空查看全部"></label><label class="field">目标 ID<input name="target" placeholder="队伍或身份 ID"></label></div><button class="btn small" type="submit">查询操作记录</button><p class="form-error" role="alert"></p></form><a class="btn secondary small" id="audit-export" href="api/admin/data/export?kind=audit" download>导出查询结果 CSV</a><div id="audit-results" aria-live="polite"></div><button class="btn secondary small" id="audit-more" type="button" hidden>读取更早记录</button></section>`;}
function installAuditTools(ctx,{alive,next,fresh}){
  const form=document.getElementById('audit-filter'),output=document.getElementById('audit-results'),more=document.getElementById('audit-more');let cursor=null;
  form.addEventListener('input',()=>{form.editRevision=(form.editRevision || 0)+1;});
  async function read(append=false){
    const id=next('audit'),revision=form.editRevision || 0;
    if(append && ctx.auditRevision!==revision){formError(form,'筛选条件已修改，请先查询再读取更早记录。');return;}
    const values=Object.fromEntries(['action','actorId','target'].map(key=>[key,form.elements[key].value.trim()]));
    formError(form,'');
    try{
      const result=await api(adminQuery('admin/data/audit',{...values,beforeId:append?cursor:null,limit:50}));
      if(!fresh('audit',id,output) || (form.editRevision || 0)!==revision)return;
      const rows=result.entries.map(entry=>`<article class="audit-entry"><div><strong>${esc(entry.action)}</strong><time>${esc(beijingTime(entry.at))}</time></div><p class="small-note">${esc(entry.actorId)} · ${esc(entry.role)} → ${esc(entry.target || '—')} · #${Number(entry.id)}</p><details><summary>查看变更</summary>${adminJson(entry.metadata)}</details></article>`).join('') || '<p class="small-note">没有符合条件的操作记录。</p>';
      if(append)output.insertAdjacentHTML('beforeend',rows);else output.innerHTML=rows;
      cursor=result.nextBeforeId;more.hidden=!cursor;ctx.auditQuery=values;ctx.auditRevision=revision;
      document.getElementById('audit-export').href='api/'+adminQuery('admin/data/export',{kind:'audit',...values});
    }catch(error){if(fresh('audit',id,output))formError(form,error.message);}
  }
  form.onsubmit=event=>{event.preventDefault();cursor=null;read();};
  more.onclick=()=>withBusy(more,()=>read(true));read();
}
