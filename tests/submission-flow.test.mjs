import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const source=fs.readFileSync(new URL('../platform.js',import.meta.url),'utf8').split("window.addEventListener('hashchange'")[0];
const sha='a'.repeat(40);
const materials={projectName:'Project',summary:'Summary',codeUrl:'https://github.com/team/project',codeVersion:'https://github.com/team/project/commit/'+sha,readmeUrl:'https://example.test/readme',license:'MIT',licenseUrl:'',demoUrl:'',demoInstructions:'Run locally',slidesUrl:'https://example.test/slides',videoUrl:'https://example.test/video',sourcesAndLimitations:'Synthetic only',confirmed:true};
function fixture(mode='success'){
  const nodes=new Map(),calls=[];let lastReceipt,latest={team:{id:'T1',number:1,name:'Synthetic'},window:{canEdit:true,submissionDeadline:null},submission:{version:0,draft:null,submitted:null,submittedVersion:null,submittedAt:null},judgingMaterials:{}};
  function node(id){if(nodes.has(id))return nodes.get(id);const item={id,isConnected:true,disabled:false,readOnly:false,dataset:{},classList:{add(){},remove(){}},listeners:{},value:'',type:'text',focus(){this.focused=true;},scrollIntoView(){},addEventListener(event,fn){this.listeners[event]=fn;},querySelector(){return null;},querySelectorAll(){return [];}};let html='';Object.defineProperty(item,'innerHTML',{configurable:true,get:()=>html,set(value){html=value;for(const match of value.matchAll(/id="([^"]+)"/g))node(match[1]);}});nodes.set(id,item);return item;}
  const root=node('platform-main'),rootSetter=Object.getOwnPropertyDescriptor(root,'innerHTML').set;
  Object.defineProperty(root,'innerHTML',{get:()=>root.html || '',set(value){for(const n of nodes.values())if(n!==root)n.isConnected=false;nodes.clear();nodes.set(root.id,root);root.html=value;rootSetter(value);const form=node('submission-form');form.elements={};for(const key of [...Object.keys(materials)]){const field=node('material-'+key);field.value='';field.checked=false;field.type=key==='confirmed'?'checkbox':'text';form.elements[key]=field;}const fieldset=node('fieldset');form.querySelector=selector=>selector==='fieldset'?fieldset:selector==='.form-error'?node('form-error'):null;form.querySelectorAll=selector=>selector==='input,textarea'?Object.values(form.elements):selector==='button'?[node('save-submission-draft')]:[];node('submission-preview').querySelectorAll=selector=>selector==='button'?[node('confirm-submission'),node('cancel-submission-preview')]:[];}});
  const transport=async(path,body)=>{calls.push({path,body});if(path==='team')return structuredClone(latest);if(path.startsWith('submission/requests/'))return mode==='lost-found'?structuredClone(lastReceipt):{requestId:path.split('/').at(-1),status:'not_found'};if(path!=='submission')throw new Error(path);if(mode==='conflict'){const error=new Error('材料已被其他页面更新');error.status=409;throw error;}latest={...latest,submission:{version:1,draft:body.materials,submitted:body.status==='submitted'?body.materials:null,submittedVersion:body.status==='submitted'?1:null,submittedAt:1234,firstSubmittedAt:1234,updatedAt:1234}};lastReceipt={requestId:body.requestId,status:'succeeded',action:body.status,baseVersion:body.expectedVersion,version:1,savedAt:1234,materials:body.materials,historyVersion:body.status==='submitted'?1:null};if(mode==='lost' || mode==='lost-found')throw new TypeError('Failed to fetch');if(mode==='advanced')latest={...latest,submission:{...latest.submission,version:5,submittedVersion:4,draft:{...body.materials,summary:'TEAMMATE_CURRENT'},submitted:{...body.materials,summary:'TEAMMATE_CURRENT'}}};return {...structuredClone(latest),receipt:{requestId:body.requestId,status:'succeeded',action:body.status,baseVersion:body.expectedVersion,version:1,savedAt:1234,materials:body.materials,historyVersion:body.status==='submitted'?1:null}};};
  const context=vm.createContext({document:{getElementById:node},location:{href:'https://example.test/platform.html#team'},URL,TextEncoder,Intl,Date,setTimeout,clearTimeout,AbortController,transport,confirm:()=>true});
  vm.runInContext(source+'\napi=transport;routeVersion=1;route="team";globalThis.render=()=>renderTeam(1);',context);
  return {context,calls,node,root,fill(){const form=node('submission-form');for(const [key,value] of Object.entries(materials)){if(key==='confirmed')form.elements[key].checked=value;else form.elements[key].value=value;}},async preview(){node('submission-form').onsubmit({preventDefault(){}});},latest:()=>latest};
}
test('real team controller preflights, previews, then sends canonical SHA and renders server receipt',async()=>{
  const f=fixture();await f.context.render();
  f.node('submission-form').onsubmit({preventDefault(){}});
  assert.match(f.node('submission-errors').innerHTML,/请检查以下/);assert.equal(f.calls.filter(c=>c.body).length,0);
  f.fill();await f.preview();assert.match(f.node('submission-preview').innerHTML,new RegExp(sha));assert.equal(f.calls.filter(c=>c.body).length,0);
  await f.node('confirm-submission').onclick();
  assert.equal(f.calls.filter(c=>c.body).length,1);assert.equal(f.calls.at(-1).body.materials.codeVersion,sha);assert.match(f.root.innerHTML,/正式版 v1 已收到/);
});
test('lost response preserves input, blocks repeat writes and reads latest only for comparison',async()=>{
  const f=fixture('lost');await f.context.render();f.fill();await f.preview();await f.node('confirm-submission').onclick();
  assert.match(f.node('form-error').textContent,/结果待确认/);assert.equal(f.node('material-summary').value,'Summary');assert.equal(f.node('material-summary').readOnly,true);
  const posts=()=>f.calls.filter(c=>c.body).length;assert.equal(posts(),1);
  await f.node('load-latest-submission').onclick({currentTarget:f.node('load-latest-submission')});
  assert.equal(posts(),1);assert.match(f.node('latest-submission').innerHTML,/服务器最近正式记录/);assert.ok(!f.node('latest-submission').innerHTML.includes('正式版 v1 已收到'));
});
test('409 keeps existing inputs available for manual merge and never retries',async()=>{
  const f=fixture('conflict');await f.context.render();f.fill();await f.preview();await f.node('confirm-submission').onclick();
  assert.equal(f.node('material-summary').value,'Summary');assert.equal(f.node('material-summary').readOnly,true);assert.match(f.node('submission-conflict').innerHTML,/本次未保存/);assert.equal(f.calls.filter(c=>c.body).length,1);
});
test('successful receipt names its exact formal version even when the outer current projection has advanced',async()=>{
  const f=fixture('advanced');await f.context.render();f.fill();await f.preview();await f.node('confirm-submission').onclick();assert.match(f.root.innerHTML,/正式版 v1 已收到/);assert.ok(!f.root.innerHTML.includes('正式版 v4 已收到'));assert.match(f.root.innerHTML,/最新正式版 v4/);assert.match(f.root.innerHTML,/TEAMMATE_CURRENT/);assert.match(f.root.innerHTML,/复制本次保存记录/);assert.equal(f.calls.filter(call=>call.body).length,1);
});
test('same-view exact terminal lookup clears the old uncertainty message while not-found keeps it and neither resends',async()=>{
  for(const mode of ['lost-found','lost']){const f=fixture(mode);await f.context.render();f.fill();await f.preview();await f.node('confirm-submission').onclick();assert.match(f.node('form-error').textContent,/结果待确认/);await f.node('query-submission-request').onclick({currentTarget:f.node('query-submission-request')});if(mode==='lost-found'){assert.equal(f.node('form-error').textContent,'');assert.match(f.node('submission-request-result').innerHTML,/正式版 v1 已收到/);}else assert.match(f.node('form-error').textContent,/结果待确认/);assert.equal(f.node('material-summary').value,'Summary');assert.equal(f.node('material-summary').readOnly,true);assert.equal(f.calls.filter(call=>call.body).length,1);}
});
