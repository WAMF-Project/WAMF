"""Detection values must never become HTML or executable event handlers."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'static/js/detection-cards.js'
PAYLOAD = '\'"><img src=x onerror="window.F001=1"><script>window.F001=1</script>'


@pytest.mark.parametrize('field,value', [
    ('frigate_event', PAYLOAD), ('common_name', PAYLOAD),
    ('detection_time', PAYLOAD), ('score', PAYLOAD),
    ('frigate_event', 'evt\n'), ('frigate_event', 'evt\r\n'),
])
def test_detection_row_treats_each_api_field_as_data(field, value):
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node is needed for the DOM construction regression')
    detection = {'frigate_event': '1700000000.123456-abcdef', 'common_name': 'American Robin',
                 'detection_time': '2024-06-01 08:30:00', 'score': 0.91}
    detection[field] = value
    runner = r'''
const assert = require('assert');
const vm = require('vm');
const fs = require('fs');
const detection = JSON.parse(process.argv[1]);
const snapshots = [];
let checked = 0;
class Element {
  constructor(tag) {
    assert(['div', 'img', 'h3'].includes(tag));
    this.tag = tag; this.dataset = {}; this.style = {}; this.children = []; this.listeners = {};
    this.classList = {add: (...names) => { this.className = names.join(' '); }};
  }
  set innerHTML(value) { throw Error('Untrusted HTML construction'); }
  set onclick(value) { throw Error('Inline click handler'); }
  set onload(value) { throw Error('Inline load handler'); }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  append(...children) { this.children.push(...children); }
}
const legacy = new Element('img');
legacy.dataset = {snapshotUrl: '/wamf/snapshot/safe.jpg', clipUrl: '/wamf/clip/safe.mp4', frigateEvent: detection.frigate_event};
const context = {window: {F001: 0}, document: {
  createElement: tag => new Element(tag), querySelectorAll: () => [legacy],
}, checkTransparentImage: image => {
  checked++;
  if ((image.src || '').includes('1x1.png')) image.style.display = 'none';
}, showSnapshot: (...args) => snapshots.push(args), encodeURIComponent};
vm.runInNewContext(fs.readFileSync(process.argv[2], 'utf8'), context);
const card = context.window.wamfDetections.buildRow(detection);
const [image, content] = card.children;
assert.equal(card.className, 'wamf-detection-card new-detection');
assert.equal(card.dataset.frigateEvent, detection.frigate_event);
assert.equal(content.children[0].textContent, detection.common_name);
assert.equal(content.children[1].textContent, `${Math.round(detection.score * 100)}%`);
assert.equal(content.children[2].textContent, detection.detection_time);
const event = {preventDefault() { this.prevented = true; }, stopPropagation() { this.stopped = true; }};
legacy.listeners.click(event);
assert(event.prevented && event.stopped);
assert.deepEqual(snapshots[0], ['/wamf/snapshot/safe.jpg', '/wamf/clip/safe.mp4', detection.frigate_event]);
image.listeners.load();
image.listeners.click(event);
assert.equal(checked, 1);
if (detection.frigate_event.startsWith('1700')) {
  assert.equal(image.src, '/frigate/1700000000.123456-abcdef/thumbnail.jpg');
  assert.deepEqual(snapshots[1], ['/frigate/1700000000.123456-abcdef/snapshot.jpg', '/frigate/1700000000.123456-abcdef/clip.mp4', detection.frigate_event]);
} else {
  assert.equal(image.src, '/static/images/1x1.png');
  assert.equal(snapshots.length, 1); // Transparent placeholders remain inert.
}
assert.equal(context.window.F001, 0);
'''
    result = subprocess.run([node, '-e', runner, json.dumps(detection), str(SCRIPT)],
                            capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == 0, result.stderr


def test_chromium_renders_hostile_detection_values_without_execution(tmp_path):
    installed = list((Path.home() / '.cache/ms-playwright').glob(
        'chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell'
    ))
    browser = shutil.which('chromium') or shutil.which('chromium-browser')
    if browser is None and installed:
        browser = str(max(installed))
    if browser is None:
        pytest.skip('An installed Chromium is needed for the real browser regression')
    payload = json.dumps(PAYLOAD).replace('<', '\\u003c')
    html = '''<!doctype html><html><head>
<meta http-equiv="Content-Security-Policy" content="img-src 'none'; connect-src 'none'">
</head><body><a id="legacy-link" href="#unwanted-navigation"><img id="legacy"></a>
<script>
window.F001 = 0;
window.snapshots = [];
window.checkTransparentImage = () => {};
window.showSnapshot = (...args) => window.snapshots.push(args);
const hostile = PAYLOAD;
const legacy = document.getElementById('legacy');
legacy.dataset.snapshotUrl = '/wamf/snapshot/safe.jpg';
legacy.dataset.clipUrl = '/wamf/clip/safe.mp4';
legacy.dataset.frigateEvent = hostile;
</script>
<script src="SCRIPT"></script>
<script>
try {
  const bad = window.wamfDetections.buildRow({frigate_event: hostile, common_name: hostile, detection_time: hostile, score: 0.91});
  const good = window.wamfDetections.buildRow({frigate_event: '1700000000.123456-abcdef', common_name: 'Robin', detection_time: '2024-06-01 08:30:00', score: 0.91});
  document.body.append(bad, good);
  const click = new MouseEvent('click', {bubbles: true, cancelable: true});
  legacy.dispatchEvent(click);
  good.querySelector('img').click();
  setTimeout(() => {
    const safe = window.F001 === 0 && document.querySelectorAll('img').length === 3
      && bad.querySelector('h3').textContent === hostile
      && bad.querySelector('.wamf-detection-time').textContent === hostile
      && bad.querySelector('.wamf-confidence').textContent === '91%'
      && !document.querySelector('[onclick], [onerror], [onload]')
      && click.defaultPrevented && window.snapshots.length === 2
      && window.snapshots[0][2] === hostile
      && window.snapshots[1][0] === '/frigate/1700000000.123456-abcdef/snapshot.jpg'
      && window.location.hash === '';
    document.body.dataset.result = safe ? 'passed' : 'failed';
  }, 100);
} catch {
  document.body.dataset.result = 'failed';
}
</script></body></html>'''.replace('PAYLOAD', payload).replace('SCRIPT', SCRIPT.as_uri())
    page = tmp_path / 'detection-security.html'
    page.write_text(html)
    result = subprocess.run([
        browser, '--headless', '--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage',
        '--disable-background-networking', '--disable-component-update', '--disable-sync',
        '--no-first-run', '--host-resolver-rules=MAP * ~NOTFOUND',
        f'--user-data-dir={tmp_path / "profile"}', '--dump-dom', '--virtual-time-budget=1000', page.as_uri(),
    ], capture_output=True, text=True, timeout=20, check=False)
    assert result.returncode == 0, result.stderr
    assert 'data-result="passed"' in result.stdout
