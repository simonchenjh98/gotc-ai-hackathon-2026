import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const platform=fs.readFileSync(new URL('../platform.js',import.meta.url),'utf8');
const source=fs.readFileSync(new URL('../platform-admin.js',import.meta.url),'utf8')+'\n'+platform.split("window.addEventListener('hashchange'")[0];
const deferred=()=>{let resolve;const promise=new Promise(r=>{resolve=r;});return {promise,resolve};};
const ok=body=>({ok:true,json:async()=>body});
const fail=(error,code='team_restore_conflict')=>({ok:false,status:409,json:async()=>({error,code})});
const detail=(id='T1',overrides={})=>({team:{id,number:id==='T1'?1:2,name:'Stored '+id,revision:7,status:'active'},recycle:null,recycleContext:'a'.repeat(64),roster:{source:'approved_memberships',members:[{personId:'P1',name:'Member '+id,school:'School '+id,schoolState:'recorded',source:'approved_membership',role:'captain'}]},restoreConflicts:[],restoreAdvisories:[],...overrides});
const archived=(id='T1',overrides={})=>detail(id,{recycle:{eventId:1,deletedAt:1,reason:'Synthetic delete',originalStatus:'active'},recycleContext:null,...overrides});
function element(){
  const listeners={};let html='',text='';let buttons=[];
  return {isConnected:true,hidden:false,disabled:false,value:'',classList:{add(){},remove(){}},
    get textContent(){return text;},set textContent(value){text=value;html='';buttons=[];},
    addEventListener(name,fn){(listeners[name]??=[]).push(fn);},
    input(){for(const fn of listeners.input || [])fn();},
    get innerHTML(){return html;},set innerHTML(value){html=value;buttons=[...value.matchAll(/data-restore-team="([^"]*)"/g)].map(match=>({...element(),dataset:{restoreTeam:match[1]}}));},
    querySelectorAll(){return buttons;},insertAdjacentHTML(){},scrollIntoView(){}};
}
function form(values={}){
  const node=element();node.elements=Object.fromEntries(Object.entries(values).map(([key,value])=>[key,Object.assign(element(),{value})]));node.button=element();node.error=element();node.querySelector=selector=>selector==='.form-error'?node.error:node.button;return node;
}
function harness(fetcher=async()=>ok(detail())){
  const ids=['member-detail','detail-refresh','recycle-open','recycle-toggle','recycle-confirm','recycle-target','recycle-reason','recycle-submit','recycle-cancel','lifecycle-error','recycle-bin','recycle-refresh','recycle-list','restore-detail','restore-heading','restore-members','restore-conflicts','restore-advisories','restore-result'];
  const elements=Object.fromEntries(ids.map(id=>['team-'+id,element()]));
  for(const id of ['recycle-bin','recycle-confirm','restore-detail','restore-result'])elements['team-'+id].hidden=true;
  elements['team-form']=form({id:'T1',expectedRevision:'7',name:'Editor T1'});
  elements['team-restore-form']=form({name:'',reason:''});
  elements['platform-main']=element();elements['platform-toast']=element();
  const requests=[],storageWrites=[],clears=[];let marker='LOGIN1',logoutMarker=null;
  const context=vm.createContext({document:{getElementById:id=>elements[id]},location:{href:'https://example.test/platform.html#admin'},URL,URLSearchParams,Intl,Date,TextEncoder,
    localStorage:{getItem:key=>key.endsWith(':logout')?logoutMarker:marker,setItem:(...args)=>storageWrites.push(args)},setTimeout:()=>0,clearTimeout,confirm:()=>true,
    fetch:async(url,options)=>{requests.push({url,method:options.method,headers:options.headers,body:options.body?JSON.parse(options.body):undefined});return fetcher(url,options);}});
  vm.runInContext(source+"\nroute='admin';routeVersion=1;session={role:'admin',loginId:'LOGIN1'};",context);
  context.config={form:elements['team-form'],alive:()=>vm.runInContext("route==='admin' && routeVersion===1",context),refreshDirectory:async()=>{context.refreshes++;},count:2,clearEditor:()=>{clears.push(true);elements['team-form'].elements.id.value='';vm.runInContext('controller.clear()',context);}};
  context.refreshes=0;
  vm.runInContext('controller=installAdminTeamLifecycle(config);trackAdminForm(config.form);',context);
  return {context,elements,requests,clears,storageWrites,node:id=>elements['team-'+id],entry:elements['team-form'],restore:elements['team-restore-form'],
    select:()=>vm.runInContext('controller.select()',context),
    switch(id){elements['team-form'].elements.id.value=id;elements['team-form'].editSession=(elements['team-form'].editSession || 0)+1;return this.select();},
    marker(value){marker=value;},logoutMarker(value){logoutMarker=value;},
    async openRestore(id='T1'){await this.node('recycle-toggle').onclick();await this.node('recycle-list').querySelectorAll().find(b=>b.dataset.restoreTeam===id).onclick();},
    restoreInput(key,value){elements['team-restore-form'].elements[key].value=value;elements['team-restore-form'].elements[key].input();elements['team-restore-form'].input();},
    submitRestore:()=>elements['team-restore-form'].onsubmit({preventDefault(){}})};
}
const listing=(ids=['T1'])=>({teams:ids.map(id=>({id,number:1,name:id,revision:7,recycle:{deletedAt:1,reason:'<script>reason</script>'}}))});

test('private roster escapes XSS, preserves recorded sources and never infers unknown schools',async()=>{
  const h=harness(async()=>ok(detail('T1',{roster:{source:'registration',members:[{name:'<img src=x onerror=alert(1)>',school:'<script>school</script>',role:'captain',source:'registration_captain',schoolState:'needs_review'},{name:'Same name',school:null,source:'registration_member',schoolState:'missing'}]}})));
  await h.select();const markup=h.node('member-detail').innerHTML;
  assert.match(markup,/&lt;img/);assert.match(markup,/&lt;script&gt;school/);assert(!markup.includes('<img'));
  assert.match(markup,/队长原登记/);assert.match(markup,/学校待核对/);assert.match(markup,/学校待补充（原登记未提供）/);
  assert.equal(h.storageWrites.length,0);assert.equal(h.requests[0].url.search,'?teamId=T1');assert.equal(h.requests[0].headers['X-GOTC-Login'],'LOGIN1');
  assert(!h.requests[0].url.href.includes('Same'));
 });
test('empty and loading states remove any previously selected private roster',async()=>{
  const pending=deferred();const h=harness(async url=>url.searchParams.get('teamId')==='T2'?pending.promise:ok(detail()));
  await h.select();const changing=h.switch('T2');assert(!h.node('member-detail').innerHTML.includes('Member T1'));assert.equal(h.node('recycle-open').disabled,true);
  pending.resolve(ok(detail('T2',{roster:{source:'none',members:[]}})));await changing;assert.match(h.node('member-detail').innerHTML,/暂无成员资料/);
 });
test('delayed team detail never replaces another team, refreshed detail or a new login',async()=>{
  const old=deferred(),fresh=deferred();let reads=0;
  const h=harness(async()=>++reads===1?old.promise:fresh.promise);
  const a=h.select(),b=h.switch('T2');fresh.resolve(ok(detail('T2')));await b;const current=h.node('member-detail').innerHTML;
  old.resolve(ok(detail()));await a;assert.equal(h.node('member-detail').innerHTML,current);
  const delayed=deferred();const h2=harness(()=>delayed.promise);const loading=h2.select();vm.runInContext("session={role:'admin',loginId:'LOGIN2'}",h2.context);delayed.resolve(ok(detail()));await loading;assert(!h2.node('member-detail').innerHTML.includes('Member T1'));
 });
 for(const change of ['route','marker','revision'])test(`a detail read ignores changed ${change}`,async()=>{
  const pending=deferred(),h=harness(()=>pending.promise),loading=h.select();
  if(change==='route')vm.runInContext('routeVersion=2',h.context);if(change==='marker')h.marker('LOGIN2');if(change==='revision')h.entry.elements.expectedRevision.value='8';
  pending.resolve(ok(detail()));await loading;assert(!h.node('member-detail').innerHTML.includes('Member T1'));assert.equal(h.node('recycle-open').disabled,true);
 });
test('refresh disables deletion immediately and cancellation causes zero writes',async()=>{
  const pending=deferred();let reads=0;const h=harness(async()=>++reads===1?ok(detail()):pending.promise);
  await h.select();h.node('recycle-open').onclick();h.node('recycle-reason').value='Reason';h.node('recycle-cancel').onclick();await h.node('recycle-submit').onclick();assert.equal(h.requests.filter(x=>x.method==='POST').length,0);
  const refresh=h.node('detail-refresh').onclick();assert.equal(h.node('recycle-open').disabled,true);assert.equal(h.node('recycle-confirm').hidden,true);pending.resolve(ok(detail()));await refresh;
 });
test('deletion captures context and revision, sends one request, and cannot clear a later team',async()=>{
  const pending=deferred();const h=harness(async(_url,options)=>options.method==='POST'?pending.promise:ok(detail(_url.searchParams.get('teamId') || 'T1')));
  await h.select();h.node('recycle-open').onclick();assert.match(h.node('recycle-target').textContent,/Stored T1.*版本 7/);h.node('recycle-reason').value='Synthetic reason';
  const a=h.node('recycle-submit').onclick(),b=h.node('recycle-submit').onclick();assert.equal(h.requests.filter(x=>x.method==='POST').length,1);
  const payload=h.requests.find(x=>x.method==='POST').body;assert.deepEqual(payload,{teamId:'T1',expectedRevision:7,recycleContext:'a'.repeat(64),reason:'Synthetic reason'});
  await h.switch('T2');pending.resolve(ok({ok:true,team:{...detail().team,revision:8},recycle:archived().recycle}));await Promise.all([a,b]);assert.equal(h.entry.elements.id.value,'T2');assert.equal(h.clears.length,0);assert.match(h.node('member-detail').innerHTML,/Member T2/);
 });
test('context conflict retains reason but requires fresh roster and explicit reconfirmation',async()=>{
  let reads=0;const h=harness(async(_url,options)=>options.method==='POST'?fail('Roster changed','team_context_conflict'):ok(detail('T1',{recycleContext:(++reads===1?'a':'b').repeat(64)})));
  await h.select();h.node('recycle-open').onclick();h.node('recycle-reason').value='Keep reason';await h.node('recycle-submit').onclick();
  assert.equal(h.node('recycle-reason').value,'Keep reason');assert.equal(h.node('recycle-confirm').hidden,true);assert.match(h.node('lifecycle-error').textContent,/Roster changed.*重新打开/);
  await h.node('recycle-submit').onclick();assert.equal(h.requests.filter(x=>x.method==='POST').length,1);h.node('recycle-open').onclick();await h.node('recycle-submit').onclick();assert.equal(h.requests.filter(x=>x.method==='POST')[1].body.recycleContext,'b'.repeat(64));
 });
test('a delete result preserves input added during the POST and disables ordinary save',async()=>{
  const pending=deferred();const h=harness(async(_url,options)=>options.method==='POST'?pending.promise:ok(detail()));
  await h.select();h.node('recycle-open').onclick();h.node('recycle-reason').value='Reason';const saving=h.node('recycle-submit').onclick();h.entry.elements.name.value='NEW INPUT';h.entry.input();
  pending.resolve(ok({team:{...detail().team,revision:8},recycle:archived().recycle}));await saving;assert.equal(h.entry.elements.name.value,'NEW INPUT');assert.equal(h.clears.length,0);assert.equal(vm.runInContext('controller.canSave()',h.context),false);assert.match(h.node('lifecycle-error').textContent,/新输入已保留/);
 });
test('recycle list escapes names and reasons and unknown-school advisory alone allows restore',async()=>{
  const h=harness(async url=>ok(url.pathname.endsWith('/recycle-bin')?{teams:[{...listing().teams[0],name:'<img src=x>'}]}:archived('T1',{restoreAdvisories:[{message:'存在同名记录，学校未知，不能确定是否为同一人'}]})));
  await h.openRestore();assert.match(h.node('recycle-list').innerHTML,/&lt;img/);assert(!h.node('recycle-list').innerHTML.includes('<script>'));assert.match(h.node('restore-members').innerHTML,/删除时记录/);assert.match(h.node('restore-advisories').innerHTML,/不阻止恢复/);assert.equal(h.restore.button.disabled,false);
 });
test('restore replacement name preserves exact POST error and inputs while hiding old-name GET conflicts',async()=>{
  const h=harness(async(url,options)=>options.method==='POST'?fail('Replacement B is occupied'):ok(url.pathname.endsWith('/recycle-bin')?listing():archived('T1',{restoreConflicts:[{type:'name',message:'Stored T1 occupied'}]})));
  await h.openRestore();assert.equal(h.restore.button.disabled,true);h.restoreInput('name','Replacement B');h.restoreInput('reason','Keep restoration reason');assert.equal(h.restore.button.disabled,false);assert(!h.node('restore-conflicts').innerHTML.includes('Stored T1 occupied'));
  await h.submitRestore();assert.match(h.restore.error.textContent,/Replacement B is occupied/);assert.equal(h.restore.elements.name.value,'Replacement B');assert.equal(h.restore.elements.reason.value,'Keep restoration reason');assert(!h.node('restore-conflicts').innerHTML.includes('Stored T1 occupied'));
  h.restoreInput('name','Free C');assert.equal(h.restore.error.textContent,'');assert.equal(h.restore.button.disabled,false);
 });
test('member conflicts block restore and provide no overwrite or identity edit action',async()=>{
  const h=harness(async url=>ok(url.pathname.endsWith('/recycle-bin')?listing():archived('T1',{restoreConflicts:[{type:'member_assigned',message:'Person owned by Other team <img>'}]})));
  await h.openRestore();h.restoreInput('name','New name');assert.equal(h.restore.button.disabled,true);assert.match(h.node('restore-conflicts').innerHTML,/成员占用或身份冲突/);assert.match(h.node('restore-conflicts').innerHTML,/&lt;img&gt;/);assert(!h.node('restore-conflicts').innerHTML.includes('<button'));
 });
test('initial and refreshed restore reads preserve inputs typed during loading',async()=>{
  const pending=deferred();const h=harness(async url=>url.pathname.endsWith('/recycle-bin')?ok(listing()):pending.promise);
  await h.node('recycle-toggle').onclick();const loading=h.node('recycle-list').querySelectorAll()[0].onclick();h.restoreInput('name','NEW NAME DURING GET');h.restoreInput('reason','NEW REASON DURING GET');pending.resolve(ok(archived()));await loading;
  assert.equal(h.restore.elements.name.value,'NEW NAME DURING GET');assert.equal(h.restore.elements.reason.value,'NEW REASON DURING GET');
 });
test('restore success preserves later input, reports original eligibility and never promises revived credentials',async()=>{
  const pending=deferred();const h=harness(async(url,options)=>options.method==='POST'?pending.promise:ok(url.pathname.endsWith('/recycle-bin')?listing():archived()));
  await h.openRestore();h.restoreInput('reason','Restore reason');const saving=h.submitRestore();h.restoreInput('name','NEW UNSAVED NAME');pending.resolve(ok({team:{...detail().team,status:'disqualified'},recycle:null,access:{submissionAccess:'inactive',apiAccess:'inactive',registrationAccess:'reissue_required'}}));await saving;
  assert.equal(h.restore.elements.name.value,'NEW UNSAVED NAME');assert.equal(h.restore.button.disabled,true);assert.match(h.node('restore-result').textContent,/队伍已恢复.*重发提交码.*新 key.*重发管理码.*取消资格/);assert.equal(vm.runInContext('dirtyAdminForms.has(config.form)',h.context),false);assert.equal(vm.runInContext('dirtyAdminForms.has(document.getElementById("team-restore-form"))',h.context),true);
 });
test('a late restore result cannot alter a later selected recycled team',async()=>{
  const pending=deferred();const h=harness(async(url,options)=>options.method==='POST'?pending.promise:ok(url.pathname.endsWith('/recycle-bin')?listing(['T1','T2']):archived(url.searchParams.get('teamId'))));
  await h.openRestore();h.restoreInput('reason','T1 reason');const saving=h.submitRestore();await h.node('recycle-list').querySelectorAll().find(b=>b.dataset.restoreTeam==='T2').onclick();const heading=h.node('restore-heading').textContent;pending.resolve(ok({team:detail().team,access:{}}));await saving;
  assert.equal(h.node('restore-heading').textContent,heading);assert.equal(h.node('restore-result').hidden,true);assert.equal(h.restore.elements.name.value,'Stored T2');assert.equal(h.restore.button.disabled,false);
 });
test('all four lifecycle paths carry the login marker and reject a changed browser owner before fetch',async()=>{
  const h=harness(async()=>ok({}));
  for(const path of ['admin/data/team?teamId=T1','admin/data/recycle-bin','admin/data/team/recycle','admin/data/team/restore'])await vm.runInContext(`api(${JSON.stringify(path)},${path.endsWith('recycle') || path.endsWith('restore')?'{}':'undefined'})`,h.context);
  assert.equal(h.requests.length,4);for(const request of h.requests)assert.equal(request.headers['X-GOTC-Login'],'LOGIN1');
  h.marker('LOGIN2');await assert.rejects(vm.runInContext("api('admin/data/team?teamId=T1')",h.context),/登录身份已变化/);assert.equal(h.requests.length,4);
 });

test('a newer refresh wins over an older read for the same team',async()=>{
  const old=deferred(),fresh=deferred();let reads=0;
  const h=harness(()=>++reads===1?old.promise:fresh.promise),first=h.select(),second=h.select();
  fresh.resolve(ok(detail('T1',{roster:{members:[{name:'FRESH MEMBER',school:'New school'}]}})));await second;
  old.resolve(ok(detail('T1',{roster:{members:[{name:'OLD MEMBER',school:'Old school'}]}})));await first;
  assert.match(h.node('member-detail').innerHTML,/FRESH MEMBER/);assert(!h.node('member-detail').innerHTML.includes('OLD MEMBER'));
});

test('a failed detail refresh hides the prior roster and prevents stale confirmation',async()=>{
  let reads=0;const h=harness(async()=>++reads===1?ok(detail()):{ok:false,status:500,json:async()=>({error:'Synthetic read failure'})});
  await h.select();h.node('recycle-open').onclick();await h.node('detail-refresh').onclick();
  assert(!h.node('member-detail').innerHTML.includes('Member T1'));assert.match(h.node('member-detail').innerHTML,/读取失败/);assert.equal(h.node('recycle-open').disabled,true);assert.equal(h.node('recycle-confirm').hidden,true);
});

for(const change of ['route','login','browser owner'])test(`late delete result is suppressed after changing ${change}`,async()=>{
  const pending=deferred(),h=harness(async(_url,options)=>options.method==='POST'?pending.promise:ok(detail()));
  await h.select();h.node('recycle-open').onclick();h.node('recycle-reason').value='Reason';const saving=h.node('recycle-submit').onclick();
  if(change==='route')vm.runInContext('routeVersion=2',h.context);
  if(change==='login')vm.runInContext("session={role:'admin',loginId:'LOGIN2'}",h.context);
  if(change==='browser owner')h.marker('LOGIN2');
  pending.resolve(ok({team:detail().team,recycle:archived().recycle}));await saving;assert.equal(h.clears.length,0);assert.equal(h.context.refreshes,0);
});

test('restore double click sends one mutation and a changed login suppresses its response',async()=>{
  const pending=deferred(),h=harness(async(url,options)=>options.method==='POST'?pending.promise:ok(url.pathname.endsWith('/recycle-bin')?listing():archived()));
  await h.openRestore();h.restoreInput('reason','Reason');const first=h.submitRestore(),second=h.submitRestore();
  assert.equal(h.requests.filter(x=>x.method==='POST').length,1);assert.deepEqual(h.requests.find(x=>x.method==='POST').body,{teamId:'T1',expectedRevision:7,name:'Stored T1',reason:'Reason'});
  vm.runInContext("session={role:'admin',loginId:'LOGIN2'}",h.context);pending.resolve(ok({team:detail().team,access:{}}));await Promise.all([first,second]);assert.equal(h.node('restore-result').hidden,true);assert.equal(h.context.refreshes,0);
});


function identityHandlers(h){
  const start=platform.indexOf("window.addEventListener('storage'");
  const end=platform.indexOf("window.addEventListener('pagehide'",start);
  const handlers={};
  h.context.window={addEventListener:(name,callback)=>handlers[name]=callback};
  vm.runInContext(platform.slice(start,end),h.context);
  return handlers;
}
for(const change of ['cross-tab login','cross-tab logout','focus','bfcache logout'])test(`already displayed current and archived rosters are erased after ${change}`,async()=>{
  const h=harness(async url=>ok(url.pathname.endsWith('/recycle-bin')?listing(['T2']):url.searchParams.get('teamId')==='T2'?archived('T2'):detail()));
  await h.select();h.node('recycle-open').onclick();h.node('recycle-reason').value='Current private reason';
  await h.openRestore('T2');h.restoreInput('reason','Archived private reason');
  assert.match(h.node('member-detail').innerHTML,/Member T1.*School T1/);
  assert.match(h.node('restore-members').innerHTML,/Member T2.*School T2/);
  const handlers=identityHandlers(h);
  if(change==='cross-tab login'){
    h.marker('LOGIN2');handlers.storage({key:vm.runInContext('loginOwnerKey',h.context),newValue:'LOGIN2'});
  }else if(change==='cross-tab logout')handlers.storage({key:vm.runInContext('teamLogoutKey',h.context),newValue:'LOGIN1'});
  else if(change==='focus'){h.marker('LOGIN2');handlers.focus();}
  else{h.logoutMarker('LOGIN1');handlers.pageshow({persisted:true});}
  assert.match(h.node('member-detail').textContent,/成员资料已隐藏/);
  assert.equal(h.node('member-detail').innerHTML,'');assert.equal(h.node('restore-members').innerHTML,'');
  assert.equal(h.node('recycle-list').innerHTML,'');assert.equal(h.node('recycle-target').textContent,'');
  assert.equal(h.node('recycle-reason').value,'');assert.equal(h.restore.elements.reason.value,'');
  assert.equal(h.node('recycle-bin').hidden,true);assert.equal(h.restore.button.disabled,true);
  assert.equal(h.entry.elements.name.value,'Editor T1');
  assert.equal(vm.runInContext('controller.canSave()',h.context),false);
  const requests=h.requests.length;
  await h.node('recycle-refresh').onclick();await h.submitRestore();
  assert.equal(h.requests.length,requests);
});

test('a lifecycle 401 clears displayed data and a pending old read cannot repopulate it',async()=>{
  let expired=false;
  const pending=deferred();
  const h=harness(async url=>{
    if(url.pathname.endsWith('/recycle-bin'))return expired?{ok:false,status:401,json:async()=>({error:'Expired'})}:ok(listing(['T2']));
    return url.searchParams.get('teamId')==='T2'?pending.promise:ok(detail());
  });
  await h.select();await h.node('recycle-toggle').onclick();
  const reading=h.node('recycle-list').querySelectorAll()[0].onclick();
  expired=true;
  await assert.rejects(vm.runInContext("api('admin/data/recycle-bin')",h.context),/Expired/);
  pending.resolve(ok(archived('T2')));await reading;
  assert.equal(vm.runInContext('session.role',h.context),null);
  assert.equal(h.node('member-detail').innerHTML,'');assert.equal(h.node('restore-members').innerHTML,'');
  assert.match(h.node('member-detail').textContent,/成员资料已隐藏/);
});

test('a lifecycle 403 locks the active roster even if the browser owner marker did not change',async()=>{
  const h=harness(async url=>url.pathname.endsWith('/recycle-bin')
    ?{ok:false,status:403,json:async()=>({error:'Forbidden'})}:ok(detail()));
  await h.select();await h.node('recycle-toggle').onclick();
  assert.equal(h.node('member-detail').innerHTML,'');
  assert.match(h.node('member-detail').textContent,/成员资料已隐藏/);
  assert.equal(vm.runInContext('controller.canSave()',h.context),false);
});

test('a stale 401 does not clear the private roster of a newly mounted admin login',async()=>{
  const pending=deferred();let reads=0;
  const h=harness(()=>++reads===2?pending.promise:ok(detail('T1',{roster:{members:[{name:reads===1?'Old member':'Current member',school:'Synthetic school'}]}})));
  await h.select();const refresh=h.select();
  h.marker('LOGIN2');
  vm.runInContext("session={role:'admin',loginId:'LOGIN2'};controller=installAdminTeamLifecycle(config);",h.context);
  await h.select();
  pending.resolve({ok:false,status:401,json:async()=>({error:'Expired old login'})});await refresh;
  assert.equal(vm.runInContext('session.loginId',h.context),'LOGIN2');
  assert.match(h.node('member-detail').innerHTML,/Current member/);
  assert.equal(vm.runInContext('controller.canSave()',h.context),true);
});

test('admin directory member cell lists captain and teammates with escaping',()=>{
  const admin=fs.readFileSync(new URL('../platform-admin.js',import.meta.url),'utf8');
  const cellSource=admin.slice(admin.indexOf('function adminTeamMembersCell('),admin.indexOf('function adminTeamRosterMarkup('));
  const escSource=platform.match(/const esc = [^\n]*/)[0];
  const context=vm.createContext({});vm.runInContext(escSource+'\n'+cellSource+'\nglobalThis.cell=adminTeamMembersCell;',context);
  const html=context.cell([{name:'张三',role:'captain',school:'一中'},{name:'<b>x</b>',role:'member',school:null}]);
  assert.match(html,/<strong>张三<\/strong><small>（队长）<\/small><br><small>一中<\/small>/);
  assert.match(html,/&lt;b&gt;x&lt;\/b&gt;/);
  assert.match(context.cell([]),/暂无成员资料/);assert.match(context.cell(undefined),/暂无成员资料/);
});
