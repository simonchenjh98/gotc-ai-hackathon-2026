import test from 'node:test';
import assert from 'node:assert/strict';
import {readSource, platformFixtureSource} from './source.mjs';
import vm from 'node:vm';

const source=readSource('platform-admin.js');
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};};
const marker=(reviewRevision=1,extra={})=>({reviewRevision,stages:{exhibition:{runId:'run-a',revision:2,canEdit:true,...extra},roadshow:{runId:'run-b',revision:0,canEdit:true}}});
const stage=()=>({stage:{id:'exhibition',runId:'run-a',revision:2},reviews:[]});
function setup(overrides={}){
  const pending=new Map();let timerId=0,listener,context={key:'view-a',stage:'exhibition',runId:'run-a',revision:2},hidden=false;
  const calls={marker:0,stage:0,applied:0,windows:[],states:[]};
  const sandbox={AbortController};vm.createContext(sandbox);vm.runInContext(source+'\nglobalThis.make=makeAdminReviewSync;',sandbox);
  const options={getContext:()=>context,isHidden:()=>hidden,
    readMarker:async()=>{calls.marker++;return marker();},readStage:async()=>{calls.stage++;return stage();},
    apply:()=>{calls.applied++;return true;},applyWindow:value=>calls.windows.push(value),status:(kind,message)=>calls.states.push({kind,message}),
    subscribe:fn=>{listener=fn;return ()=>{listener=null;};},
    schedule:(fn,delay)=>{const id=++timerId;pending.set(id,{fn,delay});return id;},cancel:id=>pending.delete(id),...overrides};
  const controller=sandbox.make(options);
  const tick=()=>{const entry=[...pending.entries()].sort((a,b)=>a[1].delay-b[1].delay)[0];assert.ok(entry,'a next check should be scheduled');pending.delete(entry[0]);return entry[1].fn();};
  return {controller,calls,pending,tick,setContext:value=>context=value,setHidden:value=>{hidden=value;listener?.();}};
}

test('first marker fetches data; unchanged marker only checks small endpoint',async()=>{
  const s=setup();s.controller.start();await s.tick();assert.equal(s.calls.stage,1);assert.equal(s.calls.applied,1);
  await s.tick();assert.equal(s.calls.marker,2);assert.equal(s.calls.stage,1);s.controller.stop();assert.equal(s.pending.size,0);
});
test('single flight coalesces another check while marker is pending',async()=>{
  const gate=deferred();let reads=0;
  const s=setup({readMarker:()=>{reads++;return gate.promise;}});s.controller.start();const running=s.tick();
  s.controller.kick();s.controller.kick();assert.equal(reads,1);gate.resolve(marker());await running;
  assert.equal(s.calls.stage,1);s.controller.stop();
});
test('failed data request does not consume marker and retries it',async()=>{
  let tries=0;const s=setup({readStage:async()=>{tries++;if(tries===1)throw new Error('temporary');return stage();}});
  s.controller.start();await s.tick();assert.equal(s.calls.applied,0);await s.tick();assert.equal(tries,2);assert.equal(s.calls.applied,1);s.controller.stop();
});
test('late data response cannot patch a different view',async()=>{
  const gate=deferred();const s=setup({readStage:()=>gate.promise});s.controller.start();const running=s.tick();await Promise.resolve();
  s.setContext({key:'view-b',stage:'roadshow',runId:'run-b',revision:0});gate.resolve(stage());await running;
  assert.equal(s.calls.applied,0);s.controller.stop();
});
test('configuration change prompts without reading or mixing the new run',async()=>{
  const s=setup({readMarker:async()=>marker(2,{runId:'new-run',revision:3})});s.controller.start();await s.tick();
  assert.equal(s.calls.stage,0);assert.equal(s.calls.applied,0);assert.equal(s.calls.states.at(-1).kind,'configuration');s.controller.stop();
});
test('deadline can change read-only badge without a new review or large fetch',async()=>{
  let checks=0;const s=setup({readMarker:async()=>marker(1,{canEdit:++checks===1})});s.controller.start();await s.tick();await s.tick();
  assert.deepEqual(s.calls.windows,[true,false]);assert.equal(s.calls.stage,1);s.controller.stop();
});
test('hidden page pauses; visible resumes; stopped controller releases listener and timer',async()=>{
  const s=setup();s.controller.start();await s.tick();s.setHidden(true);assert.equal(s.pending.size,0);
  s.setHidden(false);await s.tick();assert.equal(s.calls.marker,2);s.controller.stop();s.setHidden(false);assert.equal(s.pending.size,0);
});
test('401 and 403 stop polling and keep application state for the owner to preserve',async()=>{
  for(const status of [401,403]){const s=setup({readMarker:async()=>{throw Object.assign(new Error('denied'),{status});}});
    s.controller.start();await s.tick();assert.equal(s.calls.applied,0);assert.equal(s.pending.size,0);assert.equal(s.calls.states.at(-1).kind,'auth');}
});
test('malformed marker cannot change the badge or consume a revision',async()=>{
  for(const invalid of [null,[],{reviewRevision:1,stages:[]},marker(1,{canEdit:undefined}),marker(1,{canEdit:null}),marker(1,{canEdit:'true'}),marker(1,{runId:''}),marker(1,{revision:-1}),marker(1,{revision:'2'})]){
    let calls=0;const s=setup({readMarker:async()=>++calls===1?invalid:marker()});s.controller.start();await s.tick();
    assert.equal(s.calls.applied,0);assert.equal(s.calls.windows.length,0);assert.equal(s.calls.states.at(-1).kind,'error');
    await s.tick();assert.equal(s.calls.applied,1);assert.equal(s.calls.stage,1);s.controller.stop();
  }
});

test('read-only review patch writes only stage record statistics and preserves professional controls',()=>{
  const platform=platformFixtureSource();
  const ids=[],scrollBox={scrollLeft:0,scrollTop:0},body={children:[],innerHTML:'',closest:()=>scrollBox};
  const professional={innerHTML:'SIX_DIMENSION_PROFESSIONAL_SETTINGS',elements:{scoringOpen:{checked:true},resultsPublished:{checked:true},minJudges:{value:'5'}}};
  const elements={'platform-main':{innerHTML:'PROFESSIONAL_METRICS'},'settings-form':professional,'admin-stage-reviews':{textContent:'7'},'admin-sync-query-note':{hidden:true}};
  const view={container:{isConnected:true,querySelector:()=>body},stage:'exhibition',runId:'run-a',revision:2};
  const context=vm.createContext({document:{getElementById:id=>{ids.push(id);return elements[id];}},window:{scrollX:0,scrollY:0},location:{href:'https://example.test/platform.html#admin'},URL,Date,setTimeout,clearTimeout,view});
  vm.runInContext(platform+"\nroute='admin';adminStage='exhibition';adminReadOnlyView=view;",context);ids.length=0;
  const applied=vm.runInContext("patchAdminReviewData(view,{stage:{id:'exhibition',runId:'run-a',revision:2},reviews:[]})",context);
  assert.equal(applied,true);assert.equal(elements['admin-stage-reviews'].textContent,'0');
  assert.deepEqual(ids,['admin-stage-reviews','admin-sync-query-note']);
  assert.equal(professional.innerHTML,'SIX_DIMENSION_PROFESSIONAL_SETTINGS');assert.equal(professional.elements.minJudges.value,'5');
  assert.equal(elements['platform-main'].innerHTML,'PROFESSIONAL_METRICS');
});
