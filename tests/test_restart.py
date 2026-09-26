"""Admin restart uses the native parent, not an external service manager."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from unittest.mock import MagicMock, patch

import pytest
import yaml

from app import process_control
from app.process_control import WorkerSupervisor


@pytest.mark.parametrize('ordinary_stop', [None, signal.SIGINT, signal.SIGTERM])
def test_restart_joins_all_workers_before_exec_and_stop_overrides_restart(ordinary_stop):
    events = []
    children = [MagicMock(pid=101), MagicMock(pid=102)]
    for i, child in enumerate(children):
        child.terminate.side_effect = lambda i=i: events.append(('terminate', i))
        child.join.side_effect = lambda i=i: events.append(('join', i))
    with patch('app.process_control.reexec_application', side_effect=lambda: events.append(('exec',))) as reexec:
        with WorkerSupervisor(MagicMock(side_effect=children)) as workers:
            workers.start(lambda: None)
            workers.start(lambda: None)
            workers._request_shutdown(signal.SIGUSR1, None)
            workers._request_shutdown(signal.SIGUSR1, None)  # Duplicate is coalesced.
            assert workers.start(lambda: None) is None
            if ordinary_stop:
                workers._request_shutdown(ordinary_stop, None)
                workers._request_shutdown(signal.SIGUSR1, None)
    expected = [('terminate', 0), ('terminate', 1), ('join', 0), ('join', 1)]
    if ordinary_stop is None:
        expected.append(('exec',))
    assert events == expected
    assert reexec.call_count == (ordinary_stop is None)


@pytest.mark.parametrize('signum', [signal.SIGINT, signal.SIGTERM])
def test_stop_can_cancel_restart_after_children_have_been_joined(signum):
    with pytest.raises(SystemExit), patch('app.process_control.reexec_application') as reexec:
        with WorkerSupervisor(MagicMock()) as workers:
            workers._request_shutdown(signal.SIGUSR1, None)
            reexec.side_effect = lambda: workers._request_shutdown(signum, None)


def test_reexec_preserves_python_flags_arguments_and_environment(monkeypatch):
    monkeypatch.setattr(sys, 'executable', '/opt/wamf/.venv/bin/python')
    monkeypatch.setattr(sys, 'orig_argv', ['python', '-u', '/opt/wamf/speciesid.py', 'original-argument'])
    monkeypatch.setenv('WHOSATMYFEEDER_CONFIG', '/opt/wamf/custom.yml')
    with patch('app.process_control.os.execv') as execv:
        process_control.reexec_application()
    execv.assert_called_once_with('/opt/wamf/.venv/bin/python', ['/opt/wamf/.venv/bin/python', '-u', '/opt/wamf/speciesid.py', 'original-argument'])
    assert os.environ['WHOSATMYFEEDER_CONFIG'] == '/opt/wamf/custom.yml'


def test_restart_schedules_only_one_distinct_parent_signal(monkeypatch):
    monkeypatch.setattr(process_control, '_supervisor_pid', os.getppid())
    monkeypatch.setattr(process_control, '_restart_scheduled', False)
    with patch('app.process_control.threading.Thread') as thread, patch('app.process_control.time.sleep'), patch('app.process_control.os.kill') as kill:
        process_control.schedule_restart()
        process_control.schedule_restart()
        thread.assert_called_once()
        thread.call_args.kwargs['target']()
        kill.assert_called_once_with(os.getppid(), signal.SIGUSR1)


def test_restart_never_signals_an_unrelated_launcher(monkeypatch):
    monkeypatch.setattr(process_control, '_supervisor_pid', None)
    with patch('app.process_control.os.kill') as kill, pytest.raises(RuntimeError, match='native WAMF parent'):
        process_control.schedule_restart()
    kill.assert_not_called()


def test_watchdog_does_not_respawn_during_restart(monkeypatch):
    import speciesid
    monkeypatch.setattr(speciesid, 'config', {
        'frigate': {'frigate_url': 'http://frigate', 'mqtt_server': 'mqtt', 'main_topic': 'frigate', 'camera': ['birdcam']},
        'classification': {'model': 'model.tflite', 'threshold': 0.7},
    })
    flask, mqtt = MagicMock(pid=101), MagicMock(pid=102)
    mqtt.is_alive.return_value = False
    def request_restart(timeout=None):
        if timeout is not None:
            signal.getsignal(signal.SIGUSR1)(signal.SIGUSR1, None)
    mqtt.join.side_effect = request_restart
    with patch('speciesid.load_config'), patch('speciesid.setupdb'), patch('speciesid.log_system_event'), patch('speciesid.multiprocessing.Process', side_effect=[flask, mqtt]) as factory, patch('app.process_control.reexec_application') as reexec:
        speciesid.main()
    assert factory.call_count == 2
    flask.terminate.assert_called_once()
    flask.join.assert_called_once()
    reexec.assert_called_once()


def test_save_only_reports_restart_required_without_scheduling(flask_client, monkeypatch, tmp_path):
    import webui
    monkeypatch.setitem(webui.app.config, 'SECRET_KEY', webui.app.secret_key)
    path = tmp_path / 'config.yml'
    path.write_text('admin: {auth_enabled: false}\nwebui: {port: 7767}\n')
    monkeypatch.setenv('WHOSATMYFEEDER_CONFIG', str(path))
    monkeypatch.setattr(webui, 'config', {'admin': {'auth_enabled': False}})
    with patch('routes.admin.schedule_restart') as restart:
        response = flask_client.post('/admin/config/save', json={'config_content': 'webui: {port: 7767}\n'})
    assert response.json['success']
    assert response.json['restart_required']
    assert 'Restart WAMF' in response.json['message']
    restart.assert_not_called()


def test_restart_status_has_instance_identifier_and_cannot_be_cached(flask_client, monkeypatch):
    import webui
    monkeypatch.setattr(webui, 'config', {'admin': {'auth_enabled': False}})
    response = flask_client.get('/admin/config/restart-status')
    assert response.json == {'instance_id': process_control.INSTANCE_ID}
    assert response.headers['Cache-Control'] == 'no-store'


LIFECYCLE_RUNNER = r'''
import json
import os
from pathlib import Path
import runpy
import sys
import time
from unittest.mock import MagicMock
import yaml

root = Path(os.environ['WAMF_TEST_REPO'])
sys.path.insert(0, str(root))
log_path = Path(os.environ['LIFECYCLE_LOG'])
def records():
    return [json.loads(line) for line in log_path.read_text().splitlines()] if log_path.exists() else []
def record(kind, **values):
    value = {'kind': kind, 'pid': os.getpid(), 'ppid': os.getppid(), 'generation': generation, **values}
    fd = os.open(log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    os.write(fd, (json.dumps(value) + '\n').encode())
    os.close(fd)
previous = records()
generation = sum(row['kind'] == 'parent' for row in previous)
assert generation <= 1, 'Restart loop'
# This assertion runs in the replacement parent, before it can spawn workers.
for row in previous:
    if row['kind'] == 'worker':
        assert not Path('/proc/' + str(row['pid'])).exists(), 'Restart left an old worker alive or unreaped'
record('parent', interpreter=sys.executable, argv=sys.orig_argv, config_path=os.environ['WHOSATMYFEEDER_CONFIG'], marker=os.environ['ENV_MARKER'])

for name in ('PIL', 'PIL.Image', 'PIL.ImageOps'):
    sys.modules[name] = MagicMock()
import paho.mqtt.client as mqtt
class Client:
    def __init__(self, *args, **kwargs): pass
    def user_data_set(self, userdata): self._userdata = userdata
    def connect(self, *args): pass
    def disconnect(self): pass
    def loop_forever(self):
        record('worker', role='mqtt')
        while True: time.sleep(1)
mqtt.Client = Client
from app import health
health.start_health_monitor = lambda: None
health.FrigateClient = lambda *args, **kwargs: MagicMock()
from flask import Flask

def serve(self, **kwargs):
    import webui
    from app.process_control import INSTANCE_ID
    record('worker', role='flask', instance_id=INSTANCE_ID)
    if generation == 0 and not os.environ.get('INITIAL_SETUP'):
        deadline = time.monotonic() + 10
        while not any(row.get('role') == 'mqtt' for row in records()):
            assert time.monotonic() < deadline
            time.sleep(0.02)
    with self.test_client() as client:
        response = client.get('/admin/config')
        assert response.status_code == 200
        if generation == 0:
            config_path = Path(os.environ['WHOSATMYFEEDER_CONFIG'])
            config = yaml.safe_load(config_path.read_text())
            config['frigate'].update(mqtt_server='mqtt', camera=['updated-camera'])
            content = yaml.safe_dump(config)
            if os.environ['ADMIN_ACTION'] == 'restart':
                # Save remains a save-only operation while the original worker runs.
                saved = client.post('/admin/config/save', json={'config_content': content})
                assert saved.json['restart_required']
                assert sum(row['kind'] == 'parent' for row in records()) == 1
                response = client.post('/admin/config/restart', json={})
            else:
                response = client.post('/admin/config/save-and-restart', json={'config_content': content})
            assert response.json['success'], response.json
            assert yaml.safe_load(config_path.read_text())['frigate']['camera'] == ['updated-camera']
            record('accepted')
        else:
            assert webui.config['frigate']['camera'] == ['updated-camera']
            assert not health.get_system_health()['setup_required']
            assert b'Setup / configuration required' not in response.data
            status = client.get('/admin/config/restart-status')
            assert status.json['instance_id'] == INSTANCE_ID
            record('ui-ready', instance_id=INSTANCE_ID)
    while True: time.sleep(1)
Flask.run = serve
runpy.run_path(str(root / 'speciesid.py'), run_name='__main__')
'''


@pytest.mark.parametrize('action,setup,absolute_invocation', [
    ('restart', False, False),
    ('save-and-restart', True, False),
    ('save-and-restart', False, True),
], ids=['foreground-restart', 'setup-save-and-restart', 'systemd-style-invocation'])
def test_real_admin_restart_reexecutes_same_parent_without_orphans(tmp_path, action, setup, absolute_invocation):
    import json
    root = Path(__file__).resolve().parent.parent
    config_path = tmp_path / 'config.yml'
    config_path.write_text(yaml.safe_dump({
        'admin': {'auth_enabled': False},
        'frigate': {'frigate_url': 'http://frigate', 'mqtt_server': 'your-mqtt-server' if setup else 'mqtt',
                    'main_topic': 'frigate', 'camera': ['original-camera']},
        'classification': {'model': 'model.tflite', 'threshold': 0.7},
        'storage': {'database_path': str(tmp_path / 'data/speciesid.db')},
        'media': {'snapshots_path': str(tmp_path / 'snapshots'), 'clips_path': str(tmp_path / 'clips')},
    }))
    runner = tmp_path / 'native_runner.py'
    runner.write_text(LIFECYCLE_RUNNER)
    log_path = tmp_path / 'lifecycle.jsonl'
    env = dict(os.environ, WAMF_TEST_REPO=str(root), WHOSATMYFEEDER_CONFIG=str(config_path),
               LIFECYCLE_LOG=str(log_path), ENV_MARKER='preserve-me', ADMIN_ACTION=action)
    env.pop('INITIAL_SETUP', None)
    if setup:
        env['INITIAL_SETUP'] = '1'
    invocation = [sys.executable, '-u', str(runner) if absolute_invocation else runner.name, 'original-argument']
    process = subprocess.Popen(invocation, cwd=tmp_path, env=env, start_new_session=True,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    def rows():
        return [json.loads(line) for line in log_path.read_text().splitlines()] if log_path.exists() else []
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            events = rows()
            if any(row['kind'] == 'ui-ready' for row in events) and any(row.get('role') == 'mqtt' and row['generation'] == 1 for row in events):
                break
            if process.poll() is not None:
                _, stderr = process.communicate()
                pytest.fail(f'Native parent exited during restart: {stderr}')
            time.sleep(0.02)
        else:
            pytest.fail('Admin UI did not return after restart')
        # Allow any accidentally carried-over delayed request to fire.
        time.sleep(1.2)
        events = rows()
        parents = [row for row in events if row['kind'] == 'parent']
        assert len(parents) == 2
        assert all(row['pid'] == process.pid for row in parents)
        assert parents[0]['interpreter'] == parents[1]['interpreter'] == sys.executable
        assert parents[0]['argv'] == parents[1]['argv'] == invocation
        assert all(row['marker'] == 'preserve-me' and row['config_path'] == str(config_path) for row in parents)
        assert process.poll() is None, 'A service manager would have seen its main process exit'
        worker_rows = [row for row in events if row['kind'] == 'worker']
        assert len(worker_rows) == (3 if setup else 4)
        assert all(row['ppid'] == process.pid for row in worker_rows)
        instances = [row['instance_id'] for row in worker_rows if row['role'] == 'flask']
        assert len(set(instances)) == 2
        for row in worker_rows:
            assert Path(f"/proc/{row['pid']}").exists() is (row['generation'] == 1)
        process.send_signal(signal.SIGTERM)
        _, stderr = process.communicate(timeout=10)
        assert process.returncode == 0, stderr
        assert stderr.count('re-executing WAMF') == 1
        assert 'MQTT subprocess exited unexpectedly; restarting' not in stderr
        assert len([row for row in rows() if row['kind'] == 'parent']) == 2
        assert all(not Path(f"/proc/{row['pid']}").exists() for row in worker_rows)
    finally:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=3)


@pytest.mark.parametrize('scenario', ['success', 'lost-response', 'rejected', 'timeout', 'login'])
def test_browser_restart_waits_for_new_instance_without_reposting(scenario):
    import shutil
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node is needed to execute the browser restart regression')
    script = (Path(__file__).resolve().parent.parent / 'templates/admin_config.html').read_text().split('<script>')[1].split('</script>')[0]
    script = script.replace('{{ config_content|tojson }}', '"config"').replace('{{ csrf_token|tojson }}', '"csrf"').replace('{{ restart_instance_id|tojson }}', '"original"')
    script = script.replace("          document\n              .getElementById('restart-btn')", "          globalThis.restartAction = postConfigAction;\n          document\n              .getElementById('restart-btn')")
    harness = r'''
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
const scenario = process.argv[1];
const elements = {};
globalThis.document = {getElementById: id => elements[id] ||= {style: {}, addEventListener() {}}};
let now = 0, posts = 0, polls = 0, reloads = 0, assigned = '';
globalThis.window = {location: {reload() {reloads++;}, assign(url) {assigned = url;}}};
Date.now = () => now;
globalThis.setTimeout = (callback, delay) => { if (delay === 1000) {now += delay; queueMicrotask(callback);} return 1; };
globalThis.clearTimeout = () => {};
globalThis.monaco = {editor: {create: () => ({getValue: () => 'config'})}};
const browserRequire = (deps, callback) => callback();
browserRequire.config = () => {};
globalThis.require = browserRequire;
globalThis.fetch = async (url, options) => {
  if (options.method === 'POST') {
    posts++;
    if (scenario === 'lost-response') throw new Error('connection closed');
    return {ok: scenario !== 'rejected', json: async () => ({success: scenario !== 'rejected', error: 'Invalid config'})};
  }
  polls++;
  assert.strictEqual(options.cache, 'no-store');
  if (scenario === 'login') return {redirected: true, url: '/login'};
  return {ok: true, json: async () => ({instance_id: scenario === 'timeout' || polls < 3 ? 'original' : 'replacement'})};
};
vm.runInThisContext(fs.readFileSync(0, 'utf8'));
(async () => {
  await globalThis.restartAction('/admin/config/restart', false);
  assert.strictEqual(posts, 1);
  if (scenario === 'success' || scenario === 'lost-response') {
    assert.strictEqual(polls, 3);
    assert.strictEqual(reloads, 1);
  } else if (scenario === 'rejected') {
    assert.strictEqual(polls, 0);
    assert.strictEqual(elements['config-message'].textContent, 'Invalid config');
    assert.strictEqual(elements['restart-btn'].disabled, false);
  } else if (scenario === 'timeout') {
    assert.strictEqual(reloads, 0);
    assert.ok(elements['config-message'].textContent.includes('not reconnected'));
    assert.strictEqual(elements['restart-btn'].disabled, false);
  } else {
    assert.strictEqual(assigned, '/login');
    assert.strictEqual(reloads, 0);
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
'''
    result = subprocess.run([node, '-e', harness, scenario], input=script, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
