import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const sources=['platform-finals.js','platform-roadshow-scoring.js'].map(name=>fs.readFileSync(new URL('../'+name,import.meta.url),'utf8')).join('\n');
const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};};
const team=(id,number=1,name='Team '+id,eligible=true)=>({id,number,name,project:'Project '+id,eligible});
function runtime(extra={}){
  const ctx=vm.createContext({AbortController,Map,Set,Number,Object,Date,JSON,esc,setTimeout,clearTimeout,confirm:()=>true,toast(){},dirtyAdminForms:new Set(),
    sessionStorage:{getItem:()=>null,setItem(){},removeItem(){}},window:{addEventListener(){},removeEventListener(){}},document:{addEventListener(){},removeEventListener(){},hidden:false},
    session:{role:'admin',loginId:'admin-a'},route:'admin-roadshow',routeVersion:1,ownsBrowserLogin:()=>true,...extra});
  vm.runInContext(sources,ctx);ctx.finalsSetAccess({accessToken:'synthetic-finals-authorization-0123456789',expiresAt:Date.now()+3600000});return ctx;
}
function choices(kind='teamIds'){
  let html='';return {inputs:[],contains:()=>false,get innerHTML(){return html;},set innerHTML(value){html=value;this.inputs=[...value.matchAll(/<input\b([^>]+)>/g)].map(match=>({value:match[1].match(/value="([^"]*)"/)?.[1],checked:/\bchecked\b/.test(match[1]),disabled:/\bdisabled\b/.test(match[1]),kind}));},querySelectorAll(){return this.inputs;}};
}
function formFixture(teamChoices=null,judgeChoices=null){
  const listeners={},nodes={fieldset:{disabled:false},'[data-finals-save]':{},'.form-error':{textContent:''},'[data-finals-recovery]':{innerHTML:'',querySelector:()=>({})},'[data-roadshow-team-choices]':teamChoices,'[data-roadshow-select-all]':{},'[data-roadshow-refresh-teams]':{},'[data-roadshow-roster-state]':{}};
  const form={isConnected:true,elements:{},addEventListener(type,fn){(listeners[type]??=[]).push(fn);},emit(type='input'){listeners[type]?.forEach(fn=>fn());},querySelector:selector=>nodes[selector],querySelectorAll:selector=>selector==='[data-finals-save]'?[nodes['[data-finals-save]']]:selector.includes('teamIds')?(teamChoices?.inputs||[]).filter(item=>!selector.includes(':checked') || item.checked):selector.includes('judgeIds')?(judgeChoices?.inputs||[]).filter(item=>!selector.includes(':checked') || item.checked):[]};
  return {form,nodes};
}
const roadshowData=(revision=5,teamIds=['a'],availableTeams=[team('a'),team('b',2)])=>({config:{runId:'roadshow-run',revision:revision+20,stageRevision:revision},roster:{teamIds,editable:true},availableTeams});
function roadshowFixture(api){
  const ctx=runtime({api}),host=choices(),f=formFixture(host),view=ctx.createFinalsView(1,'admin'),applied=[];
  view.container={isConnected:true,querySelector:selector=>selector==='[data-roadshow-teams]'?f.form:{},querySelectorAll:()=>[]};
  const editor=ctx.installRoadshowTeams(view,data=>applied.push(data));editor.update(roadshowData());
  return {ctx,view,host,...f,editor,applied};
}

test('roadshow candidates load from team info but only an explicit select-all action selects new eligible teams',()=>{
  const h=roadshowFixture();h.editor.update(roadshowData(5,['a'],[team('a'),team('b',2),team('c',3),team('x',4,'Withdrawn',false)]));
  assert.deepEqual(h.host.inputs.filter(i=>i.checked).map(i=>i.value),['a']);assert.match(h.host.innerHTML,/Project c/);
  h.nodes['[data-roadshow-select-all]'].onclick();assert.deepEqual(h.host.inputs.filter(i=>i.checked).map(i=>i.value),['a','b','c']);assert.equal(h.ctx.dirtyAdminForms.has(h.form),true);h.ctx.disposeFinalsView();
});

test('catalog refresh preserves unsaved selections and cannot silently advance their stale roster revision',async()=>{
  const sent=[],h=roadshowFixture(async(path,payload)=>{sent.push({path,payload});throw Object.assign(new Error('conflict'),{status:409});});
  h.host.inputs.find(i=>i.value==='b').checked=true;h.form.emit();h.editor.update(roadshowData(6,['a'],[team('a',1,'Renamed'),team('b',2),team('c',3)]));
  assert.deepEqual(h.host.inputs.filter(i=>i.checked).map(i=>i.value),['a','b']);assert.match(h.host.innerHTML,/Renamed/);
  await h.form.onsubmit({preventDefault(){}});await h.form.onsubmit({preventDefault(){}});
  assert.equal(sent.length,1);assert.equal(sent[0].payload.expectedRevision,5);assert.equal(sent[0].payload.runId,'roadshow-run');assert.equal(sent[0].path,'admin/roadshow/teams');assert.equal('judgeIds' in sent[0].payload,false);assert.equal('open' in sent[0].payload,false);assert.equal(h.form.finalsBlocked,true);h.ctx.disposeFinalsView();
});

test('a submitted roster is single flight and later checkbox edits survive its acknowledgement',async()=>{
  const gate=deferred(),sent=[],h=roadshowFixture(async(_path,payload)=>{sent.push(payload);return sent.length===1?gate.promise:roadshowData(7,payload.teamIds);});
  h.host.inputs[1].checked=true;h.form.emit();const pending=h.form.onsubmit({preventDefault(){}});await h.form.onsubmit({preventDefault(){}});
  h.host.inputs[0].checked=false;h.form.emit();gate.resolve(roadshowData(6,['a','b']));await pending;
  assert.equal(sent.length,1);assert.deepEqual(h.host.inputs.filter(i=>i.checked).map(i=>i.value),['b']);assert.equal(h.ctx.dirtyAdminForms.has(h.form),true);
  await h.form.onsubmit({preventDefault(){}});assert.equal(sent[1].expectedRevision,6);assert.deepEqual([...sent[1].teamIds],['b']);h.ctx.disposeFinalsView();
});

test('a newly ineligible selected candidate can be explicitly removed and frozen rosters cannot save',async()=>{
  const sent=[],h=roadshowFixture(async(_path,payload)=>{sent.push(payload);return roadshowData(6,payload.teamIds);});
  h.editor.update(roadshowData(5,['a'],[team('a',1,'Retired',false),team('b',2)]));const retired=h.host.inputs.find(i=>i.value==='a');assert.equal(retired.checked,true);assert.equal(retired.disabled,false);
  retired.checked=false;h.host.inputs.find(i=>i.value==='b').checked=true;h.form.emit();await h.form.onsubmit({preventDefault(){}});assert.deepEqual([...sent[0].teamIds],['b']);
  h.editor.update({...roadshowData(7,['b']),roster:{teamIds:['b'],editable:false}});assert.equal(h.nodes.fieldset.disabled,true);await h.form.onsubmit({preventDefault(){}});assert.equal(sent.length,1);h.ctx.disposeFinalsView();
});

test('a roster save from an old administrator cannot replace the new page or input',async()=>{
  const gate=deferred(),h=roadshowFixture(()=>gate.promise),pending=h.form.onsubmit({preventDefault(){}});h.ctx.session={role:'admin',loginId:'other'};gate.resolve(roadshowData(6,['b']));await pending;
  assert.equal(h.applied.length,0);assert.deepEqual(h.host.inputs.filter(i=>i.checked).map(i=>i.value),['a']);h.ctx.disposeFinalsView();
});

function judgeFixture(roadshow=false,initialTeams=[team('a')]){
  const ctx=runtime({route:roadshow?'judge-roadshow':'judge-finals',session:{role:'judge',loginId:'judge-a'}}),view=ctx.createFinalsView(1,roadshow?'judge':'seat',roadshow?null:{id:'seat-a',name:'Judge'});
  const notice={},auth={},select={innerHTML:'',value:'a'},window={},rendered=[];
  const score=formFixture(),nomination=formFixture();score.form.finalsRefreshButtons=()=>{score.nodes['[data-finals-save]'].disabled=!ctx.scoringCanEditTeam(view,view.selected,roadshow);};nomination.form.finalsRefreshButtons=()=>{};
  ctx.finalsTrack(view,score.form);if(!roadshow)ctx.finalsTrack(view,nomination.form);
  view.container={isConnected:true,querySelector:selector=>selector==='[data-finals-auth]'?auth:selector.includes('team]')?select:selector==='[data-finals-window-state]'?window:notice,querySelectorAll:()=>[score.nodes['[data-finals-save]']]};
  view.config={runId:'run-a',revision:1,open:true,canEdit:true};view.data={teams:initialTeams,dimensions:[]};view.scores=new Map();view.scoreForm=score.form;view.nominationForm=roadshow?null:nomination.form;view.nominationRecord=null;view.selected=initialTeams[0]?.id || '';
  ctx.roadshowRenderScore=ctx.finalsRenderScore=current=>{rendered.push({selected:current.selected,records:[...current.scores.values()]});current.scoreForm=score.form;};ctx.finalsRenderNominations=(_view,record)=>{rendered.push({nomination:record});};
  const read=(teams=initialTeams,extra={})=>({config:{runId:'run-a',revision:1,open:true,canEdit:true},teams,myScores:[],myNominations:null,...extra});
  return {ctx,view,score,nomination,notice,auth,select,window,rendered,read,sync:data=>ctx.syncScoringJudge(view,data,roadshow)};
}

for(const roadshow of [false,true]){
  test(`${roadshow?'roadshow':'finals'} polls add confirmed teams, rename options, retain selection and populate an initially empty page`,()=>{
    const h=judgeFixture(roadshow);h.sync(h.read([team('a',1,'Updated'),team('b',2)]));assert.equal(h.select.value,'a');assert.match(h.select.innerHTML,/Updated/);assert.match(h.select.innerHTML,/Team b/);h.ctx.disposeFinalsView();
    const empty=judgeFixture(roadshow,[]);empty.sync(empty.read([team('b',2)]));assert.equal(empty.select.value,'b');assert.equal(empty.view.selected,'b');empty.ctx.disposeFinalsView();
  });

  test(`${roadshow?'roadshow':'finals'} dirty or pending score input survives roster changes and disqualification immediately prevents submission`,()=>{
    const h=judgeFixture(roadshow);h.ctx.dirtyAdminForms.add(h.score.form);h.score.form.inputValue='UNSAVED';h.sync(h.read([team('a',1,'Retired',false),team('b',2)]));
    assert.equal(h.rendered.length,0);assert.equal(h.score.form.inputValue,'UNSAVED');assert.equal(h.score.nodes.fieldset.disabled,true);assert.equal(h.score.nodes['[data-finals-save]'].disabled,true);assert.equal(h.notice.hidden,false);
    h.ctx.dirtyAdminForms.delete(h.score.form);h.score.form.finalsBusy=true;h.sync(h.read([team('a'),team('c',3)]));assert.equal(h.rendered.length,0);h.ctx.disposeFinalsView();
  });

  test(`${roadshow?'roadshow':'finals'} an older GET cannot roll back a score acknowledged by a later POST`,()=>{
    const h=judgeFixture(roadshow),ack={teamId:'a',revision:5,status:'submitted',scores:[20,15,20,20,15,10]};h.view.scores.set('a',ack);
    h.sync(h.read([team('a',1,'New name')],{myScores:[{teamId:'a',revision:4,status:'draft',scores:[1,1,1,1,1,1]}]}));assert.equal(h.view.scores.get('a').revision,5);assert.equal(h.rendered[0].records[0].status,'submitted');
    h.sync(h.read(undefined,{config:{runId:'other-run',revision:2,open:true,canEdit:true}}));assert.equal(h.view.locked,true);assert.match(h.auth.textContent,/轮次已变化/);h.ctx.disposeFinalsView();
  });
}

test('dirty finals nominations prevent roster redraw and an older nomination response never replaces the acknowledged ballot',()=>{
  const h=judgeFixture(),ack={revision:4,status:'submitted',nominations:{creativity:'a'}};h.view.nominationRecord=ack;h.ctx.dirtyAdminForms.add(h.nomination.form);
  h.sync(h.read([team('a'),team('b',2)],{myNominations:{revision:3,status:'draft'}}));assert.equal(h.rendered.length,0);assert.equal(h.view.nominationRecord,ack);
  h.ctx.dirtyAdminForms.delete(h.nomination.form);h.sync(h.read([team('a'),team('b',2)],{myNominations:{revision:3,status:'draft'}}));assert.equal(h.view.nominationRecord,ack);assert.equal(h.rendered.find(x=>x.nomination).nomination.revision,4);h.ctx.disposeFinalsView();
});

test('manual reread cannot dispose a pending score or nomination save',()=>{
  const calls=[],h=judgeFixture();h.ctx.renderFinalsJudge=()=>calls.push('reload');h.ctx.toast=message=>calls.push(message);h.score.form.finalsBusy=true;
  h.ctx.finalsConfirmReload(h.view);assert.equal(calls.includes('reload'),false);assert.match(calls[0],/正在保存/);h.score.form.finalsBusy=false;h.nomination.form.finalsBusy=true;h.ctx.finalsConfirmReload(h.view);assert.equal(calls.includes('reload'),false);h.ctx.disposeFinalsView();
});

function finalsAdminFixture(){
  const ctx=runtime({route:'admin-finals'}),view=ctx.createFinalsView(1,'admin'),host=choices(),judges=choices('judgeIds'),roster=formFixture(host,judges),settings=formFixture(),create=formFixture(),refresh={};
  host.innerHTML=ctx.scoringTeamChoicesMarkup([team('a'),team('b',2)],['a']);const seats=Array.from({length:8},(_,i)=>({id:'j'+i,name:'Judge '+i,active:true}));judges.innerHTML=ctx.finalsJudgeChecks(seats,seats.map(j=>j.id));settings.form.elements={open:{checked:false},published:{checked:false}};
  const nodes={'[data-finals-roster]':roster.form,'[data-finals-settings]':settings.form,'[data-finals-create-judges]':create.form,'[data-finals-team-list]':host,'[data-finals-judge-list]':judges,'[data-finals-refresh-teams]':refresh};
  view.container={isConnected:true,querySelector:selector=>nodes[selector],querySelectorAll:()=>[]};view.config={runId:'finals-run',revision:1,rosterLocked:false,teamIds:['a'],judgeIds:seats.map(j=>j.id)};
  ctx.finalsInstallAdmin(view);return {ctx,view,host,roster,judges,seats,refresh,data:(revision=2)=>({config:{...view.config,revision},availableTeams:[team('a'),team('b',2),team('c',3)],availableJudges:seats})};
}

test('finals candidate refresh preserves unsaved choices and never automatically admits newly registered teams',()=>{
  const h=finalsAdminFixture();h.host.inputs.find(i=>i.value==='b').checked=true;h.roster.form.emit();h.view.updateRosterCandidates(h.data());
  assert.deepEqual(h.host.inputs.filter(i=>i.checked).map(i=>i.value),['a','b']);assert.equal(h.host.inputs.find(i=>i.value==='c').checked,false);assert.equal(h.roster.form.finalsRevision,1);
  h.ctx.dirtyAdminForms.delete(h.roster.form);h.view.updateRosterCandidates(h.data());assert.deepEqual(h.host.inputs.filter(i=>i.checked).map(i=>i.value),['a']);assert.equal(h.roster.form.finalsRevision,2);h.ctx.disposeFinalsView();
});

test('a manual finals candidate read preserves edits made while pending and a newly frozen roster becomes read-only',async()=>{
  const h=finalsAdminFixture(),gate=deferred();h.ctx.api=()=>gate.promise;const pending=h.refresh.onclick();h.host.inputs.find(i=>i.value==='b').checked=true;h.roster.form.emit();gate.resolve(h.data());await pending;
  assert.equal(h.host.inputs.find(i=>i.value==='b').checked,true);assert.equal(h.roster.form.finalsRevision,1);
  h.view.updateRosterCandidates({...h.data(3),config:{...h.data(3).config,rosterLocked:true}});assert.equal(h.roster.nodes.fieldset.disabled,true);h.ctx.disposeFinalsView();
});
