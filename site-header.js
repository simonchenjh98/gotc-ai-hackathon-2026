(() => {
  'use strict';
  const header = document.querySelector('[data-site-header]');
  if (!header) return;
  const page = header.dataset.page;
  const local = new URL('./', location.href);
  // The same homepage is also published on GitHub Pages, where the platform lives separately.
  const platform = page === 'home' && !local.pathname.startsWith('/gotc/')
    ? new URL(header.dataset.platformBase, local) : local;
  const entry = hash => page === 'platform' ? `#${hash}` : new URL(`platform.html#${hash}`, platform).href;
  const home = hash => new URL(`index.html${hash ? `#${hash}` : ''}`, local).href;
  const link = (label, href, key, route = '') => `<a href="${href}" data-header-page="${key}"${route ? ` data-route="${route}"` : ''}>${label}</a>`;
  header.innerHTML = `
    <a class="gotc-brand" href="${home('')}" aria-label="GOTC 2026 活动首页">
      <span class="gotc-brand-mark" aria-hidden="true"><i></i><i></i><i></i><i></i></span>
      <span><strong>GOTC <b>2026</b></strong><small>百格成城 · 开源 AI 黑客松</small></span>
    </a>
    <button class="gotc-menu-toggle" type="button" aria-expanded="false" aria-controls="gotc-nav">菜单 <span aria-hidden="true">＋</span></button>
    <nav class="gotc-nav" id="gotc-nav" aria-label="主导航">
      ${link('活动首页', home(''), 'home')}
      ${link('组队登记', new URL('teams.html', platform).href, 'teams')}
      ${link('选手工作台', entry('team'), 'team', 'team')}
      ${link('领取提交码', entry('claim'), 'claim', 'claim')}
      ${link('本队查分', entry('team-score'), 'team-score', 'team-score')}
      ${link('成绩看板', entry('board'), 'board', 'board')}
      <details class="gotc-more"><summary>更多入口</summary><div class="gotc-more-links">
        ${link('关于活动', home('about'), 'about')}
        ${link('活动日程', home('schedule'), 'schedule')}
        ${link('评分细则', home('judging'), 'judging')}
        ${link('选手指引', home('participants'), 'participants')}
        ${link('专业评分', entry('judge'), 'judge', 'judge')}
        ${link('展示评审记录', entry('judge-exhibition'), 'judge-exhibition', 'judge-exhibition')}
        ${link('展示进度（未计分）', entry('board-exhibition'), 'board-exhibition', 'board-exhibition')}
        ${link('路演评审记录', entry('judge-roadshow'), 'judge-roadshow', 'judge-roadshow')}
        ${link('路演进度（未计分）', entry('board-roadshow'), 'board-roadshow', 'board-roadshow')}
        ${link('观众投票', entry('audience'), 'audience', 'audience')}
        ${link('组织方', entry('admin'), 'admin', 'admin')}
      </div></details>
    </nav>
    <div id="account"></div>`;
  const menu = header.querySelector('.gotc-menu-toggle');
  const nav = header.querySelector('.gotc-nav');
  const more = header.querySelector('.gotc-more');
  function closeMenu() {
    menu.setAttribute('aria-expanded', 'false');
    nav.classList.remove('is-open');
    more.open = false;
  }
  function markCurrent() {
    const current = page === 'platform' ? location.hash.slice(1) || 'board' : page;
    header.querySelectorAll('[data-header-page]').forEach(a => {
      const active = a.dataset.headerPage === current;
      a.classList.toggle('active', active);
      if (active) a.setAttribute('aria-current', 'page');
      else a.removeAttribute('aria-current');
    });
  }
  menu.addEventListener('click', () => {
    const open = menu.getAttribute('aria-expanded') !== 'true';
    menu.setAttribute('aria-expanded', String(open));
    nav.classList.toggle('is-open', open);
  });
  nav.addEventListener('click', event => { if (event.target.closest('a')) closeMenu(); });
  document.addEventListener('click', event => { if (!header.contains(event.target)) closeMenu(); });
  header.addEventListener('keydown', event => {
    if (event.key !== 'Escape') return;
    const returnToMenu = menu.getAttribute('aria-expanded') === 'true';
    const returnToMore = more.open;
    closeMenu();
    if (returnToMenu) menu.focus();
    else if (returnToMore) more.querySelector('summary').focus();
  });
  window.addEventListener('hashchange', () => { closeMenu(); markCurrent(); });
  markCurrent();
})();
