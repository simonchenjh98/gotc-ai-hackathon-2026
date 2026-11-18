'use strict';

let initialSessionPending = false;

function requestedRoute() {
  const page = location.hash.slice(1).split('/')[0];
  return Object.hasOwn(platformRoutes, page) ? page : 'team';
}

async function renderRoute(){
  if(typeof disposeAdminFinalsDashboard==='function')disposeAdminFinalsDashboard();
  if(typeof disposeApiAdmin==='function')disposeApiAdmin();
  if(typeof disposeRoadshowAccess==='function')disposeRoadshowAccess();
  if(typeof disposeRoadshowWindow==='function')disposeRoadshowWindow();
  if(typeof disposeFinalsView==='function')disposeFinalsView();
  currentTeamRecovery?.flush();
  currentTeamRecovery=null;
  currentTeamKeys?.dispose();
  currentIdentityPage=null;
  if(typeof currentTeamProfile!=='undefined'){currentTeamProfile?.dispose();currentTeamProfile=null;}
  if(typeof currentRegistrationAdmin!=='undefined'){currentRegistrationAdmin?.dispose();currentRegistrationAdmin=null;}
  currentAdminTeamLifecycle?.lock();currentAdminTeamLifecycle=null;
  if(typeof disposeAdminReviewSync==='function')disposeAdminReviewSync();adminReadOnlyView=null;adminLoadPending=null;
  clearTimeout(pollTimer);if(typeof currentClaimController!=='undefined'){currentClaimController?.dispose();currentClaimController=null;}const version=++routeVersion;
  route = requestedRoute();
  lastBoard=null;document.body.classList.remove('screen-mode','finals-mode');if(route.endsWith('-finals'))document.body.classList.add('finals-mode');
  document.querySelectorAll('[data-route]').forEach(a=>{a.classList.toggle('active',a.dataset.route===route);if(a.dataset.route===route)a.setAttribute('aria-current','page');else a.removeAttribute('aria-current');});
  const page = route;
  const owner = session;
  const target = routeTarget(page, owner);
  document.title = platformRoutes[page].title + ' · GOTC 2026';
  account();
  root.innerHTML = '<div class="loading-state">正在读取活动数据…</div>';
  try {
    if (target.files.length) await loadPlatformAssets(target.files);
    if (version !== routeVersion || requestedRoute() !== page) return;
    if (session !== owner) return renderRoute();
    if (owner.loginId && (!ownsBrowserLogin(owner) || browserLogoutOwner() === owner.loginId)) {
      session = {role:null};
      return renderRoute();
    }
    await target.render(version);
  } catch (error) {
    if (version !== routeVersion || requestedRoute() !== page) return;
    const role = platformRoutes[page].role;
    if (error.status === 401 && role && !session.role) {
      account();
      renderLogin(role);
    } else {
      showConnectionError(error);
    }
  }
}

window.addEventListener('hashchange',()=>{if(initialSessionPending)return;if(restoreSubmissionRoute){restoreSubmissionRoute=false;return;}if(!confirmLeave()){restoreSubmissionRoute=true;location.hash=route;return;}submissionDirty=false;reviewDirty=false;scoreDirty=false;dirtyAdminForms.clear();renderRoute();});
window.addEventListener('click',async event=>{
  if(event.target.closest?.('.skip-link[href="#platform-main"]')){
    event.preventDefault();
    root.focus();
    return;
  }
  const button=event.target.closest?.('[data-copy-value]');if(!button)return;try{await navigator.clipboard.writeText(button.dataset.copyValue);toast('已复制。');}catch{toast('复制不可用，请展开完整记录或哈希并手动复制。');}});
window.addEventListener('storage',event=>{
  if(typeof currentRegistrationAdmin!=='undefined'){if(event?.key===teamLogoutKey && event.newValue===currentRegistrationAdmin?.loginId)currentRegistrationAdmin?.lock();else currentRegistrationAdmin?.valid();}
  if(typeof currentTeamProfile!=='undefined'){if(event?.key===teamLogoutKey && event.newValue===currentTeamProfile?.loginId)currentTeamProfile?.lock();else currentTeamProfile?.valid();}
  if (route==='team-score' && event.key===teamLogoutKey && event.newValue===session.loginId) {
    session={role:null};
    renderRoute();
  }
  if (route==='team-score' && session.role==='team' && !ownsBrowserLogin(session)) renderRoute();
  if(event?.key===teamLogoutKey && event.newValue===currentAdminTeamLifecycle?.loginId)currentAdminTeamLifecycle?.lock();
  else currentAdminTeamLifecycle?.valid();
  currentTeamKeys?.valid();currentIdentityPage?.valid();currentTeamRecovery?.storageChanged();});
function refreshVisibleTeamScore(){
  if(typeof currentRegistrationAdmin!=='undefined')currentRegistrationAdmin?.valid();
  if(typeof currentTeamProfile!=='undefined')currentTeamProfile?.valid();
  currentAdminTeamLifecycle?.valid();currentIdentityPage?.valid();
  if(route==='team-score' && session.role==='team')renderRoute();
}
window.addEventListener('focus',refreshVisibleTeamScore);
window.addEventListener('pageshow',event=>{if(event.persisted)refreshVisibleTeamScore();});
window.addEventListener('pagehide',()=>currentTeamRecovery?.flush());
window.addEventListener('beforeunload',event=>{currentTeamRecovery?.flush();if(claimNavigationState().pendingInitial || claimNavigationState().dirty || profileNavigationState().dirty || submissionDirty || reviewDirty || scoreDirty || dirtyAdminForms.size){event.preventDefault();event.returnValue='';}});
async function startPlatform() {
  initialSessionPending = true;
  try { session = await api('session'); }
  catch { session = {role:null}; }
  initialSessionPending = false;
  await renderRoute();
}
startPlatform();
