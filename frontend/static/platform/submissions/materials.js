'use strict';

const materialFields=[
  ['projectName','作品名称（选填）',140,'text',false],['summary','项目介绍（选填）',600,'textarea',false],
  ['codeUrl','公开代码仓库主页（选填）',1024,'url',false],['codeVersion','最终 commit 链接或完整哈希（选填）',1024,'text',false],
  ['readmeUrl','README / 运行说明链接（选填）',1024,'url',false],['license','开源许可证名称或说明（选填）',200,'text',false],
  ['licenseUrl','许可证文件链接（选填）',1024,'url',false],
  ['demoUrl','作品展示链接（选填，可直接打开）',8192,'url',false],['demoInstructions','演示补充说明（选填）',2000,'textarea',false],
  ['slidesUrl','PPT / 展示材料链接',1024,'url',true],['filesUrl','作品压缩包 ZIP 链接',1024,'url',true],['videoUrl','演示视频链接（选填）',1024,'url',false],
  ['sourcesAndLimitations','模型、数据、图片、第三方代码来源与项目局限（选填）',2000,'textarea',false]
];
const repositoryMaterialFields=new Set(['readmeUrl','license','licenseUrl','sourcesAndLimitations']);
function leanGitee(data){return data.repository?.provider==='gitee' && !!data.repository.codeUrl;}
function repositoryMaterialsUrl(materials){
  const repo=repositoryParts(materials?.codeUrl),sha=materials?.codeVersion;
  if(repo?.host!=='gitee.com' || !fullCommit(sha))return '';
  const expected=materials.codeUrl+'/tree/'+sha.toLowerCase();
  return materials.repositoryMaterialsUrl===expected?expected:'';
}
function repositoryMaterialsText(materials){const url=repositoryMaterialsUrl(materials);return url?'仓库内材料（请在此固定版本查看 README、许可证、第三方来源与已知局限；文件内容未由平台检查）：'+url:'';}

function submissionState(s){return s?.submitted?(s.hasUnsubmittedChanges?'已提交且有草稿修改':'已正式提交'):s?.draft?'草稿':'未保存';}
function siteUploadedMaterial(value){
  try {
    const url = new URL(value), prefix = new URL('materials/', base);
    return url.origin === prefix.origin && !url.username && !url.password && !url.search && !url.hash
      && url.pathname.startsWith(prefix.pathname) && /^[A-Za-z0-9_-]{43}\/[^/]+$/.test(url.pathname.slice(prefix.pathname.length));
  } catch { return false; }
}
function safeMaterialFieldUrl(key, value){
  if (typeof value !== 'string') return '';
  const trimmed = value.trim(), demo = key === 'demoUrl';
  if (!trimmed || (demo && !/^https?:\/\//i.test(trimmed)) || /[\s\\\u0000-\u001f\u007f]/.test(trimmed)
      || /%(?:0[0-9a-f]|1[0-9a-f]|7f)/i.test(trimmed) || new TextEncoder().encode(trimmed).length > (demo ? 8192 : 1024)) return '';
  try {
    const url = new URL(trimmed);
    const eventFile = (key === 'slidesUrl' || key === 'filesUrl') && siteUploadedMaterial(trimmed);
    if (url.protocol !== 'https:' && !(url.protocol === 'http:' && (demo || eventFile))) return '';
    if (!url.hostname || /^https?:\/\/[^/?#]*@/i.test(trimmed) || url.username || url.password || url.port === '0') return '';
    const validHost = url.hostname.startsWith('[') || url.hostname.replace(/\.$/, '').split('.')
      .every(label => /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/i.test(label));
    return validHost ? trimmed : '';
  } catch { return ''; }
}
function safeMaterialUrl(value){return safeMaterialFieldUrl('', value);}
function safeDemoUrl(value){return safeMaterialFieldUrl('demoUrl', value);}
function fullCommit(value){return /^(?:[a-f0-9]{40}|[a-f0-9]{64})$/i.test(value);}
const commitLinkHosts=new Set(['github.com','gitee.com','gitcode.com','gitlab.com']);
function repositoryParts(value,commit=false){
  if(!safeMaterialUrl(value) || !/^https:\/\//i.test(value) || /[%\s\\]/.test(value) || /\/(?:\.\.?)(?:\/|$)/.test(value))return null;
  try{
    const url=new URL(value),host=url.hostname;
    // Check explicit ports as well: URL() removes the default :443 spelling.
    if(!commitLinkHosts.has(host) || value.split('/')[2].toLowerCase()!==host || url.search || url.hash)return null;
    let parts=url.pathname.replace(/\/+$/,'').slice(1).split('/'),sha=null;
    if(commit){
      let marker=host==='gitlab.com'?['-','commit']:['commit'];
      if(host==='gitcode.com' && parts.slice(-3,-1).join('/')==='commits/detail')marker=['commits','detail'];
      const tail=marker.length+1;
      if(parts.length<tail+2 || parts.slice(-tail,-1).join('/')!==marker.join('/') || !fullCommit(parts.at(-1)))return null;
      sha=parts.at(-1).toLowerCase();parts=parts.slice(0,-tail);
    }
    parts[parts.length-1]=parts.at(-1).replace(/\.git$/,'');
    // Match the server's GitHub-only, repository-segment allowance for names such as .github.
    if(parts.length<2 || (host!=='gitlab.com' && parts.length!==2) || parts.some((p,index)=>!(host==='github.com' && index===parts.length-1?/^[A-Za-z0-9_.][A-Za-z0-9_.-]*$/:/^[A-Za-z0-9_][A-Za-z0-9_.-]*$/).test(p) || ['.','..'].includes(p)))return null;
    const path=parts.join('/');return {host,path:['github.com','gitee.com'].includes(host)?path.toLowerCase():path,sha};
  }catch{return null;}
}
function validCodeRepository(value){if(!safeMaterialUrl(value))return false;const host=new URL(value).hostname;return !commitLinkHosts.has(host) || !!repositoryParts(value);}
function normalizedCommit(repository,value){
  const repo=repositoryParts(repository);
  if(!validCodeRepository(repository))throw new Error('仓库请填写完整 HTTPS 仓库主页链接，不是本地路径或文件页面；GitHub、Gitee、GitCode、GitLab 请使用仓库根地址。');
  if(fullCommit(value))return value.toLowerCase();
  const commit=repositoryParts(value,true);
  if(!repo || !commit || commit.host!==repo.host || commit.path!==repo.path)throw new Error('请填写同一 GitHub、Gitee、GitCode 或 GitLab 仓库的完整 commit 链接，或40/64位完整哈希；其他平台请填完整哈希。不要填分支、tag或前6位；链接请去掉参数和片段。');
  return commit.sha;
}
function commitViewUrl(repository,value,seal=null){
  const repo=repositoryParts(repository);if(!repo || !fullCommit(value))return '';
  const fixed=repositoryParts(seal?.viewUrl,true);
  if(repo.host==='gitee.com' && seal?.sha?.toLowerCase()===value.toLowerCase() && fixed?.host===repo.host && fixed.path===repo.path && fixed.sha===value.toLowerCase())return seal.viewUrl;
  const marker=repo.host==='gitlab.com'?'-/commit':repo.host==='gitcode.com'?'commits/detail':'commit';
  return `https://${repo.host}/${repo.path}/${marker}/${value.toLowerCase()}`;
}
function commitMarkup(value,repository='',seal=null){const url=commitViewUrl(repository,value,seal),link=url?` <a href="${esc(url)}" target="_blank" rel="noopener noreferrer">查看此版本 ↗</a>`:'';return fullCommit(value)?`<code>${esc(value.slice(0,6))}</code> <button type="button" class="text-action" data-copy-value="${esc(value)}">复制完整哈希</button><details><summary>查看完整哈希</summary><code>${esc(value)}</code>${link}</details>`:esc(value);}
function repositorySealMarkup(seal,materials){
  if(!seal || !fullCommit(materials?.codeVersion) || seal.sha!==materials.codeVersion)return '';
  return `<p class="small-note"><strong>版本已封存 · Gitee 引用</strong><br>封存引用：<code>${esc(seal.ref)}</code><br>封存时间（北京时间）：${esc(beijingTime(seal.sealedAt))}${seal.receivedAt!==undefined?`<br>服务器接收时间（北京时间）：${esc(beijingTime(seal.receivedAt))}`:''}<br>引用封存不是独立源码备份；开发分支保护另行核对。</p>`;
}
function privateBackupUrl(value,teamId,version,file){
  if(typeof value!=='string' || !Number.isSafeInteger(version) || version<1 || /[%\s\\\u0000-\u001f\u007f]/.test(value) || /\/(?:\.\.?)(?:\/|$)/.test(value))return '';
  try{
    const url=new URL(value,base),path=base.pathname+'api/submission/backups/'+encodeURIComponent(teamId)+'/'+version+'/'+file;
    return ['http:','https:'].includes(url.protocol) && url.origin===base.origin && !url.username && !url.password && !url.search && !url.hash && url.pathname===path?url.href:'';
  }catch{return '';}
}
function backupErrorText(code){
  if(['task_limit','storage_limit','file_count_limit','source_limit'].includes(code))return '备份超过容量或文件限制，请联系组织方。';
  if(code==='git_timeout')return '备份超时，请稍后查询或联系组织方。';
  if(['git_unavailable','provider_unavailable','worker_interrupted'].includes(code))return '备份服务暂不可用，请稍后查询。';
  return '备份未完成，请联系组织方核对。';
}
function repositoryBackupMarkup(backup,materials,teamId,version,seal=null){
  if(!backup)return seal && fullCommit(materials?.codeVersion) && seal.sha===materials.codeVersion?'<p class="small-note">Gitee 版本已封存；本版未创建独立源码备份任务。</p>':'<p class="small-note">本版未归档：无独立源码备份记录。</p>';
  if(!fullCommit(materials?.codeVersion) || backup.sha!==materials.codeVersion)return '<p class="small-note">备份版本不匹配，请重新读取本版记录。</p>';
  const labels={queued:'独立源码备份排队中',running:'独立源码备份处理中',ready:'独立源码备份已就绪',partial:'独立源码备份不完整',failed:'独立源码备份失败'},saved=['ready','partial'].includes(backup.status);
  const allowed=['team','admin'].includes(session.role),zip=saved && allowed?privateBackupUrl(backup.downloadUrl,teamId,version,'source.zip'):'',bundle=saved && allowed?privateBackupUrl(backup.bundleUrl,teamId,version,'repo.bundle'):'';
  const sample=backup.missingAssets || [],total=Number.isSafeInteger(backup.missingAssetCount)?backup.missingAssetCount:sample.length;
  const omitted=Number.isSafeInteger(backup.missingAssetOmitted)?backup.missingAssetOmitted:0;
  return `<p class="small-note"><strong>${esc(labels[backup.status] || '独立源码备份状态待核对')}</strong> · 尝试 ${Number.isSafeInteger(backup.attempts)?backup.attempts:0} 次<br>备份 SHA：<code>${esc(backup.sha)}</code>${backup.error?'<br>'+backupErrorText(backup.error):''}</p>${saved?`<p class="small-note">已保存并校验此版本的 Git 字节，不代表项目可运行或依赖齐全；不含 PPT 和视频。独立源码备份仅本队和组织方可下载。</p>${backup.status==='partial'?`<p class="small-note">未抓取的外部内容共 ${total} 项。${omitted?`受列表长度限制，${omitted} 项未列出。`:''}</p><ul>${sample.map(asset=>`<li>${esc(asset.kind==='lfs'?'LFS':asset.kind==='submodule'?'子模块':'外部内容')}：<code>${esc(asset.path)}</code></li>`).join('')}</ul>`:''}<p>${zip?`<a href="${esc(zip)}">下载源码 ZIP</a>`:''}${zip && bundle?' · ':''}${bundle?`<a href="${esc(bundle)}">下载 Git bundle</a>`:''}</p><p class="small-note">${/^[a-f0-9]{64}$/i.test(backup.sourceZipSha256 || '')?`ZIP SHA-256：<code>${esc(backup.sourceZipSha256)}</code><br>`:''}${/^[a-f0-9]{64}$/i.test(backup.bundleSha256 || '')?`bundle SHA-256：<code>${esc(backup.bundleSha256)}</code>`:''}</p>`:'<p class="small-note">正式提交记录仍有效；此状态不代表源码已备份。本站上传材料单独保存；外部链接内容不备份。</p>'}`;
}
function repositoryVersionMarkup(data,materials,teamId,version,id,label='本版'){
  return `<section class="repository-version" id="${esc(id)}" data-repository-version="${Number(version)}"><h3>${esc(label)} v${Number(version)} · ${data.repositoryBackup?'封存与备份':data.repositorySeal?'封存版本':'版本记录'}</h3>${repositorySealMarkup(data.repositorySeal,materials)}<div id="${esc(id)}-backup">${repositoryBackupMarkup(data.repositoryBackup,materials,teamId,version,data.repositorySeal)}</div>${data.repositoryBackup?`<button type="button" class="btn secondary small" id="${esc(id)}-refresh">刷新本版备份状态</button>`:''}<p class="small-note" id="${esc(id)}-message" role="status"></p></section>`;
}
function installRepositoryBackup(container,path,version,materials,isCurrent,{teamId,latest=false}={}){
  if(!container)return;
  const owner=session,loginId=owner.loginId,marker=browserLoginOwner(),page=route,pageVersion=routeVersion,id=container.id;
  const target=document.getElementById(id+'-backup'),message=document.getElementById(id+'-message'),button=document.getElementById(id+'-refresh');
  container.dataset.repositoryVersion=String(version);let busy=false;
  const live=()=>session===owner && owner.loginId===loginId && browserLoginOwner()===marker && ownsBrowserLogin(owner) && ['team','admin'].includes(owner.role) && route===page && routeVersion===pageVersion && container.isConnected && document.getElementById(id)===container && container.dataset.repositoryVersion===String(version) && isCurrent();
  if(!button)return;
  button.onclick=buttonAction(async()=>{
    if(busy || !live())return;busy=true;button.disabled=true;
    try{
      const data=await api(path,undefined,{loginId});if(!live())return;
      if((latest && (data.team?.id!==teamId || data.submission?.submittedVersion!==version)) || (!latest && data.version!==version)){message.textContent='正式版本已变化，已保留当前面板；请重新读取相应版本。';return;}
      // Only the mutable backup block changes. Drafts, receipts and check forms stay intact.
      target.innerHTML=repositoryBackupMarkup(data.repositoryBackup,materials,teamId,version);message.textContent='已刷新本版备份状态；当前输入保持不变。';
    }catch{if(live())message.textContent='备份状态读取未完成；当前输入已保留，请稍后再试。';}
    finally{busy=false;if(live())button.disabled=false;}
  });
}
function materialMarkup(materials,seal=null){if(!materials)return '<p class="small-note">暂无材料。</p>';return `<dl class="material-list">${repositoryMaterialsUrl(materials)?`<div><dt>仓库内项目材料</dt><dd><a href="${esc(repositoryMaterialsUrl(materials))}" target="_blank" rel="noopener noreferrer">查看这版仓库文件 ↗</a><p class="small-note">请在此版本查看 README / 运行说明、许可证、第三方来源和已知局限。平台未检查这些文件是否存在及其内容。</p></dd></div>`:''}${materialFields.map(([key,label,,type])=>{const value=materials[key] || '';if(!value)return '';const safe=type==='url'?safeMaterialFieldUrl(key,value):'';return `<div><dt>${esc(label)}</dt><dd>${key==='codeVersion'?commitMarkup(value,materials.codeUrl,seal):safe?`<a href="${esc(safe)}" target="_blank" rel="noopener noreferrer">${esc(value)} ↗</a>`:esc(value)}</dd></div>`;}).join('')}<div><dt>材料自查声明</dt><dd>${materials.confirmed===true?'已确认（队伍自行声明）':'尚未确认'}</dd></div></dl>`;}
function judgeSubmissionMarkup(record){return `<section class="surface submitted-materials"><h2>最新正式项目材料</h2><p class="small-note">原专业评分入口读取最新正式版；此处不是展示或路演的冻结快照，评分不绑定材料版本。</p>${record?`<p class="small-note">正式版本 ${Number(record.version)} · 北京时间 ${esc(beijingTime(record.submittedAt))}</p>${materialMarkup(record.materials)}`:'<p class="small-note">暂未正式提交材料；仍可根据现场演示评分。</p>'}</section>`;}
function windowMarkup(window){return `<div class="info-banner ${window.canEdit?'':'warning'}">${window.canEdit?'作品提交开放中':'当前作品提交只读：尚未开放或已截止'} · 截止时间（北京时间）：${esc(beijingTime(window.submissionDeadline))}${window.submissionDeadline===null?'（未设置）':''}<br>服务器会在实际保存前检查提交窗口和材料版本。点击提交不代表已保存，请确认成功提示和最新版本。正式材料记录保存填写的内容和链接；本站上传的文件另行保存。平台不会抓取或检查外链内容；外链中的文件之后仍可能变化。Gitee 正式版本封存状态见下方；已有独立源码备份可继续下载。</div>`;}
function stageMaterialStatus(data,latestVersion){const snapshot=data?.materialSnapshot;return !data?.inRoster?'未列入本轮名单':data.frozenAt===null?'尚未生成材料快照':snapshot?`本轮采用 v${snapshot.submissionVersion}${latestVersion && snapshot.submissionVersion!==latestVersion?`（最新正式版为 v${latestVersion}）`:''}`:'本轮冻结时无正式材料';}
function stageMaterialMarkup(stage,data,latestVersion){const snapshot=data?.materialSnapshot;return `<div class="stage-material" data-stage-material="${stage}"><strong>${stageName(stage)}：${esc(stageMaterialStatus(data,latestVersion))}</strong>${data?`<p class="small-note">轮次 ${esc(data.runId)} · 冻结时间 ${esc(beijingTime(data.frozenAt))}（北京时间）</p>`:''}${snapshot?`<details><summary>查看本轮实际材料</summary>${materialMarkup(snapshot.materials)}</details>`:''}</div>`;}

function judgingMaterialsMarkup(stages,latestVersion){return `<section class="surface submission-versions"><h2>本轮评审采用的材料</h2><p class="small-note">原专业评分读取最新正式版。展示与路演按各自首次开放时的快照；重新提交不会替换已冻结材料。</p>${['exhibition','roadshow'].map(stage=>stageMaterialMarkup(stage,stages?.[stage],latestVersion)).join('')}</section>`;}

const repositoryStates={requested:'待核对批准',provisioning:'正在处理，勿重复操作',reconcile:'结果待核对恢复',invited:'仓库已创建 · 邀请待接受',ready:'仓库已绑定'};
function repositoryStatusMarkup(repository){
  if(!repository)return '';
  const label=repository.state==='reconcile' && repository.canSubmit?'仓库已创建 · 部分成员未授权':repositoryStates[repository.state] || repository.state;
  return `<h3>${esc(label)}</h3><p>${esc(repository.message)}</p><p class="repository-location">${repository.codeUrl?`<a href="${esc(safeMaterialUrl(repository.codeUrl))}" target="_blank" rel="noopener noreferrer">${esc(repository.codeUrl)}</a>`:esc(repository.organization+'/'+repository.slug)}<br>正式分支：<code>${esc(repository.branch)}</code><br>开发者：${repository.usernames.map(esc).join('、')}</p><p class="small-note">禁止 force push、删除正式分支或移除正式提交。分支保护：${repository.protection==='enabled'?'已配置禁止强推与删除':'尚未验证，主办方须人工核对'}。仅记录完整 SHA 时，<strong>代码尚未归档</strong>；本版封存与独立备份以下方状态为准。子模块与 LFS 外部内容须另行核对。</p>`;
}
