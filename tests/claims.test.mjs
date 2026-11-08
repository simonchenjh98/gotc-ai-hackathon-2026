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
  assert.ok(!html.includes('<img'));assert.ok(!html.includes('<script>'));assert.ok(html.includes('&lt;secret&gt;'));assert.ok(!html.includes(receipt.secret));assert.ok(!html.includes(receipt.pin));
  const pending=context.claimStateMarkup({receipt,error:'',status:'pending',team:null,code:null,busy:false});assert.ok(!pending.includes('id="claim-redeem"'));
  const approved=context.claimStateMarkup({receipt,error:'',status:'approved',team:{number:1,name:'Fixture'},code:null,busy:false});
  assert.ok(approved.includes('id="claim-redeem"'));assert.match(approved,/claim-redeem-primary/);assert.ok(!approved.includes('id="claim-status"'));
  const rejected=context.claimStateMarkup({receipt,error:'',status:'rejected',team:null,code:null,busy:false});assert.match(rejected,/未完成/);assert.doesNotMatch(rejected,/立即领取/);
  assert.match(context.claimStateMarkup({receipt:null,error:'',status:'new',team:null,code:null,busy:false,draft:{name:'',school:'',teamName:'',track:'',members:context.emptyClaimMembers()}}),/提交并自动完成申请/);
});

test('admin guard rejects changed route, identity, role, stale browser owner and detached panel',()=>{
  const initial={connected:true,route:'admin',role:'admin',version:7,loginId:'admin-one',browserOwner:true};let state={...initial};const guard=context.makeClaimsAdminGuard(()=>state);assert.equal(guard(),true);
  for(const change of [{connected:false},{route:'team'},{role:'judge'},{version:8},{loginId:'admin-two'},{browserOwner:false}]){state={...initial,...change};assert.equal(guard(),false);}
  state={...initial};assert.equal(guard(),true);
});

test('legacy pending review requests applicant completion and never requires imported records',()=>{
  const request={id:'fixture',name:'Fixture Captain',school:'Submitted School',status:'pending',revision:4};
  const html=context.claimsReviewMarkup(request,{teams:[],people:[]});
  assert.match(html,/Submitted School/);assert.match(html,/修订 4/);assert.match(html,/补充全体组员/);
  assert.ok(!html.includes('name="teamId"'));assert.ok(!html.includes('name="captainId"'));assert.ok(!html.includes('type="submit"'));
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
  assert.equal(calls.at(-1).body.expectedRevision,1);assert.equal(first.states.at(-1).needsReview,true);
  fail=false;await first.client.reviewLatest();assert.equal(first.states.at(-1).draft.members[0].school,'Edited School');
  first.client.confirmReview();await first.client.update(changed);
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

test('legacy receipt preserves its draft until a changed saved version is explicitly reviewed',async()=>{
  const storage=new Storage();store(storage).write(receipt);const calls=[];let revision=1;
  const {client,states}=controller(storage,async(path,body)=>{calls.push({path,body});return {...declaredResponse,requestRevision:revision,declaration:null};});
  await client.start();client.edit();assert.equal(states.at(-1).draft.members.length,2);
  revision=2;await client.status();assert.equal(states.at(-1).editing,true);assert.equal(states.at(-1).needsReview,true);
  const before=calls.length;await client.update(declaredMembers);assert.equal(calls.length,before);
  client.confirmReview();await client.update(declaredMembers);assert.equal(calls.length,before,'ordinary status cannot confirm a new edit base');
  await client.reviewLatest();assert.equal(states.at(-1).draft.members[0].name,declaredMembers[0].name);
  client.confirmReview();assert.equal(calls.length,before+1,'confirmation does not write');await client.update(declaredMembers);
  assert.equal(calls.at(-1).body.expectedRevision,2);
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
    form.elements={};for(const name of ['name','school','teamName']){const field=html.match(new RegExp('<input name="'+name+'"[^>]*value="([^"]*)"'));if(field)form.elements[name]={value:decode(field[1])};}
    const track=html.match(/<select name="track"[^>]*>([\s\S]*?)<\/select>/);if(track)form.elements.track={value:decode(track[1].match(/value="([^"]*)" selected/)?.[1]??'')};
    const rows=[...html.matchAll(/<fieldset class="claim-member-row"[\s\S]*?<\/fieldset>/g)].map(match=>{
      const fields={};for(const name of ['memberName','memberSchool'])fields[name]={value:decode(match[0].match(new RegExp('name="'+name+'"[^>]*value="([^"]*)"'))[1])};
      return {querySelector:selector=>fields[selector.match(/name="([^"]+)"/)[1]]};
    });
    const remove=[...html.matchAll(/data-claim-remove="(\d+)"/g)].map(match=>({dataset:{claimRemove:match[1]}}));
    form.querySelectorAll=selector=>selector==='[data-claim-member-row]'?rows:selector==='[data-claim-remove]'?remove:[];
    form.rows=rows;form.remove=remove;
  }});
  const scope=vm.createContext({JSON,Number,Error,Set,encodeURIComponent,esc,heading:()=>'',beijingTime:String,teamLabel:t=>t.name,base:{pathname:'/test/'},sessionStorage:storage,root,document:{getElementById:id=>nodes.get(id)??null},api:transport,route:'claim',routeVersion:1,confirm:()=>true});vm.runInContext(source,scope);
  return {scope,root,storage,nodes,render:()=>vm.runInContext('renderClaim(1)',scope),form:()=>nodes.get('claim-request-form')??nodes.get('claim-update-form'),fill(){const f=this.form();if(f.elements.name)f.elements.name.value=declaredCaptain.name;if(f.elements.school)f.elements.school.value=declaredCaptain.school;if(f.elements.teamName)f.elements.teamName.value='Fixture Team';f.rows.forEach((row,i)=>{row.querySelector('[name="memberName"]').value='Teammate '+i;row.querySelector('[name="memberSchool"]').value='School '+i;});f.oninput();}};
}

test('real public form locks the first application and restores its submitted values after failure',async()=>{
  const calls=[];let reject;const page=claimPageFixture((path,body)=>{calls.push({path,body});return new Promise((_,no)=>{reject=no;});});
  await page.render();page.fill();assert.equal(page.form().rows.length,2);
  page.nodes.get('claim-member-add').onclick();assert.equal(page.form().rows.length,3);assert.equal(page.form().rows[0].querySelector('[name="memberName"]').value,'Teammate 0');
  page.fill();page.nodes.get('claim-member-add').onclick();assert.equal(page.form().rows.length,4);assert.ok(!page.nodes.has('claim-member-add'));
  page.fill();page.form().remove[1].onclick();assert.equal(page.form().rows.length,3);assert.equal(page.form().rows[1].querySelector('[name="memberName"]').value,'Teammate 2');
  page.form().onsubmit({preventDefault(){}});assert.equal(calls.length,1);assert.equal(calls[0].body.members.length,3);assert.equal(calls[0].body.members[1].name,'Teammate 2');
  assert.equal(page.form().elements.name.value,declaredCaptain.name);
  assert.match(page.root.innerHTML,/<form id="claim-request-form"><fieldset disabled>/);
  // A delayed input/click from the old form cannot change the frozen request draft either.
  page.form().rows[0].querySelector('[name="memberSchool"]').value='Typed While Busy';page.form().oninput();reject(new Error('重复成员'));await flush();
  assert.match(page.root.innerHTML,/重复成员/);assert.equal(page.form().rows[0].querySelector('[name="memberSchool"]').value,'School 0');
  assert.doesNotMatch(page.root.innerHTML,/<form id="claim-request-form"><fieldset disabled>/);
  assert.equal(page.form().rows.length,3);
  page.form().rows[0].querySelector('[name="memberSchool"]').value='Corrected After Failure';page.form().oninput();
  page.form().onsubmit({preventDefault(){}});assert.equal(calls[1].body.members[0].school,'Corrected After Failure');
  reject(new Error('fixture retry failed'));await flush();
});

test('real receipt form recovers after refresh, edits teammates and submits expected revision with the same capability',async()=>{
  const calls=[];let revision=1,members=declaredMembers;
  const page=claimPageFixture(async(path,body)=>{calls.push({path,body});if(path.endsWith('update')){members=body.members;revision++;}return {...declaredResponse,requestRevision:revision,declaration:{captain:declaredCaptain,members}};});
  await page.render();page.fill();page.form().onsubmit({preventDefault(){}});await flush();assert.match(page.root.innerHTML,/First Teammate/);
  await page.render();assert.equal(calls.at(-1).path,'claims/status');assert.match(page.root.innerHTML,/First Teammate/);
  page.nodes.get('claim-edit').onclick();page.form().rows[0].querySelector('[name="memberSchool"]').value='Edited On Receipt';page.form().oninput();page.form().onsubmit({preventDefault(){}});await flush();
  assert.equal(calls.at(-1).path,'claims/update');assert.equal(calls.at(-1).body.expectedRevision,1);assert.equal(calls.at(-1).body.secret,receipt.secret);assert.match(page.root.innerHTML,/Edited On Receipt/);assert.ok(!page.nodes.has('claim-update-form'));
});

test('direct staff review shows private duplicate references and posts only reviewed team plus scoped context',()=>{
  const request={id:'declared-fixture',name:declaredCaptain.name,school:declaredCaptain.school,revision:3,status:'pending',reviewContext:'opaque-context',declaration:declaredResponse.declaration,
    references:[{id:'reference',name:declaredCaptain.name,school:'Other School',teamId:'assigned',notes:'Reference note',needsReview:true}]};
  const html=context.claimsReviewMarkup(request,{people:[],teams:[]});
  assert.match(html,/First Teammate · First School/);assert.match(html,/可能重复/);assert.match(html,/Other School/);assert.ok(!html.includes('name="pin"'));assert.ok(!html.includes('name="inPersonVerified"'));assert.match(html,/线上申请审核/);
  assert.match(html,/<select name="teamId"><option value="">创建新队伍（默认）/);assert.ok(!html.includes('data-claim-map'));assert.ok(!html.includes('data-copy-value'));
  const posts=[];
  const form={elements:{teamName:{value:'Reviewed Team'},track:{value:''},pin:{value:'123456'},note:{value:'Distinct people verified'},inPersonVerified:{checked:true},resolvedIdentity:{checked:true}}};
  const nodes={'claim-review':{innerHTML:''},'claim-review-form':form,'claim-reject':{}};
  const scope=vm.createContext({model:{requests:[request]},document:{getElementById:id=>nodes[id]},busy:false,live:()=>true,replaceEditor:()=>true,trackAdminForm:()=>{},claimsReviewMarkup:()=>'',mutate:(f,path,body)=>posts.push({path,body})});
  const handler=source.slice(source.indexOf('  function selectRequest(id){'),source.indexOf("  document.getElementById('claims-admin-load').onclick=load;"));
  vm.runInContext(handler+'\nselectRequest("declared-fixture");',scope);form.onsubmit({preventDefault(){}});
  assert.equal(posts[0].path,'admin/claims/approve');assert.equal(posts[0].body.reviewContext,'opaque-context');assert.equal(posts[0].body.expectedRevision,3);
  assert.equal(posts[0].body.teamName,'Reviewed Team');assert.equal(posts[0].body.track,'');assert.equal(posts[0].body.resolvedIdentity,true);assert.equal(posts[0].body.verificationMethod,'online');assert.equal(posts[0].body.applicationReviewed,true);assert.ok(!Object.hasOwn(posts[0].body,'pin'));assert.ok(!Object.hasOwn(posts[0].body,'inPersonVerified'));
  assert.ok(!Object.hasOwn(posts[0].body,'memberIds'));
});

test('successful first application cannot discard changes from enabled inputs because the entire draft is frozen',async()=>{
  const storage=new Storage();let resolve;const calls=[];
  const members=[...declaredMembers,{name:'Third Teammate',school:'Third School'}];
  const {client,states}=controller(storage,(path,body)=>{calls.push({path,body});return new Promise(done=>{resolve=done;});});
  client.start();const pending=client.apply(declaredCaptain.name,declaredCaptain.school,members);
  const frozen=context.claimStateMarkup(states.at(-1));
  assert.match(frozen,/<form id="claim-request-form"><fieldset disabled>/);
  assert.match(frozen,/<button[^>]+data-claim-remove=/);assert.match(frozen,/id="claim-member-add"/);
  assert.ok(frozen.indexOf('name="name"')>frozen.indexOf('<fieldset disabled>'));
  assert.ok(frozen.indexOf('id="claim-member-add"')<frozen.lastIndexOf('</fieldset></form>'));
  client.setDraft({name:'Later Captain',school:'Later School',members:declaredMembers});
  client.addMember();client.removeMember(0);await client.apply('Later Captain','Later School',declaredMembers);
  assert.equal(calls.length,1);assert.equal(states.at(-1).draft.name,declaredCaptain.name);
  assert.equal(states.at(-1).draft.members.length,3);
  resolve({...declaredResponse,declaration:{captain:declaredCaptain,members}});await pending;client.edit();
  assert.equal(states.at(-1).draft.name,declaredCaptain.name);
  assert.equal(JSON.stringify(states.at(-1).draft.members),JSON.stringify(members));
});

test('real form recovers a committed update whose response was lost without overwriting newer typing or auto-saving',async()=>{
  let revision=1,members=declaredMembers,dropReply=true;const calls=[];
  const page=claimPageFixture(async(path,body)=>{
    calls.push({path,body});
    if(path.endsWith('update')){
      if(body.expectedRevision!==revision){const error=new Error('申请已变化，请查询最新状态后重新核对');error.status=409;throw error;}
      members=body.members;revision++;
      if(dropReply){dropReply=false;throw new Error('网络中断，未收到保存结果');}
    }
    return {...declaredResponse,requestRevision:revision,declaration:{captain:declaredCaptain,members}};
  });
  page.scope.savedReceipt=receipt;vm.runInContext('claimReceiptStore().write(savedReceipt)',page.scope);
  await page.render();page.nodes.get('claim-edit').onclick();
  page.form().rows[0].querySelector('[name="memberSchool"]').value='Saved Before Response Was Lost';page.form().oninput();
  page.form().onsubmit({preventDefault(){}});await flush();assert.equal(revision,2);
  assert.match(page.root.innerHTML,/网络中断/);assert.match(page.root.innerHTML,/草稿已保留/);
  page.form().rows[0].querySelector('[name="memberSchool"]').value='Newer Unsaved Draft';page.form().oninput();
  await page.nodes.get('claim-status').onclick();
  page.form().onsubmit({preventDefault(){}});await flush();
  assert.equal(calls.filter(call=>call.path.endsWith('update')).length,1,'ordinary refresh must not silently rebase or retry');
  assert.equal(page.form().rows[0].querySelector('[name="memberSchool"]').value,'Newer Unsaved Draft');
  await page.nodes.get('claim-edit-reload').onclick();
  assert.match(page.root.innerHTML,/Saved Before Response Was Lost/);assert.match(page.root.innerHTML,/Newer Unsaved Draft/);
  assert.match(page.root.innerHTML,/保存会用草稿替换已保存名单/);
  const reads=calls.length;page.nodes.get('claim-edit-confirm').onclick();assert.equal(calls.length,reads,'confirmation only changes the edit base');
  assert.equal(page.form().rows[0].querySelector('[name="memberSchool"]').value,'Newer Unsaved Draft');
  page.form().onsubmit({preventDefault(){}});await flush();
  assert.equal(calls.at(-1).body.expectedRevision,2);assert.equal(members[0].school,'Newer Unsaved Draft');assert.equal(revision,3);
  assert.ok(!page.nodes.has('claim-update-form'));assert.ok(![...page.storage.map.values()].join('').includes('Newer Unsaved Draft'));
});

test('explicit review preserves both drafts and still rejects a concurrent change after confirmation',async()=>{
  const storage=new Storage();store(storage).write(receipt);let revision=1,members=declaredMembers;const calls=[];
  const {client,states}=controller(storage,async(path,body)=>{
    calls.push({path,body});
    if(path.endsWith('update')){
      if(body.expectedRevision!==revision){const error=new Error('申请已变化');error.status=409;throw error;}
      members=body.members;revision++;
    }
    return {...declaredResponse,requestRevision:revision,declaration:{captain:declaredCaptain,members}};
  });
  await client.start();client.edit();const mine=[{...declaredMembers[0],school:'My Draft'},declaredMembers[1]];
  revision=2;members=[{...declaredMembers[0],school:'Other Tab'},declaredMembers[1]];
  await client.update(mine);assert.equal(states.at(-1).needsReview,true);
  await client.reviewLatest();assert.equal(states.at(-1).declaration.members[0].school,'Other Tab');assert.equal(states.at(-1).draft.members[0].school,'My Draft');
  const before=calls.length;await client.update(mine);assert.equal(calls.length,before,'loading does not authorize a write');
  client.confirmReview();assert.equal(calls.length,before);
  revision=3;members=[{...declaredMembers[0],school:'Even Newer Other Tab'},declaredMembers[1]];
  await client.update(mine);assert.equal(calls.at(-1).body.expectedRevision,2);
  assert.equal(members[0].school,'Even Newer Other Tab');assert.equal(states.at(-1).draft.members[0].school,'My Draft');assert.equal(states.at(-1).needsReview,true);
  assert.equal(states.at(-1).reviewRevision,null);
});

test('late explicit review cannot revive cleared credentials or an abandoned editor',async()=>{
  for(const action of ['clear','cancelEdit','dispose']){
    const storage=new Storage();store(storage).write(receipt);let resolve;let defer=false;
    const {client,states}=controller(storage,()=>defer?new Promise(done=>{resolve=done;defer=false;}):Promise.resolve(declaredResponse));
    await client.start();client.edit();defer=true;const pending=client.reviewLatest();await client[action]();
    resolve({...declaredResponse,requestRevision:99});await pending;client.confirmReview();
    assert.ok(states.every(state=>state.reviewRevision!==99));
    if(action==='clear'){assert.equal(states.at(-1).receipt,null);assert.equal(storage.map.size,0);}
  }
});

test('team metadata uses public form values, survives refresh and requires explicit conflict confirmation',async()=>{
  const calls=[];let revision=1,teamName='Original Team',track='AI for Science';
  const page=claimPageFixture(async(path,body)=>{
    calls.push({path,body});
    if(path.endsWith('request')){teamName=body.teamName;track=body.track;}
    if(path.endsWith('update')){
      if(body.expectedRevision!==revision){const error=new Error('申请已变化');error.status=409;throw error;}
      teamName=body.teamName;track=body.track;revision++;
    }
    return {...declaredResponse,requestRevision:revision,declaration:{...declaredResponse.declaration,teamName,track}};
  });
  await page.render();page.fill();page.form().elements.teamName.value='My New Team';page.form().elements.track.value='未来学习样态';page.form().oninput();
  page.form().onsubmit({preventDefault(){}});await flush();assert.equal(calls[0].body.teamName,'My New Team');assert.equal(calls[0].body.track,'未来学习样态');
  await page.render();assert.match(page.root.innerHTML,/My New Team/);page.nodes.get('claim-edit').onclick();
  assert.equal(page.form().elements.teamName.value,'My New Team');assert.equal(page.form().elements.track.value,'未来学习样态');
  page.form().elements.teamName.value='My Draft';page.form().elements.track.value='';page.form().oninput();
  revision=2;teamName='Concurrent Team';track='城市与社区问题';
  page.form().onsubmit({preventDefault(){}});await flush();assert.equal(page.form().elements.teamName.value,'My Draft');
  await page.nodes.get('claim-edit-reload').onclick();assert.match(page.root.innerHTML,/Concurrent Team/);assert.equal(page.form().elements.teamName.value,'My Draft');
  assert.equal(page.form().elements.track.value,'');const before=calls.length;
  page.form().onsubmit({preventDefault(){}});await flush();assert.equal(calls.length,before);
  page.nodes.get('claim-edit-confirm').onclick();page.form().onsubmit({preventDefault(){}});await flush();
  assert.equal(calls.at(-1).body.expectedRevision,2);assert.equal(teamName,'My Draft');assert.equal(track,'');
  assert.ok(![...page.storage.map.values()].join('').includes('My Draft'));
});

test('typing only team metadata during a pending save is retained as unsaved draft',async()=>{
  const storage=new Storage();store(storage).write(receipt);let finish;let saving=false;
  const response={...declaredResponse,declaration:{...declaredResponse.declaration,teamName:'Initial',track:''}};
  const {client,states}=controller(storage,()=>saving?new Promise(resolve=>{finish=resolve;}):Promise.resolve(response));
  await client.start();client.edit();saving=true;
  const pending=client.update(declaredMembers,{teamName:'Sent',track:''});
  client.setDraft({...declaredCaptain,members:declaredMembers,teamName:'Typed while saving',track:'AI for Science'});
  finish({...response,requestRevision:2,declaration:{...response.declaration,teamName:'Sent'}});await pending;
  assert.equal(states.at(-1).editing,true);assert.equal(states.at(-1).draft.teamName,'Typed while saving');
  assert.equal(states.at(-1).draft.track,'AI for Science');assert.equal(states.at(-1).declaration.teamName,'Sent');
});

test('public and admin track choices match existing server tracks while blank remains allowed',()=>{
  const backend=fs.readFileSync(new URL('../server/admin_data.py',import.meta.url),'utf8');
  const tracks=JSON.parse(backend.match(/TRACK_OPTIONS=(\[[^\n]+\])/)[1]);
  const html=context.claimTeamMarkup({teamName:'A < B',track:''});
  for(const track of tracks)assert.ok(html.includes('value="'+track+'"'));
  assert.match(html,/待定/);assert.match(html,/A &lt; B/);assert.ok(!html.includes('<select name="track" required'));
  assert.match(html,/<input name="teamName" required/);
});

test('optional existing team selection displays registration and binds its displayed context without auto-selection',()=>{
  const request={id:'select-fixture',name:declaredCaptain.name,school:declaredCaptain.school,revision:2,status:'pending',reviewContext:'claim-context',declaration:declaredResponse.declaration};
  const team={id:'T-existing',number:7,name:'Existing <team>',project:'Preserved <project>',teamContext:'displayed-team-context',registration:{captain:'Old Captain',school:'Old School',members:['Old Captain','Member <two>'],revision:9}};
  const html=context.claimsReviewMarkup(request,{teams:[team],people:[]});
  assert.match(html,/<option value="">创建新队伍（默认）/);assert.ok(!html.includes('selected'));
  const summary=context.claimExistingTeamMarkup(team);assert.match(summary,/Old Captain/);assert.match(summary,/Old School/);assert.match(summary,/Member &lt;two&gt;/);
  assert.match(summary,/Preserved &lt;project&gt;/);assert.match(summary,/登记修订：9/);assert.match(summary,/以本次审核通过/);
  const posts=[],form={elements:{teamId:{value:''},teamName:{value:'Reviewed Team'},track:{value:''},pin:{value:'123456'},note:{value:'Verified'},inPersonVerified:{checked:true},resolvedIdentity:{checked:false}}};
  const nodes={'claim-review':{innerHTML:''},'claim-review-form':form,'claim-reject':{},'claim-existing-summary':{innerHTML:''}};
  const scope=vm.createContext({model:{requests:[request],teams:[team]},document:{getElementById:id=>nodes[id]},busy:false,live:()=>true,replaceEditor:()=>true,trackAdminForm:()=>{},claimsReviewMarkup:()=>'',claimExistingTeamMarkup:context.claimExistingTeamMarkup,mutate:(f,path,body)=>posts.push(body),formError:(f,error)=>{f.error=error;}});
  const handler=source.slice(source.indexOf('  function selectRequest(id){'),source.indexOf("  document.getElementById('claims-admin-load').onclick=load;"));
  vm.runInContext(handler+'\nselectRequest("select-fixture");',scope);
  form.onsubmit({preventDefault(){}});assert.ok(!Object.hasOwn(posts[0],'teamId'));assert.ok(!Object.hasOwn(posts[0],'teamContext'));
  form.elements.teamId.value=team.id;form.elements.teamId.onchange();assert.match(nodes['claim-existing-summary'].innerHTML,/Old Captain/);
  form.onsubmit({preventDefault(){}});
  assert.equal(posts[1].teamId,team.id);assert.equal(posts[1].teamContext,'displayed-team-context');assert.equal(posts[1].reviewContext,'claim-context');
  form.elements.teamId.value='missing';form.onsubmit({preventDefault(){}});assert.equal(posts.length,2);assert.match(form.error,/所选队伍已变化/);
  form.elements.teamId.value='';form.elements.teamId.onchange();assert.match(nodes['claim-existing-summary'].innerHTML,/默认批准后创建新队伍/);
});

test('automatic application and historical review compatibility require no public PIN or external message',()=>{
  const pending=context.claimStateMarkup({...declaredResponse,receipt,draft:{...declaredCaptain,members:declaredMembers},busy:false,error:''});
  assert.match(pending,/正在自动完成申请/);assert.match(pending,/较早的申请/);assert.doesNotMatch(pending,/等待工作人员审核|查询审核状态/);
  assert.ok(!pending.includes(receipt.pin));assert.ok(!pending.includes('id="claim-redeem"'));
  const review=context.claimsReviewMarkup({id:'online',name:declaredCaptain.name,school:declaredCaptain.school,status:'pending',revision:1,declaration:declaredResponse.declaration},{teams:[],people:[]});
  for(const html of [pending,review]){
    assert.doesNotMatch(html,/name="(?:pin|inPersonVerified)"/);assert.doesNotMatch(html,/现场|当面|私信|出示|六位/);
  }
  assert.match(review,/点击批准即确认已审核申报信息/);assert.match(review,/说明（选填/);
  for(const file of ['index.html','platform.js','platform-admin.js','platform.html','team-registration.js']){
    const text=fs.readFileSync(new URL('../'+file,import.meta.url),'utf8');
    assert.doesNotMatch(text,/申请现场核验|现场核验入口|携组员到现场组织方处核验/);
  }
});

test('trusted import uses displayed revision and preserves all source data',async()=>{
  const payload={autoReview:{expectedRevision:0,sourceName:'Synthetic relay',sourceSha256:'a'.repeat(64),entries:[{id:'one',name:'Example',school:'Original School',eligible:true,notes:'Preserve'}]}};
  const file={size:120,text:async()=>JSON.stringify(payload)},form={isConnected:true,elements:{file:{files:[file]}}},sent=[],errors=[];
  await context.importTrustedClaimsFile({form,file,expectedRevision:9,isCurrent:()=>true,submit:body=>sent.push(JSON.parse(JSON.stringify(body))),onError:error=>errors.push(error)});
  assert.deepEqual(sent,[{autoReview:{...payload.autoReview,expectedRevision:9}}]);assert.equal(payload.autoReview.expectedRevision,0);assert.deepEqual(errors,[]);
});

test('trusted import discards detached, refreshed, replaced-model, edited, busy and stale-session reads',async()=>{
  for(const change of ['detached','read','model','input','busy','session','file']){
    let resolve;const file={size:120,text:()=>new Promise(r=>{resolve=r;})},form={isConnected:true,elements:{file:{files:[file]}}};
    const original={};let model=original,read=4,input=2,busy=false,live=true;const sent=[],errors=[];
    const pending=context.importTrustedClaimsFile({form,file,expectedRevision:7,isCurrent:()=>model===original && read===4 && input===2 && !busy && live,submit:body=>sent.push(body),onError:error=>errors.push(error)});
    if(change==='detached')form.isConnected=false;
    if(change==='read')read++;
    if(change==='model')model={};
    if(change==='input')input++;
    if(change==='busy')busy=true;
    if(change==='session')live=false;
    if(change==='file')form.elements.file.files=[{}];
    resolve(JSON.stringify({autoReview:{expectedRevision:0,sourceName:'Synthetic',sourceSha256:'a'.repeat(64),entries:[]}}));await pending;
    assert.equal(sent.length,0,change);assert.deepEqual(errors,[],change);
  }
});
