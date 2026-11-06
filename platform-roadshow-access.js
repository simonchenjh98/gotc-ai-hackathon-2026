'use strict';

// A roster name retrieves that judge's dedicated roadshow invitation. It never logs in automatically.
let activeRoadshowAccess=null;
function disposeRoadshowAccess(){activeRoadshowAccess?.dispose();activeRoadshowAccess=null;}

async function roadshowPublicRequest(path,payload,signal){
  if(!['roadshow/access','roadshow/claim'].includes(path))throw new Error('领取入口不正确。');
  const response=await fetch(new URL(`api/${path}`,base),{method:payload===undefined?'GET':'POST',credentials:'omit',cache:'no-store',headers:payload===undefined?{}:{'Content-Type':'application/json'},body:payload===undefined?undefined:JSON.stringify(payload),signal});
  let data;try{data=await response.json();}catch{throw new Error('服务暂时不可用，请稍后重试。');}
  if(!response.ok){const error=new Error(data.error || '领取未完成，请核对姓名后重试。');error.status=response.status;throw error;}
  return data;
}

function roadshowClaimMarkup(){
  return `<form class="login-card roadshow-claim" data-roadshow-claim><h2>按姓名领取路演邀请码</h2><p>填写主办方名单中的完整姓名。每位评委使用自己的专属邀请码；再次领取仍为同一个码。</p><label class="field">评委姓名<input name="judgeName" autocomplete="name" maxlength="120" required placeholder="填写名单中的完整姓名"></label><p data-roadshow-availability role="status">正在读取评委名单状态…</p><button class="btn" type="submit" data-roadshow-claim-button disabled>领取我的邀请码</button><button class="btn secondary small" type="button" data-roadshow-refresh>重新读取名单状态</button><p class="form-error" role="alert"></p><div data-roadshow-result hidden><p>已领取：<strong data-roadshow-name></strong></p><label class="field">你的路演邀请码<textarea name="claimedCode" readonly rows="3" spellcheck="false" autocomplete="off"></textarea></label><div class="form-actions"><button class="btn secondary small" type="button" data-roadshow-copy>复制邀请码</button><button class="btn small" type="button" data-roadshow-fill>填入登录框</button></div><p class="small-note">填入后，点击右侧登录按钮进入路演评审。</p></div></form>`;
}

function makeRoadshowClaimController({form,loginForm,isCurrent,load,claim,copy=code=>navigator.clipboard.writeText(code)}){
  const name=form.elements.judgeName,codeField=form.elements.claimedCode;
  const error=form.querySelector('.form-error'),availability=form.querySelector('[data-roadshow-availability]');
  const result=form.querySelector('[data-roadshow-result]'),resultName=form.querySelector('[data-roadshow-name]');
  const submit=form.querySelector('[data-roadshow-claim-button]'),refresh=form.querySelector('[data-roadshow-refresh]');
  const copyButton=form.querySelector('[data-roadshow-copy]'),fillButton=form.querySelector('[data-roadshow-fill]');
  let active=true,busy=false,configured=false,allowed=false,edit=0,loadVersion=0,code='',filled='';
  const requests=new Set();
  const current=()=>active && form.isConnected && loginForm.isConnected && isCurrent();
  function clearCode(){
    code='';codeField.value='';resultName.textContent='';result.hidden=true;
    if(filled && loginForm.elements.code.value===filled){loginForm.elements.code.value='';loginForm.elements.code.dispatchEvent(new Event('input',{bubbles:true}));}
    filled='';
  }
  function buttons(){submit.disabled=busy || !configured || !allowed;refresh.disabled=busy;name.disabled=!configured;copyButton.disabled=!code;fillButton.disabled=!code;}
  async function request(operation){
    const controller=new AbortController();requests.add(controller);const timeout=setTimeout(()=>controller.abort(),8000);
    try{return await operation(controller.signal);}finally{clearTimeout(timeout);requests.delete(controller);}
  }
  async function reload(){
    if(!current() || busy)return;
    const version=++loadVersion;busy=true;buttons();error.textContent='';availability.textContent='正在读取评委名单状态…';
    try{
      const data=await request(load);if(!current() || version!==loadVersion)return;
      configured=data.configured===true;allowed=data.claimAvailable===true;
      availability.textContent=data.message || (configured?'请按名单中的完整姓名领取。':'评委名单待配置。主办方导入名单后即可领取。');
      if(!allowed)clearCode();
    }catch(failure){if(current() && version===loadVersion){allowed=false;clearCode();availability.textContent='暂时无法读取名单状态。';error.textContent=failure.name==='AbortError'?'读取超时，请重新读取名单状态。':failure.message;}}
    finally{if(current() && version===loadVersion){busy=false;buttons();}}
  }
  async function claimName(event){
    event?.preventDefault();if(!current() || busy || !configured || !allowed)return;
    const requested=name.value.trim(),epoch=edit;if(!requested){error.textContent='请填写名单中的完整姓名。';return;}
    busy=true;clearCode();buttons();error.textContent='';
    try{
      const data=await request(signal=>claim({name:requested},signal));if(!current())return;
      if(epoch!==edit || name.value.trim()!==requested){error.textContent='姓名已变化，未显示刚才的领取结果。请按当前姓名重新领取。';return;}
      if(typeof data.code!=='string' || !data.code || data.code.length>160 || typeof data.name!=='string')throw new Error('返回的邀请码格式不正确，请联系主办方。');
      code=data.code;codeField.value=code;resultName.textContent=data.name;result.hidden=false;error.textContent='邀请码已领取，请复制或填入登录框，再点击登录。';
    }catch(failure){if(current())error.textContent=failure.name==='AbortError'?'领取超时，结果尚未确认；可按相同姓名重新领取，不会重复创建评委。':failure.message;}
    finally{if(current()){busy=false;buttons();}}
  }
  const changed=()=>{edit++;clearCode();error.textContent='';buttons();};
  name.addEventListener('input',changed);form.onsubmit=claimName;refresh.onclick=reload;
  fillButton.onclick=()=>{if(!current() || !code)return;filled=code;loginForm.elements.code.value=code;loginForm.elements.code.dispatchEvent(new Event('input',{bubbles:true}));loginForm.elements.code.focus();error.textContent='已填入登录框，请点击登录按钮。';};
  copyButton.onclick=async()=>{if(!current() || !code)return;const captured=code;try{await copy(captured);if(current() && code===captured)error.textContent='邀请码已复制。';}catch{if(current() && code===captured){codeField.focus();codeField.select();error.textContent='自动复制不可用，请复制上方已选中的邀请码。';}}};
  buttons();
  return {reload,claim:claimName,dispose(){active=false;loadVersion++;for(const controller of requests)controller.abort();clearCode();name.removeEventListener('input',changed);form.onsubmit=null;refresh.onclick=null;copyButton.onclick=null;fillButton.onclick=null;}};
}

function mountRoadshowClaim(version,loginForm){
  disposeRoadshowAccess();const owner=session;
  const story=root.querySelector('.login-story');story.insertAdjacentHTML('beforeend',roadshowClaimMarkup());
  const form=story.querySelector('[data-roadshow-claim]');
  const controller=makeRoadshowClaimController({form,loginForm,isCurrent:()=>routeVersion===version && route==='judge-roadshow' && session===owner,
    load:signal=>roadshowPublicRequest('roadshow/access',undefined,signal),claim:(body,signal)=>roadshowPublicRequest('roadshow/claim',body,signal)});
  activeRoadshowAccess=controller;void controller.reload();
}

function roadshowRosterMarkup(data){
  const rows=(data.judges || []).map(judge=>`<tr><td>${esc(judge.name)}</td><td>${judge.active?'有效':'已停用'}</td><td>${judge.claimed?'已领取':'尚未领取'}</td><td>${judge.canClaim?'可按姓名领取':'请联系主办方'}</td></tr>`).join('');
  return `<div class="info-banner ${data.configured?'':'warning'}">${data.configured?`已配置 ${(data.names || []).length} 位路演评委。`:'评委名单待配置；当前不能按姓名领取邀请码。'}</div><form class="surface" data-roadshow-roster><h2>路演评委名单</h2><p>每行填写一位评委的完整姓名。同名评委请加单位等说明区分，领取时须填写同样的名称。</p><label class="field">评委姓名（每行一位）<textarea name="names" rows="10" maxlength="20000" ${data.editable?'':'readonly'}>${esc((data.names || []).join('\n'))}</textarea></label><p class="small-note">${data.editable?'名单保存后会为每位评委建立路演专属身份，并加入当前路演评审名单。不会开放评分窗口。':'本轮路演已开放、冻结或有评审记录，名单已锁定。'}</p><button class="btn" type="submit" ${data.editable?'':'disabled'}>保存路演评委名单</button><button class="btn secondary" type="button" data-roadshow-reload>重新读取名单</button><p class="form-error" role="alert"></p></form><section class="surface"><h2>领取进度</h2>${rows?`<div class="table-scroll"><table><thead><tr><th>评委</th><th>身份状态</th><th>领取状态</th><th>姓名领取</th></tr></thead><tbody>${rows}</tbody></table></div>`:'<p>尚无评委名单。收到名单后在上方保存即可。</p>'}<p><a href="#judge-roadshow">打开路演评委入口 →</a></p><p><a href="#admin" data-roadshow-stage>配置路演队伍与评审窗口 →</a></p></section>`;
}

function installRoadshowRoster(view,data){
  const form=view.container.querySelector('[data-roadshow-roster]'),field=form.elements.names,error=form.querySelector('.form-error'),save=form.querySelector('[type=submit]');
  let revision=data.revision,runId=data.runId,edit=0,busy=false,blocked=false,editable=data.editable===true;
  view.form=form;
  const current=()=>view.current() && form.isConnected;
  const valid=()=>{if(!current())return false;if(session!==view.owner || !ownsBrowserLogin(view.owner)){save.disabled=true;error.textContent='管理员登录已失效或切换。当前名单输入已保留，请复制后重新登录。';return false;}return true;};
  const changed=()=>{edit++;dirtyAdminForms.add(form);};field.addEventListener('input',changed);
  view.cleanups.push(()=>field.removeEventListener('input',changed));
  form.querySelector('[data-roadshow-reload]').onclick=()=>{if(!valid())return;if(dirtyAdminForms.has(form) && !confirm('重新读取会替换当前未保存名单。请先复制要保留的内容，继续？'))return;dirtyAdminForms.delete(form);renderRoute();};
  form.onsubmit=async event=>{
    event.preventDefault();if(!valid() || busy || blocked || !editable)return;
    const epoch=edit,names=field.value.split(/\r?\n/).map(value=>value.trim()).filter(Boolean);
    busy=true;save.disabled=true;error.textContent='';
    try{
      const result=await api('admin/roadshow-access',{expectedRevision:revision,runId,names},{loginId:view.owner.loginId});
      if(!valid())return;revision=result.revision;runId=result.runId;editable=result.editable===true;
      if(edit===epoch){field.value=(result.names || []).join('\n');dirtyAdminForms.delete(form);error.textContent='名单已保存。评委现在可使用名单中的姓名领取邀请码。点击重新读取可查看最新领取进度。';}
      else error.textContent='提交时的名单已保存；保存期间的新输入已保留，尚未保存。';
    }catch(failure){
      if(!current())return;dirtyAdminForms.add(form);
      if(!valid())return;
      if(failure.status===409){blocked=true;error.textContent=failure.message+' 当前输入已保留，请复制后重新读取名单，再核对保存。';}
      else error.textContent=failure.message+' 当前输入已保留，没有自动重试保存。';
    }finally{busy=false;if(valid())save.disabled=blocked || !editable;}
  };
  view.container.querySelector('[data-roadshow-stage]').onclick=()=>{adminStage='roadshow';};
}

async function renderRoadshowAccessAdmin(version){
  disposeRoadshowAccess();const owner=session;
  const view={owner,container:null,form:null,disposed:false,cleanups:[],current:()=>!view.disposed && routeVersion===version && route==='admin-roadshow' && (!view.container || view.container.isConnected),dispose(){view.disposed=true;view.cleanups.forEach(fn=>fn());if(view.form)dirtyAdminForms.delete(view.form);}};
  activeRoadshowAccess=view;
  const data=await api('admin/roadshow-access',undefined,{loginId:owner.loginId});
  if(!view.current() || session!==owner || !ownsBrowserLogin(owner))return;
  root.innerHTML=heading('ROADSHOW / 评委管理','路演名单与实时成绩','先录入评委名单；评委用自己的完整姓名领取专属邀请码，再登录路演网站。')+`<div class="roadshow-access-admin">${roadshowRosterMarkup(data)}</div>`;
  view.container=root.querySelector('.roadshow-access-admin');installRoadshowRoster(view,data);
  if(typeof mountRoadshowScoringAdmin==='function')await mountRoadshowScoringAdmin(version,view.container);
}
