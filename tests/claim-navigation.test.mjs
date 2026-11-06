import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const platform=fs.readFileSync(new URL('../platform.js',import.meta.url),'utf8').split("window.addEventListener('hashchange'")[0];
const claims=fs.readFileSync(new URL('../platform-claims.js',import.meta.url),'utf8');
const receipt={requestId:'a'.repeat(32),secret:'b'.repeat(43),pin:'123456',expiresAt:12345};
function fixture(role='admin'){
  const storage=new Map(),nodes=new Map(),requests=[],prompts=[];let choice=true;
  const node=id=>{if(!nodes.has(id))nodes.set(id,{id,isConnected:true,disabled:false,innerHTML:'',textContent:'',classList:{add(){},remove(){}}});return nodes.get(id);};
  const context=vm.createContext({document:{getElementById:node},location:{href:'https://example.test/platform.html#claim',hash:'claim'},URL,Intl,Date,TextEncoder,setTimeout:()=>0,clearTimeout,
    sessionStorage:{getItem:key=>storage.get(key)??null,setItem:(key,value)=>storage.set(key,value),removeItem:key=>storage.delete(key)},localStorage:{getItem:()=>null,setItem(){}},
    confirm:message=>{prompts.push(message);return choice;},transport:async(path)=>{requests.push(path);if(path==='team')return {team:{id:'OLD',name:'Old team'},loginId:'old-login'};return {};}});
  vm.runInContext(platform+'\n'+claims+`\nsession={role:'${role}',loginId:'old-login'};route='claim';routeVersion=1;api=transport;renderRoute=async()=>{};teamDraftStore=()=>({capturePurge(){},purge:()=>true});`,context);
  context.receipt=receipt;
  return {context,node,storage,requests,prompts,choose:value=>{choice=value;}};
}
test('initial claim response must be saved before navigation or logout can proceed',async()=>{
  const h=fixture();let finish;h.context.transportClaim=()=>new Promise(resolve=>{finish=resolve;});
  vm.runInContext("currentClaimController=makeClaimController({store:claimReceiptStore(),request:transportClaim,isCurrent:()=>true,onChange(){}});",h.context);
  const pending=vm.runInContext("currentClaimController.apply('Captain','School')",h.context);
  assert.equal(vm.runInContext('confirmLeave()',h.context),false);await vm.runInContext('logoutAccount({currentTarget:document.getElementById("logout")})',h.context);
  assert.deepEqual(h.requests,[]);assert.equal(h.storage.size,0);
  finish({...receipt,status:'pending'});await pending;assert.equal(h.storage.size,1);assert.equal(vm.runInContext('confirmLeave()',h.context),true);
});
test('unsaved first and edited claim drafts prompt and respect cancellation',()=>{
  const h=fixture();vm.runInContext("currentClaimController=makeClaimController({store:claimReceiptStore(),request:async()=>({}),isCurrent:()=>true,onChange(){}});currentClaimController.setDraft({name:'Unsaved',school:'School',teamName:'Team',track:'',members:emptyClaimMembers()});",h.context);
  h.choose(false);assert.equal(vm.runInContext('confirmLeave()',h.context),false);assert.equal(h.prompts.length,1);
  h.choose(true);assert.equal(vm.runInContext('confirmLeave()',h.context),true);
});
for(const role of ['admin','judge','team'])test(`approved claim handoff from old ${role} preserves capability but intentional logout clears it`,async()=>{
  const h=fixture(role);vm.runInContext('claimReceiptStore().write(receipt)',h.context);
  await vm.runInContext('openClaimWorkbench({preventDefault(){},currentTarget:document.getElementById("claim-workbench")})',h.context);
  assert.equal(h.context.location.hash,'team');assert.equal(h.storage.size,1);assert.ok(h.requests.includes('logout'));
  vm.runInContext(`session={role:'${role}',loginId:'new-login'}`,h.context);
  h.choose(false);await vm.runInContext('logoutAccount({currentTarget:document.getElementById("logout")})',h.context);assert.equal(h.storage.size,1);
  h.choose(true);await vm.runInContext('logoutAccount({currentTarget:document.getElementById("logout")})',h.context);assert.equal(h.storage.size,0);
});

test('pending declaration edits participate in both navigation confirmation and native unload warning',async()=>{
  const h=fixture();h.context.state={status:'pending',captain:{name:'Captain',school:'School'},declaration:{captain:{name:'Captain',school:'School'},members:[{name:'One',school:'A'},{name:'Two',school:'B'}]},requestRevision:1};
  vm.runInContext('claimReceiptStore().write(receipt);currentClaimController=makeClaimController({store:claimReceiptStore(),request:async()=>state,isCurrent:()=>true,onChange(){}});',h.context);
  await vm.runInContext('currentClaimController.start()',h.context);vm.runInContext("currentClaimController.edit();currentClaimController.setDraft({name:'Captain',school:'School',teamName:'Changed Team',track:'',members:state.declaration.members});",h.context);
  h.choose(false);assert.equal(vm.runInContext('confirmLeave()',h.context),false);
  let unload;h.context.window={addEventListener:(event,handler)=>{unload=handler;}};
  const full=fs.readFileSync(new URL('../platform.js',import.meta.url),'utf8');vm.runInContext(full.split('\n').find(line=>line.startsWith("window.addEventListener('beforeunload'")),h.context);
  const event={preventDefault(){this.prevented=true;}};unload(event);assert.equal(event.prevented,true);assert.equal(event.returnValue,'');
  assert.equal(h.storage.size,1);
});
