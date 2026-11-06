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
