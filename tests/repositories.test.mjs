import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source=fs.readFileSync(new URL('../platform.js',import.meta.url),'utf8').split("window.addEventListener('hashchange'")[0];
const sha='a'.repeat(40),previewId='b'.repeat(48);
const repository={teamId:'T1',provider:'github',organization:'GOTC-AI-HACKATHON',slug:'gotc-2026-team-1-123',branch:'main',codeUrl:'https://github.com/GOTC-AI-HACKATHON/gotc-2026-team-1-123',state:'invited',revision:3,usernames:['Developer'],protection:'unverified',message:'等待接受邀请',archived:false};
const providers=[{provider:'github',organization:'GOTC-AI-HACKATHON',enabled:true,message:'已配置'},{provider:'gitee',organization:'gotc-ai-hackathon',enabled:false,message:'尚未启用'}];
const materials={projectName:'Synthetic',summary:'Summary',codeUrl:'',codeVersion:'',readmeUrl:'https://example.test/readme',license:'MIT',licenseUrl:'',demoUrl:'',demoInstructions:'Run locally',slidesUrl:'https://example.test/slides',videoUrl:'https://example.test/video',sourcesAndLimitations:'Synthetic only',confirmed:true};
function fixture(){
  const nodes=new Map(),calls=[];
  function node(id){
    if(nodes.has(id))return nodes.get(id);
    const item={id,isConnected:true,disabled:false,readOnly:false,value:'',checked:false,dataset:{},listeners:{},classList:{add(){},remove(){}},addEventListener(kind,fn){this.listeners[kind]=fn;},focus(){},scrollIntoView(){},querySelectorAll(){return [];},querySelector(){return null;}};
    let html='';Object.defineProperty(item,'innerHTML',{configurable:true,get:()=>html,set(value){html=value;for(const match of value.matchAll(/id="([^"]+)"/g))node(match[1]);}});nodes.set(id,item);return item;
  }
  const root=node('platform-main');let latest={team:{id:'T1',number:1,name:'Synthetic'},window:{canEdit:true,submissionDeadline:null},submission:{version:0,draft:null,submitted:null},repository,repositoryProviders:providers};
  const rootSetter=Object.getOwnPropertyDescriptor(root,'innerHTML').set;
  Object.defineProperty(root,'innerHTML',{get:()=>root.html || '',set(value){root.html=value;rootSetter(value);const form=node('submission-form');form.elements={};for(const [key,value] of Object.entries(materials)){const input=node('material-'+key);input.value=key==='confirmed'?'':value;input.checked=value===true;form.elements[key]=input;}form.querySelector=selector=>selector==='fieldset'?node('fieldset'):node('form-error');form.querySelectorAll=selector=>selector==='input,textarea'?Object.values(form.elements):[];}});
  const context=vm.createContext({document:{getElementById:node},location:{href:'https://example.test/platform.html#team'},URL,TextEncoder,Intl,Date,setTimeout,clearTimeout,AbortController});
  context.transport=async(path,body)=>{
    calls.push({path,body});
    if(path==='team')return structuredClone(latest);
    if(path==='team/repository/preview')return context.previewResponse?await context.previewResponse():{previewId,sha,branch:'main',codeUrl:repository.codeUrl,archived:false};
    if(path==='submission'){
      if(context.submissionResponse)await context.submissionResponse(body);
      const receipt={requestId:body.requestId,status:'succeeded',action:body.status,baseVersion:0,version:1,historyVersion:1,savedAt:123,materials:body.materials};
      latest={...latest,submission:{version:1,draft:body.materials,submitted:body.materials,submittedVersion:1,submittedAt:123}};
      return {...latest,receipt};
    }
    throw new Error(path);
  };
  context.recoveryStates=[];
  vm.runInContext(source+`\napi=transport;session={role:'team',loginId:'synthetic-login'};routeVersion=1;route='team';
    installTeamRecovery=()=>({isAuthorized:()=>true,hasAuthorization:()=>true,sessionOwner:session,teamId:'T1',input(){},pending(){},acknowledge(){},result(sent,state){globalThis.recoveryStates.push(state);}});
    installTeamRepository=()=>{};installSubmissionHistory=()=>{};loadTeamKeys=()=>{};
    globalThis.h={teamFormMarkup,teamRepositoryMarkup,repositoryAdminMarkup,repositoryStatusMarkup};globalThis.render=()=>renderTeam(1);`,context);
  return {context,calls,node,root,h:context.h};
}

test('bound form hides required manual code fields while unbound teams retain them',()=>{
  const {h}=fixture(),data={team:{project:'P'},submission:{draft:{codeUrl:'OLD',codeVersion:'OLD'}},window:{canEdit:true}};
  const bound=h.teamFormMarkup({...data,repository});
  for(const key of ['codeUrl','codeVersion']){
    assert.match(bound,new RegExp(`type="hidden" name="${key}"`));
    assert.ok(!bound.includes(`for="material-${key}"`));
  }
  assert.ok(!bound.includes('OLD'));
  assert.match(h.teamFormMarkup(data),/name="codeUrl"[^>]*required/);
  assert.match(h.teamFormMarkup(data),/for="material-codeVersion"/);
});

test('application and status panels clearly distinguish disabled channel, invitation, protection and archive',()=>{
  const {h}=fixture();
  const disabled=h.teamRepositoryMarkup({repositoryProviders:providers.map(p=>({...p,enabled:false}))});
  assert.match(disabled,/组织仓库尚未启用/);assert.ok(!disabled.includes('id="repository-application"'));
  const application=h.teamRepositoryMarkup({repositoryProviders:providers});
  assert.match(application,/申请本队仓库/);assert.match(application,/value="gitee" disabled/);
  const status=h.repositoryStatusMarkup({...repository,message:'<script>bad</script>'});
  assert.match(status,/邀请待接受/);assert.match(status,/代码尚未归档/);assert.match(status,/尚未验证/);assert.ok(!status.includes('<script>'));
});

test('admin review requires ownership acknowledgement and exposes no client credential settings',()=>{
  const {h}=fixture(),html=h.repositoryAdminMarkup({providers,items:[repository]});
  assert.match(html,/name="verified" type="checkbox" required/);
  assert.match(html,/核对恢复 \/ 刷新邀请/);
  assert.match(html,/不要在此填写令牌/);
  assert.ok(!html.includes('type="password"'));
});

test('bound team controller obtains full server SHA and sends preview id only after explicit confirmation',async()=>{
  const f=fixture();await f.context.render();
  // Stale local draft values are ignored by bound formal validation.
  f.node('material-codeUrl').value='https://github.com/other/repository';f.node('material-codeVersion').value='main';
  await f.node('submission-form').onsubmit({preventDefault(){}});
  assert.equal(f.calls.filter(c=>c.path==='submission').length,0);
  assert.match(f.node('submission-preview').innerHTML,new RegExp(sha));assert.match(f.node('submission-preview').innerHTML,/代码尚未归档/);
  await f.node('confirm-submission').onclick();
  const body=f.calls.find(c=>c.path==='submission').body;
  assert.equal(body.repoPreviewId,previewId);assert.equal(body.materials.codeVersion,sha);assert.equal(body.materials.codeUrl,repository.codeUrl);
  assert.match(f.root.innerHTML,/正式版 v1 已收到/);
});

test('input changed during SHA read invalidates preview without sending materials',async()=>{
  const f=fixture();let resolve;f.context.previewResponse=()=>new Promise(done=>{resolve=done;});await f.context.render();
  const pending=f.node('submission-form').onsubmit({preventDefault(){}});
  f.node('material-summary').value='Changed during fetch';f.node('submission-form').listeners.input();
  resolve({previewId,sha,branch:'main',codeUrl:repository.codeUrl});await pending;
  assert.equal(f.node('submission-preview').innerHTML,'');assert.match(f.node('form-error').textContent,/材料已修改/);
  assert.equal(f.calls.filter(c=>c.path==='submission').length,0);
});

test('upstream SHA failure preserves input and does not fabricate a preview or receipt',async()=>{
  const f=fixture();f.context.previewResponse=async()=>{throw new Error('仓库服务暂不可用');};await f.context.render();
  await f.node('submission-form').onsubmit({preventDefault(){}});
  assert.match(f.node('form-error').textContent,/仓库服务暂不可用/);assert.equal(f.node('material-summary').value,'Summary');
  assert.equal(f.node('submission-preview').innerHTML,'');assert.equal(f.calls.filter(c=>c.path==='submission').length,0);
});

for(const code of ['repository_head_changed','repository_preview_expired','repository_preview_required']){
  test(`${code} preserves editable unsaved materials and permits a fresh preview and confirmation`,async()=>{
    const f=fixture();let previews=0,attempts=0;
    f.context.previewResponse=async()=>({previewId:++previews===1?previewId:'c'.repeat(48),sha:previews===1?sha:'d'.repeat(40),branch:'main',codeUrl:repository.codeUrl});
    f.context.submissionResponse=async()=>{if(++attempts===1){const error=new Error('请重新预览 SHA');error.status=409;error.code=code;throw error;}};
    await f.context.render();
    f.node('material-summary').value='UNSAVED MATERIALS';f.node('submission-form').listeners.input();
    await f.node('submission-form').onsubmit({preventDefault(){}});await f.node('confirm-submission').onclick();
    assert.equal(f.node('material-summary').value,'UNSAVED MATERIALS');
    assert.equal(f.node('material-summary').readOnly,false);assert.equal(f.node('material-confirmed').disabled,false);assert.equal(f.node('fieldset').disabled,false);
    assert.equal(f.node('submission-preview').innerHTML,'');assert.equal(f.node('submission-conflict').innerHTML,'');
    assert.match(f.node('form-error').textContent,/当前材料已保留/);assert.equal(f.context.recoveryStates.at(-1),'editing');
    f.node('material-demoInstructions').value='Further unsaved edits';f.node('submission-form').listeners.input();
    await f.node('submission-form').onsubmit({preventDefault(){}});await f.node('confirm-submission').onclick();
    const posts=f.calls.filter(c=>c.path==='submission');
    assert.equal(previews,2);assert.equal(posts.length,2);assert.notEqual(posts[0].body.requestId,posts[1].body.requestId);
    assert.equal(posts[1].body.repoPreviewId,'c'.repeat(48));assert.equal(posts[1].body.materials.codeVersion,'d'.repeat(40));
    assert.equal(posts[1].body.materials.summary,'UNSAVED MATERIALS');assert.equal(posts[1].body.materials.demoInstructions,'Further unsaved edits');
    assert.match(f.root.innerHTML,/正式版 v1 已收到/);
  });
}

test('genuine material version conflicts still lock current inputs for manual reconciliation',async()=>{
  const f=fixture();f.context.submissionResponse=async()=>{const error=new Error('材料已被其他页面更新');error.status=409;error.code='version_conflict';throw error;};
  await f.context.render();f.node('material-summary').value='UNSAVED VERSION CONFLICT';
  await f.node('submission-form').onsubmit({preventDefault(){}});await f.node('confirm-submission').onclick();
  assert.equal(f.node('material-summary').value,'UNSAVED VERSION CONFLICT');assert.equal(f.node('material-summary').readOnly,true);
  assert.equal(f.context.recoveryStates.at(-1),'conflict');assert.match(f.node('submission-conflict').innerHTML,/材料版本已变化/);
  await f.node('submission-form').onsubmit({preventDefault(){}});
  assert.equal(f.calls.filter(c=>c.path==='submission').length,1);
});

test('ambiguous admin approval directs a status refresh and reconciliation before retrying',async()=>{
  const f=fixture(),box=f.node('repository-admin'),form=f.node('repository-review-form'),button=f.node('approve-button'),error=f.node('approve-error');
  form.dataset.repositoryTeam=repository.teamId;form.elements={verified:{checked:true}};
  form.querySelector=selector=>selector==='button'?button:error;
  box.querySelectorAll=selector=>selector==='[data-repository-team]'?[form]:[];
  const calls=[];f.context.adminTransport=async(path,body)=>{calls.push({path,body});if(body)throw new Error('Gateway timeout');return {providers,items:[{...repository,state:'requested'}]};};
  vm.runInContext(`api=adminTransport;route='admin';globalThis.startAdmin=()=>installRepositoryAdmin(document.getElementById('repository-admin'));`,f.context);
  await f.context.startAdmin();await form.onsubmit({preventDefault(){}});
  assert.match(error.textContent,/后台可能仍在处理/);assert.match(error.textContent,/刷新申请列表核对状态/);assert.match(error.textContent,/勿重复批准/);
  assert.equal(calls.filter(c=>c.body).length,1);
});
