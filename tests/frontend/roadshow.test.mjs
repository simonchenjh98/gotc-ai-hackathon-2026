import test from 'node:test';
import assert from 'node:assert/strict';
import {platformFixtureSource} from './source.mjs';
import vm from 'node:vm';

const source=platformFixtureSource();
const deferred=()=>{let resolve;const promise=new Promise(r=>{resolve=r;});return {promise,resolve};};
const flush=()=>new Promise(resolve=>setImmediate(resolve));
const team=id=>({id,number:Number(id.slice(1)),name:id,materialSnapshot:null});
const data=stage=>({stage:{id:stage,name:stage==='roadshow'?'路演评审':'展示评审',runId:stage+'_RUN',dimensions:[]},
  teams:[team('T1'),team('T2')],myReviews:[],window:{canEdit:true,deadline:null}});
const response=value=>({ok:true,json:async()=>value});

function editorHarness(fetch){
  const error={textContent:''},fieldset={disabled:false},feedback={value:'EXHIBITION_UNSAVED'};
  const form={isConnected:true,elements:{feedback},addEventListener(){},querySelectorAll:()=>[],
    querySelector:selector=>selector==='fieldset'?fieldset:error};
  const main={innerHTML:''},draft={},reload={};
  const elements={'platform-main':main,'judge-editor':{innerHTML:''},'review-form':form,'save-review-draft':draft,
    'recuse-review':{},'review-conflict':{innerHTML:''},'reload-review':reload,
    'platform-toast':{textContent:'',classList:{add(){},remove(){}}}};
  const context=vm.createContext({document:{getElementById:id=>elements[id]},location:{href:'https://example.test/platform.html#judge-exhibition'},
    URL,TextEncoder,Intl,Date,setTimeout:()=>0,clearTimeout,confirm:()=>true,fetch});
  vm.runInContext(source+`\nroute='judge-exhibition';routeVersion=1;selectedTeam='T1';judgeData=${JSON.stringify(data('exhibition'))};renderExhibitionJudgeEditor();renderExhibitionTeamPicker=()=>{};renderExhibitionJudgeEditor=()=>{};`,context);
  const switchStage=()=>{
    form.isConnected=false;
    main.innerHTML='ROADSHOW_INPUT_MUST_SURVIVE';
    vm.runInContext(`route='judge-roadshow';routeVersion=2;selectedTeam='T2';reviewDirty=true;judgeData=${JSON.stringify(data('roadshow'))};`,context);
  };
  return {context,form,main,draft,reload,error,switchStage};
}

test('real stage reads keep separate selected teams and explain the published top-twelve roadshow selection',async()=>{
  const urls=[],main={innerHTML:''};
  const context=vm.createContext({document:{getElementById:()=>main},location:{href:'https://example.test/platform.html'},
    URL,TextEncoder,Intl,Date,setTimeout,clearTimeout,
    fetch:async url=>{urls.push(url.pathname);return response(data(url.pathname.includes('roadshow')?'roadshow':'exhibition'));}});
  vm.runInContext(source+"\nrenderExhibitionTeamPicker=()=>{};renderExhibitionJudgeEditor=()=>{};route='judge-roadshow';routeVersion=1;selectedTeams.exhibition='T2';selectedTeams.roadshow='T1';",context);
  await vm.runInContext("renderExhibitionJudge(1)",context);
  assert.match(main.innerHTML,/组织方另行确认的决赛名单优先/);
  assert.match(main.innerHTML,/默认按有效均分从高到低同步最多 12 支队伍/);
  assert.match(main.innerHTML,/同分按队号排序/);
  assert.equal(vm.runInContext('selectedTeam',context),'T1');
  vm.runInContext("route='judge-exhibition';routeVersion=2;",context);
  await vm.runInContext('renderExhibitionJudge(2)',context);
  assert.equal(vm.runInContext('selectedTeam',context),'T2');
  assert.equal(vm.runInContext('selectedTeams.roadshow',context),'T1');
  assert.deepEqual(urls,['/api/judging/roadshow','/api/judging/exhibition']);
});

for(const flow of ['pending POST','successful-save GET','conflict-recovery GET']){
  test(`late exhibition ${flow} cannot overwrite a new roadshow editor`,async()=>{
    const post=deferred(),refresh=deferred(),refreshStarted=deferred(),requests=[];
    const h=editorHarness(async(url,options)=>{
      requests.push({path:url.pathname,method:options.method,body:options.body?JSON.parse(options.body):null});
      if(options.method==='POST'){
        if(flow==='pending POST')return post.promise;
        if(flow==='conflict-recovery GET')return {ok:false,status:409,json:async()=>({error:'Version changed'})};
        return response({ok:true});
      }
      refreshStarted.resolve();return refresh.promise;
    });
    const saving=h.draft.onclick();
    if(flow==='pending POST'){
      h.switchStage();post.resolve(response({ok:true}));await saving;
      assert.equal(requests.length,1,'A stale POST must not start a refresh for the new stage');
    }else{
      if(flow==='conflict-recovery GET'){
        await saving;
        assert.equal(h.error.textContent,'Version changed');
        h.reload.onclick();
      }
      await refreshStarted.promise;
      h.switchStage();refresh.resolve(response(data('exhibition')));
      if(flow==='successful-save GET')await saving;
      else await flush();
      assert.equal(requests[1].path,'/api/judging/exhibition');
    }
    assert.equal(requests[0].path,'/api/judging/exhibition/review');
    assert.equal(requests[0].body.runId,'exhibition_RUN');
    assert.equal(h.main.innerHTML,'ROADSHOW_INPUT_MUST_SURVIVE');
    assert.equal(vm.runInContext('judgeData.stage.id',h.context),'roadshow');
    assert.equal(vm.runInContext('reviewDirty',h.context),true);
    assert.equal(vm.runInContext('selectedTeam',h.context),'T2');
  });
}

test('late admin stage reads cannot replace the selected roadshow management form',async()=>{
  const e=deferred(),r=deferred(),installed=[],container={isConnected:true,innerHTML:''};
  const context=vm.createContext({document:{getElementById:()=>container,querySelectorAll:()=>[]},
    location:{href:'https://example.test/platform.html#admin'},URL,TextEncoder,Intl,Date,setTimeout,clearTimeout,
    fetch:url=>url.pathname.endsWith('roadshow')?r.promise:e.promise,
    install:value=>{installed.push(value.stage.id);container.innerHTML=value.stage.id;}});
  vm.runInContext(source+"\nroute='admin';routeVersion=1;adminEventData={};installJudgingAdmin=(_event,value)=>install(value);",context);
  const old=vm.runInContext('loadAdminJudging(1)',context);
  vm.runInContext("adminStage='roadshow';",context);
  const current=vm.runInContext('loadAdminJudging(1)',context);
  r.resolve(response(data('roadshow')));await current;
  container.innerHTML='ROADSHOW_ADMIN_NEW_INPUT';
  e.resolve(response(data('exhibition')));await old;
  assert.deepEqual(installed,['roadshow']);
  assert.equal(container.innerHTML,'ROADSHOW_ADMIN_NEW_INPUT');
});

test('admin save refresh preserves a stage form edited while its GET was pending',async()=>{
  const pending=deferred(),container={isConnected:true,innerHTML:'BEFORE'};
  const savedForm={isConnected:true,closest:()=>container},newInputForm={isConnected:true,closest:()=>container};
  const context=vm.createContext({document:{getElementById:()=>container,querySelectorAll:()=>[]},
    location:{href:'https://example.test/platform.html#admin'},URL,TextEncoder,Intl,Date,setTimeout,clearTimeout,
    fetch:()=>pending.promise,savedForm,newInputForm});
  vm.runInContext(source+"\nroute='admin';routeVersion=1;adminEventData={};installJudgingAdmin=()=>{document.getElementById('judging-admin').innerHTML='WRONGLY_REPLACED';};",context);
  const read=vm.runInContext('loadAdminJudging(1,savedForm)',context);
  vm.runInContext('dirtyAdminForms.add(newInputForm)',context);
  container.innerHTML='NEW_WINDOW_INPUT';
  pending.resolve(response(data('exhibition')));await read;
  assert.equal(container.innerHTML,'NEW_WINDOW_INPUT');
});

test('manual admin refresh preserves a roadshow input entered while both reads were pending',async()=>{
  const pending=deferred(),bothStarted=deferred(),listeners={},elements={};
  let slow=false,reads=0;
  const main={innerHTML:'',querySelectorAll:()=>[],insertAdjacentHTML(_position,html){this.innerHTML+=html;}};
  const container={isConnected:true};
  const stageForm={isConnected:true,closest:()=>container,addEventListener:(event,listener)=>{listeners[event]=listener;}};
  const eventData={config:{},teams:[],judges:[],audience:[]};
  const stageData={...data('roadshow'),roster:{teamIds:[],judgeIds:[]},reviews:[]};
  const getElement=id=>id==='platform-main'?main:(elements[id]??={isConnected:true});
  const context=vm.createContext({document:{getElementById:getElement,querySelectorAll:()=>[]},
    location:{href:'https://example.test/platform.html#admin'},URL,TextEncoder,Intl,Date,setTimeout,clearTimeout,confirm:()=>true,stageForm,
    fetch:async url=>{
      const value=url.pathname.endsWith('/admin')?eventData:stageData;
      if(!slow)return response(value);
      if(++reads===2)bothStarted.resolve();
      await pending.promise;return response(value);
    }});
  vm.runInContext(source+"\nroute='admin';routeVersion=1;adminStage='roadshow';installJudgingAdmin=()=>trackAdminForm(stageForm);installSubmissionAdmin=()=>{};",context);
  await vm.runInContext('renderAdmin(1)',context);
  slow=true;
  elements['refresh-admin'].onclick({currentTarget:elements['refresh-admin']});
  await bothStarted.promise;
  main.innerHTML='ROADSHOW_NEW_INPUT_AFTER_REFRESH_CLICK';
  listeners.input();
  pending.resolve();await flush();
  assert.equal(main.innerHTML,'ROADSHOW_NEW_INPUT_AFTER_REFRESH_CLICK');
  assert.equal(vm.runInContext('dirtyAdminForms.size',context),1);
  assert.equal(stageForm.editRevision,1);
});

test('editing the same admin stage form during its POST preserves new input and dirty state',async()=>{
  const post=deferred(),started=deferred(),requests=[],elements={};
  const container={isConnected:true,innerHTML:'',querySelectorAll:()=>forms};
  const form=()=>{
    const listeners={},error={textContent:''},button={isConnected:true,disabled:false};
    return {isConnected:true,elements:{},listeners,closest:()=>container,
      addEventListener:(event,listener)=>{listeners[event]=listener;},querySelectorAll:()=>[],
      querySelector:selector=>selector==='.form-error'?error:button};
  };
  const roster=form(),windowForm=form(),reset=form(),forms=[roster,windowForm,reset];
  windowForm.elements={open:{checked:true},deadline:{value:'2026-10-30T10:00'}};
  reset.elements={reason:{value:'Synthetic reason'}};
  Object.assign(elements,{'judging-admin':container,'roster-form':roster,'review-window-form':windowForm,'reset-review-form':reset,
    'platform-toast':{textContent:'',classList:{add(){},remove(){}}}});
  const context=vm.createContext({document:{getElementById:id=>elements[id]??={},querySelectorAll:()=>[]},
    location:{href:'https://example.test/platform.html#admin'},URL,TextEncoder,Intl,Date,setTimeout:()=>0,clearTimeout,confirm:()=>true,
    fetch:async(url,options)=>{requests.push({path:url.pathname,body:JSON.parse(options.body)});started.resolve();return post.promise;}});
  const stageData={...data('roadshow'),roster:{teamIds:[],judgeIds:[]},reviews:[],judges:[]};
  vm.runInContext(source+`\nroute='admin';routeVersion=1;adminStage='roadshow';installJudgingAdmin({teams:[],judges:[]},${JSON.stringify(stageData)});`,context);
  windowForm.listeners.input();
  windowForm.onsubmit({preventDefault(){}});
  await started.promise;
  windowForm.elements.deadline.value='2026-10-30T11:00';
  windowForm.listeners.input();
  container.innerHTML='SAME_FORM_NEW_INPUT';
  post.resolve(response({ok:true}));await flush();
  assert.equal(requests.length,1,'A changed form must not start a refresh that would discard its new input');
  assert.equal(requests[0].path,'/api/admin/judging/roadshow/window');
  assert.equal(requests[0].body.deadline,Date.parse('2026-10-30T10:00:00+08:00'));
  assert.equal(windowForm.elements.deadline.value,'2026-10-30T11:00');
  assert.equal(container.innerHTML,'SAME_FORM_NEW_INPUT');
  assert.equal(vm.runInContext('dirtyAdminForms.size',context),1);
  assert.equal(windowForm.editRevision,2);
});
