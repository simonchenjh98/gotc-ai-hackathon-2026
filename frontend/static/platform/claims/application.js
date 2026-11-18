'use strict';

function makeClaimController({store,request,isCurrent,onChange}){
  const saved=store.read();let storageError=saved.error,receipt=saved.receipt,generation=0,busy=false;
  let draft={name:'',school:'',teamName:'',track:'',members:emptyClaimMembers()},editRevision=null;
  let state={status:receipt?'pending':'new',team:null,code:null,error:saved.error,declaration:null,captain:null,requestRevision:null,editing:false,needsReview:false,reviewRevision:null};
  const publish=()=>{if(isCurrent())onChange({...state,draft,error:[storageError,state.error].filter((value,index,array)=>value && array.indexOf(value)===index).join(' '),receipt,busy});};
  const copyDraft=value=>({name:value.name,school:value.school,teamName:value.teamName??'',track:value.track??'',members:value.members.map(p=>({...p}))});
  async function run(action,body,reviewLatest=false){
    if(busy || !isCurrent())return;const operation=++generation;busy=true;state.error='';publish();
    try{
      const result=await request('claims/'+action,body);
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
    finally{if(operation===generation){busy=false;publish();}}
  }
  return {
    navigationState(){
      if(!isCurrent())return {pendingInitial:false,dirty:false};
      const saved=copyDraft({...state.captain,teamName:state.declaration?.teamName,track:state.declaration?.track,members:state.declaration?.members??emptyClaimMembers()});
      return {pendingInitial:busy && !receipt,dirty:receipt?state.editing && JSON.stringify(draft)!==JSON.stringify(saved):[draft.name,draft.school,draft.teamName,draft.track,...draft.members.flatMap(p=>[p.name,p.school])].some(value=>value.trim())};
    },
    start(){publish();if(receipt)return this.status();},
    setDraft(value){if(!isCurrent() || (busy && !receipt))return;draft=copyDraft(value);},
    addMember(){if(!isCurrent() || (busy && !receipt))return;if(draft.members.length<4){draft={...draft,members:[...draft.members,{name:'',school:''}]};publish();}},
    removeMember(index){if(!isCurrent() || (busy && !receipt))return;if(draft.members.length>2){draft={...draft,members:draft.members.filter((_,i)=>i!==index)};publish();}},
    apply(name,school,members,team){if(busy || !isCurrent())return;draft=copyDraft({name,school,...team,members:members??emptyClaimMembers()});return run('request',{name,school,...(members===undefined?{}:{members:draft.members}),...team});},
    edit(){if(busy || !isCurrent() || state.status!=='pending' || !state.captain || !Number.isSafeInteger(state.requestRevision))return;draft=copyDraft({...state.captain,teamName:state.declaration?.teamName,track:state.declaration?.track,members:state.declaration?.members??emptyClaimMembers()});editRevision=state.requestRevision;state.editing=true;state.error='';state.needsReview=false;state.reviewRevision=null;publish();},
    update(members,team){
      if(busy || !isCurrent() || !state.editing)return;draft=copyDraft({...draft,members,...team});
      if(state.needsReview){state.error='草稿已保留，请先读取最新已保存名单并逐人核对，再继续保存。';publish();return;}
      if(receipt)return run('update',{requestId:receipt.requestId,secret:receipt.secret,expectedRevision:editRevision,members:draft.members,...team});
    },
    reviewLatest(){if(receipt && state.editing)return run('status',{requestId:receipt.requestId,secret:receipt.secret},true);},
    confirmReview(){
      if(busy || !isCurrent() || !state.editing || state.status!=='pending' || !Number.isSafeInteger(state.reviewRevision) || state.reviewRevision!==state.requestRevision)return;
      editRevision=state.reviewRevision;state.needsReview=false;state.reviewRevision=null;state.error='';publish();
    },
    cancelEdit(){generation++;busy=false;state.editing=false;state.error='';state.needsReview=false;state.reviewRevision=null;publish();return this.status();},
    status(){if(receipt)return run('status',{requestId:receipt.requestId,secret:receipt.secret});},
    redeem(){if(receipt)return run('redeem',{requestId:receipt.requestId,secret:receipt.secret});},
    clear(){generation++;busy=false;receipt=null;storageError=store.clear();draft={name:'',school:'',teamName:'',track:'',members:emptyClaimMembers()};state={status:'new',team:null,code:null,error:'',declaration:null,captain:null,requestRevision:null,editing:false,needsReview:false,reviewRevision:null};publish();},
    dispose(){generation++;busy=false;state.code=null;}
  };
}

async function renderClaim(version){
  currentClaimController?.dispose();
  const controller=makeClaimController({store:claimReceiptStore(),request:api,isCurrent:()=>route==='claim' && version===routeVersion && currentClaimController===controller,onChange:state=>{
    root.innerHTML=claimStateMarkup(state);
    const form=document.getElementById('claim-request-form')??document.getElementById('claim-update-form');
    if(form){
      const read=()=>({name:form.elements.name?.value??state.captain.name,school:form.elements.school?.value??state.captain.school,members:claimFormMembers(form),teamName:form.elements.teamName.value,track:form.elements.track.value});
      form.oninput=()=>controller.setDraft(read());
      form.onsubmit=e=>{e.preventDefault();const value=read();if(state.receipt)controller.update(value.members,{teamName:value.teamName,track:value.track});else controller.apply(value.name,value.school,value.members,{teamName:value.teamName,track:value.track});};
      const add=document.getElementById('claim-member-add');if(add)add.onclick=()=>{controller.setDraft(read());controller.addMember();};
      form.querySelectorAll('[data-claim-remove]').forEach(button=>button.onclick=()=>{controller.setDraft(read());controller.removeMember(Number(button.dataset.claimRemove));});
    }
    const workbench=document.getElementById('claim-workbench');if(workbench)workbench.onclick=event=>openClaimWorkbench(event);
    const edit=document.getElementById('claim-edit');if(edit)edit.onclick=()=>controller.edit();
    const reload=document.getElementById('claim-edit-reload');if(reload)reload.onclick=()=>controller.reviewLatest();
    const confirmEdit=document.getElementById('claim-edit-confirm');if(confirmEdit)confirmEdit.onclick=()=>controller.confirmReview();
    const cancel=document.getElementById('claim-edit-cancel');if(cancel)cancel.onclick=()=>controller.cancelEdit();
    const refresh=document.getElementById('claim-status');if(refresh)refresh.onclick=()=>controller.status();
    const redeem=document.getElementById('claim-redeem');if(redeem)redeem.onclick=()=>controller.redeem();
    const clear=document.getElementById('claim-clear');if(clear)clear.onclick=()=>{if(confirm('清除后本标签页无法继续领取。已领取的提交码请先自行保存。确认清除？'))controller.clear();};
  }});
  currentClaimController=controller;await controller.start();
}
