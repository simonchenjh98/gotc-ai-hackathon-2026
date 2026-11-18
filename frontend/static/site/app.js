const tabs = [...document.querySelectorAll('[data-day]')];
const panels = [...document.querySelectorAll('.schedule-panel')];
const tablist = document.querySelector('.schedule-tabs');
tablist?.setAttribute('role', 'tablist');
function setDay(tab, focus = false) {
  tabs.forEach(t => { t.setAttribute('aria-selected', String(t === tab)); t.tabIndex = t === tab ? 0 : -1; t.classList.toggle('active', t === tab); });
  panels.forEach(p => { p.hidden = p.id !== tab.dataset.day; });
  if (focus) tab.focus();
}
tabs.forEach((tab, index) => {
  tab.setAttribute('role', 'tab');
  tab.setAttribute('aria-controls', tab.dataset.day);
  document.getElementById(tab.dataset.day).setAttribute('role', 'tabpanel');
  tab.addEventListener('click', () => setDay(tab));
  tab.addEventListener('keydown', e => {
    let next;
    if (e.key === 'ArrowRight') next = (index + 1) % tabs.length;
    if (e.key === 'ArrowLeft') next = (index - 1 + tabs.length) % tabs.length;
    if (e.key === 'Home') next = 0;
    if (e.key === 'End') next = tabs.length - 1;
    if (next !== undefined) { e.preventDefault(); setDay(tabs[next], true); }
  });
});
if (tabs.length) setDay(tabs[0]);
const checks = [...document.querySelectorAll('[data-check]')];
const storageKey = 'gotc-2026-submission-checklist';
try { const values = JSON.parse(localStorage.getItem(storageKey) || '{}'); checks.forEach(c => { c.checked = values[c.dataset.check] === true; }); } catch { /* Private browsing can disable storage. */ }
function updateChecklist() {
  document.getElementById('check-count').textContent = `${checks.filter(c => c.checked).length} / ${checks.length}`;
  try { localStorage.setItem(storageKey, JSON.stringify(Object.fromEntries(checks.map(c => [c.dataset.check, c.checked])))); } catch { /* Checklist remains usable without persistence. */ }
}
checks.forEach(c => c.addEventListener('change', updateChecklist));
updateChecklist();
let toastTimer;
function toast(message) { const el = document.getElementById('toast'); el.textContent = message; el.classList.add('visible'); clearTimeout(toastTimer); toastTimer = setTimeout(() => el.classList.remove('visible'), 3500); }
document.getElementById('share-button')?.addEventListener('click', async () => {
  const url = new URL('./', location.href).href;
  try { await navigator.clipboard.writeText(url); toast('官网链接已复制，可以发给队友了。'); }
  catch { toast(`官网链接：${url}`); }
});
