'use strict';

function roadshowRosterMarkup(data){
  const rows=(data.judges || []).map(judge=>`<tr><td>${esc(judge.name)}</td><td>${judge.active?'有效':'已停用'}</td><td>${judge.canLogin?(data.loginSchedule?.loginAvailable===false?'名单资格有效，等待登录开放':'可用真实姓名登录'):'请联系主办方'}</td></tr>`).join('');
  return `<div class="info-banner ${data.configured?'':'warning'}">${data.configured?`已配置 ${(data.names || []).length} 位路演评委。`:'评委名单待配置；当前不能用真实姓名登录。'}</div><form class="surface" data-roadshow-roster><h2>路演评委名单</h2><p>每行填写一位评委的完整姓名。真实姓名就是邀请码，登录时须与名单一致。同名评委请加单位等说明区分。</p><label class="field">评委姓名（每行一位）<textarea name="names" rows="10" maxlength="20000" ${data.editable?'':'readonly'}>${esc((data.names || []).join('\n'))}</textarea></label><p class="small-note">${data.editable?'名单保存后会为每位评委建立路演专属身份，并加入当前路演评审名单。不会开放评分窗口。':'本轮路演已开放、冻结或有评审记录，名单已锁定。'}</p><button class="btn" type="submit" ${data.editable?'':'disabled'}>保存路演评委名单</button><button class="btn secondary" type="button" data-roadshow-reload>重新读取名单</button><p class="form-error" role="alert"></p></form><section class="surface"><h2>登录资格</h2>${rows?`<div class="table-scroll"><table><thead><tr><th>评委</th><th>身份状态</th><th>真实姓名登录</th></tr></thead><tbody>${rows}</tbody></table></div>`:'<p>尚无评委名单。收到名单后在上方保存即可。</p>'}<p><a href="#judge-roadshow">打开路演评委入口 →</a></p><p><a href="#admin" data-roadshow-stage>配置路演队伍与评审窗口 →</a></p></section>`;
}

function installRoadshowRoster(view,data){
  const form=view.container.querySelector('[data-roadshow-roster]'),field=form.elements.names,error=form.querySelector('.form-error'),save=form.querySelector('[type=submit]');
  let revision=data.revision,runId=data.runId,edit=0,busy=false,blocked=false,editable=data.editable===true;
  view.form=form;
  const current=()=>view.current() && form.isConnected;
  const valid=()=>{if(!current())return false;if(session!==view.owner || !ownsBrowserLogin(view.owner)){save.disabled=true;error.textContent='管理员登录已失效或切换。当前名单输入已保留，请复制后重新登录。';return false;}return true;};
  const changed=()=>{edit++;dirtyAdminForms.add(form);};field.addEventListener('input',changed);
  view.cleanups.push(()=>field.removeEventListener('input',changed));
  form.querySelector('[data-roadshow-reload]').onclick=buttonAction(()=>{if(!valid())return;if(busy || [...dirtyAdminForms,...(typeof activeFinalsView!=='undefined' && activeFinalsView?activeFinalsView.forms:[])].some(item=>item.isConnected && item.finalsBusy)){error.textContent='正在保存，请等待结果返回后再重新读取。';return;}if([...dirtyAdminForms].some(item=>item.isConnected) && !confirm('重新读取会替换此页未保存的评委或队伍名单。请先复制要保留的内容，继续？'))return;dirtyAdminForms.delete(form);return renderRoute();});
  form.onsubmit=buttonAction(async event=>{
    event.preventDefault();if(!valid() || busy || blocked || !editable)return;
    const epoch=edit,names=field.value.split(/\r?\n/).map(value=>value.trim()).filter(Boolean);
    busy=true;form.roadshowBusy=true;save.disabled=true;error.textContent='';
    try{
      const result=await api('admin/roadshow-access',{expectedRevision:revision,runId,names},{loginId:view.owner.loginId});
      if(!valid())return;revision=result.revision;runId=result.runId;editable=result.editable===true;
      if(edit===epoch){field.value=(result.names || []).join('\n');dirtyAdminForms.delete(form);error.textContent='名单已保存。'+(result.loginSchedule?.message || '评委可在登录入口核对开放状态后，使用名单中的真实姓名登录。')+' 点击重新读取可查看最新登录资格。';}
      else error.textContent='提交时的名单已保存；保存期间的新输入已保留，尚未保存。';
    }catch(failure){
      if(!current())return;dirtyAdminForms.add(form);
      if(!valid())return;
      if(failure.status===409){blocked=true;error.textContent=failure.message+' 当前输入已保留，请复制后重新读取名单，再核对保存。';}
      else error.textContent=failure.message+' 当前输入已保留，没有自动重试保存。';
    }finally{busy=false;form.roadshowBusy=false;if(valid())save.disabled=blocked || !editable;}
  });
  view.container.querySelector('[data-roadshow-stage]').onclick=()=>{adminStage='roadshow';};
}

async function renderRoadshowAccessAdmin(version){
  disposeRoadshowAccess();const owner=session;
  const view={owner,container:null,form:null,disposed:false,cleanups:[],current:()=>!view.disposed && routeVersion===version && route==='admin-roadshow' && (!view.container || view.container.isConnected),dispose(){view.disposed=true;view.cleanups.forEach(fn=>fn());if(view.form)dirtyAdminForms.delete(view.form);}};
  activeRoadshowAccess=view;
  const data=await api('admin/roadshow-access',undefined,{loginId:owner.loginId});
  if(!view.current() || session!==owner || !ownsBrowserLogin(owner))return;
  root.innerHTML=heading('ROADSHOW / 评委管理','路演名单与实时成绩','先录入评委名单；真实姓名就是邀请码，登录开放后输入名单中的完整姓名即可验证登录。')+`<div class="roadshow-access-admin">${data.loginSchedule?.message?`<p class="info-banner" role="status">${esc(data.loginSchedule.message)}</p>`:''}${roadshowRosterMarkup(data)}</div>`;
  view.container=root.querySelector('.roadshow-access-admin');installRoadshowRoster(view,data);
  if(typeof mountRoadshowScoringAdmin==='function')await mountRoadshowScoringAdmin(version,view.container);
}
