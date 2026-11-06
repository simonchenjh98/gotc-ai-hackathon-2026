import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source=fs.readFileSync(new URL('../platform.js',import.meta.url),'utf8').split("window.addEventListener('hashchange'")[0];
const teamSession={role:'team',name:'Team One',loginId:'delegated-one',operator:{adminId:'A',adminName:'Organizer',teamId:'T1',teamName:'Team One'}};
const adminSession={role:'admin',name:'Organizer',loginId:'returned-admin',operator:null};
function fixture(transport){
  const nodes=new Map(),calls=[],events=[];let marker='admin-login',allow=true;
  const node=id=>{if(!nodes.has(id))nodes.set(id,{id,isConnected:true,innerHTML:'',textContent:'',disabled:false,classList:{add(){},remove(){}},addEventListener(){}});return nodes.get(id);};
  const form=node('admin-team-workbench');form.elements={teamId:{value:'T1'}};
  form.querySelector=selector=>node(selector==='.form-error'?'error':'submit');
  const ctx=vm.createContext({document:{getElementById:node},location:{href:'https://example.test/gotc/platform.html#team',hash:'#team'},
    URL,Date,Intl,setTimeout:()=>0,clearTimeout,confirm:()=>allow,
    localStorage:{getItem:()=>marker,setItem:(_key,value)=>{marker=value;events.push(['publish',value]);}},
    fetch:async(url,options)=>{
      const request={path:url.pathname.split('/api/')[1],method:options.method,headers:options.headers,body:options.body&&JSON.parse(options.body)};calls.push(request);
      const result=await transport?.(request);
      const data=result??(request.path==='admin/team-workbench' && request.method==='GET'?{teams:[{id:'T1',number:1,name:'Team One'},{id:'T2',number:2,name:'<script>unsafe</script>'}]}:teamSession);
      return {ok:true,status:200,json:async()=>data};
    }});
  ctx.record=value=>events.push(value);
  vm.runInContext(source+"\nsession={role:'admin',name:'Organizer',loginId:'admin-login'};route='team';routeVersion=1;renderRoute=async()=>record(['render']);",ctx);
  return {ctx,node,form,calls,events,setMarker:value=>{marker=value;},choose:value=>{allow=value;},
    async render(){await vm.runInContext('renderAdminTeamWorkbench(1)',ctx);},
    async submit(){await form.onsubmit({preventDefault(){}});},
    operator(){ctx.owner=teamSession;marker=teamSession.loginId;vm.runInContext('session=owner',ctx);},
    async leave(){await vm.runInContext('returnAdminTeamWorkbench({currentTarget:document.getElementById("switch-team-workbench")})',ctx);}};
}

test('administrator picker escapes team labels and enters only the explicitly selected team',async()=>{
  const f=fixture();await f.render();
  assert.match(f.node('platform-main').innerHTML,/&lt;script&gt;unsafe&lt;\/script&gt;/);
  assert.ok(!f.node('platform-main').innerHTML.includes('<script>unsafe</script>'));
  await f.submit();
  assert.deepEqual(f.calls.map(r=>[r.path,r.method]),[['admin/team-workbench','GET'],['admin/team-workbench','POST'],['session','GET']]);
  assert.deepEqual(f.calls[1].body,{teamId:'T1'});
  assert.equal(f.calls[0].headers['X-GOTC-Login'],'admin-login');
  assert.equal(f.calls[1].headers['X-GOTC-Login'],'admin-login');
  assert.equal(vm.runInContext('session.operator.teamId',f.ctx),'T1');
  assert.deepEqual(f.events.map(e=>Array.from(e)),[['publish','delegated-one'],['render']]);
});

test('unknown picker choices cannot send a switch request',async()=>{
  const f=fixture();await f.render();f.form.elements.teamId.value='OTHER';await f.submit();
  assert.equal(f.calls.length,1);assert.match(f.node('error').textContent,/请选择/);
});

for(const mismatch of ['login','team'])test(`protected session reread refuses ${mismatch} mismatch before changing the visible identity`,async()=>{
  const f=fixture(request=>request.path==='session'?mismatch==='login'?{...teamSession,loginId:'another-login'}:{...teamSession,operator:{...teamSession.operator,teamId:'T2'}}:undefined);
  await f.render();await f.submit();
  assert.equal(vm.runInContext('session.role',f.ctx),'admin');
  assert.deepEqual(f.events,[]);assert.match(f.node('error').textContent,/登录身份已变化/);
});

test('late switch response cannot take over a different browser login',async()=>{
  let resolve;const pending=new Promise(done=>resolve=done);
  const f=fixture(request=>request.method==='POST'?pending:undefined);await f.render();
  const task=f.submit();f.setMarker('other-tab-login');resolve(teamSession);await task;
  assert.equal(f.calls.filter(r=>r.path==='session').length,0);assert.deepEqual(f.events,[]);
});

test('canceling unsaved-input warning leaves delegated workbench and session unchanged',async()=>{
  const f=fixture();f.operator();vm.runInContext('submissionDirty=true',f.ctx);f.choose(false);await f.leave();
  assert.deepEqual(f.calls,[]);assert.equal(vm.runInContext('session.operator.teamId',f.ctx),'T1');
});

test('returning from delegated workbench verifies the new administrator session and clears old editor authority',async()=>{
  const f=fixture(()=>adminSession);f.operator();
  vm.runInContext("currentTeamRecovery={flush(){record(['flush']);},lock(){record(['lock']);}};currentTeamKeys={dispose(){record(['dispose']);}}",f.ctx);
  await f.leave();
  assert.equal(f.calls[0].path,'team/workbench/return');assert.equal(f.calls[0].headers['X-GOTC-Login'],'delegated-one');
  assert.equal(vm.runInContext('session.role',f.ctx),'admin');assert.equal(vm.runInContext('session.operator',f.ctx),null);
  assert.deepEqual(f.events.map(e=>Array.from(e)),[['flush'],['lock'],['dispose'],['publish','returned-admin'],['render']]);
});

test('ordinary team sessions have no administrator return capability or banner',async()=>{
  const f=fixture();vm.runInContext("session={role:'team',name:'Team One',loginId:'ordinary-team'}",f.ctx);f.setMarker('ordinary-team');
  await f.leave();assert.deepEqual(f.calls,[]);
  assert.equal(vm.runInContext("adminTeamWorkbenchNotice({team:{id:'T1',name:'Team One'}})",f.ctx),'');
});
