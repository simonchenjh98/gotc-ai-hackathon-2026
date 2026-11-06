import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source=fs.readFileSync(new URL('../platform-roadshow-access.js',import.meta.url),'utf8');
const platform=fs.readFileSync(new URL('../platform.js',import.meta.url),'utf8').split("window.addEventListener('hashchange'")[0];
const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};};
function element(value=''){
  const listeners=new Map();
  return {value,disabled:false,hidden:false,textContent:'',isConnected:true,addEventListener(name,fn){listeners.set(name,fn);},removeEventListener(name){listeners.delete(name);},dispatchEvent(event){listeners.get(event.type)?.(event);},focus(){this.focused=true;},select(){this.selected=true;}};
}
function runtime(extra={}){
  const owner={role:'admin',loginId:'admin-one'},ctx=vm.createContext({AbortController,Event,URL,Set,setTimeout,clearTimeout,esc,base:new URL('https://example.test/gotc/'),session:owner,dirtyAdminForms:new Set(),ownsBrowserLogin:()=>true,confirm:()=>true,renderRoute(){},...extra});
  vm.runInContext(source+'\nglobalThis.helpers={makeRoadshowClaimController,roadshowPublicRequest,roadshowClaimMarkup,roadshowRosterMarkup,installRoadshowRoster};',ctx);return ctx;
}
function claimHarness(extra={}){
  const controls=Object.fromEntries(['.form-error','[data-roadshow-availability]','[data-roadshow-result]','[data-roadshow-name]','[data-roadshow-claim-button]','[data-roadshow-refresh]','[data-roadshow-copy]','[data-roadshow-fill]'].map(key=>[key,element()]));
  controls['[data-roadshow-result]'].hidden=true;
  const form={isConnected:true,elements:{judgeName:element(),claimedCode:element()},querySelector:key=>controls[key]};
  const login={isConnected:true,elements:{code:element()}},calls=[];let active=true;
  const ctx=runtime();const controller=ctx.helpers.makeRoadshowClaimController({form,loginForm:login,isCurrent:()=>active,load:async()=>({configured:true,claimAvailable:true,message:'Ready'}),claim:async(body,signal)=>{calls.push({body,signal});return {name:body.name,code:'GJ.DEDICATED'};},copy:async code=>calls.push({copied:code}),...extra});
  return {ctx,controller,form,login,controls,calls,name:form.elements.judgeName,code:form.elements.claimedCode,setActive:value=>active=value,error:controls['.form-error'],button:controls['[data-roadshow-claim-button]']};
}

test('an empty roadshow roster cannot issue an invitation or accept an arbitrary name',async()=>{
  const h=claimHarness({load:async()=>({configured:false,claimAvailable:false,message:'名单待配置'})});await h.controller.reload();
  assert.equal(h.name.disabled,true);assert.equal(h.button.disabled,true);assert.match(h.controls['[data-roadshow-availability]'].textContent,/名单待配置/);
  h.name.value='Unknown';await h.controller.claim();assert.equal(h.calls.length,0);assert.equal(h.code.value,'');h.controller.dispose();
});

test('claiming returns a private code without logging in; fill and copy require explicit clicks',async()=>{
  const h=claimHarness();await h.controller.reload();h.name.value='  Judge One  ';await h.controller.claim();
  assert.equal(h.calls.length,1);assert.equal(h.calls[0].body.name,'Judge One');assert.equal(h.login.elements.code.value,'');assert.equal(h.code.value,'GJ.DEDICATED');
  h.controls['[data-roadshow-fill]'].onclick();assert.equal(h.login.elements.code.value,'GJ.DEDICATED');assert.equal(h.login.elements.code.focused,true);
  await h.controls['[data-roadshow-copy]'].onclick();assert.equal(h.calls.at(-1).copied,'GJ.DEDICATED');h.controller.dispose();
  assert.equal(h.code.value,'');assert.equal(h.login.elements.code.value,'');assert.equal(h.controls['[data-roadshow-result]'].hidden,true);
});

test('duplicate claim submits stay single flight and editing the name discards a late code',async()=>{
  const gate=deferred();let sent=0;
  const h=claimHarness({claim:()=>{sent++;return gate.promise;}});await h.controller.reload();h.name.value='Judge One';const saving=h.controller.claim();await h.controller.claim();
  h.name.value='Judge Two';h.name.dispatchEvent(new Event('input'));gate.resolve({name:'Judge One',code:'OLD.CODE'});await saving;
  assert.equal(sent,1);assert.equal(h.name.value,'Judge Two');assert.equal(h.code.value,'');assert.match(h.error.textContent,/姓名已变化/);assert.equal(h.button.disabled,false);h.controller.dispose();
});

test('leaving the page aborts a pending claim and a late response never restores secrets',async()=>{
  const gate=deferred();let signal;
  const h=claimHarness({claim:(_body,input)=>{signal=input;return gate.promise;}});await h.controller.reload();h.name.value='Judge One';const saving=h.controller.claim();
  h.controller.dispose();assert.equal(signal.aborted,true);gate.resolve({name:'Judge One',code:'LATE.CODE'});await saving;
  assert.equal(h.code.value,'');assert.equal(h.controls['[data-roadshow-name]'].textContent,'');assert.equal(h.login.elements.code.value,'');
});

test('changing a name clears only a code inserted by the claim flow, preserving manual login input',async()=>{
  const h=claimHarness();await h.controller.reload();h.name.value='Judge One';await h.controller.claim();h.controls['[data-roadshow-fill]'].onclick();
  h.login.elements.code.value='MANUALLY_TYPED';h.name.value='Judge Two';h.name.dispatchEvent(new Event('input'));
  assert.equal(h.code.value,'');assert.equal(h.login.elements.code.value,'MANUALLY_TYPED');h.controller.dispose();
});

test('claim failures retain the name, report the error, and never automatically retry a write',async()=>{
  let writes=0;const h=claimHarness({claim:async()=>{writes++;throw new Error('network disconnected');}});await h.controller.reload();h.name.value='Judge One';await h.controller.claim();
  assert.equal(writes,1);assert.equal(h.name.value,'Judge One');assert.match(h.error.textContent,/network disconnected/);assert.equal(h.button.disabled,false);assert.equal(h.code.value,'');h.controller.dispose();
});

test('public claim calls omit cookies and cannot be reused for a management endpoint',async()=>{
  const calls=[],ctx=runtime({fetch:async(url,options)=>{calls.push({url:String(url),options});return {ok:true,json:async()=>({configured:false})};}});
  await ctx.helpers.roadshowPublicRequest('roadshow/access');await ctx.helpers.roadshowPublicRequest('roadshow/claim',{name:'Judge One'});
  assert.ok(calls.every(call=>call.options.credentials==='omit' && call.options.cache==='no-store'));
  assert.equal(calls[1].options.headers['Content-Type'],'application/json');assert.deepEqual(JSON.parse(calls[1].options.body),{name:'Judge One'});
  await assert.rejects(ctx.helpers.roadshowPublicRequest('admin/roadshow-access',{}));assert.equal(calls.length,2);
});

test('clipboard failure offers manual selection and an unavailable roster hides an already claimed code',async()=>{
  let available=true;const h=claimHarness({load:async()=>({configured:available,claimAvailable:available}),copy:async()=>{throw new Error('denied');}});
  await h.controller.reload();h.name.value='Judge One';await h.controller.claim();await h.controls['[data-roadshow-copy]'].onclick();assert.equal(h.code.selected,true);assert.match(h.error.textContent,/自动复制不可用/);
  available=false;await h.controller.reload();assert.equal(h.code.value,'');assert.equal(h.controls['[data-roadshow-result]'].hidden,true);h.controller.dispose();
});

function adminHarness(api){
  const field=element('Judge One'),error=element(),save=element(),reload=element(),link=element();
  const form={isConnected:true,elements:{names:field},querySelector:key=>({'.form-error':error,'[type=submit]':save,'[data-roadshow-reload]':reload}[key])};
  const ctx=runtime({api}),view={owner:ctx.session,current:()=>true,cleanups:[],container:{querySelector:key=>key==='[data-roadshow-roster]'?form:link}};
  ctx.helpers.installRoadshowRoster(view,{revision:4,runId:'RUN1',editable:true,names:['Judge One']});return {ctx,view,form,field,error,save,reload,link};
}

test('roster save carries the captured run and administrator identity, preserving edits made while pending',async()=>{
  const gate=deferred(),calls=[];const h=adminHarness((path,body,options)=>{calls.push({path,body,options});return calls.length===1?gate.promise:Promise.resolve({revision:6,runId:'RUN1',editable:true,names:body.names});});
  const pending=h.form.onsubmit({preventDefault(){}});h.field.value='Judge One\nJudge Two';h.field.dispatchEvent(new Event('input'));
  gate.resolve({revision:5,runId:'RUN1',editable:true,names:['Judge One']});await pending;
  assert.equal(calls[0].path,'admin/roadshow-access');assert.equal(calls[0].body.expectedRevision,4);assert.equal(calls[0].body.runId,'RUN1');assert.equal(calls[0].options.loginId,'admin-one');
  assert.equal(h.field.value,'Judge One\nJudge Two');assert.equal(h.ctx.dirtyAdminForms.has(h.form),true);assert.match(h.error.textContent,/新输入已保留/);
  await h.form.onsubmit({preventDefault(){}});assert.equal(calls[1].body.expectedRevision,5);assert.equal(calls[1].body.names.length,2);assert.equal(h.ctx.dirtyAdminForms.has(h.form),false);
});

test('roster conflicts retain input and block resubmission until an explicit reread',async()=>{
  let saves=0,rereads=0;const h=adminHarness(async()=>{saves++;throw Object.assign(new Error('名单已变化'),{status:409});});h.ctx.renderRoute=()=>rereads++;
  await h.form.onsubmit({preventDefault(){}});await h.form.onsubmit({preventDefault(){}});
  assert.equal(saves,1);assert.equal(h.field.value,'Judge One');assert.equal(h.save.disabled,true);assert.match(h.error.textContent,/当前输入已保留/);
  h.ctx.confirm=()=>false;h.reload.onclick();assert.equal(rereads,0);h.ctx.confirm=()=>true;h.reload.onclick();assert.equal(rereads,1);
});

test('a switched administrator cannot receive or resend the old roster save response',async()=>{
  const gate=deferred(),h=adminHarness(()=>gate.promise);const pending=h.form.onsubmit({preventDefault(){}});
  h.ctx.session={role:'admin',loginId:'admin-two'};gate.resolve({revision:5,runId:'RUN1',editable:true,names:['OTHER']});await pending;
  assert.equal(h.field.value,'Judge One');assert.equal(h.save.disabled,true);assert.match(h.error.textContent,/登录已失效或切换/);
});

test('roster rendering escapes stored judge names and never contains invitation material',()=>{
  const ctx=runtime(),html=ctx.helpers.roadshowRosterMarkup({configured:true,editable:true,names:['<img src=x onerror=alert(1)>'],judges:[{name:'<script>evil</script>',active:true,claimed:true,canClaim:true}]});
  assert.ok(!html.includes('<img'));assert.ok(!html.includes('<script>'));assert.match(html,/&lt;img/);assert.match(html,/&lt;script&gt;/);assert.ok(!html.includes('claimedCode'));
});

test('the roadshow administrator route requires an administrator while the finals route remains independent',async()=>{
  for(const role of [null,'judge','admin']){
    const calls=[],main={innerHTML:''};
    const ctx=vm.createContext({document:{getElementById:()=>main,querySelectorAll:()=>[],body:{classList:{remove(){},add(){}}}},location:{href:'https://example.test/platform.html#admin-roadshow',hash:'#admin-roadshow'},URL,setTimeout,clearTimeout,calls,renderRoadshowAccessAdmin:async()=>calls.push('roster')});
    vm.runInContext(platform+`\nsession={role:${JSON.stringify(role)}};account=()=>{};renderLogin=r=>calls.push('login:'+r);renderWrongRole=r=>calls.push('wrong:'+r);`,ctx);
    await vm.runInContext('renderRoute()',ctx);assert.deepEqual(calls,[role==='admin'?'roster':role===null?'login:admin':'wrong:admin']);
  }
});
