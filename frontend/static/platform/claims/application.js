'use strict';

function makeClaimController({store,request,isCurrent,onChange,schools=[]}){
  const saved=store.read();let storageError=saved.error,receipt=saved.receipt,generation=0,pendingAction=null;
  let draft={name:'',school:'',email:'',teamName:'',track:'',members:emptyClaimMembers()},editRevision=null;
  let state={status:receipt?'pending':'new',team:null,code:null,error:saved.error,declaration:null,captain:null,requestRevision:null,editing:false,needsReview:false,reviewRevision:null};
  const publish=()=>{if(isCurrent())onChange({...state,schools,draft,error:[storageError,state.error].filter((value,index,array)=>value && array.indexOf(value)===index).join(' '),receipt,busy:!!pendingAction,pendingAction});};
  const copyDraft=value=>({name:value.name,school:value.school,email:value.email??'',teamName:value.teamName??'',track:value.track??'',members:value.members.map(p=>({...p}))});
  async function run(action,body,reviewLatest=false){
    if(pendingAction || !isCurrent())return;const operation=++generation;pendingAction=reviewLatest?'review':action;state.error='';publish();
    try{
      const send=()=>request('claims/'+action,body);
      const result=action==='redeem'
        ? await withLoginRetry(send,()=>operation===generation && isCurrent(),message=>{state.error=message;publish();})
        : await send();
      if(operation!==generation || !isCurrent())return;
      if(action==='request'){
        receipt={requestId:result.requestId,secret:result.secret,pin:result.pin,expiresAt:result.expiresAt};storageError=store.write(receipt);
        state={...state,...result,code:null,error:'',editing:false,needsReview:false,reviewRevision:null};
      }else{
        const keepEditing=result.status==='pending' && ((action==='update' && (JSON.stringify(draft.members)!==JSON.stringify(body.members) || (body.teamName!==undefined && (draft.teamName!==body.teamName || draft.track!==body.track)))) || (action==='status' && state.editing));
        if(action==='update' && keepEditing)editRevision=result.requestRevision;
        const needsReview=keepEditing && action==='status' && (reviewLatest || state.needsReview || result.requestRevision!==editRevision);
        state={...state,...result,error:'',editing:keepEditing,needsReview,
          reviewRevision:needsReview && reviewLatest && Number.isSafeInteger(result.requestRevision)?result.requestRevision:null};
      }
    }catch(error){if(operation===generation && isCurrent()){
      state.error=error.message;
      if(action==='update' || reviewLatest){state.needsReview=true;state.reviewRevision=null;}
    }}
    finally{if(operation===generation){pendingAction=null;publish();}}
  }
  return {
    navigationState(){
      if(!isCurrent())return {pendingInitial:false,dirty:false};
      const saved=copyDraft({...state.captain,teamName:state.declaration?.teamName,track:state.declaration?.track,members:state.declaration?.members??emptyClaimMembers()});
      return {pendingInitial:!!pendingAction && !receipt,dirty:receipt?state.editing && JSON.stringify(draft)!==JSON.stringify(saved):[draft.name,draft.school,draft.email,draft.teamName,draft.track,...draft.members.flatMap(p=>[p.name,p.school])].some(value=>value.trim())};
    },
    start(){publish();if(receipt)return this.status();},
    setDraft(value){if(!isCurrent() || (pendingAction && !receipt))return;draft=copyDraft(value);},
    addMember(){if(!isCurrent() || (pendingAction && !receipt))return;if(draft.members.length<4){draft={...draft,members:[...draft.members,{name:'',school:''}]};publish();}},
    removeMember(index){if(!isCurrent() || (pendingAction && !receipt))return;if(draft.members.length>2){draft={...draft,members:draft.members.filter((_,i)=>i!==index)};publish();}},
    apply(name,school,members,team){if(pendingAction || !isCurrent())return;draft=copyDraft({name,school,...team,members:members??emptyClaimMembers()});return run('request',{name,school,...(members===undefined?{}:{members:draft.members}),...team});},
    edit(){if(pendingAction || !isCurrent() || state.status!=='pending' || !state.captain || !Number.isSafeInteger(state.requestRevision))return;draft=copyDraft({...state.captain,teamName:state.declaration?.teamName,track:state.declaration?.track,members:state.declaration?.members??emptyClaimMembers()});editRevision=state.requestRevision;state.editing=true;state.error='';state.needsReview=false;state.reviewRevision=null;publish();},
    update(members,team){
      if(pendingAction || !isCurrent() || !state.editing)return;draft=copyDraft({...draft,members,...team});
      if(state.needsReview){state.error='草稿已保留，请先读取最新已保存名单并逐人核对，再继续保存。';publish();return;}
      if(receipt)return run('update',{requestId:receipt.requestId,secret:receipt.secret,expectedRevision:editRevision,members:draft.members,...team});
    },
    reviewLatest(){if(receipt && state.editing)return run('status',{requestId:receipt.requestId,secret:receipt.secret},true);},
    confirmReview(){
      if(pendingAction || !isCurrent() || !state.editing || state.status!=='pending' || !Number.isSafeInteger(state.reviewRevision) || state.reviewRevision!==state.requestRevision)return;
      editRevision=state.reviewRevision;state.needsReview=false;state.reviewRevision=null;state.error='';publish();
    },
    cancelEdit(){generation++;pendingAction=null;state.editing=false;state.error='';state.needsReview=false;state.reviewRevision=null;publish();return this.status();},
    status(){if(receipt)return run('status',{requestId:receipt.requestId,secret:receipt.secret});},
    redeem(){if(receipt)return run('redeem',{requestId:receipt.requestId,secret:receipt.secret});},
    clear(){generation++;pendingAction=null;receipt=null;storageError=store.clear();draft={name:'',school:'',email:'',teamName:'',track:'',members:emptyClaimMembers()};state={status:'new',team:null,code:null,error:'',declaration:null,captain:null,requestRevision:null,editing:false,needsReview:false,reviewRevision:null};publish();},
    dispose(){generation++;pendingAction=null;state.code=null;}
  };
}

function acceptClaimRecoveryLink(store,hash,replaceHash,confirmReplace=()=>true){
  const match=/^#claim\/recover\/([a-f0-9]{32})\.([A-Za-z0-9_-]{43})\.(\d{6})\.(\d{1,16})$/.exec(hash);
  if(!match){if(hash.startsWith('#claim/recover/'))replaceHash();return '';}
  const receipt={requestId:match[1],secret:match[2],pin:match[3],expiresAt:Number(match[4])},saved=store.read().receipt;
  replaceHash();
  if(saved && saved.requestId!==receipt.requestId && !confirmReplace())return '';
  return store.write(receipt);
}

async function renderClaim(version){
  currentClaimController?.dispose();
  const store=claimReceiptStore();
  const recoveryError=acceptClaimRecoveryLink(store,location.hash,()=>history.replaceState(null,'',location.pathname+location.search+'#claim'),
    ()=>confirm('此标签页已有另一份领取凭据。是否改用邮件中的申请？'));
  let schools=[];
  try{const result=await api('claims/schools');if(Array.isArray(result?.schools))schools=result.schools.filter(school=>typeof school==='string');}catch{}
  if(route!=='claim' || version!==routeVersion)return;
  const controller=makeClaimController({store,schools,request:api,isCurrent:()=>route==='claim' && version===routeVersion && currentClaimController===controller,onChange:state=>{
    root.innerHTML=claimStateMarkup(recoveryError && !state.error?{...state,error:recoveryError}:state);
    const form=document.getElementById('claim-request-form')??document.getElementById('claim-update-form');
    if(form){
      const read=()=>({name:form.elements.name?.value??state.captain.name,school:form.elements.school?.value??state.captain.school,email:form.elements.email?.value??'',members:claimFormMembers(form),teamName:form.elements.teamName.value,track:form.elements.track.value});
      form.oninput=event=>{syncClaimSchoolChoice(event?.target);controller.setDraft(read());};
      form.onsubmit=e=>{e.preventDefault();const value=read();if(state.receipt)controller.update(value.members,{teamName:value.teamName,track:value.track});else controller.apply(value.name,value.school,value.members,{email:value.email,teamName:value.teamName,track:value.track});};
      const add=document.getElementById('claim-member-add');if(add)add.onclick=()=>{controller.setDraft(read());controller.addMember();};
      form.querySelectorAll('[data-claim-remove]').forEach(button=>button.onclick=()=>{controller.setDraft(read());controller.removeMember(Number(button.dataset.claimRemove));});
    }
    const workbench=document.getElementById('claim-workbench');if(workbench)workbench.onclick=buttonAction(event=>openClaimWorkbench(event));
    const edit=document.getElementById('claim-edit');if(edit)edit.onclick=()=>controller.edit();
    const reload=document.getElementById('claim-edit-reload');if(reload)reload.onclick=()=>controller.reviewLatest();
    const confirmEdit=document.getElementById('claim-edit-confirm');if(confirmEdit)confirmEdit.onclick=()=>controller.confirmReview();
    const cancel=document.getElementById('claim-edit-cancel');if(cancel)cancel.onclick=()=>controller.cancelEdit();
    const refresh=document.getElementById('claim-status');if(refresh)refresh.onclick=()=>controller.status();
    const redeem=document.getElementById('claim-redeem');if(redeem)redeem.onclick=()=>controller.redeem();
    const clear=document.getElementById('claim-clear');if(clear)clear.onclick=()=>{if(confirm('清除后本标签页无法继续领取。已领取的参赛码请先自行保存。确认清除？'))controller.clear();};
  }});
  currentClaimController=controller;await controller.start();
}
