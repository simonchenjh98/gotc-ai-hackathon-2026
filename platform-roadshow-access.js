'use strict';

// The roster's exact full name is the roadshow invitation; ordinary and finals logins remain separate.
let activeRoadshowAccess=null;
function disposeRoadshowAccess(){activeRoadshowAccess?.dispose();activeRoadshowAccess=null;}

async function roadshowNameRequest(path,payload,signal){
  if(!['roadshow/access','roadshow/login'].includes(path))throw new Error('姓名登录入口不正确。');
  const response=await fetch(new URL(`api/${path}`,base),{method:payload===undefined?'GET':'POST',credentials:path==='roadshow/login'?'same-origin':'omit',cache:'no-store',headers:payload===undefined?{}:{'Content-Type':'application/json'},body:payload===undefined?undefined:JSON.stringify(payload),signal});
  let data;try{data=await response.json();}catch{throw new Error('服务暂时不可用，请稍后重试。');}
  if(!response.ok){const error=new Error(data.error || '登录未完成，请核对真实姓名后重试。');error.status=response.status;throw error;}
  return data;
}

function roadshowNameLoginMarkup(){
  return `<div class="login-layout"><div class="login-story"><p class="eyebrow">GOTC 2026 / ROADSHOW JUDGE</p><h1>路演评分<br>六维 100 分</h1><p>邀请码就是你的真实姓名。输入主办方名单中的完整姓名，验证后直接进入路演评分。</p><p>创意20 · 社会关怀15 · 技术呈现20 · 应用潜力20 · AI跨界探索15 · 开源贡献10。每队至少3位评委正式评分才有效，路演开始后90分钟截止。</p><span class="login-pixel" aria-hidden="true"></span></div><form class="login-card" data-roadshow-name-login><h2>路演评委登录</h2><label class="field">邀请码（真实姓名）<input name="judgeName" type="text" autocomplete="name" maxlength="120" required placeholder="输入名单中的完整真实姓名"></label><p data-roadshow-availability role="status">正在读取评委名单状态…</p><p class="form-error" role="alert"></p><button class="btn" type="submit" data-roadshow-login-button disabled>验证姓名并登录</button><button class="btn secondary small" type="button" data-roadshow-refresh>重新读取名单状态</button><p class="form-footnote">姓名须与主办方名单一致；不在名单中请联系主办方。<a href="#board-roadshow">查看公开路演成绩 →</a></p></form></div>`;
}

function makeRoadshowNameLoginController({form,isCurrent,load,login,accept}){
  const name=form.elements.judgeName,error=form.querySelector('.form-error'),availability=form.querySelector('[data-roadshow-availability]');
  const submit=form.querySelector('[data-roadshow-login-button]'),refresh=form.querySelector('[data-roadshow-refresh]');
  let active=true,busy=false,configured=false,allowed=false,edit=0;const requests=new Set();
  const current=()=>active && form.isConnected && isCurrent();
  const buttons=()=>{submit.disabled=busy || !configured || !allowed;refresh.disabled=busy;name.disabled=busy || !configured || !allowed;};
  async function request(operation){
    const controller=new AbortController();requests.add(controller);const timeout=setTimeout(()=>controller.abort(),8000);
    try{const value=await operation(controller.signal);if(controller.signal.aborted)throw Object.assign(new Error('request aborted'),{name:'AbortError'});return value;}finally{clearTimeout(timeout);requests.delete(controller);}
  }
  async function reload(){
    if(!current() || busy)return;busy=true;buttons();error.textContent='';availability.textContent='正在读取评委名单状态…';
    try{const data=await request(load);if(!current())return;configured=data.configured===true;allowed=data.nameLoginAvailable===true;availability.textContent=data.message || (configured?'请使用名单中的真实姓名作为邀请码登录。':'评委名单待配置，请等待主办方录入。');}
    catch(failure){if(current()){allowed=false;availability.textContent='暂时无法读取名单状态。';error.textContent=failure.name==='AbortError'?'读取超时，请重新读取名单状态。':failure.message;}}
    finally{if(current()){busy=false;buttons();}}
  }
  async function submitName(event){
    event?.preventDefault();if(!current() || busy || !configured || !allowed)return;
    const requested=name.value.trim(),epoch=edit;if(!requested){error.textContent='请输入名单中的完整真实姓名。';return;}
    busy=true;buttons();error.textContent='正在验证姓名并登录…';
    try{
      const result=await request(signal=>login({name:requested},signal));if(!current())return;
      if(epoch!==edit || name.value.trim()!==requested){error.textContent='姓名已变化，请核对当前姓名后重新登录。';return;}
      if(result.role!=='judge' || typeof result.loginId!=='string' || !result.loginId)throw new Error('登录返回的评委身份不正确，请重试或联系主办方。');
      await request(signal=>accept(result,signal,()=>current() && epoch===edit && name.value.trim()===requested));
    }catch(failure){if(current())error.textContent=failure.name==='AbortError'?'登录请求超时，未确认登录身份。请核对姓名后手动重试，没有自动重发。':failure.message;}
    finally{if(current()){busy=false;buttons();}}
  }
  const changed=()=>{edit++;error.textContent='';};name.addEventListener('input',changed);form.onsubmit=submitName;refresh.onclick=reload;buttons();
  return {reload,login:submitName,dispose(){active=false;for(const controller of requests)controller.abort();name.value='';name.removeEventListener('input',changed);form.onsubmit=null;refresh.onclick=null;}};
}

function renderRoadshowNameLogin(version){
  disposeRoadshowAccess();const owner=session;let marker=browserLoginOwner();
  root.innerHTML=roadshowNameLoginMarkup();const form=root.querySelector('[data-roadshow-name-login]');
  const current=()=>routeVersion===version && route==='judge-roadshow' && session===owner && browserLoginOwner()===marker;
  const controller=makeRoadshowNameLoginController({form,isCurrent:current,load:signal=>roadshowNameRequest('roadshow/access',undefined,signal),login:(body,signal)=>roadshowNameRequest('roadshow/login',body,signal),accept:async(result,signal,valid)=>{
    if(!valid())return;publishLogin(result);marker=browserLoginOwner();
    if(marker && marker!==result.loginId)throw new Error('浏览器登录身份已切换，请刷新后重新输入姓名。');
    const confirmed=await api('session',undefined,{signal});
    if(signal.aborted || !valid())return;
    if(confirmed.role!=='judge' || confirmed.loginId!==result.loginId)throw new Error('浏览器的登录身份与刚验证的姓名不一致，请核对后重新登录。');
    session=confirmed;await renderRoute();
  }});
  activeRoadshowAccess=controller;void controller.reload();return controller;
}

function roadshowRosterMarkup(data){
  const rows=(data.judges || []).map(judge=>`<tr><td>${esc(judge.name)}</td><td>${judge.active?'有效':'已停用'}</td><td>${judge.canLogin?'可用真实姓名登录':'请联系主办方'}</td></tr>`).join('');
  return `<div class="info-banner ${data.configured?'':'warning'}">${data.configured?`已配置 ${(data.names || []).length} 位路演评委。`:'评委名单待配置；当前不能用真实姓名登录。'}</div><form class="surface" data-roadshow-roster><h2>路演评委名单</h2><p>每行填写一位评委的完整姓名。真实姓名就是邀请码，登录时须与名单一致。同名评委请加单位等说明区分。</p><label class="field">评委姓名（每行一位）<textarea name="names" rows="10" maxlength="20000" ${data.editable?'':'readonly'}>${esc((data.names || []).join('\n'))}</textarea></label><p class="small-note">${data.editable?'名单保存后会为每位评委建立路演专属身份，并加入当前路演评审名单。不会开放评分窗口。':'本轮路演已开放、冻结或有评审记录，名单已锁定。'}</p><button class="btn" type="submit" ${data.editable?'':'disabled'}>保存路演评委名单</button><button class="btn secondary" type="button" data-roadshow-reload>重新读取名单</button><p class="form-error" role="alert"></p></form><section class="surface"><h2>登录资格</h2>${rows?`<div class="table-scroll"><table><thead><tr><th>评委</th><th>身份状态</th><th>真实姓名登录</th></tr></thead><tbody>${rows}</tbody></table></div>`:'<p>尚无评委名单。收到名单后在上方保存即可。</p>'}<p><a href="#judge-roadshow">打开路演评委入口 →</a></p><p><a href="#admin" data-roadshow-stage>配置路演队伍与评审窗口 →</a></p></section>`;
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
      if(edit===epoch){field.value=(result.names || []).join('\n');dirtyAdminForms.delete(form);error.textContent='名单已保存。评委现在可使用名单中的真实姓名作为邀请码直接登录。点击重新读取可查看最新登录资格。';}
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
  root.innerHTML=heading('ROADSHOW / 评委管理','路演名单与实时成绩','先录入评委名单；真实姓名就是邀请码，评委输入名单中的完整姓名即可验证登录。')+`<div class="roadshow-access-admin">${roadshowRosterMarkup(data)}</div>`;
  view.container=root.querySelector('.roadshow-access-admin');installRoadshowRoster(view,data);
  if(typeof mountRoadshowScoringAdmin==='function')await mountRoadshowScoringAdmin(version,view.container);
}
