"""Exercise the actual embed link rewriter, including native browser clicks."""

import json
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from tests.html_helpers import NavigationHTML

ROOT = Path(__file__).resolve().parents[1]
REJECTED = [
    'javascript:window.unsafeExecuted=1',
    'JaVaScRiPt:window.unsafeExecuted=1',
    ' javascript:window.unsafeExecuted=1',
    '\x00\x01\t\r\nJaVaScRiPt:window.unsafeExecuted=1',
    'java\tscript:window.unsafeExecuted=1',
    'java\r\nscript:window.unsafeExecuted=1',
    'data:text/html,<script>parent.unsafeExecuted=1</script>',
    ' DaTa:text/html,unsafe',
    'vbscript:msgbox(1)', 'VbScRiPt:msgbox(1)',
    'file:///etc/passwd', 'ftp://example.com/file', 'mailto:bird@example.com',
    'blob:https://example.com/id', 'custom:unsafe',
    'http://[', 'https://', 'https://example.com:invalid/',
]
PERMITTED = [
    ('/', '/?embed=ha'),
    ('/recent?date=2026-10-28#bird', '/recent?date=2026-10-28&embed=ha#bird'),
    ('recent', '/nested/recent?embed=ha'),
    ('./recent', '/nested/recent?embed=ha'),
    ('../activity', '/activity?embed=ha'),
    ('?date=today', '/nested/page?date=today&embed=ha'),
    ('/live?embed=other', '/live?embed=ha'),
    ('#bird', '#bird'), ('#', '#'), ('', ''),
    ('https://wamf.example/activity', '/activity?embed=ha'),
    ('HTTPS://wamf.example/activity', '/activity?embed=ha'),
    ('//wamf.example/activity', '/activity?embed=ha'),
    ('http://external.example/bird?q=1#info', 'http://external.example/bird?q=1#info'),
    ('https://external.example/bird', 'https://external.example/bird'),
    ('//external.example/bird', '//external.example/bird'),
    (' HTTPS://external.example/bird', ' HTTPS://external.example/bird'),
    ('\t\r\nhttps://external.example/bird', '\t\r\nhttps://external.example/bird'),
]


@pytest.fixture
def embed_script():
    scripts = NavigationHTML((ROOT / 'templates/base.html').read_text()).scripts
    return next(script for script in scripts if 'const withEmbed' in script)


@pytest.mark.parametrize('href,expected', [(href, None) for href in REJECTED] + PERMITTED)
def test_embed_url_validation(embed_script, href, expected):
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node is needed for URL API regressions')
    runner = r'''
const assert = require('node:assert/strict');
const vm = require('node:vm');
const [script, href, expected] = JSON.parse(process.argv[1]);
const location = new URL('https://wamf.example/nested/page?embed=ha');
const anchor = {
  href,
  getAttribute(name) { assert.equal(name, 'href'); return this.href; },
  setAttribute(name, value) { assert.equal(name, 'href'); this.href = value; },
  removeAttribute(name) { assert.equal(name, 'href'); this.href = null; },
};
let ready;
vm.runInNewContext(script, {
  URL, URLSearchParams, window: {location}, document: {
    baseURI: location.href,
    body: {classList: {add(name) { assert.equal(name, 'ha-embed'); }}},
    addEventListener(name, callback) { assert.equal(name, 'DOMContentLoaded'); ready = callback; },
    querySelectorAll(selector) { assert.equal(selector, 'a[href]'); return [anchor]; },
  },
});
assert.equal(typeof ready, 'function');
ready();
assert.equal(anchor.href, expected);
'''
    result = subprocess.run([node, '-e', runner, json.dumps([embed_script, href, expected])],
                            capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == 0, result.stderr


def test_chromium_native_clicks_and_fragment_history(embed_script, tmp_path):
    installed = sorted((Path.home() / '.cache/ms-playwright').glob(
        'chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell'
    ))
    # Prefer the standalone shell for --dump-dom; desktop Chromium can hang on exit.
    browser = (str(installed[-1]) if installed else
               shutil.which('chromium') or shutil.which('chromium-browser'))
    if browser is None:
        pytest.skip('An installed Chromium is needed for native navigation regressions')

    setup = r'''
window.unsafeExecuted = 0;
const rejected = REJECTED;
for (const href of rejected) {
  const anchor = document.createElement('a');
  anchor.className = 'unsafe';
  anchor.setAttribute('href', href);
  document.body.append(anchor);
}
'''.replace('REJECTED', json.dumps(REJECTED).replace('<', '\\u003c'))
    checks = r'''
document.addEventListener('DOMContentLoaded', async () => {
  const check = (ok, message) => { if (!ok) throw Error(message); };
  const nextHash = () => new Promise(resolve => addEventListener('hashchange', resolve, {once: true}));
  try {
    check(document.body.classList.contains('ha-embed'), 'embed class');
    const start = location.href;
    for (const anchor of document.querySelectorAll('.unsafe')) {
      check(!anchor.hasAttribute('href'), 'unsafe href retained: ' + anchor.getAttribute('href'));
      anchor.click(); // No preventDefault: exercise native browser behaviour.
    }
    await new Promise(resolve => setTimeout(resolve, 50));
    check(unsafeExecuted === 0 && location.href === start, 'unsafe native navigation');
    check(document.querySelector('#internal').getAttribute('href') ===
      '/recent?date=today&embed=ha#bird', 'internal destination');
    check(document.querySelector('#external').getAttribute('href') ===
      'https://external.example/bird', 'external destination');
    const fragment = document.querySelector('#fragment');
    check(fragment.getAttribute('href') === '#bird', 'fragment destination');
    let changed = nextHash();
    fragment.click();
    await changed;
    check(location.hash === '#bird', 'native fragment click');
    changed = nextHash();
    history.back();
    await changed;
    check(location.href === start, 'browser back');
    changed = nextHash();
    history.forward();
    await changed;
    check(location.hash === '#bird', 'browser forward');
    document.body.dataset.result = 'passed';
  } catch (error) {
    document.body.dataset.result = error.message;
  }
});
'''
    page = ('<!doctype html><body><a id="internal" href="/recent?date=today#bird">Recent</a>'
            '<a id="external" href="https://external.example/bird">External</a>'
            '<a id="fragment" href="#bird">Bird</a><div id="bird">Bird</div>'
            f'<script>{setup}</script><script>{embed_script}</script>'
            f'<script>{checks}</script></body>').encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(page)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = subprocess.run([
            browser, '--headless', '--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage',
            '--disable-background-networking', '--disable-component-update', '--disable-sync',
            '--no-first-run', '--no-proxy-server',
            '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1',
            f'--user-data-dir={tmp_path / "profile"}', '--dump-dom', '--virtual-time-budget=3000',
            f'http://127.0.0.1:{server.server_port}/nested/page?embed=ha',
        ], capture_output=True, text=True, timeout=20, check=False)
        assert result.returncode == 0, result.stderr
        outcome = NavigationHTML(result.stdout).body_attributes.get('data-result')
        assert outcome is not None, result.stderr
        assert outcome == 'passed', outcome
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
