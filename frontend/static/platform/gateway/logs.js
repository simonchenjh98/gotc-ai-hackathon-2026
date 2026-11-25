'use strict';

// Stored calls keep whatever the client sent, so every shape check is defensive (as sub2api's ops detail modal):
// parse with a raw-text fallback, check types before iterating, and never let one bad field hide the raw record.
const asList=v=>Array.isArray(v)?v:[];
const asObject=v=>v && typeof v==='object' && !Array.isArray(v)?v:null;
const asText=v=>typeof v==='string'?v:v===null || v===undefined?'':JSON.stringify(v);
function prettyJSON(raw){try{return JSON.stringify(JSON.parse(raw),null,2);}catch{return asText(raw);}}
function blockText(content){if(content===null || content===undefined)return '';if(typeof content==='string')return content;if(!Array.isArray(content))return JSON.stringify(content,null,2);
  return content.map(item=>{if(typeof item==='string')return item;const b=asObject(item);if(!b)return JSON.stringify(item);switch(b.type){case 'text':return asText(b.text);case 'thinking':return `【思考】\n${asText(b.thinking)}`;case 'redacted_thinking':return '【思考（已加密）】';case 'image':case 'image_url':case 'input_image':return '【图片】';case 'document':return '【文档】';
    case 'tool_use':return `【调用工具 ${asText(b.name)}】\n${JSON.stringify(b.input,null,2)}`;case 'tool_result':return `【工具结果${b.is_error?'（出错）':''}】\n${blockText(b.content)}`;default:return JSON.stringify(b,null,2);}}).join('\n\n');}
function openaiCalls(calls){return asList(calls).map(c=>{const f=asObject(asObject(c)?.function) || {};return `【调用工具 ${asText(f.name)}】\n${asText(f.arguments)}`;}).join('\n\n');}
function conversationOf(text){let parsed;try{parsed=JSON.parse(text);}catch{return null;}const req=asObject(parsed);if(!req)return null;const turns=[];
  if(req.system)turns.push(['system',blockText(req.system)]);
  if(req.messages!==undefined && !Array.isArray(req.messages))turns.push(['（messages 不是列表）',JSON.stringify(req.messages,null,2)]);
  for(const item of asList(req.messages)){const m=asObject(item);if(!m){turns.push(['（无法识别的消息）',JSON.stringify(item)]);continue;}const parts=[blockText(m.content),openaiCalls(m.tool_calls)].filter(Boolean);turns.push([asText(m.role) || '?',parts.join('\n\n')+(m.tool_call_id?'\n\n（工具结果）':'')]);}
  const meta=['model','max_tokens','temperature','top_p','stream','thinking','reasoning_effort','tool_choice'].filter(k=>req[k]!==undefined).map(k=>`${k}: ${typeof req[k]==='object'?JSON.stringify(req[k]):req[k]}`);
  const tools=asList(req.tools).map(t=>{const o=asObject(t) || {},f=asObject(o.function) || {};return {name:asText(o.name || f.name) || '?',description:asText(o.description || f.description)};});
  return {meta,turns,tools};}
function replyOf(text){const trimmed=asText(text).trim();if(!trimmed)return '';
  if(trimmed.startsWith('{')){try{const r=asObject(JSON.parse(trimmed));if(!r)return trimmed;if(r.content)return blockText(r.content);const m=asObject(asObject(asList(r.choices)[0])?.message);if(m)return [m.reasoning_content?`【思考】\n${asText(m.reasoning_content)}`:'',asText(m.content),openaiCalls(m.tool_calls)].filter(Boolean).join('\n\n');return JSON.stringify(r,null,2);}catch{return trimmed;}}
  if(!trimmed.split('\n').some(line=>line.startsWith('data:')))return trimmed;  // plain text (e.g. a gateway error), not a stream
  const blocks=new Map(),calls=new Map();let reasoning='',content='',stop='',finished=false;
  for(const line of trimmed.split('\n')){if(!line.startsWith('data:'))continue;if(line.slice(5).trim()==='[DONE]'){finished=true;continue;}let e;try{e=asObject(JSON.parse(line.slice(5)));}catch{continue;}if(!e)continue;
    if(e.type==='message_stop')finished=true;
    if(e.type==='content_block_start'){const c=asObject(e.content_block) || {};blocks.set(e.index,{type:c.type,name:asText(c.name),text:asText(c.text),thinking:'',json:''});}
    else if(e.type==='content_block_delta'){if(!blocks.has(e.index))blocks.set(e.index,{type:'text',text:'',thinking:'',json:''});const blk=blocks.get(e.index),dl=asObject(e.delta) || {};blk.text+=asText(dl.text);blk.thinking+=asText(dl.thinking);blk.json+=asText(dl.partial_json);}
    else if(e.type==='message_delta')stop=asText(asObject(e.delta)?.stop_reason) || stop;
    else if(e.choices!==undefined){const choice=asObject(asList(e.choices)[0]) || {},dl=asObject(choice.delta) || {};content+=asText(dl.content);reasoning+=asText(dl.reasoning_content);
      for(const item of asList(dl.tool_calls)){const c=asObject(item);if(!c)continue;const id=c.index ?? 0;if(!calls.has(id))calls.set(id,{name:'',args:''});const slot=calls.get(id),f=asObject(c.function) || {};slot.name+=asText(f.name);slot.args+=asText(f.arguments);}
      if(choice.finish_reason)stop=asText(choice.finish_reason);}}
  const out=[...blocks.values()].map(b=>b.type==='tool_use'?`【调用工具 ${b.name}】\n${b.json}`:b.type==='thinking'?`【思考】\n${b.thinking}`:b.text);
  if(reasoning)out.push(`【思考】\n${reasoning}`);if(content)out.push(content);out.push(...[...calls.values()].map(c=>`【调用工具 ${c.name}】\n${c.args}`));
  return out.join('\n\n')+(stop?`\n\n— 结束原因：${stop}`:'')+(finished?'':'\n\n— 流未正常结束（未收到结束事件）');}
function callContentMarkup(d,path){
  const turn=(role,text,i)=>`<div class="chat-turn role-${esc(String(role).split(' ')[0])}"><div class="chat-role">${i===null?'':`#${i} · `}${esc(role)} · ${Array.from(text || '').length.toLocaleString('en-US')} 字</div><pre class="chat-text">${esc(text || '（空）')}</pre></div>`;
  let chat;
  try{const conv=conversationOf(d.request),reply=replyOf(d.response);
    const conversation=conv?`<p class="small-note">${conv.meta.map(esc).join(' · ')}${conv.tools.length?` · ${conv.tools.length} 个工具`:''} · ${conv.turns.length} 条上下文消息</p>${conv.tools.length?`<details class="chat-tools"><summary>可用工具（${conv.tools.length}）</summary><ul>${conv.tools.map(t=>`<li><strong>${esc(t.name)}</strong> ${esc(t.description.split('\n')[0].slice(0,200))}</li>`).join('')}</ul></details>`:''}${conv.turns.map(([role,text],i)=>turn(role,text,i+1)).join('')}`:'<p class="small-note">请求不是常见的 JSON 对象格式，请查看原始内容。</p>';
    chat=`<h4>请求上下文</h4>${conversation}<h4>模型回复</h4>${turn('assistant · 回复',reply,null)}`;}
  catch{chat='<div class="info-banner warning">无法按对话格式解析这条记录，原始内容与请求头仍完整保存，请切换到“原始内容”查看。</div>';}
  return `<div class="form-actions call-tabs"><button type="button" class="btn small" data-view="chat" aria-pressed="true">对话视图</button><button type="button" class="btn secondary small" data-view="raw" aria-pressed="false">原始内容</button><button type="button" class="btn secondary small" data-view="headers" aria-pressed="false">请求头</button><button type="button" class="btn secondary small" data-download>下载 JSON ↓</button><span class="small-note">${d.requestId?`请求 ID ${esc(d.requestId)} · `:''}请求 ${kb(Number(d.requestBytes) || 0)} · 回复 ${kb(Number(d.responseBytes) || 0)}${d.responseTruncated?'（回复超过 16MB，仅保存前 16MB）':''} · ${esc(path)}</span></div>
  <div data-pane="chat">${chat}</div>
  <div data-pane="raw" hidden><h4>请求（客户端原样发送）</h4><pre class="log-content">${esc(prettyJSON(d.request))}</pre><h4>回复（上游原样返回）</h4><pre class="log-content">${esc(asText(d.response))}</pre></div>
  <div data-pane="headers" hidden><pre class="log-content">${esc(JSON.stringify(d.headers ?? {},null,2))}</pre></div>`;}
function installCallContent(el,d){el.querySelectorAll('[data-view]').forEach(b=>b.onclick=()=>{el.querySelectorAll('[data-view]').forEach(x=>{const on=x===b;x.setAttribute('aria-pressed',on);x.classList.toggle('secondary',!on);});el.querySelectorAll('[data-pane]').forEach(p=>p.hidden=p.dataset.pane!==b.dataset.view);});
  el.querySelector('[data-download]').onclick=()=>{let request=d.request;try{request=JSON.parse(d.request);}catch{}const blob=new Blob([JSON.stringify({id:d.id,headers:d.headers,request,response:d.response},null,2)],{type:'application/json'});const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=`gotc-call-${d.id}.json`;a.click();setTimeout(()=>URL.revokeObjectURL(a.href),1000);};}

function keyAdminView(box){
  const owner=session,context=typeof currentApiAdmin!=='undefined'?currentApiAdmin:null;
  const generation=box?(box.keyAdminGeneration=(box.keyAdminGeneration || 0)+1):0;
  const valid=()=>box?.isConnected && box.keyAdminGeneration===generation && session===owner && ownsBrowserLogin(owner) && (!context || context.alive());
  return {valid,request:(path,body)=>valid()?api(path,body,{loginId:owner.loginId}):Promise.reject(new Error('登录身份已变化，请刷新页面后重试。'))};
}
async function showKeyStats(filter,title,days,granularity='day'){const box=document.getElementById('key-detail'),view=keyAdminView(box);const data=await view.request('admin/keys/stats',{...filter,days,granularity});if(!view.valid())return;const t=data.totals;
  box.innerHTML=`<section class="surface key-detail"><div class="section-caption"><h2>用量详情 · ${esc(title)}</h2><label class="field">时间范围<select id="stats-days">${[1,7,30,90].map(d=>`<option value="${d}" ${d===days?'selected':''}>近 ${d} 天</option>`).join('')}</select></label><label class="field">时间粒度<select id="stats-granularity"><option value="day" ${granularity==='day'?'selected':''}>按日</option><option value="hour" ${granularity==='hour'?'selected':''}>按小时</option></select></label></div>${billingNote(data.billing)}${metrics([[Number(t.requests),'请求'],[billingAmount(data.billing,t.cost,t.currency || 'legacy'),'费用'],[tokens(t.input),`输入 tokens · 输出 ${tokens(t.output)}`],[`${Number(t.errors || 0)} · ${t.avgMs===null?'—':Number(t.avgMs)+'ms'}`,'失败请求 · 平均耗时']])}
  ${usageTable('按密钥',['队伍','密钥','最近调用'],data.byKey,r=>[`<td>${esc(String(r.teamNumber).padStart(2,'0'))} · ${esc(r.teamName)}</td>`,`<td><button type="button" class="link-button" data-logs-key="${esc(r.keyId)}" data-logs-title="${esc(r.teamName+' / '+r.name)}">${esc(r.name)}</button><br><small>${esc(r.masked)}${r.active?'':' · 已删除'}</small></td>`,`<td>${esc(beijingTime(r.lastAt))}</td>`])}
  ${usageTable('按模型',['模型'],data.byModel,r=>[`<td>${esc(r.model || '（被拦截，未到达模型）')}</td>`])}
  ${(data.byFailure || []).length?usageTable('失败原因（责任方 · 阶段 · 类型）',['责任方','阶段','类型','上游状态'],data.byFailure,r=>[`<td>${esc(ownerName[r.owner] || r.owner || '—')}</td>`,`<td>${esc(phaseName[r.phase] || r.phase || '—')}</td>`,`<td>${esc(r.type || '—')}</td>`,`<td>${r.upstreamStatus===null || r.upstreamStatus===undefined?'—':Number(r.upstreamStatus)}</td>`]):''}
  ${usageTable('按客户端（User-Agent）',['客户端'],data.byClient,r=>[`<td class="ua-cell">${esc(r.userAgent || '（空）')}</td>`])}
  ${usageTable(granularity==='hour'?'按小时（北京时间）':'按日（北京时间）',[granularity==='hour'?'时间':'日期'],data.daily,r=>[`<td>${esc(r.day)}</td>`])}</section>`;
  document.getElementById('stats-days').onchange=e=>view.valid() && showKeyStats(filter,title,Number(e.target.value),granularity).catch(err=>toast(err.message));
  document.getElementById('stats-granularity').onchange=e=>view.valid() && showKeyStats(filter,title,days,e.target.value).catch(err=>toast(err.message));
  box.querySelectorAll('[data-logs-key]').forEach(b=>b.onclick=()=>withBusy(b,()=>view.valid() && showKeyLogs({keyId:b.dataset.logsKey},b.dataset.logsTitle)));
  box.scrollIntoView({block:'start'});}
async function showKeyLogs(filter,title,errorsOnly=false,search={}){const box=document.getElementById('key-detail'),view=keyAdminView(box);let items=[],more=false;
  async function load(){const data=await view.request('admin/keys/logs',{...filter,...search,errorsOnly,beforeId:items.length?items[items.length-1].id:null});if(!view.valid())return;items=items.concat(data.items);more=data.more;}
  function render(){if(!view.valid())return;box.innerHTML=`<section class="surface key-detail"><div class="section-caption"><h2>调用记录 · ${esc(title)}</h2><label class="toggle-line"><input type="checkbox" id="logs-errors" ${errorsOnly?'checked':''}>只看失败</label></div><form id="logs-search" class="settings-row"><label class="field">模型（请求名或实际模型）<input name="model" maxlength="100" value="${esc(search.model || '')}"></label><label class="field">请求 ID（X-Client-Request-ID）<input name="requestId" maxlength="100" value="${esc(search.requestId || '')}"></label><button class="btn secondary small" type="submit">筛选</button></form>${callTable(items,true)}${more?'<div class="form-actions"><button type="button" class="btn secondary small" id="logs-more">加载更早的记录</button></div>':''}</section>`;
    document.getElementById('logs-errors').onchange=e=>view.valid() && showKeyLogs(filter,title,e.target.checked,search).catch(err=>toast(err.message));
    document.getElementById('logs-search').onsubmit=buttonAction(e=>{e.preventDefault();if(!view.valid())return;const f=e.currentTarget;return showKeyLogs(filter,title,errorsOnly,{model:f.elements.model.value.trim(),requestId:f.elements.requestId.value.trim()}).catch(err=>toast(err.message));});
    document.getElementById('logs-more')?.addEventListener('click',e=>withBusy(e.currentTarget,async()=>{await load();render();}));
    box.querySelectorAll('[data-log-id]').forEach(b=>b.onclick=()=>withBusy(b,async()=>{if(!view.valid())return;const row=document.getElementById('log-'+b.dataset.logId);if(!row.hidden){row.hidden=true;return;}const item=items.find(i=>String(i.id)===b.dataset.logId);const d=await view.request('admin/keys/log',{id:Number(b.dataset.logId)});if(!view.valid() || !row.isConnected)return;row.firstElementChild.innerHTML=callContentMarkup(d,item.path);installCallContent(row.firstElementChild,d);row.hidden=false;}));}
  await load();if(!view.valid())return;render();box.scrollIntoView({block:'start'});}
