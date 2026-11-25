import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const frontend = new URL('../../frontend/', import.meta.url);
const page = readFileSync(new URL('platform.html', frontend), 'utf8');
const settle = () => new Promise(resolve => setImmediate(resolve));

// Use the HTML entry point and the browser's separate classic-script evaluation
// model. Only DOM operations and HTTP are simulated; the router, renderer and
// loader run exactly as shipped.
async function openPage(hash, {beforeAsset, session = {role:null}, replies = {}, respond, storage = new Map(), timers = {setTimeout,clearTimeout}} = {}) {
  const location = new URL('https://example.test/gotc/platform.html' + hash);
  const classList = {add() {}, remove() {}, toggle() {}, contains: () => false};
  function node() {
    const children = new Map(), field = {value:'', addEventListener() {}, removeEventListener() {}};
    return {
      innerHTML:'', isConnected:true, classList, elements:{code:{...field}, accountId:{...field}, name:{...field}, username:{...field}, totp:{...field}, legacyAdmin:{...field,checked:false}},
      addEventListener() {}, removeEventListener() {}, before() {},
      querySelectorAll: () => [],
      querySelector(selector) {
        if (!children.has(selector)) children.set(selector,node());
        return children.get(selector);
      }
    };
  }
  const main = node();
  const nodes = {'platform-main': main, account:node(), retry:node()};
  let markup = '';
  Object.defineProperty(main,'innerHTML',{
    get: () => markup,
    set(value) {
      for (const match of markup.matchAll(/\bid="([^"]+)"/g)) {
        const old = nodes[match[1]];
        if (old) old.isConnected=false;
        delete nodes[match[1]];
      }
      markup=value;
    }
  });
  const requests = [], assets = [], listeners = new Map();
  const window = {
    addEventListener(name, fn) {
      if (!listeners.has(name)) listeners.set(name,new Set());
      listeners.get(name).add(fn);
    },
    removeEventListener(name, fn) {listeners.get(name)?.delete(fn);}
  };
  const document = {
    body: {classList},
    getElementById(id) {
      if (!(id in nodes) && main.innerHTML.includes(`id="${id}"`)) nodes[id] = node();
      return nodes[id] ?? null;
    },
    querySelector: () => null,
    querySelectorAll: () => [],
    createElement: tag => ({tagName: tag.toUpperCase(), remove() {}}),
    head: {appendChild: append, append},
    addEventListener() {}, removeEventListener() {}
  };
  const context = vm.createContext({
    document, location, URL, window, AbortController, Intl, TextEncoder, ...timers,
    sessionStorage: {getItem: () => null},
    localStorage: {getItem:key=>storage.get(key) ?? null,setItem:(key,value)=>storage.set(key,value)},
    fetch: async (url, options) => {
      requests.push(url.pathname);
      const path = url.pathname.slice('/gotc/api/'.length);
      if (respond) {
        const reply = await respond(path, options);
        if (reply) return reply;
      }
      if (path === 'session') return {ok:true, json:async()=>session};
      assert.ok(Object.hasOwn(replies,path),'Unexpected request: '+path);
      assert.equal(options.method,'GET');
      const reply = replies[path];
      const status = reply.status ?? 200;
      return {ok:status<400, status, json:async()=>reply.body};
    }
  });
  function evaluate(path) {
    const url = new URL(path, location);
    assert.equal(url.origin, location.origin);
    assert.ok(url.pathname.startsWith('/gotc/'), url.pathname);
    const file = new URL(url.pathname.slice('/gotc/'.length), frontend);
    return vm.runInContext(readFileSync(file, 'utf8'), context, {filename:file.pathname});
  }
  function append(element) {
    const path = new URL(element.src || element.href, location).pathname;
    assets.push(path);
    Promise.resolve().then(async () => {
      await beforeAsset?.(path);
      if (element.tagName === 'SCRIPT') evaluate(path);
      else readFileSync(new URL(path.slice('/gotc/'.length), frontend));
      element.onload?.();
    }).catch(() => element.onerror?.());
    return element;
  }
  for (const match of page.matchAll(/<script\b[^>]*\bsrc="([^"]+)"[^>]*><\/script>/g)) {
    evaluate(match[1]);
  }
  await settle();
  return {
    main, nodes, requests, assets, context,
    online() { for (const notify of [...(listeners.get('online') || [])]) notify(); },
    async go(nextHash) {
      location.hash = nextHash;
      for (const notify of listeners.get('hashchange')) notify();
      await settle();
    }
  };
}

test('the signed-out team entry renders its login before loading any team or organizer controllers',async()=>{
  const browser=await openPage('#team');
  assert.match(browser.main.innerHTML,/选手工作台/);
  assert.match(browser.main.innerHTML,/本队参赛码/);
  assert.match(browser.main.innerHTML,/参赛码（原提交码）/);
  assert.equal(browser.main.innerHTML.split('提交码').length - 1, 1);
  assert.match(browser.main.innerHTML,/placeholder="输入本队参赛码"/);
  assert.match(browser.main.innerHTML,/id="login-form"/);
  assert.deepEqual(browser.requests,['/gotc/api/session']);
  assert.ok(browser.assets.every(path=>!/(?:\/team\/|\/admin\/|\/gateway\/)/.test(path)));
  assert.equal(vm.runInContext('typeof renderTeam',browser.context),'undefined');
});

function loginReply(status, body, retryAfter=null) {
  return {ok:status<400, status, headers:{get:name=>name==='Retry-After'?retryAfter:null}, json:async()=>body};
}

async function busyLoginPage(replies) {
  let calls=0;
  const login={role:'team',loginId:'fixture-team'};
  const browser=await openPage('#team-score',{respond:path=>{
    if(path==='login') return replies[calls++];
    if(path==='session' && calls) return loginReply(200,login);
    if(path==='team/score') return loginReply(200,{team:{number:2,name:'Fixture'},state:'unpublished'});
  }});
  const timers=[];
  browser.context.setTimeout=(run,ms)=>{timers.push({run,ms});return timers.length;};
  const form=browser.nodes['login-form'];
  form.elements.code.value='fixture-code';
  const submit=()=>form.onsubmit({preventDefault(){}});
  return {browser,form,submit,timers,calls:()=>calls};
}

test('participant login waits for Retry-After and jitter, ignores duplicates, and confirms the retried login',async()=>{
  const h=await busyLoginPage([loginReply(429,{code:'login_busy',error:'Busy'},'2'),
    loginReply(200,{role:'team',loginId:'fixture-team'})]);
  const pending=h.submit();
  await settle();
  assert.equal(h.form.querySelector('button').disabled,true);
  assert.match(h.form.querySelector('.form-error').textContent,/等待.*自动重试/);
  assert.equal(h.timers.length,1);
  assert.ok(h.timers[0].ms>=2000 && h.timers[0].ms<=2500);
  await h.submit();
  assert.equal(h.calls(),1);
  h.timers[0].run();
  await pending;
  assert.equal(h.calls(),2);
  assert.match(h.browser.main.innerHTML,/成绩尚未发布/);
});

test('sign-in stops after three busy retries and leaves the code ready for a manual retry',async()=>{
  const h=await busyLoginPage(Array(4).fill(loginReply(429,{code:'login_busy'},'1')));
  const pending=h.submit();
  for(let attempt=0;attempt<3;attempt++) {
    await settle();
    assert.equal(h.timers.length,attempt+1);
    assert.ok(h.timers[attempt].ms>=Math.max(1000,500*2**attempt));
    h.timers[attempt].run();
  }
  await pending;
  assert.equal(h.calls(),4);
  assert.equal(h.timers.length,3);
  assert.match(h.form.querySelector('.form-error').textContent,/手动重试/);
  assert.equal(h.form.querySelector('button').disabled,false);
  assert.equal(h.form.elements.code.value,'fixture-code');
});

test('sign-in never resubmits rejected credentials or a generic rate limit',async()=>{
  for(const [status,code] of [[401,'login_busy'],[403,'login_busy'],[429,'rate_limit']]) {
    const h=await busyLoginPage([loginReply(status,{code,error:'Fixture rejection'},'2')]);
    await h.submit();
    assert.equal(h.calls(),1);
    assert.equal(h.timers.length,0);
    assert.match(h.form.querySelector('.form-error').textContent,/Fixture rejection/);
  }
});

test('a long Retry-After leaves sign-in available for a later manual attempt',async()=>{
  const h=await busyLoginPage([loginReply(429,{code:'login_busy'},'60')]);
  await h.submit();
  assert.equal(h.calls(),1);
  assert.equal(h.timers.length,0);
  assert.match(h.form.querySelector('.form-error').textContent,/手动重试/);
});

test('changing input, browser identity, or route during backoff cancels the pending login',async()=>{
  for(const change of ['input','identity','route']) {
    const h=await busyLoginPage([loginReply(429,{code:'login_busy'},'1')]);
    const pending=h.submit();
    await settle();
    if(change==='input'){
      h.form.elements.code.value='different';
      h.form.oninput();
      h.form.elements.code.value='fixture-code';
      h.form.oninput();
    }
    if(change==='identity') vm.runInContext("publishLogin({loginId:'other-tab'})",h.browser.context);
    if(change==='route') await h.browser.go('#board-finals');
    h.timers[0].run();
    await pending;
    assert.equal(h.calls(),1);
    if(change==='route') assert.match(h.browser.main.innerHTML,/全部队伍成绩看板已关闭/);
    else assert.match(h.form.querySelector('.form-error').textContent,/变化|切换/);
  }
});

test('opening the public claim route loads its real form on demand and reuses its scripts on return',async()=>{
  const browser=await openPage('#board-finals');
  assert.match(browser.main.innerHTML,/全部队伍成绩看板已关闭/);
  assert.equal(vm.runInContext('typeof renderClaim',browser.context),'undefined');
  await browser.go('#claim');
  assert.match(browser.main.innerHTML,/id="claim-request-form"/);
  assert.equal(vm.runInContext('typeof renderClaim',browser.context),'function');
  const loaded=[...browser.assets];
  assert.ok(loaded.some(path=>path.endsWith('/claims/application.js')));
  assert.equal(new Set(loaded).size,loaded.length);
  await browser.go('#board-roadshow');
  await browser.go('#claim');
  assert.deepEqual(browser.assets,loaded);
  assert.deepEqual(browser.requests,['/gotc/api/session','/gotc/api/claims/schools','/gotc/api/claims/schools']);
  assert.match(browser.main.innerHTML,/id="claim-request-form"/);
});

test('an asset failure permits an explicit retry that displays the requested page',async()=>{
  let fail=true;
  const browser=await openPage('#claim',{beforeAsset:path=>{
    if(fail && path.endsWith('/claims/application.js')){fail=false;throw new Error('offline');}
  }});
  assert.match(browser.main.innerHTML,/id="retry"/);
  await browser.nodes.retry.onclick();
  assert.match(browser.main.innerHTML,/id="claim-request-form"/);
  assert.equal(browser.assets.filter(path=>path.endsWith('/claims/application.js')).length,2);
});

test('public board code loads during the identity check and waits to read event data',async()=>{
  const identity=deferred();
  const browser=await openPage('#board-exhibition',{respond:path=>{
    if(path==='session')return identity.promise;
    if(path==='judging/exhibition/board')return loginReply(503,{error:'Fixture unavailable'});
  }});
  assert.ok(browser.assets.some(path=>path.endsWith('/judging/boards.js')));
  assert.deepEqual(browser.requests,['/gotc/api/session']);
  identity.resolve(loginReply(200,{role:null}));
  await settle();
  assert.ok(browser.requests.includes('/gotc/api/judging/exhibition/board'));
  await browser.go('#board-finals');
});

test('leaving a cold public route does not hold the new login screen behind its download',async()=>{
  const identity=deferred(),asset=deferred();
  const browser=await openPage('#claim',{
    respond:path=>path==='session'?identity.promise:undefined,
    beforeAsset:path=>path.endsWith('/claims/application.js')?asset.promise:undefined
  });
  await browser.go('#team');
  identity.resolve(loginReply(200,{role:null}));
  await settle();
  assert.match(browser.main.innerHTML,/id="login-form"/);
  asset.reject(new Error('Network offline'));
  await settle();
  assert.match(browser.main.innerHTML,/id="login-form"/);
  assert.deepEqual(browser.requests,['/gotc/api/session']);
});

test('a delayed script from a previous route cannot repaint the newly selected page',async()=>{
  let release;
  const pending=new Promise(resolve=>{release=resolve;});
  const browser=await openPage('#team',{beforeAsset:path=>path.endsWith('/claims/application.js')?pending:undefined});
  await browser.go('#claim');
  assert.ok(browser.assets.some(path=>path.endsWith('/claims/application.js')));
  await browser.go('#board-finals');
  release();
  await settle();
  assert.match(browser.main.innerHTML,/全部队伍成绩看板已关闭/);
  assert.doesNotMatch(browser.main.innerHTML,/id="claim-request-form"/);
});

const serviceUnavailable={status:503,body:{error:'Fixture service unavailable'}};
const noTeams={body:{teams:[]}};
const team={number:4,name:'Synthetic team'};

for(const [route,role,replies,markup] of [
  ['team','team',{team:serviceUnavailable},/Fixture service unavailable/],
  ['team','admin',{'admin/team-workbench':noTeams},/选择要进入的队伍/],
  ['team-score','team',{'team/score':{body:{state:'published',team,score:83.5}}},/83\.50/],
  ['team-profile','team',{'team/profile':serviceUnavailable},/本队资料与成员/],
  ['judge','judge',{judge:{body:{teams:[],myScores:[]}}},/还没有可评审的队伍/],
  ['judge-exhibition','judge',{'judging/exhibition':{body:{teams:[],myReviews:[],stage:{id:'exhibition',name:'展示评审'},window:{}}}},/本轮还没有可评审队伍/],
  ['board-exhibition',null,{'judging/exhibition/board':noTeams},/展示评审进度/],
  ['audience','audience',{audience:noTeams},/队伍即将出现在这里/],
  ['judge-roadshow','judge',{'roadshow/scoring':serviceUnavailable},/Fixture service unavailable/],
  ['judge-finals',null,{'finals/login-status':{body:{loginAvailable:false}}},/决赛评委验证/],
  ['admin','admin',{admin:serviceUnavailable,'admin/judging/exhibition':serviceUnavailable},/Fixture service unavailable/],
  ['admin-roadshow','admin',{'admin/roadshow-access':serviceUnavailable},/Fixture service unavailable/],
  ['admin-finals','admin',{'admin/finals':serviceUnavailable},/Fixture service unavailable/],
  ['admin-api','admin',{admin:serviceUnavailable,'admin/keys':serviceUnavailable},/Fixture service unavailable/]
]){
  test(`cold #${route} entry as ${role || 'visitor'} loads its renderer and requests its own data`,async()=>{
    const browser=await openPage('#'+route,{session:{role,loginId:role&&'fixture-'+role},replies});
    try {
      assert.deepEqual(browser.requests,['session',...Object.keys(replies)].map(path=>'/gotc/api/'+path));
      assert.match(browser.main.innerHTML,markup);
      if(route==='board-exhibition')assert.match(browser.nodes['live-status'].innerHTML,/已连接/);
      if(route==='team-profile')assert.match(browser.nodes['team-profile-panel'].innerHTML,/Fixture service unavailable/);
    } finally {
      await browser.go('#board-finals');
    }
  });
}

test('session timeout presents reconnect and retries only the identity GET before entering the latest route',async()=>{
  const timers=new Map(), checked=deferred(), methods=[];
  let calls=0;
  const browser=await openPage('#admin',{timers:{
    setTimeout(fn,ms){timers.set(fn,ms);return fn;},clearTimeout(fn){timers.delete(fn);}
  },respond:(path,options)=>{
    methods.push(options.method);
    if(path==='session' && ++calls===1){
      return new Promise((_,reject)=>options.signal.addEventListener('abort',()=>reject(options.signal.reason)));
    }
    if(path==='session')return checked.promise;
    if(path==='audience')return loginReply(200,noTeams.body);
  }});
  assert.equal(timers.size,1);
  const [[expire,ms]]=timers;
  assert.equal(ms,12000);
  expire();await settle();
  assert.match(browser.main.innerHTML,/连接超时/);
  assert.doesNotMatch(browser.main.innerHTML,/id="login-form"/);
  assert.deepEqual(browser.assets,[]);
  const retry=browser.nodes.retry.onclick;
  browser.online();
  const duplicate=retry();
  await browser.go('#audience');
  assert.deepEqual(browser.requests,['/gotc/api/session','/gotc/api/session']);
  checked.resolve(loginReply(200,{role:'audience',loginId:'current-identity'}));
  await duplicate;await settle();
  browser.online();
  assert.deepEqual(browser.requests,['/gotc/api/session','/gotc/api/session','/gotc/api/audience']);
  assert.deepEqual(methods,['GET','GET','GET']);
  assert.match(browser.main.innerHTML,/队伍即将出现在这里/);
  assert.equal(timers.size,0);
});
test('server failures permit a manual session retry before offering the sign-in form',async()=>{
  let calls=0;
  const browser=await openPage('#team',{respond:path=>{
    if(path==='session' && ++calls===1)return loginReply(503,{error:'Temporarily unavailable'});
  }});
  assert.match(browser.main.innerHTML,/尚未确认登录身份/);
  assert.doesNotMatch(browser.main.innerHTML,/id="login-form"/);
  await browser.nodes.retry.onclick();
  assert.match(browser.main.innerHTML,/id="login-form"/);
  assert.deepEqual(browser.requests,['/gotc/api/session','/gotc/api/session']);
});

test('navigation during session bootstrap opens the latest route with the restored identity',async()=>{
  let restore;
  const session=new Promise(resolve=>{restore=resolve;});
  const browser=await openPage('#team-score',{session,replies:{audience:noTeams}});
  assert.deepEqual(browser.requests,['/gotc/api/session']);
  await browser.go('#audience');
  assert.doesNotMatch(browser.main.innerHTML,/id="login-form"/);
  assert.deepEqual(browser.requests,['/gotc/api/session']);
  restore({role:'audience',loginId:'known-audience'});
  await settle();
  assert.deepEqual(browser.requests,['/gotc/api/session','/gotc/api/audience']);
  assert.match(browser.main.innerHTML,/队伍即将出现在这里/);
});

function deferred() {
  let resolve,reject;
  const promise=new Promise((done,fail)=>{resolve=done;reject=fail;});
  return {promise,resolve,reject};
}
const ownerKey='gotc-login-owner:v1:'+encodeURIComponent('/gotc/');
const ok=body=>({ok:true,status:200,json:async()=>body});

async function loginFixture(role,route,delay='confirmation',returnedRole=role,legacy=false) {
  const storage=new Map(), pending=deferred();
  const mine={role:returnedRole,loginId:'submitted-code-login'};
  let lookups=0;
  const browser=await openPage('#'+route,{storage,replies:{team:serviceUnavailable,'admin/team-workbench':noTeams},respond:(path,options)=>{
    if(path==='login'){
      assert.equal(options.method,'POST');
      assert.deepEqual(JSON.parse(options.body),{role,code:'fixture-entry',...(role==='admin' && !legacy?{username:'operator.test',totp:'000123'}:{})});
      return delay==='post'?pending.promise:ok(mine);
    }
    if(path==='session' && ++lookups>1)return delay==='confirmation'?pending.promise:ok(mine);
  }});
  const form=browser.nodes['login-form'];
  form.elements.legacyAdmin.checked=legacy;
  form.elements.username.value='operator.test';
  form.elements.totp.value='000123';
  form.elements.code.value='fixture-entry';
  const submit=()=>form.onsubmit({preventDefault(){}});
  const submitted=submit();
  await settle();
  return {browser,mine,storage,pending,submitted,form,submit};
}

for(const role of ['team','admin','judge','audience']){
  test(`a mismatched cookie confirmation cannot adopt another ${role} login or publish its marker`,async()=>{
    const f=await loginFixture(role,role);
    f.pending.resolve(ok({...f.mine,loginId:'another-login'}));
    await f.submitted;
    assert.equal(vm.runInContext('session.role',f.browser.context),null);
    assert.equal(f.storage.get(ownerKey),undefined);
    assert.match(f.form.querySelector('.form-error').textContent,/登录|身份/);
    assert.deepEqual(f.browser.requests,['/gotc/api/session','/gotc/api/login','/gotc/api/session']);
  });
}

for (const [role,legacy,endpoint] of [['team',false,'team'],['admin',false,'admin'],['admin',true,'admin']]) {
  test(`confirmed ${legacy ? 'migration ' : ''}${role} credentials stay on their own portal`,async()=>{
    const f=await loginFixture(role,role,'confirmation',role,legacy);
    assert.equal(f.storage.get(ownerKey),undefined);
    f.pending.resolve(ok(f.mine));
    await f.submitted;
    assert.equal(f.storage.get(ownerKey),f.mine.loginId);
    assert.equal(vm.runInContext('session.loginId',f.browser.context),f.mine.loginId);
    assert.ok(f.browser.requests.includes('/gotc/api/'+endpoint));
  });
}

test('a team entry rejects an organizer identity even if the cookie agrees',async()=>{
  const f=await loginFixture('team','team','confirmation','admin');
  f.pending.resolve(ok(f.mine));
  await f.submitted;
  assert.equal(vm.runInContext('session.role',f.browser.context),null);
  assert.equal(f.storage.get(ownerKey),undefined);
  assert.ok(!f.browser.requests.includes('/gotc/api/admin/team-workbench'));
});
test('an owner-marker change during confirmation rejects even a matching login response',async()=>{
  const f=await loginFixture('team','team');
  f.storage.set(ownerKey,'newer-tab');
  f.pending.resolve(ok(f.mine));
  await f.submitted;
  assert.equal(f.storage.get(ownerKey),'newer-tab');
  assert.equal(vm.runInContext('session.role',f.browser.context),null);
  assert.equal(f.browser.requests.includes('/gotc/api/team'),false);
});

test('a different role in the confirmation cannot reuse the login id from the team code',async()=>{
  const f=await loginFixture('team','team');
  f.pending.resolve(ok({...f.mine,role:'admin'}));
  await f.submitted;
  assert.equal(f.storage.get(ownerKey),undefined);
  assert.equal(vm.runInContext('session.role',f.browser.context),null);
  assert.equal(f.browser.requests.includes('/gotc/api/admin/team-workbench'),false);
});

test('a team login POST cannot overwrite an owner marker published by another tab while it was pending',async()=>{
  const f=await loginFixture('team','team','post');
  f.storage.set(ownerKey,'other-tab-login');
  f.pending.resolve(ok(f.mine));
  await f.submitted;
  assert.equal(f.storage.get(ownerKey),'other-tab-login');
  assert.equal(vm.runInContext('session.role',f.browser.context),null);
  assert.equal(f.browser.requests.includes('/gotc/api/team'),false);
  assert.match(f.browser.main.innerHTML,/id="login-form"/);
});

test('an old team login confirmation cannot replace the page selected while it was pending',async()=>{
  const f=await loginFixture('team','team');
  await f.browser.go('#claim');
  assert.equal(f.form.isConnected,false);
  f.pending.resolve(ok(f.mine));
  await f.submitted;
  assert.equal(f.storage.get(ownerKey),undefined);
  assert.equal(vm.runInContext('session.role',f.browser.context),null);
  assert.match(f.browser.main.innerHTML,/id="claim-request-form"/);
  assert.equal(f.browser.requests.includes('/gotc/api/team'),false);
});

test('a changed code and double click while team login is pending cannot adopt the first response',async()=>{
  const f=await loginFixture('team','team','post');
  f.form.elements.code.value='replacement-code';
  await f.submit();
  f.pending.resolve(ok(f.mine));
  await f.submitted;
  assert.equal(f.browser.requests.filter(path=>path.endsWith('/login')).length,1);
  assert.equal(f.storage.get(ownerKey),undefined);
  assert.equal(vm.runInContext('session.role',f.browser.context),null);
  assert.equal(f.form.elements.code.value,'replacement-code');
});

test('a late wrong-code response does not label a newly entered code as wrong',async()=>{
  const f=await loginFixture('team','team','post');
  f.form.elements.code.value='new-code';
  f.form.oninput();
  f.pending.resolve(loginReply(401,{error:'Wrong old code'}));
  await f.submitted;
  assert.doesNotMatch(f.form.querySelector('.form-error').textContent,/Wrong old code/);
  assert.equal(f.form.querySelector('button').disabled,false);
});
