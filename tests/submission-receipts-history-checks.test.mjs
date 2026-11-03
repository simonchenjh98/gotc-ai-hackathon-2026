import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const source=fs.readFileSync(new URL('../platform.js',import.meta.url),'utf8').split("window.addEventListener('hashchange'")[0];
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};};
function fixture(){
  const nodes=new Map(),calls=[];
  function node(id){if(nodes.has(id))return nodes.get(id);const n={id,isConnected:true,disabled:false,hidden:false,textContent:'',forms:[],dataset:{},listeners:{},classList:{add(){},remove(){}},scrollIntoView(){},querySelectorAll:()=>n.forms,querySelector:selector=>selector==='[data-check-refresh]'?node(id+'-refresh'):selector==='[data-check-message]'?node(id+'-message'):null,insertAdjacentHTML(position,value){n.innerHTML=position==='afterbegin'?value+n.innerHTML:n.innerHTML+value;}};
    let html='';Object.defineProperty(n,'innerHTML',{get:()=>html,set(value){html=value;for(const match of value.matchAll(/id="([^"]+)"/g))node(match[1]);n.forms=[...value.matchAll(/<form[^>]*data-check-field="([^"]+)"[^>]*>([\s\S]*?)<\/form>/g)].map(match=>{const content=match[2],button={disabled:false,isConnected:true},error={textContent:''},meta={textContent:''};return {dataset:{checkField:match[1]},isConnected:true,elements:{status:{value:/<option value="([^"]+)" selected/.exec(content)?.[1] || 'unchecked'},note:{value:/<textarea[^>]*>([\s\S]*?)<\/textarea>/.exec(content)?.[1] || ''}},listeners:{},addEventListener(event,fn){this.listeners[event]=fn;},querySelector:selector=>selector==='button'?button:selector==='[data-check-meta]'?meta:error};});}});nodes.set(id,n);return n;}
  const context=vm.createContext({document:{getElementById:node},location:{href:'https://example.test/platform.html#admin'},URL,TextEncoder,Date,Intl,Math,setTimeout,clearTimeout,confirm:()=>true});
  context.transport=async(path,body)=>{calls.push({path,body});return context.respond(path,body);};
  vm.runInContext(source+'\napi=transport;globalThis.h={newSubmissionRequestId,matchingSubmissionReceipt,requestReceiptMarkup,submissionHistoryItemMarkup,materialChecksMarkup,installSubmissionHistory,installMaterialChecks};globalThis.dirty=dirtyAdminForms;',context);
  return {h:context.h,context,node,calls};
}
const receipt={requestId:'request-1234567890',status:'succeeded',action:'submitted',baseVersion:0,version:1,historyVersion:1,savedAt:1234,materials:{summary:'EXACT_REQUEST_MATERIAL',codeVersion:'a'.repeat(40)}};
test('request ids are accepted by the shared contract and exact receipt cannot be inferred from current material',()=>{
  const {h}=fixture(),ids=Array.from({length:20},()=>h.newSubmissionRequestId());assert.equal(new Set(ids).size,20);assert.ok(ids.every(id=>/^[A-Za-z0-9_-]{16,128}$/.test(id)));
  assert.equal(h.matchingSubmissionReceipt(receipt,receipt.requestId,'submitted',0),true);for(const [id,action,base] of [['other-request-12345','submitted',0],[receipt.requestId,'draft',0],[receipt.requestId,'submitted',1]])assert.equal(h.matchingSubmissionReceipt(receipt,id,action,base),false);
  const html=h.requestReceiptMarkup(receipt,receipt.requestId);assert.match(html,/正式版 v1 已收到/);assert.match(html,/EXACT_REQUEST_MATERIAL/);assert.match(html,/可能不是队伍当前最新版/);
  for(const body of [{requestId:receipt.requestId,status:'not_found'},{...receipt,requestId:'another-request-123'}])assert.ok(!h.requestReceiptMarkup(body,receipt.requestId).includes('已收到'));
  assert.match(h.requestReceiptMarkup({requestId:receipt.requestId,status:'not_found'},receipt.requestId),/结果仍待确认/);
  assert.match(h.requestReceiptMarkup({requestId:receipt.requestId,status:'rejected',action:'draft',error:{message:'版本冲突'},decidedAt:1234},receipt.requestId),/服务器已明确拒绝/);
});
test('history keeps the full SHA and states the limited legacy migration honestly',()=>{
  const {h}=fixture(),html=h.submissionHistoryItemMarkup({version:7,submittedAt:1234,requestId:null,origin:'legacy_current',codeVersion:'b'.repeat(64),projectName:'<script>bad</script>'});
  assert.match(html,/b{64}/);assert.match(html,/无法还原更早版本/);assert.match(html,/旧记录无请求编号/);assert.ok(!html.includes('<script>'));
});
test('real history controller pages and displays immutable selected version without touching draft inputs',async()=>{
  const f=fixture(),container=f.node('history'),input=f.node('unsaved-draft');input.value='UNSAVED';f.context.respond=async path=>path.endsWith('/1')?{version:1,submittedAt:100,requestId:receipt.requestId,origin:'submitted',materials:{summary:'OLD_IMMUTABLE'}}:{items:[{version:path.includes('?')?1:3,submittedAt:100,origin:'submitted',requestId:receipt.requestId,codeVersion:'a'.repeat(40)}],nextBeforeVersion:path.includes('?')?null:3};
  f.h.installSubmissionHistory(container,'submission/history',()=>true);await f.node('history-load').onclick({currentTarget:f.node('history-load')});await f.node('history-load').onclick({currentTarget:f.node('history-load')});
  assert.deepEqual(f.calls.map(call=>call.path),['submission/history','submission/history?beforeVersion=3']);assert.equal(f.node('history-load').hidden,true);
  await f.node('history-list').onclick({target:{closest:()=>({dataset:{historyVersion:'1'},isConnected:true,disabled:false})}});assert.match(f.node('history-detail').innerHTML,/OLD_IMMUTABLE/);assert.equal(input.value,'UNSAVED');assert.equal(f.calls.filter(call=>call.body).length,0);
});
const checkData=(more={})=>({teamId:'T1',version:2,latestVersion:2,items:[{field:'codeUrl',url:'https://example.test/repo',status:'unchecked',revision:0,checkedAt:null,actor:null,note:''},{field:'slidesUrl',url:'https://example.test/slides',status:'unchecked',revision:0,checkedAt:null,actor:null,note:''}],...more});
test('old checks are visibly read-only and unchecked is independent from inaccessible',()=>{
  const {h}=fixture(),html=h.materialChecksMarkup(checkData({latestVersion:3}));assert.match(html,/旧版检查，只读/);assert.match(html,/新版本不会继承旧版检查/);assert.match(html,/未检查不代表无法访问/);assert.ok(!html.includes('type="submit"'));assert.match(html,/name="status" disabled/);assert.match(html,/maxlength="500"/);
});
test('real check editor saves one revision, keeps other dirty inputs and one-time codes, and prevents duplicate writes',async()=>{
  const f=fixture(),container=f.node('checks'),waiting=deferred();f.context.respond=async(path,body)=>body?waiting.promise:checkData();await f.h.installMaterialChecks(container,'T1',2,()=>true);
  const [code,slides]=container.forms;slides.elements.note.value='UNSAVED_SLIDES';slides.listeners.input();f.node('one-time-code').innerHTML='SECRET_CODE_ONLY_IN_UI';code.elements.status.value='permission_required';code.elements.note.value='请补权限';code.listeners.input();
  const saving=code.onsubmit({preventDefault(){}});await code.onsubmit({preventDefault(){}});assert.equal(f.calls.filter(call=>call.body).length,1);assert.deepEqual({...f.calls.at(-1).body},{version:2,field:'codeUrl',expectedRevision:0,status:'permission_required',note:'请补权限'});
  waiting.resolve(checkData({items:checkData().items.map(item=>item.field==='codeUrl'?{...item,status:'permission_required',note:'请补权限',revision:1,checkedAt:1234,actor:{id:'A',name:'管理员'}}:item)}));await saving;
  assert.equal(slides.elements.note.value,'UNSAVED_SLIDES');assert.equal(f.context.dirty.has(slides),true);assert.equal(f.context.dirty.has(code),false);assert.equal(f.node('one-time-code').innerHTML,'SECRET_CODE_ONLY_IN_UI');assert.match(code.querySelector('[data-check-meta]').textContent,/管理员 · 修订 1/);
});
test('check conflicts and late responses preserve unsaved notes and cannot repaint a later view',async()=>{
  const f=fixture(),container=f.node('checks');let live=true;f.context.respond=async(path,body)=>{if(body){const error=new Error('conflict');error.status=409;throw error;}return checkData();};await f.h.installMaterialChecks(container,'T1',2,()=>live);const form=container.forms[0];form.elements.note.value='KEEP_ME';form.listeners.input();await form.onsubmit({preventDefault(){}});assert.equal(form.elements.note.value,'KEEP_ME');assert.equal(f.context.dirty.has(form),true);assert.match(form.querySelector('.form-error').textContent,/本次没有保存/);
  const waiting=deferred();f.context.respond=()=>waiting.promise;const pending=form.onsubmit({preventDefault(){}});live=false;waiting.resolve(checkData());await pending;assert.equal(form.elements.note.value,'KEEP_ME');
});
test('check refresh and history detail reads cannot erase notes edited while their GET was pending',async()=>{
  const f=fixture(),container=f.node('checks');f.context.respond=async()=>checkData();await f.h.installMaterialChecks(container,'T1',2,()=>true);const form=container.forms[0],waiting=deferred();f.context.respond=()=>waiting.promise;const refreshing=f.node('checks-refresh').onclick();form.elements.note.value='NEW_DURING_REFRESH';form.listeners.input();waiting.resolve(checkData());await refreshing;assert.equal(container.forms[0],form);assert.equal(form.elements.note.value,'NEW_DURING_REFRESH');assert.equal(f.context.dirty.has(form),true);assert.match(f.node('checks-message').textContent,/已保留当前输入/);
  const failure=deferred();f.context.respond=()=>failure.promise;const failing=f.node('checks-refresh').onclick();failure.reject(new Error('offline'));await failing;assert.equal(container.forms[0],form);assert.equal(form.elements.note.value,'NEW_DURING_REFRESH');assert.match(f.node('checks-message').textContent,/已保留当前输入/);
  const history=f.node('history'),detail=f.node('history-detail');detail.forms=[form];f.h.installSubmissionHistory(history,'admin/submissions/T1/history',()=>true);const next=deferred();f.context.respond=()=>next.promise;const choosing=f.node('history-list').onclick({target:{closest:()=>({dataset:{historyVersion:'1'},isConnected:true,disabled:false})}});form.elements.note.value='NEW_DURING_VERSION_READ';form.listeners.input();next.resolve({version:1,materials:{summary:'OLD'}});await choosing;assert.equal(detail.forms[0],form);assert.equal(form.elements.note.value,'NEW_DURING_VERSION_READ');assert.match(f.node('history-message').textContent,/已保留当前面板/);
});
test('retained check editor stays active after a failed or superseded version read',async()=>{
  const f=fixture(),history=f.node('history');let editorCurrent;
  f.context.respond=async()=>({version:2,materials:{summary:'CURRENT'}});f.h.installSubmissionHistory(history,'admin/submissions/T1/history',()=>true,{onDetail:(item,target,isCurrent)=>{editorCurrent=isCurrent;}});
  const choose=()=>f.node('history-list').onclick({target:{closest:()=>({dataset:{historyVersion:'2'},isConnected:true,disabled:false})}});await choose();assert.equal(editorCurrent(),true);
  const waiting=deferred();f.context.respond=()=>waiting.promise;const pending=choose();assert.equal(editorCurrent(),true);waiting.reject(new Error('offline'));await pending;assert.equal(editorCurrent(),true);
  const form={editRevision:0},detail=f.node('history-detail');detail.forms=[form];const next=deferred();f.context.respond=()=>next.promise;const skipped=choose();form.editRevision++;next.resolve({version:1,materials:{summary:'OTHER'}});await skipped;assert.equal(editorCurrent(),true);
});
