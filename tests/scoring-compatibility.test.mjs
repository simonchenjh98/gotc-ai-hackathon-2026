import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source=fs.readFileSync(new URL('../platform.js',import.meta.url),'utf8').split("window.addEventListener('hashchange'")[0];
function context(hash='#judge',role='judge'){
  const main={innerHTML:''};
  const ctx=vm.createContext({document:{getElementById:id=>id==='platform-main'?main:{},querySelectorAll:()=>[],body:{classList:{remove(){}}}},location:{href:`https://example.test/platform.html${hash}`,hash},URL,Date,setTimeout,clearTimeout});
  vm.runInContext(source+`\nsession={role:${JSON.stringify(role)}};account=()=>{};globalThis.calls=[];renderBoard=async()=>calls.push('numeric-board');renderExhibitionBoard=async()=>calls.push('exhibition-board');renderJudge=async()=>calls.push('numeric-judge');renderExhibitionJudge=async()=>calls.push('exhibition-judge');renderRoadshowJudge=async()=>calls.push('roadshow-numeric-judge');renderRoadshowBoard=async()=>calls.push('roadshow-numeric-board');renderLogin=role=>calls.push('login:'+role);renderWrongRole=role=>calls.push('wrong-role:'+role);`,ctx);
  return ctx;
}

test('original numeric routes and exhibition remain unchanged while roadshow uses its independent numeric renderer',async()=>{
  for(const [hash,expected] of [['','numeric-board'],['#board','numeric-board'],['#judge','numeric-judge'],['#board-exhibition','exhibition-board'],['#judge-exhibition','exhibition-judge'],['#judge-roadshow','roadshow-numeric-judge'],['#board-roadshow','roadshow-numeric-board']]){
    const ctx=context(hash);await vm.runInContext('renderRoute()',ctx);assert.equal(ctx.calls[0],expected,hash || 'default board');
  }
  for(const hash of ['#judge','#judge-exhibition','#judge-roadshow']){
    const ctx=context(hash,null);await vm.runInContext('renderRoute()',ctx);assert.equal(ctx.calls[0],'login:judge');
    const wrong=context(hash,'audience');await vm.runInContext('renderRoute()',wrong);assert.equal(wrong.calls[0],'wrong-role:judge');
  }
});

test('numeric board reads original aggregate endpoint rather than exhibition progress',async()=>{
  const requested=[];const content={innerHTML:''},status={classList:{remove(){}},innerHTML:''};
  const ctx=vm.createContext({document:{getElementById:id=>id==='board-content'?content:id==='live-status'?status:{},body:{classList:{contains:()=>false}}},location:{href:'https://example.test/platform.html#board'},URL,Date,setTimeout,clearTimeout,fetch:async url=>{requested.push(String(url));return {ok:true,json:async()=>({teams:[{number:1,name:'Numeric team',score:82.5,rank:1,judgeCount:5}],config:{minJudges:5,resultsPublished:true}})};}});
  vm.runInContext(source+'\nrouteVersion=4;',ctx);await vm.runInContext('loadBoard(4)',ctx);
  assert.equal(requested[0],'https://example.test/api/board');assert.match(content.innerHTML,/82\.50/);assert.match(content.innerHTML,/专业均分/);
});

test('numeric score events submit numeric values and do not let a late save replace another team input',async()=>{
  let resolveSave;const inputs=Array.from({length:6},(_,i)=>({value:String(i+0.1),dataset:{score:String(i)},disabled:false}));
  const sliders=inputs.map((input,i)=>({value:input.value,dataset:{slider:String(i)},disabled:false}));
  const feedback={value:'Synthetic numeric feedback',disabled:false},error={textContent:''},submit={disabled:false},draft={disabled:false},recuse={disabled:false},total={innerHTML:''};
  const form={isConnected:true,elements:{feedback,shareFeedback:{checked:true},...Object.fromEntries(inputs.map((input,i)=>[`score${i}`,input]))},addEventListener(type,listener){this[type]=listener;},reportValidity:()=>true,querySelector(selector){if(selector==='[type=submit]')return submit;if(selector==='.form-error')return error;return sliders[Number(selector.match(/\d+/)?.[0])];},querySelectorAll(selector){return selector==='[data-score]'?inputs:selector==='[data-slider]'?sliders:[...inputs,...sliders,feedback,submit,draft,recuse];}};
  const main={innerHTML:''},elements={'platform-main':main,'judge-editor':{innerHTML:''},'score-form':form,'save-draft':draft,recuse,'score-total':total};let payload;
  const ctx=vm.createContext({document:{getElementById:id=>elements[id]},location:{href:'https://example.test/platform.html#judge'},URL,Date,setTimeout,clearTimeout,fetch:async(_url,options)=>{payload=JSON.parse(options.body);return new Promise(resolve=>{resolveSave=()=>resolve({ok:true,json:async()=>({ok:true})});});}});
  const data={teams:[{id:'T1',number:1,name:'A'}],myScores:[],dimensions:inputs.map((_,i)=>({name:`Dimension ${i}`,max:i<3?20:i<5?15:10})),scoringOpen:true};
  vm.runInContext(source+`\nroute='judge';routeVersion=1;selectedTeam='T1';judgeData=${JSON.stringify(data)};renderJudgeEditor();`,ctx);
  sliders[0].value='19.5';sliders[0].oninput();assert.equal(inputs[0].value,'19.5');assert.match(total.innerHTML,/35\.0/);
  form.input();const saving=draft.onclick({currentTarget:draft});await new Promise(resolve=>setImmediate(resolve));
  assert.equal(payload.status,'draft');assert.equal(payload.scores[0],19.5);assert(payload.scores.every(n=>typeof n==='number'));
  form.isConnected=false;main.innerHTML='OTHER_TEAM_UNSAVED';vm.runInContext("selectedTeam='T2';scoreDirty=true;",ctx);resolveSave();await saving;
  assert.equal(main.innerHTML,'OTHER_TEAM_UNSAVED');assert.equal(vm.runInContext('scoreDirty',ctx),true);
});

function realRouteHarness(hash){
  const inputs=Array.from({length:6},(_,i)=>({value:'0',dataset:{score:String(i)},disabled:false}));
  const sliders=inputs.map((_,i)=>({value:'0',dataset:{slider:String(i)},disabled:false}));
  const form={elements:{feedback:{value:''},...Object.fromEntries(inputs.map((input,i)=>[`score${i}`,input]))},addEventListener(){},querySelectorAll:selector=>selector==='[data-score]'?inputs:selector==='[data-slider]'?sliders:[],querySelector:()=>({})};
  const elements={'platform-main':{innerHTML:''},'team-picker':{innerHTML:''},'judge-editor':{innerHTML:''},'score-form':form,'score-total':{innerHTML:''},'save-draft':{},recuse:{},'board-content':{innerHTML:''},'live-status':{innerHTML:'',classList:{remove(){}}}};
  const requests=[];
  const team={id:'T1',number:1,name:'Actual numeric team',score:82.5,rank:1,judgeCount:5};
  const ctx=vm.createContext({document:{getElementById:id=>elements[id]??={},querySelectorAll:()=>[],body:{classList:{remove(){},contains:()=>false}}},location:{href:`https://example.test/platform.html${hash}`,hash},URL,Date,setTimeout:()=>0,clearTimeout,fetch:async(url,options)=>{requests.push({path:url.pathname,method:options.method});const body=url.pathname.endsWith('/judge')?{teams:[team],myScores:[],dimensions:[20,20,20,15,15,10].map((max,i)=>({name:`Dimension ${i}`,max})),scoringOpen:true,submissions:[]}:{teams:[team],config:{minJudges:5,resultsPublished:true,scoringOpen:true}};return {ok:true,json:async()=>body};}});
  vm.runInContext(source+"\nsession={role:'judge',name:'Synthetic Judge'};account=()=>{};",ctx);
  return {ctx,elements,requests};
}

test('real judge route loads the original API and renders all six numeric score inputs',async()=>{
  const {ctx,elements,requests}=realRouteHarness('#judge');await vm.runInContext('renderRoute()',ctx);
  assert.deepEqual(requests,[{path:'/api/judge',method:'GET'}]);
  const html=elements['judge-editor'].innerHTML;
  assert.equal((html.match(/type="number"/g) || []).length,6);
  assert.equal((html.match(/data-score="\d"/g) || []).length,6);
  assert.match(html,/id="score-form"/);assert.match(html,/id="score-total"/);
  assert(!html.includes('id="review-form"'));
});

test('real board route loads the original API and renders numeric score and rank',async()=>{
  const {ctx,elements,requests}=realRouteHarness('#board');await vm.runInContext('renderRoute()',ctx);
  assert.deepEqual(requests,[{path:'/api/board',method:'GET'}]);
  assert.match(elements['board-content'].innerHTML,/82\.50/);
  assert.match(elements['board-content'].innerHTML,/class="rank">01<\/td>/);
  assert.match(elements['board-content'].innerHTML,/专业均分/);
});

test('professional settings edited during their POST retain new values and unsaved-input protection',async()=>{
  let resolvePost;let sent;const listeners=[],button={disabled:false,isConnected:true},error={textContent:''};
  const settings={isConnected:true,elements:{scoringOpen:{checked:true},resultsPublished:{checked:false},minJudges:{value:'1'}},addEventListener(type,listener){if(type==='input')listeners.push(listener);},closest:()=>null,querySelector:selector=>selector==='.form-error'?error:button};
  const root={innerHTML:'',insertAdjacentHTML(_position,html){this.innerHTML+=html;},querySelectorAll:()=>[settings]};
  const elements={'platform-main':root,'settings-form':settings,'platform-toast':{textContent:'',classList:{add(){},remove(){}}}};
  const ctx=vm.createContext({document:{getElementById:id=>elements[id]??={isConnected:true},querySelectorAll:()=>[]},location:{href:'https://example.test/platform.html#admin'},URL,Date,setTimeout:()=>0,clearTimeout,confirm:()=>false,fetch:async(url,options)=>{
    if(options.method==='POST'){sent=JSON.parse(options.body);return new Promise(resolve=>{resolvePost=()=>resolve({ok:true,json:async()=>({ok:true,config:{writeRevisions:{scoring:1,submissions:0,voting:0}}})});});}
    return {ok:true,json:async()=>url.pathname.endsWith('/admin')?{config:{writeRevisions:{scoring:0,submissions:0,voting:0}},teams:[],judges:[],audience:[]}:{stage:{id:'exhibition'},roster:{judgeIds:[]},reviews:[]}};
  }});
  vm.runInContext(source+"\nroute='admin';routeVersion=1;installJudgingAdmin=()=>{};installSubmissionAdmin=()=>{};",ctx);
  await vm.runInContext('renderAdmin(1)',ctx);
  listeners.forEach(listener=>listener());
  const saving=settings.onsubmit({preventDefault(){}});await new Promise(resolve=>setImmediate(resolve));
  settings.elements.minJudges.value='3';settings.elements.resultsPublished.checked=true;listeners.forEach(listener=>listener());
  resolvePost();await saving;
  assert.equal(sent.minJudges,1);assert.equal(sent.resultsPublished,false);
  assert.equal(settings.elements.minJudges.value,'3');assert.equal(settings.elements.resultsPublished.checked,true);
  assert.equal(vm.runInContext('confirmLeave()',ctx),false,'Later edits must still require an unsaved-input confirmation');
  assert.match(elements['platform-toast'].textContent,/新输入已保留/);
});

test('team score route requires team login and only renders the fetched team result',async()=>{
  const login=context('#team-score',null);
  await vm.runInContext('renderRoute()',login);
  assert.deepEqual([...login.calls],['login:team']);
  const wrong=context('#team-score','judge');
  await vm.runInContext('renderRoute()',wrong);
  assert.deepEqual([...wrong.calls],['wrong-role:team']);

  const elements={'platform-main':{innerHTML:''},'refresh-team-score':{}};
  const requests=[];
  let response={team:{number:12,name:'<Rocket>'},state:'published',score:0};
  let send=async()=>response;
  const h=vm.createContext({
    document:{getElementById:id=>elements[id],querySelectorAll:()=>[],body:{classList:{remove(){}}}},
    location:{href:'https://test.invalid/platform.html#team-score',hash:'#team-score'},
    URL,setTimeout,clearTimeout,
    fetch:async(url,options)=>{
      requests.push([url.pathname,options.headers['X-GOTC-Login']]);
      return {ok:true,json:send};
    }
  });
  vm.runInContext(source+"\nsession={role:'team',loginId:'team-marker'};account=()=>{};",h);
  await vm.runInContext('renderRoute()',h);
  assert.deepEqual(requests,[['/api/team/score','team-marker']]);
  assert.match(elements['platform-main'].innerHTML,/0\.00/);
  assert.match(elements['platform-main'].innerHTML,/12 · &lt;Rocket&gt;/);
  for(const [state,copy] of [['unpublished','成绩尚未发布'],['awaiting_scores','有效评委人数尚未达到']]){
    response={...response,state,score:null};
    await elements['refresh-team-score'].onclick();
    assert.match(elements['platform-main'].innerHTML,new RegExp(copy));
    assert.doesNotMatch(elements['platform-main'].innerHTML,/0\.00/);
  }
  let finish;
  send=()=>new Promise(resolve=>{finish=resolve;});
  const old=vm.runInContext('renderRoute()',h);
  await new Promise(resolve=>setImmediate(resolve));
  vm.runInContext("routeVersion++;session={role:'team',loginId:'new-team'};root.innerHTML='NEXT_TEAM';",h);
  finish({team:{number:12,name:'Old'},state:'published',score:99});
  await old;
  assert.equal(elements['platform-main'].innerHTML,'NEXT_TEAM');
});

test('team score login refuses a shared cookie replaced between code entry and session read',async()=>{
  const root={innerHTML:''},error={textContent:''},submit={},identity={addEventListener(){}};
  const form={isConnected:true,elements:{code:{value:'fixture',addEventListener(){}},accountId:identity},
    querySelector:selector=>selector==='button'?submit:{before(){},set textContent(value){error.textContent=value;}}};
  const requests=[],published=[];
  const ctx=vm.createContext({
    document:{getElementById:id=>id==='platform-main'?root:form,createElement:()=>({})},
    location:{href:'https://test.invalid/platform.html#team-score'},URL,
    localStorage:{getItem:()=>null,setItem:(k,v)=>published.push(v)},
    fetch:async url=>{requests.push(url.pathname);return {ok:true,json:async()=>({role:'team',loginId:url.pathname.endsWith('/login')?'A':'B'})};}
  });
  vm.runInContext(source+"\nroute='team-score';routeVersion=4;renderLogin('team');",ctx);
  await form.onsubmit({preventDefault(){}});
  assert.deepEqual(requests,['/api/login','/api/session']);
  assert.deepEqual(published,[]);
  assert.match(error.textContent,/重新输入本队提交码/);
  assert.equal(vm.runInContext('session.role',ctx),null);
});

test('a logout notification removes the visible score and invalidates an in-flight lookup',async()=>{
  const main={innerHTML:'score for A'},listeners={};let finish;
  const full=fs.readFileSync(new URL('../platform.js',import.meta.url),'utf8').split('(async()=>{try{session=await api(\'session\');}')[0];
  const ctx=vm.createContext({document:{getElementById:()=>main,querySelectorAll:()=>[],body:{classList:{remove(){}}}},
    window:{addEventListener:(name,fn)=>listeners[name]=fn},location:{href:'https://test.invalid/platform.html#team-score',hash:'#team-score'},
    URL,clearTimeout,fetch:async()=>({ok:true,json:()=>new Promise(resolve=>{finish=resolve;})})});
  vm.runInContext(full+"\nroute='team-score';session={role:'team',loginId:'A'};account=()=>{};renderLogin=()=>{root.innerHTML='LOGIN'};",ctx);
  const reading=vm.runInContext('renderTeamScore(routeVersion)',ctx);
  await new Promise(resolve=>setImmediate(resolve));
  const key=vm.runInContext('teamLogoutKey',ctx);
  listeners.storage({key,newValue:'other-session'});
  assert.equal(main.innerHTML,'score for A');
  listeners.storage({key,newValue:'A'});
  finish({state:'published',score:99,team:{number:1,name:'A'}});
  await reading;
  assert.equal(main.innerHTML,'LOGIN');
});

test('team detail groups dimensions, escapes distinct multiline comments, and hides details when unpublished',async()=>{
  const elements={'platform-main':{innerHTML:''},'refresh-team-score':{}};
  let data={team:{number:13,name:'Team 13'},state:'published',score:0,
    dimensions:[{name:'Problem <value>',score:0,max:20},{name:'Teamwork',score:0,max:10}],
    feedback:['<img src=x>\nSecond line','Other comment']};
  const ctx=vm.createContext({document:{getElementById:id=>elements[id]},location:{href:'https://test.invalid/platform.html#team-score'},URL,
    fetch:async()=>({ok:true,json:async()=>data})});
  vm.runInContext(source+"\nroute='team-score';session={role:'team',loginId:'owner'};",ctx);
  await vm.runInContext('renderTeamScore(routeVersion)',ctx);
  const html=elements['platform-main'].innerHTML;
  assert.match(html,/<dl class="own-score-dimensions"><div><dt>Problem &lt;value&gt;<\/dt><dd>0\.00 <span>\/ 20/);
  assert.match(html,/<ol class="own-score-feedback"><li class="preserve-text">&lt;img src=x&gt;\nSecond line<\/li><li/);
  assert.doesNotMatch(html,/<img/);
  data={...data,state:'unpublished',score:null};
  await vm.runInContext('renderTeamScore(routeVersion)',ctx);
  assert.doesNotMatch(elements['platform-main'].innerHTML,/Second line|own-score-dimensions/);
});
