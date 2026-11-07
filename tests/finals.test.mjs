import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source=fs.readFileSync(new URL('../platform-finals.js',import.meta.url),'utf8');
const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};};
function runtime(extra={}){
  const storage=new Map(extra.authorized===false?[]:[['gotc-finals-access-v1',JSON.stringify({accessToken:'synthetic-finals-token-0123456789abcdef',expiresAt:Date.now()+3600000})]]);
  const ctx=vm.createContext({AbortController,Map,Set,Number,Object,Date,esc,setTimeout,clearTimeout,URL,base:new URL('https://example.test/gotc/'),
    sessionStorage:{getItem:key=>storage.get(key)??null,setItem:(key,value)=>storage.set(key,value),removeItem:key=>storage.delete(key)},
    fetch:async(url,options)=>{
      try{const result=await extra.api(String(url).split('/api/')[1],options.body?JSON.parse(options.body):undefined,{accessToken:options.headers['X-GOTC-Finals-Access'],judgeId:options.headers['X-GOTC-Finals-Judge'],credentials:options.credentials,signal:options.signal});return {ok:true,status:200,json:async()=>result};}
      catch(error){if(!error.status)throw error;return {ok:false,status:error.status,json:async()=>({error:error.message,code:error.code})};}
    },
    metrics:items=>items.map(([value,label])=>`<span>${esc(value)} ${esc(label)}</span>`).join(''),
    dirtyAdminForms:new Set(),window:{addEventListener(){},removeEventListener(){}},document:{addEventListener(){},removeEventListener(){},hidden:false},
    session:{role:'judge',loginId:'owner-a',name:'Judge A'},route:'judge-finals',routeVersion:1,ownsBrowserLogin:()=>true,...extra});
  vm.runInContext(source+'\nglobalThis.testHelpers={makeFinalsPoller,finalsSummaryMarkup,finalsSave,finalsTrack,createFinalsView,disposeFinalsView,finalsRequest,finalsRenderScore,finalsRenderNominations,finalsPatchSummary,finalsInstallAdmin,renderFinalsJudge,renderFinalsAccess,finalsHasAccess,finalsSetAccess};',ctx);
  return ctx;
}
const team=(id,number,name='Team '+number)=>({id,number,name,project:'Synthetic',eligible:true,score:81.25,scoreCount:8,recusedCount:0,complete:true,rank:number,dimensions:[16,12,17,16,12,8.25]});
const summary=(overrides={})=>({teams:[team('a',1),team('b',2)],awards:[],submittedBallots:8,expectedJudges:8,complete:true,final:false,blockers:[],awardsConfirmed:false,...overrides});
const award=(overrides={})=>({id:'creativity',name:'最佳创意奖',votes:[{teamId:'a',count:4},{teamId:'b',count:4}],winners:[],rawLeaders:['a','b'],suggestedWinners:['a','b'],confirmedWinners:null,maxVotes:4,totalVotes:8,...overrides});

test('public finals hide any supplied summary until publication is explicitly true',()=>{
  const {finalsSummaryMarkup:markup}=runtime().testHelpers;
  const html=markup({config:{published:false},summary:summary({teams:[team('private',1,'PRIVATE TEAM')]})},{publicView:true});
  assert.match(html,/尚未发布/);assert.ok(!html.includes('PRIVATE TEAM'));
});

test('closed, complete and confirmed results are still provisional until actually published',()=>{
  const {finalsSummaryMarkup:markup}=runtime().testHelpers;
  const html=markup({config:{published:false},summary:summary({final:true,awardsConfirmed:true,awards:[award({confirmedWinners:['a','b']})]})});
  assert.match(html,/暂定结果/);assert.match(html,/已确认归属 · 尚未发布/);assert.ok(!html.includes('已发布 · 最终结果'));
});

test('published ties retain both winners, while zero positive votes produce no winner',()=>{
  const {finalsSummaryMarkup:markup}=runtime().testHelpers;
  const html=markup({config:{published:true},summary:summary({final:true,awards:[award({confirmedWinners:['a','b']}),award({id:'social',name:'最具社会关怀奖',votes:[],confirmedWinners:[],suggestedWinners:[],maxVotes:0,totalVotes:0})]})},{publicView:true});
  assert.match(html,/已发布 · 最终结果/);assert.match(html,/获奖队伍 · 并列/);assert.match(html,/01 · Team 1/);assert.match(html,/02 · Team 2/);assert.match(html,/本奖项无人获奖/);assert.ok(!html.includes('data-finals-confirm-awards'));
});

test('award succession presents the lower-vote eligible suggestion and explains the raw leader',()=>{
  const {finalsSummaryMarkup:markup}=runtime().testHelpers;
  const html=markup({config:{published:false,open:false},summary:summary({awards:[award({votes:[{teamId:'a',count:5},{teamId:'b',count:3}],rawLeaders:['a'],suggestedWinners:['b'],maxVotes:5})]})});
  const winners=html.match(/<ul class="finals-winners">([\s\S]*?)<\/ul>/)[1];
  assert.ok(!winners.includes('Team 1'));assert.match(winners,/Team 2/);assert.match(winners,/3 票/);assert.match(html,/原始最高票 · 已获前序奖项，顺延/);assert.match(html,/展开候选名单与票数/);assert.match(html,/每队最多1奖/);
});

test('results escape team text, distinguish missing score from zero and show valid judges plus recusals',()=>{
  const {finalsSummaryMarkup:markup}=runtime().testHelpers;
  const html=markup({config:{published:false},summary:summary({teams:[{...team('a',1,'<img src=x onerror=evil()>'),score:0,scoreCount:7,recusedCount:1},{...team('b',2),score:null,scoreCount:0,complete:false}]})});
  assert.ok(!html.includes('<img'));assert.match(html,/&lt;img/);assert.match(html,/>0\.00</);assert.match(html,/7 \/ 8/);assert.match(html,/1 人回避/);assert.match(html,/尚未收齐/);assert.match(html,/未提交不作0分/);
});

function pollHarness(overrides={}){
  const ctx=runtime(),tasks=new Map();let seq=0,listener,hidden=false,active=true;
  const calls={reads:0,applied:[],states:[]};
  const controller=ctx.testHelpers.makeFinalsPoller({
    read:async()=>{calls.reads++;return {ok:true};},apply:data=>calls.applied.push(data),status:state=>calls.states.push(state),alive:()=>active,isHidden:()=>hidden,
    subscribe:fn=>{listener=fn;return ()=>listener=null;},schedule:(fn,delay)=>{const id=++seq;tasks.set(id,{fn,delay});return id;},cancel:id=>tasks.delete(id),...overrides});
  return {controller,calls,tasks,tick(){const [id,task]=[...tasks].sort((a,b)=>a[1].delay-b[1].delay)[0];tasks.delete(id);return task.fn();},setHidden(value){hidden=value;listener?.();},setActive(value){active=value;}};
}

test('finals statistics polling runs every two seconds and remains single flight',async()=>{
  const gate=deferred();let reads=0;const h=pollHarness({read:()=>{reads++;return gate.promise;}});h.controller.start();assert.equal([...h.tasks.values()][0].delay,2000);
  const running=h.tick();h.controller.kick();h.controller.kick();assert.equal(reads,1);gate.resolve({score:90});await running;assert.equal(h.calls.applied.length,1);
  h.controller.stop();assert.equal(h.tasks.size,0);
});

test('a late snapshot cannot update a departed view and hidden views pause and resume',async()=>{
  const gate=deferred(),h=pollHarness({read:()=>gate.promise});h.controller.start();const running=h.tick();h.setActive(false);gate.resolve({private:'OLD'});await running;
  assert.equal(h.calls.applied.length,0);assert.equal(h.tasks.size,0);h.controller.stop();
  const visible=pollHarness();visible.controller.start();visible.setHidden(true);assert.equal(visible.tasks.size,0);visible.setHidden(false);await visible.tick();assert.equal(visible.calls.applied.length,1);visible.controller.stop();
});

test('polling retries network failures and stops on authorization loss',async()=>{
  let attempts=0;const h=pollHarness({read:async()=>{if(++attempts===1)throw new Error('offline');return {};}});h.controller.start();await h.tick();assert.equal(h.calls.states.at(-1),'error');await h.tick();assert.equal(h.calls.applied.length,1);h.controller.stop();
  const locked=pollHarness({read:async()=>{throw Object.assign(new Error('expired'),{status:401});}});locked.controller.start();await locked.tick();assert.equal(locked.calls.states.at(-1),'auth');assert.equal(locked.tasks.size,0);
});

test('polling uses an abort deadline and cancels an in-flight read on disposal',async()=>{
  const gate=deferred();let signal;const h=pollHarness({read:input=>{signal=input;return gate.promise;}});h.controller.start();const running=h.tick();
  assert.equal([...h.tasks.values()][0].delay,8000);h.controller.stop();assert.equal(signal.aborted,true);gate.resolve({});await running;assert.equal(h.calls.applied.length,0);
});

function formStub(){
  const error={textContent:''},button={disabled:false},recoveryButton={onclick:null},recovery={innerHTML:'',querySelector:()=>recoveryButton};
  const form={isConnected:true,finalsEdit:1,addEventListener(){},querySelector:selector=>selector==='.form-error'?error:selector==='[data-finals-recovery]'?recovery:button,querySelectorAll:()=>[button],finalsRefreshButtons(){button.disabled=!!this.finalsBlocked;}};
  return {form,error,button,recovery,recoveryButton};
}

test('an authoritative save advances only through its response and preserves edits made while pending',async()=>{
  const gate=deferred(),sent=[];const ctx=runtime({api:async(path,payload,options)=>{sent.push({path,payload,options});return gate.promise;}});
  const view=ctx.testHelpers.createFinalsView(1,'seat',{id:'seat-a',name:'Judge A'}),f=formStub();view.forms.add(f.form);ctx.dirtyAdminForms.add(f.form);
  let saved;const running=ctx.testHelpers.finalsSave(view,f.form,'finals/score',{expectedRevision:3,scores:[1,2,3,4,5,6]},(value,unchanged)=>saved={value,unchanged});
  f.form.finalsEdit=2;gate.resolve({score:{revision:4}});await running;
  assert.equal(sent[0].options.judgeId,'seat-a');assert.equal(sent[0].options.credentials,'omit');assert.equal(sent[0].payload.expectedRevision,3);assert.equal(saved.value.score.revision,4);assert.equal(saved.unchanged,false);assert.equal(ctx.dirtyAdminForms.has(f.form),true);assert.match(f.error.textContent,/新输入已保留/);ctx.testHelpers.disposeFinalsView();
});

test('conflicting writes keep the form and cannot retry before explicit reload',async()=>{
  let writes=0;const ctx=runtime({api:async()=>{writes++;throw Object.assign(new Error('revision mismatch'),{status:409});}});
  const view=ctx.testHelpers.createFinalsView(1,'seat',{id:'seat-a',name:'Judge A'}),f=formStub();
  await ctx.testHelpers.finalsSave(view,f.form,'finals/nominations',{expectedRevision:1},()=>assert.fail('conflicts cannot save'));
  assert.equal(f.form.finalsBlocked,true);assert.match(f.error.textContent,/当前输入已保留/);assert.match(f.recovery.innerHTML,/重新载入/);
  await ctx.testHelpers.finalsSave(view,f.form,'finals/nominations',{expectedRevision:1},()=>{});assert.equal(writes,1);ctx.testHelpers.disposeFinalsView();
});

test('ordinary login changes cannot redirect a captured finals seat or block its successful save',async()=>{
  const gate=deferred(),ctx=runtime({api:()=>gate.promise});const view=ctx.testHelpers.createFinalsView(1,'seat',{id:'seat-a',name:'Judge A'}),f=formStub();ctx.dirtyAdminForms.add(f.form);let applied=false;
  const running=ctx.testHelpers.finalsSave(view,f.form,'finals/score',{expectedRevision:0},()=>applied=true);
  ctx.session={role:'judge',loginId:'owner-b'};gate.resolve({score:{revision:1}});await running;
  assert.equal(applied,true);assert.equal(ctx.dirtyAdminForms.has(f.form),false);ctx.testHelpers.disposeFinalsView();
});

test('network write failures keep input and never send an automatic second POST',async()=>{
  let writes=0;const ctx=runtime({api:async()=>{writes++;throw new Error('network disconnected');}});const view=ctx.testHelpers.createFinalsView(1,'seat',{id:'seat-a',name:'Judge A'}),f=formStub();
  await ctx.testHelpers.finalsSave(view,f.form,'finals/nominations',{expectedRevision:0},()=>{});
  assert.equal(writes,1);assert.equal(ctx.dirtyAdminForms.has(f.form),true);assert.match(f.error.textContent,/没有自动重发/);ctx.testHelpers.disposeFinalsView();
});

test('admin authentication loss still locks management controls, independently of public judge seats',()=>{
  const button={disabled:false},notice={hidden:true,textContent:''};
  const ctx=runtime({route:'admin-finals',session:{role:'admin',loginId:'admin-a'}}),view=ctx.testHelpers.createFinalsView(1,'admin');view.container={isConnected:true,querySelectorAll:()=>[button],querySelector:()=>notice};
  ctx.ownsBrowserLogin=()=>false;assert.equal(view.valid(),false);assert.equal(button.disabled,true);assert.equal(notice.hidden,false);ctx.testHelpers.disposeFinalsView();
});

const dimensions=[['creativity',20],['social',15],['technical',20],['application',20],['crossdisciplinary',15],['opensource',10]].map(([id,max])=>({id,name:id,max,award:id+'奖'}));
function editableForm(selectors,elements){
  const listeners={};
  return {isConnected:true,elements,addEventListener(type,fn){(listeners[type]??=[]).push(fn);},
    emit(type){listeners[type]?.forEach(fn=>fn());},querySelector:selector=>selectors[selector],querySelectorAll:selector=>selector==='[data-finals-save]'?[selectors['[type=submit]'],selectors['[data-finals-draft]'],selectors['[data-finals-recuse]']].filter(Boolean):[],finalsEdit:0};
}
const flush=()=>new Promise(resolve=>setImmediate(resolve));

test('real score form sends all six numeric values totaling 100 and consumes only its successful revision',async()=>{
  const posted=[],ctx=runtime({api:async(path,payload)=>{posted.push({path,payload});return {score:{...payload,revision:payload.expectedRevision+1}};},confirm:()=>true});
  const view=ctx.testHelpers.createFinalsView(1,'seat',{id:'seat-a',name:'Judge A'});
  const selectors={'.form-error':{textContent:''},'[data-finals-draft]':{disabled:false},'[data-finals-recuse]':{disabled:false},'[type=submit]':{disabled:false},'[data-finals-total]':{innerHTML:''},'[data-finals-saved]':{textContent:''},'[data-finals-recovery]':{innerHTML:''}};
  const elements={feedback:{value:' Synthetic feedback '},...Object.fromEntries(dimensions.map(d=>['score-'+d.id,{value:String(d.max)}]))};
  const form=editableForm(selectors,elements),host={innerHTML:'',querySelector:()=>form},option={value:'a',textContent:''};
  view.container={isConnected:true,querySelector:selector=>selector==='[data-finals-score-editor]'?host:{options:[option]},querySelectorAll:()=>[]};
  view.config={runId:'run',open:true};view.data={dimensions,teams:[team('a',1)]};view.selected='a';view.scores=new Map();
  ctx.testHelpers.finalsRenderScore(view);form.emit('input');form.onsubmit({preventDefault(){}});await flush();
  assert.equal(posted[0].path,'finals/score');assert.equal(posted[0].payload.expectedRevision,0);assert.equal(posted[0].payload.scores.reduce((a,b)=>a+b,0),100);assert.equal(posted[0].payload.feedback,'Synthetic feedback');
  assert.equal(selectors['[data-finals-draft]'].disabled,true);assert.equal(ctx.dirtyAdminForms.has(form),false);assert.match(option.textContent,/已正式提交/);
  elements['score-creativity'].value='19.5';form.emit('input');form.onsubmit({preventDefault(){}});await flush();assert.equal(posted[1].payload.expectedRevision,1);
  ctx.testHelpers.disposeFinalsView();
});

test('real scoring form rejects missing, out-of-range and sub-tenth inputs without a POST',async()=>{
  let writes=0;const ctx=runtime({api:async()=>writes++}),view=ctx.testHelpers.createFinalsView(1,'seat',{id:'seat-a',name:'Judge A'});
  const selectors={'.form-error':{textContent:''},'[data-finals-draft]':{},'[data-finals-recuse]':{},'[type=submit]':{},'[data-finals-total]':{},'[data-finals-saved]':{}},elements={feedback:{value:''},...Object.fromEntries(dimensions.map(d=>['score-'+d.id,{value:String(d.max)}]))};
  const form=editableForm(selectors,elements),host={innerHTML:'',querySelector:()=>form};view.container={isConnected:true,querySelector:()=>host,querySelectorAll:()=>[]};view.config={runId:'run',open:true};view.data={dimensions,teams:[team('a',1)]};view.selected='a';view.scores=new Map();ctx.testHelpers.finalsRenderScore(view);
  for(const invalid of ['', '20.1','4.25','NaN','-1']){elements['score-creativity'].value=invalid;form.onsubmit({preventDefault(){}});await flush();assert.ok(selectors['.form-error'].textContent);}
  assert.equal(writes,0);ctx.testHelpers.disposeFinalsView();
});

test('real nomination form explicitly sends all six abstentions and separately supports repeated team choices across awards',async()=>{
  const posted=[],ctx=runtime({api:async(path,payload)=>{posted.push({path,payload});return {nominations:{...payload,revision:payload.expectedRevision+1}};}}),view=ctx.testHelpers.createFinalsView(1,'seat',{id:'seat-a',name:'Judge A'});
  const selectors={'.form-error':{textContent:''},'[data-finals-draft]':{},'[type=submit]':{},'[data-finals-saved]':{},'[data-finals-nomination-count]':{}},elements=Object.fromEntries(dimensions.map(d=>['nomination-'+d.id,{value:''}]));
  const form=editableForm(selectors,elements),host={innerHTML:'',querySelector:()=>form};view.container={isConnected:true,querySelector:()=>host,querySelectorAll:()=>[]};view.config={runId:'run',open:true};view.data={dimensions,teams:[team('a',1),team('b',2)]};ctx.testHelpers.finalsRenderNominations(view,null);
  form.onsubmit({preventDefault(){}});await flush();
  assert.equal(posted[0].path,'finals/nominations');assert.equal(posted[0].payload.status,'submitted');assert.equal(Object.values(posted[0].payload.nominations).length,6);assert.ok(Object.values(posted[0].payload.nominations).every(v=>v===null));assert.equal(selectors['[data-finals-draft]'].disabled,true);
  elements['nomination-creativity'].value='a';elements['nomination-social'].value='a';form.emit('change');form.onsubmit({preventDefault(){}});await flush();
  assert.equal(posted[1].payload.expectedRevision,1);assert.equal(posted[1].payload.nominations.creativity,'a');assert.equal(posted[1].payload.nominations.social,'a');assert.match(selectors['[data-finals-nomination-count]'].textContent,/2 \/ 6/);ctx.testHelpers.disposeFinalsView();
});

test('unchanged stats polls preserve DOM identity and changed stats preserve open candidates and keyboard focus',()=>{
  let rewrites=0,focused=0,details=[{open:false,dataset:{finalsAward:'creativity'},querySelector:()=>({focus:()=>focused++})}];
  const host={querySelectorAll:()=>details,querySelector:()=>null,set innerHTML(value){rewrites++;details=[{open:false,dataset:{finalsAward:'creativity'},querySelector:()=>({focus:()=>focused++})}];}};
  const document={activeElement:null,addEventListener(){},removeEventListener(){}};
  const ctx=runtime({document}),view={container:{querySelector:()=>host}},data={config:{published:true},summary:summary({final:true,awards:[award({confirmedWinners:['a','b']})]})};
  ctx.testHelpers.finalsPatchSummary(view,data,true);const original=details[0];original.open=true;
  document.activeElement={matches:()=>true,closest:()=>original};
  ctx.testHelpers.finalsPatchSummary(view,JSON.parse(JSON.stringify(data)),true);
  assert.equal(rewrites,1);assert.equal(details[0],original);assert.equal(details[0].open,true);assert.equal(focused,0);
  const changed=JSON.parse(JSON.stringify(data));changed.summary.teams[0].score=90;
  ctx.testHelpers.finalsPatchSummary(view,changed,true);assert.equal(rewrites,2);assert.equal(details[0].open,true);assert.equal(focused,1);
});

test('unchanged statistics still re-enable award confirmation after an explicitly failed save',()=>{
  const button={disabled:false},message={textContent:''},host={querySelectorAll:()=>[],querySelector:selector=>selector==='[data-finals-confirm-awards]'?button:message,innerHTML:''};
  const ctx=runtime(),view={container:{querySelector:()=>host},awardBusy:true},data={config:{published:false,open:false,revision:3},summary:summary({awards:[award()]})};
  ctx.testHelpers.finalsPatchSummary(view,data);assert.equal(button.disabled,true);
  view.awardBusy=false;view.awardMessage='请求未完成';ctx.testHelpers.finalsPatchSummary(view,{...data,config:{...data.config,revision:4}});
  assert.equal(button.disabled,false);assert.equal(message.textContent,'请求未完成');assert.equal(host.finalsData.config.revision,4);
});

test('creating eight judge seats locks the roster until response and preserves team selections and independent settings',async()=>{
  for(const succeed of [true,false]){
    const gate=deferred();let posts=0;
    const ctx=runtime({route:'admin-finals',session:{role:'admin',loginId:'admin-a'},api:()=>{posts++;return gate.promise;}}),view=ctx.testHelpers.createFinalsView(1,'admin');
    const fieldset={disabled:false},teamInput={value:'a',checked:true},oldJudgeInput={value:'old',checked:false};
    const makeForm=()=>{
      const button={disabled:false},error={textContent:''};
      const form={isConnected:true,addEventListener(){},querySelector:selector=>selector==='[data-finals-save]'?button:selector==='fieldset'?fieldset:selector==='.form-error'?error:{innerHTML:'',querySelector:()=>({})},querySelectorAll:selector=>selector==='[data-finals-save]'?[button]:selector.includes('teamIds')?[teamInput]:[oldJudgeInput]};
      return {form,button,error};
    };
    const roster=makeForm(),settings=makeForm(),create=makeForm();settings.form.elements={open:{checked:true},published:{checked:false}};
    const judgeList={innerHTML:'old judge list'},secrets={innerHTML:'',replaceChildren(){this.innerHTML='';}};
    const nodes={'[data-finals-roster]':roster.form,'[data-finals-settings]':settings.form,'[data-finals-create-judges]':create.form,'[data-finals-judge-list]':judgeList,'[data-finals-secret]':secrets};
    view.container={isConnected:true,querySelector:selector=>nodes[selector],querySelectorAll:selector=>selector==='[data-finals-secret]'?[secrets]:[]};
    view.config={runId:'r',revision:0,rosterLocked:false,judgeIds:[],teamIds:['a']};
    ctx.testHelpers.finalsInstallAdmin(view);
    const saving=create.form.onsubmit({preventDefault(){}});
    assert.equal(fieldset.disabled,true);assert.equal(roster.button.disabled,true);assert.equal(settings.button.disabled,false);assert.equal(settings.form.elements.open.checked,true);
    create.form.onsubmit({preventDefault(){}});assert.equal(posts,1,'the pending generation cannot be duplicated');
    if(succeed)gate.resolve({config:{...view.config,revision:1,judgeIds:Array.from({length:8},(_,i)=>'j'+i)},judges:Array.from({length:8},(_,i)=>({id:'j'+i,name:'Judge '+i,active:true}))});
    else gate.reject(new Error('Temporary failure'));
    await saving;
    assert.equal(fieldset.disabled,false);assert.equal(roster.button.disabled,false);assert.equal(teamInput.checked,true);assert.equal(settings.form.elements.open.checked,true);assert.equal(settings.button.disabled,false);
    if(succeed){assert.match(judgeList.innerHTML,/Judge 7/);assert.equal(create.button.disabled,true);}else assert.equal(judgeList.innerHTML,'old judge list');
    ctx.testHelpers.disposeFinalsView();
  }
});

function directEntryPage(api,extra={}){
  const main={innerHTML:''},error={textContent:''},submit={disabled:true},refresh={onclick:null};
  const select={value:'',disabled:true,innerHTML:''};
  const form={elements:{judgeId:select},querySelector:selector=>selector==='[type=submit]'?submit:error};
  const notice={textContent:''},state={textContent:''};
  const nodes={'[data-finals-entry]':form,'[data-finals-login-status]':notice,'[data-finals-entry-state]':state,'[data-finals-login-refresh]':refresh,'[data-finals-team]':{value:'',options:[]},'[data-finals-switch-judge]':{},'[data-finals-refresh-judge]':{},'[data-finals-sync]':{textContent:''}};
  const container={isConnected:true,querySelector:selector=>nodes[selector] || null,querySelectorAll:()=>[]};main.querySelector=()=>container;
  const calls=[],ctx=runtime({root:main,heading:(_a,title,description)=>`<h1>${esc(title)}</h1><p>${esc(description)}</p>`,session:{role:null},api:async(path,payload,options)=>{calls.push({path,payload,options});return api(path,payload,options);},...extra});
  return {ctx,main,container,form,select,submit,notice,state,refresh,error,calls,submitForm:()=>form.onsubmit({preventDefault(){}})};
}
const seats=()=>({config:{runId:'r',open:false,rosterReady:true},judges:[{id:'seat-a',name:'Synthetic Judge'}]});
const openLogin={loginAvailable:true,loginOpensAt:1,message:''};
const entryApi=(entry=seats,schedule=()=>openLogin,other=()=>({config:{runId:'r',open:true,revision:1},teams:[team('a',1)],dimensions,myScores:[],myNominations:null}))=>
  path=>path==='finals/login-status'?schedule():path==='finals/entry'?entry():other(path);

test('verified open judge login lists seats without changing the ordinary session',async()=>{
  const p=directEntryPage(entryApi(()=>({config:{runId:'r',open:true,rosterReady:true},judges:[{id:'seat-a',name:'评委一'},{id:'seat-b',name:'<评委二>'}]})),{session:{role:'team',loginId:'unrelated-team'}});
  const before=p.ctx.session;await p.ctx.testHelpers.renderFinalsJudge(1);
  assert.deepEqual(p.calls.map(c=>c.path),['finals/login-status','finals/entry']);assert.equal(p.ctx.session,before);
  assert.match(p.select.innerHTML,/评委一/);assert.match(p.select.innerHTML,/&lt;评委二&gt;/);assert.equal(p.select.disabled,false);assert.equal(p.submit.disabled,false);
  assert.match(p.notice.textContent,/评委登录已开放/);assert.match(p.state.textContent,/决赛评分与提名已开放/);
  assert.ok(!p.main.innerHTML.includes('type="password"'));assert.ok(!p.main.innerHTML.includes('name="code"'));assert.match(p.main.innerHTML,/href="#board-finals"/);
  p.ctx.testHelpers.disposeFinalsView();
});

test('closed judge login keeps seats hidden and the form disabled until the scheduled time',async()=>{
  const p=directEntryPage(entryApi(()=>assert.fail('seats must not be read before login opens'),()=>({loginAvailable:false,loginOpensAt:2,message:'评委登录将于北京时间 2026-11-08 14:00 开放。'})));
  await p.ctx.testHelpers.renderFinalsJudge(1);
  assert.deepEqual(p.calls.map(c=>c.path),['finals/login-status']);assert.match(p.notice.textContent,/14:00 开放/);
  assert.equal(p.select.disabled,true);assert.equal(p.submit.disabled,true);p.select.value='seat-a';await p.submitForm();assert.equal(p.calls.length,1);
  const gated=directEntryPage(path=>{if(path==='finals/login-status'){const error=new Error('评委登录尚未开放');error.status=403;error.code='judge_login_not_open';throw error;}assert.fail(path);});
  await gated.ctx.testHelpers.renderFinalsJudge(1);assert.match(gated.notice.textContent,/尚未开放/);assert.equal(gated.submit.disabled,true);
  p.ctx.testHelpers.disposeFinalsView();gated.ctx.testHelpers.disposeFinalsView();
});

test('a network failure while reading login status offers a retry and disables entry',async()=>{
  const p=directEntryPage(()=>{throw new Error('offline');});
  await p.ctx.testHelpers.renderFinalsJudge(1);
  assert.match(p.notice.textContent,/暂时无法读取登录开放状态/);assert.equal(p.submit.disabled,true);assert.equal(typeof p.refresh.onclick,'function');assert.ok(!p.main.innerHTML.includes('type="password"'));
  p.ctx.testHelpers.disposeFinalsView();
});

test('a verified visitor explicitly chooses the scoring identity after login opens',async()=>{
  const p=directEntryPage(entryApi());
  let scoreView,nominationView;p.ctx.finalsRenderScore=view=>scoreView=view;p.ctx.finalsRenderNominations=view=>nominationView=view;
  const ordinary=p.ctx.session;await p.ctx.testHelpers.renderFinalsJudge(1);
  assert.match(p.select.innerHTML,/Synthetic Judge/);assert.equal(p.select.value,'');assert.ok(!p.main.innerHTML.includes('name="code"'));
  const reads=p.calls.length;await p.submitForm();assert.equal(p.calls.length,reads);assert.match(p.error.textContent,/请选择自己的姓名或评委席位/);
  p.select.value='not-in-roster';await p.submitForm();assert.equal(p.calls.length,reads);
  p.select.value='seat-a';await p.submitForm();
  assert.equal(p.calls.at(-1).path,'finals/judge');assert.equal(p.calls.find(c=>c.path==='finals/entry').options.judgeId,undefined);assert.equal(p.calls.at(-1).options.judgeId,'seat-a');
  assert.equal(scoreView,nominationView);assert.equal(scoreView.judgeId,'seat-a');assert.equal(scoreView.selected,'a');assert.equal(p.ctx.session,ordinary);assert.match(p.main.innerHTML,/当前评分归属：Synthetic Judge/);
  p.ctx.testHelpers.disposeFinalsView();
});

test('verified entry sends its finals token independently of ordinary credentials',async()=>{
  const requests=[],storage={getItem(){assert.fail('entry must not read ordinary credentials');},setItem(){assert.fail('entry must not persist ordinary credentials');},removeItem(){assert.fail('entry must not mutate ordinary account storage');}};
  const p=directEntryPage(()=>assert.fail('ordinary API must not receive direct entry'),{session:{role:'admin',loginId:'ordinary-admin'},localStorage:storage,ownsBrowserLogin:()=>false,fetch:async(url,options)=>{requests.push({url:String(url),options});return {ok:true,status:200,json:async()=>String(url).endsWith('login-status')?openLogin:seats()};}});
  const ordinary=p.ctx.session;await p.ctx.testHelpers.renderFinalsJudge(1);
  assert.deepEqual(requests.map(r=>r.url.split('/api/')[1]),['finals/login-status','finals/entry']);
  for(const {options} of requests){assert.equal(options.method,'GET');assert.equal(options.credentials,'omit');assert.equal(options.cache,'no-store');assert.equal(options.headers['X-GOTC-Finals-Access'],'synthetic-finals-token-0123456789abcdef');assert.deepEqual(Object.keys(options.headers),['X-GOTC-Finals-Access']);}
  assert.equal(p.ctx.session,ordinary);p.ctx.testHelpers.disposeFinalsView();
});

test('late seat choices after leaving the route cannot replace the next page',async()=>{
  const gate=deferred(),p=directEntryPage(path=>path==='finals/login-status'?openLogin:gate.promise),pending=p.ctx.testHelpers.renderFinalsJudge(1);
  await Promise.resolve();p.ctx.testHelpers.disposeFinalsView();p.ctx.routeVersion=2;p.main.innerHTML='Next page';const before=p.select.innerHTML;gate.resolve(seats());await pending;
  assert.equal(p.main.innerHTML,'Next page');assert.equal(p.select.innerHTML,before);
});

test('an unconfigured finals roster stays on explicit seat selection without inventing a judge',async()=>{
  const p=directEntryPage(entryApi(()=>({config:{rosterReady:false,open:false},judges:[]})));await p.ctx.testHelpers.renderFinalsJudge(1);
  assert.match(p.state.textContent,/组织方正在准备决赛名单与8个评委席位/);assert.equal(p.submit.disabled,true);const reads=p.calls.length;await p.submitForm();assert.equal(p.calls.length,reads);p.ctx.testHelpers.disposeFinalsView();
});

test('each tab keeps its own seat header even after cookie login changes, and cannot send that header to administration',async()=>{
  const calls=[];
  const fetch=async(url,options)=>{calls.push({url:String(url),options});return {ok:true,status:200,json:async()=>({ok:true})};};
  const first=runtime({fetch}),second=runtime({fetch});
  const a=first.testHelpers.createFinalsView(1,'seat',{id:'seat-a',name:'A'}),b=second.testHelpers.createFinalsView(1,'seat',{id:'seat-b',name:'B'});
  first.session={role:'admin',loginId:'new-admin'};first.ownsBrowserLogin=()=>false;
  await first.testHelpers.finalsRequest(a,'finals/score',{teamId:'a'});await second.testHelpers.finalsRequest(b,'finals/nominations',{});
  assert.equal(calls[0].options.headers['X-GOTC-Finals-Judge'],'seat-a');assert.equal(calls[1].options.headers['X-GOTC-Finals-Judge'],'seat-b');
  for(const {options} of calls){assert.equal(options.credentials,'omit');assert.equal(options.headers['X-GOTC-Finals-Access'],'synthetic-finals-token-0123456789abcdef');assert.equal(options.headers['X-GOTC-Login'],undefined);assert.equal(options.headers.Cookie,undefined);}
  await assert.rejects(first.testHelpers.finalsRequest(a,'admin/finals',{}));assert.equal(calls.length,2);
  first.testHelpers.disposeFinalsView();second.testHelpers.disposeFinalsView();
});

test('switching seats invalidates an earlier response instead of applying it to the new editor',async()=>{
  const gate=deferred(),ctx=runtime({api:()=>gate.promise}),first=ctx.testHelpers.createFinalsView(1,'seat',{id:'seat-a',name:'A'}),f=formStub();let applied=false;
  const running=ctx.testHelpers.finalsSave(first,f.form,'finals/score',{expectedRevision:0},()=>applied=true);
  const next=ctx.testHelpers.createFinalsView(1,'seat',{id:'seat-b',name:'B'});gate.resolve({score:{revision:1}});await running;
  assert.equal(applied,false);assert.equal(next.valid(),true);assert.equal(next.judgeId,'seat-b');ctx.testHelpers.disposeFinalsView();
});

test('a rejected seat locks only that page and does not sign out an unrelated ordinary session',async()=>{
  const ctx=runtime({session:{role:'admin',loginId:'admin-session'},fetch:async()=>({ok:false,status:403,json:async()=>({error:'席位已停用'})})}),view=ctx.testHelpers.createFinalsView(1,'seat',{id:'inactive',name:'Inactive'}),before=ctx.session;
  await assert.rejects(ctx.testHelpers.finalsRequest(view,'finals/judge'),/席位已停用/);assert.equal(view.locked,true);assert.equal(ctx.session,before);ctx.testHelpers.disposeFinalsView();
});

test('entry scope and an empty seat cannot send scoring or administrator requests',async()=>{
  let requests=0;const ctx=runtime({fetch:async()=>{requests++;throw new Error('unexpected');}}),entry=ctx.testHelpers.createFinalsView(1,'entry');
  for(const path of ['finals/judge','finals/score','finals/nominations','finals/access','admin/finals'])await assert.rejects(ctx.testHelpers.finalsRequest(entry,path,{}));
  const empty=ctx.testHelpers.createFinalsView(1,'seat');await assert.rejects(ctx.testHelpers.finalsRequest(empty,'finals/score',{}));assert.equal(requests,0);ctx.testHelpers.disposeFinalsView();
});

test('public finals board remains accessible without a judge or account',async()=>{
  const calls=[],ctx=runtime({session:{role:null},api:async path=>{calls.push(path);return {summary:null};}}),view=ctx.testHelpers.createFinalsView(1,null);
  await ctx.testHelpers.finalsRequest(view,'finals/board');assert.deepEqual(calls,['finals/board']);ctx.testHelpers.disposeFinalsView();
});

function invitePage(api,extra={},schedule=()=>openLogin){
  const page=directEntryPage((path,payload,options)=>path==='finals/login-status'?schedule():api(path,payload,options),{authorized:false,...extra});
  const error={textContent:''},button={disabled:true},exit={onclick:null};
  const form={elements:{code:{value:'synthetic-invitation',disabled:true}},get isConnected(){return page.main.innerHTML.includes('data-finals-access>');},querySelector:selector=>selector==='.form-error'?error:button};
  const query=page.container.querySelector;
  page.container.querySelector=selector=>selector==='[data-finals-access]'?form:selector==='[data-finals-access-exit]'?exit:query(selector);
  return {...page,form,error,button,exit,submit:()=>form.onsubmit({preventDefault(){}})};
}
const grant=()=>({accessToken:'new-synthetic-finals-token-0123456789',expiresAt:Date.now()+3600000});

test('unverified entry reads only public login status before showing invitation verification',async()=>{
  const p=invitePage(()=>assert.fail('seat or score data must remain protected'));
  await p.ctx.testHelpers.renderFinalsJudge(1);
  assert.deepEqual(p.calls.map(c=>c.path),['finals/login-status']);assert.match(p.main.innerHTML,/name="code" type="password"/);
  assert.match(p.main.innerHTML,/验证并选择评委席位/);assert.ok(!p.main.innerHTML.includes('name="judgeId"'));
  assert.ok(!p.main.innerHTML.includes('synthetic-invitation'));assert.equal(p.button.disabled,false);p.ctx.testHelpers.disposeFinalsView();
});

test('wrong invitation stays retryable and successful access stores only a finals token',async()=>{
  let attempts=0;const issued=grant(),p=invitePage(path=>{
    if(path==='finals/entry')return seats();
    assert.equal(path,'finals/access');
    if(++attempts===1)throw Object.assign(new Error('邀请码不正确'),{status:401,code:'finals_access_required'});
    return issued;
  });
  const ordinary=p.ctx.session;await p.ctx.testHelpers.renderFinalsJudge(1);await p.submit();
  assert.match(p.error.textContent,/邀请码不正确/);assert.equal(p.button.disabled,false);assert.equal(p.form.elements.code.disabled,false);
  assert.equal(p.form.elements.code.value,'');assert.equal(p.ctx.testHelpers.finalsHasAccess(),false);
  p.form.elements.code.value='synthetic-invitation';await p.submit();
  assert.deepEqual(p.calls.map(c=>c.path),['finals/login-status','finals/access','finals/access','finals/login-status','finals/entry']);
  assert.equal(p.calls[1].options.accessToken,undefined);assert.equal(p.calls[4].options.accessToken,issued.accessToken);
  assert.equal(p.calls[1].options.credentials,'omit');assert.equal(p.ctx.session,ordinary);assert.match(p.main.innerHTML,/邀请码已验证/);assert.match(p.select.innerHTML,/Synthetic Judge/);
  const stored=p.ctx.sessionStorage.getItem('gotc-finals-access-v1');assert.ok(!stored.includes('synthetic-invitation'));
  assert.equal(JSON.parse(stored).accessToken,issued.accessToken);assert.equal(p.form.elements.code.value,'');
  await p.exit.onclick();assert.equal(p.ctx.testHelpers.finalsHasAccess(),false);assert.equal(p.ctx.sessionStorage.getItem('gotc-finals-access-v1'),null);
  assert.match(p.main.innerHTML,/决赛评委验证/);p.ctx.testHelpers.disposeFinalsView();
});

test('future schedule prevents invitation submission and polling enables it without a reload',async()=>{
  let open=false;const issued=grant(),p=invitePage(path=>path==='finals/access'?issued:seats(),{},()=>({loginAvailable:open,loginOpensAt:2,message:open?'':'评委登录将在周日开放。'}));
  await p.ctx.testHelpers.renderFinalsJudge(1);assert.equal(p.button.disabled,true);assert.equal(p.form.elements.code.disabled,true);
  assert.match(p.notice.textContent,/周日开放/);await p.submit();assert.deepEqual(p.calls.map(c=>c.path),['finals/login-status']);
  open=true;await vm.runInContext('activeFinalsView.poller.refresh()',p.ctx);
  assert.equal(p.button.disabled,false);await p.submit();assert.equal(p.ctx.testHelpers.finalsHasAccess(),true);assert.match(p.select.innerHTML,/Synthetic Judge/);
  assert.deepEqual(p.calls.filter(c=>c.path==='finals/access').map(c=>c.payload.code),['synthetic-invitation']);p.ctx.testHelpers.disposeFinalsView();
});

test('time closing between status and invitation submission preserves input flow for later retry',async()=>{
  let closed=true;const p=invitePage(path=>{
    if(path==='finals/access' && closed)throw Object.assign(new Error('评委登录尚未开放'),{status:403,code:'judge_login_not_open'});
    return path==='finals/access'?grant():seats();
  });
  await p.ctx.testHelpers.renderFinalsJudge(1);await p.submit();assert.equal(p.ctx.testHelpers.finalsHasAccess(),false);assert.match(p.error.textContent,/尚未开放/);assert.equal(p.form.elements.code.value,'');
  closed=false;await vm.runInContext('activeFinalsView.poller.refresh()',p.ctx);p.form.elements.code.value='synthetic-invitation';await p.submit();
  assert.equal(p.ctx.testHelpers.finalsHasAccess(),true);p.ctx.testHelpers.disposeFinalsView();
});

test('late invitation success after departure cannot install access or fetch seats',async()=>{
  const gate=deferred(),p=invitePage(()=>gate.promise);await p.ctx.testHelpers.renderFinalsJudge(1);const pending=p.submit();
  p.ctx.testHelpers.disposeFinalsView();p.ctx.routeVersion=2;gate.resolve(grant());await pending;
  assert.equal(p.ctx.testHelpers.finalsHasAccess(),false);assert.equal(p.ctx.sessionStorage.getItem('gotc-finals-access-v1'),null);assert.equal(p.calls.length,2);
});

test('expired access cannot fetch a captured seat and returns entry to invitation verification',async()=>{
  const p=invitePage(()=>assert.fail('no protected data may be fetched'),{authorized:true});
  const view=p.ctx.testHelpers.createFinalsView(1,'seat',{id:'seat-a',name:'A'});p.ctx.testHelpers.finalsSetAccess({...grant(),expiresAt:Date.now()-1});
  await assert.rejects(p.ctx.testHelpers.finalsRequest(view,'finals/judge'));assert.equal(view.locked,true);assert.equal(p.calls.length,0);
  await p.ctx.testHelpers.renderFinalsJudge(1);assert.match(p.main.innerHTML,/决赛评委验证/);p.ctx.testHelpers.disposeFinalsView();
});

test('server revocation clears cached finals access and asks for the invitation again',async()=>{
  const p=invitePage(()=>{throw Object.assign(new Error('验证已失效'),{status:401,code:'finals_access_required'});},{authorized:true});
  const ordinary=p.ctx.session;await p.ctx.testHelpers.renderFinalsJudge(1);
  assert.equal(p.ctx.testHelpers.finalsHasAccess(),false);assert.equal(p.ctx.sessionStorage.getItem('gotc-finals-access-v1'),null);
  assert.match(p.main.innerHTML,/决赛评委验证/);assert.equal(p.ctx.session,ordinary);p.ctx.testHelpers.disposeFinalsView();
});

test('malformed access responses cannot unlock the finals entry',async()=>{
  for(const result of [{accessToken:'short',expiresAt:Date.now()+1000},{...grant(),expiresAt:Date.now()-1},{...grant(),expiresAt:'tomorrow'}]){
    const p=invitePage(()=>result);await p.ctx.testHelpers.renderFinalsJudge(1);await p.submit();
    assert.equal(p.ctx.testHelpers.finalsHasAccess(),false);assert.match(p.error.textContent,/验证结果无效/);
    assert.equal(p.calls.filter(c=>c.path==='finals/access').length,1);p.ctx.testHelpers.disposeFinalsView();
  }
});

test('public finals board remains accessible without an invitation',async()=>{
  const calls=[],ctx=runtime({authorized:false,api:async path=>{calls.push(path);return {summary:null};}}),view=ctx.testHelpers.createFinalsView(1,null);
  await ctx.testHelpers.finalsRequest(view,'finals/board');assert.deepEqual(calls,['finals/board']);assert.equal(ctx.testHelpers.finalsHasAccess(),false);ctx.testHelpers.disposeFinalsView();
});
