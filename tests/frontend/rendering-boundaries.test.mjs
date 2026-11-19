import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {platformFixtureSource, readSource} from './source.mjs';

test('organizer metric values and flag labels are rendered as text',()=>{
  const ctx=vm.createContext({URL,location:{href:'https://event.example/gotc/'},document:{getElementById(){}}});
  vm.runInContext(platformFixtureSource()+'\n'+readSource('platform-api-admin.js'),ctx);
  ctx.marker='A <team> & "organizer"';
  const card=vm.runInContext('apiCards([[marker,marker,marker]])',ctx);
  assert.equal(card.match(/A &lt;team&gt; &amp; &quot;organizer&quot;/g)?.length,3);
  const badge=vm.runInContext('apiBadge(marker,marker)',ctx);
  assert.equal(badge.match(/A &lt;team&gt; &amp; &quot;organizer&quot;/g)?.length,2);
});

test('history identifiers remain inside their quoted markup attributes',()=>{
  const ctx=vm.createContext({URL,location:{href:'https://event.example/gotc/'},document:{getElementById(){}}});
  vm.runInContext(platformFixtureSource(),ctx);
  ctx.marker='team "alpha" & beta';
  const html=vm.runInContext('submissionHistoryMarkup(marker)',ctx);
  assert.ok(!html.includes(ctx.marker));
  assert.ok(html.includes('id="team &quot;alpha&quot; &amp; beta-load"'));
});
