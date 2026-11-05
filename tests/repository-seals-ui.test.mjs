import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source=fs.readFileSync(new URL('../platform.js',import.meta.url),'utf8').split("window.addEventListener('hashchange'")[0];
const sha='a'.repeat(40),codeUrl='https://gitee.com/synthetic/project';
const materials={codeUrl,codeVersion:sha,summary:'IMMUTABLE MATERIAL'};
const seal={id:'synthetic-seal',ref:'seal-'+sha,sha,sealedAt:1000,receivedAt:2000,viewUrl:codeUrl+'/commit/'+sha};
const receipt={requestId:'synthetic-request-1234',status:'succeeded',action:'submitted',baseVersion:0,version:1,historyVersion:1,receivedAt:2000,savedAt:3000,materials,repositorySeal:seal};
const backup=(status='ready',more={})=>({status,sha,attempts:1,error:null,sourceZipSha256:'b'.repeat(64),bundleSha256:'c'.repeat(64),missingAssets:[],downloadUrl:'/gotc/api/submission/backups/T1/1/source.zip',bundleUrl:'/gotc/api/submission/backups/T1/1/repo.bundle',...more});
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};};
function fixture(href='https://example.test/gotc/platform.html#team'){
  const nodes=new Map(),calls=[];
  function node(id){if(nodes.has(id))return nodes.get(id);const n={id,isConnected:true,disabled:false,value:'',dataset:{},textContent:'',classList:{add(){},remove(){}},querySelectorAll(){return [];},querySelector(){return null;},scrollIntoView(){}};let html='';Object.defineProperty(n,'innerHTML',{get:()=>html,set(value){html=value;for(const match of value.matchAll(/id="([^"]+)"/g))node(match[1]);}});nodes.set(id,n);return n;}
  const context=vm.createContext({document:{getElementById:node},location:{href},URL,TextEncoder,Date,Intl,setTimeout,clearTimeout});
  context.ownerMarker='synthetic-login';context.localStorage={getItem:()=>context.ownerMarker};
  context.transport=async(path,body,options)=>{calls.push({path,body,options});return context.respond(path);};
  vm.runInContext(source+`\napi=transport;session={role:'team',loginId:'synthetic-login'};route='team';routeVersion=1;globalThis.h={materialMarkup,requestReceiptMarkup,requestReceiptText,submissionReceiptMarkup,installSubmissionHistory,backup:(...args)=>repositoryBackupMarkup(...args),proof:(...args)=>repositoryVersionMarkup(...args),installBackup:(...args)=>installRepositoryBackup(...args)};`,context);
  return {context,h:context.h,node,nodes,calls};
}

test('exact receipt shows its own seal and separate server receive/save times without a mutable backup',()=>{
  const {h}=fixture(),html=h.requestReceiptMarkup(receipt,receipt.requestId);
  assert.match(html,/版本已封存/);assert.match(html,/服务器接收时间/);assert.match(html,/服务器保存时间/);assert.match(html,/00:00:02|08:00:02/);assert.match(html,/00:00:03|08:00:03/);
  assert.match(html,new RegExp('href="'+seal.viewUrl+'"'));assert.match(html,/不是独立源码备份/);assert.doesNotMatch(html,/下载源码|排队中/);
  assert.match(h.requestReceiptText(receipt),/服务器接收时间/);
  const old=h.requestReceiptMarkup({...receipt,receivedAt:undefined,repositorySeal:null},receipt.requestId);assert.doesNotMatch(old,/版本已封存|服务器接收时间/);
});

test('fixed commit links are derived only from validated repository roots and full hashes',()=>{
  const {h}=fixture();
  for(const [url,marker] of [['https://github.com/Team/.github','commit'],['https://gitee.com/team/project','commit'],['https://gitcode.com/Team/Project','commits/detail'],['https://gitlab.com/Team/Sub/Project','-/commit']]){
    const html=h.materialMarkup({codeUrl:url,codeVersion:sha});assert.match(html,new RegExp('href="'+url.replace('Team/.github','team/.github')+'/'+marker+'/'+sha+'"','i'));
  }
  for(const url of ['https://unsupported.test/team/project','https://gitee.com/team/project/tree/main','https://gitee.com:443/team/project','https://gitee.com/team/project?x=1'])assert.doesNotMatch(h.materialMarkup({codeUrl:url,codeVersion:sha}),/查看此版本/);
  assert.doesNotMatch(h.materialMarkup({codeUrl,codeVersion:'abcdef'}),/查看此版本/);
  assert.match(h.materialMarkup(materials,{...seal,viewUrl:'https://gitee.com/SYNTHETIC/PROJECT/commit/'+sha}),/href="https:\/\/gitee.com\/SYNTHETIC\/PROJECT\/commit\//);
  const bad={...seal,viewUrl:'https://evil.test/commit/'+sha};assert.doesNotMatch(h.materialMarkup(materials,bad),/href="https:\/\/evil/);
});

test('backup statuses distinguish saved Git bytes, partial external assets and unarchived records',()=>{
  const {h}=fixture();
  for(const [status,text] of [['queued','独立源码备份排队中'],['running','独立源码备份处理中'],['ready','独立源码备份已就绪'],['partial','独立源码备份不完整'],['failed','独立源码备份失败']]){
    const html=h.backup(backup(status,{error:'git_failed'}),materials,'T1',1);assert.match(html,new RegExp(text));
    if(['ready','partial'].includes(status)){assert.match(html,/下载源码 ZIP/);assert.match(html,/下载 Git bundle/);assert.match(html,/b{64}/);assert.match(html,/不含 PPT.*视频/);assert.match(html,/不代表项目可运行/);}
    else assert.doesNotMatch(html,/下载源码|下载 Git bundle|b{64}/);
    assert.doesNotMatch(html,/git_failed/);
  }
  const html=h.backup(backup('partial',{missingAssets:[{path:'<img src=x>',kind:'lfs'},{path:'sub/module',kind:'submodule'}]}),materials,'T1',1);
  assert.match(html,/&lt;img src=x&gt;/);assert.match(html,/LFS/);assert.match(html,/子模块/);assert.doesNotMatch(html,/<img/);
  assert.match(h.backup(null,materials,'T1',1),/本版未归档/);
  assert.doesNotMatch(h.backup(backup(),{...materials,codeVersion:'d'.repeat(40)},'T1',1),/下载源码/);
});

test('private backup links stay under current same-origin API base and exact team/version',()=>{
  for(const origin of ['https://example.test','http://127.0.0.1:8765']){
    const {h}=fixture(origin+'/gotc/platform.html#team');
    assert.match(h.backup(backup('ready',{downloadUrl:origin+backup().downloadUrl}),materials,'T1',1),/下载源码 ZIP/);
    for(const downloadUrl of ['https://evil.test/gotc/api/submission/backups/T1/1/source.zip','/api/submission/backups/T1/1/source.zip','/gotc/api/submission/backups/T2/1/source.zip','/gotc/api/submission/backups/T1/2/source.zip','/gotc/api/submission/backups/T1/1/source.zip?token=bad','/gotc/api/submission/backups/T1/1/source.zip#x','javascript:alert(1)','/gotc/api/submission/backups/T1/1/../1/source.zip','/gotc/api/submission/backups/T1/1/%73ource.zip']){
      const html=h.backup(backup('ready',{downloadUrl}),materials,'T1',1);assert.doesNotMatch(html,/下载源码 ZIP/,downloadUrl);
    }
  }
  const f=fixture();vm.runInContext("session={role:'judge'}",f.context);assert.doesNotMatch(f.h.backup(backup(),materials,'T1',1),/下载源码|下载 Git bundle/);
});

test('backup-only refresh keeps dirty materials and consumes latest team JSON for the same formal version',async()=>{
  const f=fixture(),box=f.node('proof'),input=f.node('unsaved');input.value='UNSAVED';
  box.innerHTML=f.h.proof({repositorySeal:seal,repositoryBackup:backup('queued')},materials,'T1',1,'proof','最新正式版');
  f.h.installBackup(box,'team',1,materials,()=>true,{teamId:'T1',latest:true});
  f.context.respond=async()=>({team:{id:'T1'},submission:{submittedVersion:1},repositoryBackup:backup()});
  await f.node('proof-refresh').onclick();
  assert.equal(input.value,'UNSAVED');assert.match(f.node('proof-backup').innerHTML,/独立源码备份已就绪/);assert.equal(f.calls[0].path,'team');assert.equal(f.calls[0].options.loginId,'synthetic-login');
});

for(const change of ['browser','session','route','routeVersion','component','version','serverVersion','serverTeam','disconnect']){
  test(`late backup refresh cannot repaint after ${change} changes`,async()=>{
    const f=fixture(),box=f.node('proof'),waiting=deferred();box.innerHTML=f.h.proof({repositoryBackup:backup('queued')},materials,'T1',1,'proof','最新正式版');
    f.h.installBackup(box,'team',1,materials,()=>true,{teamId:'T1',latest:true});f.context.respond=()=>waiting.promise;
    const target=f.node('proof-backup');target.innerHTML='ORIGINAL';const pending=f.node('proof-refresh').onclick();
    if(change==='browser')f.context.ownerMarker='other-login';
    if(change==='session')vm.runInContext("session={role:'team',loginId:'other-login'}",f.context);
    if(change==='route')vm.runInContext("route='admin'",f.context);
    if(change==='routeVersion')vm.runInContext('routeVersion=2',f.context);
    if(change==='component')f.nodes.set('proof',{...box});
    if(change==='version')box.dataset.repositoryVersion='2';
    if(change==='disconnect')box.isConnected=false;
    waiting.resolve({team:{id:change==='serverTeam'?'T2':'T1'},submission:{submittedVersion:change==='serverVersion'?2:1},repositoryBackup:backup()});await pending;
    assert.equal(target.innerHTML,'ORIGINAL');
  });
}

test('history backup refresh reads exact version and cannot replace an administrator note form',async()=>{
  const f=fixture();vm.runInContext("session={role:'admin',loginId:'synthetic-login'};route='admin'",f.context);
  const history=f.node('history'),note=f.node('admin-note');note.value='DIRTY ADMIN NOTE';
  f.context.respond=async()=>({version:1,submittedAt:3000,materials,repositorySeal:seal,repositoryBackup:backup('partial',{missingAssets:[{kind:'lfs',path:'large.bin'}]})});
  f.h.installSubmissionHistory(history,'admin/submissions/T1/history',()=>true,{admin:true,onDetail:()=>{note.value='DIRTY ADMIN NOTE';}});
  await f.node('history-list').onclick({target:{closest:()=>({dataset:{historyVersion:'1'},isConnected:true,disabled:false})}});
  assert.match(f.node('history-detail').innerHTML,/版本已封存/);assert.match(f.node('history-detail').innerHTML,/独立源码备份不完整/);
  f.context.respond=async()=>({version:1,repositoryBackup:backup()});await f.node('history-proof-refresh').onclick();
  assert.equal(note.value,'DIRTY ADMIN NOTE');assert.equal(f.calls.at(-1).path,'admin/submissions/T1/history/1');assert.match(f.node('history-proof-backup').innerHTML,/独立源码备份已就绪/);
});

test('backup refresh failure and a stale login before clicking keep current status and editable inputs',async()=>{
  const f=fixture(),box=f.node('proof'),input=f.node('draft');input.value='KEEP';box.innerHTML=f.h.proof({repositoryBackup:backup('queued')},materials,'T1',1,'proof');
  f.h.installBackup(box,'team',1,materials,()=>true,{teamId:'T1',latest:true});
  const target=f.node('proof-backup');target.innerHTML='PENDING';f.context.respond=async()=>{throw new Error('SYNTHETIC INTERNAL PATH');};
  await f.node('proof-refresh').onclick();assert.equal(target.innerHTML,'PENDING');assert.equal(input.value,'KEEP');assert.equal(input.disabled,false);assert.match(f.node('proof-message').textContent,/当前输入已保留/);assert.doesNotMatch(f.node('proof-message').textContent,/INTERNAL/);assert.equal(f.node('proof-refresh').disabled,false);
  f.context.ownerMarker='other-login';await f.node('proof-refresh').onclick();assert.equal(f.calls.length,1);
});

test('team history uses exact version backup downloads with the owning team identity',async()=>{
  const f=fixture();f.context.respond=async()=>({version:1,materials,repositorySeal:seal,repositoryBackup:backup()});
  f.h.installSubmissionHistory(f.node('history'),'submission/history',()=>true,{teamId:'T1'});
  await f.node('history-list').onclick({target:{closest:()=>({dataset:{historyVersion:'1'},isConnected:true,disabled:false})}});
  assert.match(f.node('history-detail').innerHTML,/下载源码 ZIP/);assert.match(f.node('history-detail').innerHTML,/backups\/T1\/1\/source.zip/);
});
