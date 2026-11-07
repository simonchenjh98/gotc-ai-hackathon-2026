'use strict';

// Read-only finals overview. The server owns ranking, ties and award allocation.
let activeAdminFinalsDashboard=null;
const adminFinalsAwardDefaults=[
  {id:'creativity',name:'最佳创意奖'},
  {id:'social',name:'最具社会关怀奖'},
  {id:'technical',name:'最佳技术呈现奖'},
  {id:'application',name:'最具应用潜力奖'},
  {id:'crossdisciplinary',name:'最佳 AI 跨界探索奖'},
  {id:'opensource',name:'最佳开源贡献奖'}
];
const adminFinalsCount=value=>Number.isSafeInteger(value) && value>=0?value:0;
function adminFinalsTeamName(team){return `${String(team.number).padStart(2,'0')} · ${team.name || '未命名队伍'}`;}

function adminFinalsDashboardModel(data){
  if(!data || !data.config || !data.summary || !Array.isArray(data.summary.teams) || !Array.isArray(data.summary.awards))throw new Error('决赛统计格式不正确');
  const {config,summary}=data,teams=summary.teams,byId=new Map(teams.map(team=>[team.id,team]));
  const expectedJudges=Number.isSafeInteger(summary.expectedJudges) && summary.expectedJudges>0?summary.expectedJudges:8;
  const stateFinal=config.published===true && summary.final===true && config.publicationStale!==true;
  const confirmed=summary.awardsConfirmed===true && config.open!==true;
  const name=id=>byId.has(id)?adminFinalsTeamName(byId.get(id)):'队伍信息待核对';
  // Keep the exact order/rank returned by the server: two displayed rounded
  // averages may differ before rounding and must not be re-ranked here.
  const teamsHTML=teams.map(team=>{
    const scored=typeof team.score==='number' && Number.isFinite(team.score);
    const rank=Number.isSafeInteger(team.rank) && team.rank>0?team.rank:null;
    const count=adminFinalsCount(team.scoreCount),recused=adminFinalsCount(team.recusedCount);
    return `<li class="afd-team${rank===1?' afd-team-first':''}"><span class="afd-rank" aria-label="${rank?'排名 '+rank:'暂无排名'}">${rank ?? '—'}</span><div class="afd-team-detail"><strong>${esc(adminFinalsTeamName(team))}</strong>${team.project?`<span class="afd-project">${esc(team.project)}</span>`:''}<small>${count} / ${expectedJudges} 位已评分${recused?` · ${recused} 位回避`:''}</small><small class="${team.complete?'afd-complete':'afd-pending'}">${team.eligible===false?'已失去决赛资格':team.complete?'评分已收齐':'评分尚未收齐'}</small></div><span class="afd-score${scored?'':' afd-unscored'}">${scored?team.score.toFixed(2):'待评分'}</span></li>`;
  }).join('') || '<li class="afd-empty">尚未配置决赛入围队伍。请在决赛控制台选择本次的 12 支队伍。</li>';
  const dimensions=Array.isArray(data.dimensions)?data.dimensions:[];
  const awards=adminFinalsAwardDefaults.map((fallback,index)=>{
    const dimension=dimensions.find(item=>item.id===fallback.id) || dimensions[index];
    const award=summary.awards.find(item=>item.id===fallback.id) || {};
    const votes=Array.isArray(award.votes)?award.votes.filter(item=>adminFinalsCount(item.count)>0):[];
    const winners=confirmed && Array.isArray(award.confirmedWinners)?award.confirmedWinners:(Array.isArray(award.suggestedWinners)?award.suggestedWinners:[]);
    const resultLabel=stateFinal?'最终获奖':confirmed?'已确认 · 尚未发布':'暂定获奖建议';
    const resultHTML=`<span class="afd-result-label">${resultLabel}</span>${winners.length?`<ul>${winners.map(id=>`<li>${esc(name(id))}</li>`).join('')}</ul>${winners.length>1?'<small>同票共同获奖</small>':''}`:`<span class="afd-result-empty">${stateFinal?'本奖项空缺':'暂无有效获奖候选'}</span>`}`;
    const votesHTML=votes.map(vote=>`<li><span>${esc(name(vote.teamId))}</span><strong>${adminFinalsCount(vote.count)}<small>票</small></strong></li>`).join('') || '<li class="afd-empty">尚无正式提名票</li>';
    return {id:fallback.id,name:award.name || dimension?.award || fallback.name,votesHTML,resultHTML,totalVotes:adminFinalsCount(award.totalVotes)};
  });
  const dimensionText=dimensions.length===6?dimensions.map(item=>`${item.name} ${item.max} 分`).join('、'):'创意 20 分、社会关怀 15 分、技术呈现 20 分、应用潜力 20 分、AI 跨界探索 15 分、开源贡献 10 分';
  const rulesHTML=`<ol><li><strong>成绩排名：</strong>${esc(dimensionText)}，总分 100 分。按有效正式评分的平均分从高到低排列；同分并列，后续名次跳号。草稿、回避和未提交不计分，未提交不作 0 分。</li><li><strong>评分收齐：</strong>每队须完成 ${expectedJudges} 位评委的评分或回避记录，且至少有 1 份正式评分。未收齐时显示已有正式评分的暂定均分。</li><li><strong>专项提名：</strong>提名票与总分独立；每位评委每奖最多提名 1 队，也可弃权。每奖最多 ${expectedJudges} 票，六奖最多 ${expectedJudges*6} 票；只统计正式提交的提名表。</li><li><strong>奖项归属：</strong>按右侧六奖顺序分配，排除已获前序奖项的队伍，再取其余队伍最高正票；同票共同获奖，每队最多 1 个专项奖，无有效候选则空缺。原始提名票始终保留展示。</li><li><strong>最终发布：</strong>关闭评分、收齐评分及 ${expectedJudges} 份提名表，并由组织方确认奖项后才能发布。发布前显示暂定结果；评分或名单变化后的失效发布须重新核对。</li></ol>`;
  return {teamsHTML,awards,rulesHTML,stateLabel:stateFinal?'已发布 · 最终结果':'暂定结果 · 仅组织方可见',stateFinal,teamCount:teams.length,completedTeams:teams.filter(team=>team.complete===true).length,submittedBallots:adminFinalsCount(summary.submittedBallots),expectedJudges,notice:config.publicationStale===true?'评分或名单已变化，原发布结果已失效。请到决赛控制台核对并重新发布。':''};
}

function adminFinalsDashboardMarkup(){
  return `<section id="admin-finals-dashboard" class="admin-finals-dashboard" aria-labelledby="afd-heading"><div class="afd-heading"><div><p class="afd-eyebrow">FINALS / 决赛实时总览</p><h2 id="afd-heading">决赛成绩与专项奖提名</h2><p class="afd-counts" data-afd-counts>正在读取决赛统计…</p></div><div class="afd-tools"><span class="afd-state" data-afd-state>等待同步</span><button type="button" class="btn secondary small" data-afd-refresh>刷新统计</button><a href="#admin-finals" class="afd-console-link">决赛控制台 ↗</a></div></div><p class="afd-notice" data-afd-notice role="alert" hidden></p><div class="afd-grid"><section class="afd-ranking" aria-labelledby="afd-ranking-heading"><div class="afd-column-heading"><h3 id="afd-ranking-heading">全场排名</h3><span>均分 / 100</span></div><ol class="afd-teams afd-scroll" data-afd-teams tabindex="0" aria-label="决赛队伍排名，可上下滚动"><li class="afd-empty">正在读取成绩…</li></ol></section><section class="afd-awards" aria-label="六项专项奖提名"><div class="afd-awards-heading"><h3>专项奖提名</h3><span>按正式提名票展示 · 六奖依次分配</span></div><div class="afd-awards-scroll" tabindex="0" aria-label="六项专项奖，可左右滚动"><div class="afd-award-grid">${adminFinalsAwardDefaults.map((award,index)=>`<article class="afd-award" data-afd-award-index="${index}"><div class="afd-award-heading"><span class="afd-award-number">0${index+1}</span><h4 data-afd-award-name>${esc(award.name)}</h4><p data-afd-total-votes>等待提名统计</p></div><ul class="afd-votes afd-scroll" data-afd-votes tabindex="0" aria-label="${esc(award.name)}提名队伍，可上下滚动"><li class="afd-empty">正在读取提名…</li></ul><div class="afd-result" data-afd-result></div></article>`).join('')}</div></div></section></div><div class="afd-footer"><span class="afd-sync" data-afd-sync role="status" aria-live="polite">正在连接 · 每 2 秒自动同步</span><details class="afd-rules"><summary>查看计分与提名规则</summary><div data-afd-rules></div></details></div></section>`;
}

function patchAdminFinalsDashboard(panel,data){
  const model=adminFinalsDashboardModel(data);
  const html=(node,value)=>{
    if(!node || node.afdMarkup===value)return;
    const top=node.scrollTop,left=node.scrollLeft;
    node.innerHTML=value;node.afdMarkup=value;node.scrollTop=top;node.scrollLeft=left;
  };
  const text=(selector,value)=>{const node=panel.querySelector(selector);if(node && node.textContent!==value)node.textContent=value;};
  html(panel.querySelector('[data-afd-teams]'),model.teamsHTML);
  model.awards.forEach((award,index)=>{
    const card=panel.querySelector(`[data-afd-award-index="${index}"]`);if(!card)return;
    const title=card.querySelector('[data-afd-award-name]');if(title && title.textContent!==award.name)title.textContent=award.name;
    const total=card.querySelector('[data-afd-total-votes]'),totalLabel=`正式提名 ${award.totalVotes} 票`;if(total && total.textContent!==totalLabel)total.textContent=totalLabel;
    html(card.querySelector('[data-afd-votes]'),award.votesHTML);
    html(card.querySelector('[data-afd-result]'),award.resultHTML);
  });
  html(panel.querySelector('[data-afd-rules]'),model.rulesHTML);
  text('[data-afd-state]',model.stateLabel);
  panel.querySelector('[data-afd-state]')?.classList.toggle('afd-final',model.stateFinal);
  text('[data-afd-counts]',`${model.teamCount} 支决赛队伍 · 评分收齐 ${model.completedTeams}/${model.teamCount} 队 · 提名表 ${model.submittedBallots}/${model.expectedJudges} 份`);
  const notice=panel.querySelector('[data-afd-notice]');if(notice){notice.hidden=!model.notice;if(notice.textContent!==model.notice)notice.textContent=model.notice;}
  return model;
}

function disposeAdminFinalsDashboard(){activeAdminFinalsDashboard?.dispose();}

function mountAdminFinalsDashboard(version){
  disposeAdminFinalsDashboard();
  const owner=session,loginId=owner.loginId,headingNode=root.querySelector('.page-heading');
  const loggedOut=()=>typeof browserLogoutOwner==='function' && browserLogoutOwner()===loginId;
  if(route!=='admin' || version!==routeVersion || owner.role!=='admin' || !owner.loginId || !ownsBrowserLogin(owner) || loggedOut() || !headingNode)return null;
  headingNode.insertAdjacentHTML('afterend',adminFinalsDashboardMarkup());
  const panel=root.querySelector('#admin-finals-dashboard');if(!panel)return null;
  const view={owner,version,panel,poller:null,disposed:false,suspended:false,lastSync:null};
  activeAdminFinalsDashboard=view;
  const clear=message=>{panel.innerHTML=message?`<p class="afd-auth" role="alert">${esc(message)}</p>`:'';};
  view.dispose=message=>{
    if(view.disposed)return;view.disposed=true;view.poller?.stop();clear(message);
    if(activeAdminFinalsDashboard===view)activeAdminFinalsDashboard=null;
  };
  view.valid=()=>{
    if(view.disposed)return false;
    if(activeAdminFinalsDashboard!==view || !panel.isConnected || route!=='admin' || routeVersion!==version){view.dispose();return false;}
    if(session!==owner || owner.role!=='admin' || !loginId || owner.loginId!==loginId || !ownsBrowserLogin(owner) || loggedOut()){
      view.dispose('登录身份已变化，决赛统计已清除。请重新登录组织方后台。');return false;
    }
    return true;
  };
  const status=kind=>{
    if(!view.valid())return;
    if(kind==='auth'){view.dispose('登录已失效或无权访问，决赛统计已清除。请重新登录组织方后台。');return;}
    if(view.suspended)return;
    const node=panel.querySelector('[data-afd-sync]');if(!node)return;
    if(kind==='updated')view.lastSync=new Date();
    const time=view.lastSync?.toLocaleTimeString('zh-CN',{hour12:false,hour:'2-digit',minute:'2-digit',second:'2-digit'});
    node.textContent=kind==='error'?(time?`同步暂不可用 · 显示 ${time} 的结果，正在重试`:'尚未取得决赛数据 · 正在重试'):`最近同步 ${time} · 每 2 秒自动同步`;
    node.classList.toggle('afd-sync-error',kind==='error');
  };
  const clearForCache=()=>{
    // Do not keep private scores in a page restored from the browser cache.
    for(const node of panel.querySelectorAll('[data-afd-teams], [data-afd-votes], [data-afd-result]')){node.innerHTML='';node.afdMarkup='';}
    for(const selector of ['[data-afd-counts]','[data-afd-total-votes]','[data-afd-state]','[data-afd-sync]'])for(const node of panel.querySelectorAll(selector))node.textContent='等待重新同步';
    panel.querySelector('[data-afd-state]')?.classList.toggle('afd-final',false);
    const notice=panel.querySelector('[data-afd-notice]');if(notice){notice.textContent='';notice.hidden=true;}
    view.lastSync=null;
  };
  view.poller=makeFinalsPoller({
    read:async signal=>{
      if(!view.valid() || signal.aborted)throw new Error('当前组织方页面已失效');
      const result=await api('admin/finals',undefined,{signal,loginId});
      if(!view.valid() || signal.aborted)throw new Error('当前组织方页面已失效');
      return result;
    },
    apply:data=>{if(view.valid())patchAdminFinalsDashboard(panel,data);},status,
    alive:()=>view.valid(),isHidden:()=>document.hidden || view.suspended,
    subscribe:kick=>{
      const check=()=>{if(view.valid())kick();};
      const pagehide=()=>{if(!view.valid())return;view.suspended=true;clearForCache();kick();};
      const pageshow=()=>{if(!view.valid())return;view.suspended=false;kick();};
      document.addEventListener('visibilitychange',check);
      window.addEventListener('storage',check);window.addEventListener('focus',check);
      window.addEventListener('pagehide',pagehide);window.addEventListener('pageshow',pageshow);
      return ()=>{
        document.removeEventListener('visibilitychange',check);
        window.removeEventListener('storage',check);window.removeEventListener('focus',check);
        window.removeEventListener('pagehide',pagehide);window.removeEventListener('pageshow',pageshow);
      };
    }
  });
  panel.querySelector('[data-afd-refresh]').onclick=()=>{if(view.valid())view.poller.kick();};
  view.poller.start();view.poller.refresh();
  return view;
}
