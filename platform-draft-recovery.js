'use strict';
// Browser-only recovery records. No authentication material or network operations belong here.
(() => {
  const TTL=7*24*60*60*1000;
  const freshId=()=>globalThis.crypto?.randomUUID?.() || `local-${Date.now()}-${Math.random().toString(36).slice(2)}-${Math.random().toString(36).slice(2)}`;
  function create({storage,namespace,teamId,writerId=freshId(),baseVersion,baseMaterials,fields,now=()=>Date.now(),id=freshId,expectedEpoch,loginId=""}){
    const prefix=`gotc-material-recovery:v1:${encodeURIComponent(namespace)}:${encodeURIComponent(teamId)}:`,epochKey=prefix+(loginId?'auth:'+encodeURIComponent(loginId)+':':'')+'epoch';
    let activeId=id(),revision=0,error='',revoked=false,epoch=expectedEpoch ?? '';
    const object=value=>value!==null && typeof value==='object' && !Array.isArray(value);
    function access(){const target=storage();if(!target)throw new Error('storage unavailable');return target;}
    try{if(expectedEpoch===undefined)epoch=access().getItem(epochKey) || '';checkEpoch();}catch{error='本地恢复不可用，请保存服务器草稿或复制当前输入。';}
    function fail(){error='本地恢复不可用，请保存服务器草稿或复制当前输入。';return null;}
    function validMaterials(value){return object(value) && Object.keys(value).length===fields.length && fields.every(([key,limit])=>Object.hasOwn(value,key) && typeof value[key]==='string' && Array.from(value[key]).length<=limit);}
    function read(key,raw){
      if(typeof raw!=='string' || raw.length>131072)return null;
      let value;try{value=JSON.parse(raw);}catch{return null;}
      if(!object(value) || value.schema!==1 || value.teamId!==teamId || typeof value.id!=='string' || key!==prefix+'slot:'+value.id || !/^[a-zA-Z0-9_-]{6,160}$/.test(value.id)
          || typeof value.writerId!=='string' || !Number.isSafeInteger(value.localRevision) || value.localRevision<1 || !Number.isSafeInteger(value.baseVersion) || value.baseVersion<0
          || !Number.isSafeInteger(value.updatedAt) || value.updatedAt<0 || typeof value.epoch!=='string' || (value.loginId!==undefined && typeof value.loginId!=='string') || !['editing','pending','unknown','conflict'].includes(value.state)
          || !validMaterials(value.materials) || !validMaterials(value.baseMaterials))return null;
      if(value.request?.requestId!==undefined && (typeof value.request.requestId!=='string' || !/^[A-Za-z0-9_-]{16,128}$/.test(value.request.requestId)))return null;
      if(value.state!=='editing' && (!object(value.request) || !['draft','submitted'].includes(value.request.action) || value.request.revision!==value.localRevision || value.request.baseVersion!==value.baseVersion))return null;
      return {key,raw,record:value};
    }
    function checkEpoch(){try{if((loginId && epoch) || (access().getItem(epochKey) || '')!==epoch){revoked=true;error='本队本地副本已在其他标签页退出时清理。请重新登录本队后再编辑。';}return !revoked;}catch{fail();return false;}}
    function candidates(){
      if(!checkEpoch())return {items:[],ignored:0};
      const items=[];let ignored=0;
      try{const target=access(),keys=Array.from({length:target.length},(_,index)=>target.key(index)).filter(key=>key?.startsWith(prefix+'slot:'));
        for(const key of keys){const raw=target.getItem(key),snapshot=read(key,raw);if(!snapshot){ignored++;continue;}if(now()-snapshot.record.updatedAt>TTL){if(target.getItem(key)===raw)target.removeItem(key);continue;}const owner=snapshot.record.loginId || '',ownerEpoch=prefix+(owner?'auth:'+encodeURIComponent(owner)+':':'')+'epoch';if(snapshot.record.epoch!==(target.getItem(ownerEpoch) || '') || (owner && target.getItem(ownerEpoch)))continue;if(snapshot.record.id!==activeId)items.push(snapshot);}
        items.sort((a,b)=>b.record.updatedAt-a.record.updatedAt || a.record.id.localeCompare(b.record.id));return {items,ignored};
      }catch{fail();return {items,ignored};}
    }
    function put(record){
      if(!checkEpoch())return null;
      const key=prefix+'slot:'+record.id,raw=JSON.stringify(record);
      if(!read(key,raw))return fail();
      try{const target=access();target.setItem(key,raw);if((target.getItem(epochKey) || '')!==epoch){if(target.getItem(key)===raw)target.removeItem(key);checkEpoch();return null;}error='';return {key,raw,record};}catch{return fail();}
    }
    function write(materials,state='editing',action=null,requestId=null){
      if(!validMaterials(materials))return fail();
      if(!checkEpoch())return null;
      let previous=null;try{previous=read(prefix+'slot:'+activeId,access().getItem(prefix+'slot:'+activeId));}catch{return fail();}
      const same=previous && previous.record.baseVersion===baseVersion && fields.every(([key])=>previous.record.materials[key]===materials[key]);
      if(state==='editing' && same && previous.record.state==='editing'){error='';return previous;}
      const nextRevision=++revision,record={schema:1,id:activeId,writerId,teamId,loginId,epoch,localRevision:nextRevision,baseVersion,baseMaterials:{...baseMaterials},materials:{...materials},updatedAt:same?previous.record.updatedAt:now(),state};
      if(state!=='editing')record.request={action,revision:nextRevision,baseVersion,...(requestId?{requestId}:{})};return put(record);
    }
    function result(sent,state){
      if(!sent || !checkEpoch())return false;
      try{const current=read(sent.key,access().getItem(sent.key));if(!current || (current.record.loginId || '')!==loginId || current.record.epoch!==epoch || current.record.localRevision!==sent.record.localRevision || JSON.stringify(current.record.materials)!==JSON.stringify(sent.record.materials))return false;
        return !!put({...current.record,state});}catch{fail();return false;}
    }
    function acknowledge(sent){
      if(!sent || !checkEpoch())return false;
      try{const target=access(),current=read(sent.key,target.getItem(sent.key));if(!current || (current.record.loginId || '')!==loginId || current.record.epoch!==epoch || current.record.localRevision!==sent.record.localRevision || current.record.writerId!==writerId || current.record.request?.requestId!==sent.record.request?.requestId || JSON.stringify(current.record.materials)!==JSON.stringify(sent.record.materials))return false;
        if(target.getItem(sent.key)!==current.raw)return false;target.removeItem(sent.key);error='';return true;}catch{fail();return false;}
    }
    function discard(snapshot){if(!checkEpoch())return false;try{const target=access();if(!snapshot || !snapshot.key.startsWith(prefix+'slot:') || target.getItem(snapshot.key)!==snapshot.raw)return false;target.removeItem(snapshot.key);error='';return true;}catch{fail();return false;}}
    function capturePurge(){
      try{const target=access(),items=[];for(let index=0;index<target.length;index++){const key=target.key(index);if(key?.startsWith(prefix+'slot:')){const raw=target.getItem(key);if(read(key,raw))items.push({key,raw});}}return items;}catch{fail();return null;}
    }
    function purge(captured){
      try{const target=access(),next=id();if(!checkEpoch())return false;target.setItem(epochKey,next);if(target.getItem(epochKey)!==next)throw new Error('epoch write failed');revoked=true;
        const keys=Array.from({length:target.length},(_,index)=>target.key(index)).filter(key=>key?.startsWith(prefix+'slot:'));
        for(const key of keys){if(target.getItem(epochKey)!==next){error='本地清理代次已变化，未继续删除。';return false;}const raw=target.getItem(key),snapshot=read(key,raw);if(snapshot?.record.epoch===next)continue;if(loginId && (!snapshot || ((snapshot.record.loginId || '')!==loginId && !captured?.some(item=>item.key===key && item.raw===raw))))continue;if(target.getItem(key)===raw && target.getItem(epochKey)===next)target.removeItem(key);}return target.getItem(epochKey)===next && captured!==null;
      }catch{fail();return false;}
    }
    return {write,candidates,result,acknowledge,discard,purge,capturePurge,checkEpoch,rebase(version,materials){baseVersion=version;baseMaterials={...materials};},fork(){activeId=id();revision=0;},get error(){return error;},get revoked(){return revoked;},get prefix(){return prefix;},get epoch(){return epoch;}};
  }
  globalThis.GotcDraftRecovery={create,freshId,TTL};
})();
