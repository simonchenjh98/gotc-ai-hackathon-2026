'use strict';

const RegistrationUI = (() => {
  const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const freshCode = () => btoa(String.fromCharCode(...crypto.getRandomValues(new Uint8Array(32)))).replace(/\+/g,'-').replace(/\//g,'_').replace(/=+$/,'');
  const matches = (team, query, recruiting) => (!recruiting || team.recruiting) && [team.name,team.captain,team.school,...team.members].join(' ').toLocaleLowerCase().includes(query.trim().toLocaleLowerCase());
  function card(team) {
    return `<article class="registration-card"><header><h4>${escape(team.name)}</h4><span class="registration-number">#${String(team.number).padStart(2,'0')}</span></header><p class="registration-school">${escape(team.school)}</p><div class="registration-names">${team.members.map(name=>`<span class="${name===team.captain?'captain':''}">${escape(name)}${name===team.captain?' · 队长':''}</span>`).join('')}</div><div class="registration-badges"><span class="registration-badge">已登记 ${team.members.length} 人${team.targetSize?` / 目标 ${team.targetSize} 人`:''}</span>${team.recruiting?'<span class="registration-badge">可继续组队</span>':''}${team.flags.map(flag=>`<span class="registration-badge">${escape(flag)}</span>`).join('')}</div>${team.project?`<p class="registration-project"><strong>${escape(team.project)}</strong>${team.summary?`<br>${escape(team.summary)}`:''}</p>`:team.summary?`<p class="registration-project">${escape(team.summary)}</p>`:''}<button class="reg-button secondary" type="button" data-reg-edit="${escape(team.id)}">这是本队，补充资料</button></article>`;
  }
  function markup() {
    return `<div class="registration-layout"><section class="registration-panel"><div class="registration-tabs"><button class="reg-button" type="button" data-reg-new>创建新队伍</button><button class="reg-button secondary" type="button" data-reg-manage>管理已有队伍</button></div><div data-reg-open hidden class="registration-management"><h3>补充本队资料</h3><p class="registration-note" data-reg-open-hint>新登记的队伍请使用登记成功时的管理码。已导入队伍请向组织方领取。</p><form data-reg-code-form><label class="registration-field">队伍管理码<input name="code" required autocomplete="off" type="password" maxlength="80" spellcheck="false"></label><button class="reg-button" type="submit">读取本队资料</button></form></div><div data-reg-editor><h3 data-reg-form-title>登记你的队伍</h3><p class="registration-note">由队长填写，名字可以之后补齐，项目想法可以留空。</p><div data-reg-editing hidden class="registration-editing"></div><form class="registration-form" data-reg-form><fieldset><div class="registration-fields"><label class="registration-field wide">队伍名称 *<input name="name" maxlength="80" required placeholder="想一个属于你们的队名"></label><label class="registration-field">队长姓名 *<input name="captain" maxlength="100" autocomplete="name" required></label><label class="registration-field">目标人数（含队长）<select name="targetSize"><option value="3">3 人</option><option value="4">4 人</option><option value="5">5 人</option></select></label><label class="registration-field wide">队长所属学校 *<input name="school" maxlength="200" autocomplete="organization" required></label><label class="registration-field wide">队长手机号（选填）<input name="phone" type="tel" inputmode="tel" maxlength="11" pattern="1[3-9][0-9]{9}" autocomplete="tel"><small>仅队长和组织方可查看，不公开展示。</small></label></div><fieldset class="registration-members"><legend>队员姓名（不含队长，没找齐可留空）</legend><div class="registration-fields">${[1,2,3,4].map(i=>`<label class="registration-field">队员 ${i}<input name="member${i}" maxlength="100" autocomplete="off"></label>`).join('')}</div></fieldset><div class="registration-fields"><label class="registration-field wide">项目 / 选题名称（选填）<input name="project" maxlength="140" placeholder="还没想好，可以先留空"></label><label class="registration-field wide">一句话说明（选填）<textarea name="summary" maxlength="600" placeholder="你们想做什么？"></textarea></label></div><p class="registration-note">请先与队友确认，姓名和学校将显示在下方队伍名单中。每人只参加一队。</p><button class="reg-button" type="submit" data-reg-save>创建队伍并保存</button></fieldset></form></div><p class="registration-error" data-reg-error role="alert"></p><div class="registration-success" data-reg-receipt hidden role="status"></div><p class="registration-privacy">这里保存的是组队资料。领取提交码、AI 工具及正式提交作品，请前往<a href="https://8.141.114.201/gotc/platform.html#claim">现场核验入口</a>。管理码遗失请联系现场组织方。</p></section><section class="registration-panel" aria-label="已登记队伍"><div class="registration-directory-top"><div><h3>找到你的队伍</h3><span class="registration-count" data-reg-count>正在读取…</span></div><button class="reg-button secondary" type="button" data-reg-refresh>刷新名单</button></div><label class="registration-field">搜索队名、姓名或学校<input type="search" data-reg-search placeholder="输入队名、姓名或学校"></label><label class="registration-filter"><input type="checkbox" data-reg-recruiting>只看还可组队的队伍</label><p class="registration-error" data-reg-list-error role="status"></p><div class="registration-list" data-reg-list aria-live="polite"></div></section></div>`;
  }
  async function install(host) {
    host.innerHTML=markup();
    const q=selector=>host.querySelector(selector), form=q('[data-reg-form]'), error=q('[data-reg-error]'), receipt=q('[data-reg-receipt]');
    const endpoint=new URL(host.dataset.api,location.href).href, storageKey='gotc-registration-code:v1:'+endpoint;
    let teams=[], editing=null, code='', pending=false, generation=0;
    try {code=sessionStorage.getItem(storageKey)||'';} catch { /* The receipt remains available without storage. */ }
    if(!/^[A-Za-z0-9_-]{43}$/.test(code))code='';
    function remember() {try {sessionStorage.setItem(storageKey,code);return true;}catch {return false;}}
    async function request(action,body) {
      const response=await fetch(endpoint+(action?'/'+action:''),{method:body?'POST':'GET',credentials:'omit',cache:'no-store',headers:body?{'Content-Type':'application/json'}:{},...(body?{body:JSON.stringify(body)}:{})});
      const data=await response.json();
      if(!response.ok)throw new Error(data.error?.message || data.error || '暂时无法连接，请稍后重试');
      return data;
    }
    function showList() {
      const rows=teams.filter(t=>matches(t,q('[data-reg-search]').value,q('[data-reg-recruiting]').checked));
      q('[data-reg-count]').textContent=`共 ${teams.length} 支队伍 · 当前显示 ${rows.length} 支`;
      q('[data-reg-list]').innerHTML=rows.length?rows.map(card).join(''):'<p class="registration-empty">没有找到匹配的队伍。可以换个名字搜索，或在左侧创建新队伍。</p>';
    }
    async function refresh() {
      const requestId=++generation;const button=q('[data-reg-refresh]');button.disabled=true;
      try {const data=await request('');if(requestId!==generation)return;teams=data.teams;showList();q('[data-reg-list-error]').textContent='';}
      catch(e){if(requestId===generation)q('[data-reg-list-error]').textContent='名单暂时未能更新：'+e.message;}
      finally {if(requestId===generation)button.disabled=false;}
    }
    function fill(team) {
      editing=team;
      for(const key of ['name','captain','school','phone','project','summary'])form.elements[key].value=team[key]||'';
      form.elements.targetSize.value=String(Math.max(3,team.targetSize||team.members.length,team.members.length));
      const members=team.members.filter(name=>name!==team.captain);
      [1,2,3,4].forEach((i)=>form.elements['member'+i].value=members[i-1]||'');
      q('[data-reg-form-title]').textContent='补充本队资料';q('[data-reg-save]').textContent='保存本队资料';
      q('[data-reg-editing]').hidden=false;q('[data-reg-editing]').textContent=`正在修改 #${team.number} ${team.name} · 请核对队伍身份`;
      q('[data-reg-open]').hidden=true;q('[data-reg-editor]').hidden=false;
    }
    function showReceipt() {
      const stored=remember();receipt.hidden=false;
      receipt.innerHTML=`<strong>已保存：${escape(editing.name)}</strong><p>请队长保存本队管理码，之后可以补充姓名、修改队名。</p><code>${escape(code)}</code><button class="reg-button secondary" type="button" data-reg-copy>复制管理码</button><p class="registration-note">${stored?'本标签页已记住管理码；关闭页面前仍请另行保存。':'浏览器无法记住管理码，请立即复制保存。'} 管理码只用于组队资料，请勿公开转发。</p>`;
      q('[data-reg-copy]').onclick=async()=>{try {await navigator.clipboard.writeText(code);q('[data-reg-copy]').textContent='已复制';}catch {q('[data-reg-copy]').textContent='请选中上方管理码手动复制';}};
    }
    function lock(value) {pending=value;form.querySelector('fieldset').disabled=value;for(const el of host.querySelectorAll('[data-reg-new],[data-reg-manage],[data-reg-code-form] button,[data-reg-code-form] input'))el.disabled=value;}
    q('[data-reg-new]').onclick=()=>{
      if(pending)return;
      if((editing||form.elements.name.value)&&!confirm('创建另一支队伍前，请确认已有资料已保存，并保留原队伍管理码。继续？'))return;
      editing=null;code='';try{sessionStorage.removeItem(storageKey);}catch{/* No saved code. */}
      form.reset();q('[data-reg-open]').hidden=true;q('[data-reg-editor]').hidden=false;q('[data-reg-editing]').hidden=true;receipt.hidden=true;error.textContent='';
      q('[data-reg-form-title]').textContent='登记你的队伍';q('[data-reg-save]').textContent='创建队伍并保存';
    };
    function manage(name='') {
      if(pending)return;
      q('[data-reg-open]').hidden=false;q('[data-reg-editor]').hidden=true;error.textContent='';receipt.hidden=true;
      q('[data-reg-open-hint]').textContent=(name?`补充「${name}」资料：`:'')+'请输入队长保存的管理码。已导入队伍请向现场组织方领取；这不是作品提交码。';
      q('[data-reg-code-form]').elements.code.value=code;q('[data-reg-code-form]').elements.code.focus();
    }
    q('[data-reg-manage]').onclick=()=>manage();
    q('[data-reg-list]').onclick=e=>{const button=e.target.closest('[data-reg-edit]');if(button){manage(teams.find(t=>t.id===button.dataset.regEdit)?.name);q('[data-reg-open]').scrollIntoView({block:'center',behavior:'smooth'});}};
    q('[data-reg-code-form]').onsubmit=async e=>{
      e.preventDefault();if(pending)return;lock(true);error.textContent='';
      const entered=e.currentTarget.elements.code.value.trim();
      try {const result=await request('open',{code:entered});code=entered;fill(result.team);showReceipt();}
      catch(e){error.textContent=e.message;}finally{lock(false);}
    };
    form.onsubmit=async e=>{
      e.preventDefault();if(pending)return;
      const team=Object.fromEntries(['name','captain','school','phone','project','summary'].map(k=>[k,form.elements[k].value.trim()]));
      team.targetSize=Number(form.elements.targetSize.value);team.members=[1,2,3,4].map(i=>form.elements['member'+i].value.trim()).filter(Boolean);
      if(new Set([team.captain,...team.members]).size>team.targetSize){error.textContent='实际人数超过目标人数，请先核对人数与名单。';return;}
      lock(true);error.textContent='';
      try {
        if(!code)code=freshCode();remember();
        const result=await request(editing?'save':'create',{team,code,...(editing?{expectedRevision:editing.revision}:{})});
        fill(result.team);showReceipt();await refresh();
      } catch(e){error.textContent=e.message+'\n当前输入已保留；若网络断开，可再次点击保存，或用本标签页管理码读取资料。';}
      finally{lock(false);}
    };
    q('[data-reg-search]').oninput=showList;q('[data-reg-recruiting]').onchange=showList;q('[data-reg-refresh]').onclick=refresh;
    await refresh();
    if(code){try {const result=await request('open',{code});fill(result.team);showReceipt();}catch{/* Preserve the pending create code for an idempotent retry. */}}
  }
  return {escape,matches,card,install};
})();

if(typeof document!=='undefined')document.querySelectorAll('[data-team-registration]').forEach(host=>RegistrationUI.install(host));

function installRegistrationAdmin(version) {
  const owner=session;
  root.insertAdjacentHTML('beforeend','<section class="surface" id="registration-admin"><h2>组队登记与管理码</h2><p>查看联系方式及待核对项目；原表数据保存在私有数据库中。重发管理码后旧码立即失效。</p><button class="btn secondary small" data-registration-load>读取 / 刷新登记名单</button><p data-registration-message role="status"></p><div data-registration-content></div></section>');
  const box=document.getElementById('registration-admin'),content=box.querySelector('[data-registration-content]'),message=box.querySelector('[data-registration-message]');
  const current=()=>box.isConnected && route==='admin' && version===routeVersion && session===owner && ownsBrowserLogin(owner);
  let rows=[],reading=0;
  async function load(){const serial=++reading;try{const data=await api('admin/registration');if(!current()||serial!==reading)return;rows=data.teams;content.innerHTML=`<div class="table-scroll"><table><thead><tr><th>队伍</th><th>队长 / 学校</th><th>联系电话</th><th>姓名 / 待核对</th><th>管理码</th></tr></thead><tbody>${rows.map(t=>`<tr><td>${esc(t.number)} · ${esc(t.name)}</td><td>${esc(t.captain)}<br>${esc(t.school)}</td><td>${esc(t.phone||'未填写')}${t.phoneNeedsReview?'<br>号码格式待核对':''}</td><td>${esc(t.members.join('、'))}<br>${esc(t.flags.join('；'))}</td><td><button class="btn secondary small" data-registration-rotate="${esc(t.id)}">重发管理码</button></td></tr>`).join('')}</tbody></table></div>`;message.textContent=`共 ${rows.length} 支登记队伍。`;}catch(e){if(current())message.textContent=e.message;}}
  box.querySelector('[data-registration-load]').onclick=load;
  content.onclick=async e=>{const button=e.target.closest('[data-registration-rotate]');if(!button||button.disabled||!current())return;const team=rows.find(t=>t.id===button.dataset.registrationRotate);if(!team||!confirm(`重发「${team.name}」的组队管理码？旧管理码将立即失效，作品提交码不变。`))return;button.disabled=true;try{const result=await api('admin/registration/rotate',{teamId:team.id,expectedRevision:team.revision});if(!current())return;await load();if(current())message.textContent=`${team.name} 的新管理码（请私下交给队长）：${result.code}`;}catch(e){if(current())message.textContent=e.message;}finally{if(current())button.disabled=false;}};
}
