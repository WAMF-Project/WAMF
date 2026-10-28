"""The daily-summary redirect permits query data, never a supplied destination."""

from datetime import datetime
import json
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

DATE_PATH = '/daily_summary/2026-10-28'
QUERIES = [
    '', 'embed=ha', 'embed=ha&live=true&tag=one&tag=two&empty=&flag',
    'q=blue+tit&name=blue%20tit&encoded=%26%3D%3F%23&literal=%252F',
    'https://evil.example/path', 'next=https://evil.example/path',
    'https://wamf.example@evil.example/path',
    '//evil.example/path', 'next=//evil.example/path',
    '%2F%2Fevil.example', '%252F%252Fevil.example',
    'next=%2f%5cevil.example', 'next=%252f%255cevil.example',
    '\\evil.example', '/\\evil.example',
    'javascript:alert(1)', 'next=JaVaScRiPt:alert(1)',
    'data:text/html,unsafe', 'http://[', 'https://example.com:invalid/',
    ' https://evil.example', '\thttps://evil.example',
    '\x00//evil.example', '%09%0d%0aLocation:%20https://evil.example',
    '%250d%250aLocation:%2520https://evil.example',
    '?next=//evil.example', 'value=%3Fnext%3D%2F%2Fevil.example',
    'date=//evil.example&_external=true&_scheme=javascript&endpoint=evil',
    'value=%23//evil.example', 'q=%zz&literal=%',
]


@pytest.fixture
def summary_client(flask_client, monkeypatch):
    import routes.public as public

    class FixedDatetime(datetime):
        @classmethod
        def now(cls):
            return cls(2026, 10, 28)

    monkeypatch.setattr(public, 'datetime', FixedDatetime)
    return flask_client


def assert_internal_summary(location, prefix=''):
    parsed = urlsplit(location)
    assert parsed.scheme == parsed.netloc == ''
    assert parsed.path == prefix + DATE_PATH
    assert parsed.fragment == ''


@pytest.mark.parametrize('path', ['/daily_summary', '/daily_summary/'])
@pytest.mark.parametrize('query', QUERIES)
def test_query_data_cannot_change_summary_destination(summary_client, path, query):
    response = summary_client.get(path, query_string=query)
    assert response.status_code == 302
    assert len(response.headers.getlist('Location')) == 1
    assert_internal_summary(response.headers['Location'])
    assert not any(ord(char) < 32 for char in response.headers['Location'])


@pytest.mark.parametrize('path', ['/daily_summary', '/daily_summary/'])
@pytest.mark.parametrize('headers', [
    {'Host': 'evil.example'},
    {'Host': 'evil.example:8443'},
    {'Host': '[::1]:8443'},
    {'X-Forwarded-Host': 'evil.example', 'X-Forwarded-Proto': 'https'},
    {'X-Forwarded-Prefix': '//evil.example'},
    {'Forwarded': 'host=evil.example;proto=https'},
    {'X-Original-URL': '//evil.example', 'X-Rewrite-URL': 'https://evil.example'},
])
def test_request_headers_do_not_supply_redirect_origin(summary_client, path, headers):
    response = summary_client.get(path, query_string='embed=ha&next=//evil.example', headers=headers)
    assert response.status_code == 302
    assert response.headers['Location'] == DATE_PATH + '?embed=ha&next=//evil.example'


def test_legitimate_query_and_embed_survive_redirect_and_render(summary_client):
    query = 'embed=ha&tag=one&tag=two&empty=&flag&name=blue%20tit'
    response = summary_client.get('/daily_summary', query_string=query, follow_redirects=True)
    assert len(response.history) == 1
    assert response.history[0].headers['Location'] == DATE_PATH + '?' + query
    assert response.status_code == 200
    assert response.request.path == DATE_PATH
    assert response.request.query_string.decode() == query
    assert b'Daily Summary for 2026-10-28' in response.data


def test_trusted_wsgi_mount_prefix_remains_internal(summary_client):
    response = summary_client.get('/daily_summary', query_string='embed=ha',
                                  environ_overrides={'SCRIPT_NAME': '/wamf'})
    assert response.status_code == 302
    assert_internal_summary(response.headers['Location'], '/wamf')
    assert response.headers['Location'].endswith('?embed=ha')


@pytest.mark.parametrize('query', ['\r\nLocation: https://evil.example', '\xff'])
def test_invalid_raw_wsgi_queries_cannot_emit_an_unsafe_redirect(summary_client, monkeypatch, query):
    # Raw newline and invalid UTF-8 requests bypass normal browser URL encoding.
    monkeypatch.setitem(summary_client.application.config, 'PROPAGATE_EXCEPTIONS', False)
    response = summary_client.get('/daily_summary', environ_overrides={'QUERY_STRING': query})
    if response.status_code == 302:
        assert_internal_summary(response.headers['Location'])
    else:
        assert response.status_code in (400, 500)
        assert 'Location' not in response.headers


def test_chromium_follows_actual_redirect_headers_only_to_summary(summary_client):
    """Requires Node Playwright; NODE_PATH may point to an external installation."""
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node is needed for the browser redirect regression')
    probe = subprocess.run([node, '-e', "require.resolve('playwright')"],
                           capture_output=True, text=True, timeout=10, check=False)
    if probe.returncode:
        pytest.skip('Node Playwright is needed for browser redirect regressions')
    locations = []
    for query in QUERIES:
        response = summary_client.get('/daily_summary', query_string=query)
        assert response.status_code == 302
        locations.append(response.headers['Location'])

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            url = urlsplit(self.path)
            if url.path == '/daily_summary':
                index = int(parse_qs(url.query)['case'][0])
                self.send_response(302)
                self.send_header('Location', locations[index])
                self.end_headers()
            elif url.path == DATE_PATH:
                self.send_response(200)
                self.send_header('Content-Type', 'text/html')
                self.end_headers()
                self.wfile.write(b'<p>Summary</p>')
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *args):
            pass

    runner = r'''
const assert = require('node:assert/strict');
const {chromium} = require('playwright');
const [base, count, datePath] = JSON.parse(process.argv[1]);
(async () => {
  const browser = await chromium.launch({headless: true, args: ['--no-sandbox',
    '--no-proxy-server', '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1']});
  try {
    const page = await browser.newPage();
    page.setDefaultTimeout(5000);
    for (let index = 0; index < count; index++) {
      await page.goto(base + '/daily_summary?case=' + index);
      const final = new URL(page.url());
      assert.equal(final.origin, base);
      assert.equal(final.pathname, datePath);
      assert.equal(final.hash, '');
    }
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
'''
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f'http://127.0.0.1:{server.server_port}'
        result = subprocess.run([node, '-e', runner, json.dumps([base, len(locations), DATE_PATH])],
                                capture_output=True, text=True, timeout=30, check=False)
        assert result.returncode == 0, result.stdout + result.stderr
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
