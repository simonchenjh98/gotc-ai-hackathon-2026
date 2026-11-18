'use strict';

const settingsGroups={
  scoring:[['scoringOpen','开放专业评分'],['resultsPublished','公开专业均分'],['minJudges','公开均分所需评委数']],
  submissions:[['submissionsOpen','开放作品提交'],['submissionDeadline','提交截止时间（北京时间）']],
  voting:[['votingOpen','开放观众投票'],['votingDeadline','投票截止时间（北京时间）']]
};
function settingsValue(key,value){return key.endsWith('Open') || key==='resultsPublished'?(value?'开放':'关闭'):key.endsWith('Deadline')?beijingTime(value):String(value);}

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
