import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const read=name=>fs.readFileSync(new URL('../'+name,import.meta.url),'utf8');
const mixed=[{currency:'CNY',cost:1,pending:0},{currency:'USD',cost:1,pending:0}];
function fixture(){
  const nodes=new Map();
  const node=()=>({innerHTML:'',isConnected:true,querySelector:()=>({disabled:false}),querySelectorAll:()=>[],scrollIntoView(){}});
  const get=id=>{if(!nodes.has(id))nodes.set(id,node());return nodes.get(id);};
  const ctx=vm.createContext({document:{getElementById:get},location:{href:'https://example.test/platform.html'},URL,
    setTimeout,clearTimeout,localStorage:{getItem:()=>null},mixed});
  vm.runInContext(read('platform.js').split("window.addEventListener('hashchange'")[0]+'\n'+read('platform-api-admin.js')+"\napiCurrency='CNY';",ctx);
  return {ctx,nodes,node,get,run:code=>vm.runInContext(code,ctx)};
}

test('mixed main amount remains separate currencies and ambiguous scalar is never shown',()=>{
  const f=fixture();
  assert.equal(f.run('billingAmount(mixed,2)'), '¥1.00 + $1.00');
  assert.equal(f.run("usd(2,'MIXED')"),'多币种（见调用明细）');
  assert.equal(f.run("usd(3,'legacy')"),'3.00（原计价）');
  assert.equal(f.run('billingAmount([],0)'),'0.00');
});

test('team and per-key headline use recorded billing rather than current upstream currency',async()=>{
  const f=fixture(),box=f.node();
  const data={enabled:true,currency:'CNY',baseUrl:'https://example.test',models:{},maxKeys:5,expiresAt:null,
    usage:{used24h:null,usedTotal:null},billing:mixed,billing24h:mixed,
    keys:[{id:'key',name:'test',masked:'gk-***',requests:2,billing:mixed,billing24h:mixed,used24h:null,usedTotal:null}]};
  const context={box,out:f.node(),valid:()=>true,generation:0,request:async path=>path.endsWith('/logs')?{items:[]}:data};
  await f.ctx.loadTeamKeys(1,context);
  assert.match(box.innerHTML,/<strong>¥1\.00 \+ \$1\.00<\/strong>/);
  assert.match(box.innerHTML,/近24小时 ¥1\.00 \+ \$1\.00 · 累计 ¥1\.00 \+ \$1\.00/);
  assert.doesNotMatch(box.innerHTML,/¥2\.00/);
});

test('legacy call records never acquire a current CNY or USD label',()=>{
  const f=fixture();
  const html=f.ctx.callTable([{at:0,model:'x',status:200,cost:3,currency:'legacy',input:0,output:0,durationMs:0}],false);
  assert.match(html,/3\.00（原计价）/);
  assert.doesNotMatch(html,/[¥$]3\.00/);
  const pending=f.ctx.callTable([{status:502,cost:0,currency:'CNY',billingStatus:'pending'}],false);
  assert.match(pending,/待核价/);
  assert.doesNotMatch(pending,/¥0\.00/);
});

test('stats preserve each recorded currency and mixed groups suppress false amounts',()=>{
  const f=fixture();
  const html=f.ctx.usageTable('models',[],[{cost:2,currency:'MIXED'},{cost:1,currency:'USD'},{cost:3,currency:'legacy'}],()=>[]);
  assert.match(html,/多币种（见调用明细）/);
  assert.match(html,/\$1\.00/);
  assert.match(html,/3\.00（原计价）/);
  assert.doesNotMatch(html,/¥2\.00|¥3\.00/);
});

test('ranking orders CNY and USD independently, without converting or combining them',()=>{
  const f=fixture();
  const team=(teamId,cny,usd)=>({teamId,active:true,inflight:0,billing24h:[{currency:'CNY',cost:cny},{currency:'USD',cost:usd}],billing:[]});
  const html=f.ctx.apiRanking({data:{teams:[team('team-A',1,9),team('team-B',5,2)]},team:id=>id});
  const cny=html.slice(html.indexOf('<h3>CNY</h3>'),html.indexOf('<h3>USD</h3>'));
  const usd=html.slice(html.indexOf('<h3>USD</h3>'));
  assert.ok(cny.indexOf('team-B')<cny.indexOf('team-A'));
  assert.ok(usd.indexOf('team-A')<usd.indexOf('team-B'));
  assert.match(cny,/¥5\.00/);assert.match(usd,/\$9\.00/);
  assert.doesNotMatch(html,/¥10\.00|\$10\.00/);
});

test('admin overview main fee card is split even when the selected upstream is CNY',()=>{
  const f=fixture();
  f.ctx.consoleContext={data:{enabled:true,overview:{cost:null,billing:mixed},teams:[],upstreams:{},models:{}},
    teams:[],box:f.node(),actions:f.node(),request:()=>Promise.resolve({byModel:[]}),alive:()=>false};
  f.run('apiPageRenderers.dashboard(consoleContext)');
  assert.match(f.ctx.consoleContext.box.innerHTML,/<strong>¥1\.00 \+ \$1\.00<\/strong>/);
  assert.doesNotMatch(f.ctx.consoleContext.box.innerHTML,/¥2\.00/);
});

test('Gitee empty fixed prices are described as catalog billing, never free or USD',()=>{
  const f=fixture();
  f.ctx.consoleContext={data:{upstreams:{openai:{url:'https://ai.gitee.com/v1/',models:{'GLM-5':[]}}}},
    box:f.node(),actions:f.node()};
  f.run('apiPageRenderers.accounts(consoleContext)');
  assert.match(f.ctx.consoleContext.box.innerHTML,/人民币 · 目录计价/);
  assert.doesNotMatch(f.ctx.consoleContext.box.innerHTML,/未定价，不计费|默认 \$/);
});
