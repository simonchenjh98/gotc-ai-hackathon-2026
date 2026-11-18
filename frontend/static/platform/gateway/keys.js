'use strict';

const teamKeyLoginNotice='当前登录已失效或已切换，密钥已隐藏。请刷新页面并核对登录队伍后操作。';
function createTeamKeyContext(version){
  currentTeamKeys?.dispose();
  const owner=session,loginId=owner.loginId,box=document.getElementById('team-keys'),out=document.getElementById('new-api-key');
  const authorization=currentTeamRecovery?.sessionOwner===owner?currentTeamRecovery:null;
  const context={owner,loginId,box,out,active:true,busy:false,copying:false,key:'',pendingKey:null,data:null,generation:0,
    dispose(){this.active=false;this.key='';this.pendingKey=null;if(out)out.innerHTML='';},
    lock(){this.dispose();if(box?.isConnected && document.getElementById('team-keys')===box)box.innerHTML=`<h2>AI API 密钥</h2><p class="small-note" role="alert">${teamKeyLoginNotice}</p>`;},
    valid(){
      if(!this.active)return false;
      if(currentTeamKeys!==this || version!==routeVersion || route!=='team' || !box?.isConnected || document.getElementById('team-keys')!==box || !out?.isConnected || document.getElementById('new-api-key')!==out){this.dispose();return false;}
      if(session!==owner || owner.role!=='team' || !loginId || owner.loginId!==loginId || !ownsBrowserLogin(owner)){this.lock();return false;}
      // Logout can revoke the captured login epoch without changing the browser's owner marker.
      if(authorization && !authorization.hasAuthorization()){this.lock();return false;}
      return true;
    },
    async request(path,body){
      if(!this.valid())throw new Error(teamKeyLoginNotice);
      try{return await api(path,body,{loginId});}
      catch(error){if([401,403,428].includes(error.status) && currentTeamKeys===this)this.lock();throw error;}
    }
  };
  currentTeamKeys=context;return context;
}
function revealPendingTeamKey(view){
  if(!view.valid() || !view.pendingKey)return;const result=view.pendingKey;view.pendingKey=null;
  view.key=result.key;
  const out=view.out;
  out.innerHTML=`<div class="code-output"><strong>新密钥 · ${esc(result.item.name)}</strong><code>${esc(view.key)}</code><p>仅显示这一次，离开或刷新页面后无法再查看。请立即复制保存。</p><button type="button" class="btn secondary small" id="copy-api-key">复制密钥</button>${setupSnippets(view.data,view.key)}</div>`;
  const copy=document.getElementById('copy-api-key');
  copy.onclick=async()=>{
    if(!view.valid() || view.copying || !view.key || !copy.isConnected || document.getElementById('copy-api-key')!==copy)return;
    const value=view.key;view.copying=true;copy.disabled=true;
    try{
      await view.request('team/keys');
      if(!view.valid() || view.key!==value || !copy.isConnected || document.getElementById('copy-api-key')!==copy)return;
      await navigator.clipboard.writeText(value);
      if(view.valid() && view.key===value)toast('密钥已复制。');
    }catch(error){if(view.valid())toast('复制未完成，请核对登录身份后重试。'+error.message);}
    finally{view.copying=false;if(view.valid() && copy.isConnected)copy.disabled=false;}
  };
  out.scrollIntoView({block:'center'});
}
async function loadTeamKeys(version,context=null){
  const view=context || createTeamKeyContext(version),box=view.box;
  if(!view.valid())return false;
  const generation=++view.generation;
  const alive=()=>view.valid() && generation===view.generation;
  let data;
  try{data=await view.request('team/keys');}
  catch(error){if(alive()){
    box.innerHTML=`<h2>AI API 密钥</h2><p class="small-note" role="status">${view.pendingKey?'密钥已创建，正在等待身份验证；请保持本页，重试验证不会创建第二把密钥。':''}${esc(error.message)}</p><button type="button" class="btn secondary small" id="retry-team-keys">重试读取 / 验证</button>`;
    document.getElementById('retry-team-keys').onclick=async()=>{if(!view.valid() || view.busy)return;view.busy=true;try{await loadTeamKeys(version,view);}finally{view.busy=false;if(view.valid())view.updateButtons?.();}};
  }return false;}
  if(!alive())return false;
  if(!data.enabled){view.key='';view.pendingKey=null;view.out.innerHTML='';box.innerHTML='<h2>AI API 密钥</h2><p class="small-note">本队 API 访问权限尚未开通或已停用，请联系组织方。</p>';return false;}
  view.data=data;
  apiCurrency=data.currency || 'USD';
  const usage=data.usage,keys=data.keys,full=keys.length>=data.maxKeys,models=Object.entries(data.models).map(([kind,list])=>`${kind==='openai'?'OpenAI 兼容':'Anthropic'}：${list.map(esc).join('、')}`).join('<br>');
  box.innerHTML=`<h2>AI API 密钥</h2>${data.announcement?`<div class="info-banner warning"><strong>组织方公告</strong><br>${esc(data.announcement)}</div>`:''}<p class="small-note">OpenAI 兼容 SDK 的 base_url：<code>${esc(data.baseUrl)}/v1</code><br>Anthropic SDK 的 base_url：<code>${esc(data.baseUrl)}</code><br>${models}</p>${billingNote(data.billing)}${metrics([[billingAmount(data.billing24h,usage.used24h),'近24小时全队已用'],[billingAmount(data.billing,usage.usedTotal),'累计全队已用'],[`${keys.length} / ${data.maxKeys}`,'有效密钥'],[data.expiresAt===null?'未设置':beijingTime(data.expiresAt),'访问到期（北京时间）']])}${data.logContent?'<div class="info-banner">组织方会记录每次 API 调用的请求与回复内容（不含密钥），用于用量核对与安全审查，请勿发送个人隐私或敏感信息。</div>':''}<div class="info-banner warning">密钥只在创建时显示一次，请放入环境变量，切勿提交到代码仓库；疑似泄露请立即删除并重新创建。</div><div class="invite-list">${keys.map(k=>`<div class="invite-row"><span>${esc(k.name)}<small><code>${esc(k.masked)}</code> · 近24小时 ${billingAmount(k.billing24h,k.used24h)} · 累计 ${billingAmount(k.billing,k.usedTotal)} · ${Number(k.requests)} 次请求 · 最近使用 ${esc(beijingTime(k.lastUsedAt))}</small></span><button type="button" class="btn danger small" data-key-delete="${esc(k.id)}">删除</button></div>`).join('') || '<p class="small-note">尚未创建密钥。</p>'}</div>${setupSnippets(data,'<你的密钥>')}<h3>本队最近调用</h3><div id="team-calls"><p class="small-note">读取中…</p></div><form id="key-form"><div class="settings-row"><label class="field">新密钥名称<input name="name" required maxlength="40" placeholder="例如：alice-laptop" ${full?'disabled':''}></label><button class="btn small" type="submit" ${full?'disabled':''}>创建密钥 ↗</button></div><p class="form-error" role="alert"></p></form>`;
  const form=document.getElementById('key-form'),submit=form.querySelector('button'),deletes=[...box.querySelectorAll('[data-key-delete]')];
  view.updateButtons=()=>{if(alive()){submit.disabled=full || view.busy;deletes.forEach(button=>button.disabled=view.busy);}};
  view.updateButtons();
  form.onsubmit=async event=>{
    event.preventDefault();if(!alive() || view.busy || full || !form.isConnected || document.getElementById('key-form')!==form)return;
    view.busy=true;view.updateButtons();formError(form,'');
    try{
      const result=await view.request('team/keys',{name:form.elements.name.value.trim()});
      if(!alive())return;
      view.pendingKey=result;view.key='';view.out.innerHTML='';
      // Only a protected read can reveal this captured one-time response.
      await loadTeamKeys(version,view);
    }catch(error){if(alive() && form.isConnected)formError(form,error.message);}
    finally{view.busy=false;if(view.valid())view.updateButtons?.();}
  };
  deletes.forEach(button=>button.onclick=async()=>{
    if(!alive() || view.busy || !button.isConnected || !confirm('删除后使用该密钥的程序立即无法调用，确认删除？') || !alive())return;
    view.busy=true;view.updateButtons();
    try{
      await view.request('team/keys/delete',{id:button.dataset.keyDelete});
      if(!alive())return;
      view.key='';view.out.innerHTML='';toast('密钥已删除。');await loadTeamKeys(version,view);
    }catch(error){if(alive())formError(form,error.message);}
    finally{view.busy=false;if(view.valid())view.updateButtons?.();}
  });
  const calls=document.getElementById('team-calls');
  view.request('team/keys/logs').then(logs=>{if(alive() && calls.isConnected && document.getElementById('team-calls')===calls)calls.innerHTML=callTable(logs.items,false);})
    .catch(error=>{if(alive() && calls.isConnected && document.getElementById('team-calls')===calls)calls.innerHTML=`<p class="small-note">${esc(error.message)}</p>`;});
  revealPendingTeamKey(view);
  return true;
}

function setupSnippets(info,key){if(!info)return '';const base=info.baseUrl,claude=(info.models.anthropic || [])[0],openai=(info.models.openai || [])[0],tabs=[];
  if(claude){tabs.push(['Claude Code',`# 终端临时使用（macOS / Linux）\nexport ANTHROPIC_BASE_URL="${base}"\nexport ANTHROPIC_AUTH_TOKEN="${key}"\nexport ANTHROPIC_MODEL="${claude}"\nclaude\n\n# 或写入 ~/.claude/settings.json\n{\n  "env": {\n    "ANTHROPIC_BASE_URL": "${base}",\n    "ANTHROPIC_AUTH_TOKEN": "${key}",\n    "ANTHROPIC_MODEL": "${claude}"\n  }\n}`]);
    tabs.push(['Anthropic Python',`from anthropic import Anthropic\n\nclient = Anthropic(base_url="${base}", api_key="${key}")\nmessage = client.messages.create(\n    model="${claude}", max_tokens=1024,\n    messages=[{"role": "user", "content": "你好"}],\n)\nprint(message.content[0].text)`]);}
  if(openai){tabs.push(['OpenAI Python',`from openai import OpenAI\n\nclient = OpenAI(base_url="${base}/v1", api_key="${key}")\nreply = client.chat.completions.create(\n    model="${openai}",\n    messages=[{"role": "user", "content": "你好"}],\n)\nprint(reply.choices[0].message.content)`]);
    tabs.push(['curl',`curl ${base}/v1/chat/completions \\\n  -H "Authorization: Bearer ${key}" \\\n  -H "Content-Type: application/json" \\\n  -d '{"model": "${openai}", "messages": [{"role": "user", "content": "你好"}]}'`]);}
  return `<div class="setup-snippets"><h3>接入示例</h3>${tabs.map(([name,code])=>`<details><summary>${esc(name)}</summary><pre class="log-content">${esc(code)}</pre></details>`).join('')}<p class="small-note">密钥请放在环境变量或 .env 文件中，并把 .env 加入 .gitignore。</p></div>`;}
// Readable view of a stored call: the full request context and the reply rebuilt from JSON or a stream.
