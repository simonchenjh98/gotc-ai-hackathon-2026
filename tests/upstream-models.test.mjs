import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const read=file=>fs.readFileSync(new URL('../'+file,import.meta.url),'utf8');
const ctx=vm.createContext({document:{getElementById:()=>null},location:{href:'https://example.test/gotc/platform.html#admin-api',hash:'#admin-api'},URL,localStorage:{getItem:()=>null}});
vm.runInContext(read('platform.js').split("window.addEventListener('hashchange'")[0]+'\n'+read('platform-api-admin.js'),ctx);
const plain=value=>JSON.parse(JSON.stringify(value));

test('price cells allow name-only models and trailing blanks only',()=>{
  assert.deepEqual(plain(ctx.apiPriceRow(['','','','',''],'m')),[]);
  assert.deepEqual(plain(ctx.apiPriceRow(['0.5','2'],'m')),[0.5,2]);
  assert.deepEqual(plain(ctx.apiPriceRow(['3','15','3.75','0.3',''],'m')),[3,15,3.75,0.3]);
  assert.deepEqual(plain(ctx.apiPriceRow(['3','15','3.75','0.3','6'],'m')),[3,15,3.75,0.3,6]);
  for(const bad of [['1'],['','2'],['1','2','3'],['-1','2'],['a','2']])assert.throws(()=>ctx.apiPriceRow(bad,'DeepSeek-V4-Pro'),/DeepSeek-V4-Pro 的单价不正确/);
});

test('account payload saves synced models without prices and keeps the other account untouched',()=>{
  const saved={anthropic:{name:'Claude',url:'https://api.anthropic.com/v1',models:{'claude-x':[3,15]},aliases:{'claude-*':'claude-x'},keyHint:'…abcd'}};
  const body=plain(ctx.apiAccountPayload(saved,'openai',{name:' Gitee ',url:'',key:' sk-new ',models:['DeepSeek-V4-Pro','Qwen3-8B'],
    prices:{'Qwen3-8B':['0.1','0.2','','','']},aliases:[['gpt-*','DeepSeek-V4-Pro'],['','Qwen3-8B']],defaultPrice:['0.5','2']}));
  assert.deepEqual(body.upstreams.openai,{name:'Gitee',url:'https://api.openai.com/v1',key:'sk-new',models:{'DeepSeek-V4-Pro':[],'Qwen3-8B':[0.1,0.2]},aliases:{'gpt-*':'DeepSeek-V4-Pro'},defaultPrice:[0.5,2]});
  // The other account is resent with an empty key (the server keeps its saved key) and no keyHint.
  assert.deepEqual(body.upstreams.anthropic,{name:'Claude',url:'https://api.anthropic.com/v1',key:'',models:{'claude-x':[3,15]},aliases:{'claude-*':'claude-x'}});
  assert.equal(ctx.apiOtherAccounts(saved,'anthropic').anthropic,undefined);
});

test('account payload rejects empty whitelists and mappings to missing models, and keeps saved default cache prices',()=>{
  const draft=extra=>({name:'',url:'https://ai.gitee.com/v1',key:'',models:['a'],prices:{},aliases:[],defaultPrice:['',''],...extra});
  assert.throws(()=>ctx.apiAccountPayload({},'openai',draft({models:[]})),/至少选择一个模型/);
  assert.throws(()=>ctx.apiAccountPayload({},'openai',draft({aliases:[['gpt-*','gone']]})),/不在白名单/);
  assert.equal(plain(ctx.apiAccountPayload({},'openai',draft())).upstreams.openai.defaultPrice,undefined);
  const saved={openai:{url:'https://ai.gitee.com/v1',models:{a:[]},defaultPrice:[1,2,1.5,0.2,3]}};
  assert.deepEqual(plain(ctx.apiAccountPayload(saved,'openai',draft({defaultPrice:['2','4']})).upstreams.openai.defaultPrice),[2,4,1.5,0.2,3]);
});
