import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const source=fs.readFileSync(new URL('../platform-claims.js',import.meta.url),'utf8');
const platform=fs.readFileSync(new URL('../platform.js',import.meta.url),'utf8');
const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const context=vm.createContext({JSON,Number,Error,Set,encodeURIComponent,esc,heading:()=>'',beijingTime:String,teamLabel:t=>t.number+' · '+t.name});vm.runInContext(source,context);
const receipt={requestId:'a'.repeat(32),secret:'b'.repeat(43),pin:'123456',expiresAt:10000};
class Storage{map=new Map();getItem(k){return this.map.get(k)??null;}setItem(k,v){this.map.set(k,v);}removeItem(k){this.map.delete(k);}}
const store=(storage,namespace='/gotc/')=>context.makeClaimReceiptStore(()=>storage,namespace);
function controller(storage,request,more={}){const states=[];return {states,client:context.makeClaimController({store:store(storage),request,isCurrent:()=>true,onChange:s=>states.push(s),...more})};}
const flush=()=>new Promise(resolve=>setImmediate(resolve));

test('request sends only name and school, stores tab receipt without returned code, and refresh recovers through bearer POST',async()=>{
  const storage=new Storage(),calls=[];
  const first=controller(storage,async(path,body)=>{calls.push({path,body});return {...receipt,status:'pending',code:'NEVER_STORE'};});
  first.client.start();await first.client.apply('Fixture Captain','Fixture School');
  assert.equal(calls[0].path,'claims/request');assert.deepEqual({...calls[0].body},{name:'Fixture Captain',school:'Fixture School'});
  assert.equal(first.states.at(-1).status,'pending');assert.ok(![...storage.map.values()].join('').includes('NEVER_STORE'));
  const refreshed=controller(storage,async(path,body)=>{calls.push({path,body});return {status:'approved',team:{id:'T1',number:1,name:'Test'},code:null};});
  await refreshed.client.start();assert.equal(calls[1].path,'claims/status');assert.deepEqual({...calls[1].body},{requestId:receipt.requestId,secret:receipt.secret});assert.equal(refreshed.states.at(-1).status,'approved');
  assert.equal(store(storage,'/other/').read().receipt,null);
});

test('approved and issued states can redeem repeatable code, status never retains the last code',async()=>{
  const storage=new Storage();store(storage).write(receipt);const calls=[];
  const {client,states}=controller(storage,async(path,body)=>{calls.push({path,body});return {status:path.endsWith('redeem')?'issued':'approved',team:{id:'T1'},code:path.endsWith('redeem')?'GT_FIXTURE.SECRET':null};});
  await client.start();await client.redeem();assert.equal(states.at(-1).code,'GT_FIXTURE.SECRET');await client.redeem();assert.equal(states.at(-1).code,'GT_FIXTURE.SECRET');await client.status();assert.equal(states.at(-1).code,null);
  assert.ok(calls.every(call=>!call.path.includes(receipt.secret)));assert.ok(![...storage.map.values()].join('').includes('GT_FIXTURE'));
});

test('storage failures are visible and in-memory receipt still permits status/redeem',async()=>{
  const storage={getItem(){throw new Error('blocked');},setItem(){throw new Error('blocked');},removeItem(){throw new Error('blocked');}};
  const {client,states}=controller(storage,async path=>path.endsWith('request')?receipt:{status:'approved',code:null});client.start();assert.match(states.at(-1).error,/无法读取/);
  await client.apply('n','s');assert.match(states.at(-1).error,/无法保存/);assert.equal(states.at(-1).receipt.secret,receipt.secret);
  await client.status();assert.equal(states.at(-1).status,'approved');assert.match(states.at(-1).error,/无法保存/);client.clear();assert.match(states.at(-1).error,/无法确认/);assert.equal(states.at(-1).receipt,null);
});

test('clearing while an issuance response is in flight cannot re-display or re-save receipt/code',async()=>{
  const storage=new Storage();store(storage).write(receipt);let resolve;
  const {client,states}=controller(storage,()=>new Promise(r=>{resolve=r;}));const pending=client.redeem();client.clear();resolve({status:'issued',code:'STALE_CODE'});await pending;
  assert.equal(states.at(-1).status,'new');assert.equal(states.at(-1).code,null);assert.equal(storage.map.size,0);
});

test('leaving the claim route invalidates in-flight status and prevents receipt resurrection',async()=>{
  let current=true,resolve;const storage=new Storage();const {client,states}=controller(storage,()=>new Promise(r=>{resolve=r;}),{isCurrent:()=>current});
  const pending=client.apply('n','s');current=false;client.dispose();resolve(receipt);await pending;assert.equal(storage.map.size,0);assert.ok(states.every(s=>!s.receipt));
});

test('single flight suppresses duplicate application and invalid receipt does not offer code',async()=>{
  let resolve,calls=0;const storage=new Storage();const {client,states}=controller(storage,()=>{calls++;return new Promise(r=>{resolve=r;});});
  const pending=client.apply('n','s');await client.apply('n','s');assert.equal(calls,1);resolve(receipt);await pending;
  const html=context.claimStateMarkup({status:'invalid',receipt,error:'',team:null,code:null,busy:false});assert.ok(!html.includes('id="claim-redeem"'));assert.match(html,/无效或已过期/);
});

test('public markup escapes server strings and renders only receipt, status and bearer-approved team',()=>{
  const html=context.claimStateMarkup({receipt,error:'<img src=x onerror=alert(1)>',status:'issued',team:{number:1,name:'<script>evil()</script>'},code:'<secret>',busy:false});
  assert.ok(!html.includes('<img'));assert.ok(!html.includes('<script>'));assert.ok(html.includes('&lt;secret&gt;'));assert.ok(!html.includes(receipt.secret));assert.ok(html.includes(receipt.pin));
  const pending=context.claimStateMarkup({receipt,error:'',status:'pending',team:null,code:null,busy:false});assert.ok(!pending.includes('id="claim-redeem"'));
  assert.ok(context.claimStateMarkup({receipt,error:'',status:'approved',team:{number:1,name:'Fixture'},code:null,busy:false}).includes('id="claim-redeem"'));
});

test('admin guard rejects changed route, identity, role, stale browser owner and detached panel',()=>{
  const initial={connected:true,route:'admin',role:'admin',version:7,loginId:'admin-one',browserOwner:true};let state={...initial};const guard=context.makeClaimsAdminGuard(()=>state);assert.equal(guard(),true);
  for(const change of [{connected:false},{route:'team'},{role:'judge'},{version:8},{loginId:'admin-two'},{browserOwner:false}]){state={...initial,...change};assert.equal(guard(),false);}
  state={...initial};assert.equal(guard(),true);
});

test('review markup shows both schools, revision, explicit in-person/resolution controls and no issued code',()=>{
  const request={id:'fixture',name:'Fixture Captain',school:'Submitted School',status:'pending',revision:4};
  const html=context.claimsReviewMarkup(request,{teams:[{id:'T1',number:1,name:'Fixture'}],people:[{id:'P1',name:'Fixture Captain',school:'Roster School',needsReview:true},{id:'P2',name:'Assigned Person',school:'Other',teamId:'T2'}]});
  assert.match(html,/Submitted School/);assert.match(html,/Roster School/);assert.match(html,/修订 4/);assert.match(html,/inPersonVerified/);assert.match(html,/resolvedIdentity/);assert.ok(!html.includes('Assigned Person'));assert.ok(!html.includes('data-copy-value'));
});

test('API and route wiring protects admin reads/writes and logout clears tab claim receipt',()=>{
  assert.match(platform,/path==='admin\/claims' \|\| path\.startsWith\('admin\/claims\/'\)/);
  assert.match(platform,/route=\['claim'/);assert.match(platform,/if\(route==='claim'\)return await renderClaim\(version\)/);
  const logout=platform.slice(platform.indexOf('async function logoutAccount'),platform.indexOf('function heading'));
  assert.ok(logout.indexOf("await api('logout'")<logout.indexOf("clearClaimReceipt()"));
  assert.match(source,/if\(revision!==edit\)/);assert.match(source,/button\.dataset\.stale='true'/);
  const installer=source.slice(source.indexOf('function installClaimsAdmin'));
  assert.match(installer,/onclick=load/);assert.ok(!/\n\s*load\(\);/.test(installer));
});

test('real approval handler submits exact selected revisions from displayed model, including a stale same-panel selection',()=>{
  const people=[{id:'p1',name:'Captain',revision:2},{id:'p2',name:'Bob',revision:7},{id:'unselected',name:'Other',revision:9}];
  const request={id:'request-fixture',revision:1,status:'pending'};
  const selected=[{value:'p1'},{value:'p2'}],posts=[];
  const form={elements:{teamId:{value:'T1'},captainId:{value:'p1'},pin:{value:'123456'},note:{value:'Verified Bob onsite'},inPersonVerified:{checked:true},resolvedIdentity:{checked:false}},querySelectorAll:()=>selected};
  const nodes={'claim-review':{innerHTML:''},'claim-review-form':form,'claim-member-search':{},'claim-team-search':{},'claim-reject':{}};
  const scope=vm.createContext({Map,Object,model:{requests:[request],people},document:{getElementById:id=>nodes[id]},busy:false,live:()=>true,replaceEditor:()=>true,trackAdminForm:()=>{},claimsReviewMarkup:()=>'',mutate:(f,path,body)=>posts.push({form:f,path,body})});
  const helper=source.slice(source.indexOf('function claimMemberSnapshot'),source.indexOf('function claimsReviewMarkup'));
  const handler=source.slice(source.indexOf('  function selectRequest(id){'),source.indexOf("  document.getElementById('claims-admin-load').onclick=load;"));
  vm.runInContext(helper+'\n'+handler+'\nselectRequest("request-fixture");',scope);
  // A successful person-editor write does not refresh the displayed roster model.
  const nowSavedPerson={...people[1],name:'Charlie',revision:8};assert.equal(nowSavedPerson.revision,8);
  form.onsubmit({preventDefault(){}});
  assert.equal(posts.length,1);assert.equal(posts[0].path,'admin/claims/review');
  assert.deepEqual({...posts[0].body.memberRevisions},{p1:2,p2:7});assert.deepEqual([...posts[0].body.memberIds],['p1','p2']);
  assert.equal(posts[0].body.note,'Verified Bob onsite');assert.equal(posts[0].body.expectedRevision,1);
  assert.ok(!Object.hasOwn(posts[0].body.memberRevisions,'unselected'));
});

test('real admin mutation handler retains selected members, PIN and notes after roster revision conflict',async()=>{
  const buttons=[{disabled:false,dataset:{}},{disabled:false,dataset:{}}];
  const form={elements:{pin:{value:'123456'},note:{value:'Keep my onsite note'},members:[{checked:true,value:'p2'}]},error:'',querySelectorAll:selector=>selector==='button'?buttons:[],querySelector:()=>null};
  const dirtyAdminForms=new Set([form]);
  const scope=vm.createContext({busy:false,live:()=>true,api:async()=>{const error=new Error('所选成员的名单已变化');error.status=409;throw error;},dirtyAdminForms,message:{textContent:''},formError:(target,error)=>target.error=error,form});
  const mutation=source.slice(source.indexOf('  async function mutate(form,path,body){'),source.indexOf('  function replaceEditor(panel)'));
  vm.runInContext(mutation,scope);await vm.runInContext("mutate(form,'admin/claims/review',{})",scope);
  assert.equal(form.elements.pin.value,'123456');assert.equal(form.elements.note.value,'Keep my onsite note');assert.equal(form.elements.members[0].checked,true);
  assert.match(form.error,/名单已变化/);assert.ok(dirtyAdminForms.has(form));assert.ok(buttons.every(button=>button.disabled && button.dataset.stale==='true'));
});

const declaredCaptain={name:'Fixture Captain',school:'Captain School'};
const declaredMembers=[{name:'First Teammate',school:'First School'},{name:'Second Teammate',school:'Second School'}];
const declaredResponse={...receipt,status:'pending',requestRevision:1,captain:declaredCaptain,declaration:{captain:declaredCaptain,members:declaredMembers},code:null};

test('declared application and pending edits persist only capability, recover exact declaration and preserve failed drafts',async()=>{
  const storage=new Storage(),calls=[];let fail=false;
  const first=controller(storage,async(path,body)=>{calls.push({path,body});if(fail)throw new Error('申报冲突');return path.endsWith('update')?{...declaredResponse,requestRevision:2,declaration:{captain:declaredCaptain,members:body.members}}:declaredResponse;});
  first.client.start();await first.client.apply(declaredCaptain.name,declaredCaptain.school,declaredMembers);
  assert.equal(JSON.stringify(calls[0].body),JSON.stringify({...declaredCaptain,members:declaredMembers}));
  assert.equal(JSON.stringify(first.states.at(-1).declaration),JSON.stringify(declaredResponse.declaration));
  const saved=JSON.parse([...storage.map.values()][0]);assert.deepEqual(saved,receipt);
  first.client.edit();const changed=[{...declaredMembers[0],school:'Edited School'},declaredMembers[1]];
  fail=true;await first.client.update(changed);assert.match(first.states.at(-1).error,/申报冲突/);assert.equal(first.states.at(-1).editing,true);assert.equal(first.states.at(-1).draft.members[0].school,'Edited School');
  assert.equal(calls.at(-1).body.expectedRevision,1);fail=false;await first.client.update(changed);
  assert.equal(first.states.at(-1).editing,false);assert.equal(first.states.at(-1).requestRevision,2);
  const fresh=controller(storage,async()=>({...declaredResponse,requestRevision:2,declaration:{captain:declaredCaptain,members:changed}}));await fresh.client.start();
  assert.equal(fresh.states.at(-1).declaration.members[0].school,'Edited School');assert.equal(fresh.states.at(-1).requestRevision,2);
  assert.ok(![...storage.map.values()].join('').includes('Edited School'));
});

test('cancel edit, clear receipt and route disposal suppress stale update responses',async()=>{
  for(const action of ['cancelEdit','clear','dispose']){
    const storage=new Storage();store(storage).write(receipt);let resolve;
    const {client,states}=controller(storage,async path=>path.endsWith('update')?new Promise(r=>{resolve=r;}):declaredResponse);
    await client.start();client.edit();const pending=client.update(declaredMembers);await client[action]();
    resolve({...declaredResponse,requestRevision:99,declaration:{captain:declaredCaptain,members:[{name:'STALE',school:'STALE'}]}});await pending;
    assert.ok(states.every(s=>s.requestRevision!==99));assert.ok(states.every(s=>!JSON.stringify(s.declaration).includes('STALE')));
    if(action==='clear'){assert.equal(states.at(-1).receipt,null);assert.equal(storage.map.size,0);}
  }
});

test('legacy receipt can supplement teammates while edits retain their base revision across status refresh',async()=>{
  const storage=new Storage();store(storage).write(receipt);const calls=[];let revision=1;
  const {client,states}=controller(storage,async(path,body)=>{calls.push({path,body});return {...declaredResponse,requestRevision:revision,declaration:null};});
  await client.start();client.edit();assert.equal(states.at(-1).draft.members.length,2);
  revision=2;await client.status();assert.equal(states.at(-1).editing,true);await client.update(declaredMembers);
  assert.equal(calls.at(-1).body.expectedRevision,1);
});

// A small DOM fixture runs the actual public render/event handlers. Browser E2E
// covers native validation and layout separately; these tests verify bound values.
function claimPageFixture(transport){
  const nodes=new Map(),storage=new Storage();
  const decode=value=>value.replace(/&quot;/g,'"').replace(/&#39;/g,"'").replace(/&lt;/g,'<').replace(/&gt;/g,'>').replace(/&amp;/g,'&');
  const node=id=>{const value={id,onclick:null};nodes.set(id,value);return value;};
  const root={html:''};Object.defineProperty(root,'innerHTML',{get(){return this.html;},set(html){
    this.html=html;nodes.clear();for(const match of html.matchAll(/id="([^"]+)"/g))node(match[1]);
    const form=nodes.get('claim-request-form')??nodes.get('claim-update-form');if(!form)return;
    form.elements={};for(const name of ['name','school']){const field=html.match(new RegExp('<input name="'+name+'"[^>]*value="([^"]*)"'));if(field)form.elements[name]={value:decode(field[1])};}
    const rows=[...html.matchAll(/<fieldset class="claim-member-row"[\s\S]*?<\/fieldset>/g)].map(match=>{
      const fields={};for(const name of ['memberName','memberSchool'])fields[name]={value:decode(match[0].match(new RegExp('name="'+name+'"[^>]*value="([^"]*)"'))[1])};
      return {querySelector:selector=>fields[selector.match(/name="([^"]+)"/)[1]]};
    });
    const remove=[...html.matchAll(/data-claim-remove="(\d+)"/g)].map(match=>({dataset:{claimRemove:match[1]}}));
    form.querySelectorAll=selector=>selector==='[data-claim-member-row]'?rows:selector==='[data-claim-remove]'?remove:[];
    form.rows=rows;form.remove=remove;
  }});
  const scope=vm.createContext({JSON,Number,Error,Set,encodeURIComponent,esc,heading:()=>'',beijingTime:String,teamLabel:t=>t.name,base:{pathname:'/test/'},sessionStorage:storage,root,document:{getElementById:id=>nodes.get(id)??null},api:transport,route:'claim',routeVersion:1,confirm:()=>true});vm.runInContext(source,scope);
  return {scope,root,storage,nodes,render:()=>vm.runInContext('renderClaim(1)',scope),form:()=>nodes.get('claim-request-form')??nodes.get('claim-update-form'),fill(){const f=this.form();if(f.elements.name)f.elements.name.value=declaredCaptain.name;if(f.elements.school)f.elements.school.value=declaredCaptain.school;f.rows.forEach((row,i)=>{row.querySelector('[name="memberName"]').value='Teammate '+i;row.querySelector('[name="memberSchool"]').value='School '+i;});f.oninput();}};
}

test('real public form adds/removes dynamic rows, submits every entered person and retains failed/busy values',async()=>{
  const calls=[];let reject;const page=claimPageFixture((path,body)=>{calls.push({path,body});return new Promise((_,no)=>{reject=no;});});
  await page.render();page.fill();assert.equal(page.form().rows.length,2);
  page.nodes.get('claim-member-add').onclick();assert.equal(page.form().rows.length,3);assert.equal(page.form().rows[0].querySelector('[name="memberName"]').value,'Teammate 0');
  page.fill();page.nodes.get('claim-member-add').onclick();assert.equal(page.form().rows.length,4);assert.ok(!page.nodes.has('claim-member-add'));
  page.fill();page.form().remove[1].onclick();assert.equal(page.form().rows.length,3);assert.equal(page.form().rows[1].querySelector('[name="memberName"]').value,'Teammate 2');
  page.form().onsubmit({preventDefault(){}});assert.equal(calls.length,1);assert.equal(calls[0].body.members.length,3);assert.equal(calls[0].body.members[1].name,'Teammate 2');
  assert.equal(page.form().elements.name.value,declaredCaptain.name);
  page.form().rows[0].querySelector('[name="memberSchool"]').value='Typed While Busy';page.form().oninput();reject(new Error('重复成员'));await flush();
  assert.match(page.root.innerHTML,/重复成员/);assert.equal(page.form().rows[0].querySelector('[name="memberSchool"]').value,'Typed While Busy');
  assert.equal(page.form().rows.length,3);
});

test('real receipt form recovers after refresh, edits teammates and submits expected revision with the same capability',async()=>{
  const calls=[];let revision=1,members=declaredMembers;
  const page=claimPageFixture(async(path,body)=>{calls.push({path,body});if(path.endsWith('update')){members=body.members;revision++;}return {...declaredResponse,requestRevision:revision,declaration:{captain:declaredCaptain,members}};});
  await page.render();page.fill();page.form().onsubmit({preventDefault(){}});await flush();assert.match(page.root.innerHTML,/First Teammate/);
  await page.render();assert.equal(calls.at(-1).path,'claims/status');assert.match(page.root.innerHTML,/First Teammate/);
  page.nodes.get('claim-edit').onclick();page.form().rows[0].querySelector('[name="memberSchool"]').value='Edited On Receipt';page.form().oninput();page.form().onsubmit({preventDefault(){}});await flush();
  assert.equal(calls.at(-1).path,'claims/update');assert.equal(calls.at(-1).body.expectedRevision,1);assert.equal(calls.at(-1).body.secret,receipt.secret);assert.match(page.root.innerHTML,/Edited On Receipt/);assert.ok(!page.nodes.has('claim-update-form'));
});

test('declared staff mapping uses one selector per declaration and submits displayed revisions in declared order',()=>{
  const request={id:'declared-fixture',name:declaredCaptain.name,school:declaredCaptain.school,revision:3,status:'pending',declaration:declaredResponse.declaration};
  const people=[{id:'p1',...declaredCaptain,revision:4},{id:'p2',...declaredMembers[0],revision:5},{id:'p3',...declaredMembers[1],revision:6}];
  const html=context.claimsReviewMarkup(request,{people,teams:[]});assert.equal([...html.matchAll(/data-claim-map="\d+"/g)].length,3);assert.ok(!html.includes('name="memberIds"'));assert.match(html,/First Teammate · First School/);
  const selects=[{value:'p1'},{value:'p3'},{value:'p2'}],posts=[];
  const form={elements:{teamId:{value:'T1'},captainId:selects[0],pin:{value:'123456'},note:{value:'Check exact school aliases'},inPersonVerified:{checked:true},resolvedIdentity:{checked:true}},querySelectorAll:selector=>selector==='[data-claim-map]'?selects:[]};
  const nodes={'claim-review':{innerHTML:''},'claim-review-form':form,'claim-team-search':{},'claim-reject':{}};
  const scope=vm.createContext({Map,Object,model:{requests:[request],people},document:{getElementById:id=>nodes[id]},busy:false,live:()=>true,replaceEditor:()=>true,trackAdminForm:()=>{},claimsReviewMarkup:()=>'',mutate:(f,path,body)=>posts.push({path,body})});
  const helper=source.slice(source.indexOf('function claimMemberSnapshot'),source.indexOf('function claimsReviewMarkup'));
  const handler=source.slice(source.indexOf('  function selectRequest(id){'),source.indexOf("  document.getElementById('claims-admin-load').onclick=load;"));
  vm.runInContext(helper+'\n'+handler+'\nselectRequest("declared-fixture");',scope);form.onsubmit({preventDefault(){}});
  assert.deepEqual([...posts[0].body.declaredMemberIds],['p1','p3','p2']);assert.deepEqual({...posts[0].body.memberRevisions},{p1:4,p3:6,p2:5});assert.equal(posts[0].body.expectedRevision,3);
});
