'use strict';

const submissionUploadKinds={
  slides:{field:'slidesUrl',label:'PPT / 展示材料',accept:'.ppt,.pptx,.pdf',max:50*1024*1024,hint:'上传 PPT、PPTX 或 PDF，最大 50 MiB，建议 5–7 页。也可直接填写公开 HTTPS 链接。'},
  files:{field:'filesUrl',label:'作品压缩包 ZIP',accept:'.zip',max:100*1024*1024,hint:'必填：请把完整作品文件打包成一个 ZIP，最大 100 MiB。ZIP 内部文件结构保持不变。代码仓库和 commit 均为选填。'},
  demo:{field:'demoUrl',label:'HTML 作品',accept:'.html,.htm',max:10*1024*1024,hint:'上传单文件 HTML / HTM，最大 10 MiB，图片和脚本请内嵌或使用 HTTPS 地址。多文件网站请部署后填写公开分享链接；评委应能直接打开查看。'}
};
function submissionUploadMarkup(kind){const spec=submissionUploadKinds[kind];return `<div class="submission-upload"><p class="small-note" id="upload-${kind}-help">${spec.hint}</p><label class="field" for="upload-${kind}-file">选择${kind==='files'?'作品压缩包 ZIP':spec.label+'文件'}<input type="file" id="upload-${kind}-file" accept="${spec.accept}" aria-describedby="upload-${kind}-help upload-${kind}-name"></label><p class="small-note upload-filename" id="upload-${kind}-name">可直接选择文件；未填作品名称时，文件按“队名+作品.扩展名”命名。</p><button type="button" class="btn secondary" id="upload-${kind}-button">上传${spec.label}</button><p class="upload-status" id="upload-${kind}-status" role="status" aria-live="polite"></p><p class="upload-result" id="upload-${kind}-result"></p></div>`;}
function siteUploadedMaterial(value){try{const url=new URL(value),prefix=new URL('materials/',base);return url.origin===prefix.origin && !url.search && !url.hash && url.pathname.startsWith(prefix.pathname) && /^[A-Za-z0-9_-]{43}\/[^/]+$/.test(url.pathname.slice(prefix.pathname.length));}catch{return false;}}
function submissionUploadProblem(kind,file,project){const spec=submissionUploadKinds[kind];if(Array.from(project).length>140)return '作品名称不能超过 140 字，请修改后再上传。';if(!file)return '请先选择一个文件。';const ext='.'+file.name.split('.').at(-1).toLowerCase();if(!spec.accept.split(',').includes(ext))return `文件格式不支持，请选择 ${spec.accept} 文件。`;if(!file.size)return '文件为空，请重新选择。';if(file.size>spec.max)return `文件超过 ${spec.max/1024/1024} MiB，请压缩或填写公开 HTTPS 链接。`;return '';}
async function uploadSubmissionFile(kind,file,project,owner,signal){
  const response=await fetch(new URL(`api/team/uploads/${kind}`,base),{method:'POST',credentials:'same-origin',cache:'no-store',headers:{'Content-Type':'application/octet-stream','X-GOTC-Login':owner.loginId,'X-GOTC-Project':encodeURIComponent(project),'X-GOTC-Filename':encodeURIComponent(file.name)},body:file,signal});
  let result;try{result=await response.json();}catch{throw new Error('未收到有效上传结果；原链接和其他输入已保留，请重试。');}
  if(!response.ok){const error=new Error(result.error || '上传未成功，请重试。');error.status=response.status;throw error;}
  if(result.kind!==kind || !safeMaterialUrl(result.url) || !siteUploadedMaterial(result.url) || typeof result.filename!=='string' || !result.filename || result.size!==file.size)throw new Error('上传回执不完整；原链接已保留，请重试。');
  return result;
}
function installSubmissionUploads(data,form,{isCurrent,canUpload,onChange,onBusy,onUnauthorized}){
  const owner=session,loginId=owner.loginId,pageVersion=routeVersion,marker=browserLoginOwner();let pending=false;
  const uploads=new Map(),initialProject=form.elements.projectName?.value.trim() || '作品';
  const live=()=>session===owner && owner.loginId===loginId && browserLoginOwner()===marker && ownsBrowserLogin(owner) && pageVersion===routeVersion && form.isConnected && isCurrent();
  const element=(kind,suffix)=>document.getElementById(`upload-${kind}-${suffix}`);
  function refresh(){
    const project=form.elements.projectName?.value.trim() || '作品';
    for(const [kind,spec] of Object.entries(submissionUploadKinds)){
      const input=element(kind,'file'),file=input?.files?.[0],name=element(kind,'name'),field=form.elements[spec.field];
      if(name)name.textContent=project?`文件名预览：${data.team.name}+${project}${file?'.'+file.name.split('.').at(-1).toLowerCase():'.扩展名'}（以服务器返回名称为准）`:'可直接选择文件；未填作品名称时，文件按“队名+作品.扩展名”命名。';
      const uploaded=uploads.get(kind),needsRename=uploaded?.url===field?.value.trim()?uploaded.project!==project:siteUploadedMaterial(field?.value) && project!==initialProject;
      if(!pending){const status=element(kind,'status');if(needsRename)status.textContent='作品名称已变化，原上传链接已保留；请按新名称重新上传后再保存或正式提交。';else if(status?.textContent?.startsWith('作品名称已变化'))status.textContent='请核对材料链接，上传后仍需保存草稿或正式提交。';}
    }
  }
  function controls(busy){for(const kind of Object.keys(submissionUploadKinds)){const button=element(kind,'button');if(button)button.disabled=busy;}onBusy(busy);}
  for(const [kind,spec] of Object.entries(submissionUploadKinds)){
    const button=element(kind,'button'),input=element(kind,'file'),status=element(kind,'status'),resultBox=element(kind,'result'),field=form.elements[spec.field];if(!button || !input || !field)continue;
    input.addEventListener('change',refresh);
    button.onclick=async()=>{
      if(pending || !live() || !canUpload())return;
      const project=form.elements.projectName.value.trim() || '作品',file=input.files?.[0],problem=submissionUploadProblem(kind,file,project);
      if(problem){status.textContent=problem;(project?input:form.elements.projectName).focus();return;}
      const previous=field.value,controller=new AbortController(),timer=setTimeout(()=>controller.abort(),330000);
      pending=true;controls(true);status.textContent='正在上传，请保持此页打开。文件上传完成后仍需保存草稿或正式提交。';resultBox.innerHTML='';
      try{
        const result=await uploadSubmissionFile(kind,file,project,owner,controller.signal);if(!live())return;
        resultBox.innerHTML=`<a href="${esc(result.url)}" target="_blank" rel="noopener noreferrer">${kind==='demo'?'打开作品':'查看 / 下载'}：${esc(result.filename)} ↗</a>`;
        if((form.elements.projectName.value.trim() || '作品')!==project || field.value!==previous){status.textContent='文件已上传，但上传期间作品名或链接已修改，未替换当前输入。请核对上方文件；作品名变化时需重新上传。';return;}
        field.value=result.url;form.elements.confirmed.checked=false;uploads.set(kind,{url:result.url,project});onChange();status.textContent='文件已上传，链接已填入当前草稿；尚未保存草稿或正式提交。';
      }catch(error){if(live()){status.textContent=error.name==='AbortError'?'上传等待超时；原链接和其他输入已保留，请重试。':error.message;if([401,403].includes(error.status))onUnauthorized();}}
      finally{clearTimeout(timer);pending=false;if(live()){controls(false);refresh();}}
    };
  }
  refresh();return {busy:()=>pending,refresh};
}
