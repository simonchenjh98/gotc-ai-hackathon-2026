import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source=fs.readFileSync(new URL('../platform-admin.js',import.meta.url),'utf8')+'\n'+
  fs.readFileSync(new URL('../platform.js',import.meta.url),'utf8').split("window.addEventListener('hashchange'")[0];
const wait=()=>new Promise(resolve=>setImmediate(resolve));
const settle=async()=>{await wait();await wait();await wait();};
const deferred=()=>{let resolve;const promise=new Promise(r=>{resolve=r;});return {promise,resolve};};
const ok=value=>({ok:true,json:async()=>value});
function form(values={}){
  const listeners={},error={textContent:''},button={disabled:false,isConnected:true};
  const result={isConnected:true,editRevision:0,elements:Object.fromEntries(Object.entries(values).map(([key,value])=>[key,{value}])),
    addEventListener(event,listener){(listeners[event]??=[]).push(listener);},
    input(){for(const listener of listeners.input || [])listener();},
    querySelector:selector=>selector==='.form-error'?error:button,querySelectorAll:()=>[],
    closest:()=>null,insertAdjacentHTML(){},scrollIntoView(){},error,button};
  return result;
}
function environment(elements,fetch,extra={}){
  elements['platform-main']??={isConnected:true,insertAdjacentHTML(){},querySelectorAll:()=>[]};
  elements['platform-toast']??={textContent:'',classList:{add(){},remove(){}}};
  const context=vm.createContext({document:{getElementById:id=>elements[id],querySelectorAll:()=>[]},
    location:{href:'https://example.test/platform.html#admin'},URL,URLSearchParams,TextEncoder,Intl,Date,
    setTimeout:()=>0,clearTimeout,confirm:()=>true,fetch,...extra});
  vm.runInContext(source+"\nroute='admin';routeVersion=1;",context);
  return context;
}
function controls(){
  const ids={};
  return {alive:()=>true,next:key=>ids[key]=(ids[key] || 0)+1,
    fresh:(key,id,element)=>ids[key]===id && element.isConnected,refreshDirectory:async()=>{},track:()=>{}};
}
function importHarness(fetch){
  const importForm=form({kind:'teams',csv:'number,name\n1,ORIGINAL'});
  importForm.elements.file={files:[]};
  const elements={'import-form':importForm,'import-template':{},'import-credentials':{innerHTML:'',querySelectorAll:()=>[]},
    'import-commit-error':{textContent:''}};
  let html='INITIAL_PREVIEW';
  const preview={isConnected:true,get innerHTML(){return html;},set innerHTML(value){
    html=value;
    if(elements['commit-import'])elements['commit-import'].isConnected=false;
    elements['commit-import']=value.includes('id="commit-import"')?{isConnected:true,disabled:value.includes('id="commit-import" disabled')}:null;
    elements['discard-import']={isConnected:true};
  }};
  elements['import-preview']=preview;
  const ctrl=controls();
  const context=environment(elements,fetch,{ctrl,ctx:{requests:{}}});
  vm.runInContext("ctrl.track=trackAdminForm;installImportTools(ctx,ctrl)",context);
  return {context,form:importForm,elements,preview};
}
const preview=id=>({previewId:id,counts:{create:1,update:0,unchanged:0,error:0},rows:[],canCommit:true,expiresAt:1});

test('import template links only use fixed team or judge destinations',()=>{
  const h=importHarness(async()=>ok({}));
  for(const [kind,expectedKind,label] of [
    ['teams','teams','下载队伍模板'],
    ['judges','judges','下载评委模板'],
    ['teams&kind=judges','teams','下载队伍模板'],
    ['"><img src=x onerror=alert(1)>','teams','下载队伍模板'],
    ['javascript:alert(1)','teams','下载队伍模板'],
  ]){
    h.form.elements.kind.value=kind;
    h.form.elements.kind.onchange();
    assert.equal(h.elements['import-template'].href,`api/admin/data/template?kind=${expectedKind}`);
    assert.equal(h.elements['import-template'].textContent,label);
  }
});

test('editing CSV during preview keeps the new input and rejects the late preview',async()=>{
  const response=deferred(),requests=[];
  const h=importHarness(async(url,options)=>{requests.push({path:url.pathname,body:JSON.parse(options.body)});return response.promise;});
  h.form.input();h.form.onsubmit({preventDefault(){}});
  h.form.elements.csv.value='number,name\n2,NEW_INPUT';h.form.input();
  response.resolve(ok(preview('OLD_PREVIEW')));await wait();
  assert.equal(h.form.elements.csv.value,'number,name\n2,NEW_INPUT');
  assert.equal(h.preview.innerHTML,'INITIAL_PREVIEW');
  assert.equal(h.elements['commit-import'],undefined);
  assert.equal(vm.runInContext('dirtyAdminForms.size',h.context),1);
  assert.equal(requests[0].path,'/api/admin/data/import/preview');
});

test('a preview is invalidated by CSV edits and commit double-clicks send one batch',async()=>{
  const commit=deferred(),requests=[];
  const h=importHarness(async(url,options)=>{
    requests.push({path:url.pathname,body:JSON.parse(options.body)});
    return url.pathname.endsWith('/preview')?ok(preview('READY')):commit.promise;
  });
  h.form.input();h.form.onsubmit({preventDefault(){}});await wait();
  const oldButton=h.elements['commit-import'];
  assert.equal(oldButton.disabled,false);
  h.form.elements.csv.value='number,name\n2,CHANGED';h.form.input();
  assert.equal(oldButton.disabled,true);
  await oldButton.onclick({currentTarget:oldButton});
  assert.equal(requests.filter(row=>row.path.endsWith('/commit')).length,0);
  h.form.onsubmit({preventDefault(){}});await wait();
  const button=h.elements['commit-import'];
  const first=button.onclick({currentTarget:button});
  const second=button.onclick({currentTarget:button});
  assert.equal(requests.filter(row=>row.path.endsWith('/commit')).length,1);
  assert.deepEqual(requests.at(-1).body,{previewId:'READY'});
  commit.resolve(ok({created:1,updated:0,unchanged:0,issuedCredentials:[]}));
  await Promise.all([first,second]);
  assert.match(h.preview.innerHTML,/导入完成/);
});

test('a successful old commit cannot replace a newer preview or clear its pending changes',async()=>{
  const commit=deferred();let previews=0;
  const h=importHarness(async url=>url.pathname.endsWith('/preview')?ok(preview('P'+(++previews))):commit.promise);
  h.form.input();h.form.onsubmit({preventDefault(){}});await wait();
  const button=h.elements['commit-import'];
  const committing=button.onclick({currentTarget:button});
  h.form.elements.csv.value='number,name\n2,NEWER_BATCH';h.form.input();
  h.form.onsubmit({preventDefault(){}});await wait();
  const newest=h.preview.innerHTML,newButton=h.elements['commit-import'];
  commit.resolve(ok({created:1,updated:0,unchanged:0,issuedCredentials:[]}));await committing;
  assert.equal(h.preview.innerHTML,newest);
  assert.equal(h.elements['commit-import'],newButton);
  assert.equal(newButton.disabled,false);
  assert.equal(h.form.elements.csv.value,'number,name\n2,NEWER_BATCH');
  assert.equal(vm.runInContext('dirtyAdminForms.size',h.context),1);
});

test('adminSaveForm retains same-form edits and reports conflicts without clearing input',async()=>{
  const pending=deferred(),entry=form({reason:'Submitted reason'});
  const context=environment({},()=>pending.promise,{entry});
  vm.runInContext('trackAdminForm(entry);entry.input();',context);
  const saving=vm.runInContext("adminSaveForm(entry,'admin/data/review-validity',{expectedVersion:2},(_result,unchanged)=>{entry.savedUnchanged=unchanged;})",context);
  entry.elements.reason.value='New reason during POST';entry.input();
  pending.resolve(ok({ok:true}));await saving;
  assert.equal(entry.savedUnchanged,false);
  assert.equal(entry.elements.reason.value,'New reason during POST');
  assert.equal(vm.runInContext('dirtyAdminForms.has(entry)',context),true);
  context.fetch=async()=>({ok:false,status:409,json:async()=>({error:'Stale review version'})});
  await vm.runInContext("adminSaveForm(entry,'admin/data/review-validity',{},()=>{})",context);
  assert.match(entry.error.textContent,/Stale review version.*当前输入已保留/);
  assert.equal(entry.elements.reason.value,'New reason during POST');
  assert.equal(vm.runInContext('dirtyAdminForms.has(entry)',context),true);
});

function select(value=''){
  let html='';
  return {value,options:[],get innerHTML(){return html;},set innerHTML(text){
    html=text;this.options=[...text.matchAll(/<option value="([^"]*)"/g)].map(match=>({value:match[1]}));
    this.value=this.options[0]?.value || '';
  }};
}
function recordHarness(fetch){
  const filter=form();
  filter.elements=Object.fromEntries(['stage','runId','teamId','judgeId','track','status'].map(key=>[key,select(key==='stage'?'exhibition':key==='status'?'all':'')]));
  const elements={'record-filter':filter,'record-exports':{innerHTML:''}};
  let html='',validityForms=[];
  const results={isConnected:true,get innerHTML(){return html;},set innerHTML(value){
    html=value;for(const entry of validityForms)entry.isConnected=false;
    validityForms=[...value.matchAll(/<form data-validity="(\d+)"/g)].map(match=>{
      const entry=form({reason:''});entry.dataset={validity:match[1]};return entry;
    });
  },querySelectorAll:selector=>selector==='[data-validity]'?validityForms:[],contains:entry=>validityForms.includes(entry)};
  elements['record-results']=results;
  const ctx={requests:{}},ctrl=controls();
  const context=environment(elements,fetch,{ctx,ctrl});
  vm.runInContext('ctrl.track=trackAdminForm;installRecordTools(ctx,ctrl)',context);
  return {context,filter,elements,results,forms:()=>validityForms,ctx};
}
const reviewResult=(stage='exhibition',rows=[])=>({run:{id:stage+'_RUN',stage,current:true},
  teams:[{id:'T1',number:1,name:'Frozen one',track:'AI for Science'},{id:'T2',number:2,name:'Frozen two',track:'AI for Science'}],
  judges:[{id:'J1',name:'Synthetic judge'}],reviews:rows,coverage:{expected:2,recorded:rows.length,validSubmitted:rows.length,missing:2-rows.length}});

test('late history-filter reads cannot replace a newer selected stage or its export filter',async()=>{
  const old=deferred(),current=deferred();
  const h=recordHarness(async url=>{
    const stage=url.searchParams.get('stage');
    if(url.pathname.endsWith('/runs'))return ok({runs:[{id:stage+'_RUN',stage,current:true}]});
    return stage==='roadshow'?current.promise:old.promise;
  });
  await wait();
  h.filter.elements.stage.value='roadshow';h.filter.input();
  await h.filter.elements.stage.onchange();
  current.resolve(ok(reviewResult('roadshow')));await wait();
  const rendered=h.results.innerHTML,exports=h.elements['record-exports'].innerHTML;
  old.resolve(ok(reviewResult('exhibition')));await wait();
  assert.equal(h.results.innerHTML,rendered);
  assert.equal(h.elements['record-exports'].innerHTML,exports);
  assert.equal(h.ctx.reviewData.run.id,'roadshow_RUN');
  assert.match(exports,/stage=roadshow/);
  assert.ok(!exports.includes('stage=exhibition'));
});

test('saving validity preserves another record reason entered during the POST',async()=>{
  const post=deferred(),requests=[];
  const rows=[1,2].map(number=>({teamId:'T'+number,judgeId:'J1',version:number,submitted:{feedback:'PRIVATE_'+number},invalidated:false}));
  const h=recordHarness(async(url,options)=>{
    requests.push({path:url.pathname,method:options.method,body:options.body?JSON.parse(options.body):null});
    if(options.method==='POST')return post.promise;
    if(url.pathname.endsWith('/runs'))return ok({runs:[{id:'exhibition_RUN',current:true}]});
    return ok(reviewResult('exhibition',rows));
  });
  await wait();
  const [first,second]=h.forms();
  first.elements.reason.value='Reason submitted for first';first.input();
  first.onsubmit({preventDefault(){}});
  second.elements.reason.value='NEW UNSAVED SECOND REASON';second.input();
  const rendered=h.results.innerHTML;
  post.resolve(ok({ok:true}));await wait();
  assert.equal(requests.filter(row=>row.path.endsWith('/reviews')).length,1,'Saving one record must not refresh over another record reason');
  assert.equal(h.results.innerHTML,rendered);
  assert.equal(second.elements.reason.value,'NEW UNSAVED SECOND REASON');
  assert.equal(vm.runInContext('dirtyAdminForms.size',h.context),1);
  const submitted=requests.find(row=>row.method==='POST');
  assert.equal(submitted.path,'/api/admin/data/review-validity');
  assert.deepEqual(submitted.body,{runId:'exhibition_RUN',teamId:'T1',judgeId:'J1',expectedVersion:1,
    invalidated:true,reason:'Reason submitted for first'});
});

function teamHarness(fetch,directoryOverrides={}){
  const entry=form({id:'T1',number:'1',name:'Submitted name',project:'P',track:'AI for Science',
    expectedRevision:'7',status:'active',statusReason:'',booth:'A1',roadshowOrder:'2'});
  entry.elements.track=select('AI for Science');entry.elements.name.focus=()=>{};
  entry.reset=()=>{for(const element of Object.values(entry.elements))element.value='';};
  let tableHTML='',buttons=[];
  const table={isConnected:true,get innerHTML(){return tableHTML;},set innerHTML(value){
    tableHTML=value;buttons=[...value.matchAll(/data-edit-team="([^"]*)"/g)].map(match=>({isConnected:true,dataset:{editTeam:match[1]}}));
  },querySelectorAll:()=>buttons};
  let listHTML='',credentialButtons=[];
  const list={get innerHTML(){return listHTML;},set innerHTML(value){
    listHTML=value;for(const button of credentialButtons)button.isConnected=false;
    credentialButtons=[...value.matchAll(/data-credential="([^"]*)" data-id="([^"]*)" data-role="([^"]*)"/g)].map(match=>({
      isConnected:true,disabled:false,dataset:{credential:match[1],id:match[2],role:match[3]},
      closest:()=>({querySelector:()=>({textContent:'Name '+match[2]})})}));
  },querySelectorAll:()=>credentialButtons};
  const elements={'team-form':entry,'clear-team':{},'invite-form':form({'role':'judge','name':''}),
    'audience-settings':form(),'settings-form':form({minJudges:'5'}),'submission-settings':form(),
    'admin-total-teams':{textContent:'2'},'admin-active-audience':{textContent:'0'},
    'new-code':{innerHTML:'',insertAdjacentHTML(_position,html){this.innerHTML+=html;},querySelectorAll:()=>[]}};
  const root={isConnected:true,insertAdjacentHTML(){},querySelectorAll:()=>[]};elements['platform-main']=root;
  entry.querySelector=selector=>selector==='.form-grid'?{insertAdjacentHTML(){}}:selector==='.form-error'?entry.error:entry.button;
  const eventData={config:{votingOpen:false,votingDeadline:null,writeRevisions:{scoring:0,submissions:0,voting:0}},teams:[
    {id:'T1',number:1,name:'Stored A',track:'AI for Science',revision:7},
    {id:'T2',number:2,name:'Stored B',track:'AI for Science',revision:10}],judges:[],audience:[],trackOptions:['AI for Science'],...directoryOverrides};
  elements['settings-form'].elements.scoringOpen={checked:true};elements['settings-form'].elements.resultsPublished={checked:true};elements['settings-form'].onsubmit=()=>{elements['settings-form'].originalHandlerCalled=true;};
  elements['audience-settings'].elements={votingOpen:{checked:false},votingDeadline:{value:''}};
  const context=environment(elements,fetch,{eventData});
  context.document.querySelector=selector=>selector==='.admin-team-table'?table:list;
  vm.runInContext('installImportTools=()=>{};installRecordTools=()=>{};installAuditTools=()=>{};installAdminData(eventData,1);trackAdminForm(document.getElementById("team-form"));',context);
  return {context,entry,elements,eventData,buttons:()=>buttons,credentialButtons:()=>credentialButtons};
}

test('team editing adopts the POST revision, keeps new input and allows the next save',async()=>{
  const post=deferred(),requests=[];
  let eventData;
  const {context,entry,eventData:directory}=teamHarness(async(url,options)=>{
    requests.push({path:url.pathname,method:options.method,body:options.body?JSON.parse(options.body):null});
    return options.method==='POST'?post.promise:ok(eventData);
  });
  eventData=directory;
  entry.input();entry.onsubmit({preventDefault(){}});
  entry.elements.name.value='NEW NAME DURING POST';entry.input();
  post.resolve(ok({ok:true,team:{id:'T1',number:1,name:'Submitted name',project:'P',track:'AI for Science',revision:8}}));await settle();
  const posts=()=>requests.filter(row=>row.method==='POST');
  assert.equal(posts()[0].path,'/api/admin/teams');
  assert.equal(posts()[0].body.expectedRevision,7);
  assert.equal(posts()[0].body.name,'Submitted name');
  assert.equal(entry.elements.name.value,'NEW NAME DURING POST');
  assert.equal(entry.elements.expectedRevision.value,'8');
  assert.equal(vm.runInContext('dirtyAdminForms.size',context),1);
  assert.equal(entry.button.disabled,false,'first save must release the submit button');
  entry.onsubmit({preventDefault(){}});await settle();
  assert.equal(posts()[1].body.id,'T1');
  assert.equal(posts()[1].body.expectedRevision,8);
  assert.equal(posts()[1].body.name,'NEW NAME DURING POST');
  assert.equal(vm.runInContext('dirtyAdminForms.size',context),0);
});

test('creating a team during continued input adopts the returned ID for the next save',async()=>{
  const post=deferred(),requests=[];
  let eventData;
  const h=teamHarness(async(url,options)=>{
    requests.push({path:url.pathname,method:options.method,body:options.body?JSON.parse(options.body):null});
    return options.method==='POST'?post.promise:ok(eventData);
  });
  eventData=h.eventData;
  const entry=h.entry;
  entry.elements.id.value='';entry.elements.expectedRevision.value='';
  entry.elements.number.value='3';entry.elements.name.value='Submitted create';
  entry.elements.project.value='P';entry.elements.track.value='AI for Science';
  entry.input();entry.onsubmit({preventDefault(){}});
  entry.elements.name.value='NEW NAME DURING CREATE';entry.input();
  const posts=()=>requests.filter(row=>row.method==='POST');
  assert.equal(posts()[0].body.id,undefined);
  post.resolve(ok({ok:true,team:{id:'T3',number:3,name:'Submitted create',project:'P',track:'AI for Science',revision:1}}));await settle();
  assert.equal(entry.elements.id.value,'T3');
  assert.equal(entry.elements.expectedRevision.value,'1');
  assert.equal(entry.elements.name.value,'NEW NAME DURING CREATE');
  assert.equal(vm.runInContext('dirtyAdminForms.size',h.context),1);
  entry.onsubmit({preventDefault(){}});await settle();
  assert.equal(posts()[1].body.id,'T3');
  assert.equal(posts()[1].body.expectedRevision,1);
  assert.equal(posts()[1].body.name,'NEW NAME DURING CREATE');
  assert.equal(vm.runInContext('dirtyAdminForms.size',h.context),0);
});

for(const target of ['another team','cleared form']){
  test(`a late team save response cannot restore the saved identity into ${target}`,async()=>{
    const post=deferred();
    let h;
    h=teamHarness(async(_url,options)=>options.method==='POST'?post.promise:ok(h.eventData));
    h.entry.input();h.entry.onsubmit({preventDefault(){}});
    if(target==='another team')h.buttons().find(button=>button.dataset.editTeam==='T2').onclick();
    else h.elements['clear-team'].onclick();
    const identity={id:h.entry.elements.id.value,revision:h.entry.elements.expectedRevision.value,name:h.entry.elements.name.value};
    post.resolve(ok({ok:true,team:{id:'T1',number:1,name:'Submitted name',project:'P',track:'AI for Science',revision:8}}));await wait();
    assert.deepEqual({id:h.entry.elements.id.value,revision:h.entry.elements.expectedRevision.value,name:h.entry.elements.name.value},identity);
    assert.equal(h.entry.elements.id.value,target==='another team'?'T2':'');
    assert.equal(h.entry.elements.expectedRevision.value,target==='another team'?'10':'');
  });
}

test('concurrent credential rotations retain both one-time codes after the directory replaces old buttons',async()=>{
  const first=deferred();let listing;
  const h=teamHarness(async(url,options)=>{
    if(url.pathname.endsWith('/credentials')){
      const payload=JSON.parse(options.body);
      return payload.id==='J1'?first.promise:ok({ok:true,code:'SYNTHETIC_CODE_TWO'});
    }
    return ok(listing);
  },{judges:[{id:'J1',name:'First judge',active:true},{id:'J2',name:'Second judge',active:true}]});
  listing=h.eventData;
  const firstButton=h.credentialButtons().find(button=>button.dataset.id==='J1' && button.dataset.credential==='rotate');
  const secondButton=h.credentialButtons().find(button=>button.dataset.id==='J2' && button.dataset.credential==='rotate');
  const pending=firstButton.onclick();
  await secondButton.onclick();
  assert.equal(firstButton.isConnected,false,'The second operation really refreshed and replaced the original button');
  assert.match(h.elements['new-code'].innerHTML,/SYNTHETIC_CODE_TWO/);
  first.resolve(ok({ok:true,code:'SYNTHETIC_CODE_ONE'}));await pending;
  assert.match(h.elements['new-code'].innerHTML,/SYNTHETIC_CODE_ONE/);
  assert.match(h.elements['new-code'].innerHTML,/SYNTHETIC_CODE_TWO/);
  assert.match(h.elements['new-code'].innerHTML,/Name J1/);
  assert.match(h.elements['new-code'].innerHTML,/Name J2/);
});

test('directory refresh updates team and active-audience summaries while preserving another form input',async()=>{
  const directory=deferred(),started=deferred();
  const h=teamHarness(async(_url,options)=>{
    if(options.method==='POST')return ok({ok:true});
    started.resolve();return directory.promise;
  });
  h.entry.input();h.entry.onsubmit({preventDefault(){}});
  await started.promise;
  h.elements['invite-form'].elements.name.value='UNSAVED_INVITATION_INPUT';
  vm.runInContext('trackAdminForm(document.getElementById("invite-form"));',h.context);
  h.elements['invite-form'].input();
  directory.resolve(ok({...h.eventData,
    teams:[...h.eventData.teams,{id:'T3',number:3,name:'Newly imported',revision:0}],
    audience:[{id:'A1',active:true},{id:'A2',active:false},{id:'A3',active:true}]}));
  await wait();
  assert.equal(h.elements['admin-total-teams'].textContent,'3');
  assert.equal(h.elements['admin-active-audience'].textContent,'2');
  assert.equal(h.elements['invite-form'].elements.name.value,'UNSAVED_INVITATION_INPUT');
  assert.equal(vm.runInContext('dirtyAdminForms.has(document.getElementById("invite-form"))',h.context),true);
});


test('admin tools retain professional settings and use an independent audience settings form',async()=>{
  const requests=[];
  const h=teamHarness(async(url,options)=>{requests.push({path:url.pathname,body:options.body?JSON.parse(options.body):null});return ok({ok:true,config:{writeRevisions:{scoring:0,submissions:0,voting:1}}});});
  const professional=h.elements['settings-form'],audience=h.elements['audience-settings'];
  assert.equal(professional.elements.minJudges.value,'5');assert.equal(professional.elements.scoringOpen.checked,true);assert.equal(professional.elements.resultsPublished.checked,true);
  professional.onsubmit();assert.equal(professional.originalHandlerCalled,true,'The existing professional submit handler must survive admin extensions');
  audience.elements.votingOpen.checked=true;audience.onsubmit({preventDefault(){}});await wait();
  assert.deepEqual(requests,[{path:'/api/admin/settings',body:{votingOpen:true,votingDeadline:null,expectedRevisions:{voting:0}}}]);
  assert.equal(professional.elements.minJudges.value,'5');assert.equal(professional.elements.scoringOpen.checked,true);
  assert.match(audience.innerHTML,/name="votingOpen"/);assert.match(audience.innerHTML,/name="votingDeadline"/);assert(!audience.innerHTML.includes('name="scoringOpen"'));
});
