import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source=fs.readFileSync(new URL('../site-header.js',import.meta.url),'utf8');
function fixture(href,page){
  const listeners={},location={href,hash:new URL(href).hash},links=[];
  function node(){
    const classes=new Set(),attributes=new Map();
    return {listeners:{},dataset:{},classList:{toggle(name,on){on?classes.add(name):classes.delete(name);},remove:name=>classes.delete(name),contains:name=>classes.has(name)},
      getAttribute:name=>attributes.get(name),setAttribute:(name,value)=>attributes.set(name,value),removeAttribute:name=>attributes.delete(name),
      addEventListener(name,fn){this.listeners[name]=fn;},focus(){this.focused=true;}};
  }
  const menu=node(),nav=node(),more=node(),summary=node(),header=node();more.querySelector=()=>summary;
  header.dataset={page,platformBase:'https://8.141.114.201/gotc/'};
  Object.defineProperty(header,'innerHTML',{set(html){
    for(const match of html.matchAll(/<a href="([^"]+)" data-header-page="([^"]+)"(?: data-route="([^"]+)")?>/g)){
      const link=node();link.href=match[1];link.dataset={headerPage:match[2],route:match[3]};links.push(link);
    }
  }});
  header.querySelector=selector=>({'.gotc-menu-toggle':menu,'.gotc-nav':nav,'.gotc-more':more})[selector];
  header.querySelectorAll=()=>links;header.contains=target=>target===header;
  vm.runInNewContext(source,{location,URL,document:{querySelector:()=>header,addEventListener:(name,fn)=>listeners['document:'+name]=fn},window:{addEventListener:(name,fn)=>listeners[name]=fn}});
  return {header,menu,nav,more,summary,links,listeners,location};
}

test('all local pages share destinations and stay inside the gotc mount',()=>{
  const pages=[fixture('https://example.test/gotc/index.html','home'),fixture('https://example.test/gotc/teams.html','teams'),fixture('https://example.test/gotc/platform.html#team','platform')];
  const destinations=pages.map(f=>f.links.map(a=>{const url=new URL(a.href,f.location.href);return url.origin+url.pathname+url.search+url.hash;}));
  assert.deepEqual(destinations[0],destinations[1]);assert.deepEqual(destinations[1],destinations[2]);
  assert.ok(destinations[0].every(href=>href.startsWith('https://example.test/gotc/')));
  assert.deepEqual(pages.map(f=>f.links.find(a=>a.getAttribute('aria-current')==='page').dataset.headerPage),['home','teams','team']);
  const platform=pages[2];platform.location.hash='#admin';platform.listeners.hashchange();
  assert.equal(platform.links.find(a=>a.getAttribute('aria-current')==='page').dataset.headerPage,'admin');
  assert.equal(platform.links.find(a=>a.dataset.route==='claim').href,'#claim');
});

test('the static homepage keeps event links local and platform links on the live service',()=>{
  const f=fixture('https://example.github.io/gotc-ai-hackathon-2026/index.html','home');
  assert.equal(f.links.find(a=>a.dataset.headerPage==='home').href,'#');
  assert.equal(f.links.find(a=>a.dataset.headerPage==='schedule').href,'#schedule');
  assert.equal(f.links.find(a=>a.dataset.headerPage==='teams').href,'https://8.141.114.201/gotc/teams.html');
  assert.equal(f.links.find(a=>a.dataset.headerPage==='team').href,'https://8.141.114.201/gotc/platform.html#team');
});

test('homepage section navigation keeps the same document including root paths and query strings',()=>{
  for(const href of ['https://etownyouthai.com/','https://etownyouthai.com/index.html?from=group#teams',
    'https://example.test/gotc/?from=group#teams','https://example.test/gotc/index.html',
    'https://example.github.io/gotc-ai-hackathon-2026/','https://example.github.io/gotc-ai-hackathon-2026/index.html?from=group']){
    const f=fixture(href,'home'),current=new URL(href);
    for(const key of ['home','about','schedule','judging','participants']){
      const destination=new URL(f.links.find(a=>a.dataset.headerPage===key).href,href);
      assert.equal(destination.origin+destination.pathname+destination.search,current.origin+current.pathname+current.search,`${href}: ${key} must not reload the form document`);
      assert.equal(destination.hash,key==='home'?'':`#${key}`);
    }
  }
  for(const page of ['teams','platform']){
    const f=fixture(`https://example.test/gotc/${page}.html?from=group`,page);
    assert.equal(f.links.find(a=>a.dataset.headerPage==='home').href,'https://example.test/gotc/index.html');
    assert.equal(f.links.find(a=>a.dataset.headerPage==='schedule').href,'https://example.test/gotc/index.html#schedule');
  }
});

test('menu state closes on Escape, navigation and outside clicks',()=>{
  const f=fixture('https://example.test/gotc/platform.html#team','platform');
  const open=()=>{f.menu.listeners.click();f.more.open=true;assert.equal(f.menu.getAttribute('aria-expanded'),'true');assert.equal(f.nav.classList.contains('is-open'),true);};
  const closed=()=>{assert.equal(f.menu.getAttribute('aria-expanded'),'false');assert.equal(f.nav.classList.contains('is-open'),false);assert.equal(f.more.open,false);};
  open();f.header.listeners.keydown({key:'Escape'});closed();assert.equal(f.menu.focused,true);
  open();f.nav.listeners.click({target:{closest:()=>f.links[0]}});closed();
  open();f.listeners['document:click']({target:{}});closed();
  f.more.open=true;f.header.listeners.keydown({key:'Escape'});assert.equal(f.summary.focused,true);
});
