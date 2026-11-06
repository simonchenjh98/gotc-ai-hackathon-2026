import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source=fs.readFileSync(new URL('../platform-roadshow-access.js',import.meta.url),'utf8');
const platform=fs.readFileSync(new URL('../platform.js',import.meta.url),'utf8').split("window.addEventListener('hashchange'")[0];
const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};};
const flush=()=>new Promise(resolve=>setImmediate(resolve));
function element(value=''){
  const listeners=new Map();
  return {value,disabled:false,hidden:false,textContent:'',isConnected:true,addEventListener(name,fn){listeners.set(name,fn);},removeEventListener(name){listeners.delete(name);},dispatchEvent(event){listeners.get(event.type)?.(event);},focus(){this.focused=true;}};
}
function runtime(extra={}){
  const owner={role:'admin',loginId:'admin-one'},ctx=vm.createContext({AbortController,Event,URL,Set,setTimeout,clearTimeout,esc,base:new URL('https://example.test/gotc/'),session:owner,dirtyAdminForms:new Set(),ownsBrowserLogin:()=>true,confirm:()=>true,renderRoute(){},...extra});
  vm.runInContext(source+'\nglobalThis.helpers={makeRoadshowNameLoginController,roadshowNameRequest,roadshowNameLoginMarkup,roadshowRosterMarkup,installRoadshowRoster};',ctx);return ctx;
}
function loginHarness(extra={},runtimeExtra={}){
  const controls=Object.fromEntries(['.form-error','[data-roadshow-availability]','[data-roadshow-login-button]','[data-roadshow-refresh]'].map(key=>[key,element()]));
  const form={isConnected:true,elements:{judgeName:element()},querySelector:key=>controls[key]},calls=[];let active=true;
  const ctx=runtime(runtimeExtra),controller=ctx.helpers.makeRoadshowNameLoginController({form,isCurrent:()=>active,load:async()=>({configured:true,nameLoginAvailable:true,message:'Ready'}),login:async(body,signal)=>{calls.push({body,signal});return {ok:true,role:'judge',name:body.name,loginId:'judge-epoch'};},accept:async(result,signal,valid)=>{if(valid())calls.push({accepted:result,signal});},...extra});
  return {ctx,controller,form,controls,calls,name:form.elements.judgeName,setActive:value=>active=value,error:controls['.form-error'],button:controls['[data-roadshow-login-button]']};
}

test('empty or unavailable roadshow rosters cannot submit arbitrary names',async()=>{
  const h=loginHarness({load:async()=>({configured:false,nameLoginAvailable:false,message:'名单待配置'})});await h.controller.reload();
  assert.equal(h.name.disabled,true);assert.equal(h.button.disabled,true);assert.match(h.controls['[data-roadshow-availability]'].textContent,/名单待配置/);
  h.name.value='Unknown';await h.controller.login();assert.equal(h.calls.length,0);h.controller.dispose();
});

test('roadshow login is one real-name invitation field and never asks for a random code or identity selection',()=>{
  const html=runtime().helpers.roadshowNameLoginMarkup();assert.match(html,/邀请码（真实姓名）/);assert.match(html,/验证姓名并登录/);assert.equal((html.match(/<input /g)||[]).length,1);
  assert.ok(!html.includes('<select'));assert.ok(!html.includes('领取'));assert.ok(!html.includes('复制'));assert.ok(!html.includes('type="password"'));
});

test('only the roadshow login branch delegates to the real-name form',()=>{
  const calls=[],main={innerHTML:''},ctx=vm.createContext({document:{getElementById:()=>main},location:{href:'https://example.test/platform.html#judge-roadshow'},URL,setTimeout,clearTimeout,calls});
  vm.runInContext(platform+"\nroute='judge-roadshow';routeVersion=7;renderRoadshowNameLogin=version=>calls.push(version);renderLogin('judge');",ctx);
  assert.deepEqual(calls,[7]);assert.equal(main.innerHTML,'');
});

test('a listed full name submits once and passes the verified judge result to session confirmation',async()=>{
  const h=loginHarness();await h.controller.reload();h.name.value='  陈计宏  ';await h.controller.login();
  assert.equal(h.calls[0].body.name,'陈计宏');assert.equal(h.calls[1].accepted.role,'judge');assert.equal(h.calls[1].accepted.loginId,'judge-epoch');assert.equal(h.name.value,'  陈计宏  ');h.controller.dispose();assert.equal(h.name.value,'');
});

test('double clicks stay single flight and a name changed while pending cannot choose an old identity',async()=>{
  const gate=deferred();let writes=0,accepted=0;const h=loginHarness({login:()=>{writes++;return gate.promise;},accept:async()=>accepted++});
  await h.controller.reload();h.name.value='Judge One';const pending=h.controller.login();await h.controller.login();assert.equal(h.name.disabled,true);
  h.name.value='Judge Two';h.name.dispatchEvent(new Event('input'));gate.resolve({role:'judge',loginId:'old-epoch'});await pending;
  assert.equal(writes,1);assert.equal(accepted,0);assert.match(h.error.textContent,/姓名已变化/);h.controller.dispose();
});

test('departure aborts pending login and cannot apply its late identity',async()=>{
  const gate=deferred();let signal,accepted=0;const h=loginHarness({login:(_body,input)=>{signal=input;return gate.promise;},accept:async()=>accepted++});
  await h.controller.reload();h.name.value='Judge One';const pending=h.controller.login();h.controller.dispose();assert.equal(signal.aborted,true);
  gate.resolve({role:'judge',loginId:'late-epoch'});await pending;assert.equal(accepted,0);assert.equal(h.name.value,'');
});

test('unknown or disabled names retain input and never retry automatically',async()=>{
  for(const status of [404,403,429]){let writes=0;const h=loginHarness({login:async()=>{writes++;throw Object.assign(new Error('请核对名单'),{status});}});await h.controller.reload();h.name.value='Judge One';await h.controller.login();assert.equal(writes,1);assert.equal(h.name.value,'Judge One');assert.match(h.error.textContent,/请核对名单/);assert.equal(h.button.disabled,false);h.controller.dispose();}
});

test('name login has an eight-second abort deadline and does not automatically resend',async()=>{
  const tasks=new Map();let seq=0,writes=0;const h=loginHarness({login:(_body,signal)=>{writes++;return new Promise((_resolve,reject)=>signal.addEventListener('abort',()=>reject(Object.assign(new Error('abort'),{name:'AbortError'}))));}},{setTimeout:(fn,ms)=>{tasks.set(++seq,{fn,ms});return seq;},clearTimeout:id=>tasks.delete(id)});
  await h.controller.reload();h.name.value='Judge One';const pending=h.controller.login();assert.equal([...tasks.values()][0].ms,8000);[...tasks.values()][0].fn();await pending;
  assert.equal(writes,1);assert.match(h.error.textContent,/没有自动重发/);assert.equal(h.name.value,'Judge One');assert.equal(tasks.size,0);h.controller.dispose();
});

test('login transport accepts cookies only on the dedicated name endpoint and rejects management paths',async()=>{
  const sent=[],ctx=runtime({fetch:async(url,options)=>{sent.push({url,options});return {ok:true,json:async()=>({})};}});
  await ctx.helpers.roadshowNameRequest('roadshow/access');await ctx.helpers.roadshowNameRequest('roadshow/login',{name:'陈计宏'});
  assert.equal(sent[0].options.credentials,'omit');assert.equal(sent[1].options.credentials,'same-origin');assert.equal(sent[1].options.method,'POST');assert.equal(JSON.parse(sent[1].options.body).name,'陈计宏');
  await assert.rejects(ctx.helpers.roadshowNameRequest('admin/roadshow-access',{}),/入口不正确/);assert.equal(sent.length,2);
});

function realLoginHarness({post,confirm,storageBlocked=false}={}){
  const controls=Object.fromEntries(['.form-error','[data-roadshow-availability]','[data-roadshow-login-button]','[data-roadshow-refresh]'].map(key=>[key,element()])),form={isConnected:true,elements:{judgeName:element()},querySelector:key=>controls[key]};
  let marker='before-login';const published=[],renders=[];const ctx=runtime({route:'judge-roadshow',routeVersion:4,session:{role:null},root:{innerHTML:'',querySelector:()=>form},browserLoginOwner:()=>marker,publishLogin:result=>{published.push(result.loginId);if(!storageBlocked)marker=result.loginId;},renderRoute:async()=>renders.push('scoring'),api:confirm || (async()=>({role:'judge',loginId:'new-login',name:'陈计宏'})),fetch:async(url,options)=>({ok:true,json:async()=>String(url).endsWith('/login')?(post?post():{ok:true,role:'judge',loginId:'new-login',name:'陈计宏'}):{configured:true,nameLoginAvailable:true}})});
  const controller=ctx.renderRoadshowNameLogin(4);return {ctx,controller,form,published,renders,error:controls['.form-error'],setMarker:value=>marker=value};
}

test('real-name login publishes its marker then verifies the matching judge cookie before opening scoring',async()=>{
  const h=realLoginHarness();await flush();h.form.elements.judgeName.value='陈计宏';await h.controller.login();
  assert.deepEqual(h.published,['new-login']);assert.equal(h.ctx.session.role,'judge');assert.equal(h.ctx.session.loginId,'new-login');assert.deepEqual(h.renders,['scoring']);h.controller.dispose();
});

test('a failed storage write with a readable old marker still accepts the matching verified judge cookie',async()=>{
  const h=realLoginHarness({storageBlocked:true});await flush();h.form.elements.judgeName.value='陈计宏';await h.controller.login();
  assert.deepEqual(h.published,['new-login']);assert.equal(h.ctx.browserLoginOwner(),'before-login');assert.equal(h.ctx.session.loginId,'new-login');assert.deepEqual(h.renders,['scoring']);h.controller.dispose();
});

test('storage failure never bypasses marker changes while the judge cookie is being verified',async()=>{
  const check=deferred(),h=realLoginHarness({storageBlocked:true,confirm:()=>check.promise});await flush();h.form.elements.judgeName.value='陈计宏';const pending=h.controller.login();await flush();
  h.setMarker('new-login-in-another-tab');check.resolve({role:'judge',loginId:'new-login'});await pending;assert.equal(h.ctx.session.role,null);assert.deepEqual(h.renders,[]);h.controller.dispose();
});

test('a mismatched cookie confirmation cannot open scoring or adopt the other identity',async()=>{
  const h=realLoginHarness({confirm:async()=>({role:'judge',loginId:'someone-else'})});await flush();h.form.elements.judgeName.value='陈计宏';await h.controller.login();
  assert.equal(h.ctx.session.role,null);assert.deepEqual(h.renders,[]);assert.match(h.error.textContent,/身份与刚验证的姓名不一致/);h.controller.dispose();
});

test('late name login and confirmation cannot overwrite a new route or another tab login',async()=>{
  const post=deferred(),h=realLoginHarness({post:()=>post.promise});await flush();h.form.elements.judgeName.value='陈计宏';const pending=h.controller.login();h.ctx.routeVersion++;post.resolve({role:'judge',loginId:'new-login'});await pending;assert.deepEqual(h.published,[]);assert.deepEqual(h.renders,[]);h.controller.dispose();
  const check=deferred(),other=realLoginHarness({confirm:()=>check.promise});await flush();other.form.elements.judgeName.value='陈计宏';const waiting=other.controller.login();await flush();other.setMarker('other-tab');check.resolve({role:'judge',loginId:'new-login'});await waiting;assert.deepEqual(other.renders,[]);assert.equal(other.ctx.session.role,null);other.controller.dispose();
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

test('name-roster reload protects a clean sibling team form while its save is pending',()=>{
  const h=adminHarness();let reloads=0;h.ctx.renderRoute=()=>reloads++;
  h.ctx.activeFinalsView={forms:new Set([{isConnected:true,finalsBusy:true}])};h.reload.onclick();
  assert.equal(reloads,0);assert.match(h.error.textContent,/正在保存/);
});

test('name-roster reload checks dirty connected sibling forms while ignoring departed forms',()=>{
  const h=adminHarness();let reloads=0;h.ctx.renderRoute=()=>reloads++;h.ctx.confirm=()=>false;
  const sibling={isConnected:true};h.ctx.dirtyAdminForms.add(sibling);h.reload.onclick();assert.equal(reloads,0);
  sibling.isConnected=false;h.reload.onclick();assert.equal(reloads,1);
});

test('a switched administrator cannot receive or resend the old roster save response',async()=>{
  const gate=deferred(),h=adminHarness(()=>gate.promise);const pending=h.form.onsubmit({preventDefault(){}});
  h.ctx.session={role:'admin',loginId:'admin-two'};gate.resolve({revision:5,runId:'RUN1',editable:true,names:['OTHER']});await pending;
  assert.equal(h.field.value,'Judge One');assert.equal(h.save.disabled,true);assert.match(h.error.textContent,/登录已失效或切换/);
});

test('roster rendering escapes stored judge names and never contains invitation material',()=>{
  const ctx=runtime(),html=ctx.helpers.roadshowRosterMarkup({configured:true,editable:true,names:['<img src=x onerror=alert(1)>'],judges:[{name:'<script>evil</script>',active:true,canLogin:true}]});
  assert.ok(!html.includes('<img'));assert.ok(!html.includes('<script>'));assert.match(html,/&lt;img/);assert.match(html,/&lt;script&gt;/);assert.ok(!html.includes('claimedCode'));assert.match(html,/登录资格/);assert.match(html,/可用真实姓名登录/);assert.ok(!html.includes('领取进度'));assert.ok(!html.includes('首次登录'));
});

test('the roadshow administrator route requires an administrator while the finals route remains independent',async()=>{
  for(const role of [null,'judge','admin']){
    const calls=[],main={innerHTML:''};
    const ctx=vm.createContext({document:{getElementById:()=>main,querySelectorAll:()=>[],body:{classList:{remove(){},add(){}}}},location:{href:'https://example.test/platform.html#admin-roadshow',hash:'#admin-roadshow'},URL,setTimeout,clearTimeout,calls,renderRoadshowAccessAdmin:async()=>calls.push('roster')});
    vm.runInContext(platform+`\nsession={role:${JSON.stringify(role)}};account=()=>{};renderLogin=r=>calls.push('login:'+r);renderWrongRole=r=>calls.push('wrong:'+r);`,ctx);
    await vm.runInContext('renderRoute()',ctx);assert.deepEqual(calls,[role==='admin'?'roster':role===null?'login:admin':'wrong:admin']);
  }
});
