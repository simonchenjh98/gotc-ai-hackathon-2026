import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source=fs.readFileSync(new URL('../platform.js',import.meta.url),'utf8').split("(async()=>{try{session=await api('session');}")[0];
const deferred=()=>{let resolve;const promise=new Promise(done=>{resolve=done;});return {promise,resolve};};
const response=(data,status=200)=>({ok:status<400,status,json:async()=>data});
function harness(route,transport){
  let marker='login-a',storageAvailable=true,votes=[];
  const requests=[],events={},nodes={};
  const node=()=>({isConnected:true,innerHTML:'',textContent:'',disabled:false,dataset:{},classList:{add(){},remove(){}}});
  for(const id of ['platform-main','platform-toast','judge-editor','team-picker','review-conflict','save-review-draft','recuse-review'])nodes[id]=node();
  let html='';
  Object.defineProperty(nodes['platform-main'],'innerHTML',{get:()=>html,set(value){html=value;for(const b of votes)b.isConnected=false;votes=[...value.matchAll(/data-vote="([^"]+)"/g)].map(match=>({...node(),dataset:{vote:match[1]}}));}});
  nodes['platform-main'].insertAdjacentHTML=(_position,text)=>{html=text+html;nodes['audience-login-status']=node();};
  const feedback={value:'Unsaved A feedback',readOnly:false,focus(){}},error=node(),fieldset=node(),submit=node();
  const buttons=[submit,nodes['save-review-draft'],nodes['recuse-review']];
  const form={...node(),elements:{feedback},addEventListener(){},querySelector:s=>s==='fieldset'?fieldset:error,querySelectorAll:s=>s==='button'?buttons:[]};
  nodes['review-form']=form;
  const stage=route.endsWith('roadshow')?'roadshow':'exhibition';
  const data={stage:{id:stage,name:'Fixture stage',runId:'run-fixture',dimensions:[],frozenAt:1},teams:[{id:'T1',number:1,name:'Fixture team'}],myReviews:[],window:{canEdit:true,deadline:null}};
  const audience={teams:data.teams,votedTeamIds:[],voteLimit:3,votingOpen:true};
  const ctx=vm.createContext({document:{getElementById:id=>nodes[id],querySelectorAll:s=>s==='[data-vote]'?votes:[],body:{classList:{contains:()=>false}}},
    location:{href:'https://example.test/gotc/platform.html#'+route},URL,Date,Intl,setTimeout:()=>0,clearTimeout,confirm:()=>true,
    window:{addEventListener:(event,callback)=>{events[event]=callback;}},
    localStorage:{getItem(){if(!storageAvailable)throw new Error('Storage blocked');return marker;}},
    fetch:async(url,options)=>{const request={path:url.pathname.split('/api/')[1],...options,body:options.body?JSON.parse(options.body):undefined};requests.push(request);return await transport?.(request) ?? response(request.path==='audience'?audience:data);}});
  vm.runInContext(source+`\nroute=${JSON.stringify(route)};routeVersion=1;session={role:${JSON.stringify(route==='audience'?'audience':'judge')},loginId:'login-a'};selectedTeam='T1';judgeData=${JSON.stringify(data)};`,ctx);
  return {ctx,requests,events,nodes,form,feedback,error,fieldset,buttons,data,audience,get votes(){return votes;},
    marker(value){marker=value;},noStorage(){storageAvailable=false;},
    replaceSession(){marker='login-b';vm.runInContext(`session={role:${JSON.stringify(route==='audience'?'audience':'judge')},loginId:'login-b'};`,ctx);},
    review(){vm.runInContext('renderExhibitionJudgeEditor();reviewDirty=true;',ctx);},
    loadAudience(){return vm.runInContext('renderAudience(1)',ctx);}};
}

for(const stage of ['exhibition','roadshow']){
  test(`${stage} old editor locks after a different login is published and preserves unsaved feedback`,async()=>{
    const h=harness('judge-'+stage);h.review();h.marker('login-b');h.events.storage({key:'changed'});
    await h.form.onsubmit({preventDefault(){}});await h.nodes['save-review-draft'].onclick();
    assert.equal(h.requests.length,0);assert.equal(h.feedback.value,'Unsaved A feedback');assert.equal(h.feedback.readOnly,true);
    assert.ok(h.buttons.every(b=>b.disabled));assert.match(h.error.textContent,/登录已失效或已切换/);
  });
  test(`${stage} late POST cannot clear dirty input, refresh, or report success under a replacement login`,async()=>{
    const gate=deferred();const h=harness('judge-'+stage,request=>request.method==='POST'?gate.promise:undefined);h.review();
    const saving=h.form.onsubmit({preventDefault(){}});assert.equal(h.requests[0].headers['X-GOTC-Login'],'login-a');
    h.replaceSession();h.events.focus();gate.resolve(response({ok:true}));await saving;
    assert.equal(h.requests.length,1);assert.equal(vm.runInContext('reviewDirty',h.ctx),true);
    assert.equal(h.feedback.value,'Unsaved A feedback');assert.equal(h.nodes['platform-toast'].textContent,'');assert.ok(h.buttons.every(b=>b.disabled));
  });
  test(`${stage} late GET does not render another login's materials`,async()=>{
    const gate=deferred();const h=harness('judge-'+stage,()=>gate.promise);
    const loading=vm.runInContext('renderExhibitionJudge(1)',h.ctx);assert.equal(h.requests[0].headers['X-GOTC-Login'],'login-a');
    h.replaceSession();gate.resolve(response({...h.data,teams:[{id:'T1',name:'MUST_NOT_RENDER',number:1}]}));await loading;
    assert.ok(!h.nodes['platform-main'].innerHTML.includes('MUST_NOT_RENDER'));assert.match(h.nodes['platform-main'].innerHTML,/登录身份已失效或切换/);
  });
  test(`${stage} server identity rejection locks the old editor even without browser storage`,async()=>{
    const h=harness('judge-'+stage,()=>response({error:'Identity replaced'},401));h.noStorage();h.review();await h.form.onsubmit({preventDefault(){}});
    assert.equal(h.requests[0].headers['X-GOTC-Login'],'login-a');assert.equal(h.feedback.value,'Unsaved A feedback');
    assert.equal(h.feedback.readOnly,true);assert.ok(h.buttons.every(b=>b.disabled));assert.equal(h.nodes['platform-toast'].textContent,'');
  });
}

test('audience old buttons cannot vote after storage identity changes',async()=>{
  const h=harness('audience');await h.loadAudience();const button=h.votes[0];h.marker('login-b');h.events.storage({key:'changed'});await button.onclick();
  assert.equal(h.requests.length,1);assert.equal(h.requests[0].headers['X-GOTC-Login'],'login-a');assert.equal(button.disabled,true);assert.match(h.nodes['audience-login-status'].textContent,/身份后投票/);
});

test('audience vote keeps its original login marker and ignores late success after switching accounts',async()=>{
  const gate=deferred();const h=harness('audience',request=>request.method==='POST'?gate.promise:undefined);await h.loadAudience();const button=h.votes[0];
  const saving=button.onclick();assert.equal(h.requests[1].headers['X-GOTC-Login'],'login-a');h.replaceSession();gate.resolve(response({ok:true}));await saving;
  assert.equal(h.requests.length,2);assert.equal(button.disabled,true);assert.equal(h.nodes['platform-toast'].textContent,'');
});

test('audience late GET is discarded if the login changes during loading',async()=>{
  const gate=deferred();const h=harness('audience',()=>gate.promise);const loading=h.loadAudience();h.replaceSession();gate.resolve(response({...h.audience,teams:[{id:'T1',number:1,name:'MUST_NOT_RENDER'}]}));await loading;
  assert.ok(!h.nodes['platform-main'].innerHTML.includes('MUST_NOT_RENDER'));assert.match(h.nodes['audience-login-status'].textContent,/登录已失效或已切换/);
});

test('blocked browser storage still sends captured audience marker and a server rejection keeps voting locked',async()=>{
  const h=harness('audience',request=>request.method==='POST'?response({error:'Identity replaced'},401):undefined);h.noStorage();await h.loadAudience();const button=h.votes[0];await button.onclick();
  assert.equal(h.requests[1].headers['X-GOTC-Login'],'login-a');assert.equal(button.disabled,true);assert.equal(h.nodes['platform-toast'].textContent,'');
});
