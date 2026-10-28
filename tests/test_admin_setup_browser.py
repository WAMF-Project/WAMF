"""Execute the local setup script with a small browser API stub, offline."""

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize('scenario', [
    'success', 'invalid', 'missing', 'mismatch', 'same-document',
    'replacement', 'empty-fragment', 'unrelated-fragment', 'queued-navigation',
])
def test_fragment_removed_and_only_post_body_contains_token(scenario):
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node is needed for the setup browser regression')
    script = Path(__file__).resolve().parents[1] / 'static/js/admin-setup.js'
    runner = r'''
const assert = require('assert');
const vm = require('vm');
const fs = require('fs');
const scenario = process.argv[1];
const token = 'SYNTHETIC_PRIVATE_TOKEN';
let expectedToken = token;
const calls = [];
const error = {textContent: ''}, button = {};
const listeners = {};
let submit, reset = false;
const form = {
  action: 'https://wamf.example/setup',
  elements: {password: {value: 'SYNTHETIC_PASSWORD'}, confirmation: {value: scenario === 'mismatch' ? 'different' : 'SYNTHETIC_PASSWORD'}},
  querySelector: () => button,
  addEventListener: (name, callback) => { submit = callback; },
  reset: () => { reset = true; },
};
const window = {
  location: {hash: ['missing', 'same-document', 'queued-navigation'].includes(scenario) ? '' : '#token=' + token, pathname: '/setup', replace: path => calls.push(['redirect', path])},
  history: {replaceState: (state, title, path) => { calls.push(['history', path]); window.location.hash = ''; }},
  addEventListener: (name, callback) => {
    listeners[name] = callback;
    if (name === 'DOMContentLoaded') { assert.deepEqual(calls, [['history', '/setup']]); callback(); }
  },
};
const context = {window, document: {getElementById: id => id === 'setup-form' ? form : error},
  URL, URLSearchParams,
  FormData: class { constructor() { return [['password', 'SYNTHETIC_PASSWORD'], ['confirmation', 'SYNTHETIC_PASSWORD'], ['csrf_token', 'csrf']]; } },
  fetch: async (url, options) => {
    assert.equal(url, 'https://wamf.example/setup');
    assert.equal(options.method, 'POST');
    assert.equal(options.body.get('token'), expectedToken);
    calls.push(['post', url]);
    return {redirected: scenario === 'success', ok: scenario === 'success', json: async () => ({error: 'Link invalid'})};
  },
};
Object.defineProperty(context, 'localStorage', {get() { throw Error('Must not use localStorage'); }});
Object.defineProperty(context, 'sessionStorage', {get() { throw Error('Must not use sessionStorage'); }});
Object.defineProperty(window, 'localStorage', {get() { throw Error('Must not use localStorage'); }});
Object.defineProperty(window, 'sessionStorage', {get() { throw Error('Must not use sessionStorage'); }});
vm.runInNewContext(fs.readFileSync(process.argv[2], 'utf8'), context);
const navigate = hash => {
  window.location.hash = hash;
  listeners.hashchange({newURL: 'https://wamf.example/setup' + hash});
  assert.equal(window.location.hash, '');
  assert.deepEqual(calls.at(-1), ['history', '/setup']);
};
const send = () => submit({preventDefault() {}});
(async () => {
  if (scenario === 'same-document') {
    await send();
    assert.equal(calls.length, 1);
    navigate('#token=' + token);
    await send();
    assert.equal(calls.filter(call => call[0] === 'post').length, 1);
  } else if (scenario === 'replacement') {
    for (let i = 0; i < 3; i++) {
      expectedToken = 'SYNTHETIC_REPLACEMENT_' + i;
      navigate('#token=' + expectedToken);
      await send();
    }
    assert.equal(calls.filter(call => call[0] === 'post').length, 3);
  } else if (scenario === 'empty-fragment' || scenario === 'unrelated-fragment') {
    await send();
    navigate(scenario === 'empty-fragment' ? '' : '#other=value');
    await send();
    assert.equal(calls.filter(call => call[0] === 'post').length, 1);
    assert(error.textContent.includes('Open the private setup link'));
  } else if (scenario === 'queued-navigation') {
    expectedToken = 'SYNTHETIC_LATEST_TOKEN';
    window.location.hash = '#token=' + expectedToken;
    // A queued event for an earlier navigation must neither capture an old
    // token nor erase the latest token after replaceState has removed its hash.
    const oldEvent = {newURL: 'https://wamf.example/setup#token=' + token};
    listeners.hashchange(oldEvent);
    assert.equal(window.location.hash, '#token=' + expectedToken);
    listeners.hashchange({newURL: 'https://wamf.example/setup#token=' + expectedToken});
    assert.equal(window.location.hash, '');
    listeners.hashchange(oldEvent);
    await send();
    assert.equal(calls.filter(call => call[0] === 'post').length, 1);
  } else {
    await send();
  }
  assert(!JSON.stringify(calls).includes(token));
  assert(!JSON.stringify(calls).includes('SYNTHETIC_'));
  if (scenario === 'success') { assert(reset); assert.deepEqual(calls.at(-1), ['redirect', '/login']); }
  if (scenario === 'invalid') { assert.equal(error.textContent, 'Link invalid'); assert(!reset); }
  if (scenario === 'missing' || scenario === 'mismatch') { assert.equal(calls.length, 1); assert(error.textContent); }
})().catch(error => { console.error(error); process.exitCode = 1; });
'''
    result = subprocess.run([node, '-e', runner, scenario, str(script)], capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == 0, result.stderr
