'use strict';

async function renderTeamScore(version) {
  const owner=session;
  if (!ownsBrowserLogin(owner)) {
    root.innerHTML='<p class="info-banner" role="status">当前登录队伍已切换，请刷新页面重新查分。</p>';
    return;
  }
  const data=await api('team/score',undefined,{loginId:owner.loginId});
  if (version!==routeVersion || route!=='team-score' || session!==owner || !ownsBrowserLogin(owner)) return;
  let result;
  if (data.state==='published' && Number.isFinite(data.score)) {
    result=`<p class="own-score-value">${data.score.toFixed(2)} <span>/ 100</span></p><h2>六维专业均分</h2><dl class="own-score-dimensions">${(data.dimensions || []).map(d=>`<div><dt>${esc(d.name)}</dt><dd>${Number(d.score).toFixed(2)} <span>/ ${Number(d.max)}</span></dd></div>`).join('')}</dl><h2>匿名专业评语</h2>${data.feedback?.length?`<ol class="own-score-feedback">${data.feedback.map(text=>`<li class="preserve-text">${esc(text)}</li>`).join('')}</ol>`:'<p>暂无可分享的评语。</p>'}<p class="small-note">仅展示评委知情确认分享的正式非空评语。总分和各维度均为全部有效正式评分的平均值。</p>`;
  } else {
    const message=data.state==='awaiting_scores'?'有效评委人数尚未达到公布门槛，请稍后查询。':'成绩尚未发布，请等待主办方通知。';
    result=`<p role="status">${message}</p>`;
  }
  const stageResults=(data.stages || []).map(stage=>{
    if(stage.state!=='published' || !Number.isFinite(stage.score)){
      const message=stage.state==='awaiting_scores'?`正式评分人数尚未达到${Number(stage.minimumScores || 1)}人门槛。`:'该阶段成绩尚未由主办方确认发布。';
      return `<article class="own-stage-score"><h3>${esc(stage.name)}成绩</h3><p role="status">${message}</p></article>`;
    }
    const dimensions=(stage.dimensions || []).map(d=>`<div><dt>${esc(d.name)}</dt><dd>${Number(d.score).toFixed(2)} <span>/ ${Number(d.max)}</span></dd></div>`).join('');
    const awards=stage.awards?.length?`<p><strong>本队专项奖：</strong>${stage.awards.map(esc).join('、')}</p>`:'';
    return `<article class="own-stage-score"><h3>${esc(stage.name)}成绩</h3><p class="own-score-value">${Number(stage.score).toFixed(2)} <span>/ 100</span></p><p class="small-note">${Number(stage.scoreCount || 0)} 位评委正式评分；仅显示本队结果，不提供全队排名。</p><dl class="own-score-dimensions">${dimensions}</dl>${awards}</article>`;
  }).join('');
  const stages=stageResults?`<h2>路演 / 决赛成绩</h2>${stageResults}<h2>其他专业评分</h2>`:'<h2>专业评分总分</h2>';
  root.innerHTML=`<section class="surface own-score"><h1>本队查分</h1><p>${esc(teamLabel(data.team))}</p>${stages}${result}<p class="small-note">此页面只读取当前登录队伍，不能查询其他队伍或全队排名。</p><button type="button" id="refresh-team-score" class="btn secondary small">刷新成绩</button></section>`;
  document.getElementById('refresh-team-score').onclick=buttonAction(()=>renderRoute());
}
