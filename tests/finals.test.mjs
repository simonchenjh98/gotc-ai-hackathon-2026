import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source=fs.readFileSync(new URL('../platform-finals.js',import.meta.url),'utf8');
const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};};
function runtime(extra={}){
  const ctx=vm.createContext({AbortController,Map,Set,Number,Object,Date,esc,setTimeout,clearTimeout,
    metrics:items=>items.map(([value,label])=>`<span>${esc(value)} ${esc(label)}</span>`).join(''),
    dirtyAdminForms:new Set(),window:{addEventListener(){},removeEventListener(){}},document:{addEventListener(){},removeEventListener(){},hidden:false},
    session:{role:'judge',loginId:'owner-a',name:'Judge A'},route:'judge-finals',routeVersion:1,ownsBrowserLogin:()=>true,...extra});
  vm.runInContext(source+'\nglobalThis.testHelpers={makeFinalsPoller,finalsSummaryMarkup,finalsSave,finalsTrack,createFinalsView,disposeFinalsView,finalsRequest,finalsRenderScore,finalsRenderNominations,finalsPatchSummary,finalsInstallAdmin};',ctx);
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
  const view=ctx.testHelpers.createFinalsView(1,'judge'),f=formStub();view.forms.add(f.form);ctx.dirtyAdminForms.add(f.form);
  let saved;const running=ctx.testHelpers.finalsSave(view,f.form,'finals/score',{expectedRevision:3,scores:[1,2,3,4,5,6]},(value,unchanged)=>saved={value,unchanged});
  f.form.finalsEdit=2;gate.resolve({score:{revision:4}});await running;
  assert.equal(sent[0].options.loginId,'owner-a');assert.equal(sent[0].payload.expectedRevision,3);assert.equal(saved.value.score.revision,4);assert.equal(saved.unchanged,false);assert.equal(ctx.dirtyAdminForms.has(f.form),true);assert.match(f.error.textContent,/新输入已保留/);ctx.testHelpers.disposeFinalsView();
});

test('conflicting writes keep the form and cannot retry before explicit reload',async()=>{
  let writes=0;const ctx=runtime({api:async()=>{writes++;throw Object.assign(new Error('revision mismatch'),{status:409});}});
  const view=ctx.testHelpers.createFinalsView(1,'judge'),f=formStub();
  await ctx.testHelpers.finalsSave(view,f.form,'finals/nominations',{expectedRevision:1},()=>assert.fail('conflicts cannot save'));
  assert.equal(f.form.finalsBlocked,true);assert.match(f.error.textContent,/当前输入已保留/);assert.match(f.recovery.innerHTML,/重新载入/);
  await ctx.testHelpers.finalsSave(view,f.form,'finals/nominations',{expectedRevision:1},()=>{});assert.equal(writes,1);ctx.testHelpers.disposeFinalsView();
});

test('late success after another login cannot mutate the old view or clear unsaved input',async()=>{
  const gate=deferred(),ctx=runtime({api:()=>gate.promise});const view=ctx.testHelpers.createFinalsView(1,'judge'),f=formStub();ctx.dirtyAdminForms.add(f.form);let applied=false;
  const running=ctx.testHelpers.finalsSave(view,f.form,'finals/score',{expectedRevision:0},()=>applied=true);
  ctx.session={role:'judge',loginId:'owner-b'};gate.resolve({score:{revision:1}});await running;
  assert.equal(applied,false);assert.equal(ctx.dirtyAdminForms.has(f.form),true);ctx.testHelpers.disposeFinalsView();
});

test('network write failures keep input and never send an automatic second POST',async()=>{
  let writes=0;const ctx=runtime({api:async()=>{writes++;throw new Error('network disconnected');}});const view=ctx.testHelpers.createFinalsView(1,'judge'),f=formStub();
  await ctx.testHelpers.finalsSave(view,f.form,'finals/nominations',{expectedRevision:0},()=>{});
  assert.equal(writes,1);assert.equal(ctx.dirtyAdminForms.has(f.form),true);assert.match(f.error.textContent,/没有自动重发/);ctx.testHelpers.disposeFinalsView();
});

test('credential fragments are removed immediately when the captured login loses ownership',()=>{
  let cleared=0;const secret={replaceChildren(){cleared++;}},button={disabled:false},notice={hidden:true,textContent:''};
  const ctx=runtime(),view=ctx.testHelpers.createFinalsView(1,'judge');view.container={isConnected:true,querySelectorAll:selector=>selector==='[data-finals-secret]'?[secret]:[button],querySelector:()=>notice};
  ctx.ownsBrowserLogin=()=>false;assert.equal(view.valid(),false);assert.equal(cleared,1);assert.equal(button.disabled,true);assert.equal(notice.hidden,false);ctx.testHelpers.disposeFinalsView();
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
  const view=ctx.testHelpers.createFinalsView(1,'judge');
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
  let writes=0;const ctx=runtime({api:async()=>writes++}),view=ctx.testHelpers.createFinalsView(1,'judge');
  const selectors={'.form-error':{textContent:''},'[data-finals-draft]':{},'[data-finals-recuse]':{},'[type=submit]':{},'[data-finals-total]':{},'[data-finals-saved]':{}},elements={feedback:{value:''},...Object.fromEntries(dimensions.map(d=>['score-'+d.id,{value:String(d.max)}]))};
  const form=editableForm(selectors,elements),host={innerHTML:'',querySelector:()=>form};view.container={isConnected:true,querySelector:()=>host,querySelectorAll:()=>[]};view.config={runId:'run',open:true};view.data={dimensions,teams:[team('a',1)]};view.selected='a';view.scores=new Map();ctx.testHelpers.finalsRenderScore(view);
  for(const invalid of ['', '20.1','4.25','NaN','-1']){elements['score-creativity'].value=invalid;form.onsubmit({preventDefault(){}});await flush();assert.ok(selectors['.form-error'].textContent);}
  assert.equal(writes,0);ctx.testHelpers.disposeFinalsView();
});

test('real nomination form explicitly sends all six abstentions and separately supports repeated team choices across awards',async()=>{
  const posted=[],ctx=runtime({api:async(path,payload)=>{posted.push({path,payload});return {nominations:{...payload,revision:payload.expectedRevision+1}};}}),view=ctx.testHelpers.createFinalsView(1,'judge');
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

test('creating eight judge accounts locks the roster until response and preserves team selections and independent settings',async()=>{
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
    if(succeed)gate.resolve({config:{...view.config,revision:1,judgeIds:Array.from({length:8},(_,i)=>'j'+i)},credentials:Array.from({length:8},(_,i)=>({id:'j'+i,name:'Judge '+i,code:'synthetic-code-'+i}))});
    else gate.reject(new Error('Temporary failure'));
    await saving;
    assert.equal(fieldset.disabled,false);assert.equal(roster.button.disabled,false);assert.equal(teamInput.checked,true);assert.equal(settings.form.elements.open.checked,true);assert.equal(settings.button.disabled,false);
    if(succeed){assert.match(judgeList.innerHTML,/Judge 7/);assert.equal(create.button.disabled,true);}else assert.equal(judgeList.innerHTML,'old judge list');
    ctx.testHelpers.disposeFinalsView();
  }
});
