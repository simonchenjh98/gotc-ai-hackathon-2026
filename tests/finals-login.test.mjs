import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const platform=fs.readFileSync(new URL('../platform.js',import.meta.url),'utf8').split("window.addEventListener('hashchange'")[0];
function loginPage(route,role){
  const main={innerHTML:''},form={elements:{code:{addEventListener(){}},accountId:{}},querySelector:()=>({before(){}})};
  const ctx=vm.createContext({document:{getElementById:id=>id==='platform-main'?main:form,createElement:()=>({})},location:{href:'https://example.test/platform.html'},URL,setTimeout,clearTimeout});
  vm.runInContext(platform+`\nroute=${JSON.stringify(route)};renderLogin(${JSON.stringify(role)});`,ctx);
  return main.innerHTML;
}

test('finals judge route bypasses both login and wrong-role gates for every ordinary cookie identity',async()=>{
  for(const role of [null,'team','judge','audience','admin']){
    const main={innerHTML:''},calls=[];
    const ctx=vm.createContext({document:{getElementById:()=>main,querySelectorAll:()=>[],body:{classList:{remove(){},add(){}}}},location:{href:'https://example.test/platform.html#judge-finals',hash:'#judge-finals'},URL,setTimeout,clearTimeout,calls});
    vm.runInContext(platform+`\nrouteVersion=0;session={role:${JSON.stringify(role)}};account=()=>{};renderFinalsJudge=async()=>calls.push('finals');renderLogin=()=>calls.push('login');renderWrongRole=()=>calls.push('wrong');`,ctx);
    await vm.runInContext('renderRoute()',ctx);assert.deepEqual(calls,['finals']);
  }
});

test('finals admin login names the finals console and published finals results',()=>{
  const html=loginPage('admin-finals','admin');
  assert.match(html,/<h1>决赛管理<br>评分 · 提名 · 颁奖<\/h1>/);assert.match(html,/<h2>决赛管理员登录<\/h2>/);
  assert.match(html,/管理8位决赛评委/);assert.match(html,/公众查看已发布的决赛结果无需登录/);assert.match(html,/href="#board-finals"/);
});

test('legacy numeric and qualitative review logins retain their own descriptions and board links',()=>{
  const professional=loginPage('judge','judge'),exhibition=loginPage('judge-exhibition','judge'),roadshow=loginPage('judge-roadshow','judge'),admin=loginPage('admin','admin');
  assert.match(professional,/专业评分<br>六维 100 分/);assert.match(professional,/href="#board"/);
  assert.match(exhibition,/展示评审记录/);assert.match(exhibition,/暂不计分/);assert.match(exhibition,/href="#board-exhibition"/);
  assert.match(roadshow,/路演评审记录/);assert.match(roadshow,/暂不计分/);assert.match(roadshow,/href="#board-roadshow"/);
  assert.match(admin,/组织方<br>活动管理/);assert.ok(!admin.includes('决赛管理员登录'));
});

test('all three finals navigation labels remain valid Chinese after the operations navigation merge',()=>{
  const html=fs.readFileSync(new URL('../platform.html',import.meta.url),'utf8');
  for(const [route,label] of [['judge-finals','决赛评分与提名'],['board-finals','决赛结果'],['admin-finals','决赛后台']]){
    assert.ok(html.includes(`<a href="#${route}" data-route="${route}">${label}</a>`));
  }
  assert.ok(!html.includes('\uFFFD'));
});
