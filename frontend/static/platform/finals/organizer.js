'use strict';

async function renderFinalsAdmin(version){
  const view=createFinalsView(version,'admin'),data=await finalsRequest(view,'admin/finals');if(!view.valid())return;
  view.data=data;view.config=data.config;
  const cfg=data.config;
  finalsShell(view,'FINALS / 组织方控制台','决赛控制台','独立管理7位决赛评委、决赛队伍、六维评分和六项奖项提名。',`<div class="finals-admin-tools"><button class="btn secondary small" data-finals-reload>重新载入设置</button><span class="small-note" data-finals-sync role="status">每2秒同步统计</span></div><p class="info-banner warning" data-finals-config-notice hidden></p><div class="finals-admin-layout"><section class="surface"><h2>01 / 决赛名单</h2><p class="small-note">${cfg.confirmedFinalistCount?`组织方已确认 ${Number(cfg.confirmedFinalistCount)} 支决赛队伍，按已确认的路演成绩顺序展示。`:'默认在确认路演成绩后自动同步前12名；已由组织方单独确认的入围名单优先。'}首次开放决赛前请核对名单，开放后名单锁定；评委须恰好7位。</p><form data-finals-roster><fieldset class="finals-fieldset" ${cfg.rosterLocked?'disabled':''}><h3>路演成绩自动同步的决赛入围队伍</h3><div class="finals-roster" data-finals-team-list>${scoringTeamChoicesMarkup(data.availableTeams || [],cfg.teamIds || [])}</div><h3>决赛评委 · 必须恰好7位</h3><div class="finals-roster" data-finals-judge-list>${finalsJudgeChecks(data.availableJudges || [],cfg.judgeIds || [])}</div><button class="btn" type="submit" data-finals-save>保存决赛入围名单</button></fieldset><button class="btn secondary small" type="button" data-finals-refresh-teams>刷新队伍信息（保留勾选）</button><p class="form-error" role="status"></p><div data-finals-recovery></div></form><form data-finals-create-judges><p class="small-note">尚未配置决赛评委时，可建立7个评委席位。评委打开决赛入口，选择自己的姓名或席位即可评分。</p><button class="btn secondary" type="submit" data-finals-save ${cfg.rosterLocked || cfg.judgeIds?.length?'disabled':''}>建立7个决赛评委席位</button><p class="form-error" role="status"></p><div data-finals-recovery></div></form></section><section class="surface"><h2>02 / 开放与发布</h2><form data-finals-settings><label class="toggle-line"><input type="checkbox" name="open" ${cfg.open?'checked':''}>开放决赛评分与提名</label><label class="toggle-line"><input type="checkbox" name="published" ${cfg.published?'checked':''}>确认最终成绩和获奖队伍</label><p class="small-note">发布前必须关闭评分、收齐7位评委的提名表，并完成所有队伍的评分或回避。重新开放会取消确认。全队汇总与排名仅在组织方后台显示；确认发布后，选手只可查看本队结果。</p><button class="btn" type="submit" data-finals-save>保存开放与确认设置</button><p class="form-error" role="status"></p><div data-finals-recovery></div></form><h3>六维评分参考</h3><dl class="finals-rubrics">${data.dimensions.map(d=>`<div><dt>${esc(d.name)} · ${Number(d.max)}分</dt><dd>${esc(finalsGuidance[d.id] || '')}</dd></div>`).join('')}</dl><p class="small-note">提名票不影响100分总分；每位评委每奖最多1票，每奖最多7票，六奖合计最多42票。按固定奖项顺序排除已获奖队伍后，最高正票同票共同获奖；每队最多1奖，无有效候选则无人获奖。</p></section></div><div data-finals-summary></div>`);
  view.container.querySelector('[data-finals-reload]').onclick=buttonAction(()=>finalsConfirmReload(view));
  finalsInstallAdmin(view);
  const update=latest=>{
    if(Number(latest.config.revision)<Number(view.config.revision))return;
    view.latest=latest;view.updateRosterCandidates?.(latest);finalsPatchSummary(view,latest);
    const notice=view.container.querySelector('[data-finals-config-notice]');
    notice.hidden=latest.config.revision===view.config.revision;
    if(!notice.hidden)notice.textContent='决赛设置已在其他页面变化。统计已同步；当前表单输入已保留。请先复制需要保留的内容，再重新载入设置。';
  };
  update(data);finalsStartPolling(view,'admin/finals',update);
}

function finalsJudgeChecks(judges,selected){return judges.map(judge=>`<label class="finals-check"><input type="checkbox" name="judgeIds" value="${esc(judge.id)}" ${selected.includes(judge.id)?'checked':''} ${judge.active===false?'disabled':''}><span>${esc(judge.name)}${judge.active===false?'（已停用）':''}</span></label>`).join('') || '<p>暂无评委席位，可在下方建立7个决赛评委席位。</p>';}

function finalsInstallAdmin(view){
  const roster=view.container.querySelector('[data-finals-roster]'),settings=view.container.querySelector('[data-finals-settings]'),create=view.container.querySelector('[data-finals-create-judges]');
  for(const form of [roster,settings,create]){finalsTrack(view,form);form.finalsRevision=view.config.revision;}
  function buttons(){
    const locked=view.latestRosterLocked ?? view.config.rosterLocked;
    roster.querySelector('fieldset').disabled=!!locked || !!view.judgesGenerating;
    for(const form of [roster,settings,create]){const button=form.querySelector('[data-finals-save]');button.disabled=form.finalsBusy || form.finalsBlocked || (form!==settings && locked) || (form===roster && (view.judgesGenerating || view.candidateLoading)) || (form===create && !!(view.latestJudgeIds ?? view.config.judgeIds)?.length);}
  }
  for(const form of [roster,settings,create])form.finalsRefreshButtons=buttons;
  function saved(form,result){
    const cfg=result.config;form.finalsRevision=cfg.revision;view.config=cfg;view.latestRosterLocked=cfg.rosterLocked;view.latestJudgeIds=cfg.judgeIds;
    // Clean sibling controls can adopt this successful write's revision; dirty forms keep theirs.
    for(const other of [roster,settings,create])if(other!==form && !dirtyAdminForms.has(other) && !other.finalsBusy)other.finalsRevision=cfg.revision;
    buttons();view.poller?.kick();
  }
  roster.onsubmit=buttonAction(event=>{
    event.preventDefault();if(!view.valid() || (view.latestRosterLocked ?? view.config.rosterLocked) || view.candidateLoading)return;
    const teamIds=[...roster.querySelectorAll('input[name=teamIds]:checked')].map(input=>input.value),judgeIds=[...roster.querySelectorAll('input[name=judgeIds]:checked')].map(input=>input.value);
    if(!teamIds.length || judgeIds.length!==Number(cfg.judgeCount || 7)){finalsMessage(roster,'请选择至少1支决赛队伍，以及恰好7位不同评委。');return;}
    return finalsSave(view,roster,'admin/finals/roster',{expectedRevision:roster.finalsRevision,teamIds,judgeIds},result=>saved(roster,result));
  });
  settings.onsubmit=buttonAction(event=>{
    event.preventDefault();const open=settings.elements.open.checked,published=settings.elements.published.checked;
    if(open && published){finalsMessage(settings,'请先关闭评分与提名，再发布最终结果。');return;}
    return finalsSave(view,settings,'admin/finals/settings',{expectedRevision:settings.finalsRevision,open,published},result=>saved(settings,result));
  });
  create.onsubmit=buttonAction(event=>{
    event.preventDefault();if(!view.valid() || (view.latestRosterLocked ?? view.config.rosterLocked) || (view.latestJudgeIds ?? view.config.judgeIds)?.length || create.finalsBusy || roster.finalsBusy || view.judgesGenerating)return;
    if(dirtyAdminForms.has(roster)){finalsMessage(create,'请先保存或复制名单中的未保存输入，再重新载入后建立评委席位。');return;}
    // Replacing the judge list is safe only while its whole roster is held read-only.
    // Team selections remain in the same DOM, and the independent settings form stays editable.
    view.judgesGenerating=true;buttons();
    return finalsSave(view,create,'admin/finals/judges',{expectedRevision:create.finalsRevision},result=>{
      saved(create,result);
      const judges=result.judges || [];
      view.container.querySelector('[data-finals-judge-list]').innerHTML=finalsJudgeChecks(judges,result.config.judgeIds || []);
      roster.finalsRevision=result.config.revision;
    }).finally(()=>{view.judgesGenerating=false;if(view.valid() && roster.isConnected)buttons();});
  });
  const candidateHost=view.container.querySelector('[data-finals-team-list]'),candidateRefresh=view.container.querySelector('[data-finals-refresh-teams]');
  view.updateRosterCandidates=data=>{
    if(!view.valid() || !candidateHost || data.config.runId!==view.config.runId || Number(data.config.revision)<Number(roster.finalsRevision))return;
    view.latestRosterLocked=data.config.rosterLocked;view.latestJudgeIds=data.config.judgeIds;
    if(!roster.finalsBusy && !view.judgesGenerating){
      const dirty=dirtyAdminForms.has(roster);
      scoringPatchTeamChoices(candidateHost,data.availableTeams || [],dirty?scoringCheckedTeams(roster):(data.config.teamIds || []));
      if(!dirty){
        const host=view.container.querySelector('[data-finals-judge-list]'),markup=finalsJudgeChecks(data.availableJudges || [],data.config.judgeIds || []);
        if(host.finalsMarkup!==markup){host.innerHTML=markup;host.finalsMarkup=markup;}
        roster.finalsRevision=data.config.revision;
      }
    }
    buttons();
  };
  if(candidateRefresh)candidateRefresh.onclick=buttonAction(async()=>{
    if(!view.valid() || view.candidateLoading || roster.finalsBusy || view.judgesGenerating)return;
    view.candidateLoading=true;candidateRefresh.disabled=true;buttons();
    try{const data=await finalsRequest(view,'admin/finals');if(view.valid())view.updateRosterCandidates(data);}
    catch(error){if(view.current())finalsMessage(roster,error.message+' 当前勾选已保留。');}
    finally{view.candidateLoading=false;if(view.valid()){candidateRefresh.disabled=false;buttons();}}
  });
  buttons();
}

;
// Organizer finals dashboard bundle.
'use strict';

// Read-only finals overview. The server owns ranking, ties and award allocation.
