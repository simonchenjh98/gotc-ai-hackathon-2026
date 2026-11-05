import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source=fs.readFileSync(new URL('../platform-admin.js',import.meta.url),'utf8')+'\n'+fs.readFileSync(new URL('../platform.js',import.meta.url),'utf8').split("window.addEventListener('hashchange'")[0];
const wait=()=>new Promise(resolve=>setImmediate(resolve));
const deferred=()=>{let resolve;const promise=new Promise(done=>{resolve=done;});return {promise,resolve};};
const response=(body,status=200)=>({ok:status<400,status,json:async()=>body});
function node(){return {isConnected:true,innerHTML:'',textContent:'',disabled:false,hidden:false,classList:{add(){},remove(){}}};}
function scoreHarness(fetch,record={teamId:'T1',scores:[10,10,10,10,10,5],feedback:'原评语',status:'draft',revision:4}){
  const inputs=Array.from({length:6},(_,i)=>({...node(),value:String(record?.scores[i] ?? 0),dataset:{score:String(i)}}));
  const sliders=inputs.map((input,i)=>({...node(),value:input.value,dataset:{slider:String(i)}}));
  const elements={'platform-main':node(),'judge-editor':node(),'team-picker':node(),'score-total':node(),'platform-toast':node(),'save-draft':node(),recuse:node(),'score-conflict':node(),'preview-score':node(),'load-score':node(),'score-status':node(),'judge-score-count':node(),'judge-score-window':node()};
  const submit=node(),feedback={...node(),value:record?.feedback ?? ''},error=node(),listeners=[];
  const form={...node(),elements:{feedback,shareFeedback:{checked:true},...Object.fromEntries(inputs.map((input,i)=>[`score${i}`,input]))},
    addEventListener(type,fn){if(type==='input')listeners.push(fn);},input(){listeners.forEach(fn=>fn());},reportValidity:()=>true,
    querySelector(selector){if(selector==='.form-error')return error;if(selector==='[type=submit]')return submit;return sliders[Number(selector.match(/\d+/)?.[0])];},
    querySelectorAll(selector){return selector==='[data-score]'?inputs:selector==='[data-slider]'?sliders:selector==='button'?[submit,elements['save-draft'],elements.recuse]:[...inputs,...sliders,feedback,submit,elements['save-draft'],elements.recuse];}};
  elements['score-form']=form;
  const data={teams:[{id:'T1',number:1,name:'测试队伍'}],myScores:record?[record]:[],dimensions:[20,20,20,15,15,10].map((max,i)=>({max,name:`维度${i+1}`})),scoringOpen:true};
  const ctx=vm.createContext({document:{getElementById:id=>elements[id],querySelectorAll:()=>[]},location:{href:'https://example.test/platform.html#judge'},URL,Date,setTimeout:()=>0,clearTimeout,confirm:()=>true,fetch});
  vm.runInContext(source+`\nroute='judge';routeVersion=1;session={role:'judge'};selectedTeam='T1';judgeData=${JSON.stringify(data)};renderJudgeEditor();`,ctx);
  return {ctx,form,elements,inputs,feedback,error,submit,data};
}

test('numeric POST owns its revision and keeps later input editable and dirty without a follow-up GET',async()=>{
  const gate=deferred(),requests=[];
  const h=scoreHarness(async(url,options)=>{requests.push({path:url.pathname,body:JSON.parse(options.body)});return gate.promise;});
  h.form.input();const saving=h.elements['save-draft'].onclick({currentTarget:h.elements['save-draft']});await wait();
  assert.equal(requests[0].body.expectedRevision,4);
  assert.equal(h.feedback.disabled,false,'Feedback stays editable during save');
  h.feedback.value='保存中继续输入';h.form.input();
  gate.resolve(response({ok:true,status:'draft',score:{...h.data.myScores[0],revision:5}}));await saving;
  assert.equal(requests.length,1);assert.equal(h.feedback.value,'保存中继续输入');
  assert.equal(vm.runInContext('scoreDirty',h.ctx),true);
  const next=h.elements['save-draft'].onclick({currentTarget:h.elements['save-draft']});await next;
  assert.equal(requests[1].body.expectedRevision,5);
});

test('numeric conflict keeps the entire form and blocks retries until latest is explicitly loaded',async()=>{
  const requests=[];const latest={teamId:'T1',scores:[20,20,20,15,15,10],feedback:'另一页已保存',status:'submitted',revision:5};
  let h;h=scoreHarness(async(url,options)=>{requests.push({path:url.pathname,method:options.method});return options.method==='POST'?response({error:'冲突',code:'score_revision_conflict'},409):response({...h.data,scoringOpen:false,myScores:[latest]});});
  h.feedback.value='本页未保存评语';h.form.input();await h.elements['save-draft'].onclick({currentTarget:h.elements['save-draft']});
  assert.equal(h.feedback.value,'本页未保存评语');assert.equal(h.inputs[0].value,'10');assert.equal(vm.runInContext('scoreDirty',h.ctx),true);
  assert.match(h.elements['score-conflict'].innerHTML,/当前输入已保留/);
  await h.elements['save-draft'].onclick({currentTarget:h.elements['save-draft']});assert.equal(requests.length,1);
  await h.elements['preview-score'].onclick();assert.equal(requests.length,2);assert.equal(h.feedback.value,'本页未保存评语');
  assert.match(h.elements['score-conflict'].innerHTML,/另一页已保存/);
  await h.elements['load-score'].onclick();
  assert.equal(vm.runInContext('judgeData.myScores[0].revision',h.ctx),5);
  assert.equal(h.elements['judge-score-count'].textContent,'1');
  assert.match(h.elements['judge-score-window'].textContent,/已截止/);
  assert.equal(vm.runInContext('scoreDirty',h.ctx),false);
});

test('new score starts at zero and submit, recuse, and re-score use authoritative POST versions',async()=>{
  const requests=[];const h=scoreHarness(async(_url,options)=>{const payload=JSON.parse(options.body);requests.push(payload);return response({ok:true,status:payload.status,score:{...payload,revision:requests.length}});},null);
  await h.form.onsubmit({preventDefault(){}});await wait();
  await h.elements.recuse.onclick({currentTarget:h.elements.recuse});await wait();
  await h.form.onsubmit({preventDefault(){}});await wait();
  assert.deepEqual(requests.map(row=>row.expectedRevision),[0,1,2]);
  assert.deepEqual(requests.map(row=>row.status),['submitted','recused','submitted']);
});

function settingsHarness(fetch,group='submissions'){
  const fields={scoring:{scoringOpen:{checked:true},resultsPublished:{checked:false},minJudges:{value:'5'}},submissions:{submissionsOpen:{checked:true},submissionDeadline:{value:''}},voting:{votingOpen:{checked:true},votingDeadline:{value:''}}};
  const error=node(),button=node(),panel=node(),preview=node(),load=node(),listeners=[];
  const form={...node(),id:`${group}-test`,elements:fields[group],editRevision:0,
    addEventListener(type,fn){if(type==='input')listeners.push(fn);},input(){listeners.forEach(fn=>fn());},
    querySelector(selector){return selector==='.form-error'?error:selector==='[data-settings-conflict]'?panel:selector==='[data-settings-preview]'?preview:selector==='[data-settings-load]'?load:button;},insertAdjacentHTML(){}};
  const elements={'platform-main':node(),'platform-toast':node()};
  const ctx=vm.createContext({document:{getElementById:id=>elements[id]},location:{href:'https://example.test/platform.html#admin'},URL,Date,setTimeout:()=>0,clearTimeout,confirm:()=>true,fetch});
  vm.runInContext(source+"\nroute='admin';routeVersion=1;session={role:'admin'};",ctx);
  ctx.form=form;ctx.config={writeRevisions:{scoring:3,submissions:8,voting:2}};
  vm.runInContext(`trackAdminForm(form);installSettingsForm(form,${JSON.stringify(group)},config);`,ctx);
  return {ctx,form,error,button,panel,preview,load,elements};
}

test('every settings entry sends only its group revision and keeps edits made during POST',async()=>{
  for(const [group,revision,field] of [['scoring',3,'minJudges'],['submissions',8,'submissionDeadline'],['voting',2,'votingDeadline']]){
    const gate=deferred(),requests=[];
    const h=settingsHarness(async(_url,options)=>{requests.push(JSON.parse(options.body));return gate.promise;},group);
    h.form.input();const saving=h.form.onsubmit({preventDefault(){}});await wait();
    assert.deepEqual(requests[0].expectedRevisions,{[group]:revision});
    h.form.elements[field].value=field==='minJudges'?'7':'2026-11-09T12:00';h.form.input();
    gate.resolve(response({ok:true,config:{writeRevisions:{scoring:4,submissions:9,voting:3}}}));await saving;
    assert.equal(vm.runInContext('dirtyAdminForms.has(form)',h.ctx),true);
    assert.equal(h.form.elements[field].value,field==='minJudges'?'7':'2026-11-09T12:00');
    await h.form.onsubmit({preventDefault(){}});
    assert.deepEqual(requests[1].expectedRevisions,{[group]:revision+1});
  }
});

test('settings conflict preserves draft, previews server values, replaces only the selected group after confirmation',async()=>{
  const requests=[];const other={isConnected:true};
  const h=settingsHarness(async(_url,options)=>{requests.push(options.method);return options.method==='POST'?response({error:'冲突',code:'settings_revision_conflict'},409):response({config:{submissionsOpen:false,submissionDeadline:null,writeRevisions:{scoring:20,submissions:9,voting:20}}});});
  h.ctx.other=other;vm.runInContext('dirtyAdminForms.add(other)',h.ctx);h.form.input();
  await h.form.onsubmit({preventDefault(){}});
  assert.equal(h.form.elements.submissionsOpen.checked,true);assert.match(h.panel.innerHTML,/当前输入已保留/);
  await h.form.onsubmit({preventDefault(){}});assert.equal(requests.length,1);
  await h.preview.onclick();assert.equal(h.form.elements.submissionsOpen.checked,true);
  await h.load.onclick();assert.equal(h.form.elements.submissionsOpen.checked,false);
  assert.equal(vm.runInContext('dirtyAdminForms.has(other)',h.ctx),true);
  assert.equal(vm.runInContext('dirtyAdminForms.has(form)',h.ctx),false);
});

test('late score and settings replies from a replaced auth session cannot change the old view',async()=>{
  for(const kind of ['score','settings']){
    const gate=deferred();const h=kind==='score'?scoreHarness(()=>gate.promise):settingsHarness(()=>gate.promise);
    const saving=kind==='score'?h.elements['save-draft'].onclick({currentTarget:h.elements['save-draft']}):h.form.onsubmit({preventDefault(){}});
    await wait();vm.runInContext("session={role:'admin',name:'NEW_LOGIN'}",h.ctx);
    gate.resolve(response(kind==='score'?{ok:true,score:{revision:99}}:{ok:true,config:{writeRevisions:{submissions:99}}}));await saving;
    assert.equal(h.elements['platform-toast'].textContent,'');
    assert.equal(h.form.isConnected,true);
  }
});

for(const kind of ['score','settings']){
  test(`${kind} same-role login replacement revokes late 409 and latest-read handlers without waiting for storage events`,async()=>{
    const gate=deferred();let marker='login-a',requests=0;
    const h=kind==='score'?scoreHarness(async()=>{requests++;return gate.promise;}):settingsHarness(async()=>{requests++;return gate.promise;});
    h.ctx.localStorage={getItem:()=>marker};vm.runInContext("session.loginId='login-a'",h.ctx);
    const saving=kind==='score'?h.elements['save-draft'].onclick():h.form.onsubmit({preventDefault(){}});
    marker='login-b';gate.resolve(response({error:'late conflict',code:kind==='score'?'score_revision_conflict':'settings_revision_conflict'},409));await saving;
    assert.equal(h.error.textContent,'');
    assert.equal(kind==='score'?h.elements['score-conflict'].innerHTML:h.panel.innerHTML,'');
    assert.equal(kind==='score'?h.elements['save-draft'].disabled:h.button.disabled,true);
    await (kind==='score'?h.elements['save-draft'].onclick():h.form.onsubmit({preventDefault(){}}));assert.equal(requests,1);
  });

  test(`${kind} preview reads and already-rendered load buttons cannot adopt a baseline after authorization changes`,async()=>{
    const gate=deferred();let marker='login-a';
    const record={teamId:'T1',scores:[20,20,20,15,15,10],feedback:'LATEST',status:'submitted',revision:9};
    const config={submissionsOpen:false,submissionDeadline:null,writeRevisions:{submissions:9}};
    let reads=0,h;
    const fetch=async(_url,options)=>options.method==='POST'?response({error:'conflict',code:kind==='score'?'score_revision_conflict':'settings_revision_conflict'},409):++reads===1?gate.promise:response(kind==='score'?{...h.data,myScores:[record]}:{config});
    h=kind==='score'?scoreHarness(fetch):settingsHarness(fetch);
    h.ctx.localStorage={getItem:()=>marker};vm.runInContext("session.loginId='login-a'",h.ctx);
    await (kind==='score'?h.elements['save-draft'].onclick():h.form.onsubmit({preventDefault(){}}));
    const panel=kind==='score'?h.elements['score-conflict']:h.panel;
    const initial=panel.innerHTML;
    const preview=kind==='score'?h.elements['preview-score']:h.preview;
    const reading=preview.onclick();marker='login-b';gate.resolve(response(kind==='score'?{...h.data,myScores:[record]}:{config}));await reading;
    assert.equal(panel.innerHTML,initial,'Late latest-read response cannot paint a new preview');
    marker='login-a';await preview.onclick();
    const load=kind==='score'?h.elements['load-score']:h.load;
    marker='login-b';await load.onclick();
    if(kind==='score')assert.equal(vm.runInContext('judgeData.myScores[0].revision',h.ctx),4);
    else assert.equal(h.form.elements.submissionsOpen.checked,true);
  });
}

test('settings do not adopt newer unrelated revisions from directory data or another group POST',async()=>{
  const sent=[];const h=settingsHarness(async(_url,options)=>{sent.push(JSON.parse(options.body));return response({ok:true,config:{submissionsOpen:true,writeRevisions:{submissions:9,scoring:99,voting:99}}});});
  vm.runInContext("config.writeRevisions.submissions=88;adminEventData={config:{writeRevisions:{submissions:77}}}",h.ctx);
  await h.form.onsubmit({preventDefault(){}});await h.form.onsubmit({preventDefault(){}});
  assert.deepEqual(sent.map(row=>row.expectedRevisions),[{submissions:8},{submissions:9}]);
});

test('private score/settings requests send their captured public login identity and preserve 428 input without retry',async()=>{
  for(const kind of ['score','settings']){
    const requests=[];const fetch=async(_url,options)=>{requests.push(options);return response({error:'upgrade',code:'revision_required'},428);};
    const h=kind==='score'?scoreHarness(fetch):settingsHarness(fetch);
    vm.runInContext("session.loginId='login-a'",h.ctx);h.form.input();
    await (kind==='score'?h.elements['save-draft'].onclick():h.form.onsubmit({preventDefault(){}}));
    assert.equal(requests[0].headers['X-GOTC-Login'],'login-a');assert.match(h.error.textContent,/刷新页面升级/);
    await (kind==='score'?h.elements['save-draft'].onclick():h.form.onsubmit({preventDefault(){}}));assert.equal(requests.length,1);
    assert.equal(kind==='score'?h.feedback.value:h.form.elements.submissionsOpen.checked,kind==='score'?'原评语':true);
  }
});

test('closed windows, forbidden draft downgrade and exhausted revisions keep input without pretending a newer version exists',async()=>{
  for(const [kind,code,message] of [['score',undefined,'评分尚未开放或已关闭'],['score',undefined,'已有正式评分，请修改后重新提交'],['score','revision_exhausted','编辑版本已达上限'],['settings','revision_exhausted','编辑版本已达上限']]){
    const fetch=async()=>response({error:message,code},409);
    const h=kind==='score'?scoreHarness(fetch):settingsHarness(fetch);
    await (kind==='score'?h.elements['save-draft'].onclick():h.form.onsubmit({preventDefault(){}}));
    assert.match(h.error.textContent,new RegExp(message));assert.match(h.error.textContent,/当前输入已保留/);
    assert.equal(kind==='score'?h.elements['score-conflict'].innerHTML:h.panel.innerHTML,'');
    assert.equal(kind==='score'?h.elements['save-draft'].disabled:h.button.disabled,true);
  }
});

test('unchanged successful score uses server-normalized fields and recusal clears stored numeric values',async()=>{
  let status='submitted';
  const h=scoreHarness(async()=>response({ok:true,score:{teamId:'T1',scores:status==='recused'?[]:[1,2,3,4,5,6],feedback:'规范后的评语',status,revision:status==='recused'?6:5}}));
  h.feedback.value='  规范前的评语  ';
  await h.form.onsubmit({preventDefault(){}});
  assert.deepEqual(h.inputs.map(input=>input.value),['1','2','3','4','5','6']);
  assert.equal(h.feedback.value,'规范后的评语');assert.match(h.elements['score-total'].innerHTML,/21\.0/);
  status='recused';await h.elements.recuse.onclick();
  assert.deepEqual(h.inputs.map(input=>input.value),['0','0','0','0','0','0']);
  assert.match(h.elements['score-total'].innerHTML,/0\.0/);assert.equal(vm.runInContext('scoreDirty',h.ctx),false);
});

for(const kind of ['score','settings']){
  const save=h=>kind==='score'?h.elements['save-draft'].onclick():h.form.onsubmit({preventDefault(){}});
  const harness=fetch=>kind==='score'?scoreHarness(fetch):settingsHarness(fetch);

  test(kind+' keeps unsaved input and gives recovery instructions when its session expires',async()=>{
    let requests=0;
    const h=harness(async()=>{requests++;return response({error:'需要登录'},401);});
    const input=kind==='score'?h.feedback:h.form.elements.submissionDeadline;
    input.value=kind==='score'?'本页评语':'2030-01-02T08:15';h.form.input();
    const original=input.value;
    await save(h);
    assert.equal(input.value,original);assert.match(h.error.textContent,/复制.*刷新.*登录/);
    await save(h);assert.equal(requests,1);
  });

  test(kind+' ignores a late 401 after a new login takes over the view',async()=>{
    const gate=deferred(),h=harness(()=>gate.promise);
    const saving=save(h);await wait();
    vm.runInContext("session={role:'admin',loginId:'replacement'}",h.ctx);
    gate.resolve(response({error:'expired'},401));await saving;
    assert.equal(vm.runInContext('session.loginId',h.ctx),'replacement');
    assert.equal(h.error.textContent,'');
  });
}

test('a manual admin refresh cannot replace a pending or already committed settings save with its old snapshot',async()=>{
  for(const finishSaveFirst of [false,true]){
    const read=deferred(),write=deferred();
    const h=settingsHarness(async(url,options)=>{
      if(options.method==='POST')return write.promise;
      if(url.pathname.endsWith('/admin'))return read.promise;
      return response({roster:{judgeIds:[]},reviews:[]});
    });
    const main=h.elements['platform-main'];
    main.querySelectorAll=()=>[h.form];
    Object.defineProperty(main,'innerHTML',{set(){assert.fail('A stale settings snapshot replaced the active admin forms');}});
    const refresh=vm.runInContext('renderAdmin(1,form)',h.ctx);
    const saving=h.form.onsubmit({preventDefault(){}});
    const reply=()=>write.resolve(response({ok:true,config:{writeRevisions:{submissions:9}}}));
    if(finishSaveFirst){reply();await saving;}
    read.resolve(response({teams:[],judges:[],audience:[],config:{writeRevisions:{submissions:8}}}));
    try{await refresh;}finally{reply();await saving;}
  }
});

test('nonblank professional feedback requires fresh informed confirmation on each formal submit',async()=>{
  const requests=[];
  const h=scoreHarness(async(_url,options)=>{
    const body=JSON.parse(options.body);requests.push(body);
    return response({ok:true,score:{teamId:'T1',scores:body.scores,feedback:body.feedback,status:body.status,revision:body.expectedRevision+1}});
  });
  h.form.elements.shareFeedback.checked=false;
  await h.form.onsubmit({preventDefault(){}});
  assert.equal(requests.length,0);assert.match(h.error.textContent,/确认.*匿名分享/);
  assert.match(h.elements['judge-editor'].innerHTML,/请勿署名或填写联系方式/);
  h.form.elements.shareFeedback.checked=true;
  await h.form.onsubmit({preventDefault(){}});
  assert.equal(requests[0].feedbackSharing,'team-anonymous-v1');
  assert.equal(h.form.elements.shareFeedback.checked,false);
  await h.form.onsubmit({preventDefault(){}});
  assert.equal(requests.length,1,'A later formal submit needs renewed confirmation');
});

test('formal submission consumes sharing confirmation while delayed feedback edits survive',async()=>{
  const gate=deferred(),requests=[];
  const h=scoreHarness(async(_url,options)=>{
    const body=JSON.parse(options.body);requests.push(body);
    return requests.length===1?gate.promise:response({ok:true,score:{teamId:'T1',scores:body.scores,
      feedback:body.feedback,status:'submitted',revision:body.expectedRevision+1}});
  });
  const saving=h.form.onsubmit({preventDefault(){}});
  await wait();
  assert.equal(requests[0].feedbackSharing,'team-anonymous-v1');
  assert.equal(h.form.elements.shareFeedback.checked,false,'Confirmation is consumed at dispatch');
  h.feedback.value='Edited while the formal score was pending';h.form.input();
  gate.resolve(response({ok:true,score:{teamId:'T1',scores:requests[0].scores,
    feedback:requests[0].feedback,status:'submitted',revision:5}}));
  await saving;
  assert.equal(h.feedback.value,'Edited while the formal score was pending');
  assert.equal(vm.runInContext('scoreDirty',h.ctx),true);
  assert.equal(h.form.elements.shareFeedback.checked,false);
  await h.form.onsubmit({preventDefault(){}});
  assert.equal(requests.length,1,'The next revision cannot reuse the previous confirmation');
  assert.match(h.error.textContent,/确认.*匿名分享/);
  h.form.elements.shareFeedback.checked=true;
  await h.form.onsubmit({preventDefault(){}});
  assert.equal(requests.length,2);
  assert.equal(requests[1].expectedRevision,5);
  assert.equal(requests[1].feedback,'Edited while the formal score was pending');
  assert.equal(requests[1].feedbackSharing,'team-anonymous-v1');
});
