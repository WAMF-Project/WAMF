"""Pinned CDN inventory, byte verification and real-browser SRI enforcement."""

import base64
import hashlib
import json
import os
import shutil
import subprocess
import threading
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RESOURCES = json.loads((ROOT / 'tests/fixtures/cdn-integrity.json').read_text())


class _ExternalResources(HTMLParser):
    def __init__(self):
        super().__init__()
        self.resources = []

    def handle_starttag(self, tag, attributes):
        attributes = dict(attributes)
        url = attributes.get('src') if tag == 'script' else attributes.get('href')
        applicable = tag == 'script' or tag == 'link' and attributes.get('rel') == 'stylesheet'
        if applicable and url and url.startswith(('https://', 'http://', '//')):
            self.resources.append((tag, url, attributes))


def _template_resources():
    parser = _ExternalResources()
    for template in (ROOT / 'templates').glob('*.html'):
        parser.feed(template.read_text())
    return parser.resources


def test_external_inventory_remains_exactly_the_three_pinned_resources():
    resources = _template_resources()
    assert len(resources) == 3
    assert {url for _, url, _ in resources} == {resource['url'] for resource in RESOURCES.values()}


@pytest.mark.parametrize('filename', RESOURCES)
def test_each_pinned_resource_has_verified_sha384_and_anonymous_cors(filename):
    resource = RESOURCES[filename]
    attributes = next(attrs for _, url, attrs in _template_resources() if url == resource['url'])
    assert attributes['integrity'] == resource['sha384']
    assert attributes['crossorigin'] == 'anonymous'
    algorithm, digest = attributes['integrity'].split('-', 1)
    assert algorithm == 'sha384' and len(base64.b64decode(digest, validate=True)) == 48


@pytest.fixture
def downloaded_assets():
    directory = os.environ.get('WAMF_CDN_ASSET_DIR')
    if not directory:
        pytest.skip('Set WAMF_CDN_ASSET_DIR to verified public downloads for offline byte/browser checks')
    directory = Path(directory)
    for filename in RESOURCES:
        assert (directory / filename).is_file(), f'Missing public CDN fixture: {filename}'
    return directory


@pytest.mark.parametrize('filename', RESOURCES)
def test_integrity_matches_actual_downloaded_resource_bytes(downloaded_assets, filename):
    resource = RESOURCES[filename]
    body = (downloaded_assets / filename).read_bytes()
    assert len(body) == resource['bytes']
    assert 'sha384-' + base64.b64encode(hashlib.sha384(body).digest()).decode() == resource['sha384']


@pytest.mark.parametrize('route,expected_count', [('/', 3), ('/admin/config', 0)])
def test_rendered_pages_preserve_integrity_attributes(flask_client, monkeypatch, route, expected_count):
    import webui
    monkeypatch.setattr(webui, 'config', {**webui.config, 'admin': {'auth_enabled': False}})
    response = flask_client.get(route)
    assert response.status_code == 200
    parser = _ExternalResources()
    parser.feed(response.get_data(as_text=True))
    assert len(parser.resources) == expected_count
    for _, url, attrs in parser.resources:
        expected = next(resource for resource in RESOURCES.values() if resource['url'] == url)
        assert attrs['integrity'] == expected['sha384'] and attrs['crossorigin'] == 'anonymous'


@pytest.mark.parametrize('tamper', [None, 'jquery-3.6.0.min.js', 'bootstrap.min.css'])
def test_chromium_checks_cross_origin_sri_and_preserves_controls(downloaded_assets, tmp_path, tamper):
    installed = list((Path.home() / '.cache/ms-playwright').glob(
        'chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell'
    ))
    browser = shutil.which('chromium') or shutil.which('chromium-browser')
    if browser is None and installed:
        browser = str(max(installed))
    if browser is None:
        pytest.skip('An installed Chromium is needed for the SRI browser regression')

    requests_seen = []
    document_requests = []
    document = b''

    class AssetHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            filename = self.path.lstrip('/')
            if filename not in RESOURCES:
                self.send_error(404)
                return
            requests_seen.append((filename, self.headers.get('Origin'), self.headers.get('Cookie')))
            body = (downloaded_assets / filename).read_bytes()
            if filename == tamper:
                body += b'\n/* Synthetic SRI mismatch */'
            self.send_response(200)
            self.send_header('Content-Type', 'text/css' if filename.endswith('.css') else 'application/javascript')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    class DocumentHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            document_requests.append(self.path)
            if self.path == '/':
                body, content_type = document, 'text/html'
            elif self.path == '/static/js/detection-cards.js':
                body = (ROOT / 'static/js/detection-cards.js').read_bytes()
                content_type = 'application/javascript'
            elif self.path.startswith('/frigate/') or self.path == '/pixel.jpg':
                body = (ROOT / 'static/images/default-bird.jpg').read_bytes()
                content_type = 'image/jpeg'
            else:
                body, content_type = b'', 'video/mp4'
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            if self.path == '/':
                self.send_header('Set-Cookie', 'sri_fixture=synthetic; Path=/; SameSite=Lax')
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    assets = ThreadingHTTPServer(('127.0.0.1', 0), AssetHandler)
    server = ThreadingHTTPServer(('127.0.0.1', 0), DocumentHandler)
    asset_origin = f'http://127.0.0.1:{assets.server_port}'
    origin = f'http://127.0.0.1:{server.server_port}'
    asset_tags = []
    for tag, url, attrs in _template_resources():
        filename = next(name for name, resource in RESOURCES.items() if resource['url'] == url)
        attributes = f'integrity="{attrs["integrity"]}" crossorigin="{attrs["crossorigin"]}"'
        if tag == 'link':
            asset_tags.append(f'<link rel="stylesheet" href="{asset_origin}/{filename}" {attributes}>')
        else:
            asset_tags.append(f'<script src="{asset_origin}/{filename}" {attributes}></script>')
    modals = (ROOT / 'templates/modals_and_scripts.html').read_text()
    modals = modals.split('<script src=', 1)[0].replace('{{ csrf_token|tojson }}', '"synthetic-csrf"')
    assert '{{' not in modals
    checks = r'''
<script>
document.querySelectorAll('.modal').forEach(node => node.classList.remove('fade'));
let confirmed = 0;
window.confirm = () => { confirmed++; return false; };
try {
  const card = window.wamfDetections.buildRow({frigate_event: '1700000000.123456-abcdef', common_name: 'Robin', score: 0.91, detection_time: '2024-06-01 08:30:00'});
  document.body.append(card);
  card.querySelector('img').click();
  const snapshotShown = document.getElementById('snapshotModal').classList.contains('show');
  document.getElementById('viewVideoButton').click();
  const videoShown = document.getElementById('videoModal').classList.contains('show');
  document.getElementById('deleteDetectionButton').click();
  const collapse = new bootstrap.Collapse(document.getElementById('collapse-probe'), {toggle: false});
  collapse.show();
  setTimeout(() => {
    const jqueryOK = EXPECT_JQUERY ? window.jQuery?.fn.jquery === '3.6.0' : typeof window.jQuery === 'undefined';
    const cssOK = (getComputedStyle(document.getElementById('css-probe')).display === 'none') === EXPECT_CSS;
    const passed = jqueryOK && cssOK && bootstrap.Modal.VERSION === '5.1.3'
      && snapshotShown && videoShown && confirmed === 1
      && document.getElementById('collapse-probe').classList.contains('show')
      && card.querySelector('h3').textContent === 'Robin'
      && card.querySelector('.wamf-confidence').textContent === '91%';
    document.body.dataset.result = passed ? 'passed' : 'failed';
  }, 800);
} catch {
  document.body.dataset.result = 'failed';
}
</script>'''.replace('EXPECT_JQUERY', json.dumps(tamper != 'jquery-3.6.0.min.js')).replace(
        'EXPECT_CSS', json.dumps(tamper != 'bootstrap.min.css'))
    document = ('<!doctype html><html><head>' + ''.join(asset_tags)
                + '</head><body><div id="css-probe" class="d-none"></div>'
                + '<div id="collapse-probe" class="collapse">Navigation</div>' + modals
                + '<script src="/static/js/detection-cards.js"></script>' + checks + '</body></html>').encode()
    threads = [threading.Thread(target=item.serve_forever, daemon=True) for item in (assets, server)]
    for thread in threads:
        thread.start()
    try:
        result = subprocess.run([
            browser, '--headless', '--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage',
            '--disable-background-networking', '--disable-component-update', '--disable-sync',
            '--no-first-run', '--no-proxy-server',
            '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1, EXCLUDE localhost',
            f'--user-data-dir={tmp_path / "profile"}', '--dump-dom', '--virtual-time-budget=2000', origin,
        ], capture_output=True, text=True, timeout=20, check=False)
        assert result.returncode == 0, result.stderr
        assert '/' in document_requests, 'Chromium did not reach the loopback document fixture'
        assert 'data-result="passed"' in result.stdout, result.stderr
        assert {item[0] for item in requests_seen} == set(RESOURCES)
        assert all(request_origin == origin and cookie is None for _, request_origin, cookie in requests_seen)
    finally:
        for item in (assets, server):
            item.shutdown()
            item.server_close()
        for thread in threads:
            thread.join(timeout=5)
