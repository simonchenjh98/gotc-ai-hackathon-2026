'use strict';

const submissionHelp={
  demoUrl:'支持完整的 HTTP 或 HTTPS 展示链接，最多8192字节；此项选填。',
  codeUrl:'在 GitHub、Gitee、GitCode 或 GitLab 打开公开仓库主页，复制浏览器地址，例如 https://gitee.com/your-team/your-project。也可填写其他平台的 HTTPS 仓库主页。不是个人主页、文件页面、本地路径或上传 ZIP；只有本地代码时，请先 push 到线上仓库。',
  codeVersion:'在仓库的提交历史（Commits）选中最终一次提交，复制该 commit 页面链接，或复制40/64位完整哈希。不要填分支、tag或只填前6位；GitHub/Gitee/GitCode/GitLab 链接必须属于上面的仓库；其他平台请填完整哈希。平台只检查格式，不能证明此提交存在或属于该仓库。'
};
function repositoryJoinMarkup(provider){const url=provider?.joinUrl && safeMaterialUrl(provider.joinUrl);return url?`<a class="btn secondary small" href="${esc(url)}" target="_blank" rel="noopener noreferrer">加入赛事 ${esc(provider.provider==='gitee'?'Gitee':provider.provider)} 组织 ↗</a>`:'';}
function repositoryNextStepsMarkup(repository,provider,data){
  const pending=repository.state!=='ready',name=repository.provider==='gitee'?'Gitee':'GitHub';
  if(repository.state==='requested' || !repository.codeUrl && repository.state!=='provisioning')return `<p class="small-note">${repository.state!=='requested'?'仓库尚未创建成功，可点击下方“刷新并关联仓库”重试，或联系主办方。':provider?.autoApprove?'仓库尚未创建，点击下方“刷新并关联仓库”重试。':'等待主办方核对用户名并批准建仓；批准后点击“刷新并关联仓库”。'}在此之前也可以在下方表单手动填写自己的公开仓库与 commit。</p>`;
  if(repository.state==='provisioning')return '<p class="small-note">正在创建或核对仓库，请稍后点击“刷新并关联仓库”，勿重复申请。</p>';
  return `<ol class="repository-steps"><li>${pending?`<strong>加入组织：</strong>登录 ${name}，接受赛事组织/仓库邀请${provider?.joinUrl?'（或用下方“加入赛事组织”按钮）':''}，再点“我已加入，刷新授权”。`:`<strong>权限已就绪：</strong>队员可以向本队仓库 push。`}</li><li><strong>上传代码：</strong>把完整项目 push 到正式分支 <code>${esc(repository.branch)}</code>，写好 README。</li><li><strong>提交作品：</strong>填好下方材料后点“填写材料并检查提交”，可选勾选“同时提交仓库代码版本”后读取完整 commit；仅提交 PPT 和 ZIP 时无需勾选。</li></ol>${repository.canSubmit?`<div class="form-actions"><button type="button" class="btn" id="repository-submit" ${data?.window?.canEdit===false?'disabled':''}>填写材料并检查提交 ↓</button></div>${data?.window?.canEdit===false?'<p class="small-note">作品提交尚未开放或已截止，开放后即可提交。</p>':''}`:''}`;
}
function teamRepositoryMarkup(data){
  const repository=data.repository,providers=data.repositoryProviders || [],enabled=providers.some(p=>p.enabled),provider=providers.find(p=>p.provider===repository?.provider) || providers.find(p=>p.enabled),auto=providers.some(p=>p.enabled && p.autoApprove);
  const intro=auto?'填写本队队员的平台用户名，系统会自动在赛事组织下创建本队仓库并给队员写权限，之后提交时自动关联本仓库和 commit。':'申请主办方组织下的本队独立仓库。管理员核对用户名归属后授予本仓库写权限，之后提交时自动关联本仓库和 commit。';
  return `<section class="surface repository-panel" id="team-repository"><h2>本队代码仓库</h2>${repository?repositoryStatusMarkup(repository)+repositoryNextStepsMarkup(repository,provider,data):`<p>${intro}</p>${providers.filter(p=>!p.enabled).map(p=>`<p class="small-note">${esc(p.provider)} · ${esc(p.organization)} · ${esc(p.message)}</p>`).join('')}${!enabled?'<p>组织仓库尚未启用，请联系组织方；已有队伍仍可使用当前材料表单。</p>':`<form id="repository-application"><label class="field">仓库平台<select name="provider">${providers.map(p=>`<option value="${esc(p.provider)}" ${p.enabled?'':'disabled'}>${esc(p.provider)}${p.enabled?'':'（尚未启用）'}</option>`).join('')}</select></label><label class="field">开发者用户名<textarea name="usernames" required rows="3" maxlength="220" placeholder="每行一个用户名，最多 5 人；不要填网址或邮箱"></textarea><small>用户名是个人主页地址最后一段，例如 gitee.com/<strong>zhangsan</strong> 中的 zhangsan，不是昵称。</small></label><button class="btn secondary" type="submit">${auto?'创建并关联本队仓库':'申请本队仓库'}</button><p class="form-error" role="alert"></p></form>`}`}${repositoryJoinMarkup(provider)}${repository?`<button type="button" class="btn secondary small" id="refresh-team-repository">${repository.state==='ready'?'刷新仓库状态':repository.codeUrl?'我已加入，刷新授权':'刷新并关联仓库'}</button>`:''}<p id="repository-message" role="status"></p></section>`;
}
function syncRepositoryMaterialFields(data,previous){
  const form=document.getElementById('submission-form');if(!form || !data.repository?.codeUrl)return;
  const option=document.getElementById('repository-version-option');if(option)option.hidden=false;
  for(const key of ['codeUrl','codeVersion']){
    const input=form.elements[key];if(!input)continue;
    input.required=false;input.type='hidden';const label=input.closest?.('label');if(label)label.hidden=true;
    if(key==='codeUrl')input.value=data.repository.codeUrl;
    else if(previous?.codeUrl!==data.repository.codeUrl)input.value='';
  }
  if(leanGitee(data)){
    const preserved=[];
    for(const key of repositoryMaterialFields){
      const input=form.elements[key];if(!input)continue;
      if(input.value)preserved.push((materialFields.find(f=>f[0]===key)?.[1] || key)+'：\n'+input.value);
      input.required=false;input.disabled=true;const label=input.closest?.('label');if(label)label.hidden=true;
    }
    if(!form.querySelector('[data-repository-material-note]'))form.insertAdjacentHTML('afterbegin',repositoryMaterialNote()+
      (preserved.length?`<details data-repository-previous-input><summary>复制切换前的仓库说明输入</summary><p>这些输入保留在本页供复制到仓库，后续保存不再提交这些重复字段。</p><pre class="preserve-text">${esc(preserved.join('\n\n'))}</pre></details>`:''));
  }
}
function repositoryMaterialNote(){return '<p class="small-note" data-repository-material-note>本队已绑定 Gitee。请用 Git push，并在仓库内维护 README / 运行说明、许可证、第三方来源和已知局限；无需在官网重复填写。如选择同时提交仓库版本，将指向核验后的固定 SHA；平台不检查这些文件是否存在。</p>';}
function installTeamRepository(data,version,isCurrent,onUpdate=()=>{}){
  const box=document.getElementById('team-repository'),loginId=session.loginId;
  if(!box)return;
  const live=()=>box.isConnected && version===routeVersion && isCurrent();
  async function update(result){
    if(!live())return;
    const previous=data.repository;data.repository=result.repository;data.repositoryProviders=result.providers || data.repositoryProviders;
    syncRepositoryMaterialFields(data,previous);onUpdate();
    box.outerHTML=teamRepositoryMarkup(data);installTeamRepository(data,version,isCurrent,onUpdate);
  }
  const form=document.getElementById('repository-application');
  if(form)form.onsubmit=e=>{e.preventDefault();return withBusy(form.querySelector('button'),async()=>{if(!live())return;try{const usernames=form.elements.usernames.value.split(/[\s,，]+/).filter(Boolean);await update(await api('team/repository/apply',{provider:form.elements.provider.value,usernames},{loginId}));}catch(error){if(live())formError(form,error.message);}});};
  const refresh=document.getElementById('refresh-team-repository');
  if(refresh)refresh.onclick=e=>withBusy(e.currentTarget,async()=>{try{await update(await api('team/repository/refresh',{},{loginId}));}catch(error){if(live())document.getElementById('repository-message').textContent=error.message;}});
  const submit=document.getElementById('repository-submit');
  if(submit)submit.onclick=()=>{const target=document.getElementById('submission-form');if(!target || !live())return;target.scrollIntoView({behavior:'smooth',block:'start'});if(typeof target.requestSubmit==='function')target.requestSubmit(target.querySelector('button[type=submit]') || undefined);else target.onsubmit?.({preventDefault(){}});};
}
