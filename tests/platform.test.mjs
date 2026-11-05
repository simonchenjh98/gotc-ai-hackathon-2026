import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('../platform.js', import.meta.url), 'utf8').split("window.addEventListener('hashchange'")[0];
function renderer(screen = false, elapsed = 0) {
  const context = vm.createContext({
    document: {getElementById: () => ({}), body: {classList: {contains: () => screen}}},
    location: {href: 'https://example.test/gotc/platform.html#board'}, URL,
    Date, setTimeout, clearTimeout,
  });
  vm.runInContext(source + `\nscreenStarted = Date.now() - ${elapsed}; globalThis.render = boardMarkup;`, context);
  return data => context.render(data);
}
const cfg = {minJudges:5, resultsPublished:true, scoringOpen:true};
const team = (n, extra = {}) => ({id:`T${n}`,number:n,name:`TEAM-${n}`,project:'project',judgeCount:5,score:80,rank:n,audienceVotes:0,...extra});

test('board presents rank order, then waiting teams, preserving a zero score', () => {
  const html = renderer()({teams:[team(1,{rank:null,score:null}),team(3,{rank:2,score:0}),team(2,{rank:1,score:90})],config:cfg});
  assert.ok(html.indexOf('TEAM-2') < html.indexOf('TEAM-3'));
  assert.ok(html.indexOf('TEAM-3') < html.indexOf('TEAM-1'));
  assert.match(html,/0\.00/);
  assert.match(html,/等待评审/);
});

test('public team text cannot inject HTML or disclose unprovided judge data', () => {
  const html = renderer()({teams:[team(1,{name:'<img src=x onerror=alert(1)>',project:'<script>secret</script>'})],config:cfg});
  assert.ok(!html.includes('<img src=x'));
  assert.ok(!html.includes('<script>secret'));
  assert.match(html,/&lt;img/);
});

test('screen mode rotates eight teams and leaves full metrics intact', () => {
  const data = {teams:Array.from({length:10},(_,i)=>team(i+1)),config:cfg};
  const first = renderer(true)(data);
  const second = renderer(true,13000)(data);
  assert.match(first,/第 1 \/ 2 屏/);
  assert.ok(first.includes('TEAM-8'));
  assert.ok(!first.includes('TEAM-9'));
  assert.match(second,/第 2 \/ 2 屏/);
  assert.ok(second.includes('TEAM-9'));
  assert.ok(!second.includes('TEAM-1<'));
  assert.match(second,/>10<\/strong>/);
});

test('unpublished scores and an empty event have explicit states', () => {
  const html = renderer()({teams:[team(1,{score:null,rank:null})],config:{...cfg,resultsPublished:false}});
  assert.match(html,/暂未发布/);
  assert.match(renderer()({teams:[],stats:{teamCount:0},config:cfg}),/本届队伍尚未录入/);
});

test('top twelve board uses event-wide statistics and honest pending copy',()=>{
  const data={teams:[team(1)],stats:{teamCount:17,submittedScores:70,audienceVotes:36},config:cfg};
  const html=renderer()(data);
  for(const value of [17,70,36])assert.ok(html.includes(`<strong>${value}</strong>`));
  assert.match(html,/同分按队号升序.*不追加同分/);
  const pending=renderer()({...data,teams:[]});
  assert.match(pending,/尚无队伍达到/);assert.doesNotMatch(pending,/本届队伍尚未录入/);
  assert.match(renderer()({...data,teams:[],config:{...cfg,resultsPublished:false}}),/暂未发布/);
});
