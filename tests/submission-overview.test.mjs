import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const source=fs.readFileSync(new URL('../platform.js',import.meta.url),'utf8').split("window.addEventListener('hashchange'")[0];
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};};
function load(document={getElementById:()=>({})}){const context=vm.createContext({document,location:{href:'https://example.test/platform.html#admin'},URL,TextEncoder,Date,Intl,setTimeout,clearTimeout});vm.runInContext(source+'\nglobalThis.h={submissionOverviewSelection,submissionOverviewState,submissionVersionDifferences,submissionOverviewCard,createSubmissionOverviewController,installSubmissionOverview,submissionOverviewStamp,updateSubmissionOverview};',context);return {h:context.h,context};}
const {h}=load();
const formal={projectName:'Formal project',codeVersion:'a'.repeat(40)};
function snapshot(version,more={}){return {runId:'R1',inRoster:true,frozenAt:100,materialSnapshot:{submissionVersion:version,materials:formal},...more};}
function dataset(){return {teams:[1,2,3,4,5].map(number=>({id:'T'+number,number,name:number===4?'ALPHA Team':'Team '+number,project:'Registered '+number})),submissions:[
  {teamId:'T1',submission:{version:0,draft:null,submitted:null}},
  {teamId:'T2',submission:{version:1,draft:{projectName:'Draft Search'},submitted:null}},
  {teamId:'T3',submission:{version:2,draft:formal,submitted:formal,submittedVersion:2,hasUnsubmittedChanges:false},judgingMaterials:{exhibition:snapshot(2)}},
  {teamId:'T4',submission:{version:4,draft:{projectName:'Changed'},submitted:formal,submittedVersion:2,hasUnsubmittedChanges:true},judgingMaterials:{exhibition:snapshot(1)}},
  {teamId:'T5',submission:{version:7,draft:formal,submitted:formal,submittedVersion:2,hasUnsubmittedChanges:false},judgingMaterials:{roadshow:snapshot(1)}}]};}
const defaults={query:'',status:'all',onlyVersionDifference:false};
test('four mutually exclusive states count all teams and unchanged later draft stays formally submitted',()=>{
  const result=h.submissionOverviewSelection(dataset(),defaults);
  assert.deepEqual({...result.counts},{empty:1,draft:1,submitted:2,changes:1});assert.equal(result.total,5);
  assert.equal(h.submissionOverviewState(dataset().submissions[4].submission),'submitted');
});
test('search matches trimmed case-insensitive name, padded number, registered/formal/draft project names',()=>{
  for(const [query,ids] of [[' alpha ',['T4']],['02',['T2']],['draft search',['T2']],['registered 1',['T1']],['FORMAL PROJECT',['T3','T4','T5']]])assert.deepEqual(Array.from(h.submissionOverviewSelection(dataset(),{...defaults,query}).rows,row=>row.team.id),ids);
});
test('combined filters intersect while status counters stay global; clear restores all',()=>{
  const filtered=h.submissionOverviewSelection(dataset(),{query:'team',status:'submitted',onlyVersionDifference:true});
  assert.deepEqual(Array.from(filtered.rows,row=>row.team.id),['T5']);assert.equal(filtered.counts.submitted,2);assert.equal(filtered.total,5);
  assert.equal(h.submissionOverviewSelection(dataset(),{...defaults,query:'not present'}).rows.length,0);
  assert.equal(h.submissionOverviewSelection(dataset(),defaults).rows.length,5);
});
test('only actual frozen snapshots with a different formal version count as a difference',()=>{
  for(const stage of [undefined,snapshot(1,{inRoster:false}),snapshot(1,{frozenAt:null}),snapshot(1,{materialSnapshot:null})])assert.equal(h.submissionVersionDifferences({submission:{submitted:formal,submittedVersion:2},judgingMaterials:{exhibition:stage}}).length,0);
  assert.equal(h.submissionVersionDifferences({submission:{submitted:null,submittedVersion:null},judgingMaterials:{exhibition:snapshot(1)}}).length,0);
  assert.equal(h.submissionVersionDifferences(dataset().submissions[3])[0].stage,'exhibition');
  assert.equal(h.submissionVersionDifferences(dataset().submissions[4])[0].stage,'roadshow');
});
test('card puts version mismatch in visible summary and escapes server-provided text',()=>{
  const row=h.submissionOverviewSelection(dataset(),{...defaults,query:'alpha'}).rows[0];row.team.name='<img src=x onerror=alert(1)>';
  const html=h.submissionOverviewCard(row);
  assert.ok(html.indexOf('展示评审本轮 v1，最新正式 v2')<html.indexOf('<details class="submission-details">'));assert.ok(!html.includes('<img'));assert.match(html,/&lt;img/);
});
function controller(data=dataset(),load=async()=>data){const paints=[],errors=[];let alive=true;const c=h.createSubmissionOverviewController({data,filters:defaults,load,render:(next,filters)=>paints.push(h.submissionOverviewSelection(next,filters)),isCurrent:()=>alive,onError:error=>errors.push(error.message)});return {c,paints,errors,leave:()=>{alive=false;}};}
test('refresh keeps filters changed while response was pending and renders only fresh data',async()=>{
  const request=deferred(),f=controller(dataset(),()=>request.promise);const pending=f.c.refresh();f.c.setFilters({query:'alpha',status:'changes'});request.resolve(dataset());await pending;
  assert.equal(f.c.model.filters.query,'alpha');assert.deepEqual(Array.from(f.paints.at(-1).rows,row=>row.team.id),['T4']);
});
test('stage changes reload latest formal data before evaluating a newly frozen version',async()=>{
  const fresh=dataset();fresh.submissions[3].submission.submittedVersion=6;fresh.submissions[3].judgingMaterials.exhibition=snapshot(6);let reads=0;const f=controller(dataset(),async()=>{reads++;return fresh;});f.c.setFilters({onlyVersionDifference:true});
  await f.c.stageChanged({stage:{id:'exhibition',runId:'R1',frozenAt:100},teams:[{id:'T4',materialSnapshot:{submissionVersion:6}}]});
  assert.equal(reads,1);assert.ok(!f.paints.at(-1).rows.some(row=>row.team.id==='T4'));assert.equal(f.c.model.filters.onlyVersionDifference,true);
});
test('stage mutation during a pending read coalesces a fresh read and never paints stale data',async()=>{
  const first=deferred(),second=deferred();let reads=0;const f=controller(dataset(),()=>++reads===1?first.promise:second.promise);const pending=f.c.refresh();f.c.stageChanged({stage:{id:'roadshow',runId:'NEW',frozenAt:null},teams:[]});first.resolve({teams:[],submissions:[]});await new Promise(resolve=>setImmediate(resolve));assert.equal(reads,2);assert.equal(f.paints.length,0);second.resolve(dataset());await pending;assert.equal(f.paints.at(-1).total,5);
});
test('old directory response and late off-route response cannot replace newer overview data',async()=>{
  const f=controller();const old=f.c.stamp();const newData={teams:[],submissions:[]};assert.equal(f.c.replace(newData),true);assert.equal(f.c.replace(dataset(),old),false);assert.equal(f.c.model.data.teams.length,0);
  const request=deferred(),late=controller(dataset(),()=>request.promise);const pending=late.c.refresh();late.leave();request.resolve(newData);await pending;assert.equal(late.paints.length,0);
});
test('failed refresh retains data and filters and a retry can recover',async()=>{
  let reads=0;const f=controller(dataset(),async()=>{if(++reads===1)throw new Error('offline');return {teams:[],submissions:[]};});f.c.setFilters({status:'draft'});await f.c.refresh();assert.equal(f.c.model.data.teams.length,5);assert.deepEqual(f.errors,['offline']);await f.c.refresh();assert.equal(f.c.model.data.teams.length,0);assert.equal(f.c.model.filters.status,'draft');
});
test('real overview controls update only their region, retain unrelated form/code and support clear/refresh',async()=>{
  const nodes=new Map();function node(id){if(!nodes.has(id))nodes.set(id,{id,isConnected:true,value:'',checked:false,innerHTML:'',textContent:'',disabled:false,dataset:{}});return nodes.get(id);}
  const {h,context}=load({getElementById:node});const input=node('unrelated-admin-input'),code=node('new-code');input.value='UNSAVED';code.innerHTML='ONE_TIME_CODE';
  let reads=0;context.transport=async()=>{reads++;return dataset();};vm.runInContext('api=transport;route="admin";routeVersion=1;',context);
  node('submission-status-filter').value='all';h.installSubmissionOverview(dataset());node('submission-search').value='alpha';node('submission-search').oninput();assert.match(node('submission-results-count').textContent,/显示 1 \/ 全部 5 队/);
  node('submission-status-filter').value='draft';node('submission-status-filter').onchange();assert.match(node('submission-admin-list').innerHTML,/没有符合当前筛选/);
  node('clear-submission-filters').onclick();assert.match(node('submission-results-count').textContent,/显示 5 \/ 全部 5 队/);
  await node('refresh-submission-overview').onclick({currentTarget:node('refresh-submission-overview')});assert.equal(reads,1);assert.equal(input.value,'UNSAVED');assert.equal(code.innerHTML,'ONE_TIME_CODE');
});

test('later directory read wins over older overview read in either arrival order',async()=>{
  for(const oldFirst of [true,false]){
    const request=deferred(),f=controller(dataset(),()=>request.promise),pending=f.c.refresh();
    const directory=f.c.stamp(),newData={teams:[{id:'NEW',number:99,name:'NEW'}],submissions:[]};
    if(oldFirst){request.resolve({teams:[],submissions:[]});await pending;assert.equal(f.c.replace(newData,directory),true);}
    else {assert.equal(f.c.replace(newData,directory),true);request.resolve({teams:[],submissions:[]});await pending;}
    assert.equal(f.c.model.data.teams[0].id,'NEW');assert.equal(f.paints.at(-1).total,1);
  }
});
test('filtering during a credential rotation keeps replacement controls busy and sends only one POST',async()=>{
  const nodes=new Map();function node(id){if(nodes.has(id))return nodes.get(id);const item={id,isConnected:true,value:'',checked:false,disabled:false,dataset:{},textContent:'',classList:{add(){},remove(){}},scrollIntoView(){},querySelectorAll:()=>item.buttons || []};let html='';Object.defineProperty(item,'innerHTML',{get:()=>html,set(value){html=value;if(id==='submission-admin-list')item.buttons=[...value.matchAll(/<button([^>]*data-team-access="([^"]+)"[^>]*)>/g)].map(match=>({disabled:/\bdisabled\b/.test(match[1]),isConnected:true,dataset:{teamAccess:match[2],action:/data-action="([^"]+)"/.exec(match[1])[1]}}));}});nodes.set(id,item);return item;}
  const {h,context}=load({getElementById:node}),waiting=deferred(),data=dataset();data.submissions[2].access={id:'ACCESS',active:true};let posts=0;
  context.transport=async(path)=>{if(path==='admin/team-access'){posts++;return waiting.promise;}assert.equal(path,'admin');return data;};context.confirm=()=>true;vm.runInContext('api=transport;route="admin";routeVersion=1;',context);node('submission-status-filter').value='all';h.installSubmissionOverview(data);
  const list=node('submission-admin-list'),find=()=>list.buttons.find(button=>button.dataset.teamAccess==='T3' && button.dataset.action==='rotate');
  list.onclick({target:{closest:()=>find()}});assert.equal(posts,1);
  node('submission-search').value='Team 3';node('submission-search').oninput();assert.equal(find().disabled,true);
  list.onclick({target:{closest:()=>find()}});assert.equal(posts,1);
  waiting.resolve({code:'SYNTHETIC_NEW_CODE'});await new Promise(resolve=>setImmediate(resolve));assert.equal(posts,1);assert.equal(find().disabled,false);assert.match(node('team-access-code').innerHTML,/SYNTHETIC_NEW_CODE/);
});

test('obsolete refresh failure cannot replace a newer directory success with an error',async()=>{
  const waiting=deferred(),f=controller(dataset(),()=>waiting.promise),pending=f.c.refresh(),stamp=f.c.stamp();
  f.c.replace({teams:[],submissions:[]},stamp);waiting.reject(new Error('OLD_FAILURE'));await pending;
  assert.deepEqual(f.errors,[]);assert.equal(f.c.model.data.teams.length,0);
});
test('returning to the overview during an old credential request releases replacement controls safely',async()=>{
  const nodes=new Map();function node(id){if(nodes.has(id))return nodes.get(id);const item={id,isConnected:true,value:'',checked:false,disabled:false,dataset:{},textContent:'',classList:{add(){},remove(){}},scrollIntoView(){},querySelectorAll:()=>item.buttons || []};let html='';Object.defineProperty(item,'innerHTML',{get:()=>html,set(value){html=value;if(id==='submission-admin-list')item.buttons=[...value.matchAll(/<button([^>]*data-team-access="([^"]+)"[^>]*)>/g)].map(match=>({disabled:/\bdisabled\b/.test(match[1]),isConnected:true,dataset:{teamAccess:match[2],action:/data-action="([^"]+)"/.exec(match[1])[1]}}));}});nodes.set(id,item);return item;}
  const {h,context}=load({getElementById:node}),waiting=deferred(),data=dataset();data.submissions[2].access={id:'ACCESS',active:true};context.transport=async()=>waiting.promise;context.confirm=()=>true;vm.runInContext('api=transport;route="admin";routeVersion=1;',context);node('submission-status-filter').value='all';h.installSubmissionOverview(data);
  const list=node('submission-admin-list'),find=()=>list.buttons.find(button=>button.dataset.teamAccess==='T3' && button.dataset.action==='rotate');list.onclick({target:{closest:()=>find()}});
  h.installSubmissionOverview(data);assert.equal(find().disabled,true);waiting.resolve({code:'OLD_VIEW_CODE'});await new Promise(resolve=>setImmediate(resolve));assert.equal(find().disabled,false);assert.ok(!node('team-access-code').innerHTML.includes('OLD_VIEW_CODE'));
});

test('a stage containing a newly added team refreshes an initially empty overview',async()=>{
  let reads=0;const f=controller({teams:[],submissions:[]},async()=>{reads++;return dataset();});
  await f.c.stageChanged({stage:{id:'exhibition',runId:'NEW',frozenAt:100},teams:[{id:'T4',materialSnapshot:{submissionVersion:1}}]});
  assert.equal(reads,1);assert.equal(f.c.model.data.teams.length,5);
});
