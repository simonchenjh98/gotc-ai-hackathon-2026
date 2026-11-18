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
async function openPage(hash, {beforeAsset, session = {role:null}, replies = {}, respond, storage = new Map()} = {}) {
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
    document, location, URL, window, AbortController, Intl, TextEncoder, setTimeout, clearTimeout,
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
  assert.match(browser.main.innerHTML,/id="login-form"/);
  assert.deepEqual(browser.requests,['/gotc/api/session']);
  assert.ok(browser.assets.every(path=>!/(?:\/team\/|\/admin\/|\/gateway\/)/.test(path)));
  assert.equal(vm.runInContext('typeof renderTeam',browser.context),'undefined');
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
  assert.deepEqual(browser.requests,['/gotc/api/session']);
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
  let resolve;
  const promise=new Promise(done=>{resolve=done;});
  return {promise,resolve};
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
