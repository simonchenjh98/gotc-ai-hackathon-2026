import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const registration=fs.readFileSync(new URL('../team-registration.js',import.meta.url),'utf8');
const platform=fs.readFileSync(new URL('../platform.js',import.meta.url),'utf8').split("(async()=>{try{session=await api('session');}")[0];
const deferred=()=>{let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve};};
const ok=value=>({ok:true,status:200,json:async()=>value});
const conflict=()=>({ok:false,status:409,json:async()=>({error:'Synthetic profile revision conflict'})});
const model=(overrides={})=>({team:{id:'T1',number:1,name:'Team One',project:'Current project',track:'AI',status:'active'},profile:{name:'Team One',captain:'Captain',school:'School A',phone:'',targetSize:3,members:['Captain','Alex','Alex'],project:'Current project',summary:'Saved summary'},revision:4,teamRevision:7,loginId:'LOGIN1',listedPublicly:false,membersEditable:false,incomplete:false,incompleteReason:'',projectEditable:true,roster:{source:'approved_memberships',members:[{personId:'P1',name:'Captain',school:'School A',source:'approved_membership',role:'captain',schoolState:'recorded'},{personId:'P2',name:'Alex',school:'School B',source:'approved_membership',role:'member',schoolState:'recorded'},{personId:'P3',name:'Alex',school:null,source:'approved_membership',role:'member',schoolState:'missing'}]},...overrides});
function element(){
  const listeners={};return {isConnected:true,disabled:false,hidden:false,value:'',textContent:'',innerHTML:'',classList:{add(){},remove(){},toggle(){}},addEventListener(name,fn){(listeners[name]??=[]).push(fn);},input(){for(const fn of listeners.input || [])fn();}};
}
function fixture(transport=async()=>ok(model())){
  const elements=new Map(),requests=[],removed=[],events={};let marker='LOGIN1',logout=null,allow=true;
  let activePanel=null,rootHTML='';
  const root={...element(),get innerHTML(){return rootHTML;},set innerHTML(value){rootHTML=value;if(activePanel){activePanel.isConnected=false;if(activePanel.form)activePanel.form.isConnected=false;}if(value.includes('id="team-profile-panel"')){activePanel=makePanel();elements.set('team-profile-panel',activePanel);}}};
  function makePanel(){
    let html='';const nodes=new Map(),panel=element();
    Object.defineProperty(panel,'innerHTML',{get:()=>html,set(value){
      html=value;if(panel.form)panel.form.isConnected=false;
      if(value.includes('id="team-profile-form"')){
        const form=element(),button=element(),error=element(),fieldset=element();fieldset.disabled=value.includes('<fieldset disabled>');
        form.elements=Object.fromEntries(['name','captain','school','targetSize','phone','member1','member2','member3','member4','project','summary'].map(key=>[key,element()]));
        form.querySelector=selector=>selector==='.form-error'?error:selector==='fieldset'?fieldset:button;
        form.button=button;form.error=error;form.fieldset=fieldset;panel.form=form;
        nodes.set('#team-profile-form',form);nodes.set('#team-profile-refresh',element());nodes.set('#team-profile-roster',element());nodes.set('[data-profile-message]',element());
      }
    }});
    panel.querySelector=selector=>nodes.get(selector);return panel;
  }
  elements.set('platform-main',root);elements.set('platform-toast',element());elements.set('account',element());
  const context=vm.createContext({document:{getElementById:id=>elements.get(id),querySelectorAll:()=>[],body:{classList:{add(){},remove(){}}}},location:{href:'https://example.test/gotc/platform.html#team-profile',hash:'#team-profile'},URL,URLSearchParams,Intl,Date,TextEncoder,setTimeout:()=>0,clearTimeout,confirm:()=>allow,
    sessionStorage:{removeItem:key=>removed.push(key)},localStorage:{getItem:key=>key.endsWith(':logout')?logout:marker,setItem(){throw new Error('Profile must not publish or persist credentials');}},window:{addEventListener:(name,handler)=>{events[name]=handler;}},
    fetch:async(url,options)=>{const request={path:url.pathname.split('/api/')[1],method:options.method,headers:options.headers,body:options.body?JSON.parse(options.body):undefined};requests.push(request);return transport(request);}});
  vm.runInContext(registration+'\n'+platform+"\nsession={role:'team',loginId:'LOGIN1'};route='team-profile';routeVersion=1;",context);
  return {context,root,requests,removed,events,panel:()=>activePanel,form:()=>activePanel?.form,choose:value=>allow=value,setMarker:value=>marker=value,setLogout:value=>logout=value,
    render:()=>vm.runInContext('renderTeamProfile(1)',context),submit:()=>activePanel.form.onsubmit({preventDefault(){}}),refresh:()=>activePanel.querySelector('#team-profile-refresh').onclick(),
    input(key,value){activePanel.form.elements[key].value=value;activePanel.form.input();},state:expression=>vm.runInContext(expression,context)};
}

test('profile reads with unchanged team login, shows exact same-name members and truthful missing schools',async()=>{
  const f=fixture();await f.render();assert.equal(f.requests.length,1);assert.equal(f.requests[0].path,'team/profile');assert.equal(f.requests[0].headers['X-GOTC-Login'],'LOGIN1');
  assert.deepEqual(f.removed,[]);assert.match(f.panel().innerHTML,/本队查分/);assert.match(f.panel().innerHTML,/#team"/);assert.match(f.panel().innerHTML,/School B/);assert.match(f.panel().innerHTML,/学校待补充（原登记未提供）/);
  assert.equal(f.form().elements.member1.value,'Alex');assert.equal(f.form().elements.member2.value,'Alex');assert.equal(f.form().elements.captain.disabled,true);assert.equal(f.form().elements.targetSize.disabled,true);assert.match(f.panel().innerHTML,/已审核成员变更请联系组织方/);
});
test('verified save sends exact full approved roster and both revisions, ignoring tampered disabled inputs',async()=>{
  const f=fixture(async request=>ok(model(request.method==='POST'?{revision:5,teamRevision:8}:{})));await f.render();f.input('summary','New summary');f.form().elements.captain.value='FORGED';f.form().elements.member1.value='FORGED';await f.submit();
  const post=f.requests.find(r=>r.method==='POST');assert.deepEqual(post.body,{team:{name:'Team One',captain:'Captain',school:'School A',phone:'',project:'Current project',summary:'New summary',targetSize:3,members:['Captain','Alex','Alex']},expectedRevision:4,expectedTeamRevision:7});
  assert.equal(post.headers['X-GOTC-Login'],'LOGIN1');assert.equal(f.state('session.loginId'),'LOGIN1');assert.equal(f.state('currentTeamProfile.dirty'),false);assert(!JSON.stringify(post.body).includes('teamId'));
});
test('unverified profile exposes draft fields and raises target for five members without creating identities',async()=>{
  const draft=model({membersEditable:true,roster:{source:'none',members:[]},revision:0});const f=fixture(async()=>ok(draft));await f.render();
  assert.equal(f.form().elements.captain.disabled,false);assert.match(f.panel().innerHTML,/未审核组队资料/);for(const [i,name] of ['One','Two','Three','Four'].entries())f.input('member'+(i+1),name);await f.submit();
  const post=f.requests.find(r=>r.method==='POST');assert.equal(post.body.expectedRevision,0);assert.equal(post.body.team.targetSize,5);assert.deepEqual(post.body.team.members,['One','Two','Three','Four']);assert.equal(post.path,'team/profile');
});
test('incomplete approved identity stays read-only and does not invent captain or school',async()=>{
  const f=fixture(async()=>ok(model({incomplete:true,incompleteReason:'Missing recorded captain',profile:{...model().profile,captain:'',school:''}})));await f.render();
  assert.equal(f.form().fieldset.disabled,true);assert.equal(f.form().button.disabled,true);assert.equal(f.form().elements.captain.value,'');assert.equal(f.form().elements.school.value,'');assert.match(f.panel().innerHTML,/Missing recorded captain/);await f.submit();assert.equal(f.requests.length,1);
});
test('formally submitted project is locked to the current team title and points to submission workflow',async()=>{
  const f=fixture(async()=>ok(model({projectEditable:false,profile:{...model().profile,project:'OLD REGISTRATION PROJECT'}})));await f.render();assert.equal(f.form().elements.project.disabled,true);assert.equal(f.form().elements.project.value,'Current project');assert.match(f.panel().innerHTML,/作品提交入口/);
  f.form().elements.project.value='FORGED TITLE';await f.submit();assert.equal(f.requests.find(r=>r.method==='POST').body.team.project,'Current project');
});
test('same-form input during POST survives success and the next save uses returned revisions',async()=>{
  const pending=deferred();let posts=0;const f=fixture(async request=>request.method==='POST' && ++posts===1?pending.promise:ok(model({revision:5,teamRevision:8})));await f.render();f.input('summary','SENT');const saving=f.submit();f.input('summary','NEW UNSAVED');pending.resolve(ok(model({revision:6,teamRevision:9})));await saving;
  assert.equal(f.form().elements.summary.value,'NEW UNSAVED');assert.equal(f.state('currentTeamProfile.dirty'),true);assert.match(f.panel().querySelector('[data-profile-message]').textContent,/新输入已保留/);await f.submit();const last=f.requests.at(-1);assert.equal(last.body.expectedRevision,6);assert.equal(last.body.expectedTeamRevision,9);assert.equal(last.body.team.summary,'NEW UNSAVED');
});
test('409 retains input, blocks retries and requires explicit refresh confirmation',async()=>{
  const f=fixture(async request=>request.method==='POST'?conflict():ok(model({revision:5,teamRevision:8})));await f.render();f.input('name','UNSAVED NAME');await f.submit();assert.equal(f.form().elements.name.value,'UNSAVED NAME');assert.equal(f.form().button.disabled,true);assert.match(f.form().error.textContent,/重新读取/);await f.submit();assert.equal(f.requests.filter(r=>r.method==='POST').length,1);
  f.choose(false);await f.refresh();assert.equal(f.requests.filter(r=>r.method==='GET').length,1);assert.equal(f.form().elements.name.value,'UNSAVED NAME');f.choose(true);await f.refresh();assert.equal(f.form().elements.name.value,'Team One');assert.equal(f.form().button.disabled,false);
});
test('new input during an explicit GET refresh is retained and blocks save until reread',async()=>{
  const pending=deferred();let reads=0;const f=fixture(async()=>++reads===1?ok(model()):pending.promise);await f.render();const reading=f.refresh();f.input('name','TYPED DURING READ');pending.resolve(ok(model({revision:10})));await reading;assert.equal(f.form().elements.name.value,'TYPED DURING READ');assert.equal(f.form().button.disabled,true);assert.equal(f.state('currentTeamProfile.dirty'),true);
});
test('profile dirty state participates in navigation confirmation',async()=>{
  const f=fixture();await f.render();f.input('phone','13800000000');f.choose(false);assert.equal(f.state('confirmLeave()'),false);f.choose(true);assert.equal(f.state('confirmLeave()'),true);
});
for(const change of ['route','login','browser owner'])test(`late profile GET cannot display data after changing ${change}`,async()=>{
  const pending=deferred(),f=fixture(()=>pending.promise),loading=f.render();
  if(change==='route')f.state("route='team';routeVersion=2");if(change==='login')f.state("session={role:'team',loginId:'LOGIN2'}");if(change==='browser owner')f.setMarker('LOGIN2');pending.resolve(ok(model()));await loading;assert(!f.panel().innerHTML.includes('School B'));assert.equal(f.form(),undefined);
});
for(const change of ['route','login','browser owner'])test(`late profile POST cannot overwrite the editor after changing ${change}`,async()=>{
  const pending=deferred(),f=fixture(async request=>request.method==='POST'?pending.promise:ok(model()));await f.render();f.input('summary','SENT');const old=f.form(),saving=f.submit();
  if(change==='route')f.state("route='team';routeVersion=2");if(change==='login')f.state("session={role:'team',loginId:'LOGIN2'}");if(change==='browser owner')f.setMarker('LOGIN2');pending.resolve(ok(model({profile:{...model().profile,summary:'LATE RESULT'}})));await saving;assert(!f.panel().innerHTML.includes('LATE RESULT'));assert.equal(old.elements.summary.value,'SENT');
});
test('a switched browser owner immediately hides already rendered private data and blocks old handlers',async()=>{
  const f=fixture();await f.render();const form=f.form();f.setMarker('LOGIN2');f.state('currentTeamProfile.valid()');assert.match(f.panel().innerHTML,/资料已隐藏/);assert(!f.panel().innerHTML.includes('School B'));await form.onsubmit({preventDefault(){}});assert.equal(f.requests.length,1);
});
test('double-click sends one save and API 401 hides profile rather than retaining active editor authority',async()=>{
  const pending=deferred(),f=fixture(async request=>request.method==='POST'?pending.promise:ok(model()));await f.render();const one=f.submit(),two=f.submit();assert.equal(f.requests.filter(r=>r.method==='POST').length,1);pending.resolve({ok:false,status:401,json:async()=>({error:'Expired'})});await Promise.all([one,two]);assert.match(f.panel().innerHTML,/资料已隐藏/);assert.equal(f.state('currentTeamProfile.active'),false);
});
test('wrong loginId or returned team ID fails closed without changing visible account',async()=>{
  for(const field of ['loginId','team']){
    let reads=0;const f=fixture(async()=>ok(++reads===1?model():model(field==='loginId'?{loginId:'LOGIN2'}:{team:{...model().team,id:'T2'}})));await f.render();await f.refresh();assert.match(f.panel().innerHTML,/资料已隐藏/);assert.equal(f.state('session.loginId'),'LOGIN1');
  }
});
test('profile text and roster are escaped and recorded review state is explicit',async()=>{
  const f=fixture(async()=>ok(model({team:{...model().team,name:'<img src=x>'},roster:{members:[{name:'<script>name</script>',school:'<img school>',schoolState:'needs_review'}]}})));await f.render();assert.match(f.panel().innerHTML,/&lt;img/);assert.match(f.panel().innerHTML,/&lt;script&gt;name/);assert.match(f.panel().innerHTML,/学校待核对/);assert(!f.panel().innerHTML.includes('<script>name'));
});
test('administrator and operator profile entry preserves identity and does not request team data',async()=>{
  for(const login of [{role:'admin',loginId:'ADMIN'},{role:'team',loginId:'LOGIN1',operator:{teamId:'T1'}}]){
    const f=fixture();f.context.login=login;f.state('session=login');await f.render();assert.equal(f.requests.length,0);assert.equal(f.state('session.role'),login.role);assert.match(f.root.innerHTML,/组织方后台/);assert(!f.root.innerHTML.includes('team-profile-form'));
  }
});
test('profile route is team-only and an ordinary team keeps existing session across profile, submission and scores',async()=>{
  const f=fixture();await f.render();f.state('renderTeam=async()=>{this.seenTeam=session.loginId};renderTeamScore=async()=>{this.seenScore=session.loginId};');
  f.context.location={href:'https://example.test/gotc/platform.html#team',hash:'#team'};await f.state('renderRoute()');assert.equal(f.context.seenTeam,'LOGIN1');f.context.location.hash='#team-score';await f.state('renderRoute()');assert.equal(f.context.seenScore,'LOGIN1');assert.equal(f.requests.filter(r=>r.path==='login').length,0);
});
test('all profile requests attach marker and a changed browser owner is rejected before fetch',async()=>{
  const f=fixture();await f.state("api('team/profile')");assert.equal(f.requests[0].headers['X-GOTC-Login'],'LOGIN1');f.setMarker('LOGIN2');await assert.rejects(f.state("api('team/profile')"),/登录身份已变化/);assert.equal(f.requests.length,1);
});

test('a delayed logout event cannot let an admitted profile response repaint revoked data',async()=>{
  const pending=deferred(),f=fixture(()=>pending.promise),loading=f.render();f.setLogout('LOGIN1');pending.resolve(ok(model()));await loading;assert.match(f.panel().innerHTML,/资料已隐藏/);assert.equal(f.form(),undefined);
});
test('a profile 403 clears private data and terminates the old controller',async()=>{
  let calls=0;const f=fixture(async()=>++calls===1?ok(model()):{ok:false,status:403,json:async()=>({error:'No longer a team account'})});await f.render();await f.refresh();assert.match(f.panel().innerHTML,/资料已隐藏/);assert.equal(f.state('currentTeamProfile.active'),false);
});
test('a detached old form cannot submit the newly rendered profile',async()=>{
  const f=fixture();await f.render();const old=f.form();await f.refresh();await old.onsubmit({preventDefault(){}});assert.equal(f.requests.filter(r=>r.method==='POST').length,0);
});

test('public-scope notice distinguishes legacy listed registration from a private first profile without a new switch',async()=>{
  for(const listedPublicly of [true,false]){
    const f=fixture(async()=>ok(model({listedPublicly})));await f.render();
    assert.match(f.panel().innerHTML,listedPublicly?/原有公开组队名单中的登记姓名和队长学校仍按原公开范围展示/:/此次保存不会将本队资料新增到公开组队名单/);
    assert(!f.panel().innerHTML.includes('name="listedPublicly"'));await f.submit();assert(!Object.hasOwn(f.requests.find(r=>r.method==='POST').body,'listedPublicly'));assert(!Object.hasOwn(f.requests.find(r=>r.method==='POST').body.team,'listedPublicly'));
  }
});
for(const event of ['storage','focus','pageshow'])test(`already visible profile is hidden on ${event} after browser identity changes`,async()=>{
  const f=fixture();await f.render();f.setMarker('LOGIN2');f.events[event]({persisted:true,key:'unrelated',newValue:'LOGIN2'});assert.match(f.panel().innerHTML,/资料已隐藏/);assert(!f.panel().innerHTML.includes('School B'));
});
test('profile unsaved edits participate in native unload and canceled hash navigation retains the editor',async()=>{
  const f=fixture();await f.render();f.input('summary','UNSAVED');let prevented=false;const event={preventDefault(){prevented=true;}};f.events.beforeunload(event);assert.equal(prevented,true);assert.equal(event.returnValue,'');
  f.choose(false);f.context.location.hash='#team';f.events.hashchange();assert.equal(f.context.location.hash,'team-profile');assert.equal(f.state('route'),'team-profile');assert.equal(f.form().elements.summary.value,'UNSAVED');
});
