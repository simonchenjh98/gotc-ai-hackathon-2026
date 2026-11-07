import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const source=fs.readFileSync(new URL('../team-registration.js',import.meta.url),'utf8');
const context=vm.createContext({});vm.runInContext(source+'\nthis.ui=RegistrationUI;',context);
const team={id:'fixture',number:7,name:'<img src=x onerror=alert(1)>',captain:'Alex',school:'Fixture School',members:['Alex','Alex','Student A'],targetSize:3,project:'<script>',summary:'"unsafe"',flags:['队员待补充'],recruiting:true};

test('public directory escapes text, preserves same-name members, and links only to session-owned profile',()=>{
  const html=context.ui.card(team);
  assert(!html.includes('<img'));assert(!html.includes('<script>'));assert(html.includes('&lt;img'));
  assert.equal([...html.matchAll(/ · 队长/g)].length,1);assert.equal([...html.matchAll(/class="captain"/g)].length,1);assert.match(html,/>Alex<\/span>/);
  assert.match(html,/href="https:\/\/8\.141\.114\.201\/gotc\/platform.html#team-profile"/);
  assert(!html.includes('teamId='));assert(!html.includes('管理码'));
});
test('public search still supports school/member keywords and recruitment filter',()=>{
  assert.equal(context.ui.matches(team,' student a ',false),true);assert.equal(context.ui.matches(team,'Fixture School',true),true);
  assert.equal(context.ui.matches(team,'other',false),false);assert.equal(context.ui.matches({...team,recruiting:false},'',true),false);
});
function publicFixture(transport){
  const nodes=new Map(),requests=[],removed=[];
  const host={isConnected:true,dataset:{api:'api/registration'},innerHTML:'',querySelector(selector){if(!nodes.has(selector))nodes.set(selector,{value:'',checked:false,textContent:'',innerHTML:'',disabled:false});return nodes.get(selector);}};
  const scope=vm.createContext({document:{querySelectorAll:()=>[]},URL,location:{href:'https://example.test/gotc/teams.html'},sessionStorage:{removeItem:key=>removed.push(key),getItem(){throw new Error('Legacy code must never be restored');},setItem(){throw new Error('No registration credential should be persisted');}},fetch:async(url,options)=>{requests.push({url,options});return transport?.(url,options) || {ok:true,json:async()=>({teams:[team]})};}});
  scope.host=host;vm.runInContext(source,scope);
  return {scope,host,requests,removed,node:selector=>host.querySelector(selector),install:()=>vm.runInContext('RegistrationUI.install(host)',scope)};
}
test('public directory uses GET without credentials, removes only its legacy key and has no anonymous editor',async()=>{
  const f=publicFixture();await f.install();
  assert.equal(f.requests.length,1);assert.equal(f.requests[0].options.method,'GET');assert.equal(f.requests[0].options.credentials,'omit');
  assert.deepEqual(f.removed,['gotc-registration-code:v1:https://example.test/gotc/api/registration']);
  assert.match(f.host.innerHTML,/本队资料与成员/);assert.match(f.host.innerHTML,/platform.html#claim/);assert(!f.host.innerHTML.includes('<form'));assert(!f.host.innerHTML.includes('管理码'));
  assert.match(f.node('[data-reg-list]').innerHTML,/platform.html#team-profile/);
  f.node('[data-reg-search]').value='Student A';f.node('[data-reg-search]').oninput();assert.match(f.node('[data-reg-count]').textContent,/当前显示 1/);
});
test('out-of-order public refresh cannot replace a newer directory',async()=>{
  let firstResolve,secondResolve,calls=0;const pending=[new Promise(r=>firstResolve=r),new Promise(r=>secondResolve=r)];
  const f=publicFixture(()=>pending[calls++]),initial=f.install(),refresh=f.node('[data-reg-refresh]').onclick();
  secondResolve({ok:true,json:async()=>({teams:[{...team,name:'NEW DIRECTORY'}]})});await refresh;
  firstResolve({ok:true,json:async()=>({teams:[{...team,name:'OLD DIRECTORY'}]})});await initial;
  assert.match(f.node('[data-reg-list]').innerHTML,/NEW DIRECTORY/);assert(!f.node('[data-reg-list]').innerHTML.includes('OLD DIRECTORY'));
});
test('public pages expose existing-code profile and pending application links',()=>{
  for(const path of ['index.html','teams.html']){
    const html=fs.readFileSync(new URL('../'+path,import.meta.url),'utf8');assert.match(html,/platform.html#team-profile/);assert.match(html,/platform.html#claim/);assert.match(html,/原提交码/);assert(!html.includes('管理码'));
  }
});
test('organizer registration UI keeps contact/history reads and removes credential issuance controls',async()=>{
  const nodes=new Map(),requests=[];const node=selector=>{if(!nodes.has(selector))nodes.set(selector,{isConnected:true,textContent:'',innerHTML:'',querySelector:node});return nodes.get(selector);};
  const scope=vm.createContext({document:{querySelectorAll:()=>[],getElementById:node},root:{insertAdjacentHTML(_where,html){scope.markup=html;}},route:'admin',routeVersion:1,session:{role:'admin',loginId:'ADMIN'},ownsBrowserLogin:()=>true,browserLoginOwner:()=>null,browserLogoutOwner:()=>null,esc:context.ui.escape,
    api:async(path,body,options)=>{requests.push({path,body,options});return {teams:[{...team,recycled:true,phone:'SYNTHETIC-PHONE'}]};}});
  vm.runInContext(source+'\ninstallRegistrationAdmin(1)',scope);await node('[data-registration-load]').onclick();
  assert.equal(requests.length,1);assert.equal(requests[0].path,'admin/registration');assert.equal(requests[0].options.loginId,'ADMIN');assert.match(node('[data-registration-content]').innerHTML,/SYNTHETIC-PHONE/);assert.match(node('[data-registration-content]').innerHTML,/回收站/);assert(!scope.markup.includes('管理码'));assert(!node('[data-registration-content]').innerHTML.includes('data-registration-rotate'));
});

const platformSource=fs.readFileSync(new URL('../platform.js',import.meta.url),'utf8').split("(async()=>{try{session=await api('session');}")[0];
const ready=value=>({ok:true,status:200,json:async()=>value});
const pendingRead=()=>{let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve};};
function registrationAdminFixture(transport=async()=>ready({teams:[{...team,phone:'SYNTHETIC-PRIVATE-PHONE'}]})){
  const events={},requests=[],nodes=new Map();let owner='ADMIN',logout=null,box,content,message,button;
  const element=()=>({isConnected:true,innerHTML:'',textContent:'',disabled:false,classList:{add(){},remove(){},toggle(){}},addEventListener(){}});
  const root={...element(),insertAdjacentHTML(){}};nodes.set('platform-main',root);nodes.set('account',element());nodes.set('platform-toast',element());
  function replacePanel(){if(box)box.isConnected=false;box=element();content=element();message=element();button=element();box.querySelector=selector=>selector==='[data-registration-content]'?content:selector==='[data-registration-message]'?message:button;nodes.set('registration-admin',box);}
  replacePanel();
  const scope=vm.createContext({document:{querySelectorAll:()=>[],getElementById:id=>nodes.get(id),body:{classList:{add(){},remove(){}}}},location:{href:'https://example.test/gotc/platform.html#admin',hash:'#admin'},URL,URLSearchParams,Intl,Date,TextEncoder,setTimeout:()=>0,clearTimeout,
    localStorage:{getItem:key=>key.endsWith(':logout')?logout:owner},window:{addEventListener:(name,handler)=>events[name]=handler},
    fetch:async(url,options)=>{requests.push({path:url.pathname,headers:options.headers});return transport(url,options);}});
  vm.runInContext(source+'\n'+platformSource+"\nroute='admin';routeVersion=1;session={role:'admin',loginId:'ADMIN'};installRegistrationAdmin(1);",scope);
  return {scope,events,requests,load:()=>button.onclick(),content:()=>content,message:()=>message,button:()=>button,root,
    setLogout:value=>logout=value,setOwner:value=>owner=value,state:expression=>vm.runInContext(expression,scope),replacePanel};
}
test('administrator registration delayed GET cannot repaint phone after a logout epoch changes without an event',async()=>{
  const pending=pendingRead(),f=registrationAdminFixture(()=>pending.promise),reading=f.load();f.setLogout('ADMIN');pending.resolve(ready({teams:[{...team,phone:'LATE-PRIVATE-PHONE'}]}));await reading;
  assert(!f.content().innerHTML.includes('LATE-PRIVATE-PHONE'));assert.match(f.message().textContent,/已隐藏/);assert.equal(f.button().disabled,true);await f.load();assert.equal(f.requests.length,1);
});
for(const event of ['storage','focus','pageshow'])test(`visible administrator registration private details are cleared on ${event} after logout`,async()=>{
  const f=registrationAdminFixture();await f.load();assert.match(f.content().innerHTML,/SYNTHETIC-PRIVATE-PHONE/);f.setLogout('ADMIN');f.events[event]({key:f.state('teamLogoutKey'),newValue:'ADMIN',persisted:true});
  assert.equal(f.content().innerHTML,'');assert.match(f.message().textContent,/已隐藏/);assert.equal(f.button().disabled,true);assert.equal(f.state('currentRegistrationAdmin.active'),false);
});
test('clearing already visible administrator details also invalidates a pending reload',async()=>{
  const pending=pendingRead();let calls=0;const f=registrationAdminFixture(async()=>++calls===1?ready({teams:[{...team,phone:'SHOWN-PRIVATE-PHONE'}]}):pending.promise);await f.load();const reload=f.load();f.setLogout('ADMIN');f.events.storage({key:f.state('teamLogoutKey'),newValue:'ADMIN'});assert.equal(f.content().innerHTML,'');pending.resolve(ready({teams:[{...team,phone:'LATE-PRIVATE-PHONE'}]}));await reload;assert.equal(f.content().innerHTML,'');
});

test('the registration panel rejects a request after logout before it reaches the API',async()=>{
  const f=registrationAdminFixture();f.setLogout('ADMIN');await f.load();assert.equal(f.requests.length,0);assert.equal(f.content().innerHTML,'');assert.equal(f.button().disabled,true);
});
for(const status of [401,403])test(`registration HTTP ${status} clears previously displayed private fields`,async()=>{
  let calls=0;const f=registrationAdminFixture(async()=>++calls===1?ready({teams:[{...team,phone:'SHOWN-PRIVATE-PHONE'}]}):{ok:false,status,json:async()=>({error:'Synthetic authorization loss'})});await f.load();await f.load();assert.equal(f.content().innerHTML,'');assert.match(f.message().textContent,/已隐藏/);assert.equal(f.state('currentRegistrationAdmin.active'),false);
});
test('an old registration 401 cannot clear a replacement administrator panel or session',async()=>{
  const pending=pendingRead();let calls=0;const f=registrationAdminFixture(async()=>++calls===1?pending.promise:ready({teams:[{...team,phone:'NEW-PRIVATE-PHONE'}]}));const old=f.load();f.replacePanel();f.setOwner('ADMIN2');f.state("session={role:'admin',loginId:'ADMIN2'};installRegistrationAdmin(1)");await f.load();pending.resolve({ok:false,status:401,json:async()=>({error:'Old session expired'})});await old;assert.match(f.content().innerHTML,/NEW-PRIVATE-PHONE/);assert.equal(f.state('session.loginId'),'ADMIN2');assert.equal(f.state('currentRegistrationAdmin.active'),true);
});
test('route disposal clears administrator registration data and disables its old handler',async()=>{
  const f=registrationAdminFixture();await f.load();const load=f.load;f.scope.location.hash='#board';f.state('renderBoard=async()=>{}');await f.state('renderRoute()');assert.equal(f.content().innerHTML,'');assert.equal(f.state('currentRegistrationAdmin'),null);await load();assert.equal(f.requests.length,1);
});
