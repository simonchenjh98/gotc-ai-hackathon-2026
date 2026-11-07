import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source=fs.readFileSync(new URL('../platform-admin-finals.js',import.meta.url),'utf8');
const finalsSource=fs.readFileSync(new URL('../platform-finals.js',import.meta.url),'utf8');
const platformSource=fs.readFileSync(new URL('../platform.js',import.meta.url),'utf8');
const browserLogoutOwnerSource=platformSource.match(/^function browserLogoutOwner\(\).*$/m)?.[0];
assert.ok(browserLogoutOwnerSource,'Use the real platform logout-marker reader in lifecycle tests');
const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const flush=async()=>{await new Promise(resolve=>setImmediate(resolve));await new Promise(resolve=>setImmediate(resolve));};
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};};
const dimensions=[['creativity','创意',20],['social','社会关怀',15],['technical','技术呈现',20],['application','应用潜力',20],['crossdisciplinary','AI 跨界探索',15],['opensource','开源贡献',10]].map(([id,name,max])=>({id,name,max,award:name+'奖'}));
const team=(id,number,extra={})=>({id,number,name:'Team '+id,project:'Project '+id,eligible:true,score:91.23,rank:number,scoreCount:8,recusedCount:0,complete:true,...extra});
const award=(index,extra={})=>({id:dimensions[index].id,name:dimensions[index].award,votes:[],rawLeaders:[],suggestedWinners:[],confirmedWinners:null,totalVotes:0,maxVotes:0,...extra});
function data(overrides={}){
  return {config:{runId:'synthetic-final-run',open:true,published:false},dimensions,
    summary:{teams:[team('A',1),team('B',2)],awards:dimensions.map((_,i)=>award(i)),submittedBallots:8,expectedJudges:8,complete:true,final:false,awardsConfirmed:false,blockers:[],...overrides}};
}
function runtime(extra={}){
  const ctx=vm.createContext({AbortController,Date,Intl,URL,Map,Set,Number,Object,Array,JSON,esc,
    setTimeout,clearTimeout,sessionStorage:{getItem:()=>null},...extra});
  vm.runInContext(finalsSource+'\n'+source+'\nglobalThis.helpers={adminFinalsDashboardModel,adminFinalsDashboardMarkup,patchAdminFinalsDashboard,mountAdminFinalsDashboard,disposeAdminFinalsDashboard};',ctx);
  return ctx;
}

test('rankings keep exact server order, competition ranks and all configured teams',()=>{
  const rows=Array.from({length:13},(_,i)=>team('ROW-'+String(i+1).padStart(2,'0'),13-i,{score:90.01,rank:i<2?1:i+1}));
  const original=JSON.stringify(rows),model=runtime().helpers.adminFinalsDashboardModel(data({teams:rows}));
  assert.equal(model.teamCount,13);
  assert.equal(JSON.stringify(rows),original,'Rendering must not reorder or mutate the API data');
  for(let i=1;i<rows.length;i++)assert.ok(model.teamsHTML.indexOf(rows[i-1].name)<model.teamsHTML.indexOf(rows[i].name),'Rounded display scores must not override backend order');
  assert.match(model.teamsHTML,/Team ROW-13/,'Do not truncate the configured roster to twelve');
  assert.deepEqual([...model.teamsHTML.matchAll(/aria-label="排名 (\d+)"/g)].map(match=>Number(match[1])),rows.map(row=>row.rank));
});

test('real zero, missing scores and score completion are distinct',()=>{
  const model=runtime().helpers.adminFinalsDashboardModel(data({teams:[
    team('ZERO',1,{score:0,scoreCount:7,recusedCount:1}),
    team('MISSING',2,{score:null,rank:null,scoreCount:0,recusedCount:8,complete:false}),
    team('PARTIAL',3,{score:88,scoreCount:2,recusedCount:0,complete:false})]}));
  assert.match(model.teamsHTML,/0\.00/);
  assert.match(model.teamsHTML,/待评分|暂无评分/);
  assert.match(model.teamsHTML,/回避/);
  assert.equal(model.completedTeams,1);
  assert.match(model.teamsHTML,/88\.00/,'An incomplete team still has a provisional average');
});

test('six award columns retain all positive nominations independently of succession',()=>{
  const awards=dimensions.map((_,i)=>award(i));
  awards[1]=award(1,{votes:[{teamId:'A',count:5},{teamId:'B',count:3},{teamId:'C',count:0}],rawLeaders:['A'],suggestedWinners:['B'],totalVotes:8,maxVotes:5});
  const model=runtime().helpers.adminFinalsDashboardModel(data({teams:[team('A',1),team('B',2),team('C',3)],awards}));
  assert.equal(model.awards.length,6);
  const second=model.awards[1];
  assert.match(second.votesHTML,/Team A/);assert.match(second.votesHTML,/Team B/);
  assert.ok(!second.votesHTML.includes('Team C'),'A zero count is not a nomination');
  assert.ok(second.votesHTML.indexOf('Team A')<second.votesHTML.indexOf('Team B'));
  assert.match(second.resultHTML,/Team B/);
  assert.ok(!second.resultHTML.includes('Team A'),'The earlier award recipient must not become the next suggested winner');
  assert.equal(second.totalVotes,8);
});

test('confirmed awards remain provisional until publication, with ties preserved',()=>{
  const view=data({awardsConfirmed:true,final:true,awards:[award(0,{votes:[{teamId:'A',count:4},{teamId:'B',count:4}],confirmedWinners:['A','B'],suggestedWinners:['A','B'],totalVotes:8,maxVotes:4})]});
  view.config.open=false;
  let model=runtime().helpers.adminFinalsDashboardModel(view);
  assert.equal(model.stateFinal,false);
  assert.match(model.awards[0].resultHTML,/Team A/);assert.match(model.awards[0].resultHTML,/Team B/);
  view.config.published=true;
  model=runtime().helpers.adminFinalsDashboardModel(view);
  assert.equal(model.stateFinal,true);
  assert.match(model.stateLabel,/已发布|最终/);
});

test('empty rosters and zero-vote awards do not invent participants or winners',()=>{
  const model=runtime().helpers.adminFinalsDashboardModel(data({teams:[],submittedBallots:0,complete:false}));
  assert.equal(model.teamCount,0);assert.equal(model.awards.length,6);
  assert.ok(!model.teamsHTML.includes('Team '));
  for(const item of model.awards){assert.equal(item.totalVotes,0);assert.ok(!item.resultHTML.includes('Team '));}
  assert.match(model.rulesHTML,/草稿/);assert.match(model.rulesHTML,/回避/);
  assert.match(model.rulesHTML,/平均/);assert.match(model.rulesHTML,/最多.*1/);
});

test('names, projects and award labels cannot inject markup into the private panel',()=>{
  const view=data({teams:[team('A',1,{name:'<img src=x onerror=evil()>',project:'<svg onload=evil()> with "quotes"'})],awards:[award(0,{name:'<script>evil()</script>',votes:[{teamId:'A',count:1}],suggestedWinners:['A'],totalVotes:1})]});
  const model=runtime().helpers.adminFinalsDashboardModel(view);
  const html=[model.teamsHTML,model.rulesHTML,...model.awards.map(a=>a.votesHTML+a.resultHTML)].join('');
  assert.ok(!html.includes('<img'));assert.ok(!html.includes('<svg'));assert.ok(!html.includes('<script>'));
  assert.match(html,/&lt;img/);assert.match(html,/&lt;svg/);
});

test('missing summaries fail visibly and a stale publication is never marked final',()=>{
  const {adminFinalsDashboardModel:model}=runtime().helpers;
  for(const invalid of [null,{}, {config:{},summary:{}}, {config:{},summary:{teams:[],awards:null}}])assert.throws(()=>model(invalid));
  const view=data({final:true,awardsConfirmed:true});
  Object.assign(view.config,{published:true,open:false,publicationStale:true});
  assert.equal(model(view).stateFinal,false);
  assert.match(model(view).notice,/失效|变化/);
});

// Minimal DOM with genuine stable-node/replace semantics. Updating innerHTML
// resets scrolling and detaches children, so a whole-panel replacement cannot
// accidentally pass the lifecycle checks below.
class Events {
  listeners=new Map();
  addEventListener(name,callback){if(!this.listeners.has(name))this.listeners.set(name,new Set());this.listeners.get(name).add(callback);}
  removeEventListener(name,callback){this.listeners.get(name)?.delete(callback);}
  emit(name){for(const callback of [...(this.listeners.get(name)||[])])callback({type:name});}
  count(){return [...this.listeners.values()].reduce((sum,set)=>sum+set.size,0);}
}
class Node extends Events {
  constructor(selector=''){
    super();this.selector=selector;this.children=[];this.isConnected=true;
    this.scrollTop=0;this.scrollLeft=0;this.hidden=false;this.textContent='';this.value='';this.innerHTMLWrites=0;
    this._html='';this.classNames=new Set();this.classList={toggle:(name,force)=>{if(force)this.classNames.add(name);else this.classNames.delete(name);}};
  }
  append(node){this.children.push(node);return node;}
  detach(){this.isConnected=false;for(const node of this.children)node.detach();}
  set innerHTML(value){this._html=value;this.innerHTMLWrites++;this.scrollTop=0;this.scrollLeft=0;for(const node of this.children)node.detach();this.children=[];}
  get innerHTML(){return this._html;}
  querySelectorAll(selectors){
    const wanted=selectors.split(',').map(s=>s.trim());const found=[];
    const visit=node=>{for(const child of node.children){if(wanted.includes(child.selector))found.push(child);visit(child);}};
    visit(this);return found;
  }
  querySelector(selector){return this.querySelectorAll(selector)[0]||null;}
  contents(){return this._html+this.textContent+this.children.map(child=>child.contents()).join('');}
}
function panelDOM(){
  const panel=new Node('#admin-finals-dashboard');
  for(const name of ['counts','state','refresh','notice','teams','sync','rules'])panel.append(new Node(`[data-afd-${name}]`));
  const horizontal=panel.append(new Node('.afd-awards-scroll'));
  for(let i=0;i<6;i++){
    const card=horizontal.append(new Node(`[data-afd-award-index="${i}"]`));
    for(const name of ['award-name','total-votes','votes','result'])card.append(new Node(`[data-afd-${name}]`));
  }
  return panel;
}
function mountHarness(responses=[data()]){
  const root=new Node('#root'),heading=root.append(new Node('.page-heading'));
  const unrelated=root.append(new Node('form'));unrelated.value='unsaved organizer change';
  const document=new Events(),window=new Events();document.hidden=false;document.activeElement=unrelated;
  const timers=new Map(),calls=[],storage=new Map(),teamLogoutKey='gotc-login-owner:v1:%2F:logout';let nextTimer=0,sharedLogin='admin-a';
  const localStorage={getItem:key=>storage.get(key)??null,setItem:(key,value)=>storage.set(key,String(value))};
  const ctx=runtime({document,window,root,localStorage,teamLogoutKey,session:{role:'admin',loginId:'admin-a'},route:'admin',routeVersion:7,
    ownsBrowserLogin:owner=>owner.loginId===sharedLogin,
    setTimeout:(fn,delay)=>{const id=++nextTimer;timers.set(id,{fn,delay});return id;},clearTimeout:id=>timers.delete(id),
    api:(path,payload,options)=>{calls.push({path,payload,options});const response=responses.shift();return response instanceof Error?Promise.reject(response):Promise.resolve(response);}
  });
  vm.runInContext(browserLogoutOwnerSource,ctx);
  heading.insertAdjacentHTML=(position,markup)=>{
    assert.equal(position,'afterend');assert.match(markup,/id="admin-finals-dashboard"/);
    root.children.splice(1,0,panelDOM());
  };
  return {ctx,root,unrelated,document,window,timers,calls,responses,
    panel:()=>root.querySelector('#admin-finals-dashboard'),
    owner:loginId=>{sharedLogin=loginId;},
    logoutMarker:loginId=>localStorage.setItem(teamLogoutKey,loginId),
    mount:()=>ctx.helpers.mountAdminFinalsDashboard(7),
    async tick(delay){const entry=[...timers].find(([,timer])=>timer.delay===delay);assert.ok(entry,`Expected a ${delay}ms timer`);timers.delete(entry[0]);entry[1].fn();await flush();}
  };
}

test('mounted dashboard reads immediately with the pinned owner, then patches every two seconds without touching other work',async()=>{
  const updated=data({teams:[team('B',2,{rank:1,score:95}),team('A',1,{rank:2,score:90})],awards:[award(0,{votes:[{teamId:'B',count:3}],suggestedWinners:['B'],totalVotes:3})]});
  const h=mountHarness([data(),updated,updated]);h.mount();await flush();
  const panel=h.panel(),rank=panel.querySelector('[data-afd-teams]'),votes=panel.querySelector('[data-afd-votes]'),horizontal=panel.querySelector('.afd-awards-scroll');
  assert.equal(h.calls.length,1);assert.equal(h.calls[0].path,'admin/finals');assert.equal(h.calls[0].payload,undefined);
  assert.equal(h.calls[0].options.loginId,'admin-a');assert.ok(h.calls[0].options.signal instanceof AbortSignal);
  assert.equal(panel.querySelectorAll('[data-afd-votes]').length,6);
  rank.scrollTop=90;votes.scrollTop=20;horizontal.scrollLeft=150;h.document.activeElement=rank;
  await h.tick(2000);
  assert.equal(h.panel(),panel);assert.equal(panel.querySelector('[data-afd-teams]'),rank);
  assert.equal(rank.scrollTop,90);assert.equal(votes.scrollTop,20);assert.equal(horizontal.scrollLeft,150);
  assert.equal(h.document.activeElement,rank);assert.equal(h.unrelated.value,'unsaved organizer change');
  assert.equal(h.root.innerHTMLWrites,0);assert.equal(panel.innerHTMLWrites,0);
  assert.ok(rank.innerHTML.indexOf('Team B')<rank.innerHTML.indexOf('Team A'));
  assert.match(votes.innerHTML,/Team B/);assert.match(panel.querySelector('[data-afd-sync]').textContent,/最近同步/);
  const writes=rank.innerHTMLWrites+votes.innerHTMLWrites;await h.tick(2000);
  assert.equal(rank.innerHTMLWrites+votes.innerHTMLWrites,writes,'Unchanged results must not replace the focused/scrolling lists');
  h.ctx.helpers.disposeAdminFinalsDashboard();
});

test('patches put award labels in text nodes and keep unsafe team data escaped',()=>{
  const panel=panelDOM(),view=data();view.summary.awards[0].name='<img src=x onerror=evil()>';
  runtime().helpers.patchAdminFinalsDashboard(panel,view);
  const title=panel.querySelector('[data-afd-award-name]');
  assert.equal(title.textContent,'<img src=x onerror=evil()>');assert.equal(title.innerHTMLWrites,0);
});

test('network and malformed responses retain the last result with a stale indicator and retry',async()=>{
  const h=mountHarness([data(),new Error('offline'),{},data({teams:[team('RECOVERED',1)]})]);h.mount();await flush();
  const rank=h.panel().querySelector('[data-afd-teams]'),initial=rank.innerHTML;
  await h.tick(2000);assert.equal(rank.innerHTML,initial);assert.match(h.panel().querySelector('[data-afd-sync]').textContent,/正在重试/);
  await h.tick(2000);assert.equal(rank.innerHTML,initial);assert.match(h.panel().querySelector('[data-afd-sync]').textContent,/正在重试/);
  await h.tick(2000);assert.match(rank.innerHTML,/RECOVERED/);assert.ok(!h.panel().querySelector('[data-afd-sync]').classNames.has('afd-sync-error'));
  h.ctx.helpers.disposeAdminFinalsDashboard();
});

test('401 and 403 clear private results and remove all polling/listeners',async()=>{
  for(const status of [401,403]){
    const error=Object.assign(new Error('denied'),{status}),h=mountHarness([data(),error]);h.mount();await flush();
    await h.tick(2000);
    assert.ok(!h.panel().contents().includes('Team A'));assert.match(h.panel().contents(),/登录已失效|无权访问/);
    assert.equal(h.timers.size,0);assert.equal(h.window.count()+h.document.count(),0);
    h.window.emit('focus');h.window.emit('pageshow');assert.equal(h.calls.length,2);
  }
});

test('an ownership change aborts the old read and cannot render its late response',async()=>{
  for(const event of ['storage','focus']){
    const pending=deferred(),h=mountHarness([data(),pending.promise]);h.mount();await flush();await h.tick(2000);
    h.owner('student-b');h.window.emit(event);
    assert.equal(h.calls[1].options.signal.aborted,true);assert.equal(h.timers.size,0);
    pending.resolve(data({teams:[team('PRIVATE-LATE',1)]}));await flush();
    assert.ok(!h.panel().contents().includes('PRIVATE-LATE'));assert.ok(!h.panel().contents().includes('Team A'));
    assert.match(h.panel().contents(),/身份已变化/);assert.equal(h.calls.length,2);
  }
});

test('cross-tab logout clears immediately even while session and browser login owner remain unchanged',async()=>{
  const pending=deferred(),h=mountHarness([data(),pending.promise]);h.mount();await flush();await h.tick(2000);
  const owner=h.ctx.session;
  h.logoutMarker('admin-a');h.window.emit('storage');
  assert.equal(h.ctx.session,owner);assert.equal(h.ctx.ownsBrowserLogin(owner),true);
  assert.equal(h.ctx.browserLogoutOwner(),'admin-a');
  assert.ok(!h.panel().contents().includes('Team A'),'The storage event must wipe the private result synchronously');
  assert.equal(h.calls[1].options.signal.aborted,true);assert.equal(h.timers.size,0);
  assert.equal(h.window.count()+h.document.count(),0);
  pending.resolve(data({teams:[team('LATE-AFTER-LOGOUT',1)]}));await flush();
  assert.ok(!h.panel().contents().includes('LATE-AFTER-LOGOUT'));
  assert.equal(h.calls.length,2);assert.equal(h.timers.size,0);
});

test('a matching persisted logout marker blocks mount while an unrelated old marker allows it',async()=>{
  const blocked=mountHarness();blocked.logoutMarker('admin-a');
  assert.equal(blocked.ctx.ownsBrowserLogin(blocked.ctx.session),true);
  assert.equal(blocked.mount(),null);assert.equal(blocked.calls.length,0);assert.equal(blocked.panel(),null);
  const allowed=mountHarness();allowed.logoutMarker('previous-login');allowed.mount();await flush();
  assert.equal(allowed.calls.length,1);assert.match(allowed.panel().contents(),/Team A/);
  allowed.ctx.helpers.disposeAdminFinalsDashboard();
});

test('logout and route replacement fence late callbacks even without a storage event',async()=>{
  for(const change of [h=>{h.ctx.session={role:'guest'};},h=>{h.ctx.session.loginId='admin-b';h.owner('admin-b');},h=>{h.ctx.routeVersion++;},h=>{h.ctx.route='team';}]){
    const pending=deferred(),h=mountHarness([pending.promise]);h.mount();change(h);
    pending.resolve(data({teams:[team('PRIVATE-LATE',1)]}));await flush();
    assert.ok(!h.panel().contents().includes('PRIVATE-LATE'));assert.equal(h.timers.size,0);
    assert.equal(h.window.count()+h.document.count(),0);
  }
});

test('disposal aborts in-flight requests and never lets an old view overwrite a remounted panel',async()=>{
  const pending=deferred(),h=mountHarness([pending.promise,data({teams:[team('NEW-VIEW',1)]})]);h.mount();const old=h.panel();
  h.ctx.helpers.disposeAdminFinalsDashboard();assert.equal(h.calls[0].options.signal.aborted,true);
  h.mount();await flush();const current=h.panel();assert.notEqual(current,old);
  pending.resolve(data({teams:[team('OLD-VIEW',1)]}));await flush();
  assert.match(current.contents(),/NEW-VIEW/);assert.ok(!current.contents().includes('OLD-VIEW'));assert.equal(old.contents(),'');
  assert.equal(h.calls.length,2);h.ctx.helpers.disposeAdminFinalsDashboard();
});

test('hidden tabs pause polling; returning visible refreshes with the original owner',async()=>{
  const h=mountHarness([data(),data({teams:[team('VISIBLE',1)]})]);h.mount();await flush();
  h.document.hidden=true;h.document.emit('visibilitychange');assert.equal(h.timers.size,0);
  h.window.emit('focus');assert.equal(h.calls.length,1);assert.equal(h.timers.size,0);
  h.document.hidden=false;h.document.emit('visibilitychange');await h.tick(0);
  assert.match(h.panel().contents(),/VISIBLE/);assert.equal(h.calls[1].options.loginId,'admin-a');
  h.ctx.helpers.disposeAdminFinalsDashboard();
});

test('page cache suspension clears private results and restores only after a fresh read',async()=>{
  const pending=deferred(),h=mountHarness([data(),pending.promise]);h.mount();await flush();
  h.window.emit('pagehide');assert.ok(!h.panel().contents().includes('Team A'));assert.equal(h.timers.size,0);
  h.window.emit('pageshow');await h.tick(0);assert.ok(!h.panel().contents().includes('Team A'));
  pending.resolve(data({teams:[team('RESTORED',1)]}));await flush();assert.match(h.panel().contents(),/RESTORED/);
  h.ctx.helpers.disposeAdminFinalsDashboard();
});

test('a cached page with another browser login clears instead of refetching',async()=>{
  const h=mountHarness([data()]);h.mount();await flush();h.window.emit('pagehide');h.owner('another-admin');h.window.emit('pageshow');
  assert.ok(!h.panel().contents().includes('Team A'));assert.match(h.panel().contents(),/身份已变化/);
  assert.equal(h.calls.length,1);assert.equal(h.timers.size,0);
});

test('a request aborted on pagehide cannot repopulate restored cache with its late result',async()=>{
  const oldRequest=deferred(),freshRequest=deferred(),h=mountHarness([data(),oldRequest.promise,freshRequest.promise]);
  h.mount();await flush();await h.tick(2000);
  h.window.emit('pagehide');assert.equal(h.calls[1].options.signal.aborted,true);
  h.window.emit('pageshow');oldRequest.resolve(data({teams:[team('PRE-SUSPEND-PRIVATE',1)]}));await flush();
  assert.ok(!h.panel().contents().includes('PRE-SUSPEND-PRIVATE'),'An aborted response must not restore private results after bfcache');
  await h.tick(0);assert.equal(h.calls.length,3);
  freshRequest.resolve(data({teams:[team('FRESH-AFTER-RESTORE',1)]}));await flush();
  assert.match(h.panel().contents(),/FRESH-AFTER-RESTORE/);h.ctx.helpers.disposeAdminFinalsDashboard();
});

test('mounting outside an owned organizer route never requests private data',()=>{
  for(const change of [h=>{h.ctx.route='team';},h=>{h.ctx.routeVersion=8;},h=>{h.ctx.session.role='team';},h=>{h.ctx.session.loginId='';},h=>h.owner('other')]){
    const h=mountHarness();change(h);assert.equal(h.mount(),null);assert.equal(h.calls.length,0);assert.equal(h.panel(),null);
  }
});
