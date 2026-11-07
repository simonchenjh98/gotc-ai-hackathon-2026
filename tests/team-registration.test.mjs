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
function legacyNode(selector){
  const listeners={},node={value:'',checked:false,textContent:'',innerHTML:'',disabled:false,hidden:false,isConnected:true,addEventListener(name,fn){(listeners[name]??=[]).push(fn);},input(){for(const fn of listeners.input || [])fn();}};
  if(selector==='[data-reg-code-form]' || selector==='[data-reg-form]'){
    const fields=selector==='[data-reg-code-form]'?['code']:['name','captain','school','phone','project','summary','targetSize','member1','member2','member3','member4'];
    node.elements=Object.fromEntries(fields.map(key=>[key,legacyNode('input')]));node.reset=()=>{for(const field of Object.values(node.elements))field.value='';};
  }
  return node;
}
function publicFixture(transport){
  const nodes=new Map(),requests=[],removed=[];
  const host={isConnected:true,dataset:{api:'api/registration'},innerHTML:'',querySelector(selector){if(!nodes.has(selector))nodes.set(selector,legacyNode(selector));return nodes.get(selector);}};
  const scope=vm.createContext({document:{querySelectorAll:()=>[]},URL,location:{href:'https://example.test/gotc/teams.html'},sessionStorage:{removeItem:key=>removed.push(key),getItem:()=>'',setItem(){throw new Error('No absent credential should be persisted');}},fetch:async(url,options)=>{requests.push({url,options});return transport?.(url,options) || {ok:true,json:async()=>({teams:[team]})};}});
  scope.host=host;vm.runInContext(source,scope);
  return {scope,host,requests,removed,node:selector=>host.querySelector(selector),install:()=>vm.runInContext('RegistrationUI.install(host)',scope)};
}
test('public directory uses GET without credentials and offers existing-code editing without creation',async()=>{
  const f=publicFixture();await f.install();
  assert.equal(f.requests.length,1);assert.equal(f.requests[0].options.method,'GET');assert.equal(f.requests[0].options.credentials,'omit');
  assert.deepEqual(f.removed,[]);
  assert.match(f.host.innerHTML,/本队资料与成员/);assert.match(f.host.innerHTML,/platform.html#claim/);assert.match(f.host.innerHTML,/使用原队伍管理码修改资料（兼容）/);assert(!f.host.innerHTML.includes('data-reg-new'));
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
    const html=fs.readFileSync(new URL('../'+path,import.meta.url),'utf8');assert.match(html,/platform.html#team-profile/);assert.match(html,/platform.html#claim/);assert.match(html,/原提交码/);assert.match(html,/未作废的队伍管理码/);
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

const oldCode='a'.repeat(43),otherCode='b'.repeat(43),storageKey='gotc-registration-code:v1:https://example.test/gotc/api/registration';
const legacyTeam=changes=>({...team,name:'Synthetic Legacy',phone:'13900000001',revision:4,status:'active',...changes});
function legacyFixture(transport=async()=>ready({team:legacyTeam()}),stored=''){
  const nodes=new Map(),requests=[],writes=[],removals=[],events={},values=new Map(stored?[[storageKey,stored]]:[]);let allow=true;
  const host={isConnected:true,querySelector(selector){if(!nodes.has(selector))nodes.set(selector,legacyNode(selector));return nodes.get(selector);}};
  const scope=vm.createContext({document:{querySelectorAll:()=>[]},location:{href:'https://example.test/gotc/teams.html'},confirm:()=>allow,
    window:{addEventListener:(name,fn)=>events[name]=fn,removeEventListener:(name,fn)=>{if(events[name]===fn)delete events[name];}},
    sessionStorage:{getItem:key=>values.get(key)||'',setItem:(key,value)=>{writes.push({key,value});values.set(key,value);},removeItem:key=>{removals.push(key);values.delete(key);}},
    fetch:async(url,options)=>{const request={url,method:options.method,credentials:options.credentials,headers:options.headers,body:JSON.parse(options.body)};requests.push(request);return transport(request);}});
  scope.host=host;vm.runInContext(source+"\nvar legacy=installLegacyRegistration(host,'https://example.test/gotc/api/registration')",scope);
  const node=selector=>host.querySelector(selector);
  return {scope,host,requests,writes,removals,values,events,node,form:node('[data-reg-form]'),ready:vm.runInContext('legacy.ready',scope),choose:value=>allow=value,
    setCode(value){node('[data-reg-code-form]').elements.code.value=value;node('[data-reg-code-form]').elements.code.input();},
    open:()=>node('[data-reg-code-form]').onsubmit({preventDefault(){}}),save:()=>node('[data-reg-form]').onsubmit({preventDefault(){}}),
    input(key,value){node('[data-reg-form]').elements[key].value=value;node('[data-reg-form]').input();},state:expression=>vm.runInContext(expression,scope)};
}
test('stored existing management code is restored only through an authorized open and never removed',async()=>{
  const pending=pendingRead(),f=legacyFixture(()=>pending.promise,oldCode);assert.equal(f.requests.length,1);assert.equal(f.node('[data-reg-editor]').hidden,true);assert.equal(f.form.elements.phone.value,'');assert.equal(f.values.get(storageKey),oldCode);assert.equal(f.writes.length,0);
  pending.resolve(ready({team:legacyTeam()}));await f.ready;assert.equal(f.node('[data-reg-editor]').hidden,false);assert.equal(f.form.elements.phone.value,'13900000001');assert.deepEqual(f.requests[0].body,{code:oldCode});assert.equal(f.requests[0].credentials,'omit');assert.deepEqual(f.writes,[{key:storageKey,value:oldCode}]);assert.deepEqual(f.removals,[]);
});
test('legacy save sends only existing registration fields and code/revision with no login or session promotion',async()=>{
  const f=legacyFixture(async request=>ready({team:legacyTeam({revision:request.url.endsWith('/save')?5:4}),ok:true}));f.setCode(oldCode);await f.open();f.input('summary','Changed using existing management code');await f.save();
  const post=f.requests[1];assert.equal(post.url,'https://example.test/gotc/api/registration/save');assert.equal(post.credentials,'omit');assert.deepEqual(post.body,{code:oldCode,expectedRevision:4,team:{name:'Synthetic Legacy',captain:'Alex',school:'Fixture School',phone:'13900000001',project:'<script>',summary:'Changed using existing management code',targetSize:3,members:['Alex','Student A']}});assert.deepEqual(Object.keys(post.headers),['Content-Type']);assert(!f.requests.some(request=>request.url.endsWith('/create') || request.url.includes('/login')));assert.match(f.node('[data-reg-status]').textContent,/参赛登录.*权限保持不变/);
});
test('no management code is generated, created or remembered before a successful existing-code authorization',async()=>{
  const f=legacyFixture(async()=>({ok:false,status:403,json:async()=>({error:'Wrong old code'})}));await f.ready;assert.equal(f.requests.length,0);f.setCode('not-a-valid-code');await f.open();assert.equal(f.requests.length,0);f.setCode(oldCode);await f.open();assert.equal(f.writes.length,0);assert.equal(f.node('[data-reg-editor]').hidden,true);assert.equal(f.form.elements.phone.value,'');assert.deepEqual(f.removals,[]);assert(!source.includes('freshCode'));assert(!source.includes('clearLegacyRegistrationStorage'));
});
test('existing stale stored code is not silently deleted after an authorization failure',async()=>{
  const f=legacyFixture(async()=>({ok:false,status:403,json:async()=>({error:'Old code invalid'})}),oldCode);await f.ready;assert.equal(f.values.get(storageKey),oldCode);assert.deepEqual(f.removals,[]);assert.equal(f.writes.length,0);assert.equal(f.node('[data-reg-editor]').hidden,true);
});
test('out-of-order opens cannot display or remember another selected management code',async()=>{
  const old=pendingRead(),current=pendingRead(),f=legacyFixture(request=>request.body.code===oldCode?old.promise:current.promise);f.setCode(oldCode);const first=f.open();f.setCode(otherCode);const second=f.open();current.resolve(ready({team:legacyTeam({id:'OTHER',name:'Current chosen team',phone:'13900000002'})}));await second;old.resolve(ready({team:legacyTeam({name:'OLD RESPONSE',phone:'13900000009'})}));await first;
  assert.equal(f.form.elements.name.value,'Current chosen team');assert.equal(f.form.elements.phone.value,'13900000002');assert.equal(f.values.get(storageKey),otherCode);assert.equal(f.writes.length,1);
});
test('changing code while save is pending cannot restore the old team or replace remembered new code',async()=>{
  const pending=pendingRead(),f=legacyFixture(request=>request.url.endsWith('/save')?pending.promise:Promise.resolve(ready({team:legacyTeam(request.body.code===otherCode?{id:'OTHER',name:'Other team'}:{})})));f.setCode(oldCode);await f.open();f.input('summary','SENT');const saving=f.save();f.setCode(otherCode);await f.open();pending.resolve(ready({ok:true,team:legacyTeam({name:'LATE SAVED NAME',revision:5})}));await saving;assert.equal(f.form.elements.name.value,'Other team');assert.equal(f.values.get(storageKey),otherCode);
});
test('legacy revision conflict retains input, blocks repeated save and requires an explicit confirmed open',async()=>{
  const f=legacyFixture(request=>request.url.endsWith('/save')?Promise.resolve({ok:false,status:409,json:async()=>({error:'Other entry changed profile'})}):Promise.resolve(ready({team:legacyTeam({revision:6})})));f.setCode(oldCode);await f.open();f.input('summary','UNSAVED LEGACY INPUT');await f.save();assert.equal(f.form.elements.summary.value,'UNSAVED LEGACY INPUT');assert.equal(f.node('[data-reg-save]').disabled,true);assert.match(f.node('[data-reg-error]').textContent,/显式|明确重新读取/);await f.save();assert.equal(f.requests.filter(request=>request.url.endsWith('/save')).length,1);f.choose(false);await f.open();assert.equal(f.requests.filter(request=>request.url.endsWith('/open')).length,1);f.choose(true);await f.open();assert.equal(f.form.elements.summary.value,'"unsafe"');assert.equal(f.node('[data-reg-save]').disabled,false);
});
test('typing after a sent legacy save survives and next save adopts only returned registration revision',async()=>{
  const pending=pendingRead();let saves=0;const f=legacyFixture(request=>request.url.endsWith('/save') && ++saves===1?pending.promise:Promise.resolve(ready({team:legacyTeam({revision:8})})));f.setCode(oldCode);await f.open();f.input('summary','SENT');const saving=f.save();f.input('summary','LATER INPUT');pending.resolve(ready({team:legacyTeam({revision:9})}));await saving;assert.equal(f.form.elements.summary.value,'LATER INPUT');assert.match(f.node('[data-reg-status]').textContent,/新输入已保留/);await f.save();assert.equal(f.requests.at(-1).body.expectedRevision,9);assert.equal(f.requests.at(-1).body.team.summary,'LATER INPUT');
});
test('legacy double click submits one immutable save',async()=>{
  const pending=pendingRead(),f=legacyFixture(request=>request.url.endsWith('/save')?pending.promise:Promise.resolve(ready({team:legacyTeam()})));f.setCode(oldCode);await f.open();const first=f.save(),second=f.save();assert.equal(f.requests.filter(request=>request.url.endsWith('/save')).length,1);pending.resolve(ready({team:legacyTeam({revision:5})}));await Promise.all([first,second]);
});
for(const event of ['hashchange','pagehide'])test(`a legacy open is suppressed after ${event} and does not write the stored code`,async()=>{
  const pending=pendingRead(),f=legacyFixture(()=>pending.promise);f.setCode(oldCode);const reading=f.open();f.events[event]({});pending.resolve(ready({team:legacyTeam()}));await reading;assert.equal(f.writes.length,0);assert.equal(f.node('[data-reg-editor]').hidden,true);
});
test('legacy dirty editing participates in native unload and code-switch cancellation issues no request',async()=>{
  const f=legacyFixture();f.setCode(oldCode);await f.open();f.input('summary','Unsaved');let prevented=false;const event={preventDefault(){prevented=true;}};f.events.beforeunload(event);assert.equal(prevented,true);assert.equal(event.returnValue,'');f.setCode(otherCode);f.choose(false);await f.open();assert.equal(f.requests.length,1);assert.equal(f.values.get(storageKey),oldCode);
});
test('typing during a legacy reread is retained and requires a later explicit reread',async()=>{
  const pending=pendingRead();let reads=0;const f=legacyFixture(async()=>++reads===1?ready({team:legacyTeam()}):pending.promise);f.setCode(oldCode);await f.open();const reading=f.open();f.input('summary','TYPED DURING READ');pending.resolve(ready({team:legacyTeam({revision:9})}));await reading;assert.equal(f.form.elements.summary.value,'TYPED DURING READ');assert.equal(f.node('[data-reg-save]').disabled,true);
});
test('an authorization failure clears private legacy fields but keeps the user existing cached credential',async()=>{
  let calls=0;const f=legacyFixture(async()=>++calls===1?ready({team:legacyTeam()}):{ok:false,status:403,json:async()=>({error:'Revoked code'})});f.setCode(oldCode);await f.open();f.input('summary','Unsaved');await f.save();assert.equal(f.form.elements.phone.value,'');assert.equal(f.node('[data-reg-editor]').hidden,true);assert.equal(f.values.get(storageKey),oldCode);assert.deepEqual(f.removals,[]);
});
