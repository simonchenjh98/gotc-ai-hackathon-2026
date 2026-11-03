import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const source=fs.readFileSync(new URL('../platform-draft-recovery.js',import.meta.url),'utf8');
const context=vm.createContext({Date,Math});vm.runInContext(source,context);const h=context.GotcDraftRecovery;
class Storage{map=new Map();get length(){this.onLength?.();return this.map.size;}key(index){return [...this.map.keys()][index] ?? null;}getItem(key){return this.map.get(key) ?? null;}setItem(key,value){this.map.set(key,value);}removeItem(key){this.map.delete(key);}}
const fields=[['projectName',140],['summary',600],['codeUrl',1024]],empty={projectName:'',summary:'',codeUrl:''},raw={projectName:'  Raw title  ',summary:'\nline one\n ',codeUrl:'https://unfinished/'};
let serial=0;
function make(storage,more={}){return h.create({storage:()=>storage,namespace:'/gotc/',teamId:'T1',writerId:'writer-'+(++serial),baseVersion:0,baseMaterials:empty,fields,now:()=>1000,id:()=>'slot-'+(++serial),...more});}
test('no initial candidate is written and raw incomplete inputs survive new documents without credentials or confirmation',()=>{
  const storage=new Storage(),first=make(storage);assert.equal(storage.length,0);const saved=first.write(raw);assert.ok(saved);const reader=make(storage),candidate=reader.candidates().items[0];assert.deepEqual({...candidate.record.materials},raw);assert.equal(candidate.record.id,saved.record.id);assert.equal(candidate.record.updatedAt,1000);assert.ok(!candidate.raw.includes('confirmed'));assert.ok(!candidate.raw.includes('token'));assert.equal(first.write({...raw,confirmed:true}),null);assert.equal(first.write({...raw,token:'must-not-persist'}),null);
});
test('team/path isolation and independent document slots never overwrite each other',()=>{
  const storage=new Storage(),a=make(storage),b=make(storage);const one=a.write(raw),two=b.write({...raw,summary:'OTHER_TAB'});assert.notEqual(one.key,two.key);assert.equal(make(storage).candidates().items.length,2);assert.equal(make(storage,{teamId:'T2'}).candidates().items.length,0);assert.equal(make(storage,{namespace:'/other/'}).candidates().items.length,0);
});
test('restoring into a fresh slot preserves both the source and current input slots',()=>{
  const storage=new Storage(),source=make(storage);source.write(raw);const current=make(storage);current.write({...raw,summary:'CURRENT_INPUT'});current.fork();current.write(raw);assert.equal(make(storage).candidates().items.length,3);
});
test('discard checks the exact selected record and cannot delete a later edit',()=>{
  const storage=new Storage(),writer=make(storage),old=writer.write(raw),reader=make(storage);writer.write({...raw,summary:'NEW_INPUT'});assert.equal(reader.discard(old),false);assert.equal(reader.candidates().items[0].record.materials.summary,'NEW_INPUT');assert.equal(reader.discard(reader.candidates().items[0]),true);
});
test('pending/unknown retain exact request metadata and only known success acknowledges the sent revision',()=>{
  const storage=new Storage(),writer=make(storage),sent=writer.write(raw,'pending','draft');assert.equal(sent.record.request.action,'draft');assert.equal(sent.record.request.revision,sent.record.localRevision);assert.equal(make(storage).candidates().items[0].record.state,'pending');writer.result(sent,'unknown');assert.equal(make(storage).candidates().items[0].record.state,'unknown');assert.equal(writer.acknowledge(sent),true);assert.equal(storage.getItem(sent.key),null);
});
test('late success does not remove newer input or another tab and next editing can use the response baseline',()=>{
  const storage=new Storage(),writer=make(storage),other=make(storage);other.write({...raw,summary:'OTHER'});const sent=writer.write(raw,'pending','submitted');writer.write({...raw,summary:'NEWER'});assert.equal(writer.acknowledge(sent),false);assert.equal(make(storage).candidates().items.length,2);writer.rebase(1,raw);const next=writer.write({...raw,summary:'CONTINUED'});assert.equal(next.record.baseVersion,1);assert.equal(next.record.baseMaterials.summary,raw.summary);
});
test('conflict remains recoverable without deleting any record',()=>{
  const storage=new Storage(),writer=make(storage),sent=writer.write(raw,'pending','draft');writer.result(sent,'conflict');assert.equal(make(storage).candidates().items[0].record.state,'conflict');assert.equal(storage.length,1);
});
test('successful logout purges only this team and old pages cannot recreate old-epoch copies',()=>{
  const storage=new Storage(),writer=make(storage),other=make(storage,{teamId:'T2'});writer.write(raw);other.write(raw);assert.equal(writer.purge(),true);assert.equal(writer.write({...raw,summary:'OLD_PAGE'}),null);assert.equal(make(storage).candidates().items.length,0);assert.equal(make(storage,{teamId:'T2'}).candidates().items.length,1);
});
test('old purge stops when a newer generation and reauthorized copy arrive',()=>{
  const storage=new Storage(),old=make(storage);old.write(raw);let newer;
  storage.onLength=()=>{storage.onLength=null;storage.setItem(old.prefix+'epoch','newer-epoch');newer=make(storage).write({...raw,summary:'NEW_AUTH'});};
  assert.equal(old.purge(),false);assert.ok(storage.getItem(newer.key));assert.equal(make(storage).candidates().items[0].record.materials.summary,'NEW_AUTH');
});
test('expired records from an earlier cleanup generation are reclaimed after a prior deletion failure',()=>{
  const storage=new Storage(),writer=make(storage),saved=writer.write(raw);const remove=storage.removeItem.bind(storage);storage.removeItem=()=>{throw new Error('blocked remove');};assert.equal(writer.purge(),false);storage.removeItem=remove;assert.ok(storage.getItem(saved.key));make(storage,{now:()=>h.TTL+2000}).candidates();assert.equal(storage.getItem(saved.key),null);
});
test('corrupt JSON and invalid fields are isolated while a valid candidate is retained',()=>{
  const storage=new Storage(),writer=make(storage);writer.write(raw);storage.setItem(writer.prefix+'slot:broken','{bad');storage.setItem(writer.prefix+'slot:spoofed',JSON.stringify({schema:1,materials:{summary:'bad'}}));const read=make(storage).candidates();assert.equal(read.items.length,1);assert.equal(read.ignored,2);
});
test('disabled storage and quota failures never claim persistence and do not throw',()=>{
  const disabled=h.create({storage:()=>{throw new Error('denied');},namespace:'/gotc/',teamId:'T1',baseVersion:0,baseMaterials:empty,fields});assert.equal(disabled.write(raw),null);assert.match(disabled.error,/不可用/);assert.equal(disabled.candidates().items.length,0);
  const quota=new Storage();quota.setItem=()=>{throw new Error('quota');};const writer=make(quota);assert.equal(writer.write(raw),null);assert.match(writer.error,/不可用/);assert.equal(quota.length,0);
});

test('unchanged input and leaving do not advance edit time/revision or renew expiry',()=>{
  const storage=new Storage();let tick=1000;const writer=make(storage,{now:()=>tick});const first=writer.write(raw);tick=2000;const unchanged=writer.write({...raw});assert.equal(unchanged.record.updatedAt,1000);assert.equal(unchanged.record.localRevision,first.record.localRevision);const pending=writer.write(raw,'pending','draft');assert.equal(pending.record.updatedAt,1000);assert.ok(pending.record.localRevision>first.record.localRevision);
});
test('a replacement store inherits the original epoch and cannot adopt a later logout generation',()=>{
  const storage=new Storage(),writer=make(storage),expectedEpoch=writer.epoch;writer.write(raw);writer.purge();const inherited=make(storage,{expectedEpoch});assert.equal(inherited.epoch,expectedEpoch);assert.equal(inherited.revoked,true);assert.equal(inherited.write(raw),null);const newlyAuthorized=make(storage);assert.notEqual(newlyAuthorized.epoch,expectedEpoch);assert.ok(newlyAuthorized.write({...raw,summary:'EXPLICIT_NEW_AUTH'}));assert.equal(inherited.acknowledge(null),false);
});

test('login-scoped cleanup revokes its own writers and only exact historical snapshots',()=>{
  const storage=new Storage(),historical=make(storage,{loginId:'historical-login'}),old=make(storage,{loginId:'old-login'});
  historical.write({...raw,summary:'HISTORICAL'});old.write(raw);const captured=old.capturePurge();
  const newer=make(storage,{loginId:'new-login'}),fresh=newer.write({...raw,summary:'NEW_LOGIN'});
  historical.write({...raw,summary:'CHANGED_AFTER_CAPTURE'});assert.equal(old.purge(captured),true);
  assert.ok(storage.getItem(fresh.key));assert.ok(historical.candidates().items.some(item=>item.record.materials.summary==='NEW_LOGIN'));
  assert.ok(make(storage,{loginId:'new-login'}).candidates().items.some(item=>item.record.materials.summary==='CHANGED_AFTER_CAPTURE'));
  assert.equal(old.write(raw),null);const reconstructed=make(storage,{loginId:'old-login'});assert.equal(reconstructed.revoked,true);assert.equal(reconstructed.write(raw),null);
});
test('failed deletion leaves revoked login records hidden and does not remove unrelated corrupt records',()=>{
  const storage=new Storage(),old=make(storage,{loginId:'old-login'});old.write(raw);const captured=old.capturePurge();storage.setItem(old.prefix+'slot:corrupt','{bad');const remove=storage.removeItem.bind(storage);
  storage.removeItem=()=>{throw new Error('cannot remove');};assert.equal(old.purge(captured),false);storage.removeItem=remove;
  assert.equal(make(storage,{loginId:'new-login'}).candidates().items.length,0);assert.equal(storage.getItem(old.prefix+'slot:corrupt'),'{bad');
});
