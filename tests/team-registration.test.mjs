import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const context=vm.createContext({});
vm.runInContext(fs.readFileSync(new URL('../team-registration.js',import.meta.url),'utf8')+'\nthis.ui=RegistrationUI;',context);
const team={id:'fixture',number:7,name:'<img src=x onerror=alert(1)>',captain:'Synthetic Captain',school:'Fixture School',members:['Synthetic Captain','Student A'],targetSize:3,project:'<script>',summary:'"unsafe"',flags:['队员待补充'],recruiting:true};
test('roster escapes submitted names and project text',()=>{
  const html=context.ui.card(team);
  assert.ok(!html.includes('<img'));
  assert.ok(!html.includes('<script>'));
  assert.ok(html.includes('&lt;img'));
  assert.ok(html.includes('队长'));
});
test('roster search finds team members and schools with recruiting filter',()=>{
  assert.equal(context.ui.matches(team,' student a ',false),true);
  assert.equal(context.ui.matches(team,'Fixture School',true),true);
  assert.equal(context.ui.matches(team,'other',false),false);
  assert.equal(context.ui.matches({...team,recruiting:false},'',true),false);
});

test('registration fill preserves same-name captain teammates and card labels only the first captain',()=>{
  const source=fs.readFileSync(new URL('../team-registration.js',import.meta.url),'utf8');
  const elements=Object.fromEntries(['name','captain','school','phone','project','summary','targetSize','member1','member2','member3','member4'].map(name=>[name,{value:''}]));
  const nodes=new Map(),q=selector=>{if(!nodes.has(selector))nodes.set(selector,{});return nodes.get(selector);};
  const saved={...team,captain:'Alex',members:['Alex','Alex','Member'],targetSize:3};
  const scope=vm.createContext({form:{elements},q,editing:null,team:saved});
  const fill=source.slice(source.indexOf('    function fill(team) {'),source.indexOf('    function showReceipt()'));
  vm.runInContext(fill+'\nfill(team);',scope);
  assert.equal(elements.member1.value,'Alex');assert.equal(elements.member2.value,'Member');assert.equal(elements.member3.value,'');
  const html=context.ui.card(saved);assert.equal([...html.matchAll(/ · 队长/g)].length,1);
  assert.equal([...html.matchAll(/class="captain"/g)].length,1);assert.match(html,/>Alex<\/span>/);
});

test('registration success offers an uncredentialed application link and distinguishes management code from workbench login',()=>{
  const source=fs.readFileSync(new URL('../team-registration.js',import.meta.url),'utf8');
  const receipt={hidden:true,innerHTML:''},copy={};
  const scope=vm.createContext({remember:()=>true,receipt,escape:context.ui.escape,editing:{name:'Fixture team'},code:'synthetic-private-code',q:()=>copy});
  const fn=source.slice(source.indexOf('    function showReceipt() {'),source.indexOf('    function lock('));vm.runInContext(fn+'\nshowReceipt();',scope);
  assert.match(receipt.innerHTML,/管理码不能登录选手工作台/);assert.match(receipt.innerHTML,/href="https:\/\/8\.141\.114\.201\/gotc\/platform.html#claim">申请提交码/);
  assert.ok(![...receipt.innerHTML.matchAll(/href="([^"]+)"/g)].some(match=>match[1].includes('synthetic-private-code')));
  for(const path of ['index.html','teams.html']){const html=fs.readFileSync(new URL('../'+path,import.meta.url),'utf8');assert.match(html,/组队登记为选填/);assert.match(html,/可直接申请提交码/);}
});

async function submitRegistration(targetSize,members){
  const source=fs.readFileSync(new URL('../team-registration.js',import.meta.url),'utf8');
  const values={name:'Five-person team',captain:'Captain',school:'Fixture School',phone:'',project:'',summary:'',targetSize:String(targetSize)};
  for(let i=1;i<=4;i++)values['member'+i]=members[i-1]??'';
  const form={elements:Object.fromEntries(Object.entries(values).map(([key,value])=>[key,{value}]))};
  const calls=[],error={textContent:''};
  const scope=vm.createContext({form,error,pending:false,editing:null,code:'synthetic-fixture-code',
    lock:()=>{},remember:()=>true,fill:()=>{},showReceipt:()=>{},refresh:async()=>{},
    request:async(action,body)=>{calls.push({action,body:JSON.parse(JSON.stringify(body))});return {team:{}};}});
  const handler=source.slice(source.indexOf('    form.onsubmit=async e=>{'),source.indexOf("    q('[data-reg-search]')"));
  vm.runInContext(handler,scope);await form.onsubmit({preventDefault(){}});
  return {calls,error:error.textContent,target:form.elements.targetSize.value};
}

test('five-person registration raises the default target instead of blocking four teammates',async()=>{
  const result=await submitRegistration(3,['One','Two','Three','Four']);
  assert.equal(result.error,'');assert.equal(result.calls.length,1);
  assert.equal(result.calls[0].action,'create');assert.equal(result.calls[0].body.team.targetSize,5);
  assert.equal(result.calls[0].body.team.members.length,4);assert.equal(result.target,'5');
});

test('registration retains a larger recruitment target when fewer members are filled',async()=>{
  const result=await submitRegistration(5,['One','Two']);
  assert.equal(result.error,'');assert.equal(result.calls[0].body.team.targetSize,5);
  assert.equal(result.calls[0].body.team.members.length,2);
});
