import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync, cpSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {execFileSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import test from 'node:test';
import vm from 'node:vm';

const project = fileURLToPath(new URL('../../', import.meta.url));
const source = join(project, 'frontend');
const buildScript = `
from pathlib import Path
from tools.build_frontend import build_frontend
import sys
source, dest = map(Path, sys.argv[1:3])
files = [p.relative_to(source) for p in source.rglob('*') if p.is_file()]
build_frontend(source, dest, files, pages=len(sys.argv) > 3,
               public_origin=sys.argv[3] if len(sys.argv)>3 else None)
`;

function build(input, output, origin) {
  return execFileSync('python3', ['-B', '-c', buildScript, input, output, ...(origin ? [origin] : [])],
    {cwd:project, stdio:'pipe'});
}

test('hashed output is repeatable, rewrites eager and lazy paths, and updates only affected content', async t => {
  const tmp = mkdtempSync(join(tmpdir(), 'gotc-frontend-build-'));
  t.after(() => rmSync(tmp, {recursive:true, force:true}));
  const first = join(tmp,'first'), second = join(tmp,'second'), copy = join(tmp,'source');
  build(source, first);
  build(source, second);
  const firstManifest = readFileSync(join(first,'asset-manifest.json'),'utf8');
  assert.equal(firstManifest, readFileSync(join(second,'asset-manifest.json'),'utf8'));
  const names = readdirSync(first,{recursive:true});
  const {assets,version} = JSON.parse(firstManifest);
  assert.equal(version,1);
  for (const [logical,{path,integrity}] of Object.entries(assets)) {
    const bytes = readFileSync(join(first,path));
    const fingerprint = createHash('sha256').update(bytes).digest('hex').slice(0,16);
    assert.equal(path,logical.replace(/\.(js|css)$/,'.'+fingerprint+'.$1'));
    assert.equal(integrity,'sha384-'+createHash('sha384').update(bytes).digest('base64'));
    assert.ok(!names.includes(logical));
    assert.deepEqual(bytes,readFileSync(join(second,path)));
  }
  for (const page of ['index.html','teams.html','platform.html']) {
    const html=readFileSync(join(first,page),'utf8');
    const entries=Array.from(html.matchAll(/(?:src|href)="([^"]+\.(?:js|css))"([^>]*)>/g));
    assert.ok(entries.length);
    for(const [,path,attributes] of entries) {
      const asset=Object.values(assets).find(a=>a.path===path);
      assert.ok(asset,path);
      assert.ok(attributes.includes(`integrity="${asset.integrity}"`));
    }
  }
  const loader = assets['static/platform/routes.js'];
  const table = assets['static/platform/route-assets.js'];
  const loaderSource = readFileSync(join(first,loader.path),'utf8');
  assert.ok(loaderSource.includes(table.integrity));
  assert.ok(!loaderSource.includes(assets['static/platform/claims/application.js'].integrity));
  const scripts = [];
  let finishTable;
  const context = vm.createContext({base:new URL('https://event.example/gotc/'),URL,
    document:{createElement:()=>({remove(){}}),head:{append(element){
      scripts.push(element);
      if (element.src?.endsWith('/'+table.path)) {
        finishTable = () => {
          vm.runInContext(readFileSync(join(first,table.path),'utf8'),context);
          element.onload();
        };
      } else element.onload();
    }}}});
  vm.runInContext(loaderSource,context);
  await vm.runInContext("loadPlatformAssets(routeTarget('team',{role:null}).files)",context);
  assert.equal(scripts.length,0,'a cold signed-out team screen needs no release map');
  // A tab on an old deployment must reload its HTML if the release-pinned map is gone.
  const broken = vm.runInContext("loadPlatformAssets(['claims/form.js'])",context);
  assert.equal(scripts.length,1);
  scripts[0].onerror();
  await assert.rejects(broken,error=>error.refreshDocument === true);
  const left = vm.runInContext("loadPlatformAssets(['claims/form.js'])",context);
  const right = vm.runInContext("loadPlatformAssets(['claims/form.js'])",context);
  const abandoned = vm.runInContext("loadPlatformAssets(['team/submission.js'], () => false)",context);
  assert.equal(scripts.length,2,'concurrent features share the retried map request');
  assert.ok(scripts[1].src.endsWith('/'+table.path));
  assert.equal(scripts[1].integrity,table.integrity);
  finishTable();
  await Promise.all([left,right,abandoned]);
  assert.equal(scripts.length,3,'no duplicate feature request');
  for (const role of ['team','admin','judge','audience',null]) {
    context.role=role;
    await vm.runInContext('Promise.all(Object.keys(platformRoutes).map(page=>loadPlatformAssets(routeTarget(page,{role}).files)))',context);
  }
  assert.ok(scripts.length>20);
  for (const item of scripts) {
    const path=new URL(item.src || item.href).pathname.slice('/gotc/'.length);
    assert.ok(Object.values(assets).some(a=>a.path===path && a.integrity===item.integrity),path);
  }
  cpSync(source,copy,{recursive:true});
  const leaf='static/platform/claims/application.js';
  writeFileSync(join(copy,leaf),'\n// Synthetic build fixture\n',{flag:'a'});
  const third=join(tmp,'third');
  build(copy,third);
  const changed=JSON.parse(readFileSync(join(third,'asset-manifest.json'),'utf8')).assets;
  assert.deepEqual(Object.keys(changed).filter(key=>assets[key].path!==changed[key].path).sort(),
    [leaf,'static/platform/route-assets.js','static/platform/routes.js'].sort());
  assert.notEqual(readFileSync(join(first,'platform.html'),'utf8'),readFileSync(join(third,'platform.html'),'utf8'));
});

test('Pages uses the configured HTTPS origin and integrated HTML stays same-origin', t => {
  const tmp=mkdtempSync(join(tmpdir(),'gotc-pages-origin-'));
  t.after(()=>rmSync(tmp,{recursive:true,force:true}));
  const output=join(tmp,'public');
  build(source,output,'https://stage.example:9443');
  const homepage=readFileSync(join(output,'index.html'),'utf8');
  for (const text of ['data-api="https://stage.example:9443/gotc/api/registration"',
    'href="https://stage.example:9443/gotc/platform.html#team"', 'data-platform-base="https://stage.example:9443/gotc/"']) {
    assert.ok(homepage.includes(text));
  }
  const local=join(tmp,'integrated');
  build(source,local);
  assert.ok(readFileSync(join(local,'index.html'),'utf8').includes('data-api="api/registration"'));
  assert.throws(()=>build(source,join(tmp,'invalid'),'http://stage.example'));
  assert.throws(()=>build(source,join(tmp,'invalid'),'https://stage.example/unexpected-path'));
});
