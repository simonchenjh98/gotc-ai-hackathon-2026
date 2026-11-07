import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const platform=fs.readFileSync(new URL('../platform.js',import.meta.url),'utf8');
const source=fs.readFileSync(new URL('../platform-admin.js',import.meta.url),'utf8')+'\n'+platform.split("window.addEventListener('hashchange'")[0];
const deferred=()=>{let resolve;const promise=new Promise(r=>{resolve=r;});return {promise,resolve};};
const ok=body=>({ok:true,json:async()=>body});
const fail=(error,code)=>({ok:false,status:409,json:async()=>({error,code})});
const members=[{personId:'P1',name:'Captain One',school:'School A',role:'captain',source:'approved_membership',schoolState:'recorded'},
  {personId:'P2',name:'Member Two',school:'School B',role:'member',source:'approved_membership',schoolState:'recorded'}];
const detail=(id='T1',overrides={})=>({team:{id,number:1,name:'Stored '+id,revision:7,status:'active'},recycle:null,recycleContext:'a'.repeat(64),
  rosterContext:'c'.repeat(64),rosterMode:'approved',roster:{source:'approved_memberships',members},restoreConflicts:[],restoreAdvisories:[],...overrides});
function element(){
  const listeners={};let html='',text='';
  return {isConnected:true,hidden:false,disabled:false,value:'',classList:{add(){},remove(){}},
    get textContent(){return text;},set textContent(value){text=value;html='';},
    addEventListener(name,fn){(listeners[name]??=[]).push(fn);},
    dispatch(name,target){for(const fn of listeners[name] || [])fn({target,stopPropagation(){}});},
    input(){for(const fn of listeners.input || [])fn({target:{}});},
    get innerHTML(){return html;},set innerHTML(value){html=value;},
    querySelectorAll(){return [];},closest(){return null;},insertAdjacentHTML(){},scrollIntoView(){}};
}
function form(values={}){
  const node=element();node.elements=Object.fromEntries(Object.entries(values).map(([key,value])=>[key,Object.assign(element(),{value})]));node.button=element();node.error=element();node.querySelector=selector=>selector==='.form-error'?node.error:node.button;return node;
}
function harness(fetcher){
  const ids=['team-member-detail','team-detail-refresh','team-recycle-open','team-recycle-toggle','team-recycle-confirm','team-recycle-target','team-recycle-reason','team-recycle-submit','team-recycle-cancel','team-lifecycle-error','team-recycle-bin','team-recycle-refresh','team-recycle-list','team-restore-detail','team-restore-heading','team-restore-members','team-restore-conflicts','team-restore-advisories','team-restore-result',
    'team-member-edit','team-member-editor','team-member-mode','team-member-rows','team-member-add','team-member-reason','team-member-save','team-member-cancel','team-member-error'];
  const elements=Object.fromEntries(ids.map(id=>[id,element()]));
  for(const id of ['team-member-edit','team-member-editor'])elements[id].hidden=true;
  elements['team-form']=form({id:'T1',expectedRevision:'7',name:'Editor T1'});
  elements['team-restore-form']=form({name:'',reason:''});
  elements['platform-main']=element();elements['platform-toast']=element();
  const requests=[];
  const context=vm.createContext({document:{getElementById:id=>elements[id]},location:{href:'https://example.test/platform.html#admin'},URL,URLSearchParams,Intl,Date,TextEncoder,
    localStorage:{getItem:key=>key.endsWith(':logout')?null:'LOGIN1',setItem(){}},setTimeout:()=>0,clearTimeout,confirm:()=>true,
    fetch:async(url,options)=>{requests.push({url,method:options.method,headers:options.headers,body:options.body?JSON.parse(options.body):undefined});return fetcher(url,options);}});
  vm.runInContext(source+"\nroute='admin';routeVersion=1;session={role:'admin',loginId:'LOGIN1'};",context);
  context.refreshes=0;
  context.config={form:elements['team-form'],alive:()=>true,refreshDirectory:async()=>{context.refreshes++;},count:0,clearEditor(){}};
  vm.runInContext('controller=installAdminTeamLifecycle(config);trackAdminForm(config.form);',context);
  const node=id=>elements['team-member-'+id];
  return {context,elements,requests,node,
    select:()=>vm.runInContext('controller.select()',context),
    type(index,field,value){node('rows').dispatch('input',{dataset:{memberField:field,memberIndex:String(index)},value});},
    click(index,action){node('rows').dispatch('click',{dataset:{memberAction:action,memberIndex:String(index)}});},
    posts:()=>requests.filter(request=>request.method==='POST')};
}

test('edit button appears only for a live, non-recycled roster',async()=>{
  const h=harness(async()=>ok(detail()));
  await h.select();assert.equal(h.node('edit').hidden,false);
  const archived=harness(async()=>ok(detail('T1',{recycle:{eventId:1,deletedAt:1,reason:'x',originalStatus:'active'},recycleContext:null,rosterContext:null})));
  await archived.select();assert.equal(archived.node('edit').hidden,true);
});

test('editor renders escaped rows, reorders captain, adds and removes, then sends one exact request',async()=>{
  const pending=deferred();
  const h=harness(async(_url,options)=>options.method==='POST'?pending.promise:ok(detail('T1',{roster:{members:[{...members[0],name:'<img src=x>'},members[1]]}})));
  await h.select();h.node('edit').onclick();
  assert.equal(h.node('editor').hidden,false);
  assert.match(h.node('rows').innerHTML,/&lt;img src=x&gt;/);assert(!h.node('rows').innerHTML.includes('<img'));
  h.type(0,'name','Captain Fixed');
  h.click(1,'captain');
  h.node('add').onclick();h.type(2,'name','New Three');h.type(2,'school','School C');
  h.node('add').onclick();h.click(3,'remove');
  assert.match(h.node('rows').innerHTML,/新增/);
  await h.node('save').onclick();assert.equal(h.posts().length,0);assert.match(h.node('error').textContent,/修改原因/);
  h.node('reason').value='Synthetic correction';
  const saving=h.node('save').onclick();await h.node('save').onclick();
  assert.equal(h.posts().length,1);
  assert.deepEqual(h.posts()[0].body,{teamId:'T1',rosterContext:'c'.repeat(64),reason:'Synthetic correction',members:[
    {personId:'P2',name:'Member Two',school:'School B'},{personId:'P1',name:'Captain Fixed',school:'School A'},{personId:null,name:'New Three',school:'School C'}]});
  assert.equal(h.posts()[0].url.pathname.endsWith('/api/admin/data/team/members'),true);
  assert.equal(h.posts()[0].headers['X-GOTC-Login'],'LOGIN1');
  pending.resolve(ok({ok:true}));await saving;
  assert.equal(h.node('editor').hidden,true);assert.equal(h.context.refreshes,1);
  assert.equal(vm.runInContext('dirtyAdminForms.has(config.form)',h.context),false);
});

test('client validation blocks blank names, missing schools, duplicates and the last removal',async()=>{
  const h=harness(async()=>ok(detail()));
  await h.select();h.node('edit').onclick();h.node('reason').value='Reason';
  h.node('add').onclick();await h.node('save').onclick();assert.match(h.node('error').textContent,/第 3 位成员的姓名/);
  h.type(2,'name','Member Two');await h.node('save').onclick();assert.match(h.node('error').textContent,/第 3 位成员的学校/);
  h.type(2,'school','School B');await h.node('save').onclick();assert.match(h.node('error').textContent,/重复/);
  h.click(2,'remove');h.click(1,'remove');h.click(0,'remove');
  assert.equal(vm.runInContext('controller',h.context)&&h.node('rows').innerHTML.includes('Captain One'),true);
  assert.equal(h.posts().length,0);
});

test('registration mode locks member schools and only sends the captain school',async()=>{
  const h=harness(async(_url,options)=>options.method==='POST'?ok({ok:true}):ok(detail('T1',{rosterMode:'registration',roster:{source:'registration',members:[
    {personId:null,name:'Reg Captain',school:'School R',role:'captain'},{personId:null,name:'Reg Member',school:null,role:'member'}]}})));
  await h.select();h.node('edit').onclick();
  assert.match(h.node('mode').textContent,/只记录队长学校/);assert.match(h.node('rows').innerHTML,/disabled placeholder="原登记只记录队长学校"/);
  h.click(1,'captain');h.node('reason').value='Swap captain';
  await h.node('save').onclick();assert.match(h.node('error').textContent,/队长学校/);
  h.type(0,'school','School M');await h.node('save').onclick();
  assert.deepEqual(h.posts()[0].body.members,[{personId:null,name:'Reg Member',school:'School M'},{personId:null,name:'Reg Captain',school:''}]);
});

test('a conflict keeps the draft; a refresh of the same team keeps rows and adopts the new context',async()=>{
  let reads=0;
  const h=harness(async(_url,options)=>options.method==='POST'?fail('成员资料已被更新','roster_context_conflict'):ok(detail('T1',{rosterContext:(++reads===1?'c':'d').repeat(64)})));
  await h.select();h.node('edit').onclick();h.type(1,'name','Kept Edit');h.node('reason').value='Reason';
  await h.node('save').onclick();
  assert.match(h.node('error').textContent,/已被更新.*当前输入已保留/);assert.equal(h.node('editor').hidden,false);
  await h.select();assert.equal(h.node('editor').hidden,false);
  await h.node('save').onclick();assert.equal(h.posts()[1].body.rosterContext,'d'.repeat(64));
  assert.equal(h.posts()[1].body.members[1].name,'Kept Edit');assert.equal(h.posts()[1].body.reason,'Reason');
});

test('switching teams closes the editor and a stale draft cannot be saved to the new team',async()=>{
  const h=harness(async url=>ok(detail(url.searchParams.get('teamId'))));
  await h.select();h.node('edit').onclick();h.type(0,'name','Draft For T1');h.node('reason').value='Reason';
  h.elements['team-form'].elements.id.value='T2';h.elements['team-form'].editSession=1;await h.select();
  assert.equal(h.node('editor').hidden,true);
  await h.node('save').onclick();assert.equal(h.posts().length,0);
});

test('member drafts guard navigation independently of the parent team fields',async()=>{
  const h=harness(async()=>ok(detail()));await h.select();h.node('edit').onclick();
  h.node('reason').value='Unsaved reason';h.node('reason').input();
  let prompts=0;h.context.confirm=()=>{prompts++;return false;};
  assert.equal(vm.runInContext('dirtyAdminForms.has(config.form)',h.context),false);
  assert.equal(vm.runInContext('dirtyAdminForms.has(document.getElementById("team-member-editor"))',h.context),true);
  assert.equal(vm.runInContext('confirmLeave()',h.context),false);assert.equal(prompts,1);
  assert.equal(vm.runInContext('controller.hasUnsaved()',h.context),true);
  h.node('cancel').onclick();assert.equal(vm.runInContext('confirmLeave()',h.context),true);
  assert.equal(vm.runInContext('dirtyAdminForms.size',h.context),0);
});

test('member edits during a pending save survive success and use the refreshed roster context',async()=>{
  const pending=deferred();let posts=0,reads=0;
  const h=harness(async(_url,options)=>options.method==='POST'?(++posts===1?pending.promise:ok({ok:true})):ok(detail('T1',{rosterContext:(++reads===1?'c':'d').repeat(64)})));
  await h.select();h.node('edit').onclick();h.type(0,'name','Submitted name');h.node('reason').value='Submitted reason';
  const saving=h.node('save').onclick();
  h.type(0,'name','Later name');h.node('reason').value='Later reason';h.node('reason').input();
  pending.resolve(ok({ok:true}));await saving;
  assert.equal(h.node('editor').hidden,false);assert.equal(h.node('reason').value,'Later reason');
  assert.match(h.node('error').textContent,/新输入已保留/);
  assert.equal(vm.runInContext('controller.hasUnsaved()',h.context),true);
  await h.node('save').onclick();
  assert.equal(h.posts()[0].body.members[0].name,'Submitted name');assert.equal(h.posts()[0].body.reason,'Submitted reason');
  assert.equal(h.posts()[1].body.members[0].name,'Later name');assert.equal(h.posts()[1].body.reason,'Later reason');
  assert.equal(h.posts()[1].body.rosterContext,'d'.repeat(64));assert.equal(h.node('editor').hidden,true);
  assert.equal(vm.runInContext('dirtyAdminForms.size',h.context),0);
});

for(const change of ['session','browser owner','401'])test(`losing ${change} clears all member editor data`,async()=>{
  let denied=false;
  const h=harness(async()=>denied?{ok:false,status:401,json:async()=>({error:'Synthetic login expired'})}:ok(detail()));
  await h.select();h.node('edit').onclick();h.type(0,'name','Private edit');h.node('reason').value='Private reason';h.node('reason').input();
  if(change==='session')vm.runInContext("session={role:'team',loginId:'TEAM'};controller.valid();",h.context);
  else if(change==='browser owner'){h.context.localStorage.getItem=()=> 'OTHER';vm.runInContext('controller.valid()',h.context);}
  else{denied=true;await h.select();}
  assert.equal(h.node('editor').hidden,true);assert.equal(h.node('rows').innerHTML,'');assert.equal(h.node('reason').value,'');
  assert.equal(vm.runInContext('dirtyAdminForms.has(document.getElementById("team-member-editor"))',h.context),false);
  await h.node('save').onclick();assert.equal(h.posts().length,0);
});

test('a failed member refresh keeps draft and reason but blocks saving until a successful reread',async()=>{
  let reads=0;
  const h=harness(async(_url,options)=>{
    if(options.method==='POST')return ok({ok:true});
    if(++reads===2)return {ok:false,status:500,json:async()=>({error:'Synthetic read failure'})};
    return ok(detail('T1',{rosterContext:(reads===1?'c':'d').repeat(64)}));
  });
  await h.select();h.node('edit').onclick();h.type(1,'name','Kept member');h.node('reason').value='Kept reason';h.node('reason').input();
  await h.select();assert.equal(h.node('editor').hidden,false);assert.equal(h.node('reason').value,'Kept reason');
  assert.equal(h.node('save').disabled,true);await h.node('save').onclick();assert.equal(h.posts().length,0);
  assert.equal(vm.runInContext('controller.hasUnsaved()',h.context),true);
  await h.select();assert.equal(h.node('save').disabled,false);await h.node('save').onclick();
  assert.equal(h.posts()[0].body.members[1].name,'Kept member');assert.equal(h.posts()[0].body.reason,'Kept reason');
  assert.equal(h.posts()[0].body.rosterContext,'d'.repeat(64));
});

test('a late member save cannot restore data or refresh an administrator who changed identity',async()=>{
  const pending=deferred(),h=harness(async(_url,options)=>options.method==='POST'?pending.promise:ok(detail()));
  await h.select();h.node('edit').onclick();h.type(0,'name','Private change');h.node('reason').value='Private reason';
  const saving=h.node('save').onclick();
  vm.runInContext("session={role:'team',loginId:'TEAM'};controller.valid();",h.context);
  pending.resolve(ok({ok:true}));await saving;
  assert.equal(h.node('rows').innerHTML,'');assert.equal(h.node('reason').value,'');assert.equal(h.node('editor').hidden,true);
  assert.equal(h.context.refreshes,0);assert.equal(h.elements['platform-toast'].textContent,'');
  assert.equal(vm.runInContext('dirtyAdminForms.size',h.context),0);
});

test('administrator member edits retain the five-person roster limit',async()=>{
  const h=harness(async(_url,options)=>options.method==='POST'?ok({ok:true}):ok(detail()));
  await h.select();h.node('edit').onclick();
  for(let index=2;index<5;index++){h.node('add').onclick();h.type(index,'name','Member '+index);h.type(index,'school','School '+index);}
  assert.equal(h.node('add').disabled,true);h.node('add').onclick();
  assert.equal([...h.node('rows').innerHTML.matchAll(/data-member-field="name"/g)].length,5);
  h.node('reason').value='Five members';await h.node('save').onclick();assert.equal(h.posts()[0].body.members.length,5);
  const six=members.concat(Array.from({length:4},(_,index)=>({personId:null,name:'Extra '+index,school:'School '+index})));
  h.context.six=six;assert.match(vm.runInContext('adminMemberPayload(six,"approved").error',h.context),/最多 5 名/);
});
