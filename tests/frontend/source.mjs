import {readFileSync} from 'node:fs';

const repoRoot = new URL('../../', import.meta.url);
const sources = JSON.parse(readFileSync(new URL('./sources.json', import.meta.url), 'utf8'));

// Older controller fixtures exercise a complete script group in one VM context.
// The routing tests separately execute the real page and its script loader.
export function readSource(group) {
  const paths = sources[group];
  if (!Array.isArray(paths) || paths.length === 0) {
    throw new Error(`Missing frontend source group: ${group}`);
  }
  return paths.map(path => readFileSync(new URL(path, repoRoot), 'utf8')).join('\n');
}

// Controller fixtures preload every group into their VM. The browser-entry tests
// run with the actual loader and script tags, including route changes and errors.
export function platformFixtureSource({listeners = false} = {}) {
  const source = readSource('platform.js');
  const boundary = listeners
    ? '\nstartPlatform();'
    : "window.addEventListener('hashchange'";
  const end = source.indexOf(boundary);
  if (end < 0) throw new Error('Missing platform bootstrap boundary');
  return source.slice(0, end) + '\nloadPlatformAssets = async () => {};\n';
}
