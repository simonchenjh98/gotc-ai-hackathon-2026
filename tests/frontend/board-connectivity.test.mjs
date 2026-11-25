import assert from 'node:assert/strict';
import test from 'node:test';
import vm from 'node:vm';
import {platformFixtureSource} from './source.mjs';

const source=platformFixtureSource();
const settle=()=>new Promise(resolve=>setImmediate(resolve));

function board({hidden=false,online=true}={}){
  const document=new EventTarget(),window=new EventTarget(),nodes=new Map();
  const timers=new Map(),requests=[];
  let sequence=0,clock=0;
  const classList={add(){},remove(){},toggle(){},contains:()=>false};
  document.hidden=hidden;window.navigator={onLine:online};
  document.getElementById=id=>{
    if(!nodes.has(id))nodes.set(id,Object.assign(new EventTarget(),{innerHTML:'',classList}));
    return nodes.get(id);
  };
  document.querySelectorAll=()=>[];document.body={classList};
  const location=new URL('https://example.test/gotc/platform.html#board-exhibition');
  const ctx=vm.createContext({document,window,location,URL,AbortController,
    setTimeout(fn,delay){const id=++sequence;timers.set(id,{at:clock+delay,fn});return id;},
    clearTimeout:id=>timers.delete(id),
    fetch(url,{signal}){
      return new Promise((resolve,reject)=>{
        requests.push({url,signal,ok:()=>resolve({ok:true,json:async()=>({teams:[],stage:{id:'exhibition'}})})});
        signal.addEventListener('abort',()=>reject(new Error('aborted')));
      });
    }
  });
  vm.runInContext(source,ctx);
  const run=script=>vm.runInContext(script,ctx);
  run("route='board-exhibition';routeVersion=1;renderExhibitionBoard(1)");
  return {requests,content:nodes.get('board-content'),status:nodes.get('live-status'),
    visible(value){document.hidden=!value;document.dispatchEvent(new Event('visibilitychange'));},
    online(value){window.navigator.onLine=value;window.dispatchEvent(new Event(value?'online':'offline'));},
    async leave(){location.hash='#board-finals';await run('renderRoute()');},
    async advance(duration){
      const end=clock+duration;
      for(;;){
        const next=[...timers].sort((a,b)=>a[1].at-b[1].at)[0];
        if(!next || next[1].at>end)break;
        const [id,job]=next;timers.delete(id);clock=job.at;job.fn();await settle();
      }
      clock=end;
    }
  };
}

test('exhibition board makes zero hidden/offline reads, resumes immediately and cleans up on departure',async()=>{
  const b=board({hidden:true});
  await b.advance(60000);assert.equal(b.requests.length,0);
  b.visible(true);await b.advance(0);
  assert.equal(b.requests[0].url.pathname,'/gotc/api/judging/exhibition/board');
  b.requests[0].ok();await settle();
  assert.match(b.status.innerHTML,/已连接/);
  const snapshot=b.content.innerHTML;
  await b.advance(2000);assert.equal(b.requests.length,2);
  b.online(false);assert.equal(b.requests[1].signal.aborted,true);
  await settle();await b.advance(60000);
  assert.equal(b.requests.length,2);assert.equal(b.content.innerHTML,snapshot);assert.match(b.status.innerHTML,/网络已断开/);
  b.online(true);await b.advance(0);assert.equal(b.requests.length,3);
  await b.leave();assert.equal(b.requests[2].signal.aborted,true);
  b.visible(false);b.visible(true);await b.advance(60000);assert.equal(b.requests.length,3);
});

test('a stalled board read expires, backs off, then resumes the normal cadence after success',async()=>{
  const b=board();
  await b.advance(7999);assert.equal(b.requests[0].signal.aborted,false);
  await b.advance(1);assert.equal(b.requests[0].signal.aborted,true);
  assert.match(b.status.innerHTML,/正在重试/);
  await b.advance(3999);assert.equal(b.requests.length,1);
  await b.advance(1);assert.equal(b.requests.length,2);
  b.requests[1].ok();await settle();
  await b.advance(1999);assert.equal(b.requests.length,2);
  await b.advance(1);assert.equal(b.requests.length,3);
  await b.leave();
});
