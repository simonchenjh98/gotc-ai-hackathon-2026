import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const helper=fs.readFileSync(new URL('../platform-draft-recovery.js',import.meta.url),'utf8');
const source=fs.readFileSync(new URL('../platform.js',import.meta.url),'utf8').split("window.addEventListener('hashchange'")[0];
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};};
class Storage{map=new Map();get length(){return this.map.size;}key(i){return [...this.map.keys()][i] ?? null;}getItem(k){return this.map.get(k) ?? null;}setItem(k,v){this.map.set(k,v);}removeItem(k){this.map.delete(k);}}
function fixture(storage=new Storage()){
  const nodes=new Map(),calls=[],hooks={};let data={team:{id:'T1',number:1,name:'One',project:''},submission:{version:0,draft:null,submitted:null,submittedVersion:null},window:{canEdit:true,submissionDeadline:null},judgingMaterials:{}};
  function node(id){if(nodes.has(id))return nodes.get(id);const item={id,isConnected:true,hidden:false,value:'',checked:false,disabled:false,readOnly:false,type:'text',dataset:{},listeners:{},classList:{add(){},remove(){}},focus(){},scrollIntoView(){},addEventListener(event,fn){this.listeners[event]=fn;},querySelector(){return null;},querySelectorAll(selector){return (this.buttons || []).filter(button=>selector===`[data-${button.attribute}]`);}};let html='';Object.defineProperty(item,'innerHTML',{configurable:true,get:()=>html,set(value){html=value;for(const match of value.matchAll(/id="([^"]+)"/g))node(match[1]);item.buttons=[...value.matchAll(/data-(local-view|local-discard)="(\d+)"/g)].map(match=>({attribute:match[1],disabled:false,isConnected:true,dataset:{[match[1]==='local-view'?'localView':'localDiscard']:match[2]}}));}});nodes.set(id,item);return item;}
  const root=node('platform-main'),setRoot=Object.getOwnPropertyDescriptor(root,'innerHTML').set;
  Object.defineProperty(root,'innerHTML',{get:()=>root.html || '',set(value){for(const n of nodes.values())if(n!==root)n.isConnected=false;nodes.clear();nodes.set(root.id,root);root.html=value;setRoot(value);const form=node('submission-form');form.elements={};for(const key of ['projectName','summary','codeUrl','codeVersion','readmeUrl','license','licenseUrl','demoUrl','demoInstructions','slidesUrl','videoUrl','sourcesAndLimitations','confirmed']){const field=node('material-'+key);field.value=data.submission.draft?.[key] || '';field.checked=key==='confirmed'?!!data.submission.draft?.confirmed:false;field.type=key==='confirmed'?'checkbox':'text';form.elements[key]=field;}const fieldset=node('fieldset');form.querySelector=selector=>selector==='fieldset'?fieldset:selector==='.form-error'?node('form-error'):null;form.querySelectorAll=selector=>selector==='input,textarea'?Object.values(form.elements):selector==='button'?[node('save-submission-draft')]:[];node('submission-preview').querySelectorAll=selector=>selector==='button'?[node('confirm-submission'),node('cancel-submission-preview')]:[];}});
  const response=(body,status=200)=>({ok:status<400,status,json:async()=>structuredClone(body)});
  const fetch=async(url,options={})=>{const path=new URL(url).pathname.split('/api/')[1],body=options.body?JSON.parse(options.body):undefined;calls.push({path,body,method:options.method,headers:options.headers});if(hooks[path])return hooks[path](body);if(path==='team')return response(data);if(path==='logout')return response({ok:true});if(path==='submission'){const nextVersion=data.submission.version+1;data={...data,submission:{...data.submission,version:nextVersion,draft:body.materials,submitted:body.status==='submitted'?body.materials:data.submission.submitted,submittedVersion:body.status==='submitted'?nextVersion:data.submission.submittedVersion,submittedAt:1234,updatedAt:1234}};return response({ok:true,...data});}throw new Error(path);};
  const context=vm.createContext({document:{getElementById:node},location:{href:'https://example.test/gotc/platform.html#team'},URL,TextEncoder,Intl,Date,Math,setTimeout,clearTimeout,AbortController,localStorage:storage,fetch,confirm:()=>true});vm.runInContext(helper,context);vm.runInContext(source+'\nrouteVersion=1;route="team";session={role:"team"};globalThis.render=()=>renderTeam(1);globalThis.current=()=>currentTeamRecovery;globalThis.establish=login=>{publishLogin(login);session=login;};globalThis.invokeApi=api;globalThis.dirty=()=>submissionDirty;globalThis.logout=()=>{account();return document.getElementById("logout").listeners.click();};globalThis.seed=(teamId,summary,baseVersion=0,state="editing",at=Date.now())=>{const fields=materialFields.map(([key,,limit])=>[key,limit]),materials=Object.fromEntries(fields.map(([key])=>[key,key==="summary"?summary:""]));return GotcDraftRecovery.create({storage:()=>localStorage,namespace:base.pathname,teamId,baseVersion,baseMaterials:Object.fromEntries(fields.map(([key])=>[key,""])),fields,now:()=>at}).write(materials,state,state==="editing"?null:"draft");};renderRoute=async()=>{};',context);
  return {context,storage,nodes,node,root,calls,hooks,response,get data(){return data;},set data(next){data=next;},edit(key,value,event='input'){node('submission-form').elements[key].value=value;node('submission-form').listeners[event]();},inspect(index=0){return node('local-draft-candidates').querySelectorAll('[data-local-view]')[index].onclick();},slots(){return [...storage.map.values()].filter(value=>value.startsWith('{')).map(value=>JSON.parse(value));}};
}
test('authorized same-version recovery preserves raw strings, clears confirmation and never sends a POST',async()=>{
  const f=fixture();f.context.seed('T1','  local\nraw  ');await f.context.render();assert.equal(f.slots().length,1);assert.ok(!f.node('local-draft-candidates').innerHTML.includes('副本编号'));await f.inspect();assert.match(f.node('local-restore-actions').innerHTML,/confirm-local-restore/);await f.node('confirm-local-restore').onclick({currentTarget:f.node('confirm-local-restore')});assert.equal(f.node('material-summary').value,'  local\nraw  ');assert.equal(f.node('material-confirmed').checked,false);assert.equal(f.calls.filter(c=>c.body).length,0);assert.equal(f.slots().length,2);assert.equal(f.context.dirty(),true);
});
test('server-version changes and closed windows only offer comparison/copy instead of whole-form restore',async()=>{
  const f=fixture();f.context.seed('T1','LOCAL');f.data={...f.data,submission:{...f.data.submission,version:1,draft:{summary:'TEAMMATE'}}};await f.context.render();await f.inspect();assert.match(f.node('local-draft-comparison').innerHTML,/服务器版本已更新/);assert.match(f.node('local-draft-comparison').innerHTML,/TEAMMATE/);assert.ok(!f.node('local-restore-actions').innerHTML.includes('confirm-local-restore'));
  f.data.window.canEdit=false;await f.inspect();assert.match(f.node('local-draft-comparison').innerHTML,/提交窗口只读/);assert.equal(f.node('fieldset').disabled,true);assert.equal(f.calls.filter(c=>c.body).length,0);
});
test('new document candidate identities remain isolated across teams and unauthorized access hides candidates',async()=>{
  const storage=new Storage(),f=fixture(storage);f.context.seed('T1','PRIVATE_ONE');f.context.seed('T2','PRIVATE_TWO');await f.context.render();assert.match(f.node('local-draft-candidates').innerHTML,/本地副本/);await f.inspect();assert.ok(!f.node('local-draft-comparison').innerHTML.includes('PRIVATE_TWO'));f.hooks.team=()=>f.response({error:'expired'},401);await assert.rejects(f.context.invokeApi('team'));assert.equal(f.node('local-draft-recovery').hidden,true);assert.equal(f.slots().length,2);
});
test('successful save clears only the sent slot, keeps source candidate and does not become unknown on cleanup failure',async()=>{
  const f=fixture();f.context.seed('T1','SOURCE');await f.context.render();f.edit('summary','NEW');await f.node('save-submission-draft').onclick();assert.deepEqual(f.slots().map(record=>record.materials.summary),['SOURCE']);assert.equal(f.data.submission.version,1);
  f.edit('summary','NEXT');f.storage.removeItem=()=>{throw new Error('remove blocked');};await f.node('save-submission-draft').onclick();assert.equal(f.data.submission.version,2);assert.match(f.node('local-draft-status').textContent,/服务器已成功保存/);assert.ok(!f.node('form-error').textContent?.includes('结果待确认'));
});
test('unknown result cannot be downgraded by input/flush or a previously displayed recovery confirmation',async()=>{
  const f=fixture();f.context.seed('T1','SOURCE');await f.context.render();await f.inspect();f.edit('summary','SENT');const waiting=deferred();f.hooks.submission=()=>waiting.promise;const saving=f.node('save-submission-draft').onclick();await f.node('confirm-local-restore').onclick({currentTarget:f.node('confirm-local-restore')});assert.equal(f.node('material-summary').value,'SENT');waiting.reject(new TypeError('lost response'));await saving;f.context.current().input();f.context.current().flush();assert.equal(f.slots().find(record=>record.materials.summary==='SENT').state,'unknown');await f.inspect();assert.ok(!f.node('local-restore-actions').innerHTML.includes('confirm-local-restore'));assert.equal(f.calls.filter(c=>c.path==='submission').length,1);
});
test('409 preserves the exact local conflict record',async()=>{
  const f=fixture();await f.context.render();f.edit('summary','LOCAL_CONFLICT');f.hooks.submission=()=>f.response({error:'版本冲突'},409);await f.node('save-submission-draft').onclick();assert.equal(f.slots()[0].state,'conflict');assert.equal(f.slots()[0].materials.summary,'LOCAL_CONFLICT');
});
test('late POST success cannot repaint a newer same-route form or remove its local slot',async()=>{
  const f=fixture();await f.context.render();f.edit('summary','OLD_SEND');const waiting=deferred();f.hooks.submission=()=>waiting.promise;const saving=f.node('save-submission-draft').onclick();await f.context.render();f.edit('summary','NEW_VIEW');waiting.resolve(f.response({...f.data,submission:{...f.data.submission,version:1}}));await saving;assert.equal(f.node('material-summary').value,'NEW_VIEW');assert.equal(f.context.dirty(),true);assert.ok(f.slots().some(record=>record.materials.summary==='NEW_VIEW'));
});
test('late old 403 cannot lock a new authorized recovery context',async()=>{
  const f=fixture();await f.context.render();const waiting=deferred();f.hooks.old=()=>waiting.promise;const old=f.context.invokeApi('old');await f.context.render();waiting.resolve(f.response({error:'old denial'},403));await assert.rejects(old);assert.equal(f.node('local-draft-recovery').hidden,false);assert.equal(f.node('fieldset').disabled,false);
});
test('candidate inspection uses the latest choice when two authorized reads return out of order',async()=>{
  const f=fixture();f.context.seed('T1','FIRST',0,'editing',Date.now()-2000);f.context.seed('T1','SECOND',0,'editing',Date.now()-1000);await f.context.render();const a=deferred(),b=deferred();let reads=0;f.hooks.team=()=>++reads===1?a.promise:b.promise;const first=f.inspect(1),second=f.inspect(0);b.resolve(f.response(f.data));await second;assert.match(f.node('local-draft-comparison').innerHTML,/SECOND/);a.resolve(f.response(f.data));await first;assert.match(f.node('local-draft-comparison').innerHTML,/SECOND/);assert.ok(!f.node('local-draft-comparison').innerHTML.includes('FIRST'));
});
test('explicit logout from board without a recovery context clears only the freshly authorized team',async()=>{
  const f=fixture();f.context.seed('T1','ONE');f.context.seed('T2','TWO');await f.context.logout();assert.deepEqual(f.slots().map(record=>record.teamId),['T2']);assert.deepEqual(f.calls.map(call=>call.path),['team','logout']);
});
test('logout after a cookie team switch clears the actual current team and logout failure retains copies',async()=>{
  const f=fixture();f.context.seed('T1','ONE');f.context.seed('T2','TWO');await f.context.render();f.data={...f.data,team:{id:'T2',number:2,name:'Two'}};await f.context.logout();assert.deepEqual(f.slots().map(record=>record.teamId),['T1']);
  const failed=fixture();failed.context.seed('T1','KEEP');failed.hooks.logout=()=>failed.response({error:'failed'},500);await failed.context.logout();assert.equal(failed.slots().length,1);
});
test('quota failure still permits manual server draft save and same-value change does not renew the local revision',async()=>{
  const f=fixture();await f.context.render();f.edit('summary','CHANGE','change');const before=f.slots()[0];f.node('submission-form').listeners.change();assert.equal(f.slots()[0].localRevision,before.localRevision);f.storage.setItem=()=>{throw new Error('quota');};f.edit('summary','MANUAL');await f.node('save-submission-draft').onclick();assert.equal(f.data.submission.draft.summary,'MANUAL');assert.equal(f.calls.filter(c=>c.path==='submission').length,1);
});
test('logout is single-flight and an old response cannot purge or replace a later authorized view',async()=>{
  const f=fixture();await f.context.render();f.edit('summary','OLD_SESSION');const waiting=deferred();f.hooks.logout=()=>waiting.promise;const first=f.context.logout();await new Promise(resolve=>setImmediate(resolve));await f.context.logout();assert.equal(f.calls.filter(call=>call.path==='logout').length,1);
  vm.runInContext('session={role:"team"};',f.context);await f.context.render();f.edit('summary','NEW_AUTH_INPUT');waiting.resolve(f.response({ok:true}));await first;assert.equal(vm.runInContext('session.role',f.context),'team');assert.equal(f.node('material-summary').value,'NEW_AUTH_INPUT');assert.ok(f.slots().some(record=>record.materials.summary==='NEW_AUTH_INPUT'));
});
test('continued input after sending survives known success and uses the response version as its next baseline',async()=>{
  const f=fixture();await f.context.render();f.edit('summary','SENT');const waiting=deferred();f.hooks.submission=()=>waiting.promise;const saving=f.node('save-submission-draft').onclick();f.edit('summary','CONTINUED');waiting.resolve(f.response({...f.data,submission:{...f.data.submission,version:1,draft:{summary:'SENT'}}}));await saving;assert.equal(f.node('material-summary').value,'CONTINUED');assert.equal(f.context.dirty(),true);assert.equal(f.slots().find(record=>record.materials.summary==='CONTINUED').baseVersion,1);delete f.hooks.submission;await f.node('save-submission-draft').onclick();assert.equal(f.calls.filter(call=>call.path==='submission').at(-1).body.expectedVersion,1);
});

test('same-session route reconstruction keeps logout single-flight and completes authorized cleanup',async()=>{
  const f=fixture();await f.context.render();f.edit('summary','FIRST_VIEW');const waiting=deferred();f.hooks.logout=()=>waiting.promise;const leaving=f.context.logout();await new Promise(resolve=>setImmediate(resolve));await f.context.render();f.edit('summary','SAME_SESSION_NEW_VIEW');await f.context.logout();assert.equal(f.calls.filter(call=>call.path==='logout').length,1);waiting.resolve(f.response({ok:true}));await leaving;assert.equal(f.slots().length,0);assert.equal(vm.runInContext('session.role',f.context),null);
});
for(const delivered of [false,true])test(`another tab logout revokes a pending successful save even when storage event ${delivered?'has arrived':'is delayed'}`,async()=>{
  const storage=new Storage(),a=fixture(storage),b=fixture(storage);await a.context.render();await b.context.render();b.edit('summary','SENT_BEFORE_LOGOUT');const original=b.context.current(),form=b.node('submission-form'),waiting=deferred();b.hooks.submission=()=>waiting.promise;const saving=b.node('save-submission-draft').onclick();await a.context.logout();if(delivered)original.storageChanged();assert.equal(b.slots().length,0);
  waiting.resolve(b.response({...b.data,submission:{...b.data.submission,version:1,draft:{summary:'SENT_BEFORE_LOGOUT'}}}));await saving;assert.equal(b.context.current(),original,'late success must not create a new recovery context in the logout epoch');assert.equal(b.node('submission-form'),form);assert.equal(b.node('fieldset').disabled,true);assert.equal(b.node('local-draft-recovery').hidden,true);b.edit('summary','OLD_TAB_AFTER_LOGOUT');b.context.current().flush();assert.equal(b.slots().length,0,'the old tab cannot create a copy in the logout epoch');assert.equal(b.calls.filter(call=>call.path==='submission').length,1);
});
for(const status of [409,500])test(`late ${status} response after another tab logout keeps the old context locked`,async()=>{
  const storage=new Storage(),a=fixture(storage),b=fixture(storage);await a.context.render();await b.context.render();b.edit('summary','SENT');const waiting=deferred();b.hooks.submission=()=>waiting.promise;const saving=b.node('save-submission-draft').onclick();await a.context.logout();waiting.resolve(b.response({error:'late error'},status));await saving;assert.equal(b.node('fieldset').disabled,true);assert.equal(b.node('local-draft-recovery').hidden,true);assert.equal(b.node('submission-conflict').innerHTML,'');b.edit('summary','MUST_NOT_WRITE');assert.equal(b.slots().length,0);
});
test('a latest-record query started before another tab logout cannot offer a fresh editing context',async()=>{
  const storage=new Storage(),a=fixture(storage),b=fixture(storage);await a.context.render();await b.context.render();b.edit('summary','SENT');b.hooks.submission=()=>{throw new TypeError('lost response');};await b.node('save-submission-draft').onclick();const waiting=deferred(),original=b.context.current();b.hooks.team=()=>waiting.promise;const reading=b.node('load-latest-submission').onclick({currentTarget:b.node('load-latest-submission')});await a.context.logout();waiting.resolve(b.response({...b.data,submission:{...b.data.submission,version:1,draft:{summary:'SAVED'}}}));await reading;assert.equal(b.context.current(),original);assert.equal(b.node('latest-submission').innerHTML,'');assert.equal(b.node('fieldset').disabled,true);assert.equal(b.node('local-draft-recovery').hidden,true);assert.equal(b.slots().length,0);
});
test('a latest-record restart button cannot reuse authorization revoked after the query completed',async()=>{
  const storage=new Storage(),a=fixture(storage),b=fixture(storage);await a.context.render();await b.context.render();b.edit('summary','SENT');b.hooks.submission=()=>{throw new TypeError('lost');};await b.node('save-submission-draft').onclick();await b.node('load-latest-submission').onclick({currentTarget:b.node('load-latest-submission')});const original=b.context.current();await a.context.logout();await b.node('restart-submission').onclick();assert.equal(b.context.current(),original);assert.equal(b.node('fieldset').disabled,true);b.edit('summary','MUST_NOT_WRITE');assert.equal(b.slots().length,0);
});
test('explicit new same-team login accepts the new epoch while a prior pending response cannot touch its copy',async()=>{
  const storage=new Storage(),a=fixture(storage),b=fixture(storage);await a.context.render();await b.context.render();b.edit('summary','OLD_PENDING');const waiting=deferred();b.hooks.submission=()=>waiting.promise;const saving=b.node('save-submission-draft').onclick();await a.context.logout();vm.runInContext('session={role:"team"};',a.context);await a.context.render();a.edit('summary','NEW_LOGIN_INPUT');const snapshot=a.slots()[0];waiting.resolve(b.response({...b.data,submission:{...b.data.submission,version:1,draft:{summary:'OLD_PENDING'}}}));await saving;assert.deepEqual(a.slots(),[snapshot]);assert.equal(a.node('fieldset').disabled,false);a.edit('summary','NEW_LOGIN_CAN_CONTINUE');assert.equal(a.slots()[0].materials.summary,'NEW_LOGIN_CAN_CONTINUE');assert.equal(b.node('fieldset').disabled,true);
});
test('a team GET begun by an existing authorization cannot acquire the logout epoch during render',async()=>{
  const storage=new Storage(),a=fixture(storage),b=fixture(storage);await a.context.render();await b.context.render();const original=b.context.current(),form=b.node('submission-form'),waiting=deferred();b.hooks.team=()=>waiting.promise;const rendering=b.context.render();await a.context.logout();waiting.resolve(b.response(b.data));await rendering;assert.equal(b.context.current(),original);assert.equal(b.node('submission-form'),form);assert.equal(b.node('fieldset').disabled,true);b.edit('summary','NO_NEW_EPOCH_COPY');assert.equal(b.slots().length,0);
});
test('normal route reconstruction can reuse valid authorization after its prior form is detached',async()=>{
  const f=fixture();await f.context.render();const original=f.context.current();f.node('submission-form').isConnected=false;vm.runInContext('routeVersion=2;',f.context);await vm.runInContext('renderTeam(2)',f.context);assert.notEqual(f.context.current(),original);assert.equal(f.node('fieldset').disabled,false);f.edit('summary','NORMAL_ROUTE_RETURN');assert.equal(f.slots()[0].materials.summary,'NORMAL_ROUTE_RETURN');
});
test('unavailable storage is not logout and still permits manual server saving',async()=>{
  const storage=new Storage();storage.getItem=()=>{throw new Error('storage denied');};const f=fixture(storage);await f.context.render();f.edit('summary','MANUAL_SERVER_SAVE');await f.node('save-submission-draft').onclick();assert.equal(f.calls.filter(call=>call.path==='submission').length,1);assert.equal(f.data.submission.draft.summary,'MANUAL_SERVER_SAVE');assert.equal(f.node('fieldset').disabled,false);assert.equal(f.node('local-draft-recovery').hidden,false);
});
test('the first team GET is bound to epochs captured before the response, even without a prior context',async()=>{
  const storage=new Storage(),a=fixture(storage),b=fixture(storage);await a.context.render();const waiting=deferred();b.hooks.team=()=>waiting.promise;const rendering=b.context.render();await a.context.logout();waiting.resolve(b.response(b.data));await rendering;assert.equal(b.node('fieldset').disabled,true);assert.equal(b.node('local-draft-recovery').hidden,true);assert.equal(b.context.current().store.epoch,'');b.edit('summary','FIRST_GET_MUST_NOT_ADOPT_LOGOUT_EPOCH');assert.equal(b.slots().length,0);
});
test('the revoked tab may establish a new context only after explicit same-team login',async()=>{
  const storage=new Storage(),a=fixture(storage),b=fixture(storage);await a.context.render();await b.context.render();const old=b.context.current();await a.context.logout();old.storageChanged();await b.context.render();assert.equal(b.context.current(),old);vm.runInContext('session={role:"team"};',b.context);await b.context.render();assert.notEqual(b.context.current(),old);assert.equal(b.node('fieldset').disabled,false);b.edit('summary','EXPLICIT_RELOGIN');assert.equal(b.slots()[0].materials.summary,'EXPLICIT_RELOGIN');
});

for(const delivered of [false,true])test(`late logout cannot revoke a new same-team login when storage event ${delivered?'arrived':'is delayed'}`,async()=>{
  const storage=new Storage(),a=fixture(storage),b=fixture(storage);
  a.context.establish({role:'team',loginId:'old-login'});
  b.context.establish({role:'team',loginId:'old-login'});
  await a.context.render();await b.context.render();a.edit('summary','OLD_LOGIN');
  const waiting=deferred();a.hooks.logout=()=>waiting.promise;const leaving=a.context.logout();
  await new Promise(resolve=>setImmediate(resolve));assert.equal(a.calls.filter(call=>call.path==='logout').length,1,'logout was dispatched before new login');
  b.context.establish({role:'team',loginId:'new-login'});await b.context.render();b.edit('summary','NEW_LOGIN_DRAFT');const snapshot=b.slots().find(record=>record.materials.summary==='NEW_LOGIN_DRAFT');
  waiting.resolve(a.response({ok:true}));await leaving;if(delivered)b.context.current().storageChanged();
  assert.ok(b.slots().some(record=>JSON.stringify(record)===JSON.stringify(snapshot)),'new login slot survives late logout');
  assert.equal(b.node('material-summary').value,'NEW_LOGIN_DRAFT');assert.equal(b.node('fieldset').disabled,false);
  b.edit('summary','NEW_LOGIN_CONTINUES');await b.node('save-submission-draft').onclick();
  assert.equal(b.data.submission.draft.summary,'NEW_LOGIN_CONTINUES');assert.equal(b.node('fieldset').disabled,false);
});

test('explicit login stops a previously independent empty old tab even with delayed storage events',async()=>{
  const storage=new Storage(),c=fixture(storage),a=fixture(storage);c.context.establish({role:'team',loginId:'independent-old'});await c.context.render();
  a.context.establish({role:'team',loginId:'current-login'});await a.context.render();a.edit('summary','CURRENT');await a.context.logout();
  c.edit('summary','OLD_TAB_MUST_NOT_RECREATE');await c.context.render();assert.equal(c.slots().length,0);assert.equal(c.node('fieldset').disabled,true);await c.node('save-submission-draft').onclick();assert.equal(c.calls.filter(call=>call.path==='submission').length,0);
});
test('ordinary session reads do not publish a new explicit login boundary',async()=>{
  const storage=new Storage(),a=fixture(storage),b=fixture(storage),waiting=deferred();b.hooks.session=()=>waiting.promise;const reading=b.context.invokeApi('session');a.context.establish({role:'team',loginId:'new-login'});waiting.resolve(b.response({role:'team',loginId:'stale-login'}));
  const restored=await reading;vm.runInContext('session={role:"team",loginId:"stale-login"};',b.context);
  assert.equal(restored.loginId,'stale-login');assert.equal([...storage.map.values()][0],'new-login');await b.context.render();assert.equal(b.context.current(),null);assert.equal([...storage.map.values()][0],'new-login');
});
test('normal login logout removes legacy and current drafts, retains other teams, and terminal old login cannot reopen',async()=>{
  const f=fixture();f.context.seed('T1','LEGACY');f.context.seed('T2','OTHER_TEAM');f.context.establish({role:'team',loginId:'normal-login'});await f.context.render();f.edit('summary','CURRENT_LOGIN');await f.context.logout();
  assert.deepEqual(f.slots().map(record=>record.teamId),['T2']);vm.runInContext('session={role:"team",loginId:"normal-login"};',f.context);await f.context.render();assert.equal(f.node('fieldset').disabled,true);f.edit('summary','NO_REOPEN');assert.deepEqual(f.slots().map(record=>record.teamId),['T2']);
});
test('private requests carry the expected public login identity and failed logout preserves copies',async()=>{
  const f=fixture();f.context.establish({role:'team',loginId:'failed-login'});await f.context.render();f.edit('summary','KEEP');f.hooks.logout=()=>f.response({error:'failed'},500);await f.context.logout();
  assert.equal(f.slots().length,1);assert.equal(f.calls.filter(call=>call.path==='logout')[0].headers['X-GOTC-Login'],'failed-login');assert.equal(f.node('fieldset').disabled,false);
});

test('quota blocks publishing the new owner but server-confirmed login can still edit and manually save',async()=>{
  const storage=new Storage(),f=fixture(storage);f.context.establish({role:'team',loginId:'old-owner'});storage.setItem=()=>{throw new Error('quota exceeded');};
  f.context.establish({role:'team',loginId:'new-quota-login'});f.hooks.session=()=>f.response({role:'team',loginId:'new-quota-login'});await f.context.invokeApi('session');await f.context.render();f.edit('summary','MANUAL_AFTER_QUOTA_LOGIN');await f.node('save-submission-draft').onclick();
  assert.equal(f.node('fieldset').disabled,false);assert.equal(f.data.submission.draft.summary,'MANUAL_AFTER_QUOTA_LOGIN');assert.equal(f.slots().length,0);
});

test('confirmed old logout still cleans its bound copies after an intervening 401 changes the local session',async()=>{
  const f=fixture();f.context.establish({role:'team',loginId:'exiting-login'});await f.context.render();f.edit('summary','SENSITIVE_OLD_COPY');const waiting=deferred();f.hooks.logout=()=>waiting.promise;const leaving=f.context.logout();await new Promise(resolve=>setImmediate(resolve));
  f.hooks.team=()=>f.response({error:'revoked'},401);await assert.rejects(f.context.invokeApi('team'));waiting.resolve(f.response({ok:true}));await leaving;assert.equal(f.slots().length,0);
});
test('exit-and-switch uses the same confirmed team cleanup flow',async()=>{
  const f=fixture();f.context.establish({role:'team',loginId:'switch-login'});await f.context.render();f.edit('summary','SENSITIVE_SWITCH');vm.runInContext('renderWrongRole("judge");',f.context);await f.node('switch-role').onclick();assert.equal(f.slots().length,0);assert.equal(f.calls.filter(call=>call.path==='logout').length,1);
});

test('a storage notification during read failure keeps manual server saving available',async()=>{
  const f=fixture();f.context.establish({role:'team',loginId:'temporarily-unavailable'});await f.context.render();f.storage.getItem=()=>{throw new Error('temporary read failure');};f.context.current().storageChanged();f.edit('summary','SERVER_ONLY');await f.node('save-submission-draft').onclick();assert.equal(f.node('fieldset').disabled,false);assert.equal(f.data.submission.draft.summary,'SERVER_ONLY');
});
