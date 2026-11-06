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
