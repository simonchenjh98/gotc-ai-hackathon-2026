import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

const source = readFileSync(new URL('../../frontend/static/site/button-states.js', import.meta.url), 'utf8');
const buttonAction = vm.runInNewContext(source + '\nbuttonAction');

for (const outcome of ['resolve', 'reject']) {
  test(`pending form action blocks keyboard resubmission and releases its state on ${outcome}`, async () => {
    const attributes = new Map();
    const submitter = {
      getAttribute: key => attributes.get(key), setAttribute: (key, value) => attributes.set(key, value),
      removeAttribute: key => attributes.delete(key), disabled: false
    };
    let settle, calls = 0, prevented = 0;
    const gate = new Promise((resolve, reject) => { settle = outcome === 'reject' ? reject : resolve; });
    const handler = buttonAction(() => { calls++; return gate; });
    const event = {submitter, preventDefault: () => { prevented++; }};
    const operation = handler(event);
    assert.equal(attributes.get('aria-busy'), 'true');
    handler(event);
    assert.equal(calls, 1);
    assert.equal(prevented, 1);
    submitter.disabled = true;
    settle(new Error('request failed'));
    if (outcome === 'reject') await assert.rejects(operation, /request failed/);
    else await operation;
    assert.equal(attributes.has('aria-busy'), false);
    assert.equal(submitter.disabled, true);
  });
}
