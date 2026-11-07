import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const finals=fs.readFileSync(new URL('../platform-finals.js',import.meta.url),'utf8');
const source=fs.readFileSync(new URL('../platform-roadshow-scoring.js',import.meta.url),'utf8');
const platform=fs.readFileSync(new URL('../platform.js',import.meta.url),'utf8').split("window.addEventListener('hashchange'")[0];
const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};};
const dimensions=[['creativity',20],['social',15],['technical',20],['application',20],['crossdisciplinary',15],['opensource',10]].map(([id,max])=>({id,name:id,max}));
const team=(id='a',number=1)=>({id,number,name:'Team '+number,project:'Synthetic',eligible:true,rank:1,score:100,scoreCount:3,recusedCount:0,valid:true,complete:true});
const snapshot=(revision=4,published=false)=>({config:{runId:'run-a',revision,open:false,canEdit:false,published},dimensions,summary:{teams:[team()],expectedJudges:8,activeJudges:8,minimumScores:3,qualifiedCount:1,canPublish:true,final:true,blockers:[]}});
function runtime(extra={}){
  const ctx=vm.createContext({AbortController,Map,Set,Number,Object,Date,esc,setTimeout,clearTimeout,confirm:()=>true,
    metrics:items=>items.map(([value,label])=>`<span>${esc(value)} ${esc(label)}</span>`).join(''),
    dirtyAdminForms:new Set(),window:{addEventListener(){},removeEventListener(){}},document:{addEventListener(){},removeEventListener(){},hidden:false},
    session:{role:'judge',loginId:'judge-login-a',name:'Judge A'},route:'judge-roadshow',routeVersion:1,ownsBrowserLogin:()=>true,...extra});
  vm.runInContext(finals+'\n'+source,ctx);return ctx;
}
function scoreFixture(ctx,options={}){
  const selectors={'.form-error':{textContent:''},'[data-finals-draft]':{},'[data-finals-recuse]':{},'[type=submit]':{},'[data-finals-total]':{},'[data-finals-saved]':{},'[data-finals-recovery]':{innerHTML:'',querySelector:()=>({})},fieldset:{disabled:false},'[data-roadshow-weighted]':{textContent:''}};
  const elements={feedback:{value:' Synthetic feedback '},progressScore:{value:options.progress ?? '10'},progressNote:{value:''},...Object.fromEntries(dimensions.map(d=>['score-'+d.id,{value:String(d.max)}]))},listeners={};
  const form={isConnected:true,elements,addEventListener(type,fn){(listeners[type]??=[]).push(fn);},emit:type=>listeners[type]?.forEach(fn=>fn()),querySelector:selector=>selectors[selector],querySelectorAll:()=>[selectors['[type=submit]'],selectors['[data-finals-draft]'],selectors['[data-finals-recuse]']]};
  const host={innerHTML:'',querySelector:()=>form},option={value:'a',textContent:''},select={value:'a',options:[option]},windowNotice={classList:{toggle(){}}},changed={},auth={};
  const container={isConnected:true,querySelector:selector=>({'[data-roadshow-editor]':host,'[data-roadshow-team]':select,'[data-roadshow-window]':windowNotice,'[data-roadshow-changed]':changed,'[data-finals-auth]':auth,'[data-roadshow-reload-score]':{}})[selector],querySelectorAll:()=>form.querySelectorAll()};
  const view=ctx.createFinalsView(1,'judge');view.container=container;view.config={runId:'run-a',revision:4,open:true,canEdit:true,...options.config};view.data={dimensions,teams:[team()]};view.selected='a';view.scores=new Map(options.record?[['a',options.record]]:[]);
  ctx.roadshowRenderScore(view);return {view,form,elements,selectors,host,container,select,windowNotice,changed,auth};
}

test('unpublished public board suppresses any supplied team data and uses no award UI',()=>{
  const ctx=runtime(),data=snapshot();data.summary.teams[0].name='PRIVATE TEAM';
  assert.ok(!ctx.roadshowScoreSummaryMarkup(data,true).includes('PRIVATE TEAM'));
  const html=ctx.roadshowScoreSummaryMarkup({...data,config:{...data.config,published:true}},true);
  assert.match(html,/已发布 · 路演最终成绩/);assert.match(html,/PRIVATE TEAM/);assert.ok(!html.includes('专项奖'));
});

test('roadshow summaries escape team text and distinguish missing, zero, and recused scores',()=>{
  const ctx=runtime(),data=snapshot();data.summary.teams=[{...team(),name:'<img onerror=evil()>',score:0,scoreCount:1,recusedCount:1},{...team('b',2),score:null,scoreCount:0,complete:false}];
  const html=ctx.roadshowScoreSummaryMarkup(data);
  assert.match(html,/暂定路演成绩/);assert.match(html,/>0\.00</);assert.match(html,/已收 1 \/ 至少 3/);assert.match(html,/1 人回避/);assert.match(html,/评分不足/);assert.ok(!html.includes('<img'));assert.match(html,/&lt;img/);
});

test('two formal scores plus recusals stay unranked and hidden publicly, while three formal scores qualify',()=>{
  const ctx=runtime(),data=snapshot(4,true);data.summary.teams=[{...team('a',1),score:93.25,scoreCount:2,recusedCount:6,rank:1},{...team('b',2),score:88.5,scoreCount:3,rank:2}];
  const publicHtml=ctx.roadshowScoreSummaryMarkup(data,true),adminHtml=ctx.roadshowScoreSummaryMarkup(data);
  assert.ok(!publicHtml.includes('93.25'));assert.match(publicHtml,/88\.50/);assert.match(publicHtml,/已收 2 \/ 至少 3/);assert.match(publicHtml,/评分不足/);
  assert.match(adminHtml,/93\.25/);assert.match(adminHtml,/暂定加权均分 · 不参与排名/);assert.match(adminHtml,/六维原始总分 × 进展系数/);assert.match(adminHtml,/0\.5 \+ 0\.05 × 两天进展分/);
  assert.ok(publicHtml.indexOf('02 · Team 2')<publicHtml.indexOf('01 · Team 1'));
});

test('countdown follows server time, expires locally, and cancels on departure without changing any score input',()=>{
  const ctx=runtime(),tasks=new Map(),applied=[];let local=700000,seq=0,active=true;
  const clock=ctx.makeRoadshowCountdown({alive:()=>active,apply:(text,canEdit)=>applied.push({text,canEdit}),clock:()=>local,schedule:fn=>{tasks.set(++seq,fn);return seq;},cancel:id=>tasks.delete(id)});
  clock.update({open:true,canEdit:true,serverTime:1000,deadline:5401000});assert.match(applied.at(-1).text,/90:00/);
  local+=5399000;let [id,fn]=[...tasks][0];tasks.delete(id);fn();assert.match(applied.at(-1).text,/00:01/);assert.equal(applied.at(-1).canEdit,true);
  local+=1000;[id,fn]=[...tasks][0];tasks.delete(id);fn();assert.equal(applied.at(-1).canEdit,false);assert.match(applied.at(-1).text,/已截止/);assert.equal(tasks.size,0);
  clock.update({open:true,canEdit:true,serverTime:1000,deadline:5401000});active=false;[id,fn]=[...tasks][0];tasks.delete(id);const count=applied.length;fn();assert.equal(applied.length,count);clock.stop();assert.equal(tasks.size,0);
});

test('formal roadshow submission requires an integer progress score and sends it with the scores',async()=>{
  const sent=[],ctx=runtime({api:async(path,payload)=>{sent.push(payload);return {score:{...payload,revision:payload.expectedRevision+1}};}});
  for(const bad of ['','11','2.5']){
    const h=scoreFixture(ctx,{progress:bad});h.form.emit('input');await h.form.onsubmit({preventDefault(){}});
    assert.equal(sent.length,0);assert.match(h.selectors['.form-error'].textContent,/进展评分/);
  }
  const h=scoreFixture(ctx,{progress:'4'});h.form.emit('input');await h.form.onsubmit({preventDefault(){}});
  assert.equal(sent.length,1);assert.equal(sent[0].progressScore,4);assert.equal(sent[0].status,'submitted');assert.equal(sent[0].progressNote,'');
  assert.deepEqual(JSON.parse(JSON.stringify(ctx.roadshowWeightedPreview([20,15,20,20,15,10],4))),{rawTotal:100,multiplier:0.7,weightedTotal:70});
  ctx.disposeFinalsView();
});

test('roadshow form sends six values totaling 100 with its captured ordinary login, run, and score revision',async()=>{
  const sent=[],ctx=runtime({api:async(path,payload,options)=>{sent.push({path,payload,options});return {score:{...payload,revision:payload.expectedRevision+1}};}}),h=scoreFixture(ctx);
  h.form.emit('input');await h.form.onsubmit({preventDefault(){}});
  assert.equal(sent[0].path,'roadshow/score');assert.equal(sent[0].options.loginId,'judge-login-a');assert.equal(sent[0].payload.runId,'run-a');assert.equal(sent[0].payload.expectedRevision,0);assert.equal(sent[0].payload.scores.reduce((a,b)=>a+b,0),100);assert.equal(sent[0].payload.feedback,'Synthetic feedback');
  assert.equal(h.selectors['[data-finals-draft]'].disabled,true);assert.ok(!h.host.innerHTML.includes('nomination'));assert.equal(ctx.dirtyAdminForms.has(h.form),false);
  h.elements['score-creativity'].value='19.5';await h.form.onsubmit({preventDefault(){}});assert.equal(sent[1].payload.expectedRevision,1);ctx.disposeFinalsView();
});

test('missing, out-of-range and sub-tenth scores never submit and deadline canEdit is authoritative',async()=>{
  let writes=0;const ctx=runtime({api:async()=>writes++}),h=scoreFixture(ctx);
  for(const value of ['', '20.1','4.25','NaN','-1']){h.elements['score-creativity'].value=value;await h.form.onsubmit({preventDefault(){}});assert.ok(h.selectors['.form-error'].textContent);}
  h.elements['score-creativity'].value='20';h.view.config.canEdit=false;h.form.finalsRefreshButtons();await h.form.onsubmit({preventDefault(){}});
  assert.equal(writes,0);assert.equal(h.selectors['[type=submit]'].disabled,true);ctx.disposeFinalsView();
});

test('recusal sends no numeric scores and clears only unchanged input after acknowledgement',async()=>{
  const sent=[],ctx=runtime({api:async(path,payload)=>{sent.push(payload);return {score:{...payload,scores:[],revision:1}};}}),h=scoreFixture(ctx);
  await h.selectors['[data-finals-recuse]'].onclick();assert.equal(sent[0].status,'recused');assert.equal(sent[0].scores,null);assert.equal(h.elements['score-creativity'].value,'');assert.match(h.selectors['[data-finals-saved]'].textContent,/已回避/);ctx.disposeFinalsView();
});

test('pending roadshow submission is single flight and retains later edits',async()=>{
  const gate=deferred(),sent=[],ctx=runtime({api:async(path,payload)=>{sent.push(payload);return gate.promise;}}),h=scoreFixture(ctx);
  h.form.emit('input');const running=h.form.onsubmit({preventDefault(){}});await h.form.onsubmit({preventDefault(){}});
  h.elements['score-creativity'].value='18';h.form.emit('input');gate.resolve({score:{...sent[0],revision:1}});await running;
  assert.equal(sent.length,1);assert.equal(h.elements['score-creativity'].value,'18');assert.equal(ctx.dirtyAdminForms.has(h.form),true);assert.match(h.selectors['.form-error'].textContent,/新输入已保留/);ctx.disposeFinalsView();
});

test('score conflict blocks repeated saves and changed login cannot apply a late response',async()=>{
  let writes=0;const ctx=runtime({api:async()=>{writes++;throw Object.assign(new Error('conflict'),{status:409});}}),h=scoreFixture(ctx);
  await h.form.onsubmit({preventDefault(){}});await h.form.onsubmit({preventDefault(){}});assert.equal(writes,1);assert.equal(h.form.finalsBlocked,true);assert.equal(h.elements['score-creativity'].value,'20');ctx.disposeFinalsView();
  const gate=deferred(),other=runtime({api:()=>gate.promise}),late=scoreFixture(other),running=late.form.onsubmit({preventDefault(){}});
  other.session={role:'judge',loginId:'judge-login-b'};gate.resolve({score:{revision:1,status:'submitted'}});await running;assert.equal(late.view.scores.size,0);assert.equal(late.view.locked,true);other.disposeFinalsView();
});

test('roadshow polling preserves score inputs, enforces closing, and locks a new run',async()=>{
  const data={config:{runId:'run-a',revision:4,open:true,canEdit:true},dimensions,teams:[team()],myScores:[]},ctx=runtime({api:async()=>data}),h=scoreFixture(ctx);ctx.disposeFinalsView();
  let apply;ctx.finalsShell=view=>{view.container=h.container;};ctx.finalsStartPolling=(_view,_path,fn)=>{apply=fn;};
  await ctx.renderRoadshowJudge(1);h.elements['score-creativity'].value='17.5';h.form.emit('input');const before=h.host.innerHTML;
  apply({...data,config:{...data.config,canEdit:false},myScores:[{teamId:'a',revision:2}]});
  assert.equal(h.elements['score-creativity'].value,'17.5');assert.equal(h.host.innerHTML,before);assert.equal(h.selectors.fieldset.disabled,true);assert.equal(h.changed.hidden,false);
  apply({...data,config:{...data.config,runId:'new-run'}});assert.match(h.auth.textContent,/轮次已变化/);assert.equal(h.selectors['[type=submit]'].disabled,true);ctx.disposeFinalsView();
});

function adminFixture(api){
  const summary={innerHTML:''},publish={disabled:true},message={textContent:''},refresh={},auth={};
  const host={isConnected:true,querySelector:selector=>({'[data-roadshow-summary]':summary,'[data-roadshow-publish]':publish,'[data-roadshow-publish-message]':message,'[data-roadshow-refresh-score]':refresh,'[data-finals-auth]':auth})[selector],querySelectorAll:()=>[publish,refresh]};
  const parent={html:'ROSTER_UNSAVED_INPUT',insertAdjacentHTML(_where,html){this.appended=html;},querySelector:()=>host};
  const ctx=runtime({route:'admin-roadshow',session:{role:'admin',loginId:'admin-a'},api});let apply;ctx.finalsStartPolling=(_view,_path,fn)=>{apply=fn;};
  return {ctx,parent,publish,message,refresh,summary,get apply(){return apply;}};
}

test('admin live stats preserve the roster form and publication uses latest run and revision',async()=>{
  const sent=[],h=adminFixture(async(path,payload)=>{sent.push({path,payload});return payload?snapshot(payload.expectedRevision+1,payload.published):snapshot();});
  await h.ctx.mountRoadshowScoringAdmin(1,h.parent);h.apply(snapshot(5));await h.publish.onclick();
  const write=sent.find(item=>item.payload);assert.equal(write.path,'admin/roadshow/publish');assert.deepEqual(JSON.parse(JSON.stringify(write.payload)),{runId:'run-a',expectedRevision:5,published:true});assert.match(h.publish.textContent,/撤回/);assert.equal(h.parent.html,'ROSTER_UNSAVED_INPUT');
  h.apply(snapshot(4));assert.match(h.publish.textContent,/撤回/);h.ctx.disposeFinalsView();
});

test('publication requires an ended window and one qualified team, and conflicts require explicit reread',async()=>{
  let writes=0;const h=adminFixture(async(_path,payload)=>{if(payload){writes++;throw Object.assign(new Error('revision mismatch'),{status:409});}return snapshot();});
  await h.ctx.mountRoadshowScoringAdmin(1,h.parent);h.apply({...snapshot(5),config:{...snapshot(5).config,open:true,canEdit:true},summary:{...snapshot(5).summary,canPublish:false}});assert.equal(h.publish.disabled,true);
  const incomplete=snapshot(6);incomplete.summary.teams[0].scoreCount=2;incomplete.summary.teams[0].valid=false;incomplete.summary.canPublish=false;h.apply(incomplete);assert.equal(h.publish.disabled,true);
  h.apply(snapshot(7));await h.publish.onclick();await h.publish.onclick();assert.equal(writes,1);assert.equal(h.publish.disabled,true);assert.match(h.message.textContent,/没有自动重发/);h.apply(snapshot(8));assert.equal(h.publish.disabled,true);h.ctx.disposeFinalsView();
});

test('an expired open flag and other insufficient teams do not block publication of a qualified team',async()=>{
  const h=adminFixture(async()=>snapshot());await h.ctx.mountRoadshowScoringAdmin(1,h.parent);
  const data=snapshot(8);data.config.open=true;data.config.canEdit=false;data.summary.teams.push({...team('b',2),scoreCount:1,valid:false,complete:false,rank:null});
  h.apply(data);assert.equal(h.publish.disabled,false);assert.match(h.summary.innerHTML,/评分不足/);h.ctx.disposeFinalsView();
});

function windowFixture(api,extra={}){
  const start={},close={},error={},notice={},nodes={'[data-roadshow-start]':start,'[data-roadshow-close]':close,'.form-error':error,'[data-roadshow-window-countdown]':notice};
  const form={isConnected:true,querySelector:selector=>nodes[selector]},ctx=runtime({route:'admin',adminStage:'roadshow',session:{role:'admin',loginId:'admin-a'},api,patchSubmissionStageCards(){},toast(){},stageHasUnsavedInputs:()=>false,loadAdminJudging:async()=>{},...extra});
  ctx.makeFinalsPoller=()=>({start(){},stop(){},kick(){}});
  ctx.installRoadshowWindowControls(form,{runId:'run-a',revision:7},{open:false,canEdit:false,deadline:null,serverTime:Date.now()},1);
  return {ctx,start,close,error,notice,form};
}

test('90-minute start is explicit, single flight, server-timed and closes using the acknowledged revision',async()=>{
  const sent=[],gate=deferred(),h=windowFixture(async(path,payload,options)=>{sent.push({path,payload,options});return sent.length===1?gate.promise:{stage:{revision:9},window:{open:false,canEdit:false,deadline:null,serverTime:Date.now()}};});
  assert.equal(sent.length,0);assert.equal(h.start.disabled,false);assert.equal(h.close.disabled,true);const running=h.start.onclick();await h.start.onclick();assert.equal(sent.length,1);
  assert.deepEqual(JSON.parse(JSON.stringify(sent[0].payload)),{expectedRevision:7,open:true,start90Minutes:true});assert.equal(sent[0].options.loginId,'admin-a');assert.equal(sent[0].path,'admin/judging/roadshow/window');
  const now=Date.now();gate.resolve({stage:{revision:8},window:{open:true,canEdit:true,deadline:now+5400000,serverTime:now}});await running;assert.equal(h.start.disabled,true);assert.match(h.notice.textContent,/90:00/);
  await h.close.onclick();assert.deepEqual(JSON.parse(JSON.stringify(sent[1].payload)),{expectedRevision:8,open:false,deadline:null});h.ctx.disposeRoadshowWindow();
});

test('90-minute controls reject unsaved setup and block retry after a conflict',async()=>{
  let writes=0;const h=windowFixture(async()=>{writes++;throw Object.assign(new Error('window changed'),{status:409});},{stageHasUnsavedInputs:()=>true});
  await h.start.onclick();assert.equal(writes,0);assert.match(h.error.textContent,/请先保存/);h.ctx.stageHasUnsavedInputs=()=>false;
  await h.start.onclick();await h.start.onclick();assert.equal(writes,1);assert.equal(h.start.disabled,true);assert.match(h.error.textContent,/没有自动重发/);h.ctx.disposeRoadshowWindow();
});

test('private roadshow endpoints carry X-GOTC-Login and routes select numeric renderers',async()=>{
  const requests=[],main={innerHTML:''},ctx=vm.createContext({document:{getElementById:()=>main,querySelectorAll:()=>[],body:{classList:{remove(){},add(){}}}},location:{href:'https://example.test/platform.html',hash:'#judge-roadshow'},URL,setTimeout,clearTimeout,fetch:async(url,options)=>{requests.push({url,options});return {ok:true,json:async()=>({})};}});
  vm.runInContext(platform+"\nsession={role:'judge',loginId:'ordinary-login'};account=()=>{};globalThis.calls=[];renderRoadshowJudge=async()=>calls.push('numeric-judge');renderRoadshowBoard=async()=>calls.push('numeric-board');renderExhibitionJudge=async()=>calls.push('legacy');",ctx);
  for(const path of ['roadshow/scoring','roadshow/score','admin/roadshow/scoring','admin/roadshow/publish','admin/roadshow/teams','admin/judging/roadshow/window'])await vm.runInContext(`api(${JSON.stringify(path)})`,ctx);
  assert.ok(requests.every(item=>item.options.headers['X-GOTC-Login']==='ordinary-login'));
  await vm.runInContext('renderRoute()',ctx);ctx.location.hash='#board-roadshow';await vm.runInContext('renderRoute()',ctx);assert.deepEqual([...ctx.calls],['numeric-judge','numeric-board']);
});
