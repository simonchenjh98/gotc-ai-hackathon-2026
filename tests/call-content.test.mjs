import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source=fs.readFileSync(new URL('../platform.js',import.meta.url),'utf8').split("window.addEventListener('hashchange'")[0];
const ctx=vm.createContext({document:{getElementById:()=>({})},location:{href:'https://example.test/platform.html#admin'},URL});
vm.runInContext(source,ctx);
const sse=events=>events.map(e=>`event: ${e.type || 'x'}\ndata: ${JSON.stringify(e)}\n`).join('\n');

test('Anthropic stream rebuilds text, thinking and tool calls in block order',()=>{
  const raw=sse([{type:'message_start',message:{content:[]}},
    {type:'content_block_start',index:0,content_block:{type:'thinking',thinking:''}},{type:'content_block_delta',index:0,delta:{type:'thinking_delta',thinking:'先读文件'}},
    {type:'content_block_start',index:1,content_block:{type:'text',text:''}},{type:'content_block_delta',index:1,delta:{type:'text_delta',text:'好的，'}},{type:'content_block_delta',index:1,delta:{type:'text_delta',text:'我来看看。'}},
    {type:'content_block_start',index:2,content_block:{type:'tool_use',name:'Read'}},{type:'content_block_delta',index:2,delta:{type:'input_json_delta',partial_json:'{"file_path":'}},{type:'content_block_delta',index:2,delta:{type:'input_json_delta',partial_json:'"a.py"}'}},
    {type:'message_delta',delta:{stop_reason:'tool_use'}},{type:'message_stop'}]);
  assert.equal(ctx.replyOf(raw),'【思考】\n先读文件\n\n好的，我来看看。\n\n【调用工具 Read】\n{"file_path":"a.py"}\n\n— 结束原因：tool_use');
});

test('OpenAI stream joins content, reasoning and split tool-call arguments',()=>{
  const raw=['{"choices":[{"delta":{"reasoning_content":"想一想"}}]}','{"choices":[{"delta":{"content":"你好"}}]}',
    '{"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"name":"search","arguments":"{\\"q\\":"}}]}}]}',
    '{"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"\\"x\\"}"}}]},"finish_reason":"tool_calls"}]}','[DONE]'].map(l=>`data: ${l}\n\n`).join('');
  assert.equal(ctx.replyOf(raw),'【思考】\n想一想\n\n你好\n\n【调用工具 search】\n{"q":"x"}\n\n— 结束原因：tool_calls');
});

test('request context lists system, every turn, tool results and tools',()=>{
  const conv=ctx.conversationOf(JSON.stringify({model:'m',system:[{type:'text',text:'规则'}],tools:[{name:'Read',description:'读文件\n详细'}],
    messages:[{role:'user',content:'问题'},{role:'assistant',content:[{type:'tool_use',name:'Read',input:{p:1}}]},{role:'user',content:[{type:'tool_result',content:[{type:'text',text:'文件内容'}]},{type:'image'}]}]}));
  assert.deepEqual(JSON.parse(JSON.stringify(conv.turns)),[['system','规则'],['user','问题'],['assistant','【调用工具 Read】\n{\n  "p": 1\n}'],['user','【工具结果】\n文件内容\n\n【图片】']]);
  assert.equal(conv.tools[0].name,'Read');
  assert.equal(ctx.conversationOf('not json'),null);
  assert.equal(ctx.replyOf('{"choices":[{"message":{"content":"完成"}}]}'),'完成');
});

test('malformed stored records still open, with raw content intact',()=>{
  const record=(request,response)=>({id:1,headers:{},request,response,requestBytes:request.length,responseBytes:response.length,responseTruncated:false});
  const cases=[
    ['{"model":"m","messages":{"role":"user"}}','{}'],
    ['{"model":"m","messages":[null,"x",{"role":"user","content":"ok"}]}','{}'],
    ['{"model":"m","messages":[],"tools":{"name":"x"}}','{}'],
    ['{"model":"m","messages":[{"role":"assistant","tool_calls":{"x":1}}]}','{}'],
    ['null','{}'],['[1,2]','{}'],['"text"','42'],['not json','garbled'],
    ['{"model":"m","messages":[]}','{"choices":null}'],
    ['{"model":"m","messages":[]}','data: {"choices":[{"delta":{"tool_calls":{"a":1}}}]}\n\n'],
    ['{"model":"m","messages":[]}','data: {"type":"content_block_delta","index":"x","delta":null}\n\n'],
  ];
  for(const [request,response] of cases){
    const html=ctx.callContentMarkup(record(request,response),'/v1/chat/completions');
    assert.match(html,/data-pane="raw"/,request);
    const esc=t=>String(t).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    assert.ok(html.includes(esc(ctx.prettyJSON(request))),'raw request shown: '+request);
    assert.ok(html.includes(esc(response)),'raw response shown: '+response);
  }
  // A crash inside the conversation view degrades to a notice, never hides the raw tab.
  const html=vm.runInContext(`(()=>{const saved=conversationOf;conversationOf=()=>{throw new Error('boom');};try{return callContentMarkup({id:1,headers:{},request:'{"a":1}',response:'',requestBytes:7,responseBytes:0},'/v1/messages');}finally{conversationOf=saved;}})()`,ctx);
  assert.match(html,/无法按对话格式解析/);assert.match(html,/data-pane="raw"/);
  // Cut-off streams are called out in the rebuilt reply.
  assert.match(ctx.replyOf('data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'),/流未正常结束/);
  assert.doesNotMatch(ctx.replyOf('data: {"choices":[{"delta":{"content":"hi"}}]}\n\ndata: [DONE]\n\n'),/流未正常结束/);
  assert.equal(ctx.replyOf('模型不在本活动开放列表中'),'模型不在本活动开放列表中');  // plain-text errors shown as-is
});

test('failure notes show whose fault it was and escape upstream text',()=>{
  const note=ctx.failureNote({errorType:'api_error',errorOwner:'provider',errorPhase:'upstream',upstreamStatus:401,errorMessage:'<img src=x onerror=alert(1)> key ***'});
  assert.match(note,/上游 · api_error · 上游 401/);
  assert.ok(!note.includes('<img'));assert.match(note,/&lt;img/);
  assert.equal(ctx.failureNote({errorType:''}),'');
  const team=ctx.failureNote({errorType:'rate_limit_error',errorOwner:'client'});  // team view: no upstream detail
  assert.match(team,/学生请求 · rate_limit_error/);assert.doesNotMatch(team,/上游 \d/);
});

