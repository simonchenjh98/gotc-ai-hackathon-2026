'use strict';

// The roster's exact full name is the roadshow invitation; ordinary and finals logins remain separate.
let activeRoadshowAccess=null;
function disposeRoadshowAccess(){activeRoadshowAccess?.dispose();activeRoadshowAccess=null;}

async function roadshowNameRequest(path,payload,signal){
  if(!['roadshow/access','roadshow/login'].includes(path))throw new Error('姓名登录入口不正确。');
  const response=await fetch(new URL(`api/${path}`,base),{method:payload===undefined?'GET':'POST',credentials:path==='roadshow/login'?'same-origin':'omit',cache:'no-store',headers:payload===undefined?{}:{'Content-Type':'application/json'},body:payload===undefined?undefined:JSON.stringify(payload),signal});
  let data;try{data=await response.json();}catch{throw new Error('服务暂时不可用，请稍后重试。');}
  if(!response.ok)throw platformRequestError(response,data,'登录未完成，请核对真实姓名后重试。');
  return data;
}

function roadshowNameLoginMarkup(){
  return `<div class="login-layout"><div class="login-story"><p class="eyebrow">GOTC 2026 / ROADSHOW JUDGE</p><h1>路演评分<br>六维 100 分</h1><p>邀请码就是你的真实姓名。输入主办方名单中的完整姓名，验证后直接进入路演评分。</p><p>问题价值与理解20 · 方案与 AI 使用20 · 原型完成度20 · 测试与改进15 · 社会关怀与责任10 · 团队协作与表达15。每队至少3位评委正式评分才有效；超过3位时，统计全部有效正式评分的平均分。</p><span class="login-pixel" aria-hidden="true"></span></div><form class="login-card" data-roadshow-name-login><h2>路演评委登录</h2><label class="field">邀请码（真实姓名）<input name="judgeName" type="text" autocomplete="name" maxlength="120" required placeholder="输入名单中的完整真实姓名"></label><p data-roadshow-availability role="status">正在读取评委名单状态…</p><p class="form-error" role="alert"></p><button class="btn" type="submit" data-roadshow-login-button disabled>验证姓名并登录</button><button class="btn secondary small" type="button" data-roadshow-refresh>重新读取名单状态</button><p class="form-footnote">姓名须与主办方名单一致；不在名单中请联系主办方。路演期间看不到汇总成绩，成绩由主办方统一确认发布。</p></form></div>`;
}

function makeRoadshowNameLoginController({form,isCurrent,load,login,accept}){
  const name=form.elements.judgeName,error=form.querySelector('.form-error'),availability=form.querySelector('[data-roadshow-availability]');
  const submit=form.querySelector('[data-roadshow-login-button]'),refresh=form.querySelector('[data-roadshow-refresh]');
  let active=true,busy=false,configured=false,allowed=false,edit=0,refreshTimer=null;const requests=new Set();
  const current=()=>active && form.isConnected && isCurrent();
  const buttons=()=>{submit.disabled=busy || !configured || !allowed;refresh.disabled=busy;name.disabled=busy || !configured || !allowed;};
  const clearRefresh=()=>{if(refreshTimer!==null)clearTimeout(refreshTimer);refreshTimer=null;};
  const retryStatus=()=>{clearRefresh();if(current() && !allowed)refreshTimer=setTimeout(()=>{refreshTimer=null;void reload();},2000);};
  async function request(operation){
    const controller=new AbortController();requests.add(controller);const timeout=setTimeout(()=>controller.abort(),8000);
    try{const value=await operation(controller.signal);if(controller.signal.aborted)throw Object.assign(new Error('request aborted'),{name:'AbortError'});return value;}finally{clearTimeout(timeout);requests.delete(controller);}
  }
  async function reload(){
    if(!current() || busy)return;clearRefresh();busy=true;buttons();error.textContent='';availability.textContent='正在读取评委名单与登录开放状态…';
    try{const data=await request(load);if(!current())return;configured=data.configured===true;allowed=data.nameLoginAvailable===true && (!data.loginSchedule || data.loginSchedule.loginAvailable===true);availability.textContent=data.loginSchedule?.loginAvailable===false?(data.loginSchedule.message || '评委登录尚未开放，请等待主办方通知。')+(configured?'':' 评委名单待配置。'):data.message || (configured?'请使用名单中的真实姓名作为邀请码登录。':'评委名单待配置，请等待主办方录入。');}
    catch(failure){if(current()){allowed=false;availability.textContent='暂时无法读取名单状态。';error.textContent=failure.name==='AbortError'?'读取超时，请重新读取名单状态。':failure.message;}}
    finally{if(current()){busy=false;buttons();retryStatus();}}
  }
  async function submitName(event){
    event?.preventDefault();if(!current() || busy || !configured || !allowed)return;
    const requested=name.value.trim(),epoch=edit;if(!requested){error.textContent='请输入名单中的完整真实姓名。';return;}
    clearRefresh();busy=true;buttons();error.textContent='正在验证姓名并登录…';
    try{
      const unchanged=()=>current() && epoch===edit && name.value.trim()===requested;
      const result=await withLoginRetry(()=>request(signal=>login({name:requested},signal)), unchanged,
        message=>{error.textContent=message;});if(!current())return;
      if(epoch!==edit || name.value.trim()!==requested){error.textContent='姓名已变化，请核对当前姓名后重新登录。';return;}
      if(result.role!=='judge' || typeof result.loginId!=='string' || !result.loginId)throw new Error('登录返回的评委身份不正确，请重试或联系主办方。');
      await request(signal=>accept(result,signal,unchanged));
    }catch(failure){if(current()){if(failure.code==='judge_login_not_open'){allowed=false;availability.textContent=failure.message;}error.textContent=failure.name==='AbortError'?'登录请求超时，未确认登录身份。请核对姓名后手动重试，没有自动重发。':failure.message;}}
    finally{if(current()){busy=false;buttons();retryStatus();}}
  }
  const changed=()=>{edit++;error.textContent='';};name.addEventListener('input',changed);form.onsubmit=submitName;refresh.onclick=reload;buttons();
  return {reload,login:submitName,dispose(){active=false;clearRefresh();for(const controller of requests)controller.abort();name.value='';name.removeEventListener('input',changed);form.onsubmit=null;refresh.onclick=null;}};
}

function renderRoadshowNameLogin(version){
  disposeRoadshowAccess();const owner=session,marker=browserLoginOwner();
  root.innerHTML=roadshowNameLoginMarkup();const form=root.querySelector('[data-roadshow-name-login]');
  const current=()=>routeVersion===version && route==='judge-roadshow' && session===owner && browserLoginOwner()===marker;
  const controller=makeRoadshowNameLoginController({form,isCurrent:current,load:signal=>roadshowNameRequest('roadshow/access',undefined,signal),login:(body,signal)=>roadshowNameRequest('roadshow/login',body,signal),accept:async(result,signal,valid)=>{
    if(!valid())return;
    const confirmed=await api('session',undefined,{signal,recordVerifiedLogin:false});
    if(signal.aborted || !valid())return;
    if(confirmed.role!=='judge' || confirmed.loginId!==result.loginId)throw new Error('浏览器的登录身份与刚验证的姓名不一致，请核对后重新登录。');
    acceptVerifiedLogin(confirmed,marker);await renderRoute();
  }});
  activeRoadshowAccess=controller;void controller.reload();return controller;
}
