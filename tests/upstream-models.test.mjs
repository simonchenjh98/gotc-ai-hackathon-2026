import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source=fs.readFileSync(new URL('../platform.js',import.meta.url),'utf8').split("window.addEventListener('hashchange'")[0];
function fixture(transport){
  const calls=[];
  const node=()=>({value:'',checked:false,disabled:false,hidden:false,isConnected:true,textContent:'',listeners:{},
    classList:{toggle(){},add(){},remove(){}},addEventListener(name,fn){this.listeners[name]=fn;},dispatchEvent(event){this.listeners[event.type]?.(event);},focus(){this.focused=true;},scrollIntoView(){}});
  const form=node();form.elements={};
  const outputs={},pickers={},buttons={};
  for(const kind of ['openai','anthropic']){
    for(const field of ['url','key','models','aliases','on'])form.elements[`${kind}-${field}`]=node();
    outputs[kind]=node();buttons[kind]={...node(),dataset:{upstreamModels:kind}};
    const picker=node();picker.add=node();picker.choices=[];
    Object.defineProperty(picker,'innerHTML',{set(html){picker.html=html;picker.choices=[...html.matchAll(/<input type="checkbox" value="([^"]+)" (disabled)?/g)].map(m=>({...node(),value:m[1],disabled:!!m[2]}));}});
    picker.querySelector=()=>picker.add;picker.querySelectorAll=()=>picker.choices.filter(c=>c.checked);
    pickers[kind]=picker;
  }
  form.querySelector=selector=>selector.includes('data-upstream-result')?outputs[selector.match(/="([^"]+)"/)[1]]:selector.includes('data-upstream-picker')?pickers[selector.match(/="([^"]+)"/)[1]]:node();
  form.querySelectorAll=selector=>selector==='[data-upstream-models]'?Object.values(buttons):[];
  const ctx=vm.createContext({document:{getElementById:id=>id==='upstream-form'?form:{}},location:{href:'https://example.test/gotc/platform.html#admin'},URL,Event,setTimeout,clearTimeout,
    localStorage:{getItem:()=>null},fetch:async(url,options)=>{calls.push({path:url.pathname,body:JSON.parse(options.body),headers:options.headers});return transport();}});
  vm.runInContext(source+"\nroute='admin';routeVersion=3;session={role:'admin',loginId:'fixture-login'};",ctx);
  ctx.box={};vm.runInContext('installUpstreams(box,{},()=>{})',ctx);
  return {ctx,form,outputs,pickers,buttons,calls};
}
const response=models=>({ok:true,status:200,json:async()=>({ok:true,status:200,models,message:'模型列表已读取'})});

test('draft discovery selects models without losing prices or intervening model edits',async()=>{
  const f=fixture(async()=>response(['existing','new-model'])),e=f.form.elements;
  e['openai-url'].value='https://draft.example/v1';e['openai-key'].value='draft-key';e['openai-models'].value='existing 1 2';
  await f.buttons.openai.onclick();
  assert.deepEqual(f.calls[0].body,{kind:'openai',url:'https://draft.example/v1',key:'draft-key'});
  assert.equal(f.calls[0].headers['X-GOTC-Login'],'fixture-login');
  assert.equal(f.pickers.openai.hidden,false);assert.equal(f.pickers.openai.choices[0].disabled,true);
  e['openai-models'].value+='\nmanual 3 4';f.pickers.openai.choices[1].checked=true;f.pickers.openai.add.onclick();
  assert.equal(e['openai-models'].value,'existing 1 2\nmanual 3 4\nnew-model');
  assert.equal(e['openai-on'].checked,true);assert.equal(e['openai-models'].focused,true);
  assert.throws(()=>f.ctx.parseModels(e['openai-models'].value,'openai'),/输入价 输出价/);
  assert.match(f.outputs.openai.textContent,/补全.*价格/);
});

test('changed credentials and detached forms discard late discovery results',async()=>{
  for(const detach of [false,true]){
    let resolve;const pending=new Promise(done=>resolve=done),f=fixture(()=>pending);
    const request=f.buttons.openai.onclick();
    if(detach)f.form.isConnected=false;
    else{f.form.elements['openai-url'].value='https://replacement.example/v1';f.form.elements['openai-url'].dispatchEvent(new Event('input'));}
    resolve(response(['stale-model']));await request;
    assert.equal(f.pickers.openai.hidden,true);assert.equal(f.pickers.openai.html,undefined);
    assert.equal(f.form.elements['openai-models'].value,'');
  }
});

test('model additions deduplicate and enforce the existing configuration size limit',()=>{
  const f=fixture(async()=>response([]));
  assert.equal(f.ctx.appendUpstreamModels('existing 1 2',['existing','new','new']),'existing 1 2\nnew');
  const full=Array.from({length:200},(_,i)=>`model-${i} 1 2`).join('\n');
  assert.throws(()=>f.ctx.appendUpstreamModels(full,['extra']),/200/);
  const html=f.ctx.upstreamMarkup({},true);
  assert.equal((html.match(/data-upstream-models=/g) || []).length,2);
});
