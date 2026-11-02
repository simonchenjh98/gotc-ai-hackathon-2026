import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source=fs.readFileSync(new URL('../platform.js',import.meta.url),'utf8').split("window.addEventListener('hashchange'")[0];
function helpers(screen=false,elapsed=0){
  const context=vm.createContext({document:{getElementById:()=>({}),body:{classList:{contains:()=>screen}}},
    location:{href:'https://example.test/gotc/platform.html#board'},URL,TextEncoder,Intl,Date,setTimeout,clearTimeout});
  vm.runInContext(source+`\nscreenStarted=Date.now()-${elapsed};globalThis.helpers={rulePlaceholders,progressBoardMarkup,reviewState,reviewMaterialMarkup};`,context);
  return context.helpers;
}
const stage={id:'exhibition',name:'展示评审',rulesStatus:'unconfigured',dimensions:[{id:'problem',name:'问题价值',max:null}],
  minValidJudges:null,aggregation:null,advancementQuota:null};
const team=n=>({id:`T${n}`,number:n,name:`TEAM-${n}`,project:'Synthetic project',track:'Test track',reviewCount:3,score:null,rank:null});

test('framework progress board labels missing numeric rules and cannot render an invented score or rank',()=>{
  const html=helpers().progressBoardMarkup({stage,teams:[{...team(2),score:98.76,rank:1},team(1)],window:{canEdit:true,serverTime:1}});
  assert.match(html,/不是成绩榜/);
  assert.match(html,/未计分/);
  assert.match(html,/待配置/);
  assert.ok(!html.includes('98.76'));
  assert.ok(!html.includes('专业均分'));
  assert.ok(html.indexOf('TEAM-1')<html.indexOf('TEAM-2'));
});

test('progress board escapes public team text and rotates eight teams on the screen',()=>{
  const data={stage,teams:Array.from({length:10},(_,i)=>team(i+1)),window:{canEdit:false,serverTime:1}};
  const first=helpers(true).progressBoardMarkup(data);
  const second=helpers(true,13000).progressBoardMarkup(data);
  assert.match(first,/第 1 \/ 2 屏/);
  assert.ok(first.includes('TEAM-8'));
  assert.ok(!first.includes('TEAM-9'));
  assert.match(second,/第 2 \/ 2 屏/);
  assert.ok(second.includes('TEAM-9'));
  assert.ok(!second.includes('TEAM-1<'));
  const malicious=helpers().progressBoardMarkup({...data,teams:[{...team(1),name:'<img src=x onerror=alert(1)>',track:'<script>bad</script>'}]});
  assert.ok(!malicious.includes('<img'));
  assert.ok(!malicious.includes('<script>bad'));
  assert.match(malicious,/&lt;img/);
});

test('review state separates saved drafts, formal records, later drafts and recusal',()=>{
  const h=helpers();
  assert.equal(h.reviewState(null),'待评审');
  assert.equal(h.reviewState({draft:{feedback:''}}),'草稿已保存');
  assert.match(h.reviewState({submitted:{feedback:'Formal'}}),/已正式提交.*未计分/);
  assert.match(h.reviewState({submitted:{feedback:'Formal'},hasUnsubmittedChanges:true}),/有草稿修改/);
  assert.equal(h.reviewState({submitted:{feedback:'Formal'},recused:true}),'已回避');
  assert.equal(h.reviewState({submitted:{feedback:'Formal'},recused:true,invalidated:true}),'已作废');
});

test('material view uses the captured formal snapshot and numeric placeholders remain explicit',()=>{
  const h=helpers();
  const material=h.reviewMaterialMarkup({submissionVersion:2,submittedAt:0,capturedAt:1,
    materials:{projectName:'CAPTURED_FORMAL',summary:'<script>bad</script>'},draft:{projectName:'PRIVATE_DRAFT'}});
  assert.match(material,/CAPTURED_FORMAL/);
  assert.match(material,/提交版本 2/);
  assert.ok(!material.includes('PRIVATE_DRAFT'));
  assert.ok(!material.includes('<script>bad'));
  assert.match(h.reviewMaterialMarkup(null),/没有正式材料快照/);
  const rules=h.rulePlaceholders(stage);
  assert.match(rules,/待配置/);
  assert.match(rules,/尚未实现/);
  assert.ok(!rules.includes('100'));
  assert.ok(!rules.includes('至少 5'));
});

test('switching teams while a saved review refreshes preserves the new editor and its input',async()=>{
  let resolveRefresh,startedRefresh;
  const refreshStarted=new Promise(resolve=>{startedRefresh=resolve;});
  const refreshedData={stage,teams:[team(1),team(2)],myReviews:[],window:{canEdit:true,deadline:null}};
  const feedback={value:'Saved A feedback'},fieldset={disabled:false},error={textContent:''};
  const form={isConnected:true,elements:{feedback},addEventListener(){},querySelector(selector){return selector==='fieldset'?fieldset:error;}};
  const draftButton={},recuseButton={},editor={innerHTML:''},main={innerHTML:''};
  const elements={'platform-main':main,'judge-editor':editor,'review-form':form,'save-review-draft':draftButton,'recuse-review':recuseButton,
    'platform-toast':{textContent:'',classList:{add(){},remove(){}}}};
  const context=vm.createContext({document:{getElementById:id=>elements[id]},location:{href:'https://example.test/gotc/platform.html#judge-exhibition'},
    URL,TextEncoder,Intl,Date,setTimeout:()=>0,clearTimeout,confirm:()=>true,
    fetch:async(_url,options)=>{
      if(options.method==='POST')return {ok:true,json:async()=>({ok:true})};
      startedRefresh();return new Promise(resolve=>{resolveRefresh=()=>resolve({ok:true,json:async()=>refreshedData});});
    }});
  vm.runInContext(source+`\nroute='judge-exhibition';routeVersion=1;selectedTeam='T1';judgeData=${JSON.stringify(refreshedData)};renderExhibitionJudgeEditor();renderExhibitionTeamPicker=()=>{};renderExhibitionJudgeEditor=()=>{};`,context);
  const saving=draftButton.onclick();
  await refreshStarted;
  // A's POST succeeded; its follow-up GET is still pending while B is opened and edited.
  form.isConnected=false;
  main.innerHTML='TEAM_B_UNSAVED_INPUT';
  vm.runInContext("selectedTeam='T2';reviewDirty=true;",context);
  resolveRefresh();await saving;
  assert.equal(main.innerHTML,'TEAM_B_UNSAVED_INPUT');
  assert.equal(vm.runInContext('reviewDirty',context),true);
  assert.equal(vm.runInContext('selectedTeam',context),'T2');
});

test('switching teams while a conflict recovery refreshes preserves the new editor and its input',async()=>{
  let resolveRefresh,startedRefresh;
  const refreshStarted=new Promise(resolve=>{startedRefresh=resolve;});
  const refreshedData={stage,teams:[team(1),team(2)],myReviews:[],window:{canEdit:true,deadline:null}};
  const feedback={value:'Conflicted A feedback'},fieldset={disabled:false},error={textContent:''};
  const form={isConnected:true,elements:{feedback},addEventListener(){},querySelectorAll:()=>[],
    querySelector(selector){return selector==='fieldset'?fieldset:error;}};
  const draftButton={},reloadButton={},editor={innerHTML:''},main={innerHTML:''},conflict={innerHTML:''};
  const elements={'platform-main':main,'judge-editor':editor,'review-form':form,'save-review-draft':draftButton,
    'recuse-review':{},'review-conflict':conflict,'reload-review':reloadButton};
  const context=vm.createContext({document:{getElementById:id=>elements[id]},location:{href:'https://example.test/gotc/platform.html#judge-exhibition'},
    URL,TextEncoder,Intl,Date,setTimeout:()=>0,clearTimeout,confirm:()=>true,
    fetch:async(_url,options)=>{
      if(options.method==='POST')return {ok:false,status:409,json:async()=>({error:'Synthetic version conflict'})};
      startedRefresh();return new Promise(resolve=>{resolveRefresh=()=>resolve({ok:true,json:async()=>refreshedData});});
    }});
  vm.runInContext(source+`\nroute='judge-exhibition';routeVersion=1;selectedTeam='T1';reviewDirty=true;judgeData=${JSON.stringify(refreshedData)};renderExhibitionJudgeEditor();renderExhibitionTeamPicker=()=>{};renderExhibitionJudgeEditor=()=>{};`,context);
  await draftButton.onclick();
  assert.equal(error.textContent,'Synthetic version conflict');
  assert.equal(feedback.readOnly,true);
  assert.equal(typeof reloadButton.onclick,'function');
  reloadButton.onclick();
  await refreshStarted;
  // The real conflict-recovery callback started A's GET before B became the active editor.
  form.isConnected=false;
  main.innerHTML='TEAM_B_UNSAVED_AFTER_CONFLICT';
  vm.runInContext("selectedTeam='T2';reviewDirty=true;",context);
  resolveRefresh();
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(main.innerHTML,'TEAM_B_UNSAVED_AFTER_CONFLICT');
  assert.equal(vm.runInContext('reviewDirty',context),true);
  assert.equal(vm.runInContext('selectedTeam',context),'T2');
});

test('judge rendering excludes invalidated formal records from its count and keeps the reason and original feedback visible',async()=>{
  const main={innerHTML:''},editor={innerHTML:''},picker={innerHTML:''};
  const form={isConnected:true,addEventListener(){}};
  const elements={'platform-main':main,'judge-editor':editor,'team-picker':picker,'review-form':form,
    'save-review-draft':{},'recuse-review':{}};
  const record={teamId:'T1',version:3,invalidated:true,invalidatedReason:'<img src=x> Documented correction',
    submitted:{feedback:'ORIGINAL_FORMAL_RETAINED',submittedAt:1},draft:{feedback:'Original saved input'}};
  const data={stage,teams:[team(1)],myReviews:[record],window:{canEdit:false,deadline:null}};
  const context=vm.createContext({document:{getElementById:id=>elements[id],querySelectorAll:()=>[]},
    location:{href:'https://example.test/platform.html#judge-exhibition'},URL,TextEncoder,Intl,Date,setTimeout,clearTimeout,
    fetch:async()=>({ok:true,json:async()=>data})});
  vm.runInContext(source+"\nroute='judge-exhibition';routeVersion=1;",context);
  await vm.runInContext('renderExhibitionJudge(1)',context);
  assert.match(main.innerHTML,/已正式提交 0 份评审记录/);
  assert.match(editor.innerHTML,/组织方已作废此记录/);
  assert.match(editor.innerHTML,/继续修改仍会保留作废状态/);
  assert.match(editor.innerHTML,/&lt;img src=x&gt; Documented correction/);
  assert.ok(!editor.innerHTML.includes('<img src=x>'));
  assert.match(editor.innerHTML,/ORIGINAL_FORMAL_RETAINED/);
  assert.match(picker.innerHTML,/已作废/);
  assert.ok(!picker.innerHTML.includes('saved-status'));
});

test('review material view distinguishes unfrozen from frozen-empty and includes run/time',()=>{
  const h=helpers();
  const pending=h.reviewMaterialMarkup(null,{runId:'run-unfrozen',frozenAt:null});
  assert.match(pending,/尚未生成材料快照/);assert.ok(!pending.includes('后续提交材料不会自动替换'));
  const empty=h.reviewMaterialMarkup(null,{runId:'run-empty',frozenAt:1000});
  assert.match(empty,/本轮冻结时无正式材料/);assert.match(empty,/run-empty/);assert.match(empty,/1970/);
});
