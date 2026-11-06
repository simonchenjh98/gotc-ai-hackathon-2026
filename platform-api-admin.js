// AI API console at #admin-api, laid out like sub2api's admin: sidebar pages, stat cards, an accounts table with
// add/edit/test dialogs, usage, ops and audit. Unused sub2api parts (users, groups, payments, proxies…) are left out.
// ponytail: one account per platform, backed by the gateway's existing openai/anthropic upstream slots.
const apiPlatforms={
  openai:{label:'OpenAI 兼容',endpoint:'/v1/chat/completions · /v1/responses',official:'https://api.openai.com/v1',keyHint:'sk-...',alias:'gpt-*'},
  anthropic:{label:'Anthropic',endpoint:'/v1/messages',official:'https://api.anthropic.com/v1',keyHint:'sk-ant-...',alias:'claude-*'}};
const apiPresets=[['Gitee AI（模力方舟）','openai','https://ai.gitee.com/v1'],['DeepSeek','openai','https://api.deepseek.com/v1'],
  ['Gitee AI（Anthropic 格式，供 Claude Code）','anthropic','https://ai.gitee.com/v1'],['Anthropic 官方','anthropic','https://api.anthropic.com/v1']];
const apiPages=[['dashboard','仪表盘','AI API 概览与今日统计'],['ops','运维监控','实时性能、失败原因与失败请求'],['teams','队伍管理','为队伍开通或停用 API，查看各队消耗'],
  ['accounts','账号管理','模型上游账号：接口地址、API Key 与可用模型'],['usage','使用记录','按队伍、密钥、模型查看用量与调用明细'],['audit','操作日志','AI API 相关的管理操作'],['settings','系统设置','密钥上限、限流、公告与调用内容保存']];
const priceColumns=['输入','输出','5分钟缓存写入','缓存读取','1小时缓存写入'];
const msText=v=>v===null || v===undefined?'—':`${Number(v)}ms`;
const apiTable=(head,rows,empty='暂无数据')=>`<div class="table-wrap"><table class="board-table api-table"><thead><tr>${head.map(h=>`<th>${h}</th>`).join('')}</tr></thead><tbody>${rows.map(r=>`<tr>${r.map(c=>`<td>${c}</td>`).join('')}</tr>`).join('') || `<tr><td colspan="${head.length}">${empty}</td></tr>`}</tbody></table></div>`;
const apiCards=cards=>`<div class="api-cards">${cards.map(([label,value,sub])=>`<div class="api-stat"><span>${label}</span><strong>${value}</strong><small>${sub}</small></div>`).join('')}</div>`;
const apiBadge=(text,tone='gray')=>`<span class="api-badge ${tone}">${esc(text)}</span>`;
const apiAccountName=(kind,u)=>u.name || apiPresets.find(([,k,url])=>k===kind && url===u.url)?.[0] || (()=>{try{return new URL(u.url).host;}catch{return u.url;}})();

// Price cells -> [] (default price) or [input, output(, write5m, read(, write1h))]; blanks are only allowed at the end.
function apiPriceRow(cells,model){
  const text=cells.map(v=>String(v ?? '').trim()),used=text.reduce((n,v,i)=>v?i+1:n,0),prices=text.slice(0,used).map(Number);
  if(text.slice(0,used).some(v=>!v) || ![0,2,4,5].includes(used) || prices.some(n=>!Number.isFinite(n) || n<0))
    throw new Error(`${model} 的单价不正确：请只填输入、输出价，或再加 5分钟缓存写入、缓存读取[、1小时缓存写入]价`);
  return prices;
}
// Upstream save body: the edited account plus the other saved ones (an empty key keeps their saved key).
function apiAccountPayload(saved,kind,draft){
  if(!draft.models.length)throw new Error('请至少选择一个模型：队伍只能调用白名单中的模型');
  const models=Object.fromEntries(draft.models.map(m=>[m,apiPriceRow(draft.prices[m] || [],m)])),aliases={};
  for(const [from,to] of draft.aliases){
    if(!from.trim())continue;
    if(!models[to])throw new Error(`映射 ${from} 的实际模型不在白名单中`);
    aliases[from.trim()]=to;
  }
  const account={name:draft.name.trim(),url:draft.url.trim() || apiPlatforms[kind].official,key:draft.key.trim(),models,aliases};
  // The dialog edits input/output only; cache prices saved earlier (e.g. from gateway.json) are kept.
  if(draft.defaultPrice.some(v=>String(v).trim()))account.defaultPrice=apiPriceRow([...draft.defaultPrice.slice(0,2),...(saved[kind]?.defaultPrice || []).slice(2)],'默认单价');
  return {upstreams:{...apiOtherAccounts(saved,kind),[kind]:account}};
}
function apiOtherAccounts(saved,kind){return Object.fromEntries(Object.entries(saved).filter(([k])=>k!==kind).map(([k,u])=>[k,{name:u.name,url:u.url,key:'',models:u.models,aliases:u.aliases,defaultPrice:u.defaultPrice}]));}

async function renderApiAdmin(version){
  const sub=location.hash.split('/')[1],page=apiPages.find(p=>p[0]===sub) || apiPages[0];
  const [event,data]=await Promise.all([api('admin'),api('admin/keys')]);
  if(route!=='admin-api' || version!==routeVersion)return;
  data.upstreams ||= {};
  root.innerHTML=`<div class="api-console"><nav class="api-sidebar" aria-label="AI API 管理"><div class="api-brand"><strong>AI API</strong><small>GOTC 2026 内置网关</small></div>${apiPages.map(([id,name])=>`<a href="#admin-api/${id}" ${id===page[0]?'aria-current="page"':''}>${name}</a>`).join('')}<a class="api-back" href="#admin">← 返回活动后台</a></nav><div class="api-main"><header class="api-header"><div><h1>${page[1]}</h1><p>${page[2]}</p></div><div class="form-actions" id="api-actions"></div></header><div id="api-page"></div></div></div>`;
  const c={version,data,teams:event.teams || [],box:document.getElementById('api-page'),actions:document.getElementById('api-actions')};
  c.alive=()=>route==='admin-api' && version===routeVersion && c.box.isConnected;
  c.reload=()=>{if(c.alive())return renderApiAdmin(version);};
  c.team=id=>{const t=c.teams.find(x=>x.id===id);return t?teamLabel(t):id;};
  apiPageRenderers[page[0]](c);
}

function apiRanking(c){
  const ranked=[...c.data.teams].sort((a,b)=>b.used24h-a.used24h);
  return apiTable(['#','队伍','近24小时','累计','进行中 / 排队'],ranked.map((r,i)=>[i+1,esc(c.team(r.teamId))+(r.active?'':' '+apiBadge('已停用')),usd(r.used24h),usd(r.usedTotal),`${Number(r.inflight)} / ${Number(r.waiting || 0)}`]),'尚未开通队伍');
}

const apiPageRenderers={
  dashboard(c){
    const o=c.data.overview,models=Object.values(c.data.models || {}).flat().length;
    c.actions.innerHTML='<a class="btn secondary small" href="api/admin/keys/export" download>导出用量 CSV ↓</a>';
    c.box.innerHTML=(c.data.enabled?'':'<div class="info-banner warning">尚未配置模型账号，队伍暂时无法调用。请先到 <a href="#admin-api/accounts">账号管理</a> 添加账号。</div>')+
      apiCards([['已开通队伍',c.data.teams.filter(t=>t.active).length,`共 ${c.teams.length} 支队伍`],['账号',Object.keys(c.data.upstreams).length,`${models} 个可用模型`],['今日请求',Number(o.requests),`失败 ${Number(o.errors)}`],['今日费用',usd(o.cost),`${Number(o.teams)} 支队伍调用`]])+
      apiCards([['今日 Token',tokens(Number(o.input)+Number(o.output)),`输入 ${tokens(o.input)} · 输出 ${tokens(o.output)}`],['性能指标',`${o.rpm ?? 0} RPM`,`${tokens(o.tpm ?? 0)} TPM（近5分钟）`],['平均响应',msText(o.avgMs),`P95 ${msText(o.p95Ms)}`],['首字耗时',msText(o.avgFirstTokenMs),'今日平均']])+
      `<section class="api-card"><h2>近24小时消耗排行</h2>${apiRanking(c)}</section><section class="api-card"><h2>今日模型分布（北京时间）</h2><div id="api-models"><p class="small-note">读取中…</p></div></section>`;
    api('admin/keys/stats',{days:1,granularity:'day'}).then(s=>{if(c.alive())document.getElementById('api-models').innerHTML=apiTable(['模型','请求','输入 / 输出 tokens','费用'],s.byModel.map(r=>[esc(r.model || '（被拦截，未到达模型）'),Number(r.requests),`${tokens(r.input)} / ${tokens(r.output)}`,usd(r.cost)]));})
      .catch(e=>{if(c.alive())document.getElementById('api-models').innerHTML=`<p class="small-note">${esc(e.message)}</p>`;});
  },
  ops(c){
    const o=c.data.overview,inflight=c.data.teams.reduce((n,t)=>n+Number(t.inflight),0),waiting=c.data.teams.reduce((n,t)=>n+Number(t.waiting || 0),0);
    c.box.innerHTML=apiCards([['请求',`${o.rpm ?? 0} RPM`,`${tokens(o.tpm ?? 0)} TPM（近5分钟）`],['请求时长',msText(o.p95Ms),`P95 · 平均 ${msText(o.avgMs)}`],['首 Token 延迟',msText(o.avgFirstTokenMs),'今日平均'],['并发 / 排队',`${inflight} / ${waiting}`,'每队最多 4 并发，排队最多 30 秒']])+
      (o.authFailuresHour?`<div class="info-banner warning">近1小时有 ${Number(o.authFailuresHour)} 次无效密钥尝试${o.blockedClients?`，${Number(o.blockedClients)} 个来源地址已被临时封禁（每个地址每分钟错误超过 120 次即封禁 60 秒）`:''}。</div>`:'')+
      '<section class="api-card"><h2>今日失败原因</h2><div id="api-failures"><p class="small-note">读取中…</p></div></section><div id="key-detail"></div>';
    api('admin/keys/stats',{days:1,granularity:'day'}).then(s=>{if(c.alive())document.getElementById('api-failures').innerHTML=apiTable(['责任方','阶段','类型','上游状态','请求'],(s.byFailure || []).map(r=>[esc(ownerName[r.owner] || r.owner || '—'),esc(phaseName[r.phase] || r.phase || '—'),esc(r.type || '—'),r.upstreamStatus===null || r.upstreamStatus===undefined?'—':Number(r.upstreamStatus),Number(r.requests)]),'今日没有失败请求');}).catch(e=>toast(e.message));
    showKeyLogs({teamId:null},'失败请求',true).catch(e=>toast(e.message));
  },
  teams(c){
    c.actions.innerHTML='<input type="search" class="api-search" placeholder="搜索队伍…" aria-label="搜索队伍">';
    const rows=c.teams.map(t=>{const r=c.data.teams.find(x=>x.teamId===t.id);return `<tr data-team-row="${esc(teamLabel(t).toLowerCase())}"><td>${esc(teamLabel(t))}</td><td>${r?r.active?apiBadge('已开通','success'):apiBadge('已停用','danger'):apiBadge('未开通')}</td><td>${r?Number(r.keyCount):'—'}</td><td>${r?usd(r.used24h):'—'}</td><td>${r?usd(r.usedTotal):'—'}</td><td>${r?`${Number(r.inflight)} / ${Number(r.waiting || 0)}`:'—'}</td><td class="api-row-actions">${r?`<button type="button" class="link-button" data-key-stats="${esc(t.id)}">用量</button><button type="button" class="link-button" data-key-logs="${esc(t.id)}">调用记录</button>`:''}${r?.active?`<button type="button" class="link-button danger" data-key-revoke="${esc(t.id)}">停用 API</button>`:`<button type="button" class="link-button" data-key-provision="${esc(t.id)}">${r?'重新开通':'开通 API'}</button>`}</td></tr>`;}).join('');
    c.box.innerHTML=`${c.data.enabled?'':'<div class="info-banner warning">尚未配置模型账号：开通后队伍可以创建密钥，但调用会失败。</div>'}<section class="api-card flush"><div class="table-wrap"><table class="board-table api-table"><thead><tr><th>队伍</th><th>状态</th><th>密钥</th><th>近24小时</th><th>累计</th><th>进行中 / 排队</th><th>操作</th></tr></thead><tbody>${rows || '<tr><td colspan="7">录入队伍后可开通。</td></tr>'}</tbody></table></div></section><div id="key-detail" aria-live="polite"></div>`;
    c.actions.querySelector('input').oninput=e=>{const q=e.target.value.trim().toLowerCase();c.box.querySelectorAll('[data-team-row]').forEach(row=>row.hidden=!!q && !row.dataset.teamRow.includes(q));};
    c.box.querySelectorAll('[data-key-stats]').forEach(b=>b.onclick=()=>withBusy(b,()=>showKeyStats({teamId:b.dataset.keyStats},c.team(b.dataset.keyStats),7)));
    c.box.querySelectorAll('[data-key-logs]').forEach(b=>b.onclick=()=>withBusy(b,()=>showKeyLogs({teamId:b.dataset.keyLogs},c.team(b.dataset.keyLogs))));
    c.box.querySelectorAll('[data-key-provision],[data-key-revoke]').forEach(b=>b.onclick=()=>withBusy(b,async()=>{const revoke='keyRevoke' in b.dataset;if(revoke && !confirm('停用后该队全部密钥立即无法调用，确认停用？'))return;await api(revoke?'admin/keys/revoke':'admin/keys/provision',{teamId:b.dataset.keyRevoke || b.dataset.keyProvision});toast(revoke?'该队 API 访问权限已停用。':'该队 API 访问权限已开通，队伍可自行创建密钥。');await c.reload();}));
  },
  accounts(c){
    const saved=c.data.upstreams,free=Object.keys(apiPlatforms).filter(k=>!saved[k]);
    c.actions.innerHTML=`<button type="button" class="btn secondary small" data-refresh>刷新</button><button type="button" class="btn small" data-add ${free.length?'':'disabled title="每个平台最多一个账号"'}>添加账号</button>`;
    const pricing=u=>u.defaultPrice?`默认 $${Number(u.defaultPrice[0])} / $${Number(u.defaultPrice[1])}`:Object.values(u.models).some(p=>!p.length)?apiBadge('部分模型未定价，不计费','warning'):'按模型定价';
    c.box.innerHTML=`<section class="api-card flush">${apiTable(['名称','平台/类型','状态','接口地址','模型','计费','操作'],Object.entries(saved).map(([kind,u])=>{const names=Object.keys(u.models);return [
      `<strong>${esc(apiAccountName(kind,u))}</strong>`,`<span class="api-platform ${kind}"><b>${esc(apiPlatforms[kind].label)}</b><i>Key</i></span>`,apiBadge('正常','success'),`<code>${esc(u.url)}</code>`,
      `${names.slice(0,3).map(m=>`<span class="api-chip">${esc(m)}</span>`).join('')}${names.length>3?`<small> 等 ${names.length} 个</small>`:''}${Object.keys(u.aliases || {}).length?`<br><small>${Object.keys(u.aliases).length} 条映射</small>`:''}`,pricing(u),
      `<span class="api-row-actions"><button type="button" class="link-button" data-edit="${kind}">编辑</button><button type="button" class="link-button" data-test="${kind}">测试连接</button><button type="button" class="link-button danger" data-delete="${kind}">删除</button></span>`];}),'还没有账号。点击右上角“添加账号”，填写接口地址与 API Key，再同步上游模型即可。')}</section>`;
    c.actions.querySelector('[data-refresh]').onclick=()=>c.reload();
    c.actions.querySelector('[data-add]').onclick=()=>apiAccountDialog(c,free[0],false);
    c.box.querySelectorAll('[data-edit]').forEach(b=>b.onclick=()=>apiAccountDialog(c,b.dataset.edit,true));
    c.box.querySelectorAll('[data-test]').forEach(b=>b.onclick=()=>apiTestDialog(c,b.dataset.test));
    c.box.querySelectorAll('[data-delete]').forEach(b=>b.onclick=()=>withBusy(b,async()=>{
      const kind=b.dataset.delete;if(!confirm(`删除账号「${apiAccountName(kind,saved[kind])}」？${apiPlatforms[kind].label} 接口的请求会立即失败。`))return;
      await api('admin/keys/upstreams',{upstreams:apiOtherAccounts(saved,kind)});toast('账号已删除。');await c.reload();}));
  },
  usage(c){
    const opened=c.teams.filter(t=>c.data.teams.some(r=>r.teamId===t.id));
    c.actions.innerHTML=`<label class="api-inline">队伍<select data-team><option value="">全部队伍</option>${opened.map(t=>`<option value="${esc(t.id)}">${esc(teamLabel(t))}</option>`).join('')}</select></label><a class="btn secondary small" href="api/admin/keys/export" download>导出 CSV ↓</a>`;
    c.box.innerHTML='<div class="api-tabs" role="tablist"><button type="button" role="tab" data-tab="stats" aria-selected="true">用量统计</button><button type="button" role="tab" data-tab="logs" aria-selected="false">调用记录</button><button type="button" role="tab" data-tab="errors" aria-selected="false">错误请求</button></div><div id="key-detail" aria-live="polite"></div>';
    const select=c.actions.querySelector('[data-team]');let tab='stats';
    const show=()=>{const teamId=select.value || null,title=teamId?c.team(teamId):'全部队伍';c.box.querySelectorAll('[data-tab]').forEach(b=>b.setAttribute('aria-selected',String(b.dataset.tab===tab)));
      (tab==='stats'?showKeyStats({teamId},title,7):showKeyLogs({teamId},title,tab==='errors')).catch(e=>toast(e.message));};
    select.onchange=show;c.box.querySelectorAll('[data-tab]').forEach(b=>b.onclick=()=>{tab=b.dataset.tab;show();});show();
  },
  audit(c){
    c.box.innerHTML=`<section class="api-card flush">${apiTable(['时间（北京）','操作','对象','详情'],c.data.changes.map(x=>[esc(beijingTime(x.at)),esc(changeName[x.action] || x.action),esc(x.target?c.team(x.target):'—'),`<small class="api-json">${esc(JSON.stringify(x.metadata)).slice(0,300)}</small>`]),'暂无记录')}</section><p class="small-note">显示最近 50 条 AI API 管理操作。</p>`;
  },
  settings(c){
    const s=c.data.settings,st=c.data.storage || {};
    c.box.innerHTML=`<section class="api-card"><form id="key-settings"><h2>网关服务</h2><div class="form-grid"><label class="field">每队密钥上限<input name="maxKeys" type="number" min="1" max="20" value="${Number(s.maxKeys)}" required></label><label class="field">访问到期（北京时间）<input name="expiresAt" type="datetime-local" step="60" value="${esc(beijingInput(s.expiresAt))}"><small>留空表示不过期；到点后所有队伍的密钥停止调用。</small></label><label class="field">每队每分钟请求上限<input name="teamRpm" type="number" min="0" max="10000" value="${Number(s.teamRpm)}" required><small>0 为不限。另有每队最多 4 个并发请求，超出时排队最多 30 秒。</small></label><label class="field">屏蔽词（每行一个）<textarea name="blockedWords" rows="3" placeholder="命中即拒绝，不区分大小写">${esc(s.blockedWords.join('\n'))}</textarea></label><label class="field wide">队伍公告<textarea name="announcement" rows="2" maxlength="500" placeholder="显示在所有队伍的密钥卡片顶部，留空不显示">${esc(s.announcement)}</textarea></label></div>
      <h2>调用内容</h2><div class="form-grid"><label class="field">保留时长（小时）<input name="logRetentionHours" type="number" min="1" max="8760" value="${Number(s.logRetentionHours ?? 72)}" required><small>超过时长的请求与回复内容自动删除，用量与费用记录保留。</small></label><label class="field">总容量上限（MB）<input name="logMaxMB" type="number" min="50" max="102400" value="${Number(s.logMaxMB ?? 2048)}" required><small>超过后从最早的内容开始删除，防止磁盘写满。</small></label></div><label class="toggle-line"><input type="checkbox" name="logContent" ${s.logContent?'checked':''}>记录完整请求上下文与回复（压缩保存，队伍页会提示）</label>
      <p class="small-note">已保存 ${Number(st.rows || 0)} 条调用内容，压缩后 ${kb(Number(st.bytes || 0))}。<button type="button" class="link-button danger" id="clear-call-content">立即清空全部调用内容</button></p><button class="btn small" type="submit">保存设置</button><p class="form-error" role="alert"></p></form></section>`;
    const form=document.getElementById('key-settings');
    form.onsubmit=e=>{e.preventDefault();withBusy(form.querySelector('[type=submit]'),async()=>{formError(form,'');try{const f=form.elements;await api('admin/keys/settings',{maxKeys:Number(f.maxKeys.value),expiresAt:parseBeijingInput(f.expiresAt.value),logContent:f.logContent.checked,logRetentionHours:Number(f.logRetentionHours.value),logMaxMB:Number(f.logMaxMB.value),teamRpm:Number(f.teamRpm.value),announcement:f.announcement.value.trim(),blockedWords:f.blockedWords.value.split('\n').map(w=>w.trim()).filter(Boolean)});toast('API 设置已保存。');await c.reload();}catch(error){formError(form,error.message);}});};
    document.getElementById('clear-call-content').onclick=e=>withBusy(e.currentTarget,async()=>{if(!confirm('将删除所有已保存的请求与回复内容（用量与费用记录保留），无法恢复。继续？'))return;const r=await api('admin/keys/logs/clear',{});toast(`已删除 ${Number(r.deleted)} 条调用内容。`);await c.reload();});
  }
};

function apiDialog(html){
  const dialog=document.createElement('dialog');dialog.className='api-dialog';dialog.innerHTML=html;document.body.append(dialog);
  dialog.addEventListener('close',()=>dialog.remove());dialog.querySelectorAll('[data-close]').forEach(b=>b.onclick=()=>dialog.close());dialog.showModal();return dialog;
}

// 添加账号 / 编辑账号, as sub2api's account dialog for API Key accounts: name, platform, Base URL, key, model whitelist or mapping, pricing.
function apiAccountDialog(c,kind,editing){
  const saved=c.data.upstreams,u=editing?saved[kind]:{},p=apiPlatforms[kind],owner=session;
  const s={models:Object.keys(u.models || {}),prices:Object.fromEntries(Object.entries(u.models || {}).map(([m,v])=>[m,priceColumns.map((_,i)=>v[i] ?? '')])),
    aliases:Object.entries(u.aliases || {}),catalog:[],tab:'whitelist'};
  const d=apiDialog(`<form class="api-dialog-form" novalidate><header><h2>${editing?'编辑账号':'添加账号'}</h2><button type="button" class="api-x" data-close aria-label="关闭">×</button></header><div class="api-dialog-body">
    <label class="field">账号名称<input name="name" maxlength="60" placeholder="请输入账号名称" value="${esc(u.name || '')}"><small>可选，便于区分；留空时按接口地址显示。</small></label>
    <div class="field"><span>平台</span><div class="api-segmented">${Object.entries(apiPlatforms).map(([k,v])=>`<button type="button" data-platform="${k}" aria-pressed="${k===kind}" ${k!==kind && (editing || saved[k])?'disabled':''}>${esc(v.label)}</button>`).join('')}</div>${editing?'':'<small>每个平台一个账号；已添加的平台不可再选。</small>'}</div>
    <div class="field"><span>账号类型</span><div class="api-type-card"><strong>API Key</strong><small>${esc(p.endpoint)}</small></div></div>
    <div class="field"><span>快速填入</span><div class="form-actions">${apiPresets.filter(x=>x[1]===kind).map(([name,,url])=>`<button type="button" class="btn secondary small" data-preset="${esc(url)}" data-preset-name="${esc(name)}">${esc(name)}</button>`).join('')}</div></div>
    <label class="field">Base URL<input name="url" placeholder="${esc(p.official)}" value="${esc(u.url || '')}"><small>留空使用官方 ${esc(p.label)} API。只接受 HTTPS（本机地址除外）。</small></label>
    <label class="field">API Key${editing?'':' *'}<input name="key" type="password" autocomplete="off" placeholder="${editing?esc(u.keyHint?.startsWith('…')?`已保存 ${u.keyHint}`:'已保存'):esc(p.keyHint)}"><small>${editing?'留空以保持当前密钥（更换接口域名时需要重新填写）。':`您的 ${esc(p.label)} API Key，保存后不再显示，队伍看不到。`}</small></label>
    <div class="field"><span>模型限制</span><div class="api-tabs half"><button type="button" data-mode="whitelist" aria-selected="true">模型白名单</button><button type="button" data-mode="mapping" aria-selected="false">模型映射</button></div>
      <div data-pane="whitelist"><div class="api-chips" data-chips></div><p class="small-note" data-count></p>
        <div class="form-actions"><button type="button" class="btn secondary small api-sync" data-sync>同步上游支持的模型</button><button type="button" class="btn danger small" data-clear>清除所有模型</button><span class="small-note" data-sync-status role="status" aria-live="polite"></span></div>
        <div class="api-custom"><input data-custom placeholder="输入自定义模型名称" aria-label="自定义模型名称"><button type="button" class="btn secondary small" data-add-custom>填入</button></div><div data-catalog></div></div>
      <div data-pane="mapping" hidden><div class="info-banner">将请求模型映射到实际模型。以 * 结尾表示前缀匹配，例如 <code>${esc(p.alias)}</code> 让客户端写死的默认模型名也能使用。</div><div data-aliases></div>
        <div class="form-actions"><button type="button" class="btn secondary small api-dashed" data-alias-add>+ 添加映射</button><button type="button" class="btn secondary small" data-alias-preset>+ ${esc(p.alias)} → 第一个模型</button></div></div></div>
    <div class="field"><span>计费</span><label class="field">默认单价（美元 / 百万 tokens）<span class="api-price-pair"><input name="defaultIn" inputmode="decimal" placeholder="输入价" value="${esc(u.defaultPrice?.[0] ?? '')}" aria-label="默认输入价"><input name="defaultOut" inputmode="decimal" placeholder="输出价" value="${esc(u.defaultPrice?.[1] ?? '')}" aria-label="默认输出价"></span><small>未单独定价的模型按此计费；留空则这些模型不计费，队伍额度不扣减。缓存写入按输入价 1.25 倍、读取 0.1 倍计。人民币价格请按汇率换算。</small></label>
      <details class="api-prices"><summary>按模型单独定价（可选）</summary><div data-prices></div></details></div>
  </div><footer><p class="form-error" role="alert"></p><button type="button" class="btn secondary small" data-close>取消</button><button type="submit" class="btn small">${editing?'更新':'创建'}</button></footer></form>`);
  const form=d.querySelector('form'),f=form.elements,q=sel=>d.querySelector(sel),alive=()=>d.open && c.alive() && session===owner && ownsBrowserLogin(owner);
  const drawChips=()=>{q('[data-chips]').innerHTML=s.models.map(m=>`<span class="api-chip">${esc(m)}<button type="button" data-remove="${esc(m)}" aria-label="移除 ${esc(m)}">×</button></span>`).join('');
    q('[data-count]').textContent=s.models.length?`已选择 ${s.models.length} 个模型`:'尚未选择模型：至少选择 1 个，队伍只能调用白名单中的模型。';
    d.querySelectorAll('[data-pick]').forEach(box=>box.checked=s.models.includes(box.value));drawPrices();drawAliases();};
  const drawPrices=()=>{q('[data-prices]').innerHTML=s.models.length?`<p class="small-note">留空的模型按默认单价计费。缓存价可留空，按输入价比例计。</p><div class="table-wrap"><table class="board-table api-table"><thead><tr><th>模型</th>${priceColumns.map(h=>`<th>${h}</th>`).join('')}</tr></thead><tbody>${s.models.map(m=>`<tr><td>${esc(m)}</td>${priceColumns.map((h,i)=>`<td><input class="api-price" inputmode="decimal" data-price="${esc(m)}" data-index="${i}" value="${esc((s.prices[m] || [])[i] ?? '')}" aria-label="${esc(m)} ${h}价"></td>`).join('')}</tr>`).join('')}</tbody></table></div>`:'<p class="small-note">先在模型白名单中选择模型。</p>';};
  const drawAliases=()=>{q('[data-aliases]').innerHTML=s.aliases.map(([from,to],i)=>`<div class="api-alias"><input data-alias-from="${i}" value="${esc(from)}" placeholder="请求模型" aria-label="请求模型"><span>→</span><select data-alias-to="${i}" aria-label="实际模型">${s.models.map(m=>`<option ${m===to?'selected':''}>${esc(m)}</option>`).join('')}${s.models.includes(to)?'':`<option selected value="${esc(to)}">${esc(to || '请选择')}（不在白名单）</option>`}</select><button type="button" class="link-button danger" data-alias-del="${i}" aria-label="删除映射">删除</button></div>`).join('') || '<p class="small-note">暂无映射。</p>';};
  const add=names=>{for(const n of names)if(n && !s.models.includes(n))s.models.push(n);if(s.models.length>500){s.models.length=500;toast('每个账号最多 500 个模型。');}drawChips();};
  d.addEventListener('input',e=>{const t=e.target;
    if(t.dataset.price!==undefined){(s.prices[t.dataset.price] ||= priceColumns.map(()=>''))[Number(t.dataset.index)]=t.value;}
    if(t.dataset.aliasFrom!==undefined)s.aliases[Number(t.dataset.aliasFrom)][0]=t.value;
    if(t.dataset.filter!==undefined){const v=t.value.trim().toLowerCase();d.querySelectorAll('[data-pick]').forEach(b=>b.closest('label').hidden=!!v && !b.value.toLowerCase().includes(v));}});
  d.addEventListener('change',e=>{const t=e.target;
    if(t.dataset.aliasTo!==undefined)s.aliases[Number(t.dataset.aliasTo)][1]=t.value;
    if(t.dataset.pick!==undefined){if(t.checked)add([t.value]);else{s.models=s.models.filter(m=>m!==t.value);drawChips();}}});
  d.addEventListener('click',e=>{const t=e.target.closest('button');if(!t)return;
    if(t.dataset.remove!==undefined){s.models=s.models.filter(m=>m!==t.dataset.remove);drawChips();}
    if(t.dataset.aliasDel!==undefined){s.aliases.splice(Number(t.dataset.aliasDel),1);drawAliases();}});
  d.querySelectorAll('[data-platform]').forEach(b=>b.onclick=()=>{if(b.dataset.platform===kind)return;d.close();apiAccountDialog(c,b.dataset.platform,false);});
  d.querySelectorAll('[data-preset]').forEach(b=>b.onclick=()=>{f.url.value=b.dataset.preset;if(!f.name.value)f.name.value=b.dataset.presetName;f.key.focus();});
  d.querySelectorAll('[data-mode]').forEach(b=>b.onclick=()=>{d.querySelectorAll('[data-mode]').forEach(x=>x.setAttribute('aria-selected',String(x===b)));d.querySelectorAll('[data-pane]').forEach(x=>x.hidden=x.dataset.pane!==b.dataset.mode);});
  q('[data-clear]').onclick=()=>{if(s.models.length && !confirm('清除所有已选模型？'))return;s.models=[];drawChips();};
  q('[data-add-custom]').onclick=()=>{const n=q('[data-custom]').value.trim();if(/\s/.test(n) || n.length>100){toast('模型名不能含空格，最多 100 字。');return;}add([n]);q('[data-custom]').value='';};
  q('[data-alias-add]').onclick=()=>{s.aliases.push(['',s.models[0] || '']);drawAliases();};
  q('[data-alias-preset]').onclick=()=>{if(!s.models.length){toast('请先选择模型。');return;}if(!s.aliases.some(a=>a[0]===p.alias))s.aliases.push([p.alias,s.models[0]]);drawAliases();};
  const status=q('[data-sync-status]');
  q('[data-sync]').onclick=e=>withBusy(e.currentTarget,async()=>{
    const draft={kind,url:f.url.value.trim() || p.official,key:f.key.value.trim()},button=e.currentTarget;
    if(!draft.key && !editing){status.textContent='请先填写 API Key。';return;}
    button.textContent='同步上游中…';status.textContent='';
    try{
      const r=await api('admin/keys/upstreams/models',draft,{loginId:owner.loginId});
      if(!alive())return;
      if((f.url.value.trim() || p.official)!==draft.url || f.key.value.trim()!==draft.key){status.textContent='地址或密钥已变更，请重新同步。';return;}
      status.textContent=`${r.ok?'✓':'✗'} ${r.message || ''}${r.status?` · HTTP ${Number(r.status)}`:''}`;status.classList.toggle('status-error',!r.ok);
      if(!r.ok || !r.models?.length)return;
      s.catalog=r.models;
      q('[data-catalog]').innerHTML=`<div class="api-catalog"><div class="api-catalog-tools"><input type="search" data-filter placeholder="搜索模型..." aria-label="搜索模型"><button type="button" class="btn secondary small" data-pick-shown>全选当前显示</button></div><div class="api-catalog-list">${r.models.map(m=>`<label><input type="checkbox" data-pick value="${esc(m)}" ${s.models.includes(m)?'checked':''}><span>${esc(m)}</span></label>`).join('')}</div></div>`;
      q('[data-pick-shown]').onclick=()=>add([...d.querySelectorAll('[data-pick]')].filter(b=>!b.closest('label').hidden).map(b=>b.value));
    }catch(error){if(alive()){status.textContent=error.message;status.classList.add('status-error');}}
    finally{if(button.isConnected)button.textContent='同步上游支持的模型';}
  });
  form.onsubmit=e=>{e.preventDefault();withBusy(form.querySelector('[type=submit]'),async()=>{formError(form,'');try{
    if(!editing && !f.key.value.trim())throw new Error('请填写 API Key');
    const body=apiAccountPayload(saved,kind,{name:f.name.value,url:f.url.value,key:f.key.value,models:s.models,prices:s.prices,aliases:s.aliases,defaultPrice:[f.defaultIn.value,f.defaultOut.value]});
    await api('admin/keys/upstreams',{upstreams:body.upstreams});if(!alive())return;
    const free=!body.upstreams[kind].defaultPrice && Object.values(body.upstreams[kind].models).some(x=>!x.length);
    toast(free?'账号已保存。注意：未设默认单价，未单独定价的模型不计费。':editing?'账号已更新，立即生效。':'账号已创建，立即生效。');d.close();await c.reload();
  }catch(error){formError(form,error.message);}});};
  drawChips();
}

// 测试账号连接: sends one 1-token request with the saved account (first whitelisted model) and prints a terminal-style log.
function apiTestDialog(c,kind){
  const u=c.data.upstreams[kind],name=apiAccountName(kind,u),model=Object.keys(u.models)[0];
  const d=apiDialog(`<div class="api-dialog-form"><header><h2>测试账号连接</h2><button type="button" class="api-x" data-close aria-label="关闭">×</button></header><div class="api-dialog-body"><div class="api-test-head"><strong>${esc(name)}</strong><span class="api-platform ${kind}"><b>${esc(apiPlatforms[kind].label)}</b><i>Key</i></span></div><p class="small-note">测试模型：<code>${esc(model)}</code>（白名单第一个模型，发送一条“hi”，最多 1 个 token）。</p><pre class="api-terminal" aria-live="polite">准备测试。点击“开始测试”按钮开始...</pre></div><footer><button type="button" class="btn secondary small" data-close>关闭</button><button type="button" class="btn small" data-start>开始测试</button></footer></div>`);
  const out=d.querySelector('.api-terminal'),line=(text,tone='')=>{out.insertAdjacentHTML('beforeend',`<span class="${tone}">${esc(text)}</span>\n`);};
  const start=d.querySelector('[data-start]');
  start.onclick=()=>withBusy(start,async()=>{
    out.textContent='';line(`开始测试账号：${name}`,'info');line(`账号类型：${apiPlatforms[kind].label} · API Key`);line('连接 API 中...','warn');
    try{const r=await api(`admin/keys/upstreams/test/${kind}`);if(!d.open)return;
      if(r.ok){line('已连接到 API','ok');line(`使用模型：${r.model || model}`,'cyan');line('发送测试消息："hi"');line(`响应：${r.message || ''} · ${Number(r.latencyMs)}ms`,'warn');line('测试完成！','ok');}
      else line(`测试失败：${r.message || ''}${r.status?` · HTTP ${Number(r.status)}`:''} · ${Number(r.latencyMs)}ms`,'err');}
    catch(error){if(d.open)line(`测试失败：${error.message}`,'err');}
    start.textContent='重试';});
}
