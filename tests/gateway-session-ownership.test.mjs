import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const fullSource=fs.readFileSync(new URL('../platform.js',import.meta.url),'utf8');
// Register the actual window listeners, leaving bootstrap to the fixture.
const source=fullSource.split("(async()=>{try{session=await api('session');}")[0];
const recoverySource=fs.readFileSync(new URL('../platform-draft-recovery.js',import.meta.url),'utf8');
const wait=()=>new Promise(resolve=>setImmediate(resolve));
const deferred=()=>{let resolve;const promise=new Promise(done=>{resolve=done;});return {promise,resolve};};
const response=(data,status=200)=>({ok:status<400,status,json:async()=>data});
const info=()=>({enabled:true,baseUrl:'https://example.test/gotc',models:{openai:['synthetic-model']},maxKeys:5,
  keys:[{id:'K-old',name:'old',masked:'gk-old',requests:0}],logContent:false,
  usage:{used24h:0,usedTotal:0},expiresAt:null});

function harness(transport){
  const nodes=new Map(),requests=[],copied=[],events={},storageValues=new Map();let marker='login-a',storageAvailable=true;
  function node(id){
    const item={id,isConnected:true,disabled:false,textContent:'',dataset:{},children:[],classList:{add(){},remove(){}},scrollIntoView(){}};
    let html='';
    item.querySelectorAll=selector=>selector==='[data-key-delete]'?item.children.filter(child=>child.dataset.keyDelete):item.children.filter(child=>child.control);
    item.querySelector=selector=>selector==='.form-error'?item.error:selector.includes('button')||selector==='[type=submit]'?item.button:null;
    Object.defineProperty(item,'innerHTML',{get:()=>html,set(value){
      for(const child of item.children){child.isConnected=false;for(const nested of child.children || [])nested.isConnected=false;}
      item.children=[];html=value;
      for(const match of value.matchAll(/id="([^"]+)"/g)){
        const child=node(match[1]);item.children.push(child);
        if(child.id==='key-form'){
          child.elements={name:{value:'synthetic-laptop'}};child.button=node('key-submit');child.button.control=true;
          child.error=node('key-error');child.children=[child.button,child.error];
        }
      }
      for(const match of value.matchAll(/data-key-delete="([^"]+)"/g)){
        const child=node('delete-'+match[1]);child.dataset.keyDelete=match[1];child.control=true;item.children.push(child);
      }
    }});
    nodes.set(id,item);return item;
  }
  node('platform-main');node('platform-toast');node('team-keys');node('new-api-key');
  const ctx=vm.createContext({document:{getElementById:id=>nodes.get(id)},location:{href:'https://example.test/gotc/platform.html#team'},
    URL,Date,Intl,setTimeout:()=>0,clearTimeout,confirm:()=>true,window:{addEventListener:(name,fn)=>{events[name]=fn;}},
    localStorage:{get length(){return storageValues.size;},key:index=>[...storageValues.keys()][index] ?? null,
      getItem(key){if(!storageAvailable)throw new Error('storage unavailable');return key.startsWith('gotc-login-owner:')?marker:storageValues.get(key) ?? null;},
      setItem:(key,value)=>storageValues.set(key,value),removeItem:key=>storageValues.delete(key)},
    navigator:{clipboard:{async writeText(value){copied.push(value);}}},
    fetch:async(url,options)=>{
      const request={path:url.pathname.split('/api/')[1],...options,body:options.body===undefined?undefined:JSON.parse(options.body)};
      requests.push(request);
      const custom=await transport?.(request,requests.length);if(custom)return custom;
      if(request.path==='team/keys/logs')return response({items:[]});
      if(request.path==='team/keys/delete')return response({ok:true});
      if(request.method==='POST')return response({ok:true,key:'gk-SYNTHETIC-A',item:{name:'synthetic-laptop'}});
      return response(info());
    }});
  vm.runInContext(source+"\nroute='team';routeVersion=1;session={role:'team',loginId:'login-a'};",ctx);
  return {ctx,requests,copied,events,get:id=>nodes.get(id),setMarker:value=>{marker=value;},noStorage:()=>{storageAvailable=false;},
    installRecovery(){
      for(const id of ['local-draft-recovery','local-draft-status','local-draft-candidates','local-draft-comparison','refresh-local-drafts'])node(id);
      const form=node('submission-form'),fieldset=node('submission-fieldset');form.querySelector=selector=>selector==='fieldset'?fieldset:null;
      vm.runInContext(recoverySource+`\nconst recoveryData={team:{id:'T1',project:''},submission:{version:0,draft:null}};
        installTeamRecovery(recoveryData,document.getElementById('submission-form'),1,{restore(){},loadLatest(){},onWindow(){}},'');
        globalThis.logoutOtherTab=()=>{const other=teamDraftStore(recoveryData,undefined,session.loginId);return other.purge(other.capturePurge());};`,ctx);
    },
    async load(){await vm.runInContext('loadTeamKeys(routeVersion)',ctx);await wait();},
    async submit(){const result=nodes.get('key-form').onsubmit({preventDefault(){}});await result;await wait();},
    replacePage(login='login-b'){
      for(const id of ['team-keys','new-api-key','key-form','team-calls'])if(nodes.has(id))nodes.get(id).isConnected=false;
      node('team-keys');node('new-api-key');marker=login;ctx.nextLogin=login;
      vm.runInContext('session={role:"team",loginId:nextLogin}',ctx);
    }};
}

test('every team key request carries its captured login identity, including nested reads and deletes',async()=>{
  const h=harness();
  for(const [path,body] of [['team/keys',undefined],['team/keys/logs',undefined],['team/keys',{name:'x'}],['team/keys/delete',{id:'K-old'}]]){
    h.ctx.path=path;h.ctx.payload=body;
    await vm.runInContext("api(path,payload,{loginId:'captured-login'})",h.ctx);
  }
  assert.ok(h.requests.every(request=>request.headers['X-GOTC-Login']==='captured-login'));
});

test('a cross-tab login change blocks old create/delete handlers and clears exposed key material',async()=>{
  const h=harness();await h.load();const form=h.get('key-form'),remove=h.get('delete-K-old');
  h.get('new-api-key').innerHTML='gk-SYNTHETIC-ALREADY-SHOWN';h.setMarker('login-b');
  await form.onsubmit({preventDefault(){}});await remove.onclick();await wait();
  assert.equal(h.requests.filter(request=>request.method==='POST').length,0);
  assert.equal(h.get('new-api-key').innerHTML,'');assert.match(h.get('team-keys').innerHTML,/登录.*刷新/);
});

test('a late create reply cannot expose an old key or refresh a replacement login card',async()=>{
  const gate=deferred();const h=harness(request=>request.method==='POST'?gate.promise:undefined);
  await h.load();const pending=h.submit();await wait();
  h.replacePage();await h.load();h.get('new-api-key').innerHTML='NEW-LOGIN-UNCHANGED';
  const count=h.requests.length;
  gate.resolve(response({ok:true,key:'gk-SYNTHETIC-OLD-REPLY',item:{name:'old'}}));await pending;await wait();
  assert.equal(h.get('new-api-key').innerHTML,'NEW-LOGIN-UNCHANGED');assert.equal(h.requests.length,count);
});

test('late call history from a detached card cannot overwrite the new team history',async()=>{
  const gate=deferred();let reads=0;
  const h=harness(request=>request.path==='team/keys/logs' && ++reads===1?gate.promise:undefined);
  await h.load();h.replacePage();await h.load();const current=h.get('team-calls');current.innerHTML='NEW-TEAM-HISTORY';
  gate.resolve(response({items:[]}));await wait();await wait();
  assert.equal(current.innerHTML,'NEW-TEAM-HISTORY');
});

test('storage invalidation removes a displayed key and disables its captured copy handler',async()=>{
  const h=harness();await h.load();await h.submit();await wait();
  assert.match(h.get('new-api-key').innerHTML,/gk-SYNTHETIC-A/);const copy=h.get('copy-api-key');
  h.setMarker('login-b');h.events.storage();await copy.onclick();await wait();
  assert.equal(h.get('new-api-key').innerHTML,'');assert.deepEqual(h.copied,[]);
});

test('unavailable storage still sends the old login token and locks on server rejection',async()=>{
  let switched=false;
  const h=harness(request=>switched?response({error:'login changed'},401):undefined);h.noStorage();
  await h.load();h.get('new-api-key').innerHTML='gk-SYNTHETIC-ALREADY-SHOWN';switched=true;
  await h.submit();await wait();
  const write=h.requests.find(request=>request.method==='POST');assert.equal(write.headers['X-GOTC-Login'],'login-a');
  assert.equal(h.get('new-api-key').innerHTML,'');assert.match(h.get('team-keys').innerHTML,/登录.*刷新/);
});

test('a late 401 from the old card does not clear the replacement login or its DOM',async()=>{
  const gate=deferred();const h=harness(request=>request.method==='POST'?gate.promise:undefined);
  await h.load();const pending=h.submit();await wait();h.replacePage();await h.load();
  const html=h.get('team-keys').innerHTML;
  gate.resolve(response({error:'old login expired'},401));await pending;await wait();
  assert.equal(vm.runInContext('session.loginId',h.ctx),'login-b');assert.equal(h.get('team-keys').innerHTML,html);
});

test('duplicate creates write once and copy waits for server identity validation before touching the clipboard',async()=>{
  const create=deferred(),verify=deferred();let verifying=false;
  const h=harness(request=>request.method==='POST'?create.promise:verifying && request.path==='team/keys'?verify.promise:undefined);
  await h.load();const form=h.get('key-form'),first=form.onsubmit({preventDefault(){}}),second=form.onsubmit({preventDefault(){}});
  await wait();assert.equal(h.requests.filter(request=>request.method==='POST').length,1);
  create.resolve(response({ok:true,key:'gk-SYNTHETIC-A',item:{name:'synthetic'}}));await first;await second;await wait();await wait();
  verifying=true;const copying=h.get('copy-api-key').onclick();await wait();assert.deepEqual(h.copied,[]);
  h.setMarker('login-b');verify.resolve(response(info()));await copying;await wait();assert.deepEqual(h.copied,[]);
});

test('a team session without its login token cannot issue key requests',async()=>{
  const h=harness();vm.runInContext("session={role:'team'}",h.ctx);await h.load();
  assert.equal(h.requests.length,0);assert.match(h.get('team-keys').innerHTML,/刷新/);
});

for(const marker of ['login-a',null])for(const storageDelivered of [true,false]){
  test(`logout epoch revocation hides keys and blocks actions with owner marker ${marker} and storage event ${storageDelivered?'delivered':'delayed'}`,async()=>{
    const h=harness();h.setMarker(marker);h.installRecovery();await h.load();await h.submit();
    assert.match(h.get('new-api-key').innerHTML,/gk-SYNTHETIC-A/);
    const form=h.get('key-form'),copy=h.get('copy-api-key'),remove=h.get('delete-K-old'),count=h.requests.length;
    assert.equal(h.ctx.logoutOtherTab(),true);
    assert.equal(vm.runInContext('currentTeamRecovery.store.revoked',h.ctx),false,'Other tab changes storage, not this controller');
    if(storageDelivered){h.events.storage();assert.equal(h.get('new-api-key').innerHTML,'','Storage event immediately removes plaintext');}
    await form.onsubmit({preventDefault(){}});await copy.onclick();await remove.onclick();await wait();
    assert.equal(h.get('new-api-key').innerHTML,'');assert.equal(h.requests.length,count);assert.deepEqual(h.copied,[]);
    assert.equal(h.get('submission-fieldset').disabled,true);assert.match(h.get('team-keys').innerHTML,/刷新/);
  });
}
