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
const team=(id='a',number=1)=>({id,number,name:'Team '+number,project:'Synthetic',eligible:true,rank:1,score:100,scoreCount:2,recusedCount:0,complete:true});
const snapshot=(revision=4,published=false)=>({config:{runId:'run-a',revision,open:false,canEdit:false,published},dimensions,summary:{teams:[team()],expectedJudges:2,activeJudges:2,final:true,blockers:[]}});
function runtime(extra={}){
  const ctx=vm.createContext({AbortController,Map,Set,Number,Object,Date,esc,setTimeout,clearTimeout,confirm:()=>true,
    metrics:items=>items.map(([value,label])=>`<span>${esc(value)} ${esc(label)}</span>`).join(''),
    dirtyAdminForms:new Set(),window:{addEventListener(){},removeEventListener(){}},document:{addEventListener(){},removeEventListener(){},hidden:false},
    session:{role:'judge',loginId:'judge-login-a',name:'Judge A'},route:'judge-roadshow',routeVersion:1,ownsBrowserLogin:()=>true,...extra});
  vm.runInContext(finals+'\n'+source,ctx);return ctx;
}
function scoreFixture(ctx,options={}){
  const selectors={'.form-error':{textContent:''},'[data-finals-draft]':{},'[data-finals-recuse]':{},'[type=submit]':{},'[data-finals-total]':{},'[data-finals-saved]':{},'[data-finals-recovery]':{innerHTML:'',querySelector:()=>({})},fieldset:{disabled:false}};
  const elements={feedback:{value:' Synthetic feedback '},...Object.fromEntries(dimensions.map(d=>['score-'+d.id,{value:String(d.max)}]))},listeners={};
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
  assert.match(html,/暂定路演成绩/);assert.match(html,/>0\.00</);assert.match(html,/1 \/ 2/);assert.match(html,/1 人回避/);assert.match(html,/尚未收齐/);assert.ok(!html.includes('<img'));assert.match(html,/&lt;img/);
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

test('publication requires complete scores and close, and conflicts require explicit reread',async()=>{
  let writes=0;const h=adminFixture(async(_path,payload)=>{if(payload){writes++;throw Object.assign(new Error('revision mismatch'),{status:409});}return snapshot();});
  await h.ctx.mountRoadshowScoringAdmin(1,h.parent);h.apply({...snapshot(5),config:{...snapshot(5).config,open:true}});assert.equal(h.publish.disabled,true);
  const incomplete=snapshot(6);incomplete.summary.teams[0].complete=false;h.apply(incomplete);assert.equal(h.publish.disabled,true);
  h.apply(snapshot(7));await h.publish.onclick();await h.publish.onclick();assert.equal(writes,1);assert.equal(h.publish.disabled,true);assert.match(h.message.textContent,/没有自动重发/);h.apply(snapshot(8));assert.equal(h.publish.disabled,true);h.ctx.disposeFinalsView();
});

test('private roadshow endpoints carry X-GOTC-Login and routes select numeric renderers',async()=>{
  const requests=[],main={innerHTML:''},ctx=vm.createContext({document:{getElementById:()=>main,querySelectorAll:()=>[],body:{classList:{remove(){},add(){}}}},location:{href:'https://example.test/platform.html',hash:'#judge-roadshow'},URL,setTimeout,clearTimeout,fetch:async(url,options)=>{requests.push({url,options});return {ok:true,json:async()=>({})};}});
  vm.runInContext(platform+"\nsession={role:'judge',loginId:'ordinary-login'};account=()=>{};globalThis.calls=[];renderRoadshowJudge=async()=>calls.push('numeric-judge');renderRoadshowBoard=async()=>calls.push('numeric-board');renderExhibitionJudge=async()=>calls.push('legacy');",ctx);
  for(const path of ['roadshow/scoring','roadshow/score','admin/roadshow/scoring','admin/roadshow/publish'])await vm.runInContext(`api(${JSON.stringify(path)})`,ctx);
  assert.ok(requests.every(item=>item.options.headers['X-GOTC-Login']==='ordinary-login'));
  await vm.runInContext('renderRoute()',ctx);ctx.location.hash='#board-roadshow';await vm.runInContext('renderRoute()',ctx);assert.deepEqual([...ctx.calls],['numeric-judge','numeric-board']);
});
