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
const stageNotice=stage=>stage==='roadshow'?'<div class="info-banner"><strong>路演与决赛名单</strong><br>路演成绩确认发布时，默认按有效均分从高到低同步最多 12 支队伍，同分按队号排序。组织方另行确认的决赛名单优先。完整汇总仍仅组织方可见。</div>':'';
let logoutInFlight=null,currentTeamKeys=null,currentIdentityPage=null,currentAdminTeamLifecycle=null;
let session={role:null}, route='', pollTimer, routeVersion=0, lastBoard, judgeData, selectedTeam, toastTimer;
const roleName = {team:'参赛队伍',judge:'专业评委',audience:'观众',admin:'组织方'};
function toast(message) { const el = document.getElementById('platform-toast'); el.textContent = message; el.classList.add('visible'); clearTimeout(toastTimer); toastTimer = setTimeout(() => el.classList.remove('visible'), 4500); }
const loginOwnerKey=`gotc-login-owner:v1:${encodeURIComponent(base.pathname)}`;
// A server-verified cookie may outlive a failed/quota-blocked owner publication.
// Keep the request-time marker snapshot; a later login publication still invalidates it.
let verifiedBrowserLogin=null;
const teamLogoutKey=loginOwnerKey+':logout';
function browserLoginOwner(){try{return localStorage.getItem(loginOwnerKey);}catch{return undefined;}}
function browserLogoutOwner(){try{return localStorage.getItem(teamLogoutKey);}catch{return undefined;}}
function publishLogin(login){if(!login.loginId)return;try{localStorage.setItem(loginOwnerKey,login.loginId);}catch{/* Server saving remains available without storage. */}}
function acceptVerifiedLogin(login, previousMarker) {
  // The matching cookie stays usable if this browser can read storage but cannot write it.
  verifiedBrowserLogin = {loginId:login.loginId, owner:previousMarker};
  publishLogin(login);
  session = login;
}
function ownsBrowserLogin(login){const owner=browserLoginOwner();return !owner || !login.loginId || owner===login.loginId || (verifiedBrowserLogin?.loginId===login.loginId && verifiedBrowserLogin.owner===owner);}
async function api(path, data, options={}) {
  const lifecyclePath=['team/profile','admin/registration','admin/data/team','admin/data/recycle-bin','admin/data/team/recycle','admin/data/team/restore'].includes(path.split('?')[0]);
  if((lifecyclePath || path==='admin/keys' || path.startsWith('admin/keys/')) && !ownsBrowserLogin(session))throw new Error('登录身份已变化，请刷新页面后重试。');
  const recoveryAtStart=currentTeamRecovery,sessionAtStart=session,adminLifecycleAtStart=currentAdminTeamLifecycle,ownerAtStart=path==='session'?browserLoginOwner():null;
  const res = await fetch(new URL(`api/${path}`, base), {credentials:'same-origin',cache:'no-store',method:data === undefined ? 'GET' : 'POST',headers:{...(data===undefined?{}:{'Content-Type':'application/json'}),...((lifecyclePath || ['team','team/score','submission','logout','judge','score','admin','admin/settings','admin/keys/upstreams/models','admin/team-workbench','team/workbench/return','audience','vote','judging/exhibition','judging/exhibition/review','judging/roadshow','judging/roadshow/review'].includes(path) || path.startsWith('submission/') || path==='admin/keys' || path.startsWith('admin/keys/') || path==='team/keys' || path.startsWith('team/keys/') || path.startsWith('team/repository') || path.startsWith('admin/repositories') || path==='admin/claims' || path.startsWith('admin/claims/') || path==='admin/roadshow-access' || ['roadshow/scoring','roadshow/score','admin/roadshow/scoring','admin/roadshow/publish','admin/roadshow/teams','admin/judging/roadshow/window'].includes(path) || path.startsWith('finals/') || path.startsWith('admin/finals')) && (options.loginId ?? session.loginId)?{'X-GOTC-Login':options.loginId ?? session.loginId}:{})},body:data === undefined ? undefined : JSON.stringify(data),signal:options.signal});
  let body; try { body = await res.json(); } catch { throw new Error('服务暂时不可用，请稍后重试。'); }
  const confirmingLogin = path === 'session' && options.recordVerifiedLogin === false;
  if (!res.ok) {
    if (!confirmingLogin) {
      if (path === 'admin' && [401,403,428].includes(res.status) && session === sessionAtStart
          && currentAdminTeamLifecycle === adminLifecycleAtStart) adminLifecycleAtStart?.lock();
      if ([401,403].includes(res.status) && currentTeamRecovery === recoveryAtStart) recoveryAtStart?.lock();
      if (res.status === 401 && session === sessionAtStart && (!recoveryAtStart || currentTeamRecovery === recoveryAtStart)) {
        session = {role:null};
        currentAdminTeamLifecycle?.valid();
      }
    }
    const error = new Error(body.error || '操作未成功，请重试。');
    error.status = res.status;
    error.code = body.code;
    throw error;
  }
  if(path==='session' && !confirmingLogin)verifiedBrowserLogin={loginId:body.loginId,owner:ownerAtStart};
  return body;
}
function trackAdminForm(form){form.addEventListener('input',()=>{form.editRevision=(form.editRevision || 0)+1;dirtyAdminForms.add(form);});}
function stageHasUnsavedInputs(){return [...dirtyAdminForms].some(form=>form.isConnected && form.closest('#judging-admin'));}
function clearStageUnsavedInputs(){for(const form of dirtyAdminForms)if(!form.isConnected || form.closest('#judging-admin'))dirtyAdminForms.delete(form);}
function claimNavigationState(){return typeof currentClaimController!=='undefined'?currentClaimController?.navigationState?.()??{}:{};}
function profileNavigationState(){return typeof currentTeamProfile!=='undefined'?currentTeamProfile?.navigationState?.()??{}:{};}
function confirmLeave(){const claim=claimNavigationState();if(claim.pendingInitial){toast('申请正在提交，请等待领取凭据保存后再离开。');return false;}return !(claim.dirty || profileNavigationState().dirty || submissionDirty || reviewDirty || scoreDirty || dirtyAdminForms.size) || confirm(submissionDirty?'有尚未保存服务器的作品输入。离开不会自动提交；只有成功保存的本机副本可在重新登录本队后找回，未备份输入可能丢失。确认离开？':'有尚未保存的输入，确认离开并放弃这些输入？');}
function account(){
  const el=document.getElementById('account');el.innerHTML=session.role?`<span>${session.operator?'管理员代管 · ':''}${esc(session.name || roleName[session.role])}</span>${session.operator?'<button type="button" id="switch-team-workbench">切换队伍</button>':''}<button id="logout">退出</button>`:'';
  document.getElementById('logout')?.addEventListener('click',logoutAccount);
  document.getElementById('switch-team-workbench')?.addEventListener('click', async event => {
    const button = event.currentTarget, owner = session, version = routeVersion;
    try { await loadPlatformAssets(['team/delegation.js']); }
    catch (error) { if (button.isConnected) toast(error.message); return; }
    if (session === owner && version === routeVersion && button.isConnected) {
      await returnAdminTeamWorkbench({currentTarget:button});
    }
  });
}
async function logoutAccount(event,{preserveClaim=false}={}){
    if(claimNavigationState().pendingInitial){toast('申请正在提交，请等待领取凭据保存后再退出。');return false;}
    if(logoutInFlight?.owner===session)return;
    const operation={owner:session,context:currentTeamRecovery},button=event?.currentTarget || document.getElementById('logout');logoutInFlight=operation;button.disabled=true;
    const owned=()=>session===operation.owner;
    try{let local=null,teamAtExit=null;
      if (session.role === 'team') {
        await loadPlatformAssets(draftAssets);
        if (!owned()) return;
      }
      if(session.role==='team'){teamAtExit=await api('team');if(!owned())return;if(currentTeamRecovery && currentTeamRecovery.teamId!==teamAtExit.team.id)currentTeamRecovery.lock('当前登录队伍已变化，本地副本需重新授权后查看。');const store=teamDraftStore(teamAtExit,undefined,teamAtExit.loginId ?? operation.owner.loginId ?? '');const captured=store?.capturePurge();local={purge:()=>store?.purge(captured) ?? false};}
      if(teamAtExit?!confirm(`退出当前登录的${teamAtExit.team.name}会清除本浏览器内本队当前及历史恢复副本，并停止旧标签编辑；退出期间重新登录后新建的副本会保留；服务器材料不受影响。${claimNavigationState().dirty?'本页尚未提交的队伍申请输入也会丢弃。':''}${profileNavigationState().dirty?'本页尚未保存的资料输入也会丢弃。':''}确认退出？`):!confirmLeave())return;
      if(!preserveClaim && typeof hasClaimReceipt==='function' && hasClaimReceipt() && !confirm('退出会清除此标签页的队伍申请凭据。请先返回申请页领取并复制提交码；尚未批准的申请将无法继续领取。确认清除并退出？'))return false;
      await api('logout',{}, {loginId:teamAtExit?.loginId ?? operation.owner.loginId});
      const claimClearError=!preserveClaim && typeof clearClaimReceipt==='function'?clearClaimReceipt():'';if(claimClearError)toast(claimClearError);
      if(['team','admin'].includes(operation.owner.role) && operation.owner.loginId){
        try{localStorage.setItem(teamLogoutKey,operation.owner.loginId);}catch{/* Revalidate on focus when storage is unavailable. */}
      }
      let cleaned=true;if(local && (owned() || operation.owner.loginId))cleaned=local.purge();if(!owned())return;submissionDirty=false;reviewDirty=false;scoreDirty=false;dirtyAdminForms.clear();currentTeamRecovery?.lock('已退出。');session={role:null};await renderRoute();if(!cleaned)toast('已退出，但无法确认本队本地副本已清理，请在共享设备上清除此站点的浏览器数据。');return true;
    }catch(error){if(owned() || (logoutInFlight===operation && !session.role))toast('退出未确认，本地副本已保留。'+error.message);}
    finally{if(logoutInFlight===operation)logoutInFlight=null;if(button.isConnected)button.disabled=false;}
}

async function openClaimWorkbench(event){
  event.preventDefault();
  if(!confirmLeave())return;
  if(session.role && !await logoutAccount(event,{preserveClaim:true}))return;
  location.hash='team';
}

function heading(kicker,title,description,tools='') { return `<div class="page-heading"><div><p class="eyebrow">${kicker}</p><h1>${title}</h1><p>${description}</p></div>${tools ? `<div class="heading-tools">${tools}</div>` : ''}</div>`; }
function empty(title,description,action='') { return `<div class="empty-state"><span class="empty-symbol" aria-hidden="true">▦</span><h2>${title}</h2><p>${description}</p>${action}</div>`; }
function metrics(items) { return `<div class="metrics">${items.map(i=>`<div class="metric"><strong${i[2]?` id="${esc(i[2])}"`:''}>${esc(i[0])}</strong><span>${esc(i[1])}</span></div>`).join('')}</div>`; }
function formError(form,error) { form.querySelector('.form-error').textContent = error; }

// Keep an expired editor readable for copying. Late responses cannot update a new login.
function editorHasLogin(form,owner,{notify=false,error}={}){
  const sameBrowser=ownsBrowserLogin(owner);
  if(session===owner && sameBrowser)return true;
  if(notify || (error?.status===401 && !session.role && sameBrowser)){
    formError(form,'当前登录已失效或已切换。请先复制要保留的输入，再刷新页面并核对登录身份。');
  }
  return false;
}

async function withBusy(button, fn) { if (!button || button.disabled) return; button.disabled = true; try { await fn(); } catch(e) { toast(e.message); } finally { if (button.isConnected) button.disabled = false; } }
function showConnectionError(error) { root.innerHTML = heading('CONNECTION / 连接状态','暂时无法连接平台',esc(error.message)) + empty('连接暂时中断','请检查网络，稍后重试。尚未提交的操作不会显示为成功。','<button class="btn" id="retry">重新连接</button>'); document.getElementById('retry').onclick = error.refreshDocument ? () => location.reload() : renderRoute; }


function renderLogin(role) {
  if(route==='judge-finals')return renderFinalsJudge(routeVersion);
  if(route==='judge-roadshow' && typeof renderRoadshowNameLogin==='function')return renderRoadshowNameLogin(routeVersion);
  const roleLabel={team:'TEAM',judge:'JUDGE',audience:'AUDIENCE',admin:'ADMIN'}[role];
  if (!Object.hasOwn(roleName,role)) throw new Error('无效登录入口');
  const stage=stageForRoute();
  const loginRoleName=route==='admin-finals'?'决赛管理员':route==='admin-roadshow'?'路演管理员':roleName[role];
  const copy = {team:route==='team-profile'?['本队资料与成员','使用现有参赛码（原提交码）登录，查看本队成员和学校、补充队伍资料。作品提交和查分继续使用同一个账号。']:route==='team-score'?['本队查分','输入主办方发给本队的提交码，查看本队已发布的总分、六维均分及评委同意分享的匿名评语。']:['选手工作台<br>AI 工具 · 作品提交','用本队提交码登录，在同一工作台查看 AI 工具、准备代码仓库和提交作品。没有提交码，请先提交队伍申请。'],judge:route==='judge'?['专业评分<br>六维 100 分','使用主办方提供的评委码，选择自己的身份。评分可保存草稿，正式提交后计入专业均分。']:route==='judge-roadshow'?['路演评分<br>六维 100 分','邀请码就是你的真实姓名，输入名单中的完整姓名即可验证登录。问题价值20、方案与 AI 使用20、原型完成度20、测试与改进15、社会关怀与责任10、团队协作与表达15分；路演期间不显示汇总分数。']:[`${stageName(stage)}记录`,'使用主办方提供的评委码，选择自己的身份。可查看本轮队伍、保存草稿、提交评语或回避；本阶段记录暂不计分。'],audience:['选出让你<br>眼前一亮的作品。','观众选择票独立于专业成绩。使用观众邀请码登录，每个邀请码最多支持 3 支不同队伍。'],admin:route==='admin-finals'?['决赛管理<br>评分 · 提名 · 颁奖','管理7位决赛评委和决赛队伍，实时核对六维评分与提名票数，确认专项奖归属后发布结果。请用个人组织方账号登录。']:route==='admin-roadshow'?['路演管理<br>名单 · 实时成绩','配置路演评委名单，查看姓名登录资格与实时六维评分，核对后发布路演成绩。请用个人组织方账号登录。']:['组织方<br>活动管理','管理队伍、评委、评审窗口和作品材料。请用个人组织方账号登录。']}[role];
  const organizer = role === 'admin';
  const organizerFields = organizer ? `
    <label class="field">组织方用户名<input name="username" autocomplete="username" maxlength="80" required></label>
    <label class="field">验证器动态码<input name="totp" inputmode="numeric" autocomplete="one-time-code" maxlength="6" pattern="[0-9]{6}" required></label>
    <label><input type="checkbox" name="legacyAdmin"> 迁移期间使用原管理码</label>
    <p class="small-note">首个实名组织方账号启用后，原管理码入口关闭。</p>` : '';
  root.innerHTML = `<div class="login-layout"><div class="login-story"><p class="eyebrow">GOTC 2026 / ${roleLabel}</p><h1>${copy[0].split('<br>').map(esc).join('<br>')}</h1><p>${esc(copy[1])}</p><span class="login-pixel" aria-hidden="true"></span></div><form class="login-card" id="login-form"><h2>${esc(loginRoleName)}登录</h2>${role==='team'?'<p><a href="#claim">尚无提交码？提交队伍申请并领取 ↗</a></p>'+(route==='team-profile'?'':'<p class="small-note">组织方请从<a href="#admin">组织方登录</a>进入，再选择需要代管的队伍。</p>'):''}<p>邀请码仅用于本届活动，请妥善保管。<br>如无法登录，请核对审核状态或联系主办方。</p>${organizerFields}<label class="field">${role === 'admin' ? '个人凭据或原管理码' : (role === 'judge' ? '评委登录码' : role === 'team' ? '本队参赛码（原提交码）' : '观众邀请码')}<input name="code" type="password" autocomplete="${organizer ? 'current-password' : 'one-time-code'}" required maxlength="160" placeholder="输入你的${role === 'admin' ? '管理码' : '邀请码'}"></label><p class="form-error" role="alert"></p><button type="submit" class="btn">${route==='team-score'?'查看本队成绩':route==='team-profile'?'进入本队资料与成员':'进入'+esc(loginRoleName)+'工作台 ↗'}</button><p class="form-footnote">本队查分仅显示当前登录队伍；全部队伍成绩看板仅组织方可见。</p></form></div>`;
  const form=document.getElementById('login-form');
  const page = route, version = routeVersion;
  const mounted = () => route === page && routeVersion === version && form.isConnected
    && document.getElementById('login-form') === form;
  const changedMessage = role === 'team'
    ? '浏览器的登录身份已切换，请重新输入本队提交码。'
    : '浏览器的登录身份已切换，请核对身份并重新输入邀请码。';
  const identityField=document.createElement('label');
  identityField.className='field';identityField.hidden=true;
  const identityName=role==='judge'?'评委':'管理员';
  identityField.innerHTML=`选择自己的${identityName}身份<select name="accountId"><option value="">请选择主办方分配的身份</option></select>`;
  form.querySelector('.form-error').before(identityField);
  const identity=form.elements.accountId;
  const {username, totp, legacyAdmin} = form.elements;
  if (organizer) {
    legacyAdmin.addEventListener('change', () => {
      for (const field of [username, totp]) {
        field.disabled = legacyAdmin.checked;
        field.required = !legacyAdmin.checked;
        field.value = '';
      }
      form.elements.code.value = '';
      identityField.hidden = true;
      identity.required = false;
      identity.value = '';
    });
  }
  form.elements.code.addEventListener('input',()=>{identityField.hidden=true;identity.required=false;identity.innerHTML='<option value="">请选择主办方分配的身份</option>';});
  form.onsubmit = async event => {
    event.preventDefault();
    const button = form.querySelector('button');
    if (button.disabled || !mounted()) return;
    button.disabled = true;
    formError(form, '');
    const owner = session, marker = browserLoginOwner();
    const code = form.elements.code.value.trim();
    const named = organizer && !legacyAdmin.checked;
    const credentials = named ? {username:username.value.trim(), totp:totp.value.trim()} : {};
    if (named && (!credentials.username || !/^[0-9]{6}$/.test(credentials.totp))) {
      formError(form, '请输入组织方用户名和验证器中的六位动态码。');
      button.disabled = false;
      return;
    }
    const accountId = identityField.hidden ? '' : identity.value;
    const currentInput = () => mounted() && session === owner
      && form.elements.code.value.trim() === code
      && (!organizer || named === !legacyAdmin.checked)
      && (!named || (username.value.trim() === credentials.username && totp.value.trim() === credentials.totp))
      && (identityField.hidden ? '' : identity.value) === accountId;
    try {
      const result = await api('login', {role, code, ...credentials, ...(accountId && !named ? {accountId} : {})});
      if (!currentInput()) return;
      if (browserLoginOwner() !== marker) { formError(form, changedMessage); return; }
      if (result.selectAccount) {
        identity.replaceChildren(new Option('请选择主办方分配的身份', ''));
        for (const account of result.accounts) identity.add(new Option(account.name, account.id));
        identityField.hidden = false;
        identity.required = true;
        identity.focus();
        formError(form, `登录码正确，请选择你自己的${identityName}身份后继续。`);
        return;
      }
      const confirmed = await api('session', undefined, {recordVerifiedLogin:false});
      if (!currentInput()) return;
      const allowedRole = result.role === role;
      if (!allowedRole || !result.loginId || confirmed.loginId !== result.loginId
          || confirmed.role !== result.role || browserLoginOwner() !== marker) {
        formError(form, changedMessage);
        return;
      }
      acceptVerifiedLogin(confirmed, marker);
      await renderRoute();
    } catch (error) {
      if (mounted()) formError(form, error.message);
    } finally {
      if (form.isConnected) button.disabled = false;
    }
  };
}
function renderWrongRole(role) { root.innerHTML = heading('ACCESS / 登录身份','请使用对应的邀请码',`当前身份为${esc(roleName[session.role] || session.role)}，此入口需要${roleName[role]}身份。`) + empty('切换登录身份','切换后使用此入口的专属邀请码登录；独立的队伍申请凭据会保留，可返回申请页领取提交码。','<button class="btn" id="switch-role">切换身份（保留申请）</button>');document.getElementById('switch-role').onclick=event=>logoutAccount(event,{preserveClaim:true}); }

function beijingTime(value){return value===null || value===undefined?'—':new Date(value).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false});}
function beijingInput(value){if(value===null || value===undefined)return '';const parts=new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).formatToParts(new Date(value));const part=k=>parts.find(p=>p.type===k).value;return `${part('year')}-${part('month')}-${part('day')}T${part('hour')}:${part('minute')}`;}
function parseBeijingInput(value){if(!value)return null;if(!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(value))throw new Error('请填写有效的北京时间。');const time=new Date(value+':00+08:00').getTime();if(!Number.isSafeInteger(time) || time<0 || time>253402300799999 || beijingInput(time)!==value)throw new Error('请填写有效的北京时间。');return time;}

let currentTeamRecovery=null;
