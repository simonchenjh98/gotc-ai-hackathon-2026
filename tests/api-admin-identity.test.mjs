import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const read=name=>fs.readFileSync(new URL('../'+name,import.meta.url),'utf8');
const tick=()=>new Promise(resolve=>setImmediate(resolve));
function fixture(){
  const node=()=>({value:'',innerHTML:'',textContent:'',isConnected:true,disabled:false,dataset:{},listeners:{},
    classList:{add(){},remove(){},toggle(){}},addEventListener(name,fn){this.listeners[name]=fn;},focus(){},scrollIntoView(){},querySelectorAll:()=>[]});
  const ids=new Map(),lookup=(map,key)=>{if(!map.has(key))map.set(key,node());return map.get(key);};
  const dialogs=[],calls=[];let marker='login-A',logoutMarker=null,pending=null;
  function makeDialog(){
    const d=node(),fields=new Map(),form=node();
    form.elements=Object.fromEntries(['name','url','key','defaultIn','defaultOut'].map(k=>[k,node()]));
    form.querySelector=selector=>lookup(fields,selector);
    Object.assign(d,{form,fields,open:false,querySelector:selector=>selector==='form'?form:lookup(fields,selector),
      querySelectorAll:selector=>selector==='input,textarea'?Object.values(form.elements):[],
      showModal(){this.open=true;},close(){this.open=false;},remove(){this.isConnected=false;}});
    dialogs.push(d);return d;
  }
  const ctx=vm.createContext({document:{getElementById:id=>lookup(ids,id),createElement:makeDialog,body:{append(){},classList:node().classList},querySelectorAll:()=>[]},
    location:{href:'https://example.test/gotc/platform.html#admin-api/accounts',hash:'#admin-api/accounts'},URL,
    localStorage:{getItem:key=>key.endsWith(':logout')?logoutMarker:marker,setItem:(key,value)=>{if(key.endsWith(':logout'))logoutMarker=value;else marker=value;}},setTimeout,clearTimeout,confirm:()=>true,
    fetch:async(url,options)=>{calls.push({path:url.pathname,options});if(pending && url.pathname.endsWith(pending.path))return pending.promise;
      const body=url.pathname.endsWith('/api/admin/keys')?{upstreams:{},teams:[],models:{}}:url.pathname.endsWith('/api/admin')?{teams:[]}:{ok:true};
      return {ok:true,status:200,json:async()=>body};}});
  for(const id of ['api-page','api-actions'])lookup(ids,id).querySelector=selector=>lookup(ids,id+selector);
  vm.runInContext(read('platform.js').split("window.addEventListener('hashchange'")[0]+'\n'+read('platform-api-admin.js')+"\nroute='admin-api';routeVersion=1;session={role:'admin',loginId:'login-A'};toast=()=>{};",ctx);
  return {ctx,calls,dialogs,ids,async render(){await ctx.renderApiAdmin(1);},async open(){await this.render();vm.runInContext("apiAccountDialog(currentApiAdmin,'openai',false)",ctx);
      const d=dialogs.at(-1);d.form.elements.key.value='synthetic-unsaved-key';d.form.elements.url.value='https://example.test/v1';d.querySelector('[data-custom]').value='model-a';d.querySelector('[data-add-custom]').onclick();return d;},
    switchLogin(changeSession=true){marker='login-B';if(changeSession)vm.runInContext("session={role:'admin',loginId:'login-B'}",ctx);},
    logoutOtherTab(){logoutMarker='login-A';},
    defer(path){let resolve;const promise=new Promise(done=>resolve=done);pending={path,promise};return value=>resolve({ok:true,status:200,json:async()=>value});}};
}

test('account save is bound to its page login and old dialogs cannot submit',async()=>{
  const f=fixture(),d=await f.open();
  d.form.onsubmit({preventDefault(){}});await tick();
  const saved=f.calls.find(c=>c.path.endsWith('/keys/upstreams'));
  assert.equal(saved.options.headers['X-GOTC-Login'],'login-A');
  assert.equal(d.open,false);
  const stale=await f.open(),before=f.calls.length;f.switchLogin();
  stale.form.onsubmit({preventDefault(){}});await tick();
  assert.equal(f.calls.length,before);assert.equal(stale.open,false);assert.equal(stale.form.elements.key.value,'');
});

test('storage/focus identity validation and route disposal close and clear account secrets',async()=>{
  for(const change of ['marker','session','logout','route']){
    const f=fixture(),d=await f.open();
    if(change==='route'){
      vm.runInContext("renderBoard=async()=>{};location.hash='#board';",f.ctx);await f.ctx.renderRoute();
    }else{
      if(change==='logout')f.logoutOtherTab();else f.switchLogin(change==='session');vm.runInContext('currentIdentityPage.valid()',f.ctx);
    }
    assert.equal(d.open,false,change);assert.equal(d.isConnected,false,change);assert.equal(d.form.elements.key.value,'',change);
  }
});

test('late model discovery cannot refill a disposed dialog',async()=>{
  const f=fixture(),d=await f.open(),finish=f.defer('/keys/upstreams/models'),button=d.querySelector('[data-sync]');
  const request=button.onclick({currentTarget:button});await tick();
  const discovery=f.calls.at(-1);assert.equal(discovery.options.headers['X-GOTC-Login'],'login-A');
  f.switchLogin(false);vm.runInContext('currentIdentityPage.valid()',f.ctx);
  finish({ok:true,models:['late-model'],message:'done'});await request;
  assert.equal(d.open,false);assert.equal(d.form.elements.key.value,'');assert.equal(d.querySelector('[data-catalog]').innerHTML,'');
});

test('stale test-connection dialog cannot call the new login upstream',async()=>{
  const f=fixture();await f.render();
  vm.runInContext("currentApiAdmin.data.upstreams={openai:{url:'https://example.test/v1',models:{'model-a':[1,2]}}};apiTestDialog(currentApiAdmin,'openai')",f.ctx);
  const d=f.dialogs.at(-1),before=f.calls.length;f.switchLogin();
  await d.querySelector('[data-start]').onclick();
  assert.equal(f.calls.length,before);assert.equal(d.open,false);
});

test('late stats or logs cannot replace a newer team or tab selection',async()=>{
  for(const first of ['stats','logs']){
    const f=fixture();await f.render();const replies=new Map();
    f.ctx.fetch=(url)=>new Promise(resolve=>replies.set(url.pathname.split('/').at(-1),resolve));
    const stats={totals:{requests:0,cost:0,input:0,output:0,errors:0,avgMs:null},byKey:[],byModel:[],byClient:[],byFailure:[],daily:[]};
    const reply=kind=>replies.get(kind)({ok:true,status:200,json:async()=>kind==='stats'?stats:{items:[],more:false}});
    const show=(kind,label)=>kind==='stats'?f.ctx.showKeyStats({teamId:label},label,7):f.ctx.showKeyLogs({teamId:label},label);
    const second=first==='stats'?'logs':'stats',older=show(first,'old-team'),newer=show(second,'new-team');
    reply(second);await newer;
    const box=f.ids.get('key-detail'),current=box.innerHTML;
    assert.match(current,/new-team/);
    reply(first);await older;
    assert.equal(box.innerHTML,current,first+' late response must be ignored');
  }
});
