"""Date-picker flow regressions (#14); browser cases require Node Playwright.

Use NODE_PATH when Playwright is installed outside the default Node search path.
"""

import json
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlsplit

import pytest

from tests.html_helpers import NavigationHTML

ROOT = Path(__file__).resolve().parents[1]
VALID_DATES = ['2024-06-01', '2024-02-29', '2000-02-29']
INVALID_DATES = ['not-a-date', '2024-02-30', '2023-02-29', '2024-13-01', '0000-01-01']
PAYLOADS = [
    '<img src=x onerror=alert(1)>',
    '"><svg onload=alert(1)>',
    '</script><script>alert(1)</script>',
    'javascript:alert(1)', 'JaVaScRiPt:alert(1)', '\t\njavascript:alert(1)',
    'data:text/html,<script>alert(1)</script>',
    '%3Csvg%20onload%3Dalert(1)%3E', '%253Csvg%253E',
    '%6Aavascript%3Aalert(1)', '//evil.example',
]


@pytest.fixture(params=['index.html', 'daily_summary.html'])
def navigation_script(request):
    template = (ROOT / 'templates' / request.param).read_text()
    return next(script for script in NavigationHTML(template).scripts
                if 'function navigateToDailySummary' in script)


@pytest.mark.parametrize('value', VALID_DATES + INVALID_DATES + PAYLOADS + [''])
@pytest.mark.parametrize('search,suffix', [
    ('', ''), ('?embed=ha', '?embed=ha'),
    ('?embed=javascript%3Aalert(1)&next=https://evil.example', ''),
])
def test_navigation_keeps_even_arbitrary_text_in_a_local_url(navigation_script, value, search, suffix):
    """Assume arbitrary input text, even beyond what a native date input permits."""
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node is needed for the navigation URL regression')
    runner = r'''
const assert = require('node:assert/strict');
const vm = require('node:vm');
const [script, value, search, suffix] = JSON.parse(process.argv[1]);
const location = {search, href: null};
const context = {window: {location}, URLSearchParams};
vm.runInNewContext(script, context);
context.navigateToDailySummary({value});
assert.equal(location.href, `/daily_summary/${value}${suffix}`);
const parsed = new URL(location.href, 'https://wamf.example/');
assert.equal(parsed.origin, 'https://wamf.example');
assert.equal(parsed.protocol, 'https:');
'''
    result = subprocess.run([node, '-e', runner,
                             json.dumps([navigation_script, value, search, suffix])],
                            capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('date', VALID_DATES + ['%32%30%32%34-%30%36-%30%31'])
@pytest.mark.parametrize('query', ['', '?embed=ha'])
def test_daily_summary_accepts_valid_dates(flask_client, date, query):
    response = flask_client.get('/daily_summary/' + date + query)
    assert response.status_code == 200
    assert b'type="date"' in response.data
    assert b'onchange="navigateToDailySummary(this)"' in response.data


@pytest.mark.parametrize('value', INVALID_DATES + PAYLOADS)
def test_invalid_route_dates_never_reach_summary_queries_or_rendering(flask_client, monkeypatch, value):
    import routes.public as public
    import webui

    calls = []

    def unexpected(*args, **kwargs):
        calls.append((args, kwargs))
        pytest.fail('Invalid date reached summary query or template rendering')

    monkeypatch.setattr(webui, 'get_daily_summary', unexpected)
    monkeypatch.setattr(public, 'render_template', unexpected)
    monkeypatch.setitem(webui.app.config, 'PROPAGATE_EXCEPTIONS', False)
    response = flask_client.get('/daily_summary/' + quote(value, safe=''), follow_redirects=True)
    # Existing behaviour: strptime raises ValueError (500), or routing rejects a
    # decoded slash (404). Permit a future explicit 400 without weakening this boundary.
    assert response.status_code in (400, 404, 500)
    assert not calls
    assert value.encode() not in response.data


@pytest.mark.parametrize('value', PAYLOADS[:3])
def test_summary_template_autoescapes_date_even_if_parser_is_bypassed(flask_client, value):
    from flask import render_template

    with flask_client.application.test_request_context('/'):
        html = render_template('daily_summary.html', date=value, daily_summary={},
                               today='2024-06-01', earliest_date='2024-06-01')
    assert value not in html
    assert '&lt;' in html


@pytest.mark.parametrize('initial', ['/', '/daily_summary/2024-06-01'])
@pytest.mark.parametrize('query', ['', '?embed=ha'])
def test_chromium_date_input_navigation_and_history(flask_client, initial, query):
    installed = sorted((Path.home() / '.cache/ms-playwright').glob(
        'chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell'
    ))
    browser = shutil.which('chromium') or shutil.which('chromium-browser')
    if browser is None and installed:
        browser = str(installed[-1])
    if browser is None:
        pytest.skip('An installed Chromium is needed for date input and history regressions')

    node = shutil.which('node')
    if node is None:
        pytest.skip('Node is needed for Playwright navigation regressions')
    probe = subprocess.run([node, '-e', "require.resolve('playwright')"],
                           capture_output=True, text=True, timeout=10, check=False)
    if probe.returncode:
        pytest.skip('Node Playwright is needed; use NODE_PATH for an external installation')

    # Use the actual rendered input and handler, without unrelated polling/CDN assets.
    pages = {}
    for path in ['/', '/daily_summary/2024-06-01', '/daily_summary/2024-02-29']:
        response = flask_client.get(path + query)
        assert response.status_code == 200
        html = response.get_data(as_text=True)
        parsed = NavigationHTML(html)
        picker = parsed.date_picker
        assert picker is not None
        script = next(script for script in parsed.scripts
                      if 'function navigateToDailySummary' in script)
        pages[path] = f'<!doctype html><body>{picker}<script>{script}</script></body>'.encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(pages[urlsplit(self.path).path])

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        runner = r'''
const assert = require('node:assert/strict');
const {chromium} = require('playwright');
const [executablePath, base, initial, query, invalid, valid] = JSON.parse(process.argv[1]);
(async () => {
  const browser = await chromium.launch({executablePath, headless: true, args: ['--no-sandbox']});
  try {
    const page = await browser.newPage();
    page.setDefaultTimeout(5000);
    await page.goto(base + initial + query);
    const picker = page.locator('#date-picker');
    assert.equal(await picker.getAttribute('type'), 'date');
    for (const value of invalid) {
      assert.equal(await picker.evaluate((input, value) => {
        input.value = value;
        return input.value;
      }, value), '', 'invalid text survived native date input');
    }
    for (const value of valid) {
      assert.equal(await picker.evaluate((input, value) => {
        input.value = value;
        return input.value;
      }, value), value, 'valid date changed');
    }
    await picker.evaluate(input => { input.value = '2024-02-29'; });
    const target = base + '/daily_summary/2024-02-29' + query;
    await Promise.all([page.waitForURL(target), picker.dispatchEvent('change')]);
    assert.equal(page.url(), target);
    await page.goBack();
    assert.equal(page.url(), base + initial + query);
    await page.goForward();
    assert.equal(page.url(), target);
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
'''
        result = subprocess.run([
            node, '-e', runner, json.dumps([browser, f'http://127.0.0.1:{server.server_port}',
                                           initial, query, INVALID_DATES + PAYLOADS, VALID_DATES]),
        ], capture_output=True, text=True, timeout=25, check=False)
        assert result.returncode == 0, result.stdout + result.stderr
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
