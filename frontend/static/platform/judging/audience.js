'use strict';

async function renderAudience(version){
  const owner=session;
  const active=()=>version===routeVersion && route==='audience';
  function valid(){
    if(!active())return false;
    if(session===owner && ownsBrowserLogin(owner))return true;
    document.querySelectorAll('[data-vote]').forEach(button=>button.disabled=true);
    let notice=document.getElementById('audience-login-status');
    if(!notice){root.insertAdjacentHTML('afterbegin','<p id="audience-login-status" class="info-banner warning" role="alert"></p>');notice=document.getElementById('audience-login-status');}
    notice.textContent='当前观众登录已失效或已切换，请刷新页面并核对身份后投票。';
    return false;
  }
  currentIdentityPage={valid};
  if(!valid())return;
  const data=await api('audience',undefined,{loginId:owner.loginId});if(!valid())return;
  const voted=(data.votedTeamIds || []).map(String),limit=data.voteLimit || 3;root.innerHTML=heading('AUDIENCE / 观众选择','把一票，投给打动你的作品。',`已支持 ${voted.length} / ${limit} 支队伍。观众票独立展示，不叠加到专业总分。`)+`<div class="info-banner ${data.votingOpen?'':'warning'}">${data.votingOpen?'请先看作品演示，再选择支持的队伍。每支队伍每个邀请码最多计 1 票，提交后不能撤回。':'当前观众投票尚未开放或已截止。'}</div>`+(data.teams.length?`<div class="vote-grid">${data.teams.map(t=>`<article class="vote-card ${voted.includes(String(t.id))?'selected':''}"><span class="eyebrow">TEAM ${String(t.number).padStart(2,'0')}</span><h2>${esc(t.name)}</h2><p>${esc(t.project || '作品名称待完善')}<br>${esc(t.track || '')}</p><button class="btn ${voted.includes(String(t.id))?'secondary':''}" data-vote="${esc(t.id)}" ${voted.includes(String(t.id))||voted.length>=limit||!data.votingOpen?'disabled':''}>${voted.includes(String(t.id))?'已支持 ✓':voted.length>=limit?'投票名额已用完':'支持这个作品 ↗'}</button></article>`).join('')}</div>`:empty('队伍即将出现在这里','主办方录入队伍后，你就可以了解并支持他们的作品。'));
  document.querySelectorAll('[data-vote]').forEach(button=>button.onclick=async()=>{
    if(button.disabled || !valid())return;
    button.disabled=true;
    try{await api('vote',{teamId:button.dataset.vote},{loginId:owner.loginId});if(!valid())return;toast('已支持这支队伍，谢谢你的参与。');await renderAudience(version);}
    catch(error){if(valid())toast(error.message);}
    finally{if(button.isConnected && valid())button.disabled=false;}
  });
}
