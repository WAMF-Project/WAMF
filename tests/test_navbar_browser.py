"""Exercise the rendered navbar with the pinned Bootstrap assets in Chromium.

Set WAMF_CDN_ASSET_DIR to the public downloads used by test_cdn_integrity.py.
"""

import base64
import hashlib
import json
import mimetypes
import os
import re
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def navbar_page(flask_client):
    response = flask_client.get('/')
    assert response.status_code == 200
    return response.get_data(as_text=True)


@pytest.mark.parametrize('width', [320, 375, 425, 768, 1280])
def test_chromium_navbar_visibility_and_collapse(navbar_page, tmp_path, width):
    installed = sorted((Path.home() / '.cache/ms-playwright').glob(
        'chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell'
    ))
    browser = shutil.which('chromium') or shutil.which('chromium-browser')
    if browser is None and installed:
        browser = str(installed[-1])
    if browser is None:
        pytest.skip('An installed Chromium is needed for the navbar regression')
    asset_dir = os.environ.get('WAMF_CDN_ASSET_DIR')
    if not asset_dir:
        pytest.skip('Set WAMF_CDN_ASSET_DIR to the pinned public CDN downloads')
    inventory = json.loads((ROOT / 'tests/fixtures/cdn-integrity.json').read_text())
    assets = {}
    for name, resource in inventory.items():
        data = (Path(asset_dir) / name).read_bytes()
        digest = base64.b64encode(hashlib.sha384(data).digest()).decode()
        assert 'sha384-' + digest == resource['sha384']
        assets['/assets/' + name] = data
        navbar_page = navbar_page.replace(resource['url'], '/assets/' + name)

    checks = r'''
<script>
(async () => {
  const check = (condition, message) => { if (!condition) throw Error(message); };
  const visible = node => !!(node.offsetWidth && node.offsetHeight);
  const button = document.querySelector('.navbar-toggler');
  const menu = document.getElementById('navbarSupportedContent');
  const links = [...menu.querySelectorAll('a')];
  const accessibleLinks = () => {
    check(links.map(a => a.textContent.trim()).join('|') ===
      'Overview|Recent Feed|Activity|Live View|Admin', 'navigation links');
    for (const link of links) {
      check(visible(link) && link.getAttribute('href'), 'link visible with destination');
      link.focus();
      check(document.activeElement === link, 'link keyboard accessible');
    }
  };
  const toggle = event => new Promise(resolve => {
    menu.addEventListener(event, resolve, {once: true});
    button.click();
  });
  try {
    check(innerWidth === WIDTH, 'viewport width');
    check(bootstrap.Collapse.VERSION === '5.1.3', 'pinned Bootstrap loaded');
    if (WIDTH < 992) {
      check(visible(button) && !visible(menu), 'mobile collapsed state');
      const icon = button.querySelector('.navbar-toggler-icon');
      check(visible(icon) && getComputedStyle(icon).backgroundImage !== 'none', 'hamburger painted');
      const rect = button.getBoundingClientRect();
      check(rect.left >= 0 && rect.right <= innerWidth, 'button within viewport');
      check(button.contains(document.elementFromPoint(rect.x + rect.width / 2,
        rect.y + rect.height / 2)), 'button unobscured');
      await toggle('shown.bs.collapse');
      check(button.getAttribute('aria-expanded') === 'true' && visible(menu), 'menu opened');
      accessibleLinks();
      await toggle('hidden.bs.collapse');
      check(button.getAttribute('aria-expanded') === 'false' && !visible(menu), 'menu closed');
    } else {
      check(!visible(button) && visible(menu), 'desktop expanded state');
      accessibleLinks();
      const tops = links.map(a => a.getBoundingClientRect().top);
      check(Math.max(...tops) - Math.min(...tops) < 1, 'desktop links in one row');
    }
    document.body.dataset.navbarResult = 'passed';
  } catch (error) {
    document.body.dataset.navbarResult = error.message;
  }
})();
</script>'''.replace('WIDTH', str(width))
    document = navbar_page.replace('</body>', checks + '</body>').encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = urlsplit(self.path).path
            if path == '/':
                body = document
            elif path in assets:
                body = assets[path]
            elif path.startswith('/static/') and '..' not in path.split('/'):
                file = ROOT / path.lstrip('/')
                body = file.read_bytes() if file.is_file() else b''
            else:
                body = b''
            self.send_response(200)
            self.send_header('Content-Type', mimetypes.guess_type(path)[0] or 'text/html')
            self.end_headers()
            self.wfile.write(body)

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
            f'--user-data-dir={tmp_path / "profile"}', f'--window-size={width},1000',
            '--dump-dom', '--virtual-time-budget=3000', f'http://127.0.0.1:{server.server_port}/',
        ], capture_output=True, text=True, timeout=20, check=False)
        assert result.returncode == 0, result.stderr
        outcome = re.search(r'data-navbar-result="([^"]*)"', result.stdout)
        assert outcome is not None, result.stderr
        assert outcome.group(1) == 'passed', outcome.group(1)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
