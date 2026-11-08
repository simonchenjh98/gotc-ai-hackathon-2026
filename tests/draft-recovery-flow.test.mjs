import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const helper=fs.readFileSync(new URL('../platform-draft-recovery.js',import.meta.url),'utf8');
const source=fs.readFileSync(new URL('../platform.js',import.meta.url),'utf8').split("window.addEventListener('hashchange'")[0];
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};};
class Storage{map=new Map();get length(){return this.map.size;}key(i){return [...this.map.keys()][i] ?? null;}getItem(k){return this.map.get(k) ?? null;}setItem(k,v){this.map.set(k,v);}removeItem(k){this.map.delete(k);}}
function fixture(storage=new Storage(),options={}){
  const nodes=new Map(),calls=[],hooks={},consumedReceipts=[];let data={team:{id:'T1',number:1,name:'One',project:''},submission:{version:0,draft:null,submitted:null,submittedVersion:null},window:{canEdit:true,submissionDeadline:null},judgingMaterials:{},...(options.repository?{repository:options.repository}:{})};
  function node(id){if(nodes.has(id))return nodes.get(id);const item={id,isConnected:true,hidden:false,value:'',checked:false,disabled:false,readOnly:false,type:'text',dataset:{},listeners:{},classList:{add(){},remove(){}},focus(){},scrollIntoView(){},insertAdjacentHTML(position,value){this.innerHTML=position==='afterbegin'?value+this.innerHTML:this.innerHTML+value;},addEventListener(event,fn){this.listeners[event]=fn;},querySelector(){return null;},querySelectorAll(selector){return (this.buttons || []).filter(button=>selector===`[data-${button.attribute}]`);}};let html='';Object.defineProperty(item,'innerHTML',{configurable:true,get:()=>html,set(value){html=value;for(const match of value.matchAll(/id="([^"]+)"/g))node(match[1]);item.buttons=[...value.matchAll(/data-(local-view|local-discard)="(\d+)"/g)].map(match=>({attribute:match[1],disabled:false,isConnected:true,dataset:{[match[1]==='local-view'?'localView':'localDiscard']:match[2]}}));}});nodes.set(id,item);return item;}
  const root=node('platform-main'),setRoot=Object.getOwnPropertyDescriptor(root,'innerHTML').set;
  Object.defineProperty(root,'innerHTML',{get:()=>root.html || '',set(value){for(const n of nodes.values())if(n!==root)n.isConnected=false;nodes.clear();nodes.set(root.id,root);root.html=value;setRoot(value);const form=node('submission-form');form.elements={};const formHtml=value.match(/<form id="submission-form"[\s\S]*?<\/form>/)?.[0] || '';for(const key of ['projectName','summary','codeUrl','codeVersion','readmeUrl','license','licenseUrl','demoUrl','demoInstructions','slidesUrl','filesUrl','videoUrl','sourcesAndLimitations','confirmed']){if(!formHtml.includes(`name="${key}"`))continue;const field=node('material-'+key);field.value=data.submission.draft?.[key] || '';field.checked=key==='confirmed'?!!data.submission.draft?.confirmed:false;field.type=key==='confirmed'?'checkbox':'text';form.elements[key]=field;}const fieldset=node('fieldset');form.querySelector=selector=>selector==='fieldset'?fieldset:selector==='.form-error'?node('form-error'):null;form.querySelectorAll=selector=>selector==='input,textarea'?Object.values(form.elements):selector==='button'?[node('save-submission-draft')]:[];node('submission-preview').querySelectorAll=selector=>selector==='button'?[node('confirm-submission'),node('cancel-submission-preview')]:[];}});
  const response=(body,status=200)=>({ok:status<400,status,json:async()=>{if(body?.receipt?.status==='succeeded')consumedReceipts.push(body.receipt.requestId);return structuredClone(body);}});
  const fetch=async(url,options={})=>{const path=new URL(url).pathname.split('/api/')[1],body=options.body?(path.startsWith('team/uploads/')?options.body:JSON.parse(options.body)):undefined;calls.push({path,body,method:options.method,headers:options.headers,credentials:options.credentials});if(hooks[path])return hooks[path](body);if(path==='team')return response(data);if(path==='logout')return response({ok:true});if(path==='submission'){const nextVersion=data.submission.version+1;data={...data,submission:{...data.submission,version:nextVersion,draft:body.materials,submitted:body.status==='submitted'?body.materials:data.submission.submitted,submittedVersion:body.status==='submitted'?nextVersion:data.submission.submittedVersion,submittedAt:1234,updatedAt:1234}};return response({ok:true,...data,receipt:{requestId:body.requestId,status:'succeeded',action:body.status,baseVersion:body.expectedVersion,version:nextVersion,savedAt:1234,materials:body.materials,historyVersion:body.status==='submitted'?nextVersion:null}});}throw new Error(path);};
  const context=vm.createContext({document:{getElementById:node},location:{href:'https://example.test/gotc/platform.html#team'},URL,TextEncoder,Intl,Date,Math,setTimeout,clearTimeout,AbortController,localStorage:storage,fetch,confirm:()=>true});vm.runInContext(helper,context);vm.runInContext(source+'\nrouteVersion=1;route="team";session={role:"team"};globalThis.render=()=>renderTeam(1);globalThis.current=()=>currentTeamRecovery;globalThis.establish=login=>{publishLogin(login);session=login;};globalThis.invokeApi=api;globalThis.dirty=()=>submissionDirty;globalThis.logout=()=>{account();return document.getElementById("logout").listeners.click();};globalThis.seed=(teamId,summary,baseVersion=0,state="editing",at=Date.now())=>{const fields=materialFields.map(([key,,limit])=>[key,limit]),materials=Object.fromEntries(fields.map(([key])=>[key,key==="summary"?summary:""]));return GotcDraftRecovery.create({storage:()=>localStorage,namespace:base.pathname,teamId,baseVersion,baseMaterials:Object.fromEntries(fields.map(([key])=>[key,""])),fields,now:()=>at}).write(materials,state,state==="editing"?null:"draft");};renderRoute=async()=>{};',context);
  return {context,storage,nodes,node,root,calls,hooks,response,consumedReceipts,savedResponse(next){const sent=calls.filter(call=>call.path==='submission').at(-1).body;const receipt={requestId:sent.requestId,status:'succeeded',action:sent.status,baseVersion:sent.expectedVersion,version:next.submission.version,savedAt:1234,materials:sent.materials,historyVersion:sent.status==='submitted'?next.submission.version:null};assert.equal(vm.runInContext('matchingSubmissionReceipt',context)(receipt,sent.requestId,sent.status,sent.expectedVersion),true);return response({...next,receipt});},get data(){return data;},set data(next){data=next;},edit(key,value,event='input'){node('submission-form').elements[key].value=value;node('submission-form').listeners[event]();},inspect(index=0){return node('local-draft-candidates').querySelectorAll('[data-local-view]')[index].onclick();},slots(){return [...storage.map.values()].filter(value=>value.startsWith('{')).map(value=>JSON.parse(value));}};
}

const uploaded=(kind,ext)=>({kind,url:'https://example.test/gotc/materials/'+('u'.repeat(43))+'/'+encodeURIComponent('One+作品.'+ext),filename:'One+作品.'+ext,size:12});
async function uploadFixture(){const f=fixture();f.context.establish({role:'team',loginId:'upload-login'});await f.context.render();f.edit('projectName','作品');f.edit('summary','Keep this summary');return f;}
function chooseUpload(f,kind,name,size=12){f.node(`upload-${kind}-file`).files=[{name,size}];f.node(`upload-${kind}-file`).listeners.change();}

test('binary upload fills the matching URL and local recovery without submitting or storing file bytes',async()=>{
  for(const [kind,ext,field] of [['slides','pptx','slidesUrl'],['files','zip','filesUrl'],['demo','html','demoUrl']]){
    const f=await uploadFixture(),result=uploaded(kind,ext);chooseUpload(f,kind,'原文件.'+ext);f.node('material-confirmed').checked=true;
    f.hooks['team/uploads/'+kind]=()=>f.response(result);await f.node(`upload-${kind}-button`).onclick();
    const request=f.calls.find(c=>c.path==='team/uploads/'+kind);
    assert.equal(request.body,f.node(`upload-${kind}-file`).files[0]);assert.equal(request.method,'POST');assert.equal(request.credentials,'same-origin');
    assert.equal(request.headers['Content-Type'],'application/octet-stream');assert.equal(request.headers['X-GOTC-Login'],'upload-login');assert.equal(request.headers['X-GOTC-Project'],encodeURIComponent('作品'));assert.equal(request.headers['X-GOTC-Filename'],encodeURIComponent('原文件.'+ext));
    assert.equal(f.node('material-'+field).value,result.url);assert.equal(f.node('material-confirmed').checked,false);assert.equal(f.node('material-summary').value,'Keep this summary');
    assert.ok(f.slots().some(record=>record.materials[field]===result.url));assert.ok(f.slots().every(record=>!JSON.stringify(record).includes('原文件')));assert.match(f.node(`upload-${kind}-status`).textContent,/尚未保存草稿或正式提交/);
    assert.match(f.node(`upload-${kind}-result`).innerHTML,/One\+作品/);assert.equal(f.calls.filter(c=>c.path==='submission').length,0);
    await f.node('save-submission-draft').onclick();assert.equal(f.data.submission.draft[field],result.url);
  }
});

test('blank project uploads under the default title and equivalent blank edits do not invalidate it',async()=>{
  const f=await uploadFixture();f.edit('projectName','');chooseUpload(f,'files','work.zip');f.hooks['team/uploads/files']=()=>f.response(uploaded('files','zip'));
  assert.match(f.node('upload-files-name').textContent,/One\+作品.zip/);await f.node('upload-files-button').onclick();const call=f.calls.find(c=>c.path==='team/uploads/files');assert.equal(call.headers['X-GOTC-Project'],encodeURIComponent('作品'));assert.equal(f.node('material-filesUrl').value,uploaded('files','zip').url);assert.equal(f.node('material-projectName').value,'');
  f.edit('projectName','  ');assert.ok(!f.node('upload-files-status').textContent.includes('作品名称已变化'));f.edit('projectName','新名称');assert.match(f.node('upload-files-status').textContent,/作品名称已变化/);
});

test('an upload is single-flight and blocks draft save, preview and an already-rendered confirmation',async()=>{
  const f=await uploadFixture(),gate=deferred();
  for(const [key,value] of Object.entries({codeUrl:'https://github.com/team/project',codeVersion:'a'.repeat(40),readmeUrl:'https://example.test/readme',license:'MIT',demoUrl:'https://example.test/demo',slidesUrl:'https://example.test/slides',filesUrl:'https://example.test/work.zip',sourcesAndLimitations:'Synthetic only'}))f.edit(key,value);
  f.node('material-confirmed').checked=true;await f.node('submission-form').onsubmit({preventDefault(){}});const oldConfirm=f.node('confirm-submission').onclick;assert.equal(typeof oldConfirm,'function');
  chooseUpload(f,'slides','slides.pptx');chooseUpload(f,'files','files.zip');f.hooks['team/uploads/slides']=()=>gate.promise;
  const pending=f.node('upload-slides-button').onclick();assert.equal(f.node('save-project-intro').disabled,true);assert.equal(f.node('save-submission-draft').disabled,true);assert.equal(f.node('check-submission').disabled,true);assert.equal(f.node('submission-preview').innerHTML,'');
  await f.node('upload-slides-button').onclick();await f.node('upload-files-button').onclick();await f.node('save-project-intro').onclick();await f.node('save-submission-draft').onclick();await f.node('submission-form').onsubmit({preventDefault(){}});await oldConfirm();
  assert.equal(f.calls.filter(c=>c.path.startsWith('team/uploads/')).length,1);assert.equal(f.calls.filter(c=>c.path==='submission').length,0);
  gate.resolve(f.response(uploaded('slides','pptx')));await pending;assert.equal(f.node('save-submission-draft').disabled,false);assert.equal(f.node('check-submission').disabled,false);assert.equal(f.node('material-confirmed').checked,false);
});

test('upload failures and invalid receipts preserve the old URL and unrelated input',async()=>{
  for(const failure of ['network','server','receipt']){
    const f=await uploadFixture();f.edit('filesUrl','https://example.test/old.zip');chooseUpload(f,'files','files.zip');
    f.hooks['team/uploads/files']=()=>{if(failure==='network')throw new TypeError('Network failure');return failure==='server'?f.response({error:'上传未成功'},413):f.response({...uploaded('files','zip'),url:'https://other.test/file.zip'});};
    await f.node('upload-files-button').onclick();assert.equal(f.node('material-filesUrl').value,'https://example.test/old.zip');assert.equal(f.node('material-summary').value,'Keep this summary');assert.equal(f.node('save-submission-draft').disabled,false);assert.equal(f.calls.filter(c=>c.path==='submission').length,0);assert.ok(!f.node('upload-files-status').textContent.includes('文件已上传'));
  }
});

test('editing project name or target link during upload keeps the new input and offers the uploaded file separately',async()=>{
  for(const key of ['projectName','slidesUrl']){
    const f=await uploadFixture(),gate=deferred();f.edit('slidesUrl','https://example.test/old');chooseUpload(f,'slides','slides.pptx');f.hooks['team/uploads/slides']=()=>gate.promise;
    const pending=f.node('upload-slides-button').onclick();f.edit(key,key==='projectName'?'新作品':'https://example.test/new');gate.resolve(f.response(uploaded('slides','pptx')));await pending;
    assert.equal(f.node('material-slidesUrl').value,key==='slidesUrl'?'https://example.test/new':'https://example.test/old');assert.match(f.node('upload-slides-status').textContent,/未替换当前输入/);assert.match(f.node('upload-slides-result').innerHTML,/查看 \/ 下载/);
  }
});

test('renaming a project retains uploaded links and explicitly asks for a renamed upload',async()=>{
  const f=await uploadFixture();chooseUpload(f,'demo','demo.html');f.hooks['team/uploads/demo']=()=>f.response(uploaded('demo','html'));await f.node('upload-demo-button').onclick();const url=f.node('material-demoUrl').value;
  f.edit('projectName','新作品');assert.equal(f.node('material-demoUrl').value,url);assert.match(f.node('upload-demo-status').textContent,/作品名称已变化.*重新上传/);
  f.edit('projectName','作品');assert.ok(!f.node('upload-demo-status').textContent.includes('作品名称已变化'));
});

test('late upload success and denial cannot paint or lock a new team login',async()=>{
  for(const status of [200,403]){
    const f=await uploadFixture(),gate=deferred();chooseUpload(f,'demo','demo.html');f.hooks['team/uploads/demo']=()=>gate.promise;
    const pending=f.node('upload-demo-button').onclick();f.context.establish({role:'team',loginId:'new-login'});f.data={...f.data,team:{id:'T2',number:2,name:'Two'}};await f.context.render();f.edit('demoUrl','https://example.test/new-team');
    gate.resolve(f.response(status===200?uploaded('demo','html'):{error:'expired'},status));await pending;
    assert.equal(f.node('material-demoUrl').value,'https://example.test/new-team');assert.equal(f.node('fieldset').disabled,false);assert.ok(!f.node('upload-demo-result').innerHTML.includes('One+作品'));assert.ok(f.slots().filter(r=>r.teamId==='T2').every(r=>r.materials.demoUrl==='https://example.test/new-team'));
  }
});

test('mismatched file extension and oversized file fail before sending bytes',async()=>{
  for(const [project,kind,name,size] of [['作品','demo','site.zip',12],['作品','files','files.zip',100*1024*1024+1],['作品','slides','empty.pdf',0]]){
    const f=await uploadFixture();f.edit('projectName',project);chooseUpload(f,kind,name,size);await f.node(`upload-${kind}-button`).onclick();assert.equal(f.calls.filter(c=>c.path.startsWith('team/uploads/')).length,0);assert.ok(f.node(`upload-${kind}-status`).textContent);
  }
});
test('authorized same-version recovery preserves raw strings, clears confirmation and never sends a POST',async()=>{
  const f=fixture();f.context.seed('T1','  local\nraw  ');await f.context.render();assert.equal(f.slots().length,1);assert.ok(!f.node('local-draft-candidates').innerHTML.includes('副本编号'));await f.inspect();assert.match(f.node('local-restore-actions').innerHTML,/confirm-local-restore/);await f.node('confirm-local-restore').onclick({currentTarget:f.node('confirm-local-restore')});assert.equal(f.node('material-summary').value,'  local\nraw  ');assert.equal(f.node('material-confirmed').checked,false);assert.equal(f.calls.filter(c=>c.body).length,0);assert.equal(f.slots().length,2);assert.equal(f.context.dirty(),true);
});

test('pre-upload schema-1 draft remains visible and restores every original field before normal save',async()=>{
  const f=fixture(),seed=f.context.seed('T1','OLD RAW SUMMARY'),legacy=JSON.parse(seed.raw);
  delete legacy.materials.filesUrl;delete legacy.baseMaterials.filesUrl;
  for(const key of Object.keys(legacy.materials))legacy.materials[key]=key.endsWith('Url')?'https://example.test/'+key:key==='codeVersion'?'a'.repeat(40):`  ${key} 原始文字\n第二行  `;
  const original=JSON.stringify(legacy);f.storage.setItem(seed.key,original);
  await f.context.render();assert.match(f.node('local-draft-candidates').innerHTML,/本地副本/);assert.equal(f.storage.getItem(seed.key),original);assert.equal(f.slots().length,1);
  await f.inspect();assert.match(f.node('local-restore-actions').innerHTML,/confirm-local-restore/);assert.equal(f.storage.getItem(seed.key),original);
  await f.node('confirm-local-restore').onclick({currentTarget:f.node('confirm-local-restore')});
  for(const [key,value] of Object.entries(legacy.materials))assert.equal(f.node('material-'+key).value,value,key);
  assert.equal(f.node('material-filesUrl').value,'');assert.equal(f.node('material-confirmed').checked,false);assert.equal(f.storage.getItem(seed.key),original);assert.equal(f.calls.filter(c=>c.path==='submission').length,0);
  await f.node('save-submission-draft').onclick();const sent=f.calls.find(c=>c.path==='submission').body.materials;
  for(const [key,value] of Object.entries(legacy.materials))assert.equal(sent[key],value.trim(),key);
  assert.equal(sent.filesUrl,'');assert.equal(f.storage.getItem(seed.key),original);
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
  const f=fixture();await f.context.render();f.edit('summary','OLD_SEND');const waiting=deferred();f.hooks.submission=()=>waiting.promise;const saving=f.node('save-submission-draft').onclick();await f.context.render();f.edit('summary','NEW_VIEW');waiting.resolve(f.savedResponse({...f.data,submission:{...f.data.submission,version:1}}));await saving;assert.equal(f.node('material-summary').value,'NEW_VIEW');assert.equal(f.context.dirty(),true);assert.ok(f.slots().some(record=>record.materials.summary==='NEW_VIEW'));
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
  const f=fixture();await f.context.render();f.edit('summary','SENT');const waiting=deferred();f.hooks.submission=()=>waiting.promise;const saving=f.node('save-submission-draft').onclick();f.edit('summary','CONTINUED');waiting.resolve(f.savedResponse({...f.data,submission:{...f.data.submission,version:1,draft:{summary:'SENT'}},receipt:{requestId:f.calls.find(call=>call.path==='submission').body.requestId,status:'succeeded',action:'draft',baseVersion:0,version:1,savedAt:1234,materials:{summary:'SENT'},historyVersion:null}}));await saving;assert.equal(f.node('material-summary').value,'CONTINUED');assert.equal(f.context.dirty(),true);assert.equal(f.slots().find(record=>record.materials.summary==='CONTINUED').baseVersion,1);delete f.hooks.submission;await f.node('save-submission-draft').onclick();assert.equal(f.calls.filter(call=>call.path==='submission').at(-1).body.expectedVersion,1);
});

test('same-session route reconstruction keeps logout single-flight and completes authorized cleanup',async()=>{
  const f=fixture();await f.context.render();f.edit('summary','FIRST_VIEW');const waiting=deferred();f.hooks.logout=()=>waiting.promise;const leaving=f.context.logout();await new Promise(resolve=>setImmediate(resolve));await f.context.render();f.edit('summary','SAME_SESSION_NEW_VIEW');await f.context.logout();assert.equal(f.calls.filter(call=>call.path==='logout').length,1);waiting.resolve(f.response({ok:true}));await leaving;assert.equal(f.slots().length,0);assert.equal(vm.runInContext('session.role',f.context),null);
});
test('lost-response request id survives refresh and exact lookup confirms only that immutable request',async()=>{
  const storage=new Storage(),first=fixture(storage);await first.context.render();first.edit('summary','LOST_REQUEST');first.hooks.submission=()=>{throw new TypeError('lost response');};await first.node('save-submission-draft').onclick();const record=first.slots()[0],post=first.calls.find(call=>call.path==='submission');assert.match(post.body.requestId,/^[A-Za-z0-9_-]{16,128}$/);assert.equal(record.request.requestId,post.body.requestId);assert.equal(record.state,'unknown');
  const refreshed=fixture(storage);refreshed.data={...refreshed.data,submission:{...refreshed.data.submission,version:5,draft:{summary:'TEAMMATE_LATER'},submitted:{summary:'TEAMMATE_LATER'},submittedVersion:4}};refreshed.hooks['submission/requests/'+post.body.requestId]=()=>refreshed.response({requestId:post.body.requestId,status:'succeeded',action:'draft',baseVersion:0,version:1,savedAt:1234,materials:{summary:'LOST_REQUEST'},historyVersion:null});await refreshed.context.render();await refreshed.inspect();assert.match(refreshed.node('local-request-result').innerHTML,/草稿编辑版 v1 已保存/);assert.match(refreshed.node('local-request-result').innerHTML,/LOST_REQUEST/);assert.ok(!refreshed.node('local-request-result').innerHTML.includes('TEAMMATE_LATER'));assert.equal(refreshed.node('material-summary').value,'TEAMMATE_LATER');assert.equal(refreshed.calls.filter(call=>call.body).length,0);assert.equal(refreshed.slots().length,1);assert.equal(refreshed.node('local-comparison-title').textContent,'本地副本与服务器当前草稿对照');assert.match(refreshed.node('local-comparison-outcome').textContent,/已由上方同编号回执确认/);
});
test('not found lookup retains uncertainty and local source without automatic writes',async()=>{
  const storage=new Storage(),first=fixture(storage);await first.context.render();first.edit('summary','STILL_UNKNOWN');first.hooks.submission=()=>{throw new TypeError('lost');};await first.node('save-submission-draft').onclick();const requestId=first.slots()[0].request.requestId,second=fixture(storage);second.hooks['submission/requests/'+requestId]=()=>second.response({requestId,status:'not_found'});await second.context.render();await second.inspect();assert.match(second.node('local-request-result').innerHTML,/结果仍待确认/);assert.ok(!second.node('local-request-result').innerHTML.includes('已保存</h3>'));assert.equal(second.slots()[0].state,'unknown');assert.equal(second.calls.filter(call=>call.body).length,0);
});
for(const delivered of [false,true])test(`another tab logout revokes a pending successful save even when storage event ${delivered?'has arrived':'is delayed'}`,async()=>{
  const storage=new Storage(),a=fixture(storage),b=fixture(storage);await a.context.render();await b.context.render();b.edit('summary','SENT_BEFORE_LOGOUT');const original=b.context.current(),form=b.node('submission-form'),waiting=deferred();b.hooks.submission=()=>waiting.promise;const saving=b.node('save-submission-draft').onclick();await a.context.logout();if(delivered)original.storageChanged();assert.equal(b.slots().length,0);
  waiting.resolve(b.savedResponse({...b.data,submission:{...b.data.submission,version:1,draft:{summary:'SENT_BEFORE_LOGOUT'}}}));await saving;assert.ok(b.consumedReceipts.includes(b.calls.find(call=>call.path==='submission').body.requestId),'the successful JSON receipt was consumed');assert.equal(b.context.current(),original,'late success must not create a new recovery context in the logout epoch');assert.equal(b.node('submission-form'),form);assert.equal(b.node('fieldset').disabled,true);assert.equal(b.node('local-draft-recovery').hidden,true);b.edit('summary','OLD_TAB_AFTER_LOGOUT');b.context.current().flush();assert.equal(b.slots().length,0,'the old tab cannot create a copy in the logout epoch');assert.equal(b.calls.filter(call=>call.path==='submission').length,1);
});
for(const status of [409,500])test(`late ${status} response after another tab logout keeps the old context locked`,async()=>{
  const storage=new Storage(),a=fixture(storage),b=fixture(storage);await a.context.render();await b.context.render();b.edit('summary','SENT');const waiting=deferred();b.hooks.submission=()=>waiting.promise;const saving=b.node('save-submission-draft').onclick();await a.context.logout();waiting.resolve(b.response({error:'late error'},status));await saving;assert.equal(b.node('fieldset').disabled,true);assert.equal(b.node('local-draft-recovery').hidden,true);assert.equal(b.node('submission-conflict').innerHTML,'');b.edit('summary','MUST_NOT_WRITE');assert.equal(b.slots().length,0);
});
test('a latest-record query started before another tab logout cannot offer a fresh editing context',async()=>{
  const storage=new Storage(),a=fixture(storage),b=fixture(storage);await a.context.render();await b.context.render();b.edit('summary','SENT');b.hooks.submission=()=>{throw new TypeError('lost response');};await b.node('save-submission-draft').onclick();const waiting=deferred(),original=b.context.current();b.hooks.team=()=>waiting.promise;const reading=b.node('load-latest-submission').onclick({currentTarget:b.node('load-latest-submission')});await a.context.logout();waiting.resolve(b.savedResponse({...b.data,submission:{...b.data.submission,version:1,draft:{summary:'SAVED'}}}));await reading;assert.equal(b.context.current(),original);assert.equal(b.node('latest-submission').innerHTML,'');assert.equal(b.node('fieldset').disabled,true);assert.equal(b.node('local-draft-recovery').hidden,true);assert.equal(b.slots().length,0);
});
test('a latest-record restart button cannot reuse authorization revoked after the query completed',async()=>{
  const storage=new Storage(),a=fixture(storage),b=fixture(storage);await a.context.render();await b.context.render();b.edit('summary','SENT');b.hooks.submission=()=>{throw new TypeError('lost');};await b.node('save-submission-draft').onclick();await b.node('load-latest-submission').onclick({currentTarget:b.node('load-latest-submission')});const original=b.context.current();await a.context.logout();await b.node('restart-submission').onclick();assert.equal(b.context.current(),original);assert.equal(b.node('fieldset').disabled,true);b.edit('summary','MUST_NOT_WRITE');assert.equal(b.slots().length,0);
});
test('explicit new same-team login accepts the new epoch while a prior pending response cannot touch its copy',async()=>{
  const storage=new Storage(),a=fixture(storage),b=fixture(storage);await a.context.render();await b.context.render();b.edit('summary','OLD_PENDING');const waiting=deferred();b.hooks.submission=()=>waiting.promise;const saving=b.node('save-submission-draft').onclick();await a.context.logout();vm.runInContext('session={role:"team"};',a.context);await a.context.render();a.edit('summary','NEW_LOGIN_INPUT');const snapshot=a.slots()[0];waiting.resolve(b.savedResponse({...b.data,submission:{...b.data.submission,version:1,draft:{summary:'OLD_PENDING'}}}));await saving;assert.deepEqual(a.slots(),[snapshot]);assert.equal(a.node('fieldset').disabled,false);a.edit('summary','NEW_LOGIN_CAN_CONTINUE');assert.equal(a.slots()[0].materials.summary,'NEW_LOGIN_CAN_CONTINUE');assert.equal(b.node('fieldset').disabled,true);
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
test('an exact request query begun before logout cannot paint a terminal receipt after authorization is revoked',async()=>{
  const storage=new Storage(),a=fixture(storage),b=fixture(storage);await a.context.render();await b.context.render();b.edit('summary','LOOKUP_SENT');b.hooks.submission=()=>{throw new TypeError('lost');};await b.node('save-submission-draft').onclick();const sent=b.calls.find(call=>call.path==='submission').body,waiting=deferred();b.hooks['submission/requests/'+sent.requestId]=()=>waiting.promise;const reading=b.node('query-submission-request').onclick({currentTarget:b.node('query-submission-request')});await a.context.logout();waiting.resolve(b.response({requestId:sent.requestId,status:'succeeded',action:'draft',baseVersion:0,version:1,savedAt:1234,materials:sent.materials,historyVersion:null}));await reading;assert.equal(b.node('submission-request-result').innerHTML,'');assert.equal(b.node('fieldset').disabled,true);assert.equal(b.node('local-draft-recovery').hidden,true);
});
test('a local candidate request lookup locks its old view when logout happens after the team read',async()=>{
  const storage=new Storage(),first=fixture(storage);await first.context.render();first.edit('summary','CANDIDATE_SENT');first.hooks.submission=()=>{throw new TypeError('lost');};await first.node('save-submission-draft').onclick();const sent=first.calls.find(call=>call.path==='submission').body,a=fixture(storage),b=fixture(storage);await a.context.render();await b.context.render();const waiting=deferred();b.hooks['submission/requests/'+sent.requestId]=()=>waiting.promise;const reading=b.inspect();await new Promise(resolve=>setImmediate(resolve));await a.context.logout();waiting.resolve(b.response({requestId:sent.requestId,status:'succeeded',action:'draft',baseVersion:0,version:1,savedAt:1234,materials:sent.materials,historyVersion:null}));await reading;assert.equal(b.node('local-draft-recovery').hidden,true);assert.equal(b.node('fieldset').disabled,true);assert.ok(!b.node('local-request-result').innerHTML.includes('草稿编辑版 v1 已保存'));
});
for(const detail of [false,true])test(`formal history ${detail?'detail':'list'} read cannot paint a result into a logged-out team view`,async()=>{
  const storage=new Storage(),a=fixture(storage),b=fixture(storage);await a.context.render();await b.context.render();const item={version:1,submittedAt:1234,requestId:'history-request-0001',origin:'submitted',codeVersion:'a'.repeat(40),projectName:'PRIVATE_HISTORY'},waiting=deferred();let reading;
  if(detail){b.hooks['submission/history']=()=>b.response({items:[item],nextBeforeVersion:null});await b.node('submission-history-load').onclick({currentTarget:b.node('submission-history-load')});b.hooks['submission/history/1']=()=>waiting.promise;const selected={isConnected:true,disabled:false,dataset:{historyVersion:'1'}};reading=b.node('submission-history-list').onclick({target:{closest:()=>selected}});}
  else {b.hooks['submission/history']=()=>waiting.promise;reading=b.node('submission-history-load').onclick({currentTarget:b.node('submission-history-load')});}
  await a.context.logout();waiting.resolve(b.response(detail?{...item,materials:{summary:'PRIVATE_DETAIL'}}:{items:[item],nextBeforeVersion:null}));await reading;assert.equal(b.node(detail?'submission-history-detail':'submission-history-list').innerHTML,'');assert.equal(b.node('local-draft-recovery').hidden,true);assert.equal(b.node('fieldset').disabled,true);
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

test('Gitee recovery restores a real form with removed inputs and draft save omits them',async()=>{
  const repository={provider:'gitee',codeUrl:'https://gitee.com/synthetic/project',organization:'synthetic',slug:'project',branch:'main',state:'ready',usernames:['Developer'],protection:'unverified',message:'已确认'};
  const f=fixture(new Storage(),{repository});
  f.context.seed('T1','  restored Gitee summary  ');
  await f.context.render();
  for(const key of ['readmeUrl','license','licenseUrl','sourcesAndLimitations'])assert.equal(f.node('submission-form').elements[key],undefined);
  await f.inspect();await f.node('confirm-local-restore').onclick({currentTarget:f.node('confirm-local-restore')});
  assert.equal(f.node('material-summary').value,'  restored Gitee summary  ');
  assert.equal(f.node('material-confirmed').checked,false);
  f.edit('summary','after restoration');assert.equal(f.slots().at(-1).materials.readmeUrl,'');
  await f.node('save-submission-draft').onclick();
  const sent=f.calls.find(call=>call.path==='submission').body.materials;
  assert.equal(sent.summary,'after restoration');
  for(const key of ['readmeUrl','license','licenseUrl','sourcesAndLimitations'])assert.ok(!(key in sent));
  assert.equal(f.data.submission.version,1);
});
