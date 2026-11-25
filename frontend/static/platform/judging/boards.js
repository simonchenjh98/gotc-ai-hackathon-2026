'use strict';

function boardMarkup(data) {
  const teams = [...(data.teams || [])].sort((a,b) => (a.rank ?? Infinity) - (b.rank ?? Infinity) || Number(a.number) - Number(b.number)), cfg = data.config || {};
  const ready = teams.filter(t=>t.score !== null && t.score !== undefined).length;
  const count = data.stats?.submittedScores ?? teams.reduce((n,t)=>n + Number(t.judgeCount || 0),0);
  const votes = data.stats?.audienceVotes ?? teams.reduce((n,t)=>n + Number(t.audienceVotes || 0),0);
  const screen = document.body.classList.contains('screen-mode');
  const pageCount = Math.max(1, Math.ceil(teams.length / 8));
  const page = screen ? Math.floor((Date.now() - screenStarted) / 12000) % pageCount : 0;
  const visibleTeams = screen ? teams.slice(page * 8, page * 8 + 8) : teams;
  const table = teams.length ? `<div class="table-wrap"><table class="board-table"><thead><tr><th scope="col">排名</th><th scope="col">队伍 / 作品</th><th scope="col" class="track-col">探索方向</th><th scope="col">专业均分</th><th scope="col">有效评审</th><th scope="col">观众票</th></tr></thead><tbody>${visibleTeams.map(t=>`<tr class="${t.rank && t.rank <= 3 ? 'top-rank' : ''}"><td class="rank">${t.rank ? esc(String(t.rank).padStart(2,'0')) : '—'}</td><td><div class="team-name"><span class="team-number">${esc(String(t.number).padStart(2,'0'))}</span>${esc(t.name)}<small>${esc(t.project || '作品名称待完善')}</small></div></td><td class="track-col">${esc(t.track || '—')}</td><td class="board-score">${t.score !== null && t.score !== undefined ? Number(t.score).toFixed(2) : `<span class="board-pending">${cfg.resultsPublished ? '等待评审' : '暂未发布'}</span>`}</td><td>${Number(t.judgeCount || 0)} <span class="board-pending">/ ${Number(cfg.minJudges || 5)}</span></td><td>${Number(t.audienceVotes || 0)}</td></tr>`).join('')}</tbody></table></div>` : empty('暂无可公布的前 12 名成绩',data.stats?.teamCount===0?'本届队伍尚未录入。':!cfg.resultsPublished?'专业成绩暂未发布，请等待主办方通知。':'目前尚无队伍达到有效评委人数门槛。');
  return metrics([[data.stats?.teamCount ?? teams.length,'本届参赛队伍'],[count,'已提交专业评分'],[ready,'公开前 12 名'],[votes,'观众选择票数']]) + `<div class="info-banner">仅展示达到发布条件的前 12 名 · 专业评分满分 100 分 · 每队至少 ${Number(cfg.minJudges || 5)} 位有效评委后显示均分 · 观众票单独展示，不计入专业分。${cfg.scoringOpen ? ' 当前评分已开放。' : ' 当前评分尚未开放或已截止。'}</div>` + table + `<div class="board-foot"><span>${screen ? `第 ${page + 1} / ${pageCount} 屏 · 每 12 秒轮播 · ` : ''}按专业均分降序、同分按队号升序取前 12 名，不追加同分队伍。草稿与回避不计入。</span><span id="board-updated">北京时间 ${esc(new Date(data.serverTime || Date.now()).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false}))}</span></div>`;
}
function progressBoardMarkup(data){
  const teams=[...(data.teams || [])].sort((a,b)=>Number(a.number)-Number(b.number));
  const screen=document.body.classList.contains('screen-mode');
  const pageCount=Math.max(1,Math.ceil(teams.length/8));
  const page=screen?Math.floor((Date.now()-screenStarted)/12000)%pageCount:0;
  const visible=screen?teams.slice(page*8,page*8+8):teams;
  const stage=data.stage?.id || 'exhibition',name=stageName(stage);
  return stageNotice(stage)+metrics([[teams.length,stage==='roadshow'?'本轮路演队伍':'本轮展示队伍'],[teams.reduce((n,t)=>n+Number(t.reviewCount || 0),0),'正式评审记录（未计分）'],['待配置','数值评分规则'],['未实现','排名与成绩发布']])+`<div class="info-banner warning"><strong>${name}进度 · 不是成绩榜</strong><br>当前仅展示正式评审记录数量。分值、汇总、排名与晋级规则待配置，成绩发布尚未实现。${data.window?.canEdit?'本轮评审记录开放中。':'本轮评审记录尚未开放或已截止。'}</div>`+(teams.length?`<div class="table-wrap"><table class="board-table"><thead><tr><th scope="col">队伍 / 作品</th><th scope="col" class="track-col">探索方向</th><th scope="col">正式评审记录</th><th scope="col">成绩 / 排名</th></tr></thead><tbody>${visible.map(t=>`<tr><td><div class="team-name"><span class="team-number">${esc(String(t.number).padStart(2,'0'))}</span>${esc(t.name)}<small>${esc(t.project || '作品名称待完善')}</small></div></td><td class="track-col">${esc(t.track || '—')}</td><td>${Number(t.reviewCount || 0)} <span class="board-pending">份 · 未计分</span></td><td><span class="board-pending">规则待配置 / 未实现</span></td></tr>`).join('')}</tbody></table></div>`:empty(`本轮还没有${stage==='roadshow'?'路演':'展示'}队伍`,'组织方配置本轮工作名单后，评审进度会出现在这里。'))+`<div class="board-foot"><span>${screen?`第 ${page+1} / ${pageCount} 屏 · 每 12 秒轮播 · `:''}草稿与回避不计入正式评审记录数。</span><span>北京时间 ${esc(beijingTime(data.window?.serverTime || Date.now()))}</span></div>`;
}
function watchBoard(version,path,markup,unavailableTitle){
  const status=document.getElementById('live-status'),content=document.getElementById('board-content');
  const offline=()=>window.navigator?.onLine===false;
  function connection(message,unavailable){
    status.classList.toggle('offline',unavailable);
    status.innerHTML='<span class="live-dot"></span>'+message;
  }
  function showOffline(){
    connection('网络已断开 · 恢复后自动更新',true);
    if(!lastBoard)content.innerHTML=empty(unavailableTitle,'网络恢复后将自动重新读取。');
  }
  if(offline())showOffline();
  boardPoller=makeLivePoller({
    read:signal=>api(path,undefined,{signal}),
    apply(data){lastBoard=data;content.innerHTML=markup(data);},
    status(result,error){
      if(result==='updated'){connection('已连接 · 每 2 秒更新',false);return;}
      const message=result==='auth'?'当前身份无法读取 · 请重新登录':'连接中断 · 正在重试';
      connection(message,true);
      if(result==='auth'){lastBoard=null;content.innerHTML=empty(unavailableTitle,message);}
      else if(!lastBoard)content.innerHTML=empty(unavailableTitle,esc(error.message));
    },
    alive:()=>routeVersion===version,
    isHidden:()=>document.hidden || offline(),
    subscribe(refresh){
      function changed(){if(offline())showOffline();refresh();}
      document.addEventListener('visibilitychange',changed);
      window.addEventListener('online',changed);
      window.addEventListener('offline',changed);
      return ()=>{
        document.removeEventListener('visibilitychange',changed);
        window.removeEventListener('online',changed);
        window.removeEventListener('offline',changed);
      };
    }
  });
  return boardPoller.refresh();
}
async function renderExhibitionBoard(version){
  const stage=stageForRoute(),name=stageName(stage);
  root.innerHTML=heading(`LIVE / ${name}进度`,`${name}进度`,'查看本轮正式评审记录数量。计分与成绩发布尚未实现。','<span class="live-indicator" id="live-status"><span class="live-dot"></span>连接中…</span><button class="btn secondary small" id="screen-toggle" aria-pressed="false">大屏模式 ↗</button>')+'<div id="board-content"><div class="loading-state">正在读取本轮评审进度…</div></div>';
  document.getElementById('screen-toggle').onclick=()=>{document.getElementById('screen-toggle').setAttribute('aria-pressed',String(document.body.classList.toggle('screen-mode')));screenStarted=Date.now();if(lastBoard)document.getElementById('board-content').innerHTML=progressBoardMarkup(lastBoard);document.getElementById('screen-toggle').textContent=document.body.classList.contains('screen-mode')?'退出大屏模式':'大屏模式 ↗';};
  await watchBoard(version,`judging/${stage}/board`,progressBoardMarkup,'暂时无法读取进度');
}
async function renderBoard(version) {
  root.innerHTML = heading('LIVE / GOTC 2026','每一格，都在发生。','百格成城 · 实时成绩看板', '<span class="live-indicator" id="live-status"><span class="live-dot"></span>连接中…</span><button class="btn secondary small" id="screen-toggle" aria-pressed="false">大屏模式 ↗</button>') + '<div id="board-content"><div class="loading-state">正在读取本届活动数据…</div></div>';
  document.getElementById('screen-toggle').onclick = () => { document.getElementById('screen-toggle').setAttribute('aria-pressed',String(document.body.classList.toggle('screen-mode'))); screenStarted = Date.now(); if(lastBoard)document.getElementById('board-content').innerHTML=boardMarkup(lastBoard); document.getElementById('screen-toggle').textContent = document.body.classList.contains('screen-mode') ? '退出大屏模式' : '大屏模式 ↗'; };
  await watchBoard(version,'board',boardMarkup,'暂时无法读取数据');
}
