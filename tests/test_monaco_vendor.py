"""Complete official Monaco assets and an offline real-browser editor workflow."""

import base64
import hashlib
import json
import shutil
import subprocess
import threading
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml
from werkzeug.security import generate_password_hash
from werkzeug.serving import WSGIRequestHandler, make_server

ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / 'static/vendor/monaco-editor/0.52.2'
PREFIX = '/static/vendor/monaco-editor/0.52.2/'
PROVENANCE = json.loads((VENDOR / 'PROVENANCE.json').read_text())
PUBLISHED_INTEGRITY = 'sha512-GEQWEZmfkOGLdd3XK8ryrfWz3AIP8YymVXiPHEdewrUq7mh0qrKrfHLNCXcbB6sTnMLnOZ3ztSiKcciFUkIJwQ=='
HEALTH = {'setup_required': False, 'frigate_online': True, 'mqtt_online': True,
          'database_healthy': True, 'archive_writable': True, 'disk_used_percent': 0}


def test_complete_vendored_distribution_matches_verified_package():
    assert PROVENANCE['version'] == '0.52.2'
    assert PROVENANCE['source'] == 'https://registry.npmjs.org/monaco-editor/-/monaco-editor-0.52.2.tgz'
    assert PROVENANCE['integrity'] == PUBLISHED_INTEGRITY
    expected = PROVENANCE['files']
    assert len(expected) == 105
    assert len([name for name in expected if name.startswith('min/vs/')]) == 103
    assert {'LICENSE', 'ThirdPartyNotices.txt'} <= set(expected)
    actual = {str(path.relative_to(VENDOR)) for path in VENDOR.rglob('*') if path.is_file()}
    assert actual == set(expected) | {'PROVENANCE.json'}
    for name, record in expected.items():
        body = (VENDOR / name).read_bytes()
        assert len(body) == record['bytes'], name
        assert hashlib.sha256(body).hexdigest() == record['sha256'], name
    assert b'The MIT License' in (VENDOR / 'LICENSE').read_bytes()
    assert b'THIRD-PARTY SOFTWARE NOTICES' in (VENDOR / 'ThirdPartyNotices.txt').read_bytes()


def test_every_vendored_asset_is_served_locally(flask_client):
    for name, record in PROVENANCE['files'].items():
        response = flask_client.get(PREFIX + name)
        assert response.status_code == 200, name
        assert len(response.data) == record['bytes'], name
        assert hashlib.sha256(response.data).hexdigest() == record['sha256'], name
        if name.endswith('.css'):
            assert response.mimetype == 'text/css'
        if name.endswith('.js'):
            assert response.mimetype in {'text/javascript', 'application/javascript'}
        response.close()


class _Loader(HTMLParser):
    def __init__(self):
        super().__init__()
        self.scripts = []

    def handle_starttag(self, tag, attrs):
        if tag == 'script':
            self.scripts.append(dict(attrs))


def test_editor_template_uses_local_loader_base_and_matching_sri(flask_client, monkeypatch):
    import webui
    monkeypatch.setattr(webui, 'config', {**webui.config, 'admin': {'auth_enabled': False}})
    monkeypatch.setattr(webui, 'get_system_health', Mock(return_value=HEALTH))
    response = flask_client.get('/admin/config')
    assert response.status_code == 200
    parser = _Loader()
    text = response.get_data(as_text=True)
    parser.feed(text)
    loader = next(item for item in parser.scripts if item.get('src') == PREFIX + 'min/vs/loader.js')
    digest = base64.b64encode(hashlib.sha384((VENDOR / 'min/vs/loader.js').read_bytes()).digest()).decode()
    assert loader['integrity'] == 'sha384-' + digest
    assert PREFIX + 'min/vs' in text
    assert 'cdnjs.cloudflare.com' not in text
    assert not any(item.get('src', '').startswith(('http:', 'https:', '//')) for item in parser.scripts)


@pytest.mark.parametrize('action', ['restart', 'save-and-restart'])
def test_real_yaml_editor_workers_and_controls_are_local(flask_client, monkeypatch, tmp_path, action):
    import webui
    from routes import admin

    installed = list((Path.home() / '.cache/ms-playwright').glob(
        'chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell'
    ))
    browser = shutil.which('chromium') or shutil.which('chromium-browser')
    if browser is None and installed:
        browser = str(max(installed))
    if browser is None:
        pytest.skip('An installed Chromium is needed for the offline Monaco regression')

    config_path = tmp_path / 'config.yml'
    config = {**webui.config, 'admin': {
        'auth_enabled': True, 'password_hash': generate_password_hash('synthetic-monaco-password'),
        'session_secret': 'synthetic-monaco-session',
    }, 'api': {'token_auth_enabled': True, 'token_hash': ''}}
    config_path.write_text(yaml.safe_dump(config))
    monkeypatch.setenv('WHOSATMYFEEDER_CONFIG', str(config_path))
    monkeypatch.delenv('WAMF_SECRET_KEY', raising=False)
    monkeypatch.setattr(webui, 'config', config)
    monkeypatch.setitem(webui.app.config, 'SECRET_KEY', 'synthetic-monaco-session')
    monkeypatch.setattr(webui, 'get_system_health', Mock(return_value=HEALTH))
    monkeypatch.setattr(webui, 'get_retention_status', lambda: None)
    restart = Mock()
    monkeypatch.setattr(admin, 'schedule_restart', restart)
    requests_seen = []
    completed = threading.Event()
    session_cookie = webui.app.session_interface.get_signing_serializer(webui.app).dumps({
        'admin_authenticated': True, 'csrf_token': 'synthetic-monaco-csrf',
    })

    instrumentation = r'''<script>
window.monacoAudit = {violations: [], workers: [], posts: [], polls: 0, copied: ''};
window.addEventListener('securitypolicyviolation', event => monacoAudit.violations.push(event.blockedURI));
const NativeWorker = window.Worker;
window.Worker = class extends NativeWorker {
  constructor(url, options) {
    if (new URL(String(url), location.href).origin !== location.origin) throw Error('Off-origin worker');
    super(url, options);
    const record = {messages: 0, errors: 0};
    monacoAudit.workers.push(record);
    this.addEventListener('message', () => record.messages++);
    this.addEventListener('error', () => record.errors++);
  }
};
const nativeFetch = window.fetch.bind(window);
window.fetch = async (input, options) => {
  const url = new URL(typeof input === 'string' ? input : input.url, location.href);
  if (url.origin !== location.origin) throw Error('Off-origin fetch');
  const response = await nativeFetch(input, options);
  if (options?.method === 'POST') monacoAudit.posts.push({path: url.pathname, status: response.status});
  if (url.pathname === '/admin/config/restart-status') monacoAudit.polls++;
  return response;
};
const nativeCopy = document.execCommand.bind(document);
document.execCommand = (command, ...args) => {
  if (command === 'copy') { monacoAudit.copied = document.activeElement.value; return true; }
  return nativeCopy(command, ...args);
};
</script>'''

    driver = r'''<script>
(async () => {
  const check = (condition, label) => { if (!condition) throw Error(label); };
  const wait = async (condition, label) => {
    for (let i = 0; i < 1000; i++) {
      if (condition()) return;
      await new Promise(resolve => setTimeout(resolve, 20));
    }
    throw Error(label);
  };
  try {
    document.body.dataset.monacoStage = 'initialization';
    await wait(() => typeof editor !== 'undefined' && editor, 'Editor initialization');
    check(editor.getModel().getLanguageId() === 'yaml', 'YAML language');
    check(editor.getRawOptions().automaticLayout === true, 'Automatic layout');
    check(editor.getOption(monaco.editor.EditorOption.minimap).enabled === false, 'Minimap');
    check(editor.getDomNode().classList.contains('vs-dark'), 'Dark theme');
    await wait(() => monaco.editor.tokenize('frigate:\n  camera: "birdcam"\n', 'yaml').flat().some(token => token.type.includes('yaml')), 'YAML tokenizer');
    editor.setPosition(editor.getModel().getFullModelRange().getEndPosition());
    editor.focus();
    editor.trigger('keyboard', 'type', {text: '\n# monaco-local-synthetic\n'});
    check(editor.getValue().includes('# monaco-local-synthetic'), 'Editor input');
    document.getElementById('copy-config-btn').click();
    check(monacoAudit.copied === editor.getValue(), 'Copy selected editor text');

    document.body.dataset.monacoStage = 'worker';
    const jsonModel = monaco.editor.createModel('{"local":true}', 'json', monaco.Uri.parse('inmemory://local-test/model.json'));
    const getWorker = await monaco.languages.json.getWorker();
    const worker = await getWorker(jsonModel.uri);
    const diagnostics = await worker.doValidation(jsonModel.uri.toString());
    check(Array.isArray(diagnostics) && diagnostics.length === 0, 'Worker validation response');
    jsonModel.dispose();
    check(monacoAudit.workers.length > 0 && monacoAudit.workers.some(item => item.messages > 0), 'Worker messages');
    check(monacoAudit.workers.every(item => item.errors === 0), 'Worker errors');

    document.body.dataset.monacoStage = 'controls';
    document.getElementById('save-config-btn').click();
    await wait(() => monacoAudit.posts.some(item => item.path === '/admin/config/save'), 'Save request');
    await wait(() => document.getElementById('config-message').textContent.includes('Configuration saved'), 'Save confirmation');
    check(monacoAudit.posts[0].status === 200, 'Save status');
    if (ACTION === 'save-and-restart') {
      editor.setPosition(editor.getModel().getFullModelRange().getEndPosition());
      editor.trigger('keyboard', 'type', {text: '# saved-with-restart\n'});
    }
    document.getElementById(ACTION === 'restart' ? 'restart-btn' : 'save-restart-btn').click();
    await wait(() => monacoAudit.posts.some(item => item.path === '/admin/config/' + ACTION), 'Restart request');
    await wait(() => monacoAudit.polls > 0, 'Restart polling');
    check(document.getElementById('restart-btn').disabled, 'Duplicate restart protection');
    check(monacoAudit.posts.filter(item => item.path === '/admin/config/' + ACTION).length === 1, 'One restart POST');
    check(monacoAudit.posts.every(item => item.status === 200), 'Action statuses');
    check(monacoAudit.violations.length === 0, 'Off-origin/CSP request');
    check(performance.getEntriesByType('resource').every(item => {
      const url = new URL(item.name, location.href);
      return !['http:', 'https:'].includes(url.protocol) || url.origin === location.origin;
    }), 'All resources same origin');
    document.body.dataset.monacoResult = 'passed';
  } catch (error) {
    document.body.dataset.monacoResult = 'failed';
    document.body.dataset.monacoError = error.message;
  } finally {
    await nativeFetch('/__monaco_test_done', {method: 'POST'});
  }
})();
</script>'''.replace('ACTION', json.dumps(action))

    def fixture_app(environ, start_response):
        path = environ.get('PATH_INFO', '')
        if path == '/__monaco_test_hold':
            # Keep the page load open until async worker RPC/control checks
            # finish in real time; virtual time can outrun Web Worker messages.
            completed.wait(timeout=25)
            start_response('200 OK', [('Content-Type', 'image/png'), ('Content-Length', '0')])
            return [b'']
        if path == '/__monaco_test_done':
            completed.set()
            start_response('200 OK', [('Content-Length', '0')])
            return [b'']
        if path == '/__monaco_test_start':
            # Seed an ordinary signed session for synthetic test credentials;
            # an HTTP redirect keeps Chromium's page-load wait on the editor.
            start_response('302 Found', [('Location', '/admin/config'), ('Content-Length', '0'),
                ('Set-Cookie', f'session={session_cookie}; Path=/; HttpOnly; SameSite=Lax')])
            return [b'']
        captured = {}
        def capture(status, headers, exc_info=None):
            captured.update(status=status, headers=headers)
        response = webui.app(environ, capture)
        try:
            body = b''.join(response)
        finally:
            if hasattr(response, 'close'):
                response.close()
        requests_seen.append((path, int(captured['status'].split()[0])))
        headers = captured['headers']
        if path == '/admin/config' and captured['status'].startswith('200'):
            body = body.replace(b'<head>', b'<head>' + instrumentation.encode(), 1)
            body = body.replace(b'</body>', b'<img src="/__monaco_test_hold" hidden>' + driver.encode() + b'</body>', 1)
            headers = [(key, value) for key, value in headers if key.lower() not in {'content-length', 'content-security-policy'}]
            # Test-only policy blocks every off-origin resource, including CDNs.
            headers += [('Content-Length', str(len(body))), ('Content-Security-Policy',
                "default-src 'self'; script-src 'self' 'unsafe-inline' 'unsafe-eval'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; worker-src 'self' blob:; connect-src 'self'")]
        start_response(captured['status'], headers)
        return [body]

    class QuietHandler(WSGIRequestHandler):
        def log(self, *args, **kwargs):
            pass

    server = make_server('127.0.0.1', 0, fixture_app, threaded=True, request_handler=QuietHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = subprocess.run([
            browser, '--headless', '--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage',
            '--disable-background-networking', '--disable-component-update', '--disable-sync',
            '--no-first-run', '--no-proxy-server',
            '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1, EXCLUDE localhost',
            f'--user-data-dir={tmp_path / "profile"}', '--dump-dom',
            f'http://127.0.0.1:{server.server_port}/__monaco_test_start',
        ], capture_output=True, text=True, timeout=45, check=False)
        assert result.returncode == 0, result.stderr
        failure = result.stdout.split('data-monaco-error="')[-1].split('"')[0] if 'data-monaco-error=' in result.stdout else 'No completion marker: ' + result.stdout.split('data-monaco-stage="')[-1][:40]
        assert 'data-monaco-result="passed"' in result.stdout, failure + '\n' + result.stderr
        assets = [(path, status) for path, status in requests_seen if path.startswith(PREFIX)]
        assert assets and all(status in {200, 304} for _, status in assets), [(path, status) for path, status in assets if status not in {200, 304}]
        for suffix in ['loader.js', 'editor/editor.main.js', 'editor/editor.main.css',
                       'basic-languages/yaml/yaml.js', 'base/worker/workerMain.js', 'language/json/jsonWorker.js']:
            assert any(path.endswith(suffix) for path, _ in assets), suffix
        assert '# monaco-local-synthetic' in config_path.read_text()
        if action == 'save-and-restart':
            assert '# saved-with-restart' in config_path.read_text()
        restart.assert_called_once_with()
    finally:
        completed.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
